"""Creates a self-contained DEMO company with realistic, entirely fictitious
sample data — customers, vendors, invoices (paid/partial/open/overdue,
including one multi-currency invoice), bills, and payments — without
touching any other company already in the database. Safe to re-run: it
always registers a brand-new company under its own login, never edits an
existing one.

Built for showing a client the app without their screen displaying anyone's
real production numbers (e.g. your own company's books, or the Loomstack
payroll data used for testing).

Goes through the exact same routes a real user clicks — /register,
/sales/customers/new, /sales/invoices/new, etc. — via Flask's test client,
so every entry is posted to the ledger the normal way (balanced journal
entries, VAT, FX) rather than being hand-inserted into the database.

USAGE

  Local MySQL (default — uses the same .env this app already reads):
      python seed_demo_company.py

  Render production Postgres — this app's free web service has no Shell to
  run this from directly, so run it from your own machine against the
  database instead. Get the "External Database URL" from the Render
  dashboard's ledgerbooks-db page (Info tab), then in PowerShell:
      $env:DATABASE_URL = "postgresql://user:password@host/dbname"
      python seed_demo_company.py
  Unset it afterwards (or open a fresh terminal) so your NEXT `python run.py`
  goes back to your local MySQL database instead of production.
"""
import sys
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402


def unique_username(base="demo"):
    from app.models import User

    n = 0
    while True:
        candidate = base if n == 0 else f"{base}{n}"
        if not User.query.filter_by(username=candidate).first():
            return candidate
        n += 1


def group_fields(pairs):
    """Werkzeug's test client wants a dict (list values become repeated form
    fields), not a list of (key, value) tuples — group same-named fields
    (invoice/bill line items, applied payments) into lists here."""
    result = {}
    for key, value in pairs:
        if key in result:
            if isinstance(result[key], list):
                result[key].append(value)
            else:
                result[key] = [result[key], value]
        else:
            result[key] = value
    return result


def post(client, url, fields):
    data = group_fields(fields) if isinstance(fields, list) else fields
    resp = client.post(url, data=data, follow_redirects=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"POST {url} failed: HTTP {resp.status_code}\n{resp.get_data(as_text=True)[:1000]}")
    body = resp.get_data(as_text=True)
    if "error" in body.lower() and "flash-error" in body:
        # A validation flash rendered instead of the redirect we expected — surface it
        # rather than silently leaving the demo data half-built.
        print(f"  [warning] {url} may not have saved as expected — check the flash message on the live app.")
    return resp


def invoice_fields(customer_id, income_account_id, invoice_date, due_date, lines, currency="MUR", exchange_rate="1.0", memo=""):
    fields = [
        ("customer_id", str(customer_id)),
        ("invoice_date", invoice_date.isoformat()),
        ("due_date", due_date.isoformat()),
        ("memo", memo),
        ("vat_rate", "15.00"),
        ("currency", currency),
        ("exchange_rate", str(exchange_rate)),
    ]
    for i, (desc, qty, price, taxable) in enumerate(lines):
        fields += [
            ("description", desc), ("quantity", str(qty)), ("unit_price", str(price)),
            ("income_account_id", str(income_account_id)), ("item_id", ""),
        ]
        if taxable:
            fields.append(("taxable", str(i)))
    return fields


def bill_fields(vendor_id, expense_account_id, bill_date, due_date, lines, currency="MUR", exchange_rate="1.0", memo=""):
    fields = [
        ("vendor_id", str(vendor_id)),
        ("bill_date", bill_date.isoformat()),
        ("due_date", due_date.isoformat()),
        ("memo", memo),
        ("vat_rate", "15.00"),
        ("currency", currency),
        ("exchange_rate", str(exchange_rate)),
    ]
    for i, (desc, qty, price, taxable) in enumerate(lines):
        fields += [
            ("description", desc), ("quantity", str(qty)), ("unit_price", str(price)),
            ("expense_account_id", str(expense_account_id)), ("item_id", ""),
        ]
        if taxable:
            fields.append(("taxable", str(i)))
    return fields


def customer_payment_fields(customer_id, deposit_account_id, payment_date, amount, applications, memo=""):
    fields = [
        ("customer_id", str(customer_id)), ("payment_date", payment_date.isoformat()),
        ("amount", str(amount)), ("currency", "MUR"), ("exchange_rate", "1.0"),
        ("method", "bank_transfer"), ("deposit_account_id", str(deposit_account_id)),
        ("reference_no", ""), ("memo", memo),
    ]
    for invoice_id, applied in applications:
        fields += [("apply_invoice_id", str(invoice_id)), ("apply_amount", str(applied))]
    return fields


