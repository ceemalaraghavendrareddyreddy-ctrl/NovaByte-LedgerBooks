import smtplib
from datetime import date, datetime
from email.message import EmailMessage

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from werkzeug.security import generate_password_hash

from app import db
from app.audit import log_audit
from app.auth import current_company, current_company_id, owner_required
from app.models import (
    Account, CURRENCIES, CreditMemo, CreditMemoApplication, CreditMemoLine, Customer, Estimate,
    EstimateLine, Invoice, InvoiceLine, Item, JournalEntry, JournalLine, Payment, PaymentApplication,
    Project, RECURRING_FREQUENCIES, RecurringInvoice, RecurringInvoiceLine, SalesOrder, SalesOrderLine,
    StockMovement, TimeEntry, Warehouse,
)
from app.pdf import generate_credit_memo_pdf, generate_invoice_pdf
from app.report_export import rows_to_pdf, rows_to_xlsx
from app.scoping import scoped_get, scoped_or_404, scoped_query
from app.share_links import share_url, whatsapp_link
from app.mra_bridge import (
    fiscalize_invoice_with_mra, fiscalize_credit_memo_with_mra,
    void_invoice_via_credit_note, void_credit_memo_via_debit_note,
)
from app.webhooks import fire_webhook
from app.custom_fields import (
    get_field_definitions as get_custom_field_definitions,
    get_field_values as get_custom_field_values,
    save_field_values as save_custom_field_values,
    missing_required_fields as missing_required_custom_fields,
)

sales_bp = Blueprint("sales", __name__, url_prefix="/sales")

PAYMENT_TERMS_OPTIONS = ["Due on Receipt", "Net 7", "Net 15", "Net 30", "Net 45", "Net 60", "Custom"]

AR_ACCOUNT_CODE = "1200"
VAT_PAYABLE_CODE = "2100"
DEFAULT_INCOME_CODE = "4000"
UNDEPOSITED_FUNDS_CODE = "1100"
FX_GAIN_LOSS_CODE = "4920"


def get_account_or_400(code, label):
    account = Account.query.filter_by(company_id=current_company_id(), code=code).first()
    if not account:
        abort(400, f"Required account {code} ({label}) is missing from the Chart of Accounts.")
    return account


def next_invoice_no():
    last = scoped_query(Invoice).order_by(Invoice.id.desc()).first()
    next_num = (last.id + 1) if last else 1
    return f"INV-{next_num:04d}"


def next_estimate_no():
    last = scoped_query(Estimate).order_by(Estimate.id.desc()).first()
    next_num = (last.id + 1) if last else 1
    return f"EST-{next_num:04d}"


def next_order_no():
    last = scoped_query(SalesOrder).order_by(SalesOrder.id.desc()).first()
    next_num = (last.id + 1) if last else 1
    return f"SO-{next_num:04d}"


# ── Customers ────────────────────────────────────────────────────────

@sales_bp.route("/customers")
@login_required
def customer_list():
    customers = scoped_query(Customer).order_by(Customer.name).all()
    return render_template("sales/customers.html", customers=customers)


@sales_bp.route("/customers/new", methods=["GET", "POST"])
@login_required
def customer_new():
    custom_defs = get_custom_field_definitions("customer", current_company_id())
    if request.method == "POST":
        missing = missing_required_custom_fields(custom_defs, request.form)
        if missing:
            flash(f"Required field(s) missing: {', '.join(missing)}.", "error")
            return render_template(
                "sales/customer_form.html", form=request.form, action_url=url_for("sales.customer_new"),
                currencies=CURRENCIES, custom_fields=custom_defs, custom_values=request.form,
            )
        customer_type = request.form.get("customer_type") if request.form.get("customer_type") in ("individual", "company") else "company"
        customer = Customer(
            company_id=current_company_id(),
            name=request.form["name"].strip(),
            customer_type=customer_type,
            email=request.form.get("email", "").strip() or None,
            phone=request.form.get("phone", "").strip() or None,
            phone2=request.form.get("phone2", "").strip() or None,
            address=request.form.get("address", "").strip() or None,
            vat_number=request.form.get("vat_number", "").strip() or None,
            brn=request.form.get("brn", "").strip() or None if customer_type == "company" else None,
            billing_currency=request.form.get("billing_currency") or "MUR",
            opening_balance=request.form.get("opening_balance") or 0,
        )
        db.session.add(customer)
        db.session.flush()
        save_custom_field_values(customer.id, custom_defs, request.form)
        db.session.commit()
        flash(f"Customer '{customer.name}' created.", "success")
        return redirect(url_for("sales.customer_detail", customer_id=customer.id))
    return render_template(
        "sales/customer_form.html", form={}, action_url=url_for("sales.customer_new"), currencies=CURRENCIES,
        custom_fields=custom_defs, custom_values={},
    )


@sales_bp.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
@login_required
def customer_edit(customer_id):
    customer = scoped_or_404(Customer, customer_id)
    custom_defs = get_custom_field_definitions("customer", current_company_id())
    if request.method == "POST":
        missing = missing_required_custom_fields(custom_defs, request.form)
        if missing:
            flash(f"Required field(s) missing: {', '.join(missing)}.", "error")
            return render_template(
                "sales/customer_form.html", form=request.form, action_url=url_for("sales.customer_edit", customer_id=customer.id),
                editing=True, currencies=CURRENCIES, custom_fields=custom_defs, custom_values=request.form,
            )
        customer_type = request.form.get("customer_type") if request.form.get("customer_type") in ("individual", "company") else "company"
        customer.name = request.form["name"].strip()
        customer.customer_type = customer_type
        customer.email = request.form.get("email", "").strip() or None
        customer.phone = request.form.get("phone", "").strip() or None
        customer.phone2 = request.form.get("phone2", "").strip() or None
        customer.address = request.form.get("address", "").strip() or None
        customer.vat_number = request.form.get("vat_number", "").strip() or None
        customer.brn = request.form.get("brn", "").strip() or None if customer_type == "company" else None
        customer.billing_currency = request.form.get("billing_currency") or "MUR"
        customer.opening_balance = request.form.get("opening_balance") or 0
        save_custom_field_values(customer.id, custom_defs, request.form)
        db.session.commit()
        flash(f"Customer '{customer.name}' updated.", "success")
        return redirect(url_for("sales.customer_detail", customer_id=customer.id))
    custom_values = get_custom_field_values(customer.id, custom_defs)
    return render_template(
        "sales/customer_form.html", form=customer, action_url=url_for("sales.customer_edit", customer_id=customer.id),
        editing=True, currencies=CURRENCIES, custom_fields=custom_defs, custom_values=custom_values,
    )


@sales_bp.route("/customers/<int:customer_id>/toggle", methods=["POST"])
@login_required
def customer_toggle(customer_id):
    customer = scoped_or_404(Customer, customer_id)
    customer.is_active = not customer.is_active
    db.session.commit()
    flash(f"Customer {customer.name} {'activated' if customer.is_active else 'deactivated'}.", "success")
    return redirect(url_for("sales.customer_list"))


@sales_bp.route("/customers/<int:customer_id>/portal-access", methods=["POST"])
@login_required
def customer_portal_access(customer_id):
    customer = scoped_or_404(Customer, customer_id)
    portal_enabled = request.form.get("portal_enabled") == "on"
    if portal_enabled and not customer.email:
        flash("Add an email address for this customer before enabling portal access.", "error")
        return redirect(url_for("sales.customer_detail", customer_id=customer.id))

    new_password = request.form.get("portal_password", "")
    if new_password.strip():
        if len(new_password) < 6:
            flash("Portal password must be at least 6 characters.", "error")
            return redirect(url_for("sales.customer_detail", customer_id=customer.id))
        customer.portal_password_hash = generate_password_hash(new_password)
    elif portal_enabled and not customer.portal_password_hash:
        flash("Set a password before enabling portal access.", "error")
        return redirect(url_for("sales.customer_detail", customer_id=customer.id))

    customer.portal_enabled = portal_enabled
    db.session.commit()
    log_audit("edit", "customer", customer.id, f"{'Enabled' if portal_enabled else 'Disabled'} portal access for {customer.name}")
    flash(f"Portal access {'enabled' if portal_enabled else 'updated'} for {customer.name}.", "success")
    return redirect(url_for("sales.customer_detail", customer_id=customer.id))


@sales_bp.route("/customers/<int:customer_id>")
@login_required
def customer_detail(customer_id):
    customer = scoped_or_404(Customer, customer_id)
    invoices = sorted(
        [inv for inv in customer.invoices if inv.status != "void"], key=lambda i: i.invoice_date, reverse=True
    )
    payments = sorted(customer.payments, key=lambda p: p.payment_date, reverse=True)
    custom_defs = get_custom_field_definitions("customer", current_company_id())
    custom_values = get_custom_field_values(customer.id, custom_defs)
    return render_template(
        "sales/customer_detail.html", customer=customer, invoices=invoices, payments=payments,
        custom_fields=custom_defs, custom_values=custom_values,
    )


def _customer_statement_data(customer, start_date, end_date):
    """One customer's AR activity as a chronological ledger — the same three document
    types that make up customer.balance_due (invoices, payments, credit memos), each
    contributing a signed movement, with a running balance after every line. Returns
    (opening_balance, rows, closing_balance); rows already carry the "balance" key.

    Transactions are gathered and sorted first, THEN windowed to [start_date, end_date] —
    opening_balance is the running total right before the window starts, closing_balance
    is the running total after the last transaction on or before end_date, so both are
    correct even when the window doesn't start at the customer's very first transaction.
    """
    transactions = []
    for inv in customer.invoices:
        if inv.status == "void":
            continue
        transactions.append({
            "date": inv.invoice_date, "type": "Invoice", "doc_no": inv.invoice_no, "id": inv.id,
            "debit": float(inv.total), "credit": 0.0,
            "url": url_for("sales.invoice_detail", invoice_id=inv.id),
        })
    for p in customer.payments:
        transactions.append({
            "date": p.payment_date, "type": "Payment", "doc_no": p.reference_no or f"Payment #{p.id}", "id": p.id,
            "debit": 0.0, "credit": float(p.amount),
            "url": url_for("sales.payment_detail", payment_id=p.id),
        })
    for cm in scoped_query(CreditMemo).filter_by(customer_id=customer.id).all():
        if cm.status == "void":
            continue
        transactions.append({
            "date": cm.credit_date, "type": "Credit Memo", "doc_no": cm.credit_no, "id": cm.id,
            "debit": 0.0, "credit": float(cm.total),
            "url": url_for("sales.credit_memo_detail", credit_memo_id=cm.id),
        })
    transactions.sort(key=lambda t: (t["date"], t["type"]))

    running = float(customer.opening_balance)
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


@sales_bp.route("/customers/<int:customer_id>/statement")
@login_required
def customer_statement(customer_id):
    customer = scoped_or_404(Customer, customer_id)
    start_raw = request.args.get("start_date")
    end_raw = request.args.get("end_date")
    start_date = datetime.strptime(start_raw, "%Y-%m-%d").date() if start_raw else date(date.today().year, 1, 1)
    end_date = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else date.today()

    opening_balance, rows, closing_balance = _customer_statement_data(customer, start_date, end_date)
    return render_template(
        "sales/customer_statement.html", customer=customer, rows=rows,
        opening_balance=opening_balance, closing_balance=closing_balance,
        start_date=start_date.isoformat(), end_date=end_date.isoformat(),
    )


