import os
import io
import requests
from datetime import datetime, timedelta
from azure.identity import ClientSecretCredential
import anthropic
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.gridspec import GridSpec

# ── CONFIG ───────────────────────────────────────────────────────────────────
SLACK_WEBHOOK_URL   = os.environ["SLACK_WEBHOOK_URL"]
SLACK_BOT_TOKEN     = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL       = "C0B2UAP9D7S"   # #c2c_marketplace_alerts
AZURE_TENANT_ID     = os.environ["AZURE_TENANT_ID"]
AZURE_CLIENT_ID     = os.environ["AZURE_CLIENT_ID"]
AZURE_CLIENT_SECRET = os.environ["AZURE_CLIENT_SECRET"]
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]

PBI_DATASET_ID  = "398f2429-ae2f-4704-b784-c3a345d9d6a5"
PBI_GROUP_ID    = "d5747b7a-5967-49ab-a21c-f61a095bb063"
PBI_REPORT_ID   = "4b39cd5b-6872-49e9-bca9-7637c9b35755"
PBI_PAGE_NAME   = "Deep-Dive"

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
    cw_start   = today - timedelta(days=today.weekday())   # Mon of current week
    cw_end     = d1                                         # always D-1
    if cw_start > cw_end:                                  # Monday: no completed CW days yet
        cw_start = cw_end                                  # show just D-1
    lw_start   = (today - timedelta(days=today.weekday())) - timedelta(days=7)   # Mon of last week
    lw_end     = (today - timedelta(days=today.weekday())) - timedelta(days=1)   # Sun of last week
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
    return f"{int(round(val)):,}"


def chg(key: str, new_val, old_val) -> str:
    if new_val is None or old_val is None or old_val == 0:
        return "—"
    if key in PCT_METRICS:
        return f"{(new_val - old_val)*100:+.1f}pp"
    pct = ((new_val - old_val) / abs(old_val)) * 100
    return f"{pct:+.1f}%"


def color_chg(key: str, new_val, old_val) -> str:
    if new_val is None or old_val is None or old_val == 0:
        return "—"
    if key in PCT_METRICS:
        diff = (new_val - old_val) * 100
        sign = "✅ ▲" if diff >= 0 else "🚩 ▼"
        return f"{sign} {abs(diff):.1f}pp"
    pct = ((new_val - old_val) / abs(old_val)) * 100
    sign = "✅ ▲" if pct >= 0 else "🚩 ▼"
    return f"{sign} {abs(pct):.1f}%"


def center_col(val: str, width: int) -> str:
    return val.center(width)


def build_summary_block(data: dict) -> str:
    row = data["summary"]
    dr  = data["dr"]
    d   = lambda dt: dt.strftime("%d-%b")

    date_line = (
        f"MTD {d(dr['mtd_start'])}→{d(dr['d1'])}  |  "
        f"D-1: {d(dr['d1'])}  |  "
        f"CW {d(dr['cw_start'])}→{d(dr['cw_end'])}  |  "
        f"LW {d(dr['lw_start'])}→{d(dr['lw_end'])}"
    )

    # column widths
    CW = {"label": 16, "mtd": 8, "lmtd": 8, "chg": 12, "d1": 8, "cw": 8, "lw": 8}

    header = (
        f"{'Metric'.center(CW['label'])} | "
        f"{'MTD'.center(CW['mtd'])} | "
        f"{'LMTD'.center(CW['lmtd'])} | "
        f"{'MTD Δ'.center(CW['chg'])} | "
        f"{'D-1'.center(CW['d1'])} | "
        f"{'Cur Wk'.center(CW['cw'])} | "
        f"{'Lst Wk'.center(CW['lw'])}"
    )
    sep = "-" * len(header)
    lines = [date_line, "", header, sep]

    for key, label in zip(KEYS, LABELS):
        mtd_v  = get_val(row, "MTD",  key)
        lmtd_v = get_val(row, "LMTD", key)
        d1_v   = get_val(row, "D1",   key)
        cw_v   = get_val(row, "CW",   key)
        lw_v   = get_val(row, "LW",   key)
        mtd_chg = color_chg(key, mtd_v, lmtd_v)
        lines.append(
            f"{label:<{CW['label']}} | "
            f"{fmt(key,mtd_v).center(CW['mtd'])} | "
            f"{fmt(key,lmtd_v).center(CW['lmtd'])} | "
            f"{mtd_chg.center(CW['chg'])} | "
            f"{fmt(key,d1_v).center(CW['d1'])} | "
            f"{fmt(key,cw_v).center(CW['cw'])} | "
            f"{fmt(key,lw_v).center(CW['lw'])}"
        )
    return "\n".join(lines)


