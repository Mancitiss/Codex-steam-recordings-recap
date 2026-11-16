#!/usr/bin/env python3
"""Inspect and assemble local recap media using FFmpeg; Python standard library only."""

import argparse
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


class MediaError(Exception):
    pass


def run(args):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode:
        raise MediaError(f"{args[0]} failed ({result.returncode}):\n{result.stderr[-12000:]}")
    return result


def probe(path):
    data = json.loads(run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                           "-of", "json", path]).stdout)
    videos = [s for s in data.get("streams", []) if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")]
    if not videos:
        raise MediaError(f"No video stream: {path}")
    video = videos[0]
    raw_duration = video.get("duration") or data.get("format", {}).get("duration")
    try:
        duration = float(raw_duration)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError()
    except (TypeError, ValueError):
        raise MediaError(f"Cannot determine a finite positive video duration: {path}")
    return {"duration": duration, "video": video,
            "audio": [s for s in data.get("streams", []) if s.get("codec_type") == "audio"],
            "format": data.get("format", {})}


def seconds(value):
    if isinstance(value, bool):
        raise argparse.ArgumentTypeError("A timestamp must be a number or HH:MM:SS[.sss]")
    try:
        if isinstance(value, str) and ":" in value:
            parts = value.split(":")
            if len(parts) not in (2, 3):
                raise ValueError()
            numbers = [float(x) for x in parts]
            if any(x < 0 for x in numbers) or any(x >= 60 for x in numbers[1:]):
                raise ValueError()
            if any(x != int(x) for x in numbers[:-1]):
                raise ValueError()
            result = 0.0
            for number in numbers:
                result = result * 60 + number
        else:
            result = float(value)
        if not math.isfinite(result) or result < 0:
            raise ValueError()
        return result
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"Invalid nonnegative timestamp: {value!r}")


def existing_video(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise MediaError(f"Video does not exist: {path}")
    return path


def number(value):
    return f"{value:.9f}"


def write_json(path, data):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def checked_range(start, duration, info):
    start = seconds(start)
    duration = seconds(duration)
    if duration <= 0 or start >= info["duration"] or start + duration > info["duration"] + 0.001:
        raise MediaError(f"Range {start:.3f}–{start + duration:.3f}s exceeds video duration "
                         f"{info['duration']:.3f}s or is empty")
    return start, duration


def storyboard(args):
    source = existing_video(args.input)
    info = probe(source)
    duration = args.duration if args.duration is not None else info["duration"] - args.start
    start, duration = checked_range(args.start, duration, info)
    if args.interval <= 0 or args.columns < 1 or args.rows < 1 or args.width < 64:
        raise MediaError("Interval must be positive; columns/rows >= 1; width >= 64")
    out = Path(args.output_dir).expanduser().resolve()
    if out.exists() and any(out.iterdir()):
        raise MediaError(f"Storyboard output directory must be empty: {out}")
    frames = out / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    sample_count = max(1, math.ceil(duration / args.interval - 1e-10))
    index = {"source": str(source), "source_duration": info["duration"],
             "range_start": start, "range_end": start + duration,
             "interval": args.interval,
             "timestamp_note": "Labels show decoded source PTS. Requested seeks and actual PTS are recorded per frame.",
             "frames": [], "sheets": []}
    for i in range(sample_count):
        timestamp = start + i * args.interval
        destination = frames / f"frame-{i:06d}.jpg"
        # copyts keeps source PTS for both labels and showinfo. Accurate seeking
        # yields the first available decoded frame at/after the requested seek.
        vf = (f"scale={args.width}:-2,showinfo,"
              "drawtext=text='%{pts\\:hms}':fontcolor=white:fontsize=18:"
              "box=1:boxcolor=black@0.7:boxborderw=4:x=8:y=h-th-8")
        result = run(["ffmpeg", "-hide_banner", "-loglevel", "info", "-nostdin", "-n",
                      "-copyts", "-ss", number(timestamp), "-i", source,
                      "-map", "0:v:0", "-an", "-vf", vf, "-frames:v", "1",
                      "-fps_mode", "passthrough", "-q:v", "3", "-update", "1", destination])
        if not destination.is_file():
            raise MediaError(f"No decoded frame at {timestamp:.3f}s; use an earlier range end")
        match = re.search(r"\bn:\s*0\s+.*?\bpts_time:([-+\d.eE]+)", result.stderr)
        index["frames"].append({"frame": i, "requested_seconds": timestamp,
                                 "source_pts_seconds": float(match.group(1)) if match else None,
                                 "path": str(destination)})
        if (i + 1) % 16 == 0:
            print(f"Extracted {i + 1}/{sample_count} frames", file=sys.stderr)
    per_page = args.columns * args.rows
    for first in range(0, sample_count, per_page):
        count = min(per_page, sample_count - first)
        destination = out / f"sheet-{first // per_page + 1:03d}.jpg"
        vf = (f"tile={args.columns}x{args.rows}:nb_frames={count}:"
              "padding=8:margin=8:color=black")
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
             "-framerate", "1", "-start_number", str(first), "-i", frames / "frame-%06d.jpg",
             "-vf", vf, "-frames:v", "1", "-q:v", "3", "-update", "1", destination])
        index["sheets"].append({"path": str(destination), "first_frame": first,
                                 "last_frame": first + count - 1,
                                 "order": "left to right, top to bottom"})
    write_json(out / "index.json", index)
    print(json.dumps({"index": str(out / "index.json"), "sheets": index["sheets"]}, indent=2))


