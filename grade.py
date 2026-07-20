"""Shared best-practice color-grade filter builder (ffmpeg).

Both the CLI (upscale_hdr.py) and the GUI live preview build their grade from
this one place, so what you tune on a frame is exactly what the render applies.

The chain, in order:
  1. work in float RGB (gbrpf32le) to avoid banding
  2. tonal S-curve (curves)         -> depth / contrast, so it isn't flat
  3. warm-cast neutralize (colorbalance) -> clean color instead of muddy
  4. saturation + gamma (eq)        -> gamma>1 lifts dark midtones
  5. vibrance                       -> boosts muted colors more (protects skin)
  6. sharpen (unsharp)
Numeric knobs map 1:1 to GUI sliders.
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass
class Grade:
    saturation: float = 1.16   # 0.5 .. 2.0   (1.0 = unchanged)
    vibrance: float = 0.28     # 0.0 .. 1.0   selective saturation
    contrast: float = 0.30     # 0.0 .. 1.0   S-curve strength
    gamma: float = 1.05        # 0.8 .. 1.3   midtone lift (>1 brighter)
    warmth: float = -0.55      # -1 cool .. +1 warm (neg neutralizes warm source)
    sharpen: float = 0.50      # 0.0 .. 1.5   unsharp luma amount
    exposure: float = 0.0      # -1 .. +1     overall brightness
    tint: float = 0.0          # -1 green .. +1 magenta


# tuned presets (level 2 "vibrant" is the validated sweet spot)
PRESETS = {
    "none":    Grade(1.00, 0.00, 0.00, 1.00,  0.00, 0.15),
    "subtle":  Grade(1.10, 0.18, 0.18, 1.03, -0.25, 0.35),
    "vibrant": Grade(1.16, 0.28, 0.30, 1.05, -0.55, 0.50),
    "max":     Grade(1.42, 0.60, 0.55, 1.10, -0.65, 0.75),
}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _scurve(strength: float) -> str:
    a = 0.16 * clamp(strength, 0.0, 1.0)
    return f"0/0 0.25/{0.25 - a:.3f} 0.5/0.5 0.75/{0.75 + a:.3f} 1/1"


def build_chain(g: Grade, out_format: str | None = "yuv420p10le",
                working: str | None = "gbrpf32le", lut: str = "") -> str:
    """Return a comma-joined ffmpeg filter chain for the grade.

    out_format=None leaves the pixels in `working` space so a caller (e.g. the
    HDR path) can append its own tail. Set out_format='rgb24' for a preview PNG.
    `lut` is a bare .cube filename applied after the grade — the caller must run
    ffmpeg with cwd set to the file's directory (avoids the Windows drive-colon
    breaking the filtergraph parser).
    """
    parts: list[str] = []
    if working:
        parts.append(f"format={working}")
    if g.contrast > 0.001:
        parts.append(f"curves=master={_scurve(g.contrast)}")
    if abs(g.warmth) > 0.001 or abs(g.tint) > 0.001:
        w = clamp(g.warmth, -1.0, 1.0)
        ti = clamp(g.tint, -1.0, 1.0)
        parts.append(
            f"colorbalance=rm={0.09 * w:.3f}:gm={-0.06 * ti:.3f}:bm={-0.073 * w:.3f}:"
            f"rs={0.055 * w:.3f}:bs={-0.055 * w:.3f}")
    parts.append(f"eq=saturation={clamp(g.saturation, 0.0, 3.0):.3f}:"
                 f"gamma={clamp(g.gamma, 0.1, 3.0):.3f}:"
                 f"brightness={clamp(g.exposure, -1.0, 1.0) * 0.12:.4f}")
    if g.vibrance > 0.001:
        parts.append(f"vibrance=intensity={clamp(g.vibrance, 0.0, 2.0):.3f}")
    if g.sharpen > 0.001:
        parts.append(f"unsharp=5:5:{g.sharpen:.3f}:3:3:{g.sharpen * 0.2:.3f}")
    if lut:
        parts.append(f"lut3d={lut}")               # bare filename; caller sets cwd
    if out_format:
        parts.append(f"format={out_format}")
    return ",".join(parts)


def from_overrides(base: Grade, **kw) -> Grade:
    """Return a copy of `base` with any non-None keyword overrides applied."""
    return replace(base, **{k: v for k, v in kw.items() if v is not None})


if __name__ == "__main__":
    for name, g in PRESETS.items():
        print(f"{name:8s}: {build_chain(g)}")
