from datetime import date, datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import db
from app.auth import current_company_id
from app.models import Account, Item, ITEM_TYPES, JournalEntry, JournalLine, StockMovement
from app.scoping import scoped_or_404, scoped_query

inventory_bp = Blueprint("inventory", __name__, url_prefix="/inventory")

DEFAULT_INCOME_CODE = "4000"
DEFAULT_INVENTORY_CODE = "1300"
DEFAULT_COGS_CODE = "5000"
OPENING_EQUITY_CODE = "3000"  # Owner's Equity — the plug side for opening stock value


def get_account_or_400(code, label):
    account = Account.query.filter_by(company_id=current_company_id(), code=code).first()
    if not account:
        abort(400, f"Required account {code} ({label}) is missing from the Chart of Accounts.")
    return account


@inventory_bp.route("/items")
@login_required
def item_list():
    items = scoped_query(Item).order_by(Item.sku).all()
    return render_template("inventory/items.html", items=items)


@inventory_bp.route("/items/new", methods=["GET", "POST"])
@login_required
def item_new():
    income_accounts = scoped_query(Account).filter_by(account_type="Income", is_active=True).order_by(Account.code).all()
    inventory_accounts = scoped_query(Account).filter(
        Account.account_type == "Asset", Account.is_active == True,  # noqa: E712
        Account.subtype.in_(["Current Asset", "Fixed Asset"]),
    ).order_by(Account.code).all()
    expense_accounts = scoped_query(Account).filter_by(account_type="Expense", is_active=True).order_by(Account.code).all()

    defaults = {
        "income_account_id": scoped_query(Account).filter_by(code=DEFAULT_INCOME_CODE).first(),
        "inventory_account_id": scoped_query(Account).filter_by(code=DEFAULT_INVENTORY_CODE).first(),
        "cogs_account_id": scoped_query(Account).filter_by(code=DEFAULT_COGS_CODE).first(),
    }

    if request.method == "POST":
        sku = request.form["sku"].strip()
        if scoped_query(Item).filter_by(sku=sku).first():
            flash(f"SKU {sku} already exists.", "error")
            return render_template(
                "inventory/item_form.html", income_accounts=income_accounts, inventory_accounts=inventory_accounts,
                expense_accounts=expense_accounts, defaults=defaults, item_types=ITEM_TYPES, form=request.form,
            )

        item_type = request.form["item_type"]
        opening_qty = float(request.form.get("opening_quantity") or 0)
        opening_cost = float(request.form.get("opening_cost") or 0)

        item = Item(
            company_id=current_company_id(),
            sku=sku,
            name=request.form["name"].strip(),
            item_type=item_type,
            unit=request.form.get("unit", "pcs").strip() or "pcs",
            sales_price=request.form.get("sales_price") or 0,
            cost_price=opening_cost if item_type == "inventory" else 0,
            quantity_on_hand=opening_qty if item_type == "inventory" else 0,
            reorder_level=request.form.get("reorder_level") or 0,
            income_account_id=int(request.form["income_account_id"]),
            inventory_account_id=int(request.form["inventory_account_id"]) if item_type == "inventory" else None,
            cogs_account_id=int(request.form["cogs_account_id"]) if item_type == "inventory" else None,
        )
        db.session.add(item)
        db.session.flush()

        if item_type == "inventory" and opening_qty:
            db.session.add(StockMovement(
                item_id=item.id, movement_date=date.today(), movement_type="opening",
                quantity=opening_qty, unit_cost=opening_cost,
                running_quantity=opening_qty, running_avg_cost=opening_cost, memo="Opening stock",
            ))
            opening_value = round(opening_qty * opening_cost, 2)
            if opening_value:
                # Opening stock has real ledger value — post it, or the Balance Sheet's
                # Inventory account would silently understate actual stock on hand.
                equity_account = get_account_or_400(OPENING_EQUITY_CODE, "Owner's Equity")
                entry = JournalEntry(
                    company_id=current_company_id(),
                    entry_date=date.today(), memo=f"Opening stock - {item.sku}",
                    source_type="item_opening_stock", source_id=item.id, created_by=current_user.id,
                )
                entry.lines.append(JournalLine(account_id=item.inventory_account_id, debit=opening_value, credit=0))
                entry.lines.append(JournalLine(account=equity_account, debit=0, credit=opening_value))
                db.session.add(entry)
                db.session.flush()
                item.opening_journal_entry_id = entry.id

        db.session.commit()
        flash(f"Item '{item.name}' created.", "success")
        return redirect(url_for("inventory.item_list"))

    return render_template(
        "inventory/item_form.html", income_accounts=income_accounts, inventory_accounts=inventory_accounts,
        expense_accounts=expense_accounts, defaults=defaults, item_types=ITEM_TYPES, form={},
    )


@inventory_bp.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
@login_required
def item_edit(item_id):
    item = scoped_or_404(Item, item_id)
    income_accounts = scoped_query(Account).filter_by(account_type="Income", is_active=True).order_by(Account.code).all()

    if request.method == "POST":
        new_sku = request.form["sku"].strip()
        if new_sku != item.sku and scoped_query(Item).filter_by(sku=new_sku).first():
            flash(f"SKU {new_sku} already in use by another item.", "error")
            return render_template("inventory/item_edit.html", item=item, income_accounts=income_accounts)

        item.sku = new_sku
        item.name = request.form["name"].strip()
        item.unit = request.form.get("unit", item.unit).strip() or item.unit
        item.sales_price = request.form.get("sales_price") or 0
        item.income_account_id = int(request.form["income_account_id"])
        if item.is_tracked:
            item.reorder_level = request.form.get("reorder_level") or 0
        db.session.commit()
        flash(f"Item '{item.name}' updated.", "success")
        return redirect(url_for("inventory.item_detail", item_id=item.id))

    return render_template("inventory/item_edit.html", item=item, income_accounts=income_accounts)


@inventory_bp.route("/items/<int:item_id>")
@login_required
def item_detail(item_id):
    item = scoped_or_404(Item, item_id)
    return render_template("inventory/item_detail.html", item=item)


@inventory_bp.route("/items/<int:item_id>/toggle", methods=["POST"])
@login_required
def item_toggle(item_id):
    item = scoped_or_404(Item, item_id)
    item.is_active = not item.is_active
    db.session.commit()
    flash(f"Item {item.sku} {'activated' if item.is_active else 'deactivated'}.", "success")
    return redirect(url_for("inventory.item_list"))


@inventory_bp.route("/items/<int:item_id>/adjust", methods=["GET", "POST"])
@login_required
def item_adjust(item_id):
    item = scoped_or_404(Item, item_id)
    if not item.is_tracked:
        flash("Only inventory items can be stock-adjusted.", "error")
        return redirect(url_for("inventory.item_detail", item_id=item.id))

    if request.method == "POST":
        new_qty = float(request.form["new_quantity"])
        memo = request.form.get("memo", "").strip() or "Manual stock adjustment"
        delta = new_qty - float(item.quantity_on_hand)

        item.quantity_on_hand = new_qty
        db.session.add(StockMovement(
            item_id=item.id, movement_date=date.today(), movement_type="adjustment",
            quantity=delta, unit_cost=item.cost_price,
            running_quantity=new_qty, running_avg_cost=item.cost_price, memo=memo,
        ))
        db.session.commit()
        flash(f"Stock for {item.name} adjusted to {new_qty}.", "success")
        return redirect(url_for("inventory.item_detail", item_id=item.id))

    return render_template("inventory/item_adjust.html", item=item, today=date.today().isoformat())
