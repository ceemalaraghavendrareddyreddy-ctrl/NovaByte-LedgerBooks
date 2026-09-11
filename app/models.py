import calendar
from datetime import date, datetime

from flask_login import UserMixin

from app import db

# Which side increases balance for each account type — this drives every
# balance calculation in the ledger (trial balance, account detail, reports).
NORMAL_BALANCE = {
    "Asset": "debit",
    "Expense": "debit",
    "Liability": "credit",
    "Equity": "credit",
    "Income": "credit",
}

ACCOUNT_TYPES = ["Asset", "Liability", "Equity", "Income", "Expense"]

# Manual exchange rates only — no live rate feed. A document's exchange_rate is always
# "1 unit of the document's own currency, expressed in the company's base currency"
# (e.g. currency=USD, exchange_rate=45.20 means 1 USD = 45.20 MUR), so base_amount =
# document_amount * exchange_rate. A document in the company's own base_currency simply
# keeps exchange_rate=1.000000 and every *_base property collapses to the plain amount —
# existing single-currency companies see no change in behaviour at all.
CURRENCIES = ["MUR", "USD", "EUR", "GBP", "ZAR", "INR"]


class User(UserMixin, db.Model):
    __tablename__ = "users"
    __table_args__ = (db.UniqueConstraint("company_id", "username", name="uq_user_company_username"),)

    id = db.Column(db.Integer, primary_key=True)
    # "Home" company — where this user lands right after login. Further companies
    # (an accountant serving several clients) are granted via UserCompany.
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    username = db.Column(db.String(50), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="owner")  # owner / accountant
    is_active_user = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    home_company = db.relationship("CompanySettings", foreign_keys=[company_id])

    def accessible_companies(self):
        """Every company this user can access, via UserCompany — home company first."""
        links = (
            UserCompany.query.filter_by(user_id=self.id)
            .join(CompanySettings, UserCompany.company_id == CompanySettings.id)
            .order_by(CompanySettings.business_name)
            .all()
        )
        companies = [link.company for link in links]
        companies.sort(key=lambda c: (c.id != self.company_id, c.business_name))
        return companies

    def can_access_company(self, company_id):
        return UserCompany.query.filter_by(user_id=self.id, company_id=company_id).first() is not None


class UserCompany(db.Model):
    """Grants a user login access to a company beyond their home company — lets one
    accountant login serve several client businesses, mirroring MRA_TaxInvoice_System.
    """

    __tablename__ = "user_companies"
    __table_args__ = (db.UniqueConstraint("user_id", "company_id", name="uq_user_company"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id])
    company = db.relationship("CompanySettings", foreign_keys=[company_id])


class CompanySettings(db.Model):
    """One row per tenant company. Despite the name (kept for continuity with the
    original single-company build), this is the Company table: every other business
    record hangs off a company_id pointing back here.
    """

    __tablename__ = "company_settings"

    id = db.Column(db.Integer, primary_key=True)
    business_name = db.Column(db.String(150), nullable=False, default="My Business")
    address = db.Column(db.String(255))
    phone = db.Column(db.String(30))
    email = db.Column(db.String(120))
    vat_number = db.Column(db.String(30))
    invoice_footer_note = db.Column(db.String(255), default="Thank you for your business.")
    # The currency every ledger entry is posted in, regardless of what currency an
    # invoice/bill/payment is issued or received in — see Invoice.currency for how a
    # foreign-currency document still lands in the GL correctly.
    base_currency = db.Column(db.String(3), nullable=False, default="MUR")
    # MRA_TaxInvoice_System connection — checked before the MRA_API_URL/MRA_API_KEY
    # environment variables in app/mra_bridge.py, so this can be set from Settings
    # without needing file access. Per-company, so each tenant can point at its own
    # MRA_TaxInvoice_System company.
    mra_api_url = db.Column(db.String(255))
    mra_api_key = db.Column(db.String(64))
    # Payroll bridge — the reverse direction from the MRA connection above: an
    # external payroll system (e.g. Sicorax/Payroll.py) is the CALLER here, and
    # this key is what it presents (X-Api-Key) to POST /api/v1/payroll/import.
    # Generated by LedgerBooks itself (Settings → Regenerate), never typed in.
    payroll_api_key = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @staticmethod
    def get_by_id(company_id):
        return CompanySettings.query.get(company_id)

    @staticmethod
    def get_by_payroll_api_key(key):
        if not key:
            return None
        return CompanySettings.query.filter_by(payroll_api_key=key).first()


class Account(db.Model):
    """One row in the Chart of Accounts."""

    __tablename__ = "accounts"
    __table_args__ = (db.UniqueConstraint("company_id", "code", name="uq_account_company_code"),)

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    code = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    account_type = db.Column(db.String(20), nullable=False)  # Asset/Liability/Equity/Income/Expense
    subtype = db.Column(db.String(50))  # e.g. "Current Asset", "Cost of Goods Sold"
    parent_id = db.Column(db.Integer, db.ForeignKey("accounts.id"))
    description = db.Column(db.String(255))
    opening_balance = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    is_system = db.Column(db.Boolean, nullable=False, default=False)  # protects seeded accounts from deletion
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    parent = db.relationship("Account", remote_side=[id], backref="children")
    journal_lines = db.relationship("JournalLine", back_populates="account")

    @property
    def normal_balance(self):
        return NORMAL_BALANCE[self.account_type]

    def balance(self, as_of=None):
        """Signed balance in the account's normal-balance direction."""
        query = JournalLine.query.filter_by(account_id=self.id).join(JournalEntry)
        if as_of:
            query = query.filter(JournalEntry.entry_date <= as_of)
        total_debit = sum((line.debit for line in query), start=0)
        total_credit = sum((line.credit for line in query), start=0)
        movement = total_debit - total_credit
        if self.normal_balance == "credit":
            movement = -movement
        return self.opening_balance + movement

    def __repr__(self):
        return f"<Account {self.code} {self.name}>"


