const API = (document.body.dataset.api || "").replace(/\/$/, "");

function cleanTicker(raw) {
  const t = String(raw || "").trim();
  if (!t || /[{}\s]/.test(t)) return "";
  if (!/^[A-Za-z0-9][A-Za-z0-9.\-]{0,23}$/.test(t)) return "";
  return t.toUpperCase();
}

function activeTicker() {
  return cleanTicker($("ticker") && $("ticker").value);
}

function rememberTicker(raw) {
  const t = cleanTicker(raw);
  if (!t) return "";
  const box = $("ticker");
  if (box) box.value = t;
  document.body.dataset.ticker = t;
  try { sessionStorage.setItem("desk_ticker", t); } catch { /* private mode */ }
  return t;
}
const $ = (id) => document.getElementById(id);
const state = {
  rows: [],
  poll: null,
  charts: [],
  sparkJobs: 0,
  view: "home",
  optRows: [],
  optSort: { key: "premium", dir: "desc" },
  volPayload: null,
  volSort: { key: "notional", dir: "desc" },
  boardCache: {},
  shellUni: null,
  portCache: {},
  boardAbort: null,
  pollBusy: false,
  scanHideTimer: null,
  scanDismissed: null,
  scanCursor: {},
};

const UNIVERSE_IDS = ["mega", "global", "nasdaq100", "sp500", "midcap", "smallcap"];

const UNIVERSE_LABEL = {
  mega: "Mega liquid",
  global: "Mega + ADRs",
  nasdaq100: "Nasdaq-100",
  sp500: "S&P 500 + ADRs",
  midcap: "Mid-cap $2B–$10B",
  smallcap: "Small-cap $700M–$2B",
};

const SENTIMENT_LABEL = {
  "HIGH BUY": "Highly bullish",
  BUY: "Bullish",
  HOLD: "Hold",
  SELL: "Sell",
};

const RATING_ORDER = { "HIGH BUY": 4, BUY: 3, HOLD: 2, SELL: 1 };

const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ESC_MAP[c]);
}

function toast(msg, kind = "info", ttl = 6500) {
  const host = $("toasts");
  if (!host || !msg) return;
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<span class="toast-msg">${esc(msg)}</span>
    <button type="button" class="toast-x" aria-label="Dismiss">✕</button>`;
  const close = () => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 220);
  };
  el.querySelector(".toast-x").addEventListener("click", close);
  host.appendChild(el);
  while (host.children.length > 4) host.firstChild.remove();
  if (ttl) setTimeout(close, ttl);
}

function setBusy(target, on) {
  const btn = typeof target === "string" ? $(target) : target;
  if (!btn) return;
  btn.classList.toggle("is-busy", !!on);
  btn.disabled = !!on;
}

function skeletonCards(el, n = 8) {
  if (!el) return;
  el.innerHTML = Array.from({ length: n }, () => '<div class="skeleton skeleton-card"></div>').join("");
}

function skeletonRows(el, n = 8) {
  if (!el) return;
  const rows = Array.from({ length: n }, () => '<div class="skeleton skeleton-row"></div>').join("");
  el.innerHTML = `<div class="table-card" style="padding:.7rem">${rows}</div>`;
}

function setupChartDefaults() {
  if (typeof Chart === "undefined") return;
  Chart.defaults.color = "#8b97a8";
  Chart.defaults.borderColor = "rgba(255,255,255,.05)";
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.font.size = 11;
  Chart.defaults.responsive = true;
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.animation = { duration: 320 };
  Chart.defaults.interaction = { mode: "index", intersect: false };
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.plugins.legend.labels.boxWidth = 8;
  Object.assign(Chart.defaults.plugins.tooltip, {
    backgroundColor: "#161d26",
    borderColor: "#232b36",
    borderWidth: 1,
    titleColor: "#f3f7fb",
    bodyColor: "#dbe2ec",
    padding: 10,
    cornerRadius: 8,
    usePointStyle: true,
  });
}

function cmpVals(x, y) {
  const nx = Number(x);
  const ny = Number(y);
  if (Number.isFinite(nx) && Number.isFinite(ny) && x !== "" && y !== "") return nx - ny;
  return String(x == null ? "" : x).localeCompare(String(y == null ? "" : y));
}

function sortByKey(rows, key, dir) {
  const out = rows.slice();
  out.sort((a, b) => (dir === "asc" ? 1 : -1) * cmpVals(a[key], b[key]));
  return out;
}

function ensureSort(sort, cols, fallback) {
  if (!cols.some((c) => c.key === sort.key)) {
    sort.key = fallback;
    sort.dir = "desc";
  }
  return sort;
}

function sortableHead(cols, sort) {
  return cols.map((c) => {
    const cls = ["sortable"];
    if (c.num) cls.push("num");
    if (sort.key === c.key) cls.push(sort.dir);
    return `<th class="${cls.join(" ")}" data-key="${c.key}" title="Sort by ${c.label}">${c.label}</th>`;
  }).join("");
}

function bindSortHeaders(el, sort, repaint) {
  el.querySelectorAll("th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.key;
      if (sort.key === key) sort.dir = sort.dir === "asc" ? "desc" : "asc";
      else {
        sort.key = key;
        sort.dir = "desc";
      }
      repaint();
    });
  });
}

function bindTickerLinks(el) {
  el.querySelectorAll("[data-open]").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      openTicker(a.dataset.open);
    });
  });
}

function ratingClass(r) {
  return "rating-" + String(r || "").replace(/\s+/g, "");
}

function fmt(n, d = 2) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });
}

function pct(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${(Number(n) * 100).toFixed(0)}%`;
}

const BULLISH_PHRASES = [
  "pt raised", "price target raised", "all-time high", "guidance raise",
  "better-than-expected", "top-line beat", "contract win", "beats estimates",
  "tops estimates", "raises guidance", "raised guidance",
];
const BEARISH_PHRASES = [
  "guidance cut", "pt cut", "price target cut", "sell rating", "cause of death",
  "passed away", "loses life", "lost his life", "lost her life", "dies at",
  "dead at", "killed in", "shot dead",
];
const BULLISH_WORDS = [
  "beat", "beats", "surge", "surges", "rally", "rallies", "upgrade", "upgraded",
  "record", "bullish", "growth", "profit", "profits", "outperform", "buyback",
  "raise", "raised", "raises", "strong", "soar", "soars", "breakout", "upside",
  "expansion", "accelerate", "accelerates", "optimism", "overweight", "initiate",
  "initiated",
];
const BEARISH_WORDS = [
  "miss", "misses", "plunge", "plunges", "downgrade", "downgraded", "lawsuit",
  "bearish", "layoff", "layoffs", "cut", "cuts", "warning", "weak", "fraud",
  "probe", "investigation", "crash", "selloff", "underperform", "delay",
  "delayed", "recall", "bankruptcy", "default", "missed", "short", "overvalued",
  "slowdown", "contraction", "disappoint", "fear", "underweight", "reduce",
  "death", "died", "dies", "dying", "killed", "killing", "murder", "murdered",
  "slain", "obituary", "casualty", "casualties", "funeral", "assassination",
  "massacre", "war", "wars", "invasion", "airstrike", "sanctions", "tariff",
  "tariffs", "hostage", "hostages", "ceasefire", "conflict", "missile",
];
const HARD_NEGATIVE_RE = /\b(death|died|dies|dying|killed|killings|killing|murder|murdered|slain|obituary|casualt(?:y|ies)|funeral|assassination|massacre)\b/i;

