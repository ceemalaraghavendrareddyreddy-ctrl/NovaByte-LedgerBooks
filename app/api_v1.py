"""Public API — GET (read) and POST (write) on core resources, authenticated
by an X-Api-Key header matched against the ApiKey table (Settings -> API Keys).

Read endpoints need any active key. Write endpoints need a "read_write" scoped
key — a "read"-scoped key gets a 403, not silently reduced access, so a
third-party integration handed a read-only key fails loudly and immediately
if it tries to write, rather than mysteriously not creating anything.

Scope, deliberately: customers, vendors, invoices, and payments — the actual
core of "an external system needs to get a customer into LedgerBooks, bill
them, and record that they paid." Bills/items/vendor payments follow the same
pattern and are a natural v2, not built here to keep this reviewable as one
complete, tested slice rather than a dozen half-covered endpoints.

Every write endpoint mirrors the exact same validation and ledger-posting
logic the staff UI uses (app/sales.py's post_invoice/post_payment, the same
stock-availability guard, the same MRA fiscalisation attempt on a new
invoice) — this is not a shortcut path that skips checks a human would hit.
"""
import secrets
from datetime import date, datetime

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import login_required

from app import db
from app.audit import log_audit
from app.auth import current_company_id, owner_required, staff_session_scope
from app.models import ApiKey, Bill, CompanySettings, Customer, Invoice, InvoiceLine, Item, Payment, PaymentApplication, Vendor
from app.mra_bridge import fiscalize_invoice_with_mra

api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")


def _authenticate():
    """Returns (company, api_key_row_or_None) or (None, None). api_key_row is
    None only for the legacy CompanySettings.api_key fallback, which is
    treated as read-only (can_write is only ever true on a real ApiKey row)."""
    raw_key = request.headers.get("X-Api-Key")
    if not raw_key:
        return None, None

    key_row = ApiKey.get_by_key(raw_key)
    if key_row:
        key_row.last_used_at = datetime.utcnow()
        db.session.commit()
        return CompanySettings.get_by_id(key_row.company_id), key_row

    legacy_company = CompanySettings.get_by_api_key(raw_key)
    if legacy_company:
        return legacy_company, None
    return None, None


def _require_write(key_row):
    if not key_row or not key_row.can_write:
        abort(403, description="This API key is read-only. Use a read_write scoped key to create or update records.")


def _paginate(query):
    page = request.args.get("page", 1, type=int) or 1
    per_page = min(request.args.get("per_page", 50, type=int) or 50, 200)
    return query.limit(per_page).offset((page - 1) * per_page).all()


@api_v1_bp.route("/customers")
def list_customers():
    company, _key = _authenticate()
    if not company:
        return jsonify({"error": "Invalid or missing API key."}), 401
    rows = _paginate(Customer.query.filter_by(company_id=company.id).order_by(Customer.id))
    return jsonify([{
        "id": c.id, "name": c.name, "customer_type": c.customer_type, "email": c.email,
        "phone": c.phone, "billing_currency": c.billing_currency, "balance_due": c.balance_due,
        "is_active": c.is_active,
    } for c in rows])


@api_v1_bp.route("/customers", methods=["POST"])
def create_customer():
    company, key = _authenticate()
    if not company:
        return jsonify({"error": "Invalid or missing API key."}), 401
    _require_write(key)

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required."}), 400

    customer = Customer(
        company_id=company.id,
        name=name,
        customer_type=data.get("customer_type") if data.get("customer_type") in ("individual", "company") else "company",
        email=(data.get("email") or "").strip() or None,
        phone=(data.get("phone") or "").strip() or None,
        address=(data.get("address") or "").strip() or None,
        vat_number=(data.get("vat_number") or "").strip() or None,
        brn=(data.get("brn") or "").strip() or None,
        billing_currency=data.get("billing_currency") or company.base_currency,
    )
    db.session.add(customer)
    db.session.flush()
    # log_audit() reads current_company_id() -> session["company_id"], which a pure
    # X-Api-Key request never has (no Flask session at all) — see staff_session_scope.
    with staff_session_scope(company.id):
        log_audit("create", "customer", customer.id, f"Created customer {customer.name} via API")
    db.session.commit()
    return jsonify({"id": customer.id, "name": customer.name}), 201


@api_v1_bp.route("/vendors")
def list_vendors():
    company, _key = _authenticate()
    if not company:
        return jsonify({"error": "Invalid or missing API key."}), 401
    rows = _paginate(Vendor.query.filter_by(company_id=company.id).order_by(Vendor.id))
    return jsonify([{
        "id": v.id, "name": v.name, "vendor_type": v.vendor_type, "email": v.email,
        "phone": v.phone, "billing_currency": v.billing_currency, "balance_due": v.balance_due,
        "is_active": v.is_active,
    } for v in rows])


