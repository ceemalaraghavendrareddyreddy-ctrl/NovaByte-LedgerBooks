"""Fixed Asset Register: purchase an asset once, then depreciate it — straight-line or
reducing-balance, monthly or yearly — until it's fully written down, past its optional
end date, or disposed of.

Every posted run goes through run_depreciation_for_current_company() below — Dr
Depreciation Expense / Cr Accumulated Depreciation per asset, batched into a single
journal entry per run (like a payroll run, not one entry per asset), grouped by
whichever pair of accounts each asset actually posts to (see _expense_account /
_accum_account) since different asset categories can use different accounts. A
disposal posts its own entry: write off the asset's cost and its accumulated
depreciation, record cash received (if any), and the difference lands in Gain/Loss
on Disposal of Assets (4910).

The daily scheduled job lives in app/scheduler.py, next to the recurring-invoice job —
same shape, same per-company test_request_context pattern. It only actually posts
anything for an asset whose next_depreciation_date has arrived (Asset.is_due); a run
also catches an asset up on every period it's missed in one go (see the while-loop in
run_depreciation_for_current_company), so a gap in the scheduler running (or in the
account manually clicking "Run Depreciation") doesn't permanently stall an asset at
whatever period it last posted.
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

DEPRECIATION_METHODS = ["straight_line", "reducing_balance", "declining_150", "declining_200"]
DEPRECIATION_FREQUENCIES = ["monthly", "yearly"]


def get_account_or_400(code, label):
    account = Account.query.filter_by(company_id=current_company_id(), code=code).first()
    if not account:
        from flask import abort
        abort(400, f"Required account {code} ({label}) is missing from the Chart of Accounts. "
                   f"Run add_asset_accounts.py, or add it manually under Chart of Accounts.")
    return account


def _expense_account(asset):
    """This asset's own Depreciation Expense account if it was given one (letting
    different asset categories post to different accounts), otherwise the shared
    default (6400) every asset used before per-asset accounts existed."""
    if asset.depreciation_expense_account_id:
        return asset.depreciation_expense_account
    return get_account_or_400(DEPRECIATION_EXPENSE_CODE, "Depreciation Expense")


def _accum_account(asset):
    """Same fallback as _expense_account, for Accumulated Depreciation (1590)."""
    if asset.accumulated_depreciation_account_id:
        return asset.accumulated_depreciation_account
    return get_account_or_400(ACCUM_DEPRECIATION_CODE, "Accumulated Depreciation")


# ── Asset CRUD ───────────────────────────────────────────────────────

@assets_bp.route("")
@login_required
def asset_list():
    assets = scoped_query(Asset).order_by(Asset.status, Asset.purchase_date.desc()).all()
    due_count = sum(1 for a in assets if a.is_due)
    return render_template("assets/assets.html", assets=assets, due_count=due_count, today=date.today())


def _depreciation_accounts():
    """Accounts offered for the two per-asset overrides: any active Expense account for
    the expense side, any active Fixed Asset account for the accumulated-depreciation
    side (the same subtype 1590 itself lives under) — a plain <select> with "use the
    default" as its own option is enough here; nothing needs the full Chart of Accounts."""
    expense_accounts = scoped_query(Account).filter_by(account_type="Expense", is_active=True).order_by(Account.code).all()
    accum_accounts = (
        scoped_query(Account).filter_by(account_type="Asset", is_active=True, subtype="Fixed Asset")
        .order_by(Account.code).all()
    )
    return expense_accounts, accum_accounts


def _parse_asset_form(form):
    """Validates and returns a dict of Asset field values from a submitted form, or
    raises ValueError(message) for the caller to flash. Shared by asset_new/asset_edit
    so the two routes can't drift out of sync on what counts as valid."""
    name = form.get("name", "").strip()
    asset_account_id = form.get("asset_account_id")
    if not name or not asset_account_id:
        raise ValueError("Name and asset account are both required.")

    try:
        purchase_date = datetime.strptime(form["purchase_date"], "%Y-%m-%d").date()
        purchase_cost = float(form["purchase_cost"])
        salvage_value = float(form.get("salvage_value") or 0)
        useful_life_months = int(form["useful_life_months"])
    except (KeyError, ValueError):
        raise ValueError("Purchase date, cost, and useful life (whole months) are required and must be valid.")

    if useful_life_months <= 0:
        raise ValueError("Useful life must be at least 1 month.")
    if salvage_value >= purchase_cost:
        raise ValueError("Salvage value must be less than the purchase cost.")

    method = form.get("depreciation_method") if form.get("depreciation_method") in DEPRECIATION_METHODS else "straight_line"
    frequency = form.get("depreciation_frequency") if form.get("depreciation_frequency") in DEPRECIATION_FREQUENCIES else "monthly"

    rate = None
    if method == "reducing_balance":
        try:
            rate = float(form.get("depreciation_rate") or 0)
        except ValueError:
            raise ValueError("Enter a valid depreciation rate % for the reducing balance method.")
        if not (0 < rate <= 100):
            raise ValueError("Depreciation rate must be a percentage between 0 and 100 (exclusive of 0) for the reducing balance method.")

    period_months = 12 if frequency == "yearly" else 1
    start_raw = form.get("depreciation_start_date", "").strip()
    start_date = datetime.strptime(start_raw, "%Y-%m-%d").date() if start_raw else add_months(purchase_date, period_months)

    end_raw = form.get("depreciation_end_date", "").strip()
    end_date = None
    if end_raw:
        end_date = datetime.strptime(end_raw, "%Y-%m-%d").date()
        if end_date < start_date:
            raise ValueError("Depreciation end date can't be before the start date.")

    expense_account_id = form.get("depreciation_expense_account_id") or None
    accum_account_id = form.get("accumulated_depreciation_account_id") or None

    return {
        "name": name,
        "description": form.get("description", "").strip() or None,
        "asset_account_id": int(asset_account_id),
        "purchase_date": purchase_date,
        "purchase_cost": purchase_cost,
        "salvage_value": salvage_value,
        "useful_life_months": useful_life_months,
        "depreciation_method": method,
        "depreciation_frequency": frequency,
        "depreciation_rate": rate,
        "depreciation_start_date": start_date,
        "depreciation_end_date": end_date,
        "depreciation_expense_account_id": int(expense_account_id) if expense_account_id else None,
        "accumulated_depreciation_account_id": int(accum_account_id) if accum_account_id else None,
    }


