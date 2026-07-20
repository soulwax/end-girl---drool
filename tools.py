"""Locate external tools (ffmpeg / ffprobe / realesrgan) and the Real-ESRGAN models.

The canonical source is the system package manager — auvide does not bundle
binaries (they're large, and on Windows a copy inside a OneDrive folder is
re-scanned on every launch, adding ~4s per call):

  Windows : scoop install ffmpeg realesrgan-ncnn-vulkan   (run setup.ps1)
  macOS   : brew install ffmpeg realesrgan-ncnn-vulkan
  Arch    : sudo pacman -S ffmpeg  &&  yay -S realesrgan-ncnn-vulkan   (AUR)
  Ubuntu  : sudo apt install ffmpeg   (realesrgan-ncnn-vulkan from upstream release)
  Fedora  : sudo dnf install ffmpeg   (realesrgan-ncnn-vulkan from upstream release)

Everything is resolved from PATH. The Real-ESRGAN models (data, not an exe) are
provisioned into a local cache by setup and found automatically.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

APP_CACHE = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".cache")) / "auvide"
MODELS_CACHE = APP_CACHE / "models"
RIFE_MODELS_CACHE = APP_CACHE / "rife-models"

INSTALL_HINT = (
    "Install the prerequisites, then retry:\n"
    "  Windows : run setup.ps1  (scoop install ffmpeg realesrgan-ncnn-vulkan + models)\n"
    "  macOS   : brew install ffmpeg realesrgan-ncnn-vulkan\n"
    "  Arch    : sudo pacman -S ffmpeg && yay -S realesrgan-ncnn-vulkan\n"
    "  Ubuntu  : sudo apt install ffmpeg  (+ realesrgan-ncnn-vulkan from upstream)\n"
    "  Fedora  : sudo dnf install ffmpeg  (+ realesrgan-ncnn-vulkan from upstream)\n"
    "  Real-ESRGAN models: run setup.ps1, or drop the .param/.bin files into\n"
    f"    {MODELS_CACHE}\n"
)


def _which(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def ffmpeg():
    return _which("ffmpeg")


def ffprobe():
    return _which("ffprobe")


def realesrgan():
    return _which("realesrgan-ncnn-vulkan", "realesrgan")


def rife():
    return _which("rife-ncnn-vulkan", "rife")


def rife_model(name="rife-v4.6"):
    """Locate a RIFE model folder (or None). Optional — only for interpolation."""
    exe = rife()
    if exe:
        p = Path(exe)
        if "scoop" in p.parts and "shims" in p.parts:   # scoop bundles models in app dir
            i = p.parts.index("scoop")
            cand = Path(*p.parts[:i + 1], "apps", "rife-ncnn-vulkan", "current", name)
            if cand.exists():
                return cand
        near = p.resolve().parent / name                 # some installs: models beside exe
        if near.exists():
            return near
    cache = RIFE_MODELS_CACHE / name
    if cache.exists() and any(cache.glob("*.param")):
        return cache
    return None


def models_dir():
    """Where the Real-ESRGAN .param/.bin models live (or None)."""
    exe = realesrgan()
    if exe:                                   # some installs ship models beside the exe
        near = Path(exe).resolve().parent / "models"
        if near.exists() and any(near.glob("*.param")):
            return near
    if MODELS_CACHE.exists() and any(MODELS_CACHE.glob("*.param")):
        return MODELS_CACHE
    return None


def missing():
    """List of prerequisites that are not resolvable (empty = all good)."""
    out = []
    if not ffmpeg():
        out.append("ffmpeg")
    if not ffprobe():
        out.append("ffprobe")
    if not realesrgan():
        out.append("realesrgan-ncnn-vulkan")
    if not models_dir():
        out.append("Real-ESRGAN models")
    return out