def validate_plan(plan, base, fps):
    if not isinstance(plan, dict) or not isinstance(plan.get("clips"), list) or not plan["clips"]:
        raise MediaError("Plan must be an object with a nonempty clips array")
    target = seconds(plan.get("target_duration"))
    if target <= 0:
        raise MediaError("target_duration must be positive")
    if "chronological" in plan and not isinstance(plan["chronological"], bool):
        raise MediaError("chronological must be true or false")
    chronological = plan.get("chronological", True)
    # Round down so an exact requested range can always supply its own preview,
    # even when the requested duration ends between output frames.
    target_frames = math.floor(target * fps + 1e-6)
    if target_frames < 1:
        raise MediaError("Requested duration is shorter than one output frame")
    infos, intervals, parsed = {}, {}, []
    for i, clip in enumerate(plan["clips"]):
        if not isinstance(clip, dict) or not isinstance(clip.get("source"), str):
            raise MediaError(f"Clip {i + 1} needs a source path")
        source = Path(clip["source"]).expanduser()
        source = existing_video(source if source.is_absolute() else base / source)
        key = str(source)
        if key not in infos:
            infos[key] = probe(source)
        info = infos[key]
        start, end = seconds(clip.get("start")), seconds(clip.get("end"))
        checked_range(start, end - start if end >= start else 0, info)
        old = intervals.setdefault(key, [])
        if any(start < b - 1e-6 and end > a + 1e-6 for a, b in old):
            raise MediaError(f"Clip {i + 1} overlaps another selected interval in {source.name}")
        if chronological and old and start < old[-1][0] - 1e-6:
            raise MediaError(f"Clip {i + 1} is out of source order; set chronological:false intentionally")
        old.append((start, end))
        audio = clip.get("audio_stream", 0)
        if isinstance(audio, bool) or not isinstance(audio, int) or audio < 0:
            raise MediaError(f"Clip {i + 1}: audio_stream must be a nonnegative audio-stream ordinal")
        if info["audio"] and audio >= len(info["audio"]):
            raise MediaError(f"Clip {i + 1}: audio stream {audio} does not exist")
        parsed.append({"source": source, "start": start, "end": end, "info": info,
                       "audio_stream": audio, "reason": clip.get("reason", ""),
                       "available_frames": math.floor((end - start) * fps + 1e-6)})
    if sum(c["available_frames"] for c in parsed) < target_frames:
        raise MediaError("Selected intervals are too short after rounding each clip down to whole frames. "
                         "Select more real footage or request a shorter recap; no video padding is added.")
    remaining = target_frames
    selected = []
    for clip in parsed:
        count = min(remaining, clip["available_frames"])
        if count:
            clip["frames"] = count
            clip["duration"] = count / fps
            selected.append(clip)
            remaining -= count
        if remaining == 0:
            break
    return target, target_frames, selected


def concat_quote(path):
    return "'" + str(path).replace("\\", "/").replace("'", "'\\''") + "'"


def publish(source, destination):
    created = False
    try:
        with destination.open("xb") as out, source.open("rb") as inp:
            created = True
            shutil.copyfileobj(inp, out)
    except Exception:
        if created:
            destination.unlink(missing_ok=True)
        raise


