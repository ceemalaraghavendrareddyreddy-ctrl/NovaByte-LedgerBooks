from flask import Blueprint, render_template, request
from flask_login import current_user, login_required

from app import db
from app.auth import current_company_id, owner_required
from app.models import AuditLog, User, UserCompany
from app.scoping import scoped_query

audit_bp = Blueprint("audit", __name__, url_prefix="/audit")


def log_audit(action, entity_type, entity_id, description, entity_label=None):
    """Call this right alongside any create/void/delete/apply action worth being able to answer
    "who did this and when" about later. Cheap enough to call liberally — it's just one insert.
    """
    entry = AuditLog(
        company_id=current_company_id(),
        user_id=current_user.id if current_user.is_authenticated else None,
        action=action, entity_type=entity_type, entity_id=entity_id,
        entity_label=entity_label, description=description,
    )
    db.session.add(entry)
    # Deliberately no commit() here — piggybacks on whatever transaction the caller is already
    # in, so the audit row and the actual change land together or not at all.


@audit_bp.route("")
@login_required
@owner_required
def audit_log():
    user_id = request.args.get("user_id", type=int)
    entity_type = request.args.get("entity_type", "").strip()

    query = scoped_query(AuditLog)
    if user_id:
        query = query.filter_by(user_id=user_id)
    if entity_type:
        query = query.filter_by(entity_type=entity_type)

    logs = query.order_by(AuditLog.created_at.desc()).limit(500).all()
    users = (
        User.query.join(UserCompany, UserCompany.user_id == User.id)
        .filter(UserCompany.company_id == current_company_id())
        .order_by(User.name).all()
    )
    entity_types = sorted(
        row[0] for row in db.session.query(AuditLog.entity_type)
        .filter(AuditLog.company_id == current_company_id()).distinct().all() if row[0]
    )
    return render_template(
        "audit/log.html", logs=logs, users=users, entity_types=entity_types,
        selected_user_id=user_id, selected_entity_type=entity_type,
    )
