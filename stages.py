"""Frame-op stages — the composable 'tricks' of the pipeline.

Each stage is a folder-in -> folder-out pass (the ncnn-vulkan pattern shared by
Real-ESRGAN and RIFE). The runner in upscale_hdr.py chains them per chunk:

    batch_in --Upscale--> s0 --Interpolate--> s1 --> encode

Adding a new AI trick = a new Stage class resolved through tools.py. Stages that
change frame COUNT (interpolate) report their output multiplier so the encoder
can pick the right output frame rate.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import tools

NOWINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class StageError(RuntimeError):
    pass


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=NOWINDOW)
    if r.returncode != 0:
        raise StageError(f"{Path(cmd[0]).name} failed:\n{r.stderr[-800:]}")


class UpscaleStage:
    """Real-ESRGAN AI upscale. Changes resolution; frame count unchanged."""
    label = "upscale"
    frame_multiplier = 1

    def __init__(self, model_name, scale, gpu=0, tile=0):
        self.model_name, self.scale, self.gpu, self.tile = model_name, scale, gpu, tile

    def process(self, in_dir: Path, out_dir: Path) -> None:
        exe, models = tools.realesrgan(), tools.models_dir()
        if not exe or not models:
            raise StageError("realesrgan / models not found — run setup.ps1")
        cmd = [exe, "-i", str(in_dir), "-o", str(out_dir), "-n", self.model_name,
               "-s", str(self.scale), "-m", str(models), "-g", str(self.gpu), "-f", "png"]
        if self.tile > 0:
            cmd += ["-t", str(self.tile)]
        _run(cmd)


class InterpolateStage:
    """RIFE frame interpolation. Multiplies frame count by `factor`."""
    label = "interpolate"

    def __init__(self, factor, gpu=0, model="rife-v4.6"):
        self.factor = int(factor)
        self.frame_multiplier = self.factor
        self.gpu, self.model = gpu, model

    def process(self, in_dir: Path, out_dir: Path) -> None:
        exe, mdir = tools.rife(), tools.rife_model(self.model)
        if not exe or not mdir:
            raise StageError("rife-ncnn-vulkan / models not found — scoop install "
                             "rife-ncnn-vulkan (or see README)")
        n_in = len(list(Path(in_dir).glob("*.png")))
        target = max(2, n_in * self.factor)
        _run([exe, "-i", str(in_dir), "-o", str(out_dir), "-m", str(mdir),
              "-n", str(target), "-g", str(self.gpu), "-f", "%08d.png"])


def build_frame_stages(args) -> list:
    """Ordered folder-op stages from CLI args: upscale, then optional interpolate."""
    from upscale_hdr import MODEL_MAP
    model_name, native = MODEL_MAP[args.model]
    re_scale = 4 if (native == 4 and args.scale != 4) else args.scale
    stages = [UpscaleStage(model_name, re_scale, args.gpu, args.tile)]
    if getattr(args, "interpolate", 0) and args.interpolate > 1:
        stages.append(InterpolateStage(args.interpolate, args.gpu))
    return stages


def total_frame_multiplier(stages) -> int:
    m = 1
    for s in stages:
        m *= getattr(s, "frame_multiplier", 1)
    return m
