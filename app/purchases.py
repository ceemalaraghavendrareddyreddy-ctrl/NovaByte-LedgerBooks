from datetime import date, datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from werkzeug.security import generate_password_hash

from app import db
from app.audit import log_audit
from app.auth import current_company, current_company_id, owner_required
from app.models import (
    Account, Bill, BillLine, CURRENCIES, Item, JournalEntry, JournalLine, Project, PurchaseOrder,
    PurchaseOrderLine, RECURRING_FREQUENCIES, RecurringBill, RecurringBillLine, StockMovement, Vendor,
    VendorCredit, VendorCreditApplication, VendorCreditLine, VendorPayment, VendorPaymentApplication, Warehouse,
)
from app.webhooks import fire_webhook
from app.pdf import generate_bill_pdf
from app.report_export import rows_to_pdf, rows_to_xlsx
from app.scoping import scoped_get, scoped_or_404, scoped_query
from app.share_links import share_url, whatsapp_link
from app.custom_fields import (
    get_field_definitions as get_custom_field_definitions,
    get_field_values as get_custom_field_values,
    save_field_values as save_custom_field_values,
    missing_required_fields as missing_required_custom_fields,
)

purchases_bp = Blueprint("purchases", __name__, url_prefix="/purchases")

AP_ACCOUNT_CODE = "2000"
VAT_RECEIVABLE_CODE = "2110"
DEFAULT_EXPENSE_CODE = "5000"  # Cost of Goods Sold
FX_GAIN_LOSS_CODE = "4920"


def get_account_or_400(code, label):
    account = Account.query.filter_by(company_id=current_company_id(), code=code).first()
    if not account:
        abort(400, f"Required account {code} ({label}) is missing from the Chart of Accounts.")
    return account


def next_bill_no():
    last = scoped_query(Bill).order_by(Bill.id.desc()).first()
    next_num = (last.id + 1) if last else 1
    return f"BILL-{next_num:04d}"


def next_vendor_credit_no():
    last = scoped_query(VendorCredit).order_by(VendorCredit.id.desc()).first()
    next_num = (last.id + 1) if last else 1
    return f"VC-{next_num:04d}"


def next_po_no():
    last = scoped_query(PurchaseOrder).order_by(PurchaseOrder.id.desc()).first()
    next_num = (last.id + 1) if last else 1
    return f"PO-{next_num:04d}"


# ── Vendors ──────────────────────────────────────────────────────────

@purchases_bp.route("/vendors")
@login_required
def vendor_list():
    vendors = scoped_query(Vendor).order_by(Vendor.name).all()
    return render_template("purchases/vendors.html", vendors=vendors)


@purchases_bp.route("/vendors/new", methods=["GET", "POST"])
@login_required
def vendor_new():
    custom_defs = get_custom_field_definitions("vendor", current_company_id())
    if request.method == "POST":
        missing = missing_required_custom_fields(custom_defs, request.form)
        if missing:
            flash(f"Required field(s) missing: {', '.join(missing)}.", "error")
            return render_template(
                "purchases/vendor_form.html", form=request.form, action_url=url_for("purchases.vendor_new"),
                currencies=CURRENCIES, custom_fields=custom_defs, custom_values=request.form,
            )
        vendor_type = request.form.get("vendor_type") if request.form.get("vendor_type") in ("individual", "company") else "company"
        vendor = Vendor(
            company_id=current_company_id(),
            name=request.form["name"].strip(),
            vendor_type=vendor_type,
            email=request.form.get("email", "").strip() or None,
            phone=request.form.get("phone", "").strip() or None,
            phone2=request.form.get("phone2", "").strip() or None,
            address=request.form.get("address", "").strip() or None,
            vat_number=request.form.get("vat_number", "").strip() or None if vendor_type == "company" else None,
            brn=request.form.get("brn", "").strip() or None if vendor_type == "company" else None,
            mra_supplier_id=request.form.get("mra_supplier_id", "").strip() or None,
            billing_currency=request.form.get("billing_currency") or "MUR",
            opening_balance=request.form.get("opening_balance") or 0,
        )
        db.session.add(vendor)
        db.session.flush()
        save_custom_field_values(vendor.id, custom_defs, request.form)
        db.session.commit()
        flash(f"Vendor '{vendor.name}' created.", "success")
        return redirect(url_for("purchases.vendor_detail", vendor_id=vendor.id))
    return render_template(
        "purchases/vendor_form.html", form={}, action_url=url_for("purchases.vendor_new"), currencies=CURRENCIES,
        custom_fields=custom_defs, custom_values={},
    )


@purchases_bp.route("/vendors/<int:vendor_id>/edit", methods=["GET", "POST"])
@login_required
def vendor_edit(vendor_id):
    vendor = scoped_or_404(Vendor, vendor_id)
    custom_defs = get_custom_field_definitions("vendor", current_company_id())
    if request.method == "POST":
        missing = missing_required_custom_fields(custom_defs, request.form)
        if missing:
            flash(f"Required field(s) missing: {', '.join(missing)}.", "error")
            return render_template(
                "purchases/vendor_form.html", form=request.form, action_url=url_for("purchases.vendor_edit", vendor_id=vendor.id),
                editing=True, currencies=CURRENCIES, custom_fields=custom_defs, custom_values=request.form,
            )
        vendor_type = request.form.get("vendor_type") if request.form.get("vendor_type") in ("individual", "company") else "company"
        vendor.name = request.form["name"].strip()
        vendor.vendor_type = vendor_type
        vendor.email = request.form.get("email", "").strip() or None
        vendor.phone = request.form.get("phone", "").strip() or None
        vendor.phone2 = request.form.get("phone2", "").strip() or None
        vendor.address = request.form.get("address", "").strip() or None
        vendor.vat_number = request.form.get("vat_number", "").strip() or None if vendor_type == "company" else None
        vendor.brn = request.form.get("brn", "").strip() or None if vendor_type == "company" else None
        vendor.mra_supplier_id = request.form.get("mra_supplier_id", "").strip() or None
        vendor.billing_currency = request.form.get("billing_currency") or "MUR"
        vendor.opening_balance = request.form.get("opening_balance") or 0
        save_custom_field_values(vendor.id, custom_defs, request.form)
        db.session.commit()
        flash(f"Vendor '{vendor.name}' updated.", "success")
        return redirect(url_for("purchases.vendor_detail", vendor_id=vendor.id))
    custom_values = get_custom_field_values(vendor.id, custom_defs)
    return render_template(
        "purchases/vendor_form.html", form=vendor, action_url=url_for("purchases.vendor_edit", vendor_id=vendor.id),
        editing=True, currencies=CURRENCIES, custom_fields=custom_defs, custom_values=custom_values,
    )


@purchases_bp.route("/vendors/<int:vendor_id>/toggle", methods=["POST"])
@login_required
def vendor_toggle(vendor_id):
    vendor = scoped_or_404(Vendor, vendor_id)
    vendor.is_active = not vendor.is_active
    db.session.commit()
    flash(f"Vendor {vendor.name} {'activated' if vendor.is_active else 'deactivated'}.", "success")
    return redirect(url_for("purchases.vendor_list"))


@purchases_bp.route("/vendors/<int:vendor_id>/portal-access", methods=["POST"])
@login_required
def vendor_portal_access(vendor_id):
    vendor = scoped_or_404(Vendor, vendor_id)
    portal_enabled = request.form.get("portal_enabled") == "on"
    if portal_enabled and not vendor.email:
        flash("Add an email address for this vendor before enabling portal access.", "error")
        return redirect(url_for("purchases.vendor_detail", vendor_id=vendor.id))

    new_password = request.form.get("portal_password", "")
    if new_password.strip():
        if len(new_password) < 6:
            flash("Portal password must be at least 6 characters.", "error")
            return redirect(url_for("purchases.vendor_detail", vendor_id=vendor.id))
        vendor.portal_password_hash = generate_password_hash(new_password)
    elif portal_enabled and not vendor.portal_password_hash:
        flash("Set a password before enabling portal access.", "error")
        return redirect(url_for("purchases.vendor_detail", vendor_id=vendor.id))

    vendor.portal_enabled = portal_enabled
    db.session.commit()
    log_audit("edit", "vendor", vendor.id, f"{'Enabled' if portal_enabled else 'Disabled'} portal access for {vendor.name}")
    flash(f"Portal access {'enabled' if portal_enabled else 'updated'} for {vendor.name}.", "success")
    return redirect(url_for("purchases.vendor_detail", vendor_id=vendor.id))


