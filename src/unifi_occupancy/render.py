"""Rendering the sheets.

Pure functions from view models to a string of HTML. No scripts, no fonts, no
CDN: a sheet is a single self-contained file that any web server can hand over
and that will still open in ten years.

The view models here are deliberately dumb -- fractions of a day and bucket
counts, already worked out -- so that :mod:`unifi_occupancy.report` owns the
arithmetic and this module owns only the markup.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from html import escape

from .config import ReportConfig
from .models import MOTION, PERSON, kind_for

#: Detection kinds that always get a track, even on a day with nothing in them.
ALWAYS_RENDERED: tuple[str, ...] = (MOTION, PERSON)

#: Enough shades to tile the day; period bands cycle through them in time order.
BAND_CLASSES = 6


@dataclass(frozen=True)
class BandSpan:
    """One period, as a fraction of the day it is drawn on.

    Measured by the caller rather than worked out from the clock here, because
    a day is not always 1440 minutes long and the shading has to line up with
    the detections drawn over it.
    """

    name: str
    start: float
    end: float
    shade: int


@dataclass(frozen=True)
class AxisMark:
    """An hour label, at the fraction of the day where that hour really fell."""

    label: str
    at: float


@dataclass(frozen=True)
class Segment:
    """One detection, as a fraction of the day it is drawn on."""

    start: float
    end: float
    title: str


@dataclass(frozen=True)
class Track:
    """One detection kind for one row: when it fired, and how densely."""

    kind: str
    segments: tuple[Segment, ...] = ()
    #: Event starts per density bucket, left to right across the day.
    buckets: tuple[int, ...] = ()
    events: int = 0
    seconds: float = 0.0

    @property
    def name(self) -> str:
        return kind_for(self.kind).name


@dataclass(frozen=True)
class Row:
    """A labelled stack of tracks -- one camera, or a whole group added up."""

    label: str
    tracks: tuple[Track, ...]
    #: True for the group total, which is drawn more prominently.
    aggregate: bool = False
    note: str = ""


@dataclass(frozen=True)
class Section:
    """A group, or a single camera that belongs to no group."""

    key: str
    name: str
    rows: tuple[Row, ...]
    totals: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class DaySheet:
    """Everything one day's page needs."""

    day: date
    sections: tuple[Section, ...]
    generated_at: datetime
    #: Busiest bucket anywhere on the page; density is shaded relative to it so
    #: that rows can be compared with each other rather than only with
    #: themselves.
    peak_bucket: int = 0
    previous_day: date | None = None
    next_day: date | None = None
    bands: tuple[BandSpan, ...] = ()
    axis: tuple[AxisMark, ...] = ()


@dataclass(frozen=True)
class IndexCell:
    group_key: str
    group_name: str
    #: Detection kind -> number of events that day.
    counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class IndexEntry:
    day: date
    href: str
    cells: Mapping[str, IndexCell] = field(default_factory=dict)

    def total(self, kind: str) -> int:
        return sum(cell.counts.get(kind, 0) for cell in self.cells.values())


#: The dark palette, spliced into both the "browser says dark" rule and the
#: "reader chose dark" rule, so the two can never drift apart.
DARK_VARS = """
  --bg: #15161a; --fg: #e9e7e4; --muted: #99948e; --line: #2b2d33;
  --card: #1c1e23; --shadow: rgba(0, 0, 0, .35);
  --motion: #e79a41; --person: #6ca8e8;
  --b0: rgba(255, 255, 255, .105); --b1: rgba(255, 255, 255, .050);
  --b2: rgba(255, 255, 255, .011); --b3: rgba(255, 255, 255, .078);
  --b4: rgba(255, 255, 255, .030); --b5: rgba(255, 255, 255, .062);
"""

#: Read before the first paint, so a chosen theme never flashes the other one.
#: Every storage access is guarded: a private window or blocked site data makes
#: these throw, and a sheet that will not render is worse than one that forgets.
HEAD_SCRIPT = """
(function () {
  try {
    var saved = localStorage.getItem("unifi-occupancy-theme");
    if (saved === "light" || saved === "dark") {
      document.documentElement.setAttribute("data-theme", saved);
    }
  } catch (e) {}
})();
"""

