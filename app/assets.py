"""Fixed Asset Register: purchase an asset once, then depreciate it straight-line
every month until it's fully written down or disposed of.

Every posted period goes through post_depreciation() below — Dr Depreciation Expense
(6400) / Cr Accumulated Depreciation (1590), one line per asset, batched into a single
journal entry per run (like a payroll run, not one entry per asset). A disposal posts
its own entry: write off the asset's cost and its accumulated depreciation, record cash
received (if any), and the difference lands in Gain/Loss on Disposal of Assets (4910).

The monthly scheduled job lives in app/scheduler.py, next to the recurring-invoice job —
same shape, same per-company test_request_context pattern.
"""
from datetime import date, datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import db
from app.audit import log_audit
from app.auth import current_company_id
from app.models import Account, Asset, AssetDepreciationEntry, JournalEntry, JournalLine
from app.scoping import scoped_get, scoped_or_404, scoped_query

assets_bp = Blueprint("assets", __name__, url_prefix="/assets")

ACCUM_DEPRECIATION_CODE = "1590"
DEPRECIATION_EXPENSE_CODE = "6400"
DISPOSAL_GAIN_LOSS_CODE = "4910"


def get_account_or_400(code, label):
    account = Account.query.filter_by(company_id=current_company_id(), code=code).first()
    if not account:
        from flask import abort
        abort(400, f"Required account {code} ({label}) is missing from the Chart of Accounts. "
                   f"Run add_asset_accounts.py, or add it manually under Chart of Accounts.")
    return account


# ── Asset CRUD ───────────────────────────────────────────────────────

@assets_bp.route("")
@login_required
def asset_list():
    assets = scoped_query(Asset).order_by(Asset.status, Asset.purchase_date.desc()).all()
    due_count = sum(1 for a in assets if a.is_due)
    return render_template("assets/assets.html", assets=assets, due_count=due_count, today=date.today())


@assets_bp.route("/new", methods=["GET", "POST"])
@login_required
def asset_new():
    asset_accounts = (
        scoped_query(Account).filter_by(account_type="Asset", is_active=True)
        .filter(Account.code != ACCUM_DEPRECIATION_CODE).order_by(Account.code).all()
    )

    def render_form(form):
        return render_template(
            "assets/asset_form.html", asset_accounts=asset_accounts, form=form, today=date.today().isoformat(),
        )

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        asset_account_id = request.form.get("asset_account_id")
        if not name or not asset_account_id:
            flash("Name and asset account are both required.", "error")
            return render_form(request.form)

        try:
            purchase_date = datetime.strptime(request.form["purchase_date"], "%Y-%m-%d").date()
            purchase_cost = float(request.form["purchase_cost"])
            salvage_value = float(request.form.get("salvage_value") or 0)
            useful_life_months = int(request.form["useful_life_months"])
        except (KeyError, ValueError):
            flash("Purchase date, cost, and useful life (whole months) are required and must be valid.", "error")
            return render_form(request.form)

        if useful_life_months <= 0:
            flash("Useful life must be at least 1 month.", "error")
            return render_form(request.form)
        if salvage_value >= purchase_cost:
            flash("Salvage value must be less than the purchase cost.", "error")
            return render_form(request.form)

        asset = Asset(
            company_id=current_company_id(),
            name=name,
            description=request.form.get("description", "").strip() or None,
            asset_account_id=int(asset_account_id),
            purchase_date=purchase_date,
            purchase_cost=purchase_cost,
            salvage_value=salvage_value,
            useful_life_months=useful_life_months,
            # Depreciation starts the month *after* purchase — the purchase month itself
            # isn't expensed, matching how the existing recurring-invoice next_run_date works.
            next_depreciation_date=add_months(purchase_date, 1),
        )
        db.session.add(asset)
        db.session.flush()
        log_audit("create", "asset", asset.id, f"Added asset '{asset.name}' (cost {asset.purchase_cost:.2f})", asset.name)
        db.session.commit()
        flash(f"Asset '{asset.name}' added — {asset.monthly_depreciation:.2f}/month over {useful_life_months} months.", "success")
        return redirect(url_for("assets.asset_detail", asset_id=asset.id))

    return render_form({})


@assets_bp.route("/<int:asset_id>")
@login_required
def asset_detail(asset_id):
    asset = scoped_or_404(Asset, asset_id)
    return render_template("assets/asset_detail.html", asset=asset, today=date.today())


@assets_bp.route("/<int:asset_id>/dispose", methods=["GET", "POST"])
@login_required
def asset_dispose(asset_id):
    asset = scoped_or_404(Asset, asset_id)
    if asset.status == "disposed":
        flash("This asset has already been disposed of.", "error")
        return redirect(url_for("assets.asset_detail", asset_id=asset.id))

    if request.method == "POST":
        try:
            disposal_date = datetime.strptime(request.form["disposal_date"], "%Y-%m-%d").date()
            proceeds = float(request.form.get("proceeds") or 0)
        except (KeyError, ValueError):
            flash("A valid disposal date is required.", "error")
            return render_template("assets/asset_dispose.html", asset=asset, today=date.today().isoformat())

        post_disposal(asset, disposal_date, proceeds)
        log_audit(
            "dispose", "asset", asset.id,
            f"Disposed of asset '{asset.name}' for {proceeds:.2f} (net book value was {asset.net_book_value:.2f})",
            asset.name,
        )
        db.session.commit()
        flash(f"Asset '{asset.name}' disposed of.", "success")
        return redirect(url_for("assets.asset_detail", asset_id=asset.id))

    return render_template("assets/asset_dispose.html", asset=asset, today=date.today().isoformat())


