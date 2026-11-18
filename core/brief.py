"""
Monthly executive brief — a one-page PDF summary of a client's Azure
cost posture. Generated automatically after each pipeline run (see
pipeline/runner.py, core/email.py) and available on demand via the
dashboard's "Download brief" button (dashboard/views.py).

Built directly on reportlab's low-level Canvas rather than its Platypus
flowable/frame system: the layout is a fixed one-pager with a small,
known set of sections, so manual coordinate placement makes it easy to
guarantee the one-page constraint, instead of fighting an automatic
page-flow engine designed for documents of unknown length.

Layout constants (_LAYOUT below) are the single source of truth for
vertical spacing — both the "does 5 findings fit or do we fall back to
3" check and the actual drawing loop read the same numbers, so the two
can't silently drift out of sync with each other.
"""

import io
import re
from datetime import date, timedelta

from django.db.models import Sum, Max
from django.db.models.functions import TruncMonth
from django.utils import timezone

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.colors import HexColor, black
from reportlab.pdfgen import canvas
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics import renderPDF

from core.models import Client, PipelineRun, Finding, DailyCost

NAVY       = HexColor("#1E3A5F")
TEAL       = HexColor("#0D7A5A")
RED        = HexColor("#C23B32")
AMBER      = HexColor("#B86A20")
GRAY       = HexColor("#6B7280")
GRAY_DARK  = HexColor("#374151")
BOX_BG     = HexColor("#F3F4F6")
LINE_GRAY  = HexColor("#E5E7EB")

PAGE_W, PAGE_H = LETTER
MARGIN = 40

SEVERITY_COLORS = {"high": RED, "medium": AMBER, "low": TEAL}

# Vertical spacing, all in points — shared by the fit-check math and the
# actual drawing loop below (see module docstring).
_LAYOUT = {
    "header_line_above": 10,  # wordmark -> teal separator line
    "header_line_below": 8,   # teal separator line -> subtitle
    "header_name":      24,   # subtitle -> client display name
    "header_title":     15,   # display name -> brief title line
    "header_prepared":  12,   # brief title -> "prepared by" line
    "header_divider":   6,    # "prepared by" -> divider rule
    "header_to_boxes":  14,   # divider -> stat box top
    "box_h":            44,
    "box_to_s2_title":  14,   # box bottom -> "What we found" title
    "s2_title_step":    14,   # title -> first finding row
    "row2_h":           22,   # per finding row
    "s2_to_s3_gap":     12,   # last finding row -> "What we recommend" title
    "s3_title_step":    14,
    "row3_h":           26,   # per recommendation row
    "s3_to_s4_gap":     12,   # last recommendation row -> chart title
    "s4_title_step":    14,
    "chart_h":          120,
    "footer_floor":     70,   # reserved bottom strip for the footer
}

# Presentation-only mapping from finding_type to an imperative verb for
# section 3 headers ("{verb} {resource_name}") — finding.action text is
# free-form prose (see pipeline/analyzer_*.py) and doesn't reliably start
# with a verb, so this mirrors the FINDING_LABELS/FINDING_CATEGORY
# pattern already used for display purposes in dashboard/views.py.
ACTION_VERBS = {
    "idle_account":            "Deallocate or delete",
    "wrong_tier":               "Resize",
    "unattached_public_ip":     "Release",
    "orphaned_disk":            "Delete",
    "unattached_nic":           "Delete",
    "unused_nsg":               "Delete",
    "aging_snapshot":           "Review",
    "stopped_not_deallocated":  "Deallocate",
    "empty_load_balancer":      "Delete or reconfigure",
    "no_autoshutdown":          "Schedule auto-shutdown for",
    "token_inefficiency":       "Optimize",
    "rate_limiting":            "Review",
    "duplicate_accounts":       "Consolidate",
    "stale_model":              "Retire",
    "cost_anomaly":             "Review",
}

# Matches a full ARM resource path (subscriptions/.../providers/...) so
# it can be swapped for the resource's plain-English name — evidence and
# action text must never surface a raw ARM path in a client-facing PDF.
ARM_PATH_RE = re.compile(r"/subscriptions/\S+")


def _strip_arm_paths(text: str, fallback_name: str) -> str:
    return ARM_PATH_RE.sub(fallback_name, text or "")