@purchases_bp.route("/vendors/<int:vendor_id>")
@login_required
def vendor_detail(vendor_id):
    vendor = scoped_or_404(Vendor, vendor_id)
    bills = sorted([b for b in vendor.bills if b.status != "void"], key=lambda b: b.bill_date, reverse=True)
    payments = sorted(vendor.payments, key=lambda p: p.payment_date, reverse=True)
    custom_defs = get_custom_field_definitions("vendor", current_company_id())
    custom_values = get_custom_field_values(vendor.id, custom_defs)
    return render_template(
        "purchases/vendor_detail.html", vendor=vendor, bills=bills, payments=payments,
        custom_fields=custom_defs, custom_values=custom_values,
    )


def _vendor_statement_data(vendor, start_date, end_date):
    """AP mirror of sales._customer_statement_data — see that docstring for the
    windowing logic (opening/closing balances computed correctly even when the
    window doesn't start at the vendor's very first transaction)."""
    transactions = []
    for bill in vendor.bills:
        if bill.status == "void":
            continue
        transactions.append({
            "date": bill.bill_date, "type": "Bill", "doc_no": bill.bill_no, "id": bill.id,
            "debit": float(bill.total), "credit": 0.0,
            "url": url_for("purchases.bill_detail", bill_id=bill.id),
        })
    for p in vendor.payments:
        transactions.append({
            "date": p.payment_date, "type": "Payment", "doc_no": p.reference_no or f"Payment #{p.id}", "id": p.id,
            "debit": 0.0, "credit": float(p.amount),
            "url": url_for("purchases.payment_detail", payment_id=p.id),
        })
    for vc in scoped_query(VendorCredit).filter_by(vendor_id=vendor.id).all():
        if vc.status == "void":
            continue
        transactions.append({
            "date": vc.credit_date, "type": "Vendor Credit", "doc_no": vc.credit_no, "id": vc.id,
            "debit": 0.0, "credit": float(vc.total),
            "url": url_for("purchases.vendor_credit_detail", vendor_credit_id=vc.id),
        })
    transactions.sort(key=lambda t: (t["date"], t["type"]))

    running = float(vendor.opening_balance)
    opening_balance = running
    opening_captured = False
    rows = []
    for t in transactions:
        if t["date"] > end_date:
            break
        if not opening_captured and t["date"] >= start_date:
            opening_balance = running
            opening_captured = True
        running += t["debit"] - t["credit"]
        if t["date"] >= start_date:
            rows.append({**t, "balance": running})
    if not opening_captured:
        opening_balance = running
    return opening_balance, rows, running


@purchases_bp.route("/vendors/<int:vendor_id>/statement")
@login_required
def vendor_statement(vendor_id):
    vendor = scoped_or_404(Vendor, vendor_id)
    start_raw = request.args.get("start_date")
    end_raw = request.args.get("end_date")
    start_date = datetime.strptime(start_raw, "%Y-%m-%d").date() if start_raw else date(date.today().year, 1, 1)
    end_date = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else date.today()

    opening_balance, rows, closing_balance = _vendor_statement_data(vendor, start_date, end_date)
    return render_template(
        "purchases/vendor_statement.html", vendor=vendor, rows=rows,
        opening_balance=opening_balance, closing_balance=closing_balance,
        start_date=start_date.isoformat(), end_date=end_date.isoformat(),
    )


@purchases_bp.route("/vendors/<int:vendor_id>/statement.pdf")
@login_required
def vendor_statement_pdf(vendor_id):
    vendor = scoped_or_404(Vendor, vendor_id)
    start_raw = request.args.get("start_date")
    end_raw = request.args.get("end_date")
    start_date = datetime.strptime(start_raw, "%Y-%m-%d").date() if start_raw else date(date.today().year, 1, 1)
    end_date = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else date.today()

    opening_balance, rows, closing_balance = _vendor_statement_data(vendor, start_date, end_date)
    table_rows = [(start_date.isoformat(), "Opening Balance", "-", "", "", f"{opening_balance:,.2f}")]
    table_rows += [
        (r["date"].isoformat(), r["type"], r["doc_no"], f"{r['debit']:,.2f}" if r["debit"] else "",
         f"{r['credit']:,.2f}" if r["credit"] else "", f"{r['balance']:,.2f}")
        for r in rows
    ]
    buffer = rows_to_pdf(
        f"Statement — {vendor.name}", f"{start_date.isoformat()} to {end_date.isoformat()}",
        ["Date", "Type", "Document #", "Debit", "Credit", "Balance"], table_rows,
        company=current_company(), numeric_cols={3, 4, 5},
    )
    return send_file(
        buffer, as_attachment=True, download_name=f"Statement-{vendor.name}-{end_date.isoformat()}.pdf",
        mimetype="application/pdf",
    )


# ── Purchase Orders ───────────────────────────────────────────────────

@purchases_bp.route("/purchase-orders")
@login_required
def po_list():
    purchase_orders = scoped_query(PurchaseOrder).order_by(PurchaseOrder.po_date.desc(), PurchaseOrder.id.desc()).all()
    return render_template("purchases/pos.html", purchase_orders=purchase_orders)


@purchases_bp.route("/purchase-orders/new", methods=["GET", "POST"])
@login_required
def po_new():
    vendors = scoped_query(Vendor).filter_by(is_active=True).order_by(Vendor.name).all()
    expense_accounts = scoped_query(Account).filter(
        Account.is_active == True,  # noqa: E712
        (Account.account_type == "Expense") | (Account.code.in_(["1300", "1500"])),
    ).order_by(Account.code).all()
    default_expense = scoped_query(Account).filter_by(code=DEFAULT_EXPENSE_CODE).first()
    items = scoped_query(Item).filter_by(is_active=True).order_by(Item.sku).all()
    preselected_vendor_id = request.args.get("vendor_id", type=int)

    def render_form(form):
        return render_template(
            "purchases/po_form.html", vendors=vendors, expense_accounts=expense_accounts,
            default_expense=default_expense, items=items, form=form, today=date.today().isoformat(),
        )

    if request.method == "POST":
        vendor_id = request.form.get("vendor_id")
        if not vendor_id:
            flash("Select a vendor.", "error")
            return render_form(request.form)

        po_date = datetime.strptime(request.form["po_date"], "%Y-%m-%d").date()
        expected_raw = request.form.get("expected_date", "").strip()
        expected_date = datetime.strptime(expected_raw, "%Y-%m-%d").date() if expected_raw else None

        descriptions = request.form.getlist("description")
        quantities = request.form.getlist("quantity")
        unit_prices = request.form.getlist("unit_price")
        taxables = request.form.getlist("taxable")
        expense_account_ids = request.form.getlist("expense_account_id")
        item_ids = request.form.getlist("item_id")
        item_ids += [""] * (len(descriptions) - len(item_ids))

        po = PurchaseOrder(
            company_id=current_company_id(),
            po_no=next_po_no(),
            vendor_id=int(vendor_id),
            po_date=po_date,
            expected_date=expected_date,
            memo=request.form.get("memo", "").strip() or None,
            vat_rate=float(request.form.get("vat_rate") or 15.00),
        )

        for i, (desc, qty, price, acc_id, item_id_raw) in enumerate(
            zip(descriptions, quantities, unit_prices, expense_account_ids, item_ids)
        ):
            if not desc.strip() or not qty or not price:
                continue
            item = scoped_get(Item, int(item_id_raw)) if item_id_raw else None
            tracked_item = item if (item and item.is_tracked) else None
            po.lines.append(
                PurchaseOrderLine(
                    item=tracked_item,
                    description=desc.strip(),
                    quantity=float(qty),
                    unit_price=float(price),
                    taxable=str(i) in taxables,
                    expense_account_id=tracked_item.inventory_account_id if tracked_item else int(acc_id),
                )
            )

        if not po.lines:
            flash("Add at least one line.", "error")
            return render_form(request.form)

        db.session.add(po)
        db.session.flush()
        log_audit("create", "purchase_order", po.id, f"Created purchase order {po.po_no}", po.po_no)
        db.session.commit()
        flash(f"Purchase order {po.po_no} created.", "success")
        return redirect(url_for("purchases.po_detail", po_id=po.id))

    return render_form({"vendor_id": preselected_vendor_id} if preselected_vendor_id else {})


