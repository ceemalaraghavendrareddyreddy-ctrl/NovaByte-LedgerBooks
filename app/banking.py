import csv
import io
from datetime import date, datetime, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import db
from app.audit import log_audit
from app.auth import current_company_id
from app.models import (
    Account, BankImportLine, BankReconciliation, BankStatementImport, Deposit, DepositLine, JournalEntry,
    JournalLine, Payment,
)
from app.scoping import scoped_or_404, scoped_query

banking_bp = Blueprint("banking", __name__, url_prefix="/banking")

UNDEPOSITED_FUNDS_CODE = "1100"


def cash_accounts():
    return scoped_query(Account).filter_by(account_type="Asset", is_active=True, subtype="Cash and Cash Equivalents").order_by(Account.code).all()


def account_beginning_balance(account):
    """Opening balance plus every line already permanently cleared by a past reconciliation."""
    cleared_lines = JournalLine.query.filter_by(account_id=account.id, is_cleared=True).all()
    total = float(account.opening_balance)
    for line in cleared_lines:
        movement = float(line.debit) - float(line.credit)
        if account.normal_balance == "credit":
            movement = -movement
        total += movement
    return total


@banking_bp.route("/transfer", methods=["GET", "POST"])
@login_required
def transfer():
    accounts = scoped_query(Account).filter_by(is_active=True).order_by(Account.code).all()

    if request.method == "POST":
        from_id = int(request.form["from_account_id"])
        to_id = int(request.form["to_account_id"])
        amount = float(request.form["amount"])
        transfer_date = datetime.strptime(request.form["transfer_date"], "%Y-%m-%d").date()
        memo = request.form.get("memo", "").strip() or None

        if from_id == to_id:
            flash("From and To accounts must be different.", "error")
            return render_template("banking/transfer.html", accounts=accounts, form=request.form, today=date.today().isoformat())
        if amount <= 0:
            flash("Amount must be greater than zero.", "error")
            return render_template("banking/transfer.html", accounts=accounts, form=request.form, today=date.today().isoformat())

        from_account = scoped_query(Account).filter_by(id=from_id).first()
        to_account = scoped_query(Account).filter_by(id=to_id).first()
        if not from_account or not to_account:
            abort(404)

        entry = JournalEntry(
            company_id=current_company_id(),
            entry_date=transfer_date,
            memo=f"Transfer: {from_account.name} → {to_account.name}" + (f" - {memo}" if memo else ""),
            source_type="transfer",
            created_by=current_user.id,
        )
        entry.lines.append(JournalLine(account_id=to_id, debit=amount, credit=0, memo=memo))
        entry.lines.append(JournalLine(account_id=from_id, debit=0, credit=amount, memo=memo))
        db.session.add(entry)
        db.session.flush()
        log_audit("create", "transfer", entry.id, f"Transferred {amount:.2f} from {from_account.name} to {to_account.name}")
        db.session.commit()
        flash(f"Transferred {amount:.2f} from {from_account.name} to {to_account.name}.", "success")
        return redirect(url_for("ledger.journal_detail", entry_id=entry.id))

    return render_template("banking/transfer.html", accounts=accounts, form={}, today=date.today().isoformat())


