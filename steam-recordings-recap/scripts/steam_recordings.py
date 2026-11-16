#!/usr/bin/env python3
"""Inspect local Steam DASH recordings and prepare an accurately bounded MP4.

Python standard library plus ffmpeg/ffprobe on PATH. Recording files are read-only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

DEFAULT_ROOT = (Path(os.environ["STEAM_RECORDINGS_ROOT"]).expanduser()
                if os.environ.get("STEAM_RECORDINGS_ROOT") else None)


class RecordingError(Exception):
    pass


def duration_seconds(text):
    match = re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", text or "")
    if not match or not any(match.groups()):
        raise RecordingError(f"Unsupported MPD duration: {text!r}")
    h, m, s = (float(x or 0) for x in match.groups())
    return h * 3600 + m * 60 + s


def tag(element):
    return element.tag.rsplit("}", 1)[-1]


def children(element, name):
    return [child for child in element if tag(child) == name]


def one(element, name, required=True):
    found = children(element, name)
    if len(found) > 1 or (required and not found):
        raise RecordingError(f"Expected one {name}; found {len(found)}")
    return found[0] if found else None


def contained(path, root):
    return path.resolve().is_relative_to(root.resolve())


def local_file(folder, relative):
    if not relative or ":" in relative or "\\" in relative or relative.startswith("/"):
        raise RecordingError(f"Unsupported non-local MPD path: {relative!r}")
    result = (folder / relative).resolve()
    if not contained(result, folder):
        raise RecordingError(f"MPD path escapes its recording folder: {relative!r}")
    return result


def numeric_runs(numbers):
    runs = []
    for number in numbers:
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    return runs


def parse_manifest(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise RecordingError(f"Manifest not found: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise RecordingError(f"Invalid MPD XML: {exc}") from exc
    if tag(root) != "MPD" or root.get("type", "static") != "static":
        raise RecordingError("Only static local Steam MPD manifests are supported")
    period = one(root, "Period")
    if one(root, "BaseURL", required=False) is not None or one(period, "BaseURL", required=False) is not None:
        raise RecordingError("MPDs with BaseURL are not supported")
    period_start = duration_seconds(period.get("start", "PT0S"))
    logical_duration = duration_seconds(root.get("mediaPresentationDuration"))
    if not math.isfinite(logical_duration) or logical_duration <= 0:
        raise RecordingError("MPD duration must be positive")
    tracks = []
    for adaptation in children(period, "AdaptationSet"):
        if one(adaptation, "BaseURL", required=False) is not None:
            raise RecordingError("MPDs with adaptation BaseURL are not supported")
        representation = one(adaptation, "Representation")
        if one(representation, "BaseURL", required=False) is not None:
            raise RecordingError("MPDs with representation BaseURL are not supported")
        template = one(representation, "SegmentTemplate", required=False)
        if template is None:
            template = one(adaptation, "SegmentTemplate", required=False)
        if template is None or children(template, "SegmentTimeline"):
            raise RecordingError("Only fixed-duration SegmentTemplate MPDs are supported")
        rep_id = representation.get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", rep_id):
            raise RecordingError(f"Unsupported Representation ID: {rep_id!r}")
        init_pattern = template.get("initialization", "").replace("$RepresentationID$", rep_id)
        media = template.get("media", "").replace("$RepresentationID$", rep_id)
        match = re.search(r"\$Number(?:%0?(\d+)d)?\$", media)
        if not match or "$" in media[:match.start()] + media[match.end():]:
            raise RecordingError(f"Unsupported chunk template: {media!r}")
        local_file(path.parent, media[:match.start()] + "1" + media[match.end():])
        if "/" in media:
            raise RecordingError("Subdirectory chunk templates are not supported")
        pattern = re.compile(re.escape(media[:match.start()]) + r"(\d+)" + re.escape(media[match.end():]))
        files = {}
        for candidate in path.parent.iterdir():
            chunk_match = pattern.fullmatch(candidate.name)
            if chunk_match and candidate.is_file():
                number = int(chunk_match.group(1))
                if number in files:
                    raise RecordingError(f"Duplicate numbered chunk {number}")
                files[number] = candidate.resolve()
        numbers = sorted(files)
        timescale = int(template.get("timescale", "1"))
        ticks = int(template.get("duration", "0"))
        if timescale <= 0 or ticks <= 0:
            raise RecordingError("Chunk duration and timescale must be positive")
        kind = adaptation.get("contentType") or representation.get("mimeType", "").split("/")[0]
        if kind not in {"video", "audio"}:
            raise RecordingError(f"Unsupported track content type {kind!r}")
        tracks.append({
            "id": rep_id, "kind": kind,
            "codec": representation.get("codecs", ""),
            "width": int(representation.get("width", "0")),
            "height": int(representation.get("height", "0")),
            "init": local_file(path.parent, init_pattern),
            "files": files, "numbers": numbers,
            "template_start_number": int(template.get("startNumber", "1")),
            "segment_seconds": ticks / timescale,
        })
    if sum(track["kind"] == "video" for track in tracks) != 1 or sum(track["kind"] == "audio" for track in tracks) > 1:
        raise RecordingError("Expected one video track and at most one audio track")
    return {"path": path, "period_start_seconds": period_start,
            "logical_duration_seconds": logical_duration, "tracks": tracks}


def steam_names(steam_root=None):
    roots = []
    if steam_root:
        roots.append(Path(steam_root).expanduser())
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                roots.append(Path(winreg.QueryValueEx(key, "SteamPath")[0]))
        except OSError:
            pass
    for name in ("ProgramFiles(x86)", "ProgramFiles"):
        if os.environ.get(name):
            roots.append(Path(os.environ[name]) / "Steam")
    libraries = set()
    for root in roots:
        libraries.add(root.resolve())
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            text = vdf.read_text(encoding="utf-8", errors="replace")
            for value in re.findall(r'"path"\s+"((?:\\.|[^"\\])*)"', text):
                libraries.add(Path(value.replace("\\\\", "\\")).resolve())
    names = {}
    for library in libraries:
        for manifest in (library / "steamapps").glob("appmanifest_*.acf"):
            app_match = re.fullmatch(r"appmanifest_(\d+)\.acf", manifest.name)
            if not app_match:
                continue
            text = manifest.read_text(encoding="utf-8", errors="replace")
            name = re.search(r'"name"\s+"((?:\\.|[^"\\])*)"', text)
            if name:
                names[app_match.group(1)] = name.group(1).replace('\\"', '"').replace("\\\\", "\\")
    return names


def recording_identity(path):
    for parent in [path.parent, *path.parents]:
        match = re.fullmatch(r"(clip|bg|manual)_(\d+)_(\d{8})_(\d{6})", parent.name)
        if match:
            # Prefer a clip identity over its nested bg identity.
            if match.group(1) == "clip":
                return match.group(2), "clip", parent.name, match.group(3) + "_" + match.group(4)
    match = re.fullmatch(r"(?:bg|manual)_(\d+)_(\d{8})_(\d{6})", path.parent.name)
    if match:
        return match.group(1), "session", path.parent.name, match.group(2) + "_" + match.group(3)
    return None, "session", path.parent.name, None


def scan_recordings(root):
    if not root.is_dir():
        raise RecordingError(f"Recording root not found: {root}")
    paths = list((root / "video").glob("*/session.mpd"))
    paths.extend((root / "clips").glob("*/video/*/session.mpd"))
    return sorted(paths)


def inventory(args):
    root = args.root.expanduser().resolve()
    names = steam_names(args.steam_root)
    recordings = []
    for path in scan_recordings(root):
        appid, kind, source_id, date_label = recording_identity(path)
        if args.appid and appid != args.appid:
            continue
        if args.kind != "all" and kind != args.kind:
            continue
        if args.date and args.date.replace("-", "") not in (date_label or ""):
            continue
        name = names.get(appid, "")
        if args.game and args.game.casefold() not in name.casefold():
            continue
        row = {"source_id": source_id, "kind": kind, "appid": appid,
               "game_name": name or None, "directory_date_label": date_label,
               "manifest": str(path.resolve()), "coverage_is_estimate": True}
        try:
            parsed = parse_manifest(path)
            duration = parsed["logical_duration_seconds"]
            if args.duration is not None and abs(duration - args.duration) > args.tolerance:
                continue
            row.update({"logical_start_media_pts_seconds": parsed["period_start_seconds"],
                        "logical_duration_seconds": duration,
                        "logical_end_media_pts_seconds": parsed["period_start_seconds"] + duration,
                        "tracks": []})
            for track in parsed["tracks"]:
                numbers = track["numbers"]
                missing = ([n for n in range(numbers[0], numbers[-1] + 1) if n not in track["files"]] if numbers else [])
                row["tracks"].append({"id": track["id"], "kind": track["kind"],
                                      "codec": track["codec"], "width": track["width"], "height": track["height"],
                                      "init_exists": track["init"].is_file(), "chunk_count": len(numbers),
                                      "first_chunk": numbers[0] if numbers else None,
                                      "last_chunk": numbers[-1] if numbers else None,
                                      "contiguous_runs": numeric_runs(numbers), "missing_numbers": missing,
                                      "segment_seconds": track["segment_seconds"]})
            row["status"] = "retained_candidate" if all(t["numbers"] and t["init"].is_file() for t in parsed["tracks"]) else "missing_media"
        except (RecordingError, ValueError, OSError) as exc:
            row["status"] = "unsupported_or_unreadable"
            row["error"] = str(exc)
        recordings.append(row)
    data = {"root": str(root), "recordings": recordings,
            "notes": ["Durations and logical media PTS come from Steam MPDs; boundary frames and current coverage must be verified during prepare.",
                      "Directory timestamps are labels; they are not proof of exact gameplay capture wall time.",
                      "Standalone exported MP4 files and timeline-only sessions are not returned as retained recording candidates.",
                      "Game names are resolved offline from installed Steam app manifests; unknown names remain null. Use --appid when needed."]}
    if args.output:
        check_output(args.output, root)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_new_json(args.output, data)
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))


def check_output(path, root):
    if contained(path, root):
        raise RecordingError(f"Output must be outside the recording root: {path}")
    if path.exists():
        raise RecordingError(f"Refusing to overwrite existing output: {path}")


def write_new_json(path, data):
    with Path(path).open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def fingerprint(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def assert_unchanged(path, expected):
    if fingerprint(path) != expected:
        raise RecordingError(f"Source changed while preparing; retry a completed session: {path}")


def run(command, **kwargs):
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, **kwargs)
    except FileNotFoundError as exc:
        raise RecordingError(f"Executable not found: {command[0]}") from exc
    if result.returncode:
        stderr = result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else result.stderr
        raise RecordingError(f"{command[0]} failed (exit {result.returncode}):\n{stderr[-5000:]}")
    return result


def probe_fragment(init, chunk, fingerprints):
    payload = b""
    for path in (init, chunk):
        expected = fingerprints.setdefault(path, fingerprint(path))
        assert_unchanged(path, expected)
        payload += path.read_bytes()
        assert_unchanged(path, expected)
    result = run(["ffprobe", "-v", "error", "-show_entries", "packet=pts_time,duration_time",
                  "-of", "json", "-i", "pipe:0"], input=payload)
    data = json.loads(result.stdout)
    packets = [packet for packet in data.get("packets", []) if "pts_time" in packet]
    if not packets:
        raise RecordingError(f"No timed packets in fragment: {chunk}")
    start = min(float(packet["pts_time"]) for packet in packets)
    end = max(float(packet["pts_time"]) + float(packet.get("duration_time", "0")) for packet in packets)
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        raise RecordingError(f"Invalid fragment timestamps: {chunk}")
    return start, end


def detected_root(manifest, fallback):
    for parent in manifest.parents:
        if parent.name.lower() == "clips":
            return parent.parent
        if parent.name.lower() == "video" and parent.parent.name.lower() != "clips" and not parent.parent.name.startswith("clip_"):
            return parent.parent
    return fallback


def prepare(args):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RecordingError("ffmpeg and ffprobe must be installed and on PATH")
    manifest_path = args.manifest.resolve()
    fallback_root = args.root.expanduser().resolve() if args.root is not None else manifest_path.parent
    source_root = detected_root(manifest_path, fallback_root)
    output = args.output.resolve()
    sidecar = output.with_suffix(".source.json")
    if output.suffix.lower() != ".mp4":
        raise RecordingError("Prepared output must have the .mp4 extension")
    for destination in (output, sidecar):
        check_output(destination, source_root)
        if contained(destination, manifest_path.parent):
            raise RecordingError("Output must be outside the source manifest directory")
    manifest_stat = fingerprint(manifest_path)
    parsed = parse_manifest(manifest_path)
    if args.start < 0 or args.start >= parsed["logical_duration_seconds"]:
        raise RecordingError("--start must be within the logical source duration")
    remaining = parsed["logical_duration_seconds"] - args.start
    requested_duration = args.duration if args.duration is not None else remaining
    if requested_duration <= 0 or requested_duration > remaining + 0.000001:
        raise RecordingError(f"Requested duration exceeds MPD logical range; remaining {remaining:.6f}s")
    target_start = parsed["period_start_seconds"] + args.start
    target_end = target_start + requested_duration
    fingerprints = {manifest_path: manifest_stat}
    plans = []
    warnings = []
    for track in parsed["tracks"]:
        if not track["init"].is_file() or not track["numbers"]:
            raise RecordingError(f"Missing init or media fragments for {track['kind']} track")
        numbers = track["numbers"]
        first_start, _ = probe_fragment(track["init"], track["files"][numbers[0]], fingerprints)
        step = track["segment_seconds"]
        estimated_first = numbers[0] + math.floor((target_start - first_start) / step)
        estimated_last = numbers[0] + math.floor((target_end - first_start) / step)
        low = max(numbers[0], estimated_first - 2)
        high = min(numbers[-1], estimated_last + 2)
        if low > high:
            raise RecordingError(f"Requested range has no retained {track['kind']} fragments")
        missing = [number for number in range(low, high + 1) if number not in track["files"]]
        if missing:
            raise RecordingError(f"Missing {track['kind']} fragments in selected range: {missing[:30]}")
        selected = [track["files"][number] for number in range(low, high + 1)]
        actual_start, _ = probe_fragment(track["init"], selected[0], fingerprints)
        _, actual_end = probe_fragment(track["init"], selected[-1], fingerprints)
        if target_start < actual_start - 0.05:
            raise RecordingError(f"Requested start is before retained {track['kind']} coverage: earliest media PTS {actual_start:.6f}s")
        if actual_end < target_end - 0.05:
            if args.duration is not None:
                raise RecordingError(f"Requested end exceeds retained {track['kind']} coverage; available duration from requested start {max(0, actual_end-target_start):.6f}s")
            warnings.append(f"Clamped MPD tail to actual {track['kind']} packet coverage ({actual_end:.6f}s media PTS)")
            target_end = min(target_end, actual_end)
        plans.append({"track": track, "selected": selected, "actual_start": actual_start, "actual_end": actual_end,
                      "first_number": low, "last_number": high})
    duration = target_end - target_start
    if duration <= 0.05:
        raise RecordingError("Retained requested range is too short to prepare")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="steam-prepare-", dir=output.parent) as temporary:
        temporary = Path(temporary)
        inputs = []
        indices = {}
        for index, plan in enumerate(plans):
            track_file = temporary / f"track-{index}.mp4"
            track = plan["track"]
            with track_file.open("xb") as destination:
                for source in [track["init"], *plan["selected"]]:
                    expected = fingerprints.setdefault(source, fingerprint(source))
                    assert_unchanged(source, expected)
                    with source.open("rb") as handle:
                        shutil.copyfileobj(handle, destination, length=1024 * 1024)
                    assert_unchanged(source, expected)
            # -ss remains relative to this temporary track's actual start; -copyts
            # preserves original PTS for shared absolute trim/setpts below.
            seek = max(0, target_start - plan["actual_start"])
            inputs.extend(["-ss", f"{seek:.9f}", "-i", str(track_file)])
            indices[track["kind"]] = index
        filters = [f"[{indices['video']}:v:0]trim=start={target_start:.9f}:end={target_end:.9f},"
                   f"setpts=PTS-{target_start:.9f}/TB,fps=30,"
                   f"scale=w=-2:h='trunc(min({args.height},ih)/2)*2':flags=lanczos,setsar=1,format=yuv420p[v]"]
        if "audio" in indices:
            filters.append(f"[{indices['audio']}:a:0]atrim=start={target_start:.9f}:end={target_end:.9f},"
                           f"asetpts=PTS-{target_start:.9f}/TB,aresample=48000[a]")
        else:
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.9f},asetpts=PTS-STARTPTS[a]")
            warnings.append("Source has no audio track; prepared output contains silence")
        staged = temporary / "prepared.mp4"
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-xerror", "-copyts", *inputs,
                   "-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]",
                   "-c:v", "libx264", "-preset", "fast", "-crf", str(args.crf),
                   "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                   "-t", f"{duration:.9f}", "-movflags", "+faststart", str(staged)]
        run(command)
        result = run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,start_time,duration,width,height", "-of", "json", str(staged)])
        measured = json.loads(result.stdout)
        actual_duration = float(measured.get("format", {}).get("duration", "0"))
        if not any(stream.get("codec_type") == "video" for stream in measured.get("streams", [])) or abs(actual_duration - duration) > 0.1:
            raise RecordingError(f"Prepared output failed validation: wanted {duration:.6f}s, measured {actual_duration:.6f}s")
        for source, expected in fingerprints.items():
            assert_unchanged(source, expected)
        appid, kind, source_id, date_label = recording_identity(manifest_path)
        data = {"schema_version": 1, "source_id": source_id, "kind": kind, "appid": appid,
                "manifest": str(manifest_path), "prepared_media": str(output),
                "directory_date_label": date_label,
                "mpd_logical_start_media_pts_seconds": parsed["period_start_seconds"],
                "mpd_logical_duration_seconds": parsed["logical_duration_seconds"],
                "requested_source_start_seconds": args.start,
                "requested_duration_seconds": requested_duration,
                "prepared_start_media_pts_seconds": target_start,
                "prepared_end_media_pts_seconds": target_end,
                "prepared_logical_duration_seconds": duration,
                "measured_output_duration_seconds": actual_duration,
                "prepared_time_zero_maps_to_source_logical_seconds": args.start,
                "timestamp_tolerance_seconds": 0.05,
                "tracks": [{"kind": plan["track"]["kind"], "first_chunk": plan["first_number"],
                            "last_chunk": plan["last_number"], "selected_packet_start_media_pts_seconds": plan["actual_start"],
                            "selected_packet_end_media_pts_seconds": plan["actual_end"]} for plan in plans],
                "warnings": warnings}
        # Publish only after successful duration/stream and stable-source checks.
        check_output(output, source_root)
        check_output(sidecar, source_root)
        staged.rename(output)
        try:
            write_new_json(sidecar, data)
        except Exception:
            # Keep the finished user artifact; report missing provenance explicitly.
            raise RecordingError(f"Prepared video exists at {output}, but sidecar could not be written")
    print(json.dumps({"output": str(output), "sidecar": str(sidecar),
                      "duration_seconds": actual_duration, "warnings": warnings}, indent=2))


def positive_float(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return result


def nonnegative_float(value):
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inv = sub.add_parser("inventory", help="List retained Steam clip/session manifests")
    inv.add_argument("--root", type=Path, default=DEFAULT_ROOT, required=DEFAULT_ROOT is None,
                     help="Steam recording folder; defaults to STEAM_RECORDINGS_ROOT when set")
    inv.add_argument("--output", type=Path)
    inv.add_argument("--appid")
    inv.add_argument("--game", help="Offline installed game-name substring")
    inv.add_argument("--kind", choices=["all", "clip", "session"], default="all")
    inv.add_argument("--date", help="Directory date-label substring, YYYY-MM-DD or YYYYMMDD")
    inv.add_argument("--duration", type=positive_float, help="Approximate source duration in seconds")
    inv.add_argument("--tolerance", type=nonnegative_float, default=60)
    inv.add_argument("--steam-root", type=Path)
    inv.set_defaults(action=inventory)
    prep = sub.add_parser("prepare", help="Reconstruct and normalize a selected source window")
    prep.add_argument("--manifest", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                      help="Recording root for output protection; normally inferred from --manifest")
    prep.add_argument("--start", type=nonnegative_float, default=0, help="Seconds after MPD logical start")
    prep.add_argument("--duration", type=positive_float, help="Requested window length; default remaining verified source")
    prep.add_argument("--height", type=int, default=1080)
    prep.add_argument("--crf", type=int, default=20)
    prep.set_defaults(action=prepare)
    args = parser.parse_args(argv)
    if hasattr(args, "height") and (args.height < 2 or args.height > 4320):
        parser.error("--height must be between 2 and 4320")
    if hasattr(args, "crf") and not 0 <= args.crf <= 51:
        parser.error("--crf must be between 0 and 51")
    try:
        args.action(args)
    except (RecordingError, OSError, ValueError, ET.ParseError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
