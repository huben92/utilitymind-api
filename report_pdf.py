"""Downloadable operations reports rendered as vector PDFs."""

from datetime import datetime
from io import BytesIO
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Flowable, HRFlowable, KeepTogether, LongTable, PageBreak, Paragraph,
    SimpleDocTemplate, Spacer, Table, TableStyle,
)


NAVY = colors.HexColor("#102635")
TEAL = colors.HexColor("#16856f")
PALE = colors.HexColor("#e9f4f0")
INK = colors.HexColor("#203341")
MUTED = colors.HexColor("#657887")
LINE = colors.HexColor("#dbe5e8")
REPORT_TZ = ZoneInfo("Asia/Jakarta")


def _text(value):
    return escape(str(value if value is not None else ""))


def _number(value, digits=1):
    return "N/A" if value is None else f"{value:,.{digits}f}"


def _unit(value):
    return (value or "").replace("³", "3").replace("°", " deg ")


class TrendStrip(Flowable):
    """Small vector trend; each bar is an observed hour or day."""

    def __init__(self, values, width=505, height=44):
        super().__init__()
        self.values = values
        self.width = width
        self.height = height

    def draw(self):
        canvas = self.canv
        canvas.setStrokeColor(LINE)
        canvas.line(0, 5, self.width, 5)
        if not self.values:
            return
        maximum = max((point["value"] or 0 for point in self.values), default=0)
        bar_width = self.width / len(self.values)
        for index, point in enumerate(self.values):
            value = point["value"]
            if value is None:
                canvas.setFillColor(colors.HexColor("#e4e9ec"))
                canvas.rect(index * bar_width + 1, 5, max(1, bar_width - 2), 2, fill=1, stroke=0)
            else:
                height = max(2, (value / maximum) * (self.height - 10)) if maximum else 2
                canvas.setFillColor(TEAL)
                canvas.rect(index * bar_width + 1, 5, max(1, bar_width - 2), height, fill=1, stroke=0)


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("ReportTitle", parent=base["Title"], fontName="Helvetica-Bold", fontSize=22, leading=27, textColor=NAVY, alignment=TA_LEFT, spaceAfter=4),
        "subtitle": ParagraphStyle("ReportSubtitle", parent=base["Normal"], fontSize=10, leading=14, textColor=MUTED, spaceAfter=14),
        "section": ParagraphStyle("ReportSection", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=12, leading=15, textColor=NAVY, spaceBefore=17, spaceAfter=9),
        "body": ParagraphStyle("ReportBody", parent=base["Normal"], fontSize=9, leading=13, textColor=INK, spaceAfter=7),
        "small": ParagraphStyle("ReportSmall", parent=base["Normal"], fontSize=8, leading=11, textColor=MUTED),
        "table": ParagraphStyle("ReportTable", parent=base["Normal"], fontSize=8, leading=11, textColor=INK),
        "tablehead": ParagraphStyle("ReportTableHead", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=colors.white),
        "metric": ParagraphStyle("ReportMetric", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=16, leading=19, textColor=NAVY, alignment=TA_CENTER),
        "metriclabel": ParagraphStyle("ReportMetricLabel", parent=base["Normal"], fontSize=7, leading=10, textColor=MUTED, alignment=TA_CENTER),
    }


def _paragraph(value, style):
    return Paragraph(_text(value), style)


