"""Stock dashboard server — Python standard library only.

Run:  python server.py     then open http://localhost:8000
Proxies Yahoo Finance (search, chart, quotes, news) so the browser avoids CORS.
"""
import json
import os
import re
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

PORT = int(os.environ.get("PORT", 8000))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# range -> candle interval
RANGES = {"1d": "5m", "5d": "15m", "1mo": "60m", "6mo": "1d",
          "ytd": "1d", "1y": "1d", "5y": "1wk", "max": "1mo"}
SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-^=]{1,20}$")

_cache = {}
_cache_lock = threading.Lock()


def fetch(url, ttl, timeout=10):
    """GET url with an in-memory cache (ttl seconds).

    Retries once on network errors (e.g. a DNS hiccup) and, if it still fails,
    falls back to the last good copy so a flaky connection doesn't blank the page.
    """
    now = time.time()
    with _cache_lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < ttl:
            return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    err = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            with _cache_lock:
                _cache[url] = (time.time(), data)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404 or e.code < 500:
                raise
            err = e
        except (urllib.error.URLError, OSError) as e:
            err = e
        if attempt == 0:
            time.sleep(0.5)
    if hit:
        return hit[1]  # stale but better than nothing
    raise err


def chart(symbol, rng):
    interval = RANGES.get(rng, "1d")
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?range={rng}&interval={interval}&includePrePost=false")
    res = json.loads(fetch(url, 60))["chart"]
    if res.get("error") or not res.get("result"):
        raise ValueError((res.get("error") or {}).get("description", "No data"))
    r = res["result"][0]
    q = (r.get("indicators", {}).get("quote") or [{}])[0]
    points = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = (q.get("close") or [None])[i] if i < len(q.get("close") or []) else None
        if c is None:
            continue
        points.append({"t": t, "c": c, "o": q["open"][i], "h": q["high"][i],
                       "l": q["low"][i], "v": q["volume"][i]})
    return {"meta": r["meta"], "points": points, "interval": interval}


def quote(symbol):
    c = chart(symbol, "1d")
    m = c["meta"]
    closes = [p["c"] for p in c["points"]]
    step = max(1, len(closes) // 60)
    spark = closes[::step] + ([closes[-1]] if closes and (len(closes) - 1) % step else [])
    price = m.get("regularMarketPrice")
    prev = m.get("chartPreviousClose") or m.get("previousClose")
    change = (price - prev) if price is not None and prev else None
    return {
        "symbol": m.get("symbol", symbol),
        "name": m.get("longName") or m.get("shortName") or symbol,
        "exchange": m.get("fullExchangeName") or m.get("exchangeName"),
        "currency": m.get("currency"),
        "price": price,
        "prevClose": prev,
        "change": change,
        "changePct": (change / prev * 100) if change is not None else None,
        "dayHigh": m.get("regularMarketDayHigh"),
        "dayLow": m.get("regularMarketDayLow"),
        "volume": m.get("regularMarketVolume"),
        "high52": m.get("fiftyTwoWeekHigh"),
        "low52": m.get("fiftyTwoWeekLow"),
        "marketTime": m.get("regularMarketTime"),
        "tradingPeriod": m.get("currentTradingPeriod"),  # pre / regular / post start+end
        "gmtoffset": m.get("gmtoffset"),
        "timezone": m.get("timezone"),
        "exchangeTimezone": m.get("exchangeTimezoneName"),
        "spark": spark,
    }


def quotes(symbols):
    def one(s):
        try:
            return quote(s)
        except Exception as e:  # noqa: BLE001
            return {"symbol": s, "error": str(e)}
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(one, symbols))


SCREENERS = {"largest_market_cap", "day_gainers", "day_losers", "most_actives"}


def screener(scr_id, count):
    """Yahoo's predefined US screeners (gainers/losers only include market caps >= ~$2B)."""
    url = ("https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?"
           + urllib.parse.urlencode({"scrIds": scr_id, "count": count, "formatted": "false"}))
    res = json.loads(fetch(url, 120))["finance"]["result"][0]
    return [{"symbol": q["symbol"],
             "name": q.get("displayName") or q.get("shortName") or q.get("longName") or q["symbol"],
             "price": q.get("regularMarketPrice"),
             "change": q.get("regularMarketChange"),
             "changePct": q.get("regularMarketChangePercent"),
             "marketCap": q.get("marketCap"),
             "volume": q.get("regularMarketVolume"),
             "currency": q.get("currency"),
             "marketTime": q.get("regularMarketTime"),
             "marketState": q.get("marketState")}
            for q in res.get("quotes", [])]


def search(q):
    url = ("https://query1.finance.yahoo.com/v1/finance/search?"
           + urllib.parse.urlencode({"q": q, "quotesCount": 8, "newsCount": 0}))
    data = json.loads(fetch(url, 300))
    return [{"symbol": x["symbol"],
             "name": x.get("longname") or x.get("shortname") or x["symbol"],
             "exchange": x.get("exchDisp", ""),
             "type": x.get("typeDisp", "")}
            for x in data.get("quotes", []) if x.get("symbol")]


