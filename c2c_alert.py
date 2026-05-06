import os
import requests
from datetime import datetime
import anthropic
import pyadomd
from azure.identity import ClientSecretCredential

# ── CONFIG (all values come from environment variables / GitHub Secrets) ─────
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

AZURE_TENANT_ID   = os.environ["AZURE_TENANT_ID"]    # Directory (tenant) ID
AZURE_CLIENT_ID   = os.environ["AZURE_CLIENT_ID"]    # Application (client) ID
AZURE_CLIENT_SECRET = os.environ["AZURE_CLIENT_SECRET"]  # Client secret value

PBI_DATA_SOURCE = "powerbi://api.powerbi.com/v1.0/myorg/C2C_MarketPlace"
PBI_CATALOG     = "Cars24_C2C_MarketPlace"

# Alert thresholds — tweak as needed
THRESHOLDS = {
    "listing_live_drop_pct":    20,   # alert if Listing Live drops >20% vs yesterday
    "revenue_drop_pct":         20,   # alert if Revenue drops >20% vs yesterday
    "offer_acceptance_drop_pct": 5,   # alert if Offer Acceptance % drops >5 percentage points
}
# ────────────────────────────────────────────────────────────────────────────


def get_pbi_token():
    credential = ClientSecretCredential(
        tenant_id=AZURE_TENANT_ID,
        client_id=AZURE_CLIENT_ID,
        client_secret=AZURE_CLIENT_SECRET
    )
    token = credential.get_token("https://analysis.windows.net/powerbi/api/.default")
    return token.token


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

    prev = rows[-2]
    today = rows[-1]

    def pct_change(new, old):
        if not old:
            return 0
        return ((new - old) / old) * 100

    ll_chg   = pct_change(today.get("Listing Live") or 0,       prev.get("Listing Live") or 0)
    rev_chg  = pct_change(today.get("Listing Revenue") or 0,    prev.get("Listing Revenue") or 0)
    oa_today = (today.get("Offer Acceptance %") or 0) * 100
    oa_prev  = (prev.get("Offer Acceptance %") or 0) * 100
    oa_diff  = oa_today - oa_prev

    alert_triggered = (
        ll_chg  < -THRESHOLDS["listing_live_drop_pct"]        or
        rev_chg < -THRESHOLDS["revenue_drop_pct"]             or
        oa_diff < -THRESHOLDS["offer_acceptance_drop_pct"]
    )

    summary = f"""
C2C Dashboard — Daily Metrics Comparison
Date: {datetime.now().strftime('%Y-%m-%d')}

             Yesterday       Today          Change
Listing Live  {int(prev.get('Listing Live') or 0):>10,}   {int(today.get('Listing Live') or 0):>10,}   {ll_chg:+.1f}%
Revenue       {prev.get('Listing Revenue') or 0:>12,.0f}   {today.get('Listing Revenue') or 0:>12,.0f}   {rev_chg:+.1f}%
Offer Accept  {oa_prev:>9.1f}%   {oa_today:>9.1f}%   {oa_diff:+.1f}pp
""".strip()

    return summary, alert_triggered


def get_ai_insight(summary: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": (
                "You are a C2C automotive marketplace analyst. "
                "Analyze the following daily metrics and provide 2-3 concise insights. "
                "Flag any anomalies and suggest a likely cause if visible in the data.\n\n"
                f"{summary}"
            )
        }]
    )
    return response.content[0].text


def send_slack_alert(summary: str, insight: str, is_alert: bool):
    color  = "#ff4444" if is_alert else "#36a64f"
    prefix = "🚨 *ALERT* — Metrics need attention" if is_alert else "✅ *Daily C2C Dashboard Report*"

    payload = {
        "attachments": [
            {
                "color": color,
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": prefix}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"```{summary}```"}},
                    {"type": "divider"},
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*AI Insight:*\n{insight}"}},
                ]
            }
        ]
    }

    resp = requests.post(SLACK_WEBHOOK_URL, json=payload)
    if resp.status_code != 200:
        print(f"Slack error: {resp.status_code} — {resp.text}")
    else:
        print("Slack alert sent successfully.")


def main():
    print("Fetching Power BI token...")
    token = get_pbi_token()

    print("Querying metrics from Power BI...")
    rows = fetch_metrics(token)

    print("Building summary...")
    summary, is_alert = build_summary(rows)
    print(summary)

    print("Getting AI insight from Claude...")
    insight = get_ai_insight(summary)
    print(f"\nInsight:\n{insight}")

    print("\nSending to Slack...")
    send_slack_alert(summary, insight, is_alert)


if __name__ == "__main__":
    main()