class JournalEntry(db.Model):
    """Header for a balanced double-entry transaction."""

    __tablename__ = "journal_entries"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    entry_date = db.Column(db.Date, nullable=False, default=date.today)
    reference_no = db.Column(db.String(50))
    memo = db.Column(db.String(255))
    source_type = db.Column(db.String(30), nullable=False, default="manual")  # manual/invoice/bill/...
    source_id = db.Column(db.Integer)  # id of the source record once other modules exist
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    lines = db.relationship(
        "JournalLine", back_populates="entry", cascade="all, delete-orphan", order_by="JournalLine.id"
    )

    @property
    def total_debit(self):
        return sum((line.debit for line in self.lines), start=0)

    @property
    def total_credit(self):
        return sum((line.credit for line in self.lines), start=0)

    @property
    def is_balanced(self):
        return self.total_debit == self.total_credit and self.total_debit > 0

    def __repr__(self):
        return f"<JournalEntry {self.id} {self.entry_date}>"


class JournalLine(db.Model):
    __tablename__ = "journal_lines"

    id = db.Column(db.Integer, primary_key=True)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    debit = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    credit = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    memo = db.Column(db.String(255))
    is_cleared = db.Column(db.Boolean, nullable=False, default=False)
    reconciliation_id = db.Column(db.Integer, db.ForeignKey("bank_reconciliations.id"))

    entry = db.relationship("JournalEntry", back_populates="lines")
    reconciliation = db.relationship("BankReconciliation", back_populates="cleared_lines")
    account = db.relationship("Account", back_populates="journal_lines")


# ── Sales / Accounts Receivable ─────────────────────────────────────

class Customer(db.Model):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(120))
    phone = db.Column(db.String(30))
    address = db.Column(db.String(255))
    vat_number = db.Column(db.String(30))
    opening_balance = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    invoices = db.relationship("Invoice", back_populates="customer")
    payments = db.relationship("Payment", back_populates="customer")

    @property
    def balance_due(self):
        return float(self.opening_balance) + sum(
            (inv.balance_due for inv in self.invoices if inv.status != "void"), start=0.0
        )

    def __repr__(self):
        return f"<Customer {self.name}>"


class Invoice(db.Model):
    __tablename__ = "invoices"
    __table_args__ = (db.UniqueConstraint("company_id", "invoice_no", name="uq_invoice_company_no"),)

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    invoice_no = db.Column(db.String(20), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    invoice_date = db.Column(db.Date, nullable=False, default=date.today)
    due_date = db.Column(db.Date, nullable=False)
    memo = db.Column(db.String(255))
    vat_rate = db.Column(db.Numeric(5, 2), nullable=False, default=15.00)  # Mauritius standard VAT
    currency = db.Column(db.String(3), nullable=False, default="MUR")
    exchange_rate = db.Column(db.Numeric(12, 6), nullable=False, default=1.000000)  # 1 unit of currency, in base currency
    status = db.Column(db.String(20), nullable=False, default="open")  # open/partial/paid/void
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Set by app/mra_bridge.py after a fiscalisation attempt against MRA_TaxInvoice_System.
    # mra_status: OFFLINE / FISCALIZED / FAILED / PENDING / ERROR (ERROR = couldn't even reach the API),
    # or None if fiscalisation was never attempted (no MRA API key configured in Settings).
    mra_invoice_number = db.Column(db.String(100))
    mra_irn = db.Column(db.String(100))
    mra_status = db.Column(db.String(20))
    # Set when this invoice is voided after having been fiscalised — records the MRA
    # credit note number issued to legally cancel it out (a fiscalised invoice is never
    # deleted or altered on the MRA side, only offset).
    mra_void_reference = db.Column(db.String(100))

    customer = db.relationship("Customer", back_populates="invoices")
    journal_entry = db.relationship("JournalEntry")
    lines = db.relationship("InvoiceLine", back_populates="invoice", cascade="all, delete-orphan")
    payment_applications = db.relationship("PaymentApplication", back_populates="invoice")
    credit_applications = db.relationship("CreditMemoApplication", back_populates="invoice")

    # All money math here is done in plain float. Invoice/payment amounts arrive from
    # HTML forms as Python floats and aren't reliably Decimal until reloaded from the
    # DB, so mixing Decimal columns with those in-memory floats would raise TypeError.
    @property
    def subtotal(self):
        return sum((float(line.amount) for line in self.lines), start=0.0)

    @property
    def vat_amount(self):
        taxable = sum((float(line.amount) for line in self.lines if line.taxable), start=0.0)
        return round(taxable * float(self.vat_rate) / 100, 2)

    @property
    def total(self):
        return round(self.subtotal + self.vat_amount, 2)

    @property
    def total_base(self):
        """What this invoice's total is worth in the company's base currency, at the
        rate booked when it was created — this, never .total, is what actually posts
        to the ledger. For a base-currency invoice (exchange_rate=1) this equals .total."""
        return round(self.total * float(self.exchange_rate), 2)

    @property
    def is_foreign(self):
        return float(self.exchange_rate) != 1.0

    @property
    def amount_paid(self):
        payments = sum((float(app.amount_applied) for app in self.payment_applications), start=0.0)
        credits = sum((float(app.amount_applied) for app in self.credit_applications), start=0.0)
        return payments + credits

    @property
    def balance_due(self):
        if self.status == "void":
            return 0.0
        return round(self.total - self.amount_paid, 2)

    @property
    def days_overdue(self):
        if self.balance_due <= 0:
            return 0
        return max((date.today() - self.due_date).days, 0)

    def __repr__(self):
        return f"<Invoice {self.invoice_no}>"


class InvoiceLine(db.Model):
    __tablename__ = "invoice_lines"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"))  # set when sold from stock; null for a manual line
    description = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Numeric(12, 3), nullable=False, default=1)
    unit_price = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable = db.Column(db.Boolean, nullable=False, default=True)
    income_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)

    invoice = db.relationship("Invoice", back_populates="lines")
    income_account = db.relationship("Account")
    item = db.relationship("Item")

    @property
    def amount(self):
        return float(self.quantity) * float(self.unit_price)


