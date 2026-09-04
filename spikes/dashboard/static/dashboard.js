// Dashboard page: data fetching, state, rendering and interactions.
import {
  barChart,
  clamp,
  DAY_MS,
  el,
  fill,
  fmtCompact,
  fmtDateLong,
  fmtInt,
  fmtRatio,
  fmtSigned,
  fmtZ,
  lineChart,
  miniFactors,
  parseDay,
  sparkline,
  WDAYS,
  zoneLabel,
} from "./charts.js";
import { iconSvg } from "./icons.js";

const API = new URL("../api/", import.meta.url);
const REFRESH_MS = 5 * 60 * 1000;
const FOCUS_REFRESH_MS = 60 * 1000;
const ALERT_SEVERITIES = ["major", "spike", "watch", "drop"];
const COUNT_SEVERITIES = [...ALERT_SEVERITIES, "new"];
const COUNT_KINDS = [...COUNT_SEVERITIES, "storm"];
const SEV_RANK = { major: 0, spike: 1, watch: 2, new: 3, drop: 4, ok: 5 };
const CHART_HEIGHT = 280;
const ALL = { all: true }; // the cross-channel view
const ALL_KEY = "all";
// the version scopes with a hash prefix (`#current/...`); `all` has none
const VERSIONED_SCOPES = new Set(["current", "strict"]);
const SCOPE_TAG = { current: "current versions", strict: "strict versions" };
const SCOPE_NOTE = {
  current:
    "Only the version current on each day (the cycle restarts at every release)",
  strict:
    "Only the exact version current on each day (the cycle restarts at every beta or dot release; on nightly, the day's builds only)",
};

const app = {
  summary: null,
  channel: null, // payload of the selected channel
  selected: null, // ALL or { product, channel }
  scope: "current", // version scope: 'current', 'strict' or 'all'
  days: 90,
  granularity: "day",
  sort: { key: "severity", dir: "asc" },
  expanded: new Map(), // signature -> expanded row state (see expandRow)
  charts: {},
  lastFetch: 0,
  pendingFocus: null, // signature to focus once its channel is rendered
  hideDrops: false, // drops are demoted while Socorro lags
  events: [], // platform events, grouped per day and source
  eventsData: null,
  account: null, // /api/me: { enabled, user: { email, name, picture } | null, domains }
};

const $ = id => document.getElementById(id);
const sum = arr => arr.reduce((a, b) => a + b, 0);

// -------------------------------------------------------------------- fetching
// Conditional requests: each URL's ETag is sent back and a 304 (no scheduler
// run since) reuses the cached JSON, so identity tells unchanged data.
// Concurrent requests of one URL share a fetch; prefetchJSON relies on it to
// load a deep-linked channel alongside the summary instead of after it.
const etags = new Map(); // url -> { etag, data }
const inflight = new Map(); // url -> promise

function requestURL(endpoint, params = {}) {
  const url = new URL(endpoint, API);
  if (endpoint !== "events" && app.scope !== "all") {
    url.searchParams.set("scope", app.scope);
  }
  for (const [k, v] of Object.entries(params)) {
    if (v != null) {
      url.searchParams.set(k, v);
    }
  }
  return url.toString();
}

function fetchJSON(endpoint, params) {
  const url = requestURL(endpoint, params);
  let promise = inflight.get(url);
  if (!promise) {
    promise = fetchURL(url).finally(() => inflight.delete(url));
    inflight.set(url, promise);
  }
  return promise;
}

function prefetchJSON(endpoint, params) {
  fetchJSON(endpoint, params).catch(() => {}); // its consumer reports the failure
}

async function fetchURL(url) {
  const known = etags.get(url);
  const headers = { Accept: "application/json" };
  if (known) {
    headers["If-None-Match"] = known.etag;
  }
  const res = await fetch(url, { headers, cache: "no-store" });
  if (res.status === 304 && known) {
    app.lastFetch = Date.now();
    return known.data;
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      msg += `: ${(await res.json()).error}`;
    } catch {
      // body not JSON
    }
    throw new Error(msg);
  }
  app.lastFetch = Date.now();
  const data = await res.json();
  const etag = res.headers.get("ETag");
  if (etag) {
    etags.set(url, { etag, data });
  }
  return data;
}