@purchases_bp.route("/purchase-orders/<int:po_id>")
@login_required
def po_detail(po_id):
    po = scoped_or_404(PurchaseOrder, po_id)
    return render_template("purchases/po_detail.html", po=po)


@purchases_bp.route("/purchase-orders/<int:po_id>/status", methods=["POST"])
@login_required
def po_status(po_id):
    po = scoped_or_404(PurchaseOrder, po_id)
    new_status = request.form["status"]
    if po.status in ("closed", "cancelled"):
        flash(f"This PO is already {po.status} and can't change status.", "error")
    elif new_status not in ("draft", "sent", "cancelled"):
        flash("Invalid status.", "error")
    else:
        po.status = new_status
        db.session.commit()
        flash(f"Purchase order marked as {new_status}.", "success")
    return redirect(url_for("purchases.po_detail", po_id=po.id))


@purchases_bp.route("/purchase-orders/<int:po_id>/receive", methods=["GET", "POST"])
@login_required
def po_receive(po_id):
    po = scoped_or_404(PurchaseOrder, po_id)
    if po.status in ("closed", "cancelled"):
        flash(f"This PO is already {po.status}.", "error")
        return redirect(url_for("purchases.po_detail", po_id=po.id))

    if request.method == "POST":
        bill_date = datetime.strptime(request.form["bill_date"], "%Y-%m-%d").date()
        due_date_raw = request.form.get("due_date", "").strip()
        due_date = datetime.strptime(due_date_raw, "%Y-%m-%d").date() if due_date_raw else bill_date
        vendor_ref = request.form.get("vendor_ref", "").strip() or None

        bill = Bill(
            company_id=current_company_id(),
            bill_no=next_bill_no(),
            vendor_ref=vendor_ref,
            vendor_id=po.vendor_id,
            purchase_order_id=po.id,
            bill_date=bill_date,
            due_date=due_date,
            memo=f"Received from {po.po_no}" + (f" - {po.memo}" if po.memo else ""),
            vat_rate=po.vat_rate,
        )

        any_billed = False
        variances = []
        for line in po.lines:
            qty_raw = request.form.get(f"qty_{line.id}", "0")
            qty = float(qty_raw or 0)
            if qty <= 0:
                continue
            price_raw = request.form.get(f"price_{line.id}", "").strip()
            unit_price = float(price_raw) if price_raw else float(line.unit_price)

            # Over-billing beyond what's left on the PO is flagged, not blocked — a
            # vendor sometimes ships/bills more than ordered, and 3-way matching's job
            # is to surface that for review, not make it impossible to record at all.
            if qty > float(line.quantity_remaining) + 0.0001:
                variances.append(f"'{line.description}': billed {qty}, only {line.quantity_remaining} was remaining on the PO")
            if round(unit_price, 2) != round(float(line.unit_price), 2):
                variances.append(f"'{line.description}': billed at {unit_price:.2f}, PO quoted {float(line.unit_price):.2f}")

            any_billed = True
            bill.lines.append(
                BillLine(
                    item_id=line.item_id, description=line.description, quantity=qty,
                    unit_price=unit_price, taxable=line.taxable, expense_account_id=line.expense_account_id,
                    purchase_order_line_id=line.id,
                )
            )
            line.quantity_billed = float(line.quantity_billed) + qty

        if not any_billed:
            flash("Enter a quantity to bill for at least one line.", "error")
            return redirect(url_for("purchases.po_receive", po_id=po.id))

        db.session.add(bill)
        db.session.flush()
        post_bill(bill)
        po.status = "closed" if po.is_fully_billed else "partial"
        log_audit("create", "bill", bill.id, f"Created bill {bill.bill_no} from {po.po_no}", bill.bill_no)
        db.session.commit()
        if variances:
            flash(
                f"Bill {bill.bill_no} created from {po.po_no}, but doesn't fully match it: " + "; ".join(variances),
                "warning",
            )
        else:
            flash(f"Bill {bill.bill_no} created from {po.po_no} — matches the PO exactly.", "success")
        return redirect(url_for("purchases.bill_detail", bill_id=bill.id))

    return render_template("purchases/po_receive.html", po=po, today=date.today().isoformat())


# ── Bills ────────────────────────────────────────────────────────────

@purchases_bp.route("/bills")
@login_required
def bill_list():
    bills = scoped_query(Bill).order_by(Bill.bill_date.desc(), Bill.id.desc()).all()
    return render_template("purchases/bills.html", bills=bills)


@purchases_bp.route("/bills/new", methods=["GET", "POST"])
@login_required
def bill_new():
    vendors = scoped_query(Vendor).filter_by(is_active=True).order_by(Vendor.name).all()
    expense_accounts = scoped_query(Account).filter(
        Account.is_active == True,  # noqa: E712
        (Account.account_type == "Expense") | (Account.code.in_(["1300", "1500"])),
    ).order_by(Account.code).all()
    default_expense = scoped_query(Account).filter_by(code=DEFAULT_EXPENSE_CODE).first()
    items = scoped_query(Item).filter_by(is_active=True).order_by(Item.sku).all()
    projects = scoped_query(Project).filter_by(is_active=True).order_by(Project.name).all()
    warehouses = scoped_query(Warehouse).filter_by(is_active=True).order_by(Warehouse.code).all()
    preselected_vendor_id = request.args.get("vendor_id", type=int)
    base_currency = current_company().base_currency

    def render_form(form):
        return render_template(
            "purchases/bill_form.html", vendors=vendors, expense_accounts=expense_accounts,
            default_expense=default_expense, items=items, projects=projects, warehouses=warehouses,
            form=form, today=date.today().isoformat(),
            currencies=CURRENCIES, base_currency=base_currency,
        )

    if request.method == "POST":
        vendor_id = request.form.get("vendor_id")
        if not vendor_id:
            flash("Select a vendor.", "error")
            return render_form(request.form)

        bill_date = datetime.strptime(request.form["bill_date"], "%Y-%m-%d").date()
        due_date = datetime.strptime(request.form["due_date"], "%Y-%m-%d").date()

        currency = request.form.get("currency", base_currency) or base_currency
        exchange_rate = float(request.form.get("exchange_rate") or 1.0) if currency != base_currency else 1.0
        if exchange_rate <= 0:
            flash("Exchange rate must be greater than zero.", "error")
            return render_form(request.form)

        descriptions = request.form.getlist("description")
        quantities = request.form.getlist("quantity")
        unit_prices = request.form.getlist("unit_price")
        taxables = request.form.getlist("taxable")
        expense_account_ids = request.form.getlist("expense_account_id")
        item_ids = request.form.getlist("item_id")
        # Per-line VAT % override — blank means "use this bill's own vat_rate above",
        # exactly like every line did before this field existed.
        line_vat_rates = request.form.getlist("line_vat_rate")
        line_warehouse_ids = request.form.getlist("line_warehouse_id")
        line_lot_numbers = request.form.getlist("line_lot_number")
        # zip() truncates to the shortest list — pad item_ids so a row missing that field
        # doesn't silently drop every line below it (the real form always sends it, but be defensive).
        item_ids += [""] * (len(descriptions) - len(item_ids))
        line_vat_rates += [""] * (len(descriptions) - len(line_vat_rates))
        line_warehouse_ids += [""] * (len(descriptions) - len(line_warehouse_ids))
        line_lot_numbers += [""] * (len(descriptions) - len(line_lot_numbers))

        bill = Bill(
            company_id=current_company_id(),
            bill_no=next_bill_no(),
            vendor_ref=request.form.get("vendor_ref", "").strip() or None,
            vendor_id=int(vendor_id),
            bill_date=bill_date,
            due_date=due_date,
            memo=request.form.get("memo", "").strip() or None,
            project_id=int(request.form["project_id"]) if request.form.get("project_id") else None,
            vat_rate=float(request.form.get("vat_rate") or 15.00),
            currency=currency,
            exchange_rate=exchange_rate,
        )

        for i, (desc, qty, price, acc_id, item_id_raw, vat_rate_raw, warehouse_id_raw, lot_number_raw) in enumerate(
            zip(descriptions, quantities, unit_prices, expense_account_ids, item_ids, line_vat_rates, line_warehouse_ids, line_lot_numbers)
        ):
            if not desc.strip() or not qty or not price:
                continue
            item = scoped_get(Item, int(item_id_raw)) if item_id_raw else None
            tracked_item = item if (item and item.is_tracked) else None
            bill.lines.append(
                BillLine(
                    item=tracked_item,  # assigning the relationship (not just item_id) keeps .item populated pre-flush
                    description=desc.strip(),
                    quantity=float(qty),
                    unit_price=float(price),
                    taxable=str(i) in taxables,
                    vat_rate=float(vat_rate_raw) if vat_rate_raw.strip() != "" else None,
                    expense_account_id=tracked_item.inventory_account_id if tracked_item else int(acc_id),
                    warehouse_id=int(warehouse_id_raw) if warehouse_id_raw else None,
                    lot_number=lot_number_raw.strip() or None,
                )
            )

        if not bill.lines:
            flash("Add at least one bill line.", "error")
            return render_form(request.form)

        db.session.add(bill)
        db.session.flush()  # assign bill.id before post_bill needs it for StockMovement.reference_id

        settings = current_company()
        threshold = float(settings.bill_approval_threshold) if settings.bill_approval_threshold is not None else 0.0
        needs_approval = settings.bill_approval_enabled and bill.total_base >= threshold
        if needs_approval:
            bill.status = "pending_approval"
            bill.submitted_by = current_user.id
            log_audit("submit", "bill", bill.id, f"Submitted bill {bill.bill_no} for approval (total {bill.total:.2f})", bill.bill_no)
            db.session.commit()
            flash(f"Bill {bill.bill_no} submitted for approval — it won't post to the ledger until an owner approves it.", "success")
            return redirect(url_for("purchases.bill_detail", bill_id=bill.id))

        post_bill(bill)
        log_audit("create", "bill", bill.id, f"Created bill {bill.bill_no} (total {bill.total:.2f})", bill.bill_no)
        db.session.commit()
        fire_webhook(current_company_id(), "bill.created", {
            "id": bill.id, "bill_no": bill.bill_no, "vendor": bill.vendor.name,
            "total": bill.total, "currency": bill.currency, "due_date": bill.due_date.isoformat(),
        })
        flash(f"Bill {bill.bill_no} created and posted to the ledger.", "success")
        return redirect(url_for("purchases.bill_detail", bill_id=bill.id))

    return render_form({"vendor_id": preselected_vendor_id} if preselected_vendor_id else {})


