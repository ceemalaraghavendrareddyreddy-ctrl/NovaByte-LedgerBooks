const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, Table, TableRow, TableCell,
  WidthType, ShadingType, ImageRun, PageBreak, AlignmentType, LevelFormat,
} = require("docx");
const fs = require("fs");

const FONT = "Calibri";
const ACCENT = "A5691F";
const ACCENT2 = "3F7A4E";
const INK_SUB = "55604C";

function h1(text) {
  return new Paragraph({ text, heading: HeadingLevel.HEADING_1, spacing: { before: 400, after: 200 } });
}
function h2(text) {
  return new Paragraph({ text, heading: HeadingLevel.HEADING_2, spacing: { before: 300, after: 150 } });
}
function h3(text) {
  return new Paragraph({
    children: [new TextRun({ text, bold: true, size: 22, color: ACCENT })],
    spacing: { before: 220, after: 100 },
  });
}
function body(text, opts = {}) {
  return new Paragraph({ children: [new TextRun({ text, ...opts })], spacing: { after: 160 } });
}
function bullet(text) {
  return new Paragraph({ text, numbering: { reference: "bullets", level: 0 }, spacing: { after: 80 } });
}
function numbered(text) {
  return new Paragraph({ text, numbering: { reference: "numbered-steps", level: 0 }, spacing: { after: 80 } });
}
function caption(text) {
  return new Paragraph({
    children: [new TextRun({ text, italics: true, size: 18, color: INK_SUB })],
    alignment: AlignmentType.CENTER,
    spacing: { before: 100, after: 300 },
  });
}
function cell(text, opts = {}) {
  return new TableCell({
    width: { size: opts.width || 2000, type: WidthType.DXA },
    shading: opts.header ? { type: ShadingType.CLEAR, fill: "EEE8D8" } : undefined,
    children: [new Paragraph({ children: [new TextRun({ text, bold: !!opts.header, size: 20 })] })],
  });
}
function makeTable(headers, rows, widths) {
  return new Table({
    width: { size: widths.reduce((a, b) => a + b, 0), type: WidthType.DXA },
    columnWidths: widths,
    rows: [
      new TableRow({ children: headers.map((t, i) => cell(t, { header: true, width: widths[i] })) }),
      ...rows.map(r => new TableRow({ children: r.map((c, i) => cell(c, { width: widths[i] })) })),
    ],
  });
}

const diagramBuffer = fs.readFileSync("architecture_diagram.png");
const DIAG_W = 2179, DIAG_H = 1220;
const targetW = 620;
const targetH = Math.round(targetW * (DIAG_H / DIAG_W));

