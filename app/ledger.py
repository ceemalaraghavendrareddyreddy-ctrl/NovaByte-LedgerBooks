from datetime import date, datetime, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import db
from app.audit import log_audit
from app.auth import current_company_id
from app.models import ACCOUNT_TYPES, CURRENCIES, Account, JournalEntry, JournalLine
from app.period_lock import assert_period_open
from app.scoping import scoped_or_404, scoped_query

ledger_bp = Blueprint("ledger", __name__, url_prefix="/ledger")

VAT_PAYABLE_CODE = "2100"     # output VAT — credited when a line's own tax % applies to a credit
VAT_RECEIVABLE_CODE = "2110"  # input VAT — debited when a line's own tax % applies to a debit


def _expand_line_with_tax(lines_out, account_id, debit, credit, memo, tax_rate):
    """Appends one plain JournalLine for (account_id, debit, credit) to lines_out, plus —
    if tax_rate is set — a second line posting that percentage of whichever side is
    non-zero to the matching VAT account. The tax amount is ADDED on top of the entered
    amount (the same "line amount is net, VAT is extra" convention Invoice/Bill lines
    already use), so a manual entry with tax looks exactly like an invoice/bill line:
    100 + 15% VAT needs a 115 line on the other side of the entry, not a 100 one.

    Returns (extra_debit, extra_credit) to add to the entry's running totals, or None if
    the required VAT account is missing (caller flashes and aborts in that case).
    """
    if not tax_rate:
        lines_out.append(JournalLine(account_id=account_id, debit=debit, credit=credit, memo=memo))
        return 0.0, 0.0

    if debit > 0:
        vat_account = scoped_query(Account).filter_by(code=VAT_RECEIVABLE_CODE).first()
        if not vat_account:
            return None
        tax_amount = round(debit * tax_rate / 100, 2)
        lines_out.append(JournalLine(account_id=account_id, debit=debit, credit=0, memo=memo))
        lines_out.append(JournalLine(account_id=vat_account.id, debit=tax_amount, credit=0, memo=f"VAT {tax_rate:g}%" + (f" - {memo}" if memo else "")))
        return tax_amount, 0.0
    else:
        vat_account = scoped_query(Account).filter_by(code=VAT_PAYABLE_CODE).first()
        if not vat_account:
            return None
        tax_amount = round(credit * tax_rate / 100, 2)
        lines_out.append(JournalLine(account_id=account_id, debit=0, credit=credit, memo=memo))
        lines_out.append(JournalLine(account_id=vat_account.id, debit=0, credit=tax_amount, memo=f"VAT {tax_rate:g}%" + (f" - {memo}" if memo else "")))
        return 0.0, tax_amount


# ── Chart of Accounts ──────────────────────────────────────────────

@ledger_bp.route("/accounts")
@login_required
def account_list():
    accounts = scoped_query(Account).order_by(Account.code).all()
    grouped = {t: [a for a in accounts if a.account_type == t] for t in ACCOUNT_TYPES}
    return render_template("ledger/accounts.html", grouped=grouped)


@ledger_bp.route("/accounts/new", methods=["GET", "POST"])
@login_required
def account_new():
    if request.method == "POST":
        code = request.form["code"].strip()
        if scoped_query(Account).filter_by(code=code).first():
            flash(f"Account code {code} already exists.", "error")
            return render_template("ledger/account_form.html", account_types=ACCOUNT_TYPES, currencies=CURRENCIES, form=request.form)

        account = Account(
            company_id=current_company_id(),
            code=code,
            name=request.form["name"].strip(),
            account_type=request.form["account_type"],
            subtype=request.form.get("subtype", "").strip() or None,
            parent_id=request.form.get("parent_id") or None,
            description=request.form.get("description", "").strip() or None,
            opening_balance=request.form.get("opening_balance") or 0,
            bank_name=request.form.get("bank_name", "").strip() or None,
            account_number=request.form.get("account_number", "").strip() or None,
            currency=request.form.get("currency") or "MUR",
        )
        db.session.add(account)
        db.session.commit()
        flash(f"Account {account.code} - {account.name} created.", "success")
        return redirect(url_for("ledger.account_list"))

    accounts = scoped_query(Account).order_by(Account.code).all()
    # Lets the Banking > Accounts page's "+ Add Bank Account" link pre-fill type/subtype
    # via query string, so someone adding a bank account doesn't need to know that's
    # "Asset" + "Cash and Cash Equivalents" under the hood.
    return render_template("ledger/account_form.html", account_types=ACCOUNT_TYPES, currencies=CURRENCIES, accounts=accounts, form=request.args)


