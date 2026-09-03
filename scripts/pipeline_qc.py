#!/usr/bin/env python3
"""
Python peer of workflow_qc.json. Same two gates, same rubrics, same config.

  python3 pipeline_qc.py --stage stock
  python3 pipeline_qc.py --stage reel

Two QC checkpoints:
  stock — raw generated clips. Defects + realism, judged against a REFERENCE
          image of the persona so "this is the wrong woman" is detectable.
  reel  — finished captioned reels. Full craft rubric + caption legibility.

Flow, identical to the n8n chain:
  scan (objective gates) -> hard fail? -> reject without a model call
                         -> Gemini -> parse verdict -> approve / review / reject

What the model sees is the WHOLE CLIP, inline, not an 8-frame contact sheet
(qc_config.json -> "media"). Sheets are still written to work/ for human
spot-checking and are still the fallback above the inline upload limit.

Design rules inherited from the n8n version, do not "simplify" these away:
  * Unreadable is not bad. Zero-byte, still-settling or undecodable files are
    SKIPPED and left in the inbox, never rejected.
  * A failed or unparseable model call becomes "review", never "rejected".
    review/ means nobody actually looked at it; rejected/ means a real defect.
  * The model controls part of the log line, so every reason goes through
    shell_safe() before it is written or passed anywhere.
  * Moves never clobber. Collisions get _1, _2 and the log records the name the
    file actually landed under.
  * Throttled to config rpm. Gemini free tier is 5-15 RPM.
"""
import argparse, base64, json, os, re, shutil, sys, time
import urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DEFAULT_CONFIG = os.path.join(HERE, "qc_config.json")


# --------------------------------------------------------------------------
# helpers

