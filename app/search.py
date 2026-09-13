"""Global search — a single JSON endpoint the topbar search bar hits with fetch()
to jump straight to any customer, vendor, invoice, bill or item across the
current company. Results are scoped to the active company via scoped_query.
"""
from flask import Blueprint, jsonify, request, url_for
from flask_login import login_required

from app.models import Bill, Customer, Invoice, Item, Vendor
from app.scoping import scoped_query

search_bp = Blueprint("search", __name__)

MAX_PER_GROUP = 5


@search_bp.route("/_search")
@login_required
def search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"query": q, "groups": []})

    like = f"%{q}%"
    groups = []

    customers = scoped_query(Customer).filter(Customer.name.ilike(like)).limit(MAX_PER_GROUP).all()
    if customers:
        groups.append({
            "label": "Customers",
            "icon": "users",
            "items": [
                {"title": c.name, "sub": c.email or c.phone or "",
                 "url": url_for("sales.customer_detail", customer_id=c.id)}
                for c in customers
            ],
        })

    vendors = scoped_query(Vendor).filter(Vendor.name.ilike(like)).limit(MAX_PER_GROUP).all()
    if vendors:
        groups.append({
            "label": "Vendors",
            "icon": "receipt",
            "items": [
                {"title": v.name, "sub": v.email or "",
                 "url": url_for("purchases.vendor_detail", vendor_id=v.id)}
                for v in vendors
            ],
        })

    invoices = scoped_query(Invoice).filter(Invoice.invoice_no.ilike(like)).limit(MAX_PER_GROUP).all()
    if invoices:
        groups.append({
            "label": "Invoices",
            "icon": "book-open",
            "items": [
                {"title": f"Invoice #{i.invoice_no}",
                 "sub": f"{i.customer.name if i.customer else ''} · {i.status}",
                 "url": url_for("sales.invoice_detail", invoice_id=i.id)}
                for i in invoices
            ],
        })

    bills = scoped_query(Bill).filter(Bill.bill_no.ilike(like)).limit(MAX_PER_GROUP).all() if hasattr(Bill, "bill_no") else []
    if bills:
        groups.append({
            "label": "Bills",
            "icon": "receipt",
            "items": [
                {"title": f"Bill #{b.bill_no}",
                 "sub": f"{b.vendor.name if b.vendor else ''} · {b.status}",
                 "url": url_for("purchases.bill_detail", bill_id=b.id)}
                for b in bills
            ],
        })

    items = scoped_query(Item).filter(
        (Item.name.ilike(like)) | (Item.sku.ilike(like))
    ).limit(MAX_PER_GROUP).all()
    if items:
        groups.append({
            "label": "Items",
            "icon": "package",
            "items": [
                {"title": f"{it.sku} — {it.name}",
                 "sub": f"{it.item_type} · {it.unit}",
                 "url": url_for("inventory.item_detail", item_id=it.id)}
                for it in items
            ],
        })

    return jsonify({"query": q, "groups": groups})
