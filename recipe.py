"""Recipe = the single source of truth for one auvide job.

Both the GUI and the CLI serialize to/from a Recipe, and Styles are named
one-tap Recipes (à la iPhone Photographic Styles). This is the spine of the
pipeline: today it captures upscale + grade + HDR + encode + trim + audio;
the `frame_ops`, `lut`, and `target` fields are reserved for the stage engine
(RIFE interpolation, LUTs, delivery targets) landing next.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace

import grade

GRADE_KNOBS = ("saturation", "vibrance", "contrast", "gamma", "warmth",
               "sharpen", "exposure", "tint")


def grade_dict(name: str, **over) -> dict:
    """The 8 grade knobs of a built-in grade preset, with optional overrides."""
    return asdict(replace(grade.PRESETS[name], **over))


@dataclass
class Recipe:
    scale: int = 2
    model: str = "animevideo"           # animevideo | x4plus | x4plus-anime
    hdr: str = "on"                     # on | off
    encoder: str = "x265"              # x265 | qsv
    crf: int = 19
    preset: str = "medium"             # encoder speed
    hdr_gain: float = 1.5
    grade: dict = field(default_factory=lambda: grade_dict("vibrant"))
    trim_start: float = 0.0
    trim_dur: float = 0.0              # 0 = to end
    audio: bool = True
    interpolate: int = 0              # RIFE factor (0=off, 2/3/4)
    slowmo: bool = False              # keep fps (slow-motion) vs smoother
    deinterlace: bool = False         # restore: bwdif
    denoise: str = "off"              # restore: off/light/medium/strong
    stabilize: bool = False           # restore: vidstab
    lut: str = ""                                    # .cube LUT path
    target: str = ""                                 # delivery target preset id

    def to_grade(self) -> grade.Grade:
        return grade.Grade(**{k: self.grade.get(k, getattr(grade.Grade(), k))
                              for k in GRADE_KNOBS})


# --- Styles: one-tap named looks (the iPhone-style front door) ---------------
STYLES: dict[str, Recipe] = {
    "Vibrant HDR": Recipe(hdr="on", grade=grade_dict("vibrant")),
    "Cinematic":   Recipe(hdr="on", grade=grade_dict("subtle", warmth=-0.70, contrast=0.42,
                                                     gamma=1.04)),
    "Natural":     Recipe(hdr="off", grade=grade_dict("subtle")),
    "Punchy SDR":  Recipe(hdr="off", grade=grade_dict("max")),
    "Sharp Photo": Recipe(model="x4plus", hdr="on", grade=grade_dict("vibrant")),
    "Clean":       Recipe(hdr="off", grade=grade_dict("none")),
    "Smooth 60":   Recipe(hdr="on", grade=grade_dict("vibrant"), interpolate=2),
    "Restore":     Recipe(hdr="on", grade=grade_dict("vibrant", sharpen=0.70), denoise="medium"),
}


# --- Delivery targets: one-tap "export for platform" -------------------------
# Each may force SDR and a fixed output size (crop or pad). {} = keep source.
TARGETS: dict[str, dict] = {
    "source":  {},
    "youtube": {},                                                    # keep as-is (HDR ok)
    "web":     {"hdr": "off", "max_h": 1080},                         # cap 1080p, keep aspect
    "reel":    {"hdr": "off", "w": 1080, "h": 1920, "fit": "crop"},   # 9:16 IG/TikTok
    "tiktok":  {"hdr": "off", "w": 1080, "h": 1920, "fit": "crop"},
    "post":    {"hdr": "off", "w": 1080, "h": 1080, "fit": "crop"},   # 1:1 IG post
    "story":   {"hdr": "off", "w": 1080, "h": 1920, "fit": "pad"},    # 9:16 padded
    "x":       {"hdr": "off", "w": 1920, "h": 1080, "fit": "pad"},    # 16:9
}


def target_hdr(name):
    """HDR override a target forces, or None."""
    return (TARGETS.get(name) or {}).get("hdr")


def target_transform(name, src_w, src_h):
    """Return (ffmpeg scale/crop/pad filter or "", (out_w, out_h)) for a target."""
    t = TARGETS.get(name) or {}
    if "w" in t and "h" in t:
        w, h, fit = t["w"], t["h"], t.get("fit", "crop")
        if fit == "pad":
            f = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                 f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")
        else:
            f = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
        return f, (w, h)
    if "max_h" in t and src_h > t["max_h"]:
        mh = t["max_h"]
        w = int(round(src_w * mh / src_h / 2)) * 2       # keep width even
        return f"scale={w}:{mh}:flags=lanczos", (w, mh)
    return "", (src_w, src_h)


def save(recipe: Recipe, path) -> None:
    from pathlib import Path
    Path(path).write_text(json.dumps(asdict(recipe), indent=2))


def load(path) -> Recipe:
    from pathlib import Path
    return Recipe(**json.loads(Path(path).read_text()))


def apply_to_args(recipe: Recipe, args, given: set) -> None:
    """Overlay a recipe onto argparse `args`, but never clobber flags the user
    passed explicitly (names in `given`)."""
    def setg(attr, flag, value):
        if flag not in given:
            setattr(args, attr, value)
    setg("scale", "--scale", recipe.scale)
    setg("model", "--model", recipe.model)
    setg("hdr", "--hdr", recipe.hdr)
    setg("encoder", "--encoder", recipe.encoder)
    setg("crf", "--crf", recipe.crf)
    setg("preset", "--preset", recipe.preset)
    setg("hdr_gain", "--hdr-gain", recipe.hdr_gain)
    setg("start", "--start", recipe.trim_start)
    if recipe.trim_dur:
        setg("duration", "--duration", recipe.trim_dur)
    if not recipe.audio:
        setg("no_audio", "--no-audio", True)
    setg("interpolate", "--interpolate", recipe.interpolate)
    if recipe.slowmo:
        setg("slowmo", "--slowmo", True)
    if recipe.deinterlace:
        setg("deinterlace", "--deinterlace", True)
    setg("denoise", "--denoise", recipe.denoise)
    if recipe.stabilize:
        setg("stabilize", "--stabilize", True)
    if recipe.lut:
        setg("lut", "--lut", recipe.lut)
    if recipe.target:
        setg("target", "--target", recipe.target)
    # grade knobs (only fill those the user didn't override)
    knob_flag = {"saturation": "--saturation", "vibrance": "--vibrance-amt",
                 "contrast": "--contrast", "gamma": "--gamma", "warmth": "--warmth",
                 "sharpen": "--sharpen", "exposure": "--exposure", "tint": "--tint"}
    arg_attr = {"vibrance": "vibrance_amt"}
    for knob, flag in knob_flag.items():
        if flag not in given and knob in recipe.grade:
            setattr(args, arg_attr.get(knob, knob), recipe.grade[knob])


if __name__ == "__main__":
    for name, r in STYLES.items():
        print(f"{name:14s} scale={r.scale} model={r.model} hdr={r.hdr} "
              f"sat={r.grade['saturation']} warmth={r.grade['warmth']}")