# ── Recurring Invoices ───────────────────────────────────────────────

RECURRING_FREQUENCIES = ["weekly", "monthly", "quarterly", "yearly"]


def add_months(d, months):
    """Adds calendar months to a date, clamping the day into the target month
    (e.g. Jan 31 + 1 month -> Feb 28/29, not an invalid Mar 3 rollover)."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


class RecurringInvoice(db.Model):
    """A template that generates a real Invoice on a schedule. Never posts to the
    ledger itself — every invoice it produces goes through the same post_invoice()
    path (and the same MRA fiscalisation call) as one entered by hand."""

    __tablename__ = "recurring_invoices"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    name = db.Column(db.String(150), nullable=False)  # e.g. "Monthly retainer - Acme Ltd"
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    memo = db.Column(db.String(255))
    vat_rate = db.Column(db.Numeric(5, 2), nullable=False, default=15.00)
    frequency = db.Column(db.String(20), nullable=False, default="monthly")  # weekly/monthly/quarterly/yearly
    due_days = db.Column(db.Integer, nullable=False, default=30)  # invoice due N days after each generated date
    start_date = db.Column(db.Date, nullable=False, default=date.today)
    next_run_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date)  # nullable — runs indefinitely if unset
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    last_generated_at = db.Column(db.DateTime)
    invoices_generated = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship("Customer")
    lines = db.relationship("RecurringInvoiceLine", back_populates="template", cascade="all, delete-orphan")

    @property
    def subtotal(self):
        return sum((float(line.amount) for line in self.lines), start=0.0)

    @property
    def vat_amount(self):
        taxable = sum((float(line.amount) for line in self.lines if line.taxable), start=0.0)
        return round(taxable * float(self.vat_rate) / 100, 2)

    @property
    def total(self):
        return round(self.subtotal + self.vat_amount, 2)

    @property
    def is_due(self):
        return self.is_active and self.next_run_date <= date.today() and not self.is_ended

    @property
    def is_ended(self):
        return bool(self.end_date) and self.next_run_date > self.end_date

    def advance_next_run_date(self):
        """Moves next_run_date forward by one occurrence of this template's frequency."""
        current = self.next_run_date
        if self.frequency == "weekly":
            self.next_run_date = date.fromordinal(current.toordinal() + 7)
        elif self.frequency == "quarterly":
            self.next_run_date = add_months(current, 3)
        elif self.frequency == "yearly":
            self.next_run_date = add_months(current, 12)
        else:  # monthly (also the fallback for an unrecognised value)
            self.next_run_date = add_months(current, 1)
        if self.is_ended:
            self.is_active = False

    def __repr__(self):
        return f"<RecurringInvoice {self.name}>"


class RecurringInvoiceLine(db.Model):
    __tablename__ = "recurring_invoice_lines"

    id = db.Column(db.Integer, primary_key=True)
    recurring_invoice_id = db.Column(db.Integer, db.ForeignKey("recurring_invoices.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"))
    description = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Numeric(12, 3), nullable=False, default=1)
    unit_price = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable = db.Column(db.Boolean, nullable=False, default=True)
    income_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)

    template = db.relationship("RecurringInvoice", back_populates="lines")
    item = db.relationship("Item")
    income_account = db.relationship("Account")

    @property
    def amount(self):
        return float(self.quantity) * float(self.unit_price)


class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    payment_date = db.Column(db.Date, nullable=False, default=date.today)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    # The currency this payment was actually received in — must match every invoice it's
    # applied to (enforced in app/sales.py, since one payment can't straddle currencies).
    # exchange_rate is the rate AT THE PAYMENT DATE, which can differ from the rate the
    # invoice was originally booked at; that difference becomes realized FX gain/loss.
    currency = db.Column(db.String(3), nullable=False, default="MUR")
    exchange_rate = db.Column(db.Numeric(12, 6), nullable=False, default=1.000000)
    method = db.Column(db.String(20), nullable=False, default="cash")  # cash/bank/cheque/mobile
    deposit_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    reference_no = db.Column(db.String(50))
    memo = db.Column(db.String(255))
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship("Customer", back_populates="payments")
    deposit_account = db.relationship("Account")
    journal_entry = db.relationship("JournalEntry")
    applications = db.relationship("PaymentApplication", back_populates="payment", cascade="all, delete-orphan")
    deposit_line = db.relationship("DepositLine", back_populates="payment", uselist=False)

    @property
    def unapplied_amount(self):
        return float(self.amount) - sum((float(a.amount_applied) for a in self.applications), start=0.0)

    @property
    def is_deposited(self):
        return self.deposit_line is not None


class PaymentApplication(db.Model):
    """Links a payment to the invoice(s) it settles (supports split/partial payments)."""

    __tablename__ = "payment_applications"

    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("payments.id"), nullable=False)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    amount_applied = db.Column(db.Numeric(14, 2), nullable=False)

    payment = db.relationship("Payment", back_populates="applications")
    invoice = db.relationship("Invoice", back_populates="payment_applications")


# ── Purchases / Accounts Payable ─────────────────────────────────────

class Vendor(db.Model):
    __tablename__ = "vendors"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(120))
    phone = db.Column(db.String(30))
    address = db.Column(db.String(255))
    vat_number = db.Column(db.String(30))
    opening_balance = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    bills = db.relationship("Bill", back_populates="vendor")
    payments = db.relationship("VendorPayment", back_populates="vendor")

    @property
    def balance_due(self):
        return float(self.opening_balance) + sum(
            (bill.balance_due for bill in self.bills if bill.status != "void"), start=0.0
        )

    def __repr__(self):
        return f"<Vendor {self.name}>"


