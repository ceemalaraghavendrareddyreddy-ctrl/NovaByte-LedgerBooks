"""Adds a recurring invoice example and a completed bank reconciliation to the
EXISTING 'demo' company that seed_demo_company.py already created — without
creating a second demo company or touching anything else in the database.

Logs in as the demo user instead of registering a new one, then posts through
the exact same routes the Recurring Invoices and Bank Reconciliation screens
use.

USAGE (same DATABASE_URL rules as seed_demo_company.py — unset it, or set it,
depending on whether you mean local or Render production):
    python extend_demo_company.py [username] [password]

Defaults to username "demo", password "demo12345" if not given.
"""
import sys
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402
from seed_demo_company import post, recurring_fields  # noqa: E402


def main():
    username = sys.argv[1] if len(sys.argv) > 1 else "demo"
    password = sys.argv[2] if len(sys.argv) > 2 else "demo12345"

    app = create_app()
    today = date.today()
    client = app.test_client()

    with app.app_context():
        from app.models import User

        user = User.query.filter_by(username=username).first()
        if not user:
            print(f"No user '{username}' found in this database — check DATABASE_URL points at the right one.")
            sys.exit(1)
        company_id = user.company_id

    login_resp = post(client, "/login", {"username": username, "password": password})
    if "Invalid username or password" in login_resp.get_data(as_text=True):
        print(f"Login failed for '{username}' — wrong password?")
        sys.exit(1)

    with app.app_context():
        from app.models import Account, Customer

        acc = {a.code: a.id for a in Account.query.filter_by(company_id=company_id).all()}
        customer = Customer.query.filter_by(company_id=company_id, name="Coral Bay Hotels").first()
        if not customer:
            customer = Customer.query.filter_by(company_id=company_id).order_by(Customer.id).first()
        if not customer:
            print("This company has no customers yet — run seed_demo_company.py first.")
            sys.exit(1)
        customer_id, customer_name = customer.id, customer.name
        income_4000 = acc["4000"]

    # ---- Recurring invoice: an active monthly retainer template, plus one ----
    # invoice already generated from it.
    post(client, "/sales/recurring/new", recurring_fields(
        customer_id, income_4000, f"Monthly Software Retainer - {customer_name}",
        today - timedelta(days=35),
        [("Monthly software support & hosting retainer", 1, 15000, True)],
        memo="Recurring monthly retainer",
    ))
    with app.app_context():
        from app.models import RecurringInvoice

        template = (
            RecurringInvoice.query.filter_by(company_id=company_id)
            .order_by(RecurringInvoice.id.desc()).first()
        )
        template_id, template_name = template.id, template.name
    post(client, f"/sales/recurring/{template_id}/generate", {})
    print(f"Recurring invoice template '{template_name}' created — one invoice already generated from it.")

    # ---- Bank reconciliation: reconcile every uncleared Bank Account line ----
    # against a matching statement balance, and complete the session.
    with app.app_context():
        from app.models import JournalLine

        bank_lines = JournalLine.query.filter_by(account_id=acc["1010"], is_cleared=False).all()
        bank_line_ids = [line.id for line in bank_lines]
        bank_balance = sum(float(line.debit) - float(line.credit) for line in bank_lines)

    if not bank_line_ids:
        print("No uncleared Bank Account transactions found — nothing to reconcile.")
        return

    post(client, "/banking/reconcile", {
        "account_id": str(acc["1010"]),
        "statement_date": today.isoformat(),
        "statement_ending_balance": f"{bank_balance:.2f}",
    })
    with app.app_context():
        from app.models import BankReconciliation

        recon = (
            BankReconciliation.query.filter_by(company_id=company_id)
            .order_by(BankReconciliation.id.desc()).first()
        )
        recon_id = recon.id
    post(client, f"/banking/reconcile/{recon_id}", [("line_id", str(i)) for i in bank_line_ids] + [("action", "finish")])
    print(f"Bank Account reconciled and completed as of {today.isoformat()} (statement balance MUR {bank_balance:,.2f}).")


if __name__ == "__main__":
    main()
