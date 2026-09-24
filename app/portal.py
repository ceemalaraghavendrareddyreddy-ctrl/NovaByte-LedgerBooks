"""Customer/Vendor portal — a read-only, no-staff-login view of "your own invoices"
or "your own bills", for the actual customer/vendor rather than anyone on the
company's team. Deliberately a separate authentication track from the staff
(User/Flask-Login) system: a portal session only ever grants access to ONE
customer's or vendor's own records, scoped by (id, company_id) checked on every
request — never the broader company-wide access a staff login has.

Login is by email + a password the account owner sets from that customer's/vendor's
own page (Sales → Customers → ... → Portal Access) — there's no self-registration
and no "forgot password" email flow yet; resetting one still requires the owner.
PDFs reuse the existing signed share-link routes (app/share.py) rather than
duplicating PDF generation here.
"""
from datetime import datetime
from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from werkzeug.security import check_password_hash

from app import db
from app.audit import log_audit
from app.mra_bridge import fiscalize_invoice_with_mra
from app.models import Bill, CompanySettings, Customer, Estimate, Invoice, Vendor
from app.share_links import share_url

portal_bp = Blueprint("portal", __name__, url_prefix="/portal")


def _company_name(company_id):
    company = CompanySettings.get_by_id(company_id)
    return company.business_name if company else "Your Company"


# ── Customer portal ─────────────────────────────────────────────────

def portal_customer_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        customer = current_portal_customer()
        if not customer:
            session.pop("portal_customer_id", None)
            flash("Please sign in to view your account.", "error")
            return redirect(url_for("portal.customer_login"))
        return view_func(*args, **kwargs)
    return wrapped


def current_portal_customer():
    customer_id = session.get("portal_customer_id")
    if not customer_id:
        return None
    customer = Customer.query.get(customer_id)
    # Re-checked every request, not just at login — if the owner disables portal
    # access after someone's already signed in, that session stops working immediately.
    if not customer or not customer.portal_enabled:
        return None
    return customer


