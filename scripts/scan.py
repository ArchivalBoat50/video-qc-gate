#!/usr/bin/env python3
"""
Deterministic pre-QC. Runs BEFORE Gemini so we don't spend a model call
on clips that fail an objective check.

Emits a JSON array on stdout, one object per *processable* clip in
<PIPE_DATA>/inbox.

Files that cannot be read yet — still downloading, zero bytes, no decodable
video stream — are SKIPPED and reported on stderr, never rejected. "Can't read
it yet" is not "bad clip": rejecting would move a half-written file out of the
inbox and log a defect that isn't real. Skipped files stay put and get picked
up on the next run.
"""
import json, os, re, subprocess, sys, base64, time

# The workflow interpolates $json.path into a shell command wrapped in double
# quotes, so a filename containing any of these could break out of the quoting
# and execute arbitrary commands. Such files are skipped with a rename hint
# rather than being passed on. Closing this at the source means nothing
# downstream has to trust the filename.
UNSAFE_IN_NAME = re.compile(r'''["'`$\\;&|<>\n\r\t]''')

# --- stage config ---------------------------------------------------------
# There are two QC checkpoints with different objective gates: "stock" (raw
# Higgsfield B-roll, 5-9s) and "reel" (finished captioned reels, up to 45s so
# long-form output is not rejected before a model ever sees it). Gates live in
# qc_config.json so this script and pipeline_qc.py and the n8n workflow cannot
# drift apart. If the config is missing, the legacy 5-9s behaviour is used.
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("PIPE_CONFIG", os.path.join(HERE, "qc_config.json"))

# The stage must be requested EXPLICITLY. Defaulting to "stock" here caused a
# real incident: the pre-existing hourly workflow calls plain `python3
# /scripts/scan.py`, picked up the stock stage's data_root, and scanned
# /data/stock/inbox — while its move.sh still defaulted to /data, so it filed
# every clip into the OLD tree using the OLD reel rubric. Scan read one tree,
# moves wrote another. With no stage given we now reproduce the legacy
# behaviour exactly (root /data, 5-9s), so an un-migrated caller is unaffected.
STAGE = os.environ.get("PIPE_STAGE")
if "--stage" in sys.argv:
    STAGE = sys.argv[sys.argv.index("--stage") + 1]

_LEGACY = {"data_root": "/data", "duration_min": 5.0, "duration_max": 9.0,
           "min_aspect": 1.6, "require_silent": True}
try:
    if STAGE is None:
        raise KeyError("no stage requested; using legacy defaults")
    with open(CONFIG_PATH) as _fh:
        _CFG = json.load(_fh)
    GATES = _CFG["stages"][STAGE]
    SETTLE_DEFAULT = str(_CFG.get("settle_seconds", 20))
    KEEP_DEFAULT = str(_CFG.get("work_keep_days", 14))
    SCENE_THRESHOLD = float(_CFG.get("scene_threshold", 0.4))
    _SHEET = _CFG.get("sheet", {})
except (OSError, KeyError, ValueError):
    GATES, SETTLE_DEFAULT, KEEP_DEFAULT = _LEGACY, "20", "14"
    SCENE_THRESHOLD, _SHEET = 0.4, {}

SHEET_FRAMES = int(_SHEET.get("frames", 8))
SHEET_TILE = _SHEET.get("tile", "4x2")
SHEET_WIDTH = int(_SHEET.get("scale_width", 300))

# Root is configurable so this runs identically under Docker (PIPE_DATA=/data/stock,
# set by the n8n command) or natively on macOS. PIPE_DATA still wins so existing
# invocations keep working. A relative data_root in the config resolves against
# the project root, so the same config serves both without a native run trying
# to create /data at the filesystem root.
ROOT = os.environ.get("PIPE_DATA") or GATES.get("data_root", "/data")
if not os.path.isabs(ROOT):
    ROOT = os.path.normpath(os.path.join(HERE, "..", ROOT))
INBOX = os.path.join(ROOT, "inbox")
WORK  = os.path.join(ROOT, "work")

# A file must be untouched this long before we trust it is fully written.
SETTLE_SECONDS = float(os.environ.get("PIPE_SETTLE_SECONDS", SETTLE_DEFAULT))
# Contact sheets are kept for spot-checking, but not forever.
WORK_KEEP_DAYS = float(os.environ.get("PIPE_WORK_KEEP_DAYS", KEEP_DEFAULT))

# Native n8n launches with a minimal PATH and will not find Homebrew ffmpeg.
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def probe(path, stream, fields):
    r = sh(f'ffprobe -v error -select_streams {stream} '
           f'-show_entries stream={fields} -of json "{path}"')
    try:
        return json.loads(r.stdout).get("streams", [])
    except Exception:
        return []


def format_duration(path):
    """Container-level duration. Some mp4s carry no per-stream duration; without
    this fallback they'd read as 0.0s and be rejected as out-of-range."""
    r = sh(f'ffprobe -v error -show_entries format=duration -of json "{path}"')
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def scene_cuts(path, threshold=None):
    """Count hard cuts. Kling sometimes invents shot changes inside a 7s clip."""
    if threshold is None:
        threshold = SCENE_THRESHOLD
    r = sh(f'ffmpeg -i "{path}" -filter:v "select=\'gt(scene,{threshold})\',showinfo" '
           f'-f null - 2>&1')
    log = r.stdout + r.stderr
    # count only emitted FRAMES. "Parsed_showinfo" also matches ffmpeg's two
    # config lines, which would report a false cut on every clip.
    return sum(1 for ln in log.splitlines()
               if "Parsed_showinfo" in ln and "pts_time:" in ln)


