#!/usr/bin/env python3
"""Offline tests for pipeline_qc.py. No network, no API key, no docker.

Proves the four routes and the safety properties with a mocked Gemini. The one
thing it cannot prove is that the live endpoint accepts our request body — that
needs a real key and a reachable network.

    python3 scripts/test_qc.py
"""
import base64, json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import pipeline_qc as P

SRC = os.path.join(ROOT, "tests", "fixtures", "stock")
FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def reply(verdict, note="ok", conf=0.9, defects=None):
    payload = {"verdict": verdict, "confidence": conf,
               "defects": defects or [], "note": note}
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]}


def make_tree(tmp, clips):
    root = os.path.join(tmp, "stock")
    for d in ("inbox", "approved", "rejected", "review", "work"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    for dst, src in clips.items():
        shutil.copy(os.path.join(SRC, src), os.path.join(root, "inbox", dst))
    old = 1785000000
    for f in os.listdir(os.path.join(root, "inbox")):
        os.utime(os.path.join(root, "inbox", f), (old, old))
    return root


def main():
    cfg = os.path.join(HERE, "qc_config.json")

    # ---- 1. four routes ---------------------------------------------------
    print("\n[1] routing: pass / fail / review / api-error")
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(tmp, {
            "a_pass.mp4":   "V13_dawnfield.mp4",
            "b_fail.mp4":   "V09_haybale.mp4",
            "c_review.mp4": "V05_deerblind.mp4",
            "d_error.mp4":  "V10_hardware.mp4",
        })
        seen = {}

        def mock(rubric, media, reference_b64):
            n = len(seen)
            seen[n] = (bool(reference_b64), len(media.get("data") or ""),
                       rubric, media)
            return [reply("pass"), reply("fail", defects=["wrong character vs reference"]),
                    reply("review"), {"error": "429: rate limited"}][n]

        res, counts = P.run("stock", cfg, data_root=root, mock=mock, quiet=True)
        got = {r["file"]: r["dest"] for r in res}
        check("pass -> approved", got.get("a_pass.mp4") == "approved", got)
        check("fail -> rejected", got.get("b_fail.mp4") == "rejected", got)
        check("review -> review", got.get("c_review.mp4") == "review", got)
        check("api error -> review (never rejected)",
              got.get("d_error.mp4") == "review", got)
        check("files physically moved out of inbox",
              not [f for f in os.listdir(os.path.join(root, "inbox"))
                   if f.endswith(".mp4")])
        check("reference image sent with every stock call",
              all(v[0] for v in seen.values()))
        check("media sent with every call",
              all(v[1] > 1000 for v in seen.values()))
        check("context pack prepended to rubric",
              "STYLE GUIDE" in seen[0][2])
        check("stock rubric used, not the reel one",
              "RAW GENERATED CLIPS" in seen[0][2] and "CAPTION LEGIBILITY" not in seen[0][2])
        log = open(os.path.join(root, "rejected", "_log.tsv")).read()
        check("rejection logged with reason", "wrong character vs reference" in log, log)

    # ---- 2. objective gate spends no model call ---------------------------
    print("\n[2] objective failures never reach the model")
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(tmp, {"vertical_ok.mp4": "V13_dawnfield.mp4"})
        bad = os.path.join(root, "inbox", "landscape.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i",
                        os.path.join(SRC, "V09_haybale.mp4"),
                        "-vf", "transpose=1,scale=1280:720", "-an", bad], check=True)
        os.utime(bad, (1785000000, 1785000000))
        calls = []

        def mock(rubric, sheet, ref):
            calls.append(1)
            return reply("pass")

        res, _ = P.run("stock", cfg, data_root=root, mock=mock, quiet=True)
        got = {r["file"]: (r["dest"], r["verdict"]) for r in res}
        check("landscape rejected objectively",
              got.get("landscape.mp4") == ("rejected", "objective"), got)
        check("exactly one model call for two clips", len(calls) == 1, len(calls))

    # ---- 3. shell injection through the model reply -----------------------
    print("\n[3] model-controlled text cannot inject or corrupt the log")
    evil = 'fail"; rm -rf /data; #\nsecond line\twith tabs'
    safe = P.shell_safe(evil)
    check("quotes/semicolons/backslashes stripped",
          not any(c in safe for c in '"\'`$\\;&|<>'), safe)
    check("newlines and tabs collapsed (TSV stays one row)",
          "\n" not in safe and "\t" not in safe, safe)
    check("length capped at 200", len(P.shell_safe("x" * 5000)) == 200)
    check("empty reply still yields a reason", P.shell_safe("") == "no detail")

    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(tmp, {"evil.mp4": "V13_dawnfield.mp4"})
        res, _ = P.run("stock", cfg, data_root=root, quiet=True,
                       mock=lambda r, s, f: reply("fail", defects=[evil]))
        rows = open(os.path.join(root, "rejected", "_log.tsv")).read().strip().split("\n")
        check("one TSV row per clip despite newline in reply", len(rows) == 1, rows)
        check("row has exactly 3 columns", len(rows[0].split("\t")) == 3, rows[0])

    # ---- 4. malformed replies degrade to review ---------------------------
    print("\n[4] malformed model replies degrade to review")
    for label, r in [("not json", {"candidates": [{"content": {"parts": [{"text": "sorry!"}]}}]}),
                     ("empty dict", {}),
                     ("bad verdict word", reply("obliterate")),
                     ("safety refusal shape", {"candidates": [{"finishReason": "SAFETY"}]})]:
        v = P.parse_verdict(r)
        check(f"{label} -> review", v["verdict"] == "review", v)

    # ---- 5. no-clobber ----------------------------------------------------
    print("\n[5] repeated filenames never destroy a filed clip")
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(tmp, {"dupe.mp4": "V13_dawnfield.mp4"})
        for _ in range(3):
            P.run("stock", cfg, data_root=root, quiet=True,
                  mock=lambda r, s, f: reply("pass"))
            shutil.copy(os.path.join(SRC, "V13_dawnfield.mp4"),
                        os.path.join(root, "inbox", "dupe.mp4"))
            os.utime(os.path.join(root, "inbox", "dupe.mp4"), (1785000000, 1785000000))
        got = sorted(f for f in os.listdir(os.path.join(root, "approved"))
                     if f.endswith(".mp4"))
        check("collisions suffixed _1/_2, nothing overwritten",
              got == ["dupe.mp4", "dupe_1.mp4", "dupe_2.mp4"], got)
        rows = [l for l in open(os.path.join(root, "approved", "_log.tsv"))
                if l.strip()]
        check("log records the name each file landed under",
              all(any(n in l for n in got) for l in rows), rows)

    # ---- 6. per-stage gates differ ----------------------------------------
    print("\n[6] reel stage accepts long-form that stock rejects")
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "reels")
        for d in ("inbox", "approved", "rejected", "review", "work"):
            os.makedirs(os.path.join(root, d))
        long_clip = os.path.join(root, "inbox", "long30s.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-stream_loop", "4", "-i",
                        os.path.join(SRC, "V13_dawnfield.mp4"), "-t", "30",
                        "-c:v", "libx264", "-preset", "ultrafast", "-an", long_clip],
                       check=True)
        os.utime(long_clip, (1785000000, 1785000000))
        res, _ = P.run("reel", cfg, data_root=root, quiet=True,
                       mock=lambda r, s, f: reply("pass"))
        check("30s reel passes the reel duration gate",
              res and res[0]["dest"] == "approved", res)

        root2 = make_tree(tmp, {})
        shutil.copy(long_clip if os.path.exists(long_clip)
                    else os.path.join(root, "approved", "long30s.mp4"),
                    os.path.join(root2, "inbox", "long30s.mp4"))
        os.utime(os.path.join(root2, "inbox", "long30s.mp4"), (1785000000, 1785000000))
        res2, _ = P.run("stock", cfg, data_root=root2, quiet=True,
                        mock=lambda r, s, f: reply("pass"))
        check("same 30s clip rejected by the stock gate",
              res2 and res2[0]["dest"] == "rejected"
              and "outside 5-9s" in res2[0]["reason"], res2)

    # ---- 7. reel stage uses the reel rubric, no reference -----------------
    print("\n[7] reel stage sends the craft rubric and no reference image")
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "reels")
        for d in ("inbox", "approved", "rejected", "review", "work"):
            os.makedirs(os.path.join(root, d))
        shutil.copy(os.path.join(ROOT, "tests", "fixtures", "reels",
                                 "004_V13_dawnfield.mp4"),
                    os.path.join(root, "inbox", "reel004.mp4"))
        os.utime(os.path.join(root, "inbox", "reel004.mp4"), (1785000000, 1785000000))
        grab = {}

        def mock(rubric, sheet, ref):
            grab["rubric"], grab["ref"] = rubric, ref
            return reply("pass")

        P.run("reel", cfg, data_root=root, quiet=True, mock=mock)
        check("reel rubric used", "FINISHED short-form" in grab.get("rubric", ""))
        check("caption legibility section present",
              "CAPTION LEGIBILITY" in grab.get("rubric", ""))
        check("no reference image at reel stage", grab.get("ref") is None)

    # ---- 8. --requeue -----------------------------------------------------
    # Added after --requeue shipped with a missing `import shutil` and blew up
    # in the user's hands. Every CLI path gets exercised here now, not just run().
    print("\n[8] --requeue returns only API failures, never real verdicts")
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(tmp, {"a.mp4": "V13_dawnfield.mp4",
                               "b.mp4": "V09_haybale.mp4",
                               "c.mp4": "V05_deerblind.mp4"})
        seq = [{"error": "429: quota"}, reply("review", "face never visible"),
               reply("pass")]
        P.run("stock", cfg, data_root=root, quiet=True,
              mock=lambda r, s, f: seq.pop(0))
        rv = os.path.join(root, "review")
        check("one rate-limited + one real review landed in review/",
              len([f for f in os.listdir(rv) if f.endswith(".mp4")]) == 2)

        r = subprocess.run([sys.executable, os.path.join(HERE, "pipeline_qc.py"),
                            "--stage", "stock", "--data-root", root, "--requeue"],
                           capture_output=True, text=True)
        check("requeue exits cleanly (no NameError)", r.returncode == 0,
              r.stderr[-200:])
        inbox = sorted(f for f in os.listdir(os.path.join(root, "inbox"))
                       if f.endswith(".mp4"))
        left = sorted(f for f in os.listdir(rv) if f.endswith(".mp4"))
        check("the rate-limited clip came back to inbox", inbox == ["a.mp4"], inbox)
        check("the real 'review' verdict stayed put", left == ["b.mp4"], left)

    # A clip that failed in an EARLIER run and was given a real verdict later
    # still carries the old failure line forever — the log is append-only.
    # Matching any line instead of the last one requeued 9 clips when 2 had
    # failed, pulling seven genuine verdicts back to be re-graded and re-billed.
    print("\n[8b] --requeue reads the LAST verdict, not the whole history")
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(tmp, {"a.mp4": "V13_dawnfield.mp4"})
        rv = os.path.join(root, "review")
        os.makedirs(rv, exist_ok=True)
        with open(os.path.join(rv, "_log.tsv"), "w") as fh:
            fh.write("2026-07-30T11:00:00+0700\ta.mp4\t"
                     "review: gemini call failed: 429: quota\n")
            fh.write("2026-07-30T18:00:00+0700\ta.mp4\t"
                     "review: face never visible\n")
        shutil.move(os.path.join(root, "inbox", "a.mp4"),
                    os.path.join(rv, "a.mp4"))
        r = subprocess.run([sys.executable, os.path.join(HERE, "pipeline_qc.py"),
                            "--stage", "stock", "--data-root", root, "--requeue"],
                           capture_output=True, text=True)
        check("a stale failure behind a newer real verdict is NOT requeued",
              os.path.exists(os.path.join(rv, "a.mp4"))
              and not os.path.exists(os.path.join(root, "inbox", "a.mp4")),
              r.stdout.strip())

        # ...and the reverse: a real verdict followed by a later failure IS.
        with open(os.path.join(rv, "_log.tsv"), "a") as fh:
            fh.write("2026-07-30T19:00:00+0700\ta.mp4\t"
                     "review: gemini call failed: 503: high demand\n")
        subprocess.run([sys.executable, os.path.join(HERE, "pipeline_qc.py"),
                        "--stage", "stock", "--data-root", root, "--requeue"],
                       capture_output=True, text=True)
        check("a failure that came after a real verdict IS requeued",
              os.path.exists(os.path.join(root, "inbox", "a.mp4")))

    # ---- 9. every CLI flag at least starts ---------------------------------
    print("\n[9] CLI smoke: every flag parses and runs")
    for flags in (["--show-prompt"], ["--dry-run"], ["--requeue"]):
        for stage in ("stock", "reel"):
            with tempfile.TemporaryDirectory() as tmp:
                root = make_tree(tmp, {})
                r = subprocess.run(
                    [sys.executable, os.path.join(HERE, "pipeline_qc.py"),
                     "--stage", stage, "--data-root", root] + flags,
                    capture_output=True, text=True)
                check(f"{stage} {' '.join(flags)}", r.returncode == 0,
                      r.stderr.strip()[-160:])

    # ---- 10. the model sees the whole video, not 8 frames ------------------
    # The change these tests exist for: the contact sheet discarded the footage
    # BETWEEN its 8 samples, which is exactly where V21_frozenpond's dog walked
    # out of frame and left a leash attached to nothing.
    print("\n[10] video-native input")
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(tmp, {"vid.mp4": "V13_dawnfield.mp4"})
        clip_path = os.path.join(root, "inbox", "vid.mp4")
        clip_size = os.path.getsize(clip_path)
        grab = {}

        def mock(rubric, media, ref):
            grab["rubric"], grab["media"] = rubric, media
            return reply("pass")

        res, _ = P.run("stock", cfg, data_root=root, quiet=True, mock=mock)
        m = grab.get("media", {})
        check("default mode sends the video", m.get("kind") == "video", m.get("kind"))
        check("mime type is video/mp4", m.get("mime_type") == "video/mp4")
        check("the payload is the actual mp4, whole",
              len(base64.b64decode(m.get("data", ""))) == clip_size,
              f"{len(base64.b64decode(m.get('data', '')))} vs {clip_size}")
        check("fps override carried through (1 fps would be no better than 8 frames)",
              m.get("fps") == 4, m.get("fps"))
        check("media note appended, overriding the rubric's 'contact sheet'",
              "MEDIA NOTE" in grab.get("rubric", "")
              and "ENTIRE VIDEO" in grab.get("rubric", ""))
        check("rubric itself still fully present under the note",
              "RAW GENERATED CLIPS" in grab.get("rubric", ""))
        check("result records which medium was used",
              res[0].get("media") == "video", res[0])
        sheets = [f for f in os.listdir(os.path.join(root, "work"))
                  if f.endswith("_qc.jpg")]
        check("contact sheet still written to work/ for human spot-checking",
              sheets == ["vid_qc.jpg"], sheets)

    # ---- 11. mode / fallback / per-stage override --------------------------
    print("\n[11] sheet mode, size fallback, and per-stage overrides")
    base = json.load(open(cfg))
    clip = {"path": os.path.join(SRC, "V13_dawnfield.mp4"),
            "size_bytes": os.path.getsize(os.path.join(SRC, "V13_dawnfield.mp4")),
            "sheet_b64": base64.b64encode(b"\xff\xd8fake-jpeg" * 200).decode()}

    sheet_cfg = json.loads(json.dumps(base))
    sheet_cfg["media"]["mode"] = "sheet"
    m = P.build_media(sheet_cfg, sheet_cfg["stages"]["stock"], clip)
    check("mode 'sheet' still sends the contact sheet", m["kind"] == "sheet")
    check("sheet mode sends image/jpeg", m["mime_type"] == "image/jpeg")
    check("sheet mode gets no video note",
          "MEDIA NOTE" not in P.call_gemini(
              sheet_cfg, sheet_cfg["stages"]["stock"], "", "RUBRIC", m,
              mock=lambda r, md, f: r))

    small = json.loads(json.dumps(base))
    small["media"]["inline_max_bytes"] = 1024
    m = P.build_media(small, small["stages"]["stock"], clip)
    check("clip over the inline limit falls back to the sheet",
          m["kind"] == "sheet" and m.get("fell_back"), m)
    check("the limit is applied to the base64 size, not the raw file",
          P.build_media({"media": {"mode": "video",
                                   "inline_max_bytes": int(clip["size_bytes"] * 1.2)}},
                        {}, clip)["kind"] == "sheet")

    nofb = json.loads(json.dumps(small))
    nofb["media"]["fallback"] = "none"
    m = P.build_media(nofb, nofb["stages"]["stock"], clip)
    check("oversized with no fallback yields no evidence at all",
          m["kind"] == "none", m)

    stage_override = json.loads(json.dumps(base))
    stage_override["stages"]["reel"]["media"] = {"fps": 1, "mode": "video"}
    check("per-stage media block overrides the global one",
          P.media_settings(stage_override,
                           stage_override["stages"]["reel"])["fps"] == 1)
    check("unoverridden stage keeps the global fps",
          P.media_settings(stage_override,
                           stage_override["stages"]["stock"])["fps"] == 4)

    # A clip we cannot build ANY evidence for must land in review/ — it may be
    # perfectly good and nobody has looked at it. Filing it as rejected would
    # claim a defect that was never observed.
    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(tmp, {"huge.mp4": "V13_dawnfield.mp4"})
        tiny = os.path.join(tmp, "tiny_cfg.json")
        t = json.loads(json.dumps(base))
        t["media"].update({"inline_max_bytes": 512, "fallback": "none"})
        json.dump(t, open(tiny, "w"))
        calls = []
        res, _ = P.run("stock", tiny, data_root=root, quiet=True,
                       mock=lambda r, md, f: calls.append(1) or reply("pass"))
        check("no-evidence clip routed to review, never rejected",
              res and res[0]["dest"] == "review", res)
        check("no model call is spent on a clip with no evidence",
              not calls, calls)
        check("no-evidence clip physically moved to review/",
              "huge.mp4" in os.listdir(os.path.join(root, "review")))

    # ---- 12. the request body actually sent --------------------------------
    # Previously unassertable: the body was built inside the network call, so
    # the suite could only prove routing, never the payload.
    print("\n[12] request body shape")
    vid = P.build_media(base, base["stages"]["stock"], clip)
    body = P.build_body(base, base["stages"]["stock"], "RUBRIC", vid, "REFB64")
    parts = body["contents"][0]["parts"]
    check("part order: prompt, ref label, ref image, media label, media",
          [("text" in p) for p in parts] == [True, True, False, True, False]
          and len(parts) == 5, [list(p) for p in parts])
    check("reference image precedes the clip (identity check needs the order)",
          parts[2]["inline_data"]["mime_type"] == "image/jpeg"
          and parts[4]["inline_data"]["mime_type"] == "video/mp4")
    check("video part carries video_metadata fps",
          parts[4].get("video_metadata") == {"fps": 4.0}, parts[4].get("video_metadata"))
    check("temperature 0 and JSON response preserved",
          body["generationConfig"]["temperature"] == 0
          and body["generationConfig"]["responseMimeType"] == "application/json")
    check("media_resolution 'default' omits the field",
          "mediaResolution" not in body["generationConfig"])

    nofps = json.loads(json.dumps(base))
    nofps["media"]["fps"] = None
    m2 = P.build_media(nofps, nofps["stages"]["stock"], clip)
    b2 = P.build_body(nofps, nofps["stages"]["stock"], "RUBRIC", m2)
    check("fps null omits video_metadata (API default sampling)",
          "video_metadata" not in b2["contents"][0]["parts"][-1])
    check("no reference image means no reference parts",
          len(b2["contents"][0]["parts"]) == 3)

    for word, enum in (("low", "MEDIA_RESOLUTION_LOW"),
                       ("high", "MEDIA_RESOLUTION_HIGH"),
                       ("MEDIA_RESOLUTION_MEDIUM", "MEDIA_RESOLUTION_MEDIUM")):
        r = json.loads(json.dumps(base))
        r["media"]["media_resolution"] = word
        b = P.build_body(r, r["stages"]["stock"], "RUBRIC", vid)
        check(f"media_resolution {word!r} -> {enum}",
              b["generationConfig"].get("mediaResolution") == enum,
              b["generationConfig"].get("mediaResolution"))

    sheet_part = P.build_body(base, base["stages"]["stock"], "RUBRIC",
                              {"kind": "sheet", "mime_type": "image/jpeg",
                               "data": "X", "label": "CONTACT SHEET:"}
                              )["contents"][0]["parts"][-1]
    check("sheet part never carries video_metadata",
          "video_metadata" not in sheet_part)

    # ---- 13. the re-roll budget --------------------------------------------
    # Generation runs through an agent, so the spend cap cannot live in an
    # agent's memory of the instruction. It lives here.
    print("\n[13] retry budget is enforced by code, not by good intentions")
    import retry as R
    cfgd = json.load(open(cfg))
    maxa = int(cfgd["retry"]["max_attempts"])

    art, hits = R.classify("fail: object instability: leash attached to nothing",
                           R.retry_settings(cfgd))
    check("an artifact defect is classed re-rollable", art == "artifact", hits)
    stru, _ = R.classify("fail: wrong person vs reference",
                         R.retry_settings(cfgd))
    check("'wrong person' is NOT re-rollable (it is the character still)",
          stru == "structural", stru)
    unk, _ = R.classify("fail: something nobody has seen before",
                        R.retry_settings(cfgd))
    check("an unrecognised reason defers to the agent", unk == "unknown", unk)

    with tempfile.TemporaryDirectory() as tmp:
        root = make_tree(tmp, {})
        rej = os.path.join(root, "rejected")
        shutil.copy(os.path.join(SRC, "V13_dawnfield.mp4"),
                    os.path.join(rej, "V13_dawnfield.mp4"))
        with open(os.path.join(rej, "_log.tsv"), "w") as fh:
            fh.write("2026-07-30T18:00:00+0700\tV13_dawnfield.mp4\t"
                     "fail: object instability: floating net\n")

        plan, _ = R.build_plan(cfgd, "stock", root)
        check("a fresh artifact reject is planned for re-roll",
              plan[0]["action"] == "reroll", plan[0])
        check("the plan carries the QC note through verbatim",
              "floating net" in plan[0]["verdict_reason"])

        calls = []

        def fake_fetch(url, dest):
            calls.append(url)
            shutil.copy(os.path.join(SRC, "V13_dawnfield.mp4"), dest)

        for i in range(maxa):
            R.ingest(cfgd, "V13_dawnfield.mp4", f"https://x/{i}.mp4",
                     job_id=f"job{i}", stage="stock", data_root=root,
                     fetch=fake_fetch)
        check(f"{maxa} attempts are allowed", len(calls) == maxa, calls)

        landed = sorted(f for f in os.listdir(os.path.join(root, "inbox"))
                        if f.endswith(".mp4"))
        check("re-rolls never clobber each other",
              landed == ["V13_dawnfield.mp4", "V13_dawnfield_1.mp4"], landed)
        check("ingested clips are chmod 644 (container reads them as 'other')",
              all(oct(os.stat(os.path.join(root, "inbox", f)).st_mode)[-3:]
                  == "644" for f in landed), landed)

        try:
            R.ingest(cfgd, "V13_dawnfield.mp4", "https://x/over.mp4",
                     stage="stock", data_root=root, fetch=fake_fetch)
            over = False
        except SystemExit:
            over = True
        check("the attempt past the budget is REFUSED", over)
        check("the refused attempt downloaded nothing", len(calls) == maxa,
              len(calls))

        plan, _ = R.build_plan(cfgd, "stock", root)
        check("a clip out of budget is parked, not re-rolled",
              plan[0]["action"] == "surface"
              and "budget spent" in plan[0]["why"], plan[0])

        state = R.load_state(os.path.join(root, "work", "_retry_state.json"))
        check("attempts survive a reload (the cap is on disk, not in memory)",
              state["V13_dawnfield.mp4"]["attempts"] == maxa, state)
        check("each attempt records its job id for traceability",
              [h["job_id"] for h in state["V13_dawnfield.mp4"]["history"]]
              == [f"job{i}" for i in range(maxa)], state)

        # A truncated download must not consume an attempt or land a bad file.
        with tempfile.TemporaryDirectory() as tmp2:
            root2 = make_tree(tmp2, {})
            def empty_fetch(url, dest):
                open(dest, "wb").close()
            try:
                R.ingest(cfgd, "x.mp4", "https://x/e.mp4", stage="stock",
                         data_root=root2, fetch=empty_fetch)
                failed = False
            except SystemExit:
                failed = True
            check("a zero-byte download is an error, not an ingested clip",
                  failed and not os.listdir(os.path.join(root2, "inbox")))

    print("\n" + ("ALL TESTS PASSED" if not FAILS
                  else f"{len(FAILS)} FAILURE(S): {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
