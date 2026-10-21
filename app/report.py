"""Self-contained HTML comparison report. Charts are inline SVG so the file works offline and prints cleanly."""

from __future__ import annotations

from html import escape
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from .config import HITL_MODES, Taxonomy

JEV_COLOR = "#2a78d6"
KEV_COLOR = "#eb6834"
MODEL_COLORS = {"jev": JEV_COLOR, "kev": KEV_COLOR}
# NOUL is a criticality state: status palette, always shown with a text label.
NOUL_COLORS = {"true": "#d03b3b", "false": "#a8a79f"}
# Severity levels in order (least -> most severe), status palette.
SEVERITY_COLORS = ["#a8a79f", "#fab219", "#ec835a", "#d03b3b"]
STATUS_COLORS = {"aligned": "#0ca30c", "partial": "#fab219", "diverges": "#d03b3b", "error": "#898781"}
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e1e0d9"

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)


def _fmt(value, kind: str) -> str:
    if value is None:
        return "—"
    if kind == "pct":
        return f"{value:.0%}"
    if kind == "ms":
        return f"{value:,.0f} ms"
    if kind == "usd":
        return f"${value:,.6f}" if value < 0.01 else f"${value:,.4f}" if value < 1 else f"${value:,.2f}"
    if kind == "tok":
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def _duration(seconds) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f} s"
    m, s = divmod(round(seconds), 60)
    return f"{m}m {s:02d}s" if m < 60 else f"{m // 60}h {m % 60:02d}m"


def paired_bars(rows: list[tuple[str, float | None, float | None, str, bool]], labels: dict) -> Markup:
    """Small multiples: one row per metric, each scaled to its own max (never mixes units on one axis).
    rows: (metric name, jev value, kev value, format kind, lower_is_better)"""
    w, label_w, bar_h, gap, row_gap = 640, 170, 14, 4, 18
    track = w - label_w - 90
    row_h = bar_h * 2 + gap + row_gap
    h = len(rows) * row_h + 4
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" style="max-width:{w + 80}px" role="img" aria-label="Metric comparison" class="chart">']
    for i, (name, jv, kv, kind, lower_better) in enumerate(rows):
        y0 = i * row_h
        vmax = max(v for v in (jv, kv, 0) if v is not None) or 1
        out.append(f'<text x="0" y="{y0 + bar_h + 4}" class="lbl">{escape(name)}</text>')
        winner = None
        if jv is not None and kv is not None and jv != kv:
            winner = ("jev" if jv < kv else "kev") if lower_better else ("jev" if jv > kv else "kev")
        for j, (key, v) in enumerate((("jev", jv), ("kev", kv))):
            y = y0 + j * (bar_h + gap)
            bw = 0 if v is None else max(2, track * v / vmax)
            out.append(f'<rect x="{label_w}" y="{y}" width="{track}" height="{bar_h}" rx="3" class="track"/>')
            out.append(
                f'<rect x="{label_w}" y="{y}" width="{bw:.1f}" height="{bar_h}" rx="3" fill="{MODEL_COLORS[key]}">'
                f"<title>{escape(labels[key])}: {_fmt(v, kind)}</title></rect>"
            )
            mark = " ★" if winner == key else ""
            out.append(f'<text x="{label_w + track + 6}" y="{y + bar_h - 3}" class="val">{_fmt(v, kind)}{mark}</text>')
    out.append("</svg>")
    return Markup("".join(out))


