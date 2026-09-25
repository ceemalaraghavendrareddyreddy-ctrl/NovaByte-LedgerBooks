"""MRA Statement of Goods and Services (SGS) export — the monthly purchase
listing larger VAT-registered businesses in Mauritius file with the MRA,
alongside the VAT return. Matches the column layout of MRA's own published
CSV template exactly, so the file this produces can be uploaded as-is:

  INVOICE DATE, INVOICE NUMBER, SUPPLIER NAME, SUPPLIER BRN, SUPPLIER ID,
  DESCRIPTION OF GOODS AND SERVICES, INVOICED AMOUNT EXCLUSIVE OF VAT (MUR),
  INVOICED AMOUNT OF VAT (MUR), PAID AMOUNT (MUR), INVOICE TYPE

Covers Bills (purchase invoices from vendors) — the "I" invoice type in MRA's
template — and Vendor Credits (returns/corrections against a vendor) as "N"
(note), MRA's type for a credit/debit note in the same listing.
"""
import csv
import io
from datetime import date, datetime

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required

from app.models import Bill, VendorCredit
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


def _vendor_credits_for_range(start, end):
    return (
        scoped_query(VendorCredit)
        .filter(VendorCredit.credit_date >= start, VendorCredit.credit_date <= end, VendorCredit.status != "void")
        .order_by(VendorCredit.credit_date, VendorCredit.id)
        .all()
    )


def _sgs_row(doc, *, date_field, ref, invoice_type, paid_amount):
    description = "; ".join(line.description for line in doc.lines if line.description) or doc.memo or ""
    return [
        date_field.strftime("%Y%m%d"),
        ref,
        doc.vendor.name if doc.vendor else "",
        doc.vendor.brn if doc.vendor else "",
        doc.vendor.mra_supplier_id if doc.vendor else "",
        description,
        f"{doc.subtotal:.2f}",
        f"{doc.vat_amount:.2f}",
        f"{paid_amount:.2f}",
        invoice_type,
    ]


def _bill_row(bill):
    return _sgs_row(
        bill, date_field=bill.bill_date, ref=bill.vendor_ref or bill.bill_no,
        invoice_type="I", paid_amount=bill.amount_paid,
    )


def _vendor_credit_row(credit):
    # A vendor credit has no "paid" concept of its own — amount_applied (how much of
    # it has actually offset a vendor's bills) is the closest equivalent.
    return _sgs_row(
        credit, date_field=credit.credit_date, ref=credit.credit_no,
        invoice_type="N", paid_amount=credit.amount_applied,
    )


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
    vendor_credits = _vendor_credits_for_range(start, end)
    missing_brn = [d for d in (bills + vendor_credits) if not (d.vendor and d.vendor.brn)]

    return render_template(
        "reports/mra_sgs.html", bills=bills, vendor_credits=vendor_credits, start=start, end=end,
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
    vendor_credits = _vendor_credits_for_range(start, end)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(SGS_HEADER)
    for bill in bills:
        writer.writerow(_bill_row(bill))
    for credit in vendor_credits:
        writer.writerow(_vendor_credit_row(credit))

    data = io.BytesIO(buffer.getvalue().encode("utf-8"))
    filename = f"SGS_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.csv"
    return send_file(data, as_attachment=True, download_name=filename, mimetype="text/csv")