@ledger_bp.route("/accounts/<int:account_id>")
@login_required
def account_detail(account_id):
    account = scoped_or_404(Account, account_id)
    lines = (
        JournalLine.query.filter_by(account_id=account.id)
        .join(JournalEntry)
        .order_by(JournalEntry.entry_date, JournalEntry.id)
        .all()
    )
    running = account.opening_balance
    rows = []
    for line in lines:
        movement = line.debit - line.credit
        if account.normal_balance == "credit":
            movement = -movement
        running += movement
        rows.append({"line": line, "running_balance": running})
    return render_template("ledger/account_detail.html", account=account, rows=rows)


@ledger_bp.route("/accounts/<int:account_id>/toggle", methods=["POST"])
@login_required
def account_toggle(account_id):
    account = scoped_or_404(Account, account_id)
    account.is_active = not account.is_active
    db.session.commit()
    flash(f"Account {account.code} {'activated' if account.is_active else 'deactivated'}.", "success")
    return redirect(url_for("ledger.account_list"))


# ── Journal Entries ─────────────────────────────────────────────────

@ledger_bp.route("/journal")
@login_required
def journal_list():
    entries = scoped_query(JournalEntry).order_by(JournalEntry.entry_date.desc(), JournalEntry.id.desc()).all()
    return render_template("ledger/journal_list.html", entries=entries)


@ledger_bp.route("/journal/new", methods=["GET", "POST"])
@login_required
def journal_new():
    accounts = scoped_query(Account).filter_by(is_active=True).order_by(Account.code).all()

    if request.method == "POST":
        entry_date = datetime.strptime(request.form["entry_date"], "%Y-%m-%d").date()
        reference_no = request.form.get("reference_no", "").strip() or None
        memo = request.form.get("memo", "").strip() or None

        account_ids = request.form.getlist("account_id")
        debits = request.form.getlist("debit")
        credits = request.form.getlist("credit")
        line_memos = request.form.getlist("line_memo")
        tax_rates = request.form.getlist("tax_rate")
        tax_rates += [""] * (len(account_ids) - len(tax_rates))

        entry = JournalEntry(
            company_id=current_company_id(),
            entry_date=entry_date,
            reference_no=reference_no,
            memo=memo,
            source_type="manual",
            created_by=current_user.id,
        )

        total_debit = 0
        total_credit = 0
        for acc_id, debit_raw, credit_raw, line_memo, tax_rate_raw in zip(account_ids, debits, credits, line_memos, tax_rates):
            if not acc_id:
                continue
            debit = float(debit_raw or 0)
            credit = float(credit_raw or 0)
            if debit == 0 and credit == 0:
                continue
            tax_rate = float(tax_rate_raw) if tax_rate_raw.strip() != "" else 0.0
            memo_text = line_memo.strip() or None
            result = _expand_line_with_tax(entry.lines, int(acc_id), debit, credit, memo_text, tax_rate)
            if result is None:
                missing_code = VAT_RECEIVABLE_CODE if debit > 0 else VAT_PAYABLE_CODE
                flash(f"Can't apply tax: account {missing_code} is missing from the Chart of Accounts.", "error")
                return render_template("ledger/journal_form.html", accounts=accounts, form=request.form)
            extra_debit, extra_credit = result
            total_debit += debit + extra_debit
            total_credit += credit + extra_credit

        if not entry.lines:
            flash("Add at least one line with an amount.", "error")
            return render_template("ledger/journal_form.html", accounts=accounts, form=request.form)

        if round(total_debit, 2) != round(total_credit, 2):
            flash(f"Entry does not balance: Debit {total_debit:.2f} vs Credit {total_credit:.2f}.", "error")
            return render_template("ledger/journal_form.html", accounts=accounts, form=request.form)

        db.session.add(entry)
        db.session.flush()
        log_audit("create", "journal_entry", entry.id, f"Created manual journal entry #{entry.id} ({entry.memo or 'no memo'})")
        db.session.commit()
        flash("Journal entry posted.", "success")
        return redirect(url_for("ledger.journal_list"))

    return render_template("ledger/journal_form.html", accounts=accounts, form={}, today=date.today().isoformat())