class Bill(db.Model):
    __tablename__ = "bills"
    __table_args__ = (db.UniqueConstraint("company_id", "bill_no", name="uq_bill_company_no"),)

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    bill_no = db.Column(db.String(20), nullable=False)  # our internal running number
    vendor_ref = db.Column(db.String(50))  # the vendor's own invoice/bill number
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendors.id"), nullable=False)
    purchase_order_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id"))  # set if billed from a PO
    bill_date = db.Column(db.Date, nullable=False, default=date.today)
    due_date = db.Column(db.Date, nullable=False)
    memo = db.Column(db.String(255))
    vat_rate = db.Column(db.Numeric(5, 2), nullable=False, default=15.00)  # Mauritius standard VAT
    currency = db.Column(db.String(3), nullable=False, default="MUR")
    exchange_rate = db.Column(db.Numeric(12, 6), nullable=False, default=1.000000)  # 1 unit of currency, in base currency
    status = db.Column(db.String(20), nullable=False, default="open")  # open/partial/paid/void
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    vendor = db.relationship("Vendor", back_populates="bills")
    purchase_order = db.relationship("PurchaseOrder", back_populates="bills")
    journal_entry = db.relationship("JournalEntry")
    lines = db.relationship("BillLine", back_populates="bill", cascade="all, delete-orphan")
    payment_applications = db.relationship("VendorPaymentApplication", back_populates="bill")
    credit_applications = db.relationship("VendorCreditApplication", back_populates="bill")

    # Same float-normalized money math as Invoice — see the comment there.
    @property
    def subtotal(self):
        return sum((float(line.amount) for line in self.lines), start=0.0)

    @property
    def vat_amount(self):
        taxable = sum((float(line.amount) for line in self.lines if line.taxable), start=0.0)
        return round(taxable * float(self.vat_rate) / 100, 2)

    @property
    def total(self):
        return round(self.subtotal + self.vat_amount, 2)

    @property
    def total_base(self):
        """Base-currency equivalent at the rate booked when the bill was created —
        this, never .total, is what actually posts to the ledger."""
        return round(self.total * float(self.exchange_rate), 2)

    @property
    def is_foreign(self):
        return float(self.exchange_rate) != 1.0

    @property
    def amount_paid(self):
        payments = sum((float(app.amount_applied) for app in self.payment_applications), start=0.0)
        credits = sum((float(app.amount_applied) for app in self.credit_applications), start=0.0)
        return payments + credits

    @property
    def balance_due(self):
        if self.status == "void":
            return 0.0
        return round(self.total - self.amount_paid, 2)

    @property
    def days_overdue(self):
        if self.balance_due <= 0:
            return 0
        return max((date.today() - self.due_date).days, 0)

    def __repr__(self):
        return f"<Bill {self.bill_no}>"


class BillLine(db.Model):
    __tablename__ = "bill_lines"

    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey("bills.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"))  # set when restocking an item; null for a manual line
    description = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Numeric(12, 3), nullable=False, default=1)
    unit_price = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable = db.Column(db.Boolean, nullable=False, default=True)
    expense_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)

    bill = db.relationship("Bill", back_populates="lines")
    expense_account = db.relationship("Account")
    item = db.relationship("Item")

    @property
    def amount(self):
        return float(self.quantity) * float(self.unit_price)


class VendorPayment(db.Model):
    __tablename__ = "vendor_payments"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendors.id"), nullable=False)
    payment_date = db.Column(db.Date, nullable=False, default=date.today)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    # Same convention as Payment.currency/exchange_rate — see the comment there.
    currency = db.Column(db.String(3), nullable=False, default="MUR")
    exchange_rate = db.Column(db.Numeric(12, 6), nullable=False, default=1.000000)
    method = db.Column(db.String(20), nullable=False, default="bank")  # cash/bank/cheque/mobile
    source_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)  # cash/bank paid from
    reference_no = db.Column(db.String(50))
    memo = db.Column(db.String(255))
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    vendor = db.relationship("Vendor", back_populates="payments")
    source_account = db.relationship("Account")
    journal_entry = db.relationship("JournalEntry")
    applications = db.relationship("VendorPaymentApplication", back_populates="payment", cascade="all, delete-orphan")

    @property
    def unapplied_amount(self):
        return float(self.amount) - sum((float(a.amount_applied) for a in self.applications), start=0.0)


class VendorPaymentApplication(db.Model):
    """Links a vendor payment to the bill(s) it settles (supports split/partial payments)."""

    __tablename__ = "vendor_payment_applications"

    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("vendor_payments.id"), nullable=False)
    bill_id = db.Column(db.Integer, db.ForeignKey("bills.id"), nullable=False)
    amount_applied = db.Column(db.Numeric(14, 2), nullable=False)

    payment = db.relationship("VendorPayment", back_populates="applications")
    bill = db.relationship("Bill", back_populates="payment_applications")


# ── Inventory & COGS ──────────────────────────────────────────────────

ITEM_TYPES = ["inventory", "non_inventory", "service"]