#: Wires up the control and reveals it. Without this the control stays hidden,
#: because a toggle that cannot toggle is worse than no toggle.
BODY_SCRIPT = """
(function () {
  var KEY = "unifi-occupancy-theme";
  var root = document.documentElement;
  var bar = document.getElementById("theme");
  if (!bar) return;
  bar.hidden = false;
  function paint() {
    var chosen = root.getAttribute("data-theme") || "auto";
    var buttons = bar.getElementsByTagName("button");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].setAttribute(
        "aria-pressed", buttons[i].getAttribute("data-theme") === chosen ? "true" : "false"
      );
    }
  }
  bar.addEventListener("click", function (event) {
    var button = event.target;
    while (button && button !== bar && !button.getAttribute("data-theme")) {
      button = button.parentNode;
    }
    if (!button || button === bar) return;
    var choice = button.getAttribute("data-theme");
    try {
      if (choice === "auto") { localStorage.removeItem(KEY); }
      else { localStorage.setItem(KEY, choice); }
    } catch (e) {}
    if (choice === "auto") { root.removeAttribute("data-theme"); }
    else { root.setAttribute("data-theme", choice); }
    paint();
  });
  paint();
})();
"""

STYLE = """
:root {
  color-scheme: light dark;
  --bg: #fbfaf8; --fg: #1c1b19; --muted: #6d6964; --line: #e4dfd8;
  --card: #ffffff; --shadow: rgba(28, 27, 25, .06);
  --motion: #c0711c; --person: #2566a4;
  --b0: rgba(28, 27, 25, .115); --b1: rgba(28, 27, 25, .055);
  --b2: rgba(28, 27, 25, .014); --b3: rgba(28, 27, 25, .085);
  --b4: rgba(28, 27, 25, .034); --b5: rgba(28, 27, 25, .070);
}
/* What the browser asks for, unless the reader has said otherwise. */
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {/*DARK*/}
}
/* What the reader chose, whatever the browser asks for. */
:root[data-theme="dark"] {/*DARK*/}
:root[data-theme="dark"] { color-scheme: dark; }
:root[data-theme="light"] { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 1.5rem 1.25rem 4rem; background: var(--bg); color: var(--fg);
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  -webkit-text-size-adjust: 100%;
}
.wrap { max-width: 1180px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .2rem; letter-spacing: -.01em; }
h2 { font-size: 1.0rem; margin: 0; letter-spacing: -.005em; }
a { color: inherit; }
.sub { color: var(--muted); font-size: .85rem; margin: 0 0 1.25rem; }
.sub a { text-decoration-color: var(--line); }
nav.days { display: flex; gap: .75rem; align-items: center; flex-wrap: wrap;
  margin: 0 0 1.5rem; font-size: .85rem; }
/* The dead-end "earlier"/"later" labels, not every span that lands in here:
   the theme control is one too, and must not be dressed as a disabled link. */
nav.days a, nav.days span:not(.theme) { padding: .25rem .6rem;
  border: 1px solid var(--line); border-radius: 999px; text-decoration: none; }
nav.days span:not(.theme) { color: var(--muted); border-style: dashed; }

.cards { display: flex; flex-wrap: wrap; gap: .6rem; margin: 0 0 1.75rem; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: .6rem .8rem; min-width: 8.5rem; box-shadow: 0 1px 2px var(--shadow); }
.card b { display: block; font-size: .78rem; font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: .04em; margin-bottom: .25rem; }
.card .n { font-variant-numeric: tabular-nums; font-size: .9rem; }
.card .n span { color: var(--muted); }

section.group { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 1rem 1.1rem 1.15rem; margin: 0 0 1rem; box-shadow: 0 1px 2px var(--shadow); }
.ghead { display: flex; justify-content: space-between; align-items: baseline;
  gap: 1rem; flex-wrap: wrap; margin-bottom: .9rem; }
.ghead .t { color: var(--muted); font-size: .82rem; font-variant-numeric: tabular-nums; }

.row { display: grid; grid-template-columns: 11rem 1fr; gap: .9rem;
  padding: .45rem 0; border-top: 1px solid var(--line); }
.row:first-of-type { border-top: 0; }
.row.agg .rl b { font-weight: 650; }
.rl { min-width: 0; }
.rl b { display: block; font-weight: 500; font-size: .88rem; overflow-wrap: break-word; }
.rl i { display: block; font-style: normal; color: var(--muted); font-size: .74rem;
  font-variant-numeric: tabular-nums; }

.tracks { min-width: 0; }
.track { position: relative; margin-bottom: .45rem; }
.track:last-child { margin-bottom: 0; }
.tname { font-size: .7rem; color: var(--muted); margin-bottom: .15rem; }
.tname b { font-weight: 600; text-transform: uppercase; letter-spacing: .05em;
  font-size: .66rem; color: var(--fg); opacity: .72; }
.tname span { font-variant-numeric: tabular-nums; }
.lane { position: relative; height: 13px; border-radius: 3px; overflow: hidden;
  background: var(--b2); }
.lane.den { height: 9px; margin-top: 2px; }
.band { position: absolute; top: 0; bottom: 0; }
.band.i0 { background: var(--b0); } .band.i1 { background: var(--b1); }
.band.i2 { background: var(--b2); } .band.i3 { background: var(--b3); }
.band.i4 { background: var(--b4); } .band.i5 { background: var(--b5); }
.seg, .bk { position: absolute; top: 0; bottom: 0; }
.seg { min-width: 2px; border-radius: 2px; }
.motion .seg, .motion .bk { background: var(--motion); }
.person .seg, .person .bk { background: var(--person); }
.siren .seg, .siren .bk, .smoke .seg, .smoke .bk { background: #c0392b; }
.quiet { position: absolute; inset: 0; display: flex; align-items: center;
  justify-content: center; font-size: .68rem; color: var(--muted); }

.axis { position: relative; height: 1.1rem; margin-top: .3rem; font-size: .68rem;
  color: var(--muted); font-variant-numeric: tabular-nums; }
.axis span { position: absolute; transform: translateX(-50%); white-space: nowrap; }
.axis span:first-child { transform: none; }
.axis span:last-child { transform: translateX(-100%); }
.rowaxis { display: grid; grid-template-columns: 11rem 1fr; gap: .9rem; }

.legend { display: flex; flex-wrap: wrap; gap: 1.1rem; align-items: center;
  margin: 1.5rem 0 0; font-size: .78rem; color: var(--muted); }
.legend .k { display: inline-flex; align-items: center; gap: .35rem; }
.legend .sw { width: 1.6rem; height: .55rem; border-radius: 2px; display: inline-block;
  flex: none; }
.legend .motion .sw { background: var(--motion); }
.legend .person .sw { background: var(--person); }
.legend .per { display: inline-flex; align-items: center; gap: .35rem; }
.legend .per .sw { border: 1px solid var(--line); }
.per .sw.i0 { background: var(--b0); } .per .sw.i1 { background: var(--b1); }
.per .sw.i2 { background: var(--b2); } .per .sw.i3 { background: var(--b3); }
.per .sw.i4 { background: var(--b4); } .per .sw.i5 { background: var(--b5); }

table.index { width: 100%; border-collapse: collapse; font-size: .88rem; }
table.index th, table.index td { text-align: right; padding: .5rem .55rem;
  border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums;
  white-space: nowrap; }
table.index th:first-child, table.index td:first-child { text-align: left; }
table.index thead th { font-size: .72rem; text-transform: uppercase;
  letter-spacing: .04em; color: var(--muted); font-weight: 600; }
table.index tbody tr:hover { background: var(--b2); }
table.index td.tot { font-weight: 600; }
table.index .z { color: var(--muted); }
.scroll { overflow-x: auto; background: var(--card); border: 1px solid var(--line);
  border-radius: 12px; padding: .25rem .75rem; box-shadow: 0 1px 2px var(--shadow); }
.empty { color: var(--muted); font-size: .9rem; padding: 2rem 0; }

.theme { margin-left: auto; display: inline-flex; border: 1px solid var(--line);
  border-radius: 999px; overflow: hidden; padding: 0; }
.theme[hidden] { display: none; }
.theme button { font: inherit; font-size: .78rem; line-height: 1.5; padding: .25rem .65rem;
  border: 0; background: transparent; color: var(--muted); cursor: pointer; }
.theme button + button { border-left: 1px solid var(--line); }
.theme button[aria-pressed="true"] { background: var(--b1); color: var(--fg); }
.theme button:focus-visible { outline: 2px solid var(--person); outline-offset: -2px; }

@media (max-width: 700px) {
  body { padding: 1.1rem .8rem 3rem; }
  .theme { margin-left: 0; }
  .row, .rowaxis { grid-template-columns: 6.5rem 1fr; gap: .55rem; }
  .rl b { font-size: .8rem; }
}
""".replace("/*DARK*/", DARK_VARS)


