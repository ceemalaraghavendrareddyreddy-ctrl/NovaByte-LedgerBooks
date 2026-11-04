"""Ask-your-books chat, backed by Claude.

The model's job is understanding the question and wording the answer; every
figure it states comes from one of the read-only tools below, each of which runs
a real query scoped to the signed-in user's active company (same rule as
app/ai_qa.py, which stays as the keyword fallback when no API key is set).

Nothing here can create, change or delete anything: the tools only read, and
each one checks the user's module permission before returning data.
"""
import json
import os
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta

import anthropic
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from app.auth import current_company, current_company_id
from app.models import Account, Bill, Customer, Invoice, Vendor
from app.scoping import scoped_query

ai_chat_bp = Blueprint("ai_chat", __name__, url_prefix="/ai/chat")

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
MAX_TOOL_ROUNDS = 6
MAX_HISTORY_TURNS = 10
MAX_QUESTION_CHARS = 1000
HOURLY_LIMIT = 40  # questions per user per hour — a cost guard, not a security boundary

_recent = defaultdict(deque)

EXAMPLES = [
    "Who owes us money, and who is overdue?",
    "What was our profit last month?",
    "How much do we owe suppliers?",
    "Which customer bought the most this year?",
    "How much cash do we have?",
    "List unpaid invoices over 30 days old.",
]


def api_configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


# --------------------------------------------------------------------------- tools

def _d(value, default=None):
    if not value:
        return default
    return datetime.strptime(value, "%Y-%m-%d").date()


def _allowed(module):
    return current_user.can_use_module(module)


def _denied(module):
    return {"error": f"This user does not have access to the {module} module, so this information can't be shown."}


def _money(x):
    return round(float(x), 2)


def tool_profit_and_loss(start_date, end_date):
    from app.reports import period_movement

    start, end = _d(start_date), _d(end_date)
    out = {"period": f"{start} to {end}", "currency": current_company().base_currency}
    for key, acct_type in (("income", "Income"), ("expenses", "Expense")):
        rows = []
        for a in scoped_query(Account).filter_by(account_type=acct_type, is_active=True).all():
            amount = period_movement(a, start, end)
            if amount:
                rows.append({"account": a.name, "amount": _money(amount)})
        out[key] = sorted(rows, key=lambda r: -abs(r["amount"]))
        out[f"total_{key}"] = _money(sum(r["amount"] for r in rows))
    out["net_profit"] = _money(out["total_income"] - out["total_expenses"])
    return out


def tool_account_balances(name_contains, as_of=None):
    as_of = _d(as_of)
    needle = (name_contains or "").lower()
    rows = []
    for a in scoped_query(Account).filter_by(is_active=True).all():
        if needle in a.name.lower() or needle in (a.subtype or "").lower() or needle in (a.code or "").lower():
            rows.append({"code": a.code, "account": a.name, "type": a.account_type, "balance": _money(a.balance(as_of))})
    return {"currency": current_company().base_currency, "accounts": rows[:40]}


def tool_receivables():
    if not _allowed("sales"):
        return _denied("sales")
    today = date.today()
    per = defaultdict(lambda: {"outstanding": 0.0, "overdue": 0.0, "invoices": 0, "oldest_days_overdue": 0})
    customers = {c.id: c.name for c in scoped_query(Customer).all()}
    for inv in scoped_query(Invoice).filter(Invoice.status.in_(("open", "partial"))).all():
        key = (customers.get(inv.customer_id, "?"), inv.currency)
        row = per[key]
        row["outstanding"] += inv.balance_due
        row["invoices"] += 1
        if inv.due_date < today:
            row["overdue"] += inv.balance_due
            row["oldest_days_overdue"] = max(row["oldest_days_overdue"], (today - inv.due_date).days)
    rows = [
        {"customer": c, "currency": cur, "outstanding": _money(v["outstanding"]), "overdue": _money(v["overdue"]),
         "open_invoices": v["invoices"], "oldest_days_overdue": v["oldest_days_overdue"]}
        for (c, cur), v in per.items()
    ]
    return {"as_of": str(today), "customers": sorted(rows, key=lambda r: -r["outstanding"])[:40]}


def tool_payables():
    if not _allowed("purchases"):
        return _denied("purchases")
    today = date.today()
    per = defaultdict(lambda: {"outstanding": 0.0, "overdue": 0.0, "bills": 0})
    vendors = {v.id: v.name for v in scoped_query(Vendor).all()}
    for bill in scoped_query(Bill).filter(Bill.status.in_(("open", "partial"))).all():
        key = (vendors.get(bill.vendor_id, "?"), bill.currency)
        row = per[key]
        row["outstanding"] += bill.balance_due
        row["bills"] += 1
        if bill.due_date < today:
            row["overdue"] += bill.balance_due
    rows = [
        {"supplier": v, "currency": cur, "outstanding": _money(d["outstanding"]), "overdue": _money(d["overdue"]),
         "open_bills": d["bills"]}
        for (v, cur), d in per.items()
    ]
    return {"as_of": str(today), "suppliers": sorted(rows, key=lambda r: -r["outstanding"])[:40]}


