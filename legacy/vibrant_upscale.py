#!/usr/bin/env python3
"""
Upscale a video with Real-ESRGAN, then apply a configurable vibrant HDR-style
grade with ffmpeg.

Default output goes to ./out. Temporary frame folders are cleaned after a
successful run unless --keep-work is used.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

# Each level layers best-practice grading, not just a saturation multiply:
#   - a tonal S-curve (curves) for depth/contrast so the image isn't flat;
#   - a light warm-cast neutralization (colorbalance) because this footage is
#     heavily warm-graded, which reads as muddy/"dull";
#   - gamma > 1 to LIFT the (dark) midtones instead of the old gamma<1 that
#     darkened them;
#   - moderate saturation + the `vibrance` filter, which boosts muted colors
#     more than already-saturated ones (protects skin) — far more "pop" per
#     unit than a flat eq=saturation multiply, without clipping;
#   - a gentle sharpen.
# Level 2 ("vibrant") is the tuned sweet spot; 3-4 push progressively harder.
VIBE_PRESETS = {
    0: {
        "label": "neutral",
        "curve": "",
        "cb": "",
        "saturation": 1.00, "contrast": 1.01, "gamma": 1.00, "vibrance": 0.00,
        "unsharp_luma": 0.15, "unsharp_chroma": 0.04,
    },
    1: {
        "label": "clean-pop",
        "curve": "0/0 0.25/0.21 0.5/0.51 0.80/0.85 1/1",
        "cb": "rm=-0.02:bm=0.02",
        "saturation": 1.10, "contrast": 1.02, "gamma": 1.03, "vibrance": 0.18,
        "unsharp_luma": 0.35, "unsharp_chroma": 0.08,
    },
    2: {
        "label": "vibrant",
        "curve": "0/0 0.24/0.19 0.5/0.52 0.80/0.87 1/1",
        "cb": "rm=-0.05:bm=0.04:rs=-0.03:bs=0.03",
        "saturation": 1.16, "contrast": 1.03, "gamma": 1.05, "vibrance": 0.28,
        "unsharp_luma": 0.50, "unsharp_chroma": 0.10,
    },
    3: {
        "label": "hypercolor",
        "curve": "0/0 0.22/0.16 0.5/0.53 0.80/0.88 1/1",
        "cb": "rm=-0.05:bm=0.04",
        "saturation": 1.28, "contrast": 1.05, "gamma": 1.08, "vibrance": 0.45,
        "unsharp_luma": 0.62, "unsharp_chroma": 0.12,
    },
    4: {
        "label": "radioactive",
        "curve": "0/0 0.20/0.14 0.5/0.54 0.80/0.89 1/1",
        "cb": "rm=-0.06:bm=0.05",
        "saturation": 1.42, "contrast": 1.07, "gamma": 1.10, "vibrance": 0.60,
        "unsharp_luma": 0.75, "unsharp_chroma": 0.14,
    },
}


def eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def resolve_tool(name: str, override: str | None = None) -> str:
    candidate = override or name
    resolved = shutil.which(candidate)
    if resolved:
        return resolved
    if Path(candidate).exists():
        return str(Path(candidate))
    raise SystemExit(f"Required tool not found on PATH: {candidate}")


def local_realesrgan_candidate() -> str | None:
    candidate = Path.cwd() / "tools" / "realesrgan-ncnn-vulkan.exe"
    models_dir = candidate.parent / "models"
    if candidate.exists() and models_dir.exists():
        return str(candidate)
    return None


def command_for_log(cmd: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in cmd)


def run_logged(cmd: list[str], log_path: Path, env: dict[str, str] | None = None) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", errors="replace") as log:
        log.write("\n" + "=" * 80 + "\n")
        log.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
        log.write(command_for_log(cmd) + "\n\n")
        log.flush()
        eprint(f"running: {Path(cmd[0]).name} (log: {log_path})")
        process = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"Command failed with exit code {return_code}. See log: {log_path}")


def probe_video(ffprobe: str, input_path: Path) -> dict:
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-print_format",
        "json",
        str(input_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def first_video_stream(metadata: dict) -> dict:
    for stream in metadata.get("streams", []):
        if stream.get("codec_type") == "video":
            return stream
    raise SystemExit("No video stream found in input.")


def has_audio_stream(metadata: dict) -> bool:
    return any(stream.get("codec_type") == "audio" for stream in metadata.get("streams", []))


def frame_rate(stream: dict) -> str:
    for key in ("avg_frame_rate", "r_frame_rate"):
        rate = stream.get(key)
        if rate and rate != "0/0":
            return rate
    return "24000/1001"


def expected_frame_count(stream: dict) -> int | None:
    nb_frames = stream.get("nb_frames")
    if nb_frames and str(nb_frames).isdigit():
        return int(nb_frames)
    return None


def pick_input(input_arg: str | None) -> Path:
    if input_arg:
        path = Path(input_arg)
        if not path.exists():
            raise SystemExit(f"Input not found: {path}")
        return path

    videos = [
        path
        for path in Path.cwd().iterdir()
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    ]
    if len(videos) == 1:
        return videos[0]
    if not videos:
        raise SystemExit("No input was supplied and no video file was found in this folder.")
    names = ", ".join(path.name for path in videos)
    raise SystemExit(f"Multiple videos found. Pass one explicitly: {names}")


def count_frames(folder: Path, frame_format: str) -> int:
    if not folder.exists():
        return 0
    return sum(1 for _ in folder.glob(f"*.{frame_format}"))


def clear_folder(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for item in folder.iterdir():
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()


def safe_clean_workdir(work_dir: Path) -> None:
    marker = work_dir / ".vibrant_upscale_workdir"
    if not marker.exists():
        raise RuntimeError(f"Refusing to delete unmarked work directory: {work_dir}")

    def make_writable_and_retry(function, path, _exc_info):
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        function(path)

    last_error: Exception | None = None
    for _attempt in range(5):
        try:
            shutil.rmtree(work_dir, onerror=make_writable_and_retry)
            return
        except (PermissionError, OSError) as exc:
            last_error = exc
            time.sleep(0.5)
    if last_error:
        raise last_error


def build_grade_filter(vibe: int, output_format: str = "yuv420p10le") -> str:
    p = VIBE_PRESETS[vibe]
    # Grade in a high-precision float RGB space to avoid banding, then convert
    # back. Order: tone curve -> warm-cast neutralize -> saturation/gamma ->
    # vibrance -> sharpen. See VIBE_PRESETS for the rationale of each stage.
    parts = ["format=gbrpf32le"]
    if p["curve"]:
        parts.append(f"curves=master={p['curve']}")
    if p["cb"]:
        parts.append(f"colorbalance={p['cb']}")
    parts.append(
        f"eq=saturation={p['saturation']}:contrast={p['contrast']}:gamma={p['gamma']}"
    )
    if p["vibrance"] > 0:
        parts.append(f"vibrance=intensity={p['vibrance']}")
    parts.append(f"unsharp=5:5:{p['unsharp_luma']}:3:3:{p['unsharp_chroma']}")
    parts.append(f"format={output_format}")
    return ",".join(parts)


def sample_frames(folder: Path, frame_format: str, limit: int) -> list[Path]:
    return sorted(folder.glob(f"*.{frame_format}"))[:limit]


def make_preview_comparisons(
    ffmpeg: str,
    frames_dir: Path,
    upscaled_dir: Path,
    preview_dir: Path,
    vibe: int,
    scale: int,
    frame_format: str,
    count: int,
    log_path: Path,
) -> None:
    preview_dir.mkdir(parents=True, exist_ok=True)
    originals = sample_frames(frames_dir, frame_format, count)
    upscaled = sample_frames(upscaled_dir, frame_format, count)
    if not originals or not upscaled:
        raise RuntimeError("Preview comparison requires both original and upscaled frames.")

    grade_filter = build_grade_filter(vibe, "rgb24")
    for index, (orig, up) in enumerate(zip(originals, upscaled), start=1):
        preview_path = preview_dir / f"compare_vibe{vibe}_{index:03d}.png"
        scale_filter = f"[0:v]scale=iw*{scale}:ih*{scale}:flags=lanczos[orig]"
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(orig),
            "-i",
            str(up),
            "-filter_complex",
            f"{scale_filter};[1:v]{grade_filter}[graded];[orig][graded]hstack=inputs=2",
            "-frames:v",
            "1",
            str(preview_path),
        ]
        run_logged(cmd, log_path)
    eprint(f"preview: wrote {len(originals)} comparison images to {preview_dir}")


def output_name(input_path: Path, scale: int, vibe: int) -> str:
    label = VIBE_PRESETS[vibe]["label"]
    return f"{input_path.stem}_up{scale}x_vibe{vibe}_{label}.mp4"


def selected_model(model_arg: str, scale: int) -> str:
    if model_arg == "auto":
        return f"realesr-animevideov3-x{scale}"
    return model_arg


def slug(value: str) -> str:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    return "".join(char if char in allowed else "_" for char in value)


def extract_frames(
    ffmpeg: str,
    input_path: Path,
    frames_dir: Path,
    frame_format: str,
    log_path: Path,
    limit_frames: int | None,
) -> None:
    clear_folder(frames_dir)
    pattern = str(frames_dir / f"frame_%08d.{frame_format}")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-vsync",
        "0",
    ]
    if limit_frames:
        cmd.extend(["-frames:v", str(limit_frames)])
    if frame_format == "jpg":
        cmd.extend(["-q:v", "2"])
    cmd.append(pattern)
    run_logged(cmd, log_path)


def upscale_frames(
    realesrgan: str,
    frames_dir: Path,
    upscaled_dir: Path,
    model: str,
    scale: int,
    frame_format: str,
    tile: int,
    tta: bool,
    log_path: Path,
) -> None:
    clear_folder(upscaled_dir)
    cmd = [
        realesrgan,
        "-i",
        str(frames_dir),
        "-o",
        str(upscaled_dir),
        "-n",
        model,
        "-s",
        str(scale),
        "-f",
        frame_format,
    ]
    if tile > 0:
        cmd.extend(["-t", str(tile)])
    if tta:
        cmd.append("-x")
    run_logged(cmd, log_path)


def encode_video(
    ffmpeg: str,
    input_path: Path,
    upscaled_dir: Path,
    output_path: Path,
    frame_format: str,
    fps: str,
    vibe: int,
    codec: str,
    preset: str,
    crf: int,
    audio: bool,
    log_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pattern = str(upscaled_dir / f"frame_%08d.{frame_format}")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-framerate",
        fps,
        "-start_number",
        "1",
        "-i",
        pattern,
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
    ]
    if audio:
        cmd.extend(["-map", "1:a?"])
    cmd.extend(
        [
            "-map_metadata",
            "1",
            "-vf",
            build_grade_filter(vibe),
            "-c:v",
            codec,
            "-preset",
            preset,
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p10le",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
            "-tag:v",
            "hvc1" if codec == "libx265" else "avc1",
        ]
    )
    if audio:
        cmd.extend(["-c:a", "copy", "-shortest"])
    else:
        cmd.append("-an")
    cmd.extend(["-movflags", "+faststart", str(output_path)])
    run_logged(cmd, log_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upscale video frames with Real-ESRGAN and add a 0-4 vibrant HDR-style grade."
    )
    parser.add_argument("input", nargs="?", help="Video to process. If omitted, the single video in cwd is used.")
    parser.add_argument("--out", default="out", help="Output folder. Default: out")
    parser.add_argument("--work-dir", help="Temporary frame work folder. Default: out/_work/<input>_s<scale>")
    parser.add_argument("--vibe", type=int, choices=range(5), default=2, help="Vibrancy level 0-4. Default: 2")
    parser.add_argument("--all-vibes", action="store_true", help="Encode all vibrancy levels 0 through 4 after upscaling.")
    parser.add_argument("--scale", type=int, choices=(2, 3, 4), default=2, help="Real-ESRGAN upscale factor. Default: 2")
    parser.add_argument(
        "--model",
        default="auto",
        help="Real-ESRGAN model. Default: auto, which picks realesr-animevideov3-x<scale> for video speed.",
    )
    parser.add_argument("--frame-format", choices=("jpg", "png"), default="png", help="Intermediate frame format. Default: png for lossless preview and grading quality.")
    parser.add_argument("--tile", type=int, default=0, help="Real-ESRGAN tile size. 0 lets the tool decide. Default: 0")
    parser.add_argument("--tta", action="store_true", help="Enable Real-ESRGAN TTA mode. Slower, sometimes cleaner.")
    parser.add_argument("--codec", default="libx265", help="Video encoder. Default: libx265")
    parser.add_argument("--preset", default="medium", help="Encoder preset. Default: medium")
    parser.add_argument("--crf", type=int, default=17, help="Encoder CRF. Lower is larger/better. Default: 17")
    parser.add_argument("--fresh", action="store_true", help="Delete this script's existing work folder before starting.")
    parser.add_argument("--keep-work", action="store_true", help="Keep extracted/upscaled frames after success.")
    parser.add_argument("--limit-frames", type=int, help="Process only the first N frames for testing.")
    parser.add_argument("--preview", action="store_true", help="Generate sample comparison PNGs and skip full encode.")
    parser.add_argument("--preview-count", type=int, default=4, help="How many sample frames to render for preview. Default: 4")
    parser.add_argument("--ffmpeg", help="Path/name for ffmpeg.")
    parser.add_argument("--ffprobe", help="Path/name for ffprobe.")
    parser.add_argument("--realesrgan", help="Path/name for Real-ESRGAN. Defaults to ./tools copy when present, then PATH.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ffmpeg = resolve_tool("ffmpeg", args.ffmpeg)
    ffprobe = resolve_tool("ffprobe", args.ffprobe)
    realesrgan = resolve_tool("realesrgan-ncnn-vulkan.exe", args.realesrgan or local_realesrgan_candidate())

    if args.preview and args.limit_frames is None:
        args.limit_frames = args.preview_count
        eprint(f"preview: limiting work to first {args.limit_frames} frames")

    input_path = pick_input(args.input).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = out_dir / "logs"
    model = selected_model(args.model, args.scale)

    work_dir = (
        Path(args.work_dir).resolve()
        if args.work_dir
        else out_dir / "_work" / f"{input_path.stem}_s{args.scale}_{slug(model)}_{args.frame_format}"
    )
    if args.fresh and work_dir.exists():
        safe_clean_workdir(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / ".vibrant_upscale_workdir").write_text("owned by vibrant_upscale.py\n", encoding="utf-8")

    if args.preview and args.frame_format == "jpg":
        eprint("preview: switching intermediate frame format to png for better comparison quality")
        args.frame_format = "png"

    metadata = probe_video(ffprobe, input_path)
    video_stream = first_video_stream(metadata)
    fps = frame_rate(video_stream)
    expected = args.limit_frames or expected_frame_count(video_stream)
    audio = has_audio_stream(metadata)

    frames_dir = work_dir / "frames"
    upscaled_dir = work_dir / "upscaled"
    frame_count = count_frames(frames_dir, args.frame_format)
    upscaled_count = count_frames(upscaled_dir, args.frame_format)

    eprint(f"input: {input_path.name}")
    eprint(f"source: {video_stream.get('width')}x{video_stream.get('height')} at {fps}, audio={audio}")
    eprint(f"model: {model}")
    eprint(f"work: {work_dir}")
    eprint(f"output: {out_dir}")

    if expected and frame_count >= expected:
        eprint(f"extract: reusing {frame_count} existing frames")
    else:
        extract_frames(
            ffmpeg,
            input_path,
            frames_dir,
            args.frame_format,
            logs_dir / "01_extract_frames.log",
            args.limit_frames,
        )
        frame_count = count_frames(frames_dir, args.frame_format)
        eprint(f"extract: wrote {frame_count} frames")

    if frame_count == 0:
        raise SystemExit("No frames were extracted.")

    if upscaled_count >= frame_count:
        eprint(f"upscale: reusing {upscaled_count} existing frames")
    else:
        upscale_frames(
            realesrgan,
            frames_dir,
            upscaled_dir,
            model,
            args.scale,
            args.frame_format,
            args.tile,
            args.tta,
            logs_dir / "02_realesrgan_upscale.log",
        )
        upscaled_count = count_frames(upscaled_dir, args.frame_format)
        eprint(f"upscale: wrote {upscaled_count} frames")

    if upscaled_count < frame_count:
        raise SystemExit(f"Upscaled frame count is short: {upscaled_count} of {frame_count}")

    if args.preview:
        preview_dir = out_dir / "preview"
        vibes = list(range(5)) if args.all_vibes else [args.vibe]
        for vibe in vibes:
            make_preview_comparisons(
                ffmpeg,
                frames_dir,
                upscaled_dir,
                preview_dir,
                vibe,
                args.scale,
                args.frame_format,
                min(args.preview_count, frame_count),
                logs_dir / f"03_preview_vibe{vibe}.log",
            )
        eprint("preview: skipping full encode")
    else:
        vibes = list(range(5)) if args.all_vibes else [args.vibe]
        for vibe in vibes:
            output_path = out_dir / output_name(input_path, args.scale, vibe)
            eprint(f"encode: vibe {vibe} ({VIBE_PRESETS[vibe]['label']}) -> {output_path.name}")
            encode_video(
                ffmpeg,
                input_path,
                upscaled_dir,
                output_path,
                args.frame_format,
                fps,
                vibe,
                args.codec,
                args.preset,
                args.crf,
                audio,
                logs_dir / f"03_encode_vibe{vibe}.log",
            )

    if not args.keep_work:
        eprint("cleanup: removing intermediate frames")
        try:
            safe_clean_workdir(work_dir)
        except Exception as exc:
            eprint(f"cleanup warning: could not remove {work_dir}: {exc}")
    else:
        eprint(f"keep-work: retained {work_dir}")

    eprint("done")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        eprint("interrupted")
        raise SystemExit(130)
