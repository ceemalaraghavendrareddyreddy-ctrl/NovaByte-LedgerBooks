"""Period-end revaluation of open foreign-currency AR/AP — the real accounting gap
between "we record a gain/loss when a foreign invoice/bill is actually paid" (already
handled in sales.post_payment / purchases.post_vendor_payment) and "our foreign-currency
receivables/payables should be restated to today's rate even while still open", which
is what a month-end close under both IAS 21 and Mauritius practice actually requires.

Scope: AR (Invoice) and AP (Bill) only. A foreign-currency bank account's balance isn't
revalued here — journal lines only ever store base-currency amounts (see JournalLine),
so there's no tracked foreign face value to revalue it against without a schema change
that's a separate, bigger decision than "close the AR/AP gap".

How it works: each Invoice/Bill carries an exchange_rate it was booked at, and (once
revalued at least once) a revalued_exchange_rate — see Invoice.carrying_exchange_rate.
Revaluing a currency group restates every open document in it from its current carrying
rate to a new rate, posts the net difference as Unrealized Gain/Loss on Exchange, and
then remembers the new rate as each document's carrying rate — so the NEXT revaluation
(or the eventual real payment) only ever measures the movement since this one, never
double-counting what was already recognized here.
"""
from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import db
from app.audit import log_audit
from app.auth import current_company, current_company_id
from app.models import Account, Bill, Invoice, JournalEntry, JournalLine
from app.scoping import scoped_query

revaluation_bp = Blueprint("revaluation", __name__, url_prefix="/revaluation")

AR_ACCOUNT_CODE = "1200"
AP_ACCOUNT_CODE = "2000"
UNREALIZED_FX_CODE = "4925"


def get_account_or_400(code, label):
    from flask import abort
    account = Account.query.filter_by(company_id=current_company_id(), code=code).first()
    if not account:
        abort(400, f"Required account {code} ({label}) is missing from the Chart of Accounts.")
    return account


def _open_foreign_invoices():
    base = current_company().base_currency
    invoices = (
        scoped_query(Invoice)
        .filter(Invoice.status != "void", Invoice.currency != base)
        .all()
    )
    return [inv for inv in invoices if inv.balance_due > 0]


def _open_foreign_bills():
    base = current_company().base_currency
    bills = (
        scoped_query(Bill)
        .filter(Bill.status != "void", Bill.currency != base)
        .all()
    )
    return [bill for bill in bills if bill.balance_due > 0]


def _group_by_currency(documents):
    """{currency: {"balance": total face-value, "carrying_value": total booked base value,
    "count": how many documents}} — the carrying_value / balance ratio is the group's
    current weighted-average rate, shown so the user has a sense of where they're
    revaluing FROM before typing the new rate."""
    groups = {}
    for doc in documents:
        g = groups.setdefault(doc.currency, {"balance": 0.0, "carrying_value": 0.0, "count": 0})
        g["balance"] = round(g["balance"] + doc.balance_due, 2)
        g["carrying_value"] = round(g["carrying_value"] + doc.balance_due * doc.carrying_exchange_rate, 2)
        g["count"] += 1
    return groups


@revaluation_bp.route("", methods=["GET"])
@login_required
def index():
    ar_groups = _group_by_currency(_open_foreign_invoices())
    ap_groups = _group_by_currency(_open_foreign_bills())
    return render_template(
        "revaluation/index.html", ar_groups=ar_groups, ap_groups=ap_groups,
        base_currency=current_company().base_currency, today=date.today().isoformat(),
    )


