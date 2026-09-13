"""Granular module access for non-owner users. Owners always have full access —
this only ever narrows what an "accountant"-role user can reach, and only for
the modules listed here. Settings, User Management, Audit Log, Backup/Restore,
and the MRA sync retry queue stay owner-only regardless (see @owner_required
in app/auth.py) — those are account-level administration, not day-to-day
bookkeeping, so they were never meant to be grantable to staff in the first place.

A user with no `permissions` value set (NULL/empty) is unrestricted — this is
what every user created before this feature existed already has, so nobody's
access silently changed the day this shipped.
"""
from functools import wraps

from flask import abort
from flask_login import current_user

MODULES = [
    ("sales", "Sales", "Customers, invoices, estimates, credit memos, recurring invoices"),
    ("purchases", "Purchases", "Vendors, bills, purchase orders, vendor credits"),
    ("banking", "Banking", "Bank accounts, transfers, deposits, reconciliation"),
    ("inventory", "Inventory", "Items and stock adjustments"),
    ("assets", "Fixed Assets", "Asset register and depreciation"),
    ("budgets", "Budgets", "Budget entry and budget-vs-actual"),
    ("ledger", "Accounting", "Chart of accounts, manual journal entries, trial balance"),
    ("reports", "Reports", "P&L, Balance Sheet, Cash Flow, VAT Return, custom reports"),
]
MODULE_KEYS = {key for key, _, _ in MODULES}


def module_required(module_key):
    """Gates every route in a blueprint via that blueprint's before_request —
    see app/__init__.py's register_module_guards() — rather than decorating
    each view individually, so a route added later is covered automatically."""
    def check():
        if not current_user.is_authenticated:
            return  # login_required elsewhere handles the actual redirect
        if not current_user.can_use_module(module_key):
            abort(403)
    return check


def register_module_guards(app, blueprint_module_map):
    """blueprint_module_map: {blueprint_object: module_key}. Registers one
    before_request per blueprint that 403s a user who isn't allowed into it."""
    for blueprint, module_key in blueprint_module_map.items():
        blueprint.before_request(module_required(module_key))
