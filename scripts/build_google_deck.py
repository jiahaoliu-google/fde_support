"""Builds docs/fde_support_deck_google.pptx: the cost slide and the LLMOps slide in Google's visual
style (white canvas, Google brand colours, Roboto, light Material cards). Imports cleanly into
Google Slides (File > Import slides) and opens in PowerPoint / Keynote.

    python scripts/build_google_deck.py            # requires python-pptx==1.0.2

Numbers and content come from build_cost_slide.py and build_llmops_slide.py.
"""

from pathlib import Path

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Pt

import build_cost_slide as cost
import build_llmops_slide as ops
from build_cost_slide import px, rgb, set_cell_borders

OUT = Path(__file__).resolve().parent.parent / "docs" / "fde_support_deck_google.pptx"

SANS, MONO = "Roboto", "Roboto Mono"
G = {
    "blue": "1A73E8", "red": "EA4335", "yellow": "FBBC04", "green": "34A853",
    "blue_lt": "8AB4F8", "grey_lt": "BDC1C6", "text": "202124", "muted": "5F6368",
    "border": "DADCE0", "card": "F8F9FA", "white": "FFFFFF", "blue_bg": "E8F0FE",
    "yellow_bg": "FEF7E0", "yellow_dk": "B06000", "red_bg": "FCE8E6", "red_dk": "C5221F",
    "green_bg": "E6F4EA", "green_dk": "137333", "grey_bg": "F1F3F4",
}


# --------------------------------------------------------------------------------------
# Helpers (light theme)
# --------------------------------------------------------------------------------------
def box(slide, x, y, w, h, fill, line=None, radius=0, kind=None):
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


def runs(tf, paragraphs, align=PP_ALIGN.LEFT, line_spacing=None):
    for i, para in enumerate(paragraphs):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        if line_spacing:
            p.line_spacing = line_spacing
        for spec in para:
            t, size, color, *rest = spec
            bold = rest[0] if len(rest) > 0 else False
            font = rest[1] if len(rest) > 1 else SANS
            r = p.add_run()
            r.text = t
            r.font.size = Pt(size * 0.5)
            r.font.bold = bold
            r.font.name = font
            r.font.color.rgb = rgb(color)


def text(slide, x, y, w, h, paragraphs, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, wrap=True, line_spacing=None):
    tb = slide.shapes.add_textbox(px(x), px(y), px(w), px(h))
    tf = tb.text_frame
    tf.word_wrap = wrap
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    runs(tf, paragraphs, align, line_spacing)
    return tb


def pill(slide, x, y, w, h, label, fg, bg, size=15):
    b = box(slide, x, y, w, h, bg, radius=h / 2)
    tf = b.text_frame
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    runs(tf, [[(label, size, fg, True)]], PP_ALIGN.CENTER)


def card(slide, x, y, w, h):
    return box(slide, x, y, w, h, G["white"], G["border"], radius=12)


def slide_frame(slide, eyebrow, title, subtitle, page):
    box(slide, 0, 0, 1920, 1080, G["white"])
    # Google four-colour accent bar
    for i, c in enumerate((G["blue"], G["red"], G["yellow"], G["green"])):
        box(slide, 72 + i * 22, 56, 18, 6, c, radius=3)
    text(slide, 72, 76, 1200, 24, [[(eyebrow, 17, G["muted"], True)]])
    text(slide, 72, 104, 1400, 64, [title], wrap=False)
    if subtitle:
        text(slide, 72, 168, 1300, 28, [[(subtitle, 19, G["muted"])]])
    # footer
    box(slide, 72, 1040, 1776, 1, G["border"])
    text(slide, 72, 1048, 800, 20, [[("FDE Support Platform", 13.5, G["muted"])]])
    text(slide, 1648, 1048, 200, 20, [[(str(page), 13.5, G["muted"])]], align=PP_ALIGN.RIGHT)


def table(slide, x, y, w, col_w, header, rows, header_h, row_h, aligns):
    gf = slide.shapes.add_table(1 + len(rows), len(col_w), px(x), px(y), px(w), px(header_h + row_h * len(rows)))
    tbl = gf.table
    tbl.first_row = tbl.horz_banding = False
    tbl._tbl.tblPr.find(qn("a:tableStyleId")).text = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"
    for i, cw in enumerate(col_w):
        tbl.columns[i].width = px(cw)
    tbl.rows[0].height = px(header_h)
    for r in range(1, len(rows) + 1):
        tbl.rows[r].height = px(row_h)
    for r, row in enumerate([header] + rows):
        for c, paragraphs in enumerate(row):
            cell = tbl.cell(r, c)
            set_cell_borders(cell, bottom=G["border"] if r < len(rows) else None)
            cell.fill.solid()
            cell.fill.fore_color.rgb = rgb(G["white"])
            cell.margin_left = cell.margin_right = cell.margin_top = cell.margin_bottom = 0
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.text_frame.word_wrap = True
            runs(cell.text_frame, paragraphs, aligns[c])


def money(v):
    return f"${v:,.0f}"


# --------------------------------------------------------------------------------------
# Slide 1: expected cost
# --------------------------------------------------------------------------------------
COST_COLORS = [G["blue"], G["green"], G["yellow"], G["red"], G["blue_lt"], G["grey_lt"]]


