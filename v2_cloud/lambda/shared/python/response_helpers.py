# -*- coding: utf-8 -*-
"""
Shared HTTP response helpers for API Gateway Lambda integration.
All responses include CORS headers for cross-origin dashboard access.
"""

import base64
import json


# ──────────────────────────────────────────────────
# CORS headers (shared by all responses)
# ──────────────────────────────────────────────────
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,x-api-key,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
}


def success_response(body: dict, status_code: int = 200) -> dict:
    """Return a successful API Gateway JSON response with CORS headers."""
    return {
        "statusCode": status_code,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def error_response(status_code: int, message: str) -> dict:
    """Return an error API Gateway JSON response with CORS headers. Args: (status_code, message)."""
    return {
        "statusCode": status_code,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps({"error": message}),
    }


def binary_response(data: bytes, content_type: str, filename: str) -> dict:
    """Return a binary file download response (e.g. Excel export).
    
    API Gateway must be configured for binary media types to pass this
    through correctly (e.g. application/vnd.openxmlformats-*).
    
    Args:
        data: Raw binary content (e.g. BytesIO.getvalue()).
        content_type: MIME type (e.g. 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet').
        filename: Suggested download filename (e.g. 'loto_export.xlsx').
    """
    return {
        "statusCode": 200,
        "headers": {
            **CORS_HEADERS,
            "Content-Type": content_type,
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
        "body": base64.b64encode(data).decode("utf-8"),
        "isBase64Encoded": True,
    }


# ──────────────────────────────────────────────────
# Backward-compatible aliases (used by existing handlers)
# ──────────────────────────────────────────────────
success = success_response
error = error_response
