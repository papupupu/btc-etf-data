"""Scheduled data fetcher — run by GitHub Actions every ~10 min.

Fetches ByKaranteli ETF pages (incremental ~30d window), Farside all-data
via the latest Wayback snapshot (once per day), and CoinGecko prices.
Merges into data/*.json which the static dashboard reads via
raw.githubusercontent.com (CORS-friendly, instant).
"""
import json
import sys
import time
from pathlib import Path

import httpx

from parsers import parse_etf_page, parse_farside_table

DATA = Path("data")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

SOURCES = {
    "btc": "https://bykaranteli.com/etf",
    "eth": "https://bykaranteli.com/etf/eth",
}
FARSIDE = {
    "btc": "https://farside.co.uk/bitcoin-etf-flow-all-data/",
    "eth": "https://farside.co.uk/ethereum-etf-flow-all-data/",
}
SEED_FLAG = DATA / ".last_farside_seed"


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load(asset):
    p = DATA / f"{asset}.json"
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def save(asset, d):
    p = DATA / f"{asset}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(p)


def merge_parsed(prev, nxt):
    if not prev or not prev.get("history"):
        return nxt
    by_date = {r["date"]: r for r in prev["history"]}
    for r in nxt["history"]:
        by_date[r["date"]] = r
    nxt["history"] = sorted(by_date.values(), key=lambda r: r["date"])
    prev_funds = {f["ticker"]: f for f in prev.get("funds", [])}
    next_tickers = {f["ticker"] for f in nxt["funds"]}
    for f in nxt["funds"]:
        pf = prev_funds.get(f["ticker"])
        if not pf or not pf.get("daily"):
            continue
        daily = {d["date"]: d for d in pf["daily"]}
        for d in f["daily"]:
            daily[d["date"]] = d
        f["daily"] = sorted(daily.values(), key=lambda d: d["date"])
    for pf in prev.get("funds", []):
        if pf["ticker"] not in next_tickers:
            nxt["funds"].append({**pf, "dailyFlow": None, "netAssets": None})
    return nxt


def merge_seed(entry, patch):
    have = {r["date"] for r in entry["history"]}
    for r in patch.get("history", []):
        if r.get("date") and r["date"] not in have:
            entry["history"].append(r)
    entry["history"].sort(key=lambda r: r["date"])
    by_ticker = {f["ticker"]: f for f in entry["funds"]}
    for pf in patch.get("funds", []):
        f = by_ticker.get(pf["ticker"])
        if f is None:
            f = {"ticker": pf["ticker"], "issuer": pf.get("issuer") or "—",
                 "dailyFlow": None, "netAssets": None, "cumulative": None,
                 "fee": None, "daily": []}
            entry["funds"].append(f)
            by_ticker[f["ticker"]] = f
        if (not f.get("issuer") or f["issuer"] == "—") and pf.get("issuer"):
            f["issuer"] = pf["issuer"]
        have_daily = {d["date"] for d in f.get("daily", [])}
        for d in pf.get("daily", []):
            if d["date"] not in have_daily:
                f["daily"].append(d)
        f["daily"].sort(key=lambda d: d["date"])


def streak(history):
    n, sign = 0, 0
    for r in reversed(history):
        v = r.get("netFlow")
        if not v:
            break
        s = 1 if v > 0 else -1
        if n == 0:
            sign, n = s, 1
        elif s == sign:
            n += 1
        else:
            break
    return {"days": n, "sign": sign}


def fetch_bk(asset, client):
    res = client.get(SOURCES[asset], headers={"User-Agent": UA, "Accept": "text/html"}, timeout=30)
    res.raise_for_status()
    parsed = merge_parsed(load(asset), parse_etf_page(res.text))
    entry = {"asset": asset, **parsed, "fetchedAt": now_iso(), "stale": False}
    save(asset, entry)
    print(f"[{asset}] bykaranteli ok: {entry['latest']['date']}, {len(entry['history'])} days, {len(entry['funds'])} funds")


