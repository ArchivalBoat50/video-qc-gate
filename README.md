# video-qc-gate

Generative video fails in ways that are cheap to produce and expensive to
publish: a face that is not quite the same face by the end of the clip, a hand
that grows a sixth finger between frames, a strap that leads nowhere. Watching
every clip end to end does not scale, and the cost is asymmetric — a clip is
worth very little, and publishing a broken one costs more than the clip is
worth. This repo is the gate that sits between generation and publication for
an AI-generated short-form video account. Clips land in an inbox; a
deterministic ffmpeg check rejects the objectively wrong ones for free; what
survives is graded by a vision model against a written rubric; the verdict
routes the file to `approved/`, `rejected/` or `review/` and appends the reason
to a tab-separated log. Nothing is deleted and nothing is silently dropped.

---

## Architecture

Two gates, three destinations, two runners.

```
                        data/<stage>/inbox/
                                |
                                v
                   +-------------------------+
                   |  readability guards     |   zero bytes, unreadable,
                   |  (scan.py)              |   no decodable stream, modified
                   +-------------------------+   in the last 20s, unsafe
                                |               filename -> SKIP, left in inbox
                                v
        GATE 1 +-------------------------------------+
     objective  |  ffmpeg / ffprobe, deterministic   |
        (free)  |  hard cuts inside the clip         |--- any hit -->  rejected/
                |  baked-in audio track              |                (no model
                |  duration outside the stage window |                 call spent)
                |  aspect ratio below 1.6            |
                +-------------------------------------+
                                |  clean
                                v
                   +-------------------------+
                   |  build 8-frame contact  |  written to work/ for human
                   |  sheet (4x2, 300px)     |  spot-checking, and kept as
                   +-------------------------+  the over-the-limit fallback
                                |
                                v
        GATE 2 +-------------------------------------+
   subjective   |  Gemini, temperature 0, JSON only  |
                |  parts, in order:                  |
                |   1. context pack + rubric         |
                |   2. reference still (stock only)  |
                |   3. the WHOLE mp4, inline, 4 fps  |
                +-------------------------------------+
                                |
                    +-----------+-----------+
                    |           |           |
                  pass        fail     review / call failed
                    |           |         / unparseable
                    v           v           v
              approved/    rejected/     review/
                    \           |           /
                     \          v          /
                      \   retry.py --plan /
                       \        |        /
                        artifact defect -> re-roll, max 2 attempts
                        structural      -> parked and surfaced
```

`review/` is not a third opinion, it is an admission. `rejected/` means a defect
was observed. `review/` means nobody actually looked at the clip — the call was
throttled, timed out, refused on safety grounds, or came back as something that
would not parse. Collapsing those two into one bucket would file infrastructure
failures as quality defects and poison every later measurement of how the gate
performs.

### Two stages

| stage | input | duration gate | reference image | rubric |
|---|---|---|---|---|
| `stock` | raw generated clips, no captions | 5-9s | yes | `scripts/rubric_stock.txt` |
| `reel` | finished captioned posts | 5-45s | no | `scripts/rubric_reel.txt` |

Both stages require a vertical aspect ratio of at least 1.6 and a silent audio
track. The stock rubric grades identity, defects and realism; the reel rubric
adds caption legibility and craft and drops the reference comparison.

### Two runners, one config

- **`scripts/pipeline_qc.py`** — the Python runner. Standard library only, no
  install step. This is the one that has done the live grading.
- **`workflow_qc.json`** — the n8n runner, 26 nodes, two stages, imported into
  the container built by the `Dockerfile`. Scheduled hourly.

Both shell out to the same `scripts/scan.py`, which reads its gates out of
`scripts/qc_config.json`. That is deliberate, and the config file says why in
its first line:

> "Shared by pipeline_qc.py and workflow_qc.json. Both runners read THIS file so
> their gates cannot drift apart."

Two implementations of the same gate is two chances to disagree about what a
rejection means. Once the duration window lives in one place, "why did the
hourly job reject a clip my local run approved" stops being a question anyone
has to investigate. The sheet geometry is in the config for the same reason: if
the two runners built different contact sheets they would be grading different
evidence and the verdict logs would not be comparable.

The sharing is not total, and pretending otherwise would be dishonest: the
model name and the `media` block are duplicated into the n8n HTTP node, and
`workflow_qc.json` has not been migrated to video-native input — it still sends
the 8-frame contact sheet. See Known limitations.

