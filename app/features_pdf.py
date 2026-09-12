"""Feature-summary PDF — a downloadable one-pager the user can share, showing
every enhancement shipped in this UI overhaul.
"""
from datetime import date
from io import BytesIO

from flask import Blueprint, send_file
from flask_login import login_required
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from app.auth import current_company

features_pdf_bp = Blueprint("features_pdf", __name__)

TEAL  = colors.HexColor("#0F766E")
CORAL = colors.HexColor("#F97354")
BODY  = colors.HexColor("#1F2937")
MUTED = colors.HexColor("#6B7280")
BGRAY = colors.HexColor("#F5F7FA")
BORDER = colors.HexColor("#E5E9F0")

FEATURES = [
    ("UI Redesign",
     "Zoho-inspired shell: dark-navy sidebar with icon nav, top search bar, "
     "rounded cards, Inter font.  Teal (#0F766E) primary + coral (#F97354) accents."),
    ("Sample Data Seeder",
     "sample_data.py seeds 5 customers, 3 vendors, 4 items and 18 balanced "
     "journal entries so the dashboard is populated on first login."),
    ("Global Search",
     "Debounced dropdown in the top bar searches customers, vendors, "
     "invoices, bills and items with keyboard nav."),
    ("Dark Mode",
     "Sun/moon toggle in the topbar. Palette swap via data-theme on <html>, "
     "persisted in localStorage; respects OS preference on first visit."),
    ("Invoice Live Preview",
     "Side-by-side invoice form: as you type customer, dates, memo and line "
     "items, the preview panel re-renders the printed invoice instantly."),
    ("One-click Mark Paid",
     "Quick 'Mark paid' action on every open invoice row — auto-creates a "
     "Payment for the full balance and posts the journal in one step."),
    ("Print-ready Invoice",
     "Every invoice now has a 'Print / PDF' action that renders a clean, "
     "branded PDF matching the on-screen preview."),
    ("Payment Reminders Digest",
     "'Reminders' page lists overdue and upcoming-in-7-days invoices, grouped "
     "by customer, with a copy-friendly reminder message per party."),
    ("SMTP Email Dispatch",
     "'Send email' action on every reminder row — configure once in Settings, "
     "then dispatch a friendly reminder to the customer inbox directly."),
    ("CSV Bulk Import",
     "'/imports' page bulk-loads customers, vendors and items from a CSV, "
     "upserting by name/SKU so re-imports are idempotent."),
    ("Logo Upload",
     "Upload a PNG/JPEG/WebP logo in Settings; it appears in the sidebar and "
     "on every invoice PDF."),
    ("Onboarding Checklist",
     "Dashboard shows a four-step progress banner (customer, invoice, payment, "
     "bill) that vanishes once every step is done."),
    ("Invoice PDF Themes",
     "Pick between Classic, Minimal or Coral for every invoice PDF, or "
     "override per-invoice via ?template= in the URL."),
]


def _build_pdf(company_name):
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=20 * mm, bottomMargin=18 * mm,
        title="LedgerBooks — Feature Summary",
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle(
        "h1", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=22,
        textColor=TEAL, leading=26, spaceAfter=2,
    )
    subtitle = ParagraphStyle(
        "sub", parent=styles["Normal"], fontName="Helvetica", fontSize=10,
        textColor=MUTED, leading=14, spaceAfter=14,
    )
    section = ParagraphStyle(
        "section", parent=styles["Heading2"], fontName="Helvetica-Bold",
        fontSize=13, textColor=BODY, leading=16, spaceBefore=10, spaceAfter=6,
    )
    body = ParagraphStyle(
        "body", parent=styles["BodyText"], fontName="Helvetica", fontSize=10,
        textColor=BODY, leading=14,
    )
    small = ParagraphStyle(
        "small", parent=body, fontSize=9, textColor=MUTED, leading=12,
    )

    story = []
    story.append(Paragraph("LedgerBooks — Feature Summary", h1))
    story.append(Paragraph(
        f"Prepared for <b>{company_name}</b> · {date.today().strftime('%d %B %Y')}",
        subtitle,
    ))

    story.append(Paragraph("Everything shipped in this iteration", section))

    data = [[Paragraph("<b>Feature</b>", body),
             Paragraph("<b>What it does</b>", body)]]
    for name, blurb in FEATURES:
        data.append([Paragraph(f"<b>{name}</b>", body), Paragraph(blurb, small)])

    tbl = Table(data, colWidths=[45 * mm, 118 * mm], hAlign="LEFT")
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BGRAY),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, TEAL),
        ("BOX",       (0, 0), (-1, -1), 0.4, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, BORDER),
        ("VALIGN",    (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING",   (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 6),
    ]))
    story.append(tbl)

    story.append(Spacer(1, 12))
    story.append(Paragraph("How to reach each feature", section))
    nav_lines = [
        "Dashboard  ·  <b>Home /</b>  — populated by the sample-data seeder.",
        "Search  ·  <b>Top bar search box</b>  — start typing at least two characters.",
        "Dark mode  ·  <b>Sun/moon icon</b> in the top bar (state persists).",
        "Live invoice preview  ·  <b>Sales → Invoices → + New invoice</b>.",
        "Mark paid  ·  <b>Sales → Invoices</b> — action link on each open row.",
        "Print / PDF  ·  <b>Sales → Invoices → open one → Print / PDF</b>.",
        "Reminders digest  ·  <b>Sales → Reminders</b> (overdue + upcoming).",
        "CSV import  ·  <b>Admin → Import CSV</b>  — customers, vendors, items.",
    ]
    for line in nav_lines:
        story.append(Paragraph("• " + line, body))
        story.append(Spacer(1, 2))

    story.append(Spacer(1, 14))
    story.append(Paragraph(
        "<i>All backend features (double-entry ledger, MRA fiscalisation, "
        "payroll bridge, backups) remain untouched — only the front-end "
        "experience and these workflow shortcuts were added on top.</i>",
        small,
    ))

    doc.build(story)
    buf.seek(0)
    return buf


@features_pdf_bp.route("/features.pdf")
@login_required
def features_pdf():
    company = current_company()
    name = company.business_name if company else "Your Company"
    return send_file(
        _build_pdf(name),
        mimetype="application/pdf",
        as_attachment=False,
        download_name="LedgerBooks-Features.pdf",
    )