@banking_bp.route("/make-deposit", methods=["GET", "POST"])
@login_required
def make_deposit():
    undeposited = scoped_query(Account).filter_by(code=UNDEPOSITED_FUNDS_CODE).first()
    if not undeposited:
        flash("Undeposited Funds account (1100) is missing from the Chart of Accounts.", "error")
        return redirect(url_for("dashboard.index"))

    destination_accounts = cash_accounts()

    if request.method == "POST":
        payment_ids = [int(i) for i in request.form.getlist("payment_id")]
        if not payment_ids:
            flash("Select at least one payment to deposit.", "error")
            return redirect(url_for("banking.make_deposit"))

        # Re-check none of these were deposited by someone else between page load and submit.
        payments = scoped_query(Payment).filter(Payment.id.in_(payment_ids)).all()
        already_deposited = [p for p in payments if p.is_deposited]
        if already_deposited:
            flash(f"{len(already_deposited)} of the selected payments were already deposited elsewhere — refresh and try again.", "error")
            return redirect(url_for("banking.make_deposit"))

        destination_id = int(request.form["destination_account_id"])
        deposit_date = datetime.strptime(request.form["deposit_date"], "%Y-%m-%d").date()
        memo = request.form.get("memo", "").strip() or None

        deposit = Deposit(
            company_id=current_company_id(),
            deposit_date=deposit_date, destination_account_id=destination_id, memo=memo, created_by=current_user.id,
        )
        for p in payments:
            deposit.lines.append(DepositLine(payment_id=p.id, amount=p.amount))
        db.session.add(deposit)
        db.session.flush()

        total = deposit.total
        entry = JournalEntry(
            company_id=current_company_id(),
            entry_date=deposit_date,
            memo=f"Deposit of {len(payments)} payment(s)" + (f" - {memo}" if memo else ""),
            source_type="deposit", source_id=deposit.id, created_by=current_user.id,
        )
        entry.lines.append(JournalLine(account_id=destination_id, debit=total, credit=0))
        entry.lines.append(JournalLine(account_id=undeposited.id, debit=0, credit=total))
        db.session.add(entry)
        db.session.flush()
        deposit.journal_entry_id = entry.id

        destination_account_name = scoped_query(Account).filter_by(id=destination_id).first().name
        log_audit("create", "deposit", deposit.id, f"Deposited {total:.2f} ({len(payments)} payments) to {destination_account_name}")
        db.session.commit()
        flash(f"Deposited {total:.2f} to {destination_account_name}.", "success")
        return redirect(url_for("banking.deposit_detail", deposit_id=deposit.id))

    undeposited_payments = (
        scoped_query(Payment).filter_by(deposit_account_id=undeposited.id)
        .outerjoin(DepositLine, DepositLine.payment_id == Payment.id)
        .filter(DepositLine.id.is_(None))
        .order_by(Payment.payment_date)
        .all()
    )
    return render_template(
        "banking/make_deposit.html", undeposited_payments=undeposited_payments,
        destination_accounts=destination_accounts, today=date.today().isoformat(),
    )


@banking_bp.route("/deposits")
@login_required
def deposit_list():
    deposits = scoped_query(Deposit).order_by(Deposit.deposit_date.desc(), Deposit.id.desc()).all()
    return render_template("banking/deposits.html", deposits=deposits)


@banking_bp.route("/deposits/<int:deposit_id>")
@login_required
def deposit_detail(deposit_id):
    deposit = scoped_or_404(Deposit, deposit_id)
    return render_template("banking/deposit_detail.html", deposit=deposit)


def _line_movement(line, account):
    """Signed effect of a journal line in the same convention as a bank statement: positive = money
    in, negative = money out — regardless of the account's own normal-balance direction.
    """
    movement = float(line.debit) - float(line.credit)
    return movement if account.normal_balance == "debit" else -movement