def post_bill(bill):
    """Builds the double-entry journal entry for a bill: Dr Expense/Asset (per line), Dr VAT Receivable, Cr AP.

    Posts in the company's base currency — see the equivalent comment on sales.post_invoice
    for the conversion and rounding-remainder approach, mirrored here for AP. Tracked-item
    lines receive stock at this bill's unit price *converted to base currency*, since the
    item's moving-average cost (and everything COGS posts against it) is always base-currency —
    a foreign-currency bill must never let a foreign unit price leak into inventory valuation.
    """
    ap_account = get_account_or_400(AP_ACCOUNT_CODE, "Accounts Payable")
    rate = float(bill.exchange_rate)

    entry = JournalEntry(
        company_id=current_company_id(),
        entry_date=bill.bill_date,
        reference_no=bill.bill_no,
        memo=f"Bill {bill.bill_no} - {bill.memo or ''}".strip(" -")
             + (f" ({bill.currency} {bill.total:.2f} @ {rate})" if bill.is_foreign else ""),
        source_type="bill",
        project_id=bill.project_id,
        created_by=current_user.id,
    )

    expense_totals = {}
    for line in bill.lines:
        expense_totals[line.expense_account_id] = expense_totals.get(line.expense_account_id, 0) + line.amount
    expense_totals_base = {account_id: round(amount * rate, 2) for account_id, amount in expense_totals.items()}
    vat_base = round(bill.vat_amount * rate, 2) if bill.vat_amount else 0

    remainder = round(bill.total_base - sum(expense_totals_base.values()) - vat_base, 2)
    if remainder and vat_base:
        vat_base = round(vat_base + remainder, 2)
    elif remainder and expense_totals_base:
        last_account_id = list(expense_totals_base)[-1]
        expense_totals_base[last_account_id] = round(expense_totals_base[last_account_id] + remainder, 2)

    for account_id, amount in expense_totals_base.items():
        entry.lines.append(JournalLine(account_id=account_id, debit=amount, credit=0, memo=bill.bill_no))

    if vat_base:
        vat_account = get_account_or_400(VAT_RECEIVABLE_CODE, "VAT Receivable")
        entry.lines.append(JournalLine(account=vat_account, debit=vat_base, credit=0, memo=bill.bill_no))

    entry.lines.append(JournalLine(account=ap_account, debit=0, credit=bill.total_base, memo=bill.bill_no))

    for line in bill.lines:
        if not line.item_id:
            continue
        item = line.item
        base_unit_price = round(float(line.unit_price) * rate, 6)
        item.receive_stock(line.quantity, base_unit_price)
        db.session.add(StockMovement(
            item_id=item.id, movement_date=bill.bill_date, movement_type="purchase",
            quantity=float(line.quantity), unit_cost=base_unit_price,
            reference_type="bill", reference_id=bill.id,
            running_quantity=item.quantity_on_hand, running_avg_cost=item.cost_price,
            memo=f"Purchased via {bill.bill_no}",
            warehouse_id=line.warehouse_id, lot_number=line.lot_number,
        ))

    db.session.add(entry)
    db.session.flush()
    bill.journal_entry_id = entry.id


@purchases_bp.route("/approvals")
@login_required
@owner_required
def approval_list():
    pending = (
        scoped_query(Bill).filter_by(status="pending_approval")
        .order_by(Bill.bill_date.desc()).all()
    )
    return render_template("purchases/approval_list.html", bills=pending)


@purchases_bp.route("/bills/<int:bill_id>/approve", methods=["POST"])
@login_required
@owner_required
def bill_approve(bill_id):
    bill = scoped_or_404(Bill, bill_id)
    if bill.status != "pending_approval":
        flash("This bill isn't awaiting approval.", "error")
        return redirect(url_for("purchases.bill_detail", bill_id=bill.id))

    post_bill(bill)
    bill.status = "open"
    bill.approved_by = current_user.id
    bill.approved_at = datetime.utcnow()
    log_audit("approve", "bill", bill.id, f"Approved and posted bill {bill.bill_no} (total {bill.total:.2f})", bill.bill_no)
    db.session.commit()
    fire_webhook(current_company_id(), "bill.created", {
        "id": bill.id, "bill_no": bill.bill_no, "vendor": bill.vendor.name,
        "total": bill.total, "currency": bill.currency, "due_date": bill.due_date.isoformat(),
    })
    flash(f"Bill {bill.bill_no} approved and posted to the ledger.", "success")
    return redirect(url_for("purchases.bill_detail", bill_id=bill.id))


@purchases_bp.route("/bills/<int:bill_id>/reject", methods=["POST"])
@login_required
@owner_required
def bill_reject(bill_id):
    bill = scoped_or_404(Bill, bill_id)
    if bill.status != "pending_approval":
        flash("This bill isn't awaiting approval.", "error")
        return redirect(url_for("purchases.bill_detail", bill_id=bill.id))

    # Never posted anything to the ledger while pending, so rejecting is just marking
    # it void — there's no journal entry to reverse, unlike bill_void below.
    bill.status = "void"
    bill.approved_by = current_user.id
    bill.approved_at = datetime.utcnow()
    bill.approval_note = request.form.get("note", "").strip() or None
    log_audit("reject", "bill", bill.id, f"Rejected bill {bill.bill_no}" + (f": {bill.approval_note}" if bill.approval_note else ""), bill.bill_no)
    db.session.commit()
    flash(f"Bill {bill.bill_no} rejected.", "success")
    return redirect(url_for("purchases.approval_list"))


@purchases_bp.route("/bills/<int:bill_id>")
@login_required
def bill_detail(bill_id):
    bill = scoped_or_404(Bill, bill_id)
    share_link = share_url("bill", bill.id, bill.company_id, "share.bill_pdf")
    whatsapp_message = (
        f"Bill {bill.bill_no} from {current_company().business_name} — "
        f"{bill.currency} {bill.total:.2f}, due {bill.due_date.strftime('%d %b %Y')}. "
        f"View/download: {share_link}"
    )
    return render_template(
        "purchases/bill_detail.html", bill=bill,
        whatsapp_href=whatsapp_link(bill.vendor.phone, whatsapp_message),
    )