def cost_slide(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    slide_frame(s, "COST ENGINEERING", [
        ("Expected cost: ", 52, G["text"], True),
        (f"~${cost.PER_TICKET:.3f} per support ticket", 52, G["blue"], True),
    ], "Modeled at list prices (us-central1, Oct 2026) from the current architecture.", 1)
    pill(s, 1440, 118, 196, 38, "Estimate \u00b7 \u00b140%", G["yellow_dk"], G["yellow_bg"])
    pill(s, 1652, 118, 196, 38, "Current config", G["blue"], G["blue_bg"])

    # KPI cards
    kpis = [
        ("Variable cost", f"${cost.PER_1K}", "per 1K tickets", G["blue"],
         f"~{cost.AI_TURNS_PER_TICKET:.1f} AI-answered messages per ticket at ${cost.PER_TURN:.3f} each"),
        ("Fixed platform cost", money(cost.FIXED_PROTO), "per month", G["green"],
         f"Prototype. Production with HA Cloud SQL: ~{money(cost.FIXED_PROD)} / mo"),
        ("At launch volume", money(cost.monthly_total(10_000, cost.FIXED_PROTO)), "per month", G["red"],
         "10,000 tickets / month, all-in"),
    ]
    x, y, w, h = 72, 220, 576, 164
    for label, value, unit, accent, note in kpis:
        card(s, x, y, w, h)
        box(s, x, y + 20, 6, h - 40, accent)
        text(s, x + 32, y + 22, w - 56, 24, [[(label, 17, G["muted"], True)]])
        text(s, x + 32, y + 46, w - 56, 72, [[(value, 62, G["text"], True), ("  " + unit, 20, G["muted"])]],
             anchor=MSO_ANCHOR.BOTTOM)
        text(s, x + 32, y + 124, w - 56, 26, [[(note, 17, G["muted"])]])
        x += w + 24

    # Breakdown card
    x, y, w, h = 72, 408, 937, 452
    card(s, x, y, w, h)
    ix, iw = x + 32, w - 64
    text(s, ix, y + 26, iw, 32, [[(f"Where each AI turn's ${cost.PER_TURN:.3f} goes", 25, G["text"], True)]])
    text(s, ix, y + 62, iw, 24, [[("One customer message answered by the AI (sentiment agent + FAQ agent)", 16.5, G["muted"])]])
    bx = ix
    for i, (_, c, _) in enumerate(cost.BREAKDOWN):
        seg = iw * c / cost.PER_TURN if i < len(cost.BREAKDOWN) - 1 else ix + iw - bx
        box(s, bx, y + 102, seg, 36, COST_COLORS[i])
        bx += seg
    ry = y + 156
    for i, (label, c, _) in enumerate(cost.BREAKDOWN):
        box(s, ix, ry + 10, 14, 14, COST_COLORS[i], radius=7)
        text(s, ix + 28, ry, 560, 34, [[(label, 19, G["text"])]], anchor=MSO_ANCHOR.MIDDLE)
        text(s, ix + iw - 250, ry, 150, 34, [[(f"${c:.4f}", 17.5, G["text"], False, MONO)]],
             align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)
        text(s, ix + iw - 90, ry, 90, 34, [[(f"{c / cost.PER_TURN:.0%}", 17.5, G["muted"], False, MONO)]],
             align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)
        ry += 34
    box(s, ix, ry + 8, iw, 1, G["border"])
    ry += 16
    text(s, ix + 28, ry, 560, 34, [[("Total per AI turn", 19, G["text"], True)]], anchor=MSO_ANCHOR.MIDDLE)
    text(s, ix + iw - 250, ry, 150, 34, [[(f"${cost.PER_TURN:.4f}", 17.5, G["text"], True, MONO)]],
         align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)
    text(s, ix + iw - 90, ry, 90, 34, [[("100%", 17.5, G["muted"], True, MONO)]],
         align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)

    # Monthly card
    x, w = 1033, 815
    card(s, x, y, w, h)
    ix, iw = x + 32, w - 64
    text(s, ix, y + 26, iw, 32, [[("Expected monthly cost", 25, G["text"], True)]])
    text(s, ix, y + 62, iw, 24, [[("Variable + fixed. Production infrastructure assumed at 100K", 16.5, G["muted"])]])
    head = [[[(t, 14, G["muted"], True)]] for t in ("Tickets / month", "Variable", "Fixed", "Total")]
    rows = []
    for vol, tag, fixed in cost.VOLUMES:
        rows.append([
            [[(f"{vol:,}", 21, G["text"], True)], [(tag, 14.5, G["muted"])]],
            [[(money(vol / 1000 * cost.PER_1K), 19, G["muted"], False, MONO)]],
            [[(money(fixed), 19, G["muted"], False, MONO)]],
            [[(money(cost.monthly_total(vol, fixed)), 26, G["blue"], True, MONO)]],
        ])
    table(s, ix, y + 100, iw, [300, 150, 130, 171], head, rows, 34, 62,
          [PP_ALIGN.LEFT, PP_ALIGN.RIGHT, PP_ALIGN.RIGHT, PP_ALIGN.RIGHT])
    by, bw = y + h - 24 - 104, (iw - 14) / 2
    for i, (tag, value, desc) in enumerate((
        ("FIXED \u00b7 PROTOTYPE", f"{money(cost.FIXED_PROTO)} / mo", "IAP LB $18 \u00b7 SQL f1-micro $9 \u00b7 misc $2"),
        ("FIXED \u00b7 PRODUCTION", f"{money(cost.FIXED_PROD)} / mo", "HA SQL $197 \u00b7 LB $18 \u00b7 Cloud Run $10"),
    )):
        bx = ix + i * (bw + 14)
        box(s, bx, by, bw, 104, G["card"], radius=10)
        text(s, bx + 16, by + 12, bw - 32, 84, [
            [(tag, 12.5, G["muted"], True)],
            [(value, 22, G["text"], True, MONO)],
            [(desc, 15, G["muted"])],
        ])

    # Assumptions + risks
    y, h = 884, 140
    box(s, 72, y, 1051, h, G["card"], radius=12)
    text(s, 96, y + 16, 400, 20, [[("Key assumptions", 15, G["text"], True)]])
    for ci, items in enumerate((cost.ASSUMPTIONS[:4], cost.ASSUMPTIONS[4:])):
        paras = [[("\u2022  ", 15, G["blue"], True), (b, 15.5, G["text"], True), (r, 15.5, G["muted"])] for b, r in items]
        text(s, 96 + ci * 500, y + 46, 490, h - 54, paras, line_spacing=1.1)
    box(s, 1147, y, 701, h, G["red_bg"], radius=12)
    text(s, 1171, y + 16, 400, 20, [[("Cost risks", 15, G["red_dk"], True)]])
    paras = [[("\u2022  ", 15, G["red"], True), (a, 15.5, G["text"]), (b, 15.5, G["red_dk"], True)] for a, b in cost.RISKS]
    text(s, 1171, y + 46, 653, h - 54, paras, line_spacing=1.1)

    s.notes_slide.notes_text_frame.text = cost.NOTES


# --------------------------------------------------------------------------------------
# Slide 2: LLMOps, evaluation framework & observability
#   Layout: release-flywheel chevron band on top, then three tonal panels:
#   golden-set donut (built from block arcs, so it stays editable after a Google Slides
#   import), gate tiles with PR / Nightly chips, observability tiles with product chips.
# --------------------------------------------------------------------------------------
GOLDEN_SHORT = ["In-corpus questions", "Urgent / frustrated", "Vague, single turn", "Multi-turn", "Out of corpus"]
GOLDEN_COLORS = [G["blue"], G["red"], G["yellow"], G["green"], G["grey_lt"]]
TONES = {  # name -> (background, foreground)
    "blue": (G["blue_bg"], G["blue"]), "red": (G["red_bg"], G["red_dk"]),
    "yellow": (G["yellow_bg"], G["yellow_dk"]), "green": (G["green_bg"], G["green_dk"]),
    "grey": (G["grey_bg"], G["muted"]),
}
FLOW_TONES = ["grey", "blue", "red", "yellow", "green", "blue"]
SCORER_TONES = ["blue", "red", "green"]
OBS_TILES = [  # (title, product chip, detail, tone)
    ("Distributed tracing", "Cloud Trace", "OpenTelemetry on FastAPI + httpx; P95 / P99 per hop", "blue"),
    ("Structured logs", "BigQuery", "Cloud Logging sink \u2192 Agent Analytics: routes, tokens, cost", "green"),
    ("Online quality sampling", "Gemini judge", "Async LLM judge on sampled live traces; escalations, CSAT", "yellow"),
    ("PII-safe telemetry", "Model Armor", "Cloud DLP redacts input before agents and logs", "red"),
    ("Resilience signals", "Cloud Monitoring", "Circuit breaker (3 failures / 30 s); 4xx, 5xx, 429 counts", "blue"),
    ("Alerting", "PagerDuty", "SLO breaches page the on-call engineer", "grey"),
]


def chip_w(label, size=13):
    return 24 + len(label) * size * 0.62


def tonal_panel(s, x, y, w, h, tag, tone, title, sub):
    bg, fg = TONES[tone]
    box(s, x, y, w, h, G["card"], radius=16)
    pill(s, x + 28, y + 24, chip_w(tag), 28, tag, fg, bg, 13)
    text(s, x + 28, y + 62, w - 56, 34, [[(title, 26, G["text"], True)]])
    text(s, x + 28, y + 100, w - 56, 24, [[(sub, 15.5, G["muted"])]])


def flow_band(s, y, h=92):
    n, gap, depth = len(ops.LIFECYCLE), 8, 28
    w = (1776 + (n - 1) * (depth - gap)) / n
    for i, (step, detail) in enumerate(ops.LIFECYCLE):
        bg, fg = TONES[FLOW_TONES[i]]
        shp = box(s, 72 + i * (w - depth + gap), y, w, h, bg,
                  kind=MSO_SHAPE.PENTAGON if i == 0 else MSO_SHAPE.CHEVRON)
        shp.adjustments[0] = depth / h
        tf = shp.text_frame
        tf.word_wrap = True
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        tf.margin_left = px(24 if i == 0 else depth + 20)
        tf.margin_right = px(depth + 6)
        tf.margin_top = tf.margin_bottom = 0
        runs(tf, [[(f"{i + 1}  ", 15, fg, True, MONO), (step, 19, G["text"], True)],
                  [(detail, 15, G["muted"])]])


def donut(s, cx, cy, d, ring, values, colors, gap_deg=1.5):
    """Doughnut from BLOCK_ARC shapes. OOXML angles are clockwise from 3 o'clock in 60000ths
    of a degree; python-pptx normalises adjustments by 1e5, hence the 0.6 factor."""
    total, start = sum(values), 270.0
    for v, color in zip(values, colors):
        span = 360.0 * v / total
        arc = box(s, cx - d / 2, cy - d / 2, d, d, color, kind=MSO_SHAPE.BLOCK_ARC)
        arc.adjustments[0] = ((start + gap_deg) % 360) * 0.6
        arc.adjustments[1] = ((start + span - gap_deg) % 360) * 0.6
        arc.adjustments[2] = ring / d
        start += span


def offline_panel(s, x, y, w, h):
    tonal_panel(s, x, y, w, h, "OFFLINE", "blue", "Evaluation framework",
                "Golden set: 100 tickets sourced from the RAG corpus")
    ix, iw = x + 28, w - 56
    d, top = 200, y + 140
    donut(s, ix + d / 2, top + d / 2, d, 30, [n for _, n, _, _ in ops.GOLDEN_SET], GOLDEN_COLORS)
    text(s, ix, top + d / 2 - 40, d, 48, [[("100", 44, G["text"], True)]],
         align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.BOTTOM)
    text(s, ix, top + d / 2 + 10, d, 22, [[("tickets", 16, G["muted"])]], align=PP_ALIGN.CENTER)

    lx = ix + d + 28
    lw = ix + iw - lx
    ly = top - 4
    for (_, n, _, purpose), label, color in zip(ops.GOLDEN_SET, GOLDEN_SHORT, GOLDEN_COLORS):
        box(s, lx, ly + 8, 12, 12, color, kind=MSO_SHAPE.OVAL)
        text(s, lx + 22, ly, lw - 60, 22, [[(label, 16.5, G["text"], True)]])
        text(s, lx + 22, ly + 22, lw - 60, 18, [[(purpose, 14, G["muted"])]])
        text(s, ix + iw - 40, ly, 40, 22, [[(str(n), 17, G["text"], True, MONO)]], align=PP_ALIGN.RIGHT)
        ly += 42

    ry = top + d + 32
    box(s, ix, ry, iw, 1, G["border"])
    text(s, ix, ry + 16, iw, 22, [[("How answers are scored", 16, G["text"], True)]])
    ry += 50
    for (title, detail), tone in zip(ops.SCORERS, SCORER_TONES):
        bg, fg = TONES[tone]
        box(s, ix, ry, iw, 72, bg, radius=10)
        text(s, ix + 18, ry + 12, iw - 36, 52, [[(title, 17.5, fg, True)], [(detail, 14.5, G["text"])]])
        ry += 82


def gates_panel(s, x, y, w, h):
    tonal_panel(s, x, y, w, h, "RELEASE GATES", "red", "Quality gates",
                "PR = every pull request, deterministic \u00b7 Nightly = LLM-judged")
    ix, iw = x + 28, w - 56
    tw, th, gx, gy = (iw - 16) / 2, 96, 16, 12
    gates = sorted(ops.GATES, key=lambda g: g[2] == "Nightly")  # nightly gates on the last row
    for k, (kpi, target, gate) in enumerate(gates):
        r, c = divmod(k, 2)
        tx, ty = ix + c * (tw + gx), y + 136 + r * (th + gy)
        bg, fg = TONES["blue" if gate == "PR" else "red"]
        box(s, tx, ty, tw, th, G["white"], radius=10)
        text(s, tx + 20, ty + 10, tw - 120, 46, [[(target, 32, fg, True, MONO)]], anchor=MSO_ANCHOR.BOTTOM)
        text(s, tx + 20, ty + 60, tw - 26, 24, [[(kpi, 16.5, G["text"])]])
        cw = chip_w(gate)
        pill(s, tx + tw - 18 - cw, ty + 16, cw, 26, gate, fg, bg, 13)


def online_panel(s, x, y, w, h):
    tonal_panel(s, x, y, w, h, "ONLINE", "green", "Observability", "Production telemetry on Google Cloud")
    ix, iw = x + 28, w - 56
    ty = y + 136
    for title, product, detail, tone in OBS_TILES:
        bg, fg = TONES[tone]
        box(s, ix, ty, iw, 80, G["white"], radius=10)
        box(s, ix, ty + 14, 5, 52, fg, radius=2)
        cw = chip_w(product, 12.5)
        pill(s, ix + iw - 16 - cw, ty + 12, cw, 24, product, fg, bg, 12.5)
        text(s, ix + 22, ty + 12, iw - 50 - cw, 24, [[(title, 17.5, G["text"], True)]])
        text(s, ix + 22, ty + 38, iw - 40, 38, [[(detail, 14.5, G["muted"])]])
        ty += 90


def llmops_slide(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    slide_frame(s, "OPERATIONS", [
        ("LLMOps, ", 50, G["text"], True),
        ("Evaluation Framework & Observability", 50, G["blue"], True),
    ], "Every change runs through the same flywheel: gated offline on a golden set, watched online in production.", 2)
    pill(s, 1476, 118, 372, 38, "Framework built \u00b7 baseline run pending", G["yellow_dk"], G["yellow_bg"])
    flow_band(s, 216)
    y, h = 332, 688
    offline_panel(s, 72, y, 520, h)
    gates_panel(s, 616, y, 688, h)
    online_panel(s, 1328, y, 520, h)
    s.notes_slide.notes_text_frame.text = ops.NOTES


# --------------------------------------------------------------------------------------
# Slide 3: roadmap & future of the system
#   Now / Next / Later come from docs/SYSTEM_ANALYSIS.md (section 4 status + section 5 roadmap);
#   Future is the forward-looking vision.
# --------------------------------------------------------------------------------------
ROADMAP = [  # (chip, title, timeframe, timeline label, tone, [(status, item, detail)])
    ("NOW \u00b7 DONE", "Foundation hardened", "All P0 findings resolved in code", "Oct 2026", "green", [
        ("done", "Zero-trust identity", "IAP JWT verified end to end; owner-only ticket access"),
        ("done", "Fail-closed routing", "Schema-validated sentiment; unknown labels escalate"),
        ("done", "Production-safe data", "Fail-fast Cloud SQL config, no silent SQLite fallback"),
        ("done", "Human handoff rule", "Escalated tickets are queued for people, not the LLM"),
        ("done", "Evaluation assets", "100-ticket golden set, rubric evaluator, 178 tests"),
        ("wip", "Roll-out", "Commit, redeploy agents + backend, apply IAM script"),
    ]),
    ("NEXT \u00b7 1\u20132 WEEKS", "Reliability & quality gates", "Make every change measurable", "Nov 2026", "blue", [
        ("wip", "CI pipeline", "Lint \u2192 tests \u2192 eval gate \u2192 staging \u2192 smoke test"),
        ("plan", "First eval baseline", "Run the golden set against live agents; record KPIs"),
        ("plan", "Circuit breaker v2", "Per-dependency breakers; count 5xx, 429, timeouts only"),
        ("plan", "Timeout budget", "Proxy 90 s > backend 75 s > per call 25 s, with jitter"),
        ("plan", "Lower latency", "Sentiment + FAQ in parallel; Flash vs. Pro benchmark"),
        ("plan", "UI fixes", "Ticket-ID race on first message; escape all output"),
    ]),
    ("LATER \u00b7 1\u20132 MONTHS", "Production readiness", "Run it like a product", "Dec 2026", "yellow", [
        ("plan", "Infrastructure as code", "Cloud Run, IAM, IAP, secrets in Terraform; SHA-tagged images"),
        ("plan", "Model Armor, end to end", "Prompt-injection, jailbreak and PII screening in and out"),
        ("plan", "Human-agent console", "Escalation queue on Pub/Sub, notifications, replies"),
        ("plan", "Data model", "Alembic migrations, ticket audit events, assignment"),
        ("plan", "Corpus curation", "Drop low-information replies; scope answers to one brand"),
        ("plan", "Hardened data tier", "Private IP, backups + PITR, HA Cloud SQL"),
    ]),
    ("FUTURE \u00b7 VISION", "Beyond FAQ answers", "From answering to resolving", "2027", "red", [
        ("plan", "Streaming answers", "Token streaming to the UI; conversation restore"),
        ("plan", "Agentic actions", "Order lookup, refunds, returns through approved tools"),
        ("plan", "Agent assist", "Draft replies and summaries for human specialists"),
        ("plan", "Self-improving evals", "Production failures flow back into the golden set"),
        ("plan", "Multilingual, multi-brand", "Per-brand corpora and language-aware routing"),
        ("plan", "Enterprise distribution", "Publish agents to Gemini Enterprise / Agent Registry"),
    ]),
]
SUCCESS = [  # (value, label)
    ("100%", "of releases gated by CI + golden set"),
    ("\u2265 95%", "routing accuracy (TDD north star)"),
    ("\u2265 85%", "grounding recall on FAQ answers"),
    ("\u2264 3.0 s", "P95 end-to-end latency (proposed)"),
]
ROADMAP_NOTES = """Roadmap and future of the FDE Support platform (source: docs/SYSTEM_ANALYSIS.md, sections 4 and 5).

Now (Oct 2026): all P0 findings are resolved in code: IAP JWT verification with owner-only ticket access, CORS removed, schema-validated fail-closed sentiment routing, fail-fast production DB config, escalated tickets bypass the LLM, golden set + rubric evaluator, 178 unit tests. Still to do: commit, redeploy the agents and backend, and run the IAM script.

Next (1-2 weeks): CI pipeline with an eval gate, first golden-set baseline against live agents, circuit breaker v2 (#8), outside-in timeout budget (#9), parallel sentiment + FAQ with a Flash vs. Pro benchmark (#10), UI ticket-ID race and output escaping (N1, #18).

Later (1-2 months): full Terraform + SHA-tagged images (#19-20), real Model Armor integration (#14), human-agent console with Pub/Sub notifications and Alembic migrations (#27-28), corpus curation (#24), private-IP / backed-up / HA Cloud SQL (#16).

Future (2027, vision, not yet scoped): streaming answers, agentic actions with approved tools, agent assist for human specialists, production failures feeding the golden set, multilingual / multi-brand corpora, publishing to Gemini Enterprise.

Success measures reuse the TDD and evaluation-plan targets; the latency target is a proposed project target.
"""


def status_dot(s, x, y, status, d=14):
    if status == "done":
        box(s, x, y, d, d, G["green"], kind=MSO_SHAPE.OVAL)
    elif status == "wip":
        box(s, x, y, d, d, G["yellow"], kind=MSO_SHAPE.OVAL)
    else:
        dot = box(s, x, y, d, d, G["white"], G["grey_lt"], kind=MSO_SHAPE.OVAL)
        dot.line.width = Pt(1.5)


def roadmap_slide(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    slide_frame(s, "ROADMAP", [
        ("Roadmap & ", 50, G["text"], True),
        ("Future of the System", 50, G["blue"], True),
    ], "From a hardened prototype to a measurable, production-grade support platform, then beyond FAQ answers.", 3)

    # status legend (top right)
    lx = 1480
    for status, label in (("done", "Done"), ("wip", "In progress"), ("plan", "Planned")):
        status_dot(s, lx, 131, status)
        text(s, lx + 22, 124, 120, 28, [[(label, 15, G["muted"], True)]], anchor=MSO_ANCHOR.MIDDLE)
        lx += 22 + len(label) * 9 + 34

    n, gap = len(ROADMAP), 24
    cw = (1776 - gap * (n - 1)) / n
    # timeline
    box(s, 72, 250, 1776, 3, G["border"])
    for i, (_, _, _, when, tone, _) in enumerate(ROADMAP):
        cx = 72 + i * (cw + gap) + cw / 2
        _, fg = TONES[tone]
        text(s, cx - 120, 208, 240, 24, [[(when, 16, fg, True)]], align=PP_ALIGN.CENTER)
        ring = box(s, cx - 13, 238, 26, 26, G["white"], kind=MSO_SHAPE.OVAL)
        ring.line.color.rgb = rgb(fg)
        ring.line.width = Pt(2)
        box(s, cx - 7, 244, 14, 14, fg, kind=MSO_SHAPE.OVAL)

    # phase columns
    y, h = 282, 588
    for i, (chip, title, sub, _, tone, items) in enumerate(ROADMAP):
        x = 72 + i * (cw + gap)
        bg, fg = TONES[tone]
        box(s, x, y, cw, h, G["card"], radius=16)
        box(s, x, y, cw, 6, fg)
        pill(s, x + 28, y + 26, chip_w(chip), 28, chip, fg, bg, 13)
        text(s, x + 28, y + 64, cw - 56, 32, [[(title, 24, G["text"], True)]])
        text(s, x + 28, y + 98, cw - 56, 22, [[(sub, 15, G["muted"])]])
        box(s, x + 28, y + 130, cw - 56, 1, G["border"])
        iy = y + 146
        for status, item, detail in items:
            status_dot(s, x + 28, iy + 4, status)
            text(s, x + 54, iy, cw - 82, 22, [[(item, 17, G["text"], True)]])
            text(s, x + 54, iy + 23, cw - 82, 38, [[(detail, 14.5, G["muted"])]])
            iy += 72

    # success measures
    y, h = 890, 130
    box(s, 72, y, 1776, h, G["blue_bg"], radius=14)
    text(s, 100, y + 30, 320, 70, [
        [("What success looks like", 20, G["text"], True)],
        [("Targets from the TDD and evaluation plan", 14.5, G["muted"])],
    ], anchor=MSO_ANCHOR.MIDDLE)
    x0, gx = 400, 16
    tw = (1824 - x0 - gx * (len(SUCCESS) - 1)) / len(SUCCESS)
    for i, (value, label) in enumerate(SUCCESS):
        tx, ty = x0 + i * (tw + gx), y + 18
        box(s, tx, ty, tw, h - 36, G["white"], radius=10)
        text(s, tx + 20, ty + 8, tw - 40, 40, [[(value, 30, G["blue"], True, MONO)]], anchor=MSO_ANCHOR.BOTTOM)
        text(s, tx + 20, ty + 52, tw - 40, 36, [[(label, 15, G["text"])]])

    s.notes_slide.notes_text_frame.text = ROADMAP_NOTES


# --------------------------------------------------------------------------------------
# Slide 4: AI-driven development
#   Left: evidence from this repo. Right: where agents can help at each lifecycle stage,
#   with the human role and an example tool. Bottom: guardrails.
# --------------------------------------------------------------------------------------
AI_STATS = [  # (value, label, detail)
    ("32", "Review findings", "P0\u2013P3 system analysis; all P0 fixed in code"),
    ("178", "Unit tests", "Auth, resilience, DB config, agent clients"),
    ("100", "Golden-set tickets", "Plus a rubric evaluator and KPI targets"),
    ("4", "Slides as code", "This deck, generated with python-pptx"),
]
AI_STAGES = [  # (stage, tone, [agent tasks], human role, example tool)
    ("Plan & design", "blue", ["Draft TDDs, ADRs and plans from scoping docs",
                               "Turn review findings into scoped tickets"], "Approves the plan", "Antigravity"),
    ("Build", "red", ["Parallel agents implement tickets on isolated branches",
                      "Scaffold ADK agents, tools and Terraform"], "Reviews the PR", "agents-cli"),
    ("Review & secure", "yellow", ["AI code review on every pull request",
                                   "Security and dependency scans before merge"], "Signs off on risk", "SecureCoder"),
    ("Test & evaluate", "green", ["Generate unit tests and new golden-set cases",
                                  "LLM judges score answers every night"], "Owns KPI targets", "Vertex AI eval"),
    ("Deploy & operate", "blue", ["Triage incidents from traces and logs",
                                  "Propose fixes, rollbacks and runbooks"], "Approves deploys", "Cloud Trace"),
    ("Optimise", "red", ["Tune prompts and models against the eval set",
                         "Find cost and latency savings each release"], "Picks trade-offs", "agents-cli eval"),
]
AI_GUARDRAILS = [  # (title, detail)
    ("Gated", "Every agent change passes PR review, CI and the eval gate"),
    ("Least privilege", "Scoped agent credentials; no production secrets"),
    ("Traceable", "Plans, prompts and diffs kept with each change"),
    ("Measured", "Track lead time, change-failure rate and eval KPIs"),
]
AI_NOTES = """AI-driven development for the FDE Support platform.

Left: what AI agents have already delivered on this repo: the prioritised system analysis (32 findings across P0-P3, all P0 fixed in code; docs/SYSTEM_ANALYSIS.md), 178 unit tests, the 100-ticket golden set with a rubric evaluator (docs/EVALUATION_PLAN.md), and this deck, generated as code. PRESENTATION.md also credits AI with the OpenTelemetry instrumentation and the Docker / Cloud Build setup.

Right: where agents can help at each stage, what the human still owns, and an example tool. Plan (Antigravity planning mode), Build (parallel agents on isolated branches; agents-cli scaffolding for ADK agents), Review & secure (AI code review, SecureCoder scans), Test & evaluate (generated tests and golden-set cases, nightly LLM judges), Deploy & operate (incident triage from Cloud Trace and logs), Optimise (prompt and model tuning against the eval set).

Guardrails: agents propose, people approve. Every agent change goes through PR review, CI and the eval gate; agents use scoped credentials; plans and diffs stay with each change; impact is measured with DORA metrics and the eval KPIs.

These are possible uses, not commitments; the tools are examples.
"""


def ai_dev_slide(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    slide_frame(s, "ENGINEERING", [
        ("AI-Driven ", 50, G["text"], True),
        ("Development", 50, G["blue"], True),
    ], "Agents take the repetitive work at every stage; engineers set direction, review and approve.", 4)
    pill(s, 1588, 118, 260, 38, "Agents propose \u00b7 people approve", G["blue"], G["blue_bg"])

    y, h = 216, 650
    # Evidence panel
    x, w = 72, 520
    tonal_panel(s, x, y, w, h, "PROVEN HERE", "green", "Already AI-assisted",
                "What agents have delivered on this repo")
    ix, iw = x + 28, w - 56
    ry = y + 140
    for k, (value, label, detail) in enumerate(AI_STATS):
        if k:
            box(s, ix, ry - 8, iw, 1, G["border"])
        text(s, ix, ry, 130, 60, [[(value, 46, G["green_dk"], True, MONO)]], anchor=MSO_ANCHOR.MIDDLE)
        text(s, ix + 140, ry + 6, iw - 140, 24, [[(label, 18, G["text"], True)]])
        text(s, ix + 140, ry + 32, iw - 140, 22, [[(detail, 14.5, G["muted"])]])
        ry += 84
    box(s, ix, y + h - 132, iw, 104, G["green_bg"], radius=10)
    text(s, ix + 18, y + h - 120, iw - 36, 84, [
        [("Earlier build work", 16, G["green_dk"], True)],
        [("OpenTelemetry spans, Dockerfiles and Cloud Build pipelines were AI-generated (PRESENTATION.md)",
          14.5, G["text"])],
    ])

    # Lifecycle grid
    gx0, gw, gap = 616, 1232, 20
    tw, th = (gw - 2 * gap) / 3, (h - gap) / 2
    for k, (stage, tone, tasks, human, tool) in enumerate(AI_STAGES):
        r, c = divmod(k, 3)
        tx, ty = gx0 + c * (tw + gap), y + r * (th + gap)
        bg, fg = TONES[tone]
        box(s, tx, ty, tw, th, G["card"], radius=16)
        n = box(s, tx + 24, ty + 24, 36, 36, fg, kind=MSO_SHAPE.OVAL)
        tf = n.text_frame
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        runs(tf, [[(str(k + 1), 17, G["white"], True)]], PP_ALIGN.CENTER)
        text(s, tx + 74, ty + 24, tw - 98, 36, [[(stage, 23, G["text"], True)]], anchor=MSO_ANCHOR.MIDDLE)
        text(s, tx + 24, ty + 78, tw - 48, 20, [[("AGENT DOES", 13, fg, True)]])
        by = ty + 104
        for task in tasks:
            box(s, tx + 26, by + 8, 8, 8, fg, kind=MSO_SHAPE.OVAL)
            text(s, tx + 46, by, tw - 70, 52, [[(task, 16.5, G["text"])]])
            by += 60
        box(s, tx + 24, ty + th - 70, tw - 48, 1, G["border"])
        text(s, tx + 24, ty + th - 54, 60, 30, [[("HUMAN", 13, G["muted"], True)]], anchor=MSO_ANCHOR.MIDDLE)
        cw = chip_w(tool, 12.5)
        text(s, tx + 86, ty + th - 54, tw - 120 - cw, 30, [[(human, 16, G["text"], True)]], anchor=MSO_ANCHOR.MIDDLE)
        pill(s, tx + tw - 24 - cw, ty + th - 52, cw, 26, tool, fg, bg, 12.5)

    # Guardrails band
    y, h = 890, 130
    box(s, 72, y, 1776, h, G["yellow_bg"], radius=14)
    text(s, 100, y + 30, 290, 70, [
        [("Guardrails", 20, G["text"], True)],
        [("Humans stay in the loop", 14.5, G["muted"])],
    ], anchor=MSO_ANCHOR.MIDDLE)
    x0, gx = 400, 16
    tw = (1824 - x0 - gx * (len(AI_GUARDRAILS) - 1)) / len(AI_GUARDRAILS)
    for i, (title, detail) in enumerate(AI_GUARDRAILS):
        tx, ty = x0 + i * (tw + gx), y + 18
        box(s, tx, ty, tw, h - 36, G["white"], radius=10)
        text(s, tx + 20, ty + 12, tw - 40, 26, [[(title, 18, G["yellow_dk"], True)]])
        text(s, tx + 20, ty + 40, tw - 40, 46, [[(detail, 14.5, G["text"])]])

    s.notes_slide.notes_text_frame.text = AI_NOTES


# --------------------------------------------------------------------------------------
# Slide 5: Security, Enterprise Compliance & Resilience (Zero-Trust Strategy)
#   Three columns: Zero-Trust Network & Identity, Data Compliance & DLP, High Availability & Resilience.
#   Bottom banner: Enterprise Governance & Security Guarantees.
# --------------------------------------------------------------------------------------
SEC_COLUMNS = [  # (tag, tone, title, sub, [ (title, chip, detail) ])
    ("ZERO-TRUST NETWORK", "blue", "Identity & Access",
     "End-to-end cryptographic verification and perimeter control", [
         ("IAP Cryptographic Auth", "IAP JWT",
          "x-goog-iap-jwt-assertion verified on frontend & backend; blocks identity spoofing"),
         ("Owner-Only Authorization", "Tenant Isolation",
          "Ticket reads, chat, and resolution restricted strictly to authenticated ticket owners"),
         ("VPC Service Controls", "VPC-SC",
          "Zero-trust network perimeter restricts API egress and protects cloud resources"),
         ("Least-Privilege IAM", "Service Accounts",
          "Service accounts bounded strictly to run.invoker on destination endpoints"),
     ]),
    ("DATA PRIVACY & AUDIT", "red", "Compliance & DLP",
     "Real-time PII redaction, fail-closed routing, and audit trails", [
         ("Model Armor DLP Redaction", "Model Armor",
          "Ingress/egress PII sanitization (SSNs, cards with Luhn check, emails) before LLMs"),
         ("Fail-Closed Routing", "Schema Validated",
          "Structured Pydantic schema for sentiment; invalid/malformed outputs escalate"),
         ("Production DB Enforcement", "Cloud SQL",
          "Fail-fast config requires mounted secrets & IAM connector; rejects unencrypted SQLite"),
         ("Audit Trail & Analytics", "BigQuery", "Cloud Logging sink to BigQuery Agent Analytics; complete audit trail with zero PII retention"),
     ]),
    ("HIGH AVAILABILITY", "green", "Fault Tolerance",
     "Cascading failure prevention, graceful fallbacks, and budget controls", [
         ("Circuit Breaker Isolation", "Circuit Breaker",
          "Isolates downstream dependencies; trips on 3 failures / 30s with single-probe cooldown"),
         ("Soft-Fallback Escalation", "Auto Handoff",
          "LLM timeouts/errors gracefully route tickets to human support queue without breaking UX"),
         ("Timeout Budgeting", "Backoff + Jitter",
          "Outside-in layered timeouts (Proxy 90s > Backend 75s > Agent 25s) with exponential retry"),
         ("Stateless Auto-Scaling", "Cloud Run",
          "Serverless scale-to-zero microservices with cached IAM tokens & connection pooling"),
     ]),
]

SEC_GUARANTEES = [  # (value, label, detail)
    ("0", "Raw PII Leaked", "DLP redaction before model boundary"),
    ("100%", "Authenticated", "IAP JWT cryptographic verification"),
    ("Fail-Closed", "Safe Escalation", "Automatic human handoff on low confidence"),
    ("< 30s", "Breaker Recovery", "Isolated probe cooldown prevents cascades"),
]

SEC_NOTES = """Security, Enterprise Compliance & Resilience (Zero-Trust Strategy) for the FDE Support platform.

1. Zero-Trust Network & Identity:
   - IAP Cryptographic Auth: Verification of x-goog-iap-jwt-assertion tokens at both frontend proxy and backend services ensures request origin authenticity and prevents client-side identity spoofing.
   - Tenant-Isolated Authorization: All ticket operations (creation, chat history, resolution) strictly enforce ownership checks against verified user credentials.
   - VPC Service Controls (VPC-SC): Defines a strict network perimeter blocking unauthorized external egress and exfiltration vectors.
   - Least-Privilege IAM: Cloud Run invoker permissions restricted explicitly to destination service accounts rather than project-wide roles.

2. Compliance & Data Privacy:
   - Model Armor & Cloud DLP: Input/output prompt sanitization removing SSNs, credit card numbers (Luhn validated), and email addresses prior to LLM submission and logging.
   - Fail-Closed Routing: Pydantic output schemas enforce strict enum validation on sentiment outputs. Unparseable responses default to human escalation.
   - Production Database Enforcement: Production runtime requires Cloud SQL UNIX socket connectors and Secret Manager credentials, preventing ephemeral SQLite fallbacks.
   - Audit Trail: Immutable logging pipeline streaming to BigQuery Agent Analytics for compliance auditing.

3. Enterprise Resilience & High Availability:
   - Circuit Breaker: Dependency isolation preventing downstream failure cascades with 3-failure threshold and single-probe HALF_OPEN recovery state.
   - Soft-Fallback Escalation: Handles downstream agent timeouts gracefully by setting escalation flags and notifying users without throwing 500 errors.
   - Timeout Budgeting: Outer-to-inner timeout cascading (Proxy 90s -> Backend 75s -> Agent 25s) combined with exponential backoff and jitter.
   - Stateless Serverless: Cloud Run scale-to-zero elasticity backed by cached IAM authentication tokens and connection pooling.
"""


def security_slide(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    slide_frame(s, "GOVERNANCE & TRUST", [
        ("Security, Compliance & ", 50, G["text"], True),
        ("Resilience", 50, G["blue"], True),
    ], "Zero-trust network perimeter, real-time PII redaction, and self-healing fault tolerance.", 5)
    pill(s, 1530, 118, 318, 38, "Zero-Trust Architecture", G["red_dk"], G["red_bg"])

    y, h = 216, 650
    n, gap = len(SEC_COLUMNS), 24
    cw = (1776 - gap * (n - 1)) / n

    for i, (tag, tone, title, sub, items) in enumerate(SEC_COLUMNS):
        x = 72 + i * (cw + gap)
        bg, fg = TONES[tone]
        box(s, x, y, cw, h, G["card"], radius=16)
        box(s, x, y, cw, 6, fg)
        pill(s, x + 24, y + 24, chip_w(tag, 12), 28, tag, fg, bg, 12)
        text(s, x + 24, y + 62, cw - 48, 32, [[(title, 24, G["text"], True)]])
        text(s, x + 24, y + 96, cw - 48, 22, [[(sub, 14.5, G["muted"])]])
        box(s, x + 24, y + 128, cw - 48, 1, G["border"])

        iy = y + 142
        for title_item, chip, detail in items:
            box(s, x + 24, iy, cw - 48, 108, G["white"], radius=10)
            box(s, x + 24, iy + 14, 5, 80, fg, radius=2)
            c_width = chip_w(chip, 12)
            pill(s, x + cw - 40 - c_width, iy + 12, c_width, 24, chip, fg, bg, 12)
            text(s, x + 42, iy + 12, cw - 90 - c_width, 24, [[(title_item, 16.5, G["text"], True)]])
            text(s, x + 42, iy + 40, cw - 66, 60, [[(detail, 14, G["muted"])]])
            iy += 120

    # Bottom Governance Banner
    y, h = 890, 130
    box(s, 72, y, 1776, h, G["blue_bg"], radius=14)
    text(s, 100, y + 30, 290, 70, [
        [("Security & Compliance", 20, G["text"], True)],
        [("Enterprise governance built into every layer", 14.5, G["muted"])],
    ], anchor=MSO_ANCHOR.MIDDLE)

    x0, gx = 400, 16
    tw = (1824 - x0 - gx * (len(SEC_GUARANTEES) - 1)) / len(SEC_GUARANTEES)
    for i, (value, label, detail) in enumerate(SEC_GUARANTEES):
        tx, ty = x0 + i * (tw + gx), y + 18
        box(s, tx, ty, tw, h - 36, G["white"], radius=10)
        text(s, tx + 16, ty + 8, tw - 32, 38, [[(value, 28, G["blue"], True, MONO)]], anchor=MSO_ANCHOR.BOTTOM)
        text(s, tx + 16, ty + 48, tw - 32, 20, [[(label, 15, G["text"], True)]])
        text(s, tx + 16, ty + 68, tw - 32, 22, [[(detail, 12.5, G["muted"])]])

    s.notes_slide.notes_text_frame.text = SEC_NOTES


def build():
    prs = Presentation()
    prs.slide_width, prs.slide_height = px(1920), px(1080)
    cost_slide(prs)
    llmops_slide(prs)
    roadmap_slide(prs)
    ai_dev_slide(prs)
    security_slide(prs)
    prs.save(OUT)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    build()

