"""Ask-your-books Q&A — deliberately mocked, same rule as app/ai_suggest.py:
no external model call, no cost, and it's honest about the limitation that
comes with that. It understands a fixed set of question *shapes* (revenue,
expenses, profit, who owes us, who we owe, cash on hand — each with an
optional time period and, for expenses, an optional category) via keyword
matching, not real natural-language understanding. Anything it can't match
says so plainly and shows example questions, rather than guessing.

The part that ISN'T mocked: every number it returns comes from a real query
against this company's actual ledger — nothing here is invented or
approximated. That's the one property worth keeping even after this is
eventually replaced with a real model call: the model's job would be
understanding the question, never inventing the answer.
"""
import re
from datetime import date, timedelta

from flask import Blueprint, render_template, request
from flask_login import login_required

from app.auth import current_company_id
from app.models import Account, Bill, Customer, Invoice, Vendor
from app.scoping import scoped_query

ai_qa_bp = Blueprint("ai_qa", __name__, url_prefix="/ai")

CASH_SUBTYPE = "Cash and Cash Equivalents"

EXAMPLE_QUESTIONS = [
    "What's our net profit this month?",
    "How much did we spend on rent this year?",
    "Who owes us money?",
    "Who do we owe?",
    "How much cash do we have on hand?",
    "What were our total sales last month?",
]


def _period_from_text(text):
    """Returns (start_date, end_date, label) for whatever period phrase is in
    the question — defaults to this month if none is recognized."""
    today = date.today()
    if "today" in text:
        return today, today, "today"
    if "this week" in text:
        return today - timedelta(days=today.weekday()), today, "this week"
    if "last month" in text:
        first_this_month = today.replace(day=1)
        last_month_end = first_this_month - timedelta(days=1)
        return last_month_end.replace(day=1), last_month_end, "last month"
    if "this quarter" in text:
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        return today.replace(month=q_start_month, day=1), today, "this quarter"
    if "this year" in text or "ytd" in text:
        return today.replace(month=1, day=1), today, "this year"
    if "last 30 days" in text or "past 30 days" in text:
        return today - timedelta(days=30), today, "the last 30 days"
    if "this month" in text:
        return today.replace(day=1), today, "this month"
    # default
    return today.replace(day=1), today, "this month"


def _find_expense_account(text):
    """Matches a category word in the question against an actual expense
    account's name — e.g. "rent" -> the Rent Expense account, whatever it's
    actually called in this company's Chart of Accounts."""
    accounts = scoped_query(Account).filter_by(account_type="Expense", is_active=True).all()
    for account in accounts:
        name_words = re.findall(r"[a-z]+", account.name.lower())
        for word in name_words:
            if len(word) > 3 and word in text:
                return account
    return None


def answer_question(question, company_id):
    """Returns a plain-text answer, or None if the question didn't match any
    supported shape (the caller shows the example list in that case)."""
    text = question.lower()
    start, end, label = _period_from_text(text)

    if any(w in text for w in ("profit", "net income", "how are we doing")):
        from app.reports import period_movement

        income_accounts = scoped_query(Account).filter_by(account_type="Income", is_active=True).all()
        expense_accounts = scoped_query(Account).filter_by(account_type="Expense", is_active=True).all()
        income = sum((period_movement(a, start, end) for a in income_accounts), start=0.0)
        expense = sum((period_movement(a, start, end) for a in expense_accounts), start=0.0)
        net = income - expense
        verb = "profit" if net >= 0 else "loss"
        return (
            f"Net {verb} for {label} ({start.strftime('%d %b')} – {end.strftime('%d %b %Y')}): "
            f"MUR {abs(net):,.2f}. (Income MUR {income:,.2f} − Expenses MUR {expense:,.2f})"
        )

    if any(w in text for w in ("spend", "spent", "expense", "cost")):
        from app.reports import period_movement

        account = _find_expense_account(text)
        if account:
            amount = period_movement(account, start, end)
            return (
                f"{account.name} for {label} ({start.strftime('%d %b')} – {end.strftime('%d %b %Y')}): "
                f"MUR {amount:,.2f}."
            )
        expense_accounts = scoped_query(Account).filter_by(account_type="Expense", is_active=True).all()
        total = sum((period_movement(a, start, end) for a in expense_accounts), start=0.0)
        return f"Total expenses for {label}: MUR {total:,.2f} (no specific category recognized — showing the overall total)."

    if any(w in text for w in ("sales", "revenue", "income")) and "who" not in text:
        from app.reports import period_movement

        income_accounts = scoped_query(Account).filter_by(account_type="Income", is_active=True).all()
        total = sum((period_movement(a, start, end) for a in income_accounts), start=0.0)
        return f"Total sales for {label} ({start.strftime('%d %b')} – {end.strftime('%d %b %Y')}): MUR {total:,.2f}."

    if any(w in text for w in ("owe us", "owes us", "receivable", "outstanding invoice")):
        customers = scoped_query(Customer).filter_by(is_active=True).all()
        open_invoices = scoped_query(Invoice).filter(Invoice.status.in_(("open", "partial"))).all()
        totals = {}
        for inv in open_invoices:
            totals[inv.customer_id] = totals.get(inv.customer_id, 0.0) + inv.balance_due
        total_ar = sum(totals.values(), start=0.0)
        if not totals:
            return "Nobody currently owes you anything — no open or partial invoices."
        top = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:3]
        customers_by_id = {c.id: c for c in customers}
        top_lines = "; ".join(f"{customers_by_id[cid].name}: MUR {amt:,.2f}" for cid, amt in top if cid in customers_by_id)
        return f"Total outstanding receivables: MUR {total_ar:,.2f}. Largest: {top_lines}."

    if any(w in text for w in ("owe", "payable", "bills due")) and "us" not in text:
        open_bills = scoped_query(Bill).filter(Bill.status.in_(("open", "partial"))).all()
        totals = {}
        for bill in open_bills:
            totals[bill.vendor_id] = totals.get(bill.vendor_id, 0.0) + bill.balance_due
        total_ap = sum(totals.values(), start=0.0)
        if not totals:
            return "You don't currently owe anything — no open or partial bills."
        vendors = {v.id: v for v in scoped_query(Vendor).all()}
        top = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:3]
        top_lines = "; ".join(f"{vendors[vid].name}: MUR {amt:,.2f}" for vid, amt in top if vid in vendors)
        return f"Total outstanding payables: MUR {total_ap:,.2f}. Largest: {top_lines}."

    if any(w in text for w in ("cash", "bank balance", "how much money")):
        cash_accounts = scoped_query(Account).filter_by(account_type="Asset", is_active=True, subtype=CASH_SUBTYPE).all()
        rows = [(a, float(a.balance())) for a in cash_accounts]
        total = sum((amt for _, amt in rows), start=0.0)
        breakdown = "; ".join(f"{a.name}: MUR {amt:,.2f}" for a, amt in rows) if rows else "no cash/bank accounts set up"
        return f"Cash on hand across all accounts: MUR {total:,.2f} ({breakdown})."

    return None


@ai_qa_bp.route("/ask")
@login_required
def ask():
    question = request.args.get("q", "").strip()
    answer = answer_question(question, current_company_id()) if question else None
    return render_template(
        "ai/ask.html", question=question, answer=answer,
        examples=EXAMPLE_QUESTIONS, no_match=bool(question) and answer is None,
    )
