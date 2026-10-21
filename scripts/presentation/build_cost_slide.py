"""Builds docs/cost_slide.pptx: a single, fully editable 16:9 slide with the expected system cost.

Mirrors docs/cost_slide.html. Every number comes from COST_MODEL / the tables below, so update
them here and re-run:

    python scripts/presentation/build_cost_slide.py   # requires python-pptx==1.0.2

Layout is authored on a 1920x1080 "px" grid (same as the HTML) and converted to EMU.
"""

from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

OUT = Path(__file__).resolve().parent.parent.parent / "docs" / "cost_slide.pptx"

# --------------------------------------------------------------------------------------
# Cost model (list prices, us-central1, Oct 2026). Modeled estimate, +/-40%.
# --------------------------------------------------------------------------------------
BREAKDOWN = [  # (label, $ per AI turn, colour)
    ("Gemini 2.5 Pro \u00b7 FAQ agent (8K in / 2K out)", 0.0300, "8B5CF6"),
    ("Agent Engine sessions (~7 events)", 0.0018, "22D3EE"),
    ("Agent Engine compute (4 vCPU, ~10 s)", 0.0011, "38BDF8"),
    ("Gemini 2.5 Flash \u00b7 sentiment agent", 0.0011, "34D399"),
    ("Vertex AI Search (60% of turns)", 0.0009, "FBBF24"),
    ("Cloud Run \u00b7 Trace \u00b7 Logging", 0.0003, "94A3B8"),
]
PER_TURN = sum(c for _, c, _ in BREAKDOWN)                       # $0.0352
AI_TURNS_PER_TICKET = 0.85 * 2.5                                  # 15% urgent go straight to a human
PER_TICKET = AI_TURNS_PER_TICKET * PER_TURN + 0.15 * 0.0018       # ~$0.075
PER_1K = round(PER_TICKET * 1000)                                 # $75, used for all displayed volumes
FIXED_PROTO, FIXED_PROD = 29, 230
VOLUMES = [(1_000, "Pilot", FIXED_PROTO), (10_000, "Launch", FIXED_PROTO), (100_000, "Scale", FIXED_PROD)]


def monthly_total(volume, fixed):
    """Variable + fixed, rounded to the nearest $5 (the model is +/-40%, so no false precision)."""
    return 5 * round((volume / 1000 * PER_1K + fixed) / 5)

ASSUMPTIONS = [
    ("3", " customer messages / ticket"),
    ("15%", " urgent \u2192 human on 1st message"),
    ("2.5", " AI turns per non-urgent ticket"),
    ("", "Follow-ups on escalated tickets skip the LLM"),
    ("", "Pro $1.25 / $10, Flash $0.30 / $2.50 per 1M tokens"),
    ("", "Search $1.50 / 1K \u00b7 Sessions $0.25 / 1K events"),
    ("", "No free tiers or committed-use discounts"),
]
RISKS = [
    ("Agent Engine idle billing at GA: ", "+$557 / mo"),
    ("Nightly eval runs: ", "~$105 / mo"),
    ("Pro thinking tokens can 2\u00d7 on hard queries", ""),
    ("Token counts not yet measured (eval M3/M4)", ""),
]

# --------------------------------------------------------------------------------------
# Design tokens
# --------------------------------------------------------------------------------------
SANS, MONO = "Calibri", "Consolas"
C = {
    "bg": "070B14", "panel": "10141D", "border": "1E232D", "text": "E8ECF5", "muted": "8B95AB",
    "dim": "5D6780", "pro": "C084FC", "lav": "C4B5FD", "cyan": "22D3EE", "sky": "38BDF8",
    "warn": "FBBF24", "warn_bg": "1B1A12", "bad": "FB7185", "white": "FFFFFF",
}
PX = 6350  # EMU per px on a 1920x1080 grid spanning 13.333in x 7.5in


def px(v):
    return Emu(int(round(v * PX)))


def rgb(hex_):
    return RGBColor.from_string(hex_)


def shape(slide, x, y, w, h, fill, line=None, radius=0, kind=None):
    kind = kind or (MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE)
    s = slide.shapes.add_shape(kind, px(x), px(y), px(w), px(h))
    if radius:
        s.adjustments[0] = min(0.5, radius / min(w, h))
    s.fill.solid()
    s.fill.fore_color.rgb = rgb(fill)
    if line:
        s.line.color.rgb = rgb(line)
        s.line.width = Pt(0.75)
    else:
        s.line.fill.background()
    s.shadow.inherit = False
    return s


def _style_run(run, size, color, bold=False, font=SANS, spacing=None):
    run.font.size = Pt(size * 0.5)  # px -> pt on this grid
    run.font.bold = bold
    run.font.name = font
    run.font.color.rgb = rgb(color)
    if spacing:
        run._r.get_or_add_rPr().set("spc", str(spacing))