@sales_bp.route("/customers/<int:customer_id>/statement.pdf")
@login_required
def customer_statement_pdf(customer_id):
    customer = scoped_or_404(Customer, customer_id)
    start_raw = request.args.get("start_date")
    end_raw = request.args.get("end_date")
    start_date = datetime.strptime(start_raw, "%Y-%m-%d").date() if start_raw else date(date.today().year, 1, 1)
    end_date = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else date.today()

    opening_balance, rows, closing_balance = _customer_statement_data(customer, start_date, end_date)
    table_rows = [(start_date.isoformat(), "Opening Balance", "-", "", "", f"{opening_balance:,.2f}")]
    table_rows += [
        (r["date"].isoformat(), r["type"], r["doc_no"], f"{r['debit']:,.2f}" if r["debit"] else "",
         f"{r['credit']:,.2f}" if r["credit"] else "", f"{r['balance']:,.2f}")
        for r in rows
    ]
    buffer = rows_to_pdf(
        f"Statement — {customer.name}", f"{start_date.isoformat()} to {end_date.isoformat()}",
        ["Date", "Type", "Document #", "Debit", "Credit", "Balance"], table_rows,
        company=current_company(), numeric_cols={3, 4, 5},
    )
    return send_file(
        buffer, as_attachment=True, download_name=f"Statement-{customer.name}-{end_date.isoformat()}.pdf",
        mimetype="application/pdf",
    )


# ── Estimates (Quotes) ─────────────────────────────────────────────────

@sales_bp.route("/estimates")
@login_required
def estimate_list():
    estimates = scoped_query(Estimate).order_by(Estimate.estimate_date.desc(), Estimate.id.desc()).all()
    return render_template("sales/estimates.html", estimates=estimates)


@sales_bp.route("/estimates/new", methods=["GET", "POST"])
@login_required
def estimate_new():
    customers = scoped_query(Customer).filter_by(is_active=True).order_by(Customer.name).all()
    income_accounts = scoped_query(Account).filter_by(account_type="Income", is_active=True).order_by(Account.code).all()
    default_income = scoped_query(Account).filter_by(code=DEFAULT_INCOME_CODE).first()
    items = scoped_query(Item).filter_by(is_active=True).order_by(Item.sku).all()
    preselected_customer_id = request.args.get("customer_id", type=int)

    def render_form(form):
        return render_template(
            "sales/estimate_form.html", customers=customers, income_accounts=income_accounts,
            default_income=default_income, items=items, form=form, today=date.today().isoformat(),
        )

    if request.method == "POST":
        customer_id = request.form.get("customer_id")
        if not customer_id:
            flash("Select a customer.", "error")
            return render_form(request.form)

        estimate_date = datetime.strptime(request.form["estimate_date"], "%Y-%m-%d").date()
        expiry_raw = request.form.get("expiry_date", "").strip()
        expiry_date = datetime.strptime(expiry_raw, "%Y-%m-%d").date() if expiry_raw else None

        descriptions = request.form.getlist("description")
        quantities = request.form.getlist("quantity")
        unit_prices = request.form.getlist("unit_price")
        taxables = request.form.getlist("taxable")
        income_account_ids = request.form.getlist("income_account_id")
        item_ids = request.form.getlist("item_id")
        item_ids += [""] * (len(descriptions) - len(item_ids))

        estimate = Estimate(
            company_id=current_company_id(),
            estimate_no=next_estimate_no(),
            customer_id=int(customer_id),
            estimate_date=estimate_date,
            expiry_date=expiry_date,
            memo=request.form.get("memo", "").strip() or None,
            vat_rate=float(request.form.get("vat_rate") or 15.00),
        )

        for i, (desc, qty, price, acc_id, item_id_raw) in enumerate(
            zip(descriptions, quantities, unit_prices, income_account_ids, item_ids)
        ):
            if not desc.strip() or not qty or not price:
                continue
            item = scoped_get(Item, int(item_id_raw)) if item_id_raw else None
            estimate.lines.append(
                EstimateLine(
                    item=item,
                    description=desc.strip(),
                    quantity=float(qty),
                    unit_price=float(price),
                    taxable=str(i) in taxables,
                    income_account_id=item.income_account_id if item else int(acc_id),
                )
            )

        if not estimate.lines:
            flash("Add at least one line.", "error")
            return render_form(request.form)

        db.session.add(estimate)
        db.session.flush()
        log_audit("create", "estimate", estimate.id, f"Created estimate {estimate.estimate_no}", estimate.estimate_no)
        db.session.commit()
        flash(f"Estimate {estimate.estimate_no} created.", "success")
        return redirect(url_for("sales.estimate_detail", estimate_id=estimate.id))

    return render_form({"customer_id": preselected_customer_id} if preselected_customer_id else {})


@sales_bp.route("/estimates/<int:estimate_id>")
@login_required
def estimate_detail(estimate_id):
    estimate = scoped_or_404(Estimate, estimate_id)
    return render_template("sales/estimate_detail.html", estimate=estimate)


@sales_bp.route("/estimates/<int:estimate_id>/status", methods=["POST"])
@login_required
def estimate_status(estimate_id):
    estimate = scoped_or_404(Estimate, estimate_id)
    new_status = request.form["status"]
    if estimate.status == "converted":
        flash("This estimate was already converted to an invoice.", "error")
    elif new_status not in ("draft", "sent", "accepted", "declined"):
        flash("Invalid status.", "error")
    else:
        estimate.status = new_status
        db.session.commit()
        flash(f"Estimate marked as {new_status}.", "success")
    return redirect(url_for("sales.estimate_detail", estimate_id=estimate.id))


def _convert_estimate_to_invoice(estimate, due_date=None):
    """Shared by the staff-side Convert button (estimate_convert below) and the
    customer portal's e-signature flow (app/portal.py's estimate_sign) — same
    conversion either way, just triggered by a different person. Returns
    (invoice_or_None, error_message_or_None). Never commits; the caller decides
    when (so the portal route can set signature fields in the same transaction)."""
    invoice_date = date.today()
    invoice = Invoice(
        company_id=estimate.company_id,
        invoice_no=next_invoice_no(),
        customer_id=estimate.customer_id,
        invoice_date=invoice_date,
        due_date=due_date or invoice_date,
        memo=f"Converted from {estimate.estimate_no}" + (f" - {estimate.memo}" if estimate.memo else ""),
        vat_rate=estimate.vat_rate,
    )
    for line in estimate.lines:
        invoice.lines.append(
            InvoiceLine(
                item_id=line.item_id, description=line.description, quantity=line.quantity,
                unit_price=line.unit_price, taxable=line.taxable, income_account_id=line.income_account_id,
            )
        )

    # Same stock-availability guard as a normal invoice — a quote can sit around long enough
    # that stock sold to someone else in the meantime.
    required_qty = {}
    for line in invoice.lines:
        if line.item_id:
            item = Item.query.get(line.item_id)
            if item.is_tracked:
                required_qty[item] = required_qty.get(item, 0) + float(line.quantity)
    for item, needed in required_qty.items():
        if needed > float(item.quantity_on_hand):
            return None, (
                f"Can't convert: not enough stock for {item.name} ({item.sku}) — "
                f"have {item.quantity_on_hand} {item.unit}, need {needed}."
            )

    db.session.add(invoice)
    db.session.flush()
    post_invoice(invoice)
    estimate.status = "converted"
    estimate.converted_invoice_id = invoice.id
    return invoice, None


@sales_bp.route("/estimates/<int:estimate_id>/convert", methods=["POST"])
@login_required
def estimate_convert(estimate_id):
    estimate = scoped_or_404(Estimate, estimate_id)
    if estimate.status == "converted":
        flash("This estimate was already converted to an invoice.", "error")
        return redirect(url_for("sales.estimate_detail", estimate_id=estimate.id))
    if estimate.status == "declined":
        flash("Can't convert a declined estimate. Reopen it first if this was a mistake.", "error")
        return redirect(url_for("sales.estimate_detail", estimate_id=estimate.id))

    due_date_raw = request.form.get("due_date", "").strip()
    due_date = datetime.strptime(due_date_raw, "%Y-%m-%d").date() if due_date_raw else None

    invoice, error = _convert_estimate_to_invoice(estimate, due_date)
    if error:
        flash(error, "error")
        return redirect(url_for("sales.estimate_detail", estimate_id=estimate.id))

    log_audit(
        "convert", "estimate", estimate.id,
        f"Converted estimate {estimate.estimate_no} to invoice {invoice.invoice_no}", estimate.estimate_no,
    )
    db.session.commit()

    fiscalize_invoice_with_mra(invoice)
    db.session.commit()

    flash(f"Estimate {estimate.estimate_no} converted to invoice {invoice.invoice_no}.", "success")
    return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))


@sales_bp.route("/estimates/<int:estimate_id>/to-order", methods=["POST"])
@login_required
def estimate_to_order(estimate_id):
    """Turn an accepted quote into a confirmed Sales Order (not yet an invoice)."""
    estimate = scoped_or_404(Estimate, estimate_id)
    if estimate.status in ("converted", "declined"):
        flash(f"Can't create an order from a {estimate.status} estimate.", "error")
        return redirect(url_for("sales.estimate_detail", estimate_id=estimate.id))

    order = SalesOrder(
        company_id=estimate.company_id,
        order_no=next_order_no(),
        customer_id=estimate.customer_id,
        order_date=date.today(),
        memo=f"From {estimate.estimate_no}" + (f" - {estimate.memo}" if estimate.memo else ""),
        vat_rate=estimate.vat_rate,
        source_estimate_id=estimate.id,
    )
    for line in estimate.lines:
        order.lines.append(
            SalesOrderLine(
                item_id=line.item_id, description=line.description, quantity=line.quantity,
                unit_price=line.unit_price, taxable=line.taxable, income_account_id=line.income_account_id,
            )
        )
    if estimate.status != "accepted":
        estimate.status = "accepted"
    db.session.add(order)
    db.session.flush()
    log_audit("convert", "estimate", estimate.id, f"Created sales order {order.order_no} from {estimate.estimate_no}", estimate.estimate_no)
    db.session.commit()
    flash(f"Sales order {order.order_no} created from {estimate.estimate_no}.", "success")
    return redirect(url_for("sales.order_detail", order_id=order.id))


# ── Sales Orders (confirmed orders, not yet invoiced) ─────────────────

@sales_bp.route("/orders")
@login_required
def order_list():
    orders = scoped_query(SalesOrder).order_by(SalesOrder.order_date.desc(), SalesOrder.id.desc()).all()
    return render_template("sales/sales_orders.html", orders=orders)


