# -*- coding: utf-8 -*-
"""
Shared DynamoDB helpers for all Lambda microservices.
Each Lambda imports from this module for consistent DB access.

Supports 3 LOTO lock types with different field requirements:
  - A (Red):    Site associate login via badge (check-in & check-out)
  - B (Blue):     Site associate login via badge (check-in & check-out)
  - C (Yellow):   Contractor — requires contractor_company + contractor_employee (manual text)
"""

import boto3
import json
import os
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError
from boto3.dynamodb.conditions import Key, Attr

# ──────────────────────────────────────────────────
# Environment variables (set by SAM template)
# ──────────────────────────────────────────────────
LOCKS_TABLE = os.environ.get("LOCKS_TABLE", "loto_locks")
HISTORY_TABLE = os.environ.get("HISTORY_TABLE", "loto_history")
CONFIG_TABLE = os.environ.get("CONFIG_TABLE", "loto_config")

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
ALERT_HOURS = int(os.environ.get("ALERT_HOURS", "10"))
SITE_NAME = os.environ.get("SITE_NAME", "SITE-001")

# ──────────────────────────────────────────────────
# DynamoDB resource (reused across invocations)
# ──────────────────────────────────────────────────
_dynamodb = None


def _get_dynamodb():
    """Lazy-init DynamoDB resource (connection reuse across warm Lambda invocations)."""
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "eu-west-1"))
    return _dynamodb


def func_locks_table():
    return _get_dynamodb().Table(LOCKS_TABLE)


def func_history_table():
    return _get_dynamodb().Table(HISTORY_TABLE)


def func_config_table():
    return _get_dynamodb().Table(CONFIG_TABLE)


# ──────────────────────────────────────────────────
# Category helpers
# ──────────────────────────────────────────────────

def get_category(lock_number: str) -> str:
    """Return category string from lock number prefix: 'A', 'B', 'C', or 'OTHER'."""
    ln = lock_number.upper().strip()
    if ln.startswith("A"):
        return "A"
    elif ln.startswith("B"):
        return "B"
    elif ln.startswith("C"):
        return "C"
    return "OTHER"


# Backward-compatible alias used by existing handlers
get_lock_category = get_category


def is_contractor_lock(lock_number: str) -> bool:
    """Check if a lock belongs to the Yellow/Contractor category (C prefix). Return True if the locker is yellow
    Yellow locks have different fields: contractor_company and contractor_employee
    instead of site associate login via badge."""
    return get_category(lock_number) == "C"


# ──────────────────────────────────────────────────
# Lock CRUD
# ──────────────────────────────────────────────────

def get_lock(lock_number: str) -> dict | None:
    """Get a single lock from loto_locks by lock_number."""
    resp = func_locks_table().get_item(Key={"lock_number": lock_number})
    return resp.get("Item")


# Backward-compatible alias
get_lock_state = get_lock


def get_all_locks() -> list[dict]:
    """Scan loto_locks and return all items."""
    resp = func_locks_table().scan()    #questo ci restituisce un dizionario pieno di items
    items = resp.get("Items", [])
    # Handle pagination for large tables (unlikely with 67 locks, but safe)
    while "LastEvaluatedKey" in resp:
        resp = func_locks_table().scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    return items


def update_lock(lock_number: str, updates: dict) -> dict:
    """Update specific fields on a lock item in loto_locks.
    
    Args:
        lock_number: The partition key of the lock.
        updates: Dict of field_name -> new_value to update.
    
    Returns:
        The updated item attributes.
    """ #preparo 3 empty boxes
    expr_parts = []
    expr_names = {}
    expr_values = {}
    for i, (field, value) in enumerate(updates.items()):
        placeholder = f"#f{i}"
        val_placeholder = f":v{i}"
        expr_parts.append(f"{placeholder} = {val_placeholder}")
        expr_names[placeholder] = field
        expr_values[val_placeholder] = value
    
    update_expr = "SET " + ", ".join(expr_parts)
    resp = func_locks_table().update_item(
        Key={"lock_number": lock_number},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
        ReturnValues="ALL_NEW",
    )
    return resp.get("Attributes", {})


def set_lock_checked_in(lock_number: str, user: str, badge_id: str,
                        equipment: str, reason: str,
                        contractor_company: str = "",
                        contractor_employee: str = "") -> str:
    """Mark a lock as checked_in. Returns the server timestamp.
    
    For A/B locks: user and badge_id come from barcode scan.
    For C (Yellow) locks: user may be empty; contractor_company and
        contractor_employee are used instead.
    """
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "lock_number": lock_number,
        "status": "checked_in",
        "category": get_category(lock_number),
        "user": user,
        "badge_id": badge_id,
        "equipment": equipment,
        "reason": reason,
        "checkin_time": now,
        "contractor_company": contractor_company,
        "contractor_employee": contractor_employee,
    }
    func_locks_table().put_item(Item=item)
    return now


