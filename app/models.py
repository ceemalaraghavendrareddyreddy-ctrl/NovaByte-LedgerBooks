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
    # Comma-separated module keys this user is allowed into (see app/permissions.py's
    # MODULES) — e.g. "sales,inventory". NULL or empty means unrestricted (full access
    # to every non-owner module), so every user created before this column existed
    # keeps working exactly as before. Owners always have full access regardless of
    # this field — it only ever narrows a non-owner's access.
    permissions = db.Column(db.Text)
    is_active_user = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # TOTP-based 2FA (Settings → Security). totp_secret is only meaningful once
    # totp_enabled is True — a secret can exist mid-setup (QR shown, not yet confirmed)
    # without turning enforcement on, so login only checks totp_enabled.
    totp_secret = db.Column(db.String(32))
    totp_enabled = db.Column(db.Boolean, nullable=False, default=False)
    # Comma-separated werkzeug password hashes, one per single-use recovery code —
    # shown once at enable time for when the user loses their authenticator device.
    # Each is removed from this list the moment it's redeemed.
    totp_recovery_codes = db.Column(db.Text)

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

    def module_permissions(self):
        """None means unrestricted (owner, or no restriction ever set) — same meaning
        as before this existed. Otherwise {module_key: 'view'|'full'}. Stored as a
        comma-separated string of tokens, each either "sales" (full, the original
        format — every permissions value saved before view-only existed still parses
        exactly the same way) or "sales:view" (read-only: can open the module's pages
        but any create/edit/delete action inside it 403s)."""
        if self.role == "owner" or not self.permissions:
            return None
        result = {}
        for token in self.permissions.split(","):
            token = token.strip()
            if not token:
                continue
            if ":" in token:
                key, level = token.split(":", 1)
            else:
                key, level = token, "full"
            result[key] = level
        return result

    def allowed_modules(self):
        """None means unrestricted; otherwise the set of module keys this user may
        open at all, regardless of view-only vs full access."""
        perms = self.module_permissions()
        return None if perms is None else set(perms.keys())

    def can_use_module(self, module_key):
        allowed = self.allowed_modules()
        return allowed is None or module_key in allowed

    def module_view_only(self, module_key):
        """True only when this user was explicitly restricted to read-only for this
        module — never true for an owner or an unrestricted user."""
        perms = self.module_permissions()
        if perms is None:
            return False
        return perms.get(module_key) == "view"

    def redeem_recovery_code(self, code):
        """Checks `code` against the stored recovery-code hashes and, if it matches,
        removes that one hash so the same code can't be reused. Returns True on success.
        """
        from werkzeug.security import check_password_hash
        if not self.totp_recovery_codes:
            return False
        hashes = self.totp_recovery_codes.split(",")
        for i, h in enumerate(hashes):
            if check_password_hash(h, code.strip().replace(" ", "")):
                del hashes[i]
                self.totp_recovery_codes = ",".join(hashes)
                return True
        return False


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
    # A full data: URI ("data:image/png;base64,...."), stored directly rather than as a
    # file on disk — Render's free web service has no persistent disk, so anything
    # written to the filesystem vanishes on the next deploy/restart; this survives in
    # the database like everything else. Capped at a small size in settings.py (well
    # under MySQL's plain TEXT column limit of ~64KB) — a sidebar logo, not a photo.
    logo_data = db.Column(db.Text)
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
    # Default payment_mode sent on every fiscalisation call (app/mra_bridge.py) — one of
    # the values MRA_TaxInvoice_System's own invoice form offers: CASH / CARD /
    # BANK TRANSFER / CHEQUE. Previously hardcoded to CASH; now a per-company setting
    # since not every business collects cash at point of sale.
    mra_default_payment_mode = db.Column(db.String(20), nullable=False, default="CASH")
    # Payroll bridge — the reverse direction from the MRA connection above: an
    # external payroll system (e.g. Sicorax/Payroll.py) is the CALLER here, and
    # this key is what it presents (X-Api-Key) to POST /api/v1/payroll/import.
    # Generated by LedgerBooks itself (Settings → Regenerate), never typed in.
    payroll_api_key = db.Column(db.String(64))
    # SMTP settings for the payment reminders digest. All optional — if any of
    # host/port/from are missing, the reminders page shows preview-only mode
    # instead of trying to send. Password is stored plaintext (same threat model
    # as mra_api_key above): whoever owns the DB already owns the company anyway.
    smtp_host = db.Column(db.String(150))
    smtp_port = db.Column(db.Integer, default=587)
    smtp_username = db.Column(db.String(150))
    smtp_password = db.Column(db.String(255))
    smtp_from = db.Column(db.String(150))
    smtp_use_tls = db.Column(db.Boolean, default=True)
    # Live bank feeds (Banking → Bank Feeds) — connects a bank-aggregator service
    # so transactions can be pulled automatically instead of a manual CSV export/import.
    # Plaid is genuinely wired up (see app/plaid_client.py, app/bank_feeds.py) — Sandbox
    # works out of the box with a free developer account, no company/KYC needed; a real
    # bank connection needs Production credentials, which DO require the account owner
    # to apply directly with Plaid. Salt Edge / TrueLayer stay honest scaffolding (no
    # confirmed Mauritius bank coverage yet) — picking one of those is a business
    # decision, not something to guess at in code. bank_feed_api_key doubles as Plaid's
    # "secret" (paired with bank_feed_client_id below).
    bank_feed_provider = db.Column(db.String(30))
    bank_feed_api_key = db.Column(db.String(255))
    bank_feed_client_id = db.Column(db.String(64))
    # OCR key for receipt capture (Purchases → Expenses → Upload Receipt) — calls
    # OCR.space's API (app/ocr.py). Left blank, it falls back to OCR.space's public
    # "helloworld" demo key, which genuinely works but is rate-limited and shared
    # across everyone using it worldwide — fine for trying the feature, not for real
    # daily use. A free OCR.space account (no card needed) gives a private key with
    # much higher limits; paste it here once you have one.
    ocr_api_key = db.Column(db.String(255))
    # Automatic overdue-invoice reminder emails (Reminders already sends these
    # one click at a time — this is the same email, just on a schedule). Off by
    # default: sending anything to a customer automatically needs an explicit,
    # conscious opt-in from the account owner, not a default anyone inherits silently.
    auto_reminders_enabled = db.Column(db.Boolean, nullable=False, default=False)
    # Scheduled digest email (Net income, cash position, AR/AP, low stock, budget
    # overruns) — same opt-in reasoning as above. frequency is "daily"/"weekly"/"monthly";
    # last_sent_at is how the scheduler knows whether THIS company's cadence is due yet.
    scheduled_report_enabled = db.Column(db.Boolean, nullable=False, default=False)
    scheduled_report_frequency = db.Column(db.String(10), nullable=False, default="weekly")
    scheduled_report_recipient = db.Column(db.String(150))
    scheduled_report_last_sent_at = db.Column(db.Date)
    # Which PDF theme to render invoices with — one of "classic" (teal accent,
    # current default), "minimal" (thin lines, grayscale header), "coral"
    # (warm coral accent to match the app UI).
    invoice_template = db.Column(db.String(20), default="classic")
    # Period lock: no journal entry (manual or auto-posted from any document) may be
    # created, edited, or deleted with an entry_date on or before this date — see
    # app/period_lock.py. NULL means nothing is locked. Set once a period's figures
    # have been filed with MRA and shouldn't move again.
    locked_through_date = db.Column(db.Date)
    # Bill approval workflow (Purchases → Approvals) — only meaningful once more than
    # one person can enter bills. When enabled, a new bill above the threshold is held
    # as "pending_approval" (no journal entry, no stock movement posted yet) until an
    # owner approves or rejects it. threshold NULL or 0 means every bill needs approval.
    bill_approval_enabled = db.Column(db.Boolean, nullable=False, default=False)
    bill_approval_threshold = db.Column(db.Numeric(14, 2))
    # Mirrors bill_approval_enabled/threshold above, for the AR side — see Invoice's
    # own submitted_by/approved_by/approved_at/approval_note fields.
    invoice_approval_enabled = db.Column(db.Boolean, nullable=False, default=False)
    invoice_approval_threshold = db.Column(db.Numeric(14, 2))
    # Read-only public API key (X-Api-Key header) for GET /api/v1/* resource endpoints —
    # see app/api_v1.py. Separate from payroll_api_key above, which is a different,
    # write-only bridge for a different caller.
    api_key = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @staticmethod
    def get_by_id(company_id):
        return CompanySettings.query.get(company_id)

    @staticmethod
    def get_by_api_key(key):
        if not key:
            return None
        return CompanySettings.query.filter_by(api_key=key).first()

    @staticmethod
    def get_by_payroll_api_key(key):
        if not key:
            return None
        return CompanySettings.query.filter_by(payroll_api_key=key).first()


