import os
import shutil
import subprocess
import tempfile
from datetime import datetime

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required

from app.audit import log_audit
from app.auth import owner_required

backup_bp = Blueprint("backup", __name__, url_prefix="/backup")

KNOWN_MYSQL_BIN_DIRS = [r"C:\Program Files\MySQL\MySQL Server 8.0\bin"]


def _find_tool(name):
    """Locate mysqldump.exe / mysql.exe: PATH first, then the standard Windows install location."""
    found = shutil.which(name)
    if found:
        return found
    for bin_dir in KNOWN_MYSQL_BIN_DIRS:
        candidate = os.path.join(bin_dir, f"{name}.exe")
        if os.path.exists(candidate):
            return candidate
    return None


def _db_config():
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "user": os.environ.get("DB_USER", "root"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "name": os.environ.get("DB_NAME", "quickbooks_clone"),
    }


@backup_bp.route("")
@login_required
@owner_required
def backup_home():
    mysqldump_available = _find_tool("mysqldump") is not None
    mysql_available = _find_tool("mysql") is not None
    return render_template("backup/home.html", mysqldump_available=mysqldump_available, mysql_available=mysql_available)


@backup_bp.route("/download")
@login_required
@owner_required
def download_backup():
    mysqldump = _find_tool("mysqldump")
    if not mysqldump:
        flash("mysqldump.exe wasn't found on this machine — can't generate a backup.", "error")
        return redirect(url_for("backup.backup_home"))

    cfg = _db_config()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"ledgerbooks_backup_{timestamp}.sql"
    out_path = os.path.join(tempfile.gettempdir(), filename)

    cmd = [mysqldump, f"-h{cfg['host']}", f"-u{cfg['user']}"]
    if cfg["password"]:
        cmd.append(f"-p{cfg['password']}")
    cmd += ["--routines", "--triggers", "--single-transaction", cfg["name"]]

    with open(out_path, "wb") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)

    if result.returncode != 0:
        os.remove(out_path)
        flash(f"Backup failed: {result.stderr.decode(errors='replace')[:300]}", "error")
        return redirect(url_for("backup.backup_home"))

    log_audit("backup", "database", None, f"Downloaded database backup ({filename})")
    from app import db
    db.session.commit()  # log_audit doesn't commit itself — this is a read-only route otherwise

    return send_file(out_path, as_attachment=True, download_name=filename, mimetype="application/sql")


@backup_bp.route("/restore", methods=["POST"])
@login_required
@owner_required
def restore_backup():
    if request.form.get("confirm_text") != "RESTORE":
        flash('You must type RESTORE exactly to confirm — this replaces ALL current data.', "error")
        return redirect(url_for("backup.backup_home"))

    file = request.files.get("backup_file")
    if not file or not file.filename:
        flash("Choose a .sql backup file first.", "error")
        return redirect(url_for("backup.backup_home"))
    if not file.filename.lower().endswith(".sql"):
        flash("That doesn't look like a .sql file.", "error")
        return redirect(url_for("backup.backup_home"))

    mysql_cli = _find_tool("mysql")
    if not mysql_cli:
        flash("mysql.exe wasn't found on this machine — can't restore.", "error")
        return redirect(url_for("backup.backup_home"))

    cfg = _db_config()
    tmp_path = os.path.join(tempfile.gettempdir(), f"restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql")
    file.save(tmp_path)

    cmd = [mysql_cli, f"-h{cfg['host']}", f"-u{cfg['user']}"]
    if cfg["password"]:
        cmd.append(f"-p{cfg['password']}")
    cmd.append(cfg["name"])

    with open(tmp_path, "rb") as f:
        result = subprocess.run(cmd, stdin=f, stderr=subprocess.PIPE)
    os.remove(tmp_path)

    if result.returncode != 0:
        flash(f"Restore failed: {result.stderr.decode(errors='replace')[:300]}", "error")
        return redirect(url_for("backup.backup_home"))

    # Deliberately no log_audit here — the audit_logs table (along with everything else) was
    # just replaced by the restored dump, so logging into the pre-restore session is meaningless.
    flash("Database restored successfully. Please log out and back in.", "success")
    return redirect(url_for("backup.backup_home"))