function wordHit(text, word) {
  return new RegExp(`\\b${word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "i").test(text);
}

function headlineLexiconHits(title) {
  const t = String(title || "").toLowerCase().trim();
  if (!t) return { bull: 0, bear: 0 };
  if (HARD_NEGATIVE_RE.test(t) || BEARISH_PHRASES.some((p) => t.includes(p))) {
    return { bull: 0, bear: 3 };
  }
  let bull = BULLISH_PHRASES.filter((p) => t.includes(p)).length;
  let bear = BEARISH_PHRASES.filter((p) => t.includes(p)).length;
  for (const w of BULLISH_WORDS) if (wordHit(t, w)) bull += 1;
  for (const w of BEARISH_WORDS) if (wordHit(t, w)) bear += 1;
  return { bull, bear };
}

function headlineSentiment(title) {
  const { bull, bear } = headlineLexiconHits(title);
  if (bull > bear) return "positive";
  if (bear > bull) return "negative";
  return "neutral";
}

function signedTone(n, pos = 0, neg = -pos) {
  const v = Number(n);
  if (Number.isNaN(v)) return "";
  if (v > pos) return "pos";
  if (v < neg) return "neg";
  return "mid";
}

function kpiMeter(n) {
  const w = Math.round(Math.min(100, Math.max(0, Number(n) * 100)));
  if (Number.isNaN(w)) return "";
  return `<div class="conf-meter"><div style="width:${w}%"></div></div>`;
}

function renderNewsList(items) {
  if (!items || !items.length) return '<li class="news-empty">No headlines available.</li>';
  return items.map((n) => {
    const sent = headlineSentiment(n.title);
    const tag = sent === "positive"
      ? '<span class="news-tag positive">Positive</span>'
      : sent === "negative"
        ? '<span class="news-tag negative">Negative</span>'
        : "";
    const t = n.title || "";
    const p = n.publisher || "";
    const title = n.url
      ? `<a href="${n.url}" target="_blank" rel="noreferrer">${t}</a>`
      : t;
    return `<li class="news-item"><div class="news-row">${tag}<span class="news-title">${title}</span></div>${p ? `<span class="news-pub">${p}</span>` : ""}</li>`;
  }).join("");
}

function sma(values, window) {
  const out = new Array(values.length).fill(null);
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= window) sum -= values[i - window];
    if (i >= window - 1) out[i] = sum / window;
  }
  return out;
}

async function api(path, opts = {}) {
  const urls = [API + path];
  const base = path.split("?")[0];
  if (/^\/(financials|options|volume|portfolios|etfs|quote|search|analyze)/.test(base)) {
    urls.push(window.location.origin + "/api" + path);
  }
  let lastErr = new Error("Request failed");
  for (const url of urls) {
    try {
      const res = await fetch(url, {
        headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
        ...opts,
      });
      const text = await res.text();
      let data;
      try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
      if (res.ok) return data;
      let detail = data.detail || data.message || res.statusText;
      if (typeof detail === "string" && /<!doctype html>/i.test(detail.trim())) {
        detail = `Server error (${res.status}). Restart python app.py if this page was updated.`;
      }
      lastErr = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      if (res.status !== 404 && res.status !== 405) break;
    } catch (err) {
      if (err && err.name === "AbortError") throw err;
      lastErr = err;
      const msg = String(err && err.message || "");
      if (/failed to fetch|networkerror|load failed/i.test(msg)) {
        lastErr = new Error("Connection dropped (scan batch timed out). Retrying — names already scored stay on the board.");
      }
    }
  }
  throw lastErr;
}

function killCharts() {
  state.charts.forEach((c) => c.destroy());
  state.charts = [];
}

function money(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const v = Number(n);
  const abs = Math.abs(v);
  const sign = v < 0 ? "-" : "";
  if (abs >= 1e12) return sign + "$" + (abs / 1e12).toFixed(2) + "T";
  if (abs >= 1e9) return sign + "$" + (abs / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return sign + "$" + (abs / 1e6).toFixed(2) + "M";
  if (abs >= 1e3) return sign + "$" + (abs / 1e3).toFixed(1) + "K";
  return sign + "$" + abs.toFixed(0);
}

function quoteHtml(q) {
  if (!q || q.price == null) return "";
  const up = Number(q.change || 0) >= 0;
  const chg = q.change == null ? "" : `${up ? "+" : ""}${fmt(q.change)}`;
  const pctChg = q.change_pct == null ? "" : ` (${up ? "+" : ""}${(Number(q.change_pct) * 100).toFixed(2)}%)`;
  return `<span class="quote-sym">${q.ticker}</span>
    <span class="quote-px">${fmt(q.price)}</span>
    <span class="quote-chg ${up ? "flag-buy" : "flag-sell"}">${chg}${pctChg}</span>
    <span class="hint">${q.as_of || "live"}</span>`;
}

function renderQuote(q) {
  const el = $("live-quote");
  if (!el) return;
  if (!q || q.price == null) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  el.innerHTML = quoteHtml(q);
  const dive = $("dive-live-px");
  if (dive) dive.innerHTML = quoteHtml(q);
  const fin = $("fin-live-px");
  if (fin) fin.innerHTML = quoteHtml(q);
  document.querySelectorAll(`.name-card[data-ticker="${q.ticker}"] .live-px`).forEach((span) => {
    const up = Number(q.change || 0) >= 0;
    span.textContent = fmt(q.price);
    span.classList.toggle("flag-buy", up);
    span.classList.toggle("flag-sell", !up);
  });
}

const lookupMem = new Map();
const quoteWait = new Map();

function lookupKey(kind, parts) {
  return "desk_" + kind + "_" + parts.map((p) => String(p || "")).join("|");
}

function readLookup(key, ttlMs) {
  const mem = lookupMem.get(key);
  if (mem && Date.now() - mem.at < ttlMs) return mem.body;
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    const data = JSON.parse(raw);
    if (!data || data.body == null || Date.now() - Number(data.at || 0) > ttlMs) {
      sessionStorage.removeItem(key);
      return null;
    }
    lookupMem.set(key, data);
    return data.body;
  } catch {
    return null;
  }
}

function writeLookup(key, body) {
  const data = { at: Date.now(), body };
  lookupMem.set(key, data);
  try { sessionStorage.setItem(key, JSON.stringify(data)); } catch { /* quota */ }
}

async function showQuote(ticker) {
  const t = cleanTicker(ticker);
  if (!t) return;
  const key = lookupKey("quote", [t]);
  const cached = readLookup(key, 20000);
  if (cached) {
    state.quoteTicker = cached.ticker || t;
    renderQuote(cached);
    startQuotePoll();
    return cached;
  }
  if (quoteWait.has(t)) return quoteWait.get(t);
  const job = (async () => {
    state.quoteTicker = t;
    startQuotePoll();
    try {
      const q = await api("/quote?ticker=" + encodeURIComponent(t));
      if (q && q.price != null) writeLookup(key, q);
      if (q && q.ticker) state.quoteTicker = q.ticker;
      renderQuote(q);
      return q;
    } catch {
      return null;
    }
  })();
  quoteWait.set(t, job);
  try { return await job; } finally { quoteWait.delete(t); }
}

function startQuotePoll() {
  if (state.quotePoll) return;
  state.quotePoll = setInterval(() => {
    const t = cleanTicker(state.quoteTicker) || activeTicker();
    if (t) showQuote(t);
  }, 20000);
}

const VIEW_META = {
  home: { title: "AI Equities Desk", sub: "Home · ratings for the selected universe" },
  dive: { title: "Analysis", sub: "Ticker analysis" },
  options: { title: "Options · AI Equities Desk", sub: "Options · Call/Put premium ≥ $1M" },
  volume: { title: "Volume · AI Equities Desk", sub: "Volume · concentrated $10M+ prints" },
  financials: { title: "Financials · AI Equities Desk", sub: "Financials · statements + stance" },
  portfolios: { title: "Portfolios · AI Equities Desk", sub: "Portfolios · four risk tiers per horizon" },
  etfs: { title: "ETFs · AI Equities Desk", sub: "ETFs · funds expected to outrun the S&P 500" },
};

function setNav(name) {
  document.querySelectorAll("#main-nav a, #mobile-nav a").forEach((a) => {
    a.classList.toggle("active", a.dataset.view === name);
  });
}

function applyViewMode(name) {
  document.body.classList.toggle("mode-portfolios", name === "portfolios" || name === "etfs");
  const form = $("analyze-form");
  if (form) form.hidden = name === "portfolios" || name === "etfs";
  if (name === "portfolios") syncPortUniverse();
}

function syncPortUniverse() {
  const pu = $("port-universe");
  const u = $("universe");
  if (pu && u) pu.value = u.value;
}

function setPortUniverse(val) {
  const u = $("universe");
  const pu = $("port-universe");
  if (u) u.value = val;
  if (pu) pu.value = val;
  localStorage.setItem("desk_universe", val);
}

function setView(name, { ticker } = {}) {
  state.view = name;
  ["home", "dive", "options", "volume", "financials", "portfolios", "etfs"].forEach((key) => {
    const el = $("view-" + key);
    if (!el) return;
    el.hidden = key !== name;
    if (key === name) {
      el.classList.remove("view");
      void el.offsetWidth;
      el.classList.add("view");
    }
  });
  const meta = VIEW_META[name] || VIEW_META.home;
  $("page-sub").textContent = ticker && name === "dive" ? `${ticker} · analysis` : meta.sub;
  $("home-btn").classList.toggle("active", name === "home");
  setNav(name);
  applyViewMode(name);
  document.title = ticker && name !== "home" ? `${ticker} · ${meta.title}` : meta.title;
}

function tapeYM() {
  const now = new Date();
  const year = now.getFullYear();
  const month = now.getMonth() + 1;
  const label = now.toLocaleString("en-US", { month: "short", year: "numeric" });
  return { year, month, label };
}

function ymQuery() {
  const { year, month } = tapeYM();
  return `&year=${year}&month=${month}`;
}

function updatePeriodTags() {
  const ym = tapeYM();
  const label = `Period: ${ym.label} (auto)`;
  const op = $("options-period");
  const vol = $("volume-period");
  if (op) op.textContent = label;
  if (vol) vol.textContent = label;
}

function setOptionsAsOf(asOf) {
  const el = $("options-as-of");
  if (!el) return;
  if (!asOf) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = `Updated: ${asOf}`;
}

function finFreq() {
  const saved = localStorage.getItem("desk_fin_freq") || "quarterly";
  return saved === "annual" ? "annual" : "quarterly";
}

// Both return null until the user picks a period, so the backend can serve the
// newest filed statement instead of an unfiled current quarter.
function finQuarter() {
  const saved = Number(localStorage.getItem("desk_fin_quarter") || 0);
  return [1, 2, 3, 4].includes(saved) ? saved : null;
}

function finYear() {
  const saved = Number(localStorage.getItem("desk_fin_year") || 0);
  return saved >= 1990 ? saved : null;
}

function setFinFreq(freq) {
  const f = freq === "annual" ? "annual" : "quarterly";
  localStorage.setItem("desk_fin_freq", f);
  document.querySelectorAll("#fin-freq button").forEach((b) => {
    b.classList.toggle("active", b.dataset.freq === f);
  });
  const wrap = $("fin-quarter-wrap");
  if (wrap) wrap.hidden = f === "annual";
}

function markFinQuarter(n) {
  document.querySelectorAll("#fin-quarter button").forEach((b) => {
    b.classList.toggle("active", Number(b.dataset.quarter) === Number(n));
  });
}

function setFinQuarter(q) {
  const n = Number(q);
  if (![1, 2, 3, 4].includes(n)) return;
  localStorage.setItem("desk_fin_quarter", String(n));
  markFinQuarter(n);
}

function markFinYear(n) {
  const sel = $("fin-year");
  if (sel && sel.querySelector(`option[value="${n}"]`)) sel.value = String(n);
}

function setFinYear(y) {
  const n = Number(y);
  if (!Number.isFinite(n)) return;
  localStorage.setItem("desk_fin_year", String(n));
  markFinYear(n);
}

function fillFinYears(periods, freq, { persist = true } = {}) {
  const sel = $("fin-year");
  if (!sel) return;
  const now = new Date().getFullYear();
  let years = [...new Set((periods || []).map((p) => Number(p.year)).filter((y) => y >= 1990))];
  if (!years.length) {
    years = Array.from({ length: 8 }, (_, i) => now - i);
  }
  years.sort((a, b) => b - a);
  const cur = finYear();
  const pick = years.includes(cur) ? cur : years[0];
  sel.innerHTML = years.map((y) => `<option value="${y}"${y === pick ? " selected" : ""}>${y}</option>`).join("");
  if (persist) setFinYear(pick);
  else markFinYear(pick);
}

function finPeriodQuery() {
  const freq = finFreq();
  let q = `&freq=${encodeURIComponent(freq)}`;
  const year = finYear();
  const quarter = finQuarter();
  if (year) q += `&year=${year}`;
  if (freq === "quarterly" && quarter) q += `&quarter=${quarter}`;
  return q;
}

function finPeriodLabel() {
  const freq = finFreq();
  const year = finYear();
  const quarter = finQuarter();
  if (!year) return "the latest";
  return freq === "quarterly" && quarter ? `Q${quarter} ${year}` : `FY ${year}`;
}

function syncFinPeriodFromResponse(data) {
  if (!data) return;
  if (data.freq) setFinFreq(data.freq);
  if (data.available_periods) fillFinYears(data.available_periods, data.freq);
  if (data.selected) {
    if (data.selected.quarter) setFinQuarter(data.selected.quarter);
    if (data.selected.year) setFinYear(data.selected.year);
  } else {
    if (data.quarter) setFinQuarter(data.quarter);
    if (data.year) setFinYear(data.year);
  }
}

function minPrint() {
  const el = $("min-print");
  const v = el ? Number(el.value) : 10000000;
  return Number.isFinite(v) && v > 0 ? v : 10000000;
}

function minPrintQuery() {
  return "&min_notional=" + encodeURIComponent(String(minPrint()));
}

function showHome(push = true) {
  setView("home");
  if (push) history.pushState({ view: "home" }, "", "/");
}

function pageUrl(name, ticker) {
  const t = cleanTicker(ticker);
  if (!name || name === "home") return "/";
  if (name === "financials") return t ? "/financials/" + encodeURIComponent(t) : "/financials";
  if (name === "dive" || name === "ticker") return t ? tickerUrl(t) : "/";
  if ((name === "options" || name === "volume") && t) return "/" + name + "?ticker=" + encodeURIComponent(t);
  return "/" + name;
}

function showPage(name, push = true) {
  const t = activeTicker();
  if (t) rememberTicker(t);
  setView(name);
  if (push) history.pushState({ view: name, ticker: t }, "", pageUrl(name, t));
  updatePeriodTags();
  if (name === "options") loadOptions();
  if (name === "volume") loadVolume();
  if (name === "portfolios") loadPortfolios();
  if (name === "etfs") loadEtfs(false);
  if (name === "financials") {
    setFinFreq(finFreq());
    if (t) loadFinancials(t);
  }
}

function tickerUrl(ticker) {
  const params = new URLSearchParams({
    period: $("period").value,
    horizon: $("horizon").value,
  });
  return "/ticker/" + encodeURIComponent(ticker) + "?" + params.toString();
}

function openTicker(ticker, { push = true } = {}) {
  const t = rememberTicker(ticker);
  if (!t) return;
  pushRecent(t);
  showQuote(t);
  setView("dive", { ticker: t });
  if (push) history.pushState({ view: "dive", ticker: t }, "", tickerUrl(t));
  loadDive(t);
}

function gotoView(name) {
  const t = activeTicker();
  if (t) rememberTicker(t);
  if (name === "home") {
    showHome(true);
    return;
  }
  showPage(name, true);
}

function runForCurrentView(ticker) {
  const t = rememberTicker(ticker || activeTicker());
  if (!t) {
    $("resolve-hint").textContent = "Type a ticker first.";
    $("ticker").focus();
    return;
  }
  pushRecent(t);
  showQuote(t);
  if (state.view === "financials") loadFinancials(t);
  else if (state.view === "options") loadOptionsTicker(t);
  else if (state.view === "volume") loadVolumeTicker(t);
  else openTicker(t);
}

function reloadCurrentView() {
  const t = activeTicker();
  if (t) rememberTicker(t);
  if (state.view === "options") loadOptions();
  else if (state.view === "volume") loadVolume();
  else if (state.view === "portfolios") loadPortfolios(false);
  else if (state.view === "etfs") loadEtfs(false);
  else if (state.view === "financials" && t) loadFinancials(t);
  else if (state.view === "dive" && t) loadDive(t);
  else loadBoard().catch((err) => toast(err.message, "err"));
}

/* ---------- recent tickers ---------- */
function recentTickers() {
  try {
    const list = JSON.parse(localStorage.getItem("desk_recent") || "[]");
    const clean = (Array.isArray(list) ? list : []).map(cleanTicker).filter(Boolean);
    const uniq = [...new Set(clean)].slice(0, 8);
    if (uniq.length !== (Array.isArray(list) ? list.length : 0)) {
      localStorage.setItem("desk_recent", JSON.stringify(uniq));
    }
    return uniq;
  } catch {
    return [];
  }
}

function pushRecent(ticker) {
  const sym = cleanTicker(ticker);
  if (!sym) return;
  const list = [sym, ...recentTickers().filter((t) => t !== sym)].slice(0, 8);
  localStorage.setItem("desk_recent", JSON.stringify(list));
  renderRecents();
}

function renderRecents() {
  const host = $("recents");
  if (!host) return;
  const list = recentTickers();
  host.innerHTML = list.length
    ? '<span class="recents-lbl">Recent</span>' + list.map((t) =>
        `<button type="button" class="recent-chip" data-recent="${esc(t)}">${esc(t)}</button>`
      ).join("")
    : "";
  host.querySelectorAll("[data-recent]").forEach((b) => {
    b.addEventListener("click", () => {
      $("ticker").value = b.dataset.recent;
      runForCurrentView(b.dataset.recent);
    });
  });
}

/* ---------- ticker autocomplete ---------- */
const ac = { items: [], idx: -1, timer: null, open: false, seq: 0 };

function acClose() {
  const menu = $("ticker-ac");
  ac.items = [];
  ac.idx = -1;
  ac.open = false;
  if (!menu) return;
  menu.hidden = true;
  menu.innerHTML = "";
  const input = $("ticker");
  if (input) input.setAttribute("aria-expanded", "false");
}

function acPaint() {
  const menu = $("ticker-ac");
  if (!menu) return;
  if (!ac.items.length) {
    acClose();
    return;
  }
  menu.innerHTML = '<div class="ac-head">Matches · ↑↓ then Enter</div>' + ac.items.map((it, i) =>
    `<button type="button" class="ac-item${i === ac.idx ? " active" : ""}" data-sym="${esc(it.symbol)}" role="option">
      <span class="ac-sym">${esc(it.symbol)}</span>
      <span class="ac-name">${esc(it.shortname || it.longname || "")}</span>
      <span class="ac-ex">${esc(it.exchange || "")}</span>
    </button>`
  ).join("");
  menu.hidden = false;
  ac.open = true;
  $("ticker").setAttribute("aria-expanded", "true");
  menu.querySelectorAll("[data-sym]").forEach((b) => {
    b.addEventListener("mousedown", (e) => {
      e.preventDefault();
      acPick(b.dataset.sym);
    });
  });
}

function acPick(sym) {
  if (!sym) return;
  $("ticker").value = sym;
  acClose();
  runForCurrentView(sym);
}

function acMove(delta) {
  if (!ac.items.length) return;
  ac.idx = (ac.idx + delta + ac.items.length) % ac.items.length;
  acPaint();
}

function acSearch(q) {
  clearTimeout(ac.timer);
  const query = q.trim();
  if (query.length < 2) {
    acClose();
    return;
  }
  const cached = readLookup(lookupKey("search", [query.toUpperCase()]), 6 * 60 * 60 * 1000);
  if (cached) {
    ac.items = cached;
    ac.idx = -1;
    acPaint();
    return;
  }
  const seq = ++ac.seq;
  ac.timer = setTimeout(async () => {
    try {
      const data = await api("/search?q=" + encodeURIComponent(query));
      if (seq !== ac.seq) return;
      ac.items = (data.quotes || []).filter((x) => x && x.symbol).slice(0, 8);
      writeLookup(lookupKey("search", [query.toUpperCase()]), ac.items);
      ac.idx = -1;
      acPaint();
    } catch {
      acClose();
    }
  }, 220);
}

/* ---------- shortcuts sheet ---------- */
function toggleShortcuts(show) {
  const sheet = $("shortcut-sheet");
  if (!sheet) return;
  sheet.hidden = show === undefined ? !sheet.hidden : !show;
}

function isTyping(e) {
  const el = e.target;
  if (!el) return false;
  return el.tagName === "INPUT" || el.tagName === "SELECT" || el.tagName === "TEXTAREA" || el.isContentEditable;
}

function bindShortcuts() {
  let gAt = 0;
  const GO = { h: "home", o: "options", v: "volume", f: "financials", p: "portfolios", e: "etfs" };
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      acClose();
      toggleShortcuts(false);
      if (document.activeElement === $("ticker")) $("ticker").blur();
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey || isTyping(e)) return;
    if (e.key === "/") {
      e.preventDefault();
      $("ticker").focus();
      $("ticker").select();
      return;
    }
    if (e.key === "?") {
      e.preventDefault();
      toggleShortcuts();
      return;
    }
    const key = e.key.toLowerCase();
    if (Date.now() - gAt < 1200 && GO[key]) {
      e.preventDefault();
      gAt = 0;
      gotoView(GO[key]);
      return;
    }
    if (key === "g") {
      gAt = Date.now();
      return;
    }
    if (key === "r") {
      e.preventDefault();
      reloadCurrentView();
    }
  });
}

function resetFilters() {
  $("f-rating").value = "ALL";
  $("f-sector").value = "ALL";
  $("f-conf").value = "0";
  $("f-conf-lbl").textContent = "0%";
  $("f-q").value = "";
  renderBoard();
}

function clearFilter(which) {
  if (which === "rating") $("f-rating").value = "ALL";
  if (which === "sector") $("f-sector").value = "ALL";
  if (which === "conf") {
    $("f-conf").value = "0";
    $("f-conf-lbl").textContent = "0%";
  }
  if (which === "q") $("f-q").value = "";
  renderBoard();
}

function renderFilterChips() {
  const host = $("filter-chips");
  if (!host) return;
  const chips = [];
  const rating = $("f-rating").value;
  const sector = $("f-sector").value;
  const conf = Number($("f-conf").value);
  const q = $("f-q").value.trim();
  if (rating && rating !== "ALL") chips.push(["rating", "Rating", rating]);
  if (sector && sector !== "ALL") chips.push(["sector", "Sector", sector]);
  if (conf > 0) chips.push(["conf", "Min confidence", conf + "%"]);
  if (q) chips.push(["q", "Search", q]);
  host.innerHTML = chips.map(([id, lbl, val]) =>
    `<span class="f-chip">${lbl}: <b>${esc(val)}</b>
      <button type="button" data-clear="${id}" aria-label="Clear ${esc(lbl)} filter">✕</button></span>`
  ).join("");
  host.querySelectorAll("[data-clear]").forEach((b) => {
    b.addEventListener("click", () => clearFilter(b.dataset.clear));
  });
}

function drawSpark(canvas, points) {
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 280;
  const cssH = canvas.clientHeight || 64;
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, cssW, cssH);
  const vals = (points || []).map((p) => Number(p.close)).filter((v) => Number.isFinite(v));
  if (vals.length < 2) return;
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const span = max - min || 1;
  const pad = 6;
  const w = cssW - pad * 2;
  const h = cssH - pad * 2;
  ctx.beginPath();
  vals.forEach((v, i) => {
    const x = pad + (i / (vals.length - 1)) * w;
    const y = pad + (1 - (v - min) / span) * h;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = vals[vals.length - 1] >= vals[0] ? "#3ddc97" : "#ff6b6b";
  ctx.lineWidth = 1.6;
  ctx.stroke();
}

function renderKpis(counts, n, asOf, universe, extra = {}) {
  const session = extra.session;
  const lastSess = extra.last_session;
  const stale = extra.stale;
  const asOfLabel = session
    ? (stale ? `${session} (behind ${lastSess})` : session)
    : (asOf || "—");
  const items = [
    { lbl: "Universe", val: UNIVERSE_LABEL[universe] || universe || "—", cls: "universe" },
    { lbl: "Names", val: String(n || 0) },
    { lbl: "High Buy", val: String(counts["HIGH BUY"] || 0), cls: "pos", filter: "HIGH BUY" },
    { lbl: "Buy", val: String(counts["BUY"] || 0), cls: "pos", filter: "BUY" },
    { lbl: "Hold", val: String(counts["HOLD"] || 0), cls: "mid", filter: "HOLD" },
    { lbl: "Sell", val: String(counts["SELL"] || 0), cls: "neg", filter: "SELL" },
    { lbl: stale ? "Last close · stale" : "Last close", val: asOfLabel, cls: stale ? "neg small" : "small" },
  ];
  $("kpis").innerHTML = items.map((it) => {
    const cls = `kpi ${it.cls || ""}${it.filter ? " kpi-click" : ""}`;
    const attrs = it.filter
      ? ` data-filter="${esc(it.filter)}" role="button" tabindex="0" title="Filter the board to ${esc(it.filter)}"`
      : "";
    return `<div class="${cls}"${attrs}>
      <div class="lbl">${it.lbl}</div><div class="val">${esc(it.val)}</div>
    </div>`;
  }).join("");
  $("kpis").querySelectorAll("[data-filter]").forEach((el) => {
    const apply = () => {
      const cur = $("f-rating").value;
      $("f-rating").value = cur === el.dataset.filter ? "ALL" : el.dataset.filter;
      renderBoard();
    };
    el.addEventListener("click", apply);
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        apply();
      }
    });
  });
  syncKpiActive();
}

function syncKpiActive() {
  const rating = $("f-rating") ? $("f-rating").value : "ALL";
  document.querySelectorAll("#kpis [data-filter]").forEach((el) => {
    el.classList.toggle("is-on", el.dataset.filter === rating);
  });
}

function boardSortKey() {
  const el = $("f-sort");
  return el ? el.value : "score";
}

function sortBoardRows(rows) {
  const key = boardSortKey();
  const out = rows.slice();
  if (key === "ticker") {
    out.sort((a, b) => String(a.ticker || "").localeCompare(String(b.ticker || "")));
  } else if (key === "rating") {
    out.sort((a, b) =>
      (RATING_ORDER[b.rating] || 0) - (RATING_ORDER[a.rating] || 0) ||
      Number(b.score || 0) - Number(a.score || 0)
    );
  } else {
    out.sort((a, b) => Number(b[key] || 0) - Number(a[key] || 0));
  }
  return out;
}

function fillSectors(rows) {
  const sel = $("f-sector");
  if (!sel) return;
  const current = sel.value || "ALL";
  const sectors = [...new Set(rows.map((r) => String(r.sector || "—")))].sort();
  sel.innerHTML = `<option value="ALL">All</option>` + sectors.map((s) =>
    `<option ${s === current ? "selected" : ""}>${s}</option>`
  ).join("");
  if (![...sel.options].some((o) => o.value === current)) sel.value = "ALL";
}

function sparkPoints(row) {
  if (Array.isArray(row.spark) && row.spark.length) return row.spark;
  if (typeof row.spark === "string") {
    try {
      const parsed = JSON.parse(row.spark);
      if (Array.isArray(parsed)) return parsed;
    } catch { /* ignore */ }
  }
  return [];
}

function renderBoard() {
  const rating = $("f-rating").value;
  const sector = $("f-sector").value;
  const minConf = Number($("f-conf").value) / 100;
  const q = $("f-q").value.trim().toUpperCase();
  let rows = state.rows.slice();
  if (rating && rating !== "ALL") rows = rows.filter((r) => r.rating === rating);
  if (sector && sector !== "ALL") rows = rows.filter((r) => String(r.sector || "—") === sector);
  if (minConf > 0) rows = rows.filter((r) => Number(r.confidence || 0) >= minConf);
  if (q) {
    rows = rows.filter((r) =>
      String(r.ticker || "").toUpperCase().includes(q) ||
      String(r.name || "").toUpperCase().includes(q)
    );
  }
  renderFilterChips();
  syncKpiActive();
  const list = $("board-list");
  const total = state.rows.length;
  if (!total) {
    $("board-empty").hidden = false;
    $("board-count").textContent = "";
    list.innerHTML = "";
    return;
  }
  rows = sortBoardRows(rows);
  $("board-count").innerHTML = rows.length === total
    ? `<b>${total}</b> names`
    : `<b>${rows.length}</b> of ${total} names match`;
  if (!rows.length) {
    $("board-empty").textContent = "No names match these filters. Clear one to widen the search.";
    $("board-empty").hidden = false;
    list.innerHTML = "";
    return;
  }
  $("board-empty").hidden = true;
  const openWhy = new Set(
    [...list.querySelectorAll("details[open]")].map((el) => el.closest(".name-card")?.dataset.ticker).filter(Boolean)
  );
  list.innerHTML = rows.map((r) => {
    const pending = !!r.pending;
    const why = pending ? "" : (r.why || r.thesis || "");
    const openAttr = openWhy.has(r.ticker) ? " open" : "";
    const meta = pending
      ? `<span>scoring…</span>`
      : `<span>score ${Number(r.score || 0).toFixed(3)}</span>
          <span>conf ${pct(r.confidence)}</span>
          <span class="live-px">${fmt(r.price)}</span>`;
    return `<article class="name-card${pending ? " is-pending" : ""}" data-ticker="${esc(r.ticker)}">
      <a class="name-card-link" href="${tickerUrl(r.ticker)}">
        <div class="name-card-top">
          <span class="ticker">${esc(r.ticker)}</span>
          <span class="${pending ? "rating-pending" : ratingClass(r.rating)}">${pending ? "…" : esc(r.rating)}</span>
        </div>
        <div class="name">${pending ? "Scoring this name" : `${esc(r.name || "")} · ${esc(r.sector || "")}`}</div>
        ${pending ? "" : `<canvas class="spark" width="280" height="64"></canvas>`}
        <div class="meta-row">${meta}</div>
      </a>
      ${why ? `<details${openAttr}><summary>Why this rating</summary><div class="why">${why}</div></details>` : ""}
    </article>`;
  }).join("");

  list.querySelectorAll(".name-card-link").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      openTicker(a.closest(".name-card").dataset.ticker);
    });
  });
  list.querySelectorAll(".name-card").forEach((card) => {
    const row = rows.find((r) => r.ticker === card.dataset.ticker);
    const canvas = card.querySelector("canvas.spark");
    const pts = row ? sparkPoints(row) : [];
    if (pts.length && canvas) drawSpark(canvas, pts);
  });
}

function boardCacheKey(uni) {
  return "desk_board_" + String(uni || "global").toLowerCase();
}

const BOARD_LOCAL_TTL_MS = 7 * 24 * 60 * 60 * 1000;

function slimBoard(data) {
  const rows = (data.rows || []).map((r) => {
    const copy = { ...r };
    delete copy.spark;
    delete copy.why;
    delete copy.thesis;
    delete copy.drivers;
    return copy;
  });
  return { ...data, rows, n: rows.length, slim: true, savedAt: Date.now() };
}

function readStoredBoard(storage, uni) {
  const raw = storage.getItem(boardCacheKey(uni));
  if (!raw) return null;
  const data = JSON.parse(raw);
  if (!data || !(data.rows || []).length) {
    storage.removeItem(boardCacheKey(uni));
    return null;
  }
  if (data.savedAt && Date.now() - Number(data.savedAt) > BOARD_LOCAL_TTL_MS) {
    storage.removeItem(boardCacheKey(uni));
    return null;
  }
  return data;
}

function readBoardCache(uni) {
  const hit = state.boardCache[uni];
  if (hit && (hit.rows || []).length) return hit;
  try {
    const data = readStoredBoard(sessionStorage, uni);
    if (data) {
      state.boardCache[uni] = data;
      return data;
    }
  } catch { /* quota / parse */ }
  try {
    const data = readStoredBoard(localStorage, uni);
    if (data) {
      state.boardCache[uni] = data;
      return data;
    }
  } catch { /* quota / parse */ }
  delete state.boardCache[uni];
  return null;
}

function writeBoardCache(uni, data) {
  if (!data || !uni || !(data.rows || []).length) return;
  state.boardCache[uni] = data;
  try {
    sessionStorage.setItem(boardCacheKey(uni), JSON.stringify(data));
  } catch { /* full board may exceed quota */ }
  try {
    localStorage.setItem(boardCacheKey(uni), JSON.stringify(slimBoard(data)));
  } catch { /* quota */ }
}

function prefetchBoards(except) {
  // Large universes stay on demand. Prefetching S&P / mid / small alongside a
  // switch saturates the Vercel function and makes the click feel stuck.
  const queue = ["mega", "global", "nasdaq100"].filter((u) => u !== except && !state.boardCache[u]);
  const step = () => {
    const u = queue.shift();
    if (!u) return;
    api("/board?universe=" + encodeURIComponent(u))
      .then((data) => {
        if ((data.rows || []).length) writeBoardCache(u, data);
      })
      .catch(() => {})
      .finally(() => setTimeout(step, 500));
  };
  step();
}

function shellRows(tickers) {
  return (tickers || []).map((t) => ({
    ticker: t,
    name: "",
    sector: "",
    rating: "",
    pending: true,
    score: null,
    confidence: null,
    price: null,
  }));
}

function paintShell(uni, tickers) {
  if (!tickers.length || $("universe").value !== uni) return;
  if (readBoardCache(uni)) return;
  const scored = (state.rows || []).some((r) => r && !r.pending && r.rating);
  if (scored) return;
  state.shellUni = uni;
  paintBoard(
    { universe: uni, n: tickers.length, rows: shellRows(tickers), counts: {}, as_of: "" },
    uni,
    { quiet: true, fillEmpty: false }
  );
}

async function paintUniverseShell(uni) {
  if (readBoardCache(uni)) return;
  let tickers = [];
  try {
    const res = await fetch("/universes/" + encodeURIComponent(uni) + ".json");
    if (res.ok) {
      const data = await res.json();
      tickers = data.tickers || [];
    }
  } catch { /* static list is optional */ }
  if (!tickers.length) {
    try {
      const data = await api("/universe/" + encodeURIComponent(uni));
      tickers = data.tickers || [];
    } catch { return; }
  }
  paintShell(uni, tickers);
}

async function loadBoard({ quiet = false, fillEmpty = true } = {}) {
  const uni = $("universe").value;
  localStorage.setItem("desk_universe", uni);
  const cached = readBoardCache(uni);
  if (cached) paintBoard(cached, uni, { quiet: true, fillEmpty: false });
  else if (!quiet && state.shellUni !== uni) skeletonCards($("board-list"), 6);
  const shellPromise = cached ? null : paintUniverseShell(uni);
  if (state.boardAbort) state.boardAbort.abort();
  const ac = new AbortController();
  state.boardAbort = ac;
  try {
    const data = await api("/board?universe=" + encodeURIComponent(uni), { signal: ac.signal });
    if (shellPromise) await shellPromise.catch(() => {});
    if ($("universe").value !== uni) return;
    const incoming = data.rows || [];
    const prev = readBoardCache(uni);
    const shellOnScreen = state.shellUni === uni && (state.rows || []).some((r) => r.pending);
    if (!incoming.length && prev && (prev.rows || []).length) {
      // Server has no durable board for this isolate. Keep the cached universe
      // on screen instead of wiping it and starting a cold scan.
      return;
    }
    if (!incoming.length && shellOnScreen) {
      const job = data.job || {};
      if (job.status === "running") {
        updateScanStatus(job);
        startPoll();
        return;
      }
      if (fillEmpty) refreshUniverse();
      return;
    }
    let next = data;
    const scanning = data.job && data.job.status === "running";
    const baseRows = (prev && prev.rows.length > incoming.length)
      ? prev.rows
      : (shellOnScreen ? state.rows : null);
    if (baseRows && incoming.length && incoming.length < baseRows.length && (scanning || shellOnScreen)) {
      const by = new Map(baseRows.map((r) => [r.ticker, r]));
      incoming.forEach((row) => by.set(row.ticker, { ...(by.get(row.ticker) || {}), ...row, pending: false }));
      next = { ...data, rows: [...by.values()], n: by.size };
    }
    const cacheRows = (next.rows || []).filter((r) => !r.pending);
    if (cacheRows.length) writeBoardCache(uni, { ...next, rows: cacheRows, n: cacheRows.length });
    if (cacheRows.length && cacheRows.length === (next.rows || []).length) state.shellUni = null;
    paintBoard(next, uni, { quiet, fillEmpty });
    if (cacheRows.length) prefetchBoards(uni);
  } catch (err) {
    if (err && err.name === "AbortError") return;
    if (shellPromise) await shellPromise.catch(() => {});
    if (cached || (state.shellUni === uni && (state.rows || []).length)) {
      if (!quiet) toast("Names are up. Scores will follow — " + err.message, "err", 5000);
      return;
    }
    throw err;
  }
}

function paintBoard(data, uni, { quiet = false, fillEmpty = true } = {}) {
  state.rows = data.rows || [];
  fillSectors(state.rows);
  renderKpis(data.counts || {}, data.n, data.as_of, uni, {
    session: data.session,
    last_session: data.last_session,
    stale: data.stale,
  });
  const scanning = data.job && data.job.status === "running";
  $("board-empty").textContent = state.rows.length
    ? ""
    : scanning
      ? `Scanning ${UNIVERSE_LABEL[uni] || uni}… names appear as they finish.`
      : `Loading ${UNIVERSE_LABEL[uni] || uni}…`;
  renderBoard();
  if (!quiet) updateScanStatus(data.job || {});
  if (scanning) startPoll();
  else if (fillEmpty && !state.rows.length) {
    const busy = $("scan-btn") && $("scan-btn").classList.contains("is-busy");
    if (!busy) refreshUniverse();
  }
}

function updateScanStatus(job) {
  const el = $("scan-status");
  const wrap = $("progress-wrap");
  const bar = $("progress-bar");
  if (!el) return;
  const selectedUni = $("universe").value;
  if (!job || job.status === "idle") {
    el.textContent = "";
    wrap.hidden = true;
    return;
  }
  if (job.status === "running") {
    state.scanDismissed = null;
    if (state.scanHideTimer) {
      clearTimeout(state.scanHideTimer);
      state.scanHideTimer = null;
    }
    wrap.hidden = false;
    const pctDone = job.total ? Math.round((job.progress / job.total) * 100) : 0;
    bar.style.width = pctDone + "%";
    el.textContent = `Scanning ${UNIVERSE_LABEL[job.universe] || job.universe || ""} ${job.progress}/${job.total} (${pctDone}%) — names stream in below.`;
    setBusy("scan-btn", true);
    return;
  }
  setBusy("scan-btn", false);
  if (job.status === "error") {
    el.innerHTML = `<span class="err">${esc(job.error)}</span>`;
    wrap.hidden = true;
    return;
  }
  if (job.status === "done") {
    const id = `${job.universe || ""}|${job.as_of || ""}|${job.n || 0}`;
    if (state.scanDismissed === id) {
      el.textContent = "";
      wrap.hidden = true;
      return;
    }
    bar.style.width = "100%";
    wrap.hidden = false;
    el.textContent = job.universe && job.universe !== selectedUni
      ? `Last finished: ${UNIVERSE_LABEL[job.universe] || job.universe}. Select that universe to view it.`
      : `Done · ${job.n} names · ${job.as_of || ""}`;
    if (state.scanHideTimer) clearTimeout(state.scanHideTimer);
    state.scanHideTimer = setTimeout(() => {
      el.textContent = "";
      wrap.hidden = true;
      state.scanDismissed = id;
      state.scanHideTimer = null;
    }, 2800);
  }
}

function startPoll() {
  if (state.poll) return;
  state.poll = setInterval(async () => {
    if (state.pollBusy) return;
    state.pollBusy = true;
    try {
      const uni = $("universe").value;
      const job = await api("/board/refresh", {
        method: "POST",
        body: scanBody(uni, state.scanCursor[uni] || 0),
      });
      rememberScanCursor(uni, job);
      updateScanStatus(job);
      await loadBoard({ quiet: true, fillEmpty: false });
      if (job.status === "running") return;
      clearInterval(state.poll);
      state.poll = null;
      await loadBoard({ fillEmpty: false });
      if (job.status === "done") toast(`Board ready · ${job.n} names scored.`, "ok", 4500);
      else if (job.status === "error") toast(job.error || "Scan failed.", "err");
    } catch (err) {
      if (err && err.name === "AbortError") return;
      await loadBoard({ quiet: true, fillEmpty: false }).catch(() => {});
      if ($("scan-status")) $("scan-status").textContent = "Scan still running — retrying…";
    } finally {
      state.pollBusy = false;
    }
  }, 2000);
}

function barChart(canvas, labels, values) {
  const chart = new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        data: values,
        backgroundColor: values.map((v) => (v >= 0 ? "#3ddc97" : "#ff6b6b")),
      }],
    },
    options: {
      indexAxis: "y",
      plugins: { legend: { display: false } },
      scales: {
        x: { min: -1.05, max: 1.05, ticks: { color: "#8b949e" }, grid: { color: "#30363d" } },
        y: { ticks: { color: "#c9d1d9" }, grid: { display: false } },
      },
    },
  });
  state.charts.push(chart);
}

function priceChart(canvas, candles) {
  const labels = candles.map((x) => x.date);
  const closes = candles.map((x) => x.close);
  const chart = new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "Close", data: closes, borderColor: "#58a6ff", tension: 0.15, pointRadius: 0, fill: false, borderWidth: 2 },
        { label: "SMA50", data: sma(closes, 50), borderColor: "#e3b341", tension: 0.1, pointRadius: 0, fill: false, borderWidth: 1, spanGaps: true },
        { label: "SMA200", data: sma(closes, 200), borderColor: "#8b949e", tension: 0.1, pointRadius: 0, fill: false, borderWidth: 1, spanGaps: true },
      ],
    },
    options: {
      plugins: { legend: { labels: { color: "#c9d1d9" } } },
      scales: {
        x: { ticks: { color: "#8b949e", maxTicksLimit: 8 }, grid: { color: "#21262d" } },
        y: { ticks: { color: "#8b949e" }, grid: { color: "#30363d" } },
      },
    },
  });
  state.charts.push(chart);
}

function lineChart(canvas, labels, values, color, label) {
  const chart = new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [{ label, data: values, borderColor: color, tension: 0.15, pointRadius: 0, fill: false }],
    },
    options: {
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: "#8b949e", maxTicksLimit: 8 }, grid: { color: "#21262d" } },
        y: { ticks: { color: "#8b949e" }, grid: { color: "#30363d" } },
      },
    },
  });
  state.charts.push(chart);
}

function equityCompareChart(canvas, strategy, benchmark) {
  const labels = strategy.map((x) => x.date);
  const chart = new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "ML strategy (long-only)",
          data: strategy.map((x) => x.value),
          borderColor: "#3ddc97",
          tension: 0.12,
          pointRadius: 0,
          fill: false,
          borderWidth: 2,
        },
        {
          label: "Buy & hold",
          data: (benchmark.length ? benchmark : strategy).map((x) => x.value),
          borderColor: "#8b97a8",
          borderDash: [5, 4],
          tension: 0.12,
          pointRadius: 0,
          fill: false,
          borderWidth: 1.5,
        },
      ],
    },
    options: {
      plugins: { legend: { labels: { color: "#c9d1d9", boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: "#8b949e", maxTicksLimit: 8 }, grid: { color: "#21262d" } },
        y: { ticks: { color: "#8b949e" }, grid: { color: "#30363d" } },
      },
    },
  });
  state.charts.push(chart);
}

function pctReturn(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const v = Number(n) * 100;
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(0)}%`;
}

function companyDescription(res) {
  const direct = String(res.company_blurb || "").trim();
  if (direct) return direct;
  const f = res.fundamentals || {};
  const summary = String(f.business_summary || "").trim();
  const industry = String(f.industry || "").trim();
  const sector = String(f.sector || "").trim();
  if (summary) {
    const lead = summary.split(". ")[0].trim();
    return lead.length > 260 ? `${lead.slice(0, 257)}...` : `${lead}${lead.endsWith(".") ? "" : "."}`;
  }
  if (industry && sector && !industry.toLowerCase().includes(sector.toLowerCase())) {
    return `Operates in ${industry} (${sector}).`;
  }
  if (industry) return `Operates in ${industry}.`;
  if (sector) return `${sector} company.`;
  return "";
}

function sectorLine(res) {
  const f = res.fundamentals || {};
  const parts = [f.sector, f.industry].filter(Boolean);
  return parts.length ? parts.join(" · ") : "";
}

function renderDive(res) {
  killCharts();
  const c = res.consensus || {};
  const resolved = res.queried && res.ticker && res.queried.toUpperCase() !== res.ticker
    ? `<div class="resolve">Resolved ${res.queried} → ${res.ticker}</div>` : "";
  const votes = (c.votes || []).slice().sort((a, b) => Math.abs(b.score) - Math.abs(a.score));
  const news = renderNewsList(res.news || []);
  const geo = renderNewsList(res.geo_news || []);
  const opts = (res.options || []).map((o) =>
    `<tr><td>${o.side}</td><td>${o.expiry}</td><td>${o.dte}</td><td>${fmt(o.strike)}</td><td>${fmt(o.mid)}</td><td>${o.thesis || ""}</td></tr>`
  ).join("");
  const flow = votes.find((v) => v.name === "OptionsFlow");
  const score = Number(c.score || 0);
  const scoreTone = signedTone(score, 0.10, -0.12);
  const sharpe = (res.backtest || {}).sharpe;
  const sharpeTone = signedTone(sharpe, 0, 0);
  const bt = res.backtest || {};
  const model = res.model || {};
  const pUp = model.live_p_up;
  const metaP = model.meta_p;
  const oosAuc = model.oos_auc;
  const bits = [];
  if (metaP != null && Number.isFinite(Number(metaP))) bits.push(`meta ${pct(metaP)}`);
  if (oosAuc != null && Number.isFinite(Number(oosAuc))) bits.push(`AUC ${Number(oosAuc).toFixed(2)}`);
  const pSub = bits.join(" · ") || "5-day up";
  const blurb = companyDescription(res);
  const sector = sectorLine(res);
  const blurbHtml = blurb ? `<div class="company-blurb">${blurb}</div>` : "";
  const sectorHtml = sector ? `<div class="company-sector">${sector}</div>` : "";
  $("dive").innerHTML = `
    <div class="dive-hero hero">
      ${resolved}
      <div class="dive-hero-top">
        <div class="dive-hero-main">
          <div class="sub">${res.ticker} · ${res.name || ""} · ${res.as_of || ""} · ${res.backend || ""}</div>
          ${sectorHtml}
          ${blurbHtml}
          <div id="dive-live-px" class="live-quote dive-live">${res.price != null ? `<span class="quote-px">${fmt(res.price)}</span>` : ""}</div>
          <h1 class="${ratingClass(c.rating)}">${c.rating || ""}</h1>
        </div>
      </div>
      <div class="why">${res.why || c.thesis || ""}</div>
    </div>
    <section class="kpis dive-kpis">
      <div class="kpi kpi-accent ${scoreTone}">
        <div class="lbl">Score</div>
        <div class="val">${score.toFixed(3)}</div>
        <div class="kpi-sub">${score >= 0.10 ? "Buy zone" : score <= -0.12 ? "Sell zone" : "Hold zone"}</div>
      </div>
      <div class="kpi">
        <div class="lbl">Confidence</div>
        <div class="val">${pct(c.confidence)}</div>
        ${kpiMeter(c.confidence)}
      </div>
      <div class="kpi">
        <div class="lbl">Agreement</div>
        <div class="val">${pct(c.agreement)}</div>
        ${kpiMeter(c.agreement)}
      </div>
      <div class="kpi">
        <div class="lbl">P(up)</div>
        <div class="val">${pct(pUp)}</div>
        <div class="kpi-sub">${pSub}</div>
      </div>
      <div class="kpi ${sharpeTone}">
        <div class="lbl">OOS Sharpe</div>
        <div class="val">${fmt(sharpe)}</div>
        <div class="kpi-sub">Long-only · ${pctReturn(bt.total_return)} vs ${pctReturn(bt.buy_hold_return)} B&amp;H</div>
      </div>
    </section>
    <div class="grid2 dive-charts">
      <div class="chart-box">
        <div class="chart-title"><span>Agent votes</span></div>
        <div class="chart-canvas"><canvas id="vote-chart"></canvas></div>
      </div>
      <div class="chart-box">
        <div class="chart-title"><span>Price</span><span class="hint-inline">close · SMA50 · SMA200</span></div>
        <div class="chart-canvas"><canvas id="px-chart"></canvas></div>
      </div>
    </div>
    ${res.equity && res.equity.length
      ? `<div class="chart-box dive-equity">
          <div class="chart-title"><span>Walk-forward equity</span><span class="hint-inline">Signal at the close, filled at the next open · 12 bps per side · long when P(up) ≥ 55% · dashed = buy &amp; hold</span></div>
          <div class="chart-canvas"><canvas id="eq-chart"></canvas></div>
        </div>`
      : '<p class="hint">Not enough history for a walk-forward equity curve.</p>'}
    <div class="dive-panel">
      <h3>Agent drivers</h3>
      <table class="drivers dive-drivers">
        <thead><tr><th>Agent</th><th>Score</th><th>Why</th></tr></thead>
        <tbody>${votes.map((v) => {
          const tone = signedTone(v.score, 0.15, -0.15);
          return `<tr><td>${v.name}</td><td class="${tone}">${Number(v.score).toFixed(3)}</td><td>${v.reason || ""}</td></tr>`;
        }).join("")}</tbody>
      </table>
    </div>
    <div class="dive-panel">
      <h3>Options flow <span class="hint-inline">input, not the verdict</span></h3>
      <p class="hint">${flow ? flow.reason : "No options factor."}</p>
      ${opts ? `<table class="drivers"><thead><tr><th>Side</th><th>Expiry</th><th>DTE</th><th>Strike</th><th>Mid</th><th>Note</th></tr></thead><tbody>${opts}</tbody></table>` : ""}
    </div>
    <div class="grid2 news dive-news">
      <div class="news-panel"><h3>Company headlines</h3><ul class="news-list">${news}</ul></div>
      <div class="news-panel"><h3>Geopolitics &amp; macro</h3><ul class="news-list">${geo}</ul></div>
    </div>
  `;
  $("dive").hidden = false;
  $("dive-empty").hidden = true;
  if (state.quoteTicker === (res.ticker || "") || $("live-quote").innerHTML) {
    const live = $("live-quote");
    const divePx = $("dive-live-px");
    if (live && divePx && !live.hidden) divePx.innerHTML = live.innerHTML;
  }
  showQuote(res.ticker || ticker);
  barChart($("vote-chart"), votes.map((v) => v.name), votes.map((v) => v.score));
  const candles = res.candles || [];
  if (candles.length) priceChart($("px-chart"), candles);
  if (res.equity && res.equity.length && $("eq-chart")) {
    equityCompareChart($("eq-chart"), res.equity, res.equity_bh || []);
  }
}

function scanBody(uni, cursor) {
  return JSON.stringify({
    universe: uni,
    deep: $("deep").checked,
    cursor: Number(cursor) || 0,
  });
}

function rememberScanCursor(uni, job) {
  const next = job && (job.next_cursor ?? job.progress);
  if (next == null) return;
  state.scanCursor[uni] = Number(next) || 0;
}

async function refreshUniverse() {
  const uni = $("universe").value;
  localStorage.setItem("desk_universe", uni);
  setBusy("scan-btn", true);
  state.scanCursor[uni] = 0;
  try {
    const job = await api("/board/refresh", {
      method: "POST",
      body: scanBody(uni, 0),
    });
    rememberScanCursor(uni, job);
    updateScanStatus(job);
    startPoll();
    await loadBoard({ quiet: true, fillEmpty: false });
    const ym = tapeYM();
    const extra = { universe: uni, year: ym.year, month: ym.month };
    if (state.view === "options") {
      await api("/options/refresh", { method: "POST", body: JSON.stringify(extra) });
      startOptionsPoll();
    }
    if (state.view === "volume") {
      await api("/volume/refresh", { method: "POST", body: JSON.stringify(extra) });
      startVolumePoll();
    }
  } catch (err) {
    if ($("scan-status")) {
      $("scan-status").textContent = "Scan started — loading names in batches…";
    }
    startPoll();
    await loadBoard({ quiet: true, fillEmpty: false }).catch(() => {});
  }
}

const OPT_COLS = [
  { key: "ticker", label: "Ticker" },
  { key: "action", label: "BUY / SELL" },
  { key: "sentiment", label: "Sentiment" },
  { key: "side", label: "Side" },
  { key: "trade_date", label: "Traded" },
  { key: "expiry", label: "Expiry" },
  { key: "dte", label: "DTE", num: true },
  { key: "strike", label: "Strike", num: true },
  { key: "volume", label: "Volume", num: true },
  { key: "mid", label: "Mid", num: true },
  { key: "premium", label: "Premium", num: true },
  { key: "oi", label: "OI", num: true },
];

function optRatingFilter() {
  const el = $("opt-rating");
  return el ? el.value : "ALL";
}

function optSearch() {
  const el = $("opt-q");
  return el ? el.value.trim().toUpperCase() : "";
}

function filteredOptRows() {
  const rating = optRatingFilter();
  const q = optSearch();
  return (state.optRows || []).filter((r) => {
    if (rating && rating !== "ALL" && r.rating !== rating) return false;
    if (q && !String(r.ticker || "").toUpperCase().includes(q)) return false;
    return true;
  });
}

function renderOptKpis() {
  const host = $("opt-kpis");
  if (!host) return;
  const names = new Set();
  const counts = { "HIGH BUY": 0, BUY: 0, HOLD: 0, SELL: 0 };
  for (const r of state.optRows || []) {
    const t = String(r.ticker || "").toUpperCase();
    if (t) names.add(t);
    const rating = r.rating || "HOLD";
    if (counts[rating] != null) counts[rating] += 1;
  }
  const items = [
    { lbl: "Names on tape", val: String(names.size) },
    { lbl: "Highly bullish", val: String(counts["HIGH BUY"]), cls: "pos", filter: "HIGH BUY" },
    { lbl: "Bullish", val: String(counts.BUY), cls: "pos", filter: "BUY" },
    { lbl: "Unknown / hold", val: String(counts.HOLD), cls: "mid", filter: "HOLD" },
    { lbl: "Bearish", val: String(counts.SELL), cls: "neg", filter: "SELL" },
  ];
  const active = optRatingFilter();
  host.innerHTML = items.map((it) => {
    const on = it.filter && it.filter === active ? " is-on" : "";
    const cls = `kpi ${it.cls || ""}${it.filter ? " kpi-click" : ""}${on}`;
    const attrs = it.filter
      ? ` data-filter="${esc(it.filter)}" role="button" tabindex="0" title="Show ${esc(SENTIMENT_LABEL[it.filter] || it.filter)} names"`
      : "";
    return `<div class="${cls}"${attrs}><div class="lbl">${it.lbl}</div><div class="val">${esc(it.val)}</div></div>`;
  }).join("");
  host.querySelectorAll("[data-filter]").forEach((el) => {
    const apply = () => {
      const sel = $("opt-rating");
      if (!sel) return;
      sel.value = sel.value === el.dataset.filter ? "ALL" : el.dataset.filter;
      paintOptions();
    };
    el.addEventListener("click", apply);
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        apply();
      }
    });
  });
}

