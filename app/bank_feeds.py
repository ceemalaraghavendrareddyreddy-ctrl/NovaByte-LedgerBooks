"""Live bank feeds.

Plaid is a real, working integration (see app/plaid_client.py) — Sandbox needs only
a free developer signup, no company/KYC, and pulls genuine (if fake) transaction
data through the exact same code path a real bank connection would use later.
Salt Edge and TrueLayer stay honest scaffolding: nobody has confirmed either one
actually covers a Mauritius bank yet, and wiring up a second provider before that's
settled would be guessing at a business decision that isn't code's to make.

  - CompanySettings.bank_feed_provider / bank_feed_client_id / bank_feed_api_key hold
    which provider this company uses and its credentials (Settings → Company → Bank Feeds).
  - Account.bank_feed_access_token / bank_feed_external_account_id / bank_feed_sync_cursor
    hold this local account's Plaid connection (Banking → Bank Feeds).
  - sync_account() below fetches new transactions and drops them into the same
    BankStatementImport/BankImportLine pipeline app/banking.py's CSV import already
    uses — the review/matching/categorisation screen doesn't care where a line came from.
"""
from datetime import datetime

from app import db
from app.models import BankImportLine, BankStatementImport
from app.plaid_client import PlaidError, sync_transactions

PROVIDERS = ["plaid", "salt_edge", "truelayer"]


def is_configured(company):
    if not company or company.bank_feed_provider != "plaid":
        return bool(company and company.bank_feed_provider and company.bank_feed_api_key)
    return bool(company.bank_feed_provider and company.bank_feed_client_id and company.bank_feed_api_key)


def sync_account(account, company):
    """Fetches new transactions for `account` from its linked provider account and
    creates a BankStatementImport batch from them, exactly like a CSV upload would —
    from there it's reviewed at Banking → Import Statement like any other import.
    """
    if not is_configured(company):
        return {"status": "not_configured", "message": "Bank feeds aren't set up yet — add a provider and API key in Settings → Company first."}
    if not account.bank_feed_external_account_id or not account.bank_feed_access_token:
        return {"status": "not_linked", "message": f"{account.name} isn't linked to a {company.bank_feed_provider} account yet."}

    if company.bank_feed_provider != "plaid":
        return {
            "status": "not_implemented",
            "message": (
                f"{company.bank_feed_provider} is configured, but the actual sync isn't built yet — "
                f"this is scaffolding for when that integration is implemented. Use Import Statement (CSV) for now."
            ),
        }

    try:
        all_added = []
        cursor = account.bank_feed_sync_cursor
        has_more = True
        while has_more:
            added, _modified, _removed, cursor, has_more = sync_transactions(company, account.bank_feed_access_token, cursor)
            all_added.extend(added)
    except PlaidError as exc:
        return {"status": "error", "message": f"Plaid sync failed: {exc}"}

    account.bank_feed_sync_cursor = cursor
    db.session.commit()

    # Only transactions on the specific Plaid account this local account is linked to —
    # an Item can cover several of the bank's accounts (checking + savings, say), and
    # a mismatch here would silently mix another account's transactions into this one.
    own_transactions = [t for t in all_added if t.get("account_id") == account.bank_feed_external_account_id]
    if not own_transactions:
        return {"status": "ok", "message": "No new transactions since the last sync.", "import_id": None}

    batch = BankStatementImport(
        company_id=company.id, account_id=account.id,
        filename=f"Plaid sync — {datetime.utcnow().strftime('%d %b %Y %H:%M')}",
    )
    db.session.add(batch)
    db.session.flush()

    # Deliberately doesn't run the CSV import's same-day-amount auto-match against
    # uncleared journal lines — every synced line lands as "unmatched" and gets
    # reviewed like a fresh import. Wiring that in is a reasonable follow-up, not
    # a correctness issue: nothing here is ever silently posted without review.
    for txn in own_transactions:
        # Plaid's sign convention is the OPPOSITE of this app's statement convention:
        # Plaid reports a positive amount for money OUT and negative for money IN.
        amount = -float(txn["amount"])
        stmt_date = datetime.strptime(txn.get("date") or txn.get("authorized_date"), "%Y-%m-%d").date()
        db.session.add(BankImportLine(
            import_batch=batch, stmt_date=stmt_date,
            description=txn.get("merchant_name") or txn.get("name") or "",
            amount=amount, status="unmatched",
        ))

    db.session.commit()
    return {
        "status": "ok",
        "message": f"Imported {len(own_transactions)} transaction(s) from {company.bank_feed_provider}.",
        "import_id": batch.id,
    }