@sales_bp.route("/orders/new", methods=["GET", "POST"])
@login_required
def order_new():
    customers = scoped_query(Customer).filter_by(is_active=True).order_by(Customer.name).all()
    income_accounts = scoped_query(Account).filter_by(account_type="Income", is_active=True).order_by(Account.code).all()
    default_income = scoped_query(Account).filter_by(code=DEFAULT_INCOME_CODE).first()
    items = scoped_query(Item).filter_by(is_active=True).order_by(Item.sku).all()
    preselected_customer_id = request.args.get("customer_id", type=int)

    def render_form(form):
        return render_template(
            "sales/sales_order_form.html", customers=customers, income_accounts=income_accounts,
            default_income=default_income, items=items, form=form, today=date.today().isoformat(),
        )

    if request.method == "POST":
        customer_id = request.form.get("customer_id")
        if not customer_id:
            flash("Select a customer.", "error")
            return render_form(request.form)

        order_date = datetime.strptime(request.form["order_date"], "%Y-%m-%d").date()
        expected_raw = request.form.get("expected_date", "").strip()
        expected_date = datetime.strptime(expected_raw, "%Y-%m-%d").date() if expected_raw else None

        descriptions = request.form.getlist("description")
        quantities = request.form.getlist("quantity")
        unit_prices = request.form.getlist("unit_price")
        taxables = request.form.getlist("taxable")
        income_account_ids = request.form.getlist("income_account_id")
        item_ids = request.form.getlist("item_id")
        item_ids += [""] * (len(descriptions) - len(item_ids))

        order = SalesOrder(
            company_id=current_company_id(),
            order_no=next_order_no(),
            customer_id=int(customer_id),
            order_date=order_date,
            expected_date=expected_date,
            customer_po_ref=request.form.get("customer_po_ref", "").strip() or None,
            memo=request.form.get("memo", "").strip() or None,
            vat_rate=float(request.form.get("vat_rate") or 15.00),
        )

        for i, (desc, qty, price, acc_id, item_id_raw) in enumerate(
            zip(descriptions, quantities, unit_prices, income_account_ids, item_ids)
        ):
            if not desc.strip() or not qty or not price:
                continue
            item = scoped_get(Item, int(item_id_raw)) if item_id_raw else None
            order.lines.append(
                SalesOrderLine(
                    item=item,
                    description=desc.strip(),
                    quantity=float(qty),
                    unit_price=float(price),
                    taxable=str(i) in taxables,
                    income_account_id=item.income_account_id if item else int(acc_id),
                )
            )

        if not order.lines:
            flash("Add at least one line.", "error")
            return render_form(request.form)

        db.session.add(order)
        db.session.flush()
        log_audit("create", "sales_order", order.id, f"Created sales order {order.order_no}", order.order_no)
        db.session.commit()
        flash(f"Sales order {order.order_no} created.", "success")
        return redirect(url_for("sales.order_detail", order_id=order.id))

    return render_form({"customer_id": preselected_customer_id} if preselected_customer_id else {})


@sales_bp.route("/orders/<int:order_id>")
@login_required
def order_detail(order_id):
    order = scoped_or_404(SalesOrder, order_id)
    return render_template("sales/sales_order_detail.html", order=order)


@sales_bp.route("/orders/<int:order_id>/status", methods=["POST"])
@login_required
def order_status(order_id):
    order = scoped_or_404(SalesOrder, order_id)
    new_status = request.form["status"]
    if order.status == "invoiced":
        flash("This order was already converted to an invoice.", "error")
    elif new_status not in ("open", "cancelled"):
        flash("Invalid status.", "error")
    else:
        order.status = new_status
        db.session.commit()
        flash(f"Sales order marked as {new_status}.", "success")
    return redirect(url_for("sales.order_detail", order_id=order.id))


@sales_bp.route("/orders/<int:order_id>/convert", methods=["POST"])
@login_required
def order_convert(order_id):
    order = scoped_or_404(SalesOrder, order_id)
    if order.status == "invoiced":
        flash("This order was already converted to an invoice.", "error")
        return redirect(url_for("sales.order_detail", order_id=order.id))
    if order.status == "cancelled":
        flash("Can't convert a cancelled order. Reopen it first if this was a mistake.", "error")
        return redirect(url_for("sales.order_detail", order_id=order.id))

    due_date_raw = request.form.get("due_date", "").strip()
    due_date = datetime.strptime(due_date_raw, "%Y-%m-%d").date() if due_date_raw else None

    invoice_date = date.today()
    invoice = Invoice(
        company_id=order.company_id,
        invoice_no=next_invoice_no(),
        customer_id=order.customer_id,
        invoice_date=invoice_date,
        due_date=due_date or invoice_date,
        memo=f"From order {order.order_no}" + (f" - {order.memo}" if order.memo else ""),
        vat_rate=order.vat_rate,
    )
    for line in order.lines:
        invoice.lines.append(
            InvoiceLine(
                item_id=line.item_id, description=line.description, quantity=line.quantity,
                unit_price=line.unit_price, taxable=line.taxable, income_account_id=line.income_account_id,
            )
        )

    # Same stock guard a normal invoice uses — the order may have sat long enough
    # that tracked stock was sold to someone else in the meantime.
    required_qty = {}
    for line in invoice.lines:
        if line.item_id:
            item = Item.query.get(line.item_id)
            if item.is_tracked:
                required_qty[item] = required_qty.get(item, 0) + float(line.quantity)
    for item, needed in required_qty.items():
        if needed > float(item.quantity_on_hand):
            flash(
                f"Can't convert: not enough stock for {item.name} ({item.sku}) — "
                f"have {item.quantity_on_hand} {item.unit}, need {needed}.", "error",
            )
            return redirect(url_for("sales.order_detail", order_id=order.id))

    db.session.add(invoice)
    db.session.flush()
    post_invoice(invoice)
    order.status = "invoiced"
    order.converted_invoice_id = invoice.id
    log_audit("convert", "sales_order", order.id, f"Converted order {order.order_no} to invoice {invoice.invoice_no}", order.order_no)
    db.session.commit()

    fiscalize_invoice_with_mra(invoice)
    db.session.commit()

    flash(f"Sales order {order.order_no} converted to invoice {invoice.invoice_no}.", "success")
    return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))


# ── Invoices ─────────────────────────────────────────────────────────

@sales_bp.route("/invoices")
@login_required
def invoice_list():
    invoices = scoped_query(Invoice).order_by(Invoice.invoice_date.desc(), Invoice.id.desc()).all()
    return render_template("sales/invoices.html", invoices=invoices)


@sales_bp.route("/invoices/new", methods=["GET", "POST"])
@login_required
def invoice_new():
    customers = scoped_query(Customer).filter_by(is_active=True).order_by(Customer.name).all()
    income_accounts = scoped_query(Account).filter_by(account_type="Income", is_active=True).order_by(Account.code).all()
    default_income = scoped_query(Account).filter_by(code=DEFAULT_INCOME_CODE).first()
    items = scoped_query(Item).filter_by(is_active=True).order_by(Item.sku).all()
    projects = scoped_query(Project).filter_by(is_active=True).order_by(Project.name).all()
    warehouses = scoped_query(Warehouse).filter_by(is_active=True).order_by(Warehouse.code).all()
    preselected_customer_id = request.args.get("customer_id", type=int)
    base_currency = current_company().base_currency

    # Unbilled, billable time entries, grouped by customer — same "JS swaps the list the
    # instant you pick a customer" pattern credit_memo_form.html uses for related invoices.
    unbilled_time_by_customer = {}
    for entry in scoped_query(TimeEntry).filter_by(is_billable=True, invoice_line_id=None).order_by(TimeEntry.entry_date).all():
        if not entry.customer_id:
            continue
        unbilled_time_by_customer.setdefault(entry.customer_id, []).append({
            "id": entry.id, "date": entry.entry_date.isoformat(), "description": entry.description,
            "hours": float(entry.hours), "rate": float(entry.hourly_rate), "amount": entry.amount,
        })

    def render_form(form):
        return render_template(
            "sales/invoice_form.html", customers=customers, income_accounts=income_accounts,
            default_income=default_income, items=items, projects=projects, warehouses=warehouses,
            form=form, today=date.today().isoformat(),
            currencies=CURRENCIES, base_currency=base_currency, payment_terms_options=PAYMENT_TERMS_OPTIONS,
            unbilled_time_by_customer=unbilled_time_by_customer,
        )

    if request.method == "POST":
        customer_id = request.form.get("customer_id")
        if not customer_id:
            flash("Select a customer.", "error")
            return render_form(request.form)

        invoice_date = datetime.strptime(request.form["invoice_date"], "%Y-%m-%d").date()
        due_date = datetime.strptime(request.form["due_date"], "%Y-%m-%d").date()

        currency = request.form.get("currency", base_currency) or base_currency
        exchange_rate = float(request.form.get("exchange_rate") or 1.0) if currency != base_currency else 1.0
        if exchange_rate <= 0:
            flash("Exchange rate must be greater than zero.", "error")
            return render_form(request.form)

        descriptions = request.form.getlist("description")
        quantities = request.form.getlist("quantity")
        unit_prices = request.form.getlist("unit_price")
        taxables = request.form.getlist("taxable")  # only present (as index string) for checked rows
        income_account_ids = request.form.getlist("income_account_id")
        item_ids = request.form.getlist("item_id")
        # Per-line VAT % override — blank means "use this invoice's own vat_rate
        # above", exactly like every line did before this field existed.
        line_vat_rates = request.form.getlist("line_vat_rate")
        line_warehouse_ids = request.form.getlist("line_warehouse_id")
        line_lot_numbers = request.form.getlist("line_lot_number")
        # zip() truncates to the shortest list — pad item_ids so a row missing that field
        # doesn't silently drop every line below it (the real form always sends it, but be defensive).
        item_ids += [""] * (len(descriptions) - len(item_ids))
        line_vat_rates += [""] * (len(descriptions) - len(line_vat_rates))
        line_warehouse_ids += [""] * (len(descriptions) - len(line_warehouse_ids))
        line_lot_numbers += [""] * (len(descriptions) - len(line_lot_numbers))

        invoice = Invoice(
            company_id=current_company_id(),
            invoice_no=next_invoice_no(),
            customer_id=int(customer_id),
            invoice_date=invoice_date,
            due_date=due_date,
            payment_terms=request.form.get("payment_terms", "Due on Receipt").strip() or "Due on Receipt",
            customer_po_number=request.form.get("customer_po_number", "").strip() or None,
            memo=request.form.get("memo", "").strip() or None,
            project_id=int(request.form["project_id"]) if request.form.get("project_id") else None,
            vat_rate=float(request.form.get("vat_rate") or 15.00),
            currency=currency,
            exchange_rate=exchange_rate,
        )

        for i, (desc, qty, price, acc_id, item_id_raw, vat_rate_raw, warehouse_id_raw, lot_number_raw) in enumerate(
            zip(descriptions, quantities, unit_prices, income_account_ids, item_ids, line_vat_rates, line_warehouse_ids, line_lot_numbers)
        ):
            if not desc.strip() or not qty or not price:
                continue
            item = scoped_get(Item, int(item_id_raw)) if item_id_raw else None
            invoice.lines.append(
                InvoiceLine(
                    item=item,  # assigning the relationship (not just item_id) keeps .item populated pre-flush
                    description=desc.strip(),
                    quantity=float(qty),
                    unit_price=float(price),
                    taxable=str(i) in taxables,
                    vat_rate=float(vat_rate_raw) if vat_rate_raw.strip() != "" else None,
                    warehouse_id=int(warehouse_id_raw) if warehouse_id_raw else None,
                    lot_number=lot_number_raw.strip() or None,
                    income_account_id=item.income_account_id if item else int(acc_id),
                )
            )

        # Billable time entries selected via the "Unbilled Time" checklist — each becomes
        # its own invoice line (hours as quantity, hourly rate as unit price). Re-validated
        # here rather than trusted from the form: must still belong to this customer, still
        # be billable, and not already billed on some other invoice created in the meantime.
        time_entry_line_pairs = []
        for time_entry_id_raw in request.form.getlist("billed_time_entry_id"):
            time_entry = scoped_get(TimeEntry, int(time_entry_id_raw))
            if not time_entry or time_entry.customer_id != int(customer_id):
                continue
            if not time_entry.is_billable or time_entry.is_invoiced:
                continue
            new_line = InvoiceLine(
                description=time_entry.description, quantity=time_entry.hours,
                unit_price=time_entry.hourly_rate, taxable=True,
                income_account_id=default_income.id if default_income else get_account_or_400(DEFAULT_INCOME_CODE, "Sales Revenue").id,
            )
            invoice.lines.append(new_line)
            time_entry_line_pairs.append((time_entry, new_line))

        if not invoice.lines:
            flash("Add at least one invoice line.", "error")
            return render_form(request.form)

        # Check stock availability across all tracked-item lines before posting anything.
        # Keyed by the Item object itself, not line.item_id — that FK column isn't populated
        # in memory until the line is flushed, which hasn't happened yet at this point.
        required_qty = {}
        for line in invoice.lines:
            if line.item and line.item.is_tracked:
                required_qty[line.item] = required_qty.get(line.item, 0) + float(line.quantity)
        for item, needed in required_qty.items():
            if needed > float(item.quantity_on_hand):
                flash(
                    f"Not enough stock for {item.name} ({item.sku}): have {item.quantity_on_hand} {item.unit}, "
                    f"invoice needs {needed}.", "error",
                )
                return render_form(request.form)

        db.session.add(invoice)
        db.session.flush()  # assign invoice.id before post_invoice needs it for StockMovement.reference_id

        for time_entry, new_line in time_entry_line_pairs:
            time_entry.invoice_line_id = new_line.id  # marks it billed — never offered on another invoice again;
            # if this invoice ends up rejected below, invoice_reject() unlinks these again.

        settings = current_company()
        threshold = float(settings.invoice_approval_threshold) if settings.invoice_approval_threshold is not None else 0.0
        needs_approval = settings.invoice_approval_enabled and invoice.total_base >= threshold
        if needs_approval:
            invoice.status = "pending_approval"
            invoice.submitted_by = current_user.id
            log_audit("submit", "invoice", invoice.id, f"Submitted invoice {invoice.invoice_no} for approval (total {invoice.total:.2f})", invoice.invoice_no)
            db.session.commit()
            flash(f"Invoice {invoice.invoice_no} submitted for approval — it won't post to the ledger until an owner approves it.", "success")
            return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))

        post_invoice(invoice)
        log_audit("create", "invoice", invoice.id, f"Created invoice {invoice.invoice_no} (total {invoice.total:.2f})", invoice.invoice_no)
        db.session.commit()

        # Fiscalisation is a best-effort follow-up — never blocks or rolls back the
        # ledger posting above, even if MRA_TaxInvoice_System is unreachable.
        fiscalize_invoice_with_mra(invoice)
        db.session.commit()

        fire_webhook(current_company_id(), "invoice.created", {
            "id": invoice.id, "invoice_no": invoice.invoice_no, "customer": invoice.customer.name,
            "total": invoice.total, "currency": invoice.currency, "due_date": invoice.due_date.isoformat(),
        })

        flash(f"Invoice {invoice.invoice_no} created and posted to the ledger.", "success")
        return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))

    return render_form({"customer_id": preselected_customer_id} if preselected_customer_id else {})