function renderOptions(rows) {
  state.optRows = rows || [];
  paintOptions();
}

function paintOptions() {
  const el = $("options-list");
  if (!el) return;
  renderOptKpis();
  const all = state.optRows || [];
  const rows = filteredOptRows();
  const count = $("opt-count");
  const names = new Set(rows.map((r) => r.ticker).filter(Boolean));
  if (count) {
    count.innerHTML = all.length
      ? `<b>${rows.length}</b> contracts · <b>${names.size}</b> names`
      : "";
  }
  if (!all.length) {
    $("options-empty").hidden = false;
    el.innerHTML = "";
    return;
  }
  if (!rows.length) {
    $("options-empty").textContent = "No contracts match this sentiment filter.";
    $("options-empty").hidden = false;
    el.innerHTML = "";
    return;
  }
  $("options-empty").hidden = true;
  ensureSort(state.optSort, OPT_COLS, "premium");
  const sorted = sortByKey(rows, state.optSort.key, state.optSort.dir);
  el.innerHTML = `<div class="table-card"><div class="table-scroll"><table class="drivers">
    <thead><tr>${sortableHead(OPT_COLS, state.optSort)}</tr></thead>
    <tbody>${sorted.map((r) => {
      const action = r.action || "";
      const actionCls = action === "BUY" ? "flag-buy" : action === "SELL" ? "flag-sell" : "flag-unknown";
      const flowTitle = [
        action === "SELL" && r.side === "PUT" ? "SELL put is bullish (premium / cash-secured)" : "",
        action === "BUY" && r.side === "PUT" ? "BUY put is bearish (downside)" : "",
        action === "BUY" && r.side === "CALL" ? "BUY call is bullish" : "",
        action === "SELL" && r.side === "CALL" ? "SELL call is bearish / covered" : "",
        r.action_note || "",
        r.last && r.bid && r.ask ? `last ${r.last} · bid ${r.bid} · ask ${r.ask}` : "",
      ].filter(Boolean).join(" · ");
      return `<tr>
      <td><a class="name-card-link" href="${tickerUrl(r.ticker)}" data-open="${esc(r.ticker)}">${esc(r.ticker)}</a></td>
      <td class="${actionCls}" title="${esc(r.action_note || "Unknown")}">${esc(action || "—")}</td>
      <td class="${ratingClass(r.rating)}" title="${esc(flowTitle)}">${esc(r.sentiment || SENTIMENT_LABEL[r.rating] || "—")}</td>
      <td class="${r.side === "CALL" ? "flag-call" : "flag-put"}">${esc(r.side)}</td>
      <td class="trade-date">${esc(r.trade_date || "—")}</td>
      <td>${esc(r.expiry)}</td><td class="num">${r.dte}</td><td class="num">${fmt(r.strike)}</td>
      <td class="num">${Number(r.volume).toLocaleString()}</td><td class="num">${fmt(r.mid)}</td>
      <td class="money">${money(r.premium)}</td><td class="num">${Number(r.oi).toLocaleString()}</td>
    </tr>`;
    }).join("")}</tbody></table></div></div>`;
  bindSortHeaders(el, state.optSort, paintOptions);
  bindTickerLinks(el);
}