def text(slide, x, y, w, h, paragraphs, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP,
         wrap=True, line_spacing=None):
    """paragraphs: list of paragraphs; each paragraph is a list of
    (text, size_px, color, bold, font, spacing) tuples (trailing items optional)."""
    tb = slide.shapes.add_textbox(px(x), px(y), px(w), px(h))
    tf = tb.text_frame
    tf.word_wrap = wrap
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    fill_frame(tf, paragraphs, align, line_spacing)
    return tb


def fill_frame(tf, paragraphs, align=PP_ALIGN.LEFT, line_spacing=None):
    for i, para in enumerate(paragraphs):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        if line_spacing:
            p.line_spacing = line_spacing
        for spec in para:
            t, size, color, *rest = spec
            r = p.add_run()
            r.text = t
            _style_run(r, size, color, *rest)


def money(v):
    return f"${v:,.0f}"


def set_cell_borders(cell, bottom=None):
    """Removes all cell borders except an optional thin bottom rule. Inserted before any fill
    element to respect the a:tcPr schema order (lnL, lnR, lnT, lnB, ..., fill)."""
    tcPr = cell._tc.get_or_add_tcPr()
    for i, tag in enumerate(("a:lnL", "a:lnR", "a:lnT", "a:lnB")):
        ln = etree.Element(qn(tag))
        if tag == "a:lnB" and bottom:
            ln.set("w", "9525")
            fill = etree.SubElement(ln, qn("a:solidFill"))
            etree.SubElement(fill, qn("a:srgbClr")).set("val", bottom)
        else:
            ln.set("w", "0")
            etree.SubElement(ln, qn("a:noFill"))
        tcPr.insert(i, ln)


# --------------------------------------------------------------------------------------
# Slide sections
# --------------------------------------------------------------------------------------
def background(slide):
    bg = shape(slide, 0, 0, 1920, 1080, C["bg"])
    bg.fill.gradient()
    bg.fill.gradient_angle = 35
    stops = bg.fill.gradient_stops
    stops[0].color.rgb, stops[0].position = rgb("1A1338"), 0.0
    stops[1].color.rgb, stops[1].position = rgb("07131C"), 1.0
    # soft dark vignette band behind content keeps contrast even
    veil = shape(slide, 0, 0, 1920, 1080, C["bg"])
    veil.fill.gradient()
    veil.fill.gradient_angle = 90
    vs = veil.fill.gradient_stops
    vs[0].color.rgb, vs[0].position = rgb("0B0C1C"), 0.0
    vs[1].color.rgb, vs[1].position = rgb("070B14"), 1.0
    _set_alpha(veil, 55)


def _set_alpha(shp, pct_opaque):
    """Applies uniform transparency to every gradient stop of a shape."""
    for clr in shp.fill._xPr.iter(qn("a:srgbClr")):
        etree.SubElement(clr, qn("a:alpha")).set("val", str(pct_opaque * 1000))


def header(slide):
    text(slide, 72, 52, 1100, 26, [[("FDE SUPPORT PLATFORM \u00b7 COST ENGINEERING", 17, C["pro"], True, SANS, 300)]])
    text(slide, 72, 82, 1250, 74, [[
        ("Expected cost: ", 56, C["white"], True),
        (f"~${PER_TICKET:.3f} per support ticket", 56, C["lav"], True),
    ]], wrap=False)
    # badges (right aligned)
    for x, w, label, fg, bg, border in (
        (1312, 300, "List prices \u00b7 us-central1 \u00b7 Oct 2026", C["muted"], C["panel"], C["border"]),
        (1626, 222, "Modeled estimate \u00b7 \u00b140%", C["warn"], C["warn_bg"], "4A3F1A"),
    ):
        b = shape(slide, x, 112, w, 40, bg, border, radius=20)
        tf = b.text_frame
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        fill_frame(tf, [[(label, 15, fg, True)]], PP_ALIGN.CENTER)


def kpis(slide):
    cards = [
        ("VARIABLE COST", f"${PER_1K}", " / 1K tickets", C["pro"],
         [(f"~{AI_TURNS_PER_TICKET:.1f} AI-answered messages per ticket at ${PER_TURN:.3f} each", 18, C["text"])]),
        ("FIXED PLATFORM COST", money(FIXED_PROTO), " / month", C["sky"],
         [("Prototype. Production with HA Cloud SQL: ", 18, C["text"]), (f"~{money(FIXED_PROD)} / mo", 18, C["white"], True)]),
        ("AT LAUNCH VOLUME", money(monthly_total(10_000, FIXED_PROTO)), " / month", C["cyan"],
         [("10,000 tickets / month, all-in", 18, C["text"])]),
    ]
    x, y, w, h, gap = 72, 184, 576, 168, 24
    for label, value, unit, accent, note in cards:
        shape(slide, x, y, w, h, C["panel"], C["border"], radius=16)
        shape(slide, x + 1, y + 16, 5, h - 32, accent)  # accent rule
        text(slide, x + 30, y + 24, w - 50, 24, [[(label, 16, C["muted"], True, SANS, 150)]])
        text(slide, x + 30, y + 48, w - 50, 76, [[(value, 64, accent, True), (unit, 24, C["muted"], True)]],
             anchor=MSO_ANCHOR.BOTTOM)
        text(slide, x + 30, y + 128, w - 50, 28, [note])
        x += w + gap