@banking_bp.route("/import", methods=["GET", "POST"])
@login_required
def import_start():
    accounts = cash_accounts()

    if request.method == "POST":
        account = scoped_or_404(Account, int(request.form["account_id"]))
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Choose a CSV file first.", "error")
            return render_template("banking/import_start.html", accounts=accounts)

        try:
            text = file.read().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            reader.fieldnames = [f.strip().lower() for f in (reader.fieldnames or [])]
            rows = list(reader)
        except Exception as e:
            flash(f"Couldn't read that file as CSV: {e}", "error")
            return render_template("banking/import_start.html", accounts=accounts)

        if not rows or "date" not in reader.fieldnames or "amount" not in reader.fieldnames:
            flash("CSV must have at least 'Date' and 'Amount' columns (an optional 'Description' column too).", "error")
            return render_template("banking/import_start.html", accounts=accounts)

        # Candidate pool for auto-matching: everything not yet cleared on this account.
        uncleared = (
            JournalLine.query.filter_by(account_id=account.id, is_cleared=False)
            .join(JournalEntry)
            .all()
        )

        batch = BankStatementImport(
            company_id=current_company_id(), account_id=account.id, filename=file.filename, imported_by=current_user.id,
        )
        db.session.add(batch)

        imported_count = 0
        for row in rows:
            date_raw = (row.get("date") or "").strip()
            amount_raw = (row.get("amount") or "").strip().replace(",", "")
            if not date_raw or not amount_raw:
                continue
            try:
                stmt_date = _parse_flexible_date(date_raw)
                amount = float(amount_raw)
            except ValueError:
                continue

            match = None
            for line in uncleared:
                if line.reconciliation_id:
                    continue  # already claimed by an in-progress reconciliation session
                if abs(_line_movement(line, account) - amount) < 0.005 and abs((line.entry.entry_date - stmt_date).days) <= 5:
                    match = line
                    break

            import_line = BankImportLine(
                import_batch=batch, stmt_date=stmt_date, description=(row.get("description") or "").strip(),
                amount=amount, status="matched" if match else "unmatched",
                matched_journal_line_id=match.id if match else None,
            )
            if match:
                uncleared.remove(match)  # one statement line claims at most one journal line
            db.session.add(import_line)
            imported_count += 1

        db.session.flush()
        log_audit("import", "bank_statement_import", batch.id, f"Imported {imported_count} statement lines for {account.code} - {account.name}")
        db.session.commit()
        flash(f"Imported {imported_count} lines from '{file.filename}'.", "success")
        return redirect(url_for("banking.import_review", import_id=batch.id))

    return render_template("banking/import_start.html", accounts=accounts)


def _parse_flexible_date(raw):
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date format: {raw}")


@banking_bp.route("/import/<int:import_id>")
@login_required
def import_review(import_id):
    batch = scoped_or_404(BankStatementImport, import_id)
    accounts = scoped_query(Account).filter_by(is_active=True).order_by(Account.code).all()
    return render_template("banking/import_review.html", batch=batch, accounts=accounts)


@banking_bp.route("/import/<int:import_id>/process", methods=["POST"])
@login_required
def import_process(import_id):
    batch = scoped_or_404(BankStatementImport, import_id)
    confirmed = 0
    created = 0

    for line in batch.lines:
        if line.status == "matched" and request.form.get(f"confirm_{line.id}") == "1":
            line.matched_journal_line.is_cleared = True
            line.status = "created"  # reuse "created" to mean "fully resolved" in this UI
            confirmed += 1
        elif line.status == "unmatched":
            category_id = request.form.get(f"category_{line.id}")
            if not category_id:
                continue
            entry = JournalEntry(
                company_id=current_company_id(),
                entry_date=line.stmt_date,
                memo=line.description or "Bank import",
                source_type="bank_import", source_id=batch.id, created_by=current_user.id,
            )
            amount = abs(float(line.amount))
            if float(line.amount) >= 0:
                entry.lines.append(JournalLine(account_id=batch.account_id, debit=amount, credit=0))
                entry.lines.append(JournalLine(account_id=int(category_id), debit=0, credit=amount))
            else:
                entry.lines.append(JournalLine(account_id=batch.account_id, debit=0, credit=amount))
                entry.lines.append(JournalLine(account_id=int(category_id), debit=amount, credit=0))
            db.session.add(entry)
            db.session.flush()
            # The bank-account leg came straight off the real statement — it's inherently cleared.
            for jl in entry.lines:
                if jl.account_id == batch.account_id:
                    jl.is_cleared = True
            line.created_journal_entry_id = entry.id
            line.status = "created"
            created += 1

    log_audit("process", "bank_statement_import", batch.id, f"Resolved import: {confirmed} matches confirmed, {created} new transactions created")
    db.session.commit()
    flash(f"{confirmed} matches confirmed, {created} new transactions created.", "success")
    return redirect(url_for("banking.import_review", import_id=batch.id))


