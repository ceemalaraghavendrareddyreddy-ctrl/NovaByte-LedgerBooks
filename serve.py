"""Production entry point — runs the app behind Waitress instead of Flask's dev server.

Use this (not run.py) for actual day-to-day use: no auto-reloader, no debug traceback pages
leaking data, no "development server" warning, and no risk of leftover watcher processes
locking the port after a crash — all things that hit us with the dev server during testing.

Usage:
    venv\\Scripts\\python.exe serve.py
"""
import os

from waitress import serve

from app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5057))
    print(f"LedgerBooks running at http://localhost:{port}  (Ctrl+C to stop)")
    options = {}
    if os.environ.get("TRUST_PROXY"):
        # Behind a host's HTTPS proxy (Cloud Run, Render): believe its forwarded headers so
        # the app writes https:// links (payment returns, share links) instead of http://.
        options = dict(
            trusted_proxy="*",
            trusted_proxy_count=1,
            trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-forwarded-port"},
            clear_untrusted_proxy_headers=True,
        )
    serve(app, host="0.0.0.0", port=port, threads=4, **options)
