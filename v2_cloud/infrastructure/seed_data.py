# -*- coding: utf-8 -*-
"""
Seed script: populates DynamoDB tables with initial configuration data.
Run ONCE after deploying the SAM template.

Usage:
  python seed_data.py --site SITE-001 --region eu-west-1

Reads badge_to_login.txt and locks_list.txt from v2_cloud/config_data/
and populates:
  - loto_config (badge mappings, lock list)
  - loto_locks (initial state: all locks set to "free")

Locations, reasons, and contractor companies are NOT seeded here —
they are entered manually by the operator via the Admin Config page.

Supports 3 LOTO lock types:
  - A (Red):   Site associates, badge-based auth
  - B (Blue):    Site associates, badge-based auth (Transitional locks)
  - C (Yellow):  Contractor locks — uses contractor_company + contractor_employee
"""

# --- Libraries ---
import boto3       # AWS SDK for Python — used to write to DynamoDB
import argparse    # Reads command-line parameters (--site, --region, etc.)
import os          # File path utilities (os.path.join, os.path.exists)
import sys         # sys.exit(1) — abort the script on critical error

# =============================================================================
# FUNCTION 1: load_badges
# Reads badge_to_login.txt and returns a list of Python dicts
# =============================================================================
def load_badges(filepath: str) -> list[dict]:
    """
    Opens badge_to_login.txt, which looks like:
        10000001,marco.ricci
        10000002,sarah.kumar
        10000003,lucas.berg
    Returns a list of dicts:
        [{"badge_id": "10000001", "login": "marco.ricci"}, ...]
    """
    badges = []                                          # Empty list to accumulate results
    with open(filepath, "r", encoding="utf-8") as f:     # Open file for reading
        for line in f:                                   # For each line in the file...
            parts = line.strip().split(",")              # Strip whitespace, split by comma
            if len(parts) == 2:                          # If it has exactly 2 parts...
                badges.append({                          # Add a dict to the list
                    "badge_id": parts[0].strip(),        # First part = badge ID
                    "login": parts[1].strip()            # Second part = user login
                })
    return badges                                        # Return full list (10 entries)

# =============================================================================
# FUNCTION 2: load_locks
# Reads locks_list.txt and returns a list of lock name strings
# =============================================================================
def load_locks(filepath: str) -> list[str]:
    """
    Opens locks_list.txt, which looks like:
        A01
        A02
        B01
        C01
    Returns a list of strings: ["A01", "A02", ..., "B01", ..., "C01", ...]
    """
    locks = []                                           # Empty list
    with open(filepath, "r", encoding="utf-8") as f:     # Open file
        for line in f:                                   # For each line...
            name = line.strip()                          # Strip whitespace
            if name:                                     # If line is not empty...
                locks.append(name)                       # Add to list
    return locks                                         # Return list (67 entries)

# =============================================================================
# FUNCTION 3: get_category
# Given a lock name (e.g. "A05"), returns its category string
# =============================================================================
def get_category(lock_number: str) -> str:
    """
    Checks the lock name prefix to determine the category:
        "A05" -> "A"   (Red lock)
        "B03"   -> "B"     (Blue / Transitional lock)
        "C12"   -> "C"     (Yellow / Contractor lock)
        "TESTX" -> "OTHER" (unrecognized)
    """
    ln = lock_number.upper()           # Convert to uppercase for safety
    if ln.startswith("A"):           # Starts with "A" -> Red
        return "A"
    elif ln.startswith("B"):           # Starts with "B" -> Blue
        return "B"
    elif ln.startswith("C"):           # Starts with "C" -> Yellow
        return "C"
    return "OTHER"                     # Anything else -> unknown

