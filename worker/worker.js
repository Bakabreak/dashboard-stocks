// Pulse API — Cloudflare Worker port of server.py's /api/* proxy.
// Deploy: Cloudflare dashboard → Workers & Pages → Create → Worker → Edit code → paste this file → Deploy.
// The GitHub Pages frontend calls this Worker; locally, server.py serves the same routes.

const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
           "(KHTML, like Gecko) Chrome/130.0 Safari/537.36";

// range -> candle interval (keep in sync with server.py and index.html)
const RANGES = { "1d": "5m", "5d": "15m", "1mo": "60m", "6mo": "1d",
                 "ytd": "1d", "1y": "1d", "5y": "1wk", "max": "1mo" };
const SYMBOL_RE = /^[A-Za-z0-9.\-^=]{1,20}$/;
const SCREENERS = new Set(["largest_market_cap", "day_gainers", "day_losers", "most_actives"]);
const NEWS_DEADLINE = 8000;

// Allowed browser origins (null = allow any). Add your Pages URL if you want to lock it down.
const ALLOWED_ORIGINS = null;

class UpstreamError extends Error {
  constructor(status) { super(`Upstream error ${status}`); this.status = status; }
}

// Per-isolate memory cache; also used as a stale fallback when Yahoo hiccups.
const mem = new Map();

async function fetchText(url, ttl, timeout = 10000) {
  const now = Date.now();
  const hit = mem.get(url);
  if (hit && now - hit.t < ttl * 1000) return hit.body;

  let err;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const r = await fetch(url, {
        headers: { "User-Agent": UA, "Accept": "*/*" },
        signal: AbortSignal.timeout(timeout),
        cf: { cacheTtl: ttl, cacheEverything: true },
      });
      if (!r.ok) {
        if (r.status < 500) throw Object.assign(new UpstreamError(r.status), { final: true });
        throw new UpstreamError(r.status);
      }
      const body = await r.text();
      mem.set(url, { t: Date.now(), body });
      if (mem.size > 500) mem.delete(mem.keys().next().value);
      return body;
    } catch (e) {
      if (e.final) throw e;
      err = e;
    }
    if (attempt === 0) await new Promise(res => setTimeout(res, 500));
  }
  if (hit) return hit.body; // stale but better than nothing
  throw err;
}

const fetchJSON = async (url, ttl, timeout) => JSON.parse(await fetchText(url, ttl, timeout));
const qs = obj => new URLSearchParams(obj).toString();

