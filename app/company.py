"""Company (tenant) provisioning — creating a new company + its owner + starter
Chart of Accounts in one step, so no user is ever left without a home company.
Shared by seed.py (first company) and auth.py (/register, further companies).
"""
from werkzeug.security import generate_password_hash

from app import db
from app.models import Account, CompanySettings, User, UserCompany

# code, name, type, subtype
STARTER_ACCOUNTS = [
    ("1000", "Cash on Hand", "Asset", "Cash and Cash Equivalents"),
    ("1010", "Bank Account", "Asset", "Cash and Cash Equivalents"),
    ("1100", "Undeposited Funds", "Asset", "Current Asset"),  # holding account until payments are batch-deposited
    ("1200", "Accounts Receivable", "Asset", "Current Asset"),
    ("1300", "Inventory", "Asset", "Current Asset"),
    ("1500", "Equipment", "Asset", "Fixed Asset"),
    ("1590", "Accumulated Depreciation", "Asset", "Fixed Asset"),  # contra-asset — carries a credit balance
    ("2000", "Accounts Payable", "Liability", "Current Liability"),
    ("2100", "VAT Payable", "Liability", "Current Liability"),  # Mauritius VAT collected on sales
    ("2110", "VAT Receivable", "Asset", "Current Asset"),        # Mauritius VAT paid on purchases
    ("2500", "Loans Payable", "Liability", "Long-Term Liability"),
    ("3000", "Owner's Equity", "Equity", None),
    ("3900", "Retained Earnings", "Equity", None),
    ("4000", "Sales Revenue", "Income", None),
    ("4900", "Other Income", "Income", None),
    ("4910", "Gain/Loss on Disposal of Assets", "Income", None),  # a loss posts as a debit here, reducing it
    ("4920", "Realized Gain/Loss on Exchange", "Income", None),  # a loss posts as a debit here, reducing it
    ("5000", "Cost of Goods Sold", "Expense", "Cost of Goods Sold"),
    ("6000", "Rent Expense", "Expense", "Operating Expense"),
    ("6100", "Utilities Expense", "Expense", "Operating Expense"),
    ("6200", "Salaries & Wages", "Expense", "Operating Expense"),
    ("6300", "Office Supplies", "Expense", "Operating Expense"),
    ("6210", "Employer Statutory Contributions", "Expense", "Operating Expense"),  # employer's CSG/NSF/HRDC/PRGF/levy share
    ("6400", "Depreciation Expense", "Expense", "Operating Expense"),
    ("6900", "Miscellaneous Expense", "Expense", "Operating Expense"),
    # Payroll bridge liabilities — populated by app/payroll_bridge.py from an
    # external payroll system's Finalized run, not entered manually.
    ("2300", "PAYE Payable", "Liability", "Current Liability"),
    ("2310", "CSG Payable", "Liability", "Current Liability"),
    ("2320", "NSF Payable", "Liability", "Current Liability"),
    ("2330", "HRDC Levy Payable", "Liability", "Current Liability"),
    ("2340", "PRGF Payable", "Liability", "Current Liability"),
    ("2350", "Foreign Worker Levy Payable", "Liability", "Current Liability"),
    ("2360", "Net Salaries Payable", "Liability", "Current Liability"),
    ("2370", "Payroll Deductions Payable", "Liability", "Current Liability"),  # loan/other employee deductions withheld
]


def seed_default_accounts(company_id):
    for code, name, acc_type, subtype in STARTER_ACCOUNTS:
        if not Account.query.filter_by(company_id=company_id, code=code).first():
            db.session.add(
                Account(
                    company_id=company_id, code=code, name=name,
                    account_type=acc_type, subtype=subtype, is_system=True,
                )
            )
    db.session.commit()


def create_company_and_owner(business_name, username, password, name):
    """Registers a brand new tenant: a company_settings row, a starter Chart of
    Accounts, and its first (owner) user — granted access via UserCompany so
    login always resolves to at least one accessible company.
    """
    company = CompanySettings(business_name=business_name)
    db.session.add(company)
    db.session.flush()  # need company.id before seeding accounts / creating the user

    seed_default_accounts(company.id)

    owner = User(
        company_id=company.id, name=name, username=username,
        password_hash=generate_password_hash(password), role="owner",
    )
    db.session.add(owner)
    db.session.flush()

    db.session.add(UserCompany(user_id=owner.id, company_id=company.id))
    db.session.commit()
    return company, owner


def add_user_to_company(company_id, username, password, name, role="accountant"):
    """Creates a brand-new login, defaulting to this company on login. Use
    grant_company_access() separately to add access to further companies."""
    user = User(
        company_id=company_id, name=name, username=username,
        password_hash=generate_password_hash(password), role=role,
    )
    db.session.add(user)
    db.session.flush()
    db.session.add(UserCompany(user_id=user.id, company_id=company_id))
    db.session.commit()
    return user


def grant_company_access(user_id, company_id):
    if not UserCompany.query.filter_by(user_id=user_id, company_id=company_id).first():
        db.session.add(UserCompany(user_id=user_id, company_id=company_id))
        db.session.commit()


def revoke_company_access(user_id, company_id):
    UserCompany.query.filter_by(user_id=user_id, company_id=company_id).delete()
    db.session.commit()
