"""Consolidated P&L and Balance Sheet across every company the current user has
access to (see User.accessible_companies() — the accountant-with-several-clients
scenario UserCompany already exists for). Only meaningful with more than one
accessible company; each company keeps its own separate books as always, this is
purely a read-side rollup.

Each company's figures are converted to one reporting currency using a rate the
user supplies per foreign-currency company (same manual-rate philosophy as every
other multi-currency spot in this app — see app/fx_rates.py for the optional
live-rate lookup that can suggest a starting number).

Deliberately does NOT eliminate intercompany transactions (e.g. Company A's
"Sales to Company B" against Company B's "Purchases from Company A") — that needs
each transaction tagged as intercompany, which doesn't exist anywhere in this data
model yet. What's here is a straight sum of each company's own trial balance,
which is the right building block but not a substitute for a real elimination
step if intercompany trading is material.
"""
from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.auth import current_company
from app.models import Account
from app.reports import period_movement

consolidation_bp = Blueprint("consolidation", __name__, url_prefix="/consolidated")


@consolidation_bp.route("")
@login_required
def index():
    companies = current_user.accessible_companies()
    if len(companies) < 2:
        flash("Consolidated reporting needs access to more than one company. See your name at the bottom of the sidebar to check which companies you can access.", "error")
        return redirect(url_for("dashboard.index"))

    reporting_currency = request.args.get("reporting_currency") or current_company().base_currency
    start_raw = request.args.get("start_date")
    end_raw = request.args.get("end_date")
    as_of_raw = request.args.get("as_of")
    start_date = date.fromisoformat(start_raw) if start_raw else date.today().replace(day=1)
    end_date = date.fromisoformat(end_raw) if end_raw else date.today()
    as_of = date.fromisoformat(as_of_raw) if as_of_raw else date.today()

    rows = []
    total_income = total_expense = total_assets = total_liabilities = total_equity = 0.0

    for co in companies:
        needs_rate = co.base_currency != reporting_currency
        rate = float(request.args.get(f"rate_{co.id}") or 1.0) if needs_rate else 1.0

        income_accounts = Account.query.filter_by(company_id=co.id, account_type="Income", is_active=True).all()
        expense_accounts = Account.query.filter_by(company_id=co.id, account_type="Expense", is_active=True).all()
        asset_accounts = Account.query.filter_by(company_id=co.id, account_type="Asset", is_active=True).all()
        liability_accounts = Account.query.filter_by(company_id=co.id, account_type="Liability", is_active=True).all()
        equity_accounts = Account.query.filter_by(company_id=co.id, account_type="Equity", is_active=True).all()

        co_income = sum((period_movement(a, start_date, end_date) for a in income_accounts), start=0.0)
        co_expense = sum((period_movement(a, start_date, end_date) for a in expense_accounts), start=0.0)
        co_assets = sum((float(a.balance(as_of=as_of)) for a in asset_accounts), start=0.0)
        co_liabilities = sum((float(a.balance(as_of=as_of)) for a in liability_accounts), start=0.0)
        co_equity_raw = sum((float(a.balance(as_of=as_of)) for a in equity_accounts), start=0.0)
        co_net_income_cum = (
            sum((float(a.balance(as_of=as_of)) for a in income_accounts), start=0.0)
            - sum((float(a.balance(as_of=as_of)) for a in expense_accounts), start=0.0)
        )
        co_equity = co_equity_raw + co_net_income_cum

        row = {
            "company": co, "rate": rate, "needs_rate": needs_rate,
            "income": round(co_income * rate, 2), "expense": round(co_expense * rate, 2),
            "assets": round(co_assets * rate, 2), "liabilities": round(co_liabilities * rate, 2),
            "equity": round(co_equity * rate, 2),
        }
        rows.append(row)
        total_income += row["income"]
        total_expense += row["expense"]
        total_assets += row["assets"]
        total_liabilities += row["liabilities"]
        total_equity += row["equity"]

    return render_template(
        "consolidation/index.html", rows=rows, reporting_currency=reporting_currency,
        total_income=round(total_income, 2), total_expense=round(total_expense, 2),
        net_income=round(total_income - total_expense, 2),
        total_assets=round(total_assets, 2), total_liabilities=round(total_liabilities, 2),
        total_equity=round(total_equity, 2),
        start_date=start_date.isoformat(), end_date=end_date.isoformat(), as_of=as_of.isoformat(),
    )
