from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from app import db
from app.auth import current_company_id
from app.models import Account, Budget
from app.scoping import scoped_query

budgets_bp = Blueprint("budgets", __name__, url_prefix="/budgets")

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


@budgets_bp.route("", methods=["GET", "POST"])
@login_required
def budget_entry():
    year = request.values.get("year", type=int) or date.today().year
    month = request.values.get("month", type=int) or date.today().month

    accounts = scoped_query(Account).filter(
        Account.is_active == True, Account.account_type.in_(["Income", "Expense"]),  # noqa: E712
    ).order_by(Account.code).all()

    if request.method == "POST":
        for account in accounts:
            amount_raw = request.form.get(f"amount_{account.id}", "").strip()
            amount = float(amount_raw) if amount_raw else 0.0

            existing = Budget.query.filter_by(account_id=account.id, period_year=year, period_month=month).first()
            if existing:
                if amount == 0:
                    db.session.delete(existing)  # keep the table free of meaningless zero rows
                else:
                    existing.budgeted_amount = amount
            elif amount != 0:
                db.session.add(Budget(
                    company_id=current_company_id(), account_id=account.id,
                    period_year=year, period_month=month, budgeted_amount=amount,
                ))

        db.session.commit()
        flash(f"Budget for {MONTH_NAMES[month - 1]} {year} saved.", "success")
        return redirect(url_for("budgets.budget_entry", year=year, month=month))

    existing_budgets = {
        b.account_id: float(b.budgeted_amount)
        for b in scoped_query(Budget).filter_by(period_year=year, period_month=month).all()
    }
    rows = [{"account": a, "amount": existing_budgets.get(a.id, 0)} for a in accounts]

    return render_template(
        "budgets/entry.html", rows=rows, year=year, month=month, month_names=MONTH_NAMES,
        current_year=date.today().year,
    )


@budgets_bp.route("/vs-actual")
@login_required
def budget_vs_actual():
    from app.reports import period_movement  # local import avoids a circular import at module load time

    year = request.args.get("year", type=int) or date.today().year
    month = request.args.get("month", type=int) or date.today().month
    start_date = date(year, month, 1)
    end_date = date(year + 1, 1, 1) - date.resolution if month == 12 else date(year, month + 1, 1) - date.resolution

    budgets = {
        b.account_id: float(b.budgeted_amount)
        for b in scoped_query(Budget).filter_by(period_year=year, period_month=month).all()
    }
    accounts = scoped_query(Account).filter(
        Account.is_active == True, Account.account_type.in_(["Income", "Expense"]),  # noqa: E712
    ).order_by(Account.code).all()

    rows = []
    for account in accounts:
        budgeted = budgets.get(account.id, 0.0)
        actual = period_movement(account, start_date, end_date)
        if budgeted == 0 and actual == 0:
            continue
        variance = actual - budgeted
        # For Income, actual > budget is favorable; for Expense, actual < budget is favorable.
        favorable = variance >= 0 if account.account_type == "Income" else variance <= 0
        rows.append({
            "account": account, "budgeted": budgeted, "actual": actual, "variance": variance, "favorable": favorable,
        })

    total_budgeted_income = sum((r["budgeted"] for r in rows if r["account"].account_type == "Income"), start=0.0)
    total_actual_income = sum((r["actual"] for r in rows if r["account"].account_type == "Income"), start=0.0)
    total_budgeted_expense = sum((r["budgeted"] for r in rows if r["account"].account_type == "Expense"), start=0.0)
    total_actual_expense = sum((r["actual"] for r in rows if r["account"].account_type == "Expense"), start=0.0)

    return render_template(
        "budgets/vs_actual.html", rows=rows, year=year, month=month, month_names=MONTH_NAMES,
        current_year=date.today().year,
        total_budgeted_income=total_budgeted_income, total_actual_income=total_actual_income,
        total_budgeted_expense=total_budgeted_expense, total_actual_expense=total_actual_expense,
    )
