"""HTML parsers: ByKaranteli ETF pages + Farside all-data tables."""
import re

from bs4 import BeautifulSoup

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TICKER_RE = re.compile(r"^[A-Z]{2,6}$")
MMDD_RE = re.compile(r"^\d{2}-\d{2}$")


# "+$174.6M" | "-$120.2M" | "$2.05B" | "+$0" | "-" -> USD int
def parse_usd(s):
    if s is None:
        return None
    t = re.sub(r"[  ​]", "", str(s)).strip()
    if not t or t in ("-", "—", "–"):
        return None
    neg = t.startswith("-")
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*([KMB])?", t, re.I)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get((m.group(2) or "").upper(), 1)
    return round((-1 if neg else 1) * v * mult)


# "160.2" | "-33.4" | "-" -> USD int (values in millions)
def parse_millions(s):
    t = str(s).strip()
    if not t or t in ("-", "—"):
        return None
    try:
        return int(float(t) * 1e6)
    except ValueError:
        return None


def _cell_text(cell):
    return re.sub(r"[  ​]", " ", cell.get_text()).strip()


def _parse_table(table):
    if table is None:
        return {"headers": [], "rows": []}
    thead = table.find("thead")
    headers = [_cell_text(th) for th in thead.find_all("th")] if thead else []
    tbody = table.find("tbody")
    rows = []
    for tr in (tbody.find_all("tr") if tbody else table.find_all("tr")):
        cells = [_cell_text(td) for td in tr.find_all("td")]
        if cells:
            rows.append(cells)
    return {"headers": headers, "rows": rows}


def _table_after(soup, title):
    h2 = next((h for h in soup.find_all("h2") if h.get_text().strip() == title), None)
    if h2 is None:
        return None
    parent = h2.parent
    if parent:
        t = parent.find("table")
        if t:
            return t
        for sib in parent.find_next_siblings():
            t = sib.find("table") if hasattr(sib, "find") else None
            if t:
                return t
    section = h2.find_parent("section")
    if section:
        t = section.find("table")
        if t:
            return t
    return h2.find_next("table")


def _infer_year(mmdd, latest_date):
    ly, lm = int(latest_date[:4]), int(latest_date[5:7])
    m = int(mmdd[:2])
    return ly - 1 if m > lm else ly


def parse_etf_page(html):
    soup = BeautifulSoup(html, "html.parser")

    hist = _parse_table(_table_after(soup, "Daily history"))
    history = []
    for r in hist["rows"]:
        row = {
            "date": r[0] if len(r) > 0 else None,
            "netFlow": parse_usd(r[1] if len(r) > 1 else None),
            "valueTraded": parse_usd(r[2] if len(r) > 2 else None),
            "netAssets": parse_usd(r[3] if len(r) > 3 else None),
            "cumulative": parse_usd(r[4] if len(r) > 4 else None),
        }
        if row["date"] and DATE_RE.match(row["date"]) and row["netFlow"] is not None:
            history.append(row)
    history.sort(key=lambda r: r["date"])
    if not history:
        raise ValueError("no daily history rows parsed")
    latest_date = history[-1]["date"]

    funds = []
    for r in _parse_table(_table_after(soup, "By fund"))["rows"]:
        if len(r) < 6:
            continue
        f = {
            "ticker": r[0], "issuer": r[1],
            "dailyFlow": parse_usd(r[2]), "netAssets": parse_usd(r[3]),
            "cumulative": parse_usd(r[4]), "fee": r[5] or None, "daily": [],
        }
        if TICKER_RE.match(f["ticker"]):
            funds.append(f)

    grid = _parse_table(_table_after(soup, "Daily net flows by fund"))
    date_cols = [(i, h) for i, h in enumerate(grid["headers"]) if MMDD_RE.match(h)]
    by_ticker = {f["ticker"]: f for f in funds}
    for row in grid["rows"]:
        if not row or not TICKER_RE.match(row[0]):
            continue
        f = by_ticker.get(row[0])
        if f is None:
            f = {"ticker": row[0], "issuer": "—", "dailyFlow": None,
                 "netAssets": None, "cumulative": None, "fee": None, "daily": []}
            funds.append(f)
            by_ticker[f["ticker"]] = f
        for i, d in date_cols:
            if i >= len(row):
                continue
            v = parse_millions(row[i])
            if v is None:
                continue
            f["daily"].append({"date": f"{_infer_year(d, latest_date)}-{d}", "flow": v})

    text = soup.get_text(" ")
    as_of = re.search(r"As of\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s*UTC", text)
    since = re.search(r"recorded daily since\s*(\d{4}-\d{2}-\d{2})", text, re.I)
    provisional = bool(re.search(r"provisional until settled", text, re.I))

    return {
        "sourceAsOf": (as_of.group(1) + " UTC") if as_of else None,
        "provisional": provisional,
        "fundGridSince": since.group(1) if since else None,
        "latest": history[-1],
        "fiveDayNet": sum(r["netFlow"] or 0 for r in history[-5:]),
        "history": history,
        "funds": funds,
    }