class Item(db.Model):
    """A product/service that can appear on invoice and bill lines.

    Only 'inventory' items carry stock (quantity_on_hand, moving-average cost) and
    trigger automatic COGS postings. 'non_inventory' and 'service' items are just a
    convenience shortcut for description/price/account — they behave like a plain
    manual line, no stock tracking.
    """

    __tablename__ = "items"
    __table_args__ = (db.UniqueConstraint("company_id", "sku", name="uq_item_company_sku"),)

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    sku = db.Column(db.String(30), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    item_type = db.Column(db.String(20), nullable=False, default="inventory")
    unit = db.Column(db.String(20), nullable=False, default="pcs")
    sales_price = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    cost_price = db.Column(db.Numeric(14, 4), nullable=False, default=0)  # moving average cost
    quantity_on_hand = db.Column(db.Numeric(12, 3), nullable=False, default=0)
    reorder_level = db.Column(db.Numeric(12, 3), nullable=False, default=0)
    income_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    inventory_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"))  # required if item_type == inventory
    cogs_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"))       # required if item_type == inventory
    opening_journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    income_account = db.relationship("Account", foreign_keys=[income_account_id])
    inventory_account = db.relationship("Account", foreign_keys=[inventory_account_id])
    cogs_account = db.relationship("Account", foreign_keys=[cogs_account_id])
    stock_movements = db.relationship(
        "StockMovement", back_populates="item", order_by="StockMovement.id", cascade="all, delete-orphan"
    )

    @property
    def is_tracked(self):
        return self.item_type == "inventory"

    @property
    def stock_value(self):
        return float(self.quantity_on_hand) * float(self.cost_price)

    @property
    def is_low_stock(self):
        return self.is_tracked and float(self.quantity_on_hand) <= float(self.reorder_level)

    def receive_stock(self, quantity, unit_cost):
        """Purchase/restock: raises the moving-average cost and quantity on hand."""
        quantity = float(quantity)
        unit_cost = float(unit_cost)
        old_qty = float(self.quantity_on_hand)
        old_cost = float(self.cost_price)
        new_qty = old_qty + quantity
        if new_qty > 0:
            self.cost_price = round((old_qty * old_cost + quantity * unit_cost) / new_qty, 4)
        self.quantity_on_hand = new_qty
        return unit_cost

    def issue_stock(self, quantity):
        """Sale: reduces quantity on hand at the current moving-average cost. Raises if oversold."""
        quantity = float(quantity)
        available = float(self.quantity_on_hand)
        if quantity > available:
            raise ValueError(
                f"Not enough stock for {self.name} ({self.sku}): have {available}, need {quantity}."
            )
        self.quantity_on_hand = available - quantity
        return float(self.cost_price)

    def __repr__(self):
        return f"<Item {self.sku} {self.name}>"


class StockMovement(db.Model):
    """Audit trail of every quantity change for an item, with the running balance after it."""

    __tablename__ = "stock_movements"

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    movement_date = db.Column(db.Date, nullable=False, default=date.today)
    movement_type = db.Column(db.String(20), nullable=False)  # opening/purchase/sale/adjustment
    quantity = db.Column(db.Numeric(12, 3), nullable=False)  # signed: + in, - out
    unit_cost = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    reference_type = db.Column(db.String(20))  # bill/invoice/adjustment
    reference_id = db.Column(db.Integer)
    running_quantity = db.Column(db.Numeric(12, 3), nullable=False)
    running_avg_cost = db.Column(db.Numeric(14, 4), nullable=False)
    memo = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    item = db.relationship("Item", back_populates="stock_movements")


# ── Banking / Reconciliation ──────────────────────────────────────────

class BankReconciliation(db.Model):
    """One reconciliation session for a Cash/Bank account against a statement."""

    __tablename__ = "bank_reconciliations"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    statement_date = db.Column(db.Date, nullable=False)
    statement_ending_balance = db.Column(db.Numeric(14, 2), nullable=False)
    beginning_balance = db.Column(db.Numeric(14, 2), nullable=False, default=0)  # snapshotted when started
    is_completed = db.Column(db.Boolean, nullable=False, default=False)
    completed_at = db.Column(db.DateTime)
    completed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    account = db.relationship("Account")
    cleared_lines = db.relationship("JournalLine", back_populates="reconciliation")

    @property
    def cleared_total(self):
        """Signed movement of all lines cleared in this session, in the account's normal-balance direction."""
        total = 0.0
        for line in self.cleared_lines:
            movement = float(line.debit) - float(line.credit)
            if self.account.normal_balance == "credit":
                movement = -movement
            total += movement
        return total

    @property
    def cleared_balance(self):
        return float(self.beginning_balance) + self.cleared_total

    @property
    def difference(self):
        return round(float(self.statement_ending_balance) - self.cleared_balance, 2)


# ── Estimates (Quotes) ─────────────────────────────────────────────────

class Estimate(db.Model):
    """A quote for a customer. Never touches the ledger — only the Invoice it converts to does."""

    __tablename__ = "estimates"
    __table_args__ = (db.UniqueConstraint("company_id", "estimate_no", name="uq_estimate_company_no"),)

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    estimate_no = db.Column(db.String(20), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    estimate_date = db.Column(db.Date, nullable=False, default=date.today)
    expiry_date = db.Column(db.Date)
    memo = db.Column(db.String(255))
    vat_rate = db.Column(db.Numeric(5, 2), nullable=False, default=15.00)
    status = db.Column(db.String(20), nullable=False, default="draft")  # draft/sent/accepted/declined/converted
    converted_invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship("Customer")
    converted_invoice = db.relationship("Invoice")
    lines = db.relationship("EstimateLine", back_populates="estimate", cascade="all, delete-orphan")

    @property
    def subtotal(self):
        return sum((float(line.amount) for line in self.lines), start=0.0)

    @property
    def vat_amount(self):
        taxable = sum((float(line.amount) for line in self.lines if line.taxable), start=0.0)
        return round(taxable * float(self.vat_rate) / 100, 2)

    @property
    def total(self):
        return round(self.subtotal + self.vat_amount, 2)

    @property
    def is_expired(self):
        return bool(self.expiry_date) and self.status not in ("converted", "declined") and date.today() > self.expiry_date

    def __repr__(self):
        return f"<Estimate {self.estimate_no}>"


class EstimateLine(db.Model):
    __tablename__ = "estimate_lines"

    id = db.Column(db.Integer, primary_key=True)
    estimate_id = db.Column(db.Integer, db.ForeignKey("estimates.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"))
    description = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Numeric(12, 3), nullable=False, default=1)
    unit_price = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable = db.Column(db.Boolean, nullable=False, default=True)
    income_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)

    estimate = db.relationship("Estimate", back_populates="lines")
    item = db.relationship("Item")
    income_account = db.relationship("Account")

    @property
    def amount(self):
        return float(self.quantity) * float(self.unit_price)


# ── Credit Memos (customer returns/refunds) ────────────────────────────