def frame_count(vs, dur):
    """Frames in the clip. nb_frames is absent on fragmented mp4, so fall back
    to duration x avg_frame_rate rather than assuming a fixed clip length."""
    try:
        n = int(vs.get("nb_frames"))
        if n > 0:
            return n
    except (TypeError, ValueError):
        pass
    try:
        num, _, den = str(vs.get("avg_frame_rate", "")).partition("/")
        fps = float(num) / float(den or 1)
        if fps > 0 and dur > 0:
            return max(1, int(round(dur * fps)))
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return 0


def contact_sheet(path, out, n, dur, frames=None):
    """One tiled JPEG of N evenly-spaced frames — this is what Gemini sees.
    Geometry comes from qc_config.json so both runners send identical evidence."""
    if frames is None:
        frames = SHEET_FRAMES
    if n > 0:
        step = max(1, n // frames)
        sel = f"select='not(mod(n\\,{step}))'"
    elif dur > 0:
        # no reliable frame count: sample by time instead
        sel = f"fps={frames}/{dur}"
    else:
        return False
    r = sh(f'ffmpeg -y -v error -i "{path}" '
           f'-vf "{sel},scale={SHEET_WIDTH}:-1,tile={SHEET_TILE}" -frames:v 1 "{out}"')
    return os.path.exists(out)


def prune_work():
    """Contact sheets accumulate forever otherwise."""
    if WORK_KEEP_DAYS <= 0:
        return
    cutoff = time.time() - WORK_KEEP_DAYS * 86400
    for fn in os.listdir(WORK):
        if not fn.endswith("_qc.jpg"):
            continue
        p = os.path.join(WORK, fn)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


def collect():
    """Scan the inbox and return one dict per processable clip.

    Split out of main() so pipeline_qc.py can reuse the exact same objective
    gates rather than reimplementing them. main() is unchanged from the n8n
    caller's point of view: it still prints the JSON array on stdout."""
    os.makedirs(WORK, exist_ok=True)
    prune_work()

    out, skipped = [], []
    now = time.time()

    for fn in sorted(os.listdir(INBOX)):
        if not fn.lower().endswith((".mp4", ".mov")):
            continue
        path = os.path.join(INBOX, fn)
        stem = os.path.splitext(fn)[0]
        sheet = os.path.join(WORK, stem + "_qc.jpg")

        if UNSAFE_IN_NAME.search(fn):
            skipped.append(f"{fn}: filename contains shell metacharacters; "
                           f"rename it (letters, digits, space . _ - are safe)")
            continue

        # --- readability guards: skip, do not reject -----------------------
        try:
            st = os.stat(path)
        except OSError as e:
            skipped.append(f"{fn}: cannot stat ({e})")
            continue
        if st.st_size == 0:
            skipped.append(f"{fn}: zero bytes")
            continue
        if not os.access(path, os.R_OK):
            skipped.append(f"{fn}: not readable (chmod 644 it)")
            continue
        if now - st.st_mtime < SETTLE_SECONDS:
            skipped.append(f"{fn}: modified {now - st.st_mtime:.0f}s ago, "
                           f"still settling (<{SETTLE_SECONDS:.0f}s)")
            continue

        v = probe(path, "v:0", "width,height,duration,nb_frames,avg_frame_rate")
        if not v:
            skipped.append(f"{fn}: no decodable video stream (incomplete file?)")
            continue
        vs = v[0]

        a = probe(path, "a:0", "codec_name")
        dur = float(vs.get("duration", 0) or 0) or format_duration(path)
        w, h = int(vs.get("width", 0) or 0), int(vs.get("height", 0) or 0)
        if dur <= 0 and not (w and h):
            skipped.append(f"{fn}: unreadable dimensions and duration")
            continue

        cuts = scene_cuts(path)
        has_audio = len(a) > 0

        dmin = float(GATES.get("duration_min", 5.0))
        dmax = float(GATES.get("duration_max", 9.0))
        min_aspect = float(GATES.get("min_aspect", 1.6))

        fails = []
        if cuts > 0:
            fails.append(f"{cuts} hard cut(s) detected inside the clip")
        if has_audio and GATES.get("require_silent", True):
            fails.append("baked-in audio track present")
        if not (dmin <= dur <= dmax):
            fails.append(f"duration {dur:.1f}s outside {dmin:g}-{dmax:g}s")
        if w and h and (h / w) < min_aspect:
            fails.append(f"not vertical ({w}x{h})")

        sheet_b64 = ""
        if not fails and contact_sheet(path, sheet, frame_count(vs, dur), dur):
            with open(sheet, "rb") as fh:
                sheet_b64 = base64.b64encode(fh.read()).decode()

        out.append({
            "file": fn,
            "path": path,
            "width": w, "height": h, "duration": round(dur, 2),
            "cuts": cuts, "has_audio": has_audio,
            "hard_fail": bool(fails),
            "hard_fail_reasons": fails,
            # The mp4 itself is what Gemini now sees, but it is deliberately NOT
            # base64'd here: this dict is printed as JSON on stdout for the n8n
            # caller, and 24 clips x ~40MB of base64 would be an unusable
            # payload. pipeline_qc.py reads the file straight off disk; the size
            # is all it needs to decide inline-vs-fallback before doing so.
            "size_bytes": st.st_size,
            # Still built, still kept in work/ for human spot-checking, and
            # still the fallback for clips too large to inline.
            "sheet_path": sheet if sheet_b64 else "",
            "sheet_b64": sheet_b64,
        })

    if skipped:
        print(f"scan.py: skipped {len(skipped)} file(s), left in inbox:",
              file=sys.stderr)
        for s in skipped:
            print(f"  - {s}", file=sys.stderr)

    return out


def main():
    print(json.dumps(collect()))


if __name__ == "__main__":
    main()
