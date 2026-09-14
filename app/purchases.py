from datetime import date, datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from app import db
from app.audit import log_audit
from app.auth import current_company, current_company_id
from app.models import (
    Account, Bill, BillLine, CURRENCIES, Item, JournalEntry, JournalLine, Project, PurchaseOrder,
    PurchaseOrderLine, StockMovement, Vendor, VendorCredit, VendorCreditApplication, VendorCreditLine,
    VendorPayment, VendorPaymentApplication,
)
from app.pdf import generate_bill_pdf
from app.scoping import scoped_get, scoped_or_404, scoped_query
from app.share_links import share_url, whatsapp_link

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
    if request.method == "POST":
        vendor = Vendor(
            company_id=current_company_id(),
            name=request.form["name"].strip(),
            email=request.form.get("email", "").strip() or None,
            phone=request.form.get("phone", "").strip() or None,
            address=request.form.get("address", "").strip() or None,
            vat_number=request.form.get("vat_number", "").strip() or None,
            brn=request.form.get("brn", "").strip() or None,
            mra_supplier_id=request.form.get("mra_supplier_id", "").strip() or None,
            opening_balance=request.form.get("opening_balance") or 0,
        )
        db.session.add(vendor)
        db.session.commit()
        flash(f"Vendor '{vendor.name}' created.", "success")
        return redirect(url_for("purchases.vendor_list"))
    return render_template("purchases/vendor_form.html", form={}, action_url=url_for("purchases.vendor_new"))


@purchases_bp.route("/vendors/<int:vendor_id>/edit", methods=["GET", "POST"])
@login_required
def vendor_edit(vendor_id):
    vendor = scoped_or_404(Vendor, vendor_id)
    if request.method == "POST":
        vendor.name = request.form["name"].strip()
        vendor.email = request.form.get("email", "").strip() or None
        vendor.phone = request.form.get("phone", "").strip() or None
        vendor.address = request.form.get("address", "").strip() or None
        vendor.vat_number = request.form.get("vat_number", "").strip() or None
        vendor.brn = request.form.get("brn", "").strip() or None
        vendor.mra_supplier_id = request.form.get("mra_supplier_id", "").strip() or None
        vendor.opening_balance = request.form.get("opening_balance") or 0
        db.session.commit()
        flash(f"Vendor '{vendor.name}' updated.", "success")
        return redirect(url_for("purchases.vendor_detail", vendor_id=vendor.id))
    return render_template(
        "purchases/vendor_form.html", form=vendor, action_url=url_for("purchases.vendor_edit", vendor_id=vendor.id),
        editing=True,
    )


@purchases_bp.route("/vendors/<int:vendor_id>/toggle", methods=["POST"])
@login_required
def vendor_toggle(vendor_id):
    vendor = scoped_or_404(Vendor, vendor_id)
    vendor.is_active = not vendor.is_active
    db.session.commit()
    flash(f"Vendor {vendor.name} {'activated' if vendor.is_active else 'deactivated'}.", "success")
    return redirect(url_for("purchases.vendor_list"))


@purchases_bp.route("/vendors/<int:vendor_id>")
@login_required
def vendor_detail(vendor_id):
    vendor = scoped_or_404(Vendor, vendor_id)
    bills = sorted([b for b in vendor.bills if b.status != "void"], key=lambda b: b.bill_date, reverse=True)
    payments = sorted(vendor.payments, key=lambda p: p.payment_date, reverse=True)
    return render_template("purchases/vendor_detail.html", vendor=vendor, bills=bills, payments=payments)


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
        for line in po.lines:
            qty_raw = request.form.get(f"qty_{line.id}", "0")
            qty = float(qty_raw or 0)
            if qty <= 0:
                continue
            if qty > float(line.quantity_remaining) + 0.0001:
                flash(
                    f"Can't bill {qty} of '{line.description}' — only {line.quantity_remaining} remaining "
                    f"on this PO.", "error",
                )
                return redirect(url_for("purchases.po_receive", po_id=po.id))
            any_billed = True
            bill.lines.append(
                BillLine(
                    item_id=line.item_id, description=line.description, quantity=qty,
                    unit_price=line.unit_price, taxable=line.taxable, expense_account_id=line.expense_account_id,
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
        flash(f"Bill {bill.bill_no} created from {po.po_no}.", "success")
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
    preselected_vendor_id = request.args.get("vendor_id", type=int)
    base_currency = current_company().base_currency

    def render_form(form):
        return render_template(
            "purchases/bill_form.html", vendors=vendors, expense_accounts=expense_accounts,
            default_expense=default_expense, items=items, projects=projects, form=form, today=date.today().isoformat(),
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
        # zip() truncates to the shortest list — pad item_ids so a row missing that field
        # doesn't silently drop every line below it (the real form always sends it, but be defensive).
        item_ids += [""] * (len(descriptions) - len(item_ids))
        line_vat_rates += [""] * (len(descriptions) - len(line_vat_rates))

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

        for i, (desc, qty, price, acc_id, item_id_raw, vat_rate_raw) in enumerate(
            zip(descriptions, quantities, unit_prices, expense_account_ids, item_ids, line_vat_rates)
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
                )
            )

        if not bill.lines:
            flash("Add at least one bill line.", "error")
            return render_form(request.form)

        db.session.add(bill)
        db.session.flush()  # assign bill.id before post_bill needs it for StockMovement.reference_id
        post_bill(bill)
        log_audit("create", "bill", bill.id, f"Created bill {bill.bill_no} (total {bill.total:.2f})", bill.bill_no)
        db.session.commit()
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
        ))

    db.session.add(entry)
    db.session.flush()
    bill.journal_entry_id = entry.id


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
        ap_relief_total += round(float(app.amount_applied) * float(app.bill.exchange_rate), 2)
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

@purchases_bp.route("/aging")
@login_required
def aging_report():
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

    return render_template("purchases/aging.html", buckets=buckets, totals=totals, grand_total=grand_total, today=today)