async function loadOptions({ autoRefresh = true } = {}) {
  const uni = $("universe").value;
  const t = activeTicker();
  const ym = tapeYM();
  $("options-empty").textContent = `Loading ${ym.label || ""} option tape (≥ $1M premium)…`;
  $("options-empty").hidden = false;
  setOptionsAsOf(null);
  setBusy("options-load", true);
  try {
    if (t) {
      rememberTicker(t);
      await loadOptionsTicker(t);
      return;
    }
    skeletonRows($("options-list"), 10);
    let data = await api("/options/flags?universe=" + encodeURIComponent(uni) + ymQuery());
    if (autoRefresh && !(data.rows || []).length && (!data.job || data.job.status !== "running")) {
      await api("/options/refresh", { method: "POST", body: JSON.stringify({ universe: uni, year: ym.year, month: ym.month }) });
      data = await api("/options/flags?universe=" + encodeURIComponent(uni) + ymQuery());
    }
    renderOptions(data.rows || []);
    setOptionsAsOf(data.as_of);
    $("options-status").textContent = `${ym.label} tape · ${data.n || (data.rows || []).length} contracts ≥ $1M`;
    $("options-empty").hidden = !!(data.rows || []).length;
    if (data.job && data.job.status === "running") startOptionsPoll();
  } catch (err) {
    $("options-list").innerHTML = "";
    $("options-empty").textContent = err.message;
    $("options-empty").hidden = false;
    toast(err.message, "err");
  } finally {
    setBusy("options-load", false);
  }
}