class CreditMemo(db.Model):
    """Reduces a customer's AR balance — for a returned sale, an overcharge, or a goodwill credit."""

    __tablename__ = "credit_memos"
    __table_args__ = (db.UniqueConstraint("company_id", "credit_no", name="uq_credit_memo_company_no"),)

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    credit_no = db.Column(db.String(20), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    credit_date = db.Column(db.Date, nullable=False, default=date.today)
    memo = db.Column(db.String(255))
    vat_rate = db.Column(db.Numeric(5, 2), nullable=False, default=15.00)
    status = db.Column(db.String(20), nullable=False, default="open")  # open/closed/void
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Which invoice this credit memo relates to, for MRA's required CRN reference — set
    # explicitly on the form. Optional: a credit memo not tied to one particular sale
    # (a goodwill credit, an opening-balance correction) can leave this blank, in which
    # case fiscalize_credit_memo_with_mra() falls back to the customer's most recent
    # MRA-fiscalised invoice as a best-effort reference.
    related_invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))

    mra_invoice_number = db.Column(db.String(100))
    mra_irn = db.Column(db.String(100))
    mra_status = db.Column(db.String(20))
    # Set when this credit memo is voided after having been fiscalised — records the MRA
    # debit note number issued to legally cancel it out.
    mra_void_reference = db.Column(db.String(100))

    customer = db.relationship("Customer")
    related_invoice = db.relationship("Invoice", foreign_keys=[related_invoice_id])
    journal_entry = db.relationship("JournalEntry")
    lines = db.relationship("CreditMemoLine", back_populates="credit_memo", cascade="all, delete-orphan")
    applications = db.relationship("CreditMemoApplication", back_populates="credit_memo", cascade="all, delete-orphan")

    @property
    def subtotal(self):
        return sum((float(line.amount) for line in self.lines), start=0.0)

    @property
    def vat_amount(self):
        taxable = sum((float(line.amount) for line in self.lines if line.taxable), start=0.0)
        return round(taxable * float(self.vat_rate) / 100, 2)

    @property
    def total(self):
        return round(self.subtotal + self.vat_amount, 2)

    @property
    def amount_applied(self):
        return sum((float(a.amount_applied) for a in self.applications), start=0.0)

    @property
    def remaining_credit(self):
        if self.status == "void":
            return 0.0
        return round(self.total - self.amount_applied, 2)

    def __repr__(self):
        return f"<CreditMemo {self.credit_no}>"


class CreditMemoLine(db.Model):
    __tablename__ = "credit_memo_lines"

    id = db.Column(db.Integer, primary_key=True)
    credit_memo_id = db.Column(db.Integer, db.ForeignKey("credit_memos.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"))
    description = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Numeric(12, 3), nullable=False, default=1)
    unit_price = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable = db.Column(db.Boolean, nullable=False, default=True)
    income_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    return_to_stock = db.Column(db.Boolean, nullable=False, default=True)  # only meaningful if item_id is tracked

    credit_memo = db.relationship("CreditMemo", back_populates="lines")
    item = db.relationship("Item")
    income_account = db.relationship("Account")

    @property
    def amount(self):
        return float(self.quantity) * float(self.unit_price)


class CreditMemoApplication(db.Model):
    """Links a credit memo to the invoice(s) it reduces."""

    __tablename__ = "credit_memo_applications"

    id = db.Column(db.Integer, primary_key=True)
    credit_memo_id = db.Column(db.Integer, db.ForeignKey("credit_memos.id"), nullable=False)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    amount_applied = db.Column(db.Numeric(14, 2), nullable=False)

    credit_memo = db.relationship("CreditMemo", back_populates="applications")
    invoice = db.relationship("Invoice", back_populates="credit_applications")


# ── Vendor Credits (returns to a vendor) ────────────────────────────────

class VendorCredit(db.Model):
    """Reduces what we owe a vendor — for returned stock or a billing correction."""

    __tablename__ = "vendor_credits"
    __table_args__ = (db.UniqueConstraint("company_id", "credit_no", name="uq_vendor_credit_company_no"),)

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    credit_no = db.Column(db.String(20), nullable=False)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendors.id"), nullable=False)
    credit_date = db.Column(db.Date, nullable=False, default=date.today)
    memo = db.Column(db.String(255))
    vat_rate = db.Column(db.Numeric(5, 2), nullable=False, default=15.00)
    status = db.Column(db.String(20), nullable=False, default="open")  # open/closed/void
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    vendor = db.relationship("Vendor")
    journal_entry = db.relationship("JournalEntry")
    lines = db.relationship("VendorCreditLine", back_populates="vendor_credit", cascade="all, delete-orphan")
    applications = db.relationship(
        "VendorCreditApplication", back_populates="vendor_credit", cascade="all, delete-orphan"
    )

    @property
    def subtotal(self):
        return sum((float(line.amount) for line in self.lines), start=0.0)

    @property
    def vat_amount(self):
        taxable = sum((float(line.amount) for line in self.lines if line.taxable), start=0.0)
        return round(taxable * float(self.vat_rate) / 100, 2)

    @property
    def total(self):
        return round(self.subtotal + self.vat_amount, 2)

    @property
    def amount_applied(self):
        return sum((float(a.amount_applied) for a in self.applications), start=0.0)

    @property
    def remaining_credit(self):
        if self.status == "void":
            return 0.0
        return round(self.total - self.amount_applied, 2)

    def __repr__(self):
        return f"<VendorCredit {self.credit_no}>"


class VendorCreditLine(db.Model):
    __tablename__ = "vendor_credit_lines"

    id = db.Column(db.Integer, primary_key=True)
    vendor_credit_id = db.Column(db.Integer, db.ForeignKey("vendor_credits.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"))
    description = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Numeric(12, 3), nullable=False, default=1)
    unit_price = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable = db.Column(db.Boolean, nullable=False, default=True)
    expense_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    return_to_vendor = db.Column(db.Boolean, nullable=False, default=True)  # only meaningful if item_id is tracked

    vendor_credit = db.relationship("VendorCredit", back_populates="lines")
    item = db.relationship("Item")
    expense_account = db.relationship("Account")

    @property
    def amount(self):
        return float(self.quantity) * float(self.unit_price)