def _first_sentence(text: str, max_len: int = 100) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    cut = text.find(". ")
    sentence = text[: cut + 1] if cut != -1 else text
    if len(sentence) > max_len:
        sentence = sentence[: max_len - 1].rstrip() + "…"
    return sentence


def compute_brief_stats(client: Client) -> dict:
    """
    Shared aggregate numbers used by both the PDF (this module) and the
    brief email's plain-text body (core/email.py) — kept in one place so
    the two can't drift out of sync with each other.

    total_savings now uses core.savings.canonical_monthly_savings() so it
    matches the Overview exactly: cost_anomaly excluded, deleted resources
    excluded, per-resource savings capped at that resource's monthly cost.

    total_spend is windowed to client.analysis_start (rolling 90-day
    default) instead of all-time, matching what summary() shows.
    """
    from core.savings import canonical_monthly_savings

    window_start = client.analysis_start or (date.today() - timedelta(days=90))
    open_findings = Finding.objects.filter(client=client, status="open")
    total_spend = (
        DailyCost.objects.filter(client=client, date__gte=window_start)
        .aggregate(s=Sum("billed_cost"))["s"] or 0
    )
    total_savings = canonical_monthly_savings(client)
    return {
        "total_spend":        total_spend,
        "total_savings":      total_savings,
        "annual_savings":     total_savings * 12,
        "resolved_count":     Finding.objects.filter(client=client, status="resolved").count(),
        "open_findings_count": open_findings.count(),
    }


def _monthly_spend_trend(client: Client, months: int = 6) -> list:
    """
    Last `months` calendar months of spend, oldest first, ending on the
    month of the most recent DailyCost row — not necessarily the real
    current month, since collector_cost's actual collection window
    isn't guaranteed to reach today (see pipeline/analyzer_anomaly.py
    for the same reasoning applied to week boundaries).
    """
    latest = DailyCost.objects.filter(client=client).aggregate(m=Max("date"))["m"]
    if latest is None:
        return []

    rows = (
        DailyCost.objects.filter(client=client)
        .annotate(month=TruncMonth("date"))
        .values("month")
        .annotate(total=Sum("billed_cost"))
    )
    by_month = {r["month"]: r["total"] or 0 for r in rows}

    y, m = latest.year, latest.month
    months_desc = []
    for _ in range(months):
        months_desc.append(date(y, m, 1))
        m -= 1
        if m == 0:
            m, y = 12, y - 1

    return [(d.strftime("%b"), by_month.get(d, 0)) for d in reversed(months_desc)]