@purchases_bp.route("/bills/<int:bill_id>/pdf")
@login_required
def bill_pdf(bill_id):
    bill = scoped_or_404(Bill, bill_id)
    buffer = generate_bill_pdf(bill, current_company())
    return send_file(buffer, mimetype="application/pdf", as_attachment=False, download_name=f"{bill.bill_no}.pdf")


@purchases_bp.route("/bills/<int:bill_id>/void", methods=["POST"])
@login_required
def bill_void(bill_id):
    bill = scoped_or_404(Bill, bill_id)
    if bill.status == "pending_approval":
        flash("This bill is still awaiting approval — reject it instead of voiding (it was never posted).", "error")
        return redirect(url_for("purchases.bill_detail", bill_id=bill.id))
    if bill.amount_paid > 0:
        flash("Cannot void a bill that already has payments applied. Unapply payments first.", "error")
        return redirect(url_for("purchases.bill_detail", bill_id=bill.id))

    for line in bill.lines:
        if line.item_id:
            item = line.item
            if float(line.quantity) > float(item.quantity_on_hand):
                flash(
                    f"Cannot void: {item.name} only has {item.quantity_on_hand} {item.unit} left "
                    f"(some of this bill's stock has already been sold).", "error",
                )
                return redirect(url_for("purchases.bill_detail", bill_id=bill.id))

    for line in bill.lines:
        if line.item_id:
            item = line.item
            if item.costing_method in ("fifo", "lifo"):
                # Removes from whichever layer(s) fifo/lifo order says to consume next —
                # not necessarily the exact layer this bill opened, if some of it has
                # already sold. Same "current state, not exact history" tradeoff as the
                # weighted-average case here already accepted.
                item.consume_stock_layers(float(line.quantity))
                item.refresh_average_cost()
            item.quantity_on_hand = float(item.quantity_on_hand) - float(line.quantity)  # cost_price left as-is
            db.session.add(StockMovement(
                item_id=item.id, movement_date=date.today(), movement_type="purchase",
                quantity=-float(line.quantity), unit_cost=item.cost_price,
                reference_type="bill", reference_id=bill.id,
                running_quantity=item.quantity_on_hand, running_avg_cost=item.cost_price,
                memo=f"Reversal - {bill.bill_no} voided",
            ))

    if bill.journal_entry_id:
        entry = JournalEntry.query.get(bill.journal_entry_id)
        if entry:
            db.session.delete(entry)
    bill.status = "void"
    bill.journal_entry_id = None
    log_audit("void", "bill", bill.id, f"Voided bill {bill.bill_no}", bill.bill_no)
    db.session.commit()
    flash(f"Bill {bill.bill_no} voided.", "success")
    return redirect(url_for("purchases.bill_list"))


# ── Recurring Bills ──────────────────────────────────────────────────
# Vendor-side mirror of Sales > Recurring Invoices — a template that generates a real
# Bill on a schedule (rent, subscriptions, retainers). Generation always goes through
# post_bill(), so a recurring bill is indistinguishable from a hand-entered one once
# it lands in the ledger.

def _build_bill_from_template(template):
    """Creates and posts one real Bill from a RecurringBill template, dated today.
    Does not commit — caller commits once, after any bookkeeping (advancing next_run_date etc).
    """
    bill_date = date.today()
    due_date = date.fromordinal(bill_date.toordinal() + template.due_days)
    bill = Bill(
        company_id=current_company_id(),
        bill_no=next_bill_no(),
        vendor_id=template.vendor_id,
        bill_date=bill_date,
        due_date=due_date,
        memo=f"[Recurring: {template.name}]" + (f" - {template.memo}" if template.memo else ""),
        vat_rate=template.vat_rate,
    )
    for line in template.lines:
        bill.lines.append(
            BillLine(
                item_id=line.item_id, description=line.description, quantity=line.quantity,
                unit_price=line.unit_price, taxable=line.taxable, expense_account_id=line.expense_account_id,
            )
        )

    db.session.add(bill)
    db.session.flush()
    post_bill(bill)
    return bill


@purchases_bp.route("/recurring-bills")
@login_required
def recurring_bill_list():
    templates = scoped_query(RecurringBill).order_by(RecurringBill.next_run_date).all()
    due_count = sum(1 for t in templates if t.is_due)
    return render_template("purchases/recurring_bill_list.html", templates=templates, due_count=due_count, today=date.today())


@purchases_bp.route("/recurring-bills/new", methods=["GET", "POST"])
@login_required
def recurring_bill_new():
    vendors = scoped_query(Vendor).filter_by(is_active=True).order_by(Vendor.name).all()
    expense_accounts = scoped_query(Account).filter(
        Account.is_active == True,  # noqa: E712
        (Account.account_type == "Expense") | (Account.code.in_(["1300", "1500"])),
    ).order_by(Account.code).all()
    default_expense = scoped_query(Account).filter_by(code=DEFAULT_EXPENSE_CODE).first()
    items = scoped_query(Item).filter_by(is_active=True).order_by(Item.sku).all()

    def render_form(form):
        return render_template(
            "purchases/recurring_bill_form.html", vendors=vendors, expense_accounts=expense_accounts,
            default_expense=default_expense, items=items, form=form, today=date.today().isoformat(),
            frequencies=RECURRING_FREQUENCIES,
        )

    if request.method == "POST":
        vendor_id = request.form.get("vendor_id")
        name = request.form.get("name", "").strip()
        if not vendor_id or not name:
            flash("Name and vendor are both required.", "error")
            return render_form(request.form)

        start_date = datetime.strptime(request.form["start_date"], "%Y-%m-%d").date()
        end_raw = request.form.get("end_date", "").strip()
        end_date = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else None
        frequency = request.form.get("frequency", "monthly")
        if frequency not in RECURRING_FREQUENCIES:
            frequency = "monthly"

        descriptions = request.form.getlist("description")
        quantities = request.form.getlist("quantity")
        unit_prices = request.form.getlist("unit_price")
        taxables = request.form.getlist("taxable")
        expense_account_ids = request.form.getlist("expense_account_id")
        item_ids = request.form.getlist("item_id")
        item_ids += [""] * (len(descriptions) - len(item_ids))

        template = RecurringBill(
            company_id=current_company_id(),
            name=name,
            vendor_id=int(vendor_id),
            memo=request.form.get("memo", "").strip() or None,
            vat_rate=float(request.form.get("vat_rate") or 15.00),
            frequency=frequency,
            due_days=int(request.form.get("due_days") or 30),
            start_date=start_date,
            next_run_date=start_date,
            end_date=end_date,
        )

        for i, (desc, qty, price, acc_id, item_id_raw) in enumerate(
            zip(descriptions, quantities, unit_prices, expense_account_ids, item_ids)
        ):
            if not desc.strip() or not qty or not price:
                continue
            item = scoped_get(Item, int(item_id_raw)) if item_id_raw else None
            template.lines.append(
                RecurringBillLine(
                    item=item,
                    description=desc.strip(),
                    quantity=float(qty),
                    unit_price=float(price),
                    taxable=str(i) in taxables,
                    expense_account_id=item.inventory_account_id if (item and item.is_tracked) else int(acc_id),
                )
            )

        if not template.lines:
            flash("Add at least one line.", "error")
            return render_form(request.form)

        db.session.add(template)
        db.session.flush()
        log_audit("create", "recurring_bill", template.id, f"Created recurring bill template '{template.name}'", template.name)
        db.session.commit()
        flash(f"Recurring bill template '{template.name}' created — first bill due {template.next_run_date}.", "success")
        return redirect(url_for("purchases.recurring_bill_detail", template_id=template.id))

    return render_form({})


@purchases_bp.route("/recurring-bills/<int:template_id>")
@login_required
def recurring_bill_detail(template_id):
    template = scoped_or_404(RecurringBill, template_id)
    generated = (
        scoped_query(Bill)
        .filter(Bill.memo.like(f"[Recurring: {template.name}]%"))
        .order_by(Bill.bill_date.desc(), Bill.id.desc())
        .all()
    )
    return render_template("purchases/recurring_bill_detail.html", template=template, generated=generated)