def post_invoice(invoice):
    """Builds the double-entry journal entry for an invoice: Dr AR, Cr Income (per line), Cr VAT Payable.

    Everything posts in the company's base currency, not the invoice's own currency —
    for a base-currency invoice (exchange_rate=1) that's the same number either way; for
    a foreign invoice, each amount is converted at the invoice's booked exchange_rate
    (invoice.total_base etc). Any sub-cent rounding from converting several lines
    independently is folded into the VAT line (or the last income line, if there's no
    VAT) so the entry always balances exactly, to the cent, in base currency.

    Tracked-item lines additionally issue stock at the item's moving-average cost and add
    Dr COGS / Cr Inventory lines to the same entry, so the whole invoice posts as one balanced
    transaction (two balanced sub-pairs summed together still balance overall).
    """
    ar_account = get_account_or_400(AR_ACCOUNT_CODE, "Accounts Receivable")
    rate = float(invoice.exchange_rate)

    entry = JournalEntry(
        company_id=current_company_id(),
        entry_date=invoice.invoice_date,
        reference_no=invoice.invoice_no,
        memo=f"Invoice {invoice.invoice_no} - {invoice.memo or ''}".strip(" -")
             + (f" ({invoice.currency} {invoice.total:.2f} @ {rate})" if invoice.is_foreign else ""),
        source_type="invoice",
        project_id=invoice.project_id,
        created_by=current_user.id if current_user.is_authenticated else None,
    )
    entry.lines.append(JournalLine(account=ar_account, debit=invoice.total_base, credit=0, memo=invoice.invoice_no))

    income_totals = {}
    for line in invoice.lines:
        income_totals[line.income_account_id] = income_totals.get(line.income_account_id, 0) + line.amount
    income_totals_base = {account_id: round(amount * rate, 2) for account_id, amount in income_totals.items()}
    vat_base = round(invoice.vat_amount * rate, 2) if invoice.vat_amount else 0

    # Converting each piece independently can leave the whole entry a cent or two off
    # invoice.total_base (rounding several numbers separately vs. rounding their sum) —
    # fold that remainder into the VAT line (or the last income line) so it never fails to balance.
    remainder = round(invoice.total_base - sum(income_totals_base.values()) - vat_base, 2)
    if remainder and vat_base:
        vat_base = round(vat_base + remainder, 2)
    elif remainder and income_totals_base:
        last_account_id = list(income_totals_base)[-1]
        income_totals_base[last_account_id] = round(income_totals_base[last_account_id] + remainder, 2)

    for account_id, amount in income_totals_base.items():
        entry.lines.append(JournalLine(account_id=account_id, debit=0, credit=amount, memo=invoice.invoice_no))

    if vat_base:
        vat_account = get_account_or_400(VAT_PAYABLE_CODE, "VAT Payable")
        entry.lines.append(JournalLine(account=vat_account, debit=0, credit=vat_base, memo=invoice.invoice_no))

    # COGS: issue stock at today's average cost, per tracked item line.
    cogs_totals = {}
    inventory_totals = {}
    for line in invoice.lines:
        if not line.item_id or not line.item.is_tracked:
            continue
        item = line.item
        unit_cost = item.issue_stock(line.quantity)  # mutates item.quantity_on_hand, raises if oversold
        cogs_amount = float(line.quantity) * unit_cost
        cogs_totals[item.cogs_account_id] = cogs_totals.get(item.cogs_account_id, 0) + cogs_amount
        inventory_totals[item.inventory_account_id] = inventory_totals.get(item.inventory_account_id, 0) + cogs_amount
        db.session.add(StockMovement(
            item_id=item.id, movement_date=invoice.invoice_date, movement_type="sale",
            quantity=-float(line.quantity), unit_cost=unit_cost,
            reference_type="invoice", reference_id=invoice.id,
            running_quantity=item.quantity_on_hand, running_avg_cost=item.cost_price,
            memo=f"Sold via {invoice.invoice_no}",
            warehouse_id=line.warehouse_id, lot_number=line.lot_number,
        ))

    for account_id, amount in cogs_totals.items():
        entry.lines.append(JournalLine(account_id=account_id, debit=amount, credit=0, memo=f"COGS - {invoice.invoice_no}"))
    for account_id, amount in inventory_totals.items():
        entry.lines.append(JournalLine(account_id=account_id, debit=0, credit=amount, memo=f"COGS - {invoice.invoice_no}"))

    db.session.add(entry)
    db.session.flush()  # get entry.id without a full commit
    invoice.journal_entry_id = entry.id


@sales_bp.route("/invoices/approvals")
@login_required
@owner_required
def invoice_approval_list():
    pending = (
        scoped_query(Invoice).filter_by(status="pending_approval")
        .order_by(Invoice.invoice_date.desc()).all()
    )
    return render_template("sales/invoice_approval_list.html", invoices=pending)


@sales_bp.route("/invoices/<int:invoice_id>/approve", methods=["POST"])
@login_required
@owner_required
def invoice_approve(invoice_id):
    invoice = scoped_or_404(Invoice, invoice_id)
    if invoice.status != "pending_approval":
        flash("This invoice isn't awaiting approval.", "error")
        return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))

    post_invoice(invoice)
    invoice.status = "open"
    invoice.approved_by = current_user.id
    invoice.approved_at = datetime.utcnow()
    log_audit("approve", "invoice", invoice.id, f"Approved and posted invoice {invoice.invoice_no} (total {invoice.total:.2f})", invoice.invoice_no)
    db.session.commit()

    fiscalize_invoice_with_mra(invoice)
    db.session.commit()

    fire_webhook(current_company_id(), "invoice.created", {
        "id": invoice.id, "invoice_no": invoice.invoice_no, "customer": invoice.customer.name,
        "total": invoice.total, "currency": invoice.currency, "due_date": invoice.due_date.isoformat(),
    })
    flash(f"Invoice {invoice.invoice_no} approved and posted to the ledger.", "success")
    return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))


@sales_bp.route("/invoices/<int:invoice_id>/reject", methods=["POST"])
@login_required
@owner_required
def invoice_reject(invoice_id):
    invoice = scoped_or_404(Invoice, invoice_id)
    if invoice.status != "pending_approval":
        flash("This invoice isn't awaiting approval.", "error")
        return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))

    # Never posted anything to the ledger or touched stock while pending, so rejecting
    # is just marking it void — there's nothing to reverse, unlike invoice_void below.
    # Time entries pulled onto this invoice DO need unlinking though — they were marked
    # billed at creation time (see invoice_new), so a rejection must free them again.
    line_ids = [line.id for line in invoice.lines]
    if line_ids:
        TimeEntry.query.filter(TimeEntry.invoice_line_id.in_(line_ids)).update(
            {TimeEntry.invoice_line_id: None}, synchronize_session=False
        )

    invoice.status = "void"
    invoice.approved_by = current_user.id
    invoice.approved_at = datetime.utcnow()
    invoice.approval_note = request.form.get("note", "").strip() or None
    log_audit("reject", "invoice", invoice.id, f"Rejected invoice {invoice.invoice_no}" + (f": {invoice.approval_note}" if invoice.approval_note else ""), invoice.invoice_no)
    db.session.commit()
    flash(f"Invoice {invoice.invoice_no} rejected.", "success")
    return redirect(url_for("sales.invoice_approval_list"))


def _invoice_not_editable_reason(invoice):
    """None if the invoice can be amended in place; otherwise a user-facing reason
    it can't be, mirroring the same guards invoice_void already enforces."""
    if invoice.status == "void":
        return "it has been voided"
    if invoice.status == "pending_approval":
        return "it's awaiting approval — reject it and create a new one instead of editing"
    if invoice.amount_paid > 0:
        return "it already has payments or credits applied — unapply them first"
    if invoice.mra_invoice_number:
        return (
            "it has already been fiscalised with the MRA — a fiscalised invoice can only be "
            "reversed with a credit note (Void), never altered, then reissued as a new invoice"
        )
    return None


