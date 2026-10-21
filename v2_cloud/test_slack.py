"""
Quick test: sends a test message to the Slack LOTO alerts channel.

The webhook URL is read from the SLACK_WEBHOOK_URL environment variable (or
passed as the first CLI argument) so no secret is committed to the repo.

Run:
  PowerShell:  $env:SLACK_WEBHOOK_URL="https://hooks.slack.com/..."; python test_slack.py
  or:          python test_slack.py https://hooks.slack.com/...
"""
import os
import sys
import urllib.request
import json

SLACK_WEBHOOK_URL = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SLACK_WEBHOOK_URL", "")).strip()
if not SLACK_WEBHOOK_URL:
    print("❌ No webhook URL. Set SLACK_WEBHOOK_URL env var or pass it as the first argument.")
    sys.exit(1)

message = (
    "🚨 *LOTO ALERT — SITE-001 (TEST)*\n"
    "Lock `A05` has been in use for *11.5 hours*\n"
    "👤 User: marco.ricci\n"
    "📍 Location: Sorter Lane 3\n"
    "📋 Reason: Scheduled PM\n"
    "🕐 Check-in: 2026-09-25 03:02:15 UTC\n"
    "\n✅ This is a test message from Digital LOTO Board v2.0"
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
        print(f"✅ Success! Status: {resp.status}")
        print(f"Response: {resp.read().decode('utf-8')}")
        print("\nCheck the #loto-alerts channel for the message!")
except Exception as e:
    print(f"❌ Error: {e}")