def render_plan(plan, base, output, width, height, fps):
    if width < 64 or height < 64 or width % 2 or height % 2 or not 1 <= fps <= 120:
        raise MediaError("Use even dimensions >= 64 and integer fps between 1 and 120")
    requested, total_frames, clips = validate_plan(plan, base, fps)
    exact_duration = total_frames / fps
    output = Path(output).expanduser().resolve()
    if output.suffix.lower() != ".mp4":
        raise MediaError("Output must have an .mp4 extension")
    sidecar = output.with_suffix(output.suffix + ".edl.json")
    if output.exists() or sidecar.exists():
        raise MediaError(f"Refusing to overwrite output or sidecar: {output}")
    if any(output == clip["source"] for clip in clips):
        raise MediaError("Output must differ from every source")
    output.parent.mkdir(parents=True, exist_ok=True)
    edl = {"requested_duration": requested, "rendered_duration": exact_duration,
           "fps": fps, "width": width, "height": height,
           "transition": "hard cuts", "audio": "Selected source track; silence where source has no audio",
           "clips": []}
    cursor = 0.0
    with tempfile.TemporaryDirectory(prefix="steam-recap-", dir=output.parent) as temp:
        temp = Path(temp)
        parts = []
        for i, clip in enumerate(clips):
            print(f"Rendering clip {i + 1}/{len(clips)} ({clip['duration']:.3f}s)", file=sys.stderr)
            part = temp / f"part-{i:04d}.mkv"
            duration = number(clip["duration"])
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                       "-ss", number(clip["start"]), "-i", clip["source"]]
            has_audio = bool(clip["info"]["audio"])
            if not has_audio:
                command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            command += ["-map", "0:v:0", "-map",
                        f"0:a:{clip['audio_stream']}" if has_audio else "1:a:0"]
            vf = (f"trim=duration={duration},setpts=PTS-STARTPTS,"
                  f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                  f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                  f"setsar=1,fps={fps},format=yuv420p")
            af = (f"aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo,"
                  f"apad,atrim=duration={duration},asetpts=PTS-STARTPTS")
            command += ["-vf", vf, "-af", af, "-t", duration,
                        "-frames:v", str(clip["frames"]), "-c:v", "libx264", "-crf", "20",
                        "-preset", "fast", "-c:a", "pcm_s16le", "-map_metadata", "-1", part]
            run(command)
            parts.append((part, clip["duration"]))
            edl["clips"].append({"source": str(clip["source"]), "source_start": clip["start"],
                                  "source_end": clip["start"] + clip["duration"],
                                  "output_start": cursor, "output_end": cursor + clip["duration"],
                                  "frames": clip["frames"], "reason": clip["reason"],
                                  "audio_stream": clip["audio_stream"] if has_audio else None})
            cursor += clip["duration"]
        concat = temp / "parts.ffconcat"
        concat.write_text("ffconcat version 1.0\n" + "".join(
            f"file {concat_quote(path)}\nduration {number(duration)}\n" for path, duration in parts),
            encoding="utf-8")
        finished = temp / "recap.mp4"
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
             "-f", "concat", "-safe", "0", "-i", concat,
             "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-af", f"atrim=duration={number(exact_duration)},asetpts=PTS-STARTPTS",
             "-t", number(exact_duration), "-movflags", "+faststart", "-map_metadata", "-1", finished])
        final = probe(finished)
        actual = float(final["format"].get("duration", final["duration"]))
        if abs(actual - requested) > 1 / fps + 0.025:
            raise MediaError(f"Duration validation failed: requested {requested:.4f}s, got {actual:.4f}s")
        count = final["video"].get("nb_frames")
        if count is not None and int(count) != total_frames:
            raise MediaError(f"Frame count validation failed: wanted {total_frames}, got {count}")
        if not final["audio"]:
            raise MediaError("Final recap unexpectedly has no audio stream")
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-nostdin",
             "-i", finished, "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"])
        edl["verified_container_duration"] = actual
        edl["verified_video_duration"] = final["duration"]
        publish(finished, output)
        try:
            write_json(sidecar, edl)
        except Exception:
            output.unlink(missing_ok=True)
            raise
    print(json.dumps({"output": str(output), "edit_list": str(sidecar),
                      "duration": actual, "frames": total_frames}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    board = commands.add_parser("storyboard", help="Create timestamped contact sheets plus a frame index")
    board.add_argument("--input", required=True)
    board.add_argument("--output-dir", required=True)
    board.add_argument("--start", type=seconds, default=0.0)
    board.add_argument("--duration", type=seconds)
    board.add_argument("--interval", type=float, default=15.0)
    board.add_argument("--columns", type=int, default=4)
    board.add_argument("--rows", type=int, default=4)
    board.add_argument("--width", type=int, default=320)
    preview = commands.add_parser("preview", help="Encode one range for closer inspection")
    preview.add_argument("--input", required=True)
    preview.add_argument("--start", type=seconds, required=True)
    preview.add_argument("--duration", type=seconds, required=True)
    preview.add_argument("--output", required=True)
    rendering = commands.add_parser("render", help="Render a frame-accurate recap from a JSON edit plan")
    rendering.add_argument("--plan", required=True)
    rendering.add_argument("--output", required=True)
    for command in (preview, rendering):
        command.add_argument("--width", type=int, default=1920)
        command.add_argument("--height", type=int, default=1080)
        command.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()
    try:
        for executable in ("ffmpeg", "ffprobe"):
            if not shutil.which(executable):
                raise MediaError(f"{executable} is not on PATH")
        if args.command == "storyboard":
            storyboard(args)
        elif args.command == "preview":
            source = existing_video(args.input)
            checked_range(args.start, args.duration, probe(source))
            plan = {"target_duration": args.duration,
                    "clips": [{"source": str(source), "start": args.start,
                               "end": args.start + args.duration, "reason": "Inspection preview"}]}
            render_plan(plan, Path.cwd(), args.output, args.width, args.height, args.fps)
        else:
            plan_path = Path(args.plan).expanduser().resolve()
            with plan_path.open(encoding="utf-8-sig") as handle:
                plan = json.load(handle)
            render_plan(plan, plan_path.parent, args.output, args.width, args.height, args.fps)
    except (MediaError, argparse.ArgumentTypeError, OSError, ValueError, KeyError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