@portal_bp.route("/login", methods=["GET", "POST"])
def customer_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        customer = (
            Customer.query.filter(Customer.portal_enabled == True, Customer.email.isnot(None))  # noqa: E712
            .filter(db.func.lower(Customer.email) == email)
            .first()
        )
        if customer and customer.portal_password_hash and check_password_hash(customer.portal_password_hash, password):
            session["portal_customer_id"] = customer.id
            return redirect(url_for("portal.customer_dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("portal/customer_login.html")


@portal_bp.route("/logout")
def customer_logout():
    session.pop("portal_customer_id", None)
    return redirect(url_for("portal.customer_login"))


@portal_bp.route("/")
@portal_customer_required
def customer_dashboard():
    customer = current_portal_customer()
    invoices = sorted(
        [inv for inv in customer.invoices if inv.status != "void"], key=lambda i: i.invoice_date, reverse=True,
    )
    estimates = sorted(
        Estimate.query.filter_by(customer_id=customer.id).all(), key=lambda e: e.estimate_date, reverse=True,
    )
    return render_template(
        "portal/customer_dashboard.html", customer=customer, invoices=invoices, estimates=estimates,
        company_name=_company_name(customer.company_id), portal_kind="customer",
        logout_url=url_for("portal.customer_logout"),
    )


@portal_bp.route("/estimates/<int:estimate_id>")
@portal_customer_required
def customer_estimate(estimate_id):
    customer = current_portal_customer()
    estimate = Estimate.query.filter_by(id=estimate_id, customer_id=customer.id).first_or_404()
    return render_template(
        "portal/customer_estimate.html", customer=customer, estimate=estimate,
        company_name=_company_name(customer.company_id), portal_kind="customer",
        logout_url=url_for("portal.customer_logout"),
    )


@portal_bp.route("/estimates/<int:estimate_id>/sign", methods=["POST"])
@portal_customer_required
def customer_estimate_sign(estimate_id):
    """E-signature: capture the drawn signature + typed name, mark the estimate
    accepted, and auto-convert it to an invoice — the same conversion staff use
    (see sales._convert_estimate_to_invoice), just triggered by the customer's own
    click instead of a staff member's."""
    customer = current_portal_customer()
    estimate = Estimate.query.filter_by(id=estimate_id, customer_id=customer.id).first_or_404()

    if estimate.status in ("converted", "declined"):
        flash("This estimate can no longer be signed.", "error")
        return redirect(url_for("portal.customer_estimate", estimate_id=estimate.id))

    signature_data = request.form.get("signature_data", "").strip()
    signed_by_name = request.form.get("signed_by_name", "").strip()
    if not signature_data or not signed_by_name:
        flash("Draw your signature and type your name to accept this estimate.", "error")
        return redirect(url_for("portal.customer_estimate", estimate_id=estimate.id))

    from app.sales import _convert_estimate_to_invoice

    # post_invoice/log_audit resolve the active company from the session, exactly like
    # every staff route does — there's no such thing in a portal session, so it's set
    # here for just this one request and popped again immediately after, win or lose.
    session["company_id"] = estimate.company_id
    try:
        invoice, error = _convert_estimate_to_invoice(estimate)
        if error:
            flash(error, "error")
            return redirect(url_for("portal.customer_estimate", estimate_id=estimate.id))

        estimate.signature_data = signature_data
        estimate.signed_by_name = signed_by_name
        estimate.signed_at = datetime.utcnow()
        estimate.signed_ip = request.remote_addr
        log_audit(
            "sign", "estimate", estimate.id,
            f"Signed by {signed_by_name} via customer portal, converted to invoice {invoice.invoice_no}",
            estimate.estimate_no,
        )
        db.session.commit()

        fiscalize_invoice_with_mra(invoice)
        db.session.commit()
    finally:
        session.pop("company_id", None)

    flash(f"Signed and accepted — invoice {invoice.invoice_no} has been created.", "success")
    return redirect(url_for("portal.customer_estimate", estimate_id=estimate.id))


@portal_bp.route("/invoices/<int:invoice_id>")
@portal_customer_required
def customer_invoice(invoice_id):
    customer = current_portal_customer()
    invoice = Invoice.query.filter_by(id=invoice_id, customer_id=customer.id).first_or_404()
    pdf_url = share_url("invoice", invoice.id, invoice.company_id, "share.invoice_pdf")
    return render_template(
        "portal/customer_invoice.html", customer=customer, invoice=invoice, pdf_url=pdf_url,
        company_name=_company_name(customer.company_id), portal_kind="customer",
        logout_url=url_for("portal.customer_logout"),
    )


@portal_bp.route("/statement")
@portal_customer_required
def customer_statement():
    from datetime import date, datetime
    from app.sales import _customer_statement_data

    customer = current_portal_customer()
    start_raw = request.args.get("start_date")
    end_raw = request.args.get("end_date")
    start_date = datetime.strptime(start_raw, "%Y-%m-%d").date() if start_raw else date(date.today().year, 1, 1)
    end_date = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else date.today()

    opening_balance, rows, closing_balance = _customer_statement_data(customer, start_date, end_date)
    return render_template(
        "portal/customer_statement.html", customer=customer, rows=rows,
        opening_balance=opening_balance, closing_balance=closing_balance,
        start_date=start_date.isoformat(), end_date=end_date.isoformat(),
        company_name=_company_name(customer.company_id), portal_kind="customer",
        logout_url=url_for("portal.customer_logout"),
    )


# ── Vendor portal ────────────────────────────────────────────────────

def portal_vendor_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        vendor = current_portal_vendor()
        if not vendor:
            session.pop("portal_vendor_id", None)
            flash("Please sign in to view your account.", "error")
            return redirect(url_for("portal.vendor_login"))
        return view_func(*args, **kwargs)
    return wrapped


def current_portal_vendor():
    vendor_id = session.get("portal_vendor_id")
    if not vendor_id:
        return None
    vendor = Vendor.query.get(vendor_id)
    if not vendor or not vendor.portal_enabled:
        return None
    return vendor


@portal_bp.route("/vendor/login", methods=["GET", "POST"])
def vendor_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        vendor = (
            Vendor.query.filter(Vendor.portal_enabled == True, Vendor.email.isnot(None))  # noqa: E712
            .filter(db.func.lower(Vendor.email) == email)
            .first()
        )
        if vendor and vendor.portal_password_hash and check_password_hash(vendor.portal_password_hash, password):
            session["portal_vendor_id"] = vendor.id
            return redirect(url_for("portal.vendor_dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("portal/vendor_login.html")


@portal_bp.route("/vendor/logout")
def vendor_logout():
    session.pop("portal_vendor_id", None)
    return redirect(url_for("portal.vendor_login"))


@portal_bp.route("/vendor")
@portal_vendor_required
def vendor_dashboard():
    vendor = current_portal_vendor()
    bills = sorted([b for b in vendor.bills if b.status != "void"], key=lambda b: b.bill_date, reverse=True)
    return render_template(
        "portal/vendor_dashboard.html", vendor=vendor, bills=bills,
        company_name=_company_name(vendor.company_id), portal_kind="vendor",
        logout_url=url_for("portal.vendor_logout"),
    )


@portal_bp.route("/vendor/bills/<int:bill_id>")
@portal_vendor_required
def vendor_bill(bill_id):
    vendor = current_portal_vendor()
    bill = Bill.query.filter_by(id=bill_id, vendor_id=vendor.id).first_or_404()
    pdf_url = share_url("bill", bill.id, bill.company_id, "share.bill_pdf")
    return render_template(
        "portal/vendor_bill.html", vendor=vendor, bill=bill, pdf_url=pdf_url,
        company_name=_company_name(vendor.company_id), portal_kind="vendor",
        logout_url=url_for("portal.vendor_logout"),
    )


@portal_bp.route("/vendor/statement")
@portal_vendor_required
def vendor_statement():
    from datetime import date, datetime
    from app.purchases import _vendor_statement_data

    vendor = current_portal_vendor()
    start_raw = request.args.get("start_date")
    end_raw = request.args.get("end_date")
    start_date = datetime.strptime(start_raw, "%Y-%m-%d").date() if start_raw else date(date.today().year, 1, 1)
    end_date = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else date.today()

    opening_balance, rows, closing_balance = _vendor_statement_data(vendor, start_date, end_date)
    return render_template(
        "portal/vendor_statement.html", vendor=vendor, rows=rows,
        opening_balance=opening_balance, closing_balance=closing_balance,
        start_date=start_date.isoformat(), end_date=end_date.isoformat(),
        company_name=_company_name(vendor.company_id), portal_kind="vendor",
        logout_url=url_for("portal.vendor_logout"),
    )
