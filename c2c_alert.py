import os
import requests
from datetime import datetime
from azure.identity import ClientSecretCredential

# ── CONFIG ───────────────────────────────────────────────────────────────────
SLACK_WEBHOOK_URL   = os.environ["SLACK_WEBHOOK_URL"]
AZURE_TENANT_ID     = os.environ["AZURE_TENANT_ID"]
AZURE_CLIENT_ID     = os.environ["AZURE_CLIENT_ID"]
AZURE_CLIENT_SECRET = os.environ["AZURE_CLIENT_SECRET"]

PBI_DATASET_ID = "398f2429-ae2f-4704-b784-c3a345d9d6a5"

MEASURES = {
    "WL":      "Workable Leads",
    "Listing": "LISTING",
    "Live":    "LISTING LIVE",
    "WL_Live": "Listing Conversion",
    "Buyers":  "Unique Buyers",
    "LP_TP":   "LP/TP",
    "FLP_TP":  "FLP/TP",
    "Revenue": "Listing Revenue",
}

LABELS = {
    "WL":      "Workable Leads",
    "Listing": "Listing",
    "Live":    "Listing Live",
    "WL_Live": "WL→Live %",
    "Buyers":  "Unique Buyers",
    "LP_TP":   "LP/TP",
    "FLP_TP":  "FLP/TP",
    "Revenue": "Revenue",
}

# metrics shown as % (multiply by 100)
PCT_METRICS  = {"WL_Live"}
# metrics shown as ratio (2 decimal places)
RATIO_METRICS = {"LP_TP", "FLP_TP"}
# ─────────────────────────────────────────────────────────────────────────────


def get_token():
    cred = ClientSecretCredential(AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET)
    return cred.get_token("https://analysis.windows.net/powerbi/api/.default").token