def _duration(seconds: float) -> str:
    """Human-readable duration: ``2h 05m``, ``7m 30s``, ``12s``."""
    total = round(seconds)
    if total <= 0:
        return "0s"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _pct(value: float) -> str:
    """A percentage with enough precision for a one-second event on a day."""
    return f"{max(0.0, min(100.0, value * 100)):.4f}".rstrip("0").rstrip(".") or "0"


def _bands_html(bands: Sequence[BandSpan]) -> str:
    return "".join(
        f'<i class="band i{band.shade % BAND_CLASSES}" '
        f'style="left:{_pct(band.start)}%;width:{_pct(max(band.end - band.start, 0.0))}%"></i>'
        for band in bands
    )


def _track_html(track: Track, bands_html: str, peak: int) -> str:
    segments = "".join(
        f'<i class="seg" style="left:{_pct(segment.start)}%;'
        f'width:{_pct(max(segment.end - segment.start, 0.0))}%" '
        f'title="{escape(segment.title, quote=True)}"></i>'
        for segment in track.segments
    )
    buckets = ""
    if track.buckets and peak > 0:
        count = len(track.buckets)
        width = 1.0 / count
        buckets = "".join(
            f'<i class="bk" style="left:{_pct(index * width)}%;width:{_pct(width)}%;'
            f'opacity:{min(1.0, 0.18 + 0.82 * (value / peak)):.3f}"></i>'
            for index, value in enumerate(track.buckets)
            if value > 0
        )
    quiet = "" if track.events else '<span class="quiet">no events</span>'
    summary = (
        f"{_plural(track.events, 'event')} · {_duration(track.seconds)}"
        if track.events
        else "quiet"
    )
    return (
        f'<div class="track {escape(track.kind)}">'
        f'<div class="tname"><b>{escape(track.name)}</b> <span>{escape(summary)}</span></div>'
        f'<div class="lane">{bands_html}{segments}{quiet}</div>'
        f'<div class="lane den">{bands_html}{buckets}</div>'
        f"</div>"
    )


