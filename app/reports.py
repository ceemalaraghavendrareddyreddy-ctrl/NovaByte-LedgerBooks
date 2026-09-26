import csv
import io
from datetime import date, timedelta

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import db
from app.auth import current_company_id
from app.models import (
    ACCOUNT_TYPES, Account, Bill, BillLine, Invoice, InvoiceLine, JournalEntry, JournalLine, Project,
    RecurringBill, RecurringInvoice, SavedReport, add_months,
)
from app.scoping import scoped_or_404, scoped_query

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")

VAT_PAYABLE_CODE = "2100"    # output VAT — collected on sales
VAT_RECEIVABLE_CODE = "2110"  # input VAT — paid on purchases
CASH_SUBTYPE = "Cash and Cash Equivalents"


def period_movement(account, start_date, end_date, project_id=None):
    """Net movement for one account between two dates (inclusive), signed in its normal-balance direction.

    Unlike Account.balance() (which is cumulative since inception), this is what
    Profit & Loss needs: income/expense activity for just this period.

    project_id, when given, filters to journal entries tagged with that project
    (see Project) — the per-project P&L. Left as None (the default, and every
    existing caller) it's the whole company's consolidated movement, unchanged.
    """
    query = (
        JournalLine.query.filter_by(account_id=account.id)
        .join(JournalEntry)
        .filter(JournalEntry.entry_date >= start_date, JournalEntry.entry_date <= end_date)
    )
    if project_id is not None:
        query = query.filter(JournalEntry.project_id == project_id)
    lines = query.all()
    total_debit = sum((float(line.debit) for line in lines), start=0.0)
    total_credit = sum((float(line.credit) for line in lines), start=0.0)
    movement = total_debit - total_credit
    if account.normal_balance == "credit":
        movement = -movement
    return movement


@reports_bp.route("/profit-loss")
@login_required
def profit_loss():
    start_raw = request.args.get("start_date")
    end_raw = request.args.get("end_date")
    start_date = date.fromisoformat(start_raw) if start_raw else date.today().replace(day=1)
    end_date = date.fromisoformat(end_raw) if end_raw else date.today()
    project_id = request.args.get("project_id", type=int)

    income_accounts = scoped_query(Account).filter_by(account_type="Income", is_active=True).order_by(Account.code).all()
    expense_accounts = scoped_query(Account).filter_by(account_type="Expense", is_active=True).order_by(Account.code).all()
    projects = scoped_query(Project).order_by(Project.name).all()
    selected_project = next((p for p in projects if p.id == project_id), None) if project_id else None

    income_rows = [
        {"account": a, "amount": period_movement(a, start_date, end_date, project_id)} for a in income_accounts
    ]
    income_rows = [r for r in income_rows if r["amount"] != 0]
    expense_rows = [
        {"account": a, "amount": period_movement(a, start_date, end_date, project_id)} for a in expense_accounts
    ]
    expense_rows = [r for r in expense_rows if r["amount"] != 0]

    total_income = sum((r["amount"] for r in income_rows), start=0.0)
    total_expense = sum((r["amount"] for r in expense_rows), start=0.0)
    net_income = total_income - total_expense

    return render_template(
        "reports/profit_loss.html",
        income_rows=income_rows, expense_rows=expense_rows,
        total_income=total_income, total_expense=total_expense, net_income=net_income,
        projects=projects, selected_project=selected_project,
        start_date=start_date.isoformat(), end_date=end_date.isoformat(),
    )


