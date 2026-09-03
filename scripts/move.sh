#!/bin/sh
# move.sh <src> <approved|rejected|review> <reason>
#
# Never clobbers: a repeated filename out of Higgsfield used to silently
# destroy the previously filed clip, with the _log.tsv line still claiming
# success. Collisions now get a _1, _2, ... suffix and the log records the
# name the file actually landed under. A failed mv is now an error rather
# than a silent no-op that still wrote a log line.
ROOT="${PIPE_DATA:-/data}"
SRC="$1"; DEST="$ROOT/$2"; REASON="$3"

if [ -z "$SRC" ] || [ -z "$2" ]; then
  echo "usage: move.sh <src> <approved|rejected|review> <reason>" >&2
  exit 2
fi
if [ ! -f "$SRC" ]; then
  echo "move.sh: source not found: $SRC" >&2
  exit 1
fi

mkdir -p "$DEST" || { echo "move.sh: cannot create $DEST" >&2; exit 1; }

BASE=$(basename "$SRC")
TARGET="$DEST/$BASE"
if [ -e "$TARGET" ]; then
  STEM=${BASE%.*}
  EXT=${BASE##*.}
  [ "$EXT" = "$BASE" ] && EXT=""          # filename had no extension
  N=1
  while :; do
    if [ -n "$EXT" ]; then CAND="$DEST/${STEM}_${N}.${EXT}"; else CAND="$DEST/${STEM}_${N}"; fi
    [ -e "$CAND" ] || break
    N=$((N + 1))
  done
  TARGET="$CAND"
  echo "move.sh: $BASE already in $2, filing as $(basename "$TARGET")" >&2
fi

if ! mv "$SRC" "$TARGET"; then
  echo "move.sh: mv failed: $SRC -> $TARGET" >&2
  exit 1
fi

printf '%s\t%s\t%s\n' "$(date -Iseconds)" "$(basename "$TARGET")" "$REASON" >> "$DEST/_log.tsv"
echo "moved $BASE -> $2/$(basename "$TARGET")"
