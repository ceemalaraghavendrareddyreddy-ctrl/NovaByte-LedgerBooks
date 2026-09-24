from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from app import db
from app.company import create_company_and_owner
from app.models import CompanySettings, User
from app import two_factor

auth_bp = Blueprint("auth", __name__)


def owner_required(view_func):
    """Gates Settings and User Management — day-to-day bookkeeping (invoices, bills, payments,
    reports, reconciliation, etc.) stays open to the 'accountant' role; only account-level
    administration is restricted to 'owner'.
    """
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if current_user.role != "owner":
            abort(403)
        return view_func(*args, **kwargs)
    return wrapped


def current_company_id():
    return session.get("company_id")


def current_company():
    company_id = current_company_id()
    return CompanySettings.get_by_id(company_id) if company_id else None


def _set_active_company(user, company_id):
    """Only ever park the session on a company the logged-in user can actually access."""
    if user.can_access_company(company_id):
        session["company_id"] = company_id
        return True
    return False


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        user = User.query.filter_by(username=username, is_active_user=True).first()
        if user and check_password_hash(user.password_hash, password):
            if user.totp_enabled:
                # Password is right, but the login isn't complete yet — park the
                # user id in the session (not flask-login's session, just a plain
                # key) until they clear the second factor in verify_2fa below.
                session["pending_2fa_user_id"] = user.id
                return redirect(url_for("auth.verify_2fa"))
            login_user(user)
            _set_active_company(user, user.company_id)
            return redirect(url_for("dashboard.index"))
        flash("Invalid username or password", "error")
    return render_template("login.html")


@auth_bp.route("/login/2fa", methods=["GET", "POST"])
def verify_2fa():
    user_id = session.get("pending_2fa_user_id")
    if not user_id:
        return redirect(url_for("auth.login"))
    user = User.query.get(user_id)
    if not user or not user.totp_enabled:
        session.pop("pending_2fa_user_id", None)
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        code = request.form.get("code", "")
        recovery_code = request.form.get("recovery_code", "")
        ok = two_factor.verify_code(user.totp_secret, code) if code else False
        used_recovery = False
        if not ok and recovery_code:
            ok = user.redeem_recovery_code(recovery_code)
            used_recovery = ok

        if ok:
            session.pop("pending_2fa_user_id", None)
            if used_recovery:
                db.session.commit()
                flash("Signed in with a recovery code. Generate new codes from Settings → Security when you can.", "success")
            login_user(user)
            _set_active_company(user, user.company_id)
            return redirect(url_for("dashboard.index"))
        flash("Invalid authentication code.", "error")

    return render_template("verify_2fa.html")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    """Creates a brand new company (tenant) + its owner login together — the same
    entry point MRA_TaxInvoice_System uses for a fresh business signing up.
    """
    if request.method == "POST":
        business_name = request.form["business_name"].strip()
        name = request.form["name"].strip()
        username = request.form["username"].strip()
        password = request.form["password"]
        confirm_password = request.form["confirm_password"]

        if not business_name or not name or not username:
            flash("All fields are required.", "error")
            return render_template("register.html")
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("register.html")
        if password != confirm_password:
            flash("Password and confirmation don't match.", "error")
            return render_template("register.html")
        if User.query.filter_by(username=username).first():
            flash("That username is already taken.", "error")
            return render_template("register.html")

        company, owner = create_company_and_owner(business_name, username, password, name)
        login_user(owner)
        session["company_id"] = company.id
        flash(f"Welcome to LedgerBooks! {business_name} is ready to go.", "success")
        return redirect(url_for("dashboard.index"))

    return render_template("register.html")


@auth_bp.route("/switch-company/<int:company_id>", methods=["POST"])
@login_required
def switch_company(company_id):
    if not _set_active_company(current_user, company_id):
        abort(403)
    return redirect(url_for("dashboard.index"))


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    session.pop("company_id", None)
    return redirect(url_for("auth.login"))


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_password = request.form["current_password"]
        new_password = request.form["new_password"]
        confirm_password = request.form["confirm_password"]

        if not check_password_hash(current_user.password_hash, current_password):
            flash("Current password is incorrect.", "error")
            return render_template("change_password.html")
        if len(new_password) < 6:
            flash("New password must be at least 6 characters.", "error")
            return render_template("change_password.html")
        if new_password != confirm_password:
            flash("New password and confirmation don't match.", "error")
            return render_template("change_password.html")

        current_user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        flash("Password changed successfully.", "success")
        return redirect(url_for("dashboard.index"))

    return render_template("change_password.html")


@auth_bp.route("/security")
@login_required
def security():
    return render_template("security.html")


@auth_bp.route("/security/2fa/setup", methods=["GET", "POST"])
@login_required
def setup_2fa():
    if current_user.totp_enabled:
        flash("Two-factor authentication is already enabled.", "error")
        return redirect(url_for("auth.security"))

    if request.method == "POST":
        secret = session.get("pending_totp_secret")
        code = request.form.get("code", "")
        if not secret or not two_factor.verify_code(secret, code):
            flash("That code didn't match. Scan the QR code again and try the current 6-digit code.", "error")
            return redirect(url_for("auth.setup_2fa"))

        plain_codes, stored_value = two_factor.generate_recovery_codes()
        current_user.totp_secret = secret
        current_user.totp_enabled = True
        current_user.totp_recovery_codes = stored_value
        db.session.commit()
        session.pop("pending_totp_secret", None)
        flash("Two-factor authentication is now enabled.", "success")
        return render_template("2fa_recovery_codes.html", codes=plain_codes)

    secret = session.get("pending_totp_secret") or two_factor.generate_secret()
    session["pending_totp_secret"] = secret
    uri = two_factor.provisioning_uri(current_user, secret)
    qr_data_uri = two_factor.qr_code_data_uri(uri)
    return render_template("2fa_setup.html", secret=secret, qr_data_uri=qr_data_uri)


@auth_bp.route("/security/2fa/disable", methods=["GET", "POST"])
@login_required
def disable_2fa():
    if not current_user.totp_enabled:
        return redirect(url_for("auth.security"))

    if request.method == "POST":
        password = request.form.get("password", "")
        if not check_password_hash(current_user.password_hash, password):
            flash("Incorrect password.", "error")
            return render_template("2fa_disable.html")
        current_user.totp_enabled = False
        current_user.totp_secret = None
        current_user.totp_recovery_codes = None
        db.session.commit()
        flash("Two-factor authentication has been disabled.", "success")
        return redirect(url_for("auth.security"))

    return render_template("2fa_disable.html")
