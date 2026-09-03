# Context pack

Everything in this directory except this file is gitignored.

`pipeline_qc.py` and both n8n workflows read `context_pack.txt` and prepend it
to the rubric on every model call. It is the standing brief — the account's
style guide and brand context — that the rubric alone cannot carry.

Two documents are concatenated, each under a header the rubric refers to
by name:

    ===== STYLE GUIDE =====
    <the editorial standard clips are graded against>

    ===== BRAND CONTEXT =====
    <who the character is, what the account is for, what it never posts>

Why it is a separate file rather than part of the rubric: the rubric changes
when the *gate* changes, the context pack changes when the *account* changes,
and keeping them apart means a rubric edit can be diffed against verdict logs
without the brand text moving underneath it. Both runners read this same file
for the same reason — see the note at the top of `scripts/qc_config.json`.

Write your own before the first run. There is no default, and a missing
context pack is logged rather than silently ignored.
