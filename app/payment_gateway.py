"""Online payment gateway bridge — lets a customer pay an invoice from the
portal (a real "Pay Now" button) instead of a payment only ever being entered
manually by staff.

Provider: DPO Group ("DPO Pay by Network", https://docs.dpopay.com/), chosen
for real MUR settlement to a Mauritius bank — verified against their public
docs 2026-09-25 (Stripe doesn't fully support Mauritius; PayPal settles in
USD/EUR, not MUR). Every field/endpoint below is confirmed against
docs.dpopay.com's Create Token / Verify Token reference pages and their
worked XML examples, not inferred.

Flow (their "Hosted Payment Page" pattern — the customer's card details never
touch LedgerBooks):
  1. Customer clicks "Pay Now" on their portal invoice.
  2. create_payment_token() posts createToken, gets back a TransToken.
  3. Customer is redirected to DPO's hosted checkout page to actually pay.
  4. DPO redirects back to our RedirectURL when they're done (paid or not).
  5. verify_payment() re-checks the real status server-to-server via
     verifyToken — the redirect itself is never trusted as proof of payment,
     only this API call is (a customer could hit the redirect URL directly
     without ever paying).
  6. Result 000 = paid -> a real Payment is recorded and posted to the
     ledger, exactly as if staff had entered it manually (app/sales.py's
     post_payment), landing in Undeposited Funds until the actual bank
     deposit shows up — same as every other payment method.

IMPORTANT — not live-tested end-to-end: this environment's outbound requests
to secure.3gdirectpay.com were blocked by DPO's own CloudFront WAF (a 403,
likely an IP-range block on sandbox traffic from cloud/datacenter IPs — a
common anti-bot measure, not a bug in this code). The request/response shapes
below are built directly from DPO's own published examples, but the actual
live round-trip needs testing from a real server/network before relying on
it — test with the pre-provisioned sandbox Company Token from
docs.dpopay.com/dpo-pay-by-network/reference/sandbox-test-credentials before
switching gateway_sandbox off.
"""
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal

import requests
from flask import url_for

from app.models import CompanySettings

API_BASE = "https://secure.3gdirectpay.com/API/v6/"
CHECKOUT_URL = "https://secure.3gdirectpay.com/pay.asp?ID={trans_token}"

# DPO's own sandbox test cards (docs.dpopay.com/.../sandbox-test-credentials) —
# not secrets, published for anyone integrating to test with. Listed here so
# whoever configures gateway_sandbox knows what to actually type at checkout;
# not used by any code path.
SANDBOX_TEST_CARDS = {
    "Mastercard": "5436 8862 6984 8367",
    "Visa": "4012 8888 8888 1881",
    "American Express": "3456 7890 1234 564",
}

# Result codes verified against docs.dpopay.com/.../verify-token-response-codes.
PAID_CODE = "000"
PENDING_CODES = {"001", "003", "005", "007", "900"}
FAILED_CODES = {"901", "902", "903", "904"}


def is_configured(company_id):
    settings = CompanySettings.query.get(company_id)
    if not settings:
        return False
    if settings.gateway_provider == "demo":
        return True
    return bool(settings.gateway_provider == "dpo" and settings.gateway_company_token)


def _company(company_id):
    return CompanySettings.query.get(company_id)


def _post(xml_body):
    """Returns (ok, parsed_dict_or_None). ok=False covers both a network/HTTP
    failure and a well-formed error response from DPO — callers only need to
    know whether they got usable data back."""
    try:
        resp = requests.post(
            API_BASE, data=xml_body.encode("utf-8"),
            headers={"Content-Type": "application/xml", "Accept": "application/xml"},
            timeout=20,
        )
    except requests.exceptions.RequestException:
        return False, None
    if resp.status_code != 200:
        return False, None
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return False, None
    data = {child.tag: (child.text or "") for child in root}
    return True, data