@assets_bp.route("/new", methods=["GET", "POST"])
@login_required
def asset_new():
    asset_accounts = (
        scoped_query(Account).filter_by(account_type="Asset", is_active=True)
        .filter(Account.code != ACCUM_DEPRECIATION_CODE).order_by(Account.code).all()
    )
    expense_accounts, accum_accounts = _depreciation_accounts()

    def render_form(form):
        return render_template(
            "assets/asset_form.html", asset_accounts=asset_accounts, expense_accounts=expense_accounts,
            accum_accounts=accum_accounts, methods=DEPRECIATION_METHODS, frequencies=DEPRECIATION_FREQUENCIES,
            form=form, today=date.today().isoformat(),
        )

    if request.method == "POST":
        try:
            fields = _parse_asset_form(request.form)
        except ValueError as exc:
            flash(str(exc), "error")
            return render_form(request.form)

        asset = Asset(
            company_id=current_company_id(),
            # Depreciation starts the period *after* purchase by default — the purchase
            # period itself isn't expensed — but depreciation_start_date above may have
            # overridden that from the form.
            next_depreciation_date=fields["depreciation_start_date"],
            **fields,
        )
        db.session.add(asset)
        db.session.flush()
        log_audit("create", "asset", asset.id, f"Added asset '{asset.name}' (cost {asset.purchase_cost:.2f})", asset.name)
        db.session.commit()
        period_label = "year" if asset.depreciation_frequency == "yearly" else "month"
        flash(f"Asset '{asset.name}' added — {asset.next_depreciation_amount():.2f} per {period_label}.", "success")
        return redirect(url_for("assets.asset_detail", asset_id=asset.id))

    return render_form({})


