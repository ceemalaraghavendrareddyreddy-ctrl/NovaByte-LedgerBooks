"""Real Plaid API calls — plain HTTP via `requests`, same pattern as app/ocr.py and
app/fx_rates.py rather than pulling in the official plaid-python SDK, since these are
only a handful of endpoints.

Hardcoded to Plaid's SANDBOX environment. Sandbox needs only a free developer
signup (https://dashboard.plaid.com/signup) — no company/KYC required — and works
against Plaid's own fake test banks (e.g. "Platypus Bank", username `user_good`,
password `pass_good`). Switching a real company over to a live bank connection later
means: whoever owns the company applies for Plaid Production access under their own
name, and PLAID_BASE_URL below changes to https://production.plaid.com. Nothing else
about this integration changes — the endpoints and payloads are identical between
environments, only the base URL and whether the bank data is real.
"""
import requests

PLAID_BASE_URL = "https://sandbox.plaid.com"
REQUEST_TIMEOUT = 15


class PlaidError(Exception):
    """Raised with Plaid's own error_message when a call fails, so callers can show
    the real reason (expired token, bad credentials, rate limit, ...) instead of a
    generic failure."""


def _post(path, company, payload):
    body = {
        "client_id": company.bank_feed_client_id,
        "secret": company.bank_feed_api_key,
        **payload,
    }
    try:
        resp = requests.post(f"{PLAID_BASE_URL}{path}", json=body, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise PlaidError(f"Couldn't reach Plaid: {exc}") from exc

    data = resp.json() if resp.content else {}
    if resp.status_code >= 400:
        message = data.get("error_message") or data.get("error_code") or f"HTTP {resp.status_code}"
        raise PlaidError(message)
    return data


def create_link_token(company, user_id):
    """Starts a Plaid Link session. The link_token this returns is handed to the
    Plaid Link JS widget in the browser — the actual bank login happens entirely
    inside that widget, never on this server."""
    data = _post("/link/token/create", company, {
        "user": {"client_user_id": str(user_id)},
        "client_name": "LedgerBooks",
        "products": ["transactions"],
        "country_codes": ["US"],  # Sandbox's test banks are only registered under US
        "language": "en",
    })
    return data["link_token"]


def exchange_public_token(company, public_token):
    """Converts Link's short-lived public_token into a permanent access_token for
    this Item (one bank login connection, which may cover several accounts)."""
    data = _post("/item/public_token/exchange", company, {"public_token": public_token})
    return data["access_token"], data["item_id"]


def get_accounts(company, access_token):
    """Every account under this Item — checking, savings, credit card, etc."""
    data = _post("/accounts/get", company, {"access_token": access_token})
    return data["accounts"]


def sync_transactions(company, access_token, cursor):
    """One page of the /transactions/sync feed. Returns (added, modified, removed,
    next_cursor, has_more) — the caller loops while has_more is True, feeding each
    next_cursor back in, until it's caught up. cursor=None means "from the beginning".
    """
    payload = {"access_token": access_token}
    if cursor:
        payload["cursor"] = cursor
    data = _post("/transactions/sync", company, payload)
    return (
        data.get("added", []), data.get("modified", []), data.get("removed", []),
        data.get("next_cursor"), bool(data.get("has_more")),
    )
