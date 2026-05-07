import os
import requests
from datetime import datetime, timedelta
from azure.identity import ClientSecretCredential
import anthropic

# ── CONFIG ───────────────────────────────────────────────────────────────────
SLACK_WEBHOOK_URL   = os.environ["SLACK_WEBHOOK_URL"]
AZURE_TENANT_ID     = os.environ["AZURE_TENANT_ID"]
AZURE_CLIENT_ID     = os.environ["AZURE_CLIENT_ID"]
AZURE_CLIENT_SECRET = os.environ["AZURE_CLIENT_SECRET"]
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]

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
LABELS       = ["Workable Leads", "Listing", "Listing Live", "WL→Live %", "Unique Buyers", "LP/TP", "FLP/TP", "Revenue"]
KEYS         = list(MEASURES.keys())
PCT_METRICS  = {"WL_Live"}
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


def date_ranges():
    today     = datetime.today()
    d1        = today - timedelta(days=1)
    mtd_start = today.replace(day=1)
    # same day last month
    lmtd_end   = (mtd_start - timedelta(days=1)).replace(day=d1.day) if d1.day <= (mtd_start - timedelta(days=1)).day else (mtd_start - timedelta(days=1))
    lmtd_start = lmtd_end.replace(day=1)
    # week: Monday=0
    cw_start   = today - timedelta(days=today.weekday())
    cw_end     = d1
    lw_start   = cw_start - timedelta(days=7)
    lw_end     = cw_end   - timedelta(days=7)
    return {
        "d1": d1, "mtd_start": mtd_start,
        "lmtd_start": lmtd_start, "lmtd_end": lmtd_end,
        "cw_start": cw_start, "cw_end": cw_end,
        "lw_start": lw_start, "lw_end": lw_end,
    }


def measure_row(prefix: str, date_filter: str) -> str:
    return ", ".join(
        f'"{prefix}_{k}", CALCULATE([{v}], {date_filter})'
        for k, v in MEASURES.items()
    )


def fetch_all(token: str) -> dict:
    dr = date_ranges()

    def cal_filter(start: datetime, end: datetime) -> str:
        return (f"FILTER(ALL('CALENDAR'), "
                f"'CALENDAR'[Date] >= DATE({start.year},{start.month},{start.day}) && "
                f"'CALENDAR'[Date] <= DATE({end.year},{end.month},{end.day}))")

    mtd_f  = cal_filter(dr["mtd_start"],   dr["d1"])
    lmtd_f = cal_filter(dr["lmtd_start"],  dr["lmtd_end"])
    d1_f   = cal_filter(dr["d1"],          dr["d1"])
    cw_f   = cal_filter(dr["cw_start"],    dr["cw_end"])
    lw_f   = cal_filter(dr["lw_start"],    dr["lw_end"])

    summary_q = (
        f"EVALUATE ROW("
        f"{measure_row('MTD', mtd_f)}, "
        f"{measure_row('LMTD', lmtd_f)}, "
        f"{measure_row('D1', d1_f)}, "
        f"{measure_row('CW', cw_f)}, "
        f"{measure_row('LW', lw_f)}"
        f")"
    )
    summary = run_dax(token, summary_q)

    dod_start = dr["d1"] - timedelta(days=6)
    cols = ", ".join(f'"{k}", [{v}]' for k, v in MEASURES.items())
    dod_q = (
        f"EVALUATE CALCULATETABLE("
        f"ADDCOLUMNS(VALUES('CALENDAR'[Date]), {cols}), "
        f"'CALENDAR'[Date] >= DATE({dod_start.year},{dod_start.month},{dod_start.day}), "
        f"'CALENDAR'[Date] <= DATE({dr['d1'].year},{dr['d1'].month},{dr['d1'].day})"
        f") ORDER BY 'CALENDAR'[Date] ASC"
    )
    dod = run_dax(token, dod_q)

    # ── Region breakdown (MTD) ───────────────────────────────────────────────
    region_q = (
        f"EVALUATE CALCULATETABLE("
        f"ADDCOLUMNS(VALUES(MASTER_DATA[REGION]), "
        f"\"WL\", CALCULATE([Workable Leads]), "
        f"\"Live\", CALCULATE([LISTING LIVE]), "
        f"\"Revenue\", CALCULATE([Listing Revenue]), "
        f"\"WL_Live\", CALCULATE([Listing Conversion])), "
        f"'CALENDAR'[Date] >= DATE({dr['mtd_start'].year},{dr['mtd_start'].month},{dr['mtd_start'].day}), "
        f"'CALENDAR'[Date] <= DATE({dr['d1'].year},{dr['d1'].month},{dr['d1'].day})"
        f") ORDER BY [Revenue] DESC"
    )
    region_data = run_dax(token, region_q)

    # ── City breakdown (MTD) ─────────────────────────────────────────────────
    city_q = (
        f"EVALUATE CALCULATETABLE("
        f"ADDCOLUMNS(VALUES(MASTER_DATA[CITY_NAME]), "
        f"\"WL\", CALCULATE([Workable Leads]), "
        f"\"Live\", CALCULATE([LISTING LIVE]), "
        f"\"Revenue\", CALCULATE([Listing Revenue]), "
        f"\"WL_Live\", CALCULATE([Listing Conversion])), "
        f"'CALENDAR'[Date] >= DATE({dr['mtd_start'].year},{dr['mtd_start'].month},{dr['mtd_start'].day}), "
        f"'CALENDAR'[Date] <= DATE({dr['d1'].year},{dr['d1'].month},{dr['d1'].day})"
        f") ORDER BY [Revenue] DESC"
    )
    city_data = run_dax(token, city_q)

    return {"summary": summary[0] if summary else {}, "dod": dod, "dr": dr,
            "region_data": region_data, "city_data": city_data}