function startOptionsPoll() {
  if (state.optionsPoll) return;
  state.optionsPoll = setInterval(async () => {
    try {
      const data = await api("/options/flags?universe=" + encodeURIComponent($("universe").value));
      renderOptions(data.rows || []);
      const job = data.job || {};
      setOptionsAsOf(job.status === "running" ? null : data.as_of);
      $("options-status").textContent = job.status === "running"
        ? `Scanning options… ${job.universe || ""}`
        : `${data.n || 0} contracts ≥ $1M`;
      if (job.status !== "running") {
        clearInterval(state.optionsPoll);
        state.optionsPoll = null;
      }
    } catch { /* keep polling */ }
  }, 2500);
}

function renderVolumeSummary(summary) {
  const el = $("volume-summary");
  if (!el) return;
  if (!summary) {
    el.innerHTML = "";
    return;
  }
  const mtd = summary.mtd || {};
  el.innerHTML = `
    <div class="vol-summary">
      <div class="sub">${summary.ticker || ""} · ${summary.month_label || ""} · ${summary.interval || ""} bars ≥ ${money(summary.min_notional)}</div>
      <div class="stance ${summary.stance === "BULLISH" ? "flag-buy" : (summary.stance === "BEARISH" ? "flag-sell" : "HOLD")}">${summary.stance || ""} · ${summary.rating || ""}</div>
      <div class="why">${summary.why || ""}</div>
      <div class="kpis-mini">
        <div class="kpi"><div class="lbl">Buy prints</div><div class="val flag-buy">${money(summary.buy_notional)} · ${summary.n_buy || 0}</div></div>
        <div class="kpi"><div class="lbl">Sell prints</div><div class="val flag-sell">${money(summary.sell_notional)} · ${summary.n_sell || 0}</div></div>
        <div class="kpi"><div class="lbl">Net tape</div><div class="val">${money(summary.net_notional)}</div></div>
        <div class="kpi"><div class="lbl">Typical bar</div><div class="val">${money(summary.median_bar)}</div></div>
      </div>
    </div>`;
}

const VOL_PRINT_COLS = [
  { key: "time", label: "Time" },
  { key: "ticker", label: "Ticker" },
  { key: "side", label: "Side" },
  { key: "shares", label: "Shares", num: true },
  { key: "price", label: "Price", num: true },
  { key: "notional", label: "Notional", num: true },
  { key: "vs_median", label: "vs typ.", num: true },
  { key: "why", label: "Why" },
];

const VOL_NAME_COLS = [
  { key: "ticker", label: "Ticker" },
  { key: "month_label", label: "Month" },
  { key: "stance", label: "Stance" },
  { key: "buy_notional", label: "Buy $", num: true },
  { key: "sell_notional", label: "Sell $", num: true },
  { key: "net_notional", label: "Net", num: true },
  { key: "largest_print", label: "Largest", num: true },
  { key: "why", label: "Why" },
];

function renderVolume(data) {
  state.volPayload = Array.isArray(data) ? { rows: data } : (data || {});
  paintVolume();
}