// --------------------------------------------------------------------- account
// Reading needs no account.  The header shows "Sign in" (a Mozilla Google
// account, see auth.py) or who is signed in.
async function loadAccount() {
  try {
    const res = await fetch(new URL("me", API), {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    app.account = await res.json();
  } catch {
    app.account = null;
  }
  renderAccount();
}

function renderAccount() {
  const box = $("account");
  const a = app.account;
  box.hidden = !(a?.enabled || a?.user);
  if (box.hidden) {
    box.textContent = "";
    return;
  }
  const here = location.pathname + location.search + location.hash; // back to this view afterwards
  if (!a.user) {
    const url = new URL("../login", API);
    url.searchParams.set("next", here);
    const who = (a.domains || []).map(d => `@${d}`).join(" or ");
    fill(
      box,
      el(
        "a",
        {
          class: "account-link",
          href: url.toString(),
          title: `Sign in with a ${who} Google account to change the dashboard`,
        },
        "Sign in"
      )
    );
    return;
  }
  fill(
    box,
    a.user.picture
      ? el("img", {
          class: "avatar",
          src: a.user.picture,
          alt: "",
          width: 24,
          height: 24,
          referrerpolicy: "no-referrer",
        })
      : null,
    el(
      "span",
      { class: "account-name", title: a.user.email },
      a.user.name || a.user.email
    ),
    el(
      "form",
      {
        class: "account-form",
        method: "post",
        action: new URL("../logout", API).toString(),
      },
      el("input", { type: "hidden", name: "next", value: here }),
      el("button", { type: "submit", class: "chart-btn" }, "Sign out")
    )
  );
}

function showError(msg) {
  const line = $("error-line");
  line.textContent = msg;
  line.hidden = !msg;
}

/** Screen-reader announcement through the persistent live region. */
function announce(msg) {
  const region = $("sr-status");
  region.textContent = "";
  setTimeout(() => {
    region.textContent = msg;
  }, 50);
}

/** data-focus key of the focused control in `container`, for restoreFocus(). */
function focusedKey(container) {
  const active = document.activeElement;
  return container.contains(active)
    ? active.closest("[data-focus]")?.dataset.focus || null
    : null;
}

function restoreFocus(container, key) {
  if (key) {
    container
      .querySelector(`[data-focus="${CSS.escape(key)}"]`)
      ?.focus({ preventScroll: true });
  }
}

// --------------------------------------------------------------------- loading
async function refresh({ initial = false } = {}) {
  if (initial) {
    loadAccount(); // independent of the data
    const h = parseHash();
    if (h) {
      app.scope = h.scope;
    }
    if (h?.product) {
      prefetchJSON("channel", channelParams(h.product, h.channel));
    }
  }
  try {
    let summary;
    try {
      summary = await fetchJSON("summary");
    } catch (e) {
      // a server that collects only the all scope rejects the default one
      if (!initial || app.scope === "all") {
        throw e;
      }
      app.scope = "all";
      summary = await fetchJSON("summary");
    }
    if (summary === app.summary) {
      renderFreshness(summary); // "N min ago" keeps counting
    } else {
      app.summary = summary;
      renderSummary();
    }
    await refreshEvents();
    const target = app.selected || defaultChannel();
    if (isAll(target)) {
      if (!app.selected) {
        selectAll();
      }
    } else if (target) {
      if (!app.selected && target.signature) {
        app.pendingFocus = target.signature;
      }
      await loadChannel(target.product, target.channel);
    }
    showError("");
  } catch (e) {
    const asOf = app.summary?.as_of
      ? ` — showing data from ${fmtTime(app.summary.as_of)}`
      : "";
    showError(
      initial && !app.summary
        ? `Could not load the dashboard (${e.message})`
        : `Could not refresh (${e.message})${asOf}`
    );
  }
}

function channelParams(product, channel) {
  return { product, channel, days: app.days, granularity: app.granularity };
}

function isSelected(product, channel) {
  return (
    channelSelected() &&
    app.selected.product === product &&
    app.selected.channel === channel
  );
}

async function loadChannel(product, channel) {
  if (!isSelected(product, channel)) {
    app.selected = { product, channel };
    clearExpanded();
    updateHash();
    highlightCard();
  }
  const params = channelParams(product, channel);
  const data = await fetchJSON("channel", params);
  // a newer selection or range owns the view: drop this response
  if (
    !isSelected(product, channel) ||
    app.days !== params.days ||
    app.granularity !== params.granularity
  ) {
    return;
  }
  if (data === app.channel) {
    showView();
    return;
  }
  app.channel = data;
  showView(); // final layout before renderDetail() may scroll to a row
  renderDetail();
  await refreshExpanded();
}

// ------------------------------------------------------------- platform events
/** Badges on the charts (Windows updates, drivers, OS releases): one payload
 * for the whole page, refreshed with the summary. */
async function refreshEvents() {
  let data;
  try {
    data = await fetchJSON("events", { days: 800 });
  } catch {
    return; // the charts work without badges
  }
  if (data === app.eventsData) {
    return;
  }
  app.eventsData = data;
  app.events = data.events || [];
  if (app.channel && channelSelected()) {
    renderCharts(app.channel);
    for (const st of app.expanded.values()) {
      if (st.data && st.panel) {
        renderSignaturePanel(st);
      }
    }
  }
}

/** Events of the platforms a product runs on. */
function eventsFor(product) {
  const platforms =
    product === "Fenix" ? ["android"] : ["windows", "mac", "linux"];
  return app.events.filter(g => platforms.includes(g.platform));
}

// --------------------------------------------------------------------- helpers
const fmtClock = iso => new Date(iso).toISOString().slice(11, 16);
const fmtTime = iso => `${fmtClock(iso)} UTC`;

function fmtAgo(iso, nowIso) {
  const now = nowIso ? new Date(nowIso).getTime() : Date.now();
  const min = Math.round((now - new Date(iso).getTime()) / 60_000);
  if (min < 1) {
    return "just now";
  }
  if (min < 90) {
    return `${min} min ago`;
  }
  if (min < 48 * 60) {
    return `${Math.round(min / 60)} h ago`;
  }
  return `${Math.round(min / 1440)} days ago`;
}

/** UTC midnight of the day on screen. */
function todayMs() {
  return parseDay(
    app.channel?.day ||
      app.summary?.channels?.[0]?.day ||
      new Date().toISOString().slice(0, 10)
  );
}

/** Severity shown for a score: its flag (today's, or a previous day's within
 * the flag window), with drops demoted while Socorro lags. */
function sevOf(score) {
  const s = score?.flag?.severity || score?.severity || "ok";
  return s === "drop" && app.hideDrops ? "ok" : s;
}

function isNew(row) {
  return !!(row?.flag ? row.flag.is_new : row?.is_new);
}

function isFlagged(row) {
  return sevOf(row) !== "ok" || isNew(row);
}

/** Days between the row's day and its flag's day (0 = today's own state). */
function flagAge(row) {
  if (!row?.flag || !row.day || row.flag.day === row.day) {
    return 0;
  }
  return Math.round((parseDay(row.day) - parseDay(row.flag.day)) / DAY_MS);
}

function flagWhen(row) {
  const age = flagAge(row);
  if (age === 1) {
    return "yesterday";
  }
  return age > 1 ? `${age} days ago` : "";
}

/** "major yesterday (2026-09-03): 430 vs 98 expected (z +12.9)" */
function flagTitle(row) {
  const f = row.flag;
  let t = `${f.severity}${isNew(row) ? ", new" : ""} ${flagWhen(row)} (${f.day}): ${fmtInt(f.observed)} vs ${fmtInt(f.expected)} expected`;
  if (f.z != null) {
    t += ` (z ${fmtZ(f.z)})`;
  }
  if (f.peak?.z != null) {
    t += `, peak z ${fmtZ(f.peak.z)}`;
  }
  return t;
}

function visibleAlerts(summary) {
  return (summary.alerts || []).filter(isFlagged);
}

function rowRank(row) {
  const s = sevOf(row);
  return s === "ok" && isNew(row) ? SEV_RANK.new : (SEV_RANK[s] ?? SEV_RANK.ok);
}

/** Severity thresholds of the channel on screen (learned per channel). */
function currentRules() {
  if (app.channel?.thresholds) {
    return app.channel.thresholds;
  }
  const all = Object.values(app.summary?.thresholds || {});
  return all.length === 1 ? all[0] : null;
}

const STORM_HELP =
  "Storm / crash loop: many crashes from a handful of installs (few machines crashing repeatedly), or 20+ crashes per install. Not a regression across users, so it is never an alert.";
const CHIP_HELP = {
  new: "New: not seen above the reporting cut on any of the previous 14 days.",
  storm: STORM_HELP,
  "crash-loop": STORM_HELP,
  noise:
    "Noise: a signature listed in the skiplist (processing artefacts such as shutdown kills or empty dumps). Shown, never alerted on.",
  ok: "OK: within the range the seasonal pattern predicts for this weekday and time of day.",
};

/** Plain-language meaning of a chip, with the thresholds the server uses. */
function chipHelp(kind) {
  if (CHIP_HELP[kind]) {
    return CHIP_HELP[kind];
  }
  const t = currentRules() || {};
  const above = t[kind]
    ? `at least ${t[kind].z} standard deviations above the seasonal expectation (this channel's learned threshold, see ?)`
    : "above the seasonal expectation by this channel's learned threshold (see ?)";
  switch (kind) {
    case "major":
      return `Major spike: crashes today are ${above}, and the number of distinct installs rose as much. The strongest alert.`;
    case "spike":
      return `Spike: crashes today are ${above}, and the number of distinct installs rose as much.`;
    case "watch":
      return `Watch: crashes today are ${above}. Worth a look, not yet a confirmed spike.`;
    case "drop": {
      const below = t.drop
        ? `at least ${Math.abs(t.drop.z)} standard deviations below the seasonal expectation (this channel's learned threshold)`
        : "well below the seasonal expectation";
      return `Drop: crashes today are ${below} (a fix landed, or a data problem).`;
    }
    default:
      return "";
  }
}

function chip(sev, text) {
  return el(
    "span",
    { class: `chip chip-${sev}`, title: chipHelp(sev) },
    text ?? sev
  );
}

function countChip(sev, n) {
  return el(
    "span",
    { class: `chip chip-${sev} chip-count`, title: chipHelp(sev) },
    el("span", { class: "n" }, String(n)),
    sev
  );
}

function badge(kind, text) {
  return el(
    "span",
    { class: "chip chip-neutral", title: chipHelp(kind) },
    text ?? kind
  );
}

function dots(confidence) {
  const n = clamp(confidence || 0, 0, 3);
  return n
    ? el(
        "span",
        { class: "dots", role: "img", "aria-label": `confidence ${n} of 3` },
        "●".repeat(n)
      )
    : null;
}

function deltaText(score) {
  if (score.z == null && score.excess == null) {
    return "not scored";
  }
  const ratio = fmtRatio(score.ratio);
  return `${fmtSigned(score.excess)}${ratio ? ` (${ratio})` : ""}`;
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** Hours a flag stays listed after the last run that raised it. */
function flagWindowHours() {
  return app.summary?.flag_window_hours || 48;
}

function displayedSeverities(kinds = COUNT_SEVERITIES) {
  return app.hideDrops ? kinds.filter(kind => kind !== "drop") : kinds;
}

function countBadges(counts = {}) {
  const shown = displayedSeverities().filter(kind => counts[kind]);
  return el(
    "div",
    { class: "card-counts" },
    ...shown.map(kind => countChip(kind, counts[kind])),
    counts.storm ? badge("storm", plural(counts.storm, "storm")) : null
  );
}

/** "channel excess mostly from crash loops (72 %)" when storm-driven. */
function stormNote(total) {
  if (!total?.storm_driven) {
    return null;
  }
  const share =
    total.storm_share != null
      ? ` (${Math.round(total.storm_share * 100)} %)`
      : "";
  return `channel excess mostly from crash loops${share}`;
}

function installsTitle(score) {
  if (score.installs == null) {
    return "installs unknown";
  }
  let t = `${fmtInt(score.installs)} installs`;
  if (score.expected_installs != null) {
    t += ` vs ${score.expected_installs.toLocaleString("en-US", { maximumFractionDigits: 1 })} expected`;
  }
  if (score.z_installs != null) {
    t += ` (z ${fmtZ(score.z_installs)})`;
  }
  if (score.installs_ratio != null) {
    t += ` · ${score.installs_ratio} crashes per install`;
  }
  return t;
}

function midTruncate(s, max = 70) {
  if (s.length <= max) {
    return s;
  }
  const head = Math.ceil((max - 1) * 0.6);
  return `${s.slice(0, head)}…${s.slice(-(max - 1 - head))}`;
}

/** When the row's flag was first raised (today's or the carried-over day's). */
function sinceOf(row) {
  return row.flag?.since || row.since || null;
}

function sinceText(row) {
  const days = row.flagged_days || 0;
  if (days >= 2) {
    return `${days + 1} days`;
  }
  if (days === 1) {
    return "yesterday";
  }
  const since = sinceOf(row);
  if (!since) {
    return "—";
  }
  const ms = new Date(since).getTime();
  const dayStart = todayMs();
  if (ms >= dayStart) {
    return `${fmtClock(since)} today`;
  }
  if (ms >= dayStart - DAY_MS) {
    return `yesterday ${fmtClock(since)}`;
  }
  return `${Math.round((dayStart - ms) / DAY_MS) + 1} days`;
}

/** Sort key: how long the row has been flagged, in ms. */
function sinceValue(row) {
  const since = sinceOf(row);
  const ms = since ? new Date(since).getTime() : todayMs() + DAY_MS;
  return (row.flagged_days || 0) * DAY_MS + (todayMs() + DAY_MS - ms);
}

// ---------------------------------------------------------- selection and hash
function isAll(sel) {
  return sel?.all === true;
}

function channelSelected() {
  return !!app.selected && !isAll(app.selected);
}

function channelKey(c) {
  return isAll(c) ? ALL_KEY : `${c.product}/${c.channel}`;
}

/** `#all`, `#Product/channel` or `#Product/channel/<encoded signature>`,
 * each optionally prefixed with a versioned scope (`current/`, `strict/`):
 * { scope, ... } or null. */
function parseHash() {
  const raw = location.hash.slice(1);
  if (!raw) {
    return null;
  }
  let parts = raw.split("/");
  let scope = "all";
  if (parts.length > 1 && VERSIONED_SCOPES.has(parts[0])) {
    scope = parts[0];
    parts = parts.slice(1);
  }
  const [product, channel, ...rest] = parts.map(decodeURIComponent);
  if (parts.length === 1 && product === ALL_KEY) {
    return { ...ALL, scope };
  }
  if (!product || !channel) {
    return null;
  }
  const res = { product, channel, scope };
  const signature = rest.join("/");
  if (signature) {
    res.signature = signature;
  }
  return res;
}

function scopePrefix() {
  return app.scope === "all" ? "" : `${app.scope}/`;
}

function channelHash(product, channel, signature = null) {
  const parts = [product, channel, signature]
    .filter(Boolean)
    .map(encodeURIComponent);
  return `#${scopePrefix()}${parts.join("/")}`;
}

function updateHash(signature = null) {
  if (!app.selected) {
    return;
  }
  let hash;
  if (isAll(app.selected)) {
    hash = `#${scopePrefix()}${ALL_KEY}`;
  } else {
    // a signature deep link stays in the address bar while its channel is shown
    const current = parseHash();
    if (
      !signature &&
      current?.signature &&
      channelKey(current) === channelKey(app.selected)
    ) {
      return;
    }
    hash = channelHash(app.selected.product, app.selected.channel, signature);
  }
  if (location.hash !== hash) {
    history.replaceState(null, "", hash);
  }
}

/** The view to open on load: the hash if it names a known channel, else ALL. */
function defaultChannel() {
  const channels = app.summary?.channels || [];
  if (!channels.length) {
    return null;
  }
  const h = parseHash();
  if (
    h &&
    !isAll(h) &&
    channels.some(c => c.product === h.product && c.channel === h.channel)
  ) {
    return { product: h.product, channel: h.channel, signature: h.signature };
  }
  return ALL;
}

function scrollBehavior() {
  return matchMedia("(prefers-reduced-motion: reduce)").matches
    ? "auto"
    : "smooth";
}

/** The toolbar is pinned: the start of a view is the page top. */
function scrollToContent() {
  if (window.scrollY > 0) {
    window.scrollTo({ top: 0, behavior: scrollBehavior() });
  }
}

/** Show the cross-channel report or the channel detail. */
function showView() {
  const hasData = (app.summary?.channels || []).length > 0;
  const all = isAll(app.selected);
  $("flagged").hidden = !hasData || !all;
  $("detail").hidden = !hasData || all || !app.channel;
}

function selectAll() {
  app.selected = ALL;
  app.channel = null;
  clearExpanded();
  updateHash();
  highlightCard();
  if (app.summary) {
    renderAlerts(app.summary);
  }
  showView();
}

// ------------------------------------------------------------------ tab status
// The tab shows the overall health: a disc on the favicon and a title prefix.
const TAB_COLORS = {
  major: "#d03b3b",
  spike: "#ec835a",
  watch: "#fab219",
  drop: "#2a78d6",
  ok: "#0ca30c",
  stale: "#898781",
};
const baseIcon = new Image();
baseIcon.src = new URL("favicon.png", import.meta.url).href;
let tabColor = null;
baseIcon.addEventListener("load", () => {
  if (tabColor) {
    drawFavicon(tabColor);
  }
});

/** Worst visible severity, count per severity, whether the data is stale. */
function overallHealth(s) {
  const counts = {};
  for (const row of visibleAlerts(s)) {
    const sev = sevOf(row);
    if (sev !== "ok") {
      counts[sev] = (counts[sev] || 0) + 1;
    }
  }
  const worst = ALERT_SEVERITIES.find(kind => counts[kind]) || "ok";
  const health = s.data_health;
  const stale =
    !!health && health.status !== "ok" && health.status !== "backfilling";
  return { worst, counts, stale };
}

function drawFavicon(color) {
  const size = 32;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (!ctx) {
    return;
  }
  if (baseIcon.complete && baseIcon.naturalWidth) {
    // the logo, a quarter turn counter-clockwise
    ctx.translate(size / 2, size / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.drawImage(baseIcon, -size / 2, -size / 2, size, size);
    ctx.resetTransform();
  }
  const r = 6; // status disc, top right
  ctx.beginPath();
  ctx.arc(size - r - 1, r + 1, r, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
  $("favicon").href = canvas.toDataURL("image/png");
}

function renderTabStatus(s) {
  const h = overallHealth(s);
  let label = "OK";
  if (h.stale) {
    label = "stale";
  } else if (h.worst !== "ok") {
    label = ALERT_SEVERITIES.filter(kind => h.counts[kind])
      .slice(0, 2)
      .map(kind => `${h.counts[kind]} ${kind}`)
      .join(" · ");
  }
  document.title = `${label} – Crash spikes`;
  tabColor = TAB_COLORS[h.stale ? "stale" : h.worst];
  drawFavicon(tabColor);
}

// ---------------------------------------------------------------------- header
/** "Current version | All versions", when the server collects both scopes. */
function renderScopeControls(s) {
  const box = $("scope-controls");
  const scopes = s.scopes || ["all"];
  box.hidden = scopes.length < 2;
  for (const input of box.querySelectorAll('input[name="scope"]')) {
    input.checked = input.value === app.scope;
    input.disabled = !scopes.includes(input.value);
  }
}

/** Switch scope.  From the toggle the selection is kept (the hash gets its
 * prefix); from a hash change the hash owns the selection and is re-read by
 * defaultChannel(). */
async function setScope(scope, { fromHash = false } = {}) {
  if (scope === app.scope) {
    return;
  }
  app.scope = scope;
  clearExpanded();
  app.channel = null;
  if (fromHash) {
    app.selected = null;
  } else {
    updateHash();
  }
  await refresh();
}

/** The version a versioned-scope channel shows today ("155", "156.0b3"). */
function versionTag(c) {
  return c?.version && c.scope !== "all" ? c.version : null;
}

function renderSummary() {
  const s = app.summary;
  renderScopeControls(s);
  const hideDrops = s.data_health?.status === "stale_upstream";
  if (hideDrops !== app.hideDrops) {
    app.hideDrops = hideDrops;
    rowCache = new WeakMap(); // rows show severities
  }
  for (const label of document.querySelectorAll("#sig-filters .chip-toggle")) {
    label.title = chipHelp(label.querySelector("input").value);
  }
  renderTabStatus(s);
  renderFreshness(s);
  renderBanner(s);
  renderCards(s);
  if (isAll(app.selected)) {
    renderAlerts(s);
  }
}

function renderFreshness(s) {
  const p = $("freshness");
  if (!s.as_of) {
    p.textContent = "No data yet";
    return;
  }
  const run = s.last_run || {};
  const status = (run.status || "unknown").toUpperCase();
  fill(
    p,
    `Data as of ${fmtTime(s.as_of)} (${fmtAgo(s.as_of)}) · last run `,
    run.status === "ok"
      ? status
      : el("span", { class: "bad", title: run.message || "" }, status),
    run.queries != null
      ? ` · ${run.queries} ${run.queries === 1 ? "query" : "queries"}`
      : null,
    run.failures ? `, ${run.failures} failed` : null
  );
}

function renderBanner(s) {
  const banner = $("banner");
  const health = s.data_health || { status: "ok" };
  banner.className = "banner";
  if (health.status === "ok") {
    banner.textContent = "";
    return;
  }
  let text;
  if (health.status === "stale_upstream") {
    text = `Socorro processing appears delayed${health.since ? ` since ${fmtTime(health.since)}` : ""} — drops are hidden`;
  } else if (health.status === "backfilling") {
    const days = Math.max(
      0,
      ...(s.channels || []).map(c => c.history_days || 0)
    );
    text = `Backfilling history: ${plural(days, "day")} loaded`;
    banner.classList.add("is-info");
  } else if (health.status === "stale_local") {
    const run = s.last_run;
    const lastGood = (run?.status === "ok" && run.finished) || s.as_of;
    text = `Data is stale: last successful run ${lastGood ? fmtAgo(lastGood, s.now) : "unknown"}`;
    banner.classList.add("is-critical");
  } else {
    text = `Data health: ${health.status}`;
  }
  fill(
    banner,
    el("strong", {}, text),
    health.detail ? el("span", {}, health.detail) : null,
    s.last_run?.status !== "ok" && s.last_run?.message
      ? el("span", {}, `Last run: ${s.last_run.message}`)
      : null
  );
}

// -------------------------------------------------------------- overview cards
function renderCards(s) {
  const wrap = $("channel-cards");
  const channels = s.channels || [];
  const focus = focusedKey(wrap);
  $("empty-state").hidden = channels.length > 0;
  const groups = [];
  if (channels.length) {
    groups.push(
      el(
        "div",
        { class: "card-group card-group-all" },
        el("div", { class: "card-group-title", "aria-hidden": "true" }, " "),
        el("div", { class: "card-row" }, allCard(s))
      )
    );
    for (const [product, list] of Map.groupBy(channels, c => c.product)) {
      groups.push(
        el(
          "div",
          {
            class: "card-group",
            role: "group",
            "aria-label": product,
            style: `--n:${list.length}`,
          },
          el(
            "div",
            { class: "card-group-title", "aria-hidden": "true" },
            product
          ),
          el("div", { class: "card-row" }, ...list.map(channelCard))
        )
      );
    }
  }
  wrap.replaceChildren(...groups);
  highlightCard();
  restoreFocus(wrap, focus);
  showView();
}

/** Cross-channel card: what is flagged anywhere right now. */
function allCard(s) {
  const rows = visibleAlerts(s);
  const nchan = new Set(rows.map(channelKey)).size;
  const totals = {};
  for (const kind of COUNT_KINDS) {
    totals[kind] = sum((s.channels || []).map(c => c.counts?.[kind] || 0));
  }
  return el(
    "button",
    {
      type: "button",
      class: "card card-all",
      "data-key": ALL_KEY,
      "data-focus": `card:${ALL_KEY}`,
      "aria-pressed": "false",
    },
    el(
      "div",
      { class: "card-head" },
      el("span", { class: "card-title" }, "All channels"),
      chip(overallHealth(s).worst)
    ),
    // on its own line: next to the title it would push the chip out of the card
    SCOPE_TAG[app.scope]
      ? el("div", { class: "version-tag card-scope" }, SCOPE_TAG[app.scope])
      : null,
    el("div", { class: "tile-label" }, `Flagged, last ${flagWindowHours()} h`),
    el(
      "div",
      { class: "card-value" },
      fmtInt(rows.length),
      el(
        "span",
        { class: "vs" },
        rows.length
          ? `flagged in ${plural(nchan, "channel")}`
          : "nothing flagged"
      )
    ),
    countBadges(totals)
  );
}

function channelCard(c) {
  const t = c.total || {};
  const key = channelKey(c);
  const version = versionTag(c);
  const note = stormNote(t);
  const logo = logoFor(c.product, c.channel);
  return el(
    "button",
    {
      type: "button",
      class: "card",
      "data-key": key,
      "data-focus": `card:${key}`,
      "aria-pressed": "false",
    },
    el(
      "div",
      { class: "card-head" },
      // the product is the group's title; keep it in the button's name
      el(
        "span",
        { class: "card-title" },
        el("span", { class: "visually-hidden" }, `${c.product} `),
        c.channel,
        version
          ? el(
              "span",
              {
                class: "version-tag",
                title: `Only version ${version}, the one current today`,
              },
              version
            )
          : null
      ),
      chip(sevOf(t))
    ),
    el("div", { class: "tile-label" }, "Today so far"),
    el(
      "div",
      { class: "card-value" },
      fmtInt(t.observed),
      el("span", { class: "vs" }, `vs ${fmtInt(t.expected)} expected`)
    ),
    el("div", { class: "card-delta" }, deltaText(t), dots(t.confidence)),
    note ? el("div", { class: "card-note" }, note) : null,
    countBadges(c.counts),
    // decorative: the product is already in the name
    logo
      ? el("img", {
          class: "card-logo",
          src: new URL(logo, import.meta.url).href,
          alt: "",
          "aria-hidden": "true",
          width: 20,
          height: 20,
        })
      : null
  );
}

/** Channel logo.  Nightly and Daily have their own; Firefox beta wears the
 * Developer Edition one (the beta channel itself ships the release logo). */
function logoFor(product, channel) {
  const family = {
    Firefox: "firefox",
    Fenix: "firefox",
    Thunderbird: "thunderbird",
  }[product];
  if (!family) {
    return null;
  }
  const suffix =
    channel === "nightly" || channel === "beta" ? `-${channel}` : "";
  return `logo-${family}${suffix}.svg`;
}

function highlightCard() {
  const key = app.selected ? channelKey(app.selected) : null;
  for (const card of document.querySelectorAll(".card")) {
    const on = card.dataset.key === key;
    card.classList.toggle("is-selected", on);
    card.setAttribute("aria-pressed", String(on));
  }
}

async function selectChannel(product, channel, signature = null) {
  if (isSelected(product, channel) && app.channel) {
    if (signature) {
      focusSignature(signature);
    } else {
      scrollToContent();
    }
    return;
  }
  app.pendingFocus = signature; // consumed when the channel renders
  try {
    await loadChannel(product, channel);
    showError("");
    if (!signature) {
      scrollToContent();
    }
  } catch (e) {
    app.pendingFocus = null;
    showError(`Could not load ${product} ${channel} (${e.message})`);
  }
}

// ----------------------------------------------------------------- flagged now
function renderAlerts(s) {
  const rows = visibleAlerts(s);
  const hours = flagWindowHours();
  document.querySelector("#flagged-title .sub").textContent =
    `flagged in the last ${hours} h`;
  $("flagged-meta").textContent = rows.length
    ? `${plural(rows.length, "flagged signature")} across ${plural(new Set(rows.map(channelKey)).size, "channel")}`
    : `Nothing flagged in the last ${hours} h`;
  const wrap = $("alerts-table");
  const focus = focusedKey(wrap);
  const sorted = rows.toSorted(
    (a, b) =>
      rowRank(a) - rowRank(b) ||
      Math.abs(b.excess || 0) - Math.abs(a.excess || 0)
  );
  const onRow = row => selectChannel(row.product, row.channel, row.signature);
  fill(
    wrap,
    rows.length
      ? buildTable(sorted, { withChannel: true, sortable: false, onRow })
      : null
  );
  restoreFocus(wrap, focus);
}

// -------------------------------------------------------------- channel detail
/** The "All" range needs more than 180 days of history (Socorro keeps 6
 * months; the dashboard accumulates its own). */
function renderRangeOptions(ch) {
  const history = ch.model?.history_days || ch.history_days || 0;
  const all = $("range-all");
  all.hidden = history <= 180;
  if (all.hidden && app.days > 180) {
    app.days = 180;
    document.querySelector(
      '#range-controls input[name="days"][value="180"]'
    ).checked = true;
  }
}

function renderDetail() {
  const ch = app.channel;
  $("detail").hidden = false;
  renderRangeOptions(ch);
  const version = versionTag(ch);
  fill(
    $("detail-title"),
    `${ch.product} ${ch.channel}`,
    version
      ? el(
          "span",
          { class: "version-tag", title: SCOPE_NOTE[ch.scope] },
          `version ${version}`
        )
      : null
  );
  const scopeNote = SCOPE_TAG[ch.scope]
    ? ` · ${SCOPE_TAG[ch.scope].replace(/s$/, "")} only`
    : "";
  $("detail-meta").textContent =
    `${fmtDateLong(parseDay(ch.day))} · data as of ${fmtTime(ch.as_of)}${scopeNote}`;
  renderTiles(ch);
  renderDrivers(ch);
  renderCharts(ch);
  renderModel(ch.model, ch);
  renderSignatures();
  if (app.pendingFocus) {
    const sig = app.pendingFocus;
    app.pendingFocus = null;
    focusSignature(sig);
  }
}

function tile(label, value, sub, { chipNode, note, counts } = {}) {
  return el(
    "div",
    { class: "tile" },
    el(
      "div",
      { class: "tile-head" },
      el("span", { class: "tile-label" }, label),
      chipNode
    ),
    el("div", { class: "tile-value" }, ...value),
    sub ? el("div", { class: "tile-sub" }, ...sub) : null,
    note ? el("div", { class: "tile-note" }, note) : null,
    counts
  );
}

const vs = text => el("span", { class: "vs" }, text);

function renderTiles(ch) {
  const t = ch.total || {};
  const sev = sevOf(t);
  const elapsed = `${Math.round((t.elapsed_fraction || 0) * 100)} % of the day's crashes`;
  const c = ch.counts || {};
  const flagged = sum(
    displayedSeverities(ALERT_SEVERITIES).map(kind => c[kind] || 0)
  );
  const extras = [
    c.new ? `${c.new} new` : null,
    c.storm ? plural(c.storm, "storm") : null,
    c.noise ? `${c.noise} noise` : null,
  ].filter(Boolean);

  let projected;
  if (t.projected == null) {
    projected = tile(
      "Projected today",
      ["—"],
      [`Too early to project (${elapsed} are usually in by now)`]
    );
  } else {
    projected = tile(
      "Projected today",
      [
        fmtCompact(t.projected),
        vs(`vs ${fmtCompact(t.expected_day)} expected`),
      ],
      [
        `${fmtCompact(t.projected_lo)}–${fmtCompact(t.projected_hi)} likely · ${elapsed} usually in by now`,
      ]
    );
  }
  let yesterday;
  const y = ch.yesterday;
  if (y) {
    let finalNote = null;
    if (y.final != null) {
      finalNote = y.final ? " (final)" : " (still updating)";
    }
    const ysev = sevOf(y);
    yesterday = tile(
      "Yesterday",
      [fmtCompact(y.observed), vs(`vs ${fmtCompact(y.expected)} expected`)],
      [deltaText(y), dots(y.confidence), finalNote],
      { chipNode: ysev === "ok" ? null : chip(ysev) }
    );
  } else {
    yesterday = tile("Yesterday", ["—"], ["No data for yesterday yet"]);
  }
  fill(
    $("tiles"),
    tile(
      "Today so far",
      [fmtCompact(t.observed), vs(`vs ${fmtCompact(t.expected)} expected`)],
      t.z == null ? ["too early to score"] : [deltaText(t), dots(t.confidence)],
      {
        chipNode: chip(sev),
        note:
          stormNote(t) ||
          (t.since && sev !== "ok"
            ? `flagged since ${fmtTime(t.since)}`
            : null),
      }
    ),
    projected,
    yesterday,
    tile(
      "Flagged",
      [String(flagged), vs(`of ${fmtInt(c.scored)} scored`)],
      [extras.join(" · ") || "nothing unusual"],
      { counts: countBadges(c) }
    )
  );
}

/** "Driven by sig (40 %), sig (12 %)" with badges; a link focuses its row. */
function renderDrivers(ch) {
  const p = $("drivers");
  const focus = focusedKey(p);
  const drivers = (ch.total?.drivers || []).filter(d => d.share != null);
  const note = stormNote(ch.total);
  p.hidden = !drivers.length && !note;
  const parts = [];
  if (note) {
    parts.push(
      el(
        "span",
        { class: "storm-note" },
        note.charAt(0).toUpperCase() + note.slice(1)
      ),
      drivers.length ? " · " : null
    );
  }
  if (drivers.length) {
    parts.push("Driven by ");
  }
  drivers.forEach((d, i) => {
    if (i) {
      parts.push(", ");
    }
    parts.push(
      el(
        "a",
        {
          href: "#",
          title: d.signature,
          "data-sig": d.signature,
          "data-focus": `driver:${d.signature}`,
        },
        `${midTruncate(d.signature, 48)} (${Math.round(d.share * 100)} %)`
      )
    );
    if (d.storm) {
      parts.push(
        " ",
        badge("crash-loop"),
        d.installs != null ? ` ${plural(d.installs, "install")}` : null
      );
    }
    if (d.noise) {
      parts.push(" ", badge("noise"));
    }
  });
  fill(p, ...parts);
  restoreFocus(p, focus);
}

function productOf(data) {
  return data.product || data.row?.product || app.channel?.product;
}

function dailySpec(data, extra) {
  const daily = data.daily || {};
  return {
    ...daily,
    dates: daily.start || [],
    releases: data.releases || app.channel?.releases || [],
    events: eventsFor(productOf(data)),
    height: CHART_HEIGHT,
    ...extra,
  };
}

/** The day lets the chart translate UTC hour buckets into local time. */
function hourlySpec(data, extra) {
  return {
    hours: [],
    ...data.hourly,
    day: data.day || data.row?.day || app.channel?.day,
    events: eventsFor(productOf(data)),
    height: CHART_HEIGHT,
    ...extra,
  };
}

/** Every "Today by hour" title shows the clock in use (UTC or local zone). */
function renderZoneLabels() {
  const label = zoneLabel();
  for (const n of document.querySelectorAll(".tz-label")) {
    n.textContent = label;
  }
}

function renderCharts(ch) {
  const n = ch.daily?.start?.length || 0;
  $("daily-sub").textContent =
    `${n} ${ch.daily?.granularity === "week" ? "weeks" : "days"}`;
  const hourly = hourlySpec(ch, {
    ariaLabel: `Crashes per hour today, ${ch.product} ${ch.channel}`,
  });
  const daily = dailySpec(ch, {
    ariaLabel: `Daily crashes, ${ch.product} ${ch.channel}`,
  });
  if (app.charts.intraday) {
    app.charts.intraday.update(hourly);
    app.charts.daily.update(daily);
  } else {
    app.charts.intraday = barChart($("intraday-chart"), hourly);
    app.charts.daily = lineChart($("daily-chart"), daily);
  }
}

// ----------------------------------------------------------- model explanation
// In the current scope the 28-day cycle counts from the version's release:
// its factors are the rollout ramp of a new version.
function isReleaseCycle(model) {
  return model?.cycle_from === "release";
}

function cycleName(model) {
  return isReleaseCycle(model) ? "rollout" : "cycle";
}

/** 1-based day of the cycle: from the API, else an unambiguous factor match. */
function cycleDay(model) {
  const explicit = model.today_factors?.cycle_day ?? model.cycle_day;
  if (explicit != null) {
    return explicit;
  }
  const f = model.today_factors?.cycle;
  if (f == null) {
    return null;
  }
  const hits = (model.factors?.cycle || []).flatMap((v, i) =>
    Math.abs(v - f) < 1e-6 ? [i + 1] : []
  );
  return hits.length === 1 ? hits[0] : null;
}

const factor = x => `×${x.toFixed(2)}`;
const fmtDispersion = model =>
  model.dispersion != null ? model.dispersion.toFixed(1) : "—";

function modelSummaryText(model, day) {
  if (!model) {
    return "";
  }
  const tf = model.today_factors || {};
  const comp = model.components || {};
  const parts = [
    `Today: ${WDAYS[new Date(parseDay(day)).getUTCDay()]}${tf.weekly != null ? ` ${factor(tf.weekly)}` : ""}`,
  ];
  if (comp.cycle?.active && tf.cycle != null) {
    const d = cycleDay(model);
    let when = "";
    if (d && isReleaseCycle(model)) {
      when = d === 1 ? ": release day" : `: day ${d - 1} after the release`;
    } else if (d) {
      when = ` day ${d}/${model.factors.cycle.length}`;
    }
    parts.push(`${cycleName(model)}${when} ${factor(tf.cycle)}`);
  } else if (comp.cycle) {
    parts.push(
      `${cycleName(model)} n/a (${comp.cycle.cycles} / ${comp.cycle.min_cycles} cycles)`
    );
  }
  if (comp.yearly?.active) {
    parts.push(
      tf.yearly != null ? `yearly ${factor(tf.yearly)}` : "yearly active"
    );
  } else if (comp.yearly) {
    parts.push(
      `yearly n/a (${comp.yearly.cycles} / ${comp.yearly.min_cycles} cycles)`
    );
  }
  parts.push(
    `level ${fmtCompact(model.level)}`,
    `dispersion ${fmtDispersion(model)}`
  );
  return parts.join(" · ");
}

function componentLine(name, c, model) {
  const label = {
    weekly: "weekly seasonality",
    cycle: isReleaseCycle(model)
      ? "rollout ramp (days since the release)"
      : "release cycle (28 days)",
    yearly: "yearly seasonality",
  }[name];
  if (!c) {
    return `${label}: unknown`;
  }
  if (!c.active) {
    return `${label}: not enough history (${c.cycles} / ${c.min_cycles} cycles)`;
  }
  const src = model.borrowed?.includes(name)
    ? ", borrowed from the channel"
    : "";
  return `${label}: active (${c.cycles} cycles${src})`;
}

function renderModel(model, ch) {
  $("model-summary").textContent = modelSummaryText(model, ch.day);
  const body = $("model-body");
  if (!model) {
    body.textContent = "";
    return;
  }
  const weekly = el("div");
  if (model.factors?.weekly) {
    miniFactors(weekly, {
      values: model.factors.weekly,
      labels: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
      highlight: (new Date(parseDay(ch.day)).getUTCDay() + 6) % 7,
      kind: "bar",
    });
  }
  const cycle = el("div");
  if (model.factors?.cycle) {
    miniFactors(cycle, {
      values: model.factors.cycle,
      highlight: (cycleDay(model) || 0) - 1,
      kind: "line",
      ticks: [1, 8, 15, 22, 28],
    });
  }
  const yearly = model.components?.yearly?.active ? " × yearly factor" : "";
  fill(
    body,
    el(
      "div",
      { class: "model-block" },
      el("h4", {}, "Weekday factors"),
      weekly
    ),
    el(
      "div",
      { class: "model-block" },
      el(
        "h4",
        {},
        isReleaseCycle(model)
          ? "Rollout factors (day 1 = release day, then the days after it)"
          : "Release-cycle factors (day of the 28-day cycle)"
      ),
      cycle
    ),
    el(
      "div",
      { class: "model-block" },
      el("h4", {}, "Components and band"),
      el(
        "ul",
        {},
        ...["weekly", "cycle", "yearly"].map(k =>
          el("li", {}, componentLine(k, model.components?.[k], model))
        )
      ),
      el(
        "p",
        {},
        `Expected = level (${fmtInt(model.level)}, median of the last 14 de-seasonalised days) × weekday factor × ${cycleName(model)} factor${yearly}. ` +
          `Residuals are measured on the Anscombe scale, 2·(√(observed + ⅜) − √(expected + ⅜)), and divided by the dispersion ${fmtDispersion(model)}; ` +
          `the grey bands are ±3 (watch) and ±5 (spike) dispersions around the expectation. History: ${model.history_days ?? "—"} days.`
      )
    )
  );
}

// ------------------------------------------------------------- signature table
function readFilters() {
  const data = new FormData($("sig-filters"));
  return {
    severities: new Set(data.getAll("sev")),
    text: String(data.get("query") || "")
      .trim()
      .toLowerCase(),
    hideNoise: data.has("hide-noise"),
    minCrashes: Math.max(0, Number(data.get("min-crashes")) || 0),
    showStorms: data.has("show-storms"),
    showUnflagged: data.has("show-unflagged"),
  };
}

function rowCategory(row) {
  if (isFlagged(row)) {
    return "flagged";
  }
  return row.storm ? "storm" : "unflagged";
}

function matchesSeverityFilter(row, severities) {
  const severity = sevOf(row);
  return (
    (severity !== "ok" && severities.has(severity)) ||
    (isNew(row) && severities.has("new"))
  );
}

function visibleRows() {
  const rows = app.channel?.signatures || [];
  const filters = readFilters();
  const shown = [];
  const counts = { storm: 0, unflagged: 0 };
  for (const row of rows) {
    if (
      (filters.hideNoise && row.noise) ||
      (row.observed || 0) < filters.minCrashes ||
      (filters.text && !row.signature.toLowerCase().includes(filters.text))
    ) {
      continue;
    }
    const category = rowCategory(row);
    let wanted;
    if (category === "flagged") {
      wanted = matchesSeverityFilter(row, filters.severities);
    } else {
      counts[category] += 1;
      wanted =
        category === "storm" ? filters.showStorms : filters.showUnflagged;
    }
    if (wanted) {
      shown.push(row);
    }
  }
  return {
    shown,
    stormCount: counts.storm,
    unflaggedCount: counts.unflagged,
    total: rows.length,
  };
}

const SORTERS = {
  severity: r => rowRank(r) * 1e12 - Math.abs(r.excess || 0),
  channel: channelKey,
  signature: r => r.signature.toLowerCase(),
  observed: r => r.observed,
  expected: r => r.expected,
  excess: r => r.excess,
  recent: r => r.recent?.excess ?? null,
  installs: r => r.installs,
  since: r => (isFlagged(r) ? sinceValue(r) : null),
  trend: r => r.level_change_28,
  bug: r => shownBug(r)?.id ?? null,
};
const DEFAULT_DIR = {
  severity: "asc",
  channel: "asc",
  signature: "asc",
  since: "desc",
};

/** Sort by app.sort; rows without a value go last whatever the direction. */
function sortRows(rows) {
  const { key, dir } = app.sort;
  const get = SORTERS[key] || SORTERS.severity;
  const sign = dir === "asc" ? 1 : -1;
  return rows.toSorted((a, b) => {
    const va = get(a);
    const vb = get(b);
    if (va == null || vb == null) {
      return (va == null) - (vb == null);
    }
    return typeof va === "string"
      ? sign * va.localeCompare(vb)
      : sign * (va - vb);
  });
}

function rowElement(sig) {
  return document.querySelector(
    `#signature-table tr.row[data-sig="${CSS.escape(sig)}"]`
  );
}

function renderSignatures() {
  const { shown, unflaggedCount, stormCount, total } = visibleRows();
  $("unflagged-count").textContent = String(unflaggedCount);
  $("storm-count").textContent = plural(stormCount, "storm");
  const noMatch = shown.length
    ? ""
    : " — no signature matches the current filters";
  const metaText = total
    ? `${shown.length} of ${total} scored signatures shown${noMatch}`
    : "No scored signatures yet";
  const meta = $("signatures-meta");
  if (meta.textContent !== metaText) {
    meta.textContent = metaText; // role=status: announce changes only
  }
  const wrap = $("signature-table");
  const focus = focusedKey(wrap); // keep the keyboard user's place across the rebuild
  if (shown.length) {
    fill(
      wrap,
      buildTable(sortRows(shown), {
        withChannel: false,
        sortable: true,
        onRow: toggleRow,
      })
    );
  } else {
    fill(
      wrap,
      el(
        "div",
        { class: "table-empty" },
        total
          ? "No signature matches the current filters"
          : "No scored signatures yet"
      )
    );
  }
  for (const sig of app.expanded.keys()) {
    const tr = rowElement(sig);
    if (tr) {
      reattachExpanded(tr, sig);
    } else {
      collapseRow(sig); // filtered out: its panel goes with it
    }
  }
  restoreFocus(wrap, focus);
}

function columns(withChannel) {
  const recentHours = rows => rows?.find(r => r.recent)?.recent.hours;
  const hours =
    recentHours(app.channel?.signatures) ||
    recentHours(app.summary?.alerts) ||
    3;
  return [
    { key: "severity", label: "Severity" },
    withChannel ? { key: "channel", label: "Channel" } : null,
    { key: "signature", label: "Signature" },
    { key: "observed", label: "Today so far", num: true },
    { key: "expected", label: "Expected", num: true },
    { key: "excess", label: "Delta", num: true },
    {
      key: "recent",
      label: `Last ${hours}h`,
      num: true,
      hint: "observed / expected",
      title: "observed / expected over the last hours",
    },
    { key: "installs", label: "Installs", num: true },
    { key: "since", label: "Since" },
    { key: "trend", label: "28 days" },
    {
      key: "bug",
      label: "Bug",
      title:
        "Bugs whose crash signature is this one. Green: filed for this spike (once the crash was there). Red: only bugs from before the spike (a known crash, spiking again).",
    },
  ].filter(Boolean);
}

function headerCell(c, sortable) {
  const attrs = {
    scope: "col",
    class: c.num ? "num" : null,
    title: c.title || null,
  };
  const hint = c.hint
    ? el("span", { class: "visually-hidden" }, `, ${c.hint}`)
    : null;
  if (!sortable) {
    return el("th", attrs, c.label, hint);
  }
  const active = app.sort.key === c.key;
  const asc = app.sort.dir === "asc";
  let indicator = "";
  if (active) {
    attrs["aria-sort"] = asc ? "ascending" : "descending";
    indicator = asc ? "▲" : "▼";
  }
  const btn = el(
    "button",
    { type: "button", "data-focus": `sort:${c.key}` },
    c.label,
    hint,
    el("span", { class: "sort-ind", "aria-hidden": "true" }, indicator)
  );
  btn.addEventListener("click", () => {
    if (active) {
      app.sort.dir = asc ? "desc" : "asc";
    } else {
      app.sort = { key: c.key, dir: DEFAULT_DIR[c.key] || "desc" };
    }
    renderSignatures();
  });
  return el("th", attrs, btn);
}

/** The rows table.  Interactions are delegated to the table: a click on a
 * row, its expander or its channel button calls onRow(row, tr); the copy and
 * permalink buttons act on the row's signature. */
function buildTable(rows, { withChannel, sortable, onRow }) {
  const rowOf = new Map(); // tr -> row
  const tbody = el("tbody");
  for (const row of rows) {
    const tr = buildRow(row, withChannel);
    rowOf.set(tr, row);
    tbody.append(tr);
  }
  const head = el(
    "tr",
    {},
    ...columns(withChannel).map(c => headerCell(c, sortable))
  );
  const table = el("table", { class: "rows" }, el("thead", {}, head), tbody);
  table.addEventListener("click", e => {
    const tr = e.target.closest("tr.row");
    if (!tr) {
      return;
    }
    const row = rowOf.get(tr);
    const control = e.target.closest("a, button, input");
    if (!control) {
      onRow(row, tr);
    } else if (control.matches(".row-expander, .chip-btn")) {
      onRow(row, tr);
    } else if (control.matches(".copy-btn")) {
      copySignature(control, row.signature);
    } else if (control.matches(".perma-btn")) {
      e.preventDefault();
      history.replaceState(null, "", control.getAttribute("href"));
      focusSignature(row.signature);
      announce("Link to this signature is in the address bar");
    }
  });
  table.addEventListener("keydown", e => {
    // Enter or Space on a focused row; controls inside handle their own keys
    if (!e.target.matches("tr.row") || (e.key !== "Enter" && e.key !== " ")) {
      return;
    }
    e.preventDefault();
    onRow(rowOf.get(e.target), e.target);
  });
  return table;
}

async function copySignature(btn, signature) {
  try {
    await navigator.clipboard.writeText(signature);
    btn.textContent = "✓";
    announce("Signature copied");
    setTimeout(() => {
      btn.textContent = "⧉";
    }, 1200);
  } catch {
    announce("Could not copy the signature");
  }
}

// Row elements are reused across re-sorts and filter changes; the cache
// empties with the data (new row objects) and with app.hideDrops.
let rowCache = new WeakMap(); // row -> tr

function buildRow(row, withChannel) {
  let tr = rowCache.get(row);
  if (!tr) {
    tr = renderRow(row, withChannel);
    rowCache.set(row, tr);
  }
  return tr;
}

const hidden = text => el("span", { class: "visually-hidden" }, text);

function renderRow(row, withChannel) {
  const sev = sevOf(row);
  const fresh = isNew(row);
  const sig = row.signature;
  // the row is clickable; keyboard users get a real button (the expander,
  // or the channel button in the cross-channel table)
  const tr = el("tr", { class: "row", tabindex: -1, "data-sig": sig });

  const badges = el("div", { class: "badge-set" });
  if (sev !== "ok" || !fresh) {
    badges.append(chip(sev));
  }
  if (fresh) {
    badges.append(chip("new"));
  }
  // a flag carried over from a previous day (scores are per UTC day; the
  // flag window keeps yesterday's spikes listed)
  if (row.flag && flagAge(row) > 0 && isFlagged(row)) {
    badges.append(
      el("span", { class: "flag-when", title: flagTitle(row) }, flagWhen(row))
    );
  }
  if (row.storm) {
    badges.append(badge("storm"));
  }
  if (row.noise) {
    badges.append(badge("noise"));
  }
  tr.append(el("td", {}, badges));

  if (withChannel) {
    const open = el(
      "button",
      {
        type: "button",
        class: "chip chip-neutral chip-btn",
        "data-focus": `open:${sig}`,
        "aria-label": `Open ${row.product} ${row.channel} at this signature`,
      },
      `${row.product} ${row.channel}`
    );
    tr.append(el("td", {}, open));
  }
  const expander = withChannel
    ? null
    : el(
        "button",
        {
          type: "button",
          class: "row-expander",
          "aria-expanded": "false",
          "data-focus": `exp:${sig}`,
          "aria-label": `Details of ${midTruncate(sig, 60)}`,
        },
        "▸"
      );
  const permalink = withChannel
    ? null
    : el(
        "a",
        {
          class: "perma-btn",
          href: channelHash(
            row.product || app.selected?.product,
            row.channel || app.selected?.channel,
            sig
          ),
          title: "Link to this signature",
          "aria-label": "Link to this signature",
          "data-focus": `perma:${sig}`,
        },
        "#"
      );
  tr.append(
    el(
      "td",
      { class: "sig" },
      expander,
      el(
        "a",
        {
          href: row.socorro_url || "#",
          title: sig,
          target: "_blank",
          rel: "noopener",
          "data-focus": `link:${sig}`,
        },
        midTruncate(sig, 70),
        hidden(" (crash-stats, opens in a new tab)")
      ),
      el(
        "button",
        {
          type: "button",
          class: "copy-btn",
          "aria-label": "Copy signature",
          title: "Copy signature",
          "data-focus": `copy:${sig}`,
        },
        "⧉"
      ),
      permalink
    ),
    el("td", { class: "num" }, fmtInt(row.observed)),
    el("td", { class: "num" }, fmtInt(row.expected))
  );

  // scores are in title attributes for mouse users and hidden text for the rest
  const ratio = fmtRatio(row.ratio);
  const z = row.z != null ? `z ${fmtZ(row.z)}` : null;
  tr.append(
    el(
      "td",
      { class: "num", title: z || "not scored" },
      `${fmtSigned(row.excess)}${ratio ? ` ${ratio}` : ""}`,
      dots(row.confidence),
      z ? hidden(` (${z})`) : null
    )
  );
  tr.append(recentCell(row));
  const inst = el(
    "td",
    { class: "num", title: installsTitle(row) },
    fmtInt(row.installs),
    hidden(` (${installsTitle(row)})`)
  );
  if (row.storm) {
    inst.append(" ", badge("crash-loop"));
  }
  tr.append(
    inst,
    el("td", { class: "since" }, isFlagged(row) ? sinceText(row) : "—")
  );
  const spark = el("td", { class: "spark" });
  if (row.spark?.dates?.length) {
    sparkline(spark, {
      ...row.spark,
      severity: sev,
      partial: row.partial !== false,
    });
  }
  tr.append(spark, bugCell(row));
  return tr;
}

/** "Last Nh" cell: observed / expected, muted when the window is unscored. */
function recentCell(row) {
  const r = row.recent;
  if (!r) {
    const why = row.recent_reason || "not scorable";
    return el(
      "td",
      { class: "num" },
      el("span", { class: "dash", title: why }, "—"),
      hidden(why)
    );
  }
  const text = `${fmtInt(r.observed)} / ${fmtInt(r.expected)}`;
  if (r.z == null) {
    const why = row.recent_reason || "not scored";
    return el(
      "td",
      { class: "num muted", title: why },
      text,
      hidden(` (${why})`)
    );
  }
  const z = `z ${fmtZ(r.z)}`;
  return el(
    "td",
    {
      class: "num",
      title: r.ratio != null ? `${z} · ${fmtRatio(r.ratio)}` : z,
    },
    text,
    hidden(` (${z})`)
  );
}

const RESOLVED = new Set(["RESOLVED", "VERIFIED", "CLOSED"]);

/** The bug a row shows: the newest filed after the spike, else the newest. */
function shownBug(row) {
  const bugs = row.bugs || [];
  return bugs.find(b => b.after) || bugs[0] || null;
}

function bugTitle(b) {
  if (b.restricted) {
    return "Restricted bug";
  }
  const filed = b.created
    ? `filed ${b.created.slice(0, 16).replace("T", " ")} UTC`
    : "filing time unknown";
  let when = "";
  if (b.after === true) {
    when = ", for this spike (once the crash was there)";
  } else if (b.after === false) {
    when = ", before the spike: a known crash";
  }
  const state = [b.status, b.resolution].filter(Boolean).join(" ");
  return `Bug ${b.id}${b.summary ? `: ${b.summary}` : ""}\n${filed}${when}${state ? ` · ${state}` : ""}`;
}

/** Bug column: the shown bug (green when filed for the spike, red when only
 * known before it, struck through when resolved); the others in a tooltip. */
function bugCell(row) {
  const b = shownBug(row);
  if (!b) {
    return el("td", { class: "num" }, el("span", { class: "dash" }, "—"));
  }
  const classes = ["bug"];
  let note = "";
  if (b.after === true) {
    classes.push("bug-after");
    note = " (filed for this spike)";
  } else if (b.after === false) {
    classes.push("bug-before");
    note = " (filed before the spike)";
  } else if (b.restricted) {
    note = " (restricted bug)";
  }
  if (RESOLVED.has(b.status)) {
    classes.push("bug-closed");
  }
  let lock = null;
  if (b.restricted) {
    classes.push("bug-restricted");
    lock = iconSvg("lock", 11);
    lock.classList.add("bug-lock");
  }
  const link = el(
    "a",
    {
      class: classes.join(" "),
      href: `https://bugzilla.mozilla.org/${b.id}`,
      target: "_blank",
      rel: "noopener",
      title: bugTitle(b),
    },
    lock,
    b.after === true
      ? el("span", { class: "bug-mark", "aria-hidden": "true" }, "✓ ")
      : null, // not colour alone
    String(b.id),
    hidden(`${note}, opens in a new tab`)
  );
  const others = (row.bugs || []).filter(x => x !== b);
  return el(
    "td",
    { class: "num" },
    link,
    others.length
      ? el(
          "span",
          { class: "bug-more", title: others.map(bugTitle).join("\n\n") },
          ` +${others.length}`
        )
      : null
  );
}

// --------------------------------------------------------------- row expansion
function toggleRow(row, tr) {
  if (app.expanded.has(row.signature)) {
    collapseRow(row.signature);
  } else {
    expandRow(row, tr);
  }
}

function markExpanded(tr, on) {
  tr.classList.toggle("is-expanded", on);
  const btn = tr.querySelector(".row-expander");
  if (btn) {
    btn.setAttribute("aria-expanded", String(on));
    btn.textContent = on ? "▾" : "▸";
  }
}

function collapseRow(sig) {
  const st = app.expanded.get(sig);
  if (!st) {
    return;
  }
  markExpanded(st.tr, false);
  st.detail.remove();
  for (const c of Object.values(st.charts)) {
    c.destroy();
  }
  app.expanded.delete(sig);
}

function expandRow(row, tr) {
  const status = el("p", { class: "detail-note", role: "status" });
  const td = el("td", { colspan: String(tr.children.length) }, status);
  const detail = el("tr", { class: "row-detail" }, td);
  tr.after(detail);
  markExpanded(tr, true);
  const st = {
    tr,
    detail,
    td,
    statusEl: status,
    charts: {},
    row,
    data: null,
    panel: null,
  };
  app.expanded.set(row.signature, st);
  setTimeout(() => {
    if (!st.panel) {
      status.textContent = "Loading…"; // the live region exists before its text
    }
  }, 50);
  loadSignature(st);
}

function reattachExpanded(tr, sig) {
  const st = app.expanded.get(sig);
  tr.after(st.detail);
  markExpanded(tr, true);
  st.tr = tr;
}

async function loadSignature(st) {
  const { product, channel } = app.selected;
  try {
    const data = await fetchJSON("signature", {
      ...channelParams(product, channel),
      signature: st.row.signature,
    });
    if (
      app.expanded.get(st.row.signature) !== st ||
      (st.panel && data === st.data)
    ) {
      return; // collapsed meanwhile, or same data as shown
    }
    st.data = data;
    renderSignaturePanel(st);
  } catch (e) {
    st.panel = null;
    st.statusEl.className = "detail-note";
    st.statusEl.textContent = `Could not load this signature (${e.message})`;
    st.td.replaceChildren(st.statusEl);
  }
}

function signatureNotes(data, r) {
  const notes = [];
  if (!data.hourly) {
    notes.push(
      "No hourly data for this signature (it was below the per-hour top-200 cut)."
    );
  }
  if (r.recent?.z == null) {
    notes.push(
      `Last hours not scored${r.recent_reason ? `: ${r.recent_reason}` : ""}.`
    );
  }
  if (r.z == null) {
    notes.push("Today not scored yet (expected so far is too small).");
  }
  return notes.join(" ");
}

function signatureModelText(data, r) {
  const parts = [modelSummaryText(data.model, r.day || app.channel.day)];
  if (data.model?.borrowed?.length) {
    parts.push(
      `${data.model.borrowed.join(" and ")} factors borrowed from the channel`
    );
  }
  if (data.hourly?.profile_source) {
    parts.push(
      `hourly profile: ${data.hourly.profile_source === "own" ? "this signature" : "channel"}`
    );
  }
  return parts.join(" · ");
}

function createSignaturePanel(st) {
  st.statusEl.className = "visually-hidden";
  st.statusEl.textContent = "Details loaded";
  st.modelEl = el("p", { class: "detail-model full" });
  st.noteEl = el("p", { class: "detail-note full", hidden: true });
  st.intradayEl = el("div");
  st.dailyEl = el("div");
  st.panel = el(
    "div",
    { class: "detail-panel" },
    st.modelEl,
    st.noteEl,
    el(
      "div",
      { class: "chart-card" },
      el(
        "h3",
        {},
        "Today by hour ",
        el("span", { class: "sub tz-label" }, zoneLabel())
      ),
      st.intradayEl
    ),
    el(
      "div",
      { class: "chart-card" },
      el("h3", {}, "Daily crashes"),
      st.dailyEl
    )
  );
  st.td.replaceChildren(st.statusEl, st.panel);
  for (const c of Object.values(st.charts)) {
    c.destroy();
  }
  st.charts = {};
}

/** Build the panel once; later refreshes update text and charts in place
 * (keeps zoom, log/table toggles and focus). */
function renderSignaturePanel(st) {
  const { data, row } = st;
  const r = data.row || row;
  const short = midTruncate(r.signature, 60);
  const hourly = hourlySpec(data, {
    emptyMessage: "No hourly data for this signature",
    ariaLabel: `Crashes per hour today, ${short}`,
  });
  const daily = dailySpec(data, { ariaLabel: `Daily crashes, ${short}` });
  const create = !st.panel?.isConnected;
  if (create) {
    createSignaturePanel(st);
  }
  st.modelEl.textContent = signatureModelText(data, r);
  const notes = signatureNotes(data, r);
  st.noteEl.textContent = notes;
  st.noteEl.hidden = !notes;
  if (create) {
    st.charts.intraday = barChart(st.intradayEl, hourly);
    st.charts.daily = lineChart(st.dailyEl, daily);
  } else {
    st.charts.intraday.update(hourly);
    st.charts.daily.update(daily);
  }
}

function refreshExpanded() {
  return Promise.all([...app.expanded.values()].map(loadSignature));
}

function clearExpanded() {
  for (const sig of app.expanded.keys()) {
    collapseRow(sig);
  }
}

/** Scroll to, expand and focus a signature row, relaxing filters hiding it. */
function focusSignature(sig) {
  const row = (app.channel?.signatures || []).find(r => r.signature === sig);
  if (!row) {
    showError(
      `No scored row today for "${midTruncate(sig, 80)}" in ${app.selected?.product} ${app.selected?.channel}`
    );
    return;
  }
  const filters = readFilters();
  const category = rowCategory(row);
  let changed = false;
  const relax = (id, prop, value) => {
    $(id)[prop] = value;
    changed = true;
  };
  if (row.noise && filters.hideNoise) {
    relax("hide-noise", "checked", false);
  }
  if (category === "storm" && !filters.showStorms) {
    relax("show-storms", "checked", true);
  }
  if (category === "unflagged" && !filters.showUnflagged) {
    relax("show-unflagged", "checked", true);
  }
  if (filters.text && !sig.toLowerCase().includes(filters.text)) {
    relax("sig-search", "value", "");
  }
  if ((row.observed || 0) < filters.minCrashes) {
    relax("min-crashes", "value", "0");
  }
  if (
    category === "flagged" &&
    !matchesSeverityFilter(row, filters.severities)
  ) {
    const severity = sevOf(row) === "ok" ? "new" : sevOf(row);
    document.querySelector(`#sig-filters input[value="${severity}"]`).checked =
      true;
    changed = true;
  }
  if (changed || !rowElement(sig)) {
    renderSignatures();
  }
  const tr = rowElement(sig);
  if (!tr) {
    return;
  }
  if (!app.expanded.has(sig)) {
    expandRow(row, tr);
  }
  tr.scrollIntoView({ behavior: scrollBehavior(), block: "start" }); // scroll-padding keeps it below the toolbar
  tr.classList.add("is-target");
  tr.focus({ preventScroll: true });
  setTimeout(() => tr.classList.remove("is-target"), 2500);
}

// --------------------------------------------------------- thresholds help (?)
// Every threshold is learned from each channel's own data by the scheduler
// (calibration.py); this view shows the current values and the method.
function fmtPct(x, digits = 2) {
  return x == null ? "—" : `${(x * 100).toFixed(digits)} %`;
}

const LEVELS = ["watch", "spike", "major", "drop"];

function renderHelp() {
  const channels = app.summary?.channels || [];
  const calib = channels.find(c => c.calibration)?.calibration;
  const rates = calib?.rates || {
    watch: 0.015,
    spike: 0.0015,
    major: 0.00015,
    drop: 0.0015,
  };
  const item = (title, text) => el("li", {}, el("b", {}, title), text);
  const method = el(
    "div",
    { class: "help-method" },
    el("h3", {}, "How"),
    el(
      "ul",
      {},
      item(
        "Score. ",
        "For every signature and day, z = distance between the observed count and the seasonal expectation, on the Anscombe scale, divided by the fitted dispersion (over-dispersion grows with the count, so no ratio gate is needed)."
      ),
      item(
        "Severity thresholds. ",
        `Per channel, the quantiles of its own one-step-ahead z over the last months, pooled over all its scored signatures. The only setting is the false-alarm rate per signature and day each level may have: watch ${fmtPct(rates.watch)}, spike ${fmtPct(rates.spike)}, major ${fmtPct(rates.major)}, drop ${fmtPct(rates.drop)} (lower tail). A noisy channel gets a higher bar by itself.`
      ),
      item(
        "Floor. ",
        'The Gaussian value for the same rate: real tails are never lighter. It is used outright when the pooled sample is under 300 series-days; when a level\'s tail holds fewer than 5 points it is extrapolated from an exponential fit of the top of the sample ("extrapolated" below).'
      ),
      item(
        "Volume floors. ",
        `A signature must reach ${fmtPct(calib?.volume_share ?? 0.001, 1)} of its channel's expected daily crashes over the last 24 hours (installs: half of it, at least 2) to be flagged at all.`
      ),
      item(
        "Storm. ",
        `Crashes per install above the ${fmtPct(calib?.storm_quantile ?? 0.995, 1)} quantile of the channel's own signatures over the last 4 weeks: a badge, never an alert.`
      ),
      item(
        "Installs. ",
        "An upward severity also needs the distinct-install count to deviate as much as the crash count; the final severity is the lower of the two."
      ),
      item(
        "Refresh. ",
        "Recomputed at every scheduler run (5 min) from the fits cached with the models (refitted every 6 h)."
      )
    )
  );
  const headers = [
    "Channel",
    "watch",
    "spike",
    "major",
    "drop",
    "min crashes",
    "min installs",
    "storm ≥ crashes/install",
    "sample",
    "days above watch",
  ];
  const table = el(
    "table",
    { class: "rows help-table" },
    el(
      "thead",
      {},
      el("tr", {}, ...headers.map(h => el("th", { scope: "col" }, h)))
    ),
    el("tbody", {}, ...channels.map(helpRow))
  );
  fill($("help-body"), method, el("h3", {}, "Now"), table);
}

function helpRow(c) {
  const k = c.calibration;
  const num = (text, title) => el("td", { class: "num", title }, text);
  const tr = el("tr", {}, el("td", {}, `${c.product} ${c.channel}`));
  for (const level of LEVELS) {
    const how = k?.method?.[level];
    const gaussian = k?.gaussian?.[level];
    const cell = num(
      fmtZ(c.thresholds?.[level]?.z),
      how
        ? `${how}${gaussian != null ? `; Gaussian floor ${gaussian}` : ""}`
        : null
    );
    if (how && how !== "empirical") {
      cell.append(
        el(
          "span",
          { class: "muted" },
          how === "gaussian" ? " (Gaussian)" : " (extrapolated)"
        )
      );
    }
    tr.append(cell);
  }
  tr.append(
    num(k ? fmtInt(k.min_crashes) : "—"),
    num(k ? fmtInt(k.min_installs) : "—"),
    num(k?.storm_ratio != null ? k.storm_ratio.toFixed(1) : "—"),
    num(
      k ? `${fmtInt(k.sample)} (${fmtInt(k.series)} sig.)` : "—",
      "series-days of one-step-ahead z pooled over the scored signatures"
    ),
    num(
      k?.tail?.watch != null ? fmtPct(k.tail.watch) : "—",
      "share of the pooled series-days at or above the watch threshold (includes real spikes)"
    )
  );
  return tr;
}

function openHelp() {
  renderHelp();
  $("help").showModal();
}

// -------------------------------------------------------------------- controls
function bindControls() {
  $("help-btn").addEventListener("click", openHelp);
  $("help-close").addEventListener("click", () => $("help").close());
  $("help").addEventListener("click", e => {
    if (e.target === e.currentTarget) {
      e.currentTarget.close(); // backdrop
    }
  });
  $("channel-cards").addEventListener("click", e => {
    const key = e.target.closest(".card")?.dataset.key;
    if (!key) {
      return;
    }
    if (key === ALL_KEY) {
      selectAll();
      scrollToContent();
      return;
    }
    const [product, channel] = key.split("/");
    selectChannel(product, channel);
  });
  $("drivers").addEventListener("click", e => {
    const link = e.target.closest("a[data-sig]");
    if (link) {
      e.preventDefault();
      focusSignature(link.dataset.sig);
    }
  });
  $("scope-controls").addEventListener("change", e => {
    if (e.target.name === "scope") {
      setScope(e.target.value);
    }
  });
  $("range-controls").addEventListener("change", e => {
    if (e.target.name === "days") {
      app.days = Number(e.target.value);
    }
    if (e.target.name === "granularity") {
      app.granularity = e.target.value;
    }
    if (channelSelected()) {
      loadChannel(app.selected.product, app.selected.channel).then(
        () => showError(""),
        err => showError(`Could not reload (${err.message})`)
      );
    }
  });
  const filters = $("sig-filters");
  const typed = new Set(["sig-search", "min-crashes"]); // re-rendered on input, debounced
  filters.addEventListener("submit", e => e.preventDefault());
  filters.addEventListener("change", e => {
    if (!typed.has(e.target.id)) {
      renderSignatures();
    }
  });
  let timer = null;
  filters.addEventListener("input", e => {
    if (typed.has(e.target.id)) {
      clearTimeout(timer);
      timer = setTimeout(
        renderSignatures,
        e.target.id === "sig-search" ? 120 : 200
      );
    }
  });
  window.addEventListener("hashchange", () => {
    if (app.account) {
      renderAccount(); // the sign-in/out "next" follows the view
    }
    const h = parseHash();
    if (!h || !app.summary) {
      return;
    }
    if (h.scope !== app.scope) {
      setScope(h.scope, { fromHash: true });
      return;
    }
    if (isAll(h)) {
      if (!isAll(app.selected)) {
        selectAll();
      }
      return;
    }
    const same = app.selected && channelKey(h) === channelKey(app.selected);
    if (!same || h.signature) {
      selectChannel(h.product, h.channel, h.signature || null);
    }
  });
  window.addEventListener("dashboard:timezone", renderZoneLabels);
  renderZoneLabels();
  // the sticky toolbar's height offsets anchored scrolls (scroll-margin-top)
  const toolbar = $("toolbar");
  const setToolbarHeight = () =>
    document.documentElement.style.setProperty(
      "--toolbar-h",
      `${toolbar.offsetHeight}px`
    );
  new ResizeObserver(setToolbarHeight).observe(toolbar);
  setToolbarHeight();
  const onVisible = () => {
    if (!document.hidden && Date.now() - app.lastFetch > FOCUS_REFRESH_MS) {
      refresh();
    }
  };
  document.addEventListener("visibilitychange", onVisible);
  window.addEventListener("focus", onVisible);
  setInterval(refresh, REFRESH_MS);
}

bindControls();
refresh({ initial: true });
// for debugging and browser tests
window.dashboardRefresh = () => refresh();
window.dashboardState = app;
