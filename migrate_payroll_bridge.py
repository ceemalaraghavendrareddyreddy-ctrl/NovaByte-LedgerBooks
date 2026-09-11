"""One-time migration: adds the payroll bridge to LedgerBooks.

Adds company_settings.payroll_api_key (nullable — a company simply doesn't
accept payroll pushes until someone generates one from Settings) and backfills
the Chart-of-Accounts entries the bridge's journal entry needs into every
existing company. New companies get these automatically from
app/company.py's STARTER_ACCOUNTS; this catches up ones created earlier.

Safe to re-run: every step checks before acting.

    venv\\Scripts\\python.exe migrate_payroll_bridge.py
"""
import os

import pymysql
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "quickbooks_clone")

REQUIRED_ACCOUNTS = [
    ("6210", "Employer Statutory Contributions", "Expense", "Operating Expense"),
    ("2300", "PAYE Payable", "Liability", "Current Liability"),
    ("2310", "CSG Payable", "Liability", "Current Liability"),
    ("2320", "NSF Payable", "Liability", "Current Liability"),
    ("2330", "HRDC Levy Payable", "Liability", "Current Liability"),
    ("2340", "PRGF Payable", "Liability", "Current Liability"),
    ("2350", "Foreign Worker Levy Payable", "Liability", "Current Liability"),
    ("2360", "Net Salaries Payable", "Liability", "Current Liability"),
    ("2370", "Payroll Deductions Payable", "Liability", "Current Liability"),
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


def main():
    conn = connect()
    cur = conn.cursor()
    try:
        if not column_exists(cur, "company_settings", "payroll_api_key"):
            cur.execute("ALTER TABLE company_settings ADD COLUMN payroll_api_key VARCHAR(64) NULL")
            print("  added company_settings.payroll_api_key")
        else:
            print("  (skip) company_settings.payroll_api_key already exists.")

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
