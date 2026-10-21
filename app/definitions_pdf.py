"""PDF export of the triage definitions (teams, NOUL criticality, severity, human-review rule)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fpdf import FPDF, FontFace

from .config import HITL_MODES, Settings, Taxonomy
from .prompt import build_questions

# fontTools logs harmless "don't know how to subset" notices for some system fonts.
logging.getLogger("fontTools.subset").setLevel(logging.ERROR)

# Unicode TTF pairs (regular, bold) tried in order; falls back to the core Helvetica font.
FONT_CANDIDATES = [
    ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
]
# Latin-1 replacements when no Unicode font is available.
ASCII_MAP = str.maketrans({"—": "-", "–": "-", "≥": ">=", "≤": "<=", "→": "->", "…": "...", "“": '"', "”": '"',
                           "‘": "'", "’": "'", "•": "-", "−": "-", "×": "x", "✔": "v", "⚠": "!"})

INK, MUTED, GRID = (11, 11, 11), (82, 81, 78), (225, 224, 217)
HEAD_FILL = (240, 239, 236)
NOUL_FILL = {"true": ((208, 59, 59), (255, 255, 255)), "false": ((168, 167, 159), (11, 11, 11))}
SEVERITY_FILL = [((168, 167, 159), (11, 11, 11)), ((250, 178, 25), (11, 11, 11)),
                 ((236, 131, 90), (11, 11, 11)), ((208, 59, 59), (255, 255, 255))]


class DefinitionsPDF(FPDF):
    def __init__(self, footer_text: str, font_paths: tuple[str, str] | None = None):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.footer_text = footer_text
        self.set_margins(16, 16, 16)
        self.set_auto_page_break(auto=True, margin=16)
        self.unicode = False
        paths = [font_paths] if font_paths else FONT_CANDIDATES
        for regular, bold in paths:
            if Path(regular).exists() and Path(bold).exists():
                self.add_font("Body", "", regular)
                self.add_font("Body", "B", bold)
                self.unicode = True
                break
        self.family = "Body" if self.unicode else "Helvetica"

    def t(self, text) -> str:
        text = "" if text is None else str(text)
        return text if self.unicode else text.translate(ASCII_MAP).encode("latin-1", "replace").decode("latin-1")

    def font(self, size: float, bold: bool = False, color=INK):
        self.set_font(self.family, "B" if bold else "", size)
        self.set_text_color(*color)

    def footer(self):
        self.set_y(-11)
        self.font(7.5, color=MUTED)
        self.cell(0, 5, self.t(self.footer_text), align="L")
        self.set_x(self.l_margin)
        self.cell(0, 5, f"{self.page_no()} / {{nb}}", align="R")

    def heading(self, text: str, tag: str | None = None):
        if self.get_y() > self.h - 50:
            self.add_page()
        self.ln(4)
        self.font(14, bold=True)
        self.cell(0, 8, self.t(text))
        if tag:
            self.font(8.5, color=MUTED)
            self.cell(0, 8, self.t(tag), align="R")
        self.ln(8)
        self.set_draw_color(*GRID)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(3)

    def para(self, text: str, size: float = 9.5, color=INK, bold: bool = False, gap: float = 1.5):
        self.font(size, bold=bold, color=color)
        self.multi_cell(0, size * 0.5, self.t(text), new_x="LMARGIN", new_y="NEXT")
        self.ln(gap)

    def bullets(self, items: list[str], size: float = 9.5):
        self.font(size)
        for item in items:
            self.set_x(self.l_margin + 3)
            self.multi_cell(0, size * 0.5, self.t(f"•  {item}"), new_x="LMARGIN", new_y="NEXT")
        self.ln(1.5)

    def table_style(self):
        return dict(
            headings_style=FontFace(family=self.family, emphasis="BOLD", color=MUTED, fill_color=HEAD_FILL, size_pt=8.5),
            borders_layout="HORIZONTAL_LINES",
            line_height=4.6,
            padding=1.6,
            text_align="LEFT",
            v_align="TOP",
        )


def build_definitions_pdf(tax: Taxonomy, settings: Settings, font_paths: tuple[str, str] | None = None) -> bytes:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pdf = DefinitionsPDF(f"Triage definitions · taxonomy {tax.fingerprint} · generated {generated}", font_paths)
    pdf.set_title("Triage Definitions")
    pdf.set_author("JEV vs KEV Triage Benchmark")
    pdf.alias_nb_pages()
    pdf.add_page()

    # ---------- cover block ----------
    pdf.font(20, bold=True)
    pdf.cell(0, 10, pdf.t("Triage Definitions"), new_x="LMARGIN", new_y="NEXT")
    pdf.para("Teams, NOUL criticality and severity used to triage support tickets, plus the human-review rule. "
             "These definitions are sent, unchanged, to both models as typed Decisions API questions.", color=MUTED)
    models = " vs ".join(f"{m.label} ({m.slug}, {m.role})" for m in settings.models)
    meta = [
        ("Models", models),
        ("Taxonomy fingerprint", tax.fingerprint),
        ("Source files", "config/teams.yaml · config/criticality.yaml · config/severity.yaml"),
        ("Generated", generated),
    ]
    with pdf.table(col_widths=(40, 138), first_row_as_headings=False, **{**pdf.table_style(), "borders_layout": "NONE"}) as table:
        for k, v in meta:
            row = table.row()
            row.cell(pdf.t(k), style=FontFace(family=pdf.family, color=MUTED, size_pt=8.5))
            row.cell(pdf.t(v), style=FontFace(family=pdf.family, size_pt=9))

    # ---------- human-review rule ----------
    pdf.heading("Human-review rule")
    th = settings.hitl_threshold
    pdf.para(f"A ticket goes to a human reviewer when the confidences checked by the active mode are below "
             f"{th:.0%}. NOUL confidence = max(P(critical), 1 − P(critical)). Failed calls and invalid answers "
             f"always go to human review.")
    descriptions = {
        "team_or_critical": f"team confidence < {th:.0%} OR NOUL confidence < {th:.0%}",
        "team": f"team confidence < {th:.0%}",
        "critical": f"NOUL confidence < {th:.0%}",
    }
    with pdf.table(col_widths=(10, 50, 98, 20), **pdf.table_style()) as table:
        head = table.row()
        for h in ("#", "Mode", "Human review when", "Default"):
            head.cell(h)
        for i, (mode, name) in enumerate(HITL_MODES.items(), 1):
            active = mode == settings.hitl_mode
            style = FontFace(family=pdf.family, emphasis="BOLD" if active else None, size_pt=9)
            row = table.row()
            row.cell(str(i), style=style)
            row.cell(pdf.t(f"{name}\n{mode}"), style=style)
            row.cell(pdf.t(descriptions[mode]), style=style)
            row.cell("default" if active else "", style=style)
    pdf.ln(1)
    pdf.para("The default comes from HITL_MODE in .env; each run can choose its own mode in the UI.", size=8.5, color=MUTED)

    # ---------- teams ----------
    pdf.heading("1. Teams", "choice decision")
    pdf.para(f"Question: {tax.teams_cfg.get('instructions', 'Which support team should own this ticket?')}", bold=True)
    pdf.para("The model returns one team, its confidence and the probability of every team.", size=8.5, color=MUTED)
    with pdf.table(col_widths=(42, 72, 64), **pdf.table_style()) as table:
        head = table.row()
        for h in ("Team", "Owns", "Examples"):
            head.cell(h)
        for team in tax.teams:
            row = table.row()
            row.cell(pdf.t(f"{team['name']}\n{team['id']}"), style=FontFace(family=pdf.family, emphasis="BOLD", size_pt=9))
            row.cell(pdf.t(team["description"]), style=FontFace(family=pdf.family, size_pt=9))
            row.cell(pdf.t("\n".join(f"• {e}" for e in team.get("examples", []))), style=FontFace(family=pdf.family, size_pt=8.5, color=MUTED))

    # ---------- NOUL ----------
    noul = tax.noul
    pdf.heading("2. NOUL — criticality", "noul decision")
    pdf.para(f"Question: {noul['instructions']}", bold=True)
    pdf.para(f"The model returns P(critical). The ticket is Critical when P(critical) ≥ {tax.noul_threshold:.0%}; "
             f"NOUL confidence = max(P, 1 − P).", size=8.5, color=MUTED)
    with pdf.table(col_widths=(30, 26, 42, 80), **pdf.table_style()) as table:
        head = table.row()
        for h in ("Outcome", "Response SLA", "Action", "Criteria"):
            head.cell(h)
        for key in ("true", "false"):
            out = noul["outcomes"][key]
            fill, ink = NOUL_FILL[key]
            row = table.row()
            row.cell(pdf.t(out["name"]), style=FontFace(family=pdf.family, emphasis="BOLD", color=ink, fill_color=fill, size_pt=9))
            row.cell(pdf.t(out["response_sla"]), style=FontFace(family=pdf.family, size_pt=9))
            row.cell(pdf.t(out["action"]), style=FontFace(family=pdf.family, size_pt=9))
            row.cell(pdf.t(noul["criteria"][key]), style=FontFace(family=pdf.family, size_pt=8.5))

    # ---------- severity ----------
    sev = tax.severity
    pdf.heading("3. Severity", "score decision")
    pdf.para(f"Question: {sev['instructions']}", bold=True)
    pdf.para("Levels are ordered from least to most severe. The model returns a probability-weighted position "
             f"on the scale (0 = {sev['levels'][0]['name']} … {len(sev['levels']) - 1} = {sev['levels'][-1]['name']}) "
             "and a confidence; the nearest level is reported.", size=8.5, color=MUTED)
    if sev.get("scoring_factors"):
        pdf.para("Scoring factors:", size=9, bold=True, gap=0.5)
        pdf.bullets(sev["scoring_factors"], size=9)
    with pdf.table(col_widths=(12, 34, 132), **pdf.table_style()) as table:
        head = table.row()
        for h in ("#", "Level", "Definition"):
            head.cell(h)
        for i, lvl in enumerate(sev["levels"]):
            fill, ink = SEVERITY_FILL[min(i, len(SEVERITY_FILL) - 1)]
            row = table.row()
            row.cell(str(i), style=FontFace(family=pdf.family, size_pt=9))
            row.cell(pdf.t(lvl["name"]), style=FontFace(family=pdf.family, emphasis="BOLD", color=ink, fill_color=fill, size_pt=9))
            row.cell(pdf.t(lvl["definition"]), style=FontFace(family=pdf.family, size_pt=9))

    # ---------- appendix: exact questions ----------
    pdf.add_page()
    pdf.heading("Appendix — questions sent to the models")
    pdf.para("Body fragment of every POST /api/alpha/decisions request (the ticket goes in \"state\").", size=8.5, color=MUTED)
    pdf.set_font("Courier", "", 7.5)
    pdf.set_text_color(*INK)
    text = json.dumps(build_questions(tax), indent=2, ensure_ascii=False)
    ascii_text = text.translate(ASCII_MAP).encode("latin-1", "replace").decode("latin-1")  # Courier is Latin-1 only
    pdf.multi_cell(0, 3.4, ascii_text, align="L")

    return bytes(pdf.output())