def get_val(row: dict, prefix: str, key: str):
    target = f"{prefix}_{key}"
    for k, v in row.items():
        if k.endswith(f"[{target}]") or k == target:
            return v
    return None


def get_col(row: dict, key: str):
    for k, v in row.items():
        if k.endswith(f"[{key}]") or k == key:
            return v
    return None


def fmt(key: str, val) -> str:
    if val is None:
        return "—"
    if key in PCT_METRICS:
        return f"{val*100:.1f}%"
    if key in RATIO_METRICS:
        return f"{val:.2f}x"
    if abs(val) >= 1_000_000:
        return f"{val/1_000_000:.2f}M"
    if abs(val) >= 1_000:
        return f"{val:,.0f}"
    return f"{val:.2f}"


def chg(key: str, new_val, old_val) -> str:
    if new_val is None or old_val is None or old_val == 0:
        return "—"
    if key in PCT_METRICS:
        return f"{(new_val - old_val)*100:+.1f}pp"
    pct = ((new_val - old_val) / abs(old_val)) * 100
    return f"{pct:+.1f}%"


def build_summary_block(data: dict) -> str:
    row = data["summary"]
    dr  = data["dr"]

    d = lambda dt: dt.strftime("%d-%b")
    mtd_range  = f"MTD  {d(dr['mtd_start'])}→{d(dr['d1'])}"
    lmtd_range = f"LMTD {d(dr['lmtd_start'])}→{d(dr['lmtd_end'])}"
    cw_range   = f"CW   {d(dr['cw_start'])}→{d(dr['cw_end'])}"
    lw_range   = f"LW   {d(dr['lw_start'])}→{d(dr['lw_end'])}"

    header = (f"{'Metric':<16} {'MTD':>8} {'LMTD':>8} {'Δ':>7}  "
              f"{'D-1':>8}  {'Curr Wk':>8} {'Last Wk':>8} {'Δ':>7}")
    sep    = "─" * len(header)

    lines = [
        f"{mtd_range}  |  D-1: {d(dr['d1'])}  |  {cw_range}  |  {lw_range}",
        "",
        header, sep,
    ]
    for key, label in zip(KEYS, LABELS):
        mtd_v  = get_val(row, "MTD",  key)
        lmtd_v = get_val(row, "LMTD", key)
        d1_v   = get_val(row, "D1",   key)
        cw_v   = get_val(row, "CW",   key)
        lw_v   = get_val(row, "LW",   key)
        lines.append(
            f"{label:<16} {fmt(key,mtd_v):>8} {fmt(key,lmtd_v):>8} {chg(key,mtd_v,lmtd_v):>7}  "
            f"{fmt(key,d1_v):>8}  {fmt(key,cw_v):>8} {fmt(key,lw_v):>8} {chg(key,cw_v,lw_v):>7}"
        )
    return "\n".join(lines)