@reports_bp.route("/balance-sheet")
@login_required
def balance_sheet():
    as_of_raw = request.args.get("as_of")
    as_of = date.fromisoformat(as_of_raw) if as_of_raw else date.today()

    asset_accounts = scoped_query(Account).filter_by(account_type="Asset", is_active=True).order_by(Account.code).all()
    liability_accounts = scoped_query(Account).filter_by(account_type="Liability", is_active=True).order_by(Account.code).all()
    equity_accounts = scoped_query(Account).filter_by(account_type="Equity", is_active=True).order_by(Account.code).all()
    income_accounts = scoped_query(Account).filter_by(account_type="Income", is_active=True).all()
    expense_accounts = scoped_query(Account).filter_by(account_type="Expense", is_active=True).all()

    asset_rows = [{"account": a, "balance": float(a.balance(as_of=as_of))} for a in asset_accounts]
    liability_rows = [{"account": a, "balance": float(a.balance(as_of=as_of))} for a in liability_accounts]
    equity_rows = [{"account": a, "balance": float(a.balance(as_of=as_of))} for a in equity_accounts]

    # Income/Expense accounts aren't closed to Retained Earnings anywhere in this app yet,
    # so their cumulative net sits here as "Net Income (current period)" to keep the
    # accounting equation (Assets = Liabilities + Equity) balanced.
    net_income = sum((float(a.balance(as_of=as_of)) for a in income_accounts), start=0.0) - sum(
        (float(a.balance(as_of=as_of)) for a in expense_accounts), start=0.0
    )

    total_assets = sum((r["balance"] for r in asset_rows), start=0.0)
    total_liabilities = sum((r["balance"] for r in liability_rows), start=0.0)
    total_equity = sum((r["balance"] for r in equity_rows), start=0.0) + net_income

    # Net Income here is cumulative since inception (Account.balance() has no fiscal-year
    # reset, unlike period_movement()) — so its P&L drill-down has to start from the
    # earliest activity on record, not "the 1st of this month" like the P&L page's own default.
    earliest_entry = scoped_query(JournalEntry).order_by(JournalEntry.entry_date).first()
    net_income_start = earliest_entry.entry_date if earliest_entry else as_of

    return render_template(
        "reports/balance_sheet.html",
        asset_rows=asset_rows, liability_rows=liability_rows, equity_rows=equity_rows,
        net_income=net_income, total_assets=total_assets, total_liabilities=total_liabilities,
        total_equity=total_equity, as_of=as_of.isoformat(), net_income_start=net_income_start.isoformat(),
    )


def _vat_lines(account, start_date, end_date, direction):
    """Individual journal lines that hit a VAT account in the period, for the return's detail table.

    direction='credit' reads the amount as (credit - debit) — for VAT Payable, where a normal
    sale credits it. direction='debit' reads (debit - credit) — for VAT Receivable, where a
    normal purchase debits it. A reversal (void) naturally nets out since it hits the opposite side.
    """
    lines = (
        JournalLine.query.filter_by(account_id=account.id)
        .join(JournalEntry)
        .filter(JournalEntry.entry_date >= start_date, JournalEntry.entry_date <= end_date)
        .order_by(JournalEntry.entry_date)
        .all()
    )
    rows = []
    for line in lines:
        amount = float(line.credit) - float(line.debit) if direction == "credit" else float(line.debit) - float(line.credit)
        if amount == 0:
            continue
        rows.append({"entry": line.entry, "amount": amount})
    return rows


@reports_bp.route("/vat-return")
@login_required
def vat_return():
    start_raw = request.args.get("start_date")
    end_raw = request.args.get("end_date")
    start_date = date.fromisoformat(start_raw) if start_raw else date.today().replace(day=1)
    end_date = date.fromisoformat(end_raw) if end_raw else date.today()

    vat_payable = scoped_query(Account).filter_by(code=VAT_PAYABLE_CODE).first()
    vat_receivable = scoped_query(Account).filter_by(code=VAT_RECEIVABLE_CODE).first()
    if not vat_payable or not vat_receivable:
        flash("VAT Payable (2100) or VAT Receivable (2110) account is missing from the Chart of Accounts.", "error")
        return render_template(
            "reports/vat_return.html", output_lines=[], input_lines=[], output_total=0, input_total=0,
            net_vat=0, start_date=start_date.isoformat(), end_date=end_date.isoformat(),
        )

    output_lines = _vat_lines(vat_payable, start_date, end_date, "credit")
    input_lines = _vat_lines(vat_receivable, start_date, end_date, "debit")

    output_total = sum((r["amount"] for r in output_lines), start=0.0)
    input_total = sum((r["amount"] for r in input_lines), start=0.0)
    net_vat = output_total - input_total  # positive = owe MRA, negative = refund due

    return render_template(
        "reports/vat_return.html",
        output_lines=output_lines, input_lines=input_lines, output_total=output_total, input_total=input_total,
        net_vat=net_vat, start_date=start_date.isoformat(), end_date=end_date.isoformat(),
    )