class Project(db.Model):
    """A tag for tracking one job/branch/cost center within a single company's
    books — one flat list, one tag per invoice or bill (not per line). The name
    is entirely up to the company: "Branch A"/"Branch B" for a multi-branch
    business, "Website Redesign"/"Office Fit-out" for a firm running projects,
    or a mix of both in one list. Leaving a document untagged just means it
    only shows up in the consolidated (all-projects) report, never a hard
    requirement.
    """

    __tablename__ = "projects"
    __table_args__ = (db.UniqueConstraint("company_id", "name", name="uq_project_company_name"),)

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Project {self.name}>"


class TimeEntry(db.Model):
    """One logged block of time — optionally billable to a customer/project. Doesn't
    touch the ledger by itself; billable, uninvoiced hours get pulled onto a real
    Invoice's lines from Sales → New Invoice (see sales.py's time-entry section),
    at which point invoice_line_id links back here so the same hours can never be
    billed twice.
    """

    __tablename__ = "time_entries"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"))
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"))
    entry_date = db.Column(db.Date, nullable=False, default=date.today)
    description = db.Column(db.String(255), nullable=False)
    hours = db.Column(db.Numeric(6, 2), nullable=False)
    hourly_rate = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    is_billable = db.Column(db.Boolean, nullable=False, default=True)
    # Set once this entry's hours have actually been pulled onto an invoice — a billable
    # entry with invoice_line_id set is done, never offered again on a future invoice.
    invoice_line_id = db.Column(db.Integer, db.ForeignKey("invoice_lines.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")
    customer = db.relationship("Customer")
    project = db.relationship("Project")
    invoice_line = db.relationship("InvoiceLine")

    @property
    def amount(self):
        return round(float(self.hours) * float(self.hourly_rate), 2)

    @property
    def is_invoiced(self):
        return self.invoice_line_id is not None

    def __repr__(self):
        return f"<TimeEntry {self.entry_date} {self.hours}h>"


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

    # Bank/cash account metadata (subtype == "Cash and Cash Equivalents") — purely
    # informational identification of the real-world account, not a multi-currency
    # ledger: every posting to this account still happens in the company's base
    # currency, exactly like every other account. currency just records what
    # denomination the physical/online account is actually held in.
    bank_name = db.Column(db.String(150))
    account_number = db.Column(db.String(50))
    currency = db.Column(db.String(3), nullable=False, default="MUR")
    # Which account this maps to at the bank-feed provider (see CompanySettings.bank_feed_provider) —
    # set once a real provider is chosen and its "link account" flow hands back an id.
    bank_feed_external_account_id = db.Column(db.String(100))
    # Plaid-specific: the permanent access_token for the Item (bank login connection)
    # this local account was linked through — obtained once via Connect With Plaid and
    # then reused on every sync. NULL until linked; never shown in any UI once set.
    bank_feed_access_token = db.Column(db.String(255))
    # Plaid's /transactions/sync cursor — lets each sync fetch only what's new since
    # last time instead of the account's whole history. NULL means "never synced yet".
    bank_feed_sync_cursor = db.Column(db.Text)

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
    # Copied from the source Invoice/Bill's own project_id when this entry is posted
    # (see sales.post_invoice / purchases.post_bill), or set directly on a manual
    # entry — lets Reports > Profit & Loss filter to one project. NULL means
    # unassigned; it still counts in the consolidated (no filter) P&L.
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"))
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
    # "individual" or "company" — a company can carry a BRN/VAT number, an individual usually can't.
    customer_type = db.Column(db.String(20), nullable=False, default="company")
    email = db.Column(db.String(120))
    phone = db.Column(db.String(30))
    phone2 = db.Column(db.String(30))
    address = db.Column(db.String(255))
    vat_number = db.Column(db.String(30))
    # Business Registration Number — same purpose as Vendor.brn, only meaningful for
    # a "company" customer. Optional: left blank for individuals.
    brn = db.Column(db.String(20))
    # Currency this customer is normally billed in — a default the invoice/estimate
    # form pre-selects, still overridable per document like any other currency choice.
    billing_currency = db.Column(db.String(3), nullable=False, default="MUR")
    opening_balance = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Customer portal login — separate from staff (User) logins entirely: no role,
    # no company access list, just "can this one customer see their own invoices and
    # balance." portal_enabled off by default; password is only ever set by an owner
    # from this customer's own page (see app/sales.py's portal-access route), never
    # self-service-registered, since the portal has no "forgot password"/email flow yet.
    portal_password_hash = db.Column(db.String(255))
    portal_enabled = db.Column(db.Boolean, nullable=False, default=False)

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
    # Printed on the invoice (PDF/email) alongside the due date — e.g. "Net 30". Purely
    # descriptive: due_date is still the actual date every aging/reminder feature reads.
    payment_terms = db.Column(db.String(30), nullable=False, default="Due on Receipt")
    # The customer's own PO/reference number — the AR-side mirror of Bill.vendor_ref
    # (the vendor's own bill number, already tracked there). Purely informational:
    # printed on the invoice/PDF and searchable, never affects posting or totals.
    customer_po_number = db.Column(db.String(50))
    memo = db.Column(db.String(255))
    # Optional branch/job/cost-center tag — see Project. Copied onto the journal
    # entry this invoice posts, so Reports > Profit & Loss can filter by it.
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"))
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
    # Set by app/fx_revaluation.py the first time this invoice's open balance is
    # revalued at a period-end rate — from then on this (not the original exchange_rate)
    # is the rate AR is actually carried at, so a later revaluation or final payment
    # measures its gain/loss from here, not from the original booking rate. NULL means
    # "never revalued" — carrying_exchange_rate falls back to exchange_rate.
    revalued_exchange_rate = db.Column(db.Numeric(12, 6))
    # Set by the automatic overdue-reminder job (see app/reminders.py) — lets it skip an
    # invoice already reminded recently instead of re-emailing the customer every single
    # day it stays overdue. NULL means never auto-reminded.
    last_auto_reminder_sent_at = db.Column(db.DateTime)
    # Invoice approval workflow — see CompanySettings.invoice_approval_enabled. Mirrors
    # Bill's identical fields; see that model's comment for the full reasoning.
    submitted_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    approval_note = db.Column(db.String(255))

    customer = db.relationship("Customer", back_populates="invoices")
    submitter = db.relationship("User", foreign_keys=[submitted_by])
    approver = db.relationship("User", foreign_keys=[approved_by])
    journal_entry = db.relationship("JournalEntry")
    project = db.relationship("Project")
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
        """Each line applies its own vat_rate if it has one set (e.g. a zero-rated
        product on an otherwise-taxed line); a line with no rate of its own falls
        back to this document's header rate — the same number every line effectively
        used before per-line rates existed, so nothing already posted changes."""
        total = 0.0
        for line in self.lines:
            if not line.taxable:
                continue
            rate = float(line.vat_rate) if line.vat_rate is not None else float(self.vat_rate)
            total += round(line.amount * rate / 100, 2)
        return round(total, 2)

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
    def carrying_exchange_rate(self):
        """The rate AR is actually carried at right now — the last revaluation rate if
        this invoice has ever been revalued, otherwise the rate it was originally booked
        at. Used instead of .exchange_rate wherever AR is relieved (payments) or
        re-measured (revaluation), so those two never disagree about the starting point."""
        return float(self.revalued_exchange_rate) if self.revalued_exchange_rate is not None else float(self.exchange_rate)

    @property
    def amount_paid(self):
        payments = sum((float(app.amount_applied) for app in self.payment_applications), start=0.0)
        credits = sum((float(app.amount_applied) for app in self.credit_applications), start=0.0)
        return payments + credits

    @property
    def balance_due(self):
        # Mirrors Bill.balance_due: a still-pending invoice was never posted to AR,
        # so it must stay out of aging reports and dashboards until approved.
        if self.status in ("void", "pending_approval"):
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
    # NULL (the default, and every line that existed before this column did) means
    # "use this invoice's own vat_rate" — unchanged behavior. Set it to make just
    # this line's rate different, so one invoice can mix a 0% product with a 15%
    # one without splitting it into two invoices.
    vat_rate = db.Column(db.Numeric(5, 2))
    income_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    # Only meaningful for a tracked-item line — which warehouse the stock was issued
    # from, and (for a serial/batch-tracked item) which lot it came from. Both NULL
    # means "unspecified", exactly how every line behaved before these existed.
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"))
    lot_number = db.Column(db.String(50))

    invoice = db.relationship("Invoice", back_populates="lines")
    income_account = db.relationship("Account")
    warehouse = db.relationship("Warehouse")
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


class RecurringBill(db.Model):
    """Vendor-side mirror of RecurringInvoice — a template that generates a real Bill
    on a schedule (rent, subscriptions, retainers you pay monthly). Never posts to the
    ledger itself — every bill it produces goes through the same post_bill() path as
    one entered by hand."""

    __tablename__ = "recurring_bills"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    name = db.Column(db.String(150), nullable=False)  # e.g. "Monthly office rent"
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendors.id"), nullable=False)
    memo = db.Column(db.String(255))
    vat_rate = db.Column(db.Numeric(5, 2), nullable=False, default=15.00)
    frequency = db.Column(db.String(20), nullable=False, default="monthly")  # weekly/monthly/quarterly/yearly
    due_days = db.Column(db.Integer, nullable=False, default=30)  # bill due N days after each generated date
    start_date = db.Column(db.Date, nullable=False, default=date.today)
    next_run_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date)  # nullable — runs indefinitely if unset
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    last_generated_at = db.Column(db.DateTime)
    bills_generated = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    vendor = db.relationship("Vendor")
    lines = db.relationship("RecurringBillLine", back_populates="template", cascade="all, delete-orphan")

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
        """Same calendar math as RecurringInvoice.advance_next_run_date."""
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
        return f"<RecurringBill {self.name}>"


