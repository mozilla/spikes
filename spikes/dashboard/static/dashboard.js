// dashboard.js — crash-spikes dashboard: fetch, state, rendering, interactions.
import {
  lineChart, barChart, sparkline, miniFactors, el,
  fmtInt, fmtCompact, fmtSigned, fmtRatio, fmtZ, parseDay, fmtDateLong,
  zoneLabel,
} from './charts.js';

const API = new URL('../api/', import.meta.url);
const REFRESH_MS = 5 * 60 * 1000;
const FOCUS_REFRESH_MS = 60 * 1000;
const DAY_MS = 86400000;
const ALERT_SEVERITIES = ['major', 'spike', 'watch', 'drop'];
const COUNT_SEVERITIES = [...ALERT_SEVERITIES, 'new'];
const COUNT_KINDS = [...COUNT_SEVERITIES, 'storm'];
const SEV_RANK = { major: 0, spike: 1, watch: 2, new: 3, drop: 4, ok: 5 };
const WDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const CHART_HEIGHT = 280;

// Channel branding (mozilla-central / comm-central): Nightly and Daily have
// their own logos; Firefox beta wears the Developer Edition logo (the
// beta channel itself ships the release one); Fenix is Firefox for Android.
function logoFor(product, channel) {
  const family = product === 'Thunderbird' ? 'thunderbird'
    : product === 'Firefox' || product === 'Fenix' ? 'firefox' : null;
  if (!family) return null;
  const suffix = channel === 'nightly' || channel === 'beta' ? `-${channel}` : '';
  return `logo-${family}${suffix}.svg`;
}

const app = {
  summary: null,
  channel: null,
  selected: null,
  days: 90,
  granularity: 'day',
  sort: { key: 'severity', dir: 'asc' },
  expanded: new Map(), // signature -> { tr, panel, charts, data }
  charts: {},
  lastFetch: 0,
  pendingFocus: null,
  hideDrops: false,
  events: [], // platform events (badges on the charts), grouped per day and source
  eventsData: null,
  account: null, // /api/me: { enabled, user: { email, name, picture } | null, domains }
};

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- fetching
// Conditional requests: the ETag of every URL is remembered and sent back;
// a 304 (nothing new since the last scheduler run) reuses the cached JSON.
const etags = new Map(); // url -> { etag, data }

async function fetchJSON(endpoint, params = {}) {
  const url = new URL(endpoint, API);
  for (const [k, v] of Object.entries(params)) if (v != null) url.searchParams.set(k, v);
  const key = url.toString();
  const known = etags.get(key);
  const headers = { Accept: 'application/json' };
  if (known?.etag) headers['If-None-Match'] = known.etag;
  const res = await fetch(url, { headers, cache: 'no-store' });
  if (res.status === 304 && known) {
    app.lastFetch = Date.now();
    return known.data;
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg += `: ${(await res.json()).error}`; } catch { /* body not JSON */ }
    throw new Error(msg);
  }
  app.lastFetch = Date.now();
  const data = await res.json();
  const etag = res.headers.get('ETag');
  if (etag) etags.set(key, { etag, data });
  return data;
}

// ---------------------------------------------------------------- account
// Reading needs no account; changing the dashboard needs a Mozilla Google
// account (auth.py).  The header shows a "Sign in" link, or who is signed in.
async function loadAccount() {
  try {
    const res = await fetch(new URL('me', API), { headers: { Accept: 'application/json' }, cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    app.account = await res.json();
  } catch {
    app.account = null; // the header simply shows nothing
  }
  renderAccount();
  if (app.channel) renderSignatures(); // the "mark done" buttons depend on it
}

function signedIn() {
  return !!app.account?.user;
}

/** Mark (or unmark) a flagged signature as done, then refetch: the mark is part
 * of the data version, so the cached channel and summary are stale. */
async function markDone(row, done) {
  const url = new URL('done', API);
  let res;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ product: row.product || app.selected?.product, channel: row.channel || app.selected?.channel, signature: row.signature, done }),
    });
  } catch (e) {
    showError(`Could not mark the signature (${e.message})`);
    return;
  }
  if (res.status === 401) {
    app.account = { ...(app.account || { enabled: true, domains: [] }), user: null }; // the session ended
    renderAccount();
    renderSignatures();
    showError('Your session has ended: sign in again to mark signatures');
    return;
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg += `: ${(await res.json()).error}`; } catch { /* body not JSON */ }
    showError(`Could not mark the signature (${msg})`);
    return;
  }
  announce(done ? 'Marked done' : 'Mark removed');
  await refresh();
}

function renderAccount() {
  const box = $('account');
  const a = app.account;
  box.textContent = '';
  box.hidden = !(a?.enabled || a?.user);
  if (box.hidden) return;
  const here = location.pathname + location.search + location.hash; // come back to this view
  if (!a.user) {
    const url = new URL('../login', API);
    url.searchParams.set('next', here);
    const who = (a.domains || []).map((d) => `@${d}`).join(' or ');
    box.append(el('a', { class: 'account-link', href: url.toString(), title: `Sign in with a ${who} Google account to change the dashboard` }, 'Sign in'));
    return;
  }
  if (a.user.picture) box.append(el('img', { class: 'avatar', src: a.user.picture, alt: '', width: 24, height: 24, referrerpolicy: 'no-referrer' }));
  box.append(el('span', { class: 'account-name', title: a.user.email }, a.user.name || a.user.email));
  const form = el('form', { class: 'account-form', method: 'post', action: new URL('../logout', API).toString() });
  form.append(el('input', { type: 'hidden', name: 'next', value: here }));
  form.append(el('button', { type: 'submit', class: 'chart-btn' }, 'Sign out'));
  box.append(form);
}

function showError(msg) {
  const line = $('error-line');
  line.textContent = msg;
  line.hidden = !msg;
}

/** Polite screen-reader announcement (a persistent live region). */
function announce(msg) {
  const r = $('sr-status');
  if (!r) return;
  r.textContent = '';
  setTimeout(() => { r.textContent = msg; }, 50);
}

/** Key of the focused control inside *container* (data-focus attribute), for restoring
 * focus after the container is rebuilt. */
function focusedKey(container) {
  const a = document.activeElement;
  if (!a || !container.contains(a)) return null;
  return a.closest('[data-focus]')?.dataset.focus || null;
}

function restoreFocus(container, key) {
  if (!key) return;
  const target = [...container.querySelectorAll('[data-focus]')].find((n) => n.dataset.focus === key);
  target?.focus({ preventScroll: true });
}

async function refresh({ initial = false } = {}) {
  if (initial) loadAccount(); // independent of the data; not awaited
  try {
    const summary = await fetchJSON('summary');
    // A 304 returns the cached object, so identity is enough to avoid a redraw.
    const changed = summary !== app.summary;
    if (changed) {
      app.summary = summary;
      renderSummary();
    } else renderFreshness(app.summary); // "N min ago" keeps counting
    await refreshEvents();
    const target = app.selected || defaultChannel();
    if (isAll(target)) {
      if (!app.selected) selectAll();
    } else if (target) {
      if (!app.selected && target.signature) app.pendingFocus = target.signature; // deep link to a signature
      await loadChannel(target.product, target.channel);
    }
    showError('');
  } catch (e) {
    const asOf = app.summary?.as_of ? ` — showing data from ${fmtTime(app.summary.as_of)}` : '';
    showError(initial && !app.summary ? `Could not load the dashboard (${e.message})` : `Could not refresh (${e.message})${asOf}`);
  }
}

async function loadChannel(product, channel) {
  const changed = !app.selected || isAll(app.selected) || app.selected.product !== product || app.selected.channel !== channel;
  app.selected = { product, channel };
  if (changed) {
    clearExpanded();
    updateHash();
    highlightCard();
  }
  const req = { product, channel, days: app.days, granularity: app.granularity };
  const data = await fetchJSON('channel', req);
  // a newer selection or range change owns the view: drop this response
  if (!app.selected || isAll(app.selected) || app.selected.product !== req.product ||
      app.selected.channel !== req.channel || app.days !== req.days || app.granularity !== req.granularity) return;
  if (data === app.channel) {
    showView();
    return; // same data, same view: leave the DOM alone
  }
  app.channel = data;
  showView(); // final layout before renderDetail() may scroll to a row
  renderDetail();
  await refreshExpanded();
}

// ---------------------------------------------------------------- platform events
/** Badges on the charts (Windows updates, drivers, OS releases): one small payload for
 * the whole page, refreshed with the summary; unchanged data costs a 304. */
async function refreshEvents() {
  let data;
  try {
    data = await fetchJSON('events', { days: 800 });
  } catch {
    return; // the charts work without badges
  }
  if (data === app.eventsData) return;
  app.eventsData = data;
  app.events = data.events || [];
  if (app.channel && app.selected && !isAll(app.selected)) {
    renderCharts(app.channel);
    for (const st of app.expanded.values()) if (st.data && st.panel) renderSignaturePanel(st);
  }
}

