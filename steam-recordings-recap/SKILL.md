---
name: steam-recordings-recap
description: Find Steam Game Recording sessions or saved clips by game, date, or source duration, inspect their gameplay for key moments, and use FFmpeg to assemble a recap of a requested length. Use for local chunked Steam recordings or exported Steam gameplay videos.
---

# Steam Recordings Recap

Turn retained gameplay into a coherent highlight video by selecting observed moments and assembling them with FFmpeg. Use the recording folder supplied by the user, or the `STEAM_RECORDINGS_ROOT` environment variable. If neither is available, inspect local Steam configuration or ask for the recording folder. Never assume a particular drive letter or username. Default output is a unique job folder under the user's `Videos/Steam Recaps`, unless they choose another location outside the recordings folder.

Check that Python 3.9+, `ffmpeg`, and `ffprobe` are available. The helpers use Python's standard library only. Resolve script paths from this loaded `SKILL.md` directory, not the caller's working directory. The PowerShell examples below assume the usual user installation; adapt `$recapSkill` to the actual loaded path and set `$recapRoot` from the user's request if the environment variable is absent. On macOS/Linux, use the corresponding shell paths and `python3` when appropriate.

```powershell
$recapSkill = Join-Path $HOME '.agents\skills\steam-recordings-recap'
$recapRoot = $env:STEAM_RECORDINGS_ROOT
$recapOutput = Join-Path $HOME 'Videos\Steam Recaps'
$job = Join-Path $recapOutput ('recap-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $job -ErrorAction Stop | Out-Null
```

## Find the requested footage

Keep **source duration** separate from **recap duration**. For example, "recap my last hour of 9 Kings in two minutes" means a one-hour source window and a 120-second output. If the output length is missing, ask for it while inspecting the library. If a single duration is ambiguous, clarify before rendering.

Start with metadata discovery, rather than recursively probing every chunk:

```powershell
python "$recapSkill\scripts\steam_recordings.py" inventory --root $recapRoot --output "$job\inventory.json"
```

Filter by `--appid`, `--game`, `--kind`, `--date`, or `--duration` with `--tolerance` as appropriate; use `--help` for exact options. Names come from the local Steam installation and library manifests. Windows Steam paths are discovered automatically; on other platforms or for an undetected installation, provide `--steam-root` pointing to the Steam installation that contains `steamapps/libraryfolders.vdf`. Keep unknown games as app IDs; resolve the requested name through available local metadata or the official Steam store if needed. Do not invent names from the app ID. An unresolved name will not match `--game`; retry inventory without that filter or use `--appid`.

Compare candidates by game, saved clip versus background session, date, declared duration, and retained chunk coverage. A folder's date can be the time a clip was saved, rather than when its gameplay was captured. Prefer timeline recording metadata for capture dates, and state uncertainty when that mapping is missing. Timeline entries alone do not prove footage still exists.

If several sources remain plausible, present a short candidate list and ask which one. When the request clearly identifies a source, continue without a separate approval step. Do not silently concatenate unrelated sessions to satisfy a source-length request. If recordings are shorter than requested or have missing parts, describe the available footage and adapt only when the request permits it.

For format details, native time coordinates, and known pitfalls, read [references/steam-format.md](references/steam-format.md). Treat saved `.pb` metadata as version-dependent; use it only with a verified decoder.

## Materialize the source range

Steam DASH `.m4s` files are separate audio/video fragments. Do not pass raw manifests directly to the editor, sort chunk names lexicographically, or concatenate audio and video fragments into one byte stream. On this library, FFmpeg can exit successfully after reading only the first chunk of a manifest.

Use the helper to reconstruct a requested continuous window into a regular, normalized MP4:

```powershell
python "$recapSkill\scripts\steam_recordings.py" prepare --manifest $manifest --start 0 --duration 600 --height 1080 --output "$job\source.mp4"
```

`--start` is seconds from the logical beginning of the saved clip or retained session, not the original game's launch. Omit `--duration` to use the available remainder. Read the preparation sidecar and warnings: retained background recordings may begin hours into their original timeline, and physical final frames can end before the manifest's declared end. Prepared MP4 time zero maps to the selected range, not necessarily to Steam timeline zero.

For a long session, prepare the source window requested by the user. If that still covers hours, work in adjacent, bounded windows and keep their source offsets; avoid decoding the entire library or copying every session. Check free disk space before large materializations. Stop on internal gaps or changing source chunks and report the affected range. Never alter Steam's recordings, manifests, protobufs, or timeline files.

An ordinary exported MP4 can go straight to the media helper after probing its duration and streams. Preserve its original path in the edit plan.

