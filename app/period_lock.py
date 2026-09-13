"""Period locking — once a company sets CompanySettings.locked_through_date,
no journal entry dated on or before that date can be created, edited, or
deleted, from anywhere.

Enforced with SQLAlchemy events on JournalEntry itself rather than a check
sprinkled into every route that can post one (invoices, bills, payments,
credit memos, vendor credits, transfers, deposits, manual journal entries,
depreciation, the payroll bridge...). That list only grows over time; an
event on the model catches every existing and future caller the same way,
so a route nobody remembered to guard can't become a silent hole in the lock.

The one thing this can't do from inside a `before_insert`/`before_update`
listener is roll back a whole request cleanly and return a nice page — it
raises PeriodLockedError instead, which the app-level errorhandler in
app/__init__.py turns into a flash message and a redirect.
"""
from sqlalchemy import event, inspect

from app.models import CompanySettings, JournalEntry


class PeriodLockedError(Exception):
    """Raised when a journal entry write would fall on or before the
    company's locked-through date."""


def _company_lock_date(company_id):
    company = CompanySettings.query.get(company_id)
    return company.locked_through_date if company else None


def assert_period_open(entry_date, company_id):
    """Raises PeriodLockedError if entry_date falls on or before the company's
    lock date. Exposed publicly so a route that's about to mutate an existing
    entry (edit/delete) can check the entry's *current* date up front, before
    touching anything — belt-and-suspenders alongside the events below, which
    only reliably fire when the entry's own columns (not just its lines) change."""
    lock_date = _company_lock_date(company_id)
    if lock_date and entry_date and entry_date <= lock_date:
        raise PeriodLockedError(
            f"{entry_date.strftime('%d %b %Y')} falls within the locked period "
            f"(figures are locked through {lock_date.strftime('%d %b %Y')}). "
            f"Ask the owner to move the lock date in Settings before changing anything here."
        )


@event.listens_for(JournalEntry, "before_insert")
def _guard_insert(mapper, connection, target):
    assert_period_open(target.entry_date, target.company_id)


@event.listens_for(JournalEntry, "before_update")
def _guard_update(mapper, connection, target):
    # Checks the NEW date (blocks moving any entry's date into the locked period)
    # and, separately, the OLD date if it changed (blocks editing an entry that
    # was already sitting inside the locked period, even if the edit tries to
    # move it out — the point where it happened is still locked).
    assert_period_open(target.entry_date, target.company_id)
    history = inspect(target).attrs.entry_date.history
    if history.deleted:
        assert_period_open(history.deleted[0], target.company_id)


@event.listens_for(JournalEntry, "before_delete")
def _guard_delete(mapper, connection, target):
    assert_period_open(target.entry_date, target.company_id)
