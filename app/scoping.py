"""Shared helpers so every blueprint scopes its queries to the active company the
same way, instead of each one reinventing `.filter_by(company_id=...)` slightly
differently. `scoped_or_404` in particular is a security boundary, not just a
convenience: without it, a valid record id from another company would 404 by luck
of a missing row, or worse, load fine and leak that company's data.
"""
import re

from flask import abort

from app import db
from app.auth import current_company_id


def scoped_query(model):
    return model.query.filter_by(company_id=current_company_id())


def scoped_or_404(model, obj_id):
    obj = model.query.filter_by(id=obj_id, company_id=current_company_id()).first()
    if obj is None:
        abort(404)
    return obj


def scoped_get(model, obj_id):
    """Like scoped_or_404 but returns None instead of aborting — for optional lookups."""
    if not obj_id:
        return None
    return model.query.filter_by(id=obj_id, company_id=current_company_id()).first()


def next_number(model, column, prefix):
    """Next document number for the active company: highest existing PREFIX-#### + 1.
    Counts per company, so every company's invoices run 0001, 0002, 0003... with no gaps
    caused by other companies' records."""
    pattern = re.compile(r"^" + re.escape(prefix) + r"-(\d+)$")
    rows = db.session.query(column).filter(model.company_id == current_company_id()).all()
    highest = max((int(m.group(1)) for (value,) in rows if value and (m := pattern.match(value))), default=0)
    return f"{prefix}-{highest + 1:04d}"