def recurring_fields(customer_id, income_account_id, name, start_date, lines, frequency="monthly", due_days=30, memo=""):
    fields = [
        ("customer_id", str(customer_id)), ("name", name), ("start_date", start_date.isoformat()),
        ("end_date", ""), ("frequency", frequency), ("due_days", str(due_days)), ("memo", memo),
        ("vat_rate", "15.00"),
    ]
    for i, (desc, qty, price, taxable) in enumerate(lines):
        fields += [
            ("description", desc), ("quantity", str(qty)), ("unit_price", str(price)),
            ("income_account_id", str(income_account_id)), ("item_id", ""),
        ]
        if taxable:
            fields.append(("taxable", str(i)))
    return fields


def vendor_payment_fields(vendor_id, source_account_id, payment_date, amount, applications, memo=""):
    fields = [
        ("vendor_id", str(vendor_id)), ("payment_date", payment_date.isoformat()),
        ("amount", str(amount)), ("currency", "MUR"), ("exchange_rate", "1.0"),
        ("method", "bank_transfer"), ("source_account_id", str(source_account_id)),
        ("reference_no", ""), ("memo", memo),
    ]
    for bill_id, applied in applications:
        fields += [("apply_bill_id", str(bill_id)), ("apply_amount", str(applied))]
    return fields