@api_v1_bp.route("/items")
def list_items():
    company, _key = _authenticate()
    if not company:
        return jsonify({"error": "Invalid or missing API key."}), 401
    rows = _paginate(Item.query.filter_by(company_id=company.id).order_by(Item.id))
    return jsonify([{
        "id": i.id, "sku": i.sku, "name": i.name, "item_type": i.item_type, "unit": i.unit,
        "sales_price": float(i.sales_price), "quantity_on_hand": float(i.quantity_on_hand) if i.is_tracked else None,
        "is_active": i.is_active,
    } for i in rows])


@api_v1_bp.route("/invoices")
def list_invoices():
    company, _key = _authenticate()
    if not company:
        return jsonify({"error": "Invalid or missing API key."}), 401
    rows = _paginate(Invoice.query.filter_by(company_id=company.id).order_by(Invoice.id.desc()))
    return jsonify([{
        "id": inv.id, "invoice_no": inv.invoice_no, "customer": inv.customer.name,
        "invoice_date": inv.invoice_date.isoformat(), "due_date": inv.due_date.isoformat(),
        "currency": inv.currency, "total": inv.total, "balance_due": inv.balance_due, "status": inv.status,
    } for inv in rows])


@api_v1_bp.route("/invoices", methods=["POST"])
def create_invoice():
    """Creates and posts a real invoice — same effect as a staff member using
    Sales -> New Invoice, including the tracked-stock availability guard and an
    MRA fiscalisation attempt. Does NOT go through the invoice-approval
    workflow (Settings -> invoice_approval_enabled) even if a company has that
    turned on for the staff UI — a v2 concern, flagged here rather than silently
    ignored."""
    company, key = _authenticate()
    if not company:
        return jsonify({"error": "Invalid or missing API key."}), 401
    _require_write(key)

    data = request.get_json(silent=True) or {}
    customer_id = data.get("customer_id")
    customer = Customer.query.filter_by(id=customer_id, company_id=company.id).first() if customer_id else None
    if not customer:
        return jsonify({"error": "customer_id is required and must belong to this company."}), 400

    raw_lines = data.get("lines") or []
    if not raw_lines:
        return jsonify({"error": "lines is required and must be non-empty."}), 400

    from app.sales import next_invoice_no, post_invoice, DEFAULT_INCOME_CODE, get_account_or_400

    with staff_session_scope(company.id):
        default_income = get_account_or_400(DEFAULT_INCOME_CODE, "Sales Revenue")

        try:
            invoice_date_raw = data.get("invoice_date")
            invoice_date = datetime.strptime(invoice_date_raw, "%Y-%m-%d").date() if invoice_date_raw else date.today()
            due_date_raw = data.get("due_date")
            due_date = datetime.strptime(due_date_raw, "%Y-%m-%d").date() if due_date_raw else invoice_date
        except ValueError:
            return jsonify({"error": "invoice_date/due_date must be YYYY-MM-DD."}), 400

        invoice = Invoice(
            company_id=company.id,
            invoice_no=next_invoice_no(),
            customer_id=customer.id,
            invoice_date=invoice_date,
            due_date=due_date,
            currency=data.get("currency") or customer.billing_currency,
            vat_rate=data.get("vat_rate", 15.00),
            memo=(data.get("memo") or "").strip() or None,
        )

        try:
            for raw_line in raw_lines:
                description = (raw_line.get("description") or "").strip()
                quantity = float(raw_line.get("quantity", 1))
                unit_price = float(raw_line.get("unit_price", 0))
                if not description or quantity <= 0:
                    return jsonify({"error": "Each line needs a description and a positive quantity."}), 400
                item = None
                item_id = raw_line.get("item_id")
                if item_id:
                    item = Item.query.filter_by(id=item_id, company_id=company.id).first()
                    if not item:
                        return jsonify({"error": f"item_id {item_id} not found."}), 400
                income_account_id = item.income_account_id if item else (raw_line.get("income_account_id") or default_income.id)
                invoice.lines.append(InvoiceLine(
                    item_id=item.id if item else None, description=description, quantity=quantity,
                    unit_price=unit_price, taxable=raw_line.get("taxable", True), income_account_id=income_account_id,
                ))
        except (TypeError, ValueError):
            return jsonify({"error": "quantity and unit_price must be numbers."}), 400

        # Same stock-availability guard the staff invoice form and Sales Order
        # conversion both use — a tracked item can't be invoiced past what's on hand.
        required_qty = {}
        for line in invoice.lines:
            if line.item_id:
                item = Item.query.get(line.item_id)
                if item.is_tracked:
                    required_qty[item] = required_qty.get(item, 0) + float(line.quantity)
        for item, needed in required_qty.items():
            if needed > float(item.quantity_on_hand):
                return jsonify({
                    "error": f"Not enough stock for {item.name} ({item.sku}) — "
                             f"have {item.quantity_on_hand} {item.unit}, need {needed}.",
                }), 400

        db.session.add(invoice)
        db.session.flush()
        post_invoice(invoice)
        log_audit("create", "invoice", invoice.id, f"Created invoice {invoice.invoice_no} via API")
        db.session.commit()

    fiscalize_invoice_with_mra(invoice)
    db.session.commit()

    return jsonify({
        "id": invoice.id, "invoice_no": invoice.invoice_no, "total": invoice.total,
        "balance_due": invoice.balance_due, "status": invoice.status,
    }), 201


