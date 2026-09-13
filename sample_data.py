"""Sample-data seeder — populates a freshly-seeded company with realistic
demo customers, service items and journal entries so the dashboard has
something to show on first login.

Safe to skip if data already exists.  Only ever seeds the FIRST company.
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()

from app import create_app, db
from app.models import (
    Account,
    CompanySettings,
    Customer,
    Item,
    JournalEntry,
    JournalLine,
    Vendor,
)

SAMPLE_CUSTOMERS = [
    ("Acme Trading Ltd",   "billing@acmetrading.mu",  "+230 5713 0011", "12 Royal Rd, Curepipe"),
    ("Blue Ocean Freight", "accounts@blueocean.mu",   "+230 5432 8877", "8 Port Louis Waterfront"),
    ("Sunrise Retail Co",  "hello@sunriseretail.mu",  "+230 5921 4433", "3 Bagatelle Mall, Moka"),
    ("Indigo Consulting",  "finance@indigoconsult.mu","+230 5555 1230", "44 Ebène Cyber City"),
    ("Palm Grove Hotels",  "ap@palmgrove.mu",         "+230 5641 9922", "Belle Mare Coast Rd"),
]

SAMPLE_VENDORS = [
    ("Central Power Utilities", "billing@centralpower.mu"),
    ("Riverside Office Rental", "invoicing@riverside.mu"),
    ("Everyday Office Supplies","orders@everyday.mu"),
]

SAMPLE_ITEMS = [
    # (sku, name, item_type, unit, sales_price)
    ("SVC-CONSULT", "Advisory hour",       "service", "hr",  2500.00),
    ("SVC-AUDIT",   "Compliance audit",    "service", "pkg", 18000.00),
    ("SVC-TRAIN",   "On-site training day", "service", "day", 12000.00),
    ("SVC-SUPPORT", "Support retainer",     "service", "mo",  9500.00),
]


def _acc(company_id, code):
    return Account.query.filter_by(company_id=company_id, code=code).first()


def _post(company_id, entry_date, memo, lines, source="manual"):
    """lines: list of (account_code, debit_amount, credit_amount)."""
    je = JournalEntry(
        company_id=company_id, entry_date=entry_date,
        memo=memo, source_type=source,
    )
    db.session.add(je)
    db.session.flush()
    for code, deb, cred in lines:
        acc = _acc(company_id, code)
        db.session.add(JournalLine(
            journal_entry_id=je.id, account_id=acc.id,
            debit=Decimal(str(round(deb, 2))), credit=Decimal(str(round(cred, 2))),
        ))
    return je


def seed_demo_data():
    app = create_app()
    with app.app_context():
        company = CompanySettings.query.first()
        if company is None:
            print("[sample_data] No company found — run seed.py first.")
            return
        if Customer.query.filter_by(company_id=company.id).count() > 0:
            print("[sample_data] Company already has data — skipping demo seed.")
            return

        # ── Customers & Vendors ──────────────────────────────────
        for name, email, phone, addr in SAMPLE_CUSTOMERS:
            db.session.add(Customer(
                company_id=company.id, name=name, email=email,
                phone=phone, address=addr, opening_balance=0,
            ))
        for name, email in SAMPLE_VENDORS:
            db.session.add(Vendor(
                company_id=company.id, name=name, email=email,
                opening_balance=0,
            ))

        # ── Items ────────────────────────────────────────────────
        sales_acc = _acc(company.id, "4000")
        for sku, name, ityp, unit, price in SAMPLE_ITEMS:
            db.session.add(Item(
                company_id=company.id, sku=sku, name=name, item_type=ityp,
                unit=unit, sales_price=Decimal(str(price)),
                income_account_id=sales_acc.id,
            ))
        db.session.commit()

        # ── Journal entries: opening cash, income, expenses ──────
        today = date.today()
        random.seed(42)

        # 1) Opening cash injection (owner's equity → bank)  ~35 days ago
        _post(company.id, today - timedelta(days=35),
              "Opening owner contribution",
              [("1010", 250000, 0), ("3000", 0, 250000)])

        # 2) Sales revenue posts (Bank / Cash ← Sales Revenue) sprinkled over 30 days
        sales_events = [
            (28, 45000, "Consulting engagement — Acme"),
            (24, 12000, "Training day — Blue Ocean"),
            (20, 18000, "Compliance audit — Indigo"),
            (15,  9500, "Support retainer — Palm Grove"),
            (11, 22500, "Advisory hours — Sunrise"),
            (7,  15000, "Advisory hours — Acme"),
            (4,   9500, "Support retainer — Indigo"),
            (1,  12000, "Training day — Palm Grove"),
        ]
        for days_ago, amount, memo in sales_events:
            _post(company.id, today - timedelta(days=days_ago),
                  memo, [("1010", amount, 0), ("4000", 0, amount)])

        # 3) Operating expenses — Rent, Utilities, Salaries, Supplies, Misc
        expense_events = [
            (29, 35000, "6200", "Monthly salaries"),
            (28, 25000, "6000", "Office rent — month 1"),
            (27,  4200, "6100", "Electricity bill"),
            (18,  2800, "6300", "Office supplies — quarterly"),
            (14,  1800, "6100", "Internet & phone"),
            (10,  6500, "6900", "Client entertainment"),
            (5,  35000, "6200", "Fortnight salaries"),
            (3,  25000, "6000", "Office rent — month 2"),
            (2,   950,  "6300", "Printer toner"),
        ]
        for days_ago, amount, exp_code, memo in expense_events:
            _post(company.id, today - timedelta(days=days_ago), memo,
                  [(exp_code, amount, 0), ("1010", 0, amount)])

        db.session.commit()
        print(f"[sample_data] Seeded {len(SAMPLE_CUSTOMERS)} customers, "
              f"{len(SAMPLE_VENDORS)} vendors, {len(SAMPLE_ITEMS)} items, "
              f"{1 + len(sales_events) + len(expense_events)} journal entries.")


if __name__ == "__main__":
    seed_demo_data()
