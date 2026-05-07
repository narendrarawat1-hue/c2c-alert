import os
import requests
from datetime import datetime
from azure.identity import ClientSecretCredential

# ── CONFIG ───────────────────────────────────────────────────────────────────
SLACK_WEBHOOK_URL   = os.environ["SLACK_WEBHOOK_URL"]
AZURE_TENANT_ID     = os.environ["AZURE_TENANT_ID"]
AZURE_CLIENT_ID     = os.environ["AZURE_CLIENT_ID"]
AZURE_CLIENT_SECRET = os.environ["AZURE_CLIENT_SECRET"]

PBI_WORKSPACE_NAME = "C2C_MarketPlace"
PBI_DATASET_NAME   = "Cars24_C2C_MarketPlace"

THRESHOLDS = {
    "listing_live_drop_pct":     20,
    "revenue_drop_pct":          20,
    "offer_acceptance_drop_pct":  5,
}
# ─────────────────────────────────────────────────────────────────────────────


def get_token():
    credential = ClientSecretCredential(
        tenant_id=AZURE_TENANT_ID,
        client_id=AZURE_CLIENT_ID,
        client_secret=AZURE_CLIENT_SECRET,
    )
    return credential.get_token("https://analysis.windows.net/powerbi/api/.default").token


def get_dataset_id(token: str) -> tuple[str, str]:
    headers = {"Authorization": f"Bearer {token}"}

    groups = requests.get("https://api.powerbi.com/v1.0/myorg/groups", headers=headers).json()
    group_id = next(g["id"] for g in groups["value"] if g["name"] == PBI_WORKSPACE_NAME)

    datasets = requests.get(f"https://api.powerbi.com/v1.0/myorg/groups/{group_id}/datasets", headers=headers).json()
    dataset_id = next(d["id"] for d in datasets["value"] if d["name"] == PBI_DATASET_NAME)

    return group_id, dataset_id


def run_dax(token: str, group_id: str, dataset_id: str, query: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"https://api.powerbi.com/v1.0/myorg/groups/{group_id}/datasets/{dataset_id}/executeQueries"
    body = {"queries": [{"query": query}], "serializerSettings": {"includeNulls": True}}

    resp = requests.post(url, headers=headers, json=body)
    resp.raise_for_status()

    rows = resp.json()["results"][0]["tables"][0].get("rows", [])
    return rows


def fetch_metrics(token: str, group_id: str, dataset_id: str) -> list[dict]:
    dax = """
    EVALUATE
    CALCULATETABLE(
        ADDCOLUMNS(
            VALUES('CALENDAR'[Date]),
            "Listing Live",       [LISTING LIVE],
            "Listing Revenue",    [Listing Revenue],
            "Offer Acceptance",   [Offer Acceptance %]
        ),
        'CALENDAR'[Date] >= TODAY() - 2,
        'CALENDAR'[Date] <= TODAY()
    )
    ORDER BY 'CALENDAR'[Date] ASC
    """
    return run_dax(token, group_id, dataset_id, dax)


def build_summary(rows: list[dict]) -> tuple[str, bool]:
    if len(rows) < 2:
        return "Not enough data for comparison (need at least 2 days).", False

    prev  = rows[-2]
    today = rows[-1]

    def pct_change(new, old):
        return ((new - old) / old) * 100 if old else 0

    # DAX REST API prefixes column names with table name
    def get(row, key):
        for k, v in row.items():
            if k.endswith(f"[{key}]") or k == key:
                return v or 0
        return 0

    ll_prev  = get(prev,  "Listing Live")
    ll_today = get(today, "Listing Live")
    rv_prev  = get(prev,  "Listing Revenue")
    rv_today = get(today, "Listing Revenue")
    oa_prev  = get(prev,  "Offer Acceptance") * 100
    oa_today = get(today, "Offer Acceptance") * 100

    ll_chg  = pct_change(ll_today, ll_prev)
    rev_chg = pct_change(rv_today, rv_prev)
    oa_diff = oa_today - oa_prev

    alert_triggered = (
        ll_chg  < -THRESHOLDS["listing_live_drop_pct"]        or
        rev_chg < -THRESHOLDS["revenue_drop_pct"]             or
        oa_diff < -THRESHOLDS["offer_acceptance_drop_pct"]
    )

    summary = (
        f"C2C Dashboard — Daily Metrics\n"
        f"Date: {datetime.now().strftime('%d %b %Y')}\n\n"
        f"{'Metric':<20} {'Yesterday':>12} {'Today':>12} {'Change':>10}\n"
        f"{'-'*56}\n"
        f"{'Listing Live':<20} {int(ll_prev):>12,} {int(ll_today):>12,} {ll_chg:>+9.1f}%\n"
        f"{'Listing Revenue':<20} {rv_prev:>12,.0f} {rv_today:>12,.0f} {rev_chg:>+9.1f}%\n"
        f"{'Offer Acceptance':<20} {oa_prev:>11.1f}% {oa_today:>11.1f}% {oa_diff:>+8.1f}pp\n"
    )

    return summary, alert_triggered


def send_slack_alert(summary: str, is_alert: bool):
    color  = "#ff4444" if is_alert else "#36a64f"
    header = ":rotating_light: *ALERT — Metrics need attention*" if is_alert else ":white_check_mark: *Daily C2C Dashboard Report*"

    payload = {
        "attachments": [{
            "color": color,
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": header}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"```{summary}```"}},
            ]
        }]
    }

    resp = requests.post(SLACK_WEBHOOK_URL, json=payload)
    if resp.status_code != 200:
        print(f"Slack error: {resp.status_code} — {resp.text}")
    else:
        print("Slack alert sent successfully.")


def main():
    print("Getting token...")
    token = get_token()

    print("Finding workspace and dataset...")
    group_id, dataset_id = get_dataset_id(token)
    print(f"Workspace: {group_id} | Dataset: {dataset_id}")

    print("Querying metrics...")
    rows = fetch_metrics(token, group_id, dataset_id)
    print(f"Got {len(rows)} rows")

    print("Building summary...")
    summary, is_alert = build_summary(rows)
    print(summary)

    print("Sending to Slack...")
    send_slack_alert(summary, is_alert)


if __name__ == "__main__":
    main()
