"""Bridge to MRA_TaxInvoice_System's external Fiscalisation API
(POST /api/v1/fiscalize). Called right after an Invoice or Credit Memo is
posted to the ledger — fiscalisation is a best-effort follow-up, never
something that blocks or rolls back the actual bookkeeping entry. If the
MRA side is unreachable or misconfigured, the record still exists and is
still correctly posted; it just doesn't have an MRA status yet.
"""
import os

import requests

from app.models import CompanySettings

# Fallback only — the connection is normally set from Settings -> Company
# (CompanySettings.mra_api_url/mra_api_key), which takes priority so a
# non-technical user can (re)connect this without touching a file.
ENV_MRA_API_URL = os.environ.get("MRA_API_URL")
ENV_MRA_API_KEY = os.environ.get("MRA_API_KEY")


def _connection(company_id):
    """Each LedgerBooks company can point at its own MRA_TaxInvoice_System company,
    so the connection is always resolved from the record's own company_id — never
    from whoever happens to be logged in when a retry runs.
    """
    settings = CompanySettings.get_by_id(company_id)
    url = (settings.mra_api_url if settings else None) or ENV_MRA_API_URL
    key = (settings.mra_api_key if settings else None) or ENV_MRA_API_KEY
    return url, key


def is_configured(company_id):
    url, key = _connection(company_id)
    return bool(url and key)


def _post(company_id, payload):
    """Returns (ok, data). ok=False means either MRA rejected the request outright
    (data still has whatever JSON came back) or the request never got there at all
    (data is None) — callers treat both as ERROR, the distinction only matters for logging."""
    url, key = _connection(company_id)
    try:
        resp = requests.post(f"{url}/api/v1/fiscalize", json=payload, headers={"X-Api-Key": key}, timeout=15)
        return resp.status_code == 200, resp.json()
    except requests.exceptions.RequestException:
        return False, None


def _customer_payload(customer):
    return {
        "name": customer.name,
        "vat_number": customer.vat_number,
        "address": customer.address,
        "phone": customer.phone,
        "email": customer.email,
    }


def _line_items_payload(lines):
    return [
        {"description": l.description, "quantity": float(l.quantity), "unit_price": float(l.unit_price), "taxable": bool(l.taxable)}
        for l in lines
    ]


def fiscalize_invoice_with_mra(invoice):
    """Mutates invoice.mra_invoice_number / mra_irn / mra_status in place.
    Caller is responsible for committing afterward. Never raises."""
    if not is_configured(invoice.company_id):
        return

    ok, data = _post(invoice.company_id, {
        "document_type": "STD",
        "currency": "MUR",
        "payment_mode": "CASH",
        "customer": _customer_payload(invoice.customer),
        "line_items": _line_items_payload(invoice.lines),
    })
    if ok:
        invoice.mra_invoice_number = data.get("invoice_number")
        invoice.mra_irn = data.get("irn")
        invoice.mra_status = data.get("status")
    else:
        invoice.mra_status = "ERROR"


def fiscalize_credit_memo_with_mra(credit_memo):
    """Same pattern as fiscalize_invoice_with_mra, but posts a credit note (CRN).

    MRA requires every CRN to reference the original invoice's MRA invoice number.
    Prefer credit_memo.related_invoice (set explicitly on the form); if that's not
    set, fall back to the customer's most recently MRA-fiscalised invoice. If neither
    exists, skip fiscalisation rather than send MRA a reference that doesn't
    correspond to a real prior sale.
    """
    if not is_configured(credit_memo.company_id):
        return

    from app.models import Invoice  # local import: avoids a circular import at module load time

    reference_invoice = credit_memo.related_invoice
    if reference_invoice and not reference_invoice.mra_invoice_number:
        reference_invoice = None  # explicitly linked, but that invoice was never itself fiscalised

    if not reference_invoice:
        reference_invoice = (
            Invoice.query.filter_by(company_id=credit_memo.company_id, customer_id=credit_memo.customer_id)
            .filter(Invoice.mra_invoice_number.isnot(None))
            .order_by(Invoice.id.desc())
            .first()
        )

    if not reference_invoice:
        credit_memo.mra_status = "SKIPPED"  # no MRA-fiscalised invoice for this customer to reference
        return

    ok, data = _post(credit_memo.company_id, {
        "document_type": "CRN",
        "currency": "MUR",
        "payment_mode": "CASH",
        "reference_invoice_number": reference_invoice.mra_invoice_number,
        "reason": credit_memo.memo or f"Credit memo {credit_memo.credit_no}",
        "customer": _customer_payload(credit_memo.customer),
        "line_items": _line_items_payload(credit_memo.lines),
    })
    if ok:
        credit_memo.mra_invoice_number = data.get("invoice_number")
        credit_memo.mra_irn = data.get("irn")
        credit_memo.mra_status = data.get("status")
    else:
        credit_memo.mra_status = "ERROR"


def void_invoice_via_credit_note(invoice):
    """Called when a fiscalised invoice is voided in LedgerBooks. A fiscalised MRA
    invoice can't be deleted or altered, so this issues a full-amount credit note
    against it instead — the MRA-side record stays legally consistent with the void.
    Sets invoice.mra_void_reference to the resulting CRN's invoice number, or leaves
    it unset (silently) if MRA isn't configured or the invoice was never fiscalised
    in the first place — nothing to cancel in either case.
    """
    if not is_configured(invoice.company_id) or not invoice.mra_invoice_number:
        return

    ok, data = _post(invoice.company_id, {
        "document_type": "CRN",
        "currency": "MUR",
        "payment_mode": "CASH",
        "reference_invoice_number": invoice.mra_invoice_number,
        "reason": f"Invoice {invoice.invoice_no} voided in LedgerBooks",
        "customer": _customer_payload(invoice.customer),
        "line_items": _line_items_payload(invoice.lines),
    })
    if ok:
        invoice.mra_void_reference = data.get("invoice_number")


def void_credit_memo_via_debit_note(credit_memo):
    """Called when a fiscalised credit memo is voided in LedgerBooks. Issues a debit
    note (DRN) against the original credit note to cancel it back out, for the same
    reason void_invoice_via_credit_note() uses a CRN: nothing on the MRA side is ever
    deleted, only offset by an equal and opposite document.
    """
    if not is_configured(credit_memo.company_id) or not credit_memo.mra_invoice_number:
        return

    ok, data = _post(credit_memo.company_id, {
        "document_type": "DRN",
        "currency": "MUR",
        "payment_mode": "CASH",
        "reference_invoice_number": credit_memo.mra_invoice_number,
        "reason": f"Credit memo {credit_memo.credit_no} voided in LedgerBooks",
        "customer": _customer_payload(credit_memo.customer),
        "line_items": _line_items_payload(credit_memo.lines),
    })
    if ok:
        credit_memo.mra_void_reference = data.get("invoice_number")