# =============================================================================
# MAIN FUNCTION: seed
# Reads the 2 config files and populates DynamoDB in 3 steps
# =============================================================================
def seed(site: str, region: str, data_folder: str):
    """
    Args:
        site        = "SITE-001" (site name -> used for table names: SITE-001_loto_locks, etc.)
        region      = "eu-west-1" (the AWS region where resources are deployed)
        data_folder = path to config_data/ folder containing the .txt files
    """
    # --- Connect to DynamoDB ---
    dynamodb = boto3.resource("dynamodb", region_name=region)    # Create DynamoDB client for the target region
    config_table = dynamodb.Table(f"{site}_loto_config")         # Reference to config table
    locks_table = dynamodb.Table(f"{site}_loto_locks")           # Reference to locks state table
    # ═════════════════════════════════════════════════════════════
    # STEP 1: Load the 83 badge mappings into the config table
    # ═════════════════════════════════════════════════════════════
    badge_file = os.path.join(data_folder, "badge_to_login.txt") # Build full file path
    if os.path.exists(badge_file):                               # If file exists...
        badges = load_badges(badge_file)                         # Read all badges
        print(f"Loading {len(badges)} badges into {site}_loto_config...")
        # batch_writer() groups writes into batches of 25 -> much faster
        with config_table.batch_writer() as batch:
            for b in badges:                     # For each badge...
                batch.put_item(Item={            # Write a row to the table
                    "config_type": "badge_map",  # Partition Key: config type
                    "config_key": b["badge_id"], # Sort Key: badge number (e.g. "10000001")
                    "value": b["login"],          # The login (e.g. "marco.ricci")
                    "active": True,              # Active (can be disabled from Admin)
                })
        print(f"  ✅ {len(badges)} badges loaded")
    else:
        print(f"  ❌ {badge_file} not found — cannot seed badges")
        sys.exit(1)                              # ABORT — badges are mandatory
    # ═════════════════════════════════════════════════════════════
    # STEP 2: Load the 67 lock definitions into the config table
    # ═════════════════════════════════════════════════════════════
    locks_file = os.path.join(data_folder, "locks_list.txt")
    if os.path.exists(locks_file):
        locks = load_locks(locks_file)           # Read lock list
        print(f"Loading {len(locks)} locks into {site}_loto_config...")
        with config_table.batch_writer() as batch:
            for lock in locks:                   # For each lock (e.g. "A05")...
                batch.put_item(Item={
                    "config_type": "lock_list",  # Partition Key: type = lock list
                    "config_key": lock,          # Sort Key: lock name
                    "value": get_category(lock), # Computed category (e.g. "A")
                    "active": True,              # Lock is active
                })
        print(f"  ✅ {len(locks)} locks loaded")
        # Print summary to verify counts
        cat_a = sum(1 for l in locks if get_category(l) == "A")
        s = sum(1 for l in locks if get_category(l) == "B")
        c = sum(1 for l in locks if get_category(l) == "C")
        other = sum(1 for l in locks if get_category(l) == "OTHER")
        print(f"    A (Red): {cat_a} | B (Blue): {s} | C (Yellow): {c} | Other: {other}")
        # ═════════════════════════════════════════════════════════════
        # STEP 3: Initialize every lock as "free" in the state table
        # ═════════════════════════════════════════════════════════════
        print(f"Initializing {len(locks)} locks as 'free' in {site}_loto_locks...")
        with locks_table.batch_writer() as batch:
            for lock in locks:
                batch.put_item(Item={
                    "lock_number": lock,             # PK: lock name (e.g. "A05")
                    "status": "free",                # Initial state: available
                    "category": get_category(lock),  # Category (A/B/C)
                    "user": "",                      # Empty — no user has this lock
                    "badge_id": "",                  # Empty — no badge associated
                    "equipment": "",                 # Empty — no equipment locked out
                    "reason": "",                    # Empty — no lockout reason
                    "checkin_time": "",              # Empty — no check-in time
                    "contractor_company": "",        # Empty — only for Yellow locks
                    "contractor_employee": "",       # Empty — only for Yellow locks
                })
        print(f"  ✅ {len(locks)} locks initialized")
    else:
        print(f"  ❌ {locks_file} not found — cannot seed locks")
        sys.exit(1)
    # --- Final message ---
    print(f"\n🎉 Seed complete for site {site}!")
    print(f"   Lock types: A (Red) = badge auth | B (Blue) = badge auth | C (Yellow) = contractor")
    print(f"\n   ⚠️  Next steps — add via Admin Config page:")
    print(f"      - Locations (equipment names)")
    print(f"      - Reasons (lockout reasons)")
    print(f"      - Contractors (company names for Yellow/C locks)")

# =============================================================================
# ENTRY POINT: runs when you execute "python seed_data.py" from terminal
# =============================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed LOTO DynamoDB tables")
    parser.add_argument("--site", default="SITE-001", help="Site name (default: SITE-001)")
    parser.add_argument("--region", default="eu-west-1", help="AWS region (default: eu-west-1)")
    parser.add_argument("--data-folder", default=None,
                        help="Path to folder containing badge_to_login.txt and locks_list.txt")
    args = parser.parse_args()                   # Parse command-line args
    # If --data-folder not specified, default to ../config_data/ relative to this script
    if args.data_folder is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        args.data_folder = os.path.join(script_dir, "..", "config_data")
    print(f"Reading config files from: {os.path.abspath(args.data_folder)}")
    seed(args.site, args.region, args.data_folder)
