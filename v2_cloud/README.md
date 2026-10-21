# Digital LOTO Board v2.0 — Cloud Native

Cloud-based Lock Out / Tag Out management system with barcode scanning and real-time dashboard.

## Architecture

```
Browser (Dashboard + Check-in page)
    |
    v  HTTPS
API Gateway (REST, API Key auth)
    |
    +-- POST /checkin    -> Lambda checkin      (write)
    +-- POST /checkout   -> Lambda checkout     (write)
    +-- GET  /locks      -> Lambda get_locks    (dashboard polling)
    +-- GET  /history    -> Lambda get_history  (history + filters)
    +-- GET  /export     -> Lambda export_excel (Excel download)
    +-- POST /admin/config -> Lambda admin_config (manage badges/locks/etc.)
    |
    v
DynamoDB
    +-- loto_locks   (current state - 66 rows)
    +-- loto_history (full audit log - append-only)
    +-- loto_config  (badges, locks, locations, reasons, contractors, alert_tracker)

EventBridge (every 1 hour) -> Lambda alert_checker -> Slack webhook
```

## Project Structure

```
v2_cloud/
+-- infrastructure/
|   +-- template.yaml        <- SAM template (deploys everything to AWS)
|   +-- seed_data.py         <- Populates initial config from seed files
|
+-- lambda/                  <- Microservices (one Lambda = one function)
|   +-- shared/python/       <- Shared layer (DB helpers, response helpers)
|   |   +-- db_helpers.py
|   |   +-- response_helpers.py
|   +-- checkin/handler.py       <- POST /checkin
|   +-- checkout/handler.py      <- POST /checkout
|   +-- get_locks/handler.py     <- GET  /locks
|   +-- get_history/handler.py   <- GET  /history
|   +-- export_excel/handler.py  <- GET  /export
|   +-- admin_config/handler.py  <- POST /admin/config
|   +-- alert_checker/handler.py <- EventBridge schedule (no API route)
|
+-- layers/openpyxl/         <- openpyxl layer (build before sam build)
+-- config_data/             <- badge_to_login.txt, locks_list.txt (seed input)
|
+-- frontend/                <- Static web app
|   +-- index_cloud.html     <- Dashboard + Check-in/out + History + Admin (LIVE/DEMO)
|   +-- index_demo.html      <- Pure in-memory demo (no backend)
|
+-- barcode/                 <- Barcode label generation (TODO)
+-- README.md                <- This file
```

## Lock Categories

| Prefix | Count | Color |
|--------|-------|-------|
| A     | 20    | Red   |
| B    | 26    | Blue  |
| C      | 20    | Yellow|

> **Note:** S and C are category codes only — they do NOT stand for "Sorter" or "Conveyors". The actual meaning depends on the site.

## Prerequisites

- AWS CLI configured (`aws configure`)
- SAM CLI installed (`pip install aws-sam-cli`)
- Python 3.12 (Lambda runtime); a local Python 3.x is fine to run the helper scripts
- AWS account with access to DynamoDB, Lambda, API Gateway, EventBridge

## Deploy

### Step 1: Build the openpyxl layer (ONCE, before `sam build`)
The `/export` Lambda needs the `openpyxl` package shipped as a layer. The repo
only tracks `layers/openpyxl/requirements.txt`, so build the layer first:
```powershell
# Windows
cd layers
powershell -ExecutionPolicy Bypass -File .\build_openpyxl_layer.ps1
```
```bash
# macOS / Linux
cd layers
bash build_openpyxl_layer.sh
```
This installs openpyxl into `layers/openpyxl/python/`. If you skip this,
`ExportExcelFunction` fails at `from openpyxl import Workbook`.

### Step 2: Deploy infrastructure
```bash
cd infrastructure
sam build
sam deploy --guided \
  --parameter-overrides \
    SiteName=SITE-001 \
    AdminPassword='<choose-a-strong-password>' \
    SlackWebhookUrl='https://hooks.slack.com/triggers/...'
```
Notes:
- `SlackWebhookUrl` is optional — leave it out (or empty) to disable Slack
  notifications. Do NOT hardcode a webhook in `template.yaml`.
- `AdminPassword` guards the Admin Config write operations.

### Step 3: Seed initial data
Populates `*_loto_config` (badges + lock list) and initializes every lock as
`free` in `*_loto_locks`, reading from `config_data/`:
```bash
cd infrastructure
python seed_data.py --site SITE-001 --region eu-west-1
```

### Step 4: Retrieve the API Key value
```bash
# Get the ApiKeyId from the stack Outputs, then:
aws apigateway get-api-key --api-key <API_KEY_ID> --include-value --region eu-west-1
```

### Step 5: Configure the frontend
Open `frontend/index_cloud.html` and set the `CONFIG` object near the top of
the script (`API_URL`, `API_KEY`). While these hold their `YOUR-API...`
placeholder values the page runs in DEMO mode (in-memory data); once you fill
in the real URL + key it automatically switches to LIVE and calls the API.

### Test Slack (optional)
```powershell
$env:SLACK_WEBHOOK_URL="https://hooks.slack.com/triggers/..."; python test_slack.py
```

## Estimated Costs

| Service | Cost/month |
|---------|-----------|
| DynamoDB (on-demand, PITR) | ~$0.25 |
| Lambda (~3,000 invocations) | ~$0.00 |
| API Gateway | ~$0.10 |
| EventBridge | ~$0.00 |
| **TOTAL** | **~$0.35/month** |
