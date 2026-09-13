"""MRA Statement of Goods and Services (SGS) export — the monthly purchase
listing larger VAT-registered businesses in Mauritius file with the MRA,
alongside the VAT return. Matches the column layout of MRA's own published
CSV template exactly, so the file this produces can be uploaded as-is:

  INVOICE DATE, INVOICE NUMBER, SUPPLIER NAME, SUPPLIER BRN, SUPPLIER ID,
  DESCRIPTION OF GOODS AND SERVICES, INVOICED AMOUNT EXCLUSIVE OF VAT (MUR),
  INVOICED AMOUNT OF VAT (MUR), PAID AMOUNT (MUR), INVOICE TYPE

Covers Bills (purchase invoices from vendors) — the "I" invoice type in
MRA's template. Vendor credits/returns aren't included yet.
"""
import csv
import io
from datetime import date, datetime

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required

from app.models import Bill
from app.scoping import scoped_query

mra_sgs_bp = Blueprint("mra_sgs", __name__, url_prefix="/reports/mra-sgs")

SGS_HEADER = [
    "INVOICE DATE", "INVOICE NUMBER", "SUPPLIER NAME", "SUPPLIER BRN", "SUPPLIER ID",
    "DESCRIPTION OF GOODS AND SERVICES", "INVOICED AMOUNT EXCLUSIVE OF VAT (MUR)",
    "INVOICED AMOUNT OF VAT (MUR)", "PAID AMOUNT (MUR)", "INVOICE TYPE",
]


def _bills_for_range(start, end):
    return (
        scoped_query(Bill)
        .filter(Bill.bill_date >= start, Bill.bill_date <= end, Bill.status != "void")
        .order_by(Bill.bill_date, Bill.id)
        .all()
    )


def _sgs_row(bill):
    description = "; ".join(line.description for line in bill.lines if line.description) or bill.memo or ""
    return [
        bill.bill_date.strftime("%Y%m%d"),
        bill.vendor_ref or bill.bill_no,
        bill.vendor.name if bill.vendor else "",
        bill.vendor.brn if bill.vendor else "",
        bill.vendor.mra_supplier_id if bill.vendor else "",
        description,
        f"{bill.subtotal:.2f}",
        f"{bill.vat_amount:.2f}",
        f"{bill.amount_paid:.2f}",
        "I",
    ]


@mra_sgs_bp.route("")
@login_required
def home():
    today = date.today()
    month_start = today.replace(day=1)
    start_raw = request.args.get("start", month_start.isoformat())
    end_raw = request.args.get("end", today.isoformat())
    start = datetime.strptime(start_raw, "%Y-%m-%d").date()
    end = datetime.strptime(end_raw, "%Y-%m-%d").date()

    bills = _bills_for_range(start, end)
    missing_brn = [b for b in bills if not (b.vendor and b.vendor.brn)]

    return render_template(
        "reports/mra_sgs.html", bills=bills, start=start, end=end,
        missing_brn=missing_brn, header=SGS_HEADER,
    )


@mra_sgs_bp.route("/export.csv")
@login_required
def export_csv():
    start_raw = request.args.get("start")
    end_raw = request.args.get("end")
    if not start_raw or not end_raw:
        flash("Choose a date range first.", "error")
        return redirect(url_for("mra_sgs.home"))

    start = datetime.strptime(start_raw, "%Y-%m-%d").date()
    end = datetime.strptime(end_raw, "%Y-%m-%d").date()
    bills = _bills_for_range(start, end)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(SGS_HEADER)
    for bill in bills:
        writer.writerow(_sgs_row(bill))

    data = io.BytesIO(buffer.getvalue().encode("utf-8"))
    filename = f"SGS_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.csv"
    return send_file(data, as_attachment=True, download_name=filename, mimetype="text/csv")