const doc = new Document({
  numbering: {
    config: [
      { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
      { reference: "numbered-steps", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
    ],
  },
  styles: { default: { document: { run: { font: FONT, size: 22 } } } },
  sections: [
    {
      properties: { page: { size: { width: 12240, height: 15840 } } },
      children: [
        new Paragraph({
          children: [new TextRun({ text: "LedgerBooks ↔ MRA_TaxInvoice_System", bold: true, size: 36, color: ACCENT })],
          spacing: { after: 80 },
        }),
        new Paragraph({
          children: [new TextRun({ text: "Full Working Steps & Integration Report", size: 26, color: INK_SUB })],
          spacing: { after: 400 },
        }),

        h1("1. Overview"),
        body("LedgerBooks is a full double-entry accounting system — the equivalent of QuickBooks. MRA_TaxInvoice_System is a point-of-sale, inventory, and MRA e-invoicing compliance system for Mauritius. Neither replaces the other: LedgerBooks has no MRA fiscalisation, and MRA_TaxInvoice_System has no general ledger. The two are connected by a small integration bridge so that every invoice or credit memo posted in LedgerBooks is automatically fiscalised through MRA_TaxInvoice_System, without either system needing to know the other's internal database structure. Both systems are now multi-company: each is capable of hosting several fully isolated businesses, and any LedgerBooks company can be paired with any MRA_TaxInvoice_System company independently."),

        h1("2. Architecture Diagram"),
        new Paragraph({
          children: [new ImageRun({ data: diagramBuffer, type: "png", transformation: { width: targetW, height: targetH } })],
          alignment: AlignmentType.CENTER,
        }),
        caption("How an invoice created in LedgerBooks reaches the Mauritius Revenue Authority through MRA_TaxInvoice_System."),

        h1("3. LedgerBooks — Full Working Steps"),
        body("LedgerBooks is organised into six areas, all reachable from the left-hand navigation menu, sitting on top of a multi-company (multi-tenant) foundation added in this phase of the project."),

        h2("3.1 Registration & Company Switching"),
        numbered("Register — creates a brand-new company (its own Chart of Accounts, seeded automatically) and its first (owner) login together, in one step. Every company is fully isolated: its own accounts, customers, vendors, items, invoices, bills, and document numbering (each starts fresh at INV-0001, BILL-0001, etc.)."),
        numbered("Log in — resolves to the user's home company by default. If that login has been granted access to more than one company (an accountant serving several clients), a company switcher appears in the sidebar, listing every accessible company with the active one checked."),
        numbered("Team access — an owner can grant an existing accountant login access to their company without creating a duplicate account; that accountant then sees the switcher and can move between every company they've been granted."),
        numbered("Every business table — accounts, invoices, customers, items, journal entries, and the rest — carries a company_id, and every query in every module is scoped to the active company. Fetching another company's record by guessing its ID (not just browsing to it) is blocked with a 404, not merely hidden from the list."),

        h2("3.2 Dashboard"),
        body("The landing page after login. Shows bank account balances, sales collected vs. outstanding (last 30 days), expense breakdown, a month-to-date Profit & Loss summary, bills due, low-stock alerts, and the most recent journal activity — a single glance at the state of the business."),

        h2("3.3 Banking"),
        numbered("Reconcile — match a bank statement against cleared transactions for an account, session by session."),
        numbered("Transfer Funds — move money between two of the business's own accounts (e.g. cash to bank)."),
        numbered("Make Deposit — batch several customer payments sitting in Undeposited Funds into the single lump-sum deposit that actually appears on the bank statement."),
        numbered("Import Statement — upload a bank statement CSV; the system attempts to auto-match each line to an existing transaction."),

        h2("3.4 Sales (Accounts Receivable)"),
        numbered("Add a Customer — name, contact details, VAT number, opening balance."),
        numbered("Create an Estimate (optional) — a quote that never touches the ledger until it's converted."),
        numbered("Create an Invoice — pick a customer, add line items (from the item catalog or typed manually), set the VAT rate. Saving the invoice immediately posts a balanced double-entry journal entry: debit Accounts Receivable, credit the relevant Income account(s), credit VAT Payable. If any line is a tracked inventory item, stock is issued at its moving-average cost and a COGS entry is posted in the same transaction."),
        numbered("Receive Payment — record a customer payment and apply it against one or more open invoices (supports partial payments across several invoices)."),
        numbered("Issue a Credit Memo — for a return or billing correction. Posts the reverse of an invoice entry, and can return stock to inventory. Once created, it can be applied against the customer's open invoices."),
        numbered("AR Aging — a report bucketing every customer's outstanding balance by how overdue it is (current, 1–30, 31–60, 61–90, 90+ days)."),

        h2("3.5 Expenses (Accounts Payable)"),
        numbered("Add a Vendor."),
        numbered("Raise a Purchase Order (optional) — what was ordered; doesn't touch the ledger until billed."),
        numbered("Enter a Bill — from a vendor invoice, either standalone or against an existing PO (supports partial receiving — one PO can spawn several bills over time). Posts a balanced entry: debit the expense/inventory account, credit Accounts Payable."),
        numbered("Pay Bills — record a payment to a vendor, applied against one or more open bills."),
        numbered("Vendor Credits — for a returned purchase or vendor billing correction."),
        numbered("AP Aging — the same overdue-bucketing report, for money owed instead of money owed to the business."),

        h2("3.6 Inventory"),
        body("Each item is typed as inventory, non-inventory, or service. Only inventory items carry a quantity on hand and a moving-average cost, and automatically trigger a Cost of Goods Sold posting whenever they're sold. Every quantity change — a sale, a purchase, a return, a manual adjustment — is recorded in a stock movement log with the running balance after it, so stock and the ledger can never silently drift apart."),

        h2("3.7 Accounting & Reports"),
        numbered("Chart of Accounts — the full list of Asset / Liability / Equity / Income / Expense accounts the business uses."),
        numbered("Journal — every entry ever posted, whichever module created it."),
        numbered("Trial Balance — every account's balance, debits and credits, at a point in time."),
        numbered("Profit & Loss, Balance Sheet, Cash Flow, VAT Return — the standard financial statements, generated live from the ledger."),
        numbered("Custom Report — build and save an ad-hoc account filter/grouping for one-click re-running later."),
        numbered("Budgets — set a monthly budgeted amount per account, to compare against actuals."),

        new Paragraph({ children: [new PageBreak()] }),

        h1("4. MRA_TaxInvoice_System — Full Working Steps"),

        h2("4.1 Registration & Login"),
        numbered("Register a new company — legal name, VAT registration number, BRN, address, base currency, plus the first (owner) login. Every company registered this way is fully isolated: its own invoices, customers, stock, and invoice numbering."),
        numbered("Log in — an owner or accountant logs in; if the account has access to more than one company, a switcher in the navigation bar lets them move between them."),

        h2("4.2 Dashboard & Platform"),
        body("The Dashboard shows invoice count, total sales, total VAT collected, the most recent invoices, and — automatically — a warning banner if the business's rolling 12-month turnover is approaching or has crossed the compulsory VAT registration threshold (MUR 3 million, per the Finance Act 2025). The Platform page lays out the six core disciplines of the system as a single reference screen."),

        h2("4.3 Point of Sale"),
        numbered("Click an item to add it to the cart (quantity increments on repeat clicks)."),
        numbered("Pick a customer, or leave as Walk-in / Cash sale."),
        numbered("Click Checkout & Fiscalize — one action creates the invoice, calculates VAT, deducts stock, and attempts fiscalisation."),

        h2("4.4 Invoices"),
        numbered("New Invoice — choose a document type (Standard, Proforma, Training, Credit Note, Debit Note), a customer, and line items. VAT is calculated per MRA's TC01–TC06 supply-type codes. A Credit or Debit Note requires the original invoice number it references."),
        numbered("Every invoice is hash-chained to the one before it — each invoice's hash is computed from its date, amount, the company's BRN, and its own invoice number, and is fed into the next invoice, so no invoice can be quietly altered after the fact without breaking the chain."),
        numbered("Export CSV — a one-click spreadsheet of every invoice, ready to hand to an accountant."),

        h2("4.5 Inventory"),
        numbered("Stock levels update automatically on every sale, and restock automatically on every credit note — no manual reconciliation."),
        numbered("Adjust Stock — a manual correction (restock, stock count adjustment), always with a required note."),
        numbered("Movement Log — the full audit trail behind every stock number: every sale, return, and adjustment, timestamped and linked to the invoice that caused it."),

        h2("4.6 Analytics"),
        body("A 14-day sales trend, VAT payable (today and month-to-date), and margin per item — all computed live from the invoices already in the system."),

        h2("4.7 Settings"),
        numbered("Company details — legal name, VAT number, BRN, address, default currency."),
        numbered("MRA e-Invoicing — environment (Offline / Sandbox / Live), EBS MRA ID, TAN, MRAID Portal credentials, MRA's public key and this business's own private key."),
        numbered("Exchange Rates — manually maintained FX rates for any foreign currency the business invoices in; each invoice locks in the rate current at the moment it's created."),
        numbered("SMS Notifications — provider details for sending an invoice-ready SMS to a customer."),
        numbered("External Integration (API Key) — generates the key another system (like LedgerBooks) uses to call this company's fiscalisation API. See Section 6."),

        h2("4.8 Team"),
        body("The owner grants an accountant login scoped access to this company here. One accountant account can hold access to several companies, each granted separately by that company's owner, and switches between them using the company selector in the navigation bar."),

        h2("4.9 MRA Transmission Queue"),
        body("If a real (Sandbox/Live) fiscalisation attempt fails for a connectivity or authentication reason — not because MRA rejected the invoice — it is placed in this queue instead of being lost. A Sync Now button retries every pending invoice in strict sequence order, since MRA requires invoice numbering to stay sequential even through an outage."),

        new Paragraph({ children: [new PageBreak()] }),

        h1("5. The Bridge — How the Two Systems Connect"),

        h2("5.1 The interface"),
        body("MRA_TaxInvoice_System exposes one endpoint: POST /api/v1/fiscalize. It takes a generic invoice payload — a customer and a list of line items — and knows nothing about which external system is calling it. Authentication is a single API key per company (X-Api-Key header), generated and rotated from that company's own Settings page. On the LedgerBooks side, every fiscalisation call now resolves its connection (URL + key) from the specific LedgerBooks company that owns the invoice being posted — not from whichever user happens to be logged in — so each LedgerBooks company can be paired with a different MRA_TaxInvoice_System company independently. This was verified live: a second company registered on each side, each with its own API key, correctly fiscalised only against its own counterpart (Section 6)."),

        h2("5.2 What LedgerBooks sends"),
        makeTable(
          ["Field", "Source in LedgerBooks", "Notes"],
          [
            ["document_type", "\"STD\" for invoices, \"CRN\" for credit memos", "Fixed per call site"],
            ["customer.name / vat_number / address / phone / email", "Invoice.customer / CreditMemo.customer", "Matched or auto-created as an MRA customer by name + VAT number"],
            ["line_items[].description / quantity / unit_price / taxable", "InvoiceLine / CreditMemoLine", "taxable=true maps to TC01 (15%); false maps to TC06 (outside scope) as a placeholder"],
            ["reference_invoice_number", "Credit memos only", "The customer's most recent MRA-fiscalised invoice — see limitation in Section 7"],
          ],
          [2600, 3200, 3800],
        ),

        h2("5.3 Step by step: creating an invoice"),
        numbered("A user fills in and submits the New Invoice form in LedgerBooks."),
        numbered("LedgerBooks builds the double-entry journal entry (debit Accounts Receivable, credit Income, credit VAT Payable, plus COGS lines for any tracked items) and commits it — the invoice now exists in LedgerBooks' own books, independent of anything that happens next."),
        numbered("LedgerBooks calls MRA_TaxInvoice_System's /api/v1/fiscalize with the customer and line items."),
        numbered("MRA_TaxInvoice_System finds or creates the matching customer, builds and hash-chains the invoice, calculates VAT, and — depending on the company's configured environment — either marks it OFFLINE (no MRA certification yet) or attempts real fiscalisation."),
        numbered("The result (invoice number, IRN, status) is returned to LedgerBooks and saved directly on that LedgerBooks invoice, visible on its detail page."),

        h2("5.4 Step by step: creating a credit memo"),
        body("Identical to the invoice flow, except the document type is CRN, and LedgerBooks looks up the customer's most recently MRA-fiscalised invoice to use as the required reference."),

        h2("5.5 Design principle: fiscalisation never blocks bookkeeping"),
        body("The ledger entry in LedgerBooks is always committed before the MRA call is made, and that call is wrapped so it can never raise an error back into the invoice-creation flow. If MRA_TaxInvoice_System is completely unreachable, the LedgerBooks invoice still exists, still balances, and still shows correctly in every report — it simply carries an ERROR status until the connection is restored."),

        h1("6. Verified Test Results"),
        body("Both directions of the bridge were tested against the real running systems, not simulated."),
        makeTable(
          ["Test", "LedgerBooks record", "MRA_TaxInvoice_System record", "Result"],
          [
            ["Invoice", "INV-0020 — Rainbow Garments Co, 1,500 + 225 VAT = 1,725.00", "STD-000004, same customer, same total", "Matched exactly"],
            ["Credit Memo", "CM-0002 — 500 + 75 VAT = 575.00, referencing INV-0020's sale", "CRN-000002, correctly typed as a credit note, referencing STD-000004", "Matched exactly"],
            ["Cross-company bridge", "Company \"Second Test Co Ltd\" → INV-0001, 1,000 + 150 VAT = 1,150.00", "Company \"MRA Second Co Ltd\" → STD-000001, IRN OFFLINE-E7E868E322DF", "Landed only in its own paired MRA company — confirmed absent from the original company's invoice list"],
            ["Cross-tenant isolation", "Fetching /sales/invoices/1 and /ledger/accounts/1 (Company 1's records) while active in Company 2", "n/a", "404 in both cases — blocked at the record-ID level, not just hidden from lists"],
          ],
          [1600, 3500, 3000, 1500],
        ),

        h1("7. Multi-Company Build (Fix #5)"),
        body("LedgerBooks was originally a single-company system — one implicit business, no concept of a tenant boundary. This phase rebuilt it to match the multi-company architecture MRA_TaxInvoice_System already had, so the same bridge pattern from Section 5 now works correctly when either side serves more than one business."),

        h2("7.1 Data model"),
        bullet("company_settings changed from a single enforced row to one row per tenant — every other business table (accounts, customers, vendors, items, invoices, bills, credit memos, vendor credits, purchase orders, payments, journal entries, budgets, saved reports, deposits, bank statement imports, audit logs, users) now carries a company_id foreign key back to it."),
        bullet("Document numbering (invoice_no, bill_no, estimate_no, credit_no, po_no) and the Chart of Accounts code, plus usernames, moved from globally-unique to unique-per-company — each new company starts its own numbering at 0001 without colliding with any other company's."),
        bullet("A user_companies join table allows one accountant login to hold access to several companies, mirroring the same pattern already used on the MRA_TaxInvoice_System side."),
        bullet("Applied against the live production database via a migration script that added every column, backfilled all existing data to a single \"Company #1\", and swapped the old global unique constraints for composite ones — with zero data loss."),

        h2("7.2 Registration, login, and scoping"),
        bullet("A new /register endpoint creates a company, seeds its starter Chart of Accounts, and creates its first (owner) login, in one step."),
        bullet("Every route across all thirteen blueprints (Sales, Purchases, Ledger, Inventory, Banking, Reports, Dashboard, Budgets, Audit, Users, Attachments, Settings, the MRA Sync retry queue) was swept to filter every query by the active company, and to verify — not just filter — that any record looked up by ID actually belongs to it."),
        bullet("Document attachments (which have no company_id of their own, since they're linked polymorphically to their parent record) are checked by first confirming that parent record belongs to the active company, closing what would otherwise have been a gap in the sweep."),

        h2("7.3 Resolution of the four bridge limitations from the original build"),
        bullet("Voiding sync, automatic retry, and the credit memo reference heuristic (originally flagged as limitations) were all fixed in an earlier pass of this same project, ahead of the multi-company work: voiding now issues a matching credit/debit note on the MRA side; a Sync Now-style retry queue exists in LedgerBooks itself for bridge-connectivity failures; and the credit memo reference now uses an explicit \"Related Invoice\" selection on the form, falling back to the heuristic only when left blank."),
        bullet("The API key is now editable directly from LedgerBooks' own Settings screen (Section 4.7 mirror) — and, with this phase's change, resolved per-company rather than from a single .env file, so multiple LedgerBooks companies can each point at a different MRA_TaxInvoice_System company without any file editing."),
        bullet("The fifth and last original limitation — \"LedgerBooks is a single-company system\" — is what this section documents the resolution of."),

        h1("8. Remaining Known Limitation"),
        bullet("Backup/restore (Settings → Backup) still operates on the whole MySQL database via mysqldump/mysql, covering every company in one file — there is no per-company selective export or restore yet. This is a genuinely different piece of work (a real data export/import layer) rather than another query-scoping pass, and was intentionally left out of this phase."),
      ],
    },
  ],
});

Packer.toBuffer(doc).then((buffer) => {
  fs.writeFileSync("LedgerBooks_MRA_Integration_Report.docx", buffer);
  console.log("done");
});
