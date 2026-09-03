#!/usr/bin/env python3
"""
The re-roll loop's bookkeeping half. Generation is the other half and it is
NOT here, because it cannot be.

There is no first-party Higgsfield API key on the Creator plan, so generation
only works through the MCP, which only exists inside an agent session. The
chain is therefore:

    retry.py --plan          <- code: what to re-roll, and is the budget spent?
    (agent calls the MCP)    <- generate_video / job_status, from the plan
    retry.py --ingest ...    <- code: download, chmod, record the attempt

The budget lives in code on purpose. An agent that is asked to "stop after two
attempts" will eventually miscount; --ingest refuses the third attempt for a
clip whatever anyone believes about it.

    python3 retry.py --plan
    python3 retry.py --ingest V21_frozenpond.mp4 --url https://... --job-id abc
    python3 retry.py --status
"""
import argparse, json, os, re, shutil, sys, time
import urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pipeline_qc as P

DEFAULT_CONFIG = os.path.join(HERE, "qc_config.json")


def retry_settings(cfg):
    r = dict(cfg.get("retry") or {})
    r.setdefault("max_attempts", 2)
    r.setdefault("state_file", "work/_retry_state.json")
    r.setdefault("artifact_patterns", [])
    r.setdefault("structural_patterns", [])
    return r


def resolve_root(cfg, stage, data_root=None):
    root = data_root or cfg["stages"][stage]["data_root"]
    if not os.path.isabs(root):
        root = os.path.normpath(os.path.join(HERE, "..", root))
    return root


def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)          # atomic: a half-written budget is a bug


def latest_reasons(log_path):
    """Last verdict per clip. The log is append-only and spans every run ever
    made, so anything but the last line is history, not the current verdict.
    (This is the bug that made --requeue pull 9 clips when 2 had failed.)"""
    latest = {}
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                p = line.rstrip("\n").split("\t")
                if len(p) >= 3:
                    latest[p[1]] = {"when": p[0], "reason": p[2]}
    return latest


def load_job_map(cfg):
    """clip stem -> Higgsfield job id, parsed out of JOBS.md.

    Starting a re-roll from the ORIGINAL job means the original prompt and
    character still; inventing a new prompt is a different generation, not a
    retry, and it would not be testing the same thing."""
    rel = retry_settings(cfg).get("job_map")
    if not rel:
        return {}
    path = rel if os.path.isabs(rel) else \
        os.path.normpath(os.path.join(HERE, "..", rel))
    jobs = {}
    if not os.path.exists(path):
        return jobs
    pat = re.compile(r"^\s*([0-9a-f-]{36})\s+(\S+)\s*$")
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = pat.match(line)
            if m:
                jobs[m.group(2)] = m.group(1)
    return jobs


def classify(reason, r):
    """Is a new seed plausibly going to fix this?

    Artifact  -> yes. Floating objects and melted hands are sampling noise.
    Structural-> no. 'wrong person' is the character still and 'face never
                 visible' is the prompt; re-rolling either reproduces it.
    A HEURISTIC on the model's free text. It is reported as a suggestion and
    the agent overrides it whenever the note says something the patterns miss.
    """
    low = reason.lower()
    art = [p for p in r["artifact_patterns"] if p in low]
    stru = [p for p in r["structural_patterns"] if p in low]
    if art and not stru:
        return "artifact", art
    if stru and not art:
        return "structural", stru
    if art and stru:
        return "mixed", art + stru
    return "unknown", []


def build_plan(cfg, stage="stock", data_root=None):
    r = retry_settings(cfg)
    root = resolve_root(cfg, stage, data_root)
    rejected = os.path.join(root, "rejected")
    state = load_state(os.path.join(root, r["state_file"]))
    reasons = latest_reasons(os.path.join(rejected, "_log.tsv"))
    jobs = load_job_map(cfg)
    maxa = int(r["max_attempts"])

    plan = []
    for fn in sorted(os.listdir(rejected)) if os.path.isdir(rejected) else []:
        if not fn.lower().endswith((".mp4", ".mov")):
            continue
        info = reasons.get(fn, {})
        reason = info.get("reason", "")
        kind, hits = classify(reason, r)
        attempts = int(state.get(fn, {}).get("attempts", 0))
        stem = os.path.splitext(fn)[0]
        # A re-rolled clip keeps its original stem, so strip any _1/_2 suffix
        # the no-clobber move added when looking the job up.
        base = re.sub(r"_\d+$", "", stem)

        if attempts >= maxa:
            action, why = "surface", f"budget spent ({attempts}/{maxa} attempts)"
        elif kind == "structural":
            action, why = "surface", ("a new seed will not fix this — it is the "
                                      "prompt or the character still")
        elif kind == "artifact":
            action, why = "reroll", "random artifact, a new seed plausibly clears it"
        else:
            action, why = "ask", f"cannot classify ({kind}) — agent decides"

        plan.append({"clip": fn, "verdict_reason": reason,
                     "when": info.get("when", ""), "classification": kind,
                     "matched": hits, "attempts": attempts,
                     "attempts_left": max(0, maxa - attempts),
                     "job_id": jobs.get(base, ""), "action": action,
                     "why": why,
                     "path": os.path.join(rejected, fn)})
    return plan, root