def critical_bars(models: dict, labels: dict, noul: dict) -> Markup:
    """Share of tickets each model flags as critical vs not critical (100% stacked)."""
    w, label_w, bar_h, row_gap = 640, 90, 26, 16
    track = w - label_w - 10
    h = 2 * (bar_h + row_gap) + 30
    names = {"true": noul["outcomes"]["true"]["name"], "false": noul["outcomes"]["false"]["name"]}
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" style="max-width:{w + 80}px" role="img" aria-label="NOUL distribution" class="chart">']
    for i, key in enumerate(("jev", "kev")):
        dist = models[key]["distribution"]["critical"]
        total = sum(dist.values()) or 1
        y = i * (bar_h + row_gap)
        out.append(f'<text x="0" y="{y + bar_h / 2 + 4}" class="lbl">{escape(labels[key])}</text>')
        x = label_w
        for outcome in ("true", "false"):
            n = dist.get(outcome, 0)
            if not n:
                continue
            bw = track * n / total
            out.append(
                f'<rect x="{x:.1f}" y="{y}" width="{max(bw - 2, 1):.1f}" height="{bar_h}" rx="3" fill="{NOUL_COLORS[outcome]}">'
                f"<title>{escape(names[outcome])}: {n} ({n / total:.0%})</title></rect>"
            )
            if bw > 60:
                out.append(f'<text x="{x + 6:.1f}" y="{y + bar_h / 2 + 4}" class="seg">{escape(names[outcome])} · {n}</text>')
            x += bw
    ly = 2 * (bar_h + row_gap) + 12
    for j, outcome in enumerate(("true", "false")):
        lx = label_w + j * 150
        out.append(f'<rect x="{lx}" y="{ly - 9}" width="10" height="10" rx="2" fill="{NOUL_COLORS[outcome]}"/>')
        out.append(f'<text x="{lx + 14}" y="{ly}" class="leg">{escape(names[outcome])}</text>')
    out.append("</svg>")
    return Markup("".join(out))


def pair_scatter(points: list[dict], lo: float, hi: float, ticks: list[tuple[float, str]], labels: dict, what: str, value_fmt) -> Markup:
    """JEV value on x, KEV value on y; dots on the dashed diagonal = same answer."""
    size, pad = 320, 44
    span = hi - lo or 1
    plot = size - 2 * pad

    def px(v):
        return pad + plot * (v - lo) / span

    def py(v):
        return size - pad - plot * (v - lo) / span

    out = [f'<svg viewBox="0 0 {size} {size}" width="100%" style="max-width:380px" role="img" aria-label="{escape(what)} JEV vs KEV" class="chart">']
    for v, text in ticks:
        out.append(f'<line x1="{px(v)}" y1="{pad}" x2="{px(v)}" y2="{size - pad}" class="grid"/>')
        out.append(f'<line x1="{pad}" y1="{py(v)}" x2="{size - pad}" y2="{py(v)}" class="grid"/>')
        out.append(f'<text x="{px(v)}" y="{size - pad + 14}" class="tick" text-anchor="middle">{escape(text)}</text>')
        out.append(f'<text x="{pad - 6}" y="{py(v) + 4}" class="tick" text-anchor="end">{escape(text)}</text>')
    out.append(f'<line x1="{px(lo)}" y1="{py(lo)}" x2="{px(hi)}" y2="{py(hi)}" class="diag"/>')
    for p in points:
        out.append(
            f'<circle cx="{px(p["jev"]):.1f}" cy="{py(p["kev"]):.1f}" r="6" class="dot">'
            f"<title>{escape(str(p.get('id') or ''))}: {escape(labels['jev'])} {value_fmt(p['jev'])} / {escape(labels['kev'])} {value_fmt(p['kev'])}</title></circle>"
        )
    out.append(f'<text x="{size / 2}" y="{size - 6}" class="axis" text-anchor="middle">{escape(labels["jev"])} {escape(what)} →</text>')
    out.append(f'<text x="12" y="{size / 2}" class="axis" text-anchor="middle" transform="rotate(-90 12 {size / 2})">{escape(labels["kev"])} {escape(what)} →</text>')
    out.append("</svg>")
    return Markup("".join(out))


