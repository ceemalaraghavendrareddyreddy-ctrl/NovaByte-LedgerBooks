"""Shared helpers so every blueprint scopes its queries to the active company the
same way, instead of each one reinventing `.filter_by(company_id=...)` slightly
differently. `scoped_or_404` in particular is a security boundary, not just a
convenience: without it, a valid record id from another company would 404 by luck
of a missing row, or worse, load fine and leak that company's data.
"""
from flask import abort

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