def set_lock_free(lock_number: str) -> str:
    """Mark a lock as free. Returns the server timestamp."""
    now = datetime.now(timezone.utc).isoformat()
    func_locks_table().put_item(Item={
        "lock_number": lock_number,
        "status": "free",
        "category": get_category(lock_number),
        "user": "",
        "badge_id": "",
        "equipment": "",
        "reason": "",
        "checkin_time": "",
        "contractor_company": "",
        "contractor_employee": "",
    })
    return now


# ──────────────────────────────────────────────────
# History helpers
# ──────────────────────────────────────────────────

def put_history(record: dict) -> None:
    """Put a record into loto_history.
    
    Caller provides a complete dict; this function ensures timestamp
    and category are set if missing.
    """
    if "timestamp" not in record:
        record["timestamp"] = datetime.now(timezone.utc).isoformat()
    if "category" not in record and "lock_number" in record:
        record["category"] = get_category(record["lock_number"])
    func_history_table().put_item(Item=record)


def write_history(lock_number: str, action: str, **kwargs) -> None:
    """Append an entry to loto_history (backward-compatible convenience wrapper).
    All extra kwargs (user_in, user_out, equipment, reason,
    contractor_company, contractor_employee, etc.) are stored as-is."""
    record = {
        "lock_number": lock_number,
        "action": action,
    }
    record.update(kwargs)
    put_history(record)


def query_history(lock_number: str | None = None, category: str | None = None) -> list[dict]:
    """Query loto_history, optionally filtered by lock_number and/or category.
    
    - If lock_number is provided, queries on the partition key (efficient).
    - If only category is provided, scans with a filter.
    - If neither is provided, returns all history (full scan).
    """
    if lock_number:
        # Query by partition key
        key_condition = Key("lock_number").eq(lock_number)
        kwargs = {"KeyConditionExpression": key_condition, "ScanIndexForward": False}
        if category:
            kwargs["FilterExpression"] = Attr("category").eq(category)
        resp = func_history_table().query(**kwargs)
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
            resp = func_history_table().query(**kwargs)
            items.extend(resp.get("Items", []))
        return items
    else:
        # Full scan, optionally filtered by category
        scan_kwargs = {}
        if category:
            scan_kwargs["FilterExpression"] = Attr("category").eq(category)
        resp = func_history_table().scan(**scan_kwargs)
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
            resp = func_history_table().scan(**scan_kwargs)
            items.extend(resp.get("Items", []))
        return items


# ──────────────────────────────────────────────────
# Config helpers
# ──────────────────────────────────────────────────

def get_config(config_type: str, config_key: str | None = None) -> dict | list[dict] | None:
    """Get item(s) from loto_config.
    
    - If config_key is provided: returns a single item dict or None.
    - If config_key is None: queries all items for that config_type, returns list.
    """
    if config_key is not None:
        resp = func_config_table().get_item(Key={"config_type": config_type, "config_key": config_key})
        return resp.get("Item")
    else:
        resp = func_config_table().query(KeyConditionExpression=Key("config_type").eq(config_type))
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = func_config_table().query(
                KeyConditionExpression=Key("config_type").eq(config_type),
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))
        return items


def put_config(config_type: str, config_key: str, data: dict) -> None:
    """Put an item into loto_config.
    
    config_type and config_key form the composite key; data fields are merged in.
    """
    item = {"config_type": config_type, "config_key": config_key}
    item.update(data)
    func_config_table().put_item(Item=item)


def resolve_badge(badge_id: str) -> str | None:
    """Convert badge number to user login. Returns login string or None if not found."""
    item = get_config("badge_map", badge_id)
    if item and item.get("active", True):
        return item.get("value")
    return None


def lock_exists(lock_number: str) -> bool:
    """Check if a lock number is registered in the config."""
    item = get_config("lock_list", lock_number)
    return bool(item and item.get("active", True))


# ──────────────────────────────────────────────────
# Slack alert
# ──────────────────────────────────────────────────

def send_slack_alert(message: str) -> bool:
    """Send a message to the Slack webhook URL configured in SLACK_WEBHOOK_URL.
    
    Returns True on success, False on failure (never raises).
    """
    url = SLACK_WEBHOOK_URL           # configurato nel template.yaml come variabile d'ambiente
    if not url:
        print("[WARN] SLACK_WEBHOOK_URL not set — skipping alert")
        return False
    try:
        payload = json.dumps({"Content": f"[{SITE_NAME} LOTO] {message}"}).encode("utf-8")
        req = Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except (URLError, Exception) as e:
        print(f"[ERROR] Slack alert failed: {e}")
        return False


# ──────────────────────────────────────────────────
# Timestamp utility
# ──────────────────────────────────────────────────

def utcnow() -> str:
    """Return current UTC time in ISO 8601."""
    return datetime.now(timezone.utc).isoformat()
