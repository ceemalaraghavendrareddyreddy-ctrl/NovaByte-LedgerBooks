"""Signed, no-login share links for sending a document (invoice/bill/credit
memo) to WhatsApp — or anywhere else outside the app. No database table:
the token itself carries and verifies (doc_type, doc_id, company_id), signed
with the app's SECRET_KEY, so it can't be forged or repointed at a different
company's document just by guessing an id.

Deliberately time-limited (default 45 days — long enough to outlast a normal
AR/AP cycle, short enough that a link posted somewhere public eventually goes
cold) rather than permanent, since anyone with the link can view that one
document without logging in.
"""
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from flask import current_app, url_for

SHARE_LINK_MAX_AGE_SECONDS = 45 * 24 * 60 * 60  # 45 days


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="doc-share-link")


def make_share_token(doc_type, doc_id, company_id):
    return _serializer().dumps({"t": doc_type, "id": doc_id, "c": company_id})


def verify_share_token(token, expected_doc_type):
    """Returns (doc_id, company_id) if the token is valid, unexpired, and for the
    expected document type; otherwise None. Never raises — a bad/expired/tampered
    link should read as "not found", not a 500."""
    try:
        data = _serializer().loads(token, max_age=SHARE_LINK_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or data.get("t") != expected_doc_type:
        return None
    try:
        return int(data["id"]), int(data["c"])
    except (KeyError, TypeError, ValueError):
        return None


def share_url(doc_type, doc_id, company_id, endpoint):
    """Builds the full external (_external=True) URL for a share route, e.g.
    share_url("invoice", inv.id, inv.company_id, "share.invoice_pdf")."""
    token = make_share_token(doc_type, doc_id, company_id)
    return url_for(endpoint, doc_id=doc_id, token=token, _external=True)


def whatsapp_link(phone, message):
    """Builds a wa.me link. With a phone number, opens a chat with that contact
    pre-filled; without one (missing, or too mangled to trust), falls back to
    WhatsApp's own contact picker (wa.me/?text=...) rather than guessing wrong
    and silently messaging the wrong person."""
    import urllib.parse
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    text = urllib.parse.quote(message)
    if len(digits) >= 7:  # short enough to reject obviously-incomplete numbers, not so strict it rejects real ones
        return f"https://wa.me/{digits}?text={text}"
    return f"https://wa.me/?text={text}"