@purchases_bp.route("/recurring-bills/<int:template_id>/toggle", methods=["POST"])
@login_required
def recurring_bill_toggle(template_id):
    template = scoped_or_404(RecurringBill, template_id)
    if not template.is_active and template.is_ended:
        flash("This template's end date has passed — extend the end date before reactivating it.", "error")
        return redirect(url_for("purchases.recurring_bill_detail", template_id=template.id))
    template.is_active = not template.is_active
    log_audit(
        "update", "recurring_bill", template.id,
        f"{'Resumed' if template.is_active else 'Paused'} recurring bill '{template.name}'", template.name,
    )
    db.session.commit()
    flash(f"'{template.name}' {'resumed' if template.is_active else 'paused'}.", "success")
    return redirect(url_for("purchases.recurring_bill_detail", template_id=template.id))


@purchases_bp.route("/recurring-bills/<int:template_id>/delete", methods=["POST"])
@login_required
def recurring_bill_delete(template_id):
    template = scoped_or_404(RecurringBill, template_id)
    if template.bills_generated > 0:
        flash("Can't delete a template that has already generated bills — pause it instead.", "error")
        return redirect(url_for("purchases.recurring_bill_detail", template_id=template.id))
    name = template.name
    db.session.delete(template)
    log_audit("delete", "recurring_bill", template_id, f"Deleted unused recurring bill template '{name}'", name)
    db.session.commit()
    flash(f"Template '{name}' deleted.", "success")
    return redirect(url_for("purchases.recurring_bill_list"))


@purchases_bp.route("/recurring-bills/<int:template_id>/generate", methods=["POST"])
@login_required
def recurring_bill_generate_now(template_id):
    """Generates one bill from this template immediately, regardless of next_run_date,
    then advances the schedule by one occurrence — same as if it had come due today."""
    template = scoped_or_404(RecurringBill, template_id)
    bill = _build_bill_from_template(template)

    template.advance_next_run_date()
    template.last_generated_at = datetime.utcnow()
    template.bills_generated += 1
    log_audit(
        "generate", "recurring_bill", template.id,
        f"Generated bill {bill.bill_no} from template '{template.name}'", template.name,
    )
    db.session.commit()

    flash(f"Bill {bill.bill_no} generated from '{template.name}'.", "success")
    return redirect(url_for("purchases.bill_detail", bill_id=bill.id))


@purchases_bp.route("/recurring-bills/run-due", methods=["POST"])
@login_required
def recurring_bill_run_due():
    """Bulk action: generates one bill for every active template in the current company
    whose next_run_date has arrived. Same underlying logic the nightly scheduler (app/scheduler.py)
    runs automatically across every company — this button exists so a due bill can be produced
    on demand, without waiting for the next scheduled pass."""
    generated_numbers, skipped = generate_due_bills_for_current_company(source="manual")
    if not generated_numbers and not skipped:
        flash("No recurring bills are due right now.", "success")
    if generated_numbers:
        flash(f"Generated {len(generated_numbers)} bill(s): {', '.join(generated_numbers)}.", "success")
    for message in skipped:
        flash(message, "error")
    return redirect(url_for("purchases.recurring_bill_list"))


def generate_due_bills_for_current_company(source="manual"):
    """Generates one bill for every active template in the *currently active company*
    (from current_company_id()) whose next_run_date has arrived, advancing each template's
    schedule as it goes. Company-agnostic caller convention — see the identical comment on
    sales.generate_due_invoices_for_current_company.

    Returns (generated_bill_numbers, skipped_error_messages).
    """
    due_templates = [t for t in scoped_query(RecurringBill).all() if t.is_due]
    generated_numbers = []
    skipped = []
    for template in due_templates:
        try:
            bill = _build_bill_from_template(template)
        except Exception as exc:  # a missing Chart-of-Accounts entry etc — never let one bad template block the rest
            db.session.rollback()
            skipped.append(f"{template.name}: {exc}")
            continue
        template.advance_next_run_date()
        template.last_generated_at = datetime.utcnow()
        template.bills_generated += 1
        log_audit(
            "generate", "recurring_bill", template.id,
            f"Generated bill {bill.bill_no} from template '{template.name}' ({source} run)", template.name,
        )
        db.session.commit()
        generated_numbers.append(bill.bill_no)
    return generated_numbers, skipped


# ── Vendor Credits ────────────────────────────────────────────────────

@purchases_bp.route("/vendor-credits")
@login_required
def vendor_credit_list():
    vendor_credits = scoped_query(VendorCredit).order_by(VendorCredit.credit_date.desc(), VendorCredit.id.desc()).all()
    return render_template("purchases/vendor_credits.html", vendor_credits=vendor_credits)


@purchases_bp.route("/vendor-credits/new", methods=["GET", "POST"])
@login_required
def vendor_credit_new():
    vendors = scoped_query(Vendor).filter_by(is_active=True).order_by(Vendor.name).all()
    expense_accounts = scoped_query(Account).filter(
        Account.is_active == True,  # noqa: E712
        (Account.account_type == "Expense") | (Account.code.in_(["1300", "1500"])),
    ).order_by(Account.code).all()
    default_expense = scoped_query(Account).filter_by(code=DEFAULT_EXPENSE_CODE).first()
    items = scoped_query(Item).filter_by(is_active=True).order_by(Item.sku).all()
    preselected_vendor_id = request.args.get("vendor_id", type=int)

    def render_form(form):
        return render_template(
            "purchases/vendor_credit_form.html", vendors=vendors, expense_accounts=expense_accounts,
            default_expense=default_expense, items=items, form=form, today=date.today().isoformat(),
        )

    if request.method == "POST":
        vendor_id = request.form.get("vendor_id")
        if not vendor_id:
            flash("Select a vendor.", "error")
            return render_form(request.form)

        credit_date = datetime.strptime(request.form["credit_date"], "%Y-%m-%d").date()
        descriptions = request.form.getlist("description")
        quantities = request.form.getlist("quantity")
        unit_prices = request.form.getlist("unit_price")
        taxables = request.form.getlist("taxable")
        expense_account_ids = request.form.getlist("expense_account_id")
        item_ids = request.form.getlist("item_id")
        return_flags = request.form.getlist("return_to_vendor")
        item_ids += [""] * (len(descriptions) - len(item_ids))

        vendor_credit = VendorCredit(
            company_id=current_company_id(),
            credit_no=next_vendor_credit_no(),
            vendor_id=int(vendor_id),
            credit_date=credit_date,
            memo=request.form.get("memo", "").strip() or None,
            vat_rate=float(request.form.get("vat_rate") or 15.00),
        )

        for i, (desc, qty, price, acc_id, item_id_raw) in enumerate(
            zip(descriptions, quantities, unit_prices, expense_account_ids, item_ids)
        ):
            if not desc.strip() or not qty or not price:
                continue
            item = scoped_get(Item, int(item_id_raw)) if item_id_raw else None
            tracked_item = item if (item and item.is_tracked) else None
            vendor_credit.lines.append(
                VendorCreditLine(
                    item=tracked_item,
                    description=desc.strip(),
                    quantity=float(qty),
                    unit_price=float(price),
                    taxable=str(i) in taxables,
                    expense_account_id=tracked_item.inventory_account_id if tracked_item else int(acc_id),
                    return_to_vendor=str(i) in return_flags,
                )
            )

        if not vendor_credit.lines:
            flash("Add at least one line.", "error")
            return render_form(request.form)

        # If returning tracked stock to the vendor, make sure we actually have it to send back.
        required_qty = {}
        for line in vendor_credit.lines:
            if line.item and line.item.is_tracked and line.return_to_vendor:
                required_qty[line.item] = required_qty.get(line.item, 0) + float(line.quantity)
        for item, needed in required_qty.items():
            if needed > float(item.quantity_on_hand):
                flash(
                    f"Not enough stock to return for {item.name} ({item.sku}): have {item.quantity_on_hand} "
                    f"{item.unit}, need {needed}.", "error",
                )
                return render_form(request.form)

        db.session.add(vendor_credit)
        db.session.flush()
        post_vendor_credit(vendor_credit)
        log_audit("create", "vendor_credit", vendor_credit.id, f"Created vendor credit {vendor_credit.credit_no} (total {vendor_credit.total:.2f})", vendor_credit.credit_no)
        db.session.commit()
        flash(f"Vendor credit {vendor_credit.credit_no} created and posted to the ledger.", "success")
        return redirect(url_for("purchases.vendor_credit_detail", vendor_credit_id=vendor_credit.id))

    return render_form({"vendor_id": preselected_vendor_id} if preselected_vendor_id else {})


