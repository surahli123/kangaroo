"""Charts for the report, as SVG files. Standard library only.

Three charts, each from aggregates that the report already shows in a table (the table is the
chart's text twin, so no value lives only in a chart):

- results.svg  each contract metric: its change (dot) and 95% CI (line) against zero, with its
               bar or limit (short upright mark). One color for one arm; one color per arm for more.
- trend.svg    the lead primary's relative change in each daily scorecard snapshot.
- cuts.svg     each cause with cuts: the cut and its control cut, with their 95% CIs.

Colors and marks follow the reference data-viz palette: categorical slots in fixed order (validated
for 3 slots in light and dark), 2px lines, dots of radius 4.5 with a 2px surface ring, hairline
solid grid, text in ink colors only. Each SVG carries its own light and dark styles, so it reads the
same as a file and inside the HTML report. Hover gives the values (native SVG titles).
"""

import html
import math

SLOTS = [("#2a78d6", "#3987e5"), ("#eb6834", "#d95926"), ("#1baf7a", "#199e70"), ("#eda100", "#c98500"),
         ("#e87ba4", "#d55181"), ("#008300", "#008300"), ("#6250d6", "#9085e9"), ("#e34948", "#e66767")]
CONTEXT = "#898781"  # de-emphasis gray for marks that are context (the control cut, other metrics)
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def _style():
    light = "".join(f".kg-s{i}{{stroke:{a};fill:{a}}}" for i, (a, _) in enumerate(SLOTS))
    dark = "".join(f".kg-s{i}{{stroke:{b};fill:{b}}}" for i, (_, b) in enumerate(SLOTS))
    return ("<style>"
            f".kg text{{font-family:{FONT};font-size:12px;fill:#0b0b0b}}"
            ".kg .kg-t2{fill:#52514e}.kg .kg-t3{fill:#898781;font-size:11px;font-variant-numeric:tabular-nums}"
            ".kg .kg-head{font-weight:600;fill:#52514e}.kg .kg-title{font-size:13px;font-weight:600}"
            ".kg .kg-grid{stroke:#e1e0d9;stroke-width:1}.kg .kg-base{stroke:#c3c2b7;stroke-width:1}"
            ".kg .kg-thr{stroke:#52514e;stroke-width:2;stroke-linecap:round}"
            f".kg .kg-ctx{{stroke:{CONTEXT};fill:{CONTEXT}}}.kg .kg-ring{{stroke:var(--bg,#ffffff);stroke-width:2}}"
            ".kg .kg-line{fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}"
            ".kg .kg-hit{fill:transparent;stroke:none}.kg g.kg-row:hover .kg-hit{fill:rgba(11,11,11,0.04)}"
            + light +
            "@media (prefers-color-scheme: dark){"
            ".kg text{fill:#f0efec}.kg .kg-t2,.kg .kg-head{fill:#c3c2b7}.kg .kg-grid{stroke:#2c2c2a}.kg .kg-base{stroke:#383835}"
            ".kg .kg-thr{stroke:#c3c2b7}.kg .kg-ring{stroke:var(--bg,#0d1117)}"
            ".kg g.kg-row:hover .kg-hit{fill:rgba(255,255,255,0.06)}" + dark + "}"
            "</style>")


def _e(text):
    return html.escape(str(text), quote=True)


def _pct(x):
    v = 100 * x
    return f"{v:+.1f}%" if abs(v) >= 10 else f"{v:+.2f}%"


def _tick_text(x):
    v = round(100 * x, 6)
    return f"{v:+g}%" if v else "0%"


def _ticks(lo, hi, n=5):
    """Clean tick values that cover [lo, hi]."""
    span = hi - lo or abs(hi) or 0.01
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.floor(lo / step) * step
    out, t = [], first
    while t <= hi + step * 1e-9:
        out.append(round(t, 12))
        t += step
    return out


