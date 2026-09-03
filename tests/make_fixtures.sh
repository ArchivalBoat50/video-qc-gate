#!/bin/sh
# Generates the synthetic clips scripts/test_qc.py expects. Requires ffmpeg.
# The tests never send these to a model — the Gemini call is mocked — so the
# pixels are irrelevant. Only duration, aspect ratio and silence are checked
# by the objective gate, and those are what this reproduces.
set -e
cd "$(dirname "$0")/fixtures"
mkdir -p stock reels
mk() {  # mk <path> <seconds> <WxH>
  ffmpeg -loglevel error -y -f lavfi -i "testsrc=size=$3:rate=30:duration=$2" \
         -pix_fmt yuv420p -an "$1"
}
mk stock/V13_dawnfield.mp4      7 1080x1920   # in-spec vertical, silent
mk stock/V21_frozenpond.mp4     7 1080x1920   # in-spec; used for identity tests
mk stock/20_snow_raw.mp4        7 1080x1920   # in-spec vertical
mk stock/V09_haybale.mp4        7 1080x1920   # in-spec; rotated to landscape by test [2]
mk stock/V05_deerblind.mp4      7 1080x1920   # in-spec; routed to review/ by the mock
mk stock/V10_hardware.mp4       7 1080x1920   # in-spec; the mocked API-error clip
mk stock/too_long.mp4          12 1080x1920   # fails the 5-9s duration gate
mk stock/horizontal.mp4         7 1920x1080   # fails the 1.6 aspect gate
mk reels/004_V13_dawnfield.mp4  8 1080x1920   # finished-post stage fixture
echo "fixtures written to $(pwd)"