@sales_bp.route("/invoices/<int:invoice_id>/edit", methods=["GET", "POST"])
@login_required
def invoice_edit(invoice_id):
    invoice = scoped_or_404(Invoice, invoice_id)
    not_editable_reason = _invoice_not_editable_reason(invoice)
    if not_editable_reason:
        flash(f"Invoice {invoice.invoice_no} can't be edited because {not_editable_reason}.", "error")
        return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))

    customers = scoped_query(Customer).filter_by(is_active=True).order_by(Customer.name).all()
    income_accounts = scoped_query(Account).filter_by(account_type="Income", is_active=True).order_by(Account.code).all()
    default_income = scoped_query(Account).filter_by(code=DEFAULT_INCOME_CODE).first()
    items = scoped_query(Item).filter_by(is_active=True).order_by(Item.sku).all()
    projects = scoped_query(Project).filter_by(is_active=True).order_by(Project.name).all()
    warehouses = scoped_query(Warehouse).filter_by(is_active=True).order_by(Warehouse.code).all()
    base_currency = current_company().base_currency

    def render_form(form):
        return render_template(
            "sales/invoice_form.html", customers=customers, income_accounts=income_accounts,
            default_income=default_income, items=items, projects=projects, warehouses=warehouses,
            form=form, today=date.today().isoformat(),
            currencies=CURRENCIES, base_currency=base_currency, payment_terms_options=PAYMENT_TERMS_OPTIONS,
            editing=True, invoice=invoice,
        )

    if request.method == "POST":
        customer_id = request.form.get("customer_id")
        if not customer_id:
            flash("Select a customer.", "error")
            return render_form(request.form)

        invoice_date = datetime.strptime(request.form["invoice_date"], "%Y-%m-%d").date()
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
        income_account_ids = request.form.getlist("income_account_id")
        item_ids = request.form.getlist("item_id")
        line_vat_rates = request.form.getlist("line_vat_rate")
        line_warehouse_ids = request.form.getlist("line_warehouse_id")
        line_lot_numbers = request.form.getlist("line_lot_number")
        item_ids += [""] * (len(descriptions) - len(item_ids))
        line_vat_rates += [""] * (len(descriptions) - len(line_vat_rates))
        line_warehouse_ids += [""] * (len(descriptions) - len(line_warehouse_ids))
        line_lot_numbers += [""] * (len(descriptions) - len(line_lot_numbers))

        new_lines = []
        for i, (desc, qty, price, acc_id, item_id_raw, vat_rate_raw, warehouse_id_raw, lot_number_raw) in enumerate(
            zip(descriptions, quantities, unit_prices, income_account_ids, item_ids, line_vat_rates, line_warehouse_ids, line_lot_numbers)
        ):
            if not desc.strip() or not qty or not price:
                continue
            item = scoped_get(Item, int(item_id_raw)) if item_id_raw else None
            new_lines.append(
                InvoiceLine(
                    item=item,
                    description=desc.strip(),
                    quantity=float(qty),
                    unit_price=float(price),
                    taxable=str(i) in taxables,
                    vat_rate=float(vat_rate_raw) if vat_rate_raw.strip() != "" else None,
                    income_account_id=item.income_account_id if item else int(acc_id),
                    warehouse_id=int(warehouse_id_raw) if warehouse_id_raw else None,
                    lot_number=lot_number_raw.strip() or None,
                )
            )

        if not new_lines:
            flash("Add at least one invoice line.", "error")
            return render_form(request.form)

        # Return stock issued by the invoice's CURRENT (pre-edit) tracked lines first, so
        # availability for the new lines is checked against the right baseline — otherwise
        # a line that isn't even changing would look like it's competing with itself.
        for old_line in invoice.lines:
            if old_line.item_id and old_line.item.is_tracked:
                item = old_line.item
                item.quantity_on_hand = float(item.quantity_on_hand) + float(old_line.quantity)
                db.session.add(StockMovement(
                    item_id=item.id, movement_date=date.today(), movement_type="sale",
                    quantity=float(old_line.quantity), unit_cost=item.cost_price,
                    reference_type="invoice", reference_id=invoice.id,
                    running_quantity=item.quantity_on_hand, running_avg_cost=item.cost_price,
                    memo=f"Reversal - {invoice.invoice_no} amended",
                ))

        required_qty = {}
        for line in new_lines:
            if line.item and line.item.is_tracked:
                required_qty[line.item] = required_qty.get(line.item, 0) + float(line.quantity)
        for item, needed in required_qty.items():
            if needed > float(item.quantity_on_hand):
                db.session.rollback()
                flash(
                    f"Not enough stock for {item.name} ({item.sku}): have {item.quantity_on_hand} {item.unit}, "
                    f"invoice needs {needed}.", "error",
                )
                return render_form(request.form)

        if invoice.journal_entry_id:
            old_entry = JournalEntry.query.get(invoice.journal_entry_id)
            if old_entry:
                db.session.delete(old_entry)
        invoice.journal_entry_id = None
        invoice.lines = new_lines

        invoice.customer_id = int(customer_id)
        invoice.invoice_date = invoice_date
        invoice.due_date = due_date
        invoice.payment_terms = request.form.get("payment_terms", "Due on Receipt").strip() or "Due on Receipt"
        invoice.customer_po_number = request.form.get("customer_po_number", "").strip() or None
        invoice.memo = request.form.get("memo", "").strip() or None
        invoice.project_id = int(request.form["project_id"]) if request.form.get("project_id") else None
        invoice.vat_rate = float(request.form.get("vat_rate") or 15.00)
        invoice.currency = currency
        invoice.exchange_rate = exchange_rate

        db.session.flush()
        post_invoice(invoice)
        log_audit("edit", "invoice", invoice.id, f"Amended invoice {invoice.invoice_no} (total {invoice.total:.2f})", invoice.invoice_no)
        db.session.commit()

        # Same best-effort fiscalisation as a brand-new invoice — this is the first time
        # THESE contents are being fiscalised, since editing was only allowed pre-fiscalisation.
        fiscalize_invoice_with_mra(invoice)
        db.session.commit()

        flash(f"Invoice {invoice.invoice_no} updated and re-posted to the ledger.", "success")
        return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))

    form = {
        "customer_id": invoice.customer_id, "invoice_date": invoice.invoice_date.isoformat(),
        "due_date": invoice.due_date.isoformat(), "payment_terms": invoice.payment_terms,
        "customer_po_number": invoice.customer_po_number,
        "vat_rate": float(invoice.vat_rate), "currency": invoice.currency,
        "exchange_rate": float(invoice.exchange_rate), "memo": invoice.memo, "project_id": invoice.project_id,
        "lines": [
            {
                "item_id": line.item_id, "description": line.description, "quantity": float(line.quantity),
                "unit_price": float(line.unit_price), "income_account_id": line.income_account_id,
                "taxable": line.taxable, "line_vat_rate": float(line.vat_rate) if line.vat_rate is not None else None,
                "warehouse_id": line.warehouse_id, "lot_number": line.lot_number,
            }
            for line in invoice.lines
        ],
    }
    return render_form(form)


@sales_bp.route("/invoices/<int:invoice_id>")
@login_required
def invoice_detail(invoice_id):
    invoice = scoped_or_404(Invoice, invoice_id)
    share_link = share_url("invoice", invoice.id, invoice.company_id, "share.invoice_pdf")
    whatsapp_message = (
        f"Hi {invoice.customer.name}, here's Invoice {invoice.invoice_no} from {current_company().business_name} — "
        f"{invoice.currency} {invoice.total:.2f}, due {invoice.due_date.strftime('%d %b %Y')}. "
        f"View/download: {share_link}"
    )
    company = current_company()
    return render_template(
        "sales/invoice_detail.html", invoice=invoice,
        whatsapp_href=whatsapp_link(invoice.customer.phone, whatsapp_message),
        not_editable_reason=_invoice_not_editable_reason(invoice),
        smtp_ready=bool(company and company.smtp_host and company.smtp_from and company.smtp_port),
    )


@sales_bp.route("/invoices/<int:invoice_id>/email", methods=["POST"])
@login_required
def invoice_email(invoice_id):
    invoice = scoped_or_404(Invoice, invoice_id)
    company = current_company()
    if not (company and company.smtp_host and company.smtp_from and company.smtp_port):
        flash("SMTP isn't configured yet — set host, port and from-address in Settings → Company.", "error")
        return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))
    if not invoice.customer.email:
        flash(f"{invoice.customer.name} has no email address on file.", "error")
        return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))

    pdf_buffer = generate_invoice_pdf(invoice, company)

    msg = EmailMessage()
    msg["Subject"] = f"Invoice {invoice.invoice_no} from {company.business_name}"
    msg["From"] = company.smtp_from
    msg["To"] = invoice.customer.email
    msg.set_content(
        f"Hi {invoice.customer.name},\n\n"
        f"Please find attached Invoice {invoice.invoice_no} from {company.business_name} — "
        f"{invoice.currency} {invoice.total:.2f}, {invoice.payment_terms} (due {invoice.due_date.strftime('%d %b %Y')}).\n\n"
        f"Thanks,\n{company.business_name}"
    )
    msg.add_attachment(
        pdf_buffer.read(), maintype="application", subtype="pdf", filename=f"{invoice.invoice_no}.pdf",
    )

    try:
        with smtplib.SMTP(company.smtp_host, company.smtp_port, timeout=15) as server:
            if company.smtp_use_tls:
                server.starttls()
            if company.smtp_username:
                server.login(company.smtp_username, company.smtp_password or "")
            server.send_message(msg)
    except Exception as exc:  # broad: any SMTP/network error should reach the user, not the log
        flash(f"Could not email invoice to {invoice.customer.name}: {exc}", "error")
        return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))

    log_audit("send", "invoice", invoice.id, f"Emailed invoice {invoice.invoice_no} to {invoice.customer.email}", invoice.invoice_no)
    flash(f"Invoice {invoice.invoice_no} emailed to {invoice.customer.name} ({invoice.customer.email}).", "success")
    return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))


@sales_bp.route("/invoices/<int:invoice_id>/quick-pay", methods=["POST"])
@login_required
def invoice_quick_pay(invoice_id):
    """One-click 'Mark paid' — records a full-balance Payment against this invoice,
    posts the matching journal entry, and marks the invoice paid.  Deposit goes to
    the first Cash / Bank asset account (or Undeposited Funds if none is active)."""
    invoice = scoped_or_404(Invoice, invoice_id)
    if invoice.status not in ("open", "partial") or invoice.balance_due <= 0:
        flash(f"Invoice {invoice.invoice_no} is already settled.", "warning")
        return redirect(url_for("sales.invoice_list"))

    # Pick the first active bank/cash account; fall back to Undeposited Funds.
    deposit_account = scoped_query(Account).filter(
        Account.account_type == "Asset", Account.is_active == True,  # noqa: E712
        Account.subtype == "Cash and Cash Equivalents",
    ).order_by(Account.code).first()
    if deposit_account is None:
        deposit_account = get_account_or_400(UNDEPOSITED_FUNDS_CODE, "Undeposited Funds")

    amount = round(invoice.balance_due, 2)
    payment = Payment(
        company_id=current_company_id(),
        customer_id=invoice.customer_id,
        payment_date=date.today(),
        amount=amount,
        currency=invoice.currency,
        exchange_rate=invoice.exchange_rate,
        method="bank",
        deposit_account_id=deposit_account.id,
        reference_no=None,
        memo=f"Quick-pay for {invoice.invoice_no}",
    )
    payment.applications.append(PaymentApplication(invoice=invoice, amount_applied=amount))

    post_payment(payment)
    db.session.add(payment)
    db.session.flush()

    invoice.status = "paid" if invoice.balance_due <= 0 else "partial"
    log_audit("create", "payment", payment.id,
              f"Quick-pay recorded for {invoice.invoice_no}: {amount:.2f} {invoice.currency}")
    db.session.commit()
    flash(
        f"Recorded payment of {amount:.2f} {invoice.currency} for {invoice.invoice_no}.",
        "success",
    )
    return redirect(url_for("sales.invoice_list"))



@sales_bp.route("/invoices/<int:invoice_id>/pdf")
@login_required
def invoice_pdf(invoice_id):
    invoice = scoped_or_404(Invoice, invoice_id)
    company = current_company()
    template = request.args.get("template")  # optional override, e.g. ?template=coral
    buffer = generate_invoice_pdf(invoice, company, template=template)
    return send_file(
        buffer, mimetype="application/pdf", as_attachment=False,
        download_name=f"{invoice.invoice_no}.pdf",
    )


