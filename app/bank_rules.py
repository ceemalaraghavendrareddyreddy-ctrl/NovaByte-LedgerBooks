from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from app import db
from app.audit import log_audit
from app.auth import current_company_id
from app.models import Account, BankRule
from app.scoping import scoped_or_404, scoped_query

bank_rules_bp = Blueprint("bank_rules", __name__, url_prefix="/banking/rules")


@bank_rules_bp.route("")
@login_required
def rule_list():
    rules = scoped_query(BankRule).order_by(BankRule.id.desc()).all()
    return render_template("banking/rule_list.html", rules=rules)


@bank_rules_bp.route("/new", methods=["GET", "POST"])
@login_required
def rule_new():
    accounts = scoped_query(Account).filter_by(is_active=True).order_by(Account.code).all()
    prefill_keyword = request.args.get("keyword", "")
    prefill_account_id = request.args.get("account_id", type=int)

    if request.method == "POST":
        keyword = request.form["keyword"].strip()
        account_id = request.form.get("account_id")
        if not keyword or not account_id:
            flash("Both a keyword and an account are required.", "error")
            return render_template(
                "banking/rule_form.html", accounts=accounts,
                form={"keyword": keyword, "account_id": account_id},
            )
        rule = BankRule(company_id=current_company_id(), keyword=keyword, account_id=int(account_id))
        db.session.add(rule)
        db.session.flush()
        log_audit("create", "bank_rule", rule.id, f"Bank rule created: '{keyword}' -> {rule.account.code}")
        db.session.commit()
        flash("Bank rule created.", "success")
        return redirect(url_for("bank_rules.rule_list"))

    return render_template(
        "banking/rule_form.html", accounts=accounts,
        form={"keyword": prefill_keyword, "account_id": prefill_account_id},
    )


@bank_rules_bp.route("/<int:rule_id>/edit", methods=["GET", "POST"])
@login_required
def rule_edit(rule_id):
    rule = scoped_or_404(BankRule, rule_id)
    accounts = scoped_query(Account).filter_by(is_active=True).order_by(Account.code).all()

    if request.method == "POST":
        keyword = request.form["keyword"].strip()
        account_id = request.form.get("account_id")
        if not keyword or not account_id:
            flash("Both a keyword and an account are required.", "error")
            return render_template("banking/rule_form.html", accounts=accounts, form=request.form, rule=rule)
        rule.keyword = keyword
        rule.account_id = int(account_id)
        db.session.commit()
        flash("Bank rule updated.", "success")
        return redirect(url_for("bank_rules.rule_list"))

    return render_template(
        "banking/rule_form.html", accounts=accounts,
        form={"keyword": rule.keyword, "account_id": rule.account_id}, rule=rule,
    )


@bank_rules_bp.route("/<int:rule_id>/toggle", methods=["POST"])
@login_required
def rule_toggle(rule_id):
    rule = scoped_or_404(BankRule, rule_id)
    rule.is_active = not rule.is_active
    db.session.commit()
    return redirect(url_for("bank_rules.rule_list"))


@bank_rules_bp.route("/<int:rule_id>/delete", methods=["POST"])
@login_required
def rule_delete(rule_id):
    rule = scoped_or_404(BankRule, rule_id)
    db.session.delete(rule)
    db.session.commit()
    flash("Bank rule deleted.", "success")
    return redirect(url_for("bank_rules.rule_list"))
