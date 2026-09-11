import os
import uuid

from flask import Blueprint, abort, current_app, flash, redirect, request, send_from_directory, url_for
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from app import db
from app.audit import log_audit
from app.models import Attachment, Bill, CreditMemo, Invoice, JournalEntry, PurchaseOrder, VendorCredit
from app.scoping import scoped_get

attachments_bp = Blueprint("attachments", __name__, url_prefix="/attachments")

# entity_type -> (label used in audit/flash messages, endpoint to redirect back to, url_for kwarg name)
ENTITY_REDIRECTS = {
    "invoice": ("Invoice", "sales.invoice_detail", "invoice_id"),
    "bill": ("Bill", "purchases.bill_detail", "bill_id"),
    "credit_memo": ("Credit Memo", "sales.credit_memo_detail", "credit_memo_id"),
    "vendor_credit": ("Vendor Credit", "purchases.vendor_credit_detail", "vendor_credit_id"),
    "purchase_order": ("Purchase Order", "purchases.po_detail", "po_id"),
    "journal_entry": ("Journal Entry", "ledger.journal_detail", "entry_id"),
}

# entity_type -> model, so upload/download/delete can confirm the parent document actually
# belongs to the active company before touching its attachments — Attachment itself carries
# no company_id (it's scoped through its parent), so this check is the security boundary.
ENTITY_MODELS = {
    "invoice": Invoice, "bill": Bill, "credit_memo": CreditMemo,
    "vendor_credit": VendorCredit, "purchase_order": PurchaseOrder, "journal_entry": JournalEntry,
}


def _parent_or_404(entity_type, entity_id):
    model = ENTITY_MODELS.get(entity_type)
    if not model or not scoped_get(model, entity_id):
        abort(404)

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB — plenty for scanned receipts/invoices, keeps the DB/disk sane
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "gif", "webp", "doc", "docx", "xls", "xlsx", "csv", "txt"}


def upload_dir():
    path = os.path.join(current_app.instance_path, "uploads")
    os.makedirs(path, exist_ok=True)
    return path


def _redirect_target(entity_type, entity_id):
    if entity_type not in ENTITY_REDIRECTS:
        abort(404)
    _, endpoint, kwarg = ENTITY_REDIRECTS[entity_type]
    return redirect(url_for(endpoint, **{kwarg: entity_id}))


@attachments_bp.route("/upload/<entity_type>/<int:entity_id>", methods=["POST"])
@login_required
def upload(entity_type, entity_id):
    if entity_type not in ENTITY_REDIRECTS:
        abort(404)
    _parent_or_404(entity_type, entity_id)
    label, _, _ = ENTITY_REDIRECTS[entity_type]

    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a file first.", "error")
        return _redirect_target(entity_type, entity_id)

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        flash(f"File type .{ext} isn't allowed. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}.", "error")
        return _redirect_target(entity_type, entity_id)

    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_FILE_SIZE:
        flash(f"File is too large ({size / 1024 / 1024:.1f}MB) — max is 10MB.", "error")
        return _redirect_target(entity_type, entity_id)

    original_name = secure_filename(file.filename) or "upload"
    stored_name = f"{uuid.uuid4().hex}.{ext}"
    file.save(os.path.join(upload_dir(), stored_name))

    attachment = Attachment(
        entity_type=entity_type, entity_id=entity_id, original_filename=original_name,
        stored_filename=stored_name, content_type=file.content_type, size_bytes=size,
        uploaded_by=current_user.id,
    )
    db.session.add(attachment)
    log_audit("attach", entity_type, entity_id, f"Attached file '{original_name}' to {label} #{entity_id}")
    db.session.commit()
    flash(f"'{original_name}' attached.", "success")
    return _redirect_target(entity_type, entity_id)


@attachments_bp.route("/<int:attachment_id>/download")
@login_required
def download(attachment_id):
    attachment = Attachment.query.get_or_404(attachment_id)
    _parent_or_404(attachment.entity_type, attachment.entity_id)
    return send_from_directory(
        upload_dir(), attachment.stored_filename, as_attachment=True, download_name=attachment.original_filename,
    )


@attachments_bp.route("/<int:attachment_id>/delete", methods=["POST"])
@login_required
def delete(attachment_id):
    attachment = Attachment.query.get_or_404(attachment_id)
    entity_type, entity_id = attachment.entity_type, attachment.entity_id
    _parent_or_404(entity_type, entity_id)
    label = ENTITY_REDIRECTS.get(entity_type, ("Document",))[0]

    file_path = os.path.join(upload_dir(), attachment.stored_filename)
    if os.path.exists(file_path):
        os.remove(file_path)

    original_filename = attachment.original_filename  # capture before delete expires the instance
    db.session.delete(attachment)
    log_audit("delete", entity_type, entity_id, f"Removed attachment '{original_filename}' from {label} #{entity_id}")
    db.session.commit()
    flash("Attachment removed.", "success")
    return _redirect_target(entity_type, entity_id)
