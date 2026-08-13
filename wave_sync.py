#!/usr/bin/env python3
"""
wave_sync.py — Pulls live account balances from Wave (waveapps.com) for
Tiff Roze LLC and writes them into fleet_dashboard_v2.html.

Run manually:  python3 wave_sync.py
Run on a schedule via the Cowork scheduled-tasks feature (weekly).

What it does each run:
  1. Reads credentials from wave_config.json (same folder).
  2. Queries Wave's GraphQL API for CASH_AND_BANK, INCOME, and EXPENSE
     accounts and their current balances.
  3. Saves a timestamped snapshot to wave_snapshots/.
  4. Compares to the most recent prior snapshot to compute a "weekly
     delta" (net movement in each category since last run) and appends
     it to wave_snapshots/history.json (capped at the last 26 entries).
  5. Rewrites the `let WAVE = ...;` block inside fleet_dashboard_v2.html
     (between the WAVE_DATA_START / WAVE_DATA_END markers) so the
     dashboard shows fresh data next time it's opened in a browser.

Wave's public API only exposes current cumulative balances per account,
not a transaction history — so "weekly delta" here is computed by
diffing successive snapshots, not pulled directly from Wave.
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "wave_config.json")
SNAPSHOT_DIR = os.path.join(SCRIPT_DIR, "wave_snapshots")
HISTORY_PATH = os.path.join(SNAPSHOT_DIR, "history.json")
DASHBOARD_PATH = os.path.join(SCRIPT_DIR, "index.html")

GRAPHQL_URL = "https://gql.waveapps.com/graphql/public"

QUERY = """
query GetBalances($id: ID!) {
  business(id: $id) {
    bank: accounts(types: [ASSET]) {
      edges { node { id name subtype { name } balance } }
    }
    income: accounts(types: [INCOME]) {
      edges { node { id name subtype { name } balance } }
    }
    expense: accounts(types: [EXPENSE]) {
      edges { node { id name subtype { name } balance } }
    }
  }
}
"""


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def run_query(token, business_id):
    body = json.dumps({"query": QUERY, "variables": {"id": business_id}}).encode()
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Wave API HTTP error {e.code}: {e.read().decode()}")
    if "errors" in payload:
        raise RuntimeError(f"Wave API returned errors: {payload['errors']}")
    return payload["data"]["business"]


def extract_accounts(edges):
    out = []
    for e in edges:
        node = e["node"]
        out.append({
            "id": node["id"],
            "name": node["name"],
            "subtype": node["subtype"]["name"] if node.get("subtype") else None,
            "balance": float(node["balance"]),
        })
    return out


def is_bank_account(acct):
    return acct["subtype"] in ("Cash & Bank",)


def load_history():
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH) as f:
            return json.load(f)
    return []


def save_history(history):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    with open(HISTORY_PATH, "w") as f:
        json.dump(history[-26:], f, indent=2)  # keep ~6 months of weekly runs


def sum_balances(accounts):
    return sum(a["balance"] for a in accounts)


def compute_delta(prev_accounts, curr_accounts):
    """Net change in total balance between two snapshots of the same account list."""
    prev_by_id = {a["id"]: a["balance"] for a in prev_accounts}
    delta = 0.0
    for a in curr_accounts:
        delta += a["balance"] - prev_by_id.get(a["id"], 0.0)
    return delta


def main():
    cfg = load_config()
    token = cfg["access_token"]
    business_id = cfg["business_id"]

    print(f"Querying Wave for business {cfg.get('business_name', business_id)}...")
    data = run_query(token, business_id)

    bank_all = extract_accounts(data["bank"]["edges"])
    bank = [a for a in bank_all if is_bank_account(a)]
    income = extract_accounts(data["income"]["edges"])
    expense = extract_accounts(data["expense"]["edges"])

    now = datetime.now(timezone.utc)
    snapshot = {
        "timestamp": now.isoformat(),
        "bank": bank,
        "income": income,
        "expense": expense,
    }

    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    snap_filename = f"snapshot_{now.strftime('%Y-%m-%dT%H%M%S')}.json"
    with open(os.path.join(SNAPSHOT_DIR, snap_filename), "w") as f:
        json.dump(snapshot, f, indent=2)
    print(f"Saved snapshot: {snap_filename}")

    history = load_history()
    if history:
        prev = history[-1]
        income_delta = compute_delta(prev["snapshot"]["income"], income)
        expense_delta = compute_delta(prev["snapshot"]["expense"], expense)
        history.append({
            "weekEnding": now.strftime("%Y-%m-%d"),
            "incomeDelta": round(income_delta, 2),
            "expenseDelta": round(expense_delta, 2),
            "snapshot": snapshot,
        })
    else:
        # First run ever — no prior snapshot to diff against.
        history.append({
            "weekEnding": now.strftime("%Y-%m-%d"),
            "incomeDelta": 0.0,
            "expenseDelta": 0.0,
            "snapshot": snapshot,
        })
    save_history(history)

    # Build the WAVE JS object for the dashboard.
    weekly_deltas = [
        {"weekEnding": h["weekEnding"], "incomeDelta": h["incomeDelta"], "expenseDelta": h["expenseDelta"]}
        for h in history
    ]

    wave_obj = {
        "lastSynced": now.isoformat(),
        "accounts": {
            "bank": bank,
            "income": income,
            "expense": expense,
        },
        "weeklyDeltas": weekly_deltas,
    }

    wave_js = "let WAVE = " + json.dumps(wave_obj, indent=2) + ";"

    with open(DASHBOARD_PATH, "r") as f:
        html = f.read()

    pattern = re.compile(
        r"(// WAVE_DATA_START\n)(.*?)(\n\s*// WAVE_DATA_END)",
        re.DOTALL,
    )
    if not pattern.search(html):
        raise RuntimeError("Could not find WAVE_DATA_START/END markers in dashboard HTML.")

    new_html = pattern.sub(lambda m: m.group(1) + wave_js + m.group(3), html, count=1)

    with open(DASHBOARD_PATH, "w") as f:
        f.write(new_html)

    cash_total = sum_balances(bank)
    print(f"Dashboard updated. Live cash/bank balance: ${cash_total:,.2f}")
    print(f"Categorized income (LTD): ${sum_balances([a for a in income if 'uncategorized' not in a['name'].lower()]):,.2f}")
    print(f"Categorized expense (LTD): ${sum_balances([a for a in expense if 'uncategorized' not in a['name'].lower()]):,.2f}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
