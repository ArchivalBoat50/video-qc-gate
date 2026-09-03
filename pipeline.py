#!/usr/bin/env python3
"""
Video QC gate — no Docker, no n8n, no dependencies.

    python3 pipeline.py            # run it
    python3 pipeline.py --dry-run  # ffmpeg gate only, no Gemini call, no moves

Reads clips from data/inbox, writes to data/approved or data/rejected,
appends a reason to _log.tsv in whichever folder it lands.
"""
import base64, json, os, sys, shutil, time, urllib.request, urllib.error
from datetime import datetime

HERE   = os.path.dirname(os.path.abspath(__file__))
ROOT   = os.environ.get("PIPE_DATA", os.path.join(HERE, "data"))
INBOX  = os.path.join(ROOT, "inbox")
WORK   = os.path.join(ROOT, "work")
MODEL  = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
RUBRIC = os.path.join(HERE, "scripts", "gemini_rubric.txt")
CTX_DIR= os.path.join(HERE, "context")
THROTTLE = 5          # seconds between Gemini calls; free tier is 5-15 RPM

sys.path.insert(0, os.path.join(HERE, "scripts"))
import scan  # reuse the tested ffmpeg logic
scan.INBOX, scan.WORK = INBOX, WORK


def load_key():
    k = os.environ.get("GEMINI_API_KEY")
    if k:
        return k.strip()
    envf = os.path.join(HERE, ".env")
    if os.path.exists(envf):
        for line in open(envf):
            if line.strip().startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("GEMINI_API_KEY not found in environment or .env")


def load_context():
    """Style guide + brand context, prepended to every QC call.
    Distilled (~1.7k tokens) rather than sending the 605k-token raw transcript,
    which would cost roughly $0.90 per clip on 3.6-flash."""
    parts = []
    for fn, label in (("style_guide.md", "STYLE GUIDE"),
                      ("brand_context.md",    "BRAND CONTEXT")):
        p = os.path.join(CTX_DIR, fn)
        if os.path.exists(p):
            parts.append(f"===== {label} =====\n" + open(p).read())
    if not parts:
        print("  ! warning: no context files found in context/ — QC will be generic")
    return "\n\n".join(parts)


def ask_gemini(key, rubric, sheet_b64, context=""):
    """Returns (verdict_dict, error_string). One of them is always None."""
    body = json.dumps({
        "contents": [{"parts": [
            {"text": (context + "\n\n" + rubric) if context else rubric},
            {"inline_data": {"mime_type": "image/jpeg", "data": sheet_b64}},
        ]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }).encode()

    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        data=body,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode()[:300]}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

    try:
        txt = payload["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(txt), None
    except Exception:
        # A safety refusal lands here. Send to review, don't crash the batch.
        return ({"verdict": "review", "confidence": 0.0, "defects": [],
                 "note": "unparseable model reply (possible safety refusal)"}, None)


def route(clip, dest, reason):
    d = os.path.join(ROOT, dest)
    os.makedirs(d, exist_ok=True)
    shutil.move(clip["path"], os.path.join(d, clip["file"]))
    with open(os.path.join(d, "_log.tsv"), "a") as fh:
        fh.write(f"{datetime.now().isoformat(timespec='seconds')}\t{clip['file']}\t{reason}\n")


def main():
    dry = "--dry-run" in sys.argv
    for p in (INBOX, WORK):
        os.makedirs(p, exist_ok=True)

    clips = json.loads(_capture(scan.main))
    if not clips:
        print(f"nothing in {INBOX}")
        return

    key    = None if dry else load_key()
    rubric = open(RUBRIC).read()
    context= load_context()
    tally  = {}
    first  = True

    print(f"\n{len(clips)} clip(s)  |  model={MODEL}  |  context={len(context)} chars  |  {'DRY RUN' if dry else 'live'}\n")
    for c in clips:
        name = c["file"][:32]

        if c["hard_fail"]:
            reason = "; ".join(c["hard_fail_reasons"])
            print(f"  {name:<34} REJECT   {reason}")
            if not dry:
                route(c, "rejected", reason)
            tally["rejected"] = tally.get("rejected", 0) + 1
            continue

        if dry:
            print(f"  {name:<34} -> would go to Gemini")
            tally["would_send"] = tally.get("would_send", 0) + 1
            continue

        if not first:
            time.sleep(THROTTLE)
        first = False

        verdict, err = ask_gemini(key, rubric, c["sheet_b64"], context)
        if err:
            print(f"  {name:<34} ERROR    {err}")
            tally["error"] = tally.get("error", 0) + 1
            continue

        v       = verdict.get("verdict", "review")
        defects = "; ".join(verdict.get("defects", [])) or verdict.get("note", "")
        craft   = verdict.get("craft", {})
        score   = sum(1 for x in craft.values() if x) if craft else "?"
        dest    = "approved" if v == "pass" else "rejected"
        print(f"  {name:<34} {v.upper():<8} craft={score}/3 {defects[:52]}")
        route(c, dest, f"{v} craft={score}/3: {defects}")
        tally[v] = tally.get(v, 0) + 1

    print("\n  " + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    print(f"  approved -> {os.path.join(ROOT,'approved')}")
    print(f"  sheets   -> {WORK}\n")


def _capture(fn):
    """scan.main() prints JSON; grab it without touching the tested code."""
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


if __name__ == "__main__":
    main()
