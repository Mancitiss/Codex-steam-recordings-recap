# Local Steam recording format

Observed in a Windows Steam recording library on 2026-11-16. Inspect current metadata when using the skill; these are implementation observations rather than a public Steam storage contract. Recording and output folders are configured per device.

## Layout and source selection

- `video/bg_<appid>_<YYYYMMDD>_<HHMMSS>/session.mpd` describes a background/on-demand recording with sibling `init-stream<ID>.m4s` and `chunk-stream<ID>-<number>.m4s` files.
- `clips/clip_<appid>_<date>_<time>/` contains `clip.pb`, a thumbnail, copied timelines, and one or more nested `video/<recording-id>/session.mpd` directories. The clip creation date is not necessarily the date of the captured gameplay. A clip with several manifests may span recordings; preserve their order and verified metadata mapping instead of treating one manifest as the whole clip.
- `timelines/timeline_<id>.json` and `gamerecording.pb` contain metadata that can outlive the video. Inventory the physical media, not just the timelines.
- Ordinary `.mp4` exports can also be present. Do not treat them as fragments or count them twice as native sessions.

Recorded game IDs can be resolved offline through Steam's installation. On Windows, read `SteamPath` from `HKCU\Software\Valve\Steam`, then parse `steamapps/libraryfolders.vdf` and each library's `steamapps/appmanifest_<appid>.acf` for the `name` value. Supply `--steam-root` when automatic discovery is unavailable, including on macOS/Linux. This avoids requiring an online lookup for installed games. Unknown app IDs remain unknown until resolved.

## Time coordinates

The observed manifests use static DASH, one Period, representations with SegmentTemplate, a 1,000,000 template timescale, and nominal 3-second segments. Representation IDs connect the initialization and numbered fragment filenames. Support must be checked against the current XML rather than assuming these values everywhere.

`MPD@mediaPresentationDuration` is the logical selected duration. `Period@start` is a nonzero media-time origin. For a saved clip, its first fragment can contain footage before that logical origin and the last fragment can contain footage after its logical end. For background recordings, the first retained chunk may have a large number because earlier material was removed. Numeric continuity is significant; filenames alone do not establish exact playable duration.

Example retained background: `bg_2784470_20261115_074430` began at chunk 2293, logical media time 6876 seconds, declared length 10798.647 seconds. Its physical final video frame ended around 17674.350 seconds, slightly before the declared endpoint. Treat declared duration as metadata until the requested range has been verified against decoded streams.

Example saved clip: `clip_1030300_20250904_191217` had logical start 9078.535 seconds and duration 11.999 seconds, but first-fragment video began around 9078.006 and the copied chunks extended beyond the logical end. Blindly joining chunks would include extra boundary footage.

The helpers prepare a regular MP4 whose time zero is the chosen logical range. Preserve its sidecar when converting selected moments back to Steam coordinates.

## Why direct DASH input is avoided

With the tested Windows FFmpeg build and these Steam manifests, FFprobe sometimes reported zero duration while showing valid streams. Direct FFmpeg input could read only the first 3-second chunk and still return exit code zero. Input seeking could produce an empty file with a successful process exit. These behaviors were reproduced on a real saved clip and a retained background recording.

Reconstruction therefore joins initialization and selected numbered fragments **separately for each track** in a private workspace, probes the resulting regular fragmented MP4 tracks, and seeks against their actual start times. Video and audio must share the same requested absolute media-time origin; independently resetting their first samples would discard their original offset. Re-encode the selected window to obtain accurate clip edges and an ordinary seekable MP4.

Do not skip an internal hole, fall back to an unverified glob ordering, or repair Steam's original files. Handle an incomplete or changing tail explicitly. Full declared duration can be longer than physical playable coverage, so report any clamped endpoint.

## Timeline and protobuf metadata

Observed JSON timeline entry `time` values are milliseconds; `daterecorded` is a Unix timestamp stored as a string. Types included achievements, screenshots, user markers, and errors. A timestamp is a candidate cue rather than proof that an event appears in the retained range.

The observed protobuf layout is not guaranteed stable. Inspection found saved-clip fields for title, game ID, clip timeline start, timeline ranges, and a recording's timeline-zero offset. For a verified matching schema, the original timeline timestamp maps to media PTS by subtracting the recording-zero timeline offset. Then subtract the prepared source's absolute start PTS to get its local seconds. Do not apply this relationship to unmatched timelines or guess undocumented protobuf fields. Without a verified mapping, use the frames and local MP4 timestamps for the edit.

## Primary references

- [Steam Game Recording support](https://help.steampowered.com/en/faqs/view/23B7-49AD-4A28-9590): recording modes, temporary background storage, clips, timeline cues, and export behavior. It does not document a stable raw file schema.
- [FFmpeg CLI documentation](https://ffmpeg.org/ffmpeg.html): seeking, timestamp handling, stream mapping, and encoding.
- [FFmpeg formats documentation](https://ffmpeg.org/ffmpeg-formats.html): fragmented MP4 and concatenation behavior.
- [FFmpeg filters documentation](https://ffmpeg.org/ffmpeg-filters.html): scaling, frame sampling, drawtext, and tiling.
