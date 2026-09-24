"""Read-only public API — GET /api/v1/<resource>, authenticated by the company's own
api_key (Settings → API & Webhooks → Regenerate), presented as the X-Api-Key header.

Mirrors app/payroll_bridge.py's auth shape (no Flask-Login session — the caller is
another application, not a person with a browser) but the opposite direction: this
is LedgerBooks handing data OUT, not receiving it. Deliberately read-only and scoped
to a handful of core resources — a write API (creating invoices, etc.) is a much
bigger trust and validation surface and isn't something to add speculatively.
"""
from flask import Blueprint, jsonify, request

from app.models import Bill, CompanySettings, Customer, Invoice, Item, Vendor

api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")


def _authenticate():
    api_key = request.headers.get("X-Api-Key")
    return CompanySettings.get_by_api_key(api_key) if api_key else None


def _paginate(query):
    page = request.args.get("page", 1, type=int) or 1
    per_page = min(request.args.get("per_page", 50, type=int) or 50, 200)
    return query.limit(per_page).offset((page - 1) * per_page).all()


@api_v1_bp.route("/customers")
def list_customers():
    company = _authenticate()
    if not company:
        return jsonify({"error": "Invalid or missing API key."}), 401
    rows = _paginate(Customer.query.filter_by(company_id=company.id).order_by(Customer.id))
    return jsonify([{
        "id": c.id, "name": c.name, "customer_type": c.customer_type, "email": c.email,
        "phone": c.phone, "billing_currency": c.billing_currency, "balance_due": c.balance_due,
        "is_active": c.is_active,
    } for c in rows])


@api_v1_bp.route("/vendors")
def list_vendors():
    company = _authenticate()
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
    company = _authenticate()
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
    company = _authenticate()
    if not company:
        return jsonify({"error": "Invalid or missing API key."}), 401
    rows = _paginate(Invoice.query.filter_by(company_id=company.id).order_by(Invoice.id.desc()))
    return jsonify([{
        "id": inv.id, "invoice_no": inv.invoice_no, "customer": inv.customer.name,
        "invoice_date": inv.invoice_date.isoformat(), "due_date": inv.due_date.isoformat(),
        "currency": inv.currency, "total": inv.total, "balance_due": inv.balance_due, "status": inv.status,
    } for inv in rows])


@api_v1_bp.route("/bills")
def list_bills():
    company = _authenticate()
    if not company:
        return jsonify({"error": "Invalid or missing API key."}), 401
    rows = _paginate(Bill.query.filter_by(company_id=company.id).order_by(Bill.id.desc()))
    return jsonify([{
        "id": bill.id, "bill_no": bill.bill_no, "vendor": bill.vendor.name,
        "bill_date": bill.bill_date.isoformat(), "due_date": bill.due_date.isoformat(),
        "currency": bill.currency, "total": bill.total, "balance_due": bill.balance_due, "status": bill.status,
    } for bill in rows])