def print_plan(plan, maxa):
    if not plan:
        print("nothing in rejected/ — no re-rolls to plan.")
        return
    order = {"reroll": 0, "ask": 1, "surface": 2}
    for item in sorted(plan, key=lambda i: order.get(i["action"], 3)):
        print(f"\n  {item['action'].upper():<8} {item['clip']}"
              f"   [{item['attempts']}/{maxa} attempts used]")
        print(f"    QC said : {item['verdict_reason'][:110]}")
        print(f"    class   : {item['classification']}"
              + (f"  ({', '.join(item['matched'][:3])})" if item["matched"] else ""))
        print(f"    because : {item['why']}")
        if item["action"] == "reroll":
            jid = item["job_id"] or "(not in JOBS.md — agent must supply one)"
            print(f"    job_id  : {jid}")
    n = sum(1 for i in plan if i["action"] == "reroll")
    print(f"\n{n} to re-roll, "
          f"{sum(1 for i in plan if i['action'] == 'ask')} to decide, "
          f"{sum(1 for i in plan if i['action'] == 'surface')} parked.")
    if n:
        print("\nNext: the agent calls the Higgsfield MCP for each 'reroll' "
              "item, starting from job_id and adjusting the prompt against the "
              "QC note, then:\n  python3 retry.py --ingest <clip> --url <cdn "
              "url> --job-id <new job>")


def ingest(cfg, clip, url, job_id="", stage="stock", data_root=None,
           fetch=None, note=""):
    """Download a re-rolled clip into inbox/ and spend one attempt.

    Refuses past the budget. This is where the cap is actually enforced — the
    plan is advisory, this is not."""
    r = retry_settings(cfg)
    root = resolve_root(cfg, stage, data_root)
    state_path = os.path.join(root, r["state_file"])
    state = load_state(state_path)
    maxa = int(r["max_attempts"])

    rec = state.setdefault(clip, {"attempts": 0, "history": []})
    if int(rec["attempts"]) >= maxa:
        raise SystemExit(
            f"refusing: {clip} has used {rec['attempts']}/{maxa} attempts. "
            f"Raise retry.max_attempts in qc_config.json if that is really "
            f"what you want — but the reason for the cap is that a clip which "
            f"failed twice usually has a prompt problem, not a seed problem.")

    inbox = os.path.join(root, "inbox")
    os.makedirs(inbox, exist_ok=True)
    dest = os.path.join(inbox, clip)
    if os.path.exists(dest):
        stem, ext = os.path.splitext(clip)
        n = 1
        while os.path.exists(os.path.join(inbox, f"{stem}_{n}{ext}")):
            n += 1
        dest = os.path.join(inbox, f"{stem}_{n}{ext}")

    tmp = dest + ".part"
    try:
        if fetch is not None:
            fetch(url, tmp)
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "video-qc-gate"})
            with urllib.request.urlopen(req, timeout=300) as resp, \
                    open(tmp, "wb") as fh:
                shutil.copyfileobj(resp, fh)
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise SystemExit(f"download failed for {clip}: {type(e).__name__}: {e}")

    if os.path.getsize(tmp) == 0:
        os.remove(tmp)
        raise SystemExit(f"download failed for {clip}: zero bytes")

    # Downloads land as 600 on macOS and the container user is uid 1000, to
    # which host files are "other". scan.py would skip it as unreadable.
    os.replace(tmp, dest)
    os.chmod(dest, 0o644)

    rec["attempts"] = int(rec["attempts"]) + 1
    rec["history"].append({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                           "job_id": job_id, "url": url[:200],
                           "landed": os.path.basename(dest),
                           "note": P.shell_safe(note) if note else ""})
    save_state(state_path, state)

    print(f"ingested {os.path.basename(dest)} -> inbox/  "
          f"({rec['attempts']}/{maxa} attempts used)")
    print(f"  re-run the gate:  python3 scripts/pipeline_qc.py --stage {stage}")
    print(f"  NOTE: scan.py skips files modified in the last "
          f"{cfg.get('settle_seconds', 20)}s — wait, or run it twice.")
    return dest


def print_status(cfg, stage="stock", data_root=None):
    r = retry_settings(cfg)
    root = resolve_root(cfg, stage, data_root)
    state = load_state(os.path.join(root, r["state_file"]))
    maxa = int(r["max_attempts"])
    if not state:
        print("no re-rolls recorded yet.")
        return
    for clip in sorted(state):
        rec = state[clip]
        print(f"  {clip:<26} {rec['attempts']}/{maxa}")
        for h in rec.get("history", []):
            print(f"      {h.get('at', '')[:19]}  job={h.get('job_id') or '?'}"
                  f"  -> {h.get('landed', '')}")
            if h.get("note"):
                print(f"      {' ' * 19}  {h['note'][:100]}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--stage", default="stock", choices=["stock", "reel"])
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--plan", action="store_true",
                    help="what should be re-rolled, and what is out of budget")
    ap.add_argument("--json", action="store_true",
                    help="with --plan, emit the plan as JSON for an agent")
    ap.add_argument("--status", action="store_true",
                    help="attempts used per clip")
    ap.add_argument("--ingest", metavar="CLIP",
                    help="record a re-rolled clip and put it back in inbox/")
    ap.add_argument("--url", help="CDN url of the regenerated clip")
    ap.add_argument("--job-id", default="", help="the new Higgsfield job id")
    ap.add_argument("--note", default="",
                    help="what was changed in the prompt, for the record")
    args = ap.parse_args()

    cfg = P.load_config(args.config)
    if args.ingest:
        if not args.url:
            raise SystemExit("--ingest needs --url")
        ingest(cfg, args.ingest, args.url, args.job_id, args.stage,
               args.data_root, note=args.note)
    elif args.status:
        print_status(cfg, args.stage, args.data_root)
    else:
        plan, _ = build_plan(cfg, args.stage, args.data_root)
        if args.json:
            print(json.dumps(plan, indent=2))
        else:
            print_plan(plan, int(retry_settings(cfg)["max_attempts"]))


if __name__ == "__main__":
    main()
