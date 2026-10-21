# -*- coding: utf-8 -*-
"""
Lambda: Alert Checker (triggered by EventBridge every 5 minutes)
Checks for ALL locks that have been checked in longer than ALERT_HOURS.
Sends alerts to a dedicated Slack channel via Workflow Trigger webhook.

Anti-spam: uses loto_config (config_type="alert_tracker") to avoid
re-sending the same alert within 10 hours.

Category-aware alerts:
  - A / B locks → show "User: {login}"
  - C locks       → show "Contractor: {contractor_company}"

NOT triggered by API Gateway — runs exclusively on a schedule.
"""

import json
import os
import sys
import urllib.request
from urllib.error import URLError, HTTPError
from datetime import datetime, timezone

# ──────────────────────────────────────────────────
# Shared layer
# ──────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared", "python"))
from db_helpers import get_all_locks, get_lock_category, func_config_table

# ──────────────────────────────────────────────────
# Environment variables
# ──────────────────────────────────────────────────
ALERT_HOURS = float(os.environ.get("ALERT_HOURS", "10"))
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SITE_NAME = os.environ.get("SITE_NAME", "SITE-001")

# Anti-spam cooldown matches ALERT_HOURS (re-alert only after another full cycle)
COOLDOWN_HOURS = ALERT_HOURS


# ──────────────────────────────────────────────────
# Duration formatting
# ──────────────────────────────────────────────────
def _format_duration(total_hours: float) -> str:
    """Convert fractional hours to 'Xh Ym' string."""
    h = int(total_hours)
    m = int((total_hours - h) * 60)
    return f"{h}h {m:02d}m"


# ──────────────────────────────────────────────────
# Anti-spam helpers (loto_config table)
# ──────────────────────────────────────────────────
def _should_send_alert(lock_number: str, now: datetime) -> bool:
    """Return True if no alert was sent for this lock in the last COOLDOWN_HOURS."""
    try:
        resp = func_config_table().get_item(Key={
            "config_type": "alert_tracker",
            "config_key": lock_number,
        })
        item = resp.get("Item")
        if not item:
            return True

        last_alert = item.get("value", "")
        if not last_alert:
            return True

        last_dt = datetime.fromisoformat(last_alert)
        hours_since = (now - last_dt).total_seconds() / 3600
        return hours_since >= COOLDOWN_HOURS

    except Exception as e:
        print(f"[anti-spam] Error checking tracker for {lock_number}: {e}")
        return True  # On error, send the alert to be safe


def _record_alert_sent(lock_number: str, now: datetime):
    """Update the alert tracker with the current timestamp."""
    try:
        func_config_table().put_item(Item={
            "config_type": "alert_tracker",
            "config_key": lock_number,
            "value": now.isoformat(),
            "active": True,
        })
    except Exception as e:
        print(f"[anti-spam] Error recording alert for {lock_number}: {e}")


# ──────────────────────────────────────────────────
# Slack delivery
# ──────────────────────────────────────────────────
def _send_slack_alert(message: str) -> bool:
    """Send a message to Slack via Workflow Trigger webhook.
    Payload format: {"Content": "message"} — Slack Workflow Trigger format."""
    if not SLACK_WEBHOOK_URL:
        print(f"[slack] No SLACK_WEBHOOK_URL configured. Message:\n{message}")
        return False

    data = json.dumps({"Content": message}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                print("[slack] Alert sent successfully")
                return True
            print(f"[slack] Unexpected status {resp.status}")
            return False
    except (URLError, HTTPError) as e:
        print(f"[slack] Error sending alert: {e}")
        return False


# ──────────────────────────────────────────────────
# Alert message builder
# ──────────────────────────────────────────────────
def _build_alert_message(item: dict, hours_elapsed: float) -> str:
    """Build category-aware Slack alert message."""
    lock_number = item.get("lock_number", "")
    category = get_lock_category(lock_number)
    equipment = item.get("equipment", "unknown")
    reason = item.get("reason", "")
    duration = _format_duration(hours_elapsed)

    # Category-aware user/contractor line
    if category == "C":
        company = item.get("contractor_company", "unknown")
        owner_line = f"👷 Contractor: {company}"
    else:
        # A and B locks — show the associate login
        user = item.get("user", "") or "unknown"
        owner_line = f"👤 User: {user}"

    return (
        f"🚨 LOTO ALERT — {SITE_NAME}\n"
        f"🔑 Lock: {lock_number}\n"
        f"{owner_line}\n"
        f"🔧 Equipment: {equipment}\n"
        f"⏱️ Duration: {duration}\n"
        f"📋 Reason: {reason}"
    )


# ──────────────────────────────────────────────────
# Lambda handler
# ──────────────────────────────────────────────────
def handler(event, context):
    """Scan all checked-in locks. If any exceed ALERT_HOURS, send Slack
    alerts with anti-spam protection via loto_config tracker."""
    now = datetime.now(timezone.utc)
    items = get_all_locks()

    alerts_sent = []
    alerts_skipped = []
    alerts_failed = []
    locks_under_threshold = 0

    for item in items:
        lock_number = item.get("lock_number", "")
        status = item.get("status", "free")

        # 1. Only process checked-in locks
        if status != "checked_in":
            continue

        checkin_time_str = item.get("checkin_time", "")
        if not checkin_time_str:
            continue

        # 2. Calculate hours elapsed since check-in
        try:
            checkin_dt = datetime.fromisoformat(checkin_time_str)
            hours_elapsed = (now - checkin_dt).total_seconds() / 3600
        except (ValueError, TypeError):
            print(f"[skip] Bad checkin_time for {lock_number}: {checkin_time_str}")
            continue

        # 3. Skip if under threshold
        if hours_elapsed < ALERT_HOURS:
            locks_under_threshold += 1
            continue

        # 4. Anti-spam: skip if already alerted recently
        if not _should_send_alert(lock_number, now):
            alerts_skipped.append(lock_number)
            print(f"[anti-spam] Skipping {lock_number} — alerted within last {COOLDOWN_HOURS}h")
            continue

        # 5. Build and send Slack alert
        message = _build_alert_message(item, hours_elapsed)
        sent_ok = _send_slack_alert(message)

        if sent_ok:
            alerts_sent.append(lock_number)
            _record_alert_sent(lock_number, now)
        else:
            alerts_failed.append(lock_number)

    print(
        f"[summary] sent={len(alerts_sent)} skipped={len(alerts_skipped)} "
        f"failed={len(alerts_failed)} under_threshold={locks_under_threshold}"
    )

    return {
        "alerts_sent": alerts_sent,
        "alerts_skipped_antispam": alerts_skipped,
        "alerts_failed": alerts_failed,
        "locks_under_threshold": locks_under_threshold,
        "checked_at": now.isoformat(),
    }