def _table(rows, widths, header=True):
    table = LongTable(rows, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, LINE),
    ]
    if header:
        commands += [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f8f9")])]
    else:
        commands += [("BACKGROUND", (0, 0), (-1, -1), PALE), ("BOX", (0, 0), (-1, -1), 0.5, LINE)]
    table.setStyle(TableStyle(commands))
    return table


def _footer(canvas, doc, report):
    canvas.saveState()
    width, height = A4
    canvas.setFillColor(NAVY)
    canvas.rect(0, height - 12 * mm, width, 12 * mm, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 8)
    canvas.drawString(17 * mm, height - 8 * mm, "UTILITYMIND AI  /  WHEELWORKS")
    canvas.setStrokeColor(LINE)
    canvas.line(17 * mm, 17 * mm, width - 17 * mm, 17 * mm)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(17 * mm, 12 * mm, f"{report['kind'].title()} report  |  {report['period']}  |  {report['data_origin']}  |  Advisory only")
    canvas.drawRightString(width - 17 * mm, 12 * mm, f"Page {doc.page}")
    canvas.restoreState()


def make_report_pdf(report):
    """Return PDF bytes for a report calculated from current source records."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=17 * mm, leftMargin=17 * mm,
                            topMargin=20 * mm, bottomMargin=23 * mm,
                            title=f"{report['kind'].title()} utility report - {report['period']}",
                            author="UtilityMind AI / Wheelworks")
    styles = _styles()
    width = A4[0] - 34 * mm
    story = []
    generated = datetime.fromisoformat(report["generated_at"]).astimezone(REPORT_TZ)
    end = datetime.fromisoformat(report["period_end"]).astimezone(REPORT_TZ)
    story.append(_paragraph(f"{report['kind'].title()} utility report", styles["title"]))
    story.append(_paragraph(f"{report['factory']['name']}  |  {report['label']}  |  Timezone: {report['timezone']}", styles["subtitle"]))
    story.append(_paragraph(f"Observed through {end:%d %b %Y, %H:%M}. Generated {generated:%d %b %Y, %H:%M}. "
                            f"Data origin: {report['data_origin']}. "
                            + ("This period is still in progress." if report["partial"] else "Completed calendar period."), styles["body"]))

    metrics = [
        (str(len(report["utilities"])), "UTILITY GROUPS"),
        (f"{report['reporting_meters']} / {report['meter_count']}", "METERING POINTS"),
        (f"{_number(report['coverage_pct'])}%", "SAMPLE COVERAGE"),
        (f"{report['samples']:,}", "READINGS"),
    ]
    cards = [[Paragraph(_text(value), styles["metric"]) for value, _ in metrics],
             [Paragraph(_text(label), styles["metriclabel"]) for _, label in metrics]]
    story.append(_table(cards, [width / 4] * 4, header=False))
    story.append(Spacer(1, 5 * mm))

    story.append(_paragraph("Utility ledger", styles["section"]))
    story.append(_paragraph("Observed totals use covered samples only. Values from different utilities are not added together.", styles["small"]))
    rows = [[_paragraph(value, styles["tablehead"]) for value in ("Utility", "Observed total", "Coverage", "Baseline use*", "Meters")]]
    for group in report["utilities"]:
        total = f"{_number(group['observed_total'])} {_unit(group['quantity_unit'])}" if group["observed_total"] is not None else "N/A"
        utilization = f"{_number(group['utilization_pct'])}%" if group["utilization_pct"] is not None else "N/A"
        rows.append([_paragraph(group["category"], styles["table"]), _paragraph(total, styles["table"]),
                     _paragraph(f"{_number(group['coverage_pct'])}%", styles["table"]),
                     _paragraph(utilization, styles["table"]),
                     _paragraph(f"{group['reporting_meters']}/{group['meter_count']}", styles["table"])])
    if len(rows) == 1:
        rows.append([_paragraph("No metering points", styles["table"]), "", "", "", ""])
    story.append(_table(rows, [width * p for p in (.23, .24, .17, .21, .15)]))
    story.append(Spacer(1, 3 * mm))
    story.append(_paragraph("*Observed quantity divided by the current configured baseline over covered time. This is indicative, not a production-adjusted historical target.", styles["small"]))

    story.append(_paragraph("Operational review", styles["section"]))
    for item in report["review_items"]:
        story.append(_paragraph("- " + item, styles["body"]))
    story.append(_paragraph("These are screening observations. No anomaly has been verified by an operator in this prototype.", styles["small"]))

    story.append(PageBreak())
    story.append(_paragraph("Usage pattern and machine hotspots", styles["title"]))
    story.append(_paragraph("Each strip shows observed integrated quantity by local hour (daily) or day (monthly). Gray marks indicate no readings.", styles["subtitle"]))
    for group in report["utilities"]:
        label = f"{group['category']}  |  {_number(group['observed_total'])} {_unit(group['quantity_unit'])} observed  |  {_number(group['coverage_pct'])}% coverage"
        story.append(KeepTogether([_paragraph(label, styles["body"]), TrendStrip(group["trend"], width=width), Spacer(1, 3 * mm)]))
    story.append(_paragraph("Highest observed machine by utility", styles["section"]))
    machine_rows = [[_paragraph(value, styles["tablehead"]) for value in ("Utility", "Machine", "Observed total", "Share")]]
    for group in report["utilities"]:
        if not group["machines"]:
            continue
        machine = group["machines"][0]
        share = 100 * machine["total"] / group["observed_total"] if group["observed_total"] else None
        machine_rows.append([_paragraph(group["category"], styles["table"]),
                             _paragraph(machine["name"], styles["table"]),
                             _paragraph(f"{_number(machine['total'])} {_unit(group['quantity_unit'])}", styles["table"]),
                             _paragraph(f"{_number(share)}%", styles["table"])])
    if len(machine_rows) == 1:
        machine_rows.append([_paragraph("No readings", styles["table"]), "", "", ""])
    story.append(_table(machine_rows, [width * p for p in (.23, .39, .23, .15)]))
    story.append(_paragraph("Shares compare observed quantities within the same utility and unit. Missing metering reduces the denominator.", styles["small"]))

    story.append(PageBreak())
    story.append(_paragraph("Data quality and interpretation", styles["title"]))
    story.append(_paragraph("Meter-level coverage against the elapsed calendar period and the current catalog.", styles["subtitle"]))
    quality_rows = [[_paragraph(value, styles["tablehead"]) for value in ("Metering point", "Utility", "Machine", "Coverage", "Samples")]]
    for meter in report["meter_quality"]:
        quality_rows.append([_paragraph(meter["name"], styles["table"]),
                             _paragraph(meter["type"], styles["table"]),
                             _paragraph(meter["machine_name"], styles["table"]),
                             _paragraph(f"{_number(meter['coverage_pct'])}%", styles["table"]),
                             _paragraph(f"{meter['samples']:,}", styles["table"])])
    if len(quality_rows) == 1:
        quality_rows.append([_paragraph("No metering points", styles["table"]), "", "", "", ""])
    story.append(_table(quality_rows, [width * p for p in (.25, .2, .26, .14, .15)]))
    story.append(_paragraph("KPIs awaiting source data", styles["section"]))
    for item in report["unavailable"]:
        story.append(_paragraph("- " + item, styles["body"]))
    story.append(_paragraph("Calculation and scope", styles["section"]))
    story.append(_paragraph(report["method"], styles["body"]))
    story.append(_paragraph("Source: ClickHouse telemetry joined to the current PostgreSQL factory, machine, and metering catalog. "
                            "This report is for human review; it does not command utility systems.", styles["small"]))

    doc.build(story, onFirstPage=lambda c, d: _footer(c, d, report),
              onLaterPages=lambda c, d: _footer(c, d, report))
    return buffer.getvalue()