def breakdown_panel(slide):
    x, y, w, h = 72, 380, 937, 480
    shape(slide, x, y, w, h, C["panel"], C["border"], radius=16)
    ix, iw = x + 32, w - 64
    text(slide, ix, y + 26, iw, 32, [[(f"Where each AI turn's ${PER_TURN:.3f} goes", 25, C["text"], True)]])
    text(slide, ix, y + 62, iw, 24, [[("One customer message answered by the AI (sentiment agent + FAQ agent)", 16.5, C["muted"])]])

    # stacked bar (sum of segment widths = iw)
    bx, by, bh = ix, y + 104, 44
    for i, (_, cost, color) in enumerate(BREAKDOWN):
        seg = iw * cost / PER_TURN if i < len(BREAKDOWN) - 1 else ix + iw - bx
        shape(slide, bx, by, seg, bh, color)
        bx += seg

    # legend
    ry, row_h = y + 170, 36
    for label, cost, color in BREAKDOWN:
        shape(slide, ix, ry + 10, 15, 15, color, radius=3)
        text(slide, ix + 30, ry, 560, row_h, [[(label, 19.5, C["text"])]], anchor=MSO_ANCHOR.MIDDLE)
        text(slide, ix + iw - 250, ry, 150, row_h, [[(f"${cost:.4f}", 18, C["text"], False, MONO)]],
             align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)
        text(slide, ix + iw - 90, ry, 90, row_h, [[(f"{cost / PER_TURN:.0%}", 18, C["muted"], False, MONO)]],
             align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)
        ry += row_h
    shape(slide, ix, ry + 8, iw, 1.5, C["border"])
    ry += 16
    text(slide, ix + 30, ry, 560, row_h, [[("Total per AI turn", 19.5, C["white"], True)]], anchor=MSO_ANCHOR.MIDDLE)
    text(slide, ix + iw - 250, ry, 150, row_h, [[(f"${PER_TURN:.4f}", 18, C["white"], True, MONO)]],
         align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)
    text(slide, ix + iw - 90, ry, 90, row_h, [[("100%", 18, C["muted"], True, MONO)]],
         align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)


def monthly_panel(slide):
    x, y, w, h = 1033, 380, 815, 480
    shape(slide, x, y, w, h, C["panel"], C["border"], radius=16)
    ix, iw = x + 32, w - 64
    text(slide, ix, y + 26, iw, 32, [[("Expected monthly cost", 25, C["text"], True)]])
    text(slide, ix, y + 62, iw, 24, [[("Variable + fixed. Production infrastructure assumed at 100K", 16.5, C["muted"])]])

    # native, editable table
    col_w = [300, 150, 130, 171]
    header_h, row_h = 36, 64
    gf = slide.shapes.add_table(1 + len(VOLUMES), 4, px(ix), px(y + 102), px(iw), px(header_h + row_h * len(VOLUMES)))
    tbl = gf.table
    tbl.first_row = tbl.horz_banding = False
    tbl._tbl.tblPr.find(qn("a:tableStyleId")).text = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"  # No Style, No Grid
    for i, cw in enumerate(col_w):
        tbl.columns[i].width = px(cw)
    tbl.rows[0].height = px(header_h)
    for r in range(1, len(VOLUMES) + 1):
        tbl.rows[r].height = px(row_h)

    def put(r, c, paragraphs):
        cell = tbl.cell(r, c)
        set_cell_borders(cell, bottom=C["border"] if r < len(VOLUMES) else None)
        cell.fill.solid()
        cell.fill.fore_color.rgb = rgb(C["panel"])
        cell.margin_left = cell.margin_right = 0
        cell.margin_top = cell.margin_bottom = 0
        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        tf = cell.text_frame
        tf.word_wrap = True
        fill_frame(tf, paragraphs, PP_ALIGN.LEFT if c == 0 else PP_ALIGN.RIGHT)

    for c, head in enumerate(("TICKETS / MONTH", "VARIABLE", "FIXED", "TOTAL")):
        put(0, c, [[(head, 14.5, C["muted"], True, SANS, 120)]])
    for r, (vol, tag, fixed) in enumerate(VOLUMES, start=1):
        variable = vol / 1000 * PER_1K
        total = monthly_total(vol, fixed)
        put(r, 0, [[(f"{vol:,}", 22, C["text"], True)], [(tag, 15, C["muted"])]])
        put(r, 1, [[(money(variable), 20, C["muted"], False, MONO)]])
        put(r, 2, [[(money(fixed), 20, C["muted"], False, MONO)]])
        put(r, 3, [[(money(total), 27, C["pro"], True, MONO)]])

    # fixed-cost detail boxes
    by, bh, bw = y + h - 28 - 112, 112, (iw - 14) / 2
    for i, (tag, value, desc) in enumerate((
        ("FIXED \u00b7 PROTOTYPE", f"{money(FIXED_PROTO)} / mo", "IAP LB $18 \u00b7 SQL f1-micro $9 \u00b7 misc $2"),
        ("FIXED \u00b7 PRODUCTION", f"{money(FIXED_PROD)} / mo", "HA SQL $197 \u00b7 LB $18 \u00b7 Cloud Run $10"),
    )):
        bx = ix + i * (bw + 14)
        shape(slide, bx, by, bw, bh, "141820", C["border"], radius=12)
        text(slide, bx + 16, by + 12, bw - 32, bh - 20, [
            [(tag, 12.5, C["dim"], True, SANS, 120)],
            [(value, 22, C["text"], True, MONO)],
            [(desc, 15, C["muted"])],
        ])


