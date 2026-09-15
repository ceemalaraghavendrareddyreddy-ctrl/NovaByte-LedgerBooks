"""Anomaly detection — the third mocked "AI model" feature, same rule as
app/ai_suggest.py and app/ai_qa.py: no external model call yet, just rules
a real model would eventually make smarter, not more honest. Every flagged
item here is a genuine statistical check against this company's own real
data — "3x this customer's usual invoice size" is computed from their
actual invoice history, not a guess.

Three checks, each cheap enough to run on every page load for a company
this size:

  1. Unusually large invoice — more than 3x a customer's own average of
     their other invoices (needs at least 2 prior invoices to have an
     average worth comparing against).
  2. Unusually large bill — same idea, vendor-side.
  3. Possible duplicate bill — same vendor, same total, within 7 days of
     each other. Catches the classic double-entry mistake.
"""
from datetime import timedelta

from flask import Blueprint, render_template
from flask_login import login_required

from app.models import Bill, Invoice
from app.scoping import scoped_query

ai_anomalies_bp = Blueprint("ai_anomalies", __name__, url_prefix="/reports/anomalies")

LARGE_MULTIPLE = 3.0


def _large_document_flags(documents, get_party_id, get_amount, label, detail_url_endpoint, get_no):
    """Shared logic for "this one's much bigger than usual for this party" —
    used for both Invoice/Customer and Bill/Vendor."""
    by_party = {}
    for doc in documents:
        by_party.setdefault(get_party_id(doc), []).append(doc)

    flags = []
    for party_docs in by_party.values():
        if len(party_docs) < 3:
            continue  # not enough history to know what's "usual" yet
        for doc in party_docs:
            others = [d for d in party_docs if d is not doc]
            avg_others = sum((get_amount(d) for d in others), start=0.0) / len(others)
            amount = get_amount(doc)
            if avg_others > 0 and amount > avg_others * LARGE_MULTIPLE:
                flags.append({
                    "label": label, "doc": doc, "no": get_no(doc), "amount": amount,
                    "average": avg_others, "endpoint": detail_url_endpoint,
                })
    return flags


def _duplicate_bill_flags(bills):
    flags = []
    seen = []
    for bill in sorted(bills, key=lambda b: b.bill_date):
        for other in seen:
            if (
                other.vendor_id == bill.vendor_id
                and abs(bill.total - other.total) < 0.01
                and abs((bill.bill_date - other.bill_date).days) <= 7
            ):
                flags.append({
                    "label": "Possible duplicate bill", "doc": bill, "no": bill.bill_no,
                    "amount": bill.total, "duplicate_of": other.bill_no,
                })
        seen.append(bill)
    return flags


@ai_anomalies_bp.route("")
@login_required
def anomalies():
    invoices = scoped_query(Invoice).filter(Invoice.status != "void").all()
    bills = scoped_query(Bill).filter(Bill.status != "void").all()

    invoice_flags = _large_document_flags(
        invoices, lambda i: i.customer_id, lambda i: i.total_base,
        "Unusually large invoice", "sales.invoice_detail", lambda i: i.invoice_no,
    )
    bill_flags = _large_document_flags(
        bills, lambda b: b.vendor_id, lambda b: b.total_base,
        "Unusually large bill", "purchases.bill_detail", lambda b: b.bill_no,
    )
    duplicate_flags = _duplicate_bill_flags(bills)

    return render_template(
        "ai/anomalies.html",
        invoice_flags=invoice_flags, bill_flags=bill_flags, duplicate_flags=duplicate_flags,
    )