class VendorCreditApplication(db.Model):
    """Links a vendor credit to the bill(s) it reduces."""

    __tablename__ = "vendor_credit_applications"

    id = db.Column(db.Integer, primary_key=True)
    vendor_credit_id = db.Column(db.Integer, db.ForeignKey("vendor_credits.id"), nullable=False)
    bill_id = db.Column(db.Integer, db.ForeignKey("bills.id"), nullable=False)
    amount_applied = db.Column(db.Numeric(14, 2), nullable=False)

    vendor_credit = db.relationship("VendorCredit", back_populates="applications")
    bill = db.relationship("Bill", back_populates="credit_applications")


# ── Purchase Orders ──────────────────────────────────────────────────

class PurchaseOrder(db.Model):
    """What we ordered from a vendor. Never touches the ledger — only the Bill(s) billed
    against it do. Supports partial receiving: one PO can spawn several bills over time.
    """

    __tablename__ = "purchase_orders"
    __table_args__ = (db.UniqueConstraint("company_id", "po_no", name="uq_po_company_no"),)

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    po_no = db.Column(db.String(20), nullable=False)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendors.id"), nullable=False)
    po_date = db.Column(db.Date, nullable=False, default=date.today)
    expected_date = db.Column(db.Date)
    memo = db.Column(db.String(255))
    vat_rate = db.Column(db.Numeric(5, 2), nullable=False, default=15.00)
    status = db.Column(db.String(20), nullable=False, default="draft")  # draft/sent/partial/closed/cancelled
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    vendor = db.relationship("Vendor")
    lines = db.relationship("PurchaseOrderLine", back_populates="purchase_order", cascade="all, delete-orphan")
    bills = db.relationship("Bill", back_populates="purchase_order")

    @property
    def subtotal(self):
        return sum((float(line.amount) for line in self.lines), start=0.0)

    @property
    def vat_amount(self):
        taxable = sum((float(line.amount) for line in self.lines if line.taxable), start=0.0)
        return round(taxable * float(self.vat_rate) / 100, 2)

    @property
    def total(self):
        return round(self.subtotal + self.vat_amount, 2)

    @property
    def is_fully_billed(self):
        return all(float(line.quantity_billed) >= float(line.quantity) for line in self.lines)

    def __repr__(self):
        return f"<PurchaseOrder {self.po_no}>"


class PurchaseOrderLine(db.Model):
    __tablename__ = "purchase_order_lines"

    id = db.Column(db.Integer, primary_key=True)
    purchase_order_id = db.Column(db.Integer, db.ForeignKey("purchase_orders.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"))
    description = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Numeric(12, 3), nullable=False, default=1)
    unit_price = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable = db.Column(db.Boolean, nullable=False, default=True)
    expense_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    quantity_billed = db.Column(db.Numeric(12, 3), nullable=False, default=0)  # running total already billed

    purchase_order = db.relationship("PurchaseOrder", back_populates="lines")
    item = db.relationship("Item")
    expense_account = db.relationship("Account")

    @property
    def amount(self):
        return float(self.quantity) * float(self.unit_price)

    @property
    def quantity_remaining(self):
        return round(float(self.quantity) - float(self.quantity_billed), 3)


# ── Document Attachments ────────────────────────────────────────────────

class Attachment(db.Model):
    """A file attached to any document — invoice, bill, credit memo, vendor credit, PO, or
    journal entry. entity_type/entity_id is a lightweight polymorphic link rather than a
    separate join table per document type, since the attach/list/download logic is identical
    for all of them. No direct company_id: access is always checked through the parent
    document, which is itself company-scoped.
    """

    __tablename__ = "attachments"

    id = db.Column(db.Integer, primary_key=True)
    entity_type = db.Column(db.String(30), nullable=False)  # invoice/bill/credit_memo/vendor_credit/purchase_order/journal_entry
    entity_id = db.Column(db.Integer, nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(100), nullable=False)  # UUID-based name on disk, collision-proof
    content_type = db.Column(db.String(100))
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    uploaded_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    uploader = db.relationship("User")


# ── Audit Log ───────────────────────────────────────────────────────────

class AuditLog(db.Model):
    """Records who did what to which document and when — separate from JournalEntry.created_by,
    which only covers ledger postings. This also captures voids, deletes, and non-ledger actions
    (estimate status changes, user management, etc.).
    """

    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    action = db.Column(db.String(30), nullable=False)  # create/edit/void/delete/apply/status_change/etc.
    entity_type = db.Column(db.String(30), nullable=False)
    entity_id = db.Column(db.Integer)
    entity_label = db.Column(db.String(100))  # e.g. "INV-0012" — so the log reads without extra joins
    description = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")


# ── Budgets ───────────────────────────────────────────────────────────

class Budget(db.Model):
    """One account's budgeted amount for one calendar month."""

    __tablename__ = "budgets"
    __table_args__ = (db.UniqueConstraint("account_id", "period_year", "period_month", name="uq_budget_period"),)

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    period_year = db.Column(db.Integer, nullable=False)
    period_month = db.Column(db.Integer, nullable=False)  # 1-12
    budgeted_amount = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    account = db.relationship("Account")


# ── Saved (custom) Reports ───────────────────────────────────────────────

class SavedReport(db.Model):
    """A user-defined account filter/grouping, saved for one-click re-running later."""

    __tablename__ = "saved_reports"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    account_type = db.Column(db.String(20))       # optional filter: Asset/Liability/Equity/Income/Expense
    account_subtype = db.Column(db.String(50))    # optional filter
    date_mode = db.Column(db.String(10), nullable=False, default="range")  # "range" (period movement) or "asof" (balance)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    creator = db.relationship("User")


