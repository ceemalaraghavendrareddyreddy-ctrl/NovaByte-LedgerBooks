"""CSV bulk import — one page, three tabs (customers, vendors, items).
Upserts by unique key so re-importing the same file is idempotent.
"""
import csv
import io
from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from app import db
from app.auth import current_company_id
from app.models import Account, Customer, Item, Vendor
from app.scoping import scoped_query

imports_bp = Blueprint("imports", __name__, url_prefix="/imports")

SAMPLE_HEADERS = {
    "customers": ["name", "email", "phone", "address", "vat_number", "opening_balance"],
    "vendors":   ["name", "email", "phone", "address", "vat_number", "opening_balance"],
    "items":     ["sku", "name", "item_type", "unit", "sales_price"],
}


def _num(v, default=0):
    if v is None or v == "":
        return default
    try:
        return Decimal(str(v).replace(",", ""))
    except InvalidOperation:
        return default


def _import_customers(rows, company_id):
    created, updated, errors = 0, 0, []
    for i, row in enumerate(rows, start=2):
        name = (row.get("name") or "").strip()
        if not name:
            errors.append(f"Row {i}: missing 'name' — skipped.")
            continue
        existing = scoped_query(Customer).filter_by(name=name).first()
        target = existing or Customer(company_id=company_id, name=name)
        target.email = (row.get("email") or "").strip() or target.email
        target.phone = (row.get("phone") or "").strip() or target.phone
        target.address = (row.get("address") or "").strip() or target.address
        target.vat_number = (row.get("vat_number") or "").strip() or target.vat_number
        if row.get("opening_balance"):
            target.opening_balance = _num(row.get("opening_balance"))
        if existing:
            updated += 1
        else:
            db.session.add(target)
            created += 1
    db.session.commit()
    return created, updated, errors


def _import_vendors(rows, company_id):
    created, updated, errors = 0, 0, []
    for i, row in enumerate(rows, start=2):
        name = (row.get("name") or "").strip()
        if not name:
            errors.append(f"Row {i}: missing 'name' — skipped.")
            continue
        existing = scoped_query(Vendor).filter_by(name=name).first()
        target = existing or Vendor(company_id=company_id, name=name)
        target.email = (row.get("email") or "").strip() or target.email
        target.phone = (row.get("phone") or "").strip() or target.phone
        target.address = (row.get("address") or "").strip() or target.address
        if hasattr(target, "vat_number"):
            target.vat_number = (row.get("vat_number") or "").strip() or target.vat_number
        if row.get("opening_balance"):
            target.opening_balance = _num(row.get("opening_balance"))
        if existing:
            updated += 1
        else:
            db.session.add(target)
            created += 1
    db.session.commit()
    return created, updated, errors


def _import_items(rows, company_id):
    created, updated, errors = 0, 0, []
    default_income = Account.query.filter_by(
        company_id=company_id, code="4000"
    ).first()
    if not default_income:
        return 0, 0, ["Cannot import items: '4000 - Sales Revenue' account is missing."]
    for i, row in enumerate(rows, start=2):
        sku = (row.get("sku") or "").strip()
        name = (row.get("name") or "").strip()
        if not sku or not name:
            errors.append(f"Row {i}: 'sku' and 'name' are both required — skipped.")
            continue
        existing = scoped_query(Item).filter_by(sku=sku).first()
        target = existing or Item(
            company_id=company_id, sku=sku, name=name,
            income_account_id=default_income.id,
        )
        target.name = name
        target.item_type = (row.get("item_type") or "service").strip().lower()
        target.unit = (row.get("unit") or "pcs").strip() or "pcs"
        if row.get("sales_price"):
            target.sales_price = _num(row.get("sales_price"))
        if existing:
            updated += 1
        else:
            db.session.add(target)
            created += 1
    db.session.commit()
    return created, updated, errors


IMPORTERS = {
    "customers": _import_customers,
    "vendors":   _import_vendors,
    "items":     _import_items,
}


@imports_bp.route("/", methods=["GET", "POST"])
@login_required
def csv_import():
    result = None
    active_kind = request.values.get("kind", "customers")
    if request.method == "POST":
        kind = request.form.get("kind", "customers")
        active_kind = kind
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Please choose a CSV file.", "error")
        elif kind not in IMPORTERS:
            flash(f"Unknown import kind: {kind}", "error")
        else:
            try:
                text_stream = io.StringIO(file.stream.read().decode("utf-8-sig"))
                reader = csv.DictReader(text_stream)
                rows = [r for r in reader]
                if not rows:
                    flash("CSV had no data rows.", "error")
                else:
                    created, updated, errors = IMPORTERS[kind](rows, current_company_id())
                    result = {"kind": kind, "created": created, "updated": updated, "errors": errors}
                    msg_kind = kind.rstrip('s').capitalize()
                    if errors:
                        flash(f"{msg_kind}: created {created}, updated {updated}, {len(errors)} row(s) skipped.", "warning")
                    else:
                        flash(f"{msg_kind}: created {created}, updated {updated}.", "success")
            except UnicodeDecodeError:
                flash("CSV must be UTF-8 encoded.", "error")
            except Exception as e:
                flash(f"Could not parse CSV: {e}", "error")
    return render_template(
        "imports/csv_import.html",
        headers=SAMPLE_HEADERS, kind=active_kind, result=result,
    )
