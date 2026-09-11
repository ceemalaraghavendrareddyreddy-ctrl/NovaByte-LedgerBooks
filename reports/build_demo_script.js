const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, Table, TableRow, TableCell,
  WidthType, ShadingType, AlignmentType, LevelFormat,
} = require("docx");
const fs = require("fs");

const FONT = "Calibri";
const ACCENT = "A5691F";
const ACCENT2 = "3F7A4E";
const WARN = "B03A2E";
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
function say(text) {
  return new Paragraph({
    children: [new TextRun({ text: "SAY: ", bold: true, color: ACCENT2, size: 20 }), new TextRun({ text: `"${text}"`, italics: true, size: 20, color: INK_SUB })],
    spacing: { after: 160 }, indent: { left: 360 },
  });
}
function warn(text) {
  return new Paragraph({
    children: [new TextRun({ text: "⚠ ", bold: true, color: WARN, size: 20 }), new TextRun({ text, bold: true, size: 20, color: WARN })],
    spacing: { after: 160 },
  });
}
function bullet(text) {
  return new Paragraph({ text, numbering: { reference: "bullets", level: 0 }, spacing: { after: 80 } });
}
function numbered(text, ref = "numbered-steps") {
  return new Paragraph({ text, numbering: { reference: ref, level: 0 }, spacing: { after: 80 } });
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

const doc = new Document({
  numbering: {
    config: [
      { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
      { reference: "numbered-steps", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
      { reference: "numbered-steps2", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
    ],
  },
  styles: { default: { document: { run: { font: FONT, size: 22 } } } },
  sections: [
    {
      properties: { page: { size: { width: 12240, height: 15840 } } },
      children: [
        new Paragraph({
          children: [new TextRun({ text: "Demo Rehearsal Script", bold: true, size: 36, color: ACCENT })],
          spacing: { after: 80 },
        }),
        new Paragraph({
          children: [new TextRun({ text: "Practice guide for the live \"register → invoice → fiscalize → switch\" demo, click by click.", size: 24, color: INK_SUB })],
          spacing: { after: 400 },
        }),

        h1("Before You Practice: Quick Reference"),
        makeTable(
          ["", "URL", "Existing login (for Step 4's contrast company)"],
          [
            ["LedgerBooks", "http://localhost:5057", "admin / admin123"],
            ["MRA_TaxInvoice_System", "http://localhost:5059", "(create your own demo login — see One-Time Prep)"],
          ],
          [2600, 3400, 3400],
        ),
        body("Both are started the same way you've been running them all along — the Browser pane, or by hand if you're rehearsing outside this session. Nothing below needs internet access; it all runs on your own machine."),
        warn("A one-time registration named 'demo_reference' on MRA_TaxInvoice_System (company 'LoomStack Demo Reference Ltd') already exists from an earlier verification pass — reuse it, or register your own; either works. Check Settings → External Integration on that login before re-registering, to avoid creating a duplicate reference company."),

        h1("One-Time Prep (do this once, before your first rehearsal)"),
        body("This is the part that must NOT happen live in front of a prospect — it's slow and fiddly the first time. Do it once, now, and you'll never need to repeat it."),
        numbered("Open http://localhost:5059/register and register a permanent \"reference\" company you'll reuse for every practice run and every real demo. Suggested values:"),
        makeTable(
          ["Field", "Value to type"],
          [
            ["Legal Name", "LoomStack Demo Reference Ltd"],
            ["VAT Registration No.", "12345678"],
            ["BRN", "C99999999"],
            ["Business Address", "1 Demo Street, Port Louis"],
            ["Full name", "your own name"],
            ["Username", "demo_reference"],
            ["Password / Confirm", "demo123456"],
          ],
          [3200, 5800],
        ),
        numbered("Log in with that username/password. Go to Settings (left nav) → find \"External Integration (API Key)\" near the bottom."),
        numbered("Click \"Regenerate API Key\" once. A key appears — something like adc1bad8...c9de9d21. This is the key every practice LedgerBooks company will connect through."),
        numbered("Copy that key into a plain text file on your Desktop — e.g. demo_mra_key.txt — alongside the URL http://localhost:5059. Keep that file open every time you rehearse or demo. This is your \"cheat sheet\" for the fast paste in Step 2 of each run below."),
        warn("Do this prep at least once before your first rehearsal — skip it and Step 3 of every run below will silently show nothing (see the callout in Step 2)."),

        h1("The 90-Second Talk Track"),
        body("Say this while you click — don't narrate the software, narrate what it means for them:"),
        say("This is a brand-new client company — I haven't touched this before today."),
        say("Watch the clock — registering, creating a real invoice, and getting it MRA-compliant takes under a minute."),
        say("...and if you have fifteen clients, each one looks exactly like this — fully separate books, one login for you."),

        h1("Example Run #1 — Services Business"),
        body("Practice this one first. A consulting company issuing one invoice for advisory work."),

        h2("Step 1 — Register the company (aim: 30–40 seconds)"),
        numbered("Go to http://localhost:5057/register."),
        numbered("Fill in exactly:"),
        makeTable(
          ["Field", "Type this"],
          [
            ["Business Name", "Rainbow Consulting Ltd"],
            ["Your Name", "your own name"],
            ["Username", "demo_owner"],
            ["Password", "demo123456"],
            ["Confirm Password", "demo123456"],
          ],
          [3200, 5800],
        ),
        numbered("Click \"Create Company\". You land straight on the Dashboard, already logged in — no separate login step."),
        say("That's it — a fully isolated set of books, its own Chart of Accounts already seeded, ready to invoice from."),

        h2("Step 2 — Connect it to MRA (aim: 15–20 seconds)"),
        warn("This step is easy to forget in rehearsal — a brand-new company has NO MRA connection configured until you do this. Skip it and the invoice you're about to create will show no MRA status at all, with no error to explain why."),
        numbered("Click Settings in the left sidebar (under Admin)."),
        numbered("Scroll to \"MRA API URL\" and \"MRA API Key\"."),
        numbered("Paste in http://localhost:5059 and the key from your demo_mra_key.txt file."),
        numbered("Click \"Save Settings\"."),
        say("This one-time link is what turns on automatic compliance — from here every invoice fiscalizes itself."),

        h2("Step 3 — Create the customer (aim: 15 seconds)"),
        numbered("Sales → Customers → \"+ New Customer\"."),
        numbered("Name: Baobab Textiles Ltd — leave every other field blank, click Save Customer."),

        h2("Step 4 — Create and post the invoice (aim: 40–50 seconds)"),
        numbered("Sales → Invoices → New Invoice."),
        numbered("Customer: Baobab Textiles Ltd."),
        numbered("Leave Invoice Date and Due Date at their defaults."),
        numbered("Memo: Consulting services - August."),
        numbered("On the line: leave the Item dropdown at \"-- manual line --\" (a brand-new company has no items yet — that's expected, not a bug). Description: Business advisory - August. Qty: 1. Unit Price: 5000. Leave Taxable checked."),
        numbered("Watch the Subtotal / VAT / Total boxes update live as you type — the screen will read Subtotal 5000.00, VAT 750.00, Total 5750.00 (LedgerBooks doesn't use thousand-separator commas on screen)."),
        numbered("Click \"Save & Post Invoice\"."),
        say("That single click just posted a balanced double-entry journal entry AND sent it to MRA for fiscalisation — both in one step."),

        h2("Step 5 — Show it landed on the MRA side (aim: 15 seconds)"),
        numbered("On the invoice detail page you're now viewing, point at the line under the header: \"MRA: OFFLINE · [invoice number] · IRN [...]\" — this appeared automatically, nothing was clicked to make it happen."),
        numbered("Switch to your already-open MRA_TaxInvoice_System tab (logged in as demo_reference), click Invoices in the nav, and point at the newest row — same customer, same total, already there."),
        say("I didn't touch that system — LedgerBooks called it directly the moment I saved the invoice."),

        h2("Step 6 — Switch companies to show isolation (aim: 15 seconds)"),
        numbered("Back in LedgerBooks, log out and log back in as admin / admin123 (this account has access to more than one company, so the switcher will appear in the sidebar)."),
        numbered("Click the company switcher at the top of the sidebar, and pick a different company from the list."),
        numbered("Land on that company's Dashboard — different invoices, different balances, completely separate."),
        say("This is what it looks like when you have fifteen clients — one login, a dropdown, zero chance of one client ever seeing another's numbers."),

        h1("Example Run #2 — Retail Business (practice variant)"),
        body("Once Run #1 feels smooth, do it again with different data so you're not just reciting memorized values — you're comfortable with the shape of the flow."),
        makeTable(
          ["Step", "Use this instead"],
          [
            ["Register", "Business Name: Ocean Breeze Retail Ltd  ·  Username: demo_owner2"],
            ["Customer", "Coastal Hardware Mauritius"],
            ["Invoice memo", "POS restock - August batch"],
            ["Invoice line", "Description: Assorted hardware supplies · Qty: 3 · Unit Price: 850.00"],
            ["Expected totals", "Subtotal 2550.00 · VAT 382.50 · Total 2932.50"],
          ],
          [2400, 6600],
        ),
        body("Everything else — connect to MRA, save & post, show it landed, switch companies — is identical to Run #1. Repeating the same six steps with different numbers is exactly the muscle memory you want before doing this in front of someone."),

        h1("Self-Timing Checklist"),
        makeTable(
          ["Step", "Target time", "Your time (fill in as you practice)"],
          [
            ["1. Register company", "30–40 sec", ""],
            ["2. Connect to MRA", "15–20 sec", ""],
            ["3. Create customer", "15 sec", ""],
            ["4. Create & post invoice", "40–50 sec", ""],
            ["5. Show MRA side", "15 sec", ""],
            ["6. Switch companies", "15 sec", ""],
            ["Total", "under 3 minutes", ""],
          ],
          [3000, 2600, 3400],
        ),
        body("Note the original target in Step 3 of your outreach plan was \"under a minute\" for registration alone — the full six-step demo above (including the MRA connect and the company switch) realistically runs closer to 2–3 minutes once smooth. That's still well inside a 10-minute meeting slot with room for questions."),

        h1("If Something Goes Wrong Mid-Demo"),
        bullet("No MRA line appears on the invoice → you skipped Step 2 (connect to MRA) for this company. Recovery: go to Settings now, paste the key, then void and recreate the invoice — or simply say \"let me show you that connection step separately\" and move to Step 5 using an existing invoice."),
        bullet("Username already taken → someone practiced with demo_owner already today. Just add a number: demo_owner3."),
        bullet("Forgot exact numbers mid-sentence → that's fine, say the real number is less important than the fact that both systems agree on it — then let the on-screen totals speak for themselves."),
        bullet("Nothing loads → check both servers are actually running before the meeting starts, not during it."),
      ],
    },
  ],
});

Packer.toBuffer(doc).then((buffer) => {
  fs.writeFileSync("Demo_Rehearsal_Script.docx", buffer);
  console.log("done");
});