def _row_html(row: Row, bands_html: str, peak: int) -> str:
    tracks = "".join(_track_html(track, bands_html, peak) for track in row.tracks)
    note = f"<i>{escape(row.note)}</i>" if row.note else ""
    return (
        f'<div class="row{" agg" if row.aggregate else ""}">'
        f'<div class="rl"><b>{escape(row.label)}</b>{note}</div>'
        f'<div class="tracks">{tracks}</div>'
        f"</div>"
    )


def _axis_html(marks: Sequence[AxisMark]) -> str:
    spans = "".join(
        f'<span style="left:{_pct(mark.at)}%">{escape(mark.label)}</span>' for mark in marks
    )
    return f'<div class="rowaxis"><div></div><div class="axis">{spans}</div></div>'


def _section_html(section: Section, bands_html: str, peak: int, axis: str) -> str:
    rows = "".join(_row_html(row, bands_html, peak) for row in section.rows)
    totals = " · ".join(
        f"{escape(kind_for(kind).name)} {count}"
        for kind, count in section.totals.items()
        if kind in ALWAYS_RENDERED or count
    )
    return (
        f'<section class="group" id="{escape(section.key, quote=True)}">'
        f'<div class="ghead"><h2>{escape(section.name)}</h2>'
        f'<div class="t">{totals}</div></div>'
        f"{rows}{axis}"
        f"</section>"
    )


def _legend_html(bands: Sequence[BandSpan], peak: int) -> str:
    kinds = "".join(
        f'<span class="k {escape(kind)}"><i class="sw"></i>{escape(kind_for(kind).name)}</span>'
        for kind in ALWAYS_RENDERED
    )
    seen: list[str] = []
    periods: list[str] = []
    for band in bands:
        if band.name in seen:
            continue
        seen.append(band.name)
        periods.append(
            f'<span class="per"><i class="sw i{band.shade % BAND_CLASSES}"></i>'
            f"{escape(band.name)}</span>"
        )
    density = (
        f"Upper strip: when a detection was active. Lower strip: events per bucket, "
        f"darkest at {_plural(peak, 'event')}."
        if peak
        else "Upper strip: when a detection was active. Lower strip: events per bucket."
    )
    return (
        f'<div class="legend">{kinds}{"".join(periods)}</div>'
        f'<p class="sub" style="margin-top:.6rem">{escape(density)}</p>'
    )


def _theme_html() -> str:
    """The Auto/Light/Dark control, hidden until a script can make it work."""
    buttons = "".join(
        f'<button type="button" data-theme="{choice}" aria-pressed="false">{label}</button>'
        for choice, label in (("auto", "Auto"), ("light", "Light"), ("dark", "Dark"))
    )
    return (
        '<span class="theme" id="theme" role="group" aria-label="Colour theme" hidden>'
        f"{buttons}</span>"
    )


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n<style>{STYLE}</style>\n"
        f"<script>{HEAD_SCRIPT}</script>\n</head>\n"
        f'<body>\n<div class="wrap">\n{body}\n</div>\n'
        f"<script>{BODY_SCRIPT}</script>\n</body>\n</html>\n"
    )


