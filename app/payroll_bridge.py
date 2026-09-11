"""Inbound bridge: an external payroll system (e.g. Sicorax/Payroll.py) pushes
one summarized journal entry per Finalized payroll run via POST /api/v1/payroll/import.

Mirrors app/mra_bridge.py in shape but runs the opposite direction — LedgerBooks
is the SERVER being authenticated against here, not the caller. Each company's
own payroll_api_key (Settings → Regenerate) is presented as X-Api-Key; there is
no Flask-Login session on this route at all, since the caller is another
application, not a person with a browser.

Deliberately posts ONE entry per run, not one line per employee — the same way
a real accountant would post a payroll run to the general ledger. Per-employee
detail (payslips, statutory returns) is expected to stay the system of record
on the payroll side; LedgerBooks only needs what actually hits the GL:

    Dr Salaries & Wages Expense           (gross pay)
    Dr Employer Statutory Contributions   (employer's CSG/NSF/HRDC/PRGF/levy share)
    Cr PAYE Payable
    Cr CSG Payable                        (employee + employer share combined)
    Cr NSF Payable                        (employee + employer share combined)
    Cr HRDC Levy Payable
    Cr PRGF Payable
    Cr Foreign Worker Levy Payable
    Cr Payroll Deductions Payable         (loan/other deductions withheld from employees)
    Cr Net Salaries Payable               (what's actually owed out to employees)

These sum to a balanced entry by construction: gross pay equals PAYE + employee
CSG/NSF + other deductions + net pay (the same arithmetic the payroll system
itself uses to arrive at net pay), and every employer-only contribution appears
on both sides in identical amounts.

Idempotent by `reference`: pushing the same period twice updates the existing
entry (deletes and rebuilds its lines) instead of creating a duplicate — a
payroll system retrying after a network blip, or deliberately re-pushing after
a correction, never doubles the numbers in the ledger.
"""
from datetime import date

from flask import Blueprint, jsonify, request

from app import db
from app.models import Account, CompanySettings, JournalEntry, JournalLine

payroll_bridge_bp = Blueprint("payroll_bridge", __name__, url_prefix="/api/v1")

SALARIES_EXPENSE_CODE = "6200"
EMPLOYER_CONTRIB_EXPENSE_CODE = "6210"
PAYE_PAYABLE_CODE = "2300"
CSG_PAYABLE_CODE = "2310"
NSF_PAYABLE_CODE = "2320"
HRDC_PAYABLE_CODE = "2330"
PRGF_PAYABLE_CODE = "2340"
FOREIGN_LEVY_PAYABLE_CODE = "2350"
NET_SALARIES_PAYABLE_CODE = "2360"
DEDUCTIONS_PAYABLE_CODE = "2370"

REQUIRED_TOTALS_FIELDS = [
    "gross_pay", "paye", "csg_employee", "csg_employer", "nsf_employee", "nsf_employer",
    "hrdc_employer", "prgf_employer", "foreign_worker_levy", "other_deductions", "net_pay",
]


def _get_account(company_id, code):
    return Account.query.filter_by(company_id=company_id, code=code).first()


