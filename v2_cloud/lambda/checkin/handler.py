# -*- coding: utf-8 -*-
"""
Lambda: POST /checkin
Registers a lock check-in (lock taken from the board).

Supports 3 LOTO types with different field requirements:
  - A (Red):    badge_id (resolved to login) + equipment + reason
  - B (Blue):     badge_id (resolved to login) + equipment + reason
  - C (Yellow):   contractor_company + equipment + reason
                  (NO badge, NO contractor_employee at check-in —
                   contractor_employee is only captured at check-out per Appendix A)

Request body (A / B locks):
{
    "lock_number": "A05",
    "badge_id": "10000001",
    "equipment": "Sorter Lane 3",
    "reason": "Scheduled PM"
}

Request body (C / Yellow locks):
{
    "lock_number": "C04",
    "equipment": "Conveyor C4",
    "reason": "Scheduled PM",
    "contractor_company": "ACME Maintenance"
}

Validates all inputs, writes to loto_locks (state) and loto_history (audit),
and sends a Slack notification.
"""

import json
import os
import sys
import urllib.request
from urllib.error import URLError, HTTPError
from datetime import datetime, timezone

# Add shared layer to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared", "python"))
from db_helpers import (
    resolve_badge, lock_exists, get_lock_state, get_lock_category,
    is_contractor_lock, func_locks_table, func_history_table,
)
from response_helpers import success, error

# ── Environment variables ──
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SITE_NAME = os.environ.get("SITE_NAME", "SITE-001")


# ── Slack notification ──

def _send_slack_notification(message: str) -> bool:
    """Send a check-in notification to Slack via Workflow Trigger webhook.
    Payload format: {"Content": "message"} — same as alert_checker."""
    if not SLACK_WEBHOOK_URL:
        print(f"No SLACK_WEBHOOK_URL configured. Message: {message}")
        return False

    payload = {"Content": message}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                print("Slack check-in notification sent successfully")
                return True
            else:
                print(f"Slack returned status {resp.status}")
                return False
    except (URLError, HTTPError) as e:
        print(f"Error sending Slack notification: {e}")
        return False


def _format_checkin_time(iso_timestamp: str) -> str:
    """Format ISO timestamp to human-readable string for Slack message."""
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_timestamp


# ── Lambda handler ──

def handler(event, context):
    # ── Parse request body ──
    try:
        body = json.loads(event.get("body", "{}"))
    except (json.JSONDecodeError, TypeError):
        return error(400, "Invalid JSON body")

    lock_number = body.get("lock_number", "").strip().upper()
    equipment = body.get("equipment", "").strip()
    reason = body.get("reason", "").strip()

    # ── Validate common required fields ──
    if not lock_number:
        return error(400, "Lock number is required")
    if not equipment:
        return error(400, "Equipment is required")
    if not reason:
        return error(400, "Reason is required")

    # ── Validate lock exists in config ──
    if not lock_exists(lock_number):
        return error(400, f"Lock '{lock_number}' is not registered in the system")

    # ── Determine category ──
    category = get_lock_category(lock_number)
    if category == "OTHER":
        return error(400, f"Lock '{lock_number}' has an unrecognized prefix (expected A, S, or C)")

    # ── Check lock is currently free ──
    state = get_lock_state(lock_number)
    if state and state.get("status") == "checked_in":
        current_user = state.get("user") or state.get("contractor_company", "unknown")
        return error(409, f"Lock '{lock_number}' is already checked in by {current_user}")

    # ── Branch validation by lock category ──
    now = datetime.now(timezone.utc).isoformat()

    if is_contractor_lock(lock_number):
        # ━━ YELLOW (C) LOCK ━━
        # Required: contractor_company. NO badge, NO contractor_employee at check-in.
        contractor_company = body.get("contractor_company", "").strip()
        if not contractor_company:
            return error(400, "Contractor company is required for Yellow (C) locks")

        # ── Write lock state to loto_locks ──
        func_locks_table().put_item(Item={
            "lock_number": lock_number,
            "status": "checked_in",
            "category": "C",
            "contractor_company": contractor_company,
            "equipment": equipment,
            "reason": reason,
            "checkin_time": now,
        })

        # ── Write audit entry to loto_history ──
        func_history_table().put_item(Item={
            "lock_number": lock_number,
            "timestamp": now,
            "action": "check_in",
            "category": "C",
            "contractor_company": contractor_company,
            "equipment": equipment,
            "reason": reason,
            "checkin_time": now,
        })

        # ── Slack notification ──
        display_time = _format_checkin_time(now)
        message = (
            f"🔒 LOTO CHECK-IN — {SITE_NAME}\n"
            f"🔑 Lock: {lock_number}\n"
            f"🏢 Contractor: {contractor_company}\n"
            f"🔧 Equipment: {equipment}\n"
            f"📋 Reason: {reason}\n"
            f"⏱️ Time: {display_time}"
        )
        _send_slack_notification(message)

        # ── Response ──
        return success({
            "status": "ok",
            "message": "Check-in completed",
            "lock_number": lock_number,
            "category": "C",
            "contractor_company": contractor_company,
            "equipment": equipment,
            "reason": reason,
            "checkin_time": now,
        })

    else:
        # ━━ RED (A) / BLUE (S) LOCK ━━
        # Required: badge_id (resolved to login via loto_config badge_map)
        badge_id = body.get("badge_id", "").strip()
        if not badge_id:
            return error(400, "Badge ID is required for A and B locks")

        login = resolve_badge(badge_id)
        if not login:
            return error(400, f"Badge '{badge_id}' not recognized")

        # ── Write lock state to loto_locks ──
        func_locks_table().put_item(Item={
            "lock_number": lock_number,
            "status": "checked_in",
            "category": category,
            "user": login,
            "badge_id": badge_id,
            "equipment": equipment,
            "reason": reason,
            "checkin_time": now,
        })

        # ── Write audit entry to loto_history ──
        func_history_table().put_item(Item={
            "lock_number": lock_number,
            "timestamp": now,
            "action": "check_in",
            "category": category,
            "user_in": login,
            "badge_in": badge_id,
            "equipment": equipment,
            "reason": reason,
            "checkin_time": now,
        })

        # ── Slack notification ──
        display_time = _format_checkin_time(now)
        message = (
            f"🔒 LOTO CHECK-IN — {SITE_NAME}\n"
            f"🔑 Lock: {lock_number}\n"
            f"👤 User: {login}\n"
            f"🔧 Equipment: {equipment}\n"
            f"📋 Reason: {reason}\n"
            f"⏱️ Time: {display_time}"
        )
        _send_slack_notification(message)

        # ── Response ──
        return success({
            "status": "ok",
            "message": "Check-in completed",
            "lock_number": lock_number,
            "category": category,
            "user": login,
            "badge_id": badge_id,
            "equipment": equipment,
            "reason": reason,
            "checkin_time": now,
        })
