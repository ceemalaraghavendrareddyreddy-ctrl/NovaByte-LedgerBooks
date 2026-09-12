"""Invoice/Bill/Credit Memo PDF generation using reportlab's platypus layer
(table/paragraph flow, not raw canvas coordinates — much easier to keep aligned
as content length varies). One shared layout function; each document type just
supplies its own title, party, lines, and totals.

Invoices support three themes chosen via CompanySettings.invoice_template:
  classic  — teal accent header row (default, matches original design)
  minimal  — grayscale, hairline borders, no coloured bands
  coral    — warm coral accent, matches the LedgerBooks web UI
"""

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# Default (classic) theme values — kept in module scope so bill/credit-memo
# PDFs (which don't take a theme) still work unchanged.
ACCENT = colors.HexColor("#1a7f5a")
MUTED = colors.HexColor("#647883")
BORDER = colors.HexColor("#dce4e9")


THEMES = {
    "classic": {
        "accent": colors.HexColor("#0F766E"),
        "muted":  colors.HexColor("#647883"),
        "border": colors.HexColor("#DCE4E9"),
        "header_bg": colors.HexColor("#0F766E"),
        "header_fg": colors.white,
        "title_color": "#0F766E",
        "line_weight": 0.5,
    },
    "minimal": {
        "accent": colors.HexColor("#1F2937"),
        "muted":  colors.HexColor("#6B7280"),
        "border": colors.HexColor("#D1D5DB"),
        "header_bg": colors.white,
        "header_fg": colors.HexColor("#1F2937"),
        "title_color": "#111827",
        "line_weight": 0.3,
    },
    "coral": {
        "accent": colors.HexColor("#E85A38"),
        "muted":  colors.HexColor("#78716C"),
        "border": colors.HexColor("#F3E4DE"),
        "header_bg": colors.HexColor("#F97354"),
        "header_fg": colors.white,
        "title_color": "#E85A38",
        "line_weight": 0.5,
    },
}


def _party_lines(label, party):
    lines = [f"<b>{label}</b>", party.name]
    if getattr(party, "address", None):
        lines.append(party.address.replace("\n", "<br/>"))
    if getattr(party, "phone", None):
        lines.append(party.phone)
    if getattr(party, "email", None):
        lines.append(party.email)
    if getattr(party, "vat_number", None):
        lines.append(f"VAT No: {party.vat_number}")
    return lines


def _company_logo_flowable(company, max_height_mm=14):
    """Returns a reportlab Image built from company.logo_data (a data: URI),
    or None if no logo is set / it isn't decodable. SVG isn't rasterised by
    reportlab, so we transparently skip it."""
    data_uri = getattr(company, "logo_data", None)
    if not data_uri or not data_uri.startswith("data:"):
        return None
    try:
        header, b64 = data_uri.split(",", 1)
    except ValueError:
        return None
    if "svg" in header.lower():
        return None  # reportlab's Image can't rasterise SVG without extra deps
    import base64
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    try:
        img = Image(BytesIO(raw))
    except Exception:
        return None
    # Scale down proportionally to a sane sidebar-logo size in the header.
    max_h = max_height_mm * mm
    if img.imageHeight > max_h:
        ratio = max_h / float(img.imageHeight)
        img.drawHeight = max_h
        img.drawWidth = img.imageWidth * ratio
    return img