Two more files are history rather than current paths: `pipeline.py` is the
original single-file, zero-dependency version of the gate, and
`workflow_qc_native.json` is the earlier single-stage n8n workflow for a
host-installed n8n. Both still send contact sheets and read the older
`scripts/gemini_rubric.txt`.

---

## Key design decisions

**The model gets the whole mp4, not an 8-frame contact sheet.** This is the
central decision and it was made on cost arithmetic plus one concrete failure.
From `scripts/qc_config.json`:

> "'video' sends the whole mp4 inline; the 8-frame sheet cost about the same in
> tokens (~1500-2000 vs ~2100 for 7s) while discarding nine-tenths of the
> footage, and defects BETWEEN sampled frames - V21_frozenpond's vanishing dog -
> were structurally invisible."

The sheet was not saving money. It was spending roughly the same tokens to look
at eight moments out of about two hundred. A dog that walks out of frame between
two samples, leaving a leash attached to nothing, is not a defect the model
missed — it is a defect that was never in the evidence. The sheet survives in
two roles: it is still written to `work/` so a human can eyeball a verdict
without opening a video player, and it is still the fallback for a clip too big
to inline.

**4 fps, not the API default.** The default sampling rate would have made the
change pointless:

> "THE API DEFAULT IS 1 fps, which on a 7s clip is 7 frames - no better than the
> 8-frame sheet, and the whole point of this change would be lost. 4 fps gives
> ~28 frames of a 7s clip at ~4x the video token cost (still cents)."

Switching the medium without switching the sampling rate would have swapped an
8-frame sheet for a 7-frame one and changed nothing. There is a test for this
(`test_qc.py` [10]) whose name is literally "1 fps would be no better than 8
frames".

**The model is pinned in config, overridable per run.** `qc_config.json` names
an exact model; `--model` overrides it for one run. A judge is only useful if
its verdicts are comparable over time, and a floating model version silently
changes the meaning of every historical log line. The `--reset` flag exists for
exactly the case where that comparability broke: "Use when a batch was graded by
more than one model."

**The objective gate runs before any model call.** Duration, aspect ratio,
baked-in audio and hard cuts inside a clip are facts, not judgements. ffprobe
answers them for free and answers them the same way every time; a language model
might simply not mention an invented shot change. Ordering cheap and
deterministic first means a model call is never spent on a clip that a
one-second local check already disqualifies. `test_qc.py` [2] asserts the count
directly: two clips in, one of them landscape, exactly one model call.

**Scene-cut counting counts `pts_time:` lines, not `Parsed_showinfo` lines.**
ffmpeg's own configuration output also matches `Parsed_showinfo`, which reported
two phantom cuts on every clip. Since any cut is a hard rejection, that bug
would have quietly rejected the entire library while reporting success.

**The retry cap is 2, and it is enforced in code.** Generation is not scriptable
on this plan — it only works through an MCP inside an agent session — so
`retry.py` splits the loop: `--plan` decides what is worth re-rolling and
whether the budget is spent, the agent makes the generation calls, `--ingest`
downloads the result and records the attempt. The config explains the split:

> "Splitting it this way means the spend cap is enforced by code, not by an
> agent remembering it."

and the docstring is blunter: "An agent that is asked to 'stop after two
attempts' will eventually miscount; --ingest refuses the third attempt for a
clip whatever anyone believes about it." The cap exists at all because
re-rolling is not free — the config cites the warning that "retrying blind burns
credits" — and because a clip that failed twice usually has a prompt problem
rather than a seed problem. `retry.py` also classifies the reject reason before
spending anything: a floating object or a melted hand is sampling noise and a
new seed plausibly clears it, whereas "wrong person" or "face never visible" is
the prompt or the character still, and re-rolling the same prompt reproduces it.
Those are heuristics over the model's free text, flagged as a suggestion, not
executed blindly. The attempt count is written to disk atomically, so it
survives a crash and cannot be lost with the process.

