"""Receipt OCR — calls OCR.space's free hosted API (https://ocr.space/ocrapi) to turn
a photographed/scanned receipt into plain text, then a few regex heuristics guess at
the date, total amount, and vendor name. This is genuinely working today (unlike a
scaffold) using OCR.space's public "helloworld" demo key, but that key is shared by
every developer testing OCR.space worldwide and is rate-limited — fine for trying this
feature out, not for real daily use. A free OCR.space account (no card required) gives
a private key with a real quota; paste it into Settings → Company → Receipt OCR once
you have one (CompanySettings.ocr_api_key).

The parsing is intentionally simple pattern-matching, not real document understanding —
exactly like app/ai_suggest.py's bank-line categorizer, it's an honest, early best-effort
guess. Every field it fills stays a normal editable form field; nothing here is trusted
as fact.
"""
import re
from datetime import datetime

import requests

OCR_SPACE_URL = "https://api.ocr.space/parse/image"
DEMO_API_KEY = "helloworld"  # OCR.space's public, rate-limited testing key

DATE_PATTERNS = [
    (r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", "%Y-%m-%d"),
    (r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", "%d/%m/%Y"),
    (r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b", "%d-%m-%Y"),
]

AMOUNT_LINE_KEYWORDS = ("total", "amount due", "grand total", "balance due", "amount")
AMOUNT_PATTERN = re.compile(r"(\d{1,3}(?:[,.]\d{3})*(?:[.,]\d{2})|\d+[.,]\d{2})")


def extract_text(image_bytes, filename, api_key=None):
    """Uploads the image to OCR.space and returns the extracted plain text, or raises
    an exception with a human-readable message on any failure (network, bad key,
    unsupported file, OCR.space itself reporting an error) — the caller decides how
    to surface that (this module never silently returns an empty/fake result)."""
    key = (api_key or "").strip() or DEMO_API_KEY
    try:
        response = requests.post(
            OCR_SPACE_URL,
            files={"file": (filename or "receipt.jpg", image_bytes)},
            data={"apikey": key, "OCREngine": 2, "scale": True, "isTable": True},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Couldn't reach OCR.space: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError(f"OCR.space returned an unreadable response: {exc}") from exc

    if data.get("IsErroredOnProcessing"):
        message = data.get("ErrorMessage") or data.get("ErrorDetails") or "Unknown OCR error."
        if isinstance(message, list):
            message = "; ".join(message)
        raise RuntimeError(f"OCR.space couldn't process this image: {message}")

    results = data.get("ParsedResults") or []
    if not results:
        raise RuntimeError("OCR.space returned no text for this image.")
    return results[0].get("ParsedText") or ""


def parse_receipt_fields(text):
    """Best-effort guesses from raw OCR text — a date, a total amount, and a vendor
    name candidate (the first non-empty line, which is where a receipt's letterhead
    almost always sits). Returns a dict with any/all of these possibly None; never
    raises on unparseable text, just returns fewer fields."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    guessed_date = None
    for pattern, fmt in DATE_PATTERNS:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            if fmt == "%Y-%m-%d":
                guessed_date = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date()
            else:
                day, month, year = match.group(1), match.group(2), match.group(3)
                guessed_date = datetime(int(year), int(month), int(day)).date()
            break
        except ValueError:
            continue  # e.g. "13/13/2026" matched the pattern but isn't a real date — try the next one

    guessed_amount = None
    candidates = []
    for line in lines:
        lower = line.lower()
        amounts_in_line = AMOUNT_PATTERN.findall(line)
        if not amounts_in_line:
            continue
        weight = 2 if any(k in lower for k in AMOUNT_LINE_KEYWORDS) else 1
        for raw in amounts_in_line:
            try:
                value = float(raw.replace(",", ""))
            except ValueError:
                continue
            candidates.append((weight, value))
    if candidates:
        # Prefer amounts on a "total"-labelled line; among those (or if none), the largest
        # number on a receipt is almost always the grand total, not a line-item or a quantity.
        best_weight = max(c[0] for c in candidates)
        guessed_amount = max(v for w, v in candidates if w == best_weight)

    guessed_vendor = lines[0] if lines else None

    return {
        "date": guessed_date.isoformat() if guessed_date else None,
        "amount": guessed_amount,
        "vendor_name": guessed_vendor,
        "raw_text": text,
    }