@revaluation_bp.route("", methods=["POST"])
@login_required
def run():
    revaluation_date_raw = request.form.get("revaluation_date")
    revaluation_date = date.fromisoformat(revaluation_date_raw) if revaluation_date_raw else date.today()

    entry = JournalEntry(
        company_id=current_company_id(),
        entry_date=revaluation_date,
        memo=f"FX revaluation as of {revaluation_date.isoformat()}",
        source_type="fx_revaluation",
        created_by=current_user.id,
    )

    touched_currencies = []

    invoices_by_currency = {}
    for inv in _open_foreign_invoices():
        invoices_by_currency.setdefault(inv.currency, []).append(inv)

    bills_by_currency = {}
    for bill in _open_foreign_bills():
        bills_by_currency.setdefault(bill.currency, []).append(bill)

    for key, raw_value in request.form.items():
        if not raw_value or not raw_value.strip():
            continue
        try:
            new_rate = float(raw_value)
        except ValueError:
            continue
        if new_rate <= 0:
            continue

        if key.startswith("ar_rate_"):
            currency = key[len("ar_rate_"):]
            invoices = invoices_by_currency.get(currency, [])
            if not invoices:
                continue
            diff_total = 0.0
            for inv in invoices:
                diff_total += round(inv.balance_due * new_rate - inv.balance_due * inv.carrying_exchange_rate, 2)
                inv.revalued_exchange_rate = new_rate
            diff_total = round(diff_total, 2)
            if diff_total:
                ar_account = get_account_or_400(AR_ACCOUNT_CODE, "Accounts Receivable")
                fx_account = get_account_or_400(UNREALIZED_FX_CODE, "Unrealized Gain/Loss on Exchange")
                if diff_total > 0:
                    entry.lines.append(JournalLine(account=ar_account, debit=diff_total, credit=0, memo=f"AR revaluation - {currency}"))
                    entry.lines.append(JournalLine(account=fx_account, debit=0, credit=diff_total, memo=f"Unrealized FX gain - {currency} AR"))
                else:
                    entry.lines.append(JournalLine(account=ar_account, debit=0, credit=-diff_total, memo=f"AR revaluation - {currency}"))
                    entry.lines.append(JournalLine(account=fx_account, debit=-diff_total, credit=0, memo=f"Unrealized FX loss - {currency} AR"))
                touched_currencies.append(f"AR/{currency}")

        elif key.startswith("ap_rate_"):
            currency = key[len("ap_rate_"):]
            bills = bills_by_currency.get(currency, [])
            if not bills:
                continue
            diff_total = 0.0
            for bill in bills:
                diff_total += round(bill.balance_due * new_rate - bill.balance_due * bill.carrying_exchange_rate, 2)
                bill.revalued_exchange_rate = new_rate
            diff_total = round(diff_total, 2)
            if diff_total:
                ap_account = get_account_or_400(AP_ACCOUNT_CODE, "Accounts Payable")
                fx_account = get_account_or_400(UNREALIZED_FX_CODE, "Unrealized Gain/Loss on Exchange")
                if diff_total > 0:
                    # AP grew in base-currency terms — we now owe more, a loss.
                    entry.lines.append(JournalLine(account=fx_account, debit=diff_total, credit=0, memo=f"Unrealized FX loss - {currency} AP"))
                    entry.lines.append(JournalLine(account=ap_account, debit=0, credit=diff_total, memo=f"AP revaluation - {currency}"))
                else:
                    entry.lines.append(JournalLine(account=ap_account, debit=-diff_total, credit=0, memo=f"AP revaluation - {currency}"))
                    entry.lines.append(JournalLine(account=fx_account, debit=0, credit=-diff_total, memo=f"Unrealized FX gain - {currency} AP"))
                touched_currencies.append(f"AP/{currency}")

    if not entry.lines:
        flash("No revaluation was posted — enter a new rate for at least one currency group.", "error")
        return redirect(url_for("revaluation.index"))

    db.session.add(entry)
    db.session.flush()
    log_audit("create", "fx_revaluation", entry.id, f"FX revaluation posted for {', '.join(touched_currencies)}")
    db.session.commit()
    flash(f"Revaluation posted for {', '.join(touched_currencies)}.", "success")
    return redirect(url_for("ledger.journal_detail", entry_id=entry.id))
