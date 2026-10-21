# -*- coding: utf-8 -*-
"""
Lambda: GET /locks
Returns the current state of all locks for the dashboard.

Called by the dashboard every 15 seconds.
Scans loto_locks (67 items = instant) and calculates elapsed time + alerts.
"""

import re
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared", "python"))
from db_helpers import get_all_locks, get_category, utcnow, ALERT_HOURS
from response_helpers import success_response


def _lock_sort_key(lock_number: str) -> tuple:
    """Sort: A first, then C, then S, then numeric within group."""
    ln = lock_number.upper().strip()
    if ln.startswith("A"):
        group, num_part = 0, ln[1:]
    elif ln.startswith("C"):
        group, num_part = 1, ln[1:]
    elif ln.startswith("B"):
        group, num_part = 2, ln[1:]
    else:
        group, num_part = 3, ln
    match = re.search(r"\d+", num_part)
    num = int(match.group()) if match else 0
    return (group, num, ln)


def handler(event, context):
    now = datetime.now(timezone.utc)
    items = get_all_locks()

    locks = []
    in_use = 0
    alerts = 0

    for item in items:
        lock_number = item.get("lock_number", "")
        status = item.get("status", "free")
        category = get_category(lock_number)

        lock_data = {
            "lock_number": lock_number,
            "status": status,
            "category": category,
        }

        if status == "checked_in":
            in_use += 1

            # Calculate elapsed hours since check-in
            checkin_time_str = item.get("checkin_time", "")
            hours_elapsed = 0.0
            if checkin_time_str:
                try:
                    checkin_dt = datetime.fromisoformat(checkin_time_str)
                    hours_elapsed = round((now - checkin_dt).total_seconds() / 3600, 1)
                except (ValueError, TypeError):
                    pass

            is_alert = hours_elapsed >= ALERT_HOURS
            if is_alert:
                alerts += 1

            # Common fields for all checked-in locks
            lock_data.update({
                "equipment": item.get("equipment", ""),
                "reason": item.get("reason", ""),
                "checkin_time": checkin_time_str,
                "hours_elapsed": hours_elapsed,
                "alert": is_alert,
            })

            # Category-specific fields
            if category == "C":
                lock_data.update({
                    "user": "",
                    "badge_id": "",
                    "contractor_company": item.get("contractor_company", ""),
                    "contractor_employee": item.get("contractor_employee", ""),
                })
            else:
                # A and B locks — site associate
                lock_data.update({
                    "user": item.get("user", ""),
                    "badge_id": item.get("badge_id", ""),
                    "contractor_company": "",
                    "contractor_employee": "",
                })
        else:
            # Free locks — include empty fields so frontend shape is consistent
            lock_data.update({
                "user": "",
                "badge_id": "",
                "equipment": "",
                "reason": "",
                "checkin_time": "",
                "hours_elapsed": 0.0,
                "alert": False,
                "contractor_company": "",
                "contractor_employee": "",
            })

        locks.append(lock_data)

    # Sort by category then number
    locks.sort(key=lambda x: _lock_sort_key(x["lock_number"]))

    total = len(locks)
    return success_response({
        "server_time": utcnow(),
        "total": total,
        "in_use": in_use,
        "free": total - in_use,
        "alerts": alerts,
        "locks": locks,
    })
