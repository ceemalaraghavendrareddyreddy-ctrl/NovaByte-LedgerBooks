"""Payment reminders — a digest view of what's overdue and what's due soon,
with a per-customer copy-to-clipboard message. Also has an SMTP send action
(POST /reminders/send/<customer_id>) that dispatches a real email using the
company's Settings → SMTP configuration. Falls back to preview-only if SMTP
isn't configured, so nothing silently succeeds without credentials.
"""
import smtplib
from collections import defaultdict
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from flask import Blueprint, abort, flash, redirect, render_template, url_for
from flask_login import login_required

from app import db
from app.audit import log_audit
from app.auth import current_company
from app.models import Customer, Invoice
from app.scoping import scoped_get, scoped_query

reminders_bp = Blueprint("reminders", __name__)

UPCOMING_WINDOW_DAYS = 7
AUTO_REMINDER_COOLDOWN_DAYS = 7  # don't re-email the same overdue invoice more than once a week


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


def _send_reminder_email(company, customer, invoices):
    """Sends one reminder email covering `invoices` to `customer`. Raises on any
    SMTP/network failure — callers decide how to surface that (flash vs. log)."""
    total = sum((inv.balance_due for inv in invoices), start=0.0)
    body = _build_message_text(company.business_name, customer, invoices, total)

    msg = EmailMessage()
    msg["Subject"] = f"Payment reminder — {len(invoices)} invoice{'' if len(invoices) == 1 else 's'} outstanding"
    msg["From"] = company.smtp_from
    msg["To"] = customer.email
    msg.set_content(body)

    with smtplib.SMTP(company.smtp_host, company.smtp_port, timeout=15) as server:
        if company.smtp_use_tls:
            server.starttls()
        if company.smtp_username:
            server.login(company.smtp_username, company.smtp_password or "")
        server.send_message(msg)


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

    try:
        _send_reminder_email(company, customer, invoices)
    except Exception as exc:  # broad: any SMTP/network error should reach the user, not the log
        flash(f"Could not send email to {customer.name}: {exc}", "error")
        return redirect(url_for("reminders.digest"))

    log_audit("send", "reminder", customer.id,
              f"Sent reminder email to {customer.email} covering {len(invoices)} invoice(s)")
    flash(f"Reminder emailed to {customer.name} ({customer.email}).", "success")
    return redirect(url_for("reminders.digest"))


def send_auto_reminders_for_current_company():
    """Scheduled counterpart to the manual Send button above — only fires when the
    company has explicitly opted in (CompanySettings.auto_reminders_enabled), and
    only for invoices that are actually OVERDUE (not the "coming due soon" ones the
    manual digest also nudges about — auto-emailing before something is even late
    would be presumptuous). Each overdue invoice is only auto-reminded once every
    AUTO_REMINDER_COOLDOWN_DAYS, tracked via Invoice.last_auto_reminder_sent_at, so
    a customer doesn't get the same nag every single day it stays unpaid.

    Returns (results, skipped) — same shape every other scheduled job returns, for
    app/scheduler.py's shared _run_across_companies logging.
    """
    company = current_company()
    if not company or not company.auto_reminders_enabled or not _smtp_configured(company):
        return [], []

    today = date.today()
    cutoff = datetime.utcnow() - timedelta(days=AUTO_REMINDER_COOLDOWN_DAYS)

    overdue_invoices = scoped_query(Invoice).filter(
        Invoice.status.in_(("open", "partial")), Invoice.due_date < today,
    ).all()
    overdue_invoices = [
        inv for inv in overdue_invoices
        if inv.balance_due > 0 and (inv.last_auto_reminder_sent_at is None or inv.last_auto_reminder_sent_at < cutoff)
    ]
    if not overdue_invoices:
        return [], []

    by_customer = defaultdict(list)
    for inv in overdue_invoices:
        by_customer[inv.customer_id].append(inv)

    results, skipped = [], []
    for customer_id, invoices in by_customer.items():
        customer = scoped_get(Customer, customer_id)
        if not customer or not customer.email:
            skipped.append(f"Customer #{customer_id}: no email on file, skipped.")
            continue
        invoices.sort(key=lambda i: i.due_date)
        try:
            _send_reminder_email(company, customer, invoices)
        except Exception as exc:
            skipped.append(f"{customer.name}: {exc}")
            continue
        now = datetime.utcnow()
        for inv in invoices:
            inv.last_auto_reminder_sent_at = now
        log_audit(
            "send", "reminder", customer.id,
            f"Auto-reminded {customer.email} covering {len(invoices)} overdue invoice(s) (scheduled run)",
        )
        db.session.commit()
        results.append(f"{customer.name}: {len(invoices)} invoice(s)")

    return results, skipped
