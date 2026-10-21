# -*- coding: utf-8 -*-
"""
Lambda: GET /export
Generates Excel files on-demand from loto_history, one sheet per lock category.

Query parameters:
  ?type=A           → Red (A) sheet only
  ?type=S             → Blue (B) Transitional sheet only
  ?type=C             → Yellow (C) sheet only
  ?type=all (or omit) → all 3 sheets in one workbook
  &month=2026-09      (optional — ISO month prefix to filter records)
  &search=conveyor    (optional — text search across all fields, case-insensitive)

Produces up to 3 sheets matching the official Appendix A LOTO log formats:
  Sheet 1: "Red (A)"             — Red Lock LOTO Log
  Sheet 2: "Blue (B) Transitional" — Transitional Lock LOTO Log
  Sheet 3: "Yellow (C)"            — Yellow Lock LOTO Log (contractor fields)

Each sheet has the EXACT column headers from the official Appendix A documents.
"""

import json
import base64
import sys
import os
from io import BytesIO
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared", "python"))
from db_helpers import func_history_table
from response_helpers import error
from boto3.dynamodb.conditions import Attr

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side


# ── Column definitions per lock type (matching Appendix A exactly) ──────────

RED_BLUE_HEADERS = [
    "Lock Number",
    "Equipment Being Locked Out",
    "Authorized Associate (login) Performing Lockout",
    "Lockout Start Date/Time",
    "Reason for Lockout",
    "Authorized Associate (login) Removing Lockout",
    "Lockout Removal Date/Time",
]

YELLOW_HEADERS = [
    "Lock Number",
    "Equipment Being Locked Out",
    "Contractor Company",
    "Lockout Start Date/Time",
    "Reason for Lockout",
    "Contractor Employee who is using the lock",
    "Lockout Removal Date/Time",
]

# Sheet config: (sheet_name, category_code, headers, header_fill_color)
SHEET_CONFIGS = [
    ("Red (A)",              "A", RED_BLUE_HEADERS, "C62828"),
    ("Blue (B) Transitional",  "B",   RED_BLUE_HEADERS, "1565C0"),
    ("Yellow (C)",             "C",   YELLOW_HEADERS,    "F9A825"),
]


# ── Helpers ─────────────────────────────────────────────────────────────────