# ---------- Farside all-data (via Wayback snapshot) ----------

ISSUERS = {
    "IBIT": "BlackRock", "FBTC": "Fidelity", "BITB": "Bitwise", "ARKB": "Ark & 21Shares",
    "BTCO": "Invesco", "EZBC": "Franklin", "BRRR": "Valkyrie", "HODL": "VanEck",
    "BTCW": "WisdomTree", "GBTC": "Grayscale", "BTC": "Grayscale", "MSBT": "Morgan Stanley",
    "DEFI": "Hashdex", "ETHA": "BlackRock", "FETH": "Fidelity", "ETHW": "Bitwise",
    "ETHV": "VanEck", "ETHE": "Grayscale", "ETH": "Grayscale", "TETH": "21Shares",
    "EZET": "Franklin", "QETH": "Invesco", "CETH": "VanEck", "ETHB": "BlackRock",
}

MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
          "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def _farside_date(s):
    m = re.match(r"^(\d{1,2}) (\w{3}) (\d{4})$", str(s).strip())
    if not m or m.group(2) not in MONTHS:
        return None
    return f"{m.group(3)}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"


# "655.3" | "(95.1)" | "-" | "0.0" -> USD int (table is in US$ millions)
def _farside_cell(s):
    t = re.sub(r" ", "", str(s)).strip()
    if not t or t in ("-", "—"):
        return None
    m = re.match(r"^\(?\s*([\d,]+(?:\.\d+)?)\s*\)?$", t)
    if not m:
        return None
    neg = "(" in t
    return int((-1 if neg else 1) * float(m.group(1).replace(",", "")) * 1e6)


def parse_farside_table(html):
    soup = BeautifulSoup(html, "html.parser")
    header = None
    fund_daily = {}
    history = []
    cum = 0

    for tr in soup.find_all("tr"):
        cells = [c.get_text().strip() for c in tr.find_all(["td", "th"])]
        if len(cells) < 3:
            continue
        if header is None:
            tickerish = sum(1 for c in cells[1:] if TICKER_RE.match(c))
            if tickerish >= 3:
                header = cells
            continue
        date = _farside_date(cells[0])
        if not date:
            continue
        total = _farside_cell(cells[-1])
        row = {"date": date, "netFlow": total, "valueTraded": None,
               "netAssets": None, "cumulative": None}
        if total is not None:
            cum += total
            row["cumulative"] = round(cum)
        history.append(row)
        for i in range(1, min(len(cells) - 1, len(header))):
            v = _farside_cell(cells[i])
            if v is None:
                continue
            t = header[i]
            if not TICKER_RE.match(t):
                continue
            fund_daily.setdefault(t, []).append({"date": date, "flow": v})

    funds = [{"ticker": t, "issuer": ISSUERS.get(t, "—"), "dailyFlow": None,
              "netAssets": None, "cumulative": None, "fee": None, "daily": d}
             for t, d in fund_daily.items()]
    return {"history": history, "funds": funds}