@ledger_bp.route("/journal/<int:entry_id>")
@login_required
def journal_detail(entry_id):
    entry = scoped_or_404(JournalEntry, entry_id)
    return render_template("ledger/journal_detail.html", entry=entry)


@ledger_bp.route("/journal/<int:entry_id>/edit", methods=["GET", "POST"])
@login_required
def journal_edit(entry_id):
    entry = scoped_or_404(JournalEntry, entry_id)
    assert_period_open(entry.entry_date, entry.company_id)
    if entry.source_type not in ("manual", "transfer"):
        flash(
            f"This entry was auto-posted by a {entry.source_type} — edit or void that document instead "
            f"of the journal entry directly.", "error",
        )
        return redirect(url_for("ledger.journal_detail", entry_id=entry.id))

    accounts = scoped_query(Account).filter_by(is_active=True).order_by(Account.code).all()

    if request.method == "POST":
        entry.entry_date = datetime.strptime(request.form["entry_date"], "%Y-%m-%d").date()
        entry.reference_no = request.form.get("reference_no", "").strip() or None
        entry.memo = request.form.get("memo", "").strip() or None

        account_ids = request.form.getlist("account_id")
        debits = request.form.getlist("debit")
        credits = request.form.getlist("credit")
        line_memos = request.form.getlist("line_memo")
        tax_rates = request.form.getlist("tax_rate")
        tax_rates += [""] * (len(account_ids) - len(tax_rates))

        new_lines = []
        total_debit = 0
        total_credit = 0
        for acc_id, debit_raw, credit_raw, line_memo, tax_rate_raw in zip(account_ids, debits, credits, line_memos, tax_rates):
            if not acc_id:
                continue
            debit = float(debit_raw or 0)
            credit = float(credit_raw or 0)
            if debit == 0 and credit == 0:
                continue
            tax_rate = float(tax_rate_raw) if tax_rate_raw.strip() != "" else 0.0
            memo_text = line_memo.strip() or None
            result = _expand_line_with_tax(new_lines, int(acc_id), debit, credit, memo_text, tax_rate)
            if result is None:
                missing_code = VAT_RECEIVABLE_CODE if debit > 0 else VAT_PAYABLE_CODE
                flash(f"Can't apply tax: account {missing_code} is missing from the Chart of Accounts.", "error")
                return render_template("ledger/journal_form.html", accounts=accounts, form=request.form, editing=True, entry=entry)
            extra_debit, extra_credit = result
            total_debit += debit + extra_debit
            total_credit += credit + extra_credit

        if not new_lines:
            flash("Add at least one line with an amount.", "error")
            return render_template("ledger/journal_form.html", accounts=accounts, form=request.form, editing=True, entry=entry)

        if round(total_debit, 2) != round(total_credit, 2):
            flash(f"Entry does not balance: Debit {total_debit:.2f} vs Credit {total_credit:.2f}.", "error")
            return render_template("ledger/journal_form.html", accounts=accounts, form=request.form, editing=True, entry=entry)

        entry.lines.clear()  # cascade="all, delete-orphan" cleans up the old rows
        entry.lines.extend(new_lines)
        log_audit("edit", "journal_entry", entry.id, f"Edited manual journal entry #{entry.id}")
        db.session.commit()
        flash("Journal entry updated.", "success")
        return redirect(url_for("ledger.journal_detail", entry_id=entry.id))

    form = {
        "entry_date": entry.entry_date.isoformat(), "reference_no": entry.reference_no, "memo": entry.memo,
    }
    return render_template(
        "ledger/journal_form.html", accounts=accounts, form=form, editing=True, entry=entry,
        today=date.today().isoformat(),
    )