@api_v1_bp.route("/bills")
def list_bills():
    company, _key = _authenticate()
    if not company:
        return jsonify({"error": "Invalid or missing API key."}), 401
    rows = _paginate(Bill.query.filter_by(company_id=company.id).order_by(Bill.id.desc()))
    return jsonify([{
        "id": bill.id, "bill_no": bill.bill_no, "vendor": bill.vendor.name,
        "bill_date": bill.bill_date.isoformat(), "due_date": bill.due_date.isoformat(),
        "currency": bill.currency, "total": bill.total, "balance_due": bill.balance_due, "status": bill.status,
    } for bill in rows])


@api_v1_bp.route("/payments", methods=["POST"])
def create_payment():
    """Records a payment against one existing invoice and posts it to the
    ledger — same effect as Sales -> Receive Payment, applied in full to the
    given invoice. For splitting one payment across several invoices, or a
    payment not yet applied to any invoice, use the staff UI for now — a v2
    concern for this endpoint, same as bills/items above."""
    company, key = _authenticate()
    if not company:
        return jsonify({"error": "Invalid or missing API key."}), 401
    _require_write(key)

    data = request.get_json(silent=True) or {}
    invoice_id = data.get("invoice_id")
    invoice = Invoice.query.filter_by(id=invoice_id, company_id=company.id).first() if invoice_id else None
    if not invoice:
        return jsonify({"error": "invoice_id is required and must belong to this company."}), 400
    if invoice.status in ("paid", "void"):
        return jsonify({"error": f"Invoice {invoice.invoice_no} is already {invoice.status}."}), 400

    try:
        amount = float(data.get("amount", invoice.balance_due))
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be a number."}), 400
    if amount <= 0 or amount > invoice.balance_due + 0.01:
        return jsonify({"error": f"amount must be > 0 and <= balance due ({invoice.balance_due})."}), 400

    from app.sales import post_payment, get_account_or_400, UNDEPOSITED_FUNDS_CODE

    with staff_session_scope(company.id):
        deposit_account = get_account_or_400(UNDEPOSITED_FUNDS_CODE, "Undeposited Funds")
        payment = Payment(
            company_id=company.id,
            customer_id=invoice.customer_id,
            payment_date=date.today(),
            amount=amount,
            currency=invoice.currency,
            exchange_rate=float(invoice.exchange_rate),
            method=data.get("method", "other"),
            deposit_account_id=deposit_account.id,
            reference_no=(data.get("reference_no") or "").strip() or None,
            memo=(data.get("memo") or "").strip() or None,
        )
        payment.applications.append(PaymentApplication(invoice=invoice, amount_applied=amount))
        post_payment(payment)
        db.session.add(payment)
        db.session.flush()
        invoice.status = "paid" if invoice.balance_due <= 0 else "partial"
        log_audit("create", "payment", payment.id, f"Recorded payment of {amount:.2f} via API for invoice {invoice.invoice_no}")
        db.session.commit()

    return jsonify({
        "id": payment.id, "amount": float(payment.amount), "invoice_status": invoice.status,
        "invoice_balance_due": invoice.balance_due,
    }), 201


# ── API key management (staff UI, Settings -> API & Webhooks -> Manage API Keys) ──

@api_v1_bp.route("/keys")
@login_required
@owner_required
def list_keys():
    # Full key value always shown (like mra_api_key/smtp_password elsewhere in
    # Settings) — this codebase's established threat model for these fields is
    # "whoever owns the DB already owns the company", so there's no case for
    # hashing it or hiding it after creation.
    keys = ApiKey.query.filter_by(company_id=current_company_id()).order_by(ApiKey.created_at.desc()).all()
    return render_template("settings/api_keys.html", keys=keys)


@api_v1_bp.route("/keys/new", methods=["POST"])
@login_required
@owner_required
def new_key():
    label = request.form.get("label", "").strip()
    scope = request.form.get("scope", "read")
    if not label:
        flash("Give the key a label (e.g. \"Reporting tool\", \"Zapier\").", "error")
        return redirect(url_for("api_v1.list_keys"))
    if scope not in ("read", "read_write"):
        scope = "read"

    key = ApiKey(company_id=current_company_id(), label=label, scope=scope, key=secrets.token_hex(32))
    db.session.add(key)
    db.session.commit()
    flash(f"API key \"{label}\" created.", "success")
    return redirect(url_for("api_v1.list_keys"))


@api_v1_bp.route("/keys/<int:key_id>/revoke", methods=["POST"])
@login_required
@owner_required
def revoke_key(key_id):
    key = ApiKey.query.filter_by(id=key_id, company_id=current_company_id()).first_or_404()
    key.is_active = False
    db.session.commit()
    flash(f"API key \"{key.label}\" revoked — it stops working immediately.", "success")
    return redirect(url_for("api_v1.list_keys"))