def _build_document_pdf(company, title, doc_no, meta_lines, party_label, party,
                        line_rows, totals_rows, memo, theme=None):
    theme = theme or THEMES["classic"]
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=20 * mm, bottomMargin=20 * mm, leftMargin=20 * mm, rightMargin=20 * mm,
    )
    styles = getSampleStyleSheet()
    muted_style = ParagraphStyle("Muted", parent=styles["Normal"], textColor=theme["muted"], fontSize=9)
    right_style = ParagraphStyle("Right", parent=styles["Normal"], alignment=TA_RIGHT)

    elements = []

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

    logo = _company_logo_flowable(company)
    if logo is not None:
        left_col = Table([[logo], [business_para]], colWidths=[100 * mm])
        left_col.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOTTOMPADDING", (0, 0), (0, 0), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ]))
    else:
        left_col = business_para

    doc_meta = Paragraph(
        f"<font color='{theme['title_color']}'><b>{title}</b></font><br/>"
        f"<b>{doc_no}</b><br/>" + "<br/>".join(meta_lines),
        right_style,
    )
    header_table = Table([[left_col, doc_meta]], colWidths=[100 * mm, 70 * mm])
    header_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    elements.append(header_table)

    # Minimal theme uses a thin rule under the header instead of coloured band.
    if theme.get("header_bg") == colors.white:
        elements.append(Spacer(1, 4 * mm))
        rule = Table([[""]], colWidths=[170 * mm], rowHeights=[0.6])
        rule.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.8, theme["accent"])]))
        elements.append(rule)
        elements.append(Spacer(1, 6 * mm))
    else:
        elements.append(Spacer(1, 10 * mm))

    elements.append(Paragraph("<br/>".join(_party_lines(party_label, party)), styles["Normal"]))
    elements.append(Spacer(1, 8 * mm))

    items_table = Table([["Description", "Qty", "Unit Price", "Amount"]] + line_rows,
                         colWidths=[80 * mm, 25 * mm, 30 * mm, 35 * mm])
    items_style = [
        ("BACKGROUND", (0, 0), (-1, 0), theme["header_bg"]),
        ("TEXTCOLOR", (0, 0), (-1, 0), theme["header_fg"]),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    if theme.get("header_bg") == colors.white:
        # Minimal: horizontal rules only, no vertical/grid lines.
        items_style += [
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, theme["accent"]),
            ("LINEBELOW", (0, 1), (-1, -1), theme["line_weight"], theme["border"]),
        ]
    else:
        items_style += [("GRID", (0, 0), (-1, -1), theme["line_weight"], theme["border"])]
    items_table.setStyle(TableStyle(items_style))
    elements.append(items_table)
    elements.append(Spacer(1, 4 * mm))

    totals_style = [
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for i, (_label, _value, is_bold) in enumerate(totals_rows):
        if is_bold:
            totals_style.append(("FONTNAME", (0, i), (-1, i), "Helvetica-Bold"))
            totals_style.append(("LINEABOVE", (0, i), (-1, i), 0.5, theme["border"]))
    totals_table = Table([[label, value] for label, value, _ in totals_rows], colWidths=[135 * mm, 35 * mm])
    totals_table.setStyle(TableStyle(totals_style))
    elements.append(totals_table)

    if memo:
        elements.append(Spacer(1, 8 * mm))
        elements.append(Paragraph(f"<b>Memo:</b> {memo}", styles["Normal"]))

    if company.invoice_footer_note:
        elements.append(Spacer(1, 12 * mm))
        elements.append(Paragraph(company.invoice_footer_note, muted_style))

    doc.build(elements)
    buffer.seek(0)
    return buffer


def _line_rows(lines):
    return [
        [line.description, f"{float(line.quantity):g}", f"{float(line.unit_price):,.2f}", f"{float(line.amount):,.2f}"]
        for line in lines
    ]


def _resolve_theme(name):
    return THEMES.get((name or "classic").lower(), THEMES["classic"])


def generate_invoice_pdf(invoice, company, template=None):
    theme = _resolve_theme(template or getattr(company, "invoice_template", None) or "classic")
    meta_lines = [
        f"Date: {invoice.invoice_date.strftime('%d %b %Y')}",
        f"Due: {invoice.due_date.strftime('%d %b %Y')}",
        f"Status: {invoice.status.upper()}",
    ]
    totals_rows = [
        ["Subtotal", f"{invoice.currency} {float(invoice.subtotal):,.2f}", False],
        [f"VAT ({float(invoice.vat_rate):g}%)", f"{invoice.currency} {float(invoice.vat_amount):,.2f}", False],
        ["Total", f"{invoice.currency} {float(invoice.total):,.2f}", True],
        ["Paid", f"{invoice.currency} {float(invoice.amount_paid):,.2f}", False],
        ["Balance Due", f"{invoice.currency} {float(invoice.balance_due):,.2f}", True],
    ]
    return _build_document_pdf(
        company, "INVOICE", invoice.invoice_no, meta_lines, "Bill To", invoice.customer,
        _line_rows(invoice.lines), totals_rows, invoice.memo,
        theme=theme,
    )


def generate_bill_pdf(bill, company):
    meta_lines = [
        f"Date: {bill.bill_date.strftime('%d %b %Y')}",
        f"Due: {bill.due_date.strftime('%d %b %Y')}",
        f"Status: {bill.status.upper()}",
    ]
    totals_rows = [
        ["Subtotal", f"{bill.currency} {float(bill.subtotal):,.2f}", False],
        [f"VAT ({float(bill.vat_rate):g}%)", f"{bill.currency} {float(bill.vat_amount):,.2f}", False],
        ["Total", f"{bill.currency} {float(bill.total):,.2f}", True],
        ["Paid", f"{bill.currency} {float(bill.amount_paid):,.2f}", False],
        ["Balance Due", f"{bill.currency} {float(bill.balance_due):,.2f}", True],
    ]
    return _build_document_pdf(
        company, "BILL", bill.bill_no, meta_lines, "Vendor", bill.vendor,
        _line_rows(bill.lines), totals_rows, bill.memo,
    )


def generate_credit_memo_pdf(credit_memo, company):
    meta_lines = [
        f"Date: {credit_memo.memo_date.strftime('%d %b %Y')}",
        f"Status: {credit_memo.status.upper()}",
    ]
    if credit_memo.reason:
        meta_lines.append(f"Reason: {credit_memo.reason}")
    totals_rows = [
        ["Subtotal", f"{credit_memo.currency} {float(credit_memo.subtotal):,.2f}", False],
        [f"VAT ({float(credit_memo.vat_rate):g}%)", f"{credit_memo.currency} {float(credit_memo.vat_amount):,.2f}", False],
        ["Total Credit", f"{credit_memo.currency} {float(credit_memo.total):,.2f}", True],
        ["Applied", f"{credit_memo.currency} {float(credit_memo.amount_applied):,.2f}", False],
        ["Remaining Credit", f"{credit_memo.currency} {float(credit_memo.remaining_credit):,.2f}", True],
    ]
    return _build_document_pdf(
        company, "CREDIT MEMO", credit_memo.memo_no, meta_lines, "Customer", credit_memo.customer,
        _line_rows(credit_memo.lines), totals_rows, credit_memo.memo,
    )