def post_vendor_credit(vendor_credit):
    """Dr Accounts Payable, Cr Expense/Asset (per line) + Cr VAT Receivable. Returned stock: Cr Inventory / Dr COGS."""
    ap_account = get_account_or_400(AP_ACCOUNT_CODE, "Accounts Payable")

    entry = JournalEntry(
        company_id=current_company_id(),
        entry_date=vendor_credit.credit_date,
        reference_no=vendor_credit.credit_no,
        memo=f"Vendor Credit {vendor_credit.credit_no} - {vendor_credit.memo or ''}".strip(" -"),
        source_type="vendor_credit",
        created_by=current_user.id,
    )
    entry.lines.append(JournalLine(account=ap_account, debit=vendor_credit.total, credit=0, memo=vendor_credit.credit_no))

    expense_totals = {}
    for line in vendor_credit.lines:
        if line.item_id:
            continue  # tracked-item lines are handled below via return_to_vendor logic instead
        expense_totals[line.expense_account_id] = expense_totals.get(line.expense_account_id, 0) + line.amount
    for account_id, amount in expense_totals.items():
        entry.lines.append(JournalLine(account_id=account_id, debit=0, credit=amount, memo=vendor_credit.credit_no))

    if vendor_credit.vat_amount:
        vat_account = get_account_or_400(VAT_RECEIVABLE_CODE, "VAT Receivable")
        entry.lines.append(JournalLine(account=vat_account, debit=0, credit=vendor_credit.vat_amount, memo=vendor_credit.credit_no))

    # Purchase returns never touch COGS — this stock was never sold. Every tracked-item line
    # (returned physically or not) just reverses its original purchase Dr Inventory with a
    # matching Cr Inventory here; only a physical return additionally reduces quantity on hand.
    inventory_totals = {}
    for line in vendor_credit.lines:
        if not line.item_id:
            continue
        item = line.item
        inventory_totals[item.inventory_account_id] = inventory_totals.get(item.inventory_account_id, 0) + line.amount
        if line.return_to_vendor:
            cost = float(item.cost_price)
            item.issue_stock(line.quantity)  # physically leaving — raises if somehow oversold
            db.session.add(StockMovement(
                item_id=item.id, movement_date=vendor_credit.credit_date, movement_type="purchase",
                quantity=-float(line.quantity), unit_cost=cost,
                reference_type="vendor_credit", reference_id=vendor_credit.id,
                running_quantity=item.quantity_on_hand, running_avg_cost=item.cost_price,
                memo=f"Returned to vendor via {vendor_credit.credit_no}",
            ))

    for account_id, amount in inventory_totals.items():
        entry.lines.append(JournalLine(account_id=account_id, debit=0, credit=amount, memo=f"Credit - {vendor_credit.credit_no}"))

    db.session.add(entry)
    db.session.flush()
    vendor_credit.journal_entry_id = entry.id


@purchases_bp.route("/vendor-credits/<int:vendor_credit_id>")
@login_required
def vendor_credit_detail(vendor_credit_id):
    vendor_credit = scoped_or_404(VendorCredit, vendor_credit_id)
    open_bills = sorted(
        [b for b in vendor_credit.vendor.bills if b.status in ("open", "partial")], key=lambda b: b.due_date
    )
    return render_template("purchases/vendor_credit_detail.html", vendor_credit=vendor_credit, open_bills=open_bills)


@purchases_bp.route("/vendor-credits/<int:vendor_credit_id>/apply", methods=["POST"])
@login_required
def vendor_credit_apply(vendor_credit_id):
    vendor_credit = scoped_or_404(VendorCredit, vendor_credit_id)
    bill_ids = request.form.getlist("apply_bill_id")
    apply_amounts = request.form.getlist("apply_amount")

    total_applied = 0.0
    applications = []
    for bill_id, amt_raw in zip(bill_ids, apply_amounts):
        amt = float(amt_raw or 0)
        if amt <= 0:
            continue
        applications.append((int(bill_id), amt))
        total_applied += amt

    if round(total_applied, 2) > round(vendor_credit.remaining_credit, 2):
        flash(
            f"Applied amount ({total_applied:.2f}) exceeds remaining credit ({vendor_credit.remaining_credit:.2f}).",
            "error",
        )
        return redirect(url_for("purchases.vendor_credit_detail", vendor_credit_id=vendor_credit.id))

    for bill_id, amt in applications:
        vendor_credit.applications.append(VendorCreditApplication(bill_id=bill_id, amount_applied=amt))
    db.session.flush()

    for bill_id, _amt in applications:
        bill = scoped_get(Bill, bill_id)
        bill.status = "paid" if bill.balance_due <= 0 else "partial"

    if vendor_credit.remaining_credit <= 0:
        vendor_credit.status = "closed"

    log_audit("apply", "vendor_credit", vendor_credit.id, f"Applied {total_applied:.2f} of {vendor_credit.credit_no} to bill(s)", vendor_credit.credit_no)
    db.session.commit()
    flash(f"Applied {total_applied:.2f} credit to bill(s).", "success")
    return redirect(url_for("purchases.vendor_credit_detail", vendor_credit_id=vendor_credit.id))


@purchases_bp.route("/vendor-credits/<int:vendor_credit_id>/void", methods=["POST"])
@login_required
def vendor_credit_void(vendor_credit_id):
    vendor_credit = scoped_or_404(VendorCredit, vendor_credit_id)
    if vendor_credit.amount_applied > 0:
        flash("Cannot void a vendor credit that's already been applied to a bill. Unapply it first.", "error")
        return redirect(url_for("purchases.vendor_credit_detail", vendor_credit_id=vendor_credit.id))

    for line in vendor_credit.lines:
        if line.item_id and line.return_to_vendor:
            item = line.item
            item.quantity_on_hand = float(item.quantity_on_hand) + float(line.quantity)
            db.session.add(StockMovement(
                item_id=item.id, movement_date=date.today(), movement_type="purchase",
                quantity=float(line.quantity), unit_cost=item.cost_price,
                reference_type="vendor_credit", reference_id=vendor_credit.id,
                running_quantity=item.quantity_on_hand, running_avg_cost=item.cost_price,
                memo=f"Reversal - {vendor_credit.credit_no} voided",
            ))

    if vendor_credit.journal_entry_id:
        entry = JournalEntry.query.get(vendor_credit.journal_entry_id)
        if entry:
            db.session.delete(entry)
    vendor_credit.status = "void"
    vendor_credit.journal_entry_id = None
    log_audit("void", "vendor_credit", vendor_credit.id, f"Voided vendor credit {vendor_credit.credit_no}", vendor_credit.credit_no)
    db.session.commit()
    flash(f"Vendor credit {vendor_credit.credit_no} voided.", "success")
    return redirect(url_for("purchases.vendor_credit_list"))


# ── Vendor Payments ───────────────────────────────────────────────────

@purchases_bp.route("/payments")
@login_required
def payment_list():
    payments = scoped_query(VendorPayment).order_by(VendorPayment.payment_date.desc(), VendorPayment.id.desc()).all()
    return render_template("purchases/payments.html", payments=payments)