function paintVolume() {
  const payload = state.volPayload || {};
  const rows = payload.rows || [];
  const summary = payload.summary || null;
  const el = $("volume-list");
  if (!el) return;
  renderVolumeSummary(summary && summary.ticker ? summary : null);
  if (!rows.length) {
    $("volume-empty").hidden = false;
    el.innerHTML = "";
    return;
  }
  $("volume-empty").hidden = true;
  const isPrint = rows.some((r) => r.side && r.notional != null);
  const cols = isPrint ? VOL_PRINT_COLS : VOL_NAME_COLS;
  ensureSort(state.volSort, cols, isPrint ? "notional" : "net_notional");
  const sorted = sortByKey(rows, state.volSort.key, state.volSort.dir);
  const body = isPrint
    ? sorted.map((r) => `<tr>
        <td>${esc(r.time || "")}</td>
        <td><a href="${tickerUrl(r.ticker)}" data-open="${esc(r.ticker)}">${esc(r.ticker)}</a></td>
        <td class="${r.side === "BUY" ? "flag-buy" : "flag-sell"}">${esc(r.side)}</td>
        <td class="num">${Number(r.shares || 0).toLocaleString()}</td>
        <td class="num">${fmt(r.price || r.close)}</td>
        <td class="money">${money(r.notional)}</td>
        <td class="num">${r.vs_median ? r.vs_median.toFixed(1) + "×" : esc(r.interval || "")}</td>
        <td>${esc(r.why || "")}</td>
      </tr>`).join("")
    : sorted.map((r) => `<tr>
        <td><a href="${tickerUrl(r.ticker)}" data-open="${esc(r.ticker)}">${esc(r.ticker)}</a></td>
        <td>${esc(r.month_label || r.month || "")}</td>
        <td class="${r.stance === "BULLISH" ? "flag-buy" : (r.stance === "BEARISH" ? "flag-sell" : "")}">${esc(r.stance || r.rating || "")}</td>
        <td class="money flag-buy">${money(r.buy_notional)}</td>
        <td class="money flag-sell">${money(r.sell_notional)}</td>
        <td class="money">${money(r.net_notional)}</td>
        <td class="money">${money(r.largest_print)}</td>
        <td>${esc(r.why || "")}</td>
      </tr>`).join("");
  el.innerHTML = `<div class="table-card"><div class="table-scroll"><table class="drivers">
    <thead><tr>${sortableHead(cols, state.volSort)}</tr></thead>
    <tbody>${body}</tbody></table></div></div>`;
  bindSortHeaders(el, state.volSort, paintVolume);
  bindTickerLinks(el);
}

async function loadVolume({ autoRefresh = true } = {}) {
  const uni = $("universe").value;
  const t = activeTicker();
  const ym = tapeYM();
  $("volume-empty").textContent = `Loading ${ym.label || ""} prints ≥ ${money(minPrint())}…`;
  $("volume-empty").hidden = false;
  if ($("volume-summary")) $("volume-summary").innerHTML = "";
  setBusy("volume-load", true);
  try {
    if (t) {
      rememberTicker(t);
      await loadVolumeTicker(t);
      return;
    }
    skeletonRows($("volume-list"), 10);
    let data = await api("/volume/large?universe=" + encodeURIComponent(uni) + ymQuery() + minPrintQuery());
    if (autoRefresh && !(data.rows || []).length && (!data.job || data.job.status !== "running")) {
      await api("/volume/refresh", { method: "POST", body: JSON.stringify({ universe: uni, year: ym.year, month: ym.month }) });
      data = await api("/volume/large?universe=" + encodeURIComponent(uni) + ymQuery() + minPrintQuery());
    }
    renderVolume(data);
    $("volume-status").textContent = `${ym.label} concentrated prints ≥ ${money(minPrint())} · ${data.n || (data.rows || []).length} names · ${data.as_of || "scanning"}`;
    $("volume-empty").hidden = !!(data.rows || []).length;
    if (data.job && data.job.status === "running") startVolumePoll();
  } catch (err) {
    $("volume-list").innerHTML = "";
    $("volume-empty").textContent = err.message;
    $("volume-empty").hidden = false;
    toast(err.message, "err");
  } finally {
    setBusy("volume-load", false);
  }
}

function startVolumePoll() {
  if (state.volumePoll) return;
  state.volumePoll = setInterval(async () => {
    try {
      const data = await api("/volume/large?universe=" + encodeURIComponent($("universe").value));
      renderVolume(data);
      const job = data.job || {};
      $("volume-status").textContent = job.status === "running"
        ? `Scanning volume… ${job.universe || ""}`
        : `${data.n || 0} large prints · ${data.as_of || ""}`;
      if (job.status !== "running") {
        clearInterval(state.volumePoll);
        state.volumePoll = null;
      }
    } catch { /* keep polling */ }
  }, 2000);
}

function stmtTable(title, stmt) {
  if (!stmt || !(stmt.rows || []).length) return `<p class="hint">${title}: no Yahoo statement.</p>`;
  const head = (stmt.columns || []).map((c, i) =>
    `<th class="${i === 0 ? "fin-selected-col" : ""}">${c}</th>`
  ).join("");
  const body = (stmt.rows || []).map((r) =>
    `<tr><td>${r.item}</td>${(r.values || []).map((v, i) =>
      `<td class="money${i === 0 ? " fin-selected-col" : ""}">${v == null ? "—" : money(v)}</td>`
    ).join("")}</tr>`
  ).join("");
  return `<h3>${title}</h3><div class="stmt-wrap"><table class="stmt"><thead><tr><th>Line</th>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function renderFinancials(data) {
  const v = data.verdict || {};
  const period = data.period_label || data.selected?.label || (data.freq === "annual" ? `FY ${data.year}` : `Q${data.quarter} ${data.year}`);
  const compare = data.freq === "quarterly"
    ? "Selected quarter in the first column; prior quarters to the right for QoQ context."
    : "Selected fiscal year in the first column; prior years to the right.";
  const note = data.note ? `<p class="fin-note">${esc(data.note)}</p>` : "";
  $("fin").innerHTML = `
    <div class="hero">
      <div class="sub">${esc(data.ticker)} · ${esc(data.name || "")} · <strong>${esc(period)}</strong> · ${esc(data.as_of || "")}</div>
      ${note}
      <p class="hint fin-period-hint">${compare}</p>
      <div id="fin-live-px" class="live-quote" style="margin:.45rem 0 .2rem;align-self:flex-start"></div>
      <h1 class="${ratingClass(v.rating)}">${v.stance || ""} · ${v.rating || ""}</h1>
      <div class="why">${v.thesis || ""}</div>
      ${(v.notes || []).length ? `<ul class="hint">${v.notes.map((n) => `<li>${n}</li>`).join("")}</ul>` : ""}
    </div>
    ${stmtTable("Income statement", data.income)}
    ${stmtTable("Cash flow", data.cashflow)}
    ${stmtTable("Balance sheet", data.balance)}
  `;
  $("fin").hidden = false;
  $("fin-empty").hidden = true;
}

async function loadFinancials(ticker) {
  const t = rememberTicker(ticker);
  if (!t) return;
  const periodTxt = finPeriodLabel();
  const cacheKey = lookupKey("fin", [t, finFreq(), finQuarter() || "", finYear() || ""]);
  const cached = readLookup(cacheKey, 12 * 60 * 60 * 1000);
  if (cached && cached.ticker) {
    renderFinancials(cached);
    $("resolve-hint").textContent = `${cached.ticker} · saved statements`;
  } else {
    $("fin").hidden = true;
    $("fin-empty").hidden = false;
    $("fin-empty").textContent = `Loading ${periodTxt} statements…`;
    $("resolve-hint").textContent = "Pulling income, cash flow, balance sheet…";
  }
  setBusy("analyze-btn", true);
  setBusy("fin-load", true);
  showQuote(t);
  try {
    const data = await api("/financials?ticker=" + encodeURIComponent(t) + finPeriodQuery());
    syncFinPeriodFromResponse(data);
    rememberTicker(data.ticker || t);
    writeLookup(cacheKey, data);
    $("resolve-hint").textContent = `${data.ticker} · ${data.name || ""}`;
    renderFinancials(data);
    const live = $("live-quote");
    const finPx = $("fin-live-px");
    if (live && finPx && !live.hidden) finPx.innerHTML = live.innerHTML;
    showQuote(data.ticker || t);
  } catch (err) {
    if (!(cached && cached.ticker)) {
      $("resolve-hint").innerHTML = `<span class="err">${esc(err.message)}</span>`;
      $("fin-empty").textContent = err.message;
      $("fin-empty").hidden = false;
      toast(err.message, "err");
    }
  } finally {
    setBusy("analyze-btn", false);
    setBusy("fin-load", false);
  }
}

async function loadOptionsTicker(ticker) {
  const t = rememberTicker(ticker);
  if (!t) return;
  const ym = tapeYM();
  const cacheKey = lookupKey("opt", [t, ym.year, ym.month]);
  const cached = readLookup(cacheKey, 15 * 60 * 1000);
  if (cached) {
    renderOptions(cached.rows || []);
    setOptionsAsOf(cached.as_of);
    $("options-empty").hidden = !!(cached.rows || []).length;
  } else {
    $("options-empty").textContent = `Scanning ${t} ${ym.label} tape for ≥ $1M premiums…`;
    $("options-empty").hidden = false;
    setOptionsAsOf(null);
    skeletonRows($("options-list"), 8);
  }
  showQuote(t);
  try {
    const data = await api("/options/flags?ticker=" + encodeURIComponent(t) + "&universe=" + encodeURIComponent($("universe").value) + ymQuery());
    writeLookup(cacheKey, data);
    renderOptions(data.rows || []);
    setOptionsAsOf(data.as_of);
    $("options-status").textContent = `${ym.label} · ${data.n || 0} contracts ≥ $1M on ${data.ticker}` + (data.note ? " · " + data.note : "");
    $("resolve-hint").textContent = data.ticker;
    showQuote(data.ticker || t);
    if (!(data.rows || []).length) {
      $("options-empty").textContent = `No ≥ $1M ${ym.label} expiries listed for ${data.ticker || t}. Yahoo only keeps the current month's chain after prior months expire.`;
      $("options-empty").hidden = false;
    }
  } catch (err) {
    if (!cached) {
      $("options-list").innerHTML = "";
      $("options-empty").textContent = err.message;
      $("options-empty").hidden = false;
      toast(err.message, "err");
    }
  }
}

async function loadVolumeTicker(ticker) {
  const t = rememberTicker(ticker);
  if (!t) return;
  const ym = tapeYM();
  const cacheKey = lookupKey("vol", [t, ym.year, ym.month, minPrint()]);
  const cached = readLookup(cacheKey, 15 * 60 * 1000);
  if (cached) {
    renderVolume(cached);
    $("volume-empty").hidden = !!(cached.rows || []).length;
  } else {
    $("volume-empty").textContent = `Scanning ${t} ${ym.label} for prints ≥ ${money(minPrint())}…`;
    $("volume-empty").hidden = false;
    skeletonRows($("volume-list"), 8);
  }
  showQuote(t);
  try {
    const data = await api("/volume/large?ticker=" + encodeURIComponent(t) + ymQuery() + minPrintQuery());
    writeLookup(cacheKey, data);
    renderVolume(data);
    $("volume-status").textContent = `${ym.label} · ${data.n || 0} prints ≥ ${money(minPrint())} on ${data.ticker} (${data.interval || ""} bars)` + (data.note ? " · " + data.note : "");
    $("resolve-hint").textContent = data.ticker;
    showQuote(data.ticker || t);
    if (!(data.rows || []).length) {
      $("volume-empty").textContent = `No ${ym.label} bar of at least ${money(minPrint())} for ${t}. Try a smaller min print size.`;
      $("volume-empty").hidden = false;
    }
  } catch (err) {
    if (!cached) {
      $("volume-list").innerHTML = "";
      $("volume-empty").textContent = err.message;
      $("volume-empty").hidden = false;
      toast(err.message, "err");
    }
  }
}

function portYears() {
  const saved = Number(localStorage.getItem("desk_port_years") || 1);
  return [1, 3, 5, 10].includes(saved) ? saved : 1;
}

function setPortYears(y) {
  localStorage.setItem("desk_port_years", String(y));
  document.querySelectorAll("#port-years button").forEach((b) => {
    b.classList.toggle("active", Number(b.dataset.years) === Number(y));
  });
}

function holdingsAllocation(s) {
  const rows = s.holdings || [];
  if (s.allocation) return s.allocation;
  const parts = allocationPercents(rows.map((h) => Number(h.weight) || 0));
  return rows.map((h, i) => `${h.ticker} ${parts[i]}%`).join(", ");
}

function allocationPercents(weights) {
  const raw = weights.map((w) => Math.max(0, Number(w) || 0) * 100);
  const tot = raw.reduce((a, b) => a + b, 0) || 1;
  const scaled = raw.map((x) => x * 100 / tot);
  const floors = scaled.map((x) => Math.floor(x));
  let left = 100 - floors.reduce((a, b) => a + b, 0);
  const order = scaled.map((x, i) => i).sort((a, b) => (scaled[b] - floors[b]) - (scaled[a] - floors[a]));
  const out = floors.slice();
  for (const i of order) {
    if (left <= 0) break;
    out[i] += 1;
    left -= 1;
  }
  return out;
}

function spyAnn(data) {
  const v = Number(data && data.spy_ann);
  return Number.isFinite(v) && v > 0 ? v : 0.115;
}

function sleeveBeatsSpy(s, hurdle) {
  if (typeof s.beats_spy === "boolean") return s.beats_spy;
  return Number(s.exp_ann || 0) > hurdle;
}

function sleeveEdge(s, hurdle) {
  if (s.edge_vs_spy != null && Number.isFinite(Number(s.edge_vs_spy))) return Number(s.edge_vs_spy);
  return Number(s.exp_ann || 0) - hurdle;
}

function holdingWeightLabel(h, pctInt) {
  if (h.weight_pct != null && Number.isFinite(Number(h.weight_pct))) return `${Number(h.weight_pct)}%`;
  if (pctInt != null) return `${pctInt}%`;
  return pct(h.weight);
}

function signedPct(n, d = 0) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const v = Number(n) * 100;
  return `${v > 0 ? "+" : ""}${v.toFixed(d)}%`;
}

