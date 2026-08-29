---
description: Run the fixture through the pipeline and look at the result — verification report plus actual frames.
argument-hint: "[job-dir] [profile]"
allowed-tools: Bash, Read
---

Run screencut end to end and report what came out, looking at pixels rather than
only at exit codes.

Job directory: $1 (default `data/fixtures/demo01`, generating it with
`make fixture` if absent). Profile: $2 (default both).

1. `python3 -m runner.cli run <job> --encoder software`. Note which stages ran and
   which were cached — a second run should do no work at all.
2. Print the verification report. Explain any FAIL, and any WARN that is not the
   known one (the fixture's filler cut leaves one caption block too short to
   read, and there is nowhere to extend it to).
3. Extract three frames per rendered profile, spread across the render, and
   **read them**:
   `ffmpeg -v error -y -ss <t> -i <render> -frames:v 1 -vf scale=<w>:<h> <out>.png`
   Scale to about a third — small enough to read quickly, large enough that a
   caption edge and an overlay border are still legible.
4. Say what the frames show: is the framing on the thing the cursor was doing, is
   the caption inside the frame and readable, are the overlays where they should
   be, is the progress pill advancing.

If something is wrong, hand it to the `render-debug` agent rather than guessing.