def _format_ts(iso_str: str) -> str:
    """Convert ISO timestamp to DD/MM/YYYY HH:MM (European format for Italy)."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%d/%m/%Y %H:%M")
    except (ValueError, TypeError):
        return iso_str


def _build_sheet(wb: Workbook, sheet_name: str, category: str,
                 headers: list, fill_hex: str, items: list):
    """
    Build one Excel sheet for a given lock category.

    Data logic:
      • check_out records contain full lifecycle data (user_in, checkin_time,
        user_out, checkout timestamp) → rendered as complete rows.
      • check_in records without a matching check_out → rendered with the
        "Removing" and "Removal Date/Time" columns left empty.
    """
    ws = wb.create_sheet(title=sheet_name)

    # ── Header row styling ──
    header_fill = PatternFill(start_color=fill_hex, end_color=fill_hex,
                              fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF", size=10)
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )

    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
        cell.border = thin_border

    # ── Filter items for this category ──
    # The "category" field is written by write_history() at insert time.
    cat_items = [i for i in items if i.get("category", "") == category]

    checkin_records = []
    checkout_records = []
    for item in cat_items:
        action = item.get("action", "")
        if action == "check_in":
            checkin_records.append(item)
        elif action == "check_out":
            checkout_records.append(item)

    # Build a set of (lock_number, checkin_time) from check_out records so we
    # can skip the corresponding check_in (already covered by the check_out).
    completed_keys = set()
    for co in checkout_records:
        key = (co.get("lock_number", ""), co.get("checkin_time", ""))
        if key[1]:
            completed_keys.add(key)

    # ── Assemble rows ──
    rows = []  # list of (sort_key, [cell_values])
    is_yellow = (category == "C")

    # 1) check_out records → complete rows
    for item in checkout_records:
        ci_time = item.get("checkin_time", "")
        co_time = item.get("timestamp", "")

        if is_yellow:
            row = [
                item.get("lock_number", ""),
                item.get("equipment", ""),
                item.get("contractor_company", ""),
                _format_ts(ci_time),
                item.get("reason", ""),
                item.get("contractor_employee_out",
                         item.get("contractor_employee_in", "")),
                _format_ts(co_time),
            ]
        else:
            row = [
                item.get("lock_number", ""),
                item.get("equipment", ""),
                item.get("user_in", ""),
                _format_ts(ci_time),
                item.get("reason", ""),
                item.get("user_out", ""),
                _format_ts(co_time),
            ]
        rows.append((ci_time, row))

    # 2) check_in records WITHOUT a matching check_out → incomplete rows
    for item in checkin_records:
        ci_time = item.get("timestamp", "")
        key = (item.get("lock_number", ""), ci_time)
        if key in completed_keys:
            continue

        if is_yellow:
            row = [
                item.get("lock_number", ""),
                item.get("equipment", ""),
                item.get("contractor_company", ""),
                _format_ts(ci_time),
                item.get("reason", ""),
                item.get("contractor_employee", ""),
                "",  # Lockout Removal Date/Time — still locked
            ]
        else:
            row = [
                item.get("lock_number", ""),
                item.get("equipment", ""),
                item.get("user_in", ""),
                _format_ts(ci_time),
                item.get("reason", ""),
                "",  # Authorized Associate Removing Lockout — empty
                "",  # Lockout Removal Date/Time — empty
            ]
        rows.append((ci_time, row))

    # Sort chronologically by Lockout Start Date/Time
    rows.sort(key=lambda r: r[0])

    # Write data rows
    for _, row_data in rows:
        ws.append(row_data)

    # ── Column widths (A–G) ──
    col_widths = [14, 30, 30, 22, 30, 30, 22]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[chr(64 + i)].width = w

    # Apply borders and alignment to data cells
    for row_idx in range(2, ws.max_row + 1):
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.border = thin_border
            cell.alignment = Alignment(vertical="center", wrap_text=True)

    ws.freeze_panes = "A2"
    ws.page_setup.orientation = "landscape"


# ── Lambda entry point ──────────────────────────────────────────────────────

def handler(event, context):
    params = event.get("queryStringParameters") or {}
    type_filter = params.get("type", "all").upper()
    month = params.get("month", "")  # optional ISO month prefix, e.g. "2026-09"
    search = params.get("search", "").strip().lower()  # optional text search

    valid_types = {"A", "B", "C", "ALL"}
    if type_filter not in valid_types:
        return error(400, f"Invalid type '{type_filter}'. Use A, S, C, or all.")

    # ── Fetch history records (optionally filtered by month) ──
    table = func_history_table()
    items = []
    last_key = None

    while True:
        scan_kwargs = {}
        if month:
            scan_kwargs["FilterExpression"] = Attr("timestamp").begins_with(month)
        if last_key:
            scan_kwargs["ExclusiveStartKey"] = last_key
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break

    # ── Apply search filter (case-insensitive across all fields) ──
    if search:
        def _matches(item):
            for v in item.values():
                if isinstance(v, str) and search in v.lower():
                    return True
            return False
        items = [i for i in items if _matches(i)]

    # ── Build workbook with sheets per Appendix A ──
    wb = Workbook()
    wb.remove(wb.active)  # remove default empty sheet

    for sheet_name, cat_code, headers, color in SHEET_CONFIGS:
        if type_filter != "ALL" and type_filter != cat_code:
            continue
        _build_sheet(wb, sheet_name, cat_code, headers, color, items)

    # Fallback: if no sheets were created, add an info sheet
    if not wb.sheetnames:
        ws = wb.create_sheet("No Data")
        ws.append(["No records found for the selected filters."])

    # ── Serialize to binary and return via API Gateway ──
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"LOTO_Log_SITE-001_{today}.xlsx"

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": (
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            ),
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Allow-Origin": "*",
        },
        "body": base64.b64encode(buffer.getvalue()).decode("utf-8"),
        "isBase64Encoded": True,
    }
