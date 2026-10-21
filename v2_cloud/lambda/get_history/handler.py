# -*- coding: utf-8 -*-
"""
Lambda: GET /history
Returns completed LOTO records from loto_history, with column definitions
that match Appendix A for each lock type.

Query parameters (required):
  ?type=A|S|C              — lock category (REQUIRED)

Query parameters (optional):
  &month=2026-09             — filter by month (compared in Europe/Rome local time)
  &search=text               — free-text search across all relevant fields
  &page=1                    — page number (default 1)
  &page_size=50              — results per page (default 50, max 500)
  &sort_by=checkin_time      — field to sort on (default checkin_time)
  &sort_dir=asc|desc         — sort direction (default desc)

Used by the "Operation History" tab in the dashboard.
"""

import json
import sys
import os
from datetime import datetime, timedelta, timezone

# Add shared layer to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared", "python"))
from db_helpers import func_history_table, get_lock_category
from response_helpers import success, error
from boto3.dynamodb.conditions import Key, Attr


# ── Italy timezone (UTC+1 base, +2 in summer / CEST) ──────────────────────
# Python 3.9+ zoneinfo would be ideal but Lambda may lack tzdata.
# We use a simple offset helper: Italy is UTC+1 (CET) or UTC+2 (CEST).
# For month-boundary filtering this is close enough — worst case a record
# near midnight could slip by one hour.  A full IANA lookup would fix that.
ITALY_OFFSET = timezone(timedelta(hours=2))  # CEST (summer, majority of year)


# ── Column definitions per type (Appendix A) ──────────────────────────────

COLUMNS_AB = [
    {"key": "lock_number",   "label": "Lock Number"},
    {"key": "equipment",     "label": "Equipment Being Locked Out"},
    {"key": "user_in",       "label": "Authorized Associate (login) Performing Lockout"},
    {"key": "checkin_time",  "label": "Lockout Start Date/Time"},
    {"key": "reason",        "label": "Reason for Lockout"},
    {"key": "user_out",      "label": "Authorized Associate (login) Removing Lockout"},
    {"key": "checkout_time", "label": "Lockout Removal Date/Time"},
]

COLUMNS_C = [
    {"key": "lock_number",          "label": "Lock Number"},
    {"key": "equipment",            "label": "Equipment Being Locked Out"},
    {"key": "contractor_company",   "label": "Contractor Company"},
    {"key": "checkin_time",         "label": "Lockout Start Date/Time"},
    {"key": "reason",               "label": "Reason for Lockout"},
    {"key": "contractor_employee",  "label": "Contractor Employee who is using the lock"},
    {"key": "checkout_time",        "label": "Lockout Removal Date/Time"},
]


def _columns_for_type(loto_type):
    """Return the Appendix A column list for the given type."""
    if loto_type in ("A", "B"):
        return COLUMNS_AB
    elif loto_type == "C":
        return COLUMNS_C
    return COLUMNS_AB  # fallback


def _searchable_keys(loto_type):
    """Return the field keys to search across for a given type."""
    return [col["key"] for col in _columns_for_type(loto_type)]


def _to_italy_month(utc_timestamp_str):
    """
    Given a UTC ISO-8601 timestamp string, return the 'YYYY-MM' portion
    in Italy local time (UTC+2 CEST approximation).
    Returns empty string on parse failure.
    """
    if not utc_timestamp_str:
        return ""
    try:
        dt = datetime.fromisoformat(utc_timestamp_str.replace("Z", "+00:00"))
        local_dt = dt.astimezone(ITALY_OFFSET)
        return local_dt.strftime("%Y-%m")
    except (ValueError, TypeError):
        # Fallback: if already a local string, just take first 7 chars
        return utc_timestamp_str[:7] if len(utc_timestamp_str) >= 7 else ""


def handler(event, context):
    params = event.get("queryStringParameters") or {}

    # ── Required: type ─────────────────────────────────────────────────
    loto_type = (params.get("type") or "").upper()
    if loto_type not in ("A", "B", "C"):
        return error(400, "Query parameter 'type' is required and must be A, S, or C.")

    # ── Optional filters ───────────────────────────────────────────────
    month = params.get("month", "")              # e.g. "2026-09"
    search = params.get("search", "").lower()    # free text
    page = max(int(params.get("page", "1")), 1)
    page_size = min(max(int(params.get("page_size", "50")), 1), 500)
    sort_by = params.get("sort_by", "checkin_time")
    sort_dir = params.get("sort_dir", "desc").lower()

    # ── Build DynamoDB scan with server-side filters where possible ────
    filter_expressions = []

    # Category is derived from lock_number prefix — filter server-side
    # by requiring lock_number to start with the right prefix.
    prefix_map = {"A": "A", "B": "B", "C": "C"}
    prefix = prefix_map.get(loto_type, loto_type)
    filter_expressions.append(Attr("lock_number").begins_with(prefix))

    if filter_expressions:
        combined = filter_expressions[0]
        for fe in filter_expressions[1:]:
            combined = combined & fe
        scan_kwargs = {"FilterExpression": combined}
    else:
        scan_kwargs = {}

    # ── Execute paginated scan (retrieve all matching items) ───────────
    table = func_history_table()
    items = []
    last_key = None

    while True:
        if last_key:
            scan_kwargs["ExclusiveStartKey"] = last_key
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break

    # ── Post-processing: verify category (handles edge cases) ─────────
    items = [i for i in items if get_lock_category(i.get("lock_number", "")) == loto_type]

    # ── Month filter (in Italy local time) ─────────────────────────────
    if month:
        items = [
            i for i in items
            if _to_italy_month(i.get("checkin_time", "")) == month
        ]

    # ── Text search across all relevant fields ─────────────────────────
    if search:
        keys = _searchable_keys(loto_type)
        filtered = []
        for item in items:
            for k in keys:
                val = str(item.get(k, "")).lower()
                if search in val:
                    filtered.append(item)
                    break
        items = filtered

    # ── Sort ───────────────────────────────────────────────────────────
    reverse = sort_dir != "asc"
    items.sort(key=lambda x: x.get(sort_by, ""), reverse=reverse)

    # ── Pagination ─────────────────────────────────────────────────────
    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    page_items = items[start:end]

    # ── Build records with only the fields relevant to this type ───────
    columns = _columns_for_type(loto_type)
    col_keys = [c["key"] for c in columns]

    records = []
    for item in page_items:
        record = {}
        for key in col_keys:
            # Yellow (C) check-out records store the returning contractor under
            # 'contractor_employee_out' (and '_in' from check-in); the Appendix A
            # column is 'contractor_employee'. Resolve it so completed Yellow
            # rows show the employee name instead of a blank cell.
            if key == "contractor_employee":
                record[key] = (
                    item.get("contractor_employee")
                    or item.get("contractor_employee_out")
                    or item.get("contractor_employee_in")
                    or ""
                )
            else:
                record[key] = item.get(key, "")
        records.append(record)

    return success({
        "type": loto_type,
        "records": records,
        "total": total,
        "page": page,
        "page_size": page_size,
        "columns": columns,
    })