function bandRail(s) {
  const b = s.band || {};
  if (b.lo_total == null || b.hi_total == null) return "";
  const lo = Number(b.lo_total), hi = Number(b.hi_total), mid = Number(s.exp_total || 0);
  const min = Math.min(lo, 0, mid), max = Math.max(hi, 0, mid);
  const span = (max - min) || 1;
  const x = (v) => ((v - min) / span * 100).toFixed(1);
  return `<div class="band">
    <span class="${lo < 0 ? "neg" : "pos"}">${signedPct(lo)}</span>
    <span class="rail"><i style="left:${x(lo)}%;width:${(Number(x(hi)) - Number(x(lo))).toFixed(1)}%"></i><b style="left:${x(mid)}%"></b></span>
    <span class="pos">${signedPct(hi)}</span>
    <span>1σ range</span>
  </div>`;
}

function portfolioChart(canvas, sleeves, years, hurdle) {
  const labels = Array.from({ length: years + 1 }, (_, i) => (i === 0 ? "Now" : `Y${i}`));
  const spy = Number(hurdle) || 0.115;
  const datasets = sleeves.filter((s) => (s.path || []).length).map((s) => ({
    label: s.label,
    data: s.path.map((p) => p.expected),
    borderColor: s.color,
    backgroundColor: s.color,
    tension: 0.25,
    pointRadius: years <= 3 ? 3 : 0,
    borderWidth: 2,
    fill: false,
  }));
  datasets.push({
    label: "S&P 500 11.5%/yr",
    data: labels.map((_, i) => Math.round(10000 * (1 + spy) ** i)),
    borderColor: "#8b97a8",
    backgroundColor: "#8b97a8",
    tension: 0.15,
    pointRadius: 0,
    borderWidth: 1.5,
    borderDash: [5, 4],
    fill: false,
  });
  const chart = new Chart(canvas, {
    type: "line",
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { color: "#c9d1d9", usePointStyle: true, boxWidth: 8 } },
        tooltip: { callbacks: { label: (c) => ` ${c.dataset.label}: ${money(c.parsed.y)}` } },
      },
      scales: {
        x: { ticks: { color: "#8b949e" }, grid: { color: "#21262d" } },
        y: { ticks: { color: "#8b949e", callback: (v) => money(v) }, grid: { color: "#30363d" } },
      },
    },
  });
  state.charts.push(chart);
}

function renderSwitches(s, years) {
  const swaps = s.switches || [];
  if (!swaps.length) {
    return `<div class="swap-box empty"><span class="hint">No rotations vs the shorter horizon — names already rank highest at ${years}y.</span></div>`;
  }
  const chips = swaps.slice(0, 6).map((w) => `
    <span class="swap-chip" title="${w.reason || ""}">
      <span class="in">${w.in}</span><span class="arr">→</span><span class="out">${w.out}</span>
    </span>`).join("");
  const more = swaps.length > 6 ? `<span class="hint swap-more">+${swaps.length - 6} more</span>` : "";
  return `<details class="swap-box"${swaps.length <= 4 ? " open" : ""}>
    <summary>${swaps.length} swap${swaps.length === 1 ? "" : "s"} for higher ${years}y return${s.vs_years ? ` · vs ${s.vs_years}y` : ""}</summary>
    <div class="swap-chips">${chips}${more}</div>
    <ul class="swap-list">${swaps.map((w) => `<li><span class="in">${w.in}</span> replaces <span class="out">${w.out}</span> — ${w.reason || ""}</li>`).join("")}</ul>
  </details>`;
}

function renderHoldings(s, years) {
  const rows = s.holdings || [];
  const parts = allocationPercents(rows.map((h) => Number(h.weight) || 0));
  return rows.map((h, i) => `
    <a class="hold-tile" href="${tickerUrl(h.ticker)}" data-open="${h.ticker}" style="--tier:${s.color}">
      <div class="hold-top">
        <span class="sym">${h.ticker}</span>
        <span class="hold-wt">${holdingWeightLabel(h, parts[i])}</span>
      </div>
      <div class="hold-name hint">${(h.name || "").slice(0, 28)}</div>
      <div class="hold-metrics">
        <span class="${Number(h.exp_ann) >= 0 ? "pos" : "neg"}">${signedPct(h.exp_ann, 1)}/yr</span>
        <span>${signedPct(h.exp_total)} ${years}y</span>
      </div>
      <div class="hold-foot">
        <span class="${ratingClass(h.rating)}">${h.rating || "—"}</span>
        <span class="hint">conf ${pct(h.conf)}</span>
        <span class="hint">vol ${pct(h.vol)}</span>
      </div>
      <div class="hold-bar"><i style="width:${Math.min(100, Number(h.weight || 0) * 100 / 0.2).toFixed(0)}%"></i></div>
    </a>`).join("");
}

function renderPortfolios(data) {
  killCharts();
  const el = $("port-list");
  const years = Number(data.years || 1);
  const hurdle = spyAnn(data);
  const sleeves = (data.sleeves || []).filter((s) => sleeveBeatsSpy(s, hurdle) && Number(s.n || (s.holdings || []).length) > 0);
  const vs = $("port-vs-spy");
  if (!sleeves.length) {
    $("port-empty").hidden = false;
    $("port-empty").textContent = `No ${years}-year sleeve in this universe is expected to outrun the S&P 500’s 11.5%/yr, so none are listed.`;
    $("port-summary").innerHTML = "";
    $("port-chart-wrap").hidden = true;
    if (vs) {
      vs.hidden = false;
      vs.classList.add("lags");
      vs.innerHTML = `<span class="port-vs-kicker lags">None modeled above the S&amp;P 500</span>
        <p>Every sleeve’s expected annualized return is at or below the S&amp;P’s <b>11.5%/yr</b> long-run hurdle on this universe and horizon.</p>`;
    }
    el.innerHTML = "";
    return;
  }
  $("port-empty").hidden = true;
  const beaters = sleeves.filter((s) => sleeveBeatsSpy(s, hurdle));
  if (vs) {
    vs.hidden = false;
    if (beaters.length) {
      const bits = beaters.map((s) => {
        const edge = sleeveEdge(s, hurdle);
        return `<b>${s.label}</b> (${signedPct(s.exp_ann, 1)}/yr, ${signedPct(edge, 1)} vs S&amp;P)`;
      }).join(", ");
      vs.innerHTML = `<span class="port-vs-kicker">Expected to outrun the S&amp;P 500</span>
        <p>S&amp;P 500 hurdle is <b>11.5%/yr</b> annualized. Model sleeves above that: ${bits}.</p>`;
      vs.classList.remove("lags");
    } else {
      vs.innerHTML = `<span class="port-vs-kicker lags">None modeled above the S&amp;P 500</span>
        <p>Every sleeve’s expected annualized return is at or below the S&amp;P’s <b>11.5%/yr</b> long-run hurdle on this universe and horizon.</p>`;
      vs.classList.add("lags");
    }
  }
  $("port-summary").innerHTML = sleeves.map((s) => {
    const edge = sleeveEdge(s, hurdle);
    const beats = sleeveBeatsSpy(s, hurdle);
    return `
    <div class="tier-pill${beats ? " beats-spy" : ""}" style="--tier:${s.color}" data-jump="port-${s.id}">
      <div class="lbl">${s.label}${beats ? " · vs S&P" : ""}</div>
      <div class="val">${signedPct(s.exp_total)}</div>
      <div class="sub2">${signedPct(s.exp_ann, 1)}/yr · ${beats ? signedPct(edge, 1) + " vs S&P" : "trails S&P"} · ${s.n} names</div>
    </div>`;
  }).join("");
  $("port-summary").querySelectorAll("[data-jump]").forEach((p) => {
    p.addEventListener("click", () => {
      const target = document.getElementById(p.dataset.jump);
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
  $("port-chart-wrap").hidden = false;
  $("port-chart-sub").textContent = `${years}-year horizon vs S&P 500 11.5%/yr · ${data.universe || ""} · ${data.as_of || ""}`;
  const canvas = $("port-chart");
  const wrap = canvas.parentElement;
  const fresh = document.createElement("canvas");
  fresh.id = "port-chart";
  wrap.replaceChild(fresh, canvas);
  portfolioChart(fresh, sleeves, years, hurdle);

  el.innerHTML = sleeves.map((s) => {
    const chips = (s.sectors || []).slice(0, 8).map((x) => `<span class="chip">${x.sector} <b>${pct(x.weight)}</b></span>`).join("");
    const dd = s.est_drawdown != null ? signedPct(s.est_drawdown) : "—";
    const end10k = money(((s.path || []).slice(-1)[0] || {}).expected);
    const beats = sleeveBeatsSpy(s, hurdle);
    const edge = sleeveEdge(s, hurdle);
    const alloc = holdingsAllocation(s);
    return `<article class="port-card" id="port-${s.id}" style="--tier:${s.color}">
      <div class="port-card-top">
        <div class="port-card-head">
          <span class="tier-badge">${s.label}</span>
          ${beats ? `<span class="spy-badge pos">Outruns S&amp;P 500</span>` : `<span class="spy-badge muted">Trails S&amp;P 500</span>`}
          <h2>${signedPct(s.exp_total)} <small>over ${years} ${years === 1 ? "year" : "years"}</small></h2>
          <div class="tagline">${s.tagline || ""}</div>
        </div>
        <div class="port-hero-stat">
          <div class="growth">${signedPct(s.exp_ann, 1)}<small>/yr</small></div>
          <div class="hint">${signedPct(edge, 1)} vs 11.5% S&amp;P · ${s.n} names · $10k → ${end10k}</div>
        </div>
      </div>
      ${bandRail(s)}
      <div class="port-stats">
        <div class="kpi"><div class="lbl">Vs S&amp;P 500</div><div class="val ${beats ? "pos" : "neg"}">${signedPct(edge, 1)}</div><div class="kpi-sub">${beats ? "above 11.5%/yr" : "below 11.5%/yr"}</div></div>
        <div class="kpi"><div class="lbl">Confidence</div><div class="val">${pct(s.confidence)}</div><div class="conf-meter"><div style="width:${Math.round(Number(s.confidence || 0) * 100)}%"></div></div></div>
        <div class="kpi"><div class="lbl">Blended vol</div><div class="val">${pct(s.risk_vol)}</div></div>
        <div class="kpi"><div class="lbl">Est. drawdown</div><div class="val ${s.est_drawdown != null && s.est_drawdown < 0 ? "neg" : ""}">${dd}</div></div>
      </div>
      <div class="sector-chips">${chips}</div>
      ${renderSwitches(s, years)}
      <div class="alloc-block">
        <div class="hold-head"><span>Total allocation</span><span class="hint">sums to 100%</span></div>
        <p class="alloc-line">${alloc || "—"}</p>
      </div>
      <div class="hold-section">
        <div class="hold-head"><span>Holdings</span><span class="hint">${s.n} positions</span></div>
        <div class="hold-grid">${renderHoldings(s, years)}</div>
      </div>
    </article>`;
  }).join("");
  el.insertAdjacentHTML("beforeend", `<p class="port-footnote hint">${data.note || ""}</p>`);
  el.querySelectorAll("[data-open]").forEach((a) => {
    a.addEventListener("click", (e) => { e.preventDefault(); openTicker(a.dataset.open); });
  });
}

function portCacheKey(uni, years) {
  return String(uni || "global").toLowerCase() + "|" + Number(years || 1);
}

function readPortCache(uni, years) {
  const key = portCacheKey(uni, years);
  if (state.portCache[key]) return state.portCache[key];
  try {
    const raw = sessionStorage.getItem("desk_port_" + key);
    if (!raw) return null;
    const data = JSON.parse(raw);
    if (data && Array.isArray(data.sleeves)) {
      state.portCache[key] = data;
      return data;
    }
  } catch { /* quota / parse */ }
  return null;
}

function writePortCache(uni, years, data) {
  const key = portCacheKey(uni, years);
  state.portCache[key] = data;
  try {
    sessionStorage.setItem("desk_port_" + key, JSON.stringify(data));
  } catch { /* quota */ }
}

async function loadPortfolios(refresh = false) {
  const uni = $("universe").value;
  const years = portYears();
  setPortYears(years);
  const cached = refresh ? null : readPortCache(uni, years);
  if (cached) {
    renderPortfolios(cached);
  } else {
    $("port-empty").hidden = false;
    $("port-empty").textContent = refresh
      ? `Rebuilding ${years}-year sleeves that outrun the S&P 500…`
      : `Loading ${years}-year portfolios that outrun the S&P 500…`;
    $("port-list").innerHTML = "";
    $("port-summary").innerHTML = "";
    if ($("port-vs-spy")) { $("port-vs-spy").hidden = true; $("port-vs-spy").innerHTML = ""; }
    $("port-chart-wrap").hidden = true;
    skeletonCards($("port-list"), 2);
  }
  $("resolve-hint").textContent = `Scoring ${years}-year expected return versus the S&P 500…`;
  setBusy("port-load", true);
  try {
    const q = "/portfolios?universe=" + encodeURIComponent(uni) + "&years=" + years + (refresh ? "&refresh=true" : "");
    const data = await api(q);
    if ($("universe").value !== uni || portYears() !== years) return;
    writePortCache(uni, years, data);
    renderPortfolios(data);
    const listed = (data.beats_spy || []).length || (data.sleeves || []).filter((s) => sleeveBeatsSpy(s, spyAnn(data))).length;
    $("port-status").textContent = `${years}-year horizon · ${listed} sleeve${listed === 1 ? "" : "s"} above the S&P · ${data.n_candidates || 0} names scored from ${UNIVERSE_LABEL[data.universe] || data.universe || uni} · ${data.as_of || ""}`;
    $("resolve-hint").textContent = data.as_of || "";
  } catch (err) {
    $("port-list").innerHTML = "";
    $("port-empty").textContent = err.message;
    $("port-empty").hidden = false;
    $("resolve-hint").innerHTML = `<span class="err">${esc(err.message)}</span>`;
    toast(err.message, "err");
  } finally {
    setBusy("port-load", false);
  }
}

function moneyCompact(n) {
  if (n === null || n === undefined || !Number.isFinite(Number(n))) return "—";
  const v = Number(n);
  if (v >= 1e12) return "$" + (v / 1e12).toFixed(2) + "T";
  if (v >= 1e9) return "$" + (v / 1e9).toFixed(1) + "B";
  if (v >= 1e6) return "$" + (v / 1e6).toFixed(0) + "M";
  return "$" + Math.round(v).toLocaleString();
}

function renderEtfs(data) {
  const el = $("etf-list");
  const empty = $("etf-empty");
  if (!el || !empty) return;
  const hurdle = Number(data.spy_ann || 0.115);
  const funds = (data.funds || []).filter((f) => Number(f.exp_ann) > hurdle && (f.holdings || []).length);
  const status = $("etf-status");
  if (status) {
    status.textContent = funds.length
      ? `${funds.length} fund${funds.length === 1 ? "" : "s"} above the S&P 500's ${(hurdle * 100).toFixed(1)}%/yr · holdings as reported${data.as_of ? " · " + data.as_of : ""}`
      : "Only funds expected to outrun the S&P 500’s 11.5%/yr are listed.";
  }
  if (!funds.length) {
    empty.hidden = false;
    empty.textContent = "No ETF in this screen clears the S&P 500's 11.5%/yr hurdle.";
    el.innerHTML = "";
    return;
  }
  empty.hidden = true;
  el.innerHTML = funds.map((f) => {
    const sectors = (f.sectors || []).filter((s) => Number(s.weight) >= 0.005);
    const sectorHtml = sectors.map((s) => {
      const w = Math.max(0, Math.min(100, Number(s.weight) * 100));
      return `<div class="sector-row">
        <span>${esc(s.sector)}</span>
        <span class="sector-track"><span style="width:${w.toFixed(1)}%"></span></span>
        <span class="num">${w.toFixed(1)}%</span>
      </div>`;
    }).join("");
    const holds = f.holdings || [];
    const weightSum = holds.reduce((sum, h) => sum + Number(h.weight || 0), 0);
    const holdLabel = weightSum >= 0.9 ? "Current holdings" : `Largest positions · ${holds.length} names`;
    const holdRows = holds.map((h) => `<tr>
      <td><a href="${tickerUrl(h.ticker)}" data-open="${esc(h.ticker)}">${esc(h.ticker)}</a></td>
      <td class="etf-hold-name">${esc(h.name || "")}</td>
      <td class="num">${(Number(h.weight || 0) * 100).toFixed(1)}%</td>
    </tr>`).join("");
    const exp = f.expense == null ? "—" : (Number(f.expense) * 100).toFixed(2) + "%";
    return `<article class="etf-card">
      <div class="port-card-top">
        <div class="port-card-head">
          <span class="spy-badge pos">Outruns S&amp;P 500</span>
          <h2><a href="${tickerUrl(f.ticker)}" data-open="${esc(f.ticker)}">${esc(f.ticker)}</a> <small>${esc(f.name || "")}</small></h2>
          <div class="tagline">${esc(f.why || "")}</div>
        </div>
        <div class="port-hero-stat">
          <div class="growth">${signedPct(f.exp_ann, 1)}<small>/yr</small></div>
          <div class="hint">${signedPct(f.edge_vs_spy, 1)} vs 11.5% S&amp;P · 10y ${f.cagr_10y == null ? "—" : signedPct(f.cagr_10y, 1)}</div>
        </div>
      </div>
      <div class="etf-meta">
        <span>AUM ${moneyCompact(f.aum)}</span>
        <span>Expense ${exp}</span>
        <span>${f.as_of ? "Holdings " + esc(f.as_of) : ""}</span>
      </div>
      <div class="etf-split">
        <div>
          <div class="chart-title"><span>Sectors</span></div>
          <div class="sector-bars">${sectorHtml || `<div class="hint">Sector mix not reported.</div>`}</div>
        </div>
        <div>
          <div class="chart-title"><span>${holdLabel}</span></div>
          <div class="table-scroll">
            <table class="etf-holds">
              <thead><tr><th>Ticker</th><th>Name</th><th class="num">Weight</th></tr></thead>
              <tbody>${holdRows}</tbody>
            </table>
          </div>
        </div>
      </div>
    </article>`;
  }).join("");
  el.querySelectorAll("[data-open]").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      openTicker(a.dataset.open);
    });
  });
}