**A reference still is sent at the stock stage and not at the reel stage.** At
the raw-clip stage the reference goes in as the *first* image, before the clip,
because the stock rubric's Part 0 depends on that ordering to answer "is this
even the right character". The config states the failure mode it exists to
catch: "Without it, a clip that is internally consistent but the wrong woman
(V21_frozenpond) passes." Self-consistency is not identity — a clip can be
flawless and still be somebody else, which no single-media call can detect. At
the reel stage the reference is `null`, because a reel is built from an
already-approved stock clip and identity was checked upstream; the config keeps
the knob and documents when to turn it back on ("Set to reference.jpg if you
ever render reels from unapproved footage"). `test_qc.py` [12] asserts the part
order, and [7] asserts that the reel stage sends no reference at all.

**The rubric and the context pack are separate files.** `context/README.md` gives
the reason: "the rubric changes when the *gate* changes, the context pack
changes when the *account* changes, and keeping them apart means a rubric edit
can be diffed against verdict logs without the brand text moving underneath it."
If both lived in one prompt file, every account-level edit would look like a
gate change in the diff, and no verdict comparison across a rubric edit would
mean anything.

**Verdict logs are tracked in git; the clips are not.** From `.gitignore`:

> "the verdict logs ARE worth keeping — they are the record of what the gate
> decided and when, and diffing them across model changes is how you tell
> whether a rubric edit helped"

The mp4s are gigabytes of regenerable output. The `_log.tsv` files are the only
durable record of what the gate decided, and they are what makes "did this
change help" a question with an answer instead of an opinion.

**Everything the model writes is treated as untrusted input.** The verdict text
is interpolated into a shell command by the n8n move nodes and into a TSV row by
both runners, so every reason goes through `shell_safe()` first: shell
metacharacters stripped, newlines and tabs collapsed so one reply cannot become
two log rows, length capped at 200. `scan.py` refuses filenames containing shell
metacharacters at the source, so nothing downstream has to trust a filename
either. `test_qc.py` [3] feeds `fail"; rm -rf /data; #` through the mocked model
and checks the log still has exactly one row of exactly three columns.

**Moves never clobber.** A repeated filename used to silently destroy the
previously filed clip while the log line still claimed success. Collisions now
get a `_1`, `_2` suffix and the log records the name the file actually landed
under, in both `move.sh` and the Python `move()`.

**Unreadable is not bad.** A zero-byte file, a file still being written, or one
with no decodable video stream is skipped and left in the inbox, reported on
stderr, and picked up on the next run. Rejecting it would move a half-written
file out of the inbox and log a defect that was never observed. A file must sit
untouched for 20 seconds before it is trusted to be complete.

**429s honour the delay the API states.** A fixed 2s/4s exponential backoff
fired three requests inside six seconds and pushed the run further over a
per-minute quota — each failure made the next one likelier. The retry now parses
the "Please retry in 56.8s" the API returns and waits that long. And a second
429 after already serving the stated delay is read as a *daily* cap rather than
a per-minute burst: two of those in a row aborts the run with the remaining
clips untouched in the inbox, rather than spending two minutes per clip writing
"review" twenty more times on top of real verdicts already on disk.

---

## Measured results

Every number below is quoted from the repo. Nothing here is estimated by me.

**One live run of the stock gate, 24 clips.** `docs/PIPELINE.md` records the
library state after it: **approved 13, review 8, rejected 3**.

**The switch to video-native input changed 5 of those 24 verdicts.**
`docs/PIPELINE.md` heading: "Changed by the switch to video-native input (5 of
24)", with the per-clip table:

| clip | was | now | why |
|---|---|---|---|
| V21_frozenpond | approved | rejected | leash detaches into a floating loop, ~3.5s |
| V24_trout | approved | rejected | floating duplicate net behind shoulder |
| V20_drivein | approved | review | power lines shift ~1s; smoke artifact at 6s |
| V12_rockingchair | approved | review | side profile only; bare-leg framing risk |
| V14_dirtbike | rejected | review | framed from behind — softer than 30 Jul |

Four of the five moved in the strict direction. Two clips the contact sheet had
approved were rejected once the whole clip was visible, and both are
between-frame defects of exactly the kind the sheet could not represent.
[CONFIRM] whether any of these five verdicts were checked against a human
judgement, or whether "the model changed its mind" is the whole of the evidence.

**Cost.** `docs/PIPELINE.md` gives `gemini-3.6-flash` at "~$0.016/clip" and
states "A full 24-clip QC run costs about **$0.38**". [CONFIRM] whether that is
read off a billing statement or computed from token counts.