def _findings_that_fit(open_findings_count: int) -> int:
    """
    Decides between showing 5 or 3 findings in Section 2, based on
    whether the fixed-size sections below it (recommendations + chart +
    footer) leave enough room on a single page. Uses the exact same
    _LAYOUT numbers the drawing loop uses, so this can't drift out of
    sync with what actually gets rendered.
    """
    L = _LAYOUT
    header_descent = (
        L["header_line_above"] + L["header_line_below"] + L["header_name"]
        + L["header_title"] + L["header_prepared"]
        + L["header_divider"] + L["header_to_boxes"]
    )
    first_row_y = (
        PAGE_H - MARGIN - header_descent - L["box_h"]
        - L["box_to_s2_title"] - L["s2_title_step"]
    )
    section3_block = L["s2_to_s3_gap"] + L["s3_title_step"] + 3 * L["row3_h"]
    section4_block = L["s3_to_s4_gap"] + L["s4_title_step"] + L["chart_h"]

    space_for_rows = first_row_y - L["footer_floor"] - section3_block - section4_block
    max_rows_that_fit = int(space_for_rows // L["row2_h"])

    if max_rows_that_fit >= 5:
        return min(5, open_findings_count)
    return min(3, open_findings_count)


def _draw_stat_box(c, x, top, w, h, label, value):
    c.setFillColor(BOX_BG)
    c.roundRect(x, top - h, w, h, 4, fill=1, stroke=0)
    c.setFont("Helvetica", 6.5)
    c.setFillColor(GRAY)
    c.drawString(x + 8, top - 14, label)
    c.setFont("Helvetica-Bold", 13)
    c.setFillColor(NAVY)
    c.drawString(x + 8, top - 33, value)


def _draw_severity_badge(c, x, y, severity):
    label = severity.upper()
    color = SEVERITY_COLORS.get(severity, GRAY)
    c.setFont("Helvetica-Bold", 6.5)
    w = c.stringWidth(label, "Helvetica-Bold", 6.5) + 10
    c.setFillColor(color)
    c.roundRect(x, y - 2, w, 11, 2, fill=1, stroke=0)
    c.setFillColor(HexColor("#FFFFFF"))
    c.drawString(x + 5, y + 1, label)
    return w


def _build_spend_chart(trend: list, width: float, height: float) -> Drawing:
    d = Drawing(width, height)
    bc = VerticalBarChart()
    bc.x = 28
    bc.y = 18
    bc.width = width - 40
    bc.height = height - 30
    bc.data = [[v for _, v in trend]]
    bc.categoryAxis.categoryNames = [label for label, _ in trend]
    bc.categoryAxis.labels.fontName = "Helvetica"
    bc.categoryAxis.labels.fontSize = 7
    bc.categoryAxis.labels.fillColor = GRAY_DARK
    bc.valueAxis.visible = False
    bc.valueAxis.valueMin = 0
    bc.bars[0].fillColor = NAVY
    bc.bars[0].strokeColor = TEAL
    bc.bars[0].strokeWidth = 1
    bc.barWidth = 10
    bc.barLabels.fontName = "Helvetica-Bold"
    bc.barLabels.fontSize = 7
    bc.barLabels.fillColor = TEAL
    bc.barLabels.nudge = 8
    bc.barLabelFormat = "$%.0f"
    d.add(bc)
    return d


def _settings_contact_email() -> str:
    from django.conf import settings
    return getattr(settings, "GHASHEM_CONTACT_EMAIL", "support@ghashem.ca")


def generate_monthly_brief(client: Client, run_obj: PipelineRun) -> bytes:
    L = _LAYOUT
    stats = compute_brief_stats(client)
    trend = _monthly_spend_trend(client)

    open_findings = list(
        Finding.objects.filter(client=client, status="open")
        .order_by("-estimated_monthly_savings_usd")
    )
    recommendations = open_findings[:3]

    num_findings = _findings_that_fit(len(open_findings))
    shown_findings = open_findings[:num_findings]

    today = timezone.localdate()
    month_name, year = today.strftime("%B"), today.year

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER, pageCompression=1)
    c.setTitle(f"Ghashem Azure Cost Brief — {client.display_name} — {month_name} {year}")

    # ── Header ──
    y = PAGE_H - MARGIN
    c.setFont("Helvetica-Bold", 20)
    c.setFillColor(NAVY)
    c.drawString(MARGIN, y, "Ghashem")

    y -= L["header_line_above"]
    c.setStrokeColor(TEAL)
    c.setLineWidth(0.75)
    c.line(MARGIN, y, PAGE_W - MARGIN, y)

    y -= L["header_line_below"]
    c.setFont("Helvetica", 9)
    c.setFillColor(TEAL)
    c.drawString(MARGIN, y, "Azure Cost Intelligence")

    y -= L["header_name"]
    c.setFont("Helvetica-Bold", 13)
    c.setFillColor(black)
    c.drawString(MARGIN, y, client.display_name)

    y -= L["header_title"]
    c.setFont("Helvetica", 10)
    c.setFillColor(GRAY_DARK)
    c.drawString(MARGIN, y, f"Azure Cost Intelligence Brief — {month_name} {year}")

    y -= L["header_prepared"]
    c.setFont("Helvetica", 7.5)
    c.setFillColor(GRAY)
    c.drawString(MARGIN, y, f"Prepared by Ghashem · {today.strftime('%B %d, %Y')}")

    y -= L["header_divider"]
    c.setStrokeColor(LINE_GRAY)
    c.setLineWidth(1)
    c.line(MARGIN, y, PAGE_W - MARGIN, y)
    y -= L["header_to_boxes"]

    # ── Section 1: stat boxes ──
    box_h_gap = 10
    box_w = (PAGE_W - 2 * MARGIN - 3 * box_h_gap) / 4
    box_top = y
    stat_boxes = [
        ("TOTAL SPEND THIS PERIOD",     f"${stats['total_spend']:,.0f} {client.report_currency}"),
        ("IDENTIFIED WASTE",            f"${stats['total_savings']:,.0f}/mo"),
        ("POTENTIAL ANNUAL SAVING",     f"${stats['annual_savings']:,.0f}"),
        ("FINDINGS RESOLVED TO DATE",   f"{stats['resolved_count']}"),
    ]
    for i, (label, value) in enumerate(stat_boxes):
        _draw_stat_box(c, MARGIN + i * (box_w + box_h_gap), box_top, box_w, L["box_h"], label, value)
    y = box_top - L["box_h"] - L["box_to_s2_title"]

    # ── Section 2: What we found ──
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(NAVY)
    c.drawString(MARGIN, y, "What we found")
    y -= L["s2_title_step"]

    if not shown_findings:
        c.setFont("Helvetica", 8.5)
        c.setFillColor(GRAY)
        c.drawString(MARGIN, y, "No open findings — nothing to report this period.")
        y -= L["row2_h"]
    for f in shown_findings:
        desc = _strip_arm_paths(_first_sentence(f.evidence), f.resource_name)
        badge_w = _draw_severity_badge(c, MARGIN, y, f.severity)
        c.setFont("Helvetica-Bold", 8.5)
        c.setFillColor(black)
        c.drawString(MARGIN + badge_w + 6, y, f.resource_name[:48])
        c.setFont("Helvetica-Bold", 8.5)
        c.setFillColor(TEAL)
        c.drawRightString(PAGE_W - MARGIN, y, f"${f.estimated_monthly_savings_usd:,.2f}/mo")
        c.setFont("Helvetica", 7.5)
        c.setFillColor(GRAY)
        c.drawString(MARGIN, y - 9, desc[:118])
        y -= L["row2_h"]

    # ── Section 3: What we recommend ──
    y -= L["s2_to_s3_gap"]
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(NAVY)
    c.drawString(MARGIN, y, "What we recommend")
    y -= L["s3_title_step"]

    if not recommendations:
        c.setFont("Helvetica", 8.5)
        c.setFillColor(GRAY)
        c.drawString(MARGIN, y, "No recommendations — no open findings this period.")
        y -= L["row3_h"]
    for f in recommendations:
        verb = ACTION_VERBS.get(f.finding_type, "Review")
        header_line = f"{verb} {f.resource_name}"[:70]
        annual = f.estimated_monthly_savings_usd * 12
        why = _strip_arm_paths(_first_sentence(f.action), f.resource_name)

        c.setFont("Helvetica-Bold", 8.5)
        c.setFillColor(black)
        c.drawString(MARGIN, y, header_line)
        c.setFont("Helvetica-Bold", 8)
        c.setFillColor(TEAL)
        c.drawRightString(
            PAGE_W - MARGIN, y,
            f"${f.estimated_monthly_savings_usd:,.2f}/mo · ${annual:,.2f}/yr",
        )
        c.setFont("Helvetica", 7.5)
        c.setFillColor(GRAY)
        c.drawString(MARGIN, y - 10, why[:118])
        y -= L["row3_h"]

    # ── Section 4: Spend trend ──
    y -= L["s3_to_s4_gap"]
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(NAVY)
    c.drawString(MARGIN, y, "Spend trend — last 6 months")
    y -= L["s4_title_step"]

    if trend and any(v > 0 for _, v in trend):
        drawing = _build_spend_chart(trend, PAGE_W - 2 * MARGIN, L["chart_h"])
        renderPDF.draw(drawing, c, MARGIN, y - L["chart_h"])
    else:
        c.setFont("Helvetica", 8.5)
        c.setFillColor(GRAY)
        c.drawString(MARGIN, y - 20, "No spend data recorded yet for this period.")
    y -= L["chart_h"]

    # ── Section 5: Footer (fixed near the bottom of the page) ──
    contact_email = _settings_contact_email()

    c.setStrokeColor(LINE_GRAY)
    c.line(MARGIN, 46, PAGE_W - MARGIN, 46)
    c.setFont("Helvetica", 7.5)
    c.setFillColor(GRAY)
    c.drawString(MARGIN, 34, f"Questions? {contact_email}")
    c.drawString(MARGIN, 23, f"Ghashem · {contact_email}")
    c.drawRightString(PAGE_W - MARGIN, 23, "Page 1 of 1")

    c.showPage()
    c.save()
    return buf.getvalue()
