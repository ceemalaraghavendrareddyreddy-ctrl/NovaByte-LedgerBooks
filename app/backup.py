"""Backup & Restore — pure Python, no external command-line tools.

The previous version shelled out to mysqldump.exe/mysql.exe, which only ever
worked with a local MySQL install on PATH. That breaks two ways: it's simply
not installed on most machines (the exact error that prompted this rewrite),
and it can never work at all against the live Render deployment, which runs
Postgres and has no Shell access to run any command-line tool in the first
place. This version instead reads/writes every table directly through
SQLAlchemy, so the exact same code backs up and restores either database
engine — matching how the rest of this app was made portable (build_database_uri,
the _sync_missing_columns startup check).

Format: gzip-compressed JSON. One dict of {table_name: [row_dict, ...]}, with
dates/datetimes/Decimals converted to strings on the way out and converted
back using each column's real type on the way in — not just naive strings.
"""
import gzip
import io
import json
from datetime import date, datetime
from decimal import Decimal

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required
from sqlalchemy import DateTime as SA_DateTime
from sqlalchemy import Date as SA_Date
from sqlalchemy import Numeric as SA_Numeric

from app import db
from app.audit import log_audit
from app.auth import owner_required

backup_bp = Blueprint("backup", __name__, url_prefix="/backup")

BACKUP_FORMAT_VERSION = 1


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _export_all_tables():
    """Returns {table_name: [row_dict, ...]} for every table, in FK-dependency
    order (parents before children) — the same order they must be restored in."""
    data = {}
    for table in db.metadata.sorted_tables:
        with db.engine.connect() as conn:
            rows = conn.execute(table.select()).mappings().all()
        data[table.name] = [dict(row) for row in rows]
    return data


def _coerce_value(column, value):
    """Turns a JSON-decoded value (string/int/float/bool/None) back into whatever
    Python type this column actually needs, using the column's own SQLAlchemy
    type rather than guessing — a Numeric column gets a Decimal, a DateTime
    column gets an actual datetime, everything else passes through unchanged."""
    if value is None:
        return None
    col_type = column.type
    try:
        if isinstance(col_type, SA_DateTime):
            return datetime.fromisoformat(value) if isinstance(value, str) else value
        if isinstance(col_type, SA_Date):
            return date.fromisoformat(value) if isinstance(value, str) else value
        if isinstance(col_type, SA_Numeric):
            return Decimal(str(value))
    except (ValueError, TypeError):
        # If the stored value doesn't actually look like what the column expects
        # (a corrupted/hand-edited backup file), pass it through as-is and let the
        # database's own INSERT raise a clear error rather than silently guessing.
        pass
    return value


def _restore_all_tables(data):
    """Wipes and replaces every table's contents in one transaction — either the
    whole restore lands, or (on any error) none of it does, so a failure partway
    through never leaves the database half-replaced."""
    tables_by_name = {t.name: t for t in db.metadata.sorted_tables}
    with db.engine.begin() as conn:
        # Children before parents, so deleting doesn't trip a foreign key still
        # pointing at a row in a table we haven't cleared yet.
        for table in reversed(db.metadata.sorted_tables):
            conn.execute(table.delete())
        # Parents before children, for the same reason in reverse on the way back in.
        for table in db.metadata.sorted_tables:
            rows = data.get(table.name, [])
            if not rows:
                continue
            coerced_rows = [
                {col.name: _coerce_value(col, row.get(col.name)) for col in table.columns}
                for row in rows
            ]
            conn.execute(table.insert(), coerced_rows)


@backup_bp.route("")
@login_required
@owner_required
def backup_home():
    return render_template("backup/home.html")


@backup_bp.route("/download")
@login_required
@owner_required
def download_backup():
    payload = {
        "format_version": BACKUP_FORMAT_VERSION,
        "exported_at": datetime.utcnow().isoformat(),
        "dialect": db.engine.dialect.name,
        "tables": _export_all_tables(),
    }
    raw = json.dumps(payload, default=_json_default).encode("utf-8")
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as gz:
        gz.write(raw)
    buffer.seek(0)

    filename = f"ledgerbooks_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json.gz"
    log_audit("backup", "database", None, f"Downloaded database backup ({filename})")
    db.session.commit()  # log_audit doesn't commit itself — this route is otherwise read-only

    return send_file(buffer, as_attachment=True, download_name=filename, mimetype="application/gzip")


@backup_bp.route("/restore", methods=["POST"])
@login_required
@owner_required
def restore_backup():
    if request.form.get("confirm_text") != "RESTORE":
        flash('You must type RESTORE exactly to confirm — this replaces ALL current data.', "error")
        return redirect(url_for("backup.backup_home"))

    file = request.files.get("backup_file")
    if not file or not file.filename:
        flash("Choose a backup file first.", "error")
        return redirect(url_for("backup.backup_home"))
    if not file.filename.lower().endswith((".json.gz", ".gz")):
        flash("That doesn't look like a LedgerBooks backup file (expected .json.gz).", "error")
        return redirect(url_for("backup.backup_home"))

    try:
        raw = gzip.decompress(file.read())
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        flash(f"Couldn't read that file — it may be corrupted or not a real backup: {exc}", "error")
        return redirect(url_for("backup.backup_home"))

    if payload.get("format_version") != BACKUP_FORMAT_VERSION:
        flash(f"This backup's format (v{payload.get('format_version')}) isn't one this version of "
              f"LedgerBooks knows how to restore.", "error")
        return redirect(url_for("backup.backup_home"))

    try:
        _restore_all_tables(payload.get("tables", {}))
    except Exception as exc:
        flash(f"Restore failed and was rolled back — no data was changed: {exc}", "error")
        return redirect(url_for("backup.backup_home"))

    # Deliberately no log_audit here — the audit_logs table (along with everything else) was
    # just replaced by the restored backup, so logging into the pre-restore session is meaningless.
    flash("Database restored successfully. Please log out and back in.", "success")
    return redirect(url_for("backup.backup_home"))
