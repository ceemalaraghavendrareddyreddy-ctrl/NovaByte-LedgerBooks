import base64
import secrets

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from app import db
from app.auth import current_company, owner_required
from app.models import CURRENCIES, CompanySettings

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")

# Kept well under MySQL's plain TEXT column limit (~64KB) once base64-inflated
# (~33% larger than the raw file) — a sidebar logo, not a photo upload.
MAX_LOGO_BYTES = 40 * 1024
ALLOWED_LOGO_TYPES = {"image/png", "image/jpeg", "image/svg+xml", "image/webp"}


def _handle_logo_upload(settings):
    """Reads request.files['logo'] (if present) into settings.logo_data as a data:
    URI. Never raises on a bad upload — flashes an error and leaves the existing
    logo untouched, since a mistake here shouldn't block saving the rest of the form."""
    file = request.files.get("logo")
    if not file or not file.filename:
        return
    if file.mimetype not in ALLOWED_LOGO_TYPES:
        flash(f"Logo must be PNG, JPEG, WebP, or SVG (got {file.mimetype}). Logo not changed.", "error")
        return
    raw = file.read()
    if len(raw) > MAX_LOGO_BYTES:
        flash(f"Logo must be under {MAX_LOGO_BYTES // 1024}KB (got {len(raw) // 1024}KB). "
              f"Resize/compress it and try again. Logo not changed.", "error")
        return
    encoded = base64.b64encode(raw).decode("ascii")
    settings.logo_data = f"data:{file.mimetype};base64,{encoded}"


@settings_bp.route("/company", methods=["GET", "POST"])
@login_required
@owner_required
def company():
    settings = current_company()
    if request.method == "POST":
        settings.business_name = request.form["business_name"].strip()
        settings.address = request.form.get("address", "").strip() or None
        settings.phone = request.form.get("phone", "").strip() or None
        settings.email = request.form.get("email", "").strip() or None
        settings.vat_number = request.form.get("vat_number", "").strip() or None
        settings.invoice_footer_note = request.form.get("invoice_footer_note", "").strip() or None
        settings.mra_api_url = request.form.get("mra_api_url", "").strip() or None
        settings.mra_api_key = request.form.get("mra_api_key", "").strip() or None
        # SMTP settings for payment-reminder emails.
        settings.smtp_host = request.form.get("smtp_host", "").strip() or None
        smtp_port_raw = request.form.get("smtp_port", "").strip()
        settings.smtp_port = int(smtp_port_raw) if smtp_port_raw.isdigit() else 587
        settings.smtp_username = request.form.get("smtp_username", "").strip() or None
        # Only overwrite the password if the user actually typed something —
        # blank means "keep the one on file" so passwords survive routine saves.
        new_smtp_password = request.form.get("smtp_password", "")
        if new_smtp_password.strip():
            settings.smtp_password = new_smtp_password
        settings.smtp_from = request.form.get("smtp_from", "").strip() or None
        settings.smtp_use_tls = request.form.get("smtp_use_tls") == "on"
        # Invoice PDF theme.
        tpl = request.form.get("invoice_template", "classic").strip()
        if tpl in ("classic", "minimal", "coral"):
            settings.invoice_template = tpl
        new_base_currency = request.form.get("base_currency", "").strip() or settings.base_currency
        if new_base_currency != settings.base_currency:
            flash(
                f"Base currency changed from {settings.base_currency} to {new_base_currency} — this "
                f"only affects new invoices/bills/payments going forward. Past ledger entries stay "
                f"exactly as posted; the company's historical books are never retroactively rewritten.",
                "success",
            )
        settings.base_currency = new_base_currency
        if request.form.get("remove_logo"):
            settings.logo_data = None
        else:
            _handle_logo_upload(settings)
        db.session.commit()
        flash("Company settings updated.", "success")
        return redirect(url_for("settings.company"))
    return render_template("settings/company.html", settings=settings, currencies=CURRENCIES)


@settings_bp.route("/payroll-api-key/regenerate", methods=["POST"])
@login_required
@owner_required
def regenerate_payroll_api_key():
    """Generates a new key for an external payroll system (e.g. Sicorax/Payroll.py)
    to authenticate its journal-entry pushes with — see app/payroll_bridge.py.
    Regenerating immediately invalidates the old key; update it on the payroll
    system's own side too, or its next push will get a 401."""
    settings = current_company()
    settings.payroll_api_key = secrets.token_hex(32)
    db.session.commit()
    flash("Payroll API key regenerated — copy it into the payroll system's LedgerBooks connection settings now.", "success")
    return redirect(url_for("settings.company"))
