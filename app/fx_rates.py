"""Live exchange rate lookup — answers "where do we get today's rate from"
for the exchange_rate field on invoices, bills, and payments.

Source: exchangerate-api.com's open, keyless endpoint (updated once daily,
mid-market rates). This is a SUGGESTION only, fetched on demand when someone
clicks "Get today's rate" — it never runs automatically and never overwrites
what's already in the field without the user clicking. The field stays a
plain number input either way, so if MRA or the company's bank publishes a
different official rate, typing over the suggestion works exactly like it
always has. Nothing here changes how the rate is stored or posted — only
where the starting number can come from.
"""
import urllib.request
import json

from flask import Blueprint, jsonify, request
from flask_login import login_required

fx_rates_bp = Blueprint("fx_rates", __name__, url_prefix="/api/fx-rate")

FX_API_URL = "https://open.er-api.com/v6/latest/{base}"


@fx_rates_bp.route("")
@login_required
def get_rate():
    from_currency = (request.args.get("from") or "").strip().upper()
    to_currency = (request.args.get("to") or "").strip().upper()
    if not from_currency or not to_currency:
        return jsonify({"error": "Both 'from' and 'to' currency codes are required."}), 400
    if from_currency == to_currency:
        return jsonify({"rate": 1.0, "source": "same-currency"})

    try:
        with urllib.request.urlopen(FX_API_URL.format(base=from_currency), timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return jsonify({"error": f"Couldn't reach the rate source: {exc}. Enter the rate manually."}), 502

    rates = data.get("rates") or {}
    rate = rates.get(to_currency)
    if rate is None:
        return jsonify({"error": f"No published rate for {from_currency} → {to_currency}. Enter it manually."}), 404

    return jsonify({
        "rate": rate,
        "source": "exchangerate-api.com (mid-market, updated daily)",
        "as_of": data.get("time_last_update_utc"),
    })
