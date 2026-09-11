"""One-time backfill: adds the three Chart-of-Accounts entries the new Fixed Asset
Register needs (Accumulated Depreciation, Gain/Loss on Disposal of Assets,
Depreciation Expense) to every existing company. New companies get these
automatically from app/company.py's STARTER_ACCOUNTS; this script catches up
companies that were created before that list was extended.

Safe to re-run: every account is only inserted if that company doesn't already
have one with the same code.

    venv\\Scripts\\python.exe add_asset_accounts.py
"""
from app import create_app, db
from app.models import Account, CompanySettings

NEW_ACCOUNTS = [
    ("1590", "Accumulated Depreciation", "Asset", "Fixed Asset"),
    ("4910", "Gain/Loss on Disposal of Assets", "Income", None),
    ("6400", "Depreciation Expense", "Expense", "Operating Expense"),
]


def run():
    app = create_app()
    with app.app_context():
        companies = CompanySettings.query.all()
        added = 0
        for company in companies:
            for code, name, acc_type, subtype in NEW_ACCOUNTS:
                if not Account.query.filter_by(company_id=company.id, code=code).first():
                    db.session.add(Account(
                        company_id=company.id, code=code, name=name,
                        account_type=acc_type, subtype=subtype, is_system=True,
                    ))
                    added += 1
                    print(f"  + {company.business_name}: {code} {name}")
        db.session.commit()
        print(f"Done — {added} account(s) added across {len(companies)} company(ies).")


if __name__ == "__main__":
    run()
