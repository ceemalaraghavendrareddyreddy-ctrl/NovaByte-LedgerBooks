from datetime import date, datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import db
from app.audit import log_audit
from app.auth import current_company_id
from app.models import (
    Account, COSTING_METHODS, Item, ITEM_TYPES, JournalEntry, JournalLine, StockLayer, StockMovement,
    TRACKING_TYPES, Warehouse,
)
from app.scoping import scoped_or_404, scoped_query
from app.custom_fields import (
    get_field_definitions as get_custom_field_definitions,
    get_field_values as get_custom_field_values,
    save_field_values as save_custom_field_values,
    missing_required_fields as missing_required_custom_fields,
)

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
    warehouses = scoped_query(Warehouse).filter_by(is_active=True).order_by(Warehouse.code).all()

    defaults = {
        "income_account_id": scoped_query(Account).filter_by(code=DEFAULT_INCOME_CODE).first(),
        "inventory_account_id": scoped_query(Account).filter_by(code=DEFAULT_INVENTORY_CODE).first(),
        "cogs_account_id": scoped_query(Account).filter_by(code=DEFAULT_COGS_CODE).first(),
    }
    custom_defs = get_custom_field_definitions("item", current_company_id())

    if request.method == "POST":
        sku = request.form["sku"].strip()
        missing = missing_required_custom_fields(custom_defs, request.form)
        if scoped_query(Item).filter_by(sku=sku).first():
            flash(f"SKU {sku} already exists.", "error")
            return render_template(
                "inventory/item_form.html", income_accounts=income_accounts, inventory_accounts=inventory_accounts,
                expense_accounts=expense_accounts, warehouses=warehouses, defaults=defaults,
                item_types=ITEM_TYPES, tracking_types=TRACKING_TYPES, costing_methods=COSTING_METHODS, form=request.form,
                custom_fields=custom_defs, custom_values=request.form,
            )
        if missing:
            flash(f"Required field(s) missing: {', '.join(missing)}.", "error")
            return render_template(
                "inventory/item_form.html", income_accounts=income_accounts, inventory_accounts=inventory_accounts,
                expense_accounts=expense_accounts, warehouses=warehouses, defaults=defaults,
                item_types=ITEM_TYPES, tracking_types=TRACKING_TYPES, costing_methods=COSTING_METHODS, form=request.form,
                custom_fields=custom_defs, custom_values=request.form,
            )

        item_type = request.form["item_type"]
        opening_qty = float(request.form.get("opening_quantity") or 0)
        opening_cost = float(request.form.get("opening_cost") or 0)
        tracking_type = request.form.get("tracking_type") if request.form.get("tracking_type") in TRACKING_TYPES else "none"
        costing_method = request.form.get("costing_method") if request.form.get("costing_method") in COSTING_METHODS else "weighted_average"

        item = Item(
            company_id=current_company_id(),
            sku=sku,
            name=request.form["name"].strip(),
            item_type=item_type,
            unit=request.form.get("unit", "pcs").strip() or "pcs",
            sales_price=request.form.get("sales_price") or 0,
            default_vat_rate=request.form.get("default_vat_rate") or None,
            cost_price=opening_cost if item_type == "inventory" else 0,
            quantity_on_hand=opening_qty if item_type == "inventory" else 0,
            reorder_level=request.form.get("reorder_level") or 0,
            tracking_type=tracking_type if item_type == "inventory" else "none",
            costing_method=costing_method if item_type == "inventory" else "weighted_average",
            income_account_id=int(request.form["income_account_id"]),
            inventory_account_id=int(request.form["inventory_account_id"]) if item_type == "inventory" else None,
            cogs_account_id=int(request.form["cogs_account_id"]) if item_type == "inventory" else None,
        )
        db.session.add(item)
        db.session.flush()

        if item_type == "inventory" and opening_qty:
            warehouse_id = request.form.get("opening_warehouse_id") or None
            lot_number = request.form.get("opening_lot_number", "").strip() or None
            if item.costing_method in ("fifo", "lifo"):
                item.add_stock_layer(opening_qty, opening_cost)
            db.session.add(StockMovement(
                item_id=item.id, movement_date=date.today(), movement_type="opening",
                quantity=opening_qty, unit_cost=opening_cost,
                running_quantity=opening_qty, running_avg_cost=opening_cost, memo="Opening stock",
                warehouse_id=int(warehouse_id) if warehouse_id else None, lot_number=lot_number,
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

        save_custom_field_values(item.id, custom_defs, request.form)
        db.session.commit()
        flash(f"Item '{item.name}' created.", "success")
        return redirect(url_for("inventory.item_list"))

    return render_template(
        "inventory/item_form.html", income_accounts=income_accounts, inventory_accounts=inventory_accounts,
        expense_accounts=expense_accounts, warehouses=warehouses, defaults=defaults,
        item_types=ITEM_TYPES, tracking_types=TRACKING_TYPES, costing_methods=COSTING_METHODS, form={},
        custom_fields=custom_defs, custom_values={},
    )


@inventory_bp.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
@login_required
def item_edit(item_id):
    item = scoped_or_404(Item, item_id)
    income_accounts = scoped_query(Account).filter_by(account_type="Income", is_active=True).order_by(Account.code).all()
    custom_defs = get_custom_field_definitions("item", current_company_id())

    if request.method == "POST":
        new_sku = request.form["sku"].strip()
        missing = missing_required_custom_fields(custom_defs, request.form)
        if new_sku != item.sku and scoped_query(Item).filter_by(sku=new_sku).first():
            flash(f"SKU {new_sku} already in use by another item.", "error")
            return render_template(
                "inventory/item_edit.html", item=item, income_accounts=income_accounts, tracking_types=TRACKING_TYPES,
                costing_methods=COSTING_METHODS, custom_fields=custom_defs,
                custom_values=get_custom_field_values(item.id, custom_defs),
            )
        if missing:
            flash(f"Required field(s) missing: {', '.join(missing)}.", "error")
            return render_template(
                "inventory/item_edit.html", item=item, income_accounts=income_accounts, tracking_types=TRACKING_TYPES,
                costing_methods=COSTING_METHODS, custom_fields=custom_defs, custom_values=request.form,
            )

        item.sku = new_sku
        item.name = request.form["name"].strip()
        item.unit = request.form.get("unit", item.unit).strip() or item.unit
        item.sales_price = request.form.get("sales_price") or 0
        item.default_vat_rate = request.form.get("default_vat_rate") or None
        item.income_account_id = int(request.form["income_account_id"])
        if item.is_tracked:
            item.reorder_level = request.form.get("reorder_level") or 0
            tracking_type = request.form.get("tracking_type")
            if tracking_type in TRACKING_TYPES:
                item.tracking_type = tracking_type

            new_costing_method = request.form.get("costing_method")
            if new_costing_method in COSTING_METHODS and new_costing_method != item.costing_method:
                # Switching TO fifo/lifo from weighted-average: fold whatever's on hand
                # right now into one opening layer at the current average cost, so
                # existing stock isn't silently left uncosted for the next sale.
                # Switching AWAY from fifo/lifo just stops opening new layers — cost_price
                # is already sitting at the right average, nothing to migrate.
                if new_costing_method in ("fifo", "lifo") and float(item.quantity_on_hand) > 0:
                    item.add_stock_layer(float(item.quantity_on_hand), float(item.cost_price))
                item.costing_method = new_costing_method

        save_custom_field_values(item.id, custom_defs, request.form)
        db.session.commit()
        flash(f"Item '{item.name}' updated.", "success")
        return redirect(url_for("inventory.item_detail", item_id=item.id))

    return render_template(
        "inventory/item_edit.html", item=item, income_accounts=income_accounts, tracking_types=TRACKING_TYPES,
        costing_methods=COSTING_METHODS, custom_fields=custom_defs,
        custom_values=get_custom_field_values(item.id, custom_defs),
    )


def warehouse_quantities(item):
    """{warehouse_id: quantity} for this item, summed from every StockMovement that
    carries one — the source of truth for "how much of this item is actually in
    warehouse X", since Item.quantity_on_hand is deliberately one company-wide total
    (see Warehouse's docstring in models.py). Used both for display (item_detail) and
    to validate a transfer actually has enough stock to move out of a warehouse."""
    totals = {}
    for m in item.stock_movements:
        if m.warehouse_id:
            totals[m.warehouse_id] = round(totals.get(m.warehouse_id, 0) + float(m.quantity), 3)
    return totals


@inventory_bp.route("/items/<int:item_id>")
@login_required
def item_detail(item_id):
    item = scoped_or_404(Item, item_id)

    by_warehouse_id = warehouse_quantities(item)
    by_warehouse = {}
    for warehouse_id, qty in by_warehouse_id.items():
        if qty == 0:
            continue
        warehouse = Warehouse.query.get(warehouse_id)
        by_warehouse[warehouse.code if warehouse else f"#{warehouse_id}"] = qty

    by_lot = {}
    for m in item.stock_movements:
        if m.lot_number:
            by_lot[m.lot_number] = round(by_lot.get(m.lot_number, 0) + float(m.quantity), 3)

    remaining_layers = []
    if item.costing_method in ("fifo", "lifo"):
        remaining_layers = [l for l in item.stock_layers if float(l.quantity_remaining) > 0]
        remaining_layers.sort(key=lambda l: (l.received_date, l.id), reverse=(item.costing_method == "lifo"))

    custom_defs = get_custom_field_definitions("item", current_company_id())
    custom_values = get_custom_field_values(item.id, custom_defs)
    return render_template(
        "inventory/item_detail.html", item=item,
        quantity_by_warehouse=by_warehouse,
        quantity_by_lot={k: v for k, v in by_lot.items() if v != 0},
        remaining_layers=remaining_layers,
        custom_fields=custom_defs, custom_values=custom_values,
    )


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

    warehouses = scoped_query(Warehouse).filter_by(is_active=True).order_by(Warehouse.code).all()

    if request.method == "POST":
        new_qty = float(request.form["new_quantity"])
        memo = request.form.get("memo", "").strip() or "Manual stock adjustment"
        delta = new_qty - float(item.quantity_on_hand)
        warehouse_id = request.form.get("warehouse_id") or None
        lot_number = request.form.get("lot_number", "").strip() or None

        if item.costing_method in ("fifo", "lifo") and delta != 0:
            if delta > 0:
                item.add_stock_layer(delta, float(item.cost_price))
            else:
                item.consume_stock_layers(-delta)
            item.refresh_average_cost()

        item.quantity_on_hand = new_qty
        db.session.add(StockMovement(
            item_id=item.id, movement_date=date.today(), movement_type="adjustment",
            quantity=delta, unit_cost=item.cost_price,
            running_quantity=new_qty, running_avg_cost=item.cost_price, memo=memo,
            warehouse_id=int(warehouse_id) if warehouse_id else None, lot_number=lot_number,
        ))
        db.session.commit()
        flash(f"Stock for {item.name} adjusted to {new_qty}.", "success")
        return redirect(url_for("inventory.item_detail", item_id=item.id))

    return render_template("inventory/item_adjust.html", item=item, warehouses=warehouses, today=date.today().isoformat())


@inventory_bp.route("/items/<int:item_id>/transfer", methods=["GET", "POST"])
@login_required
def item_transfer(item_id):
    """Moves quantity from one warehouse to another for the same item. Item.quantity_on_hand
    (the company-wide total) never changes — a transfer only ever relocates stock, it doesn't
    create or destroy it — so this posts two StockMovement rows (equal and opposite, at two
    different warehouses) rather than touching the item's own running quantity/cost fields."""
    item = scoped_or_404(Item, item_id)
    if not item.is_tracked:
        flash("Only inventory items can be transferred between warehouses.", "error")
        return redirect(url_for("inventory.item_detail", item_id=item.id))

    warehouses = scoped_query(Warehouse).filter_by(is_active=True).order_by(Warehouse.code).all()
    if len(warehouses) < 2:
        flash("Add at least two warehouses before transferring stock between them.", "error")
        return redirect(url_for("inventory.item_detail", item_id=item.id))

    if request.method == "POST":
        from_warehouse_id = request.form.get("from_warehouse_id")
        to_warehouse_id = request.form.get("to_warehouse_id")
        try:
            quantity = float(request.form["quantity"])
        except (KeyError, ValueError):
            flash("A valid quantity is required.", "error")
            return render_template("inventory/item_transfer.html", item=item, warehouses=warehouses, today=date.today().isoformat())

        if not from_warehouse_id or not to_warehouse_id:
            flash("Both warehouses are required.", "error")
            return render_template("inventory/item_transfer.html", item=item, warehouses=warehouses, today=date.today().isoformat())
        if from_warehouse_id == to_warehouse_id:
            flash("From and To warehouses must be different.", "error")
            return render_template("inventory/item_transfer.html", item=item, warehouses=warehouses, today=date.today().isoformat())
        if quantity <= 0:
            flash("Quantity must be greater than zero.", "error")
            return render_template("inventory/item_transfer.html", item=item, warehouses=warehouses, today=date.today().isoformat())

        from_warehouse = scoped_or_404(Warehouse, int(from_warehouse_id))
        to_warehouse = scoped_or_404(Warehouse, int(to_warehouse_id))

        available = warehouse_quantities(item).get(from_warehouse.id, 0.0)
        if quantity > available:
            flash(
                f"Not enough stock in {from_warehouse.code}: have {available} {item.unit}, "
                f"tried to transfer {quantity}.", "error",
            )
            return render_template("inventory/item_transfer.html", item=item, warehouses=warehouses, today=date.today().isoformat())

        transfer_date = date.today()
        memo = request.form.get("memo", "").strip()
        lot_number = request.form.get("lot_number", "").strip() or None

        db.session.add(StockMovement(
            item_id=item.id, movement_date=transfer_date, movement_type="transfer",
            quantity=-quantity, unit_cost=item.cost_price,
            reference_type="transfer", warehouse_id=from_warehouse.id, lot_number=lot_number,
            running_quantity=item.quantity_on_hand, running_avg_cost=item.cost_price,
            memo=f"Transfer to {to_warehouse.code}" + (f" - {memo}" if memo else ""),
        ))
        db.session.add(StockMovement(
            item_id=item.id, movement_date=transfer_date, movement_type="transfer",
            quantity=quantity, unit_cost=item.cost_price,
            reference_type="transfer", warehouse_id=to_warehouse.id, lot_number=lot_number,
            running_quantity=item.quantity_on_hand, running_avg_cost=item.cost_price,
            memo=f"Transfer from {from_warehouse.code}" + (f" - {memo}" if memo else ""),
        ))
        log_audit(
            "transfer", "item", item.id,
            f"Transferred {quantity} {item.unit} of {item.sku} from {from_warehouse.code} to {to_warehouse.code}",
            item.sku,
        )
        db.session.commit()
        flash(f"Transferred {quantity} {item.unit} of {item.name} from {from_warehouse.code} to {to_warehouse.code}.", "success")
        return redirect(url_for("inventory.item_detail", item_id=item.id))

    return render_template("inventory/item_transfer.html", item=item, warehouses=warehouses, today=date.today().isoformat())


# ── Warehouses ───────────────────────────────────────────────────────

@inventory_bp.route("/warehouses")
@login_required
def warehouse_list():
    warehouses = scoped_query(Warehouse).order_by(Warehouse.code).all()
    return render_template("inventory/warehouses.html", warehouses=warehouses)


@inventory_bp.route("/warehouses/new", methods=["GET", "POST"])
@login_required
def warehouse_new():
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        name = request.form.get("name", "").strip()
        if not code or not name:
            flash("Code and name are both required.", "error")
            return render_template("inventory/warehouse_form.html", form=request.form)
        if scoped_query(Warehouse).filter_by(code=code).first():
            flash(f"Warehouse code {code} already exists.", "error")
            return render_template("inventory/warehouse_form.html", form=request.form)

        warehouse = Warehouse(
            company_id=current_company_id(), code=code, name=name,
            address=request.form.get("address", "").strip() or None,
        )
        db.session.add(warehouse)
        db.session.commit()
        log_audit("create", "warehouse", warehouse.id, f"Added warehouse '{warehouse.name}'", warehouse.name)
        flash(f"Warehouse '{warehouse.name}' added.", "success")
        return redirect(url_for("inventory.warehouse_list"))

    return render_template("inventory/warehouse_form.html", form={})


@inventory_bp.route("/warehouses/<int:warehouse_id>/edit", methods=["GET", "POST"])
@login_required
def warehouse_edit(warehouse_id):
    warehouse = scoped_or_404(Warehouse, warehouse_id)
    if request.method == "POST":
        new_code = request.form.get("code", "").strip()
        if new_code != warehouse.code and scoped_query(Warehouse).filter_by(code=new_code).first():
            flash(f"Warehouse code {new_code} already in use.", "error")
            return render_template("inventory/warehouse_form.html", form=request.form, editing=True, warehouse=warehouse)

        warehouse.code = new_code
        warehouse.name = request.form.get("name", "").strip()
        warehouse.address = request.form.get("address", "").strip() or None
        db.session.commit()
        flash(f"Warehouse '{warehouse.name}' updated.", "success")
        return redirect(url_for("inventory.warehouse_list"))

    form = {"code": warehouse.code, "name": warehouse.name, "address": warehouse.address}
    return render_template("inventory/warehouse_form.html", form=form, editing=True, warehouse=warehouse)


@inventory_bp.route("/warehouses/<int:warehouse_id>/toggle", methods=["POST"])
@login_required
def warehouse_toggle(warehouse_id):
    warehouse = scoped_or_404(Warehouse, warehouse_id)
    warehouse.is_active = not warehouse.is_active
    db.session.commit()
    flash(f"Warehouse {warehouse.name} {'activated' if warehouse.is_active else 'deactivated'}.", "success")
    return redirect(url_for("inventory.warehouse_list"))