def calibration_chart(models: dict, labels: dict, dim: str, threshold: float) -> Markup:
    size, pad = 320, 40
    plot = size - 2 * pad

    def px(v):
        return pad + plot * v

    def py(v):
        return size - pad - plot * v

    out = [f'<svg viewBox="0 0 {size} {size}" width="100%" style="max-width:380px" role="img" aria-label="Calibration {dim}" class="chart">']
    for v in (0, 0.2, 0.4, 0.6, 0.8, 1.0):
        out.append(f'<line x1="{px(v)}" y1="{pad}" x2="{px(v)}" y2="{size - pad}" class="grid"/>')
        out.append(f'<line x1="{pad}" y1="{py(v)}" x2="{size - pad}" y2="{py(v)}" class="grid"/>')
        out.append(f'<text x="{px(v)}" y="{size - pad + 14}" class="tick" text-anchor="middle">{v:.0%}</text>')
        out.append(f'<text x="{pad - 6}" y="{py(v) + 4}" class="tick" text-anchor="end">{v:.0%}</text>')
    out.append(f'<line x1="{px(0)}" y1="{py(0)}" x2="{px(1)}" y2="{py(1)}" class="diag"/>')
    out.append(f'<line x1="{px(threshold)}" y1="{pad}" x2="{px(threshold)}" y2="{size - pad}" class="threshold"/>')
    for key in ("jev", "kev"):
        pts = [b for b in models[key]["calibration"][dim] if b["count"] and b["accuracy"] is not None]
        if len(pts) > 1:
            d = " ".join(f"{px(b['avg_confidence']):.1f},{py(b['accuracy']):.1f}" for b in pts)
            out.append(f'<polyline points="{d}" fill="none" stroke="{MODEL_COLORS[key]}" stroke-width="2"/>')
        for b in pts:
            out.append(
                f'<circle cx="{px(b["avg_confidence"]):.1f}" cy="{py(b["accuracy"]):.1f}" r="5" fill="{MODEL_COLORS[key]}" class="ring">'
                f"<title>{escape(labels[key])} bin {b['bin']}: n={b['count']}, conf {b['avg_confidence']:.0%}, accuracy {b['accuracy']:.0%}</title></circle>"
            )
    out.append(f'<text x="{size / 2}" y="{size - 6}" class="axis" text-anchor="middle">Stated confidence →</text>')
    out.append(f'<text x="12" y="{size / 2}" class="axis" text-anchor="middle" transform="rotate(-90 12 {size / 2})">Actual accuracy →</text>')
    out.append("</svg>")
    return Markup("".join(out))


def _cm_label(key: str, names: dict) -> str:
    # Boolean NOUL outcomes read better by name; team ids stay short.
    return names.get(key, key) if key in ("true", "false") else key