def fetch_seed(asset, client):
    res = client.get(f"https://web.archive.org/web/2026/{FARSIDE[asset]}",
                     headers={"User-Agent": "Mozilla/5.0"},
                     timeout=60, follow_redirects=True)
    res.raise_for_status()
    patch = parse_farside_table(res.text)
    if not patch["history"]:
        raise ValueError("no rows")
    entry = load(asset) or {"asset": asset, "history": [], "funds": [],
                            "stale": True, "fetchedAt": None, "latest": None, "fiveDayNet": None}
    merge_seed(entry, patch)
    if not entry.get("latest") and entry["history"]:
        entry["latest"] = entry["history"][-1]
        entry["fiveDayNet"] = sum(r["netFlow"] or 0 for r in entry["history"][-5:])
    entry["streak"] = streak(entry["history"])
    save(asset, entry)
    print(f"[{asset}] farside seed -> {len(entry['history'])} days")


def fetch_prices(client):
    url = ("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum"
           "&vs_currencies=usd&include_24hr_change=true&include_last_updated_at=true")
    res = client.get(url, headers={"Accept": "application/json"}, timeout=20)
    res.raise_for_status()
    (DATA / "prices.json").write_text(res.text)
    print("prices ok")


def fetch_price_hist(asset, client):
    coin = "ethereum" if asset == "eth" else "bitcoin"
    url = (f"https://api.coingecko.com/api/v3/coins/{coin}/market_chart"
           "?vs_currency=usd&days=365&interval=daily")
    res = client.get(url, headers={"Accept": "application/json"}, timeout=20)
    res.raise_for_status()
    rows = [{"date": time.strftime("%Y-%m-%d", time.gmtime(t / 1000)), "price": round(p)}
            for t, p in res.json().get("prices", [])]
    (DATA / f"price_{asset}.json").write_text(
        json.dumps(rows, separators=(",", ":")))
    print(f"price hist {asset}: {len(rows)} rows")


def main():
    errors = []
    with httpx.Client() as client:
        for asset in SOURCES:
            try:
                fetch_bk(asset, client)
            except Exception as e:
                print(f"[{asset}] bykaranteli failed: {e}", file=sys.stderr)
                errors.append(f"{asset}:bk")
                d = load(asset)
                if d:
                    d["stale"] = True
                    save(asset, d)
        # farside seed: once per 23h
        try:
            last_seed = float(SEED_FLAG.read_text()) if SEED_FLAG.exists() else 0
        except Exception:
            last_seed = 0
        if time.time() - last_seed > 23 * 3600:
            for asset in FARSIDE:
                try:
                    fetch_seed(asset, client)
                except Exception as e:
                    print(f"[{asset}] farside seed failed: {e}", file=sys.stderr)
                    errors.append(f"{asset}:seed")
            SEED_FLAG.write_text(str(time.time()))
        try:
            fetch_prices(client)
        except Exception as e:
            print(f"prices failed: {e}", file=sys.stderr)
            errors.append("prices")
        for asset in SOURCES:
            try:
                fetch_price_hist(asset, client)
            except Exception as e:
                print(f"price hist {asset} failed: {e}", file=sys.stderr)
                errors.append(f"pricehist:{asset}")
        # attach streak + status
        status = {"updatedAt": now_iso(), "assets": {}}
        for asset in SOURCES:
            d = load(asset)
            if not d:
                continue
            d["streak"] = streak(d["history"])
            save(asset, d)
            status["assets"][asset] = {
                "latest": (d.get("latest") or {}).get("date"),
                "days": len(d.get("history") or []),
                "stale": bool(d.get("stale")),
                "fetchedAt": d.get("fetchedAt"),
            }
        (DATA / "status.json").write_text(json.dumps(status, separators=(",", ":")))
    if errors:
        print("errors:", errors, file=sys.stderr)
        # exit 0 anyway — partial data still worth committing


if __name__ == "__main__":
    main()
