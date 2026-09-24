"""Quick Expenses — already-paid spend backed by a receipt (fuel, meals, office
supplies bought with cash/card), as opposed to a Bill (money owed to a vendor, paid
later). Posts immediately: Dr expense/asset account, Cr the cash/bank account it came
out of. See app/ocr.py for the "upload a photo, autofill the form" flow.
"""
import os
import uuid

from datetime import date, datetime

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from app import db
from app.attachments import ALLOWED_EXTENSIONS, MAX_FILE_SIZE, upload_dir
from app.audit import log_audit
from app.auth import current_company, current_company_id
from app.models import Account, Attachment, Expense, JournalEntry, JournalLine, Vendor
from app.ocr import extract_text, parse_receipt_fields
from app.scoping import scoped_or_404, scoped_query

expenses_bp = Blueprint("expenses", __name__, url_prefix="/expenses")


def get_account_or_400(code, label):
    from flask import abort
    account = Account.query.filter_by(company_id=current_company_id(), code=code).first()
    if not account:
        abort(400, f"Required account {code} ({label}) is missing from the Chart of Accounts.")
    return account


@expenses_bp.route("")
@login_required
def expense_list():
    expenses = scoped_query(Expense).order_by(Expense.expense_date.desc(), Expense.id.desc()).all()
    return render_template("expenses/expenses.html", expenses=expenses)


@expenses_bp.route("/ocr-extract", methods=["POST"])
@login_required
def ocr_extract():
    """AJAX endpoint the New Expense page calls right after a receipt is chosen —
    returns best-effort field guesses as JSON; never touches the database."""
    file = request.files.get("receipt")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded."}), 400

    company = current_company()
    try:
        text = extract_text(file.read(), file.filename, api_key=company.ocr_api_key if company else None)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502

    fields = parse_receipt_fields(text)
    return jsonify(fields)


@expenses_bp.route("/new", methods=["GET", "POST"])
@login_required
def expense_new():
    vendors = scoped_query(Vendor).filter_by(is_active=True).order_by(Vendor.name).all()
    category_accounts = scoped_query(Account).filter_by(account_type="Expense", is_active=True).order_by(Account.code).all()
    payment_accounts = scoped_query(Account).filter(
        Account.account_type == "Asset", Account.is_active == True,  # noqa: E712
        Account.subtype == "Cash and Cash Equivalents",
    ).order_by(Account.code).all()

    def render_form(form):
        return render_template(
            "expenses/expense_form.html", vendors=vendors, category_accounts=category_accounts,
            payment_accounts=payment_accounts, form=form, today=date.today().isoformat(),
        )

    if request.method == "POST":
        try:
            expense_date = datetime.strptime(request.form["expense_date"], "%Y-%m-%d").date()
            amount = float(request.form["amount"])
        except (KeyError, ValueError):
            flash("A valid date and amount are required.", "error")
            return render_form(request.form)
        description = request.form.get("description", "").strip()
        if not description:
            flash("Description is required.", "error")
            return render_form(request.form)
        if amount <= 0:
            flash("Amount must be greater than zero.", "error")
            return render_form(request.form)

        vendor_id = request.form.get("vendor_id") or None
        category_account_id = request.form.get("category_account_id")
        payment_account_id = request.form.get("payment_account_id")
        if not category_account_id or not payment_account_id:
            flash("Category and payment accounts are both required.", "error")
            return render_form(request.form)

        expense = Expense(
            company_id=current_company_id(),
            vendor_id=int(vendor_id) if vendor_id else None,
            vendor_name_raw=request.form.get("vendor_name_raw", "").strip() or None,
            expense_date=expense_date,
            description=description,
            amount=amount,
            vat_rate=float(request.form.get("vat_rate") or 0),
            category_account_id=int(category_account_id),
            payment_account_id=int(payment_account_id),
            ocr_raw_text=request.form.get("ocr_raw_text", "").strip() or None,
            created_by=current_user.id,
        )
        db.session.add(expense)
        db.session.flush()

        _post_expense(expense)

        # If a receipt was uploaded, attach it the same way every other document does.
        file = request.files.get("receipt")
        if file and file.filename:
            ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
            if ext in ALLOWED_EXTENSIONS:
                file.seek(0, os.SEEK_END)
                size = file.tell()
                file.seek(0)
                if size <= MAX_FILE_SIZE:
                    stored_name = f"{uuid.uuid4().hex}.{ext}"
                    file.save(os.path.join(upload_dir(), stored_name))
                    db.session.add(Attachment(
                        entity_type="expense", entity_id=expense.id,
                        original_filename=secure_filename(file.filename) or "receipt",
                        stored_filename=stored_name, content_type=file.content_type, size_bytes=size,
                        uploaded_by=current_user.id,
                    ))

        log_audit("create", "expense", expense.id, f"Recorded expense '{expense.description}' ({expense.amount:.2f})", expense.description)
        db.session.commit()
        flash(f"Expense '{expense.description}' recorded.", "success")
        return redirect(url_for("expenses.expense_detail", expense_id=expense.id))

    return render_form({})


def _post_expense(expense):
    """Dr category account (net of VAT) + Dr VAT Receivable (if any), Cr payment account
    (gross). Does not commit."""
    entry = JournalEntry(
        company_id=current_company_id(),
        entry_date=expense.expense_date,
        memo=f"Expense: {expense.description}" + (f" - {expense.vendor_name_raw}" if expense.vendor_name_raw else ""),
        source_type="expense", source_id=expense.id,
        created_by=current_user.id,
    )
    net_amount = round(float(expense.amount) - expense.vat_amount, 2)
    entry.lines.append(JournalLine(account_id=expense.category_account_id, debit=net_amount, credit=0, memo=expense.description))
    if expense.vat_amount:
        vat_account = get_account_or_400("2110", "VAT Receivable")
        entry.lines.append(JournalLine(account=vat_account, debit=expense.vat_amount, credit=0, memo=expense.description))
    entry.lines.append(JournalLine(account_id=expense.payment_account_id, debit=0, credit=float(expense.amount), memo=expense.description))

    db.session.add(entry)
    db.session.flush()
    expense.journal_entry_id = entry.id


@expenses_bp.route("/<int:expense_id>")
@login_required
def expense_detail(expense_id):
    expense = scoped_or_404(Expense, expense_id)
    return render_template("expenses/expense_detail.html", expense=expense)


@expenses_bp.route("/<int:expense_id>/delete", methods=["POST"])
@login_required
def expense_delete(expense_id):
    expense = scoped_or_404(Expense, expense_id)
    if expense.journal_entry_id:
        entry = JournalEntry.query.get(expense.journal_entry_id)
        if entry:
            db.session.delete(entry)
    description = expense.description
    db.session.delete(expense)
    log_audit("delete", "expense", expense_id, f"Deleted expense '{description}'", description)
    db.session.commit()
    flash(f"Expense '{description}' deleted.", "success")
    return redirect(url_for("expenses.expense_list"))