def confusion_table(cells: list[dict], ids: list[str], labels: dict, names: dict) -> Markup:
    grid = {(c["jev"], c["kev"]): c["count"] for c in cells}
    present = [i for i in ids if any(i in (c["jev"], c["kev"]) for c in cells)] or ids
    vmax = max(grid.values(), default=1)
    head = "".join(f'<th scope="col" title="{escape(names.get(i, i))}">{escape(_cm_label(i, names))}</th>' for i in present)
    rows = []
    for a in present:
        tds = []
        for b in present:
            n = grid.get((a, b), 0)
            alpha = 0 if not n else 0.15 + 0.85 * n / vmax
            cls = "diag" if a == b else ("off" if n else "")
            style = f"background:rgba(42,120,214,{alpha:.2f});" + ("color:#fff;" if alpha > 0.55 else "")
            tds.append(f'<td class="{cls}" style="{style}">{n or ""}</td>')
        rows.append(f'<tr><th scope="row" title="{escape(names.get(a, a))}">{escape(_cm_label(a, names))}</th>{"".join(tds)}</tr>')
    return Markup(
        f'<div style="overflow-x:auto"><table class="cm"><caption>Rows: {escape(labels["jev"])} · Columns: {escape(labels["kev"])} — '
        f'diagonal = agreement</caption><thead><tr><th></th>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def render(run: dict, tax: Taxonomy) -> str:
    summary = run["summary"]
    models = summary["models"]
    labels = {k: v["label"] for k, v in run["models"].items()}
    jev, kev = models["jev"], models["kev"]
    perf_rows = [
        ("Avg latency", jev["latency_ms"]["avg"], kev["latency_ms"]["avg"], "ms", True),
        ("p95 latency", jev["latency_ms"]["p95"], kev["latency_ms"]["p95"], "ms", True),
        ("Avg TTFB", jev["ttfb_ms"]["avg"], kev["ttfb_ms"]["avg"], "ms", True),
        ("Input tokens / call", jev["tokens"]["input_avg"], kev["tokens"]["input_avg"], "tok", True),
        ("Output tokens / call", jev["tokens"]["output_avg"], kev["tokens"]["output_avg"], "tok", True),
        ("Cost / 1k tickets", jev["cost"]["per_1k_tickets_usd"], kev["cost"]["per_1k_tickets_usd"], "usd", True),
    ]
    quality_rows = [
        ("Human-review rate", jev["hitl_rate"], kev["hitl_rate"], "pct", True),
        ("Avg team confidence", jev["avg_team_confidence"], kev["avg_team_confidence"], "pct", False),
        ("Avg NOUL confidence", jev["avg_noul_confidence"], kev["avg_noul_confidence"], "pct", False),
        ("Avg severity confidence", jev["avg_severity_confidence"], kev["avg_severity_confidence"], "pct", False),
    ]
    has_gt = bool(jev["accuracy"]["labelled"] or kev["accuracy"]["labelled"])
    if has_gt:
        quality_rows += [
            ("Team accuracy", jev["accuracy"]["team"], kev["accuracy"]["team"], "pct", False),
            ("NOUL accuracy", jev["accuracy"]["noul"], kev["accuracy"]["noul"], "pct", False),
            ("Team + NOUL accuracy", jev["accuracy"]["both"], kev["accuracy"]["both"], "pct", False),
            ("Severity MAE (levels)", jev["accuracy"]["severity_mae"], kev["accuracy"]["severity_mae"], "num", True),
        ]
    if jev["consistency"] and kev["consistency"]:
        quality_rows += [
            ("Team consistency", jev["consistency"]["team"], kev["consistency"]["team"], "pct", False),
            ("NOUL consistency", jev["consistency"]["noul"], kev["consistency"]["noul"], "pct", False),
        ]

    levels = tax.severity_levels
    noul_names = {"true": tax.noul["outcomes"]["true"]["name"], "false": tax.noul["outcomes"]["false"]["name"]}
    results = run["results"]
    charts = {
        "perf": paired_bars(perf_rows, labels),
        "quality": paired_bars(quality_rows, labels),
        "noul": critical_bars(models, labels, tax.noul),
        "noul_scatter": pair_scatter(
            summary["pair"]["noul_scatter"], 0, 1, [(v / 4, f"{v * 25}%") for v in range(5)], labels, "P(critical)", lambda v: f"{v:.0%}"
        ),
        "scatter": pair_scatter(
            summary["pair"]["severity_scatter"], 0, len(levels) - 1, [(i, l["name"]) for i, l in enumerate(levels)],
            labels, "severity", lambda v: f"{v:.2f}",
        ),
        "team_cm": confusion_table(summary["pair"]["team_confusion"], tax.team_ids, labels, {t["id"]: t["name"] for t in tax.teams}),
        "noul_cm": confusion_table(summary["pair"]["noul_confusion"], ["true", "false"], labels, noul_names),
        "cal_team": calibration_chart(models, labels, "team", run["settings"]["hitl_threshold"]) if has_gt else None,
        "cal_noul": calibration_chart(models, labels, "noul", run["settings"]["hitl_threshold"]) if has_gt else None,
    }
    template = _env.get_template("report.html")
    return template.render(
        run=run,
        summary=summary,
        models=models,
        labels=labels,
        charts=charts,
        has_gt=has_gt,
        divergent=[r for r in results if r["comparison"]["status"] in ("diverges", "partial", "error")],
        hitl=[r for r in results if r["primary"]["jev"]["hitl"]["required"] or r["primary"]["kev"]["hitl"]["required"]],
        results=results,
        fmt=_fmt,
        duration=_duration,
        hitl_modes=HITL_MODES,
        team_names={t["id"]: t["name"] for t in tax.teams},
        noul_names=noul_names,
        noul_colors=NOUL_COLORS,
        severity_colors=SEVERITY_COLORS,
        severity_names={l["id"]: l["name"] for l in levels},
        status_colors=STATUS_COLORS,
        model_colors=MODEL_COLORS,
        taxonomy=tax,
    )