def tool_list_invoices(status=None, customer_name=None, date_from=None, date_to=None, limit=20):
    if not _allowed("sales"):
        return _denied("sales")
    q = scoped_query(Invoice)
    if status:
        q = q.filter(Invoice.status == status)
    if date_from:
        q = q.filter(Invoice.invoice_date >= _d(date_from))
    if date_to:
        q = q.filter(Invoice.invoice_date <= _d(date_to))
    customers = {c.id: c.name for c in scoped_query(Customer).all()}
    rows = []
    for inv in q.order_by(Invoice.invoice_date.desc()).all():
        name = customers.get(inv.customer_id, "?")
        if customer_name and customer_name.lower() not in name.lower():
            continue
        rows.append({"invoice": inv.invoice_no, "customer": name, "date": str(inv.invoice_date), "due": str(inv.due_date),
                     "status": inv.status, "currency": inv.currency, "total": _money(inv.total),
                     "balance_due": _money(inv.balance_due), "days_overdue": inv.days_overdue})
        if len(rows) >= min(int(limit or 20), 50):
            break
    return {"invoices": rows}


def tool_list_bills(status=None, supplier_name=None, date_from=None, date_to=None, limit=20):
    if not _allowed("purchases"):
        return _denied("purchases")
    q = scoped_query(Bill)
    if status:
        q = q.filter(Bill.status == status)
    if date_from:
        q = q.filter(Bill.bill_date >= _d(date_from))
    if date_to:
        q = q.filter(Bill.bill_date <= _d(date_to))
    vendors = {v.id: v.name for v in scoped_query(Vendor).all()}
    rows = []
    for b in q.order_by(Bill.bill_date.desc()).all():
        name = vendors.get(b.vendor_id, "?")
        if supplier_name and supplier_name.lower() not in name.lower():
            continue
        rows.append({"bill": b.bill_no, "supplier": name, "date": str(b.bill_date), "due": str(b.due_date),
                     "status": b.status, "currency": b.currency, "total": _money(b.total),
                     "balance_due": _money(b.balance_due)})
        if len(rows) >= min(int(limit or 20), 50):
            break
    return {"bills": rows}


def tool_sales_by_customer(start_date, end_date):
    if not _allowed("sales"):
        return _denied("sales")
    start, end = _d(start_date), _d(end_date)
    customers = {c.id: c.name for c in scoped_query(Customer).all()}
    totals = defaultdict(lambda: [0.0, 0])
    for inv in scoped_query(Invoice).filter(Invoice.status != "void", Invoice.invoice_date >= start, Invoice.invoice_date <= end).all():
        t = totals[customers.get(inv.customer_id, "?")]
        t[0] += float(inv.total_base)
        t[1] += 1
    rows = [{"customer": n, "sales_incl_vat": _money(v[0]), "invoices": v[1]} for n, v in totals.items()]
    return {"period": f"{start} to {end}", "currency": current_company().base_currency,
            "customers": sorted(rows, key=lambda r: -r["sales_incl_vat"])[:20]}


TOOL_FUNCS = {
    "get_profit_and_loss": tool_profit_and_loss,
    "get_account_balances": tool_account_balances,
    "get_receivables": tool_receivables,
    "get_payables": tool_payables,
    "list_invoices": tool_list_invoices,
    "list_bills": tool_list_bills,
    "get_sales_by_customer": tool_sales_by_customer,
}

_DATE = {"type": "string", "description": "Date as YYYY-MM-DD"}
TOOLS = [
    {"name": "get_profit_and_loss", "description": "Income, expenses and net profit by account for a date range, in the company's base currency.",
     "input_schema": {"type": "object", "properties": {"start_date": _DATE, "end_date": _DATE}, "required": ["start_date", "end_date"], "additionalProperties": False}},
    {"name": "get_account_balances", "description": "Balances of chart-of-accounts entries whose name, code or subtype contains the given text (e.g. 'cash', 'bank', 'rent', 'inventory'). Use an empty string for all accounts. Optional as_of date.",
     "input_schema": {"type": "object", "properties": {"name_contains": {"type": "string"}, "as_of": _DATE}, "required": ["name_contains"], "additionalProperties": False}},
    {"name": "get_receivables", "description": "Money customers owe the company: outstanding and overdue amounts per customer and currency, from open and partly paid invoices.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_payables", "description": "Money the company owes suppliers: outstanding and overdue amounts per supplier and currency, from open and partly paid bills.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "list_invoices", "description": "List sales invoices, newest first, with optional filters. Status is one of open, partial, paid, void.",
     "input_schema": {"type": "object", "properties": {"status": {"type": "string", "enum": ["open", "partial", "paid", "void"]}, "customer_name": {"type": "string"}, "date_from": _DATE, "date_to": _DATE, "limit": {"type": "integer"}}, "additionalProperties": False}},
    {"name": "list_bills", "description": "List supplier bills, newest first, with optional filters. Status is one of open, partial, paid, void.",
     "input_schema": {"type": "object", "properties": {"status": {"type": "string", "enum": ["open", "partial", "paid", "void"]}, "supplier_name": {"type": "string"}, "date_from": _DATE, "date_to": _DATE, "limit": {"type": "integer"}}, "additionalProperties": False}},
    {"name": "get_sales_by_customer", "description": "Total invoiced per customer (VAT included, base currency, voids excluded) for a date range, largest first.",
     "input_schema": {"type": "object", "properties": {"start_date": _DATE, "end_date": _DATE}, "required": ["start_date", "end_date"], "additionalProperties": False}},
]