class RecurringBillLine(db.Model):
    __tablename__ = "recurring_bill_lines"

    id = db.Column(db.Integer, primary_key=True)
    recurring_bill_id = db.Column(db.Integer, db.ForeignKey("recurring_bills.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"))
    description = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Numeric(12, 3), nullable=False, default=1)
    unit_price = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable = db.Column(db.Boolean, nullable=False, default=True)
    expense_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)

    template = db.relationship("RecurringBill", back_populates="lines")
    item = db.relationship("Item")
    expense_account = db.relationship("Account")

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
    # "individual" or "company" — same distinction as Customer.customer_type.
    vendor_type = db.Column(db.String(20), nullable=False, default="company")
    email = db.Column(db.String(120))
    phone = db.Column(db.String(30))
    phone2 = db.Column(db.String(30))
    address = db.Column(db.String(255))
    vat_number = db.Column(db.String(30))
    # Business Registration Number and MRA-issued supplier ID — needed only for the
    # Statement of Goods and Services (SGS) export MRA requires from larger VAT
    # filers (app/mra_sgs.py). Optional: a vendor without these still works
    # everywhere else, that one export just leaves the columns blank for them.
    brn = db.Column(db.String(20))
    mra_supplier_id = db.Column(db.String(30))
    # Currency this vendor is normally billed in — same purpose as Customer.billing_currency.
    billing_currency = db.Column(db.String(3), nullable=False, default="MUR")
    opening_balance = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Vendor portal login — same design as Customer.portal_password_hash/portal_enabled above.
    portal_password_hash = db.Column(db.String(255))
    portal_enabled = db.Column(db.Boolean, nullable=False, default=False)

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
    # Optional branch/job/cost-center tag — see Project. Copied onto the journal
    # entry this bill posts, so Reports > Profit & Loss can filter by it.
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"))
    vat_rate = db.Column(db.Numeric(5, 2), nullable=False, default=15.00)  # Mauritius standard VAT
    currency = db.Column(db.String(3), nullable=False, default="MUR")
    exchange_rate = db.Column(db.Numeric(12, 6), nullable=False, default=1.000000)  # 1 unit of currency, in base currency
    status = db.Column(db.String(20), nullable=False, default="open")  # open/partial/paid/void
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Mirrors Invoice.revalued_exchange_rate — see that field's comment.
    revalued_exchange_rate = db.Column(db.Numeric(12, 6))
    # Bill approval workflow — see CompanySettings.bill_approval_enabled. submitted_by
    # is always set once that setting is on; approved_by/approved_at/approval_note stay
    # NULL until an owner acts on a pending bill. A bill that skips the workflow entirely
    # (approval disabled, or under threshold) leaves all four NULL, same as before this existed.
    submitted_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    approval_note = db.Column(db.String(255))

    vendor = db.relationship("Vendor", back_populates="bills")
    submitter = db.relationship("User", foreign_keys=[submitted_by])
    approver = db.relationship("User", foreign_keys=[approved_by])
    purchase_order = db.relationship("PurchaseOrder", back_populates="bills")
    journal_entry = db.relationship("JournalEntry")
    project = db.relationship("Project")
    lines = db.relationship("BillLine", back_populates="bill", cascade="all, delete-orphan")
    payment_applications = db.relationship("VendorPaymentApplication", back_populates="bill")
    credit_applications = db.relationship("VendorCreditApplication", back_populates="bill")

    # Same float-normalized money math as Invoice — see the comment there.
    @property
    def subtotal(self):
        return sum((float(line.amount) for line in self.lines), start=0.0)

    @property
    def vat_amount(self):
        """Each line applies its own vat_rate if it has one set (e.g. a zero-rated
        product on an otherwise-taxed line); a line with no rate of its own falls
        back to this document's header rate — the same number every line effectively
        used before per-line rates existed, so nothing already posted changes."""
        total = 0.0
        for line in self.lines:
            if not line.taxable:
                continue
            rate = float(line.vat_rate) if line.vat_rate is not None else float(self.vat_rate)
            total += round(line.amount * rate / 100, 2)
        return round(total, 2)

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
    def carrying_exchange_rate(self):
        """Mirrors Invoice.carrying_exchange_rate — see that property's comment."""
        return float(self.revalued_exchange_rate) if self.revalued_exchange_rate is not None else float(self.exchange_rate)

    @property
    def amount_paid(self):
        payments = sum((float(app.amount_applied) for app in self.payment_applications), start=0.0)
        credits = sum((float(app.amount_applied) for app in self.credit_applications), start=0.0)
        return payments + credits

    @property
    def balance_due(self):
        # A bill still awaiting approval was never posted to AP — nothing is actually
        # owed against it yet as far as the books are concerned, so it must stay out
        # of aging reports and "bills to pay" until an owner approves it.
        if self.status in ("void", "pending_approval"):
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
    # Same NULL-means-"use the bill's own vat_rate" convention as InvoiceLine.vat_rate.
    vat_rate = db.Column(db.Numeric(5, 2))
    expense_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    # Same purpose as InvoiceLine.warehouse_id/lot_number — which warehouse this line's
    # stock was received into, and which lot/serial it belongs to.
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"))
    lot_number = db.Column(db.String(50))
    # Set when this line came from Receive Goods against a specific PurchaseOrderLine
    # (see purchases.po_receive) — lets bill_detail show a PO-vs-Bill price match/variance
    # instead of just linking the whole bill to the PO header. NULL for any bill line
    # entered the ordinary way (not against a PO), same as before this field existed.
    purchase_order_line_id = db.Column(db.Integer, db.ForeignKey("purchase_order_lines.id"))

    bill = db.relationship("Bill", back_populates="lines")
    expense_account = db.relationship("Account")
    item = db.relationship("Item")
    warehouse = db.relationship("Warehouse")
    purchase_order_line = db.relationship("PurchaseOrderLine")

    @property
    def amount(self):
        return float(self.quantity) * float(self.unit_price)

    @property
    def po_price_variance(self):
        """None if this line isn't tied to a PO line, or matches it exactly. Otherwise
        the signed difference (billed - ordered) per unit — positive means billed
        MORE than the PO quoted."""
        if not self.purchase_order_line_id:
            return None
        diff = round(float(self.unit_price) - float(self.purchase_order_line.unit_price), 2)
        return diff if diff != 0 else None


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
TRACKING_TYPES = ["none", "serial", "batch"]
COSTING_METHODS = ["weighted_average", "fifo", "lifo"]