@ledger_bp.route("/journal/<int:entry_id>/delete", methods=["POST"])
@login_required
def journal_delete(entry_id):
    entry = scoped_or_404(JournalEntry, entry_id)
    assert_period_open(entry.entry_date, entry.company_id)
    if entry.source_type not in ("manual", "transfer"):
        flash(
            f"This entry was auto-posted by a {entry.source_type} — void that document instead of "
            f"deleting the journal entry directly.", "error",
        )
        return redirect(url_for("ledger.journal_detail", entry_id=entry.id))
    log_audit("delete", "journal_entry", entry.id, f"Deleted manual journal entry #{entry.id}")
    db.session.delete(entry)
    db.session.commit()
    flash("Journal entry deleted.", "success")
    return redirect(url_for("ledger.journal_list"))


# ── Reports ─────────────────────────────────────────────────────────

@ledger_bp.route("/general-ledger")
@login_required
def general_ledger():
    """Every account's activity for a date range, in one document — Trial Balance
    tells you the ending balances; this is the detail an auditor actually reads
    to see how each account got there. Accounts with no activity in range are
    skipped rather than shown empty."""
    start_raw = request.args.get("start_date")
    end_raw = request.args.get("end_date")
    start_date = datetime.strptime(start_raw, "%Y-%m-%d").date() if start_raw else date.today().replace(day=1)
    end_date = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else date.today()

    accounts = scoped_query(Account).filter_by(is_active=True).order_by(Account.code).all()
    sections = []
    for account in accounts:
        lines = (
            JournalLine.query.filter_by(account_id=account.id)
            .join(JournalEntry)
            .filter(JournalEntry.entry_date >= start_date, JournalEntry.entry_date <= end_date)
            .order_by(JournalEntry.entry_date, JournalEntry.id)
            .all()
        )
        if not lines:
            continue
        opening = account.balance(as_of=start_date - timedelta(days=1))
        running = opening
        rows = []
        for line in lines:
            movement = line.debit - line.credit
            if account.normal_balance == "credit":
                movement = -movement
            running += movement
            rows.append({"line": line, "running_balance": running})
        sections.append({"account": account, "opening": opening, "closing": running, "rows": rows})

    return render_template(
        "ledger/general_ledger.html", sections=sections,
        start_date=start_date.isoformat(), end_date=end_date.isoformat(),
    )


@ledger_bp.route("/trial-balance")
@login_required
def trial_balance():
    as_of_raw = request.args.get("as_of")
    as_of = datetime.strptime(as_of_raw, "%Y-%m-%d").date() if as_of_raw else date.today()

    accounts = scoped_query(Account).filter_by(is_active=True).order_by(Account.code).all()
    rows = []
    total_debit = 0
    total_credit = 0
    for account in accounts:
        balance = account.balance(as_of=as_of)
        if balance == 0:
            continue
        if account.normal_balance == "debit":
            debit, credit = (balance, 0) if balance >= 0 else (0, -balance)
        else:
            credit, debit = (balance, 0) if balance >= 0 else (0, -balance)
        total_debit += debit
        total_credit += credit
        rows.append({"account": account, "debit": debit, "credit": credit})

    return render_template(
        "ledger/trial_balance.html",
        rows=rows,
        total_debit=total_debit,
        total_credit=total_credit,
        as_of=as_of.isoformat(),
    )
