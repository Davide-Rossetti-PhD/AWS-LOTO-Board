# -*- coding: utf-8 -*-
"""
Lambda: Admin Config Management
Endpoint: POST /admin/config

CRUD for the loto_config DynamoDB table — manages:
  - badge_map     (badge ID → user login)
  - lock_list     (lock number → category; also syncs loto_locks table)
  - locations     (predefined location names)
  - reasons       (predefined reason texts)
  - contractors   (predefined contractor company names)

Actions:
  list   — no password required, returns all entries for a config_type
  add    — password required, creates new entry (duplicate → 409)
  update — password required, toggles active flag only
  delete — password required, removes entry (lock_list also removes from loto_locks)

Request body keys: action, config_type, key, value, active, password
"""

import json
import sys
import os

# ── Shared layer ──
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared", "python"))
from db_helpers import func_config_table, func_locks_table, get_lock_category
from response_helpers import success, error
from boto3.dynamodb.conditions import Key

# ── Constants ──
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "CHANGE_ME")
VALID_CONFIG_TYPES = {"badge_map", "lock_list", "locations", "reasons", "contractors"}
WRITE_ACTIONS = {"add", "update", "delete"}


# ══════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════

def handler(event, context):
    # ── Parse request ──
    try:
        body = json.loads(event.get("body", "{}"))
    except (json.JSONDecodeError, TypeError):
        return error(400, "Invalid JSON body")

    action = body.get("action", "").strip().lower()
    config_type = body.get("config_type", "").strip()

    # ── Validate action ──
    if action not in ("add", "update", "delete", "list"):
        return error(400, "Invalid action. Must be: add, update, delete, or list")

    # ── Validate config_type ──
    if config_type not in VALID_CONFIG_TYPES:
        return error(400, f"Invalid config_type. Must be one of: {', '.join(sorted(VALID_CONFIG_TYPES))}")

    # ── Password check for write operations ──
    if action in WRITE_ACTIONS:
        password = body.get("password", "")
        if not password:
            return error(403, "Admin password is required for add/update/delete operations")
        if password != ADMIN_PASSWORD:
            return error(403, "Invalid admin password")

    # ── Route ──
    if action == "list":
        return _list_config(config_type)

    key = body.get("key", "").strip()
    if not key:
        return error(400, "'key' is required for add/update/delete")

    if action == "add":
        value = body.get("value", "").strip()
        return _add_config(config_type, key, value)
    elif action == "update":
        active = body.get("active")
        if active is None or not isinstance(active, bool):
            return error(400, "'active' (true/false) is required for update")
        return _update_config(config_type, key, active)
    elif action == "delete":
        return _delete_config(config_type, key)


# ══════════════════════════════════════════════════
# LIST — no password required
# ══════════════════════════════════════════════════

def _list_config(config_type: str) -> dict:
    """Return all entries for a config_type, sorted by key."""
    table = func_config_table()
    resp = table.query(
        KeyConditionExpression=Key("config_type").eq(config_type)
    )
    items = resp.get("Items", [])

    entries = []
    for item in items:
        entries.append({
            "key": item.get("config_key", ""),
            "value": item.get("value", item.get("config_value", "")),
            "active": item.get("active", True),
        })

    entries.sort(key=lambda e: e["key"])

    return success({
        "config_type": config_type,
        "count": len(entries),
        "entries": entries,
    })


# ══════════════════════════════════════════════════
# ADD — password required
# ══════════════════════════════════════════════════

def _add_config(config_type: str, key: str, value: str) -> dict:
    """Add a new config entry. Rejects duplicates with 409."""
    table = func_config_table()

    # Duplicate check
    resp = table.get_item(Key={"config_type": config_type, "config_key": key})
    if resp.get("Item"):
        return error(409, f"Entry already exists: {config_type}/{key}. Delete it first or use 'update'.")

    # Value required for badge_map and lock_list
    if config_type in ("badge_map", "lock_list") and not value:
        return error(400, f"'value' is required when adding to {config_type}")

    # Write to loto_config
    table.put_item(Item={
        "config_type": config_type,
        "config_key": key,
        "value": value,
        "active": True,
    })

    # For lock_list: also create a free lock in loto_locks
    if config_type == "lock_list":
        func_locks_table().put_item(Item={
            "lock_number": key,
            "status": "free",
            "category": get_lock_category(key),
            "user": "",
            "badge_id": "",
            "equipment": "",
            "reason": "",
            "checkin_time": "",
            "contractor_company": "",
            "contractor_employee": "",
        })

    return success({
        "message": f"Added {config_type}/{key}",
        "config_type": config_type,
        "key": key,
        "value": value,
        "active": True,
    }, 201)


# ══════════════════════════════════════════════════
# UPDATE — password required (toggle active flag)
# ══════════════════════════════════════════════════

def _update_config(config_type: str, key: str, active: bool) -> dict:
    """Enable or disable a config entry by setting its active flag."""
    table = func_config_table()

    # Existence check
    resp = table.get_item(Key={"config_type": config_type, "config_key": key})
    if not resp.get("Item"):
        return error(404, f"Entry not found: {config_type}/{key}")

    # Update active flag in loto_config
    table.update_item(
        Key={"config_type": config_type, "config_key": key},
        UpdateExpression="SET #a = :a",
        ExpressionAttributeNames={"#a": "active"},
        ExpressionAttributeValues={":a": active},
    )

    # For lock_list: sync status in loto_locks when disabling
    if config_type == "lock_list" and not active:
        try:
            lock_resp = func_locks_table().get_item(Key={"lock_number": key})
            lock_item = lock_resp.get("Item")
            if lock_item:
                current_status = lock_item.get("status", "free")
                if current_status == "checked_in":
                    user = lock_item.get("user") or lock_item.get("contractor_employee", "unknown")
                    return error(409,
                        f"Cannot disable lock {key} — it is currently in use by {user}. "
                        f"Check it out first.")
                # Mark as disabled in loto_locks
                func_locks_table().update_item(
                    Key={"lock_number": key},
                    UpdateExpression="SET #s = :s",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":s": "disabled"},
                )
        except Exception:
            pass  # Best effort sync

    return success({
        "message": f"Updated {config_type}/{key} — active={active}",
        "config_type": config_type,
        "key": key,
        "active": active,
    })


# ══════════════════════════════════════════════════
# DELETE — password required
# ══════════════════════════════════════════════════

def _delete_config(config_type: str, key: str) -> dict:
    """Remove a config entry. For lock_list, also removes from loto_locks."""
    table = func_config_table()

    # Existence check
    resp = table.get_item(Key={"config_type": config_type, "config_key": key})
    if not resp.get("Item"):
        return error(404, f"Entry not found: {config_type}/{key}")

    # For lock_list: block deletion if the lock is currently checked in
    if config_type == "lock_list":
        lock_resp = func_locks_table().get_item(Key={"lock_number": key})
        lock_item = lock_resp.get("Item")
        if lock_item and lock_item.get("status") == "checked_in":
            user = lock_item.get("user") or lock_item.get("contractor_employee", "unknown")
            return error(409,
                f"Cannot delete lock {key} — it is currently in use by {user}. "
                f"Check it out first.")

    # Delete from loto_config
    table.delete_item(Key={"config_type": config_type, "config_key": key})

    # For lock_list: also remove from loto_locks
    if config_type == "lock_list":
        try:
            func_locks_table().delete_item(Key={"lock_number": key})
        except Exception:
            pass  # Best effort

    return success({
        "message": f"Deleted {config_type}/{key}",
        "config_type": config_type,
        "key": key,
    })