class Warehouse(db.Model):
    """A physical or logical stock location. Optional dimension on StockMovement —
    an item's quantity_on_hand stays one company-wide total (unchanged, so moving-
    average costing and every existing posting path is untouched); warehouse is
    purely "which location did this movement happen at", queried back out as a
    breakdown (see item_detail's per-warehouse quantities) rather than a separate
    per-warehouse balance column that would need to be kept in sync.
    """

    __tablename__ = "warehouses"
    __table_args__ = (db.UniqueConstraint("company_id", "code", name="uq_warehouse_company_code"),)

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    code = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    address = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Warehouse {self.code} {self.name}>"


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
    # "none" (default — unchanged behavior), "serial" (each unit is unique, movements
    # of this item are expected in single-unit quantities with their own lot_number),
    # or "batch" (a shared lot_number for a whole received quantity, e.g. an expiry-dated
    # batch). Only meaningful for item_type == "inventory"; purely a UI/reporting hint —
    # it doesn't change how quantity_on_hand or moving-average cost are computed.
    tracking_type = db.Column(db.String(10), nullable=False, default="none")
    # "weighted_average" (default, unchanged from before this existed) blends every
    # receipt into one running cost_price, same as always. "fifo"/"lifo" instead track
    # each receipt as its own StockLayer, consumed oldest-first or newest-first on
    # issue — see receive_stock/issue_stock/add_stock_layer/consume_stock_layers below.
    # cost_price stays meaningful either way: for fifo/lifo it's kept as the weighted
    # average of whatever layers are still remaining, purely for display (Item detail's
    # "Avg Cost", reports, etc.) — the real costing for COGS is per-layer.
    costing_method = db.Column(db.String(20), nullable=False, default="weighted_average")
    sales_price = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    # NULL means "use whatever rate the invoice/bill itself is set to" — the same
    # behavior as before this column existed. Set it to give this specific product
    # its own VAT treatment (e.g. 0% for a zero-rated item) regardless of what rate
    # the rest of that document is at; still overridable per line either way.
    default_vat_rate = db.Column(db.Numeric(5, 2))
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

    def receive_stock(self, quantity, unit_cost, received_date=None):
        """Purchase/restock. Weighted-average: blends into the running cost_price, exactly
        as before fifo/lifo existed. Fifo/lifo: opens a new StockLayer instead, leaving
        cost_price as a display-only average of what's left in stock."""
        quantity = float(quantity)
        unit_cost = float(unit_cost)
        old_qty = float(self.quantity_on_hand)
        new_qty = old_qty + quantity

        if self.costing_method in ("fifo", "lifo"):
            self.add_stock_layer(quantity, unit_cost, received_date)
            self.quantity_on_hand = new_qty
            self.refresh_average_cost()
        else:
            old_cost = float(self.cost_price)
            if new_qty > 0:
                self.cost_price = round((old_qty * old_cost + quantity * unit_cost) / new_qty, 4)
            self.quantity_on_hand = new_qty
        return unit_cost

    def issue_stock(self, quantity):
        """Sale: reduces quantity on hand. Weighted-average: costs it at the current
        cost_price, unchanged. Fifo/lifo: consumes tracked layers oldest/newest-first
        and returns the blended cost of exactly what was consumed. Raises if oversold."""
        quantity = float(quantity)
        available = float(self.quantity_on_hand)
        if quantity > available:
            raise ValueError(
                f"Not enough stock for {self.name} ({self.sku}): have {available}, need {quantity}."
            )
        self.quantity_on_hand = available - quantity

        if self.costing_method in ("fifo", "lifo"):
            unit_cost = self.consume_stock_layers(quantity)
            self.refresh_average_cost()
            return unit_cost
        return float(self.cost_price)

    def add_stock_layer(self, quantity, unit_cost, received_date=None):
        """Opens one new StockLayer — a purchase lot to be consumed later in fifo/lifo
        order. No-op in effect for a weighted-average item (nothing ever reads its
        layers), but harmless to call regardless."""
        db.session.add(StockLayer(
            item=self, received_date=received_date or date.today(),
            unit_cost=unit_cost, quantity_received=quantity, quantity_remaining=quantity,
        ))

    def consume_stock_layers(self, quantity):
        """Removes `quantity` units from this item's remaining layers — oldest received
        first for fifo, newest first for lifo — and returns the blended unit cost of
        what was actually consumed. Any shortfall not covered by tracked layers (e.g.
        stock received before this item was switched to fifo/lifo) is priced at the
        item's last known average cost instead of silently left uncosted.
        """
        layers = sorted(
            (l for l in self.stock_layers if float(l.quantity_remaining) > 0),
            key=lambda l: (l.received_date, l.id), reverse=(self.costing_method == "lifo"),
        )
        remaining = quantity
        total_cost = 0.0
        for layer in layers:
            if remaining <= 0:
                break
            take = min(float(layer.quantity_remaining), remaining)
            total_cost += take * float(layer.unit_cost)
            layer.quantity_remaining = float(layer.quantity_remaining) - take
            remaining -= take
        if remaining > 0:
            total_cost += remaining * float(self.cost_price)
        return round(total_cost / quantity, 4) if quantity else 0.0

    def refresh_average_cost(self):
        """Recomputes cost_price as the weighted average of whatever's left across this
        item's remaining layers — display-only for fifo/lifo items (Item detail's
        'Avg Cost', reports, etc.); the real per-sale costing is consume_stock_layers above."""
        remaining_layers = [l for l in self.stock_layers if float(l.quantity_remaining) > 0]
        total_qty = sum(float(l.quantity_remaining) for l in remaining_layers)
        if total_qty > 0:
            self.cost_price = round(
                sum(float(l.quantity_remaining) * float(l.unit_cost) for l in remaining_layers) / total_qty, 4
            )

    def __repr__(self):
        return f"<Item {self.sku} {self.name}>"