# ── Undeposited Funds / Deposits ─────────────────────────────────────

class Deposit(db.Model):
    """Batches several customer payments (sitting in Undeposited Funds) into the single lump-sum
    deposit that actually shows up on the bank statement — mirrors how the money really moves.
    """

    __tablename__ = "deposits"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    deposit_date = db.Column(db.Date, nullable=False, default=date.today)
    destination_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    memo = db.Column(db.String(255))
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    destination_account = db.relationship("Account")
    journal_entry = db.relationship("JournalEntry")
    lines = db.relationship("DepositLine", back_populates="deposit", cascade="all, delete-orphan")

    @property
    def total(self):
        return sum((float(line.amount) for line in self.lines), start=0.0)


class DepositLine(db.Model):
    __tablename__ = "deposit_lines"

    id = db.Column(db.Integer, primary_key=True)
    deposit_id = db.Column(db.Integer, db.ForeignKey("deposits.id"), nullable=False)
    payment_id = db.Column(db.Integer, db.ForeignKey("payments.id"), nullable=False, unique=True)
    amount = db.Column(db.Numeric(14, 2), nullable=False)

    deposit = db.relationship("Deposit", back_populates="lines")
    payment = db.relationship("Payment", back_populates="deposit_line")


# ── Bank Statement Import ─────────────────────────────────────────────

class BankStatementImport(db.Model):
    """One CSV upload session for one account."""

    __tablename__ = "bank_statement_imports"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    filename = db.Column(db.String(255))
    imported_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    imported_at = db.Column(db.DateTime, default=datetime.utcnow)

    account = db.relationship("Account")
    lines = db.relationship("BankImportLine", back_populates="import_batch", cascade="all, delete-orphan")


class BankImportLine(db.Model):
    """One row from the uploaded statement, with whatever auto-match was found (if any)."""

    __tablename__ = "bank_import_lines"

    id = db.Column(db.Integer, primary_key=True)
    import_id = db.Column(db.Integer, db.ForeignKey("bank_statement_imports.id"), nullable=False)
    stmt_date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(255))
    amount = db.Column(db.Numeric(14, 2), nullable=False)  # positive = money in, negative = money out
    status = db.Column(db.String(20), nullable=False, default="unmatched")  # unmatched/matched/created/ignored
    matched_journal_line_id = db.Column(db.Integer, db.ForeignKey("journal_lines.id"))
    created_journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))

    import_batch = db.relationship("BankStatementImport", back_populates="lines")
    matched_journal_line = db.relationship("JournalLine", foreign_keys=[matched_journal_line_id])
    created_journal_entry = db.relationship("JournalEntry", foreign_keys=[created_journal_entry_id])


# ── Fixed Assets ─────────────────────────────────────────────────────

class Asset(db.Model):
    """A fixed asset (equipment, vehicle, building fixture, ...) depreciated straight-line
    over its useful life. Each monthly depreciation run posts Dr Depreciation Expense /
    Cr Accumulated Depreciation for one AssetDepreciationEntry — the asset's own cost
    account is never touched again after purchase; only accumulated depreciation moves.
    """

    __tablename__ = "assets"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.String(255))
    asset_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)  # e.g. 1500 Equipment
    purchase_date = db.Column(db.Date, nullable=False, default=date.today)
    purchase_cost = db.Column(db.Numeric(14, 2), nullable=False)
    salvage_value = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    useful_life_months = db.Column(db.Integer, nullable=False)
    next_depreciation_date = db.Column(db.Date, nullable=False)
    accumulated_depreciation = db.Column(db.Numeric(14, 2), nullable=False, default=0)  # running cache, ledger is source of truth
    periods_depreciated = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default="active")  # active/fully_depreciated/disposed
    disposal_date = db.Column(db.Date)
    disposal_proceeds = db.Column(db.Numeric(14, 2))
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))  # the original purchase entry, if posted from here
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    asset_account = db.relationship("Account")
    journal_entry = db.relationship("JournalEntry")
    depreciation_entries = db.relationship(
        "AssetDepreciationEntry", back_populates="asset", cascade="all, delete-orphan",
        order_by="AssetDepreciationEntry.period_date",
    )

    @property
    def depreciable_base(self):
        return round(float(self.purchase_cost) - float(self.salvage_value), 2)

    @property
    def monthly_depreciation(self):
        if self.useful_life_months <= 0:
            return 0.0
        return round(self.depreciable_base / self.useful_life_months, 2)

    @property
    def net_book_value(self):
        return round(float(self.purchase_cost) - float(self.accumulated_depreciation), 2)

    @property
    def is_fully_depreciated(self):
        return float(self.accumulated_depreciation) >= self.depreciable_base

    @property
    def is_due(self):
        return (
            self.status == "active"
            and not self.is_fully_depreciated
            and self.next_depreciation_date <= date.today()
        )

    def next_depreciation_amount(self):
        """The amount the *next* run will post — the plain monthly figure, except the
        final period, which absorbs whatever rounding remainder is left so the asset's
        accumulated depreciation lands exactly on depreciable_base, never a cent over/under."""
        remaining = round(self.depreciable_base - float(self.accumulated_depreciation), 2)
        if remaining <= 0:
            return 0.0
        return min(self.monthly_depreciation, remaining)

    def __repr__(self):
        return f"<Asset {self.name}>"


class AssetDepreciationEntry(db.Model):
    """One posted month of depreciation for one asset — the audit trail, and the guard
    against ever posting the same period twice for the same asset."""

    __tablename__ = "asset_depreciation_entries"
    __table_args__ = (db.UniqueConstraint("asset_id", "period_date", name="uq_asset_depreciation_period"),)

    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey("assets.id"), nullable=False)
    period_date = db.Column(db.Date, nullable=False)  # the month this entry expenses
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    asset = db.relationship("Asset", back_populates="depreciation_entries")
    journal_entry = db.relationship("JournalEntry")