def shell_safe(s, limit=200):
    """Strip anything that could break out of a shell command or a TSV row.

    The model controls defects/note. A reply containing '"; rm -rf /data; #'
    would otherwise be interpolated into a command. Also collapses newlines and
    tabs, which would corrupt the TSV log, and caps length so one rambling note
    cannot blow out a line."""
    s = "" if s is None else str(s)
    s = re.sub(r"[\r\n\t]+", " ", s)
    s = re.sub(r"[\"'`$\\;&|<>]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] or "no detail"


def load_config(path):
    with open(path) as fh:
        return json.load(fh)


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def b64_file(path):
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()


def move(src, dest_dir, reason, log_name="_log.tsv"):
    """Never clobber. Mirrors move.sh, including the _1/_2 suffix behaviour and
    logging the name the file actually landed under."""
    os.makedirs(dest_dir, exist_ok=True)
    base = os.path.basename(src)
    target = os.path.join(dest_dir, base)
    if os.path.exists(target):
        stem, ext = os.path.splitext(base)
        n = 1
        while os.path.exists(os.path.join(dest_dir, f"{stem}_{n}{ext}")):
            n += 1
        target = os.path.join(dest_dir, f"{stem}_{n}{ext}")
    os.rename(src, target)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with open(os.path.join(dest_dir, log_name), "a", encoding="utf-8") as fh:
        fh.write(f"{stamp}\t{os.path.basename(target)}\t{shell_safe(reason)}\n")
    return os.path.basename(target)


# --------------------------------------------------------------------------
# what the model looks at

# The rubrics were written around a contact sheet and still describe one, both
# because the n8n chain still sends sheets and because the sheet is our
# over-the-limit fallback. When the whole video goes instead, this is appended
# after the rubric — last thing before the media, so it is read as the override
# it is.
VIDEO_NOTICE = """\
## MEDIA NOTE — this overrides how the rubric above describes the input
You are given the ENTIRE VIDEO, not a contact sheet of sampled frames.
Wherever the rubric says "contact sheet", "the 8 frames", "across the frames"
or "between frames", read it as "anywhere in the clip".

Judge continuity across the WHOLE clip, not a handful of stills. A defect
lasting a fraction of a second is still a defect. When you report one, say
roughly WHEN it happens, in seconds. Do not mark a defect you cannot point to a
moment for.

DO NOT let this note tell you what to look for. The defect list in the rubric
above is the whole list; this note changes only the medium and the requirement
to timestamp. Naming example defects here would prime you to report them, so
none are given."""

MIME_BY_EXT = {".mp4": "video/mp4", ".mov": "video/quicktime",
               ".m4v": "video/mp4", ".webm": "video/webm"}

_RESOLUTION_ENUM = {
    "low": "MEDIA_RESOLUTION_LOW",
    "medium": "MEDIA_RESOLUTION_MEDIUM",
    "high": "MEDIA_RESOLUTION_HIGH",
}


def media_settings(cfg, gates):
    """Global media block, with any per-stage overrides merged over it."""
    m = dict(cfg.get("media") or {})
    m.update(gates.get("media") or {})
    return m


def build_media(cfg, gates, clip):
    """Decide what evidence this clip is graded on, and load it.

    Returns a dict describing one inline_data part: kind, mime_type, data
    (base64), label. kind 'none' means we could not assemble any evidence — the
    caller must route that to review/, never to rejected/, because nobody
    actually looked at the clip.

    Video is the default. The sheet survives as the fallback for clips too
    large to inline, and is written to work/ either way.
    """
    m = media_settings(cfg, gates)
    mode = str(m.get("mode", "video")).lower()
    sheet_b64 = clip.get("sheet_b64") or ""
    sheet = {"kind": "sheet", "mime_type": "image/jpeg", "data": sheet_b64,
             "label": "CONTACT SHEET (the clip under review):"}

    if mode != "video":
        if sheet_b64:
            return sheet
        return {"kind": "none", "data": "",
                "reason": "no contact sheet could be built"}

    path = clip["path"]
    size = clip.get("size_bytes")
    if size is None:
        try:
            size = os.path.getsize(path)
        except OSError as e:
            size = None
            if not sheet_b64:
                return {"kind": "none", "data": "",
                        "reason": f"cannot read clip ({e})"}

    # The cap applies to the encoded request, and base64 inflates by 4/3.
    # Comparing the raw file size would let a 90MB clip through and then eat a
    # 413 from the API.
    limit = int(m.get("inline_max_bytes", 100 * 1024 * 1024))
    if size is not None and size * 4 // 3 > limit:
        if str(m.get("fallback", "sheet")).lower() == "sheet" and sheet_b64:
            return {**sheet, "fell_back":
                    f"{size / 1e6:.0f}MB exceeds the inline limit"}
        return {"kind": "none", "data": "",
                "reason": f"{size / 1e6:.0f}MB exceeds the inline limit and "
                          f"no sheet fallback is available"}

    try:
        data = b64_file(path)
    except OSError as e:
        if sheet_b64:
            return {**sheet, "fell_back": f"could not read the mp4 ({e})"}
        return {"kind": "none", "data": "", "reason": f"cannot read clip ({e})"}

    fps = m.get("fps")
    return {"kind": "video",
            "mime_type": MIME_BY_EXT.get(os.path.splitext(path)[1].lower(),
                                         "video/mp4"),
            "data": data,
            "label": "VIDEO (the clip under review, in full):",
            "fps": float(fps) if fps else None,
            "bytes": size}


def build_body(cfg, gates, prompt, media, reference_b64=None):
    """Assemble the request body. Split out of call_gemini so the exact payload
    can be asserted offline — the one thing the test suite could never see when
    the body was built inline inside the network call."""
    parts = [{"text": prompt}]
    if reference_b64:
        parts.append({"text": "REFERENCE IMAGE (the persona):"})
        parts.append({"inline_data": {"mime_type": "image/jpeg",
                                      "data": reference_b64}})
    parts.append({"text": media["label"]})

    if media["kind"] == "file":
        # Already uploaded via the Files API — referenced by URI, not resent.
        part = {"file_data": {"mime_type": media["mime_type"],
                              "file_uri": media["file_uri"]}}
    else:
        part = {"inline_data": {"mime_type": media["mime_type"],
                                "data": media["data"]}}
    if media["kind"] in ("video", "file") and media.get("fps"):
        # Gemini samples video at 1 fps unless told otherwise. On a 7s clip
        # that is 7 frames — we would have swapped an 8-frame sheet for a
        # 7-frame one and changed nothing.
        part["video_metadata"] = {"fps": media["fps"]}
    parts.append(part)

    gen = {"temperature": 0, "responseMimeType": "application/json"}
    res = str(media_settings(cfg, gates).get("media_resolution", "default"))
    if res.upper().startswith("MEDIA_RESOLUTION_"):
        gen["mediaResolution"] = res.upper()
    elif res.lower() in _RESOLUTION_ENUM:
        gen["mediaResolution"] = _RESOLUTION_ENUM[res.lower()]
    # "default"/anything else: omit the field and let the API choose.

    return {"contents": [{"parts": parts}], "generationConfig": gen}


def upload_file(cfg, api_key, path, mime_type, timeout=None, poll_seconds=2.0,
                max_wait=300.0):
    """Upload via the Files API and return {'uri':…, 'name':…}, or {'error':…}.

    Only stage 0 needs this: 100MB inline covers our own 7s clips several times
    over, but a reference is somebody else's post and can be minutes long. The
    upload is the resumable protocol (start -> upload+finalize), then a poll,
    because a video is not usable the instant the bytes land — it sits in
    PROCESSING while Google decodes it, and referencing it too early fails.
    """
    timeout = timeout or float(cfg.get("timeout_seconds", 300))
    base = cfg.get("files_endpoint",
                   "https://generativelanguage.googleapis.com")
    size = os.path.getsize(path)

    try:
        start = urllib.request.Request(
            f"{base}/upload/v1beta/files",
            data=json.dumps({"file": {"display_name":
                                      os.path.basename(path)}}).encode(),
            headers={"x-goog-api-key": api_key,
                     "X-Goog-Upload-Protocol": "resumable",
                     "X-Goog-Upload-Command": "start",
                     "X-Goog-Upload-Header-Content-Length": str(size),
                     "X-Goog-Upload-Header-Content-Type": mime_type,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(start, timeout=timeout) as r:
            upload_url = r.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            return {"error": "files API gave no upload URL"}

        with open(path, "rb") as fh:
            put = urllib.request.Request(
                upload_url, data=fh.read(),
                headers={"Content-Length": str(size),
                         "X-Goog-Upload-Offset": "0",
                         "X-Goog-Upload-Command": "upload, finalize"})
            with urllib.request.urlopen(put, timeout=timeout) as r:
                info = json.loads(r.read().decode()).get("file", {})
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        m = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', detail)
        return {"error": f"files API {e.code}: "
                         f"{m.group(1) if m else detail[:200]}"}
    except Exception as e:
        return {"error": f"files API {type(e).__name__}: {e}"}

    name, uri, state = info.get("name"), info.get("uri"), info.get("state")
    waited = 0.0
    while state == "PROCESSING" and waited < max_wait:
        time.sleep(poll_seconds)
        waited += poll_seconds
        try:
            q = urllib.request.Request(f"{base}/v1beta/{name}",
                                       headers={"x-goog-api-key": api_key})
            with urllib.request.urlopen(q, timeout=timeout) as r:
                info = json.loads(r.read().decode())
            state, uri = info.get("state"), info.get("uri", uri)
        except Exception as e:
            return {"error": f"files API poll failed: {type(e).__name__}: {e}"}

    if state != "ACTIVE":
        return {"error": f"uploaded file is {state or 'in an unknown state'} "
                         f"after {waited:.0f}s, not ACTIVE"}
    return {"uri": uri, "name": name}


# --------------------------------------------------------------------------
# model call

def call_gemini(cfg, gates, api_key, rubric, media, reference_b64=None,
                mock=None, timeout=None, notice=VIDEO_NOTICE):
    """Return the parsed reply dict, or {'error': msg}.

    Parts are sent in a deliberate order: REFERENCE first, then the clip. The
    stock rubric depends on that order to answer "is this even the right
    person" — a clip can be perfectly self-consistent and still be the wrong
    woman, which no single-media call can detect.

    `notice` is appended only when video is actually sent. the describer stage
    passes "" — its prompt is written for video from the start and has no
    contact-sheet wording to override.
    """
    prompt = rubric
    if media["kind"] in ("video", "file") and notice:
        prompt = rubric.rstrip() + "\n\n" + notice

    if mock is not None:
        return mock(prompt, media, reference_b64)

    body = json.dumps(build_body(cfg, gates, prompt, media,
                                 reference_b64)).encode()

    url = cfg["endpoint"].format(model=cfg["model"])
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key})

    tries = int(cfg.get("max_tries", 3))
    timeout = timeout or float(cfg.get("timeout_seconds", 120))
    last = "unknown error"
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            m = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', detail)
            last = f"{e.code}: {m.group(1) if m else detail[:200]}"
            # 400 (bad key) will never succeed on retry.
            if e.code not in (429, 500, 502, 503, 504):
                break
            if e.code == 429:
                # A *daily* cap does not clear by waiting. If we already served
                # the stated delay once and got 429 again, this is not a
                # per-minute burst - stop retrying and let the caller abort the
                # run instead of grinding for an hour on a quota that will not
                # reset until tomorrow.
                if attempt > 1:
                    last += "  [quota looks daily, not per-minute]"
                    break
                # Google tells us exactly how long to wait ("Please retry in
                # 56.8s"). Retrying on a 2s/4s exponential instead fires three
                # requests inside six seconds and pushes us FURTHER over a
                # per-minute quota - each failure made the next one likelier.
                # Honour the stated delay; it is the only value that works.
                d = re.search(r"retry in ([\d.]+)s", detail)
                wait = float(d.group(1)) + 2 if d else 60.0
                if attempt < tries:
                    print(f"     rate limited — waiting {wait:.0f}s "
                          f"(attempt {attempt}/{tries})", flush=True)
                    time.sleep(wait)
                continue
        except Exception as e:                      # timeouts, DNS, TLS
            last = f"{type(e).__name__}: {e}"
        if attempt < tries:
            time.sleep(2 ** attempt)
    return {"error": last}


def parse_verdict(reply):
    """Mirror of the n8n 'Parse verdict' node.

    A failed or unparseable call becomes 'review' with the reason attached, so
    a throttled clip is never filed as a real defect and never strands the batch.
    """
    out = {"verdict": "review", "confidence": 0.0, "defects": [],
           "note": "unparseable model reply"}

    if isinstance(reply, dict) and reply.get("error"):
        return {**out, "note": "gemini call failed: " + str(reply["error"])}

    try:
        text = reply["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            out = {**out, **parsed}
    except Exception:
        return out

    d = out.get("defects")
    out["defects"] = d if isinstance(d, list) else ([str(d)] if d else [])
    if out.get("verdict") not in ("pass", "review", "fail"):
        out["verdict"] = "review"
    return out


# --------------------------------------------------------------------------
# main

def explain(clip, v, reply, work_dir, media=None):
    """Show the model's full reasoning for one clip, and keep the raw reply.

    The verdict alone hides why. This prints every field the rubric asks for --
    identity match, realism sub-scores, craft -- next to what the
    model actually looked at, so a surprising verdict can be argued with rather
    than just accepted."""
    print(f"\n  ── {clip['file']} " + "─" * max(0, 56 - len(clip['file'])))
    if media:
        sent = media["kind"]
        if media.get("kind") == "video":
            sent += f", {media.get('bytes', 0) / 1e6:.0f}MB"
            sent += f", {media['fps']:g} fps" if media.get("fps") else \
                    ", API default fps"
        if media.get("fell_back"):
            sent += f"  (fell back: {media['fell_back']})"
        print(f"     sent to model : {sent}")
    print(f"     sheet in work : {clip.get('sheet_path') or '(none)'}")
    print(f"     verdict       : {v.get('verdict')}  conf={v.get('confidence')}")
    for key, label in (("identity", "identity"), ("realism", "realism"),
                       ("caption", "caption"), ("craft", "craft")):
        if isinstance(v.get(key), dict):
            bits = ", ".join(f"{k}={json.dumps(val)}" for k, val in v[key].items())
            print(f"     {label:<13} : {bits}")
    if v.get("defects"):
        print(f"     defects       :")
        for d in v["defects"]:
            print(f"                     - {d}")
    if v.get("note"):
        print(f"     note          : {v['note']}")

    os.makedirs(work_dir, exist_ok=True)
    out = os.path.join(work_dir,
                       os.path.splitext(clip["file"])[0] + "_reply.json")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"clip": clip["file"], "parsed": v, "raw": reply},
                      fh, indent=2)
        print(f"     raw reply     : {out}")
    except OSError as e:
        print(f"     raw reply     : could not write ({e})")


