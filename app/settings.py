import secrets

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from app import db
from app.auth import current_company, owner_required
from app.models import CURRENCIES, CompanySettings

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")


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
        new_base_currency = request.form.get("base_currency", "").strip() or settings.base_currency
        if new_base_currency != settings.base_currency:
            flash(
                f"Base currency changed from {settings.base_currency} to {new_base_currency} — this "
                f"only affects new invoices/bills/payments going forward. Past ledger entries stay "
                f"exactly as posted; the company's historical books are never retroactively rewritten.",
                "success",
            )
        settings.base_currency = new_base_currency
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
