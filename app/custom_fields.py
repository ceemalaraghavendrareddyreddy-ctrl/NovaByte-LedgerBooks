"""Generic custom-field engine — lets a company add its own extra fields to
Customers/Vendors/Items without a schema migration. Two models in models.py:

  - CustomFieldDefinition: the company's own field list per entity type,
    managed at Settings -> Custom fields (see custom_fields_bp below).
  - CustomFieldValue: one value per (definition, record), always stored as
    text and parsed per field_type on display.

Usage from a blueprint's create/edit/detail route:
    fields = get_field_definitions("customer", current_company_id())
    values = get_field_values(record.id, fields)   # dict field_key -> value string
    # render with {% include "settings/_custom_fields_inputs.html" %} for a
    # form, or "_custom_fields_display.html" for a read-only detail page
    save_field_values(record.id, fields, request.form)   # on POST, after db.session.flush()

Validating "required" fields is the caller's job, same as every other field
in these forms — save_field_values just persists whatever comes in.
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from app import db
from app.auth import current_company_id, owner_required
from app.models import CustomFieldDefinition, CustomFieldValue

ENTITY_TYPES = {
    "customer": "Customers",
    "vendor": "Vendors",
    "item": "Items",
}

FIELD_TYPES = {
    "text": "Text",
    "number": "Number",
    "date": "Date",
    "checkbox": "Checkbox",
    "dropdown": "Dropdown",
}


def get_field_definitions(entity_type, company_id):
    return (
        CustomFieldDefinition.query
        .filter_by(company_id=company_id, entity_type=entity_type, is_active=True)
        .order_by(CustomFieldDefinition.sort_order, CustomFieldDefinition.id)
        .all()
    )


def get_field_values(record_id, definitions):
    """dict field_key -> value string, for pre-filling an edit form or a
    read-only display. Empty dict (not an error) for a brand-new record with
    no id yet, or if there are no active definitions."""
    if not record_id or not definitions:
        return {d.field_key: "" for d in definitions} if definitions else {}
    def_ids = [d.id for d in definitions]
    rows = CustomFieldValue.query.filter(
        CustomFieldValue.record_id == record_id, CustomFieldValue.definition_id.in_(def_ids)
    ).all()
    by_def = {r.definition_id: r.value for r in rows}
    return {d.field_key: (by_def.get(d.id) or "") for d in definitions}


def save_field_values(record_id, definitions, form):
    """Upserts a CustomFieldValue per definition from request.form (keys are
    prefixed cf_<field_key>, matching the shared _custom_fields_inputs.html
    partial). Checkbox fields use HTML's usual convention: present in the
    posted form = checked ("1"), absent = unchecked ("0")."""
    for d in definitions:
        if d.field_type == "checkbox":
            raw = "1" if form.get(f"cf_{d.field_key}") else "0"
        else:
            raw = (form.get(f"cf_{d.field_key}") or "").strip()
        existing = CustomFieldValue.query.filter_by(definition_id=d.id, record_id=record_id).first()
        if existing:
            existing.value = raw
        else:
            db.session.add(CustomFieldValue(definition_id=d.id, record_id=record_id, value=raw))


def missing_required_fields(definitions, form):
    """List of labels for required fields left blank on submit — the caller
    flashes this and re-renders the form, same pattern as every other
    required-field check in these blueprints."""
    missing = []
    for d in definitions:
        if not d.is_required:
            continue
        if d.field_type == "checkbox":
            continue  # an unchecked box isn't "missing", it's a valid false
        if not (form.get(f"cf_{d.field_key}") or "").strip():
            missing.append(d.label)
    return missing


def _slugify(label):
    import re
    s = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return s[:50] or "field"


# ── Settings screen: define/manage custom fields ───────────────────────

custom_fields_bp = Blueprint("custom_fields", __name__, url_prefix="/settings/custom-fields")


@custom_fields_bp.route("")
@login_required
@owner_required
def list_fields():
    defs = (
        CustomFieldDefinition.query.filter_by(company_id=current_company_id())
        .order_by(CustomFieldDefinition.entity_type, CustomFieldDefinition.sort_order, CustomFieldDefinition.id)
        .all()
    )
    by_entity = {}
    for d in defs:
        by_entity.setdefault(d.entity_type, []).append(d)
    return render_template(
        "settings/custom_fields.html", by_entity=by_entity, entity_types=ENTITY_TYPES, field_types=FIELD_TYPES,
    )


@custom_fields_bp.route("/new", methods=["GET", "POST"])
@login_required
@owner_required
def new_field():
    if request.method == "POST":
        entity_type = request.form.get("entity_type", "")
        label = request.form.get("label", "").strip()
        field_type = request.form.get("field_type", "text")

        def render_form():
            return render_template(
                "settings/custom_field_form.html", entity_types=ENTITY_TYPES, field_types=FIELD_TYPES,
                form=request.form,
            )

        if not label or entity_type not in ENTITY_TYPES:
            flash("Enter a label and choose a record type.", "error")
            return render_form()
        if field_type not in FIELD_TYPES:
            flash("Invalid field type.", "error")
            return render_form()
        if field_type == "dropdown" and not request.form.get("options", "").strip():
            flash("A dropdown field needs at least one option (comma-separated).", "error")
            return render_form()

        field_key = _slugify(label)
        existing = CustomFieldDefinition.query.filter_by(
            company_id=current_company_id(), entity_type=entity_type, field_key=field_key,
        ).first()
        if existing:
            flash(f"A field named '{label}' already exists on {ENTITY_TYPES[entity_type]} (active or disabled).", "error")
            return render_form()

        last = (
            CustomFieldDefinition.query.filter_by(company_id=current_company_id(), entity_type=entity_type)
            .order_by(CustomFieldDefinition.sort_order.desc()).first()
        )
        definition = CustomFieldDefinition(
            company_id=current_company_id(), entity_type=entity_type, field_key=field_key, label=label,
            field_type=field_type, options=request.form.get("options", "").strip() or None,
            is_required=request.form.get("is_required") == "on",
            sort_order=(last.sort_order + 1) if last else 0,
        )
        db.session.add(definition)
        db.session.commit()
        flash(f"Custom field '{label}' added to {ENTITY_TYPES[entity_type]}.", "success")
        return redirect(url_for("custom_fields.list_fields"))

    return render_template("settings/custom_field_form.html", entity_types=ENTITY_TYPES, field_types=FIELD_TYPES, form={})


@custom_fields_bp.route("/<int:field_id>/toggle", methods=["POST"])
@login_required
@owner_required
def toggle_field(field_id):
    definition = CustomFieldDefinition.query.filter_by(id=field_id, company_id=current_company_id()).first_or_404()
    definition.is_active = not definition.is_active
    db.session.commit()
    flash(f"Field '{definition.label}' {'enabled' if definition.is_active else 'disabled'}.", "success")
    return redirect(url_for("custom_fields.list_fields"))


@custom_fields_bp.route("/<int:field_id>/delete", methods=["POST"])
@login_required
@owner_required
def delete_field(field_id):
    definition = CustomFieldDefinition.query.filter_by(id=field_id, company_id=current_company_id()).first_or_404()
    CustomFieldValue.query.filter_by(definition_id=definition.id).delete()
    label = definition.label
    db.session.delete(definition)
    db.session.commit()
    flash(f"Deleted custom field '{label}' and all its saved values. This can't be undone.", "success")
    return redirect(url_for("custom_fields.list_fields"))
