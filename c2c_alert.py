import os
import requests
from datetime import datetime
import pyadomd
from azure.identity import ClientSecretCredential

# ── CONFIG (all values come from environment variables / GitHub Secrets) ─────
SLACK_WEBHOOK_URL   = os.environ["SLACK_WEBHOOK_URL"]
AZURE_TENANT_ID     = os.environ["AZURE_TENANT_ID"]
AZURE_CLIENT_ID     = os.environ["AZURE_CLIENT_ID"]
AZURE_CLIENT_SECRET = os.environ["AZURE_CLIENT_SECRET"]

PBI_DATA_SOURCE = "powerbi://api.powerbi.com/v1.0/myorg/C2C_MarketPlace"
PBI_CATALOG     = "Cars24_C2C_MarketPlace"

THRESHOLDS = {
    "listing_live_drop_pct":     20,
    "revenue_drop_pct":          20,
    "offer_acceptance_drop_pct":  5,
}
# ─────────────────────────────────────────────────────────────────────────────


def get_pbi_token():
    credential = ClientSecretCredential(
        tenant_id=AZURE_TENANT_ID,
        client_id=AZURE_CLIENT_ID,
        client_secret=AZURE_CLIENT_SECRET,
    )
    return credential.get_token("https://analysis.windows.net/powerbi/api/.default").token


def fetch_metrics(token: str) -> list[dict]:
    conn_str = (
        "Provider=MSOLAP;"
        f"Data Source={PBI_DATA_SOURCE};"
        f"Initial Catalog={PBI_CATALOG};"
        f"Password={token};"
        "Persist Security Info=True;"
        "Impersonation Level=Impersonate;"
    )
    dax = """
    EVALUATE
    CALCULATETABLE(
        ADDCOLUMNS(
            VALUES('CALENDAR'[Date]),
            "Listing Live",       [LISTING LIVE],
            "Listing Revenue",    [Listing Revenue],
            "Offer Acceptance %", [Offer Acceptance %]
        ),
        'CALENDAR'[Date] >= TODAY() - 2,
        'CALENDAR'[Date] <= TODAY()
    )
    ORDER BY 'CALENDAR'[Date] ASC
    """
    rows = []
    with pyadomd.Pyadomd(conn_str) as conn:
        with conn.cursor().execute(dax) as cur:
            cols = [c[0] for c in cur.description]
            for row in cur.fetchall():
                rows.append(dict(zip(cols, row)))
    return rows


def build_summary(rows: list[dict]) -> tuple[str, bool]:
    if len(rows) < 2:
        return "Not enough data for comparison (need at least 2 days).", False

    prev  = rows[-2]
    today = rows[-1]

    def pct_change(new, old):
        return ((new - old) / old) * 100 if old else 0

    ll_chg  = pct_change(today.get("Listing Live") or 0,    prev.get("Listing Live") or 0)
    rev_chg = pct_change(today.get("Listing Revenue") or 0, prev.get("Listing Revenue") or 0)
    oa_today = (today.get("Offer Acceptance %") or 0) * 100
    oa_prev  = (prev.get("Offer Acceptance %") or 0) * 100
    oa_diff  = oa_today - oa_prev

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
        f"{'Listing Live':<20} {int(prev.get('Listing Live') or 0):>12,} {int(today.get('Listing Live') or 0):>12,} {ll_chg:>+9.1f}%\n"
        f"{'Listing Revenue':<20} {prev.get('Listing Revenue') or 0:>12,.0f} {today.get('Listing Revenue') or 0:>12,.0f} {rev_chg:>+9.1f}%\n"
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
    print("Fetching Power BI token...")
    token = get_pbi_token()

    print("Querying metrics...")
    rows = fetch_metrics(token)

    print("Building summary...")
    summary, is_alert = build_summary(rows)
    print(summary)

    print("Sending to Slack...")
    send_slack_alert(summary, is_alert)


if __name__ == "__main__":
    main()