def run_dax(token: str, query: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url  = f"https://api.powerbi.com/v1.0/myorg/datasets/{PBI_DATASET_ID}/executeQueries"
    body = {"queries": [{"query": query}], "serializerSettings": {"includeNulls": True}}
    resp = requests.post(url, headers=headers, json=body)
    resp.raise_for_status()
    return resp.json()["results"][0]["tables"][0].get("rows", [])


def measure_row(alias_prefix: str, date_filter: str) -> str:
    cols = ", ".join(
        f'"{alias_prefix}_{k}", CALCULATE([{v}], {date_filter})'
        for k, v in MEASURES.items()
    )
    return cols


def fetch_all(token: str) -> dict:
    # ── MTD vs LMTD ──────────────────────────────────────────────────────────
    mtd_filter  = "FILTER(ALL('CALENDAR'), 'CALENDAR'[Date] >= DATE(YEAR(TODAY()), MONTH(TODAY()), 1) && 'CALENDAR'[Date] <= TODAY()-1)"
    lmtd_filter = "FILTER(ALL('CALENDAR'), 'CALENDAR'[Date] >= EOMONTH(TODAY(),-2)+1 && 'CALENDAR'[Date] <= EDATE(TODAY()-1,-1))"

    mtd_lmtd = run_dax(token, f"EVALUATE ROW({measure_row('MTD', mtd_filter)}, {measure_row('LMTD', lmtd_filter)})")

    # ── D-1 ──────────────────────────────────────────────────────────────────
    d1_filter = "FILTER(ALL('CALENDAR'), 'CALENDAR'[Date] = TODAY()-1)"
    d1 = run_dax(token, f"EVALUATE ROW({measure_row('D1', d1_filter)})")

    # ── Current Week vs Last Week ─────────────────────────────────────────────
    # Week starts Monday; CW = Mon this week to D-1, LW = same span previous week
    cw_filter = "FILTER(ALL('CALENDAR'), 'CALENDAR'[Date] >= TODAY() - WEEKDAY(TODAY(),2) + 1 && 'CALENDAR'[Date] <= TODAY()-1)"
    lw_filter = "FILTER(ALL('CALENDAR'), 'CALENDAR'[Date] >= TODAY() - WEEKDAY(TODAY(),2) - 6 && 'CALENDAR'[Date] <= TODAY() - WEEKDAY(TODAY(),2) - 1 + (TODAY()-1 - (TODAY() - WEEKDAY(TODAY(),2) + 1)))"
    wk = run_dax(token, f"EVALUATE ROW({measure_row('CW', cw_filter)}, {measure_row('LW', lw_filter)})")

    # ── Last 7 days DoD ───────────────────────────────────────────────────────
    cols_dod = ", ".join(f'"{k}", [{v}]' for k, v in MEASURES.items())
    dod = run_dax(token, f"""
        EVALUATE
        CALCULATETABLE(
            ADDCOLUMNS(VALUES('CALENDAR'[Date]), {cols_dod}),
            'CALENDAR'[Date] >= TODAY()-7,
            'CALENDAR'[Date] <= TODAY()-1
        )
        ORDER BY 'CALENDAR'[Date] ASC
    """)

    return {"mtd_lmtd": mtd_lmtd[0] if mtd_lmtd else {}, "d1": d1[0] if d1 else {},
            "wk": wk[0] if wk else {}, "dod": dod}


def fmt(key: str, val) -> str:
    if val is None:
        return "—"
    if key in PCT_METRICS:
        return f"{val*100:.1f}%"
    if key in RATIO_METRICS:
        return f"{val:.2f}x"
    if isinstance(val, float):
        return f"{val:,.0f}"
    return f"{int(val):,}"


def pct_chg(key: str, new, old) -> str:
    if not old or old == 0:
        return "—"
    if key in PCT_METRICS:
        diff = (new - old) * 100
        return f"{diff:+.1f}pp"
    chg = ((new - old) / abs(old)) * 100
    return f"{chg:+.1f}%"


def get_val(row: dict, prefix: str, key: str):
    for k, v in row.items():
        if k.endswith(f"[{prefix}_{key}]") or k == f"{prefix}_{key}":
            return v
    return None


def get_dod_val(row: dict, key: str):
    for k, v in row.items():
        if k.endswith(f"[{key}]") or k == key:
            return v
    return None


def build_mtd_block(data: dict) -> str:
    row = data["mtd_lmtd"]
    lines = [f"{'Metric':<18} {'MTD':>10} {'LMTD':>10} {'Chg':>8}"]
    lines.append("─" * 50)
    for k in MEASURES:
        mtd_val  = get_val(row, "MTD",  k)
        lmtd_val = get_val(row, "LMTD", k)
        lines.append(f"{LABELS[k]:<18} {fmt(k, mtd_val):>10} {fmt(k, lmtd_val):>10} {pct_chg(k, mtd_val, lmtd_val):>8}")
    return "\n".join(lines)


def build_d1_block(data: dict) -> str:
    row = data["d1"]
    lines = [f"{'Metric':<18} {'D-1 Value':>12}"]
    lines.append("─" * 32)
    for k in MEASURES:
        val = get_val(row, "D1", k)
        lines.append(f"{LABELS[k]:<18} {fmt(k, val):>12}")
    return "\n".join(lines)


def build_week_block(data: dict) -> str:
    row = data["wk"]
    lines = [f"{'Metric':<18} {'Curr Wk':>10} {'Last Wk':>10} {'Chg':>8}"]
    lines.append("─" * 50)
    for k in MEASURES:
        cw_val = get_val(row, "CW", k)
        lw_val = get_val(row, "LW", k)
        lines.append(f"{LABELS[k]:<18} {fmt(k, cw_val):>10} {fmt(k, lw_val):>10} {pct_chg(k, cw_val, lw_val):>8}")
    return "\n".join(lines)


def build_dod_block(data: dict) -> str:
    rows = data["dod"]
    # Show key metrics only for readability: WL, Live, Revenue, WL_Live
    keys = ["WL", "Live", "WL_Live", "Revenue"]
    header = f"{'Date':<10}" + "".join(f"{LABELS[k]:>12}" for k in keys)
    lines  = [header, "─" * (10 + 12 * len(keys))]
    for row in rows:
        date_val = get_dod_val(row, "Date") or get_dod_val(row, "CALENDAR[Date]") or ""
        try:
            date_str = datetime.fromisoformat(str(date_val)).strftime("%d-%b")
        except Exception:
            date_str = str(date_val)[:10]
        line = f"{date_str:<10}" + "".join(f"{fmt(k, get_dod_val(row, k)):>12}" for k in keys)
        lines.append(line)
    return "\n".join(lines)


def send_slack(data: dict):
    date_str = datetime.now().strftime("%d %b %Y")

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"C2C Dashboard Report — {date_str}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*📊 MTD vs LMTD*\n```" + build_mtd_block(data) + "```"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*📅 D-1 (Yesterday)*\n```" + build_d1_block(data) + "```"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*📆 Current Week vs Last Week*\n```" + build_week_block(data) + "```"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*📈 Last 7 Days (DoD)*\n```" + build_dod_block(data) + "```"}},
    ]

    resp = requests.post(SLACK_WEBHOOK_URL, json={"blocks": blocks})
    if resp.status_code != 200:
        print(f"Slack error: {resp.status_code} — {resp.text}")
    else:
        print("Sent to Slack successfully.")


def main():
    print("Getting token...")
    token = get_token()
    print("Fetching all metrics...")
    data = fetch_all(token)
    print("Sending to Slack...")
    send_slack(data)


if __name__ == "__main__":
    main()