class StockLayer(db.Model):
    """One purchase lot for a fifo/lifo-costed item, consumed oldest-first (fifo) or
    newest-first (lifo) as stock is issued — see Item.add_stock_layer/consume_stock_layers.
    Irrelevant for a weighted-average item, which keeps using Item.cost_price/
    quantity_on_hand exactly as it always has; rows only ever get created for an item
    whose costing_method is fifo or lifo.
    """

    __tablename__ = "stock_layers"

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False)
    received_date = db.Column(db.Date, nullable=False, default=date.today)
    unit_cost = db.Column(db.Numeric(14, 4), nullable=False)
    quantity_received = db.Column(db.Numeric(12, 3), nullable=False)
    quantity_remaining = db.Column(db.Numeric(12, 3), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    item = db.relationship("Item", backref=db.backref("stock_layers", order_by="StockLayer.id"))

    def __repr__(self):
        return f"<StockLayer item={self.item_id} {self.quantity_remaining}/{self.quantity_received} @ {self.unit_cost}>"


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
    # Both optional, both purely a "where/which" tag on top of the existing quantity
    # math above — see Warehouse and Item.tracking_type. NULL (every movement before
    # these columns existed, and every invoice/bill-line movement today, since that
    # UI doesn't ask for a warehouse or lot yet) reads as "unspecified", not an error.
    warehouse_id = db.Column(db.Integer, db.ForeignKey("warehouses.id"))
    lot_number = db.Column(db.String(50))

    item = db.relationship("Item", back_populates="stock_movements")
    warehouse = db.relationship("Warehouse")


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
    # E-signature capture from the customer portal (see app/portal.py's estimate_sign) —
    # a drawn signature (PNG data URI) plus the typed name and IP address at the moment
    # of signing, as the record of consent. NULL means never signed (e.g. accepted the
    # old way, by a staff member clicking Accept on the customer's behalf).
    signature_data = db.Column(db.Text)
    signed_by_name = db.Column(db.String(150))
    signed_at = db.Column(db.DateTime)
    signed_ip = db.Column(db.String(45))

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


# ── Sales Orders (confirmed orders, not yet invoiced) ──────────────────

class SalesOrder(db.Model):
    """A confirmed customer order that is committed but not yet invoiced — the
    stage between an accepted Estimate and an Invoice, for businesses that fulfil
    before they bill (wholesale/distribution). Like an Estimate, it never touches
    the ledger; only the Invoice it converts to does."""

    __tablename__ = "sales_orders"
    __table_args__ = (db.UniqueConstraint("company_id", "order_no", name="uq_sales_order_company_no"),)

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    order_no = db.Column(db.String(20), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    order_date = db.Column(db.Date, nullable=False, default=date.today)
    expected_date = db.Column(db.Date)  # expected fulfilment/delivery date
    customer_po_ref = db.Column(db.String(50))  # the customer's own PO number, if any
    memo = db.Column(db.String(255))
    vat_rate = db.Column(db.Numeric(5, 2), nullable=False, default=15.00)
    status = db.Column(db.String(20), nullable=False, default="open")  # open/invoiced/cancelled
    # If this order came from converting an accepted estimate, remember which one.
    source_estimate_id = db.Column(db.Integer, db.ForeignKey("estimates.id"))
    converted_invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship("Customer")
    source_estimate = db.relationship("Estimate", foreign_keys=[source_estimate_id])
    converted_invoice = db.relationship("Invoice", foreign_keys=[converted_invoice_id])
    lines = db.relationship("SalesOrderLine", back_populates="sales_order", cascade="all, delete-orphan")

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

    def __repr__(self):
        return f"<SalesOrder {self.order_no}>"


class SalesOrderLine(db.Model):
    __tablename__ = "sales_order_lines"

    id = db.Column(db.Integer, primary_key=True)
    sales_order_id = db.Column(db.Integer, db.ForeignKey("sales_orders.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"))
    description = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Numeric(12, 3), nullable=False, default=1)
    unit_price = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    taxable = db.Column(db.Boolean, nullable=False, default=True)
    income_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)

    sales_order = db.relationship("SalesOrder", back_populates="lines")
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


