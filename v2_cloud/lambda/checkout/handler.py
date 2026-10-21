# -*- coding: utf-8 -*-
"""
Lambda: POST /checkout
Registers a lock check-out (lock returned to the board).

Supports 3 LOTO types with different check-out rules:
  - A (Red):   badge required — MUST be the same user who checked it in
  - B (Blue):    badge required — any authorized associate can return it
  - C (Yellow):  contractor_employee name required (manual text), no badge needed

Request body (A / B locks):
{
    "badge": "10000002",
    "lock": "A05"
}

Request body (C / Yellow locks):
{
    "lock": "C04",
    "contractor_employee": "John Smith"
}

Validates inputs, enforces ownership rules per POC / Appendix A,
calculates lock duration, updates state, writes history, sends Slack notification.
"""

import json
import sys
import os
import urllib.request
from urllib.error import URLError, HTTPError
from datetime import datetime, timezone

# Add shared layer to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared", "python"))
from db_helpers import (
    resolve_badge, get_lock_state, get_lock_category,
    set_lock_free, write_history, is_contractor_lock
)
from response_helpers import success, error

# ── Slack notification config ──
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SITE_NAME = os.environ.get("SITE_NAME", "SITE-001")


def _format_duration(minutes: int) -> str:
    """Format duration_minutes into a human-readable string like '11h 30m'."""
    if minutes < 1:
        return "< 1m"
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h 0m"
    return f"{m}m"


def _send_slack_notification(lock_number: str, user_or_contractor: str,
                             duration_minutes: int, reason: str) -> None:
    """Best-effort Slack notification on successful check-out.
    Payload format: {"Content": "message"} — same as alert_checker."""
    if not SLACK_WEBHOOK_URL:
        print(f"No SLACK_WEBHOOK_URL configured — skipping checkout notification")
        return

    message = (
        f"🔓 LOTO CHECK-OUT — {SITE_NAME}\n"
        f"🔑 Lock: {lock_number}\n"
        f"👤 User: {user_or_contractor}\n"
        f"⏱️ Duration: {_format_duration(duration_minutes)}\n"
        f"📋 Reason: {reason}"
    )

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
                print("Slack checkout notification sent successfully")
            else:
                print(f"Slack returned status {resp.status}")
    except (URLError, HTTPError) as e:
        print(f"Error sending Slack checkout notification: {e}")


def handler(event, context):
    # ── Parse request body ──
    try:
        body = json.loads(event.get("body", "{}"))
    except (json.JSONDecodeError, TypeError):
        return error(400, "Invalid JSON body")

    lock_number = body.get("lock_number", body.get("lock", "")).strip().upper()

    # ── Validate lock number ──
    if not lock_number:
        return error(400, "Lock number is required")

    # ── Check lock is currently in use ──
    state = get_lock_state(lock_number)
    if not state or state.get("status") != "checked_in":
        return error(409, f"Lock '{lock_number}' is not currently in use")

    # ── Detect lock category ──
    category = get_lock_category(lock_number)

    # ── Retrieve original check-in data ──
    user_in = state.get("user", "")
    equipment = state.get("equipment", "")
    reason = state.get("reason", "")
    checkin_time_str = state.get("checkin_time", "")
    contractor_company = state.get("contractor_company", "")
    contractor_employee_in = state.get("contractor_employee", "")

    # ── Branch validation by lock category ──
    user_out = ""
    badge_out = ""
    contractor_employee_out = ""

    if category == "C":
        # ─── YELLOW (C) LOCK ───
        # No badge required.  contractor_employee is the person returning the lock.
        contractor_employee_out = body.get("contractor_employee", "").strip()
        if not contractor_employee_out:
            return error(400, "Contractor employee name is required for Yellow (C) lock check-out")

    elif category == "A":
        # ─── RED (A) LOCK ───
        # Badge required.  MUST be the same user who checked it in.
        badge_out = body.get("badge", "").strip()
        if not badge_out:
            return error(400, "Badge is required for A lock check-out")
        user_out = resolve_badge(badge_out)
        if not user_out:
            return error(400, f"Badge '{badge_out}' not recognized")
        # Ownership check — only the user who checked in can check out
        if user_in and user_out != user_in:
            return error(
                403,
                f"Lock was checked in by {user_in} — only they can check it out"
            )

    elif category == "B":
        # ─── BLUE (S) LOCK — Transitional ───
        # Badge required, but ANY authorized associate may return it.
        badge_out = body.get("badge", "").strip()
        if not badge_out:
            return error(400, "Badge is required for B (Blue) lock check-out")
        user_out = resolve_badge(badge_out)
        if not user_out:
            return error(400, f"Badge '{badge_out}' not recognized")
        # No ownership check — any valid badge holder can return a Blue lock.

    else:
        return error(400, f"Unrecognized lock category for '{lock_number}'")

    # ── Calculate duration ──
    duration_minutes = 0
    now = datetime.now(timezone.utc)
    checkout_time = now.isoformat()

    if checkin_time_str:
        try:
            checkin_dt = datetime.fromisoformat(checkin_time_str)
            duration_minutes = round((now - checkin_dt).total_seconds() / 60)
        except (ValueError, TypeError):
            pass

    # ── Execute check-out: mark lock as free ──
    set_lock_free(lock_number)

    # ── Write audit entry to history table ──
    history_kwargs = dict(
        lock_number=lock_number,
        action="check_out",
        user_in=user_in,
        user_out=user_out,
        badge_out=badge_out,
        equipment=equipment,
        reason=reason,
        duration_minutes=duration_minutes,
        checkin_time=checkin_time_str,
        checkout_time=checkout_time,
        contractor_company=contractor_company,
        contractor_employee_in=contractor_employee_in,
        contractor_employee_out=contractor_employee_out,
    )
    write_history(**history_kwargs)

    # ── Slack notification (best-effort, never blocks response) ──
    try:
        notify_user = user_out or contractor_employee_out or "unknown"
        _send_slack_notification(lock_number, notify_user, duration_minutes, reason)
    except Exception as e:
        print(f"Slack notification error (non-fatal): {e}")

    # ── Build response ──
    resp = {
        "status": "ok",
        "message": "Check-out completed",
        "lock": lock_number,
        "category": category,
        "duration_minutes": duration_minutes,
        "duration_display": _format_duration(duration_minutes),
        "checkout_time": checkout_time,
    }
    if category == "C":
        resp["contractor_company"] = contractor_company
        resp["contractor_employee_in"] = contractor_employee_in
        resp["contractor_employee_out"] = contractor_employee_out
    else:
        resp["user_in"] = user_in
        resp["user_out"] = user_out

    return success(resp)
