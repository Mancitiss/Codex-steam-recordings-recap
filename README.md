# Steam Recordings Recap for Codex

A reusable Codex skill that finds Steam Game Recording sessions or saved clips, reviews gameplay for key moments, and uses FFmpeg to assemble a recap of a requested length.

It supports Steam's native `.mpd` / `.m4s` recordings and ordinary exported videos. Codex makes the editorial choices from timestamped frames or available video tools; the Python helpers reconstruct footage, generate storyboards, and render the edit. The helpers do not identify gameplay events by themselves.

## Requirements

- Codex with local file access, command execution, and image viewing available.
- Python **3.9 or newer**, available as `python` or `python3`. No pip packages are required.
- `ffmpeg` and `ffprobe` on `PATH`. The FFmpeg build needs `libx264`, AAC encoding, a decoder for your source footage (often HEVC), and the `drawtext` / `tile` filters. Timestamp labels also need an installed font.
- Access to the recordings on the device doing the work, plus space for intermediate videos and the finished recap.

Get dependencies from [Python](https://www.python.org/downloads/) and [FFmpeg](https://ffmpeg.org/download.html), or your operating system's package manager. Add their executable directories to `PATH`, then check in a new terminal:

```powershell
python --version
ffmpeg -version
ffprobe -version
```

On macOS/Linux, use `python3` if `python` is unavailable. Windows has been tested with real Steam footage. Other operating systems have not been tested; use explicit recording and Steam installation paths there.

## Install on another device

Copy or download this repository onto the other device. Install the **`steam-recordings-recap` subfolder**, including its scripts and references. Copying only `SKILL.md` is insufficient.

The user skill location in the current [Codex skills guide](https://learn.chatgpt.com/docs/build-skills) is `~/.agents/skills`. The final layout should be:

```text
~/.agents/skills/
└── steam-recordings-recap/
    ├── SKILL.md
    ├── agents/
    │   └── openai.yaml
    ├── references/
    │   └── steam-format.md
    └── scripts/
        ├── steam_recordings.py
        └── recap_media.py
```

### Windows / PowerShell

Open PowerShell **in this repository's root folder**, then run:

```powershell
$skillSource = Join-Path (Get-Location) 'steam-recordings-recap'
$skillsDirectory = Join-Path $HOME '.agents\skills'
$skillDestination = Join-Path $skillsDirectory 'steam-recordings-recap'

if (-not (Test-Path -LiteralPath (Join-Path $skillSource 'SKILL.md'))) {
    throw 'Open PowerShell in the repository root first.'
}
if (Test-Path -LiteralPath $skillDestination) {
    throw 'This skill is already installed. Back up and update that copy instead.'
}

New-Item -ItemType Directory -Path $skillsDirectory -Force | Out-Null
Copy-Item -LiteralPath $skillSource -Destination $skillDestination -Recurse -ErrorAction Stop
Test-Path -LiteralPath (Join-Path $skillDestination 'SKILL.md')
```

The last command should print `True`.

### macOS / Linux

Open a terminal **in this repository's root folder**, then run:

```sh
test -f steam-recordings-recap/SKILL.md || { echo 'Run from the repository root.'; exit 1; }
skills_directory="$HOME/.agents/skills"
skill_destination="$skills_directory/steam-recordings-recap"
test ! -e "$skill_destination" || { echo 'Skill already installed; back up and update that copy.'; exit 1; }
mkdir -p "$skills_directory"
cp -R steam-recordings-recap "$skill_destination"
test -f "$skill_destination/SKILL.md" && echo 'Installed'
```

Start a new Codex chat and invoke `$steam-recordings-recap`. If it does not appear, restart Codex and check the folder layout above. Avoid duplicate installations with the same skill name. Existing Codex setups may already keep this skill under `~/.codex/skills`; update the existing copy rather than installing a second one.

## Choose folders for each device

Find the recording folder in **Steam → Settings → Game Recording**. Use the folder containing `video`, `clips`, and `timelines`, rather than an individual chunk file. This package does not assume an `A:` drive or a particular username.

The simplest configuration is to include both paths in your request:

```text
Use $steam-recordings-recap to turn my latest background recording session
of 9 Kings into a two-minute recap.
My recordings are in E:\Steam Recordings.
Save the result in E:\Steam Recaps.
```

If no output folder is specified, the skill uses a unique job folder under `~/Videos/Steam Recaps`. The output must be outside the Steam recording folder.

Optionally set `STEAM_RECORDINGS_ROOT` before launching Codex. For the current PowerShell terminal:

```powershell
$env:STEAM_RECORDINGS_ROOT = 'E:\Steam Recordings'
```

For persistent Windows user configuration, run this once, then restart Codex so it inherits the setting:

```powershell
[Environment]::SetEnvironmentVariable('STEAM_RECORDINGS_ROOT', 'E:\Steam Recordings', 'User')
```

On macOS/Linux, set it in the shell that launches Codex, or your usual shell startup configuration:

```sh
export STEAM_RECORDINGS_ROOT="/path/to/Steam Recordings"
```

Passing `--root` to the inventory helper overrides this environment variable. Without either setting, the helper requests `--root` instead of guessing a recording location. `prepare --manifest` normally infers the protected recording root from the manifest's directory layout.

## Use the skill

Example requests:

```text
Use $steam-recordings-recap to recap my latest 9 Kings background session in two minutes.
```

```text
Use $steam-recordings-recap to find a roughly 20-minute Silksong clip
from yesterday and make a 60-second recap.
```

```text
Use $steam-recordings-recap to recap this exported MP4 in 30 seconds:
E:\Gameplay\boss-fight.mp4
```

The skill inventories candidates, reviews timestamped storyboards, selects meaningful intervals, and renders a chronological H.264/AAC MP4. It preserves source audio by default, omits idle stretches, and keeps an edit plan and timing sidecars alongside the output for revisions. Sampled-frame review has limits; it does not imply every frame was watched or audio was listened to.

## Verify the installed helpers

For a Windows user installation:

```powershell
$recapSkill = Join-Path $HOME '.agents\skills\steam-recordings-recap'
python "$recapSkill\scripts\steam_recordings.py" inventory --help
python "$recapSkill\scripts\recap_media.py" render --help
python "$recapSkill\scripts\steam_recordings.py" inventory --root 'E:\Steam Recordings'
```

An inventory should return JSON with recording candidates. It reads metadata and does not render a recap.

On Windows, Steam installation paths are discovered automatically. If game names are missing, supply `--steam-root` pointing to the Steam installation containing `steamapps/libraryfolders.vdf`. On macOS/Linux, provide this explicitly when resolving game names:

```sh
python3 "$HOME/.agents/skills/steam-recordings-recap/scripts/steam_recordings.py" inventory \
  --root "/path/to/Steam Recordings" \
  --steam-root "/path/to/Steam"
```

Only games with local app manifests can be named offline. Unknown names remain app IDs; use `--appid` or ask Codex to resolve the name. Filtering with `--game` will not match a game whose name is unknown locally.

## Notes and troubleshooting

- **Skill missing:** Check that `steam-recordings-recap/SKILL.md` sits directly inside the installed skill folder, with no extra repository directory nested between it and `skills`.
- **FFmpeg or FFprobe missing:** Add the directory containing both executables to `PATH`, then reopen Codex.
- **Timestamp/font errors:** Check `ffmpeg -filters` for `drawtext` and `tile`, and install a normal system font or a fuller FFmpeg build if needed.
- **Missing early footage:** Steam can remove old background chunks. The skill can recap only the footage still retained; timeline metadata alone does not recover deleted video.
- **Raw manifest looks empty or too short:** Use the reconstruction helper. Some Steam manifests can produce incomplete results through FFmpeg's DASH input even when FFmpeg reports success.
- **Long sessions:** Prepare bounded source windows rather than transcoding the entire library at once. Working files can be large. Save the finished MP4 before removing its job folder; removing that folder also removes its edit plan and revision sources.
- **Unsupported manifests:** The native helper handles static, single-period, fixed-duration SegmentTemplate recordings with one video track and at most one audio track. Other layouts are reported rather than guessed.

The package contains only the skill, helper scripts, and format notes. It includes no gameplay footage, recordings, private paths, or API keys. It does not upload or publish footage, and installing it does not install Python or FFmpeg.