# ── Expenses (quick, already-paid spend — receipts) ─────────────────────

class Expense(db.Model):
    """A single already-paid expense — the "I paid for this with cash/card, here's the
    receipt" case, as opposed to a Bill (money owed to a vendor, paid later). Posts
    immediately: Dr expense/asset account, Cr the cash/bank account it came out of.
    Optionally backed by a scanned receipt (app/ocr.py) whose extracted date/amount/
    vendor pre-fill the form — always editable before saving, never trusted blindly.
    """

    __tablename__ = "expenses"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendors.id"))  # optional — a receipt may not match a known vendor
    vendor_name_raw = db.Column(db.String(150))  # what the receipt/OCR actually said, kept even if vendor_id is set
    expense_date = db.Column(db.Date, nullable=False, default=date.today)
    description = db.Column(db.String(255), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    vat_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    category_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    payment_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)  # cash/bank paid from
    # Raw OCR text, kept for audit ("why did it guess this amount") — never shown as fact,
    # only as a "here's what the receipt said" reference alongside the editable fields above.
    ocr_raw_text = db.Column(db.Text)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    vendor = db.relationship("Vendor")
    category_account = db.relationship("Account", foreign_keys=[category_account_id])
    payment_account = db.relationship("Account", foreign_keys=[payment_account_id])
    journal_entry = db.relationship("JournalEntry")

    @property
    def vat_amount(self):
        return round(float(self.amount) * float(self.vat_rate) / (100 + float(self.vat_rate)), 2) if self.vat_rate else 0.0

    def __repr__(self):
        return f"<Expense {self.expense_date} {self.amount}>"


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


