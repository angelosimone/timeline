#!/usr/bin/env python3
"""
timeline.py: draws annotated timeline charts from a JSON file.

    python timeline.py examples/hooke_young_en.json
    python timeline.py examples/*.json            # several files at once
    python timeline.py my_data.json -o figure.pdf # choose name / format

Each JSON file holds a "config" block (all keys are optional) and a list of
"events" (date, label, optional note). See README.md for the full reference.

How the layout works
--------------------
All geometry is computed in inches from the *rendered* size of each text, so
the chart is never squeezed or clipped and the figure height follows from
the content.

  * Each event becomes a "block" (label + bold date) attached to the time axis
    by a connector. Events with exactly the same date share one block.
  * A small optimiser chooses, for every event, the side of the axis
    (above/below, or right/left) and how the block sits on its connector
    (centred, or as a flag to the left/right), then stacks the blocks so that
    no two texts overlap and no connector runs through another event's text,
    keeping the chart as compact as possible.
  * Events too close in time to be separated that way get a connector with
    one bend, in the empty band next to the axis, which moves their label
    sideways. In horizontal charts, labels still in conflict are then
    re-wrapped to narrower lines.
  * A tick label that would be covered by a connector is nudged sideways,
    or left out if there is no room.
  * Notes are word-wrapped using measured text widths and stacked using their
    measured heights, so the spacing between notes is uniform.
  * Finally every text box and connector of the drawing is checked, and the
    result is printed ("layout check: OK"). If something could not be
    resolved, the crossing connector is drawn faded and a warning is printed.

Author and history
------------------
Angelo Simone (University of Padua). MIT License, see LICENSE.

  * v0 (before April 2026): one script per timeline, data in the code;
    started from the CoderzColumn tutorial "Timeline using Matplotlib"
    (https://coderzcolumn.com/tutorials/data-science/timeline-using-matplotlib).
  * v1 (April 2026): data and settings in JSON, notes box, collision-aware
    stacking; developed with the help of ChatGPT (OpenAI) and Gemini (Google).
  * v2.0 (4 November 2026): layout engine rewritten with Claude (Anthropic):
    layout in inches from measured text, optimised label placement with no
    overlaps (bent connectors when needed), automatic figure height and axis
    ticks, date ranges, merged same-date events, vertical orientation, uniform
    note spacing, layout self-check, command-line interface.

Requires Python >= 3.9 and matplotlib >= 3.5.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
import textwrap
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib

if "--show" not in sys.argv:
    matplotlib.use("Agg")  # no window needed: faster and works headless
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import cbook  # noqa: E402
from matplotlib.font_manager import FontProperties  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

__version__ = "2.0"

# ─────────────────────────────────────────────────────────────────────────────
# 1. CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

DEFAULTS: dict = {
    # Output
    "title": "",
    "output_file": None,  # default: same name as the JSON file, .png
    "dpi": 300,
    # Geometry (inches)
    "orientation": "horizontal",  # "horizontal" or "vertical"
    "chart_width": 13.0,  # figure width (minimum width when vertical)
    "chart_height": 9.0,  # vertical only: length of the time axis
    "axis_clearance": 0.30,  # minimum distance between axis and labels
    "label_padding": 0.12,  # minimum free space around every label
    "date_gap": 0.05,  # space between a label and its date
    "notes_gap": 0.35,  # space between chart and notes
    # Axis ticks
    "tick_step": "auto",  # "auto" or a number of years
    "tick_label_offset": 0.12,
    # Text
    "font_family": "DejaVu Sans",
    "font_title": 14,
    "font_ticks": 9,
    "font_dates": 9,
    "font_events": 9,
    "font_notes": 9,
    "event_linespacing": 1.15,
    "note_linespacing": 1.35,
    "label_wrap_chars": None,  # auto-wrap labels longer than this (chars)
    # Notes
    "show_notes": True,
    "note_spacing": 0.8,  # empty space between two notes, in note lines
    # Colours
    "background_color": "#f0f0f0",
    "text_color": "black",
    "axis_color": "black",
    "tick_color": "olive",
    "marker_color": "white",
    "connector_color": "red",
    "range_color": "#e8846f",
    "border_color": None,  # e.g. "grey" to frame chart and notes
}

# Keys used by version 1.x of this script → current names.
LEGACY_KEYS = {
    "font_centuries": "font_ticks",
    "note_paragraph_gap": "note_spacing",
    "century_tick_distance": "tick_label_offset",
}
# Version 1.x keys that no longer have any effect.
OBSOLETE_KEYS = {
    "min_axis_distance", "label_distance", "close_year_threshold",
    "note_wrap_chars", "note_prefix_gap", "connector_alpha_under_text",
    "event_gap_scale", "connector_text_gap_scale",
}

MARGIN_X = 0.5  # left/right figure margin (in)
EDGE_PAD = 0.08  # labels never come closer than this to the figure edge
TOP_PAD = 0.3
BOTTOM_PAD = 0.3
TITLE_GAP = 0.25
CONNECTOR_GAP = 0.03  # gap between connector end and text
CONNECTOR_CLEAR = 0.07  # horizontal clearance between a connector and text
FLAG_INSET = 0.04  # connector position inside a flag-style label
# Sideways moves (in) of a label whose connector cannot go straight: the
# connector then bends once, inside the empty band next to the axis.
SHIFTS = (-0.7, -0.45, -0.25, 0.25, 0.45, 0.7)


def load_config(raw: dict, source: str) -> dict:
    conf = dict(DEFAULTS)
    for key, value in raw.items():
        if key in LEGACY_KEYS:
            _warn(f"{source}: config key '{key}' is now '{LEGACY_KEYS[key]}'")
            key = LEGACY_KEYS[key]
        elif key in OBSOLETE_KEYS:
            _warn(f"{source}: config key '{key}' is obsolete and ignored "
                  "(the layout is now computed automatically)")
            continue
        elif key not in DEFAULTS:
            _warn(f"{source}: unknown config key '{key}' ignored")
            continue
        conf[key] = value
    if conf["orientation"] not in ("horizontal", "vertical"):
        raise ValueError(f"{source}: orientation must be 'horizontal' or 'vertical'")
    return conf


def _warn(msg: str) -> None:
    print(f"  warning: {msg}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# 2. EVENTS
# ─────────────────────────────────────────────────────────────────────────────

_YEAR = r"-?\d+(?:\.\d+)?"
_RANGE_RE = re.compile(rf"^\s*({_YEAR})\s*[-–—]\s*({_YEAR})\s*$")
_SINGLE_RE = re.compile(rf"^\s*({_YEAR})\s*$")


@dataclass
class Event:
    date: str  # as displayed
    t: float  # position on the time axis
    t_range: tuple | None
    label: str
    notes: list  # one entry per merged event; may be empty strings
    side: int | None  # forced side (+1 above/right, -1 below/left) or None
    # measured sizes (inches), filled in later
    label_w: float = 0.0
    label_h: float = 0.0
    date_w: float = 0.0
    date_h: float = 0.0
    a: float = 0.0  # block extent along the axis
    b: float = 0.0  # block extent away from the axis
    u: float = 0.0  # anchor position along the axis (in)
    options: list = field(default_factory=list)


def parse_date(text: str) -> tuple[float, tuple | None, str]:
    """Return (axis position, range or None, display string)."""
    s = str(text)
    m = _RANGE_RE.match(s)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if b < a:
            a, b = b, a
        return (a + b) / 2.0, (a, b), f"{m.group(1)}–{m.group(2)}"
    m = _SINGLE_RE.match(s)
    if m:
        return float(m.group(1)), None, m.group(1)
    raise ValueError(f"cannot read date '{text}': use '1660' or '1490-1499'")


def _clean(text: str) -> str:
    """NFC-normalise and strip the spaces around manual line breaks."""
    text = unicodedata.normalize("NFC", str(text))
    lines = [ln.strip() for ln in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def load_events(records: list, conf: dict, source: str) -> list[Event]:
    events = []
    for k, r in enumerate(records, 1):
        if "date" not in r or "label" not in r:
            raise ValueError(f"{source}: event #{k} needs a 'date' and a 'label'")
        t, rng, shown = parse_date(r["date"])
        side = {"above": 1, "right": 1, "below": -1, "left": -1}.get(
            str(r.get("side", "")).lower())
        label = _clean(r["label"])
        if conf["label_wrap_chars"]:
            label = _wrap_label(label, int(conf["label_wrap_chars"]))
        events.append(Event(shown, t, rng, label, [_clean(r.get("note", ""))], side))
    if not events:
        raise ValueError(f"{source}: no events")
    events.sort(key=lambda e: (e.t_range[0] if e.t_range else e.t, e.t))
    # Events with exactly the same date share one connector and one date.
    merged: list[Event] = []
    for e in events:
        prev = merged[-1] if merged else None
        if prev and prev.t == e.t and prev.t_range == e.t_range:
            prev.label += "\n\n" + e.label
            prev.notes += e.notes
            prev.side = prev.side or e.side
        else:
            merged.append(e)
    return merged


def _wrap_label(label: str, width: int) -> str:
    out = []
    for line in label.split("\n"):
        out.extend(textwrap.wrap(line, width=width, break_long_words=False,
                                 break_on_hyphens=False) or [""])
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# 3. TEXT MEASUREMENT
# ─────────────────────────────────────────────────────────────────────────────


class Measurer:
    """Measures rendered text in inches, using the same dpi as the output."""

    def __init__(self, dpi: float, family: str):
        self.fig = plt.figure(dpi=dpi)
        self.renderer = self.fig.canvas.get_renderer()
        self.dpi = dpi
        self.family = family
        self._cache: dict = {}

    def box(self, text, size, weight="normal", linespacing=1.2):
        """Width and height (inches) of a possibly multi-line text."""
        key = ("box", text, size, weight, linespacing)
        if key not in self._cache:
            t = self.fig.text(0, 0, text, fontsize=size, fontfamily=self.family,
                              fontweight=weight, linespacing=linespacing)
            bb = t.get_window_extent(self.renderer)
            t.remove()
            self._cache[key] = (bb.width / self.dpi, bb.height / self.dpi)
        return self._cache[key]

    def line_width(self, text, size, weight="normal"):
        key = ("w", text, size, weight)
        if key not in self._cache:
            prop = FontProperties(family=self.family, size=size, weight=weight)
            w, _, _ = self.renderer.get_text_width_height_descent(
                text, prop, ismath=cbook.is_math_text(text))
            self._cache[key] = w / self.dpi
        return self._cache[key]

    def wrap(self, text, max_width, size):
        """Greedy word wrap to a maximum width in inches ($...$ kept whole)."""
        out = []
        for para in text.split("\n"):
            words = re.findall(r"\$[^$]*\$\S*|\S+", para)
            line = ""
            for w in words:
                trial = f"{line} {w}" if line else w
                if not line or self.line_width(trial, size) <= max_width * 0.985:
                    line = trial
                else:
                    out.append(line)
                    line = w
            out.append(line)
        return "\n".join(out)

    def close(self):
        plt.close(self.fig)


# ─────────────────────────────────────────────────────────────────────────────
# 4. LAYOUT
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Option:
    lo: float  # start of the block along the axis (in)
    ha: str  # text alignment inside the block
    penalty: float
    d: float = 0.0  # sideways shift of the connector's straight part (in)
    ticks: frozenset = frozenset()  # tick labels its connector would cover


@dataclass
class Layout:
    cost: float
    side: list
    opt: list
    level: list  # distance axis → block inner edge (in)
    extent: dict  # side → max distance reached
    violations: list  # (event whose connector crosses, event crossed)
    hidden_ticks: set  # tick labels left out to make room for a connector


class LayoutEngine:
    def __init__(self, events, conf, horizontal, clearance, u_limits,
                 tick_spans=(), shifted=()):
        self.ev = events
        self.n = len(events)
        self.pad = float(conf["label_padding"])
        self.horizontal = horizontal
        self.clear = clearance  # {+1: in, -1: in}
        self.umin, self.umax = u_limits
        self.allowed = [[e.side] if e.side else [1, -1] for e in events]
        self.tick_spans = list(tick_spans)
        self.any_shift = bool(shifted)
        for i, e in enumerate(events):
            e.options = self._options(e, 0.0)
            if i in shifted:
                for d in SHIFTS:
                    e.options += self._options(e, d)

    # ── candidate placements of one block along the axis ────────────────
    def _options(self, e: Event, d: float) -> list[Option]:
        a, u = e.a, e.u + d
        if d and not (self.umin <= u <= self.umax):
            return []
        if self.horizontal:
            cand = [Option(u - a / 2, "center", 0.0),
                    Option(u - 0.3 * a, "center", 0.03),
                    Option(u - 0.7 * a, "center", 0.03),
                    Option(u - FLAG_INSET, "left", 0.05),
                    Option(u + FLAG_INSET - a, "right", 0.05)]
            if d:  # keep only the placements that lean away from the event
                cand = [cand[0], cand[3] if d > 0 else cand[4]]
        else:  # vertical: "lo" is the top of the block (time runs downward)
            first = e.date_h / 2
            cand = [Option(u - first, "", 0.0),
                    Option(u - a / 2, "", 0.03),
                    Option(u - a + FLAG_INSET * 2, "", 0.05)]
            if d:
                cand = [cand[0] if d > 0 else cand[2], cand[1]]
        ok = [o for o in cand
              if o.lo >= self.umin - 1e-9 and o.lo + a <= self.umax + 1e-9]
        if self.horizontal:  # labels near the figure edges: pin to the edge
            for lo, ha in ((self.umin, "left"), (self.umax - a, "right")):
                if lo - 1e-9 <= u - FLAG_INSET and u + FLAG_INSET <= lo + a + 1e-9:
                    if all(abs(o.lo - lo) > 1e-6 for o in ok):
                        ok.append(Option(lo, ha, 0.06))
        if not ok and not d:  # wider than the chart: centre it, let it overflow
            ok = [Option(u - a / 2, "center", 0.0)]
        for o in ok:
            o.d = d
            o.penalty += (0.15 + 0.6 * abs(d)) if d else 0.0
            # Tick labels sit on side -1. A connector there that would run
            # through one hides (or nudges) it: allowed, if it pays off.
            o.ticks = frozenset(k for k, (t0, t1) in enumerate(self.tick_spans)
                                if t0 - CONNECTOR_CLEAR <= u <= t1 + CONNECTOR_CLEAR)
        return ok

    # ── evaluate one assignment ─────────────────────────────────────────
    def evaluate(self, side, opt) -> Layout:
        ev, pad = self.ev, self.pad
        level = [0.0] * self.n
        extent = {1: 0.0, -1: 0.0}
        cost = 0.0
        for s in (1, -1):
            idx = [i for i in range(self.n) if side[i] == s]
            if not idx:
                continue
            span = {i: (ev[i].options[opt[i]].lo,
                        ev[i].options[opt[i]].lo + ev[i].a) for i in idx}
            anc = {i: ev[i].u + ev[i].options[opt[i]].d for i in idx}
            # p in preds[i]: the connector of p passes under block i,
            # so block i must lie beyond block p.
            preds = {i: [p for p in idx if p != i and
                         span[i][0] - CONNECTOR_CLEAR <= anc[p] <= span[i][1] + CONNECTOR_CLEAR]
                     for i in idx}
            near = {i: [k for k in idx if k != i and
                        span[i][0] - pad < span[k][1] and span[k][0] - pad < span[i][1]]
                    for i in idx}
            placed: dict = {}
            remaining = set(idx)
            while remaining:
                avail = [i for i in remaining if all(p in placed for p in preds[i])]
                if not avail:  # cyclic constraints: unavoidable crossing
                    avail = [min(remaining, key=lambda i: (
                        sum(p not in placed for p in preds[i]), anc[i]))]
                best = None
                for i in avail:
                    y = self._lowest(i, s, placed, preds[i], near[i])
                    if best is None or y < best[0] - 1e-9:
                        best = (y, i)
                y, i = best
                placed[i] = y
                level[i] = y
                remaining.discard(i)
            extent[s] = max(level[i] + ev[i].b for i in idx)
            cost += sum(level[i] for i in idx) * 0.01
            if self.any_shift:  # bent connectors must not cross each other
                for i in idx:
                    for j in idx:
                        if ev[i].u < ev[j].u and anc[i] > anc[j] - 0.02:
                            cost += 0.5
        violations = self._violations(side, opt, level)
        cost += extent[1] + extent[-1]
        cost += sum(ev[i].options[opt[i]].penalty for i in range(self.n))
        cost += 100.0 * len(violations)
        hidden = set()
        for i in range(self.n):
            if side[i] == -1:
                hidden |= ev[i].options[opt[i]].ticks
        cost += 1.0 * len(hidden)
        return Layout(cost, list(side), list(opt), level, extent, violations, hidden)

    def _lowest(self, i, s, placed, preds, near):
        """Lowest admissible level for block i given the blocks already placed."""
        ev, pad = self.ev, self.pad
        base = self.clear[s]
        for p in preds:
            if p in placed:
                base = max(base, placed[p] + ev[p].b + pad)
        blocked = sorted((placed[k] - pad - ev[i].b, placed[k] + ev[k].b + pad)
                         for k in near if k in placed)
        y = base
        for lo, hi in blocked:
            if y <= lo + 1e-9:
                break
            if y < hi:
                y = hi
        return y

    def _violations(self, side, opt, level):
        out = []
        ev = self.ev
        for i in range(self.n):
            lo = ev[i].options[opt[i]].lo
            hi = lo + ev[i].a
            for p in range(self.n):
                up = ev[p].u + ev[p].options[opt[p]].d
                if p != i and side[p] == side[i] and level[p] > level[i] \
                        and lo - CONNECTOR_CLEAR <= up <= hi + CONNECTOR_CLEAR:
                    out.append((p, i))
        return out

    # ── search ──────────────────────────────────────────────────────────
    def solve(self, restarts: int = 8, seed: int = 2026) -> Layout:
        start_side, toggle = [], 1
        for i in range(self.n):
            s = toggle if toggle in self.allowed[i] else self.allowed[i][0]
            start_side.append(s)
            toggle = -s
        best = self._descend(start_side, [0] * self.n)
        rng = random.Random(seed)
        for _ in range(restarts):
            if best.cost < 50 and _ >= restarts // 2:
                break
            side = [rng.choice(a) for a in self.allowed]
            opt = [rng.randrange(len(e.options)) for e in self.ev]
            res = self._descend(side, opt)
            if res.cost < best.cost - 1e-9:
                best = res
        return best

    def _descend(self, side, opt) -> Layout:
        cur = self.evaluate(side, opt)
        improved = True
        while improved:
            improved = False
            for i in range(self.n):
                for s in self.allowed[i]:
                    for k in range(len(self.ev[i].options)):
                        if s == cur.side[i] and k == cur.opt[i]:
                            continue
                        side = list(cur.side)
                        opt = list(cur.opt)
                        side[i], opt[i] = s, k
                        trial = self.evaluate(side, opt)
                        if trial.cost < cur.cost - 1e-9:
                            cur, improved = trial, True
        return cur


# ─────────────────────────────────────────────────────────────────────────────
# 5. AXIS TICKS
# ─────────────────────────────────────────────────────────────────────────────


def axis_ticks(tmin, tmax, length_in, step="auto"):
    if step in (None, "auto"):
        max_ticks = max(3, int(length_in / 1.25))
        for step in (1, 2, 5, 10, 20, 50, 100, 200, 500,
                     1000, 2000, 5000, 10000, 20000, 50000, 100000):
            if (math.floor(tmax / step) + 1 - math.floor(tmin / step)) <= max_ticks:
                break
    step = float(step)
    t0 = math.floor(tmin / step) * step
    t1 = (math.floor(tmax / step) + 1) * step
    n = int(round((t1 - t0) / step))
    return [t0 + k * step for k in range(n + 1)]


def _fmt_tick(t):
    return str(int(round(t))) if abs(t - round(t)) < 1e-9 else f"{t:g}"


# ─────────────────────────────────────────────────────────────────────────────
# 6. DRAWING
# ─────────────────────────────────────────────────────────────────────────────


def render(json_path: Path, output: Path | None = None, show=False) -> Path:
    with open(json_path, encoding="utf-8") as f:
        raw = json.load(f)
    src = json_path.name
    conf = load_config(raw.get("config", {}), src)
    events = load_events(raw.get("events", []), conf, src)

    horizontal = conf["orientation"] == "horizontal"
    fam = conf["font_family"]
    m = Measurer(conf["dpi"], fam)
    try:
        fig_path = _render(json_path, conf, events, horizontal, m, output, show)
    finally:
        m.close()
    return fig_path


def _narrow_labels(events, which) -> bool:
    """Re-wrap the given labels to ~70% of their width; False if no change."""
    changed = False
    for i in which:
        e = events[i]
        longest = max(len(x) for x in e.label.split("\n"))
        if longest > 14:
            e.label = "\n\n".join(
                _wrap_label(" ".join(p.split()), max(12, int(longest * 0.7)))
                for p in e.label.split("\n\n"))
            changed = True
    return changed


def _measure_events(events, conf, m, horizontal):
    gap = float(conf["date_gap"])
    for e in events:
        e.label_w, e.label_h = m.box(e.label, conf["font_events"],
                                     linespacing=conf["event_linespacing"])
        e.date_w, e.date_h = m.box(e.date, conf["font_dates"], weight="bold")
        if horizontal:
            e.a = max(e.label_w, e.date_w)
            e.b = e.label_h + gap + e.date_h
        else:
            e.a = e.date_h + gap + e.label_h
            e.b = max(e.label_w, e.date_w)


def _render(json_path, conf, events, horizontal, m, output, show):
    fam = conf["font_family"]
    pad = float(conf["label_padding"])
    width = float(conf["chart_width"])

    # ── time axis ───────────────────────────────────────────────────────
    tmin = min(e.t_range[0] if e.t_range else e.t for e in events)
    tmax = max(e.t_range[1] if e.t_range else e.t for e in events)
    length = width - 2 * MARGIN_X if horizontal else float(conf["chart_height"])
    ticks = axis_ticks(tmin, tmax, length, conf["tick_step"])
    t0, t1 = ticks[0], ticks[-1]

    def u_of(t):
        return (t - t0) / (t1 - t0) * length

    tick_w = max(m.box(_fmt_tick(t), conf["font_ticks"])[0] for t in ticks)
    tick_h = max(m.box(_fmt_tick(t), conf["font_ticks"])[1] for t in ticks)
    off = float(conf["tick_label_offset"])
    clear = float(conf["axis_clearance"])
    if horizontal:
        clearance = {1: clear, -1: max(clear, off + tick_h + pad)}
        limits = (-(MARGIN_X - EDGE_PAD), length + MARGIN_X - EDGE_PAD)
        tick_spans = [(u_of(t) - m.box(_fmt_tick(t), conf["font_ticks"])[0] / 2,
                       u_of(t) + m.box(_fmt_tick(t), conf["font_ticks"])[0] / 2)
                      for t in ticks]
    else:
        clearance = {1: clear, -1: max(clear, off + tick_w + pad)}
        limits = (-math.inf, math.inf)
        tick_spans = [(u_of(t) - tick_h / 2, u_of(t) + tick_h / 2) for t in ticks]
    knee = {1: min(0.15, 0.5 * clearance[1]), -1: min(0.8 * off, 0.5 * clearance[-1])}

    # ── label placement (with re-wrapping fallback) ─────────────────────
    _measure_events(events, conf, m, horizontal)
    for e in events:
        e.u = u_of(e.t)
    # 1) straight connectors; 2) allow bent connectors near the conflicts;
    # 3) (horizontal) re-wrap the labels involved to narrower lines.
    shifted: set = set()
    max_attempts = 6
    for attempt in range(max_attempts):
        engine = LayoutEngine(events, conf, horizontal, clearance, limits,
                              tick_spans, shifted)
        lay = engine.solve()
        if not lay.violations or attempt == max_attempts - 1:
            break
        culprits = {i for pair in lay.violations for i in pair}
        around = {j for j, e in enumerate(events) for i in culprits
                  if abs(e.u - events[i].u) < 1.5}
        if not around <= shifted:
            shifted |= around
            continue
        if not horizontal or not _narrow_labels(events, culprits):
            break
        _measure_events(events, conf, m, horizontal)

    # ── notes ───────────────────────────────────────────────────────────
    notes = [(e.date, n) for e in events for n in e.notes if n] \
        if conf["show_notes"] else []
    fs_n = conf["font_notes"]
    # ── figure geometry ─────────────────────────────────────────────────
    title = _clean(conf["title"])
    title_h = m.box(title, conf["font_title"], weight="bold")[1] if title else 0.0

    if horizontal:
        fig_w = width
        above = max(lay.extent[1], clearance[1])
        below = max(lay.extent[-1], off + tick_h)
        chart_h = above + below
        axis_x0 = MARGIN_X
    else:
        left = max(lay.extent[-1], off + tick_w)
        right = max(lay.extent[1], clearance[1])
        fig_w = max(width, left + right + 2 * MARGIN_X)
        axis_x = MARGIN_X + left + (fig_w - (left + right + 2 * MARGIN_X)) / 2
        los = [e.options[lay.opt[i]].lo for i, e in enumerate(events)]
        his = [lo + e.a for lo, e in zip(los, events)]
        ext_top = max(0.0, -min(los)) + 0.1
        ext_bot = max(0.0, max(his) - length) + 0.1
        chart_h = ext_top + length + ext_bot

    notes_w = fig_w - 2 * MARGIN_X
    note_rows = []
    if notes:
        prefixes = [f"[{d}]" for d, _ in notes]
        pre_w = max(m.box(p, fs_n, weight="bold")[0] for p in prefixes) + 0.15
        body_w = notes_w - pre_w
        line_h = fs_n / 72.0 * float(conf["note_linespacing"])  # one note line
        for p, (_, note) in zip(prefixes, notes):
            body = m.wrap(note, body_w, fs_n)
            h = m.box(body, fs_n, linespacing=conf["note_linespacing"])[1]
            note_rows.append((p, body, h))
        spacing = float(conf["note_spacing"]) * line_h
        notes_h = sum(r[2] for r in note_rows) + spacing * (len(note_rows) - 1)
    else:
        notes_h = 0.0
    notes_gap = float(conf["notes_gap"]) if note_rows else 0.0
    fig_h = (TOP_PAD + title_h + (TITLE_GAP if title else 0.0) + chart_h
             + notes_gap + notes_h + BOTTOM_PAD)
    chart_top = fig_h - TOP_PAD - title_h - (TITLE_GAP if title else 0.0)

    # ── figure in inch coordinates ──────────────────────────────────────
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=conf["dpi"],
                     facecolor=conf["background_color"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.set_axis_off()
    txt = dict(fontfamily=fam, color=conf["text_color"], clip_on=False)
    report = {"texts": [], "blocks": [], "connectors": []}

    if title:
        t = ax.text(MARGIN_X, fig_h - TOP_PAD, title, ha="left", va="top",
                    fontsize=conf["font_title"], fontweight="bold", **txt)
        report["texts"].append((t, None))

    if horizontal:
        axis_y = chart_top - above

        def P(u, d):  # (along, across) → figure inches
            return axis_x0 + u, axis_y + d
    else:
        axis_top = chart_top - ext_top

        def P(u, d):
            return axis_x + d, axis_top - u

    # axis line, ranges, ticks, event markers
    (x0, y0), (x1, y1) = P(0, 0), P(length, 0)
    ax.plot([x0, x1], [y0, y1], color=conf["axis_color"], lw=2.5,
            solid_capstyle="butt", zorder=1)
    for e in events:
        if e.t_range:
            (xa, ya), (xb, yb) = P(u_of(e.t_range[0]), 0), P(u_of(e.t_range[1]), 0)
            ax.plot([xa, xb], [ya, yb], color=conf["range_color"], lw=7,
                    solid_capstyle="round", zorder=2)
    shifts = _shift_tick_labels(lay, events, tick_spans, limits)
    for k, t in enumerate(ticks):
        x, y = P(u_of(t), 0)
        ax.scatter([x], [y], s=50, facecolor=conf["tick_color"],
                   edgecolor="black", linewidths=0.8, zorder=3)
        if k in lay.hidden_ticks:
            if shifts.get(k) is None:
                continue
            x, y = P(u_of(t) + shifts[k], 0)
        if horizontal:
            lab = ax.text(x, y - off, _fmt_tick(t), ha="center", va="top",
                          fontsize=conf["font_ticks"], **txt)
        else:
            lab = ax.text(x - off, y, _fmt_tick(t), ha="right", va="center",
                          fontsize=conf["font_ticks"], **txt)
        report["texts"].append((lab, None))

    # event blocks
    gap = float(conf["date_gap"])
    ev_kw = dict(fontsize=conf["font_events"], linespacing=conf["event_linespacing"],
                 zorder=5, **txt)
    dt_kw = dict(fontsize=conf["font_dates"], fontweight="bold", zorder=5, **txt)
    for i, e in enumerate(events):
        s, o, lvl = lay.side[i], e.options[lay.opt[i]], lay.level[i]
        if horizontal:
            xa = {"center": o.lo + e.a / 2, "left": o.lo, "right": o.lo + e.a}[o.ha]
            x, _ = P(xa, 0)
            va = "bottom" if s > 0 else "top"
            lt = ax.text(x, axis_y + s * lvl, e.label, ha=o.ha, va=va,
                         multialignment=o.ha, **ev_kw)
            dt = ax.text(x, axis_y + s * (lvl + e.label_h + gap), e.date,
                         ha=o.ha, va=va, **dt_kw)
        else:
            ha = "left" if s > 0 else "right"
            x, ytop = P(o.lo, s * lvl)
            dt = ax.text(x, ytop, e.date, ha=ha, va="top", **dt_kw)
            lt = ax.text(x, ytop - e.date_h - gap, e.label, ha=ha, va="top",
                         multialignment=ha, **ev_kw)
        report["texts"] += [(lt, i), (dt, i)]
        report["blocks"].append((i, lt, dt))
        pts = [P(e.u, 0)]
        if o.d:  # bend inside the empty band next to the axis
            pts.append(P(e.u + o.d, s * knee[s]))
        pts.append(P(e.u + o.d, s * (lvl - CONNECTOR_GAP)))
        report["connectors"].append((i, pts))

    # connectors (faded where they cross text: only if unavoidable)
    fig.canvas.draw()
    rend = fig.canvas.get_renderer()
    inv = ax.transData.inverted()
    block_boxes = {i: [a.get_window_extent(rend).transformed(inv) for a in (lt, dt)]
                   for i, lt, dt in report["blocks"]}
    for i, pts in report["connectors"]:
        others = [bb for k, bbs in block_boxes.items() if k != i for bb in bbs]
        if len(pts) == 3:
            ax.plot([pts[0][0], pts[1][0]], [pts[0][1], pts[1][1]],
                    color=conf["connector_color"], lw=1.2, zorder=2,
                    solid_capstyle="round")
        _draw_connector(ax, pts[-2], pts[-1], others, conf["connector_color"],
                        horizontal)
        xs, ys = pts[0]
        ax.scatter([xs], [ys], s=28, facecolor=conf["marker_color"],
                   edgecolor="black", linewidths=0.8, zorder=4)

    # notes
    if note_rows:
        y = BOTTOM_PAD + notes_h
        pre_x = MARGIN_X
        body_x = MARGIN_X + pre_w
        for k, (p, body, h) in enumerate(note_rows):
            a1 = ax.text(pre_x, y, p, ha="left", va="top", fontsize=fs_n,
                         fontweight="bold", **txt)
            a2 = ax.text(body_x, y, body, ha="left", va="top", fontsize=fs_n,
                         linespacing=conf["note_linespacing"], **txt)
            report["texts"] += [(a1, None), (a2, None)]
            y -= h + spacing

    if conf["border_color"]:
        bc = conf["border_color"]
        ax.add_patch(Rectangle((EDGE_PAD, chart_top - chart_h - 0.1),
                               fig_w - 2 * EDGE_PAD, chart_h + 0.2,
                               fill=False, ec=bc, lw=1.2))
        if note_rows:
            ax.add_patch(Rectangle((EDGE_PAD, BOTTOM_PAD - 0.12),
                                   fig_w - 2 * EDGE_PAD, notes_h + 0.24,
                                   fill=False, ec=bc, lw=1.2))

    # ── check and save ──────────────────────────────────────────────────
    problems = check_layout(fig, ax, report)
    if output is None:
        out = conf["output_file"] or json_path.with_suffix(".png").name
        output = Path(out)
        if not output.is_absolute():
            output = json_path.parent / output
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=conf["dpi"], facecolor=conf["background_color"])
    status = "OK" if not problems else f"{len(problems)} issue(s)"
    print(f"{json_path} → {output}   [{fig_w:.1f} × {fig_h:.1f} in, "
          f"layout check: {status}]")
    for p in problems:
        _warn(p)
    if problems:
        _warn("a clean layout was not possible: try shorter labels, a wider "
              "chart, or 'side' on some events (crossings are drawn faded)")
    if show:
        plt.show()
    plt.close(fig)
    return output


def _shift_tick_labels(lay, events, spans, limits):
    """Move a tick label sideways when a connector would run through it.

    Returns {tick index: shift along the axis (in), or None to hide it}.
    """
    conn = [e.u + e.options[lay.opt[i]].d
            for i, e in enumerate(events) if lay.side[i] == -1]
    out = {}
    for k in sorted(lay.hidden_ticks):
        lo, hi = spans[k]
        w = hi - lo
        cands = []
        for c in conn:
            cands.append(c + CONNECTOR_CLEAR - lo)  # label right of connector
            cands.append(c - CONNECTOR_CLEAR - hi)  # label left of connector
        best = None
        for d in sorted(cands, key=abs):
            a, b = lo + d, hi + d
            if abs(d) > 1.2 * w or a < limits[0] or b > limits[1]:
                continue
            if any(a - CONNECTOR_CLEAR + 1e-6 < c < b + CONNECTOR_CLEAR - 1e-6
                   for c in conn):
                continue
            if any(j != k and a - 0.08 < sp[1] and sp[0] < b + 0.08
                   for j, sp in enumerate(spans)):
                continue
            best = d
            break
        out[k] = best
    return out


def _draw_connector(ax, start, end, boxes, color, horizontal):
    """Straight connector; any stretch running behind a text box is faded."""
    (xs, ys), (xe, ye) = start, end
    if horizontal:
        lo, hi, c = min(ys, ye), max(ys, ye), xs
        cov = [(bb.y0, bb.y1) for bb in boxes if bb.x0 <= c <= bb.x1]
    else:
        lo, hi, c = min(xs, xe), max(xs, xe), ys
        cov = [(bb.x0, bb.x1) for bb in boxes if bb.y0 <= c <= bb.y1]
    cov = sorted((max(lo, a), min(hi, b)) for a, b in cov if a < hi and b > lo)
    merged = []
    for a, b in cov:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    pieces, cur = [], lo
    for a, b in merged:
        if cur < a:
            pieces.append((cur, a, 1.0))
        pieces.append((a, b, 0.18))
        cur = b
    if cur < hi:
        pieces.append((cur, hi, 1.0))
    for a, b, alpha in pieces:
        if horizontal:
            ax.plot([c, c], [a, b], color=color, lw=1.2, alpha=alpha, zorder=2)
        else:
            ax.plot([a, b], [c, c], color=color, lw=1.2, alpha=alpha, zorder=2)


def _segment_hits_box(p, q, bb, tol):
    """True if segment p→q enters the (slightly shrunk) box bb."""
    x0, y0, x1, y1 = bb.x0 + tol, bb.y0 + tol, bb.x1 - tol, bb.y1 - tol
    t0, t1 = 0.0, 1.0
    dx, dy = q[0] - p[0], q[1] - p[1]
    for num, den in ((p[0] - x0, -dx), (x1 - p[0], dx),
                     (p[1] - y0, -dy), (y1 - p[1], dy)):
        if abs(den) < 1e-12:
            if num < 0:
                return False
        else:
            t = num / den
            if den < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
    return t0 < t1


def check_layout(fig, ax, report) -> list[str]:
    """Verify on the final drawing: no text overlaps, no connector crosses text."""
    fig.canvas.draw()
    rend = fig.canvas.get_renderer()
    inv = ax.transData.inverted()
    items = [(a.get_text().split("\n")[0][:40], owner,
              a.get_window_extent(rend).transformed(inv))
             for a, owner in report["texts"] if a.get_text().strip()]
    tol = 0.005  # inches
    problems = []
    for k in range(len(items)):
        for j in range(k + 1, len(items)):
            (na, _, a), (nb, _, b) = items[k], items[j]
            if (a.x0 + tol < b.x1 and b.x0 + tol < a.x1
                    and a.y0 + tol < b.y1 and b.y0 + tol < a.y1):
                problems.append(f"text overlap: '{na}' / '{nb}'")
    for i, pts in report["connectors"]:
        for name, owner, bb in items:
            if owner == i:
                continue
            if any(_segment_hits_box(p, q, bb, tol) for p, q in zip(pts, pts[1:])):
                problems.append(f"connector crosses text: '{name}'")
    return problems


# ─────────────────────────────────────────────────────────────────────────────
# 7. COMMAND LINE
# ─────────────────────────────────────────────────────────────────────────────


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Draw annotated timeline charts from JSON files.")
    ap.add_argument("inputs", nargs="+", type=Path, help="JSON data file(s)")
    ap.add_argument("-o", "--output", type=Path,
                    help="output file (single input only); the extension sets "
                         "the format: .png, .pdf, .svg, ...")
    ap.add_argument("--show", action="store_true", help="also open a window")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)
    if args.output and len(args.inputs) > 1:
        ap.error("--output can only be used with a single input file")
    failed = 0
    for path in args.inputs:
        try:
            render(path, args.output, args.show)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"{path}: error: {exc}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
