"""One-time migration: adds multi-currency support to LedgerBooks.

Unlike the Recurring Invoices and Fixed Asset Register features, this one touches
tables that already hold real data (invoices, bills, payments, vendor_payments,
company_settings), so it needs real ALTER TABLE statements rather than relying on
db.create_all() (which only ever creates brand-new tables, never alters existing ones).

Every existing row gets currency='MUR' and exchange_rate=1.000000 by column DEFAULT —
mathematically a no-op (base_amount = amount * 1.0 = amount), so nothing about any
past invoice, bill, or payment changes. Also backfills the three multi-currency/asset
accounts (1590, 4910, 4920, 6400) the same way add_asset_accounts.py does, in case
that script wasn't run first.

Safe to re-run: every step checks before acting.

    venv\\Scripts\\python.exe migrate_multicurrency.py
"""
import os

import pymysql
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "quickbooks_clone")

# (table, column, DDL type+default)
CURRENCY_COLUMNS = [
    ("invoices", "currency", "VARCHAR(3) NOT NULL DEFAULT 'MUR'"),
    ("invoices", "exchange_rate", "DECIMAL(12,6) NOT NULL DEFAULT 1.000000"),
    ("bills", "currency", "VARCHAR(3) NOT NULL DEFAULT 'MUR'"),
    ("bills", "exchange_rate", "DECIMAL(12,6) NOT NULL DEFAULT 1.000000"),
    ("payments", "currency", "VARCHAR(3) NOT NULL DEFAULT 'MUR'"),
    ("payments", "exchange_rate", "DECIMAL(12,6) NOT NULL DEFAULT 1.000000"),
    ("vendor_payments", "currency", "VARCHAR(3) NOT NULL DEFAULT 'MUR'"),
    ("vendor_payments", "exchange_rate", "DECIMAL(12,6) NOT NULL DEFAULT 1.000000"),
    ("company_settings", "base_currency", "VARCHAR(3) NOT NULL DEFAULT 'MUR'"),
]

# Accounts the FX gain/loss and asset-depreciation postings need. (code, name, type, subtype)
REQUIRED_ACCOUNTS = [
    ("1590", "Accumulated Depreciation", "Asset", "Fixed Asset"),
    ("4910", "Gain/Loss on Disposal of Assets", "Income", None),
    ("4920", "Realized Gain/Loss on Exchange", "Income", None),
    ("6400", "Depreciation Expense", "Expense", "Operating Expense"),
]


def connect():
    return pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD, database=DB_NAME, autocommit=False)


def column_exists(cur, table, column):
    cur.execute(
        """SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema=%s AND table_name=%s AND column_name=%s""",
        (DB_NAME, table, column),
    )
    return cur.fetchone()[0] > 0


def table_exists(cur, table):
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=%s AND table_name=%s",
        (DB_NAME, table),
    )
    return cur.fetchone()[0] > 0


def main():
    conn = connect()
    cur = conn.cursor()
    try:
        for table, column, ddl in CURRENCY_COLUMNS:
            if not table_exists(cur, table):
                print(f"  (skip) {table}: table doesn't exist.")
                continue
            if column_exists(cur, table, column):
                print(f"  (skip) {table}.{column} already exists.")
                continue
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            print(f"  added {table}.{column}")

        cur.execute("SELECT id, business_name FROM company_settings")
        companies = cur.fetchall()
        added = 0
        for company_id, business_name in companies:
            for code, name, acc_type, subtype in REQUIRED_ACCOUNTS:
                cur.execute(
                    "SELECT COUNT(*) FROM accounts WHERE company_id=%s AND code=%s", (company_id, code)
                )
                if cur.fetchone()[0] == 0:
                    cur.execute(
                        """INSERT INTO accounts (company_id, code, name, account_type, subtype, is_active, is_system, opening_balance)
                           VALUES (%s, %s, %s, %s, %s, 1, 1, 0)""",
                        (company_id, code, name, acc_type, subtype),
                    )
                    added += 1
                    print(f"  + {business_name}: {code} {name}")

        conn.commit()
        print(f"\nMigration complete. {added} account(s) added across {len(companies)} company(ies).")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
