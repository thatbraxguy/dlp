#!/bin/bash
# Builds tiny test clips (progressive MP4 + HLS + DASH) under tests/fixtures/media.
set -euo pipefail
cd "$(dirname "$0")/fixtures"
rm -rf media

clip() {  # clip <dir> <seconds>
  local d="media/$1"
  mkdir -p "$d"
  ffmpeg -v error -y \
    -f lavfi -i "testsrc2=size=320x180:rate=15:duration=$2" \
    -f lavfi -i "sine=frequency=440:duration=$2" \
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p -g 15 -c:a aac -b:a 48k \
    -movflags +faststart "$d/clip_180p.mp4"
}

# Two different videos, each as MP4, HLS (in hls/) and DASH (in dash/)
for spec in "vid10001 2" "vid20002 4"; do
  set -- $spec
  clip "$1" "$2"
  d="media/$1"
  mkdir -p "$d/hls" "$d/dash"
  ffmpeg -v error -y -i "$d/clip_180p.mp4" -c copy -f hls -hls_time 1 -hls_playlist_type vod \
    -hls_segment_filename "$d/hls/seg%d.ts" -master_pl_name master.m3u8 "$d/hls/media.m3u8"
  (cd "$d/dash" && ffmpeg -v error -y -i ../clip_180p.mp4 -c copy -f dash -seg_duration 1 manifest.mpd)
  # the same manifests with no file extension, to prove detection is by content
  cp "$d/hls/master.m3u8" "$d/hls/master"
  cp "$d/dash/manifest.mpd" "$d/dash/manifest"
done

# A third video with HLS and DASH side by side under one stem (for sibling probing)
clip vid30003 2
d=media/vid30003
ffmpeg -v error -y -i "$d/clip_180p.mp4" -c copy -f hls -hls_time 1 -hls_playlist_type vod \
  -hls_segment_filename "$d/hseg%d.ts" "$d/stream.m3u8"
(cd "$d" && ffmpeg -v error -y -i clip_180p.mp4 -c copy -f dash -seg_duration 1 stream.mpd)
rm "$d/clip_180p.mp4"

echo "fixtures ready: $(find media -type f | wc -l | tr -d ' ') files"