def add_months(d, months):
    """Same calendar-safe month math as RecurringInvoice — kept as a local copy here
    (rather than importing the app.models private helper) since assets.py may end up
    with its own depreciation-calendar needs (e.g. a future non-monthly method)."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    import calendar
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


# ── Depreciation posting ─────────────────────────────────────────────

def post_disposal(asset, disposal_date, proceeds):
    """Writes off an asset's cost and accumulated depreciation, records proceeds (if any),
    and posts the difference to Gain/Loss on Disposal of Assets. Does not commit."""
    accum_account = get_account_or_400(ACCUM_DEPRECIATION_CODE, "Accumulated Depreciation")
    gain_loss_account = get_account_or_400(DISPOSAL_GAIN_LOSS_CODE, "Gain/Loss on Disposal of Assets")

    nbv = asset.net_book_value
    gain_or_loss = round(proceeds - nbv, 2)  # positive = gain (credit), negative = loss (debit)

    entry = JournalEntry(
        company_id=current_company_id(),
        entry_date=disposal_date,
        reference_no=f"DISPOSE-{asset.id}",
        memo=f"Disposal of asset: {asset.name}",
        source_type="asset_disposal",
        created_by=current_user.id if current_user.is_authenticated else None,
    )
    # Remove the asset from the books: credit its cost account, debit out the
    # accumulated depreciation built up against it.
    entry.lines.append(JournalLine(account_id=asset.asset_account_id, debit=0, credit=asset.purchase_cost, memo=asset.name))
    if float(asset.accumulated_depreciation):
        entry.lines.append(JournalLine(account=accum_account, debit=asset.accumulated_depreciation, credit=0, memo=asset.name))
    if proceeds:
        cash_account = get_account_or_400("1010", "Bank Account")
        entry.lines.append(JournalLine(account=cash_account, debit=proceeds, credit=0, memo=f"Proceeds - {asset.name}"))
    if gain_or_loss > 0:
        entry.lines.append(JournalLine(account=gain_loss_account, debit=0, credit=gain_or_loss, memo=f"Gain - {asset.name}"))
    elif gain_or_loss < 0:
        entry.lines.append(JournalLine(account=gain_loss_account, debit=-gain_or_loss, credit=0, memo=f"Loss - {asset.name}"))

    db.session.add(entry)
    db.session.flush()

    asset.status = "disposed"
    asset.disposal_date = disposal_date
    asset.disposal_proceeds = proceeds


def run_depreciation_for_current_company(source="manual"):
    """Posts one batched journal entry covering every due asset in the currently active
    company (from current_company_id()) — one Dr Depreciation Expense / Cr Accumulated
    Depreciation line pair per asset, all in a single entry, like a payroll run rather
    than one journal entry per asset. Repeats month over month until each asset is fully
    depreciated or disposed of.

    Returns (list of "AssetName: amount" strings posted, list of skipped-asset error strings).
    """
    due_assets = [a for a in scoped_query(Asset).all() if a.is_due]
    if not due_assets:
        return [], []

    expense_account = get_account_or_400(DEPRECIATION_EXPENSE_CODE, "Depreciation Expense")
    accum_account = get_account_or_400(ACCUM_DEPRECIATION_CODE, "Accumulated Depreciation")

    run_date = date.today()
    entry = JournalEntry(
        company_id=current_company_id(),
        entry_date=run_date,
        reference_no=f"DEPR-{run_date.isoformat()}",
        memo=f"Monthly depreciation run - {run_date.strftime('%B %Y')}",
        source_type="depreciation",
        created_by=current_user.id if current_user.is_authenticated else None,
    )

    posted = []
    skipped = []
    total = 0.0
    for asset in due_assets:
        amount = asset.next_depreciation_amount()
        if amount <= 0:
            skipped.append(f"{asset.name}: nothing left to depreciate.")
            continue
        entry.lines.append(JournalLine(account=expense_account, debit=amount, credit=0, memo=asset.name))
        total += amount
        posted.append((asset, amount))

    if not posted:
        return [], skipped

    entry.lines.append(JournalLine(account=accum_account, debit=0, credit=round(total, 2), memo="Monthly depreciation"))
    db.session.add(entry)
    db.session.flush()

    results = []
    for asset, amount in posted:
        db.session.add(AssetDepreciationEntry(
            asset_id=asset.id, period_date=run_date, amount=amount, journal_entry_id=entry.id,
        ))
        asset.accumulated_depreciation = round(float(asset.accumulated_depreciation) + amount, 2)
        asset.periods_depreciated += 1
        asset.next_depreciation_date = add_months(asset.next_depreciation_date, 1)
        if asset.is_fully_depreciated:
            asset.status = "fully_depreciated"
        log_audit(
            "generate", "asset", asset.id,
            f"Posted {amount:.2f} depreciation for '{asset.name}' ({source} run)", asset.name,
        )
        results.append(f"{asset.name}: {amount:.2f}")

    db.session.commit()
    return results, skipped


@assets_bp.route("/run-due", methods=["POST"])
@login_required
def run_due():
    posted, skipped = run_depreciation_for_current_company(source="manual")
    if not posted and not skipped:
        flash("No depreciation is due right now.", "success")
    if posted:
        flash(f"Posted depreciation for {len(posted)} asset(s): {', '.join(posted)}.", "success")
    for message in skipped:
        flash(message, "error")
    return redirect(url_for("assets.asset_list"))