**Token arithmetic behind the sheet-vs-video decision.** From
`scripts/qc_config.json`: the 8-frame sheet and the whole 7-second clip cost
"about the same in tokens (~1500-2000 vs ~2100 for 7s)". The same file gives the
resolution tiers: default is "~300 tok/s of video", low is "~100 tok/s -
cheaper than the contact sheet it replaced". [CONFIRM] whether these figures
were measured from API responses or taken from published pricing.

**Context pack sizing.** `pipeline.py` documents the distilled context as
"~1.7k tokens" against a "605k-token raw transcript, which would cost roughly
$0.90 per clip on 3.6-flash". [CONFIRM] the $0.90 figure — the docstring says
"roughly".

**Rate limits.** `qc_config.json`: the free tier "capped at 20 requests" per day
and 5-15 RPM; after billing was enabled the runner is throttled to 30 rpm.
Request timeout was "Raised from 120" to 300 seconds "when the payload went from
a 60KB contact sheet to a 9-30MB mp4".

**Bugs the logs caught, with counts.** Matching any line of the append-only
verdict log instead of the last one "requeued 9 clips when 2 had actually
failed, dragging seven genuine verdicts back into the inbox to be re-graded and
re-billed" (`pipeline_qc.py`, and regression-tested in `test_qc.py` [8b]).
`docs/PIPELINE.md` also records that putting example defects in the prompt
"manufactured 2 of 7 verdicts" — the model reported the examples it was shown.

**Test suite.** `python3 scripts/test_qc.py` runs 85 assertions across 14
groups with the model call mocked. On a fresh clone 83 pass and 2 fail, both
because they need account-specific files that are deliberately not in the repo
(`scripts/reference.jpg` and `context/context_pack.txt`). Supply those two and
the suite is green.

**Not measured, and worth saying plainly:** there is no measurement of the
gate's agreement with a human reviewer. See Known limitations.

---

## Stack

Python 3 standard library only — no pip install, no virtualenv. ffmpeg and
ffprobe for the objective gate and the contact sheet. Gemini via plain HTTP
(`urllib`), temperature 0, `responseMimeType: application/json`. n8n for the
scheduled path, in a Docker image built on `n8nio/n8n:2.31.7` with python3 and a
static ffmpeg staged in from other images. Shell for the move helper and
preflight. Config and verdict data are JSON and TSV.

---

## Running it locally

Requirements: `python3`, `ffmpeg`, `ffprobe`. A Gemini API key for anything that
actually calls the model.

### Offline first — the tests need no key and no network

```sh
sh tests/make_fixtures.sh          # synthetic clips, generated with ffmpeg lavfi
python3 scripts/test_qc.py
```

The fixtures are `testsrc` patterns at the right durations and aspect ratios.
The tests never send them to a model, so the pixels are irrelevant; only
duration, aspect ratio and silence matter, and those are what the script
reproduces.

To see exactly what would be sent to the model without sending it and without a
key:

```sh
python3 scripts/pipeline_qc.py --stage stock --show-prompt
```

### Path A — the standalone Python runner

```sh
mkdir -p data/stock/{inbox,approved,rejected,review,work}
mkdir -p data/reels/{inbox,approved,rejected,review,work}

cp .env.example .env               # add your key
export GEMINI_API_KEY=...          # or leave it in .env

# account-specific, not in the repo:
#   scripts/reference.jpg          a canonical still of the character
#   context/context_pack.txt       style guide + brand context, see context/README.md

cp your_clips/*.mp4 data/stock/inbox/

python3 scripts/pipeline_qc.py --stage stock --dry-run   # objective gates only
python3 scripts/pipeline_qc.py --stage stock --explain   # live, full reasoning
```

`--dry-run` runs gate 1 and moves nothing. `--explain` prints every field the
rubric asks for and saves each raw reply to `data/stock/work/<clip>_reply.json`,
so a surprising verdict can be argued with rather than accepted. `--limit N`
caps a run. `--requeue` returns only the clips whose API call failed. `--reset`
puts everything back in the inbox for a clean re-run, keeping the logs.

Results land in `data/stock/{approved,rejected,review}/` with the reason
appended to `_log.tsv` in whichever directory the clip landed in.

### Path B — Docker and n8n

```sh
cp .env.example .env               # add your key
docker volume create n8n_data      # compose declares this volume external
docker compose up -d --build
```