@reports_bp.route("/cash-flow")
@login_required
def cash_flow():
    """Indirect-method Cash Flow Statement, driven generically by account subtype rather than a
    hardcoded account list — so it stays correct as new accounts get added, as long as their
    subtype is set sensibly (Current Asset/Liability = operating, Fixed Asset = investing,
    Long-Term Liability or Equity = financing).
    """
    start_raw = request.args.get("start_date")
    end_raw = request.args.get("end_date")
    start_date = date.fromisoformat(start_raw) if start_raw else date.today().replace(day=1)
    end_date = date.fromisoformat(end_raw) if end_raw else date.today()
    period_start_prior = start_date - timedelta(days=1)

    income_accounts = scoped_query(Account).filter_by(account_type="Income", is_active=True).all()
    expense_accounts = scoped_query(Account).filter_by(account_type="Expense", is_active=True).all()
    net_income = sum((period_movement(a, start_date, end_date) for a in income_accounts), start=0.0) - sum(
        (period_movement(a, start_date, end_date) for a in expense_accounts), start=0.0
    )

    def change_in(account):
        begin = float(account.balance(as_of=period_start_prior))
        end = float(account.balance(as_of=end_date))
        return round(end - begin, 2)

    operating_rows = []
    investing_rows = []
    financing_rows = []

    all_accounts = scoped_query(Account).filter_by(is_active=True).all()
    for account in all_accounts:
        if account.account_type == "Asset" and account.subtype == CASH_SUBTYPE:
            continue  # this is the cash we're explaining the movement of, not a driver of it
        delta = change_in(account)
        if delta == 0:
            continue

        direction = "increase" if delta > 0 else "decrease"
        if account.subtype == "Current Asset":
            # A growing asset (AR, Inventory, VAT Receivable) ties up cash — subtract its increase.
            operating_rows.append({"account": account, "amount": -delta, "direction": direction})
        elif account.subtype == "Current Liability":
            # A growing liability (AP, VAT Payable) means cash hasn't gone out yet — add its increase.
            operating_rows.append({"account": account, "amount": delta, "direction": direction})
        elif account.subtype == "Fixed Asset":
            investing_rows.append({"account": account, "amount": -delta, "direction": direction})
        elif account.subtype == "Long-Term Liability" or account.account_type == "Equity":
            financing_rows.append({"account": account, "amount": delta, "direction": direction})

    operating_total = net_income + sum((r["amount"] for r in operating_rows), start=0.0)
    investing_total = sum((r["amount"] for r in investing_rows), start=0.0)
    financing_total = sum((r["amount"] for r in financing_rows), start=0.0)
    net_change = operating_total + investing_total + financing_total

    cash_accounts = scoped_query(Account).filter_by(account_type="Asset", is_active=True, subtype=CASH_SUBTYPE).all()
    cash_begin = sum((float(a.balance(as_of=period_start_prior)) for a in cash_accounts), start=0.0)
    cash_end = sum((float(a.balance(as_of=end_date)) for a in cash_accounts), start=0.0)
    reconciliation_gap = round(cash_end - (cash_begin + net_change), 2)

    return render_template(
        "reports/cash_flow.html",
        net_income=net_income, operating_rows=operating_rows, investing_rows=investing_rows,
        financing_rows=financing_rows, operating_total=operating_total, investing_total=investing_total,
        financing_total=financing_total, net_change=net_change, cash_begin=cash_begin, cash_end=cash_end,
        reconciliation_gap=reconciliation_gap, start_date=start_date.isoformat(), end_date=end_date.isoformat(),
    )


# ── Cash Flow Forecast (forward-looking, distinct from the historical Cash Flow
# Statement above) ──────────────────────────────────────────────────────