def main():
    app = create_app()
    today = date.today()

    with app.app_context():
        username = unique_username()

    client = app.test_client()

    business_name = "Atlas Trading Co Ltd"
    password = "demo12345"
    post(client, "/register", {
        "business_name": business_name, "name": "Demo Owner",
        "username": username, "password": password, "confirm_password": password,
    })

    with app.app_context():
        from app.models import Account, CompanySettings

        company = (
            CompanySettings.query.filter_by(business_name=business_name)
            .order_by(CompanySettings.id.desc()).first()
        )
        if not company:
            print("Registration did not create a company — aborting. Check the app is reachable "
                  "and DATABASE_URL (if set) points at a real database.")
            sys.exit(1)
        company_id = company.id
        acc = {a.code: a.id for a in Account.query.filter_by(company_id=company_id).all()}

    print(f"Company '{business_name}' created — login: {username} / {password}")

    # ---- Customers ----------------------------------------------------
    for name in ["Blue Horizon Retail", "Meridian Exports Ltd", "Coral Bay Hotels"]:
        post(client, "/sales/customers/new", {
            "name": name, "email": "", "phone": "", "address": "", "vat_number": "", "opening_balance": "",
        })

    with app.app_context():
        from app.models import Customer

        cust = {c.name: c.id for c in Customer.query.filter_by(company_id=company_id).all()}

    # ---- Vendors --------------------------------------------------------
    for name in ["Nova Print & Supplies", "Southern Freight Services"]:
        post(client, "/purchases/vendors/new", {
            "name": name, "email": "", "phone": "", "address": "", "vat_number": "", "opening_balance": "",
        })

    with app.app_context():
        from app.models import Vendor

        vend = {v.name: v.id for v in Vendor.query.filter_by(company_id=company_id).all()}

    def last_invoice():
        from app.models import Invoice

        with app.app_context():
            inv = Invoice.query.filter_by(company_id=company_id).order_by(Invoice.id.desc()).first()
            return inv.id, inv.invoice_no, float(inv.total)

    def last_bill():
        from app.models import Bill

        with app.app_context():
            b = Bill.query.filter_by(company_id=company_id).order_by(Bill.id.desc()).first()
            return b.id, b.bill_no, float(b.total)

    income_4000 = acc["4000"]

    # ---- Invoice 1: Blue Horizon Retail — MUR, paid in full ----------
    post(client, "/sales/invoices/new", invoice_fields(
        cust["Blue Horizon Retail"], income_4000, today - timedelta(days=20), today - timedelta(days=5),
        [("Retail POS software license - Q1", 1, 45000, True),
         ("On-site setup & staff training", 1, 12000, True)],
        memo="Annual POS package",
    ))
    inv1_id, inv1_no, inv1_total = last_invoice()

    # ---- Invoice 2: Coral Bay Hotels — MUR, will be part-paid --------
    post(client, "/sales/invoices/new", invoice_fields(
        cust["Coral Bay Hotels"], income_4000, today - timedelta(days=15), today + timedelta(days=15),
        [("Property management software - annual subscription", 1, 180000, True)],
        memo="Annual PMS subscription",
    ))
    inv2_id, inv2_no, inv2_total = last_invoice()

    # ---- Invoice 3: Meridian Exports Ltd — USD, left open (multi-currency demo) ----
    post(client, "/sales/invoices/new", invoice_fields(
        cust["Meridian Exports Ltd"], income_4000, today - timedelta(days=3), today + timedelta(days=27),
        [("Export documentation & customs consulting - 10 hrs", 10, 85, False)],
        currency="USD", exchange_rate="45.50",
        memo="Zero-rated export service",
    ))
    inv3_id, inv3_no, inv3_total = last_invoice()

    # ---- Invoice 4: Blue Horizon Retail — MUR, overdue and still open ----
    post(client, "/sales/invoices/new", invoice_fields(
        cust["Blue Horizon Retail"], income_4000, today - timedelta(days=60), today - timedelta(days=30),
        [("Quarterly maintenance & support", 1, 25000, True)],
        memo="Q1 maintenance",
    ))
    inv4_id, inv4_no, inv4_total = last_invoice()

    # ---- Payment: Blue Horizon Retail pays Invoice 1 in full ----------
    post(client, "/sales/payments/new", customer_payment_fields(
        cust["Blue Horizon Retail"], acc["1010"], today - timedelta(days=2), inv1_total,
        [(inv1_id, inv1_total)], memo=f"Full settlement of {inv1_no}",
    ))

    # ---- Payment: Coral Bay Hotels pays part of Invoice 2 -------------
    partial_amount = 100000.00
    post(client, "/sales/payments/new", customer_payment_fields(
        cust["Coral Bay Hotels"], acc["1010"], today - timedelta(days=1), partial_amount,
        [(inv2_id, partial_amount)], memo=f"Partial payment on {inv2_no}",
    ))

    # ---- Bill 1: Nova Print & Supplies — MUR, will be paid in full ----
    post(client, "/purchases/bills/new", bill_fields(
        vend["Nova Print & Supplies"], acc["6300"], today - timedelta(days=10), today + timedelta(days=20),
        [("Marketing brochures & storefront signage", 1, 18000, True)],
        memo="Q1 marketing collateral",
    ))
    bill1_id, bill1_no, bill1_total = last_bill()

    # ---- Bill 2: Southern Freight Services — MUR, left open -----------
    post(client, "/purchases/bills/new", bill_fields(
        vend["Southern Freight Services"], acc["6900"], today - timedelta(days=5), today + timedelta(days=25),
        [("Freight & customs clearance - March shipment", 1, 32000, True)],
        memo="March inbound shipment",
    ))
    bill2_id, bill2_no, bill2_total = last_bill()

    # ---- Vendor payment: settle Bill 1 in full -------------------------
    post(client, "/purchases/payments/new", vendor_payment_fields(
        vend["Nova Print & Supplies"], acc["1010"], today - timedelta(days=1), bill1_total,
        [(bill1_id, bill1_total)], memo=f"Full settlement of {bill1_no}",
    ))

    # ---- Recurring invoice: an active monthly retainer template, plus one ----
    # invoice already generated from it, so the demo shows both the template
    # and its output without waiting for the scheduler.
    post(client, "/sales/recurring/new", recurring_fields(
        cust["Coral Bay Hotels"], income_4000, "Monthly Software Retainer - Coral Bay Hotels",
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
    print(f"  Recurring : '{template_name}' — active template, one invoice already generated from it")

    # ---- Bank reconciliation: reconcile every Bank Account movement posted ----
    # above against a matching statement balance, and complete the session —
    # shows a fully worked example, not just an empty "start a reconciliation" screen.
    with app.app_context():
        from app.models import JournalLine

        bank_lines = JournalLine.query.filter_by(account_id=acc["1010"], is_cleared=False).all()
        bank_line_ids = [line.id for line in bank_lines]
        bank_balance = sum(float(line.debit) - float(line.credit) for line in bank_lines)

    if bank_line_ids:
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
        print(f"  Bank recon: Bank Account reconciled and completed as of {today.isoformat()} "
              f"(statement balance MUR {bank_balance:,.2f})")

    print("\nDemo data created:")
    print(f"  Customers : Blue Horizon Retail, Meridian Exports Ltd, Coral Bay Hotels")
    print(f"  Vendors   : Nova Print & Supplies, Southern Freight Services")
    print(f"  Invoices  : {inv1_no} (paid, MUR {inv1_total:,.2f}), {inv2_no} (partial, MUR {inv2_total:,.2f}), "
          f"{inv3_no} (open, USD {inv3_total:,.2f}), {inv4_no} (overdue/open, MUR {inv4_total:,.2f})")
    print(f"  Bills     : {bill1_no} (paid, MUR {bill1_total:,.2f}), {bill2_no} (open, MUR {bill2_total:,.2f})")
    print(f"\nLog in at /login with:  {username} / {password}")


if __name__ == "__main__":
    main()
