"""One-time migration: adds company_settings.logo_data.

Written to be portable across MySQL (local dev) and Postgres (Render) — both
speak the same ALTER TABLE ... ADD COLUMN ... TEXT syntax, so this uses
SQLAlchemy's engine/inspector directly instead of a dialect-specific script,
unlike the earlier MySQL-only migrate_*.py scripts.

Safe to re-run: checks the column exists before adding it.

    venv\\Scripts\\python.exe migrate_company_logo.py
"""
from sqlalchemy import inspect, text

from app import create_app, db


def main():
    app = create_app()
    with app.app_context():
        inspector = inspect(db.engine)
        columns = {col["name"] for col in inspector.get_columns("company_settings")}
        if "logo_data" in columns:
            print("(skip) company_settings.logo_data already exists.")
            return
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE company_settings ADD COLUMN logo_data TEXT"))
        print("Added company_settings.logo_data.")


if __name__ == "__main__":
    main()