@banking_bp.route("/reconcile", methods=["GET", "POST"])
@login_required
def reconcile_start():
    accounts = cash_accounts()

    if request.method == "POST":
        account = scoped_or_404(Account, int(request.form["account_id"]))
        statement_date = datetime.strptime(request.form["statement_date"], "%Y-%m-%d").date()
        statement_ending_balance = float(request.form["statement_ending_balance"])

        recon = BankReconciliation(
            company_id=current_company_id(),
            account_id=account.id,
            statement_date=statement_date,
            statement_ending_balance=statement_ending_balance,
            beginning_balance=account_beginning_balance(account),
            created_by=current_user.id,
        )
        db.session.add(recon)
        db.session.commit()
        return redirect(url_for("banking.reconcile_work", recon_id=recon.id))

    return render_template("banking/start.html", accounts=accounts, today=date.today().isoformat())


@banking_bp.route("/reconcile/<int:recon_id>", methods=["GET", "POST"])
@login_required
def reconcile_work(recon_id):
    recon = scoped_or_404(BankReconciliation, recon_id)
    if recon.is_completed:
        return redirect(url_for("banking.reconcile_detail", recon_id=recon.id))

    if request.method == "POST":
        action = request.form.get("action")
        checked_ids = set(int(i) for i in request.form.getlist("line_id"))

        # Any line currently tied to this session but unchecked now: release it.
        for line in JournalLine.query.filter_by(reconciliation_id=recon.id, is_cleared=False).all():
            if line.id not in checked_ids:
                line.reconciliation_id = None

        # Every checked line (that belongs to this account and isn't already permanently cleared): claim it.
        if checked_ids:
            candidate_lines = JournalLine.query.filter(
                JournalLine.id.in_(checked_ids), JournalLine.account_id == recon.account_id,
                JournalLine.is_cleared == False,  # noqa: E712
            ).all()
            for line in candidate_lines:
                line.reconciliation_id = recon.id

        db.session.commit()

        if action == "finish":
            if recon.difference != 0:
                flash(f"Can't finish: still out of balance by {recon.difference:.2f}.", "error")
            else:
                for line in JournalLine.query.filter_by(reconciliation_id=recon.id, is_cleared=False).all():
                    line.is_cleared = True
                recon.is_completed = True
                recon.completed_at = datetime.utcnow()
                recon.completed_by = current_user.id
                log_audit(
                    "reconcile", "bank_reconciliation", recon.id,
                    f"Completed reconciliation of {recon.account.code} - {recon.account.name} as of {recon.statement_date}",
                )
                db.session.commit()
                flash("Reconciliation completed — account matches your statement.", "success")
                return redirect(url_for("banking.reconcile_detail", recon_id=recon.id))
        else:
            flash("Progress saved.", "success")

        return redirect(url_for("banking.reconcile_work", recon_id=recon.id))

    uncleared_lines = (
        JournalLine.query.filter_by(account_id=recon.account_id, is_cleared=False)
        .join(JournalEntry)
        .filter(JournalEntry.entry_date <= recon.statement_date)
        .order_by(JournalEntry.entry_date)
        .all()
    )
    return render_template("banking/work.html", recon=recon, uncleared_lines=uncleared_lines)


@banking_bp.route("/reconcile/<int:recon_id>/cancel", methods=["POST"])
@login_required
def reconcile_cancel(recon_id):
    recon = scoped_or_404(BankReconciliation, recon_id)
    if recon.is_completed:
        flash("Can't cancel a completed reconciliation.", "error")
        return redirect(url_for("banking.reconcile_detail", recon_id=recon.id))
    for line in JournalLine.query.filter_by(reconciliation_id=recon.id).all():
        line.reconciliation_id = None
    db.session.delete(recon)
    db.session.commit()
    flash("Reconciliation cancelled.", "success")
    return redirect(url_for("banking.reconcile_history"))


@banking_bp.route("/reconciliations")
@login_required
def reconcile_history():
    reconciliations = scoped_query(BankReconciliation).order_by(BankReconciliation.id.desc()).all()
    return render_template("banking/history.html", reconciliations=reconciliations)


@banking_bp.route("/reconciliations/<int:recon_id>")
@login_required
def reconcile_detail(recon_id):
    recon = scoped_or_404(BankReconciliation, recon_id)
    cleared_lines = sorted(recon.cleared_lines, key=lambda ln: ln.entry.entry_date)
    return render_template("banking/detail.html", recon=recon, cleared_lines=cleared_lines)