class Webhook(db.Model):
    """A URL to notify when something happens in this company — see app/webhooks.py
    for the actual firing logic and the list of event types. secret is used to sign
    each delivery (HMAC-SHA256 in the X-LedgerBooks-Signature header) so the receiving
    endpoint can verify a payload genuinely came from here.
    """

    __tablename__ = "webhooks"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    secret = db.Column(db.String(64), nullable=False)
    event_types = db.Column(db.String(255), nullable=False)  # comma-separated, e.g. "invoice.created,bill.created"
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_triggered_at = db.Column(db.DateTime)
    last_status = db.Column(db.String(255))  # e.g. "200 OK" or an error message from the last delivery attempt

    def subscribes_to(self, event_type):
        return self.is_active and event_type in {e.strip() for e in self.event_types.split(",") if e.strip()}


class BankRule(db.Model):
    """A user-defined "if the statement description contains this text, categorize it
    to this account" rule — the persistent, editable alternative to app/ai_suggest.py's
    keyword/history mock. Checked first in suggest_category_account, so a rule always
    wins over the built-in guesses once one exists for a description.
    """

    __tablename__ = "bank_rules"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company_settings.id"), nullable=False)
    keyword = db.Column(db.String(150), nullable=False)  # matched case-insensitively, substring
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    account = db.relationship("Account")

    def matches(self, description):
        return self.is_active and self.keyword.lower() in (description or "").lower()


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

    # "straight_line" (default) or "reducing_balance". A column added by the app's
    # auto-migrate startup check (ALTER TABLE, no backfill — see app/__init__.py) lands
    # NULL on every pre-existing asset, so every read of this column falls back to
    # straight_line rather than branching on the literal string — None behaves exactly
    # like the old, only-ever-straight-line behavior.
    depreciation_method = db.Column(db.String(20), nullable=False, default="straight_line")
    # "monthly" (default) or "yearly" — same NULL-means-old-default reasoning as above.
    depreciation_frequency = db.Column(db.String(10), nullable=False, default="monthly")
    # Annual % rate, only used/required for reducing_balance (applied to the *current*
    # net book value each period, prorated for the period length) — straight_line derives
    # its own effective rate from purchase_cost/salvage_value/useful_life_months instead.
    depreciation_rate = db.Column(db.Numeric(6, 3))
    # Purely informational record of when depreciation was set to begin — the actual
    # running cursor is next_depreciation_date, exactly as before this field existed,
    # so a pre-existing asset with this NULL keeps depreciating on schedule unaffected.
    depreciation_start_date = db.Column(db.Date)
    # Optional hard stop: once next_depreciation_date passes this, the asset stops being
    # due even if a balance remains — e.g. a lease-term or contract end date. NULL (the
    # default, and every pre-existing asset) means "run until fully depreciated", unchanged.
    depreciation_end_date = db.Column(db.Date)
    # Per-asset override of which Expense / contra-Asset account depreciation posts to —
    # lets different asset categories (vehicles, IT equipment, buildings...) land in their
    # own accounts instead of everything sharing 6400/1590. NULL falls back to those two
    # codes, exactly as every asset behaved before these columns existed.
    depreciation_expense_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"))
    accumulated_depreciation_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"))

    asset_account = db.relationship("Account", foreign_keys=[asset_account_id])
    depreciation_expense_account = db.relationship("Account", foreign_keys=[depreciation_expense_account_id])
    accumulated_depreciation_account = db.relationship("Account", foreign_keys=[accumulated_depreciation_account_id])
    journal_entry = db.relationship("JournalEntry")
    depreciation_entries = db.relationship(
        "AssetDepreciationEntry", back_populates="asset", cascade="all, delete-orphan",
        order_by="AssetDepreciationEntry.period_date",
    )

    @property
    def period_months(self):
        """How many calendar months one depreciation period spans: 1 for monthly, 12 for yearly."""
        return 12 if self.depreciation_frequency == "yearly" else 1

    @property
    def depreciable_base(self):
        return round(float(self.purchase_cost) - float(self.salvage_value), 2)

    @property
    def monthly_depreciation(self):
        """Straight-line figure for a single MONTH, regardless of this asset's own posting
        frequency — used as the base rate other calculations scale from, and kept under this
        name since existing templates/scripts already read it."""
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
        if self.status != "active" or self.is_fully_depreciated:
            return False
        if self.depreciation_end_date and self.next_depreciation_date > self.depreciation_end_date:
            return False
        return self.next_depreciation_date <= date.today()

    @property
    def total_periods(self):
        """How many periods (at this asset's own frequency) its useful life spans."""
        return max(round(self.useful_life_months / self.period_months), 1)

    def next_depreciation_amount(self):
        """The amount the *next* period will post — capped so accumulated depreciation
        never exceeds depreciable_base (the final period absorbs any rounding remainder).

        straight_line: a fixed per-period amount (monthly_depreciation, scaled up for a
        yearly-frequency asset) — unchanged in shape from before these fields existed.
        reducing_balance: depreciation_rate% of the CURRENT net book value, prorated for
        the period length (a year's rate applied once a year, or 1/12 of it applied monthly).
        declining_150 / declining_200: same net-book-value-based calculation as
        reducing_balance, but the rate is DERIVED (150%/200% of the straight-line rate)
        rather than typed in — the standard "150%/200% declining balance" methods. Pure
        declining balance never actually reaches zero on its own, so every period it's
        compared against what straight-line would take over the periods left, and
        whichever is bigger wins — the same switchover real depreciation schedules (and
        Zoho Books' own declining-balance methods) use to make sure the asset is fully
        written down by the end of its useful life instead of trailing off forever.
        """
        remaining = round(self.depreciable_base - float(self.accumulated_depreciation), 2)
        if remaining <= 0:
            return 0.0

        if self.depreciation_method == "reducing_balance":
            annual_rate = float(self.depreciation_rate or 0) / 100
            period_rate = annual_rate * (self.period_months / 12)
            amount = round(float(self.net_book_value) * period_rate, 2)
        elif self.depreciation_method in ("declining_150", "declining_200"):
            factor = 1.5 if self.depreciation_method == "declining_150" else 2.0
            useful_life_years = self.useful_life_months / 12
            annual_rate = (factor / useful_life_years) if useful_life_years > 0 else 0
            period_rate = annual_rate * (self.period_months / 12)
            declining_amount = float(self.net_book_value) * period_rate

            periods_left = max(self.total_periods - self.periods_depreciated, 1)
            straight_line_amount = remaining / periods_left

            amount = round(max(declining_amount, straight_line_amount), 2)
        else:
            amount = round(self.monthly_depreciation * self.period_months, 2)

        if amount <= 0:
            return 0.0
        return min(amount, remaining)

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
