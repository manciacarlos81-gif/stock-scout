// Stock Scout live alerts — runs on Cloudflare's servers on a timer, so your laptop can be off.
//
// Every 15 minutes while the US market is open: checks your watchlist (watchlist.txt in the repo)
// plus today's top-ranked stocks, and sends a Telegram message when one moves 5%+ (again at 10%+)
// or hits a price target you set. Once a day after the scan: sends a summary of the new report.

const REPO_RAW = "https://raw.githubusercontent.com/manciacarlos81-gif/stock-scout/main";
const SITE = "https://manciacarlos81-gif.github.io/stock-scout";
const MAX_TICKERS = 40; // Cloudflare's free plan allows 50 outgoing requests per run
const DAY = 86400;

export default {
  async scheduled(event, env, ctx) {
    if (event.cron === env.DIGEST_CRON) await dailyDigest(env);
    else await checkPrices(env);
  },
  // Visiting the worker's URL shows when it last ran (handy to confirm it's alive)
  async fetch(request, env) {
    // ?quote=AAPL shows what Yahoo returns from Cloudflare's servers (read-only, sends nothing)
    const t = new URL(request.url).searchParams.get("quote");
    if (t && /^[A-Za-z.\-]{1,8}$/.test(t)) {
      return Response.json({ ticker: t.toUpperCase(), quote: await quote(t.toUpperCase()) });
    }
    const [prices, digest] = await Promise.all([env.STATE.get("last_price_check"), env.STATE.get("last_digest")]);
    const body = { ok: true, last_price_check: JSON.parse(prices || "null"), last_digest: JSON.parse(digest || "null") };
    return new Response(JSON.stringify(body, null, 2), { headers: { "content-type": "application/json" } });
  },
};

// ------------------------------------------------------------------ price alerts

async function checkPrices(env) {
  const now = new Date();
  const force = env.FORCE_MARKET_OPEN === "1";
  if (!force && !marketOpen(now)) return;

  const list = await watchlist(env);
  const quotes = await Promise.all(list.map((w) => quote(w.ticker).catch(() => null)));
  const day = nyDate(now);
  const movePct = Number(env.MOVE_PCT || 5);
  const lines = [];
  let checked = 0;

  for (let i = 0; i < list.length; i++) {
    const w = list[i], q = quotes[i];
    if (!q) continue;
    if (!force && now / 1000 - q.time > 1800) continue; // no trade in 30 min: holiday or halted
    checked++;
    const change = q.price / q.prev - 1;
    for (const tier of [movePct * 2, movePct]) {  // biggest tier first, one message per tier per day
      if (Math.abs(change) * 100 < tier) continue;
      const dir = change > 0 ? "up" : "down";
      if (await once(env, `move:${day}:${w.ticker}:${tier}:${dir}`)) {
        lines.push(`${change > 0 ? "🚀" : "🔻"} <b>${w.ticker}</b> ${pct(change)} today — ${usd(q.price)}` +
                   `${w.reason ? ` <i>(${esc(w.reason)})</i>` : ""}`);
      }
      break;
    }
    if (w.cond && ((w.cond === "below" && q.price <= w.target) || (w.cond === "above" && q.price >= w.target))) {
      if (await once(env, `target:${day}:${w.ticker}:${w.cond}:${w.target}`)) {
        lines.push(`🎯 <b>${w.ticker}</b> hit your target (${w.cond} ${usd(w.target)}) — now ${usd(q.price)}`);
      }
    }
  }

  if (lines.length) await telegram(env, lines.join("\n") + `\n\n<a href="${SITE}/">Dashboard</a>`);
  await env.STATE.put("last_price_check", JSON.stringify({
    at: now.toISOString(), watching: list.length, quotes_ok: quotes.filter(Boolean).length, checked, alerts: lines.length,
  }));
}