def _clip_domain(rows, marks):
    """[lo, hi] of the x axis. An interval far wider than the others is clipped at the edge, so that
    one large change does not hide the small ones (the clipped value is written at the edge)."""
    ext = sorted(max(abs(r["lo"]), abs(r["hi"])) for r in rows)
    med = ext[len(ext) // 2] if ext else 0.01
    cap = max([e for e in ext if e <= 4 * med] + [abs(m) for m in marks] + [med, 1e-4]) * 1.15
    lo = min([0] + [max(r["lo"], -cap) for r in rows] + [m for m in marks if m >= -cap])
    hi = max([0] + [min(r["hi"], cap) for r in rows] + [m for m in marks if m <= cap])
    pad = (hi - lo) * 0.06 or 0.001
    return lo - pad, hi + pad


def _svg(width, height, cid, title, desc, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" class="kg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="{cid}-t {cid}-d">'
            f'<title id="{cid}-t">{_e(title)}</title><desc id="{cid}-d">{_e(desc)}</desc>{_style()}{body}</svg>')


def _legend(items, x, y):
    """items: [(class, label)]; dots in the series color, text in ink."""
    out = []
    for cls, label in items:
        out.append(f'<circle cx="{x + 5}" cy="{y - 4}" r="4.5" class="{cls}"/>'
                   f'<text x="{x + 14}" y="{y}" class="kg-t2">{_e(label)}</text>')
        x += 24 + 7 * len(label)
    return "".join(out)


def interval_chart(cid, title, groups, legend=None, width=720):
    """groups: [(heading or None, [row])]; row: {label, sub (muted label or None), point, lo, hi,
    cls (series class), mark (bar or limit, or None), tip}. Returns SVG text."""
    rows = [r for _, rs in groups for r in rs]
    if not rows:
        return None
    marks = [r["mark"] for r in rows if r.get("mark") is not None]
    lo, hi = _clip_domain(rows, marks)
    longest = max(len(r.get("sub") or r["label"]) for r in rows)  # the label column fits its longest label, within limits
    left, right, top, rh = int(min(max(longest * 6.6 + 24, 160), 400)), 24, 44 if legend else 30, 26
    fits = longest if longest * 6.6 + 24 <= 400 else int((400 - 24) / 6.6)
    plot_w = width - left - right
    x = lambda v: left + (min(max(v, lo), hi) - lo) / (hi - lo) * plot_w  # noqa: E731
    n_lines = sum(len(rs) + (1 if h else 0) for h, rs in groups)
    plot_h = n_lines * rh
    height = top + plot_h + 34
    body = [f'<text x="0" y="16" class="kg-title">{_e(title)}</text>']
    key = "dot: change \u00b7 line: 95% CI" + (" \u00b7 upright mark: bar or limit" if marks else "")
    body.append(f'<text x="{width - right}" y="16" text-anchor="end" class="kg-t3">{key}</text>')
    if legend:
        body.append(_legend(legend, left, 36))
    ticks = [t for t in _ticks(lo, hi) if lo <= t <= hi]
    for t in ticks:
        body.append(f'<line x1="{x(t):.1f}" y1="{top - 4}" x2="{x(t):.1f}" y2="{top + plot_h}" class="kg-grid"/>'
                    f'<text x="{x(t):.1f}" y="{top + plot_h + 16}" text-anchor="middle" class="kg-t3">{_tick_text(t)}</text>')
    body.append(f'<line x1="{x(0):.1f}" y1="{top - 4}" x2="{x(0):.1f}" y2="{top + plot_h}" class="kg-base"/>')
    y = top
    for head, rs in groups:
        if head:
            body.append(f'<text x="0" y="{y + 17}" class="kg-head">{_e(head)}</text>')
            y += rh
        for r in rs:
            cy = y + rh / 2
            label = r["label"] if len(r["label"]) <= fits else r["label"][:fits - 1] + "…"
            g = [f'<g class="kg-row" tabindex="0"><title>{_e(r["tip"])}</title>'
                 f'<rect x="0" y="{y}" width="{width}" height="{rh}" class="kg-hit"/>',
                 f'<text x="{left - 12}" y="{cy + 4}" text-anchor="end" class="{"kg-t2" if r.get("sub") else ""}">'
                 f'{_e(r.get("sub") or label)}</text>']
            if r.get("mark") is not None and lo <= r["mark"] <= hi:
                g.append(f'<line x1="{x(r["mark"]):.1f}" y1="{cy - 7}" x2="{x(r["mark"]):.1f}" y2="{cy + 7}" class="kg-thr"/>')
            g.append(f'<line x1="{x(r["lo"]):.1f}" y1="{cy}" x2="{x(r["hi"]):.1f}" y2="{cy}" '
                     f'class="kg-line {r["cls"]}"/>')
            for edge, side in ((hi, 1), (lo, -1)):  # a clipped interval: a chevron and its value at the edge
                beyond = r["hi"] > hi if side > 0 else r["lo"] < lo
                if beyond:
                    ex = x(edge)
                    g.append(f'<path d="M{ex - 5 * side:.1f},{cy - 4} L{ex:.1f},{cy} L{ex - 5 * side:.1f},{cy + 4}" '
                             f'class="kg-line {r["cls"]}"/>')
            px = x(r["point"])
            g.append(f'<circle cx="{px:.1f}" cy="{cy}" r="4.5" class="{r["cls"]} kg-ring"/>')
            if not lo <= r["point"] <= hi:
                anchor, dx = ("end", -10) if r["point"] > hi else ("start", 10)
                g.append(f'<text x="{px + dx:.1f}" y="{cy - 7}" text-anchor="{anchor}" class="kg-t3">{_pct(r["point"])}</text>')
            body.append("".join(g) + "</g>")
            y += rh
    desc = "; ".join(r["tip"] for r in rows)
    return _svg(width, height, cid, title, desc, "".join(body))


def line_chart(cid, title, lines, width=720, height=230):
    """lines: [{name, cls, points: [(day text, value)]}] on one shared day axis. Returns SVG text."""
    days = sorted({d for ln in lines for d, _ in ln["points"]})
    if len(days) < 3:
        return None
    vals = [v for ln in lines for _, v in ln["points"]] + [0]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or 0.001
    lo, hi = lo - pad, hi + pad
    left, right, top = 56, 80, 44 if len(lines) > 1 else 30
    plot_w, plot_h = width - left - right, height - top - 30
    x = lambda d: left + days.index(d) / (len(days) - 1) * plot_w  # noqa: E731
    yv = lambda v: top + (hi - v) / (hi - lo) * plot_h  # noqa: E731
    body = [f'<text x="0" y="16" class="kg-title">{_e(title)}</text>']
    if len(lines) > 1:
        body.append(_legend([(ln["cls"], ln["name"]) for ln in lines], left, 36))
    for t in [t for t in _ticks(lo, hi, 4) if lo <= t <= hi]:
        body.append(f'<line x1="{left}" y1="{yv(t):.1f}" x2="{left + plot_w}" y2="{yv(t):.1f}" class="kg-grid"/>'
                    f'<text x="{left - 8}" y="{yv(t) + 4:.1f}" text-anchor="end" class="kg-t3">{_tick_text(t)}</text>')
    body.append(f'<line x1="{left}" y1="{yv(0):.1f}" x2="{left + plot_w}" y2="{yv(0):.1f}" class="kg-base"/>')
    for d in (days[0], days[len(days) // 2], days[-1]):
        body.append(f'<text x="{x(d):.1f}" y="{top + plot_h + 18}" text-anchor="middle" class="kg-t3">{_e(d[5:10])}</text>')
    ends = []
    for ln in lines:
        pts = sorted(ln["points"])
        path = " ".join(f"{'M' if i == 0 else 'L'}{x(d):.1f},{yv(v):.1f}" for i, (d, v) in enumerate(pts))
        body.append(f'<path d="{path}" class="kg-line {ln["cls"]}"/>')
        for d, v in pts:  # hover targets: one per day, larger than the line
            body.append(f'<g class="kg-row"><title>{_e(ln["name"])}, {_e(d[:10])}: {_pct(v)}</title>'
                        f'<circle cx="{x(d):.1f}" cy="{yv(v):.1f}" r="10" class="kg-hit"/></g>')
        d, v = pts[-1]
        body.append(f'<circle cx="{x(d):.1f}" cy="{yv(v):.1f}" r="4.5" class="{ln["cls"]} kg-ring"/>')
        ends.append((yv(v), v, x(d)))
    ys = sorted(e[0] for e in ends)
    if all(b - a >= 13 for a, b in zip(ys, ys[1:])):  # end labels only where they do not collide
        for ey, v, ex in ends:
            body.append(f'<text x="{ex + 10:.1f}" y="{ey + 4:.1f}" class="kg-t2">{_pct(v)}</text>')
    desc = "; ".join(f"{ln['name']}: {', '.join(f'{d[:10]} {_pct(v)}' for d, v in sorted(ln['points']))}" for ln in lines)
    return _svg(width, height, cid, title, desc, "".join(body))