def preview_prompt(stage, cfg_path=DEFAULT_CONFIG, model=None):
    """Print exactly what gets sent, without sending it or needing a key."""
    cfg = load_config(cfg_path)
    if model:
        cfg["model"] = model
    gates = cfg["stages"][stage]
    rubric = read_text(os.path.join(HERE, gates["rubric"]))
    pack = cfg.get("context_pack")
    if pack and os.path.exists(os.path.join(HERE, pack)):
        rubric = read_text(os.path.join(HERE, pack)) + "\n\n" + rubric
    ref = gates.get("reference_image")

    m = media_settings(cfg, gates)
    mode = str(m.get("mode", "video")).lower()
    video = mode == "video"
    if video:
        rubric = rubric.rstrip() + "\n\n" + VIDEO_NOTICE
        fps = f"{float(m['fps']):g} fps" if m.get("fps") else "API default 1 fps"
        desc = (f"the clip itself, inline as video/mp4 — {fps}, "
                f"media_resolution={m.get('media_resolution', 'default')}, "
                f"falls back to the contact sheet over "
                f"{int(m.get('inline_max_bytes', 104857600)) / 1e6:.0f}MB")
    else:
        desc = "the clip's 8-frame contact sheet"
    kind = "VIDEO (the clip under review, in full):" if video else \
           "CONTACT SHEET (the clip under review):"

    print(f"stage={stage}  model={cfg['model']}  temperature=0  "
          f"responseMimeType=application/json")
    print(f"media  mode={mode}  (sheets are still written to work/ either way)")
    print(f"parts sent, in order:")
    print(f"  1. text  — rubric + context pack"
          f"{' + media note' if video else ''} ({len(rubric)} chars)")
    if ref:
        print(f"  2. text  — 'REFERENCE IMAGE (the persona):'")
        print(f"  3. image — {ref}")
        print(f"  4. text  — '{kind}'")
        print(f"  5. {'video' if video else 'image'} — {desc}")
    else:
        print(f"  2. text  — '{kind}'")
        print(f"  3. {'video' if video else 'image'} — {desc}")
    print("\n" + "=" * 72 + "\n" + rubric)