async function chart(symbol, rng) {
  const interval = RANGES[rng] || "1d";
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}` +
              `?range=${rng}&interval=${interval}&includePrePost=false`;
  const res = (await fetchJSON(url, 60)).chart;
  if (res.error || !res.result || !res.result.length)
    throw new Error((res.error || {}).description || "No data");
  const r = res.result[0];
  const q = ((r.indicators || {}).quote || [{}])[0] || {};
  const close = q.close || [];
  const points = [];
  (r.timestamp || []).forEach((t, i) => {
    const c = close[i];
    if (c == null) return;
    points.push({ t, c, o: q.open[i], h: q.high[i], l: q.low[i], v: q.volume[i] });
  });
  return { meta: r.meta, points, interval };
}

async function quote(symbol) {
  const c = await chart(symbol, "1d");
  const m = c.meta;
  const closes = c.points.map(p => p.c);
  const step = Math.max(1, Math.floor(closes.length / 60));
  const spark = closes.filter((_, i) => i % step === 0);
  if (closes.length && (closes.length - 1) % step) spark.push(closes[closes.length - 1]);
  const price = m.regularMarketPrice;
  const prev = m.chartPreviousClose || m.previousClose;
  const change = price != null && prev ? price - prev : null;
  return {
    symbol: m.symbol || symbol,
    name: m.longName || m.shortName || symbol,
    exchange: m.fullExchangeName || m.exchangeName,
    currency: m.currency,
    price,
    prevClose: prev,
    change,
    changePct: change != null ? change / prev * 100 : null,
    dayHigh: m.regularMarketDayHigh,
    dayLow: m.regularMarketDayLow,
    volume: m.regularMarketVolume,
    high52: m.fiftyTwoWeekHigh,
    low52: m.fiftyTwoWeekLow,
    marketTime: m.regularMarketTime,
    tradingPeriod: m.currentTradingPeriod,
    gmtoffset: m.gmtoffset,
    timezone: m.timezone,
    exchangeTimezone: m.exchangeTimezoneName,
    spark,
  };
}

const quotes = symbols => Promise.all(symbols.map(s =>
  quote(s).catch(e => ({ symbol: s, error: String(e.message || e) }))));

async function screener(id, count) {
  const url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?" +
              qs({ scrIds: id, count, formatted: "false" });
  const res = (await fetchJSON(url, 120)).finance.result[0];
  return (res.quotes || []).map(q => ({
    symbol: q.symbol,
    name: q.displayName || q.shortName || q.longName || q.symbol,
    price: q.regularMarketPrice,
    change: q.regularMarketChange,
    changePct: q.regularMarketChangePercent,
    marketCap: q.marketCap,
    volume: q.regularMarketVolume,
    currency: q.currency,
    marketTime: q.regularMarketTime,
    marketState: q.marketState,
  }));
}

async function search(q) {
  const url = "https://query1.finance.yahoo.com/v1/finance/search?" +
              qs({ q, quotesCount: 8, newsCount: 0 });
  const data = await fetchJSON(url, 300);
  return (data.quotes || []).filter(x => x.symbol).map(x => ({
    symbol: x.symbol,
    name: x.longname || x.shortname || x.symbol,
    exchange: x.exchDisp || "",
    type: x.typeDisp || "",
  }));
}

/* ---------- news (RSS parsed with regex; Workers have no DOMParser) ---------- */
const ENT = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };
function decode(s) {
  return s.replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
          .replace(/&(#x[0-9a-f]+|#\d+|\w+);/gi, (m, e) =>
            e[0] === "#" ? String.fromCodePoint(e[1].toLowerCase() === "x" ? parseInt(e.slice(2), 16) : +e.slice(1))
                         : ENT[e.toLowerCase()] ?? m);
}
function tag(xml, name) {
  const m = xml.match(new RegExp(`<${name}(?:\\s[^>]*)?>([\\s\\S]*?)</${name}>`, "i"));
  return m ? decode(m[1]).trim() : "";
}
const items = xml => xml.match(/<item[\s>][\s\S]*?<\/item>/gi) || [];
const rssTime = s => { const t = Date.parse(s); return isNaN(t) ? 0 : Math.floor(t / 1000); };
const stripTags = s => s.replace(/<[^>]+>/g, "").trim();

async function newsYahooRss(symbol) {
  const url = "https://feeds.finance.yahoo.com/rss/2.0/headline?" +
              qs({ s: symbol, region: "US", lang: "en-US" });
  return items(await fetchText(url, 300, 6000)).map(it => ({
    title: tag(it, "title"), link: tag(it, "link"),
    summary: stripTags(tag(it, "description")),
    source: "Yahoo Finance", time: rssTime(tag(it, "pubDate")),
  }));
}

async function newsYahooSearch(symbol) {
  const url = "https://query1.finance.yahoo.com/v1/finance/search?" +
              qs({ q: symbol, quotesCount: 0, newsCount: 15 });
  const data = await fetchJSON(url, 300, 6000);
  return (data.news || []).filter(n => n.title).map(n => ({
    title: n.title.trim(), link: n.link || "", summary: "",
    source: n.publisher || "Yahoo Finance", time: +(n.providerPublishTime || 0),
  }));
}

async function newsGoogle(symbol, name) {
  const term = name && name !== symbol ? `"${name}"` : symbol;
  const url = "https://news.google.com/rss/search?" +
              qs({ q: `${term} stock when:7d`, hl: "en-US", gl: "US", ceid: "US:en" });
  return items(await fetchText(url, 300, 6000)).slice(0, 25).map(it => {
    const src = tag(it, "source");
    let title = tag(it, "title");
    if (src && title.endsWith(" - " + src)) title = title.slice(0, -src.length - 3);
    return { title, link: tag(it, "link"), summary: "", source: src || "Google News",
             time: rssTime(tag(it, "pubDate")) };
  });
}

const withTimeout = (p, ms) => Promise.race([p,
  new Promise((_, rej) => setTimeout(() => rej(new Error("timed out")), ms))]);
const norm = t => t.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim().slice(0, 80);

async function news(symbol) {
  const deadline = Date.now() + NEWS_DEADLINE;
  let name = symbol;
  try { name = (await withTimeout(quote(symbol), 3000)).name; } catch {}
  const left = () => Math.max(100, deadline - Date.now());
  const results = await Promise.allSettled(
    [newsYahooRss, newsYahooSearch, newsGoogle].map(fn => withTimeout(fn(symbol, name), left())));
  const all = [], errors = [];
  for (const r of results) r.status === "fulfilled" ? all.push(...r.value) : errors.push(String(r.reason?.message || r.reason));
  if (!all.length && errors.length) throw new Error("all news sources failed (" + errors.slice(0, 2).join("; ") + ")");
  const seen = new Set(), merged = [];
  for (const it of all.sort((a, b) => b.time - a.time)) {
    const k = norm(it.title);
    if (k && !seen.has(k)) { seen.add(k); merged.push(it); }
  }
  return merged.slice(0, 60);
}

/* ---------- routing ---------- */
function cors(req) {
  const origin = req.headers.get("Origin") || "*";
  const allow = !ALLOWED_ORIGINS ? "*" : ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0];
  return { "Access-Control-Allow-Origin": allow, "Access-Control-Allow-Methods": "GET, OPTIONS", "Vary": "Origin" };
}
const json = (req, obj, status = 200) => new Response(JSON.stringify(obj), {
  status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...cors(req) },
});

async function route(req) {
  const u = new URL(req.url);
  const p = Object.fromEntries(u.searchParams);
  switch (u.pathname) {
    case "/api/search": {
      const q = (p.q || "").trim();
      return json(req, q ? await search(q) : []);
    }
    case "/api/quotes": {
      const syms = (p.symbols || "").toUpperCase().split(",").filter(s => SYMBOL_RE.test(s)).slice(0, 30);
      return json(req, await quotes(syms));
    }
    case "/api/screener": {
      if (!SCREENERS.has(p.id)) return json(req, { error: "Unknown screener" }, 400);
      const count = Math.min(Math.max(parseInt(p.count, 10) || 10, 1), 50);
      return json(req, await screener(p.id, count));
    }
    case "/api/chart":
    case "/api/news": {
      const sym = (p.symbol || "").toUpperCase();
      if (!SYMBOL_RE.test(sym)) return json(req, { error: "Invalid symbol" }, 400);
      if (u.pathname === "/api/news") return json(req, await news(sym));
      return json(req, await chart(sym, RANGES[p.range] ? p.range : "1d"));
    }
    case "/":
      return json(req, { ok: true, service: "pulse-api" });
  }
  return json(req, { error: "Not found" }, 404);
}

export default {
  async fetch(req) {
    if (req.method === "OPTIONS") return new Response(null, { headers: cors(req) });
    if (req.method !== "GET") return json(req, { error: "Method not allowed" }, 405);
    try {
      return await route(req);
    } catch (e) {
      if (e instanceof UpstreamError)
        return json(req, { error: e.status === 404 ? "Symbol not found" : e.message }, 502);
      return json(req, { error: String(e.message || e) }, 502);
    }
  },
};
