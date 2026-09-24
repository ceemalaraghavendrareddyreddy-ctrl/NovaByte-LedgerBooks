"""Scheduled digest email — Net income, cash position, AR/AP, low stock, and
budget overruns, sent on whatever cadence the company picked (CompanySettings.
scheduled_report_frequency). Off by default; the account owner opts in from
Settings → Company, same reasoning as the auto-reminder toggle in app/reminders.py:
nothing gets emailed automatically until someone consciously turns it on.

The scheduled job (app/scheduler.py) runs daily for every company, and this module
decides for itself whether THIS company's cadence is actually due today — daily
means "not sent yet today", weekly means "7+ days since the last one", monthly
means "a different calendar month than the last one".
"""
import smtplib
from datetime import date, timedelta
from email.message import EmailMessage

from app import db
from app.audit import log_audit
from app.auth import current_company, current_company_id
from app.models import Account, Budget, Bill, Invoice, Item
from app.scoping import scoped_query


def _smtp_configured(company):
    return bool(company and company.smtp_host and company.smtp_from and company.smtp_port)


def _is_due(company, today):
    last = company.scheduled_report_last_sent_at
    if last is None:
        return True
    frequency = company.scheduled_report_frequency or "weekly"
    if frequency == "daily":
        return last < today
    if frequency == "monthly":
        return (last.year, last.month) != (today.year, today.month)
    return (today - last).days >= 7  # weekly, the default


def _build_report_text(company, today):
    from app.reports import period_movement  # local import avoids a circular import at module load time

    period_start = today.replace(day=1)
    income_accounts = scoped_query(Account).filter_by(account_type="Income", is_active=True).all()
    expense_accounts = scoped_query(Account).filter_by(account_type="Expense", is_active=True).all()
    net_income = sum((period_movement(a, period_start, today) for a in income_accounts), start=0.0) - sum(
        (period_movement(a, period_start, today) for a in expense_accounts), start=0.0
    )

    cash_accounts = scoped_query(Account).filter_by(
        account_type="Asset", is_active=True, subtype="Cash and Cash Equivalents",
    ).all()
    cash_position = sum((float(a.balance(as_of=today)) for a in cash_accounts), start=0.0)

    ar_total = sum(
        (inv.balance_due for inv in scoped_query(Invoice).filter(Invoice.status.in_(("open", "partial"))).all()),
        start=0.0,
    )
    ap_total = sum(
        (bill.balance_due for bill in scoped_query(Bill).filter(Bill.status.in_(("open", "partial"))).all()),
        start=0.0,
    )

    low_stock_items = [i for i in scoped_query(Item).filter_by(is_active=True).all() if i.is_low_stock]

    budgets = {
        b.account_id: float(b.budgeted_amount)
        for b in scoped_query(Budget).filter_by(period_year=today.year, period_month=today.month).all()
    }
    over_budget = []
    for account in expense_accounts:
        budgeted = budgets.get(account.id)
        if not budgeted:
            continue
        actual = period_movement(account, period_start, today)
        if actual > budgeted:
            over_budget.append((account, budgeted, actual))

    lines = [
        f"{company.business_name} — scheduled report as of {today.strftime('%d %b %Y')}",
        "",
        f"Net income (month to date): {net_income:,.2f}",
        f"Cash position: {cash_position:,.2f}",
        f"Accounts Receivable outstanding: {ar_total:,.2f}",
        f"Accounts Payable outstanding: {ap_total:,.2f}",
        "",
    ]

    if low_stock_items:
        lines.append(f"Low stock ({len(low_stock_items)} item(s) at or below reorder level):")
        for item in low_stock_items:
            lines.append(f"  • {item.sku} - {item.name}: {item.quantity_on_hand} {item.unit} on hand (reorder at {item.reorder_level})")
        lines.append("")

    if over_budget:
        lines.append(f"Over budget this month ({len(over_budget)} account(s)):")
        for account, budgeted, actual in over_budget:
            lines.append(f"  • {account.code} - {account.name}: {actual:,.2f} actual vs {budgeted:,.2f} budgeted")
        lines.append("")

    if not low_stock_items and not over_budget:
        lines.append("No low-stock items or budget overruns to flag.")

    return "\n".join(lines)


def send_scheduled_report_for_current_company():
    """Returns (results, skipped) — same shape every other scheduled job returns."""
    company = current_company()
    if not company or not company.scheduled_report_enabled or not _smtp_configured(company):
        return [], []

    today = date.today()
    if not _is_due(company, today):
        return [], []

    recipient = company.scheduled_report_recipient or company.smtp_from
    body = _build_report_text(company, today)

    msg = EmailMessage()
    msg["Subject"] = f"{company.business_name} — scheduled report, {today.strftime('%d %b %Y')}"
    msg["From"] = company.smtp_from
    msg["To"] = recipient
    msg.set_content(body)

    try:
        with smtplib.SMTP(company.smtp_host, company.smtp_port, timeout=15) as server:
            if company.smtp_use_tls:
                server.starttls()
            if company.smtp_username:
                server.login(company.smtp_username, company.smtp_password or "")
            server.send_message(msg)
    except Exception as exc:
        return [], [f"Could not send scheduled report to {recipient}: {exc}"]

    company.scheduled_report_last_sent_at = today
    log_audit("send", "scheduled_report", company.id, f"Sent scheduled report to {recipient}")
    db.session.commit()
    return [f"Sent to {recipient}"], []
