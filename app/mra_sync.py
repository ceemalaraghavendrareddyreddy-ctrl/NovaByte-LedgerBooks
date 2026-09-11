"""Retry queue for LedgerBooks -> MRA_TaxInvoice_System bridge failures.

Distinct from MRA_TaxInvoice_System's own internal offline queue: that one covers
failures between MRA_TaxInvoice_System and the real MRA server. This one covers
failures between LedgerBooks and MRA_TaxInvoice_System itself — e.g. it was
temporarily down at the moment an invoice was posted here.
"""
from flask import Blueprint, flash, redirect, render_template, url_for
from flask_login import login_required

from app import db
from app.auth import current_company_id
from app.models import Invoice, CreditMemo
from app.mra_bridge import fiscalize_invoice_with_mra, fiscalize_credit_memo_with_mra

mra_sync_bp = Blueprint("mra_sync", __name__, url_prefix="/mra-sync")


@mra_sync_bp.route("/")
@login_required
def sync_status():
    failed_invoices = (
        Invoice.query.filter_by(company_id=current_company_id(), mra_status="ERROR").order_by(Invoice.id).all()
    )
    failed_credit_memos = (
        CreditMemo.query.filter_by(company_id=current_company_id(), mra_status="ERROR").order_by(CreditMemo.id).all()
    )
    return render_template(
        "mra_sync/status.html", failed_invoices=failed_invoices, failed_credit_memos=failed_credit_memos,
    )


@mra_sync_bp.route("/retry", methods=["POST"])
@login_required
def retry_all():
    # Oldest first, and invoices before credit memos — credit memos may reference an
    # invoice that itself is still pending, so giving invoices first crack at succeeding
    # gives the credit memo a real reference to use if it retries right after.
    retried, still_failed = 0, 0

    for invoice in Invoice.query.filter_by(company_id=current_company_id(), mra_status="ERROR").order_by(Invoice.id).all():
        fiscalize_invoice_with_mra(invoice)
        db.session.commit()
        if invoice.mra_status == "ERROR":
            still_failed += 1
        else:
            retried += 1

    for credit_memo in CreditMemo.query.filter_by(company_id=current_company_id(), mra_status="ERROR").order_by(CreditMemo.id).all():
        fiscalize_credit_memo_with_mra(credit_memo)
        db.session.commit()
        if credit_memo.mra_status == "ERROR":
            still_failed += 1
        else:
            retried += 1

    if retried:
        flash(f"{retried} record(s) fiscalised successfully.", "success")
    if still_failed:
        flash(f"{still_failed} record(s) still couldn't reach MRA_TaxInvoice_System.", "error")
    if not retried and not still_failed:
        flash("Nothing pending to retry.", "success")

    return redirect(url_for("mra_sync.sync_status"))