@sales_bp.route("/invoices/<int:invoice_id>/void", methods=["POST"])
@login_required
def invoice_void(invoice_id):
    invoice = scoped_or_404(Invoice, invoice_id)
    if invoice.status == "pending_approval":
        flash("This invoice is still awaiting approval — reject it instead of voiding (it was never posted).", "error")
        return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))
    if invoice.amount_paid > 0:
        flash("Cannot void an invoice that already has payments applied. Unapply payments first.", "error")
        return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))

    for line in invoice.lines:
        if line.item_id and line.item.is_tracked:
            item = line.item
            if item.costing_method in ("fifo", "lifo"):
                # Restores at the item's current average cost rather than the original
                # sale's exact layer cost — this app doesn't track which layer(s) a
                # specific sale consumed, the same simplification a weighted-average
                # item already accepts here (below).
                item.add_stock_layer(float(line.quantity), float(item.cost_price))
                item.refresh_average_cost()
            item.quantity_on_hand = float(item.quantity_on_hand) + float(line.quantity)  # cost_price left as-is
            db.session.add(StockMovement(
                item_id=item.id, movement_date=date.today(), movement_type="sale",
                quantity=float(line.quantity), unit_cost=item.cost_price,
                reference_type="invoice", reference_id=invoice.id,
                running_quantity=item.quantity_on_hand, running_avg_cost=item.cost_price,
                memo=f"Reversal - {invoice.invoice_no} voided",
            ))

    if invoice.journal_entry_id:
        entry = JournalEntry.query.get(invoice.journal_entry_id)
        if entry:
            db.session.delete(entry)
    invoice.status = "void"
    invoice.journal_entry_id = None
    log_audit("void", "invoice", invoice.id, f"Voided invoice {invoice.invoice_no}", invoice.invoice_no)
    db.session.commit()

    # If this invoice was fiscalised, cancel it out on the MRA side too — a fiscalised
    # invoice can't be deleted or altered there, only offset by a credit note.
    void_invoice_via_credit_note(invoice)
    db.session.commit()

    flash(f"Invoice {invoice.invoice_no} voided.", "success")
    return redirect(url_for("sales.invoice_list"))


# ── Recurring Invoices ───────────────────────────────────────────────
# Templates that generate a real Invoice on a schedule. Generation (manual "Run now",
# or the bulk "Generate Due Invoices" button) always goes through post_invoice() and
# fiscalize_invoice_with_mra(), so a recurring invoice is indistinguishable from a
# hand-entered one once it lands in the ledger.

def _build_invoice_from_template(template):
    """Creates and posts one real Invoice from a RecurringInvoice template, dated today.
    Does not commit — caller commits once, after any bookkeeping (advancing next_run_date etc).
    """
    invoice_date = date.today()
    due_date = date.fromordinal(invoice_date.toordinal() + template.due_days)
    invoice = Invoice(
        company_id=current_company_id(),
        invoice_no=next_invoice_no(),
        customer_id=template.customer_id,
        invoice_date=invoice_date,
        due_date=due_date,
        memo=f"[Recurring: {template.name}]" + (f" - {template.memo}" if template.memo else ""),
        vat_rate=template.vat_rate,
    )
    for line in template.lines:
        invoice.lines.append(
            InvoiceLine(
                item_id=line.item_id, description=line.description, quantity=line.quantity,
                unit_price=line.unit_price, taxable=line.taxable, income_account_id=line.income_account_id,
            )
        )

    required_qty = {}
    for line in invoice.lines:
        if line.item_id:
            item = scoped_get(Item, line.item_id)
            if item.is_tracked:
                required_qty[item] = required_qty.get(item, 0) + float(line.quantity)
    for item, needed in required_qty.items():
        if needed > float(item.quantity_on_hand):
            raise ValueError(
                f"Not enough stock for {item.name} ({item.sku}) on template '{template.name}': "
                f"have {item.quantity_on_hand} {item.unit}, need {needed}."
            )

    db.session.add(invoice)
    db.session.flush()
    post_invoice(invoice)
    return invoice


@sales_bp.route("/recurring")
@login_required
def recurring_list():
    templates = scoped_query(RecurringInvoice).order_by(RecurringInvoice.next_run_date).all()
    due_count = sum(1 for t in templates if t.is_due)
    return render_template("sales/recurring_list.html", templates=templates, due_count=due_count, today=date.today())


@sales_bp.route("/recurring/new", methods=["GET", "POST"])
@login_required
def recurring_new():
    customers = scoped_query(Customer).filter_by(is_active=True).order_by(Customer.name).all()
    income_accounts = scoped_query(Account).filter_by(account_type="Income", is_active=True).order_by(Account.code).all()
    default_income = scoped_query(Account).filter_by(code=DEFAULT_INCOME_CODE).first()
    items = scoped_query(Item).filter_by(is_active=True).order_by(Item.sku).all()

    def render_form(form):
        return render_template(
            "sales/recurring_form.html", customers=customers, income_accounts=income_accounts,
            default_income=default_income, items=items, form=form, today=date.today().isoformat(),
            frequencies=RECURRING_FREQUENCIES,
        )

    if request.method == "POST":
        customer_id = request.form.get("customer_id")
        name = request.form.get("name", "").strip()
        if not customer_id or not name:
            flash("Name and customer are both required.", "error")
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
        income_account_ids = request.form.getlist("income_account_id")
        item_ids = request.form.getlist("item_id")
        item_ids += [""] * (len(descriptions) - len(item_ids))

        template = RecurringInvoice(
            company_id=current_company_id(),
            name=name,
            customer_id=int(customer_id),
            memo=request.form.get("memo", "").strip() or None,
            vat_rate=float(request.form.get("vat_rate") or 15.00),
            frequency=frequency,
            due_days=int(request.form.get("due_days") or 30),
            start_date=start_date,
            next_run_date=start_date,
            end_date=end_date,
        )

        for i, (desc, qty, price, acc_id, item_id_raw) in enumerate(
            zip(descriptions, quantities, unit_prices, income_account_ids, item_ids)
        ):
            if not desc.strip() or not qty or not price:
                continue
            item = scoped_get(Item, int(item_id_raw)) if item_id_raw else None
            template.lines.append(
                RecurringInvoiceLine(
                    item=item,
                    description=desc.strip(),
                    quantity=float(qty),
                    unit_price=float(price),
                    taxable=str(i) in taxables,
                    income_account_id=item.income_account_id if item else int(acc_id),
                )
            )

        if not template.lines:
            flash("Add at least one line.", "error")
            return render_form(request.form)

        db.session.add(template)
        db.session.flush()
        log_audit("create", "recurring_invoice", template.id, f"Created recurring invoice template '{template.name}'", template.name)
        db.session.commit()
        flash(f"Recurring invoice template '{template.name}' created — first invoice due {template.next_run_date}.", "success")
        return redirect(url_for("sales.recurring_detail", template_id=template.id))

    return render_form({})


@sales_bp.route("/recurring/<int:template_id>")
@login_required
def recurring_detail(template_id):
    template = scoped_or_404(RecurringInvoice, template_id)
    generated = (
        scoped_query(Invoice)
        .filter(Invoice.memo.like(f"[Recurring: {template.name}]%"))
        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
        .all()
    )
    return render_template("sales/recurring_detail.html", template=template, generated=generated)


@sales_bp.route("/recurring/<int:template_id>/toggle", methods=["POST"])
@login_required
def recurring_toggle(template_id):
    template = scoped_or_404(RecurringInvoice, template_id)
    if not template.is_active and template.is_ended:
        flash("This template's end date has passed — extend the end date before reactivating it.", "error")
        return redirect(url_for("sales.recurring_detail", template_id=template.id))
    template.is_active = not template.is_active
    log_audit(
        "update", "recurring_invoice", template.id,
        f"{'Resumed' if template.is_active else 'Paused'} recurring invoice '{template.name}'", template.name,
    )
    db.session.commit()
    flash(f"'{template.name}' {'resumed' if template.is_active else 'paused'}.", "success")
    return redirect(url_for("sales.recurring_detail", template_id=template.id))


@sales_bp.route("/recurring/<int:template_id>/delete", methods=["POST"])
@login_required
def recurring_delete(template_id):
    template = scoped_or_404(RecurringInvoice, template_id)
    if template.invoices_generated > 0:
        flash("Can't delete a template that has already generated invoices — pause it instead.", "error")
        return redirect(url_for("sales.recurring_detail", template_id=template.id))
    name = template.name
    db.session.delete(template)
    log_audit("delete", "recurring_invoice", template_id, f"Deleted unused recurring invoice template '{name}'", name)
    db.session.commit()
    flash(f"Template '{name}' deleted.", "success")
    return redirect(url_for("sales.recurring_list"))


@sales_bp.route("/recurring/<int:template_id>/generate", methods=["POST"])
@login_required
def recurring_generate_now(template_id):
    """Generates one invoice from this template immediately, regardless of next_run_date,
    then advances the schedule by one occurrence — same as if it had come due today."""
    template = scoped_or_404(RecurringInvoice, template_id)
    try:
        invoice = _build_invoice_from_template(template)
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("sales.recurring_detail", template_id=template.id))

    template.advance_next_run_date()
    template.last_generated_at = datetime.utcnow()
    template.invoices_generated += 1
    log_audit(
        "generate", "recurring_invoice", template.id,
        f"Generated invoice {invoice.invoice_no} from template '{template.name}'", template.name,
    )
    db.session.commit()

    fiscalize_invoice_with_mra(invoice)
    db.session.commit()

    flash(f"Invoice {invoice.invoice_no} generated from '{template.name}'.", "success")
    return redirect(url_for("sales.invoice_detail", invoice_id=invoice.id))


@sales_bp.route("/recurring/run-due", methods=["POST"])
@login_required
def recurring_run_due():
    """Bulk action: generates one invoice for every active template in the current company
    whose next_run_date has arrived. Same underlying logic the nightly scheduler (app/scheduler.py)
    runs automatically across every company — this button exists so a due invoice can be produced
    on demand, without waiting for the next scheduled pass."""
    generated_numbers, skipped = generate_due_invoices_for_current_company(source="manual")
    if not generated_numbers and not skipped:
        flash("No recurring invoices are due right now.", "success")
    if generated_numbers:
        flash(f"Generated {len(generated_numbers)} invoice(s): {', '.join(generated_numbers)}.", "success")
    for message in skipped:
        flash(message, "error")
    return redirect(url_for("sales.recurring_list"))


def generate_due_invoices_for_current_company(source="manual"):
    """Generates one invoice for every active template in the *currently active company*
    (from current_company_id()) whose next_run_date has arrived, advancing each template's
    schedule as it goes. Company-agnostic caller convention: this reads whatever company_id
    is on the active session/request context — the nightly scheduler switches that context
    once per company (see app/scheduler.py) rather than this function looping over companies
    itself, so it behaves identically whether triggered by a click or by the clock.

    Returns (generated_invoice_numbers, skipped_error_messages).
    """
    due_templates = [t for t in scoped_query(RecurringInvoice).all() if t.is_due]
    generated_numbers = []
    skipped = []
    for template in due_templates:
        try:
            invoice = _build_invoice_from_template(template)
        except ValueError as exc:
            db.session.rollback()
            skipped.append(str(exc))
            continue
        template.advance_next_run_date()
        template.last_generated_at = datetime.utcnow()
        template.invoices_generated += 1
        log_audit(
            "generate", "recurring_invoice", template.id,
            f"Generated invoice {invoice.invoice_no} from template '{template.name}' ({source} run)", template.name,
        )
        db.session.commit()
        fiscalize_invoice_with_mra(invoice)
        db.session.commit()
        generated_numbers.append(invoice.invoice_no)
    return generated_numbers, skipped


# ── Credit Memos ─────────────────────────────────────────────────────

def next_credit_no():
    last = scoped_query(CreditMemo).order_by(CreditMemo.id.desc()).first()
    next_num = (last.id + 1) if last else 1
    return f"CM-{next_num:04d}"


@sales_bp.route("/credit-memos")
@login_required
def credit_memo_list():
    credit_memos = scoped_query(CreditMemo).order_by(CreditMemo.credit_date.desc(), CreditMemo.id.desc()).all()
    return render_template("sales/credit_memos.html", credit_memos=credit_memos)


