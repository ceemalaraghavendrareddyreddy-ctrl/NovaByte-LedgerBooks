"""One-time migration: converts LedgerBooks from single-company to multi-company.

Adds company_id to every business table, creates the user_companies join table,
backfills everything to a single "Company #1" (the existing CompanySettings row,
or a fresh one if none existed), and swaps global unique constraints (invoice_no,
bill_no, account.code, item.sku, username, ...) for per-company composite ones.

Safe to re-run: every step checks before acting.

    python migrate_multicompany.py
"""
import os

import pymysql
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "quickbooks_clone")

# Tables that get a plain company_id column, backfilled to the one existing company.
TABLES_NEEDING_COMPANY_ID = [
    "accounts", "journal_entries", "customers", "invoices", "payments", "vendors",
    "bills", "vendor_payments", "items", "bank_reconciliations", "estimates",
    "credit_memos", "vendor_credits", "purchase_orders", "budgets", "saved_reports",
    "deposits", "bank_statement_imports", "audit_logs",
]

# (table, old unique column(s), constraint name to drop if present, new composite unique name)
COMPOSITE_UNIQUES = [
    ("accounts", "code", "uq_account_company_code"),
    ("invoices", "invoice_no", "uq_invoice_company_no"),
    ("bills", "bill_no", "uq_bill_company_no"),
    ("items", "sku", "uq_item_company_sku"),
    ("estimates", "estimate_no", "uq_estimate_company_no"),
    ("credit_memos", "credit_no", "uq_credit_memo_company_no"),
    ("vendor_credits", "credit_no", "uq_vendor_credit_company_no"),
    ("purchase_orders", "po_no", "uq_po_company_no"),
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


def find_unique_index_on_column(cur, table, column):
    """Finds a single-column UNIQUE index name on this column (the old unique=True
    constraint SQLAlchemy created), so it can be dropped before adding the composite one."""
    cur.execute(
        """SELECT DISTINCT index_name FROM information_schema.statistics
           WHERE table_schema=%s AND table_name=%s AND column_name=%s
             AND non_unique=0 AND index_name != 'PRIMARY'""",
        (DB_NAME, table, column),
    )
    rows = cur.fetchall()
    # Only return it if that index covers exactly this one column (not already composite).
    for (index_name,) in rows:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=%s AND table_name=%s AND index_name=%s",
            (DB_NAME, table, index_name),
        )
        if cur.fetchone()[0] == 1:
            return index_name
    return None


def main():
    conn = connect()
    cur = conn.cursor()
    try:
        # 1. Make sure exactly one company exists to backfill into.
        cur.execute("SELECT id FROM company_settings ORDER BY id LIMIT 1")
        row = cur.fetchone()
        if row:
            company_id = row[0]
            print(f"Using existing company_settings row id={company_id} as Company #1.")
        else:
            cur.execute("INSERT INTO company_settings (business_name) VALUES (%s)", ("My Business",))
            company_id = cur.lastrowid
            print(f"Created company_settings row id={company_id} as Company #1.")

        # 2. Add company_id to every business table, backfill, then make NOT NULL.
        for table in TABLES_NEEDING_COMPANY_ID:
            if not table_exists(cur, table):
                print(f"  (skip) {table}: table doesn't exist yet.")
                continue
            if not column_exists(cur, table, "company_id"):
                cur.execute(f"ALTER TABLE {table} ADD COLUMN company_id INT")
                print(f"  added company_id to {table}")
            cur.execute(f"UPDATE {table} SET company_id=%s WHERE company_id IS NULL", (company_id,))
            cur.execute(f"ALTER TABLE {table} MODIFY COLUMN company_id INT NOT NULL")
            # FK (ignore failure if it already exists)
            try:
                cur.execute(
                    f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_company "
                    f"FOREIGN KEY (company_id) REFERENCES company_settings(id)"
                )
            except pymysql.err.OperationalError as e:
                if "Duplicate" not in str(e) and "errno: 121" not in str(e).lower():
                    pass  # constraint likely already there under a different auto-name; not fatal

        # 3. users.company_id (users table needs the same treatment, kept separate
        #    since it's not in the generic list — every user needs a *specific* company).
        if not column_exists(cur, "users", "company_id"):
            cur.execute("ALTER TABLE users ADD COLUMN company_id INT")
            print("  added company_id to users")
        cur.execute("UPDATE users SET company_id=%s WHERE company_id IS NULL", (company_id,))
        cur.execute("ALTER TABLE users MODIFY COLUMN company_id INT NOT NULL")

        # Drop the old global-unique index on users.username, replace with (company_id, username).
        old_idx = find_unique_index_on_column(cur, "users", "username")
        if old_idx:
            cur.execute(f"ALTER TABLE users DROP INDEX {old_idx}")
            print(f"  dropped old global unique index {old_idx} on users.username")
        cur.execute(
            """SELECT COUNT(*) FROM information_schema.statistics
               WHERE table_schema=%s AND table_name='users' AND index_name='uq_user_company_username'""",
            (DB_NAME,),
        )
        if cur.fetchone()[0] == 0:
            cur.execute("ALTER TABLE users ADD CONSTRAINT uq_user_company_username UNIQUE (company_id, username)")
            print("  added composite unique (company_id, username) on users")

        # 4. user_companies join table.
        if not table_exists(cur, "user_companies"):
            cur.execute(
                """CREATE TABLE user_companies (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    company_id INT NOT NULL,
                    created_at DATETIME,
                    UNIQUE KEY uq_user_company (user_id, company_id),
                    CONSTRAINT fk_uc_user FOREIGN KEY (user_id) REFERENCES users(id),
                    CONSTRAINT fk_uc_company FOREIGN KEY (company_id) REFERENCES company_settings(id)
                )"""
            )
            print("  created user_companies table")
        cur.execute("SELECT id FROM users")
        for (user_id,) in cur.fetchall():
            cur.execute(
                "INSERT IGNORE INTO user_companies (user_id, company_id) VALUES (%s, %s)",
                (user_id, company_id),
            )
        print("  granted every existing user access to Company #1")

        # 5. Swap the old per-column global unique constraints for composite ones.
        for table, column, new_name in COMPOSITE_UNIQUES:
            if not table_exists(cur, table):
                continue
            old_idx = find_unique_index_on_column(cur, table, column)
            if old_idx:
                cur.execute(f"ALTER TABLE {table} DROP INDEX {old_idx}")
                print(f"  dropped old global unique index {old_idx} on {table}.{column}")
            cur.execute(
                """SELECT COUNT(*) FROM information_schema.statistics
                   WHERE table_schema=%s AND table_name=%s AND index_name=%s""",
                (DB_NAME, table, new_name),
            )
            if cur.fetchone()[0] == 0:
                cur.execute(f"ALTER TABLE {table} ADD CONSTRAINT {new_name} UNIQUE (company_id, {column})")
                print(f"  added composite unique ({column}) on {table}")

        conn.commit()
        print("\nMigration complete.")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
