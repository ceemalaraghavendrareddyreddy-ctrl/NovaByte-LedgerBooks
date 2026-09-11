"""Invoice/Bill/Credit Memo PDF generation using reportlab's platypus layer
(table/paragraph flow, not raw canvas coordinates — much easier to keep aligned
as content length varies). One shared layout function; each document type just
supplies its own title, party, lines, and totals."""

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


def _party_lines(label, party):
    """Renders a Bill To / Vendor block from anything with .name/.address/.phone/.email/.vat_number."""
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


def _build_document_pdf(company, title, doc_no, meta_lines, party_label, party, line_rows, totals_rows, memo):
    """Shared layout for every document type below. meta_lines is the right-hand header
    block (date/due/status etc, already formatted); totals_rows is a list of [label, value]
    pairs, with the grand-total row's index passed separately so it can be bolded."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=20 * mm, bottomMargin=20 * mm, leftMargin=20 * mm, rightMargin=20 * mm,
    )
    styles = getSampleStyleSheet()
    muted_style = ParagraphStyle("Muted", parent=styles["Normal"], textColor=MUTED, fontSize=9)
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

    doc_meta = Paragraph(
        f"<font color='#1a7f5a'><b>{title}</b></font><br/>"
        f"<b>{doc_no}</b><br/>" + "<br/>".join(meta_lines),
        right_style,
    )

    header_table = Table([[business_para, doc_meta]], colWidths=[100 * mm, 70 * mm])
    header_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    elements.append(header_table)
    elements.append(Spacer(1, 10 * mm))

    elements.append(Paragraph("<br/>".join(_party_lines(party_label, party)), styles["Normal"]))
    elements.append(Spacer(1, 8 * mm))

    items_table = Table([["Description", "Qty", "Unit Price", "Amount"]] + line_rows,
                         colWidths=[80 * mm, 25 * mm, 30 * mm, 35 * mm])
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

    totals_style = [
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for i, (_label, _value, is_bold) in enumerate(totals_rows):
        if is_bold:
            totals_style.append(("FONTNAME", (0, i), (-1, i), "Helvetica-Bold"))
            totals_style.append(("LINEABOVE", (0, i), (-1, i), 0.5, BORDER))
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


def generate_invoice_pdf(invoice, company):
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
    )


def generate_bill_pdf(bill, company):
    meta_lines = [
        f"Date: {bill.bill_date.strftime('%d %b %Y')}",
        f"Due: {bill.due_date.strftime('%d %b %Y')}",
        f"Status: {bill.status.upper()}",
    ] + ([f"Vendor Ref: {bill.vendor_ref}"] if bill.vendor_ref else [])
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
        f"Date: {credit_memo.credit_date.strftime('%d %b %Y')}",
        f"Status: {credit_memo.status.upper()}",
    ] + ([f"Ref. Invoice: {credit_memo.related_invoice.invoice_no}"] if credit_memo.related_invoice else [])
    totals_rows = [
        ["Subtotal", f"{float(credit_memo.subtotal):,.2f}", False],
        [f"VAT ({float(credit_memo.vat_rate):g}%)", f"{float(credit_memo.vat_amount):,.2f}", False],
        ["Total Credit", f"{float(credit_memo.total):,.2f}", True],
        ["Applied", f"{float(credit_memo.amount_applied):,.2f}", False],
        ["Remaining Credit", f"{float(credit_memo.remaining_credit):,.2f}", True],
    ]
    return _build_document_pdf(
        company, "CREDIT MEMO", credit_memo.credit_no, meta_lines, "Customer", credit_memo.customer,
        _line_rows(credit_memo.lines), totals_rows, credit_memo.memo,
    )