TAG_RE = re.compile(r"<[^>]+>")
NEWS_DEADLINE = 8  # seconds: return whatever sources answered by then
_news_pool = ThreadPoolExecutor(max_workers=12)


def _rss_time(pub):
    try:
        return int(parsedate_to_datetime(pub).timestamp())
    except Exception:  # noqa: BLE001
        return 0


def news_yahoo_rss(symbol, _name):
    url = ("https://feeds.finance.yahoo.com/rss/2.0/headline?"
           + urllib.parse.urlencode({"s": symbol, "region": "US", "lang": "en-US"}))
    root = ET.fromstring(fetch(url, 300, timeout=6))
    return [{"title": (it.findtext("title") or "").strip(),
             "link": (it.findtext("link") or "").strip(),
             "summary": TAG_RE.sub("", it.findtext("description") or "").strip(),
             "source": "Yahoo Finance",
             "time": _rss_time(it.findtext("pubDate") or "")}
            for it in root.iter("item")]


def news_yahoo_search(symbol, _name):
    url = ("https://query1.finance.yahoo.com/v1/finance/search?"
           + urllib.parse.urlencode({"q": symbol, "quotesCount": 0, "newsCount": 15}))
    data = json.loads(fetch(url, 300, timeout=6))
    return [{"title": n.get("title", "").strip(), "link": n.get("link", ""),
             "summary": "", "source": n.get("publisher") or "Yahoo Finance",
             "time": int(n.get("providerPublishTime") or 0)}
            for n in data.get("news", []) if n.get("title")]


def news_google(symbol, name):
    # Search by company name (better recall than the ticker), fall back to ticker.
    term = f'"{name}"' if name and name != symbol else symbol
    url = ("https://news.google.com/rss/search?"
           + urllib.parse.urlencode({"q": f"{term} stock when:7d", "hl": "en-US",
                                     "gl": "US", "ceid": "US:en"}))
    root = ET.fromstring(fetch(url, 300, timeout=6))
    out = []
    for it in list(root.iter("item"))[:25]:
        src = (it.findtext("source") or "").strip()
        title = (it.findtext("title") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        out.append({"title": title, "link": (it.findtext("link") or "").strip(),
                    "summary": "", "source": src or "Google News",
                    "time": _rss_time(it.findtext("pubDate") or "")})
    return out


def _norm(title):
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()[:80]


def news(symbol):
    """Merge news from several sources; succeed if at least one answers in time."""
    deadline = time.time() + NEWS_DEADLINE
    try:  # company name for the Google query (usually already cached by the quote call)
        name_f = _news_pool.submit(lambda: quote(symbol)["name"])
        name = name_f.result(timeout=3)
    except Exception:  # noqa: BLE001
        name = symbol
    futures = [_news_pool.submit(fn, symbol, name)
               for fn in (news_yahoo_rss, news_yahoo_search, news_google)]
    items, errors = [], []
    for f in futures:
        try:
            items += f.result(timeout=max(0.1, deadline - time.time()))
        except Exception as e:  # noqa: BLE001  (includes TimeoutError)
            errors.append(str(e) or type(e).__name__)
    if not items and errors:
        raise RuntimeError("all news sources failed (" + "; ".join(errors[:2]) + ")")
    seen, merged = set(), []
    for it in sorted(items, key=lambda x: x["time"], reverse=True):
        key = _norm(it["title"])
        if key and key not in seen:
            seen.add(key)
            merged.append(it)
    return merged[:60]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC_DIR, **kw)

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")  # always pick up page updates
        super().end_headers()

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if not u.path.startswith("/api/"):
            return super().do_GET()
        p = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/api/search":
                q = p.get("q", "").strip()
                return self.send_json(search(q) if q else [])
            if u.path == "/api/quotes":
                syms = [s for s in p.get("symbols", "").upper().split(",") if SYMBOL_RE.match(s)][:30]
                return self.send_json(quotes(syms))
            if u.path == "/api/screener":
                sid = p.get("id", "")
                if sid not in SCREENERS:
                    return self.send_json({"error": "Unknown screener"}, 400)
                count = min(max(int(p.get("count", 10) or 10), 1), 50)
                return self.send_json(screener(sid, count))
            sym = p.get("symbol", "").upper()
            if not SYMBOL_RE.match(sym):
                return self.send_json({"error": "Invalid symbol"}, 400)
            if u.path == "/api/chart":
                rng = p.get("range", "1d")
                if rng not in RANGES:
                    rng = "1d"
                return self.send_json(chart(sym, rng))
            if u.path == "/api/news":
                return self.send_json(news(sym))
            return self.send_json({"error": "Not found"}, 404)
        except urllib.error.HTTPError as e:
            msg = "Symbol not found" if e.code == 404 else f"Upstream error {e.code}"
            return self.send_json({"error": msg}, 502)
        except Exception as e:  # noqa: BLE001
            return self.send_json({"error": str(e)}, 502)


if __name__ == "__main__":
    print(f"Stock dashboard running at http://localhost:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
