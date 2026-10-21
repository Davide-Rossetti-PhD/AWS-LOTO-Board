# Digital LOTO Board — Architecture Document

## 1. Project Overview

### 1.1 Objective

Digitize the LOTO (Lock Out / Tag Out) procedure with a cloud-based system, eliminating human error through barcode scanning on both badges and locks, and providing a real-time dashboard and a fully queryable operation history.

### 1.2 Lock Categories

The system manages 67 locks divided into 3 categories plus one test lock:

| Prefix | Count | Color | Range |
|--------|-------|-------|-------|
| **A** | 20 | Red | A01 – A20 |
| **B** | 26 | Blue | B01 – B26 |
| **C** | 20 | Yellow | C01 – C20 |
| **TEST** | 1 | Grey | TESTBENCH |

> Note: A, B, and C are category codes only — they do NOT stand for specific meanings. The actual meaning depends on the site configuration.

## 2. Architecture

### 2.1 System Diagram

```
AT THE SITE
  Browser: Dashboard + Check-in page
  Hardware: USB Barcode Scanner
  Barcode labels on all 67 locks
        |
        v  HTTPS (internet)

AWS CLOUD (eu-west-1)
  API Gateway (REST, API Key auth)
    +-- POST /checkin    → Lambda checkin
    +-- POST /checkout   → Lambda checkout
    +-- GET  /locks      → Lambda get_locks
    +-- GET  /history    → Lambda get_history
    +-- GET  /export     → Lambda export_excel
    +-- POST /admin      → Lambda admin_config
        |
        v
  DynamoDB (3 tables)
    +-- loto_locks   (current state — 67 rows)
    +-- loto_history (audit log — append-only)
    +-- loto_config  (badges, locks, locations, reasons)

  EventBridge (hourly) → Lambda alert_checker → Slack webhook
```

### 2.2 Data Flow

Every operation follows the same pattern:
1. Browser sends HTTPS request to API Gateway
2. API Gateway validates API Key, invokes appropriate Lambda
3. Lambda validates inputs against `loto_config`
4. Lambda reads/writes `loto_locks` for current state
5. Lambda appends to `loto_history` for the audit trail
6. Dashboard polls `GET /locks` every 15 seconds to refresh

## 3. DynamoDB Schema

All tables use on-demand billing (PAY_PER_REQUEST) and have Point-in-Time Recovery enabled.

### 3.1 loto_locks (Current State)

Real-time state of every lock. Always contains exactly 67 items.

| Attribute | Type | Key | Description |
|-----------|------|-----|-------------|
| **lock_number** | String | PK | e.g. "A05", "B14", "C03" |
| status | String | | "free" or "checked_in" |
| user | String | | User login |
| badge_id | String | | Scanned badge number |
| location | String | | Where the lock was placed |
| reason | String | | Why the equipment is locked out |
| checkin_time | String (ISO) | | UTC timestamp |

### 3.2 loto_history (Audit Log)

Permanent record of every operation. Append-only — rows are never updated or deleted.

| Attribute | Type | Key | Description |
|-----------|------|-----|-------------|
| **lock_number** | String | PK | Partition Key |
| **timestamp** | String (ISO) | SK | Sort Key — UTC server timestamp |
| action | String | | "check_in" or "check_out" |
| user_in | String | | Login of who locked |
| user_out | String | | Login of who unlocked (check_out only) |
| location | String | | Location |
| reason | String | | Reason |
| duration_minutes | Number | | Lock duration (check_out only) |

### 3.3 loto_config (Configuration)

Centralized configuration managed via the Admin Config panel.

| Attribute | Type | Key | Description |
|-----------|------|-----|-------------|
| **config_type** | String | PK | "badge_map", "lock_list", "locations", "reasons" |
| **config_key** | String | SK | The specific value (badge ID, lock number, etc.) |
| value | String | | Associated data (login, category, etc.) |
| active | Boolean | | Soft delete — set to false to disable |

## 4. Lambda Microservices

Each Lambda is independent with a single `handler.py` file and least-privilege IAM permissions.

| Lambda | Trigger | Purpose | Memory | Timeout |
|--------|---------|---------|--------|---------|
| **checkin** | POST /checkin | Validate badge + lock, mark as checked_in | 128 MB | 60s |
| **checkout** | POST /checkout | Validate badge, calculate duration, mark free | 128 MB | 60s |
| **get_locks** | GET /locks | Return all 67 locks with state + alerts | 128 MB | 60s |
| **get_history** | GET /history | Return filtered operation history | 128 MB | 60s |
| **export_excel** | GET /export | Generate Excel file on-demand | 256 MB | 120s |
| **admin_config** | POST /admin | Manage badges, locks, locations, reasons | 128 MB | 60s |
| **alert_checker** | EventBridge (hourly) | Check for locks >10h, send Slack alert | 128 MB | 60s |

## 5. Security

Five layers of protection:

| Layer | Control | Implementation |
|-------|---------|----------------|
| **1. API Key** | Authentication | Every request requires `x-api-key` header. Revocable in 30 seconds. |
| **2. CORS** | Origin restriction | API Gateway accepts requests only from configured origin. |
| **3. Validation** | Input checking | Every Lambda validates badge, lock, and state consistency. |
| **4. IAM** | Least privilege | Each Lambda has only the DynamoDB permissions it needs. |
| **5. PITR** | Data protection | Continuous backup with 1-second granularity for 35 days. |

## 6. Alert System

The alert checker runs independently of the browser — alerts fire even if no one is looking at the dashboard.

1. **EventBridge** triggers `alert_checker` Lambda every hour
2. Lambda scans `loto_locks` for checked-in A locks
3. If any A lock has been in use for >10 hours → sends alert
4. Alert is posted to Slack via webhook URL (stored as environment variable)
5. Anti-spam: tracks last alert time per lock to avoid duplicate notifications

## 7. Cost Estimate

Based on 67 locks, ~3,000 operations/month, dashboard polling every 15 seconds:

| Service | Detail | Cost/month |
|---------|--------|------------|
| DynamoDB | 3 tables, on-demand, PITR | ~$0.25 |
| Lambda | ~3,000 invocations, 128MB | ~$0.00 |
| API Gateway | ~90,000 calls (polling) | ~$0.10 |
| EventBridge | Hourly rule | ~$0.00 |
| CloudWatch | Lambda logs | ~$0.00 |
| **Total** | | **~$0.35/month** |

### Hardware (one-time)
- USB Barcode Scanner: ~€25
- Barcode labels for 67 locks: ~€5
- **Total: ~€30**

## 8. Failure Scenarios

| Scenario | Impact | Protection |
|----------|--------|------------|
| Site PC shuts down | Dashboard not visible | Open browser on any other device |
| Internet drops | Cannot check in/out | No data loss — resume when connected |
| Lambda crashes | Single operation fails | Auto-retry (up to 2 attempts) |
| Data deleted by mistake | Data lost | PITR: restore to any second in 35 days |
| API Key stolen | Fake check-ins possible | Revoke in 30 seconds, generate new key |
| Barcode scanner breaks | Cannot scan | Fallback: type manually in the text field |