def _recurring_occurrences_in_window(template, window_end):
    """Every future occurrence date of a recurring invoice/bill template, from
    its own next_run_date up to (and including) window_end — without mutating
    the real template (advance_next_run_date() does that; this just replays the
    same stepping logic on a local variable). A template already past its
    end_date or inactive contributes nothing."""
    if not template.is_active or template.is_ended:
        return []
    dates = []
    current = template.next_run_date
    while current <= window_end and (not template.end_date or current <= template.end_date):
        dates.append(current)
        if template.frequency == "weekly":
            current = date.fromordinal(current.toordinal() + 7)
        elif template.frequency == "quarterly":
            current = add_months(current, 3)
        elif template.frequency == "yearly":
            current = add_months(current, 12)
        else:
            current = add_months(current, 1)
    return dates


@reports_bp.route("/cash-flow-forecast")
@login_required
def cash_flow_forecast():
    """Forward-looking weekly cash projection: starting cash position, plus
    expected inflows (open/partial invoices by due date, plus scheduled
    recurring-invoice occurrences) and expected outflows (same for bills),
    bucketed into weeks. A best-effort projection, not a guarantee — a customer
    paying late or a recurring template being paused shifts the real numbers;
    this is why every bucket also lists what actually makes it up, not just a
    single projected total to take on faith.
    """
    weeks = request.args.get("weeks", 13, type=int)
    weeks = max(1, min(weeks, 26))
    today = date.today()
    window_end = today + timedelta(weeks=weeks)

    cash_accounts = scoped_query(Account).filter_by(account_type="Asset", is_active=True, subtype=CASH_SUBTYPE).all()
    starting_cash = sum((float(a.balance(as_of=today)) for a in cash_accounts), start=0.0)

    # Bucket boundaries: [today, today+7), [today+7, today+14), ... — the last
    # bucket also absorbs anything already overdue, since that money is still
    # realistically expected "soon", not excluded from the forecast entirely.
    bucket_starts = [today + timedelta(weeks=i) for i in range(weeks)]
    buckets = [{"start": b, "end": b + timedelta(days=6), "inflows": [], "outflows": []} for b in bucket_starts]

    def bucket_for(due_date):
        if due_date < today:
            return buckets[0]  # overdue — expected in the very first bucket
        for b in buckets:
            if b["start"] <= due_date <= b["end"]:
                return b
        return None  # past the forecast window entirely

    for inv in scoped_query(Invoice).filter(Invoice.status.in_(["open", "partial"])).all():
        if inv.balance_due <= 0:
            continue
        b = bucket_for(inv.due_date)
        if b:
            b["inflows"].append({"label": f"Invoice {inv.invoice_no} — {inv.customer.name}", "amount": inv.balance_due, "date": inv.due_date})

    for bill in scoped_query(Bill).filter(Bill.status.in_(["open", "partial"])).all():
        if bill.balance_due <= 0:
            continue
        b = bucket_for(bill.due_date)
        if b:
            b["outflows"].append({"label": f"Bill {bill.bill_no} — {bill.vendor.name}", "amount": bill.balance_due, "date": bill.due_date})

    for template in scoped_query(RecurringInvoice).filter_by(is_active=True).all():
        for occurrence_date in _recurring_occurrences_in_window(template, window_end):
            due = occurrence_date + timedelta(days=template.due_days)
            b = bucket_for(due)
            if b:
                b["inflows"].append({"label": f"{template.name} (recurring)", "amount": template.total, "date": due})

    for template in scoped_query(RecurringBill).filter_by(is_active=True).all():
        for occurrence_date in _recurring_occurrences_in_window(template, window_end):
            due = occurrence_date + timedelta(days=template.due_days)
            b = bucket_for(due)
            if b:
                b["outflows"].append({"label": f"{template.name} (recurring)", "amount": template.total, "date": due})

    running_balance = starting_cash
    chart_labels, chart_balances = [], []
    for b in buckets:
        b["inflow_total"] = round(sum(i["amount"] for i in b["inflows"]), 2)
        b["outflow_total"] = round(sum(o["amount"] for o in b["outflows"]), 2)
        b["net"] = round(b["inflow_total"] - b["outflow_total"], 2)
        running_balance = round(running_balance + b["net"], 2)
        b["projected_balance"] = running_balance
        b["inflows"].sort(key=lambda r: r["date"])
        b["outflows"].sort(key=lambda r: r["date"])
        chart_labels.append(b["start"].strftime("%d %b"))
        chart_balances.append(b["projected_balance"])

    lowest_point = min((b["projected_balance"] for b in buckets), default=starting_cash)

    return render_template(
        "reports/cash_flow_forecast.html",
        buckets=buckets, starting_cash=starting_cash, weeks=weeks, today=today,
        lowest_point=lowest_point, final_balance=running_balance,
        chart_labels=chart_labels, chart_balances=chart_balances,
    )