def run_tool(name, args):
    func = TOOL_FUNCS.get(name)
    if func is None:
        return {"error": f"Unknown tool {name}"}
    try:
        return func(**args)
    except (TypeError, ValueError) as exc:
        return {"error": f"Bad arguments: {exc}"}


# --------------------------------------------------------------------------- chat

def system_prompt():
    company = current_company()
    today = date.today()
    return (
        f"You are the built-in accounting assistant of LedgerBooks, answering questions for {company.business_name}, "
        f"a business in Mauritius. Today is {today.isoformat()} ({today.strftime('%A')}). The company's base currency is "
        f"{company.base_currency}.\n\n"
        "Rules:\n"
        "- Every figure, name, date and status you state must come from a tool result in this conversation. Never estimate or invent numbers. "
        "If the tools can't answer something, say so plainly and mention what you can look up instead.\n"
        "- You can only read data. You cannot create, edit, pay, send or delete anything; if asked, explain where in LedgerBooks the user can do it.\n"
        "- Resolve relative dates ('last month', 'this year', 'Q2') yourself from today's date before calling a tool. Financial year is the calendar year unless the user says otherwise.\n"
        "- Amounts in different currencies are never added together; show each currency separately and say which it is.\n"
        "- Write for a non-accountant: short, plain sentences, amounts like 'MUR 12,345.00', and put the answer first. "
        "Use simple dashed lines for lists (no tables, no markdown headings or bold). Offer one useful follow-up question at most."
    )


def _rate_limited():
    now = time.time()
    q = _recent[current_user.id]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= HOURLY_LIMIT:
        return True
    q.append(now)
    return False


def _clean_history(raw):
    history = []
    for item in (raw or [])[-MAX_HISTORY_TURNS * 2:]:
        if isinstance(item, dict) and item.get("role") in ("user", "assistant") and isinstance(item.get("content"), str) and item["content"].strip():
            history.append({"role": item["role"], "content": item["content"][:4000]})
    while history and history[0]["role"] != "user":
        history.pop(0)
    return history


def ask_claude(question, history):
    client = anthropic.Anthropic(timeout=90.0)
    messages = history + [{"role": "user", "content": question}]
    system = system_prompt()
    for _ in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(
            model=MODEL, max_tokens=4000, system=system, tools=TOOLS, messages=messages,
        )
        if response.stop_reason == "refusal":
            return "Sorry, I can't help with that question."
        if response.stop_reason != "tool_use":
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            return text or "Sorry, I couldn't put an answer together. Please try rephrasing the question."
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(run_tool(block.name, block.input), default=str)})
        messages.append({"role": "user", "content": results})
    return "That question needed more look-ups than I'm allowed in one go. Please ask it in a narrower way."


@ai_chat_bp.route("/send", methods=["POST"])
@login_required
def send():
    if not current_user.can_use_module("reports"):
        return jsonify(error="You don't have access to Reports, which this assistant uses."), 403
    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()[:MAX_QUESTION_CHARS]
    if not question:
        return jsonify(error="Please type a question."), 400

    if not api_configured():
        from app.ai_qa import answer_question

        answer = answer_question(question, current_company_id())
        return jsonify(answer=answer or "The AI assistant isn't switched on yet, and I only recognise a few fixed questions without it. "
                                        "Try one of the examples, or ask the administrator to add the AI key.",
                       mode="basic")
    if _rate_limited():
        return jsonify(error=f"You've reached the limit of {HOURLY_LIMIT} questions this hour. Please try again later."), 429
    try:
        answer = ask_claude(question, _clean_history(payload.get("history")))
    except anthropic.AuthenticationError:
        return jsonify(error="The AI key is not valid. Please ask the administrator to check it."), 502
    except anthropic.RateLimitError:
        return jsonify(error="The AI service is busy right now. Please try again in a minute."), 503
    except anthropic.APIStatusError as exc:
        return jsonify(error=f"The AI service returned an error ({exc.status_code}). Please try again."), 502
    except anthropic.APIConnectionError:
        return jsonify(error="Couldn't reach the AI service. Check the internet connection and try again."), 502
    return jsonify(answer=answer, mode="ai")


@ai_chat_bp.app_context_processor
def _inject_chat_info():
    allowed = current_user.is_authenticated and current_user.can_use_module("reports")
    return {"ai_chat_info": {"allowed": allowed, "on": api_configured(), "examples": EXAMPLES}}
