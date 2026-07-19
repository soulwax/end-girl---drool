#!/usr/bin/env python3
"""auvide GUI - desktop front-end over upscale_hdr.py with a live grade preview.

Two tabs:
  * Render        - pick a file, set scale/model/HDR/encoder, Start; streams the
                    CLI log and shows a progress bar.
  * Grade & Preview - load one frame and dial in the color grade with sliders,
                    seeing a real before/after (draggable wipe). The exact grade
                    is shared with the render (via grade.py), so what you tune is
                    what you get.

The preview grades the SOURCE frame (color is resolution-independent), so slider
drags are snappy — no per-change upscaling.

Run:  uv run --python 3.12 --with pillow gui.py   (or double-click run-gui.bat)
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import grade
import tools

try:
    from PIL import Image, ImageTk, ImageDraw
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

HERE = Path(__file__).resolve().parent
CLI = HERE / "upscale_hdr.py"
FFMPEG = tools.ffmpeg()
FFPROBE = tools.ffprobe()
CONFIG = Path(os.environ.get("LOCALAPPDATA", HERE)) / "auvide" / "gui.json"
PREVIEW_DIR = Path(os.environ.get("TEMP", HERE)) / "auvide" / "preview"
INPUT_DIR = HERE / "input"
OUTPUT_DIR = HERE / "output"
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}

SCALES = ["2", "3", "4"]
MODELS = ["animevideo", "x4plus", "x4plus-anime"]
HDR = ["on", "off"]
ENCODERS = ["x265", "qsv"]
VIDEO_TYPES = [("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v"), ("All files", "*.*")]

# grade slider specs: (key, label, min, max, tip)
GRADE_SLIDERS = [
    ("exposure", "Exposure", -1.0, 1.0, "Overall brightness."),
    ("saturation", "Saturation", 0.5, 2.0, "Overall color intensity (1.0 = unchanged)."),
    ("vibrance", "Vibrance", 0.0, 1.0, "Selective saturation — boosts muted colors, protects skin."),
    ("contrast", "Contrast", 0.0, 1.0, "S-curve depth: deepens blacks, lifts highlights."),
    ("gamma", "Midtones", 0.8, 1.3, "Lift/lower midtone brightness (>1 brighter)."),
    ("warmth", "Warmth", -1.0, 1.0, "Cool (−) neutralizes a warm cast; warm (+) adds it."),
    ("tint", "Tint", -1.0, 1.0, "Green (−)  ↔  magenta (+) balance."),
    ("sharpen", "Sharpen", 0.0, 1.5, "Micro-contrast / edge sharpening."),
]

CHUNK_RE = re.compile(r"chunk\s+(\d+)/(\d+)")
ETA_RE = re.compile(r"ETA\s+(\S+)")
FPS_RE = re.compile(r"([\d.]+)\s*fps")
DONE_RE = re.compile(r"done\s+->")
NOWINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# palette
BG = "#101218"; PANEL = "#191b24"; FIELD = "#262a37"; LINE = "#333849"
TEXT = "#e9eaf2"; MUTED = "#888da4"
OK = "#57c98a"; ERR = "#e0687a"
ACCENTS = {  # name -> (base, hover/brighter)
    "Indigo": ("#6c8cff", "#8aa2ff"),
    "Violet": ("#a78bfa", "#c4b5fd"),
    "Teal":   ("#2dd4bf", "#5eead4"),
    "Amber":  ("#e0a35a", "#f0c084"),
    "Rose":   ("#fb7185", "#fda4af"),
}
ACCENT, ACCENT_HI = ACCENTS["Indigo"]
FONT = ("Segoe UI", 10); FONT_SM = ("Segoe UI", 9)
FONT_H = ("Segoe UI Semibold", 18); FONT_MONO = ("Consolas", 9)


def configure_accent(s, root, base, hi):
    """(Re)apply the accent-dependent styles — call to switch accent live."""
    s.configure("Accent.TButton", background=base, foreground="#0f1016",
                font=("Segoe UI Semibold", 10), padding=(16, 6))
    s.map("Accent.TButton", background=[("active", hi), ("disabled", LINE)],
          foreground=[("disabled", MUTED)])
    s.configure("Accent.Horizontal.TProgressbar", background=base, troughcolor=FIELD,
                bordercolor=LINE, lightcolor=base, darkcolor=base)
    s.configure("Val.TLabel", background=PANEL, foreground=hi, font=FONT_SM, width=6)
    s.configure("TLabelframe.Label", background=PANEL, foreground=hi, font=FONT_SM)
    s.map("TNotebook.Tab", background=[("selected", FIELD)], foreground=[("selected", TEXT)])
    root.option_add("*TCombobox*Listbox.selectBackground", base)


def apply_theme(root: tk.Tk):
    root.configure(bg=BG)
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=PANEL, foreground=TEXT, font=FONT,
                fieldbackground=FIELD, bordercolor=LINE, lightcolor=PANEL, darkcolor=PANEL)
    s.configure("TFrame", background=PANEL)
    s.configure("Bg.TFrame", background=BG)
    s.configure("TLabel", background=PANEL, foreground=TEXT)
    s.configure("Bg.TLabel", background=BG, foreground=TEXT)
    s.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=FONT_SM)
    s.configure("MutedBg.TLabel", background=BG, foreground=MUTED, font=FONT_SM)
    s.configure("Head.TLabel", background=BG, foreground=TEXT, font=FONT_H)
    s.configure("Info.TLabel", background=FIELD, foreground=TEXT, font=FONT_SM)
    s.configure("OK.TLabel", background=PANEL, foreground=OK)
    s.configure("Err.TLabel", background=PANEL, foreground=ERR)
    s.configure("Val.TLabel", background=PANEL, foreground=ACCENT_HI, font=FONT_SM, width=6)
    s.configure("TNotebook", background=BG, bordercolor=LINE)
    s.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(16, 7),
                bordercolor=LINE)
    s.map("TNotebook.Tab", background=[("selected", FIELD)], foreground=[("selected", TEXT)])
    s.configure("TLabelframe", background=PANEL, bordercolor=LINE, relief="solid", borderwidth=1)
    s.configure("TLabelframe.Label", background=PANEL, foreground=ACCENT_HI, font=FONT_SM)
    s.configure("TButton", background=FIELD, foreground=TEXT, bordercolor=LINE,
                focuscolor=PANEL, padding=(10, 5))
    s.map("TButton", background=[("active", LINE), ("disabled", PANEL)],
          foreground=[("disabled", MUTED)])
    s.configure("Accent.TButton", background=ACCENT, foreground="#0f1016",
                font=("Segoe UI Semibold", 10), padding=(16, 6))
    s.map("Accent.TButton", background=[("active", ACCENT_HI), ("disabled", LINE)],
          foreground=[("disabled", MUTED)])
    s.configure("Chip.TButton", background=PANEL, foreground=MUTED, bordercolor=LINE,
                padding=(9, 3), font=FONT_SM)
    s.map("Chip.TButton", background=[("active", FIELD)], foreground=[("active", TEXT)])
    s.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=TEXT,
                arrowcolor=TEXT, bordercolor=LINE, padding=3)
    s.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", TEXT)],
          selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)])
    s.configure("TSpinbox", fieldbackground=FIELD, background=FIELD, foreground=TEXT,
                arrowcolor=TEXT, bordercolor=LINE, padding=3)
    s.configure("TCheckbutton", background=PANEL, foreground=TEXT, focuscolor=PANEL)
    s.map("TCheckbutton", background=[("active", PANEL)],
          indicatorcolor=[("selected", ACCENT), ("!selected", FIELD)])
    s.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, bordercolor=LINE, padding=4)
    s.configure("Accent.Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD,
                bordercolor=LINE, lightcolor=ACCENT, darkcolor=ACCENT)
    s.configure("Horizontal.TScale", background=PANEL, troughcolor=FIELD)
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#0f1016")
    root.option_add("*TCombobox*Listbox.font", FONT_SM)
    configure_accent(s, root, ACCENT, ACCENT_HI)
    return s


class Tooltip:
    def __init__(self, widget, text):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, background="#0c0d12", foreground=TEXT,
                 font=FONT_SM, justify="left", padx=8, pady=4,
                 highlightbackground=LINE, highlightthickness=1).pack()

    def _hide(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class App:
    PW, PH = 900, 470  # preview canvas size

    def __init__(self, root: tk.Tk, self_test: bool = False, screenshot=None):
        self.root = root
        self.proc = None
        self.q: queue.Queue = queue.Queue()
        self.out_edited = False
        self.info = None
        self.start_ts = 0.0
        self.cancelling = False
        # preview state
        self._orig = None
        self._graded = None
        self._tkimg = None
        self._divx = self.PW // 2
        self._pgen = 0
        self._fgen = 0
        self._render_after = None
        self._frame_after = None
        self._loaded = False
        self._show_orig = False
        self._ab_off_after = None
        self._preset_defaults = grade.PRESETS["vibrant"]
        root.title("auvide  ·  AI upscale + vibrant HDR10")
        root.minsize(960, 740)
        self.style = apply_theme(root)
        self.accent = "Indigo"
        self._accentbar = None
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

        self._build_vars()
        self._build_ui()
        self._load_config()
        self._check_deps_ui()
        self._autoload_input()
        self._poll()
        self._tick_elapsed()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._center()
        if self_test:
            root.after(300, root.destroy)
        if screenshot:
            self._shotdir = screenshot
            root.after(2200, self._shots)

    def _shots(self):
        try:
            from PIL import ImageGrab
            self.root.deiconify(); self.root.lift(); self.root.update()

            def cap(name):
                self.root.update_idletasks()
                ImageGrab.grab().save(str(Path(self._shotdir) / name))  # full primary screen
            cap("render.png")
            self.nb.select(self.tab_prev)
            self.root.after(3200, lambda: (cap("preview.png"), self.root.destroy()))
        except Exception as e:
            print("screenshot failed:", e)
            self.root.destroy()

    # ---- state ----------------------------------------------------------
    def _build_vars(self):
        self.v_in = tk.StringVar()
        self.v_out = tk.StringVar()
        self.v_scale = tk.StringVar(value="2")
        self.v_model = tk.StringVar(value="animevideo")
        self.v_hdr = tk.StringVar(value="on")
        self.v_enc = tk.StringVar(value="x265")
        self.v_crf = tk.IntVar(value=19)
        self.v_hdrgain = tk.DoubleVar(value=1.5)
        self.v_chunk = tk.IntVar(value=300)
        self.v_gpu = tk.IntVar(value=0)
        self.v_tile = tk.IntVar(value=0)
        self.v_resume = tk.BooleanVar(value=True)
        self.v_keep = tk.BooleanVar(value=False)
        self.v_preset = tk.StringVar(value="medium")
        self.v_audio = tk.BooleanVar(value=True)
        self.v_open = tk.BooleanVar(value=True)
        self.v_start = tk.DoubleVar(value=0.0)
        self.v_dur = tk.DoubleVar(value=0.0)   # 0 = to end
        self.v_status = tk.StringVar(value="Ready — choose a video to begin.")
        self.v_elapsed = tk.StringVar(value="")
        self.v_plan = tk.StringVar(value="No file selected.")
        self.v_ptime = tk.DoubleVar(value=5.0)
        self.v_ptimelabel = tk.StringVar(value="0:00 / 0:00")
        self.v_pstatus = tk.StringVar(value="Choose a video to preview the grade.")
        # grade vars from the 'vibrant' preset
        base = grade.PRESETS["vibrant"]
        self.g_vars = {k: tk.DoubleVar(value=getattr(base, k))
                       for k, *_ in GRADE_SLIDERS}
        self.g_labels = {k: tk.StringVar(value=self._fmt(getattr(base, k)))
                         for k, *_ in GRADE_SLIDERS}
        for k, var in self.g_vars.items():
            var.trace_add("write", lambda *_a, kk=k: self._on_grade_change(kk))
        self.v_in.trace_add("write", lambda *_: (self._suggest_output(), self._on_input_change()))
        self.v_scale.trace_add("write", lambda *_: (self._suggest_output(), self._refresh_plan()))
        self.v_hdr.trace_add("write", lambda *_: self._suggest_output())
        self.v_model.trace_add("write", lambda *_: self._refresh_plan())

    @staticmethod
    def _fmt(v):
        return f"{v:+.2f}" if v < 0 else f"{v:.2f}"

    def _dbg(self, *a):
        if os.environ.get("AUVIDE_DEBUG"):
            print("[dbg]", *a, file=sys.stderr, flush=True)

    # ---- layout ---------------------------------------------------------
    def _build_ui(self):
        head = ttk.Frame(self.root, style="Bg.TFrame", padding=(18, 12, 18, 4))
        head.pack(fill="x")
        ttk.Label(head, text="auvide", style="Head.TLabel").pack(side="left")
        ttk.Label(head, text="AI upscale  →  vibrant HDR10", style="MutedBg.TLabel").pack(
            side="left", padx=12, pady=(10, 0))
        sw = ttk.Frame(head, style="Bg.TFrame"); sw.pack(side="right", pady=(4, 0))
        ttk.Label(sw, text="Accent", style="MutedBg.TLabel").pack(side="left", padx=(0, 8))
        self._swatches = {}
        for name, (base, hi) in ACCENTS.items():
            c = tk.Frame(sw, width=18, height=18, background=base, cursor="hand2",
                         highlightthickness=2, highlightbackground=BG)
            c.pack(side="left", padx=3); c.pack_propagate(False)
            c.bind("<Button-1>", lambda e, n=name: self._set_accent(n))
            self._swatches[name] = c
        self._accentbar = tk.Frame(self.root, height=2, background=ACCENT)
        self._accentbar.pack(fill="x")

        nb = ttk.Notebook(self.root)
        self.nb = nb
        nb.pack(fill="both", expand=True, padx=12, pady=(4, 10))
        self.tab_render = ttk.Frame(nb, padding=12)
        self.tab_prev = ttk.Frame(nb, padding=12)
        nb.add(self.tab_render, text="  Render  ")
        nb.add(self.tab_prev, text="  Grade & Preview  ")
        self._build_render_tab(self.tab_render)
        self._build_preview_tab(self.tab_prev)
        self._set_accent(self.accent)

    def _build_render_tab(self, body):
        body.columnconfigure(0, weight=1)
        pad = dict(padx=6, pady=5)

        files = ttk.Frame(body)
        files.grid(row=0, column=0, sticky="ew")
        files.columnconfigure(1, weight=1)
        ttk.Label(files, text="Input").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(files, textvariable=self.v_in).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(files, text="Browse…", command=self._browse_in).grid(row=0, column=2, **pad)
        ttk.Label(files, text="Output").grid(row=1, column=0, sticky="w", **pad)
        e_out = ttk.Entry(files, textvariable=self.v_out)
        e_out.grid(row=1, column=1, sticky="ew", **pad)
        e_out.bind("<Key>", lambda *_: setattr(self, "out_edited", True))
        ttk.Button(files, text="Browse…", command=self._browse_out).grid(row=1, column=2, **pad)

        strip = tk.Frame(body, background=FIELD, highlightbackground=LINE, highlightthickness=1)
        strip.grid(row=1, column=0, sticky="ew", pady=(4, 8))
        ttk.Label(strip, textvariable=self.v_plan, style="Info.TLabel",
                  background=FIELD, padding=(10, 7)).pack(side="left")

        opt = ttk.LabelFrame(body, text="Render options", padding=(10, 6))
        opt.grid(row=2, column=0, sticky="ew", pady=4)
        for c in (1, 3):
            opt.columnconfigure(c, weight=1)

        def combo(r, c, label, var, values, tip=""):
            ttk.Label(opt, text=label).grid(row=r, column=c * 2, sticky="w", padx=6, pady=5)
            cb = ttk.Combobox(opt, textvariable=var, values=values, state="readonly", width=13)
            cb.grid(row=r, column=c * 2 + 1, sticky="ew", padx=6, pady=5)
            if tip:
                Tooltip(cb, tip)

        combo(0, 0, "Scale", self.v_scale, SCALES, "Upscale factor (2× recommended).")
        combo(0, 1, "Model", self.v_model, MODELS, "animevideo=fast/video, x4plus=sharp photo.")
        combo(1, 0, "HDR", self.v_hdr, HDR, "HDR10 remap (on) or SDR BT.709 (off).")
        combo(1, 1, "Encoder", self.v_enc, ENCODERS, "x265=software; qsv=Intel GPU (faster).")
        ttk.Label(opt, text="Quality (CRF)").grid(row=2, column=0, sticky="w", padx=6, pady=5)
        cf = ttk.Frame(opt); cf.grid(row=2, column=1, sticky="ew", padx=6, pady=5)
        cf.columnconfigure(0, weight=1)
        self.v_crflabel = tk.StringVar(value="19")
        self.v_crf.trace_add("write", lambda *_: self.v_crflabel.set(str(self.v_crf.get())))
        ttk.Scale(cf, from_=12, to=30, orient="horizontal", variable=self.v_crf,
                  command=lambda v: self.v_crf.set(round(float(v)))).grid(row=0, column=0, sticky="ew")
        ttk.Label(cf, textvariable=self.v_crflabel, style="Val.TLabel").grid(row=0, column=1)
        ttk.Label(opt, text="HDR punch").grid(row=2, column=2, sticky="w", padx=6, pady=5)
        ttk.Spinbox(opt, from_=1.0, to=3.0, increment=0.1, textvariable=self.v_hdrgain,
                    width=6).grid(row=2, column=3, sticky="w", padx=6, pady=5)

        num = ttk.Frame(opt)
        num.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(6, 2))
        def spin(label, var, lo, hi, step=1, tip=""):
            f = ttk.Frame(num); f.pack(side="left", padx=(6, 14))
            ttk.Label(f, text=label, style="Muted.TLabel").pack(side="left", padx=(0, 5))
            sp = ttk.Spinbox(f, from_=lo, to=hi, increment=step, textvariable=var, width=6)
            sp.pack(side="left")
            if tip:
                Tooltip(sp, tip)
        spin("Chunk", self.v_chunk, 30, 4000, 30, "Frames per encode chunk.")
        spin("GPU id", self.v_gpu, -1, 8, 1, "Real-ESRGAN GPU (-1 = CPU).")
        spin("Tile", self.v_tile, 0, 1024, 32, "0 = auto; lower on VRAM OOM.")
        ttk.Checkbutton(num, text="Resume", variable=self.v_resume).pack(side="left", padx=8)
        ttk.Checkbutton(num, text="Keep scratch", variable=self.v_keep).pack(side="left", padx=8)

        num2 = ttk.Frame(opt); num2.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(4, 2))
        ttk.Label(num2, text="Enc preset", style="Muted.TLabel").pack(side="left", padx=(6, 5))
        cbp = ttk.Combobox(num2, textvariable=self.v_preset, state="readonly", width=10,
                           values=["ultrafast", "veryfast", "faster", "fast", "medium",
                                   "slow", "slower", "veryslow"])
        cbp.pack(side="left")
        Tooltip(cbp, "Encoder speed/quality. Slower = smaller/better, much slower.")
        ttk.Checkbutton(num2, text="Include audio", variable=self.v_audio).pack(side="left", padx=14)
        ttk.Checkbutton(num2, text="Open when done", variable=self.v_open).pack(side="left", padx=8)

        trim = ttk.Frame(opt); trim.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(4, 2))
        ttk.Label(trim, text="Trim", style="Muted.TLabel").pack(side="left", padx=(6, 8))
        ttk.Label(trim, text="start", style="Muted.TLabel").pack(side="left")
        sp1 = ttk.Spinbox(trim, from_=0, to=100000, increment=1, textvariable=self.v_start, width=7)
        sp1.pack(side="left", padx=(4, 2))
        ttk.Label(trim, text="s    length", style="Muted.TLabel").pack(side="left")
        sp2 = ttk.Spinbox(trim, from_=0, to=100000, increment=1, textvariable=self.v_dur, width=7)
        sp2.pack(side="left", padx=(4, 2))
        ttk.Label(trim, text="s   (length 0 = whole clip · set a few seconds for a quick test)",
                  style="Muted.TLabel").pack(side="left", padx=(4, 0))
        Tooltip(sp1, "Skip this many seconds from the start.")
        Tooltip(sp2, "Process only this many seconds (0 = to the end).")

        bar = ttk.Frame(body); bar.grid(row=3, column=0, sticky="ew", pady=(8, 4))
        self.btn_start = ttk.Button(bar, text="▶  Start", style="Accent.TButton", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_cancel = ttk.Button(bar, text="Cancel", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        ttk.Button(bar, text="Show command", command=self._show_cmd).pack(side="left", padx=6)
        self.btn_open = ttk.Button(bar, text="Open folder", command=self._open_out, state="disabled")
        self.btn_open.pack(side="left", padx=6)
        ttk.Label(bar, textvariable=self.v_elapsed, style="Muted.TLabel").pack(side="right", padx=6)

        self.pbar = ttk.Progressbar(body, mode="determinate", maximum=100,
                                    style="Accent.Horizontal.TProgressbar")
        self.pbar.grid(row=4, column=0, sticky="ew", pady=(4, 3))
        self.lbl_status = ttk.Label(body, textvariable=self.v_status, style="Muted.TLabel")
        self.lbl_status.grid(row=5, column=0, sticky="w", padx=6)

        logf = ttk.LabelFrame(body, text="Log", padding=4)
        logf.grid(row=6, column=0, sticky="nsew", pady=(6, 0))
        body.rowconfigure(6, weight=1)
        self.log = tk.Text(logf, height=9, wrap="none", state="disabled", font=FONT_MONO,
                           background="#0e0f14", foreground="#c8ccd8", insertbackground=TEXT,
                           relief="flat", borderwidth=0, highlightthickness=0)
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=sb.set)
        self.log.tag_configure("err", foreground=ERR)
        self.log.tag_configure("ok", foreground=OK)
        self.log.tag_configure("cmd", foreground=MUTED)

    def _build_preview_tab(self, body):
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        top = ttk.Frame(body); top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        top.columnconfigure(2, weight=1)
        ttk.Button(top, text="◀", width=3, command=lambda: self._step_frame(-1)).grid(row=0, column=0)
        ttk.Button(top, text="▶", width=3, command=lambda: self._step_frame(1)).grid(row=0, column=1, padx=(4, 8))
        self.scrub = ttk.Scale(top, from_=0, to=1, orient="horizontal", variable=self.v_ptime,
                               command=lambda v: self._on_scrub())
        self.scrub.grid(row=0, column=2, sticky="ew")
        ttk.Label(top, textvariable=self.v_ptimelabel, style="Val.TLabel", width=12).grid(
            row=0, column=3, padx=(8, 8))
        self.btn_ab = ttk.Button(top, text="Hold: original")
        self.btn_ab.grid(row=0, column=4)
        self.btn_ab.bind("<ButtonPress-1>", lambda e: self._ab(True))
        self.btn_ab.bind("<ButtonRelease-1>", lambda e: self._ab(False))
        Tooltip(self.btn_ab, "Hold (or press Space) to flash the untouched original.")
        ttk.Label(body, textvariable=self.v_pstatus, style="Muted.TLabel").grid(
            row=4, column=0, sticky="w", padx=4, pady=(6, 0))
        self.root.bind("<KeyPress-space>", lambda e: self._ab(True))
        self.root.bind("<KeyRelease-space>", lambda e: self._ab(False))

        # preview canvas
        if HAVE_PIL:
            self.canvas = tk.Canvas(body, width=self.PW, height=self.PH, background="#0e0f14",
                                    highlightthickness=1, highlightbackground=LINE)
            self.canvas.grid(row=1, column=0, sticky="n", pady=2)
            self.canvas.bind("<Button-1>", self._wipe)
            self.canvas.bind("<B1-Motion>", self._wipe)
            self.canvas.create_text(self.PW // 2, self.PH // 2, fill=MUTED, font=FONT,
                                    text="Scrub the timeline above to load a frame — then drag "
                                    "here to wipe BEFORE | AFTER.", tags="hint")
        else:
            self.canvas = None
            warn = ttk.Frame(body, style="TFrame", padding=20)
            warn.grid(row=1, column=0, sticky="n")
            ttk.Label(warn, text="Live preview needs Pillow.\n\nInstall it, then relaunch:\n"
                      "    uv run --python 3.12 --with pillow gui.py",
                      style="Muted.TLabel", justify="left").pack()

        # grade sliders
        gf = ttk.LabelFrame(body, text="Grade", padding=(12, 8))
        gf.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        gf.columnconfigure(1, weight=1)
        for i, (key, label, lo, hi, tip) in enumerate(GRADE_SLIDERS):
            ttk.Label(gf, text=label).grid(row=i, column=0, sticky="w", padx=(4, 10), pady=3)
            sc = ttk.Scale(gf, from_=lo, to=hi, orient="horizontal", variable=self.g_vars[key])
            sc.grid(row=i, column=1, sticky="ew", pady=3)
            sc.bind("<Double-Button-1>", lambda e, kk=key: self._reset_slider(kk))
            Tooltip(sc, tip + "  (double-click to reset)")
            ttk.Label(gf, textvariable=self.g_labels[key], style="Val.TLabel").grid(
                row=i, column=2, padx=(10, 4))

        chips = ttk.Frame(body); chips.grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Label(chips, text="Presets", style="Muted.TLabel").pack(side="left", padx=(4, 8))
        for name in grade.PRESETS:
            ttk.Button(chips, text=name.capitalize(), style="Chip.TButton",
                       command=lambda n=name: self._apply_grade_preset(n)).pack(side="left", padx=3)
        ttk.Button(chips, text="Reset", style="Chip.TButton",
                   command=lambda: self._apply_grade_preset("vibrant")).pack(side="left", padx=(12, 3))

    # ---- helpers --------------------------------------------------------
    def _set_accent(self, name):
        if name not in ACCENTS:
            return
        base, hi = ACCENTS[name]
        self.accent = name
        configure_accent(self.style, self.root, base, hi)
        if self._accentbar:
            self._accentbar.configure(background=base)
        for n, c in getattr(self, "_swatches", {}).items():
            c.configure(highlightbackground=(TEXT if n == name else BG))

    def _center(self):
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = max(0, (self.root.winfo_screenheight() - h) // 4)
        self.root.geometry(f"+{x}+{y}")

    def _current_grade(self) -> grade.Grade:
        return grade.Grade(**{k: self.g_vars[k].get() for k, *_ in GRADE_SLIDERS})

    def _apply_grade_preset(self, name):
        g = grade.PRESETS[name]
        self._preset_defaults = g
        for k, *_ in GRADE_SLIDERS:
            self.g_vars[k].set(getattr(g, k))

    def _reset_slider(self, key):
        self.g_vars[key].set(getattr(self._preset_defaults, key))

    def _on_grade_change(self, key):
        self.g_labels[key].set(self._fmt(self.g_vars[key].get()))
        self._schedule_render()

    def _suggest_output(self):
        if self.out_edited:
            return
        src = self.v_in.get().strip()
        if not src:
            return
        p = Path(src)
        tag = "hdr" if self.v_hdr.get() == "on" else "sdr"
        self.v_out.set(str(OUTPUT_DIR / f"{p.stem}_{self.v_scale.get()}x_{tag}.mp4"))

    def _check_deps_ui(self):
        m = tools.missing()
        if m:
            self._set_status("Missing: " + ", ".join(m)
                             + " — run setup.ps1 (Windows) or see README.", "err")

    def _autoload_input(self):
        if self.v_in.get().strip() or not INPUT_DIR.exists():
            return
        vids = [p for p in sorted(INPUT_DIR.glob("*")) if p.suffix.lower() in VIDEO_EXTS]
        if len(vids) == 1:
            self.v_in.set(str(vids[0]))

    def _browse_in(self):
        start = str(INPUT_DIR if INPUT_DIR.exists() else HERE)
        f = filedialog.askopenfilename(title="Choose a video", filetypes=VIDEO_TYPES,
                                       initialdir=start)
        if f:
            self.out_edited = False
            self.v_in.set(f)

    def _browse_out(self):
        f = filedialog.asksaveasfilename(title="Save as", defaultextension=".mp4",
                                         filetypes=VIDEO_TYPES)
        if f:
            self.out_edited = True
            self.v_out.set(f)

    def _on_input_change(self):
        p = self.v_in.get().strip()
        if p and Path(p).exists() and FFPROBE:
            self.v_plan.set("Reading media…")
            threading.Thread(target=self._probe, args=(p,), daemon=True).start()
        else:
            self.info = None
            self.v_plan.set("No file selected." if not p else "File not found.")

    def _probe(self, path):
        try:
            out = subprocess.run(
                [str(FFPROBE), "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height,r_frame_rate,nb_frames,duration", "-of", "json", path],
                capture_output=True, text=True, creationflags=NOWINDOW, timeout=30)
            st = json.loads(out.stdout)["streams"][0]
            num, den = (st.get("r_frame_rate", "24/1").split("/") + ["1"])[:2]
            fps = int(num) / max(1, int(den or 1))
            nb = st.get("nb_frames"); dur = float(st.get("duration") or 0)
            frames = int(nb) if (nb and nb.isdigit()) else int(dur * fps)
            self.q.put(("info", dict(w=int(st["width"]), h=int(st["height"]),
                                     fps=fps, frames=frames, dur=dur)))
        except Exception as e:
            self.q.put(("info", None))
            self.q.put(f"[probe] could not read media: {e}\n")

    def _refresh_plan(self):
        if not self.info:
            return
        w, h, fps, frames, dur = (self.info[k] for k in ("w", "h", "fps", "frames", "dur"))
        scale = int(self.v_scale.get())
        model = self.v_model.get()
        per = 5.6 if model != "animevideo" else 1.4 * (scale / 2) ** 2
        mm, ss = divmod(int(dur), 60)
        self.v_plan.set(
            f"Source {w}×{h} · {fps:.2f} fps · {mm}:{ss:02d} · {frames} frames"
            f"     →     Target {w*scale}×{h*scale} · ~{self._hms(frames*per)} to render")

    def _on_media_ready(self):
        """Media probed: set the scrubber range and auto-load a frame."""
        self._dbg("media_ready HAVE_PIL=", HAVE_PIL, "info=", bool(self.info),
                  "loaded=", self._loaded)
        if not HAVE_PIL or not self.info:
            return
        dur = self.info["dur"]
        self.scrub.configure(to=max(0.1, dur))
        if not self._loaded:
            self.v_ptime.set(round(dur / 2) if dur > 4 else 0.0)
            self._request_frame()
        self.v_ptimelabel.set(self._timelabel())

    @staticmethod
    def _hms(sec):
        sec = int(sec); h, r = divmod(sec, 3600); m, s = divmod(r, 60)
        return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"

    # ---- preview --------------------------------------------------------
    def _timelabel(self):
        dur = self.info["dur"] if self.info else 0
        t = self.v_ptime.get()
        return f"{int(t)//60}:{int(t)%60:02d} / {int(dur)//60}:{int(dur)%60:02d}"

    def _on_scrub(self):
        self.v_ptimelabel.set(self._timelabel())
        self._request_frame()

    def _step_frame(self, direction):
        fps = self.info["fps"] if self.info else 24.0
        dur = self.info["dur"] if self.info else self.v_ptime.get()
        step = max(1.0 / fps, 0.04)
        self.v_ptime.set(min(max(0.0, self.v_ptime.get() + direction * step), max(0.0, dur)))
        self._on_scrub()

    def _request_frame(self):
        """Debounced: extract the frame at the current scrubber time."""
        if not HAVE_PIL:
            return
        p = self.v_in.get().strip()
        if not p or not Path(p).exists():
            return
        if self._frame_after:
            self.root.after_cancel(self._frame_after)
        self._frame_after = self.root.after(180, self._kick_frame)

    def _kick_frame(self):
        self._frame_after = None
        self._fgen += 1
        self.v_pstatus.set("Loading frame…")
        self._dbg("kick_frame t=", self.v_ptime.get(), "gen=", self._fgen)
        threading.Thread(target=self._extract_worker,
                         args=(self._fgen, self.v_in.get().strip(),
                               max(0.0, float(self.v_ptime.get()))), daemon=True).start()

    def _extract_worker(self, gen, path, t):
        src = PREVIEW_DIR / "src.png"
        try:
            r = subprocess.run([str(FFMPEG), "-y", "-ss", str(t), "-i", path, "-frames:v", "1",
                                str(src)], creationflags=NOWINDOW, capture_output=True,
                               text=True, timeout=60)
            self._dbg("extract rc=", r.returncode, "exists=", src.exists(),
                      "err=", r.stderr[-160:] if r.returncode else "")
            img = Image.open(src).convert("RGB"); img.load()
            self.q.put(("sample", gen, img))
        except Exception as e:
            self._dbg("extract EXC", e)
            self.q.put(("perror", f"could not extract frame: {e}"))

    def _ab(self, show_original):
        # defer turning OFF slightly so key auto-repeat (press/release spam)
        # doesn't strobe the image
        if not self._loaded:
            return
        if self._ab_off_after:
            self.root.after_cancel(self._ab_off_after)
            self._ab_off_after = None
        if show_original:
            if not self._show_orig:
                self._show_orig = True
                self._composite()
        else:
            self._ab_off_after = self.root.after(70, self._ab_off)

    def _ab_off(self):
        self._ab_off_after = None
        if self._show_orig:
            self._show_orig = False
            self._composite()

    def _schedule_render(self):
        if not HAVE_PIL or self._orig is None:
            return
        if self._render_after:
            self.root.after_cancel(self._render_after)
        self._render_after = self.root.after(160, self._kick_render)

    def _kick_render(self):
        self._render_after = None
        self._pgen += 1
        gen = self._pgen
        g = self._current_grade()
        threading.Thread(target=self._grade_worker, args=(gen, g), daemon=True).start()

    def _grade_worker(self, gen, g):
        src = PREVIEW_DIR / "src.png"
        out = PREVIEW_DIR / f"g{gen % 3}.png"
        vf = grade.build_chain(g, out_format="rgb24", working="gbrpf32le")
        try:
            r = subprocess.run([str(FFMPEG), "-y", "-i", str(src), "-vf", vf, str(out)],
                               creationflags=NOWINDOW, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                self.q.put(("perror", f"grade render failed: {r.stderr[-300:]}"))
                return
            img = Image.open(out).convert("RGB"); img.load()
            self.q.put(("graded", gen, img))
        except Exception as e:
            self.q.put(("perror", f"grade render error: {e}"))

    def _fit(self, img):
        w, h = img.size
        scale = min(self.PW / w, self.PH / h)
        return img.resize((max(1, int(w * scale)), max(1, int(h * scale))))

    def _label(self, d, x0, x1, y, text):
        d.rectangle([x0, y, x1, y + 20], fill=(0, 0, 0))
        d.text((x0 + 6, y + 4), text, fill=(255, 255, 255))

    def _composite(self):
        if self._orig is None or self._graded is None:
            return
        a = self._fit(self._orig); b = self._fit(self._graded)
        w, h = a.size
        if self._show_orig:                       # A/B: flash the full original
            combo = a.copy()
            d = ImageDraw.Draw(combo)
            self._label(d, 6, 96, 6, "ORIGINAL")
        else:
            div = max(0, min(w, self._divx))
            combo = a.copy()
            if div < w:
                combo.paste(b.crop((div, 0, w, h)), (div, 0))
            d = ImageDraw.Draw(combo)
            d.line([(div, 0), (div, h)], fill=(255, 255, 255), width=2)
            cy = h // 2                            # grab handle on the divider
            d.ellipse([div - 9, cy - 9, div + 9, cy + 9], fill=(255, 255, 255))
            d.line([(div - 3, cy - 4), (div - 3, cy + 4)], fill=(30, 30, 30), width=1)
            d.line([(div + 3, cy - 4), (div + 3, cy + 4)], fill=(30, 30, 30), width=1)
            self._label(d, 6, 78, 6, "BEFORE")
            self._label(d, w - 68, w - 6, 6, "AFTER")
        self._tkimg = ImageTk.PhotoImage(combo)
        self.canvas.delete("all")
        ox = (self.PW - w) // 2; oy = (self.PH - h) // 2
        self._img_off = (ox, oy, w)
        self.canvas.create_image(ox, oy, anchor="nw", image=self._tkimg)

    def _wipe(self, event):
        if not hasattr(self, "_img_off"):
            return
        ox, oy, w = self._img_off
        self._divx = max(0, min(w, event.x - ox))
        self._composite()

    # ---- command / run --------------------------------------------------
    def _build_command(self):
        g = self._current_grade()
        cmd = [sys.executable, "-u", str(CLI), self.v_in.get(), "-o", self.v_out.get(),
               "--scale", self.v_scale.get(), "--model", self.v_model.get(),
               "--hdr", self.v_hdr.get(), "--encoder", self.v_enc.get(),
               "--crf", str(self.v_crf.get()), "--chunk", str(self.v_chunk.get()),
               "--gpu", str(self.v_gpu.get()),
               "--saturation", f"{g.saturation:.3f}", "--vibrance-amt", f"{g.vibrance:.3f}",
               "--contrast", f"{g.contrast:.3f}", "--gamma", f"{g.gamma:.3f}",
               "--warmth", f"{g.warmth:.3f}", "--tint", f"{g.tint:.3f}",
               "--exposure", f"{g.exposure:.3f}", "--sharpen", f"{g.sharpen:.3f}",
               "--hdr-gain", f"{self.v_hdrgain.get():.2f}", "--preset", self.v_preset.get()]
        if self.v_start.get() > 0:
            cmd += ["--start", f"{self.v_start.get():g}"]
        if self.v_dur.get() > 0:
            cmd += ["--duration", f"{self.v_dur.get():g}"]
        if not self.v_audio.get():
            cmd.append("--no-audio")
        if self.v_tile.get() > 0:
            cmd += ["--tile", str(self.v_tile.get())]
        if self.v_resume.get():
            cmd.append("--resume")
        if self.v_keep.get():
            cmd.append("--keep")
        return cmd

    def _show_cmd(self):
        if not self.v_in.get().strip():
            messagebox.showwarning("auvide", "Pick an input video first.")
            return
        pretty = " ".join(f'"{c}"' if " " in c else c for c in self._build_command())
        messagebox.showinfo("Equivalent command", pretty)

    def _append(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text, tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, text, kind="muted"):
        self.v_status.set(text)
        self.lbl_status.configure(
            style={"ok": "OK.TLabel", "err": "Err.TLabel"}.get(kind, "Muted.TLabel"))

    def _start(self):
        if self.proc is not None:
            return
        if not self.v_in.get().strip() or not Path(self.v_in.get()).exists():
            messagebox.showerror("auvide", "Input video not found.")
            return
        if not CLI.exists():
            messagebox.showerror("auvide", f"Cannot find upscale_hdr.py at {CLI}")
            return
        cmd = self._build_command()
        self._append("$ " + " ".join(cmd) + "\n\n", "cmd")
        self.pbar.configure(value=0)
        self._set_status("Starting…")
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.btn_open.configure(state="disabled")
        self.start_ts = time.time()
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env, creationflags=NOWINDOW)
        except Exception as e:
            messagebox.showerror("auvide", f"Failed to launch:\n{e}")
            self._reset_buttons()
            return
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        for line in proc.stdout:
            self.q.put(line)
        proc.wait()
        self.q.put(("exit", proc.returncode))

    def _cancel(self):
        if self.proc and self.proc.poll() is None:
            self.cancelling = True
            self._set_status("Cancelling…")
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                                   creationflags=NOWINDOW, capture_output=True)
                else:
                    self.proc.terminate()
            except Exception:
                pass

    def _reset_buttons(self):
        self.btn_start.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        self.proc = None
        self.start_ts = 0.0

    def _open_out(self):
        out = Path(self.v_out.get())
        folder = out.parent if out.parent.exists() else HERE
        try:
            if sys.platform == "win32":
                os.startfile(str(folder))  # noqa: S606
            else:
                subprocess.run(["xdg-open", str(folder)])
        except Exception:
            pass

    # ---- pumps ----------------------------------------------------------
    def _tick_elapsed(self):
        if self.start_ts:
            self.v_elapsed.set("elapsed " + self._hms(time.time() - self.start_ts))
        self.root.after(1000, self._tick_elapsed)

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if isinstance(msg, tuple):
                    kind = msg[0]
                    if kind == "info":
                        self.info = msg[1]
                        if msg[1]:
                            self._refresh_plan()
                            self._on_media_ready()
                        elif self.v_in.get().strip():
                            self.v_plan.set("Could not read media info.")
                    elif kind == "exit":
                        self._on_exit(msg[1])
                    elif kind == "sample":
                        self._dbg("poll sample gen=", msg[1], "cur=", self._fgen)
                        if msg[1] == self._fgen:      # ignore stale scrubs
                            self._orig = msg[2]
                            self._loaded = True
                            self.v_pstatus.set("Drag the image to wipe · Space = original · "
                                               "double-click a slider to reset")
                            self._schedule_render()
                    elif kind == "graded":
                        self._dbg("poll graded gen=", msg[1], "cur=", self._pgen)
                        if msg[1] == self._pgen:
                            self._graded = msg[2]
                            self._composite()
                    elif kind == "perror":
                        self._dbg("poll perror", msg[1])
                        self.v_pstatus.set(msg[1])
                    continue
                low = "err" if "[error]" in msg else ("ok" if DONE_RE.search(msg) else None)
                self._append(msg, low)
                m = CHUNK_RE.search(msg)
                if m:
                    k, n = int(m.group(1)), int(m.group(2))
                    self.pbar.configure(value=max(1, round(k / n * 100)))
                    eta = ETA_RE.search(msg); fps = FPS_RE.search(msg)
                    extra = []
                    if fps: extra.append(f"{fps.group(1)} fps")
                    if eta: extra.append(f"ETA {eta.group(1)}")
                    tail = ("  ·  " + "  ·  ".join(extra)) if extra else ""
                    self._set_status(f"Upscaling + encoding — chunk {k}/{n}{tail}")
                elif "[1/3]" in msg:
                    self._set_status("Extracting frames…")
                elif "[3/3]" in msg:
                    self._set_status("Concatenating + muxing audio…")
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _on_exit(self, code):
        if self.cancelling:
            self._set_status("Cancelled — re-run with Resume to continue.", "muted")
            self.pbar.configure(value=0)
        elif code == 0:
            self.pbar.configure(value=100)
            self._set_status("Done ✔  —  output ready", "ok")
            self.btn_open.configure(state="normal")
            if self.v_open.get():
                self._open_out()
        else:
            self._set_status(f"Failed (exit {code}) — see log", "err")
        self.cancelling = False
        self._reset_buttons()

    # ---- config ---------------------------------------------------------
    def _cfg_map(self):
        m = dict(scale=self.v_scale, model=self.v_model, hdr=self.v_hdr, encoder=self.v_enc,
                 crf=self.v_crf, hdrgain=self.v_hdrgain, chunk=self.v_chunk, gpu=self.v_gpu,
                 tile=self.v_tile, resume=self.v_resume, keep=self.v_keep, preset=self.v_preset,
                 audio=self.v_audio, open_done=self.v_open, trim_start=self.v_start,
                 trim_dur=self.v_dur)
        for k, *_ in GRADE_SLIDERS:
            m[f"g_{k}"] = self.g_vars[k]
        return m

    def _load_config(self):
        try:
            d = json.loads(CONFIG.read_text())
            for k, var in self._cfg_map().items():
                if k in d:
                    var.set(d[k])
            if d.get("accent") in ACCENTS:
                self._set_accent(d["accent"])
        except Exception:
            pass

    def _save_config(self):
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            data = {k: v.get() for k, v in self._cfg_map().items()}
            data["accent"] = self.accent
            CONFIG.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _on_close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("auvide", "A render is running. Cancel and quit?"):
                return
            self._cancel()
        self._save_config()
        self.root.destroy()


def main():
    self_test = "--self-test" in sys.argv
    shot = None
    if "--screenshot" in sys.argv:
        shot = sys.argv[sys.argv.index("--screenshot") + 1]
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.25)
    except tk.TclError:
        pass
    App(root, self_test=self_test, screenshot=shot)
    root.mainloop()
    if self_test:
        print(f"self-test OK (Pillow={'yes' if HAVE_PIL else 'no'})")


if __name__ == "__main__":
    main()