# ── Custom Report Builder ─────────────────────────────────────────────

def _custom_report_rows(account_type, account_subtype, date_mode, start_date, end_date, as_of):
    query = scoped_query(Account).filter_by(is_active=True)
    if account_type:
        query = query.filter_by(account_type=account_type)
    if account_subtype:
        query = query.filter_by(subtype=account_subtype)
    accounts = query.order_by(Account.code).all()

    rows = []
    for account in accounts:
        if date_mode == "asof":
            amount = float(account.balance(as_of=as_of))
        else:
            amount = period_movement(account, start_date, end_date)
        if amount == 0:
            continue
        rows.append({"account": account, "amount": amount})
    return rows


def _parse_custom_report_args(args):
    account_type = args.get("account_type", "").strip()
    account_subtype = args.get("account_subtype", "").strip()
    date_mode = args.get("date_mode", "range")
    start_raw = args.get("start_date")
    end_raw = args.get("end_date")
    as_of_raw = args.get("as_of")
    start_date = date.fromisoformat(start_raw) if start_raw else date.today().replace(day=1)
    end_date = date.fromisoformat(end_raw) if end_raw else date.today()
    as_of = date.fromisoformat(as_of_raw) if as_of_raw else date.today()
    return account_type, account_subtype, date_mode, start_date, end_date, as_of


def _range_args():
    start_raw = request.args.get("start_date")
    end_raw = request.args.get("end_date")
    start_date = date.fromisoformat(start_raw) if start_raw else date.today().replace(day=1)
    end_date = date.fromisoformat(end_raw) if end_raw else date.today()
    return start_date, end_date


@reports_bp.route("/sales-by-customer")
@login_required
def sales_by_customer():
    start_date, end_date = _range_args()
    invoices = scoped_query(Invoice).filter(
        Invoice.invoice_date >= start_date, Invoice.invoice_date <= end_date, Invoice.status != "void",
    ).all()

    totals = {}
    for inv in invoices:
        row = totals.setdefault(inv.customer_id, {"customer": inv.customer, "count": 0, "amount": 0.0})
        row["count"] += 1
        row["amount"] += inv.total_base
    rows = sorted(totals.values(), key=lambda r: r["amount"], reverse=True)
    total = sum((r["amount"] for r in rows), start=0.0)

    return render_template(
        "reports/sales_by_customer.html", rows=rows, total=total,
        start_date=start_date.isoformat(), end_date=end_date.isoformat(),
    )


@reports_bp.route("/sales-by-item")
@login_required
def sales_by_item():
    start_date, end_date = _range_args()
    lines = (
        InvoiceLine.query.join(Invoice)
        .filter(
            Invoice.company_id == current_company_id(), Invoice.invoice_date >= start_date,
            Invoice.invoice_date <= end_date, Invoice.status != "void",
        ).all()
    )

    totals = {}
    for line in lines:
        key = line.item_id or 0
        row = totals.setdefault(key, {
            "label": f"{line.item.sku} — {line.item.name}" if line.item else "No item (generic line)",
            "qty": 0.0, "amount": 0.0,
        })
        row["qty"] += float(line.quantity)
        row["amount"] += line.amount * float(line.invoice.exchange_rate)
    rows = sorted(totals.values(), key=lambda r: r["amount"], reverse=True)
    total = sum((r["amount"] for r in rows), start=0.0)

    return render_template(
        "reports/sales_by_item.html", rows=rows, total=total,
        start_date=start_date.isoformat(), end_date=end_date.isoformat(),
    )