@assets_bp.route("/<int:asset_id>/edit", methods=["GET", "POST"])
@login_required
def asset_edit(asset_id):
    asset = scoped_or_404(Asset, asset_id)
    if asset.status == "disposed":
        flash(f"'{asset.name}' has been disposed of and can no longer be edited.", "error")
        return redirect(url_for("assets.asset_detail", asset_id=asset.id))

    asset_accounts = (
        scoped_query(Account).filter_by(account_type="Asset", is_active=True)
        .filter(Account.code != ACCUM_DEPRECIATION_CODE).order_by(Account.code).all()
    )
    expense_accounts, accum_accounts = _depreciation_accounts()

    def render_form(form):
        return render_template(
            "assets/asset_form.html", asset_accounts=asset_accounts, expense_accounts=expense_accounts,
            accum_accounts=accum_accounts, methods=DEPRECIATION_METHODS, frequencies=DEPRECIATION_FREQUENCIES,
            form=form, today=date.today().isoformat(), editing=True, asset=asset,
        )

    if request.method == "POST":
        try:
            fields = _parse_asset_form(request.form)
        except ValueError as exc:
            flash(str(exc), "error")
            return render_form(request.form)

        # Editing changes how FUTURE periods are calculated — it never rewrites journal
        # entries or AssetDepreciationEntry rows already posted, so accumulated_depreciation
        # and periods_depreciated (the record of what already happened) are left untouched.
        # next_depreciation_date only moves if the new start date is later than what's
        # already been posted through — it never jumps backwards into already-posted ground.
        for key, value in fields.items():
            setattr(asset, key, value)
        if fields["depreciation_start_date"] > asset.next_depreciation_date:
            asset.next_depreciation_date = fields["depreciation_start_date"]

        log_audit("edit", "asset", asset.id, f"Edited asset '{asset.name}'", asset.name)
        db.session.commit()
        flash(f"Asset '{asset.name}' updated.", "success")
        return redirect(url_for("assets.asset_detail", asset_id=asset.id))

    form = {
        "name": asset.name, "description": asset.description, "asset_account_id": asset.asset_account_id,
        "purchase_date": asset.purchase_date.isoformat(), "purchase_cost": float(asset.purchase_cost),
        "salvage_value": float(asset.salvage_value), "useful_life_months": asset.useful_life_months,
        "depreciation_method": asset.depreciation_method, "depreciation_frequency": asset.depreciation_frequency,
        "depreciation_rate": float(asset.depreciation_rate) if asset.depreciation_rate is not None else None,
        "depreciation_start_date": (asset.depreciation_start_date or asset.next_depreciation_date).isoformat(),
        "depreciation_end_date": asset.depreciation_end_date.isoformat() if asset.depreciation_end_date else "",
        "depreciation_expense_account_id": asset.depreciation_expense_account_id,
        "accumulated_depreciation_account_id": asset.accumulated_depreciation_account_id,
    }
    return render_form(form)


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
    accum_account = _accum_account(asset)
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
    Depreciation pair per (account, asset), all in a single entry, like a payroll run
    rather than one journal entry per asset. Different asset categories can post to
    different accounts (Asset.depreciation_expense_account_id /
    accumulated_depreciation_account_id), so the entry's lines are grouped and summed
    per account pair rather than assuming everyone shares 6400/1590.

    Catches each asset up on EVERY period it's missed in one call, not just one month —
    if next_depreciation_date is 3 months behind (the scheduler didn't run, or nobody
    clicked "Run Depreciation" for a while), this posts all 3 periods now rather than
    requiring 3 separate runs on 3 separate days. Each period still gets its own
    AssetDepreciationEntry row (dated to that period, for an accurate history), even
    though they land in one summed journal-entry line per asset for this run.

    Returns (list of "AssetName: amount" strings posted, list of skipped-asset error strings).
    """
    due_assets = [a for a in scoped_query(Asset).all() if a.is_due]
    if not due_assets:
        return [], []

    run_date = date.today()
    entry = JournalEntry(
        company_id=current_company_id(),
        entry_date=run_date,
        reference_no=f"DEPR-{run_date.isoformat()}",
        memo=f"Depreciation run - {run_date.strftime('%d %b %Y')}",
        source_type="depreciation",
        created_by=current_user.id if current_user.is_authenticated else None,
    )

    skipped = []
    results = []
    # account_id -> running total, so multiple assets sharing an account still get ONE
    # summed line each for expense and for accumulated depreciation, not one line per asset.
    expense_totals = {}
    accum_totals = {}
    accounts_by_id = {}
    new_dep_entries = []  # created here, linked to `entry` once it has an id (after flush)

    for asset in due_assets:
        expense_account = _expense_account(asset)
        accum_account = _accum_account(asset)
        accounts_by_id[expense_account.id] = expense_account
        accounts_by_id[accum_account.id] = accum_account

        periods_posted = 0
        asset_total = 0.0
        while asset.is_due:
            period_date = asset.next_depreciation_date
            amount = asset.next_depreciation_amount()
            if amount <= 0:
                break
            dep_entry = AssetDepreciationEntry(asset_id=asset.id, period_date=period_date, amount=amount)
            db.session.add(dep_entry)
            new_dep_entries.append(dep_entry)
            asset.accumulated_depreciation = round(float(asset.accumulated_depreciation) + amount, 2)
            asset.periods_depreciated += 1
            asset.next_depreciation_date = add_months(asset.next_depreciation_date, asset.period_months)
            asset_total += amount
            periods_posted += 1
            if asset.is_fully_depreciated:
                asset.status = "fully_depreciated"

        if periods_posted == 0:
            skipped.append(f"{asset.name}: nothing left to depreciate.")
            continue

        asset_total = round(asset_total, 2)
        expense_totals[expense_account.id] = round(expense_totals.get(expense_account.id, 0) + asset_total, 2)
        accum_totals[accum_account.id] = round(accum_totals.get(accum_account.id, 0) + asset_total, 2)
        log_audit(
            "generate", "asset", asset.id,
            f"Posted {asset_total:.2f} depreciation for '{asset.name}' over {periods_posted} period(s) ({source} run)",
            asset.name,
        )
        results.append(f"{asset.name}: {asset_total:.2f}")

    if not results:
        return [], skipped

    for account_id, amount in expense_totals.items():
        entry.lines.append(JournalLine(account=accounts_by_id[account_id], debit=amount, credit=0, memo="Depreciation"))
    for account_id, amount in accum_totals.items():
        entry.lines.append(JournalLine(account=accounts_by_id[account_id], debit=0, credit=amount, memo="Depreciation"))

    db.session.add(entry)
    db.session.flush()
    for dep_entry in new_dep_entries:
        dep_entry.journal_entry_id = entry.id

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