def run(stage, cfg_path=DEFAULT_CONFIG, data_root=None, limit=None,
        dry_run=False, mock=None, quiet=False, show=False, model=None):
    cfg = load_config(cfg_path)
    if model:
        cfg["model"] = model
    if stage not in cfg["stages"]:
        raise SystemExit(f"unknown stage {stage!r}; have {list(cfg['stages'])}")
    gates = cfg["stages"][stage]

    root = data_root or os.environ.get("PIPE_DATA") or gates["data_root"]
    # A relative data_root resolves against the project root so the same config
    # works natively and under docker (where n8n passes an absolute PIPE_DATA).
    if not os.path.isabs(root):
        root = os.path.normpath(os.path.join(HERE, "..", root))
    os.environ["PIPE_DATA"] = root
    os.environ["PIPE_STAGE"] = stage
    os.environ["PIPE_CONFIG"] = cfg_path

    # scan.py resolves PIPE_DATA/PIPE_STAGE/PIPE_CONFIG into module-level
    # constants at import time. Python caches modules, so a second run() in the
    # same process would silently keep the FIRST stage's inbox and gates —
    # stock clips graded with reel gates, or written to the wrong tree. Reload
    # so the constants are recomputed against the env we just set.
    import importlib, scan
    importlib.reload(scan)
    clips = scan.collect()

    rubric = read_text(os.path.join(HERE, gates["rubric"]))
    pack = cfg.get("context_pack")
    if pack and os.path.exists(os.path.join(HERE, pack)):
        rubric = read_text(os.path.join(HERE, pack)) + "\n\n" + rubric

    reference_b64 = None
    ref = gates.get("reference_image")
    if ref:
        ref_path = os.path.join(HERE, ref)
        if os.path.exists(ref_path):
            reference_b64 = b64_file(ref_path)
        else:
            print(f"WARNING: reference image {ref_path} missing — identity "
                  f"cannot be checked against a reference this run",
                  file=sys.stderr)

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key and not dry_run and mock is None:
        raise SystemExit("GEMINI_API_KEY not set (it lives in .env, never in a "
                         "tracked file)")

    interval = 60.0 / float(cfg.get("rpm", 12) or 12)
    counts = {"approved": 0, "rejected": 0, "review": 0, "skipped": 0}
    quota_strikes = 0
    results = []

    if limit:
        clips = clips[:limit]

    for i, clip in enumerate(clips):
        name, path = clip["file"], clip["path"]

        # --- gate 1: objective. No model call is spent on these. -----------
        if clip["hard_fail"]:
            reason = "; ".join(clip["hard_fail_reasons"])
            if not dry_run:
                move(path, os.path.join(root, "rejected"), reason)
            counts["rejected"] += 1
            results.append({"file": name, "dest": "rejected",
                            "verdict": "objective", "reason": reason})
            if not quiet:
                print(f"  REJECT (objective)  {name}  — {reason}")
            continue

        if dry_run:
            counts["skipped"] += 1
            results.append({"file": name, "dest": "(dry-run)",
                            "verdict": "gates-passed", "reason": ""})
            if not quiet:
                print(f"  gates passed        {name}  (dry run, no model call)")
            continue

        # --- gate 2: model -------------------------------------------------
        media = build_media(cfg, gates, clip)
        if media["kind"] == "none":
            # No evidence to grade on. review/, not rejected/ — the clip may be
            # perfectly fine; we simply never looked at it.
            reason = shell_safe("review: " + media["reason"])
            landed = move(path, os.path.join(root, "review"), reason)
            counts["review"] += 1
            results.append({"file": landed, "dest": "review",
                            "verdict": "review", "media": "none",
                            "reason": reason})
            if not quiet:
                print(f"  REVIEW    {landed}  — {reason}")
            continue

        # Only throttle real network calls. Sleeping between mocked calls made
        # the offline test suite take minutes and time out for no reason.
        if i and mock is None:
            time.sleep(interval)
        reply = call_gemini(cfg, gates, api_key, rubric, media,
                            reference_b64, mock=mock)
        v = parse_verdict(reply)

        # Abort rather than grind. Two clips in a row killed by a quota that
        # does not clear on retry means the daily allowance is gone; continuing
        # would spend ~2 minutes per clip to write "review" 20 more times and
        # bury the real verdicts already on disk.
        if isinstance(reply, dict) and "quota looks daily" in str(reply.get("error", "")):
            quota_strikes += 1
            if quota_strikes >= 2:
                print(f"\n  ABORTING: daily quota for {cfg['model']} is "
                      f"exhausted — waiting will not clear it.")
                print(f"  {len(clips) - i} clip(s) left untouched in inbox/. "
                      f"Options: wait for the quota to reset, run with "
                      f"--model gemini-3.1-flash-lite, or enable billing.")
                break
        else:
            quota_strikes = 0

        detail = "; ".join(v["defects"]) if v["defects"] else v.get("note", "")
        reason = shell_safe(f"{v['verdict']}: {detail}")

        if v["verdict"] == "pass":
            dest = "approved"
        elif v["verdict"] == "fail":
            dest = "rejected"
        else:
            dest = "review"

        if show:
            explain(clip, v, reply, os.path.join(root, "work"), media)

        landed = move(path, os.path.join(root, dest), reason)
        counts[dest] += 1
        results.append({"file": landed, "dest": dest,
                        "verdict": v["verdict"],
                        "confidence": v.get("confidence"),
                        "media": media["kind"],
                        "reason": reason})
        if not quiet:
            conf = v.get("confidence")
            conf = f" conf={conf}" if isinstance(conf, (int, float)) else ""
            print(f"  {dest.upper():<8}  {landed}{conf}  — {reason}")

    if not quiet:
        print(f"\nstage={stage} root={root}")
        print(f"  approved={counts['approved']} rejected={counts['rejected']} "
              f"review={counts['review']}")
        left = [f for f in os.listdir(os.path.join(root, "inbox"))
                if f.lower().endswith((".mp4", ".mov"))]
        if left and limit:
            # Not skipped - just outside --limit. Saying "skipped" here sent the
            # reader hunting stderr for a problem that does not exist.
            print(f"  {len(left)} file(s) left in inbox (not reached, --limit "
                  f"{limit}). Rerun without --limit to process them.")
        elif left:
            # Project rule: verify a run by counting files, not by trusting a
            # success message. Anything left behind here really was skipped.
            print(f"  {len(left)} file(s) still in inbox (skipped, see stderr): "
                  f"{', '.join(sorted(left)[:5])}")
    return results, counts


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", required=True, choices=["stock", "reel"])
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--data-root", default=None,
                    help="override the stage's data_root")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="run the objective gates only; no model call, no moves")
    ap.add_argument("--explain", action="store_true",
                    help="print the model's full reasoning per clip and save "
                         "each raw reply to <root>/work/<clip>_reply.json")
    ap.add_argument("--show-prompt", action="store_true",
                    help="print exactly what would be sent, then exit "
                         "(no API key needed)")
    ap.add_argument("--model", default=None,
                    help="override the model for this run, e.g. "
                         "gemini-3.1-flash-lite (separate free-tier quota)")
    ap.add_argument("--reset", action="store_true",
                    help="move EVERY clip in approved/rejected/review back to "
                         "inbox/ for a clean re-run. Verdict logs are kept. Use "
                         "when a batch was graded by more than one model.")
    ap.add_argument("--requeue", action="store_true",
                    help="move clips that landed in review/ because the API "
                         "call FAILED (rate limit, timeout) back to inbox/, "
                         "then exit. Real model verdicts are left alone.")
    args = ap.parse_args()
    if args.show_prompt:
        preview_prompt(args.stage, args.config, args.model)
        return
    if args.reset:
        cfg = load_config(args.config)
        root = args.data_root or cfg["stages"][args.stage]["data_root"]
        if not os.path.isabs(root):
            root = os.path.normpath(os.path.join(HERE, "..", root))
        ib = os.path.join(root, "inbox")
        os.makedirs(ib, exist_ok=True)
        n = 0
        for d in ("approved", "rejected", "review"):
            src = os.path.join(root, d)
            if not os.path.isdir(src):
                continue
            for f in sorted(os.listdir(src)):
                p = os.path.join(src, f)
                if os.path.isfile(p) and f.lower().endswith((".mp4", ".mov")):
                    shutil.move(p, os.path.join(ib, f))
                    n += 1
        print(f"reset {n} clip(s) back to inbox/. Verdict logs kept in place — "
              f"the next run appends, so old and new verdicts stay comparable.")
        return
    if args.requeue:
        cfg = load_config(args.config)
        root = args.data_root or cfg["stages"][args.stage]["data_root"]
        if not os.path.isabs(root):
            root = os.path.normpath(os.path.join(HERE, "..", root))
        rv, ib = os.path.join(root, "review"), os.path.join(root, "inbox")
        log = os.path.join(rv, "_log.tsv")
        # Only the LAST line for each clip counts. The log is append-only and
        # spans every run ever made, so a clip that was rate-limited in an
        # earlier run and then given a real verdict later still has a "gemini
        # call failed" line sitting in its history forever. Matching any line
        # requeued 9 clips when 2 had actually failed, dragging seven genuine
        # verdicts back into the inbox to be re-graded and re-billed.
        latest = {}
        if os.path.exists(log):
            for line in open(log, encoding="utf-8"):
                p = line.rstrip("\n").split("\t")
                if len(p) >= 3:
                    latest[p[1]] = p[2]
        # Infrastructure failures only, never a real verdict. "unparseable
        # model reply" is deliberately NOT included: a safety refusal reads the
        # same way and would be requeued forever.
        failed = {name for name, reason in latest.items()
                  if "gemini call failed" in reason}
        n = 0
        for f in sorted(os.listdir(rv)):
            if f in failed and os.path.isfile(os.path.join(rv, f)):
                shutil.move(os.path.join(rv, f), os.path.join(ib, f))
                n += 1
        print(f"requeued {n} clip(s) from review/ -> inbox/ "
              f"(only ones whose API call failed)")
        return
    run(args.stage, args.config, args.data_root, args.limit, args.dry_run,
        show=args.explain, model=args.model)


if __name__ == "__main__":
    main()