@reports_bp.route("/purchases-by-vendor")
@login_required
def purchases_by_vendor():
    start_date, end_date = _range_args()
    bills = scoped_query(Bill).filter(
        Bill.bill_date >= start_date, Bill.bill_date <= end_date, Bill.status != "void",
    ).all()

    totals = {}
    for bill in bills:
        row = totals.setdefault(bill.vendor_id, {"vendor": bill.vendor, "count": 0, "amount": 0.0})
        row["count"] += 1
        row["amount"] += bill.total_base
    rows = sorted(totals.values(), key=lambda r: r["amount"], reverse=True)
    total = sum((r["amount"] for r in rows), start=0.0)

    return render_template(
        "reports/purchases_by_vendor.html", rows=rows, total=total,
        start_date=start_date.isoformat(), end_date=end_date.isoformat(),
    )


@reports_bp.route("/purchases-by-item")
@login_required
def purchases_by_item():
    start_date, end_date = _range_args()
    lines = (
        BillLine.query.join(Bill)
        .filter(
            Bill.company_id == current_company_id(), Bill.bill_date >= start_date,
            Bill.bill_date <= end_date, Bill.status != "void",
        ).all()
    )

    totals = {}
    for line in lines:
        key = line.item_id or 0
        row = totals.setdefault(key, {
            "label": f"{line.item.sku} — {line.item.name}" if line.item else "No item (generic line)",
            "qty": 0.0, "amount": 0.0,
        })
        row["qty"] += float(line.quantity)
        row["amount"] += line.amount * float(line.bill.exchange_rate)
    rows = sorted(totals.values(), key=lambda r: r["amount"], reverse=True)
    total = sum((r["amount"] for r in rows), start=0.0)

    return render_template(
        "reports/purchases_by_item.html", rows=rows, total=total,
        start_date=start_date.isoformat(), end_date=end_date.isoformat(),
    )


@reports_bp.route("/custom")
@login_required
def custom_report():
    account_type, account_subtype, date_mode, start_date, end_date, as_of = _parse_custom_report_args(request.args)
    rows = _custom_report_rows(account_type, account_subtype, date_mode, start_date, end_date, as_of)
    total = sum((r["amount"] for r in rows), start=0.0)
    subtypes = sorted({
        row[0] for row in db.session.query(Account.subtype)
        .filter(Account.company_id == current_company_id()).distinct().all() if row[0]
    })
    saved_reports = scoped_query(SavedReport).order_by(SavedReport.name).all()

    return render_template(
        "reports/custom.html", rows=rows, total=total, account_types=ACCOUNT_TYPES, subtypes=subtypes,
        account_type=account_type, account_subtype=account_subtype, date_mode=date_mode,
        start_date=start_date.isoformat(), end_date=end_date.isoformat(), as_of=as_of.isoformat(),
        saved_reports=saved_reports,
    )


@reports_bp.route("/custom/export.csv")
@login_required
def custom_report_export():
    account_type, account_subtype, date_mode, start_date, end_date, as_of = _parse_custom_report_args(request.args)
    rows = _custom_report_rows(account_type, account_subtype, date_mode, start_date, end_date, as_of)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Code", "Account", "Type", "Subtype", "Amount"])
    for r in rows:
        writer.writerow([r["account"].code, r["account"].name, r["account"].account_type, r["account"].subtype or "", f"{r['amount']:.2f}"])

    return Response(
        buffer.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=custom_report.csv"},
    )


@reports_bp.route("/custom/save", methods=["POST"])
@login_required
def custom_report_save():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Give the report a name before saving.", "error")
        return redirect(url_for("reports.custom_report", **request.form.to_dict()))

    saved = SavedReport(
        company_id=current_company_id(),
        name=name,
        account_type=request.form.get("account_type") or None,
        account_subtype=request.form.get("account_subtype") or None,
        date_mode=request.form.get("date_mode", "range"),
        created_by=current_user.id,
    )
    db.session.add(saved)
    db.session.commit()
    flash(f"Report '{name}' saved.", "success")
    return redirect(url_for("reports.custom_report"))


@reports_bp.route("/custom/saved/<int:saved_id>/delete", methods=["POST"])
@login_required
def custom_report_delete(saved_id):
    saved = scoped_or_404(SavedReport, saved_id)
    db.session.delete(saved)
    db.session.commit()
    flash("Saved report deleted.", "success")
    return redirect(url_for("reports.custom_report"))