def build_dod_block(data: dict) -> str:
    rows = data["dod"]
    cols  = ["WL", "Listing", "Live", "WL_Live", "Buyers", "Revenue"]
    hdrs  = ["Workable Leads", "Listing", "Live", "WL→Live%", "Buyers", "Revenue"]
    widths = [14, 8, 7, 9, 8, 9]

    header = f"{'Date'.center(8)} | " + " | ".join(h.center(w) for h, w in zip(hdrs, widths))
    sep    = "-" * len(header)
    lines  = [header, sep]

    for row in rows:
        date_val = None
        for k, v in row.items():
            if "date" in k.lower() or "Date" in k:
                date_val = v
                break
        try:
            date_str = datetime.fromisoformat(str(date_val)).strftime("%d-%b")
        except Exception:
            date_str = str(date_val)[:8]

        vals = [fmt(k, get_col(row, k)) for k in cols]
        lines.append(f"{date_str.center(8)} | " + " | ".join(v.center(w) for v, w in zip(vals, widths)))
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

    # ── build DoD trend lines for prompt ─────────────────────────────────────
    dod_cols = ["WL", "Listing", "Live", "WL_Live", "Revenue"]
    dod_lines = []
    for r in data["dod"]:
        date_val = None
        for k, v in r.items():
            if "date" in k.lower():
                date_val = v; break
        try:
            ds = datetime.fromisoformat(str(date_val)).strftime("%d-%b")
        except Exception:
            ds = str(date_val)[:8]
        vals = "  ".join(f"{fmt(k, get_col(r, k))}" for k in dod_cols)
        dod_lines.append(f"{ds}  {vals}")
    dod_section = "\n".join(dod_lines)

    prompt = f"""You are a senior analyst for Cars24 C2C Marketplace. Analyze this daily data and give sharp, actionable insights.

DATE: {d(dr['d1'])} (D-1)
MTD period: {d(dr['mtd_start'])} to {d(dr['d1'])}
LMTD period: {d(dr['lmtd_start'])} to {d(dr['lmtd_end'])}

NATIONAL SUMMARY (MTD vs LMTD):
Metric         MTD       LMTD      Δ        D-1
Workable Leads {sv('MTD','WL')}  {sv('LMTD','WL')}  {sc('WL','MTD','LMTD')}  {sv('D1','WL')}
Listing        {sv('MTD','Listing')}  {sv('LMTD','Listing')}  {sc('Listing','MTD','LMTD')}  {sv('D1','Listing')}
Listing Live   {sv('MTD','Live')}  {sv('LMTD','Live')}  {sc('Live','MTD','LMTD')}  {sv('D1','Live')}
WL→Live %      {sv('MTD','WL_Live')}  {sv('LMTD','WL_Live')}  {sc('WL_Live','MTD','LMTD')}  {sv('D1','WL_Live')}
Unique Buyers  {sv('MTD','Buyers')}  {sv('LMTD','Buyers')}  {sc('Buyers','MTD','LMTD')}  {sv('D1','Buyers')}
Revenue        {sv('MTD','Revenue')}  {sv('LMTD','Revenue')}  {sc('Revenue','MTD','LMTD')}  {sv('D1','Revenue')}

LAST 7 DAYS TREND (Date | WL | Listing | Live | WL→Live% | Revenue):
{dod_section}
Note: Last row is D-1 ({d(dr['d1'])}). Compare D-1 against the 7-day trend to identify if it is an improvement, decline, or continuation.

TOP 3 REGIONS (MTD by Revenue):
{chr(10).join(f"{i+1}. {region_name(r)} — Revenue: {rev(r)}, Live: {live(r)}, WL→Live: {conv(r)}" for i, r in enumerate(top_regions))}

BOTTOM 3 REGIONS (MTD by Revenue):
{chr(10).join(f"{i+1}. {region_name(r)} — Revenue: {rev(r)}, Live: {live(r)}, WL→Live: {conv(r)}" for i, r in enumerate(bottom_regions))}

TOP 3 CITIES (MTD by Revenue):
{chr(10).join(f"{i+1}. {city_name(c)} — Revenue: {rev(c)}, Live: {live(c)}, WL→Live: {conv(c)}" for i, c in enumerate(top_cities))}

BOTTOM 3 CITIES (MTD by Revenue):
{chr(10).join(f"{i+1}. {city_name(c)} — Revenue: {rev(c)}, Live: {live(c)}, WL→Live: {conv(c)}" for i, c in enumerate(bottom_cities))}

Write a concise daily insight report with 4 sections using bullet points.
IMPORTANT RULES:
- Never use "YoY". Always say "vs LMTD" for month comparisons.
- Use bullet points (•) for every point, not paragraphs.
- Be specific with numbers from the data above.
- Keep each bullet to 1-2 lines max.

Format exactly like this:

*National Overview*
• [MTD vs LMTD insight with specific numbers]
• [key concern or bright spot with specific numbers]

*D-1 vs Last 7 Days Trend*
• [how D-1 compares to the recent 7-day trend — is it an improvement, decline, or flat?]
• [which metric showed the biggest daily swing on D-1 vs prior days]
• [any trend (rising/falling/volatile) worth flagging]

*Region Spotlight*
• [top region insight]
• [bottom region insight]

*City Spotlight*
• [top city insight]
• [bottom city insight]"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp   = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text


def build_changes_block(data: dict) -> str:
    row = data["summary"]
    lines = []
    for key, label in zip(KEYS, LABELS):
        mtd_v  = get_val(row, "MTD",  key)
        lmtd_v = get_val(row, "LMTD", key)
        cw_v   = get_val(row, "CW",   key)
        lw_v   = get_val(row, "LW",   key)

        def indicator(new_val, old_val):
            if new_val is None or old_val is None or old_val == 0:
                return ""
            diff = new_val - old_val
            if key in PCT_METRICS:
                pct = diff * 100
            else:
                pct = (diff / abs(old_val)) * 100
            arrow = "🟢 ▲" if pct >= 0 else "🔴 ▼"
            return f"{arrow} {abs(pct):.1f}{'pp' if key in PCT_METRICS else '%'}"

        mtd_ind = indicator(mtd_v, lmtd_v)
        wk_ind  = indicator(cw_v, lw_v)
        lines.append(f"`{label:<16}` MTD: {mtd_ind:<18} WoW: {wk_ind}")
    return "\n".join(lines)


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


def generate_dashboard_image(data: dict) -> bytes:
    """Generate a clean PNG dashboard image from the fetched data."""
    row = data["summary"]
    dr  = data["dr"]
    d   = lambda dt: dt.strftime("%d-%b")

    # ── collect table rows ────────────────────────────────────────────────────
    col_headers = ["Metric", "MTD", "LMTD", "MTD Δ", "D-1"]
    table_rows  = []
    for key, label in zip(KEYS, LABELS):
        mtd_v  = get_val(row, "MTD",  key)
        lmtd_v = get_val(row, "LMTD", key)
        d1_v   = get_val(row, "D1",   key)
        table_rows.append([
            label,
            fmt(key, mtd_v),
            fmt(key, lmtd_v),
            color_chg(key, mtd_v, lmtd_v),
            fmt(key, d1_v),
        ])

    # ── DoD rows ──────────────────────────────────────────────────────────────
    dod_col_keys  = ["WL", "Listing", "Live", "WL_Live", "Buyers", "Revenue"]
    dod_col_hdrs  = ["Date", "Workable Leads", "Listing", "Live", "WL→Live%", "Buyers", "Revenue"]
    dod_rows = []
    for r in data["dod"]:
        date_val = None
        for k, v in r.items():
            if "date" in k.lower():
                date_val = v; break
        try:
            ds = datetime.fromisoformat(str(date_val)).strftime("%d-%b")
        except Exception:
            ds = str(date_val)[:8]
        dod_rows.append([ds] + [fmt(k, get_col(r, k)) for k in dod_col_keys])

    # ── figure layout ─────────────────────────────────────────────────────────
    BG      = "#0f1117"
    HEADER  = "#1a1d2e"
    ACC     = "#4f8ef7"
    WHITE   = "#ffffff"
    SUBTEXT = "#8b8fa8"
    GREEN   = "#2ecc71"
    RED     = "#e74c3c"
    ROW_A   = "#16192a"
    ROW_B   = "#1c2035"

    fig = plt.figure(figsize=(20, 14), facecolor=BG)
    gs  = GridSpec(3, 1, figure=fig, height_ratios=[0.7, 5, 3.5], hspace=0.35)

    # ── title bar ─────────────────────────────────────────────────────────────
    ax_title = fig.add_subplot(gs[0])
    ax_title.set_facecolor(HEADER)
    ax_title.axis("off")
    date_line = (f"MTD {d(dr['mtd_start'])}→{d(dr['d1'])}  |  D-1: {d(dr['d1'])}  |  "
                 f"CW {d(dr['cw_start'])}→{d(dr['cw_end'])}  |  LW {d(dr['lw_start'])}→{d(dr['lw_end'])}")
    ax_title.text(0.5, 0.72, "C2C Marketplace Dashboard", ha="center", va="center",
                  fontsize=22, fontweight="bold", color=WHITE, transform=ax_title.transAxes)
    ax_title.text(0.5, 0.22, date_line, ha="center", va="center",
                  fontsize=11, color=SUBTEXT, transform=ax_title.transAxes)

    def draw_table(ax, headers, rows, title):
        ax.set_facecolor(BG)
        ax.axis("off")
        ax.text(0.0, 1.02, title, transform=ax.transAxes, fontsize=13,
                fontweight="bold", color=ACC, va="bottom")

        n_cols = len(headers)
        n_rows = len(rows)
        col_w  = [0.18] + [0.115] * (n_cols - 1) if n_cols == 8 else [0.10] + [0.135] * (n_cols - 1)

        # header row
        x = 0.0
        for i, h in enumerate(headers):
            ax.text(x + col_w[i] / 2, 0.97, h, transform=ax.transAxes,
                    ha="center", va="top", fontsize=9.5, fontweight="bold",
                    color=ACC)
            x += col_w[i]

        # separator line
        ax.plot([0, 1], [0.93, 0.93], color=ACC, linewidth=0.8,
                transform=ax.transAxes, clip_on=False)

        row_h = 0.88 / max(n_rows, 1)
        for ri, row in enumerate(rows):
            y_top = 0.92 - ri * row_h
            bg    = ROW_A if ri % 2 == 0 else ROW_B
            ax.add_patch(mpatches.FancyBboxPatch(
                (0, y_top - row_h + 0.005), 1, row_h - 0.005,
                boxstyle="round,pad=0.002", linewidth=0,
                facecolor=bg, transform=ax.transAxes, clip_on=False))

            x = 0.0
            for ci, cell in enumerate(row):
                cell_str = str(cell)
                # colour Δ columns
                if "▲" in cell_str or "✅" in cell_str:
                    col = GREEN
                elif "▼" in cell_str or "🚩" in cell_str:
                    col = RED
                elif ci == 0:
                    col = WHITE
                else:
                    col = "#c8cce0"

                ax.text(x + col_w[ci] / 2, y_top - row_h / 2, cell_str,
                        transform=ax.transAxes, ha="center", va="center",
                        fontsize=8.5, color=col)
                x += col_w[ci]

    # ── summary table ─────────────────────────────────────────────────────────
    ax_sum = fig.add_subplot(gs[1])
    draw_table(ax_sum, col_headers, table_rows, "Summary — MTD vs LMTD | D-1 | Week vs Last Week")

    # ── DoD table ─────────────────────────────────────────────────────────────
    ax_dod = fig.add_subplot(gs[2])
    draw_table(ax_dod, dod_col_hdrs, dod_rows, "Last 7 Days — Day over Day")

    # ── footer ────────────────────────────────────────────────────────────────
    fig.text(0.5, 0.01, f"Generated {datetime.now().strftime('%d %b %Y, %H:%M')} IST  •  Cars24 C2C Marketplace",
             ha="center", fontsize=9, color=SUBTEXT)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor=BG, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    print("Dashboard image generated successfully.")
    return buf.read()


def upload_snapshot_to_slack(image_bytes: bytes, date_str: str):
    """Upload PNG snapshot to Slack channel using Bot Token."""
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}

    # Step 1 — get upload URL
    url_resp = requests.get(
        "https://slack.com/api/files.getUploadURLExternal",
        headers=headers,
        params={"filename": f"C2C_Dashboard_{date_str}.png", "length": len(image_bytes)}
    )
    url_data = url_resp.json()
    if not url_data.get("ok"):
        print(f"Slack upload URL error: {url_data}")
        return

    upload_url = url_data["upload_url"]
    file_id    = url_data["file_id"]

    # Step 2 — upload file
    requests.post(upload_url, data=image_bytes,
                  headers={"Content-Type": "application/octet-stream"})

    # Step 3 — complete upload and share to channel
    complete_resp = requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers={**headers, "Content-Type": "application/json"},
        json={
            "files": [{"id": file_id, "title": f"C2C Dashboard — {date_str}"}],
            "channel_id": SLACK_CHANNEL,
            "initial_comment": f"*[C2C] Dashboard Snapshot — {date_str}*"
        }
    )
    result = complete_resp.json()
    if result.get("ok"):
        print("Snapshot uploaded to Slack successfully.")
    else:
        print(f"Slack complete upload error: {result}")


def main():
    print("Getting token...")
    token = get_token()

    print("Fetching metrics...")
    data = fetch_all(token)

    print("Sending report to Slack...")
    send_slack(data)

    print("Done.")


if __name__ == "__main__":
    main()
