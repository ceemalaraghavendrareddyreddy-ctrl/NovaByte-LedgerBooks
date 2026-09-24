"""TOTP-based two-factor authentication for staff logins. Uses pyotp for the
standard RFC 6238 algorithm (compatible with Google Authenticator, Authy, 1Password,
etc.) and the qrcode library to render the enrollment QR code as a data: URI, so
nothing ever needs to be written to disk (same constraint as CompanySettings.logo_data
— Render's free tier has no persistent disk).
"""
import base64
import io
import secrets

import pyotp
import qrcode
from werkzeug.security import generate_password_hash

RECOVERY_CODE_COUNT = 8


def generate_secret():
    return pyotp.random_base32()


def provisioning_uri(user, secret):
    return pyotp.totp.TOTP(secret).provisioning_uri(name=user.username, issuer_name="LedgerBooks")


def qr_code_data_uri(uri):
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def verify_code(secret, code):
    if not secret or not code:
        return False
    return pyotp.totp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)


def generate_recovery_codes():
    """Returns (plain_codes, stored_value) — plain_codes are shown to the user once,
    stored_value is the comma-joined password hashes to save on the user row."""
    plain_codes = ["-".join([secrets.token_hex(2), secrets.token_hex(2)]) for _ in range(RECOVERY_CODE_COUNT)]
    stored_value = ",".join(generate_password_hash(code) for code in plain_codes)
    return plain_codes, stored_value