@purchases_bp.route("/payments/new", methods=["GET", "POST"])
@login_required
def payment_new():
    vendors = scoped_query(Vendor).filter_by(is_active=True).order_by(Vendor.name).all()
    source_accounts = scoped_query(Account).filter(
        Account.account_type == "Asset", Account.is_active == True,  # noqa: E712
        Account.subtype == "Cash and Cash Equivalents",
    ).order_by(Account.code).all()
    base_currency = current_company().base_currency

    vendor_id = request.values.get("vendor_id", type=int)
    is_advance = bool(request.values.get("advance"))
    outstanding_bills = []
    if vendor_id:
        vendor = scoped_get(Vendor, vendor_id)
        if vendor:
            outstanding_bills = sorted(
                [b for b in vendor.bills if b.status in ("open", "partial")], key=lambda b: b.due_date
            )

    if request.method == "POST":
        amount = float(request.form["amount"])
        currency = request.form.get("currency", base_currency) or base_currency
        exchange_rate = float(request.form.get("exchange_rate") or 1.0) if currency != base_currency else 1.0
        bill_ids = request.form.getlist("apply_bill_id")
        apply_amounts = request.form.getlist("apply_amount")

        applications = []
        total_applied = 0
        for bill_id, amt_raw in zip(bill_ids, apply_amounts):
            amt = float(amt_raw or 0)
            if amt <= 0:
                continue
            applications.append((int(bill_id), amt))
            total_applied += amt

        def render_error(message):
            flash(message, "error")
            return render_template(
                "purchases/payment_form.html", vendors=vendors, source_accounts=source_accounts,
                selected_vendor_id=vendor_id, outstanding_bills=outstanding_bills,
                form=request.form, today=date.today().isoformat(), currencies=CURRENCIES, base_currency=base_currency,
                is_advance=is_advance,
            )

        if exchange_rate <= 0:
            return render_error("Exchange rate must be greater than zero.")
        if round(total_applied, 2) > round(amount, 2):
            return render_error(f"Applied amount ({total_applied:.2f}) exceeds payment amount ({amount:.2f}).")

        applied_bills = [scoped_get(Bill, bill_id) for bill_id, _ in applications]
        mismatched = [b for b in applied_bills if b and b.currency != currency]
        if mismatched:
            names = ", ".join(b.bill_no for b in mismatched)
            return render_error(
                f"This payment is in {currency}, but {names} {'is' if len(mismatched) == 1 else 'are'} in a "
                f"different currency. Record separate payments per currency."
            )

        payment = VendorPayment(
            company_id=current_company_id(),
            vendor_id=vendor_id,
            payment_date=datetime.strptime(request.form["payment_date"], "%Y-%m-%d").date(),
            amount=amount,
            currency=currency,
            exchange_rate=exchange_rate,
            method=request.form.get("method", "bank"),
            source_account_id=int(request.form["source_account_id"]),
            reference_no=request.form.get("reference_no", "").strip() or None,
            memo=request.form.get("memo", "").strip() or None,
        )
        bills_by_id = {b.id: b for b in applied_bills if b}
        for bill_id, amt in applications:
            # Assigning the bill relationship (not just bill_id) keeps app.bill populated
            # in memory before this payment is added to the session — post_vendor_payment()
            # needs each application's bill.exchange_rate to compute FX.
            payment.applications.append(VendorPaymentApplication(bill=bills_by_id[bill_id], amount_applied=amt))

        post_vendor_payment(payment)
        db.session.add(payment)
        db.session.flush()

        for bill_id, _amt in applications:
            bill = scoped_get(Bill, bill_id)
            if bill.balance_due <= 0:
                bill.status = "paid"
            else:
                bill.status = "partial"

        log_audit("create", "vendor_payment", payment.id, f"Recorded payment of {amount:.2f} to vendor #{vendor_id}")
        db.session.commit()
        flash(f"Payment of {amount:.2f} recorded.", "success")
        return redirect(url_for("purchases.payment_detail", payment_id=payment.id))

    return render_template(
        "purchases/payment_form.html", vendors=vendors, source_accounts=source_accounts,
        selected_vendor_id=vendor_id, outstanding_bills=outstanding_bills,
        form={}, today=date.today().isoformat(), currencies=CURRENCIES, base_currency=base_currency,
        is_advance=is_advance,
    )


def post_vendor_payment(payment):
    """Dr Accounts Payable (at each bill's own booked rate), Cr Cash/Bank (at the payment's
    own rate). Mirrors sales.post_payment's FX handling for the AP side — see that function's
    docstring for the full reasoning. For a base-currency payment this is unchanged from before."""
    ap_account = get_account_or_400(AP_ACCOUNT_CODE, "Accounts Payable")
    payment_rate = float(payment.exchange_rate)

    entry = JournalEntry(
        company_id=current_company_id(),
        entry_date=payment.payment_date,
        reference_no=payment.reference_no,
        memo=f"Payment to vendor - {payment.memo or ''}".strip(" -"),
        source_type="vendor_payment",
        created_by=current_user.id,
    )

    cash_base = round(float(payment.amount) * payment_rate, 2)

    ap_relief_total = 0.0
    for app in payment.applications:
        ap_relief_total += round(float(app.amount_applied) * app.bill.carrying_exchange_rate, 2)
    unapplied = round(float(payment.amount) - sum(float(a.amount_applied) for a in payment.applications), 2)
    ap_relief_total = round(ap_relief_total + unapplied * payment_rate, 2)

    entry.lines.append(JournalLine(account=ap_account, debit=ap_relief_total, credit=0))
    entry.lines.append(JournalLine(account_id=payment.source_account_id, debit=0, credit=cash_base))

    fx_gain_loss = round(ap_relief_total - cash_base, 2)
    if fx_gain_loss:
        fx_account = get_account_or_400(FX_GAIN_LOSS_CODE, "Realized Gain/Loss on Exchange")
        if fx_gain_loss > 0:
            entry.lines.append(JournalLine(account=fx_account, debit=0, credit=fx_gain_loss, memo="Realized FX gain"))
        else:
            entry.lines.append(JournalLine(account=fx_account, debit=-fx_gain_loss, credit=0, memo="Realized FX loss"))

    db.session.add(entry)
    db.session.flush()
    payment.journal_entry_id = entry.id


@purchases_bp.route("/payments/<int:payment_id>")
@login_required
def payment_detail(payment_id):
    payment = scoped_or_404(VendorPayment, payment_id)
    return render_template("purchases/payment_detail.html", payment=payment)


# ── Reports ─────────────────────────────────────────────────────────

AGING_BUCKET_LABELS = [("current", "Current"), ("1_30", "1-30 Days"), ("31_60", "31-60 Days"), ("61_90", "61-90 Days"), ("90_plus", "90+ Days")]


def _ap_aging_buckets():
    today = date.today()
    bills = scoped_query(Bill).filter(Bill.status.in_(["open", "partial"])).all()

    buckets = {"current": [], "1_30": [], "31_60": [], "61_90": [], "90_plus": []}
    for bill in bills:
        if bill.balance_due <= 0:
            continue
        days = (today - bill.due_date).days
        if days <= 0:
            buckets["current"].append(bill)
        elif days <= 30:
            buckets["1_30"].append(bill)
        elif days <= 60:
            buckets["31_60"].append(bill)
        elif days <= 90:
            buckets["61_90"].append(bill)
        else:
            buckets["90_plus"].append(bill)

    totals = {key: sum((b.balance_due for b in bs), start=0.0) for key, bs in buckets.items()}
    grand_total = sum(totals.values(), start=0.0)
    return today, buckets, totals, grand_total


@purchases_bp.route("/aging")
@login_required
def aging_report():
    today, buckets, totals, grand_total = _ap_aging_buckets()
    return render_template("purchases/aging.html", buckets=buckets, totals=totals, grand_total=grand_total, today=today)


def _ap_aging_rows():
    today, buckets, _, _ = _ap_aging_buckets()
    rows = []
    for key, label in AGING_BUCKET_LABELS:
        for bill in buckets[key]:
            rows.append((label, bill.bill_no, bill.vendor.name, bill.due_date.isoformat(), bill.days_overdue, round(bill.balance_due, 2)))
    return today, rows


@purchases_bp.route("/aging/export.xlsx")
@login_required
def aging_export_xlsx():
    today, rows = _ap_aging_rows()
    headers = ["Bucket", "Bill #", "Vendor", "Due Date", "Days Overdue", "Balance Due"]
    buffer = rows_to_xlsx(headers, rows, sheet_title="AP Aging")
    return send_file(
        buffer, as_attachment=True, download_name=f"AP-Aging-{today.isoformat()}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@purchases_bp.route("/aging/export.pdf")
@login_required
def aging_export_pdf():
    today, rows = _ap_aging_rows()
    headers = ["Bucket", "Bill #", "Vendor", "Due Date", "Days Overdue", "Balance Due"]
    buffer = rows_to_pdf(
        "Accounts Payable Aging", f"As of {today.isoformat()}", headers, rows,
        company=current_company(), numeric_cols={4, 5},
    )
    return send_file(
        buffer, as_attachment=True, download_name=f"AP-Aging-{today.isoformat()}.pdf", mimetype="application/pdf",
    )