def build_dod_block(data: dict) -> str:
    rows = data["dod"]
    header = f"{'Date':<9}" + "".join(f"{lbl:>10}" for lbl in ["WL", "Listing", "Live", "WL→Live%", "Buyers", "Revenue"])
    lines  = [header, "─" * len(header)]
    for row in rows:
        # find date value
        date_val = None
        for k, v in row.items():
            if "date" in k.lower() or "Date" in k:
                date_val = v
                break
        try:
            date_str = datetime.fromisoformat(str(date_val)).strftime("%d-%b")
        except Exception:
            date_str = str(date_val)[:9]

        vals = [
            fmt("WL",      get_col(row, "WL")),
            fmt("Listing", get_col(row, "Listing")),
            fmt("Live",    get_col(row, "Live")),
            fmt("WL_Live", get_col(row, "WL_Live")),
            fmt("Buyers",  get_col(row, "Buyers")),
            fmt("Revenue", get_col(row, "Revenue")),
        ]
        lines.append(f"{date_str:<9}" + "".join(f"{v:>10}" for v in vals))
    return "\n".join(lines)


def get_ai_insights(data: dict) -> str:
    row    = data["summary"]
    dr     = data["dr"]
    d      = lambda dt: dt.strftime("%d-%b")

    def sv(prefix, key):
        v = get_val(row, prefix, key)
        return fmt(key, v) if v is not None else "—"

    def sc(key, p1, p2):
        return chg(key, get_val(row, p1, key), get_val(row, p2, key))

    # top 3 / bottom 3 regions by revenue
    valid_regions = [r for r in data["region_data"] if get_col(r, "Revenue")]
    top_regions    = valid_regions[:3]
    bottom_regions = valid_regions[-3:][::-1]

    # top 3 / bottom 3 cities by revenue
    valid_cities  = [c for c in data["city_data"] if get_col(c, "Revenue")]
    top_cities    = valid_cities[:3]
    bottom_cities = valid_cities[-3:][::-1]

    def region_name(r): return get_col(r, "REGION") or get_col(r, "MASTER_DATA[REGION]") or "?"
    def city_name(c):   return get_col(c, "CITY_NAME") or get_col(c, "MASTER_DATA[CITY_NAME]") or "?"
    def rev(r):         return fmt("Revenue", get_col(r, "Revenue"))
    def live(r):        return fmt("Live", get_col(r, "Live"))
    def conv(r):        return fmt("WL_Live", get_col(r, "WL_Live"))

    prompt = f"""You are a senior analyst for Cars24 C2C Marketplace. Analyze this daily data and give sharp, actionable insights.

DATE: {d(dr['d1'])} (D-1)
MTD period: {d(dr['mtd_start'])} to {d(dr['d1'])}
LMTD period: {d(dr['lmtd_start'])} to {d(dr['lmtd_end'])}

NATIONAL SUMMARY:
Metric         MTD       LMTD      Δ        D-1       Curr Wk   Last Wk   Δ
Workable Leads {sv('MTD','WL')}  {sv('LMTD','WL')}  {sc('WL','MTD','LMTD')}  {sv('D1','WL')}  {sv('CW','WL')}  {sv('LW','WL')}  {sc('WL','CW','LW')}
Listing        {sv('MTD','Listing')}  {sv('LMTD','Listing')}  {sc('Listing','MTD','LMTD')}  {sv('D1','Listing')}  {sv('CW','Listing')}  {sv('LW','Listing')}  {sc('Listing','CW','LW')}
Listing Live   {sv('MTD','Live')}  {sv('LMTD','Live')}  {sc('Live','MTD','LMTD')}  {sv('D1','Live')}  {sv('CW','Live')}  {sv('LW','Live')}  {sc('Live','CW','LW')}
WL→Live %      {sv('MTD','WL_Live')}  {sv('LMTD','WL_Live')}  {sc('WL_Live','MTD','LMTD')}  {sv('D1','WL_Live')}  {sv('CW','WL_Live')}  {sv('LW','WL_Live')}  {sc('WL_Live','CW','LW')}
Unique Buyers  {sv('MTD','Buyers')}  {sv('LMTD','Buyers')}  {sc('Buyers','MTD','LMTD')}  {sv('D1','Buyers')}  {sv('CW','Buyers')}  {sv('LW','Buyers')}  {sc('Buyers','CW','LW')}
Revenue        {sv('MTD','Revenue')}  {sv('LMTD','Revenue')}  {sc('Revenue','MTD','LMTD')}  {sv('D1','Revenue')}  {sv('CW','Revenue')}  {sv('LW','Revenue')}  {sc('Revenue','CW','LW')}

TOP 3 REGIONS (MTD by Revenue):
{chr(10).join(f"{i+1}. {region_name(r)} — Revenue: {rev(r)}, Live: {live(r)}, WL→Live: {conv(r)}" for i, r in enumerate(top_regions))}

BOTTOM 3 REGIONS (MTD by Revenue):
{chr(10).join(f"{i+1}. {region_name(r)} — Revenue: {rev(r)}, Live: {live(r)}, WL→Live: {conv(r)}" for i, r in enumerate(bottom_regions))}

TOP 3 CITIES (MTD by Revenue):
{chr(10).join(f"{i+1}. {city_name(c)} — Revenue: {rev(c)}, Live: {live(c)}, WL→Live: {conv(c)}" for i, c in enumerate(top_cities))}

BOTTOM 3 CITIES (MTD by Revenue):
{chr(10).join(f"{i+1}. {city_name(c)} — Revenue: {rev(c)}, Live: {live(c)}, WL→Live: {conv(c)}" for i, c in enumerate(bottom_cities))}

Write a concise daily insight report with 3 sections:
1. *National Overview* — what's the overall health, key concern, and bright spot (3-4 lines)
2. *Region Spotlight* — top and bottom regions, what's driving it (3-4 lines)
3. *City Spotlight* — top and bottom cities, any notable patterns (3-4 lines)

Be specific with numbers. Use plain English. No bullet points — write in paragraph form."""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp   = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text


def send_slack(data: dict):
    date_str = datetime.now().strftime("%d %b %Y")
    summary  = build_summary_block(data)
    dod      = build_dod_block(data)
    insights = get_ai_insights(data)

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"C2C Dashboard Report — {date_str}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*📊 Summary (MTD vs LMTD | D-1 | Week vs Last Week)*\n```{summary}```"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*📈 Last 7 Days (Day over Day)*\n```{dod}```"}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*🤖 AI Insights — National | Region | City*\n{insights}"}},
    ]

    resp = requests.post(SLACK_WEBHOOK_URL, json={"blocks": blocks})
    if resp.status_code != 200:
        print(f"Slack error: {resp.status_code} — {resp.text}")
    else:
        print("Sent to Slack successfully.")


def main():
    print("Getting token...")
    token = get_token()
    print("Fetching metrics...")
    data = fetch_all(token)
    print("Sending to Slack...")
    send_slack(data)


if __name__ == "__main__":
    main()
