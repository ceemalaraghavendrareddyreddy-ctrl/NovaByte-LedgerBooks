"""Public, no-login routes for viewing one shared document's PDF — see
share_links.py for how the token is signed and verified. Deliberately outside
any @login_required boundary: this is the whole point (a customer or vendor
opening a WhatsApp link shouldn't need a LedgerBooks account), so every route
here MUST verify its token itself rather than relying on session/company
scoping from app.scoping (which assumes a logged-in user).
"""
from flask import Blueprint, abort, send_file

from app.models import Bill, CompanySettings, CreditMemo, Invoice
from app.pdf import generate_bill_pdf, generate_credit_memo_pdf, generate_invoice_pdf
from app.share_links import verify_share_token

share_bp = Blueprint("share", __name__, url_prefix="/share")


@share_bp.route("/invoice/<int:doc_id>/<token>")
def invoice_pdf(doc_id, token):
    result = verify_share_token(token, "invoice")
    if not result or result[0] != doc_id:
        abort(404)
    _, company_id = result
    invoice = Invoice.query.filter_by(id=doc_id, company_id=company_id).first_or_404()
    company = CompanySettings.get_by_id(company_id)
    buffer = generate_invoice_pdf(invoice, company)
    return send_file(buffer, mimetype="application/pdf", as_attachment=False, download_name=f"{invoice.invoice_no}.pdf")


@share_bp.route("/bill/<int:doc_id>/<token>")
def bill_pdf(doc_id, token):
    result = verify_share_token(token, "bill")
    if not result or result[0] != doc_id:
        abort(404)
    _, company_id = result
    bill = Bill.query.filter_by(id=doc_id, company_id=company_id).first_or_404()
    company = CompanySettings.get_by_id(company_id)
    buffer = generate_bill_pdf(bill, company)
    return send_file(buffer, mimetype="application/pdf", as_attachment=False, download_name=f"{bill.bill_no}.pdf")


@share_bp.route("/credit-memo/<int:doc_id>/<token>")
def credit_memo_pdf(doc_id, token):
    result = verify_share_token(token, "credit_memo")
    if not result or result[0] != doc_id:
        abort(404)
    _, company_id = result
    credit_memo = CreditMemo.query.filter_by(id=doc_id, company_id=company_id).first_or_404()
    company = CompanySettings.get_by_id(company_id)
    buffer = generate_credit_memo_pdf(credit_memo, company)
    return send_file(buffer, mimetype="application/pdf", as_attachment=False, download_name=f"{credit_memo.credit_no}.pdf")