def footer(slide):
    y, h = 888, 140
    # assumptions
    x, w = 72, 1051
    shape(slide, x, y, w, h, "0D1119", C["border"], radius=14)
    text(slide, x + 24, y + 16, 400, 20, [[("KEY ASSUMPTIONS", 14, C["muted"], True, SANS, 200)]])
    cols = (ASSUMPTIONS[:4], ASSUMPTIONS[4:])
    for ci, items in enumerate(cols):
        paras = [[("\u25b8  ", 15, C["dim"]), (b, 16, C["white"], True), (rest, 16, C["text"])] for b, rest in items]
        text(slide, x + 24 + ci * 500, y + 44, 490, h - 52, paras, line_spacing=1.12)
    # risks
    x, w = 1147, 701
    shape(slide, x, y, w, h, "0D1119", C["border"], radius=14)
    text(slide, x + 24, y + 16, 400, 20, [[("COST RISKS", 14, C["bad"], True, SANS, 200)]])
    paras = [[("\u25b8  ", 15, C["bad"]), (a, 16, C["text"]), (b, 16, C["white"], True)] for a, b in RISKS]
    text(slide, x + 24, y + 44, w - 48, h - 52, paras, line_spacing=1.12)


NOTES = f"""Expected cost of the FDE Support platform (modeled estimate, +/-40%, list prices us-central1, Oct 2026).

Per AI turn = ${PER_TURN:.4f}: Gemini 2.5 Pro FAQ agent (~8K input / 2K output tokens incl. thinking) $0.0300, Agent Engine sessions (~7 events at $0.25/1K) $0.0018, Agent Engine compute (4 vCPU / 4 GiB, ~10 s busy) $0.0011, Gemini 2.5 Flash sentiment (~600 in / 350 out) $0.0011, Vertex AI Search ($1.50/1K x 60% of turns) $0.0009, Cloud Run + Trace + Logging $0.0003.

Per ticket = 85% non-urgent x 2.5 AI turns x ${PER_TURN:.4f} + 15% urgent x sentiment-only = ~${PER_TICKET:.3f} (${PER_TICKET * 1000:.0f} per 1K tickets).

Fixed: prototype ${FIXED_PROTO}/mo (IAP external HTTPS load balancer $18.25, Cloud SQL db-f1-micro + 10 GB $9.37, registry/GCS/secrets ~$1-2). Production ${FIXED_PROD}/mo (HA Cloud SQL db-custom-2-7680 ~$197, LB, Cloud Run backend min-instance ~$10).

Risks: Agent Engine idle instances are not billed in Preview; at GA the defaults (min 1 instance, 4 vCPU / 4 GiB, x2 agents) would add ~$557/mo. Nightly 100-case eval on 2.5 Pro + judge ~$105/mo. Token counts are estimates until eval metrics M3/M4 record real usage_metadata.
"""


def build():
    prs = Presentation()
    prs.slide_width, prs.slide_height = px(1920), px(1080)
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    background(slide)
    header(slide)
    kpis(slide)
    breakdown_panel(slide)
    monthly_panel(slide)
    footer(slide)
    slide.notes_slide.notes_text_frame.text = NOTES
    OUT.parent.mkdir(parents=True, exist_ok=True)
    prs.save(OUT)
    print(f"Wrote {OUT}  (per turn ${PER_TURN:.4f}, per ticket ${PER_TICKET:.4f})")


if __name__ == "__main__":
    build()
