"""Smart category suggestions for bank statement import review.

No external API, no per-use cost — every suggestion is derived from the
company's OWN data. It is honest about being a best-effort suggestion, never
authoritative: the category dropdown it feeds stays fully editable regardless.

Three layers, in priority order, each more useful the more a company actually
uses Import Statement / bank feeds:

  1. User-defined Bank Rules (app/bank_rules.py) — an explicit "if the
     description contains X, use account Y" rule the user set up themselves.
     Always wins, since the user said so directly.
  2. History — a self-improving classifier over the company's own past
     resolved transactions on this bank account (every CSV import AND Plaid
     feed line flows through BankImportLine, so both teach it). Scores
     candidate accounts by how OFTEN and how RECENTLY similar descriptions
     were categorised each way, weighting rare/distinctive words over common
     ones — so it resists a single miscategorisation and ignores noise words
     like "payment"/"transfer". See _history_match.
  3. Keyword rules — a small dictionary of common expense terms, as a
     sensible default before any history exists.
"""
import math
import re
from collections import defaultdict

from app.models import Account, BankImportLine, BankRule, BankStatementImport

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

# Ultra-common bank-statement noise words carry no categorisation signal on
# their own — matching on these alone is what made the old history layer fire
# false suggestions. IDF weighting already down-ranks frequent words; this just
# drops the universal ones outright so they never anchor a match.
_STOPWORDS = frozenset({
    "payment", "payments", "transfer", "transfers", "ref", "reference", "transaction",
    "txn", "trx", "purchase", "pos", "card", "debit", "credit", "from", "the", "and",
    "for", "via", "to", "of", "on", "at", "by", "ltd", "limited", "co", "inc", "pvt",
    "mur", "rs", "no", "acc", "account", "online", "mobile", "app", "bank", "fee",
})

# A candidate account needs at least this combined score (summed rare-token
# weights across matched past transactions, recency-decayed) before we surface
# it — below this the "match" is too thin to be worth suggesting.
_MIN_SCORE = 1.0


def _tokens(text):
    """Meaningful lowercase word tokens: length >= 3, not a stopword."""
    return [t for t in re.findall(r"[a-z]+", (text or "").lower()) if len(t) >= 3 and t not in _STOPWORDS]


def _history_match(description, bank_account_id, company_id):
    """Self-improving classifier over past resolved transactions on this bank
    account. Returns (Account, detail_str) — detail describes the confidence
    (e.g. "8 of 10 similar past transactions") — or (None, None).

    Method: gather the labelled corpus (each past resolved line is a
    description -> categorised-account example), weight each token by inverse
    document frequency so distinctive words (a vendor name) count far more than
    common ones, and score each candidate account by the recency-decayed sum of
    shared-token weights. The best-scoring account wins if it clears a floor."""
    query_tokens = set(_tokens(description))
    if not query_tokens:
        return None, None

    past_lines = (
        BankImportLine.query.join(BankStatementImport)
        .filter(
            BankStatementImport.company_id == company_id,
            BankStatementImport.account_id == bank_account_id,
            BankImportLine.status == "created",
            BankImportLine.created_journal_entry_id.isnot(None),
        )
        .order_by(BankImportLine.id.desc())
        .limit(400)
        .all()
    )
    if not past_lines:
        return None, None

    # Resolve each past line to (its meaningful token set, the non-bank account
    # it was categorised to). Skip ambiguous entries whose non-bank leg isn't a
    # single clear account.
    examples = []  # list of (token_set, account) newest-first
    doc_freq = defaultdict(int)
    for past in past_lines:
        entry = past.created_journal_entry
        if not entry:
            continue
        other_accounts = [ln.account for ln in entry.lines if ln.account_id != bank_account_id and ln.account]
        if len(other_accounts) != 1:
            continue  # a split/multi-leg entry is not a clean single-category example
        toks = set(_tokens(past.description))
        if not toks:
            continue
        examples.append((toks, other_accounts[0]))
        for t in toks:
            doc_freq[t] += 1

    if not examples:
        return None, None

    n_docs = len(examples)
    idf = {t: math.log((n_docs + 1) / (df + 1)) + 1.0 for t, df in doc_freq.items()}

    # Score candidate accounts. Recency decays with position (newest examples
    # weigh most), so a category the company has moved away from fades out.
    scores = defaultdict(float)
    match_counts = defaultdict(int)
    total_matches = 0
    for rank, (toks, account) in enumerate(examples):
        if not account.is_active:
            continue
        shared = query_tokens & toks
        if not shared:
            continue
        recency = 1.0 / (1.0 + rank / 50.0)
        contribution = sum(idf.get(t, 1.0) for t in shared) * recency
        scores[account.id] += contribution
        match_counts[account.id] += 1
        total_matches += 1

    if not scores:
        return None, None

    best_account_id = max(scores, key=scores.get)
    if scores[best_account_id] < _MIN_SCORE:
        return None, None

    account = Account.query.get(best_account_id)
    if not account or not account.is_active:
        return None, None

    won = match_counts[best_account_id]
    detail = f"{won} of {total_matches} similar past transaction{'s' if total_matches != 1 else ''}"
    return account, detail


def _rule_match(description, company_id):
    rules = BankRule.query.filter_by(company_id=company_id, is_active=True).order_by(BankRule.id).all()
    for rule in rules:
        if rule.matches(description) and rule.account.is_active:
            return rule.account
    return None


def suggest_category_account(description, bank_account_id, company_id):
    """Best-effort suggestion for which Chart of Accounts entry a bank
    statement line probably belongs to. Returns (Account, reason, detail) —
    reason is "rule", "history" or "keyword" so the UI can say honestly which
    it was, and detail is an optional short confidence string (history only) —
    or (None, None, None) if nothing matched. Always just a suggestion."""
    rule_account = _rule_match(description, company_id)
    if rule_account:
        return rule_account, "rule", None

    history_account, history_detail = _history_match(description, bank_account_id, company_id)
    if history_account:
        return history_account, "history", history_detail

    text = (description or "").lower()
    for keywords, code in KEYWORD_RULES:
        if any(k in text for k in keywords):
            account = Account.query.filter_by(company_id=company_id, code=code, is_active=True).first()
            if account:
                return account, "keyword", None
    return None, None, None