const DESKTOP_PLATFORMS = ['windows', 'mac', 'linux'];

/** Platforms whose events matter for a product: Fenix runs on Android, the rest on desktop. */
function platformsFor(product) {
  return product === 'Fenix' ? ['android'] : DESKTOP_PLATFORMS;
}

function eventsFor(product) {
  const platforms = platformsFor(product);
  return app.events.filter((g) => platforms.includes(g.platform));
}

// ---------------------------------------------------------------- helpers
function fmtTime(iso) {
  const d = new Date(iso);
  return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')} UTC`;
}

function fmtAgo(iso, nowIso) {
  const now = nowIso ? new Date(nowIso).getTime() : Date.now();
  const min = Math.round((now - new Date(iso).getTime()) / 60000);
  if (min < 1) return 'just now';
  if (min < 90) return `${min} min ago`;
  if (min < 48 * 60) return `${Math.round(min / 60)} h ago`;
  return `${Math.round(min / 1440)} days ago`;
}

function todayMs() {
  return parseDay(app.channel?.day || app.summary?.channels?.[0]?.day || new Date().toISOString().slice(0, 10));
}

/** Severity shown: the row's flag (today's live state, or what a previous day reached
 * within the flag window) after the data-health rule (drops are demoted when Socorro lags). */
function sevOf(score) {
  const s = score?.flag?.severity || score?.severity || 'ok';
  return s === 'drop' && app.hideDrops ? 'ok' : s;
}

function isNew(row) {
  return !!(row?.flag ? row.flag.is_new : row?.is_new);
}

/** Days between the row's day and the day its flag comes from (0 = today's own state). */
function flagAge(row) {
  if (!row?.flag || !row.day || row.flag.day === row.day) return 0;
  return Math.round((parseDay(row.day) - parseDay(row.flag.day)) / DAY_MS);
}

/** "done by someone@mozilla.com on 3 Sep 2026 14:02 (was a spike)". */
function doneTitle(row) {
  const d = row.done;
  if (!d) return '';
  const was = d.severity && d.severity !== 'ok' ? ` (was a ${d.severity})` : '';
  return `Marked done by ${d.by || 'someone'} on ${fmtTime(d.at)}${was}`;
}

function flagWhen(row) {
  const age = flagAge(row);
  return age === 1 ? 'yesterday' : age > 1 ? `${age} days ago` : '';
}

/** "major yesterday: 430 vs 98 expected (z +12.9)" for a carried-over flag. */
function flagTitle(row) {
  const f = row.flag;
  let t = `${f.severity}${isNew(row) ? ', new' : ''} ${flagWhen(row)} (${f.day}): ${fmtInt(f.observed)} vs ${fmtInt(f.expected)} expected`;
  if (f.z != null) t += ` (z ${fmtZ(f.z)})`;
  if (f.peak?.z != null) t += `, peak z ${fmtZ(f.peak.z)}`;
  return t;
}

function visibleAlerts(summary) {
  return (summary.alerts || []).filter((row) => sevOf(row) !== 'ok' || isNew(row));
}

/** The alerts still to look at: done ones are listed (with their badge) but not counted. */
function openAlerts(summary) {
  return visibleAlerts(summary).filter((row) => !row.done);
}

function rowRank(row) {
  const s = sevOf(row);
  if (s === 'ok' && isNew(row)) return SEV_RANK.new;
  return SEV_RANK[s] ?? SEV_RANK.ok;
}

/** The severity thresholds of the channel on screen (learned per channel, see the ? view). */
function currentRules() {
  if (app.channel?.thresholds) return app.channel.thresholds;
  const all = app.summary?.thresholds || {};
  const keys = Object.keys(all);
  return keys.length === 1 ? all[keys[0]] : null;
}

