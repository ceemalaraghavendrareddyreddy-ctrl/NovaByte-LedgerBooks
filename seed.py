"""Creates the database (if needed, and if it's MySQL — a managed Postgres
database like Render's already exists), then the first company, its starter
Chart of Accounts, and an owner login. Run once after cloning, or once against
a fresh deploy:

    python seed.py
"""
import os

from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "quickbooks_clone")


def ensure_database_exists():
    """MySQL-only — a managed Postgres database (Render, etc.) is provisioned
    by the host itself before DATABASE_URL is ever handed to the app, so
    there's nothing to create here in that case."""
    if os.environ.get("DATABASE_URL"):
        return
    import pymysql
    conn = pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS {DB_NAME} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
        print(f"Database '{DB_NAME}' ready.")
    finally:
        conn.close()


def seed_data():
    """Delegates to the same company-provisioning path /register uses
    (app/company.py's create_company_and_owner), so a freshly seeded company
    gets exactly the same Chart of Accounts a real sign-up would — one
    STARTER_ACCOUNTS list, not two copies that can drift apart."""
    from app import create_app
    from app.company import create_company_and_owner
    from app.models import CompanySettings

    app = create_app()
    with app.app_context():
        if CompanySettings.query.first():
            print("A company already exists — nothing to seed. (Use /register in the app for another.)")
            return

        create_company_and_owner(
            business_name="My Business",
            username="admin",
            password="admin123",
            name="Admin",
        )
        print("Created company 'My Business' with login: admin / admin123  (change this password!)")


if __name__ == "__main__":
    ensure_database_exists()
    seed_data()