The `Dockerfile` exists because the stock n8n image ships as a hardened Alpine
build with the package manager removed, so `apk add ffmpeg python3` fails with
"apk: not found". python3 and a statically linked ffmpeg are staged in from
other images instead; Alpine's own ffmpeg package would drag in 116 shared
libraries including libssl and zlib and overwrite the ones Node links against.
The image is pinned rather than `:latest` because it reuses an existing n8n data
volume and n8n's DB migrations are one-way.

Then, at http://localhost:5678, import `workflow_qc.json` and run it once
manually before switching it to Active. The container mounts `./data` at `/data`
and `./scripts` at `/scripts`; `context/` is not mounted, which is why the n8n
path reads its context pack from `/scripts/context_pack.txt` rather than
`context/`.

`sh scripts/preflight.sh` checks the whole set of preconditions — directories,
ffmpeg, ffprobe, python3, the key, and a dry run of `scan.py` over the current
inbox. Every line must say OK. Set `PIPE_DATA` first; it defaults to `/data`,
which is the container path.

`workflow_qc_native.json` is the older single-stage workflow for an n8n
installed on the host. Its paths are placeholders — every
`/ABSOLUTE/PATH/TO/video-qc-gate/...` must be replaced before importing.

---

## Known limitations, and what I would do next

**There is no measurement of agreement with a human reviewer.** This is the
biggest gap and it is the one an interviewer should press on. I know the model
changed 5 of 24 verdicts when the input changed; I do not know how many of those
24 verdicts a person would have agreed with, in either configuration. Without a
labelled set there is no precision, no recall, and no way to tell a stricter
judge from a better one. What I would do: hand-label the existing library
blind — two passes, ideally two people — then score the gate against those
labels and report the confusion matrix per verdict class, not a single accuracy
number. The asymmetry matters: a false approve reaches an audience, a false
reject costs a re-roll. Those should not be weighted the same.

**A judge drifts, and nothing here watches for it.** The model is pinned and
`--reset` exists for the case where a batch spans two models, but there is no
canary set re-graded on a schedule to detect the day a provider-side change
moves the verdicts under a fixed rubric. A dozen clips with known labels, run
weekly, and an alert when the verdicts move would close this cheaply.

**The test fixtures are synthetic.** `make_fixtures.sh` generates `testsrc`
patterns. They exercise routing, the objective gates, the request body, the
injection guards, the no-clobber logic and the retry budget — everything except
whether the rubric is any good, because there is no defect in a test pattern to
find. Prompt quality is untested by construction and can only be evaluated
against real footage with real labels.

**The two runners have partly drifted.** `workflow_qc.json` still sends 8-frame
contact sheets and has not been migrated to video-native input, which means the
scheduled hourly path currently grades on weaker evidence than the Python
runner. The shared config prevents drift in the objective gates and the sheet
geometry, but the model name and the `media` block are duplicated into the n8n
HTTP node and nothing enforces that they match. The right fix is to have the n8n
path shell out to `pipeline_qc.py` instead of reimplementing the request in a
node expression. [CONFIRM] whether the hourly workflow is currently active.

**Three prompt files exist where one lineage should.**
`scripts/gemini_rubric.txt` is the pre-split rubric, still read by `pipeline.py`
and `workflow_qc_native.json`; `rubric_stock.txt` and `rubric_reel.txt` are the
current ones. The stock and reel rubrics also still describe their input as a
contact sheet, and a `MEDIA NOTE` block is appended at call time to override
that wording. It works, and there is a test asserting the override is present,
but rewriting the rubrics for video would be cleaner than patching them in
flight.

**The retry loop has never been executed end to end.** It is built, and the
budget, the classifier, the no-clobber ingest and the refusal past the cap are
all tested against a fake fetcher, but the generation half runs through an agent
session and the full loop has not been run live. [CONFIRM] before claiming
otherwise.

**Confidence scores are logged and never used.** The model returns a
`confidence` field, it is written to the log, and nothing reads it. An obvious
next step is to route low-confidence passes to `review/` instead of `approved/`
— but only once there are human labels to pick the threshold with, otherwise it
is a number chosen to feel right.

**`approved/` means "worth a human glance", not "guaranteed clean".** The gate
raises the floor and reduces how much footage a person has to watch. It does not
replace the person, and the contact sheets in `work/` exist so that spot-check
is cheap.