@sales_bp.route("/credit-memos/new", methods=["GET", "POST"])
@login_required
def credit_memo_new():
    customers = scoped_query(Customer).filter_by(is_active=True).order_by(Customer.name).all()
    income_accounts = scoped_query(Account).filter_by(account_type="Income", is_active=True).order_by(Account.code).all()
    default_income = scoped_query(Account).filter_by(code=DEFAULT_INCOME_CODE).first()
    items = scoped_query(Item).filter_by(is_active=True).order_by(Item.sku).all()
    preselected_customer_id = request.args.get("customer_id", type=int)
    # "Create Credit Memo" from an invoice's own page — preselects that customer and related
    # invoice, and copies its lines over as a starting point (e.g. to credit it back in full,
    # or edit quantities/lines down for a partial credit) instead of starting from a blank form.
    source_invoice = scoped_get(Invoice, request.args.get("invoice_id", type=int))

    # Which invoice, per customer, this credit memo relates to — populated client-side
    # so the "Related Invoice" dropdown updates the moment a customer is picked, no
    # page reload. Only invoices that actually reached MRA (mra_invoice_number is set)
    # are worth offering, since that's the only kind MRA will accept as a CRN reference.
    invoices_by_customer = {}
    for inv in scoped_query(Invoice).filter(Invoice.status != "void").order_by(Invoice.invoice_date.desc()).all():
        invoices_by_customer.setdefault(inv.customer_id, []).append({
            "id": inv.id, "invoice_no": inv.invoice_no, "total": f"{inv.total:.2f}",
            "mra_invoice_number": inv.mra_invoice_number,
        })

    def render_form(form):
        return render_template(
            "sales/credit_memo_form.html", customers=customers, income_accounts=income_accounts,
            default_income=default_income, items=items, form=form, today=date.today().isoformat(),
            invoices_by_customer=invoices_by_customer,
        )

    if request.method == "GET" and source_invoice:
        return render_form({
            "customer_id": source_invoice.customer_id,
            "related_invoice_id": source_invoice.id,
            "vat_rate": float(source_invoice.vat_rate),
            "memo": f"Credit for {source_invoice.invoice_no}",
            "lines": [
                {
                    "item_id": line.item_id, "description": line.description, "quantity": float(line.quantity),
                    "unit_price": float(line.unit_price), "income_account_id": line.income_account_id,
                    "taxable": line.taxable,
                }
                for line in source_invoice.lines
            ],
        })

    if request.method == "POST":
        customer_id = request.form.get("customer_id")
        if not customer_id:
            flash("Select a customer.", "error")
            return render_form(request.form)

        credit_date = datetime.strptime(request.form["credit_date"], "%Y-%m-%d").date()
        descriptions = request.form.getlist("description")
        quantities = request.form.getlist("quantity")
        unit_prices = request.form.getlist("unit_price")
        taxables = request.form.getlist("taxable")
        income_account_ids = request.form.getlist("income_account_id")
        item_ids = request.form.getlist("item_id")
        return_flags = request.form.getlist("return_to_stock")  # present (as index) only when checked
        item_ids += [""] * (len(descriptions) - len(item_ids))

        related_invoice_id = request.form.get("related_invoice_id") or None

        credit_memo = CreditMemo(
            company_id=current_company_id(),
            credit_no=next_credit_no(),
            customer_id=int(customer_id),
            credit_date=credit_date,
            memo=request.form.get("memo", "").strip() or None,
            vat_rate=float(request.form.get("vat_rate") or 15.00),
            related_invoice_id=int(related_invoice_id) if related_invoice_id else None,
        )

        for i, (desc, qty, price, acc_id, item_id_raw) in enumerate(
            zip(descriptions, quantities, unit_prices, income_account_ids, item_ids)
        ):
            if not desc.strip() or not qty or not price:
                continue
            item = scoped_get(Item, int(item_id_raw)) if item_id_raw else None
            credit_memo.lines.append(
                CreditMemoLine(
                    item=item,
                    description=desc.strip(),
                    quantity=float(qty),
                    unit_price=float(price),
                    taxable=str(i) in taxables,
                    income_account_id=item.income_account_id if item else int(acc_id),
                    return_to_stock=str(i) in return_flags,
                )
            )

        if not credit_memo.lines:
            flash("Add at least one line.", "error")
            return render_form(request.form)

        db.session.add(credit_memo)
        db.session.flush()
        post_credit_memo(credit_memo)
        log_audit("create", "credit_memo", credit_memo.id, f"Created credit memo {credit_memo.credit_no} (total {credit_memo.total:.2f})", credit_memo.credit_no)
        db.session.commit()

        fiscalize_credit_memo_with_mra(credit_memo)
        db.session.commit()

        flash(f"Credit memo {credit_memo.credit_no} created and posted to the ledger.", "success")
        return redirect(url_for("sales.credit_memo_detail", credit_memo_id=credit_memo.id))

    return render_form({"customer_id": preselected_customer_id} if preselected_customer_id else {})


def post_credit_memo(credit_memo):
    """Dr Income (per line) + Dr VAT Payable, Cr Accounts Receivable. Returned stock: Dr Inventory / Cr COGS."""
    ar_account = get_account_or_400(AR_ACCOUNT_CODE, "Accounts Receivable")

    entry = JournalEntry(
        company_id=current_company_id(),
        entry_date=credit_memo.credit_date,
        reference_no=credit_memo.credit_no,
        memo=f"Credit Memo {credit_memo.credit_no} - {credit_memo.memo or ''}".strip(" -"),
        source_type="credit_memo",
        created_by=current_user.id,
    )

    income_totals = {}
    for line in credit_memo.lines:
        income_totals[line.income_account_id] = income_totals.get(line.income_account_id, 0) + line.amount
    for account_id, amount in income_totals.items():
        entry.lines.append(JournalLine(account_id=account_id, debit=amount, credit=0, memo=credit_memo.credit_no))

    if credit_memo.vat_amount:
        vat_account = get_account_or_400(VAT_PAYABLE_CODE, "VAT Payable")
        entry.lines.append(JournalLine(account=vat_account, debit=credit_memo.vat_amount, credit=0, memo=credit_memo.credit_no))

    entry.lines.append(JournalLine(account=ar_account, debit=0, credit=credit_memo.total, memo=credit_memo.credit_no))

    cogs_totals = {}
    inventory_totals = {}
    for line in credit_memo.lines:
        if not line.item_id or not line.item.is_tracked or not line.return_to_stock:
            continue
        item = line.item
        cost = float(item.cost_price)
        item.receive_stock(line.quantity, cost)  # adds qty back at current avg cost — cost itself is unchanged
        amount = float(line.quantity) * cost
        cogs_totals[item.cogs_account_id] = cogs_totals.get(item.cogs_account_id, 0) + amount
        inventory_totals[item.inventory_account_id] = inventory_totals.get(item.inventory_account_id, 0) + amount
        db.session.add(StockMovement(
            item_id=item.id, movement_date=credit_memo.credit_date, movement_type="return",
            quantity=float(line.quantity), unit_cost=cost,
            reference_type="credit_memo", reference_id=credit_memo.id,
            running_quantity=item.quantity_on_hand, running_avg_cost=item.cost_price,
            memo=f"Returned via {credit_memo.credit_no}",
        ))

    for account_id, amount in inventory_totals.items():
        entry.lines.append(JournalLine(account_id=account_id, debit=amount, credit=0, memo=f"Return - {credit_memo.credit_no}"))
    for account_id, amount in cogs_totals.items():
        entry.lines.append(JournalLine(account_id=account_id, debit=0, credit=amount, memo=f"Return - {credit_memo.credit_no}"))

    db.session.add(entry)
    db.session.flush()
    credit_memo.journal_entry_id = entry.id


@sales_bp.route("/credit-memos/<int:credit_memo_id>")
@login_required
def credit_memo_detail(credit_memo_id):
    credit_memo = scoped_or_404(CreditMemo, credit_memo_id)
    open_invoices = sorted(
        [inv for inv in credit_memo.customer.invoices if inv.status in ("open", "partial")],
        key=lambda i: i.due_date,
    )
    share_link = share_url("credit_memo", credit_memo.id, credit_memo.company_id, "share.credit_memo_pdf")
    whatsapp_message = (
        f"Hi {credit_memo.customer.name}, Credit Memo {credit_memo.credit_no} from "
        f"{current_company().business_name} — total {credit_memo.total:.2f}. View/download: {share_link}"
    )
    return render_template(
        "sales/credit_memo_detail.html", credit_memo=credit_memo, open_invoices=open_invoices,
        whatsapp_href=whatsapp_link(credit_memo.customer.phone, whatsapp_message),
    )


@sales_bp.route("/credit-memos/<int:credit_memo_id>/pdf")
@login_required
def credit_memo_pdf(credit_memo_id):
    credit_memo = scoped_or_404(CreditMemo, credit_memo_id)
    buffer = generate_credit_memo_pdf(credit_memo, current_company())
    return send_file(
        buffer, mimetype="application/pdf", as_attachment=False,
        download_name=f"{credit_memo.credit_no}.pdf",
    )


@sales_bp.route("/credit-memos/<int:credit_memo_id>/apply", methods=["POST"])
@login_required
def credit_memo_apply(credit_memo_id):
    credit_memo = scoped_or_404(CreditMemo, credit_memo_id)
    invoice_ids = request.form.getlist("apply_invoice_id")
    apply_amounts = request.form.getlist("apply_amount")

    total_applied = 0.0
    applications = []
    for inv_id, amt_raw in zip(invoice_ids, apply_amounts):
        amt = float(amt_raw or 0)
        if amt <= 0:
            continue
        applications.append((int(inv_id), amt))
        total_applied += amt

    if round(total_applied, 2) > round(credit_memo.remaining_credit, 2):
        flash(
            f"Applied amount ({total_applied:.2f}) exceeds remaining credit ({credit_memo.remaining_credit:.2f}).",
            "error",
        )
        return redirect(url_for("sales.credit_memo_detail", credit_memo_id=credit_memo.id))

    for inv_id, amt in applications:
        credit_memo.applications.append(CreditMemoApplication(invoice_id=inv_id, amount_applied=amt))
    db.session.flush()

    for inv_id, _amt in applications:
        invoice = scoped_get(Invoice, inv_id)
        invoice.status = "paid" if invoice.balance_due <= 0 else "partial"

    if credit_memo.remaining_credit <= 0:
        credit_memo.status = "closed"

    log_audit("apply", "credit_memo", credit_memo.id, f"Applied {total_applied:.2f} of {credit_memo.credit_no} to invoice(s)", credit_memo.credit_no)
    db.session.commit()
    flash(f"Applied {total_applied:.2f} credit to invoice(s).", "success")
    return redirect(url_for("sales.credit_memo_detail", credit_memo_id=credit_memo.id))


@sales_bp.route("/credit-memos/<int:credit_memo_id>/void", methods=["POST"])
@login_required
def credit_memo_void(credit_memo_id):
    credit_memo = scoped_or_404(CreditMemo, credit_memo_id)
    if credit_memo.amount_applied > 0:
        flash("Cannot void a credit memo that's already been applied to an invoice. Unapply it first.", "error")
        return redirect(url_for("sales.credit_memo_detail", credit_memo_id=credit_memo.id))

    for line in credit_memo.lines:
        if line.item_id and line.item.is_tracked and line.return_to_stock:
            item = line.item
            if float(line.quantity) > float(item.quantity_on_hand):
                flash(
                    f"Cannot void: {item.name} only has {item.quantity_on_hand} {item.unit} left "
                    f"(some of this returned stock has already been sold again).", "error",
                )
                return redirect(url_for("sales.credit_memo_detail", credit_memo_id=credit_memo.id))

    for line in credit_memo.lines:
        if line.item_id and line.item.is_tracked and line.return_to_stock:
            item = line.item
            item.quantity_on_hand = float(item.quantity_on_hand) - float(line.quantity)
            db.session.add(StockMovement(
                item_id=item.id, movement_date=date.today(), movement_type="return",
                quantity=-float(line.quantity), unit_cost=item.cost_price,
                reference_type="credit_memo", reference_id=credit_memo.id,
                running_quantity=item.quantity_on_hand, running_avg_cost=item.cost_price,
                memo=f"Reversal - {credit_memo.credit_no} voided",
            ))

    if credit_memo.journal_entry_id:
        entry = JournalEntry.query.get(credit_memo.journal_entry_id)
        if entry:
            db.session.delete(entry)
    credit_memo.status = "void"
    credit_memo.journal_entry_id = None
    log_audit("void", "credit_memo", credit_memo.id, f"Voided credit memo {credit_memo.credit_no}", credit_memo.credit_no)
    db.session.commit()

    void_credit_memo_via_debit_note(credit_memo)
    db.session.commit()

    flash(f"Credit memo {credit_memo.credit_no} voided.", "success")
    return redirect(url_for("sales.credit_memo_list"))


