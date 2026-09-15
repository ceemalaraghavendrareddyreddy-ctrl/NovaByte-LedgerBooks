"""Smart category suggestions for bank statement import review.

This is a deliberate MOCK — a lightweight, rule-and-history stand-in for
what would become a real AI model call once the client confirms they
actually want that (see the "AI model" request). Nothing here talks to any
external API, costs nothing per use, and is honest about what it is: a
best-effort suggestion, never authoritative, and the category dropdown it
feeds stays fully editable regardless of what it suggests.

Two layers, in priority order, so it also gets more useful the more a
company actually uses Import Statement — the thing a static keyword list
alone never does:

  1. History — has a transaction with a similar description been
     categorized before, on this same bank account? If so, suggest whatever
     account was used then.
  2. Keyword rules — a small dictionary of common expense terms, as a
     sensible default before any history exists.

Swapping in a real model later means replacing suggest_category_account's
body with an actual API call — every caller (banking.import_review) stays
exactly the same, since the contract (a description in, an Account or
None out) doesn't change.
"""
import re

from app.models import Account, BankImportLine, BankStatementImport

# (keywords to look for in the statement line's description, account code to suggest)
KEYWORD_RULES = [
    (("rent", "lease"), "6000"),
    (("electric", "water", "ceb", "cwa", "utility", "utilities"), "6100"),
    (("salary", "salaries", "wages", "payroll"), "6200"),
    (("stationery", "office supplies", "printing"), "6300"),
    (("fuel", "petrol", "diesel", "transport", "vehicle"), "6900"),
    (("bank charge", "bank fee", "commission", "service fee", "admin fee"), "6900"),
    (("insurance",), "6900"),
    (("interest", "refund", "reversal"), "4900"),
]


def _tokenize(text):
    return set(re.findall(r"[a-z]+", (text or "").lower()))


def _history_match(description, bank_account_id, company_id):
    """Looks at recently-resolved import lines on the same bank account for a
    description sharing at least one meaningful word, and returns whichever
    non-bank account was used to categorize the most recent one."""
    words = _tokenize(description)
    if not words:
        return None

    past_lines = (
        BankImportLine.query.join(BankStatementImport)
        .filter(
            BankStatementImport.company_id == company_id,
            BankStatementImport.account_id == bank_account_id,
            BankImportLine.status == "created",
            BankImportLine.created_journal_entry_id.isnot(None),
        )
        .order_by(BankImportLine.id.desc())
        .limit(200)
        .all()
    )
    for past in past_lines:
        if not (_tokenize(past.description) & words):
            continue
        entry = past.created_journal_entry
        if not entry:
            continue
        for line in entry.lines:
            if line.account_id != bank_account_id:  # skip the bank-account leg itself
                return line.account
    return None


def suggest_category_account(description, bank_account_id, company_id):
    """Best-effort suggestion for which Chart of Accounts entry a bank
    statement line probably belongs to. Returns (Account, reason) — reason
    is "history" or "keyword", so the UI can say honestly which one it was —
    or (None, None) if nothing matched. Always just a suggestion, never
    assumed to be correct."""
    history = _history_match(description, bank_account_id, company_id)
    if history and history.is_active:
        return history, "history"

    text = (description or "").lower()
    for keywords, code in KEYWORD_RULES:
        if any(k in text for k in keywords):
            account = Account.query.filter_by(company_id=company_id, code=code, is_active=True).first()
            if account:
                return account, "keyword"
    return None, None