@payroll_bridge_bp.route("/payroll/import", methods=["POST"])
def import_payroll_run():
    api_key = request.headers.get("X-Api-Key")
    company = CompanySettings.get_by_payroll_api_key(api_key) if api_key else None
    if not company:
        return jsonify({"error": "Invalid or missing API key."}), 401

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "Request body must be JSON."}), 400

    reference = (payload.get("reference") or "").strip()
    period_year = payload.get("period_year")
    period_month = payload.get("period_month")
    totals = payload.get("totals") or {}

    if not reference or not period_year or not period_month:
        return jsonify({"error": "reference, period_year, and period_month are required."}), 400
    try:
        period_year, period_month = int(period_year), int(period_month)
    except (TypeError, ValueError):
        return jsonify({"error": "period_year and period_month must be integers."}), 400
    missing = [f for f in REQUIRED_TOTALS_FIELDS if f not in totals]
    if missing:
        return jsonify({"error": f"totals is missing required field(s): {', '.join(missing)}"}), 400

    try:
        totals = {f: round(float(totals[f]), 2) for f in REQUIRED_TOTALS_FIELDS}
    except (TypeError, ValueError):
        return jsonify({"error": "All totals values must be numbers."}), 400

    accounts = {
        "salaries": _get_account(company.id, SALARIES_EXPENSE_CODE),
        "employer_contrib": _get_account(company.id, EMPLOYER_CONTRIB_EXPENSE_CODE),
        "paye": _get_account(company.id, PAYE_PAYABLE_CODE),
        "csg": _get_account(company.id, CSG_PAYABLE_CODE),
        "nsf": _get_account(company.id, NSF_PAYABLE_CODE),
        "hrdc": _get_account(company.id, HRDC_PAYABLE_CODE),
        "prgf": _get_account(company.id, PRGF_PAYABLE_CODE),
        "foreign_levy": _get_account(company.id, FOREIGN_LEVY_PAYABLE_CODE),
        "net_salaries": _get_account(company.id, NET_SALARIES_PAYABLE_CODE),
        "deductions": _get_account(company.id, DEDUCTIONS_PAYABLE_CODE),
    }
    missing_accounts = [code for code, acct in [
        (SALARIES_EXPENSE_CODE, accounts["salaries"]), (EMPLOYER_CONTRIB_EXPENSE_CODE, accounts["employer_contrib"]),
        (PAYE_PAYABLE_CODE, accounts["paye"]), (CSG_PAYABLE_CODE, accounts["csg"]), (NSF_PAYABLE_CODE, accounts["nsf"]),
        (HRDC_PAYABLE_CODE, accounts["hrdc"]), (PRGF_PAYABLE_CODE, accounts["prgf"]),
        (FOREIGN_LEVY_PAYABLE_CODE, accounts["foreign_levy"]), (NET_SALARIES_PAYABLE_CODE, accounts["net_salaries"]),
        (DEDUCTIONS_PAYABLE_CODE, accounts["deductions"]),
    ] if not acct]
    if missing_accounts:
        return jsonify({
            "error": f"Required account(s) missing from the Chart of Accounts: {', '.join(missing_accounts)}. "
                     f"Run migrate_payroll_bridge.py, or add them manually."
        }), 400

    # Idempotent: a retry (or a deliberate re-push after correcting a run) updates the
    # existing entry for this reference instead of creating a duplicate.
    existing = JournalEntry.query.filter_by(
        company_id=company.id, source_type="payroll_import", reference_no=reference
    ).first()
    if existing:
        JournalLine.query.filter_by(journal_entry_id=existing.id).delete()
        entry = existing
        entry.memo = f"Payroll {period_year}-{period_month:02d} (updated)"
    else:
        entry = JournalEntry(
            company_id=company.id,
            entry_date=date(period_year, period_month, 1),
            reference_no=reference,
            memo=f"Payroll {period_year}-{period_month:02d}",
            source_type="payroll_import",
        )
        db.session.add(entry)

    employer_contrib_total = round(
        totals["csg_employer"] + totals["nsf_employer"] + totals["hrdc_employer"]
        + totals["prgf_employer"] + totals["foreign_worker_levy"], 2
    )

    def line(account, debit=0, credit=0):
        if debit or credit:
            entry.lines.append(JournalLine(account=account, debit=debit, credit=credit, memo=reference))

    line(accounts["salaries"], debit=totals["gross_pay"])
    line(accounts["employer_contrib"], debit=employer_contrib_total)
    line(accounts["paye"], credit=totals["paye"])
    line(accounts["csg"], credit=round(totals["csg_employee"] + totals["csg_employer"], 2))
    line(accounts["nsf"], credit=round(totals["nsf_employee"] + totals["nsf_employer"], 2))
    line(accounts["hrdc"], credit=totals["hrdc_employer"])
    line(accounts["prgf"], credit=totals["prgf_employer"])
    line(accounts["foreign_levy"], credit=totals["foreign_worker_levy"])
    line(accounts["deductions"], credit=totals["other_deductions"])
    line(accounts["net_salaries"], credit=totals["net_pay"])

    db.session.flush()
    if not entry.is_balanced:
        db.session.rollback()
        return jsonify({
            "error": f"Computed entry does not balance (debit {entry.total_debit}, credit {entry.total_credit}) "
                     f"— rejected without posting. Check the totals sent."
        }), 400

    db.session.commit()
    return jsonify({
        "status": "ok",
        "journal_entry_id": entry.id,
        "journal_reference": reference,
        "debit_total": float(entry.total_debit),
    }), 200