def create_payment_token(invoice, return_url, back_url):
    """Starts a payment attempt for the given invoice's current balance due.
    Mutates invoice.gateway_trans_token / gateway_status in place — caller
    commits. Returns (checkout_url_or_None, error_message_or_None)."""
    settings = _company(invoice.company_id)
    if not is_configured(invoice.company_id):
        return None, "Online payments aren't set up for this business yet."

    amount = invoice.balance_due
    if amount <= 0:
        return None, "This invoice has nothing left to pay."

    if settings.gateway_provider == "demo":
        # No real gateway involved — just a locally-hosted fake checkout page
        # (app/portal.py's demo_checkout route) so the portal flow can be seen
        # end-to-end without a DPO merchant account. Outcome is decided there,
        # not here; verify_payment() below reads it back off the trans_token.
        invoice.gateway_trans_token = f"DEMO-PENDING-{uuid.uuid4().hex[:12]}"
        invoice.gateway_status = "pending"
        return url_for("portal.demo_checkout", invoice_id=invoice.id, _external=True), None

    service_type = settings.gateway_service_type or ""
    now = datetime.utcnow().strftime("%Y/%m/%d %H:%M")

    root = ET.Element("API3G")
    ET.SubElement(root, "CompanyToken").text = settings.gateway_company_token
    ET.SubElement(root, "Request").text = "createToken"
    txn = ET.SubElement(root, "Transaction")
    ET.SubElement(txn, "PaymentAmount").text = f"{Decimal(str(amount)):.2f}"
    ET.SubElement(txn, "PaymentCurrency").text = invoice.currency
    ET.SubElement(txn, "CompanyRef").text = invoice.invoice_no
    ET.SubElement(txn, "RedirectURL").text = return_url
    ET.SubElement(txn, "BackURL").text = back_url
    ET.SubElement(txn, "CompanyRefUnique").text = "0"  # invoice_no can be reused across payment attempts (retries)
    ET.SubElement(txn, "PTL").text = "5"  # payment time limit, hours — DPO's own example value
    services = ET.SubElement(root, "Services")
    service = ET.SubElement(services, "Service")
    ET.SubElement(service, "ServiceType").text = service_type
    ET.SubElement(service, "ServiceDescription").text = f"Invoice {invoice.invoice_no}"
    ET.SubElement(service, "ServiceDate").text = now

    xml_body = '<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(root, encoding="unicode")
    ok, data = _post(xml_body)
    # createToken's own success code is "000" ("Transaction created") — the same
    # value verifyToken uses for "paid", but a different meaning; both are just
    # DPO's generic "all good" code for whichever operation was requested.
    if not ok or not data or data.get("Result") != "000":
        invoice.gateway_status = "failed"
        explanation = (data or {}).get("ResultExplanation") if data else None
        return None, explanation or "Could not start the payment — please try again or contact us."

    trans_token = data.get("TransToken")
    if not trans_token:
        invoice.gateway_status = "failed"
        return None, "Payment gateway didn't return a valid session — please try again."

    invoice.gateway_trans_token = trans_token
    invoice.gateway_status = "pending"
    return CHECKOUT_URL.format(trans_token=trans_token), None


def verify_payment(invoice):
    """Re-checks the real payment status server-to-server (never trust the
    redirect alone — see module docstring). Mutates invoice.gateway_status in
    place — caller commits. Returns (status, explanation) where status is one
    of "paid" / "pending" / "failed" / "error" (error = couldn't reach DPO or
    no attempt on file at all)."""
    settings = _company(invoice.company_id)
    if not invoice.gateway_trans_token or not is_configured(invoice.company_id):
        return "error", "No payment attempt on file for this invoice."

    if settings.gateway_provider == "demo":
        # The demo checkout page (app/portal.py) already decided the outcome
        # and encoded it into the trans_token — nothing to call out to.
        token = invoice.gateway_trans_token
        if token.startswith("DEMO-SUCCESS"):
            invoice.gateway_status = "paid"
            return "paid", "Demo payment simulated as successful."
        if token.startswith("DEMO-FAIL"):
            invoice.gateway_status = "failed"
            return "failed", "Demo payment simulated as failed."
        invoice.gateway_status = "pending"
        return "pending", "Waiting for the demo checkout page to be completed."

    root = ET.Element("API3G")
    ET.SubElement(root, "CompanyToken").text = settings.gateway_company_token
    ET.SubElement(root, "Request").text = "verifyToken"
    ET.SubElement(root, "TransactionToken").text = invoice.gateway_trans_token
    xml_body = '<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(root, encoding="unicode")

    ok, data = _post(xml_body)
    if not ok or not data:
        return "error", "Could not reach the payment gateway to confirm payment status."

    result = data.get("Result", "")
    explanation = data.get("ResultExplanation", "")
    if result == PAID_CODE:
        invoice.gateway_status = "paid"
        return "paid", explanation
    if result in FAILED_CODES:
        invoice.gateway_status = "failed"
        return "failed", explanation
    invoice.gateway_status = "pending"
    return "pending", explanation
