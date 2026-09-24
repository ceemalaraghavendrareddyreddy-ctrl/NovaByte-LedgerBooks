"""Time tracking — log billable/non-billable hours against a customer and/or project.
Billable, uninvoiced entries can be pulled onto a real invoice's lines from Sales →
New Invoice (see sales.py's "Unbilled Time" section); this module itself never posts
to the ledger — an entry only affects the books once it's actually on an invoice.
"""
from datetime import date, datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import db
from app.audit import log_audit
from app.auth import current_company_id
from app.models import Customer, Project, TimeEntry
from app.scoping import scoped_or_404, scoped_query

time_tracking_bp = Blueprint("time_tracking", __name__, url_prefix="/time-tracking")


@time_tracking_bp.route("")
@login_required
def time_entry_list():
    customer_id = request.args.get("customer_id", type=int)
    status = request.args.get("status", "all")

    query = scoped_query(TimeEntry)
    if customer_id:
        query = query.filter_by(customer_id=customer_id)
    if status == "unbilled":
        query = query.filter_by(is_billable=True, invoice_line_id=None)
    elif status == "billed":
        query = query.filter(TimeEntry.invoice_line_id.isnot(None))

    entries = query.order_by(TimeEntry.entry_date.desc(), TimeEntry.id.desc()).all()
    customers = scoped_query(Customer).filter_by(is_active=True).order_by(Customer.name).all()
    total_hours = sum((float(e.hours) for e in entries), start=0.0)
    total_unbilled_amount = sum((e.amount for e in entries if e.is_billable and not e.is_invoiced), start=0.0)
    return render_template(
        "time_tracking/time_entries.html", entries=entries, customers=customers,
        selected_customer_id=customer_id, status=status,
        total_hours=total_hours, total_unbilled_amount=total_unbilled_amount,
    )


@time_tracking_bp.route("/new", methods=["GET", "POST"])
@login_required
def time_entry_new():
    customers = scoped_query(Customer).filter_by(is_active=True).order_by(Customer.name).all()
    projects = scoped_query(Project).filter_by(is_active=True).order_by(Project.name).all()

    def render_form(form):
        return render_template(
            "time_tracking/time_entry_form.html", customers=customers, projects=projects,
            form=form, today=date.today().isoformat(),
        )

    if request.method == "POST":
        try:
            entry_date = datetime.strptime(request.form["entry_date"], "%Y-%m-%d").date()
            hours = float(request.form["hours"])
        except (KeyError, ValueError):
            flash("A valid date and number of hours are required.", "error")
            return render_form(request.form)
        description = request.form.get("description", "").strip()
        if not description:
            flash("Description is required.", "error")
            return render_form(request.form)
        if hours <= 0:
            flash("Hours must be greater than zero.", "error")
            return render_form(request.form)

        entry = TimeEntry(
            company_id=current_company_id(),
            user_id=current_user.id,
            customer_id=request.form.get("customer_id") or None,
            project_id=request.form.get("project_id") or None,
            entry_date=entry_date,
            description=description,
            hours=hours,
            hourly_rate=float(request.form.get("hourly_rate") or 0),
            is_billable=request.form.get("is_billable") == "on",
        )
        db.session.add(entry)
        db.session.flush()
        log_audit("create", "time_entry", entry.id, f"Logged {hours}h — {description}", description)
        db.session.commit()
        flash("Time entry logged.", "success")
        return redirect(url_for("time_tracking.time_entry_list"))

    return render_form({})


@time_tracking_bp.route("/<int:entry_id>/edit", methods=["GET", "POST"])
@login_required
def time_entry_edit(entry_id):
    entry = scoped_or_404(TimeEntry, entry_id)
    if entry.is_invoiced:
        flash("This time entry has already been invoiced and can't be edited.", "error")
        return redirect(url_for("time_tracking.time_entry_list"))

    customers = scoped_query(Customer).filter_by(is_active=True).order_by(Customer.name).all()
    projects = scoped_query(Project).filter_by(is_active=True).order_by(Project.name).all()

    if request.method == "POST":
        try:
            entry_date = datetime.strptime(request.form["entry_date"], "%Y-%m-%d").date()
            hours = float(request.form["hours"])
        except (KeyError, ValueError):
            flash("A valid date and number of hours are required.", "error")
            return render_template("time_tracking/time_entry_form.html", customers=customers, projects=projects, form=request.form, editing=True, entry=entry)

        entry.entry_date = entry_date
        entry.description = request.form.get("description", "").strip()
        entry.hours = hours
        entry.customer_id = request.form.get("customer_id") or None
        entry.project_id = request.form.get("project_id") or None
        entry.hourly_rate = float(request.form.get("hourly_rate") or 0)
        entry.is_billable = request.form.get("is_billable") == "on"
        db.session.commit()
        flash("Time entry updated.", "success")
        return redirect(url_for("time_tracking.time_entry_list"))

    form = {
        "entry_date": entry.entry_date.isoformat(), "description": entry.description, "hours": float(entry.hours),
        "customer_id": entry.customer_id, "project_id": entry.project_id, "hourly_rate": float(entry.hourly_rate),
        "is_billable": entry.is_billable,
    }
    return render_template("time_tracking/time_entry_form.html", customers=customers, projects=projects, form=form, editing=True, entry=entry)


@time_tracking_bp.route("/<int:entry_id>/delete", methods=["POST"])
@login_required
def time_entry_delete(entry_id):
    entry = scoped_or_404(TimeEntry, entry_id)
    if entry.is_invoiced:
        flash("This time entry has already been invoiced and can't be deleted.", "error")
        return redirect(url_for("time_tracking.time_entry_list"))
    db.session.delete(entry)
    log_audit("delete", "time_entry", entry_id, f"Deleted time entry '{entry.description}'", entry.description)
    db.session.commit()
    flash("Time entry deleted.", "success")
    return redirect(url_for("time_tracking.time_entry_list"))
