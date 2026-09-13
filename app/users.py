from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.security import generate_password_hash

from app import db
from app.audit import log_audit
from app.auth import current_company_id, owner_required
from app.company import add_user_to_company, grant_company_access, revoke_company_access
from app.models import User, UserCompany
from app.permissions import MODULES, MODULE_KEYS

users_bp = Blueprint("users", __name__, url_prefix="/users")

ROLES = ["owner", "accountant"]


def _permissions_from_form():
    """None (unrestricted) if 'unrestricted' was checked or nothing was checked at
    all — an accountant with every box left blank isn't locked out of everything,
    they just weren't given a restriction. Only an explicit narrower selection
    (at least one module checked, 'unrestricted' left off) actually restricts."""
    if request.form.get("unrestricted"):
        return None
    checked = [key for key in MODULE_KEYS if request.form.get(f"module_{key}")]
    return ",".join(checked) if checked else None


def _company_users():
    """Every user with access to the active company — not just those whose *home*
    company is this one, since an accountant's home may be a different client."""
    return (
        User.query.join(UserCompany, UserCompany.user_id == User.id)
        .filter(UserCompany.company_id == current_company_id())
        .order_by(User.name).all()
    )


@users_bp.route("")
@login_required
@owner_required
def user_list():
    users = _company_users()
    return render_template("users/list.html", users=users)


@users_bp.route("/new", methods=["GET", "POST"])
@login_required
@owner_required
def user_new():
    if request.method == "POST":
        username = request.form["username"].strip()
        if User.query.filter_by(company_id=current_company_id(), username=username).first():
            flash(f"Username '{username}' already exists.", "error")
            return render_template("users/form.html", roles=ROLES, modules=MODULES, form=request.form)

        password = request.form["password"]
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("users/form.html", roles=ROLES, modules=MODULES, form=request.form)

        user = add_user_to_company(
            current_company_id(), username, password, request.form["name"].strip(),
            role=request.form.get("role", "accountant"),
        )
        user.permissions = _permissions_from_form()
        log_audit("create", "user", user.id, f"Created user '{user.name}' ({user.username}, role: {user.role})")
        db.session.commit()
        flash(f"User '{user.name}' created.", "success")
        return redirect(url_for("users.user_list"))

    return render_template("users/form.html", roles=ROLES, modules=MODULES, form={})


@users_bp.route("/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@owner_required
def user_edit(user_id):
    user = next((u for u in _company_users() if u.id == user_id), None)
    if user is None:
        flash("That user doesn't have access to this company.", "error")
        return redirect(url_for("users.user_list"))

    if request.method == "POST":
        new_username = request.form["username"].strip()
        if (
            new_username != user.username
            and User.query.filter_by(company_id=user.company_id, username=new_username).first()
        ):
            flash(f"Username '{new_username}' already exists.", "error")
            return render_template("users/edit.html", user=user, roles=ROLES, modules=MODULES)

        if user.id == current_user.id and request.form.get("role") != "owner":
            flash("You can't demote yourself — ask another owner to change your role.", "error")
            return render_template("users/edit.html", user=user, roles=ROLES, modules=MODULES)

        user.name = request.form["name"].strip()
        user.username = new_username
        user.role = request.form.get("role", user.role)
        user.permissions = _permissions_from_form()

        new_password = request.form.get("new_password", "").strip()
        if new_password:
            if len(new_password) < 6:
                flash("New password must be at least 6 characters.", "error")
                return render_template("users/edit.html", user=user, roles=ROLES, modules=MODULES)
            user.password_hash = generate_password_hash(new_password)

        log_audit("edit", "user", user.id, f"Updated user '{user.name}' ({user.username}, role: {user.role})")
        db.session.commit()
        flash(f"User '{user.name}' updated.", "success")
        return redirect(url_for("users.user_list"))

    return render_template("users/edit.html", user=user, roles=ROLES, modules=MODULES)


@users_bp.route("/<int:user_id>/toggle", methods=["POST"])
@login_required
@owner_required
def user_toggle(user_id):
    user = next((u for u in _company_users() if u.id == user_id), None)
    if user is None:
        flash("That user doesn't have access to this company.", "error")
        return redirect(url_for("users.user_list"))
    if user.id == current_user.id:
        flash("You can't deactivate your own account.", "error")
        return redirect(url_for("users.user_list"))
    user.is_active_user = not user.is_active_user
    log_audit("status_change", "user", user.id, f"{'Activated' if user.is_active_user else 'Deactivated'} user '{user.name}'")
    db.session.commit()
    flash(f"User {user.name} {'activated' if user.is_active_user else 'deactivated'}.", "success")
    return redirect(url_for("users.user_list"))


@users_bp.route("/<int:user_id>/revoke", methods=["POST"])
@login_required
@owner_required
def user_revoke_access(user_id):
    """Removes this user's access to the active company only — unlike toggle (deactivate),
    this doesn't touch their account elsewhere, just this one company's Team.
    """
    user = next((u for u in _company_users() if u.id == user_id), None)
    if user is None:
        flash("That user doesn't have access to this company.", "error")
        return redirect(url_for("users.user_list"))
    if user.id == current_user.id:
        flash("You can't revoke your own access.", "error")
        return redirect(url_for("users.user_list"))
    if user.company_id == current_company_id():
        flash("Can't revoke access to a user's home company — deactivate them instead.", "error")
        return redirect(url_for("users.user_list"))
    revoke_company_access(user.id, current_company_id())
    log_audit("edit", "user", user.id, f"Revoked '{user.name}' access to this company")
    flash(f"Revoked {user.name}'s access to this company.", "success")
    return redirect(url_for("users.user_list"))
