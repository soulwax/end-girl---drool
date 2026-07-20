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
    # --- reserved for the stage engine (declared now, executed next) ---
    frame_ops: list = field(default_factory=list)   # e.g. [{"op": "interpolate", "factor": 2}]
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
}


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
