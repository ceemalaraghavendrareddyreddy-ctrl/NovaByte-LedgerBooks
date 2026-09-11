const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, Table, TableRow, TableCell,
  WidthType, ShadingType, ImageRun, AlignmentType, LevelFormat,
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
function body(text, opts = {}) {
  return new Paragraph({ children: [new TextRun({ text, ...opts })], spacing: { after: 160 } });
}
function bullet(text) {
  return new Paragraph({ text, numbering: { reference: "bullets", level: 0 }, spacing: { after: 80 } });
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

const diagramBuffer = fs.readFileSync("example_transaction_diagram.png");
const DIAG_W = 1900, DIAG_H = 3100;
const targetW = 460;
const targetH = Math.round(targetW * (DIAG_H / DIAG_W));

const doc = new Document({
  numbering: {
    config: [
      { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
    ],
  },
  styles: { default: { document: { run: { font: FONT, size: 22 } } } },
  sections: [
    {
      properties: { page: { size: { width: 12240, height: 15840 } } },
      children: [
        new Paragraph({
          children: [new TextRun({ text: "One Transaction, End to End", bold: true, size: 36, color: ACCENT })],
          spacing: { after: 80 },
        }),
        new Paragraph({
          children: [new TextRun({ text: "A worked example from LedgerBooks through MRA_TaxInvoice_System, using a real transaction posted during testing — every value below is what actually happened, not a mock-up.", size: 24, color: INK_SUB })],
          spacing: { after: 400 },
        }),

        h1("1. The Transaction"),
        makeTable(
          ["Field", "Value"],
          [
            ["LedgerBooks company", "Second Test Co Ltd  (Company #2)"],
            ["Customer", "Co2 Customer"],
            ["Invoice", "INV-0001 — 2026-08-14, due 2026-09-13"],
            ["Line item", "Test Service — Qty 1 @ 1,000.00, taxable"],
            ["VAT rate", "15% (standard Mauritius rate)"],
            ["Subtotal / VAT / Total", "1,000.00 / 150.00 / 1,150.00"],
          ],
          [3200, 6400],
        ),

        h1("2. Schematic Flow"),
        new Paragraph({
          children: [new ImageRun({ data: diagramBuffer, type: "png", transformation: { width: targetW, height: targetH } })],
          alignment: AlignmentType.CENTER,
        }),
        caption("INV-0001, step by step — from the New Invoice form in LedgerBooks (Second Test Co Ltd) to a fiscalised record in MRA_TaxInvoice_System (MRA Second Co Ltd)."),

        h1("3. Step-by-Step Narrative"),

        h2("Step 1 — The form"),
        body("A user, logged into LedgerBooks with Second Test Co Ltd as the active company, opens New Invoice, selects customer Co2 Customer, and adds one line: \"Test Service\", quantity 1, unit price 1,000.00, marked taxable, posting to income account 4000 – Sales Revenue."),

        h2("Step 2 — Posted to the ledger"),
        body("On submit, LedgerBooks assigns the next invoice number scoped to this company alone — INV-0001, because this is Second Test Co Ltd's first invoice, regardless of what number any other company is up to — and immediately builds and commits a balanced double-entry Journal Entry (#60):"),
        makeTable(
          ["Account", "Debit", "Credit"],
          [
            ["1200 — Accounts Receivable", "1,150.00", ""],
            ["4000 — Sales Revenue", "", "1,000.00"],
            ["2100 — VAT Payable", "", "150.00"],
          ],
          [4800, 2400, 2400],
        ),
        body("This step is final and independent of everything that follows — the invoice is correctly posted in LedgerBooks' own books whether or not MRA_TaxInvoice_System is even reachable."),

        h2("Step 3 — The bridge call"),
        body("Immediately after committing, LedgerBooks fires POST http://localhost:5059/api/v1/fiscalize, authenticated with the X-Api-Key belonging specifically to Second Test Co Ltd's own MRA connection (configured on its own Settings page) — not a key shared across every LedgerBooks company. The payload is the customer's details and the one line item, in MRA_TaxInvoice_System's generic format."),

        h2("Steps 4–5 — Received and fiscalised"),
        body("MRA_TaxInvoice_System, running as company MRA Second Co Ltd, matches or auto-creates a customer record for \"Co2 Customer\", classifies the line under VAT code TC01 (the standard 15% rate), and computes the same total independently: 1,000.00 + 150.00 = 1,150.00 MUR. Because this MRA company has no live MRA EBS certification configured, the invoice is fiscalised in offline mode: invoice number STD-000001, IRN OFFLINE-E7E868E322DF."),

        h2("Steps 6–7 — Result written back"),
        body("The response is saved directly onto the LedgerBooks invoice — mra_invoice_number, mra_irn, and mra_status — and shown on the invoice's own detail page as \"MRA: OFFLINE · STD-000001 · IRN OFFLINE-E7E868E322DF\", with no further action needed from the user."),

        h1("4. Independent Verification"),
        body("Both ends of this transaction were checked directly against their own systems, not inferred:"),
        bullet("LedgerBooks, Company 2 (Second Test Co Ltd): invoice detail page for INV-0001 shows MRA STD-000001 / OFFLINE-E7E868E322DF."),
        bullet("MRA_TaxInvoice_System, company MRA Second Co Ltd: STD-000001 appears in its own invoice list, customer Co2 Customer, total 1,150.00 MUR — matching exactly."),
        bullet("Confirmed absent from Company 1 on the LedgerBooks side and from the original MRA company on the MRA side — this transaction belongs to exactly one pair of companies, with no cross-tenant bleed in either direction."),

        h1("5. Why This Example Matters"),
        body("This single transaction exercises every layer added in the multi-company build: company-scoped invoice numbering (INV-0001, not a number inherited from another company's sequence), a per-company MRA connection resolved from the invoice's own company_id rather than from whoever is logged in, and a ledger entry that posts and stays correct in LedgerBooks regardless of what MRA_TaxInvoice_System does with it afterward."),
      ],
    },
  ],
});

Packer.toBuffer(doc).then((buffer) => {
  fs.writeFileSync("Example_Transaction_Report.docx", buffer);
  console.log("done");
});