async function watchlist(env) {
  const out = new Map();
  // Your own list: one per line, "TICKER", "TICKER below 300" or "TICKER above 250"
  const text = await fetch(`${REPO_RAW}/watchlist.txt`, { cf: { cacheTtl: 60 } }).then((r) => (r.ok ? r.text() : ""));
  for (const raw of text.split("\n")) {
    const line = raw.replace(/#.*/, "").trim();
    if (!line) continue;
    const [t, cond, target] = line.split(/\s+/);
    const c = (cond || "").toLowerCase();
    out.set(t.toUpperCase(), {
      ticker: t.toUpperCase(), reason: "your watchlist",
      cond: c === "below" || c === "above" ? c : null, target: Number(target) || null,
    });
  }
  // Plus the top-ranked stocks from the latest scan
  const top = Number(env.AUTO_WATCH_TOP || 10);
  if (top > 0) {
    const a = await fetchJson(`${SITE}/alerts.json`).catch(() => null);
    for (const s of (a?.top || []).slice(0, top)) {
      if (!out.has(s.ticker)) out.set(s.ticker, { ticker: s.ticker, reason: `#${s.rank} in today's scan`, cond: null, target: null });
    }
  }
  return [...out.values()].slice(0, MAX_TICKERS);
}

async function quote(ticker) {
  for (const host of ["query1", "query2"]) {
    try {
      const d = await fetchJson(`https://${host}.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(ticker)}?range=1d&interval=1d`);
      const m = d.chart.result[0].meta;
      if (m.regularMarketPrice && m.chartPreviousClose) {
        return { price: m.regularMarketPrice, prev: m.chartPreviousClose, time: m.regularMarketTime };
      }
    } catch (e) { /* try the other host */ }
  }
  return null;
}

// ------------------------------------------------------------------ daily summary

async function dailyDigest(env) {
  const now = new Date();
  const a = await fetchJson(`${SITE}/alerts.json`).catch(() => null);
  const ageDays = a ? (Date.parse(nyDate(now)) - Date.parse(a.date)) / (DAY * 1000) : 99;

  if (!a || ageDays > 1) {  // the GitHub scan didn't produce a report today
    if (await once(env, `stale:${nyDate(now)}`)) {
      await telegram(env, `⚠️ Stock Scout: no fresh report today (latest: ${a?.date || "none"}). ` +
        `Check <a href="https://github.com/manciacarlos81-gif/stock-scout/actions">GitHub Actions</a>.`);
    }
    return;
  }
  if (!(await once(env, `digest:${a.date}`, 7 * DAY))) return;

  const parts = [`📊 <b>Stock Scout — ${a.date}</b> (${a.scanned.toLocaleString("en-US")} stocks scored)`];
  parts.push("\n🏆 <b>Top 5 overall</b>\n" + a.top.slice(0, 5).map((s) =>
    `${s.rank}. <b>${s.ticker}</b> ${esc(s.name)} — score ${s.score}, ${usd(s.price)}${s.flags ? ` ⚠️ ${esc(s.flags)}` : ""}`).join("\n"));
  if (a.new_top?.length) parts.push(`\n🆕 <b>New in the top list:</b> ${a.new_top.join(", ")}`);
  if (a.insider?.length) {
    parts.push("\n🕵️ <b>Fresh insider buying</b>\n" + a.insider.slice(0, 5).map((s) =>
      `<b>${s.ticker}</b> ${esc(s.name)} — ${usdShort(s.value)} by ${s.buyers} ${s.buyers === 1 ? "person" : "people"} (last ${s.last})`).join("\n"));
  }
  parts.push(`\n<a href="${SITE}/">Dashboard</a> · <a href="https://github.com/manciacarlos81-gif/stock-scout/blob/main/reports/latest.md">Full report</a>`);
  parts.push("<i>Automatic screen, not financial advice.</i>");
  await telegram(env, parts.join("\n"));
  await env.STATE.put("last_digest", JSON.stringify({ at: now.toISOString(), report: a.date }));
}

// ------------------------------------------------------------------ helpers

async function telegram(env, html) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) {
    console.log("[dry run — Telegram secrets not set]\n" + html);
    return;
  }
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text: html, parse_mode: "HTML", disable_web_page_preview: true }),
  });
  if (!r.ok) console.log("Telegram error", r.status, await r.text());
}

// true the first time a key is seen (then remembered for ttl seconds) — stops repeat alerts
async function once(env, key, ttl = 2 * DAY) {
  if (await env.STATE.get(key)) return false;
  await env.STATE.put(key, "1", { expirationTtl: ttl });
  return true;
}

async function fetchJson(url) {
  const r = await fetch(url, { headers: { "user-agent": "Mozilla/5.0 stock-scout-alerts" }, signal: AbortSignal.timeout(8000) });
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json();
}

function nyParts(d) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
    weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(d).map((x) => [x.type, x.value]));
  return p;
}

function nyDate(d) {
  const p = nyParts(d);
  return `${p.year}-${p.month}-${p.day}`;
}

function marketOpen(d) {  // 9:30am–4:05pm New York time, Mon–Fri (holidays caught by the stale-quote check)
  const p = nyParts(d);
  const mins = Number(p.hour) * 60 + Number(p.minute);
  return !["Sat", "Sun"].includes(p.weekday) && mins >= 570 && mins <= 965;
}

const pct = (x) => `${x > 0 ? "+" : ""}${(x * 100).toFixed(1)}%`;
const usd = (x) => `$${Number(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const usdShort = (x) => (x >= 1e6 ? `$${(x / 1e6).toFixed(1)}M` : `$${Math.round(x / 1e3)}K`);
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