# ── Payments ─────────────────────────────────────────────────────────

@sales_bp.route("/payments")
@login_required
def payment_list():
    payments = scoped_query(Payment).order_by(Payment.payment_date.desc(), Payment.id.desc()).all()
    return render_template("sales/payments.html", payments=payments)


@sales_bp.route("/payments/new", methods=["GET", "POST"])
@login_required
def payment_new():
    customers = scoped_query(Customer).filter_by(is_active=True).order_by(Customer.name).all()
    # Straight into Cash/Bank, or into Undeposited Funds to batch several payments into one
    # lump-sum deposit later (Banking → Make Deposit) — matching how the bank statement will show it.
    deposit_accounts = scoped_query(Account).filter(
        Account.account_type == "Asset", Account.is_active == True,  # noqa: E712
        (Account.subtype == "Cash and Cash Equivalents") | (Account.code == UNDEPOSITED_FUNDS_CODE),
    ).order_by(Account.code).all()
    base_currency = current_company().base_currency

    customer_id = request.values.get("customer_id", type=int)
    is_advance = bool(request.values.get("advance"))
    outstanding_invoices = []
    if customer_id:
        customer = scoped_get(Customer, customer_id)
        if customer:
            outstanding_invoices = sorted(
                [inv for inv in customer.invoices if inv.status in ("open", "partial")],
                key=lambda i: i.due_date,
            )

    if request.method == "POST":
        amount = float(request.form["amount"])
        currency = request.form.get("currency", base_currency) or base_currency
        exchange_rate = float(request.form.get("exchange_rate") or 1.0) if currency != base_currency else 1.0
        invoice_ids = request.form.getlist("apply_invoice_id")
        apply_amounts = request.form.getlist("apply_amount")

        applications = []
        total_applied = 0
        for inv_id, amt_raw in zip(invoice_ids, apply_amounts):
            amt = float(amt_raw or 0)
            if amt <= 0:
                continue
            applications.append((int(inv_id), amt))
            total_applied += amt

        def render_error(message):
            flash(message, "error")
            return render_template(
                "sales/payment_form.html", customers=customers, deposit_accounts=deposit_accounts,
                selected_customer_id=customer_id, outstanding_invoices=outstanding_invoices,
                form=request.form, today=date.today().isoformat(), currencies=CURRENCIES, base_currency=base_currency,
                is_advance=is_advance,
            )

        if exchange_rate <= 0:
            return render_error("Exchange rate must be greater than zero.")
        if round(total_applied, 2) > round(amount, 2):
            return render_error(f"Applied amount ({total_applied:.2f}) exceeds payment amount ({amount:.2f}).")

        # A single payment can't straddle currencies — every invoice it's applied to must
        # be booked in the same currency the payment itself was declared in. Splitting one
        # wire transfer across a USD invoice and a MUR invoice needs two separate payments.
        applied_invoices = [scoped_get(Invoice, inv_id) for inv_id, _ in applications]
        mismatched = [inv for inv in applied_invoices if inv and inv.currency != currency]
        if mismatched:
            names = ", ".join(inv.invoice_no for inv in mismatched)
            return render_error(
                f"This payment is in {currency}, but {names} {'is' if len(mismatched) == 1 else 'are'} in a "
                f"different currency. Record separate payments per currency."
            )

        payment = Payment(
            company_id=current_company_id(),
            customer_id=customer_id,
            payment_date=datetime.strptime(request.form["payment_date"], "%Y-%m-%d").date(),
            amount=amount,
            currency=currency,
            exchange_rate=exchange_rate,
            method=request.form.get("method", "cash"),
            deposit_account_id=int(request.form["deposit_account_id"]),
            reference_no=request.form.get("reference_no", "").strip() or None,
            memo=request.form.get("memo", "").strip() or None,
        )
        invoices_by_id = {inv.id: inv for inv in applied_invoices if inv}
        for inv_id, amt in applications:
            # Assigning the invoice relationship (not just invoice_id) keeps app.invoice
            # populated in memory before this payment is ever added to the session —
            # post_payment() needs each application's invoice.exchange_rate to compute FX.
            payment.applications.append(PaymentApplication(invoice=invoices_by_id[inv_id], amount_applied=amt))

        post_payment(payment)
        db.session.add(payment)
        db.session.flush()  # materialize amounts as Decimal so balance_due math below is consistent

        for inv_id, _amt in applications:
            invoice = scoped_get(Invoice, inv_id)
            if invoice.balance_due <= 0:
                invoice.status = "paid"
            else:
                invoice.status = "partial"

        log_audit("create", "payment", payment.id, f"Recorded payment of {amount:.2f} from customer #{customer_id}")
        db.session.commit()
        fire_webhook(current_company_id(), "payment.received", {
            "id": payment.id, "customer": payment.customer.name, "amount": float(payment.amount),
            "currency": payment.currency, "payment_date": payment.payment_date.isoformat(),
        })
        flash(f"Payment of {amount:.2f} recorded.", "success")
        return redirect(url_for("sales.payment_detail", payment_id=payment.id))

    return render_template(
        "sales/payment_form.html", customers=customers, deposit_accounts=deposit_accounts,
        selected_customer_id=customer_id, outstanding_invoices=outstanding_invoices,
        form={}, today=date.today().isoformat(), currencies=CURRENCIES, base_currency=base_currency,
        is_advance=is_advance,
    )


def post_payment(payment):
    """Dr Cash/Bank (at the payment's own exchange rate), Cr Accounts Receivable.

    For a base-currency payment against base-currency invoices, this is exactly the old
    Dr Bank / Cr AR for payment.amount — no behaviour change. For a foreign-currency
    payment, AR is relieved at each invoice's *own* booked exchange_rate (so the invoice's
    original AR debit is fully and exactly reversed), while cash lands at the payment's
    own rate — the difference between what the invoice was booked at and what actually
    came in is realized FX gain/loss, not left to silently distort AR.
    """
    ar_account = get_account_or_400(AR_ACCOUNT_CODE, "Accounts Receivable")
    payment_rate = float(payment.exchange_rate)

    entry = JournalEntry(
        company_id=current_company_id(),
        entry_date=payment.payment_date,
        reference_no=payment.reference_no,
        memo=f"Payment received - {payment.memo or ''}".strip(" -"),
        source_type="payment",
        created_by=current_user.id,
    )

    cash_base = round(float(payment.amount) * payment_rate, 2)
    entry.lines.append(JournalLine(account_id=payment.deposit_account_id, debit=cash_base, credit=0))

    ar_relief_total = 0.0
    for app in payment.applications:
        ar_relief_base = round(float(app.amount_applied) * app.invoice.carrying_exchange_rate, 2)
        ar_relief_total += ar_relief_base
    # Whatever wasn't applied to a specific invoice is still a real cash receipt against
    # this customer's AR (an unapplied credit balance) — relieved at the payment's own
    # rate, since there's no invoice rate to reconcile it against.
    unapplied = round(float(payment.amount) - sum(float(a.amount_applied) for a in payment.applications), 2)
    unapplied_base = round(unapplied * payment_rate, 2)
    ar_relief_total = round(ar_relief_total + unapplied_base, 2)

    entry.lines.append(JournalLine(account=ar_account, debit=0, credit=ar_relief_total))

    fx_gain_loss = round(cash_base - ar_relief_total, 2)
    if fx_gain_loss:
        fx_account = get_account_or_400(FX_GAIN_LOSS_CODE, "Realized Gain/Loss on Exchange")
        if fx_gain_loss > 0:
            entry.lines.append(JournalLine(account=fx_account, debit=0, credit=fx_gain_loss, memo="Realized FX gain"))
        else:
            entry.lines.append(JournalLine(account=fx_account, debit=-fx_gain_loss, credit=0, memo="Realized FX loss"))

    db.session.add(entry)
    db.session.flush()
    payment.journal_entry_id = entry.id


@sales_bp.route("/payments/<int:payment_id>")
@login_required
def payment_detail(payment_id):
    payment = scoped_or_404(Payment, payment_id)
    return render_template("sales/payment_detail.html", payment=payment)


# ── Reports ─────────────────────────────────────────────────────────

AGING_BUCKET_LABELS = [("current", "Current"), ("1_30", "1-30 Days"), ("31_60", "31-60 Days"), ("61_90", "61-90 Days"), ("90_plus", "90+ Days")]


def _ar_aging_buckets():
    today = date.today()
    invoices = scoped_query(Invoice).filter(Invoice.status.in_(["open", "partial"])).all()

    buckets = {"current": [], "1_30": [], "31_60": [], "61_90": [], "90_plus": []}
    for inv in invoices:
        if inv.balance_due <= 0:
            continue
        days = (today - inv.due_date).days
        if days <= 0:
            buckets["current"].append(inv)
        elif days <= 30:
            buckets["1_30"].append(inv)
        elif days <= 60:
            buckets["31_60"].append(inv)
        elif days <= 90:
            buckets["61_90"].append(inv)
        else:
            buckets["90_plus"].append(inv)

    totals = {key: sum((inv.balance_due for inv in invs), start=0) for key, invs in buckets.items()}
    grand_total = sum(totals.values(), start=0)
    return today, buckets, totals, grand_total


@sales_bp.route("/aging")
@login_required
def aging_report():
    today, buckets, totals, grand_total = _ar_aging_buckets()
    return render_template("sales/aging.html", buckets=buckets, totals=totals, grand_total=grand_total, today=today)


def _ar_aging_rows():
    today, buckets, _, _ = _ar_aging_buckets()
    rows = []
    for key, label in AGING_BUCKET_LABELS:
        for inv in buckets[key]:
            rows.append((label, inv.invoice_no, inv.customer.name, inv.due_date.isoformat(), inv.days_overdue, round(inv.balance_due, 2)))
    return today, rows


@sales_bp.route("/aging/export.xlsx")
@login_required
def aging_export_xlsx():
    today, rows = _ar_aging_rows()
    headers = ["Bucket", "Invoice #", "Customer", "Due Date", "Days Overdue", "Balance Due"]
    buffer = rows_to_xlsx(headers, rows, sheet_title="AR Aging")
    return send_file(
        buffer, as_attachment=True, download_name=f"AR-Aging-{today.isoformat()}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@sales_bp.route("/aging/export.pdf")
@login_required
def aging_export_pdf():
    today, rows = _ar_aging_rows()
    headers = ["Bucket", "Invoice #", "Customer", "Due Date", "Days Overdue", "Balance Due"]
    buffer = rows_to_pdf(
        "Accounts Receivable Aging", f"As of {today.isoformat()}", headers, rows,
        company=current_company(), numeric_cols={4, 5},
    )
    return send_file(
        buffer, as_attachment=True, download_name=f"AR-Aging-{today.isoformat()}.pdf", mimetype="application/pdf",
    )