async function loadEtfs(refresh = false) {
  const empty = $("etf-empty");
  if (!refresh) {
    try {
      const res = await fetch("/etfs.json");
      if (res.ok) renderEtfs(await res.json());
    } catch { /* the API response still paints the tab */ }
  } else {
    const status = $("etf-status");
    if (status) status.textContent = "Refreshing funds against the S&P 500 hurdle…";
  }
  setBusy("etf-load", true);
  try {
    const data = await api("/etfs" + (refresh ? "?refresh=true" : ""));
    renderEtfs(data);
  } catch (err) {
    const blank = !$("etf-list").children.length;
    if (blank && empty) {
      empty.hidden = false;
      empty.textContent = err.message;
    }
    if (refresh || blank) toast(err.message, "err");
  } finally {
    setBusy("etf-load", false);
  }
}

async function loadDive(ticker) {
  const t = cleanTicker(ticker) || ticker;
  const period = $("period").value;
  const horizon = Number($("horizon").value);
  const cacheKey = lookupKey("an", [cleanTicker(t) || t, period, horizon]);
  const cached = readLookup(cacheKey, 6 * 60 * 60 * 1000);
  if (cached && cached.ticker) {
    renderDive(cached);
    $("dive").hidden = false;
    $("dive-empty").hidden = true;
    $("resolve-hint").textContent = `${cached.ticker} · saved analysis, checking for a newer one…`;
  } else {
    $("dive").hidden = true;
    $("dive-empty").hidden = false;
    $("dive-empty").textContent = `Running analysis on ${t}…`;
    $("resolve-hint").textContent = "Resolving symbol and running analysis…";
  }
  setBusy("analyze-btn", true);
  try {
    const res = await api(
      "/analyze?ticker=" + encodeURIComponent(t) +
      "&period=" + encodeURIComponent(period) +
      "&horizon=" + encodeURIComponent(horizon) +
      "&deep=true"
    );
    writeLookup(cacheKey, res);
    const extra = res.queried && res.ticker && res.queried.toUpperCase() !== res.ticker
      ? `Resolved ${res.queried} → ${res.ticker} (${res.name || ""}).`
      : `${res.ticker} · ${res.name || ""}`;
    $("resolve-hint").textContent = extra;
    if (res.ticker) rememberTicker(res.ticker);
    setView("dive", { ticker: res.ticker || t });
    renderDive(res);
  } catch (err) {
    if (!(cached && cached.ticker)) {
      $("resolve-hint").innerHTML = `<span class="err">${esc(err.message)}</span>`;
      $("dive-empty").textContent = err.message;
      $("dive-empty").hidden = false;
      toast(err.message, "err");
    }
  } finally {
    setBusy("analyze-btn", false);
  }
}

function bindUi() {
  $("home-btn").addEventListener("click", (e) => {
    e.preventDefault();
    showHome(true);
  });
  document.querySelectorAll("#main-nav a, #mobile-nav a").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      if (a.dataset.view === "home") showHome(true);
      else gotoView(a.dataset.view);
    });
  });
  $("analyze-form").addEventListener("submit", (e) => {
    e.preventDefault();
    acClose();
    if (state.view === "portfolios") {
      loadPortfolios(true);
      return;
    }
    if (state.view === "etfs") {
      loadEtfs(true);
      return;
    }
    runForCurrentView();
  });
  const tickerInput = $("ticker");
  tickerInput.addEventListener("input", () => acSearch(tickerInput.value));
  tickerInput.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      acMove(1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      acMove(-1);
    } else if (e.key === "Enter" && ac.open && ac.idx >= 0) {
      e.preventDefault();
      acPick(ac.items[ac.idx].symbol);
    }
  });
  tickerInput.addEventListener("blur", () => setTimeout(acClose, 120));
  document.addEventListener("click", (e) => {
    if (e.target instanceof Element && !e.target.closest(".field-ac")) acClose();
  });
  if ($("shortcut-btn")) $("shortcut-btn").addEventListener("click", () => toggleShortcuts());
  if ($("shortcut-close")) $("shortcut-close").addEventListener("click", () => toggleShortcuts(false));
  if ($("shortcut-sheet")) {
    $("shortcut-sheet").addEventListener("click", (e) => {
      if (e.target === $("shortcut-sheet")) toggleShortcuts(false);
    });
  }
  if ($("f-sort")) {
    const savedSort = localStorage.getItem("desk_board_sort");
    if (savedSort && $("f-sort").querySelector(`option[value="${savedSort}"]`)) $("f-sort").value = savedSort;
    $("f-sort").addEventListener("change", () => {
      localStorage.setItem("desk_board_sort", $("f-sort").value);
      renderBoard();
    });
  }
  if ($("fin-load")) {
    $("fin-load").addEventListener("click", () => {
      const t = activeTicker();
      if (!t) {
        $("fin-empty").textContent = "Type a ticker in the box above, then click Load statements.";
        $("fin-empty").hidden = false;
        return;
      }
      loadFinancials(t);
    });
  }
  document.querySelectorAll("#fin-freq button").forEach((b) => {
    b.addEventListener("click", () => {
      setFinFreq(b.dataset.freq);
      const t = activeTicker();
      if (state.view === "financials" && t) loadFinancials(t);
    });
  });
  document.querySelectorAll("#fin-quarter button").forEach((b) => {
    b.addEventListener("click", () => {
      setFinQuarter(b.dataset.quarter);
      const t = activeTicker();
      if (state.view === "financials" && t) loadFinancials(t);
    });
  });
  if ($("fin-year")) {
    $("fin-year").addEventListener("change", () => {
      setFinYear($("fin-year").value);
      const t = activeTicker();
      if (state.view === "financials" && t) loadFinancials(t);
    });
  }
  setFinFreq(finFreq());
  markFinQuarter(finQuarter() || Math.ceil((new Date().getMonth() + 1) / 3));
  fillFinYears([], finFreq(), { persist: false });
  if ($("options-load")) $("options-load").addEventListener("click", () => loadOptions());
  if ($("opt-rating")) $("opt-rating").addEventListener("change", paintOptions);
  if ($("opt-q")) $("opt-q").addEventListener("input", paintOptions);
  if ($("opt-all")) {
    $("opt-all").addEventListener("click", () => {
      if ($("opt-rating")) $("opt-rating").value = "ALL";
      if ($("opt-q")) $("opt-q").value = "";
      paintOptions();
    });
  }
  if ($("volume-load")) $("volume-load").addEventListener("click", () => loadVolume());
  if ($("port-load")) $("port-load").addEventListener("click", () => loadPortfolios(true));
  if ($("etf-load")) $("etf-load").addEventListener("click", () => loadEtfs(true));
  document.querySelectorAll("#port-years button").forEach((b) => {
    b.addEventListener("click", () => {
      setPortYears(Number(b.dataset.years));
      loadPortfolios(false);
    });
  });
  setPortYears(portYears());
  if ($("min-print")) $("min-print").addEventListener("change", () => {
    if (state.view === "volume") loadVolume();
  });
  if ($("port-universe")) {
    $("port-universe").addEventListener("change", () => {
      setPortUniverse($("port-universe").value);
      if (state.view === "portfolios") loadPortfolios(false);
    });
  }
  $("universe").addEventListener("change", () => {
    syncPortUniverse();
    if (state.view === "home") {
      loadBoard({ fillEmpty: true }).catch((err) => {
        $("board-empty").textContent = err.message;
        $("board-empty").hidden = false;
        toast(err.message, "err");
      });
    }
    if (state.view === "options") loadOptions({ autoRefresh: false });
    if (state.view === "volume") loadVolume({ autoRefresh: false });
    if (state.view === "portfolios") loadPortfolios(false);
  });
  $("scan-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await refreshUniverse();
  });
  $("f-all").addEventListener("click", resetFilters);
  $("ticker").addEventListener("change", () => {
    const t = rememberTicker($("ticker").value);
    if (!t) {
      document.body.dataset.ticker = "";
      try { sessionStorage.removeItem("desk_ticker"); } catch { /* private mode */ }
      return;
    }
    showQuote(t);
  });
  ["f-rating", "f-sector", "f-conf", "f-q"].forEach((id) => {
    $(id).addEventListener("input", () => {
      if (id === "f-conf") $("f-conf-lbl").textContent = $("f-conf").value + "%";
      renderBoard();
    });
    $(id).addEventListener("change", renderBoard);
  });
  window.addEventListener("popstate", (e) => {
    const st = e.state || {};
    if (st.view === "dive") openTicker(st.ticker, { push: false });
    else if (st.view && st.view !== "home") {
      if (st.ticker) rememberTicker(st.ticker);
      showPage(st.view, false);
    } else showHome(false);
  });
}

async function boot() {
  setupChartDefaults();
  bindUi();
  bindShortcuts();
  renderRecents();
  $("board-empty").hidden = true;
  const saved = localStorage.getItem("desk_universe");
  if (saved && $("universe").querySelector(`option[value="${saved}"]`)) {
    $("universe").value = saved;
  }
  syncPortUniverse();
  const period = document.body.dataset.period;
  const horizon = document.body.dataset.horizon;
  if (period && [...$("period").options].some((o) => o.value === period)) $("period").value = period;
  if (horizon && [...$("horizon").options].some((o) => o.value === horizon)) $("horizon").value = horizon;
  const fromUrl = cleanTicker(document.body.dataset.ticker);
  let fromSession = "";
  try { fromSession = cleanTicker(sessionStorage.getItem("desk_ticker")); } catch { /* private mode */ }
  const ticker = fromUrl || fromSession;
  if ($("ticker") && /[{}]/.test($("ticker").value)) $("ticker").value = "";
  if (ticker) rememberTicker(ticker);
  const uni = $("universe").value;
  if (!readBoardCache(uni)) skeletonCards($("board-list"), 8);
  paintUniverseShell(uni);
  loadBoard().catch((err) => {
    if (state.shellUni === uni && (state.rows || []).length) return;
    $("board-list").innerHTML = "";
    $("board-empty").textContent = "Could not reach FastAPI at " + API + " — " + err.message;
    $("board-empty").hidden = false;
    toast("Backend unreachable — " + err.message, "err", 9000);
  });
  const initial = activeTicker();
  const page = document.body.dataset.page || "home";
  if (initial) showQuote(initial);
  if (page === "ticker" && initial) openTicker(initial, { push: false });
  else if (page === "financials") {
    setView("financials");
    setFinFreq(finFreq());
    if (initial) loadFinancials(initial);
  } else if (page === "options" || page === "volume" || page === "portfolios" || page === "etfs") showPage(page, false);
  else setView("home");
  updatePeriodTags();
  applyViewMode(state.view);
}

boot();
