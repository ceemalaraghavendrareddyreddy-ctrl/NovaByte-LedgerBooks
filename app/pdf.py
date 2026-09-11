"""Invoice PDF generation using reportlab's platypus layer (table/paragraph flow, not raw canvas
coordinates — much easier to keep aligned as content length varies)."""

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ACCENT = colors.HexColor("#1a7f5a")
MUTED = colors.HexColor("#647883")
BORDER = colors.HexColor("#dce4e9")


def generate_invoice_pdf(invoice, company):
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=20 * mm, bottomMargin=20 * mm, leftMargin=20 * mm, rightMargin=20 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleAccent", parent=styles["Title"], textColor=ACCENT, fontSize=22)
    muted_style = ParagraphStyle("Muted", parent=styles["Normal"], textColor=MUTED, fontSize=9)
    right_style = ParagraphStyle("Right", parent=styles["Normal"], alignment=TA_RIGHT)
    right_muted = ParagraphStyle("RightMuted", parent=muted_style, alignment=TA_RIGHT)

    elements = []

    # Header: business info (left) vs invoice number/dates (right)
    business_lines = [f"<b>{company.business_name}</b>"]
    if company.address:
        business_lines.append(company.address.replace("\n", "<br/>"))
    if company.phone:
        business_lines.append(f"Tel: {company.phone}")
    if company.email:
        business_lines.append(company.email)
    if company.vat_number:
        business_lines.append(f"VAT No: {company.vat_number}")
    business_para = Paragraph("<br/>".join(business_lines), styles["Normal"])

    invoice_meta = Paragraph(
        f"<font color='#1a7f5a'><b>INVOICE</b></font><br/>"
        f"<b>{invoice.invoice_no}</b><br/>"
        f"Date: {invoice.invoice_date.strftime('%d %b %Y')}<br/>"
        f"Due: {invoice.due_date.strftime('%d %b %Y')}<br/>"
        f"Status: {invoice.status.upper()}",
        right_style,
    )

    header_table = Table([[business_para, invoice_meta]], colWidths=[100 * mm, 70 * mm])
    header_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    elements.append(header_table)
    elements.append(Spacer(1, 10 * mm))

    # Bill To
    customer = invoice.customer
    bill_to_lines = [f"<b>Bill To</b>", customer.name]
    if customer.address:
        bill_to_lines.append(customer.address.replace("\n", "<br/>"))
    if customer.phone:
        bill_to_lines.append(customer.phone)
    if customer.email:
        bill_to_lines.append(customer.email)
    if customer.vat_number:
        bill_to_lines.append(f"VAT No: {customer.vat_number}")
    elements.append(Paragraph("<br/>".join(bill_to_lines), styles["Normal"]))
    elements.append(Spacer(1, 8 * mm))

    # Line items
    line_rows = [["Description", "Qty", "Unit Price", "Amount"]]
    for line in invoice.lines:
        line_rows.append([
            line.description,
            f"{float(line.quantity):g}",
            f"{float(line.unit_price):,.2f}",
            f"{float(line.amount):,.2f}",
        ])
    items_table = Table(line_rows, colWidths=[80 * mm, 25 * mm, 30 * mm, 35 * mm])
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 4 * mm))

    # Totals
    totals_rows = [
        ["Subtotal", f"{float(invoice.subtotal):,.2f}"],
        [f"VAT ({float(invoice.vat_rate):g}%)", f"{float(invoice.vat_amount):,.2f}"],
        ["Total", f"{float(invoice.total):,.2f}"],
        ["Paid", f"{float(invoice.amount_paid):,.2f}"],
        ["Balance Due", f"{float(invoice.balance_due):,.2f}"],
    ]
    totals_table = Table(totals_rows, colWidths=[135 * mm, 35 * mm])
    totals_table.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
        ("FONTNAME", (0, 4), (-1, 4), "Helvetica-Bold"),
        ("LINEABOVE", (0, 2), (-1, 2), 0.5, BORDER),
        ("LINEABOVE", (0, 4), (-1, 4), 0.5, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    elements.append(totals_table)

    if invoice.memo:
        elements.append(Spacer(1, 8 * mm))
        elements.append(Paragraph(f"<b>Memo:</b> {invoice.memo}", styles["Normal"]))

    if company.invoice_footer_note:
        elements.append(Spacer(1, 12 * mm))
        elements.append(Paragraph(company.invoice_footer_note, muted_style))

    doc.build(elements)
    buffer.seek(0)
    return buffer
