from datetime import date, timedelta

from flask import Blueprint, render_template
from flask_login import current_user, login_required

from app.models import Account, Bill, Customer, Invoice, Item, JournalEntry, Payment, Vendor
from app.scoping import scoped_query

dashboard_bp = Blueprint("dashboard", __name__)

CASH_SUBTYPE = "Cash and Cash Equivalents"
EXPENSE_COLORS = ["#1a7f5a", "#2a5db0", "#c0522d", "#8a6dc9", "#d9a441", "#647883"]


@dashboard_bp.route("/")
def index():
    # Signed-out visitors get the public marketing site (Home/Features/Pricing/
    # About/Contact) instead of being bounced straight to the login form — this
    # is the one URL a prospect or a client demo link actually gets shared.
    if not current_user.is_authenticated:
        return render_template("marketing/home.html")

    from app.reports import period_movement  # local import avoids a circular import at module load time

    today = date.today()
    month_start = today.replace(day=1)
    thirty_days_ago = today - timedelta(days=30)

    # ── Bank Accounts widget ──────────────────────────────────────────
    bank_accounts = scoped_query(Account).filter_by(account_type="Asset", is_active=True, subtype=CASH_SUBTYPE).order_by(Account.code).all()
    bank_rows = [{"account": a, "balance": float(a.balance())} for a in bank_accounts]
    bank_total = sum((r["balance"] for r in bank_rows), start=0.0)

    # ── Sales & Get Paid funnel: Not Paid → Paid → Deposited ────────────
    # Mirrors QuickBooks' own dashboard widget: money's journey from invoiced,
    # to collected-but-not-yet-banked (sitting in Undeposited Funds), to actually deposited.
    open_invoices = scoped_query(Invoice).filter(Invoice.status.in_(["open", "partial"])).all()
    funnel_not_paid = sum((inv.balance_due for inv in open_invoices), start=0.0)

    recent_payments = scoped_query(Payment).filter(Payment.payment_date >= thirty_days_ago).all()
    funnel_paid = sum((float(p.amount) for p in recent_payments if not p.is_deposited), start=0.0)
    funnel_deposited = sum((float(p.amount) for p in recent_payments if p.is_deposited), start=0.0)
    funnel_max = max(funnel_not_paid, funnel_paid, funnel_deposited, 1.0)

    # ── Bills widget: same breakdown on the payable side ────────────────
    open_bills = scoped_query(Bill).filter(Bill.status.in_(["open", "partial"])).all()
    bills_overdue_total = sum((b.balance_due for b in open_bills if b.days_overdue > 0), start=0.0)
    bills_not_due_total = sum((b.balance_due for b in open_bills if b.days_overdue == 0), start=0.0)

    # ── Expenses widget: top categories over the last 30 days ──────────
    expense_accounts = scoped_query(Account).filter_by(account_type="Expense", is_active=True).all()
    expense_rows = [
        {"account": a, "amount": period_movement(a, thirty_days_ago, today)} for a in expense_accounts
    ]
    expense_rows = [r for r in expense_rows if r["amount"] > 0]
    expense_rows.sort(key=lambda r: r["amount"], reverse=True)
    expense_total_30d = sum((r["amount"] for r in expense_rows), start=0.0)

    top_expenses = expense_rows[:5]
    other_expense = sum((r["amount"] for r in expense_rows[5:]), start=0.0)
    if other_expense > 0:
        top_expenses.append({"account": None, "amount": other_expense, "label": "Other"})
    for i, row in enumerate(top_expenses):
        row["color"] = EXPENSE_COLORS[i % len(EXPENSE_COLORS)]
        row["pct"] = round(row["amount"] / expense_total_30d * 100, 1) if expense_total_30d else 0

    # Conic-gradient stops for a pure-CSS donut chart — no JS charting library needed.
    gradient_stops = []
    running_pct = 0.0
    for row in top_expenses:
        start_pct = running_pct
        running_pct += row["pct"]
        gradient_stops.append(f"{row['color']} {start_pct}% {running_pct}%")
    donut_gradient = ", ".join(gradient_stops) if gradient_stops else "var(--border) 0% 100%"

    # ── Profit & Loss snapshot (month to date) ──────────────────────────
    income_accounts = scoped_query(Account).filter_by(account_type="Income", is_active=True).all()
    income_mtd = sum((period_movement(a, month_start, today) for a in income_accounts), start=0.0)
    expense_mtd = sum((period_movement(a, month_start, today) for a in expense_accounts), start=0.0)
    net_income_mtd = income_mtd - expense_mtd
    pl_max = max(income_mtd, expense_mtd, 1.0)

    # ── Low stock alert ──────────────────────────────────────────────
    low_stock_items = [
        i for i in scoped_query(Item).filter_by(item_type="inventory", is_active=True).all() if i.is_low_stock
    ]

    # ── Recent activity ──────────────────────────────────────────────
    recent_entries = scoped_query(JournalEntry).order_by(JournalEntry.entry_date.desc(), JournalEntry.id.desc()).limit(8).all()

    # ── Getting-started checklist — real counts, not a static "welcome" banner.
    # Shows until every step is done, then disappears on its own; a company that's
    # already active just never sees it re-appear once all four are checked.
    onboarding = {
        "has_customer": scoped_query(Customer).first() is not None,
        "has_invoice": scoped_query(Invoice).first() is not None,
        "has_payment": scoped_query(Payment).first() is not None,
        "has_vendor_bill": scoped_query(Vendor).first() is not None and scoped_query(Bill).first() is not None,
    }
    show_onboarding = not all(onboarding.values())

    return render_template(
        "dashboard.html",
        bank_rows=bank_rows, bank_total=bank_total,
        funnel_not_paid=funnel_not_paid, funnel_paid=funnel_paid, funnel_deposited=funnel_deposited, funnel_max=funnel_max,
        bills_overdue_total=bills_overdue_total, bills_not_due_total=bills_not_due_total,
        top_expenses=top_expenses, expense_total_30d=expense_total_30d, donut_gradient=donut_gradient,
        income_mtd=income_mtd, expense_mtd=expense_mtd, net_income_mtd=net_income_mtd, pl_max=pl_max,
        low_stock_items=low_stock_items, recent_entries=recent_entries,
        month_name=today.strftime("%B"),
        onboarding=onboarding, show_onboarding=show_onboarding,
    )
