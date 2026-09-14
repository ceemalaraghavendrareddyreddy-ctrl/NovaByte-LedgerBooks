import os

from dotenv import load_dotenv
from flask import Flask, render_template, session
from flask_login import LoginManager, current_user
from flask_sqlalchemy import SQLAlchemy

load_dotenv()

db = SQLAlchemy()
login_manager = LoginManager()


def build_database_uri():
    """Postgres (e.g. Render's managed database, which injects DATABASE_URL)
    takes priority when present; otherwise falls back to the original local
    MySQL setup (DB_USER/DB_PASSWORD/DB_HOST/DB_NAME) so nothing changes for
    an existing local dev environment.

    Render (and some other hosts) hand out DATABASE_URL with the old
    'postgres://' scheme, which SQLAlchemy 1.4+ rejects — it must be
    'postgresql://'. Rewritten here rather than requiring every deploy target
    to know that quirk.
    """
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        return database_url

    db_user = os.environ.get("DB_USER", "root")
    db_password = os.environ.get("DB_PASSWORD", "")
    db_host = os.environ.get("DB_HOST", "localhost")
    db_name = os.environ.get("DB_NAME", "quickbooks_clone")
    return f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"


def create_app():
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = build_database_uri()
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from app.auth import auth_bp
    from app.ledger import ledger_bp
    from app.sales import sales_bp
    from app.purchases import purchases_bp
    from app.reports import reports_bp
    from app.inventory import inventory_bp
    from app.settings import settings_bp
    from app.banking import banking_bp
    from app.users import users_bp
    from app.attachments import attachments_bp
    from app.audit import audit_bp
    from app.budgets import budgets_bp
    from app.backup import backup_bp
    from app.dashboard import dashboard_bp
    from app.mra_sync import mra_sync_bp
    from app.assets import assets_bp
    from app.payroll_bridge import payroll_bridge_bp
    from app.share import share_bp
    from app.search import search_bp
    from app.reminders import reminders_bp
    from app.imports import imports_bp
    from app.features_pdf import features_pdf_bp
    from app.mra_sgs import mra_sgs_bp
    from app.fx_rates import fx_rates_bp

    from app.permissions import register_module_guards

    register_module_guards(app, {
        sales_bp: "sales",
        purchases_bp: "purchases",
        banking_bp: "banking",
        inventory_bp: "inventory",
        assets_bp: "assets",
        budgets_bp: "budgets",
        ledger_bp: "ledger",
        reports_bp: "reports",
        mra_sgs_bp: "reports",
    })

    app.register_blueprint(auth_bp)
    app.register_blueprint(ledger_bp)
    app.register_blueprint(sales_bp)
    app.register_blueprint(purchases_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(banking_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(attachments_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(budgets_bp)
    app.register_blueprint(backup_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(mra_sync_bp)
    app.register_blueprint(assets_bp)
    app.register_blueprint(payroll_bridge_bp)
    app.register_blueprint(share_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(reminders_bp)
    app.register_blueprint(imports_bp)
    app.register_blueprint(features_pdf_bp)
    app.register_blueprint(mra_sgs_bp)
    app.register_blueprint(fx_rates_bp)

    from app import period_lock  # noqa: F401 — registers the JournalEntry lock events on import (avoid `import app.x`, which rebinds the local `app` Flask instance to the package)
    from app.period_lock import PeriodLockedError

    @app.errorhandler(PeriodLockedError)
    def handle_period_locked(exc):
        from flask import flash, jsonify, redirect, request, url_for

        db.session.rollback()
        # The payroll bridge and MRA bridge are machine callers, not a browser with
        # a page to flash a message onto and redirect back from — a redirect here
        # would just hand them a 302-to-HTML they can't parse as the JSON error they
        # were expecting. Anything under /api/ gets a real JSON error instead.
        if request.path.startswith("/api/"):
            return jsonify({"error": str(exc)}), 409
        flash(str(exc), "error")
        return redirect(request.referrer or url_for("dashboard.index"))

    app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # hard cap on any request body (uploads, backup restore)

    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("403.html"), 403

    @app.before_request
    def ensure_active_company():
        # A logged-in session should always carry a valid company_id — falls back to
        # the user's home company if it's missing (fresh login, old session from
        # before multi-company) or points at a company they've lost access to.
        if current_user.is_authenticated:
            company_id = session.get("company_id")
            if not company_id or not current_user.can_access_company(company_id):
                session["company_id"] = current_user.company_id

    @app.context_processor
    def inject_helpers():
        from datetime import date

        from flask_login import current_user

        from app.auth import current_company, current_company_id
        from app.models import Attachment, Invoice

        def get_attachments(entity_type, entity_id):
            return (
                Attachment.query.filter_by(entity_type=entity_type, entity_id=entity_id)
                .order_by(Attachment.uploaded_at.desc())
                .all()
            )

        def overdue_invoice_count():
            # Backs the bell icon's notification badge — how many open/partial
            # invoices are already past their due date, right now.
            if not current_user.is_authenticated:
                return 0
            return (
                Invoice.query.filter(
                    Invoice.company_id == current_company_id(),
                    Invoice.status.in_(("open", "partial")),
                    Invoice.due_date < date.today(),
                ).count()
            )

        return {
            "get_attachments": get_attachments,
            "current_company": current_company,
            "current_company_id": current_company_id,
            "overdue_invoice_count": overdue_invoice_count,
        }

    with app.app_context():
        db.create_all()
        _sync_missing_columns()

    from app.scheduler import init_scheduler
    init_scheduler(app)

    return app


def _sync_missing_columns():
    """Adds any column that exists on a model but not yet in the actual database —
    a small safety net for exactly the situation this app hit with company_settings.logo_data:
    a hosting platform's free tier (Render) has no Shell access to run a one-off
    migrate_*.py script against the live database.

    Deliberately conservative: only ADDs a plain nullable column with no backfill.
    A column that needs a NOT NULL constraint, a default applied to existing rows, or
    any other real migration logic still belongs in its own migrate_*.py script (see
    migrate_multicurrency.py, migrate_payroll_bridge.py for that pattern) — this only
    covers the "forgot a column exists on a platform with no shell" case.
    db.create_all() already gives a brand-new table every column it needs; this only
    ever touches tables that already existed before this app boot.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # db.create_all() just created this one fresh, with every column already
        existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing_columns:
                continue
            try:
                col_type = column.type.compile(dialect=db.engine.dialect)
                with db.engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}"))
                print(f"[auto-migrate] added {table.name}.{column.name}")
            except Exception as exc:
                # Never let a startup-time schema sync take the whole app down — worst
                # case, that one column stays missing and whatever route needs it errors
                # normally, same as before this safety net existed.
                print(f"[auto-migrate] could not add {table.name}.{column.name}: {exc}")
