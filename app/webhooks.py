"""Outgoing webhooks: notify a URL of the company's choosing when something happens.

fire_webhook() is called synchronously, right after the triggering action commits —
there's no background job queue in this app (see app/scheduler.py's per-company-tick
pattern for the one bit of async infrastructure that does exist, which runs on a timer,
not per-event), so a webhook fires inline with a short timeout and swallows every
error. A slow or broken receiving endpoint must never be able to break invoice/bill
creation for the person using the app — at worst their webhook just doesn't fire, and
that's recorded on the Webhook row (last_status) so it's visible from the UI.

Event types fired today: "invoice.created", "bill.created", "payment.received".
Adding a new one is just calling fire_webhook(company_id, "the.new.event", payload)
from wherever that event actually happens.
"""
import hashlib
import hmac
import json

import requests
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required
from datetime import datetime

from app import db
from app.audit import log_audit
from app.auth import current_company_id, owner_required
from app.models import Webhook
from app.scoping import scoped_or_404, scoped_query

webhooks_bp = Blueprint("webhooks", __name__, url_prefix="/settings/webhooks")

EVENT_TYPES = ["invoice.created", "bill.created", "payment.received"]
WEBHOOK_TIMEOUT_SECONDS = 3


def fire_webhook(company_id, event_type, payload):
    hooks = Webhook.query.filter_by(company_id=company_id, is_active=True).all()
    for hook in hooks:
        if not hook.subscribes_to(event_type):
            continue
        body = json.dumps({"event": event_type, "data": payload}).encode("utf-8")
        signature = hmac.new(hook.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        try:
            resp = requests.post(
                hook.url, data=body, timeout=WEBHOOK_TIMEOUT_SECONDS,
                headers={
                    "Content-Type": "application/json",
                    "X-LedgerBooks-Event": event_type,
                    "X-LedgerBooks-Signature": f"sha256={signature}",
                },
            )
            hook.last_status = f"{resp.status_code} {resp.reason}"
        except Exception as exc:
            hook.last_status = f"error: {exc}"
        hook.last_triggered_at = datetime.utcnow()
    if hooks:
        db.session.commit()


@webhooks_bp.route("")
@login_required
@owner_required
def webhook_list():
    hooks = scoped_query(Webhook).order_by(Webhook.id.desc()).all()
    return render_template("settings/webhook_list.html", hooks=hooks, event_types=EVENT_TYPES)


@webhooks_bp.route("/new", methods=["GET", "POST"])
@login_required
@owner_required
def webhook_new():
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        events = request.form.getlist("event_types")
        if not url or not events:
            flash("A URL and at least one event type are required.", "error")
            return render_template("settings/webhook_form.html", event_types=EVENT_TYPES, form=request.form)
        import secrets as secrets_module
        hook = Webhook(
            company_id=current_company_id(), url=url, secret=secrets_module.token_hex(24),
            event_types=",".join(events),
        )
        db.session.add(hook)
        db.session.flush()
        log_audit("create", "webhook", hook.id, f"Webhook created for {url}")
        db.session.commit()
        flash("Webhook created.", "success")
        return redirect(url_for("webhooks.webhook_list"))

    return render_template("settings/webhook_form.html", event_types=EVENT_TYPES, form={})


@webhooks_bp.route("/<int:webhook_id>/toggle", methods=["POST"])
@login_required
@owner_required
def webhook_toggle(webhook_id):
    hook = scoped_or_404(Webhook, webhook_id)
    hook.is_active = not hook.is_active
    db.session.commit()
    return redirect(url_for("webhooks.webhook_list"))


@webhooks_bp.route("/<int:webhook_id>/delete", methods=["POST"])
@login_required
@owner_required
def webhook_delete(webhook_id):
    hook = scoped_or_404(Webhook, webhook_id)
    db.session.delete(hook)
    db.session.commit()
    flash("Webhook deleted.", "success")
    return redirect(url_for("webhooks.webhook_list"))