def day_filename(day: date) -> str:
    return f"{day.isoformat()}.html"


def render_day(sheet: DaySheet, reports: ReportConfig) -> str:
    """Render one day's sheet."""
    bands_html = _bands_html(sheet.bands)
    axis_html = _axis_html(sheet.axis)
    heading = sheet.day.strftime("%A %-d %B %Y") if _supports_dash() else sheet.day.isoformat()
    title = f"{reports.title} — {sheet.day.isoformat()}"

    nav = ['<nav class="days">']
    if sheet.previous_day is not None:
        nav.append(f'<a href="{day_filename(sheet.previous_day)}">← {sheet.previous_day}</a>')
    else:
        nav.append("<span>← earlier</span>")
    nav.append('<a href="index.html">All days</a>')
    if sheet.next_day is not None:
        nav.append(f'<a href="{day_filename(sheet.next_day)}">{sheet.next_day} →</a>')
    else:
        nav.append("<span>later →</span>")
    nav.append(_theme_html())
    nav.append("</nav>")

    cards = "".join(
        f'<div class="card"><b>{escape(section.name)}</b><div class="n">'
        + "<br>".join(
            f"{escape(kind_for(kind).name)} <span>{count}</span>"
            for kind, count in section.totals.items()
            if kind in ALWAYS_RENDERED or count
        )
        + "</div></div>"
        for section in sheet.sections
    )
    sections = "".join(
        _section_html(section, bands_html, sheet.peak_bucket, axis_html)
        for section in sheet.sections
    )
    if not sections:
        sections = '<p class="empty">No cameras to report on yet.</p>'

    generated = sheet.generated_at.strftime("%Y-%m-%d %H:%M %Z")
    body = (
        f"<h1>{escape(reports.title)} — {escape(heading)}</h1>\n"
        f'<p class="sub">Updated {escape(generated)} · rewritten every '
        f'{_plural(reports.refresh_minutes, "minute")}</p>\n'
        f'{"".join(nav)}\n'
        f'<div class="cards">{cards}</div>\n'
        f"{sections}\n"
        f"{_legend_html(sheet.bands, sheet.peak_bucket)}"
    )
    return _page(title, body)


def render_index(
    entries: Sequence[IndexEntry],
    columns: Sequence[tuple[str, str]],
    reports: ReportConfig,
    generated_at: datetime,
) -> str:
    """Render ``index.html``: every day so far, with per-group event counts."""
    if not entries:
        body = (
            f"<h1>{escape(reports.title)}</h1>\n"
            f'<nav class="days">{_theme_html()}</nav>\n'
            '<p class="empty">No days have been recorded yet. The first sheet appears '
            "within the hour.</p>"
        )
        return _page(reports.title, body)

    head = "".join(f"<th>{escape(name)}</th>" for _, name in columns)
    rows: list[str] = []
    for entry in entries:
        cells: list[str] = []
        for key, _ in columns:
            cell = entry.cells.get(key)
            motion = cell.counts.get(MOTION, 0) if cell else 0
            person = cell.counts.get(PERSON, 0) if cell else 0
            klass = ' class="z"' if not (motion or person) else ""
            cells.append(f"<td{klass}>{motion} / {person}</td>")
        rows.append(
            f'<tr><td><a href="{entry.href}">{entry.day.isoformat()}</a></td>'
            f'{"".join(cells)}'
            f'<td class="tot">{entry.total(MOTION)} / {entry.total(PERSON)}</td></tr>'
        )

    generated = generated_at.strftime("%Y-%m-%d %H:%M %Z")
    body = (
        f"<h1>{escape(reports.title)}</h1>\n"
        f'<p class="sub">{_plural(len(entries), "day")} recorded · '
        f"updated {escape(generated)} · counts are "
        f"<b>motion / person</b> events</p>\n"
        f'<nav class="days">{_theme_html()}</nav>\n'
        f'<div class="scroll"><table class="index">'
        f"<thead><tr><th>Day</th>{head}<th>All</th></tr></thead>"
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )
    return _page(reports.title, body)


def _supports_dash() -> bool:
    """``%-d`` is a glibc/BSD extension; fall back to ISO dates where it is not."""
    try:
        return date(2026, 1, 2).strftime("%-d") == "2"
    except ValueError:
        return False