## Identify moments from evidence

Make an overview storyboard, then inspect its images using the available local image-viewing tool:

```powershell
python "$recapSkill\scripts\recap_media.py" storyboard --input "$job\source.mp4" --output-dir "$job\overview" --interval 15 --columns 4 --rows 4 --width 400
```

Each storyboard needs a new or empty output directory. Choose sample spacing for the game's pace and source length. An overview locates candidate regions; it cannot establish fast action or prove that no other event happened between samples. Re-sample interesting regions at 0.5–2 second intervals, including the lead-in and outcome. For quick fights or uncertain actions, use denser frames or inspect a short preview through an available video-capable tool:

```powershell
python "$recapSkill\scripts\recap_media.py" storyboard --input "$job\source.mp4" --output-dir "$job\detail-01" --start 120 --duration 20 --interval 1 --columns 4 --rows 5 --width 480
python "$recapSkill\scripts\recap_media.py" preview --input "$job\source.mp4" --start 120 --duration 20 --output "$job\preview-01.mp4"
```

Use the storyboard's timestamp mapping to record observed events. Steam achievements and user markers can suggest candidates, but verify them against the available frames and map their milliseconds into prepared-source time first. Scene changes, motion, and loudness are search aids, not proof of an important gameplay event.

Select moments that explain progress or produce a clear payoff: a meaningful encounter, clutch escape, objective, discovery, unusual failure, or visible result. Preserve enough setup and aftermath for the moment to make sense. Drop menus, loading, repeated attempts, and uneventful travel unless they provide needed context. Tailor this judgment to the user's requested style and game.

Default to chronological order and original game audio. Do not add music, narration, speed changes, captions, or dramatic effects unless requested or clearly needed for the requested style. Do not infer dialogue from waveforms, invent player intent, or name bosses/achievements without visible or reliable supporting evidence. If only images are available, describe the review as sampled-frame analysis; do not claim to have watched every frame or listened to audio.

## Budget and serialize the edit

Write an edit plan before rendering. Each clip needs a source path, an inclusive start and exclusive end in prepared-source seconds, and a short reason grounded in what was observed. Use disjoint intervals and a consistent time origin. Include both setup and outcome within the duration budget.

Example plan for a 30-second recap (replace the example moments and times with real selections):

```json
{
  "target_duration": 30,
  "chronological": true,
  "clips": [
    {"source": "source.mp4", "start": 42, "end": 52, "reason": "Observed setup and encounter"},
    {"source": "source.mp4", "start": 118, "end": 130, "reason": "Observed turning point"},
    {"source": "source.mp4", "start": 201, "end": 209, "reason": "Observed outcome"}
  ]
}
```

Make the selected durations sum to the output target at the chosen frame rate. Rebalance the selections before rendering, so the renderer's duration limit does not accidentally cut a payoff. If the footage cannot support the requested duration, explain the constraint instead of repeating, freezing, or padding gameplay. Multiple prepared sources are supported; keep their chronological order explicit. Set `chronological` to `false` only when intentionally making a requested reordered edit.

```powershell
python "$recapSkill\scripts\recap_media.py" render --plan "$job\edit-plan.json" --output "$job\recap.mp4" --width 1920 --height 1080 --fps 30
```

The helper re-encodes cuts for accurate boundaries, normalizes video/audio for concatenation, uses hard cuts, and creates an edit sidecar. Its default audio policy selects the first audio stream and supplies silence for a source without audio. A clip's optional `audio_stream` selects another audio-stream ordinal, starting at zero. If the user requests mixing, inspect the streams and adapt the FFmpeg command deliberately rather than losing a requested track.

Match the source's aspect ratio unless the user asks for another format. The helper pads rather than crops when the output ratio differs. Choose dimensions that avoid unnecessary upscaling. The target and individual selected intervals round down to whole output frames; a nonrepresentable target is shortened by less than one frame. Budget using those rounded intervals. For exact durations, use a frame rate at which the target is representable and report any remaining difference.

## Verify and deliver

Check the rendered duration against the target, video/audio streams, decoded output, and joins. Inspect frames around selected moments and cut boundaries; confirm that the edit includes the outcomes described in its plan. File existence and FFmpeg's exit code are insufficient, especially with Steam manifests.

Return a playable link to the recap's absolute local path, its actual duration, and a brief description of the included moments. Keep the edit plan and sidecars with the output so a later request can revise the selection. Mention meaningful limitations such as missing source footage, sampled-only analysis, or unavailable audio review. Do not publish or upload footage as part of a local recap request.
