"""Payment reminders — a digest view of what's overdue and what's due soon,
with a per-customer copy-to-clipboard message. Also has an SMTP send action
(POST /reminders/send/<customer_id>) that dispatches a real email using the
company's Settings → SMTP configuration. Falls back to preview-only if SMTP
isn't configured, so nothing silently succeeds without credentials.
"""
import smtplib
from collections import defaultdict
from datetime import date, timedelta
from email.message import EmailMessage

from flask import Blueprint, abort, flash, redirect, render_template, url_for
from flask_login import login_required

from app.audit import log_audit
from app.auth import current_company
from app.models import Customer, Invoice
from app.scoping import scoped_get, scoped_query

reminders_bp = Blueprint("reminders", __name__)

UPCOMING_WINDOW_DAYS = 7


def _smtp_configured(company):
    return bool(company and company.smtp_host and company.smtp_from and company.smtp_port)


def _build_message_text(company_name, customer, invoices, total):
    lines = [
        f"Hi {customer.name},",
        "",
        f"A quick reminder from {company_name} about the following invoice"
        f"{'' if len(invoices) == 1 else 's'}:",
    ]
    for inv in invoices:
        overdue = f" ({inv.days_overdue} day{'' if inv.days_overdue == 1 else 's'} overdue)" if inv.days_overdue else ""
        lines.append(
            f"• {inv.invoice_no} — {inv.currency} {inv.balance_due:,.2f}, "
            f"due {inv.due_date.strftime('%d %b %Y')}{overdue}"
        )
    lines += [
        "",
        f"Total outstanding: {total:,.2f}",
        "",
        "Could you let me know when we can expect payment? "
        "Happy to send fresh copies if useful.",
        "",
        "Thanks,",
        company_name,
    ]
    return "\n".join(lines)


def _collect_reminders_for(customer):
    today = date.today()
    horizon = today + timedelta(days=UPCOMING_WINDOW_DAYS)
    invoices = [
        inv for inv in customer.invoices
        if inv.status in ("open", "partial")
        and inv.balance_due > 0
        and inv.due_date <= horizon
    ]
    invoices.sort(key=lambda i: i.due_date)
    return invoices


@reminders_bp.route("/reminders")
@login_required
def digest():
    today = date.today()
    horizon = today + timedelta(days=UPCOMING_WINDOW_DAYS)

    open_invoices = scoped_query(Invoice).filter(
        Invoice.status.in_(("open", "partial"))
    ).order_by(Invoice.due_date).all()

    overdue, upcoming = [], []
    for inv in open_invoices:
        if inv.balance_due <= 0:
            continue
        if inv.due_date < today:
            overdue.append(inv)
        elif inv.due_date <= horizon:
            upcoming.append(inv)

    by_customer = defaultdict(lambda: {"customer": None, "invoices": [], "total": 0.0})
    for inv in overdue + upcoming:
        row = by_customer[inv.customer_id]
        row["customer"] = inv.customer
        row["invoices"].append(inv)
        row["total"] += inv.balance_due

    total_overdue = sum((i.balance_due for i in overdue), start=0.0)
    total_upcoming = sum((i.balance_due for i in upcoming), start=0.0)

    company = current_company()
    return render_template(
        "reminders/digest.html",
        overdue=overdue, upcoming=upcoming,
        by_customer=list(by_customer.values()),
        total_overdue=total_overdue, total_upcoming=total_upcoming,
        today=today, horizon=horizon,
        company_name=(company.business_name if company else "Your Company"),
        smtp_ready=_smtp_configured(company),
    )


@reminders_bp.route("/reminders/send/<int:customer_id>", methods=["POST"])
@login_required
def send(customer_id):
    company = current_company()
    if not _smtp_configured(company):
        flash("SMTP isn't configured yet — set host, port and from-address in Settings → Company.", "error")
        return redirect(url_for("reminders.digest"))

    customer = scoped_get(Customer, customer_id)
    if customer is None:
        abort(404)
    if not customer.email:
        flash(f"{customer.name} has no email address on file.", "error")
        return redirect(url_for("reminders.digest"))

    invoices = _collect_reminders_for(customer)
    if not invoices:
        flash(f"Nothing to remind {customer.name} about right now.", "warning")
        return redirect(url_for("reminders.digest"))

    total = sum((inv.balance_due for inv in invoices), start=0.0)
    body = _build_message_text(company.business_name, customer, invoices, total)

    msg = EmailMessage()
    msg["Subject"] = f"Payment reminder — {len(invoices)} invoice{'' if len(invoices) == 1 else 's'} outstanding"
    msg["From"] = company.smtp_from
    msg["To"] = customer.email
    msg.set_content(body)

    try:
        with smtplib.SMTP(company.smtp_host, company.smtp_port, timeout=15) as server:
            if company.smtp_use_tls:
                server.starttls()
            if company.smtp_username:
                server.login(company.smtp_username, company.smtp_password or "")
            server.send_message(msg)
    except Exception as exc:  # broad: any SMTP/network error should reach the user, not the log
        flash(f"Could not send email to {customer.name}: {exc}", "error")
        return redirect(url_for("reminders.digest"))

    log_audit("send", "reminder", customer.id,
              f"Sent reminder email to {customer.email} covering {len(invoices)} invoice(s)")
    flash(f"Reminder emailed to {customer.name} ({customer.email}).", "success")
    return redirect(url_for("reminders.digest"))
