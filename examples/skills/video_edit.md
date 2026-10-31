# Video edit

Edit video with ffmpeg: trim, crop, scale, concatenate, title cards, and face-aware framing. Produces YouTube-compatible mp4 (H.264, yuv420p).

## Before you start

Check the tools exist in the execution environment first. If ffmpeg is missing from a local sandbox, it is an approved apt package for a capability image build; if it is missing on the host, install it with approval. Never claim an edit worked without verifying it.

```
ffmpeg -version
ffprobe -version
```

## Trim

Cut a segment without re-encoding when no crop or scale follows. Fast, exact enough for cuts on keyframes; use re-encoding when frame accuracy matters.

```
ffmpeg -y -ss START -t DURATION -i input.mp4 -c copy trimmed.mp4
```

## Crop and scale

Crop with the crop filter, then scale to the delivery size. Always re-encode for YouTube compatibility, and always set yuv420p: some players reject other pixel formats.

```
ffmpeg -y -i input.mp4 -vf "crop=W:H:X:Y,scale=1280:720" -c:v libx264 -pix_fmt yuv420p -c:a copy cropped.mp4
```

If the input has no audio track, drop `-c:a copy`. If libx264 is unavailable, fall back to mpeg4 and say so in the result.

## Concatenate

For clips with the same codec and size, use the concat demuxer (no re-encode). Otherwise re-encode every clip to the same settings first, then concatenate.

```
printf "file 'a.mp4'\nfile 'b.mp4'\n" > list.txt
ffmpeg -y -f concat -safe 0 -i list.txt -c copy joined.mp4
```

## Title card

Build a short card from a color source and overlay text only when a font file exists. Check first; if no font is present, skip the card and say so rather than failing the whole edit.

```
ls /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
ffmpeg -y -f lavfi -i color=c=black:s=1280x720:d=3 -vf "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='TITLE':fontsize=64:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2" -c:v libx264 -pix_fmt yuv420p card.mp4
```

## Face-aware framing (advanced)

When the goal asks for the person framed rather than a fixed crop, detect the face with OpenCV Haar cascades. opencv-python is not an approved package, so building an image with it requires owner approval first; never install it silently.

1. Build a capability image with opencv-python (approval-gated), or use an environment where it is already installed.
2. Sample frames at 1 fps and detect the largest face per frame with `haarcascade_frontalface_default.xml`, which ships with OpenCV.
3. Take the median face box across frames, expand it about 2.2x around its center for headroom, and clamp it to the frame.
4. Crop with ffmpeg using the computed box, then scale to the delivery size.
5. If no face is found in more than half the sampled frames, fall back to a center crop and say so plainly in the result: a silent center crop must never pose as face-aware.

## Verify every edit

Never report an edit done without evidence. After each output, check duration and dimensions, and extract one mid-point frame to confirm the framing.

```
ffprobe -v error -show_entries format=duration -show_entries stream=width,height -of default=noprint_wrappers=1 output.mp4
ffmpeg -y -ss 5 -i output.mp4 -frames:v 1 check.jpg
```

Repair rules: wrong duration means re-trim; wrong dimensions mean re-scale; a cut-off face means recompute the crop box with more padding and re-crop. The original input is never overwritten; every edit writes a new file.