/** Plain-language meaning of a chip, with the thresholds the server uses. */
function chipHelp(kind) {
  const t = currentRules() || {};
  const rule = (k) => (t[k] ? `at least ${t[k].z} standard deviations above the seasonal expectation (this channel's learned threshold, see ?)` : 'above the seasonal expectation by this channel\'s learned threshold (see ?)');
  switch (kind) {
    case 'major': return `Major spike: crashes today are ${rule('major')}, and the number of distinct installs rose as much. The strongest alert.`;
    case 'spike': return `Spike: crashes today are ${rule('spike')}, and the number of distinct installs rose as much.`;
    case 'watch': return `Watch: crashes today are ${rule('watch')}. Worth a look, not yet a confirmed spike.`;
    case 'drop': return `Drop: crashes today are ${t.drop ? `at least ${Math.abs(t.drop.z)} standard deviations below the seasonal expectation (this channel's learned threshold)` : 'well below the seasonal expectation'} (a fix landed, or a data problem).`;
    case 'new': return 'New: not seen above the reporting cut on any of the previous 14 days.';
    case 'storm': case 'crash-loop': return 'Storm / crash loop: many crashes from a handful of installs (few machines crashing repeatedly), or 20+ crashes per install. Not a regression across users, so it is never an alert.';
    case 'noise': return 'Noise: a signature listed in the skiplist (processing artefacts such as shutdown kills or empty dumps). Shown, never alerted on.';
    case 'done': return 'Done: a signed-in Mozillian marked this spike as handled (bug filed, cause known). Hidden by default. The mark ends with the spike, or when it reaches a higher severity.';
    case 'ok': return 'OK: within the range the seasonal pattern predicts for this weekday and time of day.';
    default: return '';
  }
}

function chip(sev, text) {
  return el('span', { class: `chip chip-${sev}`, title: chipHelp(sev) }, text ?? sev);
}

function countChip(sev, n) {
  return el('span', { class: `chip chip-${sev} chip-count`, title: chipHelp(sev) }, el('span', { class: 'n' }, String(n)), sev);
}

function badge(kind, text) {
  return el('span', { class: 'chip chip-neutral', title: chipHelp(kind) }, text ?? kind);
}

function dots(confidence) {
  const n = Math.max(0, Math.min(3, confidence || 0));
  if (!n) return null;
  return el('span', { class: 'dots', role: 'img', 'aria-label': `confidence ${n} of 3` }, '●'.repeat(n));
}

function deltaText(score) {
  if (score.z == null && score.excess == null) return 'not scored';
  const ratio = fmtRatio(score.ratio);
  return `${fmtSigned(score.excess)}${ratio ? ` (${ratio})` : ''}`;
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}

/** How long a flag stays listed after the last run that raised it (server setting). */
function flagWindowHours() {
  return app.summary?.flag_window_hours || 48;
}

function displayedSeverities(kinds = COUNT_SEVERITIES) {
  return app.hideDrops ? kinds.filter((kind) => kind !== 'drop') : kinds;
}

function countBadges(counts = {}) {
  const wrap = el('div', { class: 'card-counts' });
  for (const kind of displayedSeverities()) {
    if (counts[kind]) wrap.append(countChip(kind, counts[kind]));
  }
  if (counts.storm) wrap.append(badge('storm', plural(counts.storm, 'storm')));
  if (counts.done) wrap.append(countChip('done', counts.done));
  return wrap;
}

/** "channel excess mostly from crash loops (72 %)" for storm-driven totals, else null. */
function stormNote(total) {
  if (!total?.storm_driven) return null;
  const share = total.storm_share != null ? ` (${Math.round(total.storm_share * 100)} %)` : '';
  return `channel excess mostly from crash loops${share}`;
}

function installsTitle(score) {
  if (score.installs == null) return 'installs unknown';
  let t = `${fmtInt(score.installs)} installs`;
  if (score.expected_installs != null) t += ` vs ${score.expected_installs.toLocaleString('en-US', { maximumFractionDigits: 1 })} expected`;
  if (score.z_installs != null) t += ` (z ${fmtZ(score.z_installs)})`;
  if (score.installs_ratio != null) t += ` · ${score.installs_ratio} crashes per install`;
  return t;
}

function midTruncate(s, max = 70) {
  if (s.length <= max) return s;
  const head = Math.ceil((max - 1) * 0.6);
  return `${s.slice(0, head)}…${s.slice(s.length - (max - 1 - head))}`;
}

/** When the row's flag was first raised: today's own, or the carried-over day's. */
function sinceOf(row) {
  return row.flag?.since || row.since || null;
}

function sinceText(row) {
  const days = row.flagged_days || 0;
  if (days >= 2) return `${days + 1} days`;
  if (days === 1) return 'yesterday';
  const since = sinceOf(row);
  if (!since) return '—';
  const ms = new Date(since).getTime();
  const dayStart = todayMs();
  if (ms >= dayStart) return `${fmtTime(since).replace(' UTC', '')} today`;
  if (ms >= dayStart - DAY_MS) return `yesterday ${fmtTime(since).replace(' UTC', '')}`;
  return `${Math.round((dayStart - ms) / DAY_MS) + 1} days`;
}

function sinceValue(row) {
  const since = sinceOf(row);
  const ms = since ? new Date(since).getTime() : todayMs() + DAY_MS;
  return (row.flagged_days || 0) * DAY_MS + (todayMs() + DAY_MS - ms);
}

const ALL = { all: true };
const ALL_KEY = 'all';

function isAll(sel) {
  return !!sel && sel.all === true;
}

function channelKey(c) {
  return isAll(c) ? ALL_KEY : `${c.product}/${c.channel}`;
}

/** `#all`, `#Product/channel` or `#Product/channel/<encoded signature>`
 * (the signature part is percent-encoded, so its own slashes are safe). */
function parseHash() {
  const raw = location.hash.slice(1);
  if (!raw) return null;
  if (decodeURIComponent(raw) === ALL_KEY) return ALL;
  const parts = raw.split('/');
  if (parts.length < 2 || !parts[0] || !parts[1]) return null;
  const res = { product: decodeURIComponent(parts[0]), channel: decodeURIComponent(parts[1]) };
  if (parts.length > 2 && parts.slice(2).join('/')) res.signature = decodeURIComponent(parts.slice(2).join('/'));
  return res;
}

function channelHash(product, channel, signature = null) {
  let hash = `#${encodeURIComponent(product)}/${encodeURIComponent(channel)}`;
  if (signature) hash += `/${encodeURIComponent(signature)}`;
  return hash;
}

function updateHash(signature = null) {
  if (!app.selected) return;
  if (!isAll(app.selected) && !signature) {
    // keep a signature deep link in the address bar while its channel is shown
    const current = parseHash();
    if (current && !isAll(current) && current.signature && channelKey(current) === channelKey(app.selected)) return;
  }
  const hash = isAll(app.selected) ? `#${ALL_KEY}` : channelHash(app.selected.product, app.selected.channel, signature);
  if (location.hash !== hash) history.replaceState(null, '', hash);
}

/** The view to open on load: the hash if valid, else the cross-channel report. */
function defaultChannel() {
  const channels = app.summary?.channels || [];
  if (!channels.length) return null;
  const fromHash = parseHash();
  if (isAll(fromHash)) return ALL;
  if (fromHash && channels.some((c) => c.product === fromHash.product && c.channel === fromHash.channel)) return fromHash; // may carry a signature
  return ALL;
}

/** The toolbar is pinned: showing a view from its start means the page top. */
function scrollToContent() {
  if (window.scrollY > 0) window.scrollTo({ top: 0, behavior: 'smooth' });
}

/** Show either the cross-channel report or the channel detail. */
function showView() {
  const hasData = (app.summary?.channels || []).length > 0;
  const all = isAll(app.selected);
  $('flagged').hidden = !hasData || !all;
  $('detail').hidden = !hasData || all || !app.channel;
}

function selectAll() {
  app.selected = ALL;
  app.channel = null;
  clearExpanded();
  updateHash();
  highlightCard();
  if (app.summary) renderAlerts(app.summary);
  showView();
}

// ---------------------------------------------------------------- tab status
// The browser tab shows the overall health: the Mozilla support favicon with
// a small status swatch at the bottom right, and a short title prefix.
const TAB_COLORS = { major: '#d03b3b', spike: '#ec835a', watch: '#fab219', drop: '#2a78d6', ok: '#0ca30c', stale: '#898781' };
const baseIcon = new Image();
baseIcon.src = new URL('favicon.png', import.meta.url).href;
let lastTabColor = null;
baseIcon.addEventListener('load', () => { if (lastTabColor) drawFavicon(lastTabColor); });

function overallHealth(s) {
  const rows = openAlerts(s);
  const counts = {};
  for (const r of rows) { const sev = sevOf(r); if (sev !== 'ok') counts[sev] = (counts[sev] || 0) + 1; }
  const worst = ALERT_SEVERITIES.find((kind) => counts[kind]) || 'ok';
  const stale = s.data_health && s.data_health.status !== 'ok' && s.data_health.status !== 'backfilling';
  return { worst, counts, stale };
}

function drawFavicon(color) {
  const size = 32;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  if (baseIcon.complete && baseIcon.naturalWidth) {
    // the logo rotated a quarter turn counter-clockwise
    ctx.save();
    ctx.translate(size / 2, size / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.drawImage(baseIcon, -size / 2, -size / 2, size, size);
    ctx.restore();
  }
  // small status disc, top right
  const r = 6;
  const cx = size - r - 1;
  const cy = r + 1;
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
  const link = $('favicon');
  if (link) link.href = canvas.toDataURL('image/png');
}

function renderTabStatus(s) {
  const h = overallHealth(s);
  const label = h.stale ? 'stale' : h.worst === 'ok' ? 'OK'
    : ALERT_SEVERITIES.filter((kind) => h.counts[kind]).slice(0, 2).map((kind) => `${h.counts[kind]} ${kind}`).join(' · ');
  document.title = `${label} – Crash spikes`;
  lastTabColor = h.stale ? TAB_COLORS.stale : TAB_COLORS[h.worst];
  drawFavicon(lastTabColor);
}

// ---------------------------------------------------------------- header
function renderSummary() {
  const s = app.summary;
  app.hideDrops = s.data_health?.status === 'stale_upstream';
  for (const label of document.querySelectorAll('#sig-filters .chip-toggle')) {
    const input = label.querySelector('input');
    if (input) label.title = chipHelp(input.value);
  }
  renderTabStatus(s);
  renderFreshness(s);
  renderBanner(s);
  renderCards(s);
  if (isAll(app.selected)) renderAlerts(s);
}

function renderFreshness(s) {
  const p = $('freshness');
  p.textContent = '';
  if (!s.as_of) { p.textContent = 'No data yet'; return; }
  const run = s.last_run || {};
  p.append(`Data as of ${fmtTime(s.as_of)} (${fmtAgo(s.as_of)}) · last run `);
  const status = (run.status || 'unknown').toUpperCase();
  p.append(run.status === 'ok' ? status : el('span', { class: 'bad', title: run.message || '' }, status));
  if (run.queries != null) p.append(` · ${run.queries} ${run.queries === 1 ? 'query' : 'queries'}`);
  if (run.failures) p.append(`, ${run.failures} failed`);
}

function renderBanner(s) {
  const banner = $('banner');
  const health = s.data_health || { status: 'ok' };
  banner.textContent = '';
  banner.className = 'banner';
  if (health.status === 'ok') return;
  let text = '';
  if (health.status === 'stale_upstream') {
    text = `Socorro processing appears delayed${health.since ? ` since ${fmtTime(health.since)}` : ''} — drops are hidden`;
  } else if (health.status === 'backfilling') {
    const days = Math.max(0, ...(s.channels || []).map((c) => c.history_days || 0));
    text = `Backfilling history: ${days} ${days === 1 ? 'day' : 'days'} loaded`;
    banner.classList.add('is-info');
  } else if (health.status === 'stale_local') {
    const finished = s.last_run?.status === 'ok' ? s.last_run.finished : null;
    text = `Data is stale: last successful run ${finished ? fmtAgo(finished, s.now) : (s.as_of ? fmtAgo(s.as_of, s.now) : 'unknown')}`;
    banner.classList.add('is-critical');
  } else {
    text = `Data health: ${health.status}`;
  }
  banner.append(el('strong', {}, text));
  if (health.detail) banner.append(el('span', {}, health.detail));
  if (s.last_run?.status !== 'ok' && s.last_run?.message) banner.append(el('span', {}, `Last run: ${s.last_run.message}`));
}

// ---------------------------------------------------------------- overview cards
function renderCards(s) {
  const wrap = $('channel-cards');
  const channels = s.channels || [];
  const focus = focusedKey(wrap);
  $('empty-state').hidden = channels.length > 0;
  wrap.textContent = '';
  if (channels.length) {
    const all = el('div', { class: 'card-group card-group-all' }, el('div', { class: 'card-group-title', 'aria-hidden': 'true' }, '\u00a0'));
    all.append(el('div', { class: 'card-row' }, allCard(s)));
    wrap.append(all);

    const groups = new Map();
    for (const channel of channels) {
      if (!groups.has(channel.product)) groups.set(channel.product, []);
      groups.get(channel.product).push(channel);
    }
    for (const [product, productChannels] of groups) {
      const group = el('div', { class: 'card-group', role: 'group', 'aria-label': product, style: `--n:${productChannels.length}` });
      group.append(el('div', { class: 'card-group-title', 'aria-hidden': 'true' }, product));
      const row = el('div', { class: 'card-row' });
      for (const channel of productChannels) row.append(channelCard(channel));
      group.append(row);
      wrap.append(group);
    }
  }
  highlightCard();
  restoreFocus(wrap, focus);
  showView();
}

/** Cross-channel card: what is flagged anywhere right now. */
function allCard(s) {
  const rows = openAlerts(s); // done ones only count as done
  const worst = rows.map(sevOf).filter((sev) => sev in SEV_RANK && sev !== 'ok')
    .sort((a, b) => SEV_RANK[a] - SEV_RANK[b])[0] || 'ok';
  const card = el('button', { type: 'button', class: 'card card-all', 'data-key': ALL_KEY, 'data-focus': `card:${ALL_KEY}`, 'aria-pressed': 'false' });
  card.append(el('div', { class: 'card-head' },
    el('span', { class: 'card-title' }, 'All channels'),
    chip(worst)));
  card.append(el('div', { class: 'tile-label' }, `Flagged, last ${flagWindowHours()} h`));
  const nchan = new Set(rows.map((r) => `${r.product}/${r.channel}`)).size;
  card.append(el('div', { class: 'card-value' }, fmtInt(rows.length),
    el('span', { class: 'vs' }, rows.length ? `flagged in ${plural(nchan, 'channel')}` : 'nothing flagged')));
  const totals = {};
  for (const c of s.channels || []) {
    for (const kind of [...COUNT_KINDS, 'done']) totals[kind] = (totals[kind] || 0) + (c.counts?.[kind] || 0);
  }
  card.append(countBadges(totals));
  return card;
}

function channelCard(c) {
  const t = c.total || {};
  const sev = sevOf(t);
  const key = channelKey(c);
  const card = el('button', { type: 'button', class: 'card', 'data-key': key, 'data-focus': `card:${key}`, 'aria-pressed': 'false' });
  // the product is the group's title; keep it in the button's name
  card.append(el('div', { class: 'card-head' },
    el('span', { class: 'card-title' }, el('span', { class: 'visually-hidden' }, `${c.product} `), c.channel),
    chip(sev)));
  card.append(el('div', { class: 'tile-label' }, 'Today so far'));
  card.append(el('div', { class: 'card-value' }, fmtInt(t.observed), el('span', { class: 'vs' }, `vs ${fmtInt(t.expected)} expected`)));
  card.append(el('div', { class: 'card-delta' }, deltaText(t), dots(t.confidence)));
  const note = stormNote(t);
  if (note) card.append(el('div', { class: 'card-note' }, note));
  card.append(countBadges(c.counts));
  // product logo, bottom right (decorative: the product is in the name)
  const logo = logoFor(c.product, c.channel);
  if (logo) card.append(el('img', { class: 'card-logo', src: new URL(logo, import.meta.url).href, alt: '', 'aria-hidden': 'true', width: 20, height: 20 }));
  return card;
}

function highlightCard() {
  for (const card of document.querySelectorAll('.card')) {
    const on = !!app.selected && card.dataset.key === channelKey(app.selected);
    card.classList.toggle('is-selected', on);
    card.setAttribute('aria-pressed', String(on));
  }
}

async function selectChannel(product, channel, signature = null) {
  if (app.selected && !isAll(app.selected) && app.selected.product === product && app.selected.channel === channel && app.channel) {
    if (signature) focusSignature(signature);
    else scrollToContent();
    return;
  }
  app.pendingFocus = signature; // consumed by the render of the loaded channel
  try {
    await loadChannel(product, channel);
    showError('');
    if (!signature) scrollToContent();
  } catch (e) {
    app.pendingFocus = null;
    showError(`Could not load ${product} ${channel} (${e.message})`);
  }
}

// ---------------------------------------------------------------- flagged now
function renderAlerts(s) {
  const rows = visibleAlerts(s);
  const sub = document.querySelector('#flagged-title .sub');
  if (sub) sub.textContent = `flagged in the last ${flagWindowHours()} h`;
  const open = rows.filter((r) => !r.done);
  const done = rows.length - open.length;
  $('flagged-meta').textContent = (open.length ? `${open.length} flagged ${open.length === 1 ? 'signature' : 'signatures'} across ${plural(new Set(open.map(channelKey)).size, 'channel')}` : `Nothing flagged in the last ${flagWindowHours()} h`)
    + (done ? ` · ${done} done` : '');
  const wrap = $('alerts-table');
  const focus = focusedKey(wrap);
  wrap.textContent = '';
  if (!rows.length) return;
  // done rows are listed last, with their badge: handled, but visible to the team
  const sorted = rows.slice().sort((a, b) => (a.done ? 1 : 0) - (b.done ? 1 : 0) || rowRank(a) - rowRank(b) || Math.abs(b.excess || 0) - Math.abs(a.excess || 0));
  wrap.append(buildTable(sorted, { withChannel: true, sortable: false, onRow: (row) => selectChannel(row.product, row.channel, row.signature) }));
  restoreFocus(wrap, focus);
}

// ---------------------------------------------------------------- channel detail
/** The "All" range is only offered once more than 180 days of history exist
 * (Socorro keeps 6 months; the dashboard accumulates its own history). */
function renderRangeOptions(ch) {
  const history = ch.model?.history_days || ch.history_days || 0;
  const all = $('range-all');
  all.hidden = history <= 180;
  if (all.hidden && app.days > 180) {
    app.days = 180;
    const radio = document.querySelector('#range-controls input[name="days"][value="180"]');
    if (radio) radio.checked = true;
  }
}

function renderDetail() {
  const ch = app.channel;
  const section = $('detail');
  section.hidden = false;
  renderRangeOptions(ch);
  $('detail-title').textContent = `${ch.product} ${ch.channel}`;
  $('detail-meta').textContent = `${fmtDateLong(parseDay(ch.day))} · data as of ${fmtTime(ch.as_of)}`;
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

function tile(label, valueNodes, subNodes, { chipNode, note, counts } = {}) {
  const t = el('div', { class: 'tile' });
  t.append(el('div', { class: 'tile-head' }, el('span', { class: 'tile-label' }, label), chipNode));
  t.append(el('div', { class: 'tile-value' }, ...valueNodes));
  if (subNodes) t.append(el('div', { class: 'tile-sub' }, ...subNodes));
  if (note) t.append(el('div', { class: 'tile-note' }, note));
  if (counts) t.append(counts);
  return t;
}

function renderTiles(ch) {
  const wrap = $('tiles');
  wrap.textContent = '';
  const t = ch.total || {};
  const sev = sevOf(t);
  // Today so far
  wrap.append(tile('Today so far',
    [fmtCompact(t.observed), el('span', { class: 'vs' }, `vs ${fmtCompact(t.expected)} expected`)],
    t.z == null ? ['too early to score'] : [deltaText(t), dots(t.confidence)],
    { chipNode: chip(sev), note: stormNote(t) || (t.since && sev !== 'ok' ? `flagged since ${fmtTime(t.since)}` : null) }));
  // Projected
  if (t.projected != null) {
    wrap.append(tile('Projected today',
      [fmtCompact(t.projected), el('span', { class: 'vs' }, `vs ${fmtCompact(t.expected_day)} expected`)],
      [`${fmtCompact(t.projected_lo)}–${fmtCompact(t.projected_hi)} likely · ${Math.round((t.elapsed_fraction || 0) * 100)} % of the day's crashes usually in by now`]));
  } else {
    wrap.append(tile('Projected today', ['—'],
      [`Too early to project (${Math.round((t.elapsed_fraction || 0) * 100)} % of the day's crashes are usually in by now)`]));
  }
  // Yesterday
  const y = ch.yesterday;
  if (y) {
    const finalNote = y.final == null ? null : y.final ? '(final)' : '(still updating)';
    const ysev = sevOf(y);
    wrap.append(tile('Yesterday',
      [fmtCompact(y.observed), el('span', { class: 'vs' }, `vs ${fmtCompact(y.expected)} expected`)],
      [deltaText(y), dots(y.confidence), finalNote ? ` ${finalNote}` : null],
      { chipNode: ysev !== 'ok' ? chip(ysev) : null }));
  } else {
    wrap.append(tile('Yesterday', ['—'], ['No data for yesterday yet']));
  }
  // Flagged
  const c = ch.counts || {};
  const flagged = displayedSeverities(ALERT_SEVERITIES).reduce((n, kind) => n + (c[kind] || 0), 0);
  wrap.append(tile('Flagged', [String(flagged), el('span', { class: 'vs' }, `of ${fmtInt(c.scored)} scored`)],
    [[c.new ? `${c.new} new` : null, c.storm ? plural(c.storm, 'storm') : null, c.noise ? `${c.noise} noise` : null, c.done ? `${c.done} done` : null].filter(Boolean).join(' · ') || 'nothing unusual'],
    { counts: countBadges(c) }));
}

function renderDrivers(ch) {
  const p = $('drivers');
  const focus = focusedKey(p);
  p.textContent = '';
  const drivers = (ch.total?.drivers || []).filter((d) => d.share != null);
  const note = stormNote(ch.total);
  p.hidden = !drivers.length && !note;
  if (p.hidden) return;
  if (note) p.append(el('span', { class: 'storm-note' }, note.charAt(0).toUpperCase() + note.slice(1)), drivers.length ? ' · ' : '');
  if (!drivers.length) return;
  p.append('Driven by ');
  drivers.forEach((d, i) => {
    if (i) p.append(', ');
    const link = el('a', { href: '#', title: d.signature, 'data-sig': d.signature, 'data-focus': `driver:${d.signature}` }, `${midTruncate(d.signature, 48)} (${Math.round(d.share * 100)} %)`);
    link.addEventListener('click', (e) => { e.preventDefault(); focusSignature(d.signature); });
    p.append(link);
    if (d.storm) p.append(' ', badge('crash-loop'), d.installs != null ? ` ${plural(d.installs, 'install')}` : '');
    if (d.noise) p.append(' ', badge('noise'));
  });
  restoreFocus(p, focus);
}

function productOf(data) {
  return data.product || data.row?.product || app.channel?.product;
}

function dailySpec(data, extra = {}) {
  const daily = data.daily || { start: [] };
  return { ...daily, dates: daily.start || [], releases: data.releases || app.channel?.releases || [], events: eventsFor(productOf(data)), height: CHART_HEIGHT, ...extra };
}

function hourlySpec(data, extra = {}) {
  // the day lets the chart translate UTC hour buckets into local time
  return { ...(data.hourly || { hours: [] }), day: data.day || data.row?.day || app.channel?.day, events: eventsFor(productOf(data)), height: CHART_HEIGHT, ...extra };
}

/** Every "Today by hour" title shows the clock in use (UTC or the local zone). */
function renderZoneLabels() {
  const label = zoneLabel();
  for (const n of document.querySelectorAll('.tz-label')) n.textContent = label;
}

function renderCharts(ch) {
  $('daily-sub').textContent = `${ch.daily?.start?.length || 0} ${ch.daily?.granularity === 'week' ? 'weeks' : 'days'}`;
  const names = { hourly: { ariaLabel: `Crashes per hour today, ${ch.product} ${ch.channel}` },
    daily: { ariaLabel: `Daily crashes, ${ch.product} ${ch.channel}` } };
  if (!app.charts.intraday) {
    app.charts.intraday = barChart($('intraday-chart'), hourlySpec(ch, names.hourly));
    app.charts.daily = lineChart($('daily-chart'), dailySpec(ch, names.daily));
  } else {
    app.charts.intraday.update(hourlySpec(ch, names.hourly));
    app.charts.daily.update(dailySpec(ch, names.daily));
  }
}

// ---------------------------------------------------------------- model explanation
/** 1-based day of the cycle: explicit when the API gives it, else an unambiguous factor match. */
function cycleDay(model) {
  const explicit = model.today_factors?.cycle_day ?? model.cycle_day;
  if (explicit != null) return explicit;
  const f = model.today_factors?.cycle;
  const arr = model.factors?.cycle || [];
  if (f == null || !arr.length) return null;
  const hits = arr.map((v, i) => (Math.abs(v - f) < 1e-6 ? i + 1 : null)).filter((v) => v != null);
  return hits.length === 1 ? hits[0] : null;
}

function modelSummaryText(model, day) {
  if (!model) return '';
  const parts = [];
  const wd = WDAYS[new Date(parseDay(day)).getUTCDay()];
  const tf = model.today_factors || {};
  const comp = model.components || {};
  parts.push(`Today: ${wd}${tf.weekly != null ? ` ×${tf.weekly.toFixed(2)}` : ''}`);
  if (comp.cycle?.active && tf.cycle != null) {
    const d = cycleDay(model);
    parts.push(`cycle${d ? ` day ${d}/${model.factors.cycle.length}` : ''} ×${tf.cycle.toFixed(2)}`);
  } else if (comp.cycle) parts.push(`cycle n/a (${comp.cycle.cycles} / ${comp.cycle.min_cycles} cycles)`);
  if (comp.yearly) parts.push(comp.yearly.active ? `yearly${tf.yearly != null ? ` ×${tf.yearly.toFixed(2)}` : ' active'}` : `yearly n/a (${comp.yearly.cycles} / ${comp.yearly.min_cycles} cycles)`);
  parts.push(`level ${fmtCompact(model.level)}`);
  parts.push(`dispersion ${model.dispersion != null ? model.dispersion.toFixed(1) : '—'}`);
  return parts.join(' · ');
}

function componentLine(name, c, borrowed) {
  const label = { weekly: 'weekly seasonality', cycle: 'release cycle (28 days)', yearly: 'yearly seasonality' }[name] || name;
  if (!c) return `${label}: unknown`;
  const src = borrowed?.includes(name) ? ', borrowed from the channel' : '';
  return c.active ? `${label}: active (${c.cycles} cycles${src})` : `${label}: not enough history (${c.cycles} / ${c.min_cycles} cycles)`;
}

function renderModel(model, ch) {
  $('model-summary').textContent = modelSummaryText(model, ch.day);
  const body = $('model-body');
  body.textContent = '';
  if (!model) return;
  const todayIdx = (new Date(parseDay(ch.day)).getUTCDay() + 6) % 7;
  const weekly = el('div', { class: 'model-block' }, el('h4', {}, 'Weekday factors'));
  const wMini = el('div');
  weekly.append(wMini);
  body.append(weekly);
  if (model.factors?.weekly) miniFactors(wMini, { values: model.factors.weekly, labels: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'], highlight: todayIdx, kind: 'bar' });
  const cycle = el('div', { class: 'model-block' }, el('h4', {}, 'Release-cycle factors (day of the 28-day cycle)'));
  const cMini = el('div');
  cycle.append(cMini);
  body.append(cycle);
  if (model.factors?.cycle) miniFactors(cMini, { values: model.factors.cycle, highlight: (cycleDay(model) || 0) - 1, kind: 'line', ticks: [1, 8, 15, 22, 28] });
  const comps = el('ul');
  for (const k of ['weekly', 'cycle', 'yearly']) comps.append(el('li', {}, componentLine(k, model.components?.[k], model.borrowed)));
  const disp = model.dispersion != null ? model.dispersion.toFixed(1) : '—';
  body.append(el('div', { class: 'model-block' }, el('h4', {}, 'Components and band'), comps,
    el('p', {}, `Expected = level (${fmtInt(model.level)}, median of the last 14 de-seasonalised days) × weekday factor × cycle factor${model.components?.yearly?.active ? ' × yearly factor' : ''}. `
      + `Residuals are measured on the Anscombe scale, 2·(√(observed + ⅜) − √(expected + ⅜)), and divided by the dispersion ${disp}; `
      + `the grey bands are ±3 (watch) and ±5 (spike) dispersions around the expectation. History: ${model.history_days ?? '—'} days.`)));
}

// ---------------------------------------------------------------- signature table
function readFilters() {
  const data = new FormData($('sig-filters'));
  return {
    severities: new Set(data.getAll('sev')),
    text: String(data.get('query') || '').trim().toLowerCase(),
    hideNoise: data.has('hide-noise'),
    minCrashes: Math.max(0, Number(data.get('min-crashes')) || 0),
    showStorms: data.has('show-storms'),
    showUnflagged: data.has('show-unflagged'),
  };
}

function rowCategory(row) {
  if (row.done) return 'done';
  if (sevOf(row) !== 'ok' || isNew(row)) return 'flagged';
  return row.storm ? 'storm' : 'unflagged';
}

function matchesSeverityFilter(row, severities) {
  const severity = sevOf(row);
  return (severity !== 'ok' && severities.has(severity)) || (isNew(row) && severities.has('new'));
}

function visibleRows() {
  const rows = app.channel?.signatures || [];
  const filters = readFilters();
  const shown = [];
  let unflaggedCount = 0;
  let stormCount = 0;
  let doneCount = 0;
  for (const row of rows) {
    if ((filters.hideNoise && row.noise) || (row.observed || 0) < filters.minCrashes ||
        (filters.text && !row.signature.toLowerCase().includes(filters.text))) continue;
    const category = rowCategory(row);
    if (category === 'storm') stormCount += 1;
    else if (category === 'unflagged') unflaggedCount += 1;
    else if (category === 'done') doneCount += 1;
    if ((category === 'flagged' && matchesSeverityFilter(row, filters.severities)) ||
        (category === 'done' && filters.severities.has('done')) ||
        (category === 'storm' && filters.showStorms) ||
        (category === 'unflagged' && filters.showUnflagged)) shown.push(row);
  }
  return { shown, unflaggedCount, stormCount, doneCount, showDone: filters.severities.has('done'), total: rows.length };
}

const SORTERS = {
  severity: (r) => rowRank(r) * 1e12 - Math.abs(r.excess || 0),
  channel: (r) => channelKey(r),
  signature: (r) => r.signature.toLowerCase(),
  observed: (r) => r.observed,
  expected: (r) => r.expected,
  excess: (r) => r.excess,
  recent: (r) => r.recent?.excess ?? null,
  installs: (r) => r.installs,
  since: (r) => (sevOf(r) === 'ok' && !isNew(r) ? null : sinceValue(r)),
  trend: (r) => r.level_change_28,
  bug: (r) => r.bugs?.open ?? r.bugs?.closed ?? null,
};
const DEFAULT_DIR = { severity: 'asc', channel: 'asc', signature: 'asc', since: 'desc' };

function sortRows(rows) {
  const { key, dir } = app.sort;
  const get = SORTERS[key] || SORTERS.severity;
  const sign = dir === 'asc' ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const va = get(a);
    const vb = get(b);
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === 'string') return sign * va.localeCompare(vb);
    return sign * (va - vb);
  });
}

function renderSignatures() {
  const { shown, unflaggedCount, stormCount, doneCount, showDone, total } = visibleRows();
  $('unflagged-count').textContent = String(unflaggedCount);
  $('storm-count').textContent = plural(stormCount, 'storm');
  const hiddenDone = doneCount && !showDone ? ` · ${doneCount} done hidden` : '';
  const metaText = total
    ? `${shown.length} of ${total} scored signatures shown${shown.length ? '' : ' — no signature matches the current filters'}${hiddenDone}`
    : 'No scored signatures yet';
  const meta = $('signatures-meta');
  if (meta.textContent !== metaText) meta.textContent = metaText; // role=status: announce changes only
  const wrap = $('signature-table');
  const focus = focusedKey(wrap); // the table is rebuilt: keep the keyboard user's place
  wrap.textContent = '';
  const open = new Set(app.expanded.keys());
  if (!shown.length) {
    wrap.append(el('div', { class: 'table-empty' }, total ? 'No signature matches the current filters' : 'No scored signatures yet'));
    return;
  }
  wrap.append(buildTable(sortRows(shown), { withChannel: false, sortable: true, onRow: toggleRow }));
  for (const sig of open) {
    const tr = wrap.querySelector(`tr.row[data-sig="${cssEscape(sig)}"]`);
    if (tr) reattachExpanded(tr, sig);
    else app.expanded.delete(sig);
  }
  restoreFocus(wrap, focus);
}

function cssEscape(s) {
  return typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(s) : s.replace(/["\\]/g, '\\$&');
}

function columns(withChannel) {
  const hours = app.channel?.signatures?.find((r) => r.recent)?.recent?.hours || app.summary?.alerts?.find((r) => r.recent)?.recent?.hours || 3;
  return [
    { key: 'severity', label: 'Severity' },
    ...(withChannel ? [{ key: 'channel', label: 'Channel' }] : []),
    { key: 'signature', label: 'Signature' },
    { key: 'observed', label: 'Today so far', num: true },
    { key: 'expected', label: 'Expected', num: true },
    { key: 'excess', label: 'Delta', num: true },
    { key: 'recent', label: `Last ${hours}h`, num: true, hint: 'observed / expected', title: 'observed / expected over the last hours' },
    { key: 'installs', label: 'Installs', num: true },
    { key: 'since', label: 'Since' },
    { key: 'trend', label: '28 days' },
    { key: 'bug', label: 'Bug' },
  ];
}

function buildTable(rows, { withChannel, sortable, onRow }) {
  const cols = columns(withChannel);
  const table = el('table', { class: 'rows' });
  const thead = el('thead');
  const hr = el('tr');
  for (const c of cols) {
    const th = el('th', { scope: 'col', class: c.num ? 'num' : null, title: c.title || null });
    if (sortable) {
      const active = app.sort.key === c.key;
      if (active) th.setAttribute('aria-sort', app.sort.dir === 'asc' ? 'ascending' : 'descending');
      const btn = el('button', { type: 'button', 'data-focus': `sort:${c.key}` }, c.label,
        c.hint ? el('span', { class: 'visually-hidden' }, `, ${c.hint}`) : '',
        el('span', { class: 'sort-ind', 'aria-hidden': 'true' }, active ? (app.sort.dir === 'asc' ? '▲' : '▼') : ''));
      btn.addEventListener('click', () => {
        if (app.sort.key === c.key) app.sort.dir = app.sort.dir === 'asc' ? 'desc' : 'asc';
        else app.sort = { key: c.key, dir: DEFAULT_DIR[c.key] || 'desc' };
        renderSignatures();
      });
      th.append(btn);
    } else {
      th.append(c.label);
      if (c.hint) th.append(el('span', { class: 'visually-hidden' }, `, ${c.hint}`));
    }
    hr.append(th);
  }
  thead.append(hr);
  const tbody = el('tbody');
  for (const row of rows) tbody.append(buildRow(row, withChannel, onRow));
  table.append(thead, tbody);
  return table;
}

function buildRow(row, withChannel, onRow) {
  const sev = sevOf(row);
  // the row is clickable; keyboard users get a real button (the expander,
  // or the channel chip in the cross-channel table)
  const tr = el('tr', { class: 'row', tabindex: -1, 'data-sig': row.signature });
  // severity + badges
  const badges = el('div', { class: 'badge-set' });
  const fresh = isNew(row);
  if (row.done) badges.append(el('span', { class: 'chip chip-done', title: doneTitle(row) }, 'done'));
  if (sev !== 'ok' || !fresh) badges.append(chip(sev));
  if (fresh) badges.append(chip('new'));
  // a flag carried over from a previous day (scores are per UTC day; the flag
  // window keeps yesterday's spikes listed): say so, with that day's numbers
  if (row.flag && flagAge(row) > 0 && (sev !== 'ok' || fresh)) badges.append(el('span', { class: 'flag-when', title: flagTitle(row) }, flagWhen(row)));
  if (row.storm) badges.append(badge('storm'));
  if (row.noise) badges.append(badge('noise'));
  // signed-in users mark a flagged spike as handled (POST /api/done)
  if (!withChannel && signedIn() && (row.done || row.flag)) {
    const btn = el('button', { type: 'button', class: 'chart-btn done-btn', 'data-focus': `done:${row.signature}`,
      title: row.done ? 'Remove the done mark' : chipHelp('done') }, row.done ? 'undo' : 'mark done');
    btn.addEventListener('click', (e) => { e.stopPropagation(); btn.disabled = true; markDone(row, !row.done); });
    badges.append(btn);
  }
  tr.append(el('td', {}, badges));
  if (withChannel) {
    const open = el('button', { type: 'button', class: 'chip chip-neutral chip-btn', 'data-focus': `open:${row.signature}`,
      'aria-label': `Open ${row.product} ${row.channel} at this signature` }, `${row.product} ${row.channel}`);
    open.addEventListener('click', (e) => { e.stopPropagation(); onRow(row, tr); });
    tr.append(el('td', {}, open));
  }
  // signature
  const sigCell = el('td', { class: 'sig' });
  if (!withChannel) {
    const expander = el('button', { type: 'button', class: 'row-expander', 'aria-expanded': 'false', 'data-focus': `exp:${row.signature}`,
      'aria-label': `Details of ${midTruncate(row.signature, 60)}` }, '▸');
    expander.addEventListener('click', (e) => { e.stopPropagation(); onRow(row, tr); });
    sigCell.append(expander);
  }
  sigCell.append(el('a', { href: row.socorro_url || '#', title: row.signature, target: '_blank', rel: 'noopener', 'data-focus': `link:${row.signature}` }, midTruncate(row.signature, 70)));
  const copy = el('button', { type: 'button', class: 'copy-btn', 'aria-label': 'Copy signature', title: 'Copy signature', 'data-focus': `copy:${row.signature}` }, '⧉');
  copy.addEventListener('click', async (e) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(row.signature);
      copy.textContent = '✓';
      announce('Signature copied');
      setTimeout(() => { copy.textContent = '⧉'; }, 1200);
    } catch { announce('Could not copy the signature'); }
  });
  sigCell.append(copy);
  if (!withChannel) {
    const permalink = el('a', { class: 'perma-btn', href: channelHash(row.product || app.selected?.product, row.channel || app.selected?.channel, row.signature),
      title: 'Link to this signature', 'aria-label': 'Link to this signature', 'data-focus': `perma:${row.signature}` }, '#');
    permalink.addEventListener('click', (e) => {
      e.stopPropagation();
      e.preventDefault();
      history.replaceState(null, '', permalink.getAttribute('href'));
      focusSignature(row.signature);
      announce('Link to this signature is in the address bar');
    });
    sigCell.append(permalink);
  }
  tr.append(sigCell);
  tr.append(el('td', { class: 'num' }, fmtInt(row.observed)));
  tr.append(el('td', { class: 'num' }, fmtInt(row.expected)));
  const ratio = fmtRatio(row.ratio);
  // scores live in title attributes for mouse users and as hidden text for the rest
  const hidden = (text) => el('span', { class: 'visually-hidden' }, text);
  tr.append(el('td', { class: 'num', title: row.z != null ? `z ${fmtZ(row.z)}` : 'not scored' }, `${fmtSigned(row.excess)}${ratio ? ` ${ratio}` : ''}`, dots(row.confidence),
    row.z != null ? hidden(` (z ${fmtZ(row.z)})`) : ''));
  if (row.recent && row.recent.z != null) {
    tr.append(el('td', { class: 'num', title: `z ${fmtZ(row.recent.z)}${row.recent.ratio != null ? ` · ${fmtRatio(row.recent.ratio)}` : ''}` },
      `${fmtInt(row.recent.observed)} / ${fmtInt(row.recent.expected)}`, hidden(` (z ${fmtZ(row.recent.z)})`)));
  } else if (row.recent) {
    // too few expected crashes to score the window: show the activity, muted
    tr.append(el('td', { class: 'num muted', title: row.recent_reason || 'not scored' }, `${fmtInt(row.recent.observed)} / ${fmtInt(row.recent.expected)}`,
      hidden(` (${row.recent_reason || 'not scored'})`)));
  } else {
    tr.append(el('td', { class: 'num' }, el('span', { class: 'dash', title: row.recent_reason || 'not scorable' }, '—'), hidden(row.recent_reason || 'not scorable')));
  }
  const inst = el('td', { class: 'num', title: installsTitle(row) }, fmtInt(row.installs), el('span', { class: 'visually-hidden' }, ` (${installsTitle(row)})`));
  if (row.storm) inst.append(' ', badge('crash-loop'));
  tr.append(inst);
  tr.append(el('td', { class: 'since' }, sev === 'ok' && !row.is_new ? '—' : sinceText(row)));
  const sparkCell = el('td', { class: 'spark' });
  if (row.spark?.dates?.length) sparkline(sparkCell, { ...row.spark, severity: sev, partial: row.partial !== false });
  tr.append(sparkCell);
  tr.append(bugCell(row.bugs));
  tr.addEventListener('click', (e) => {
    if (e.target.closest('a, button, input')) return;
    onRow(row, tr);
  });
  tr.addEventListener('keydown', (e) => {
    if (e.target !== tr) return; // buttons and links inside handle their own keys
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onRow(row, tr); }
  });
  return tr;
}

function bugCell(bugs) {
  const td = el('td', { class: 'num' });
  if (bugs?.open) td.append(el('a', { href: `https://bugzilla.mozilla.org/${bugs.open}`, target: '_blank', rel: 'noopener', title: 'Open bug' }, String(bugs.open), el('span', { class: 'visually-hidden' }, ' (open bug)')));
  else if (bugs?.closed) td.append(el('a', { class: 'bug-closed', href: `https://bugzilla.mozilla.org/${bugs.closed}`, target: '_blank', rel: 'noopener', title: 'Closed bug' }, String(bugs.closed), el('span', { class: 'visually-hidden' }, ' (closed bug)')));
  else td.append(el('span', { class: 'dash' }, '—'));
  return td;
}

// ---------------------------------------------------------------- row expansion
function toggleRow(row, tr) {
  if (app.expanded.has(row.signature)) collapseRow(row.signature);
  else expandRow(row, tr);
}

function collapseRow(sig) {
  const st = app.expanded.get(sig);
  if (!st) return;
  st.tr.classList.remove('is-expanded');
  const btn = st.tr.querySelector('.row-expander');
  if (btn) { btn.setAttribute('aria-expanded', 'false'); btn.textContent = '▸'; }
  st.detail.remove();
  for (const c of Object.values(st.charts)) c.destroy?.();
  app.expanded.delete(sig);
}

function expandRow(row, tr) {
  const detail = el('tr', { class: 'row-detail' });
  const td = el('td', { colspan: String(tr.children.length) });
  detail.append(td);
  tr.after(detail);
  tr.classList.add('is-expanded');
  const btn = tr.querySelector('.row-expander');
  if (btn) { btn.setAttribute('aria-expanded', 'true'); btn.textContent = '▾'; }
  const status = el('p', { class: 'detail-note', role: 'status' });
  const st = { tr, detail, td, charts: {}, row, data: null, statusEl: status };
  app.expanded.set(row.signature, st);
  td.append(status);
  setTimeout(() => { if (!st.panel) status.textContent = 'Loading…'; }, 50); // live region exists before its text
  loadSignature(st);
}

function reattachExpanded(tr, sig) {
  const st = app.expanded.get(sig);
  tr.after(st.detail);
  tr.classList.add('is-expanded');
  const btn = tr.querySelector('.row-expander');
  if (btn) { btn.setAttribute('aria-expanded', 'true'); btn.textContent = '▾'; }
  st.tr = tr;
}

async function loadSignature(st) {
  const { product, channel } = app.selected;
  try {
    const data = await fetchJSON('signature', { product, channel, signature: st.row.signature, days: app.days, granularity: app.granularity });
    // same data as what the panel shows: nothing to redraw
    if (st.panel && data === st.data) return;
    st.data = data;
    renderSignaturePanel(st);
  } catch (e) {
    st.panel = null;
    st.td.textContent = '';
    st.statusEl.className = 'detail-note';
    st.statusEl.textContent = `Could not load this signature (${e.message})`;
    st.td.append(st.statusEl);
  }
}

function signatureNotes(data, r) {
  const notes = [];
  if (!data.hourly) notes.push('No hourly data for this signature (it was below the per-hour top-200 cut).');
  if (r.recent == null || r.recent.z == null) notes.push(`Last hours not scored${r.recent_reason ? `: ${r.recent_reason}` : ''}.`);
  if (r.z == null) notes.push('Today not scored yet (expected so far is too small).');
  return notes.join(' ');
}

function signatureModelText(data, r) {
  return modelSummaryText(data.model, r.day || app.channel.day)
    + (data.model?.borrowed?.length ? ` · ${data.model.borrowed.join(' and ')} factors borrowed from the channel` : '')
    + (data.hourly?.profile_source ? ` · hourly profile: ${data.hourly.profile_source === 'own' ? 'this signature' : 'channel'}` : '');
}

function createSignaturePanel(st) {
  st.td.textContent = '';
  if (st.statusEl) {
    st.statusEl.className = 'visually-hidden';
    st.statusEl.textContent = 'Details loaded';
    st.td.append(st.statusEl);
  }
  const panel = el('div', { class: 'detail-panel' });
  st.modelEl = el('p', { class: 'detail-model full' });
  panel.append(st.modelEl);
  st.noteEl = el('p', { class: 'detail-note full', hidden: true });
  panel.append(st.noteEl);
  const intradayCard = el('div', { class: 'chart-card' }, el('h3', {}, 'Today by hour ', el('span', { class: 'sub tz-label' }, zoneLabel())));
  st.intradayEl = el('div');
  intradayCard.append(st.intradayEl);
  const dailyCard = el('div', { class: 'chart-card' }, el('h3', {}, 'Daily crashes'));
  st.dailyEl = el('div');
  dailyCard.append(st.dailyEl);
  panel.append(intradayCard, dailyCard);
  st.td.append(panel);
  st.panel = panel;
  for (const c of Object.values(st.charts)) c.destroy?.();
  st.charts = {};
}

/** Build the expanded panel once; later refreshes update its text and charts in place
 * (keeps chart zoom, log/table toggles and focus). */
function renderSignaturePanel(st) {
  const { data, row } = st;
  const r = data.row || row;
  const shortSignature = midTruncate(r.signature, 60);
  const intraday = hourlySpec(data, { emptyMessage: 'No hourly data for this signature', ariaLabel: `Crashes per hour today, ${shortSignature}` });
  const daily = dailySpec(data, { ariaLabel: `Daily crashes, ${shortSignature}` });
  const create = !st.panel || !st.panel.isConnected;
  if (create) createSignaturePanel(st);

  st.modelEl.textContent = signatureModelText(data, r);
  const notes = signatureNotes(data, r);
  st.noteEl.textContent = notes;
  st.noteEl.hidden = !notes;
  if (create) {
    st.charts.intraday = barChart(st.intradayEl, intraday);
    st.charts.daily = lineChart(st.dailyEl, daily);
  } else {
    st.charts.intraday.update(intraday);
    st.charts.daily.update(daily);
  }
}

async function refreshExpanded() {
  await Promise.all([...app.expanded.values()].map((st) => loadSignature(st)));
}

function clearExpanded() {
  for (const sig of [...app.expanded.keys()]) collapseRow(sig);
}

function focusSignature(sig) {
  const rows = app.channel?.signatures || [];
  const row = rows.find((r) => r.signature === sig);
  if (!row) { showError(`No scored row today for "${midTruncate(sig, 80)}" in ${app.selected?.product} ${app.selected?.channel}`); return; }
  const filters = readFilters();
  const category = rowCategory(row);
  let changed = false;
  if (row.noise && filters.hideNoise) { $('hide-noise').checked = false; changed = true; }
  if (category === 'storm' && !filters.showStorms) { $('show-storms').checked = true; changed = true; }
  else if (category === 'unflagged' && !filters.showUnflagged) { $('show-unflagged').checked = true; changed = true; }
  else if (category === 'done' && !filters.severities.has('done')) { document.querySelector('#sig-filters input[value="done"]').checked = true; changed = true; }
  if (filters.text && !sig.toLowerCase().includes(filters.text)) { $('sig-search').value = ''; changed = true; }
  if ((row.observed || 0) < filters.minCrashes) { $('min-crashes').value = '0'; changed = true; }
  if (category === 'flagged' && !matchesSeverityFilter(row, filters.severities)) {
    const severity = sevOf(row) === 'ok' ? 'new' : sevOf(row);
    document.querySelector(`#sig-filters input[value="${severity}"]`).checked = true;
    changed = true;
  }
  if (changed || !document.querySelector(`#signature-table tr.row[data-sig="${cssEscape(sig)}"]`)) renderSignatures();
  const tr = document.querySelector(`#signature-table tr.row[data-sig="${cssEscape(sig)}"]`);
  if (!tr) return;
  if (!app.expanded.has(sig)) expandRow(row, tr);
  tr.scrollIntoView({ behavior: 'smooth', block: 'start' }); // scroll-padding keeps it below the toolbar
  tr.classList.add('is-target');
  tr.focus({ preventScroll: true });
  setTimeout(() => tr.classList.remove('is-target'), 2500);
}

// ---------------------------------------------------------------- thresholds help (?)
// Every threshold is learned from each channel's own data by the scheduler
// (calibration.py); this view shows the current values and the method.
function fmtPct(x, digits = 2) {
  return x == null ? '—' : `${(x * 100).toFixed(digits)} %`;
}

function renderHelp() {
  const body = $('help-body');
  body.textContent = '';
  const channels = app.summary?.channels || [];
  const calib = channels.find((c) => c.calibration)?.calibration;
  const rates = calib?.rates || { watch: 0.015, spike: 0.0015, major: 0.00015, drop: 0.0015 };
  const method = el('div', { class: 'help-method' });
  method.append(el('h3', {}, 'How'));
  const ul = el('ul');
  const li = (...nodes) => ul.append(el('li', {}, ...nodes));
  li(el('b', {}, 'Score. '), 'For every signature and day, z = distance between the observed count and the seasonal expectation, on the Anscombe scale, divided by the fitted dispersion (over-dispersion grows with the count, so no ratio gate is needed).');
  li(el('b', {}, 'Severity thresholds. '), `Per channel, the quantiles of its own one-step-ahead z over the last months, pooled over all its scored signatures. The only setting is the false-alarm rate per signature and day each level may have: watch ${fmtPct(rates.watch)}, spike ${fmtPct(rates.spike)}, major ${fmtPct(rates.major)}, drop ${fmtPct(rates.drop)} (lower tail). A noisy channel gets a higher bar by itself.`);
  li(el('b', {}, 'Floor. '), 'The Gaussian value for the same rate: real tails are never lighter. It is used outright when the pooled sample is under 300 series-days; when a level\'s tail holds fewer than 5 points it is extrapolated from an exponential fit of the top of the sample ("extrapolated" below).');
  li(el('b', {}, 'Volume floors. '), `A signature must reach ${fmtPct(calib?.volume_share ?? 0.001, 1)} of its channel's expected daily crashes over the last 24 hours (installs: half of it, at least 2) to be flagged at all.`);
  li(el('b', {}, 'Storm. '), `Crashes per install above the ${fmtPct(calib?.storm_quantile ?? 0.995, 1)} quantile of the channel's own signatures over the last 4 weeks: a badge, never an alert.`);
  li(el('b', {}, 'Installs. '), 'An upward severity also needs the distinct-install count to deviate as much as the crash count; the final severity is the lower of the two.');
  li(el('b', {}, 'Refresh. '), 'Recomputed at every scheduler run (5 min) from the fits cached with the models (refitted every 6 h).');
  method.append(ul);
  body.append(method);

  const table = el('table', { class: 'rows help-table' });
  const head = el('tr');
  for (const h of ['Channel', 'watch', 'spike', 'major', 'drop', 'min crashes', 'min installs', 'storm ≥ crashes/install', 'sample', 'days above watch']) head.append(el('th', { scope: 'col' }, h));
  table.append(el('thead', {}, head));
  const tbody = el('tbody');
  for (const c of channels) {
    const k = c.calibration;
    const tr = el('tr');
    tr.append(el('td', {}, `${c.product} ${c.channel}`));
    for (const level of ['watch', 'spike', 'major', 'drop']) {
      const z = c.thresholds?.[level]?.z;
      const how = k?.method?.[level];
      const g = k?.gaussian?.[level];
      const cell = el('td', { class: 'num', title: how ? `${how}${g != null ? `; Gaussian floor ${g}` : ''}` : '' }, z == null ? '—' : fmtZ(z));
      if (how && how !== 'empirical') cell.append(el('span', { class: 'muted' }, how === 'gaussian' ? ' (Gaussian)' : ' (extrapolated)'));
      tr.append(cell);
    }
    tr.append(el('td', { class: 'num' }, k ? fmtInt(k.min_crashes) : '—'));
    tr.append(el('td', { class: 'num' }, k ? fmtInt(k.min_installs) : '—'));
    tr.append(el('td', { class: 'num' }, k?.storm_ratio != null ? k.storm_ratio.toFixed(1) : '—'));
    tr.append(el('td', { class: 'num', title: 'series-days of one-step-ahead z pooled over the scored signatures' }, k ? `${fmtInt(k.sample)} (${fmtInt(k.series)} sig.)` : '—'));
    tr.append(el('td', { class: 'num', title: 'share of the pooled series-days at or above the watch threshold (includes real spikes)' }, k?.tail?.watch != null ? fmtPct(k.tail.watch) : '—'));
    tbody.append(tr);
  }
  table.append(tbody);
  body.append(el('h3', {}, 'Now'), table);
}

function openHelp() {
  renderHelp();
  const dlg = $('help');
  if (typeof dlg.showModal === 'function') dlg.showModal();
  else dlg.setAttribute('open', '');
}

// ---------------------------------------------------------------- controls
function bindControls() {
  $('help-btn').addEventListener('click', openHelp);
  $('help-close').addEventListener('click', () => $('help').close());
  $('help').addEventListener('click', (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); }); // backdrop
  // Cards are rebuilt on refresh, so their click handler is delegated.
  $('channel-cards').addEventListener('click', (e) => {
    const card = e.target.closest('.card');
    if (!card) return;
    if (card.dataset.key === ALL_KEY) { selectAll(); scrollToContent(); return; }
    const [product, channel] = card.dataset.key.split('/');
    selectChannel(product, channel);
  });
  $('range-controls').addEventListener('change', (e) => {
    if (e.target.name === 'days') app.days = Number(e.target.value);
    if (e.target.name === 'granularity') app.granularity = e.target.value;
    if (app.selected && !isAll(app.selected)) loadChannel(app.selected.product, app.selected.channel).then(() => showError(''), (err) => showError(`Could not reload (${err.message})`));
  });
  const filters = $('sig-filters');
  filters.addEventListener('submit', (e) => e.preventDefault());
  filters.addEventListener('change', (e) => {
    if (e.target.id !== 'sig-search' && e.target.id !== 'min-crashes') renderSignatures();
  });
  let timer = null;
  filters.addEventListener('input', (e) => {
    const delay = e.target.id === 'sig-search' ? 120 : e.target.id === 'min-crashes' ? 200 : null;
    if (delay == null) return;
    clearTimeout(timer);
    timer = setTimeout(renderSignatures, delay);
  });
  window.addEventListener('hashchange', () => {
    if (app.account) renderAccount(); // the sign-in/out "next" follows the view
    const h = parseHash();
    if (!h || !app.summary) return;
    if (isAll(h)) { if (!isAll(app.selected)) selectAll(); return; }
    const same = app.selected && channelKey(h) === channelKey(app.selected);
    if (!same || h.signature) selectChannel(h.product, h.channel, h.signature || null);
  });
  window.addEventListener('dashboard:timezone', renderZoneLabels);
  renderZoneLabels();
  // the sticky toolbar's height offsets anchored scrolls (scroll-margin-top)
  const toolbar = $('toolbar');
  const setToolbarHeight = () => document.documentElement.style.setProperty('--toolbar-h', `${toolbar.offsetHeight}px`);
  new ResizeObserver(setToolbarHeight).observe(toolbar);
  setToolbarHeight();
  const onVisible = () => {
    if (!document.hidden && Date.now() - app.lastFetch > FOCUS_REFRESH_MS) refresh();
  };
  document.addEventListener('visibilitychange', onVisible);
  window.addEventListener('focus', onVisible);
  setInterval(() => refresh(), REFRESH_MS);
}

bindControls();
refresh({ initial: true });
// for debugging and browser tests
window.dashboardRefresh = () => refresh();
window.dashboardState = app;
