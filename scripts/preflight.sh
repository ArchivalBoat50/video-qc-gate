#!/bin/sh
# Run this BEFORE importing the workflow. Every line must say OK.
ROOT="${PIPE_DATA:-/data}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
echo "data root      : $ROOT"
[ -d "$ROOT/inbox" ] && echo "inbox          : OK" || echo "inbox          : MISSING -> mkdir -p $ROOT/inbox"
for d in approved rejected review work; do
  [ -d "$ROOT/$d" ] && echo "$(printf '%-15s' $d): OK" || echo "$(printf '%-15s' $d): MISSING -> mkdir -p $ROOT/$d"
done
command -v ffmpeg  >/dev/null && echo "ffmpeg         : OK ($(command -v ffmpeg))"  || echo "ffmpeg         : MISSING -> brew install ffmpeg"
command -v ffprobe >/dev/null && echo "ffprobe        : OK" || echo "ffprobe        : MISSING"
command -v python3 >/dev/null && echo "python3        : OK" || echo "python3        : MISSING"
[ -n "$GEMINI_API_KEY" ] && echo "GEMINI_API_KEY : OK (${#GEMINI_API_KEY} chars)" || echo "GEMINI_API_KEY : NOT SET in this shell (n8n reads it from its own env)"
echo "clips in inbox : $(ls "$ROOT/inbox" 2>/dev/null | grep -ci '\.mp4$')"
echo "--- scan.py dry run ---"
OUT=$(python3 "$(dirname "$0")/scan.py")
echo "$OUT" | python3 -c "
import json,sys
try: rows=json.load(sys.stdin)
except Exception as e: print('  scan.py FAILED:',e); sys.exit(1)
for r in rows:
    v='HARD FAIL: '+'; '.join(r['hard_fail_reasons']) if r['hard_fail'] else '-> goes to Gemini'
    print(f\"  {r['file'][:34]:<34} {r['duration']}s cuts={r['cuts']} audio={str(r['has_audio']):<5} {v}\")
print(f'  {len(rows)} clip(s) scanned')
"
