"""Background scheduler for anything that should happen on a calendar, not a click:
recurring invoices and fixed-asset depreciation, so far.

There's no separate worker process in this app — LedgerBooks runs as a single Flask
process (dev server or Waitress), so the scheduler lives inside it as a background
thread (APScheduler's BackgroundScheduler). It wakes up once a day and, for every
company in turn, runs the same logic the corresponding manual button uses
("Generate Due Invoices", "Run Depreciation") — a scheduled run and a manual click
produce identical results, just triggered differently. Running the check daily is
safe even though depreciation is monthly and invoices may be weekly/quarterly/yearly:
each job's own is_due check is what actually gates whether anything happens, so a
daily wake-up just means "due" items never wait more than a day past their date.

Each company is processed inside its own `test_request_context()` with
session["company_id"] set to that company — the cheapest way to reuse the existing
session-scoped helpers (current_company_id, scoped_query, log_audit) from a context
that isn't a real HTTP request. No user is logged in during a scheduled run, so
audit log entries land with user_id=None — visibly "the system", not a person.
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger("ledgerbooks.scheduler")

_scheduler = None  # module-level singleton — guards against starting it twice


def _run_across_companies(app, run_one_company, job_label):
    """Runs `run_one_company()` once per company, each inside its own request-like
    context with that company active. `run_one_company` takes no arguments and
    returns (results, skipped) — two lists of human-readable strings for logging.
    Never raises: a failure in one company is logged and skipped, not fatal to the rest.
    """
    from app.models import CompanySettings

    with app.app_context():
        from app import db

        companies = CompanySettings.query.all()
        total = 0
        for company in companies:
            with app.test_request_context():
                from flask import session
                session["company_id"] = company.id
                try:
                    results, skipped = run_one_company()
                except Exception:
                    db.session.rollback()
                    logger.exception("%s failed for company_id=%s", job_label, company.id)
                    continue
                total += len(results)
                if results:
                    logger.info("%s: company_id=%s -> %s", job_label, company.id, ", ".join(results))
                for message in skipped:
                    logger.warning("%s: company_id=%s skipped: %s", job_label, company.id, message)
        logger.info("%s complete: %s item(s) processed across %s company(ies).", job_label, total, len(companies))


def run_due_recurring_invoices(app):
    """Runs one pass across every company, generating whatever recurring invoices are due."""
    from app.sales import generate_due_invoices_for_current_company
    _run_across_companies(
        app, lambda: generate_due_invoices_for_current_company(source="scheduled"),
        "Recurring invoices",
    )


def run_due_asset_depreciation(app):
    """Runs one pass across every company, posting depreciation for whatever fixed
    assets are due (a no-op for any asset not yet due — see Asset.is_due)."""
    from app.assets import run_depreciation_for_current_company
    _run_across_companies(
        app, lambda: run_depreciation_for_current_company(source="scheduled"),
        "Asset depreciation",
    )


def init_scheduler(app, hour=6, minute=0):
    """Starts the daily background jobs, once per process. Call from create_app()
    after db.create_all() so every table these jobs touch is guaranteed to exist.

    `hour`/`minute` are local server time (24h) — default 06:00, before most
    businesses' working day starts. Override per job via app.config, e.g.
    app.config["RECURRING_INVOICE_HOUR"] = 2, app.config["ASSET_DEPRECIATION_MINUTE"] = 15
    (the two jobs run five minutes apart by default so they don't contend for the
    same DB connections at the exact same second).
    """
    global _scheduler
    if _scheduler is not None:
        return _scheduler  # already running in this process

    # Flask's dev-server reloader (debug=True) forks a second process; only the
    # child that actually serves requests should run the scheduler, or every due
    # item would be generated twice a day.
    import os
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return None

    inv_hour = app.config.get("RECURRING_INVOICE_HOUR", hour)
    inv_minute = app.config.get("RECURRING_INVOICE_MINUTE", minute)
    dep_hour = app.config.get("ASSET_DEPRECIATION_HOUR", hour)
    dep_minute = app.config.get("ASSET_DEPRECIATION_MINUTE", minute + 5)

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        run_due_recurring_invoices, "cron", hour=inv_hour, minute=inv_minute,
        args=[app], id="recurring_invoices_daily", replace_existing=True,
    )
    scheduler.add_job(
        run_due_asset_depreciation, "cron", hour=dep_hour, minute=dep_minute,
        args=[app], id="asset_depreciation_daily", replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "Scheduler started — recurring invoices at %02d:%02d, asset depreciation at %02d:%02d.",
        inv_hour, inv_minute, dep_hour, dep_minute,
    )
    _scheduler = scheduler
    return scheduler
