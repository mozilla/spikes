// dashboard.js — crash-spikes dashboard: fetch, state, rendering, interactions.
import {
  lineChart, barChart, sparkline, miniFactors, el,
  fmtInt, fmtCompact, fmtSigned, fmtRatio, fmtZ, parseDay,
} from './charts.js';

const API = '/dashboard/api';
const REFRESH_MS = 5 * 60 * 1000;
const FOCUS_REFRESH_MS = 60 * 1000;
const DAY_MS = 86400000;
const SEV_RANK = { major: 0, spike: 1, watch: 2, new: 3, drop: 4, ok: 5 };
const WDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const CHART_HEIGHT = 280;

const app = {
  summary: null,
  channel: null,
  selected: null,
  days: 90,
  granularity: 'day',
  sort: { key: 'severity', dir: 'asc' },
  filters: { sev: new Set(['major', 'spike', 'watch', 'drop', 'new']), text: '', hideNoise: true, minCrashes: 0, showUnflagged: false, showStorms: false },
  expanded: new Map(), // signature -> { tr, panel, charts, data }
  charts: {},
  lastFetch: 0,
  pendingFocus: null,
  hideDrops: false,
  versions: { summary: null, channel: null }, // data_version of what is rendered
};

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- fetching
// Conditional requests: the ETag of every URL is remembered and sent back;
// a 304 (nothing new since the last scheduler run) reuses the cached JSON.
const etags = new Map(); // url -> { etag, data }

async function fetchJSON(endpoint, params = {}) {
  const url = new URL(`${API}/${endpoint}`, location.origin);
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

function setRefreshing(on) {
  // background refreshes update elements in place; nothing is dimmed
  document.body.classList.toggle('is-loading', on);
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
  setRefreshing(true);
  try {
    const summary = await fetchJSON('summary');
    // unchanged data version: nothing to redraw (the poll cost a 304)
    const changed = summary.data_version == null || summary.data_version !== app.versions.summary;
    if (changed || !app.summary) {
      app.summary = summary;
      app.versions.summary = summary.data_version || null;
      renderSummary();
    } else renderFreshness(app.summary); // "N min ago" keeps counting
    const target = app.selected || defaultChannel();
    if (isAll(target)) {
      if (changed || !app.selected) selectAll();
    } else if (target) await loadChannel(target.product, target.channel);
    showError('');
  } catch (e) {
    const asOf = app.summary?.as_of ? ` — showing data from ${fmtTime(app.summary.as_of)}` : '';
    showError(initial && !app.summary ? `Could not load the dashboard (${e.message})` : `Could not refresh (${e.message})${asOf}`);
  } finally {
    setRefreshing(false);
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
  const key = `${req.product}/${req.channel}|${req.days}|${req.granularity}|${data.data_version}`;
  if (!changed && data.data_version != null && key === app.versions.channel) {
    showView();
    return; // same data, same view: leave the DOM alone
  }
  app.versions.channel = key;
  app.channel = data;
  showView(); // final layout before renderDetail() may scroll to a row
  renderDetail();
  await refreshExpanded();
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

/** Severity after the data-health rule (drops are demoted when Socorro lags). */
function sevOf(score) {
  const s = score?.severity || 'ok';
  return s === 'drop' && app.hideDrops ? 'ok' : s;
}

function rowRank(row) {
  const s = sevOf(row);
  if (s === 'ok' && row.is_new) return SEV_RANK.new;
  return SEV_RANK[s] ?? SEV_RANK.ok;
}

/** Plain-language meaning of a chip, with the thresholds the server uses. */
function chipHelp(kind) {
  const t = app.summary?.thresholds || {};
  const above = (r) => (r >= 2 ? `${r}x the expected count` : `${Math.round((r - 1) * 100)} % above it`);
  const rule = (k) => (t[k] ? `at least ${t[k].z} standard deviations above the seasonal expectation and ${above(t[k].ratio)}` : 'well above the seasonal expectation');
  switch (kind) {
    case 'major': return `Major spike: crashes today are ${rule('major')}, and the number of distinct installs rose as much. The strongest alert.`;
    case 'spike': return `Spike: crashes today are ${rule('spike')}, and the number of distinct installs rose as much.`;
    case 'watch': return `Watch: crashes today are ${rule('watch')}. Worth a look, not yet a confirmed spike.`;
    case 'drop': return `Drop: crashes today are ${t.drop ? `at least ${Math.abs(t.drop.z)} standard deviations below the seasonal expectation and ${Math.round(t.drop.ratio * 100)} % of it or less` : 'well below the seasonal expectation'} (a fix landed, or a data problem).`;
    case 'new': return 'New: not seen above the reporting cut on any of the previous 14 days.';
    case 'storm': case 'crash-loop': return 'Storm / crash loop: many crashes from a handful of installs (few machines crashing repeatedly), or 20+ crashes per install. Not a regression across users, so it is never an alert.';
    case 'noise': return 'Noise: a signature listed in the skiplist (processing artefacts such as shutdown kills or empty dumps). Shown, never alerted on.';
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

function sinceText(row) {
  const days = row.flagged_days || 0;
  if (days >= 2) return `${days + 1} days`;
  if (days === 1) return 'yesterday';
  if (!row.since) return '—';
  const ms = new Date(row.since).getTime();
  const dayStart = todayMs();
  if (ms >= dayStart) return `${fmtTime(row.since).replace(' UTC', '')} today`;
  if (ms >= dayStart - DAY_MS) return `yesterday ${fmtTime(row.since).replace(' UTC', '')}`;
  return `${Math.round((dayStart - ms) / DAY_MS) + 1} days`;
}

function sinceValue(row) {
  const ms = row.since ? new Date(row.since).getTime() : todayMs() + DAY_MS;
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

function parseHash() {
  const h = decodeURIComponent(location.hash.slice(1));
  if (h === ALL_KEY) return ALL;
  const m = h.match(/^([^/]+)\/([^/]+)$/);
  return m ? { product: m[1], channel: m[2] } : null;
}

function updateHash() {
  if (!app.selected) return;
  const hash = isAll(app.selected) ? `#${ALL_KEY}`
    : `#${encodeURIComponent(app.selected.product)}/${encodeURIComponent(app.selected.channel)}`;
  if (location.hash !== hash) history.replaceState(null, '', hash);
}

/** The view to open on load: the hash if valid, else the cross-channel report. */
function defaultChannel() {
  const channels = app.summary?.channels || [];
  if (!channels.length) return null;
  const fromHash = parseHash();
  if (isAll(fromHash)) return ALL;
  if (fromHash && channels.some((c) => c.product === fromHash.product && c.channel === fromHash.channel)) return fromHash;
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
baseIcon.src = '/dashboard/static/favicon.png';
let lastTabColor = null;
baseIcon.addEventListener('load', () => { if (lastTabColor) drawFavicon(lastTabColor); });

function overallHealth(s) {
  const rows = (s.alerts || []).filter((r) => !(r.severity === 'drop' && app.hideDrops));
  const counts = {};
  for (const r of rows) if (r.severity !== 'ok') counts[r.severity] = (counts[r.severity] || 0) + 1;
  const worst = ['major', 'spike', 'watch', 'drop'].find((k) => counts[k]) || 'ok';
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
    : ['major', 'spike', 'watch', 'drop'].filter((k) => h.counts[k]).slice(0, 2).map((k) => `${h.counts[k]} ${k}`).join(' · ');
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
  $('empty-state').hidden = channels.length > 0;
  // cards are grouped by product; the structure is rebuilt only when the
  // set of channels changes, otherwise every card is updated in place
  const keys = channels.length ? [ALL_KEY, ...channels.map(channelKey)] : [];
  const current = [...wrap.querySelectorAll('.card')].map((c) => c.dataset.key);
  const fresh = new Map();
  if (channels.length) fresh.set(ALL_KEY, allCard(s));
  for (const c of channels) fresh.set(channelKey(c), channelCard(c));
  const sameShape = keys.length === current.length && keys.every((k, i) => k === current[i]);
  if (sameShape) {
    for (const node of wrap.querySelectorAll('.card')) {
      const card = fresh.get(node.dataset.key);
      node.replaceChildren(...card.childNodes);
      node.className = card.className;
    }
  } else {
    wrap.textContent = '';
    if (channels.length) {
      const all = el('div', { class: 'card-group card-group-all' }, el('div', { class: 'card-group-title', 'aria-hidden': 'true' }, '\u00a0'));
      all.append(el('div', { class: 'card-row' }, fresh.get(ALL_KEY)));
      wrap.append(all);
      const groups = [];
      for (const c of channels) {
        let g = groups.find((x) => x.product === c.product);
        if (!g) { g = { product: c.product, channels: [] }; groups.push(g); }
        g.channels.push(c);
      }
      for (const g of groups) {
        const grp = el('div', { class: 'card-group', role: 'group', 'aria-label': g.product, style: `--n:${g.channels.length}` });
        grp.append(el('div', { class: 'card-group-title', 'aria-hidden': 'true' }, g.product));
        const row = el('div', { class: 'card-row' });
        for (const c of g.channels) row.append(fresh.get(channelKey(c)));
        grp.append(row);
        wrap.append(grp);
      }
    }
  }
  highlightCard();
  showView();
}

/** Cross-channel card: what is flagged anywhere right now. */
function allCard(s) {
  const rows = (s.alerts || []).filter((r) => !(r.severity === 'drop' && app.hideDrops));
  const worst = rows.map((r) => r.severity).filter((sev) => sev in SEV_RANK && sev !== 'ok')
    .sort((a, b) => SEV_RANK[a] - SEV_RANK[b])[0] || 'ok';
  const card = el('button', { type: 'button', class: 'card card-all', 'data-key': ALL_KEY, 'aria-pressed': 'false' });
  card.append(el('div', { class: 'card-head' },
    el('span', { class: 'card-title' }, 'All channels'),
    chip(worst)));
  card.append(el('div', { class: 'tile-label' }, 'Flagged now'));
  const nchan = new Set(rows.map((r) => `${r.product}/${r.channel}`)).size;
  card.append(el('div', { class: 'card-value' }, fmtInt(rows.length),
    el('span', { class: 'vs' }, rows.length ? `flagged in ${plural(nchan, 'channel')}` : 'nothing flagged')));
  const counts = el('div', { class: 'card-counts' });
  const totals = {};
  for (const c of s.channels || []) {
    for (const k of ['major', 'spike', 'watch', 'drop', 'new', 'storm']) totals[k] = (totals[k] || 0) + (c.counts?.[k] || 0);
  }
  for (const k of ['major', 'spike', 'watch', 'drop', 'new']) {
    if (k === 'drop' && app.hideDrops) continue;
    if (totals[k]) counts.append(countChip(k, totals[k]));
  }
  if (totals.storm) counts.append(badge('storm', plural(totals.storm, 'storm')));
  card.append(counts);
  return card;
}

function channelCard(c) {
  const t = c.total || {};
  const sev = sevOf(t);
  const card = el('button', { type: 'button', class: 'card', 'data-key': channelKey(c), 'aria-pressed': 'false' });
  // the product is the group's title; keep it in the button's name
  card.append(el('div', { class: 'card-head' },
    el('span', { class: 'card-title' }, el('span', { class: 'visually-hidden' }, `${c.product} `), c.channel),
    chip(sev)));
  card.append(el('div', { class: 'tile-label' }, 'Today so far'));
  card.append(el('div', { class: 'card-value' }, fmtInt(t.observed), el('span', { class: 'vs' }, `vs ${fmtInt(t.expected)} expected`)));
  card.append(el('div', { class: 'card-delta' }, deltaText(t), dots(t.confidence)));
  const note = stormNote(t);
  if (note) card.append(el('div', { class: 'card-note' }, note));
  const counts = el('div', { class: 'card-counts' });
  for (const k of ['major', 'spike', 'watch', 'drop', 'new']) {
    if (k === 'drop' && app.hideDrops) continue;
    const n = c.counts?.[k] || 0;
    if (n) counts.append(countChip(k, n));
  }
  if (c.counts?.storm) counts.append(badge('storm', plural(c.counts.storm, 'storm')));
  card.append(counts);
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
  const rows = (s.alerts || []).filter((r) => !(r.severity === 'drop' && app.hideDrops));
  $('flagged-meta').textContent = rows.length ? `${rows.length} flagged ${rows.length === 1 ? 'signature' : 'signatures'} across ${new Set(rows.map(channelKey)).size} channels` : 'Nothing flagged right now';
  const wrap = $('alerts-table');
  const focus = focusedKey(wrap);
  wrap.textContent = '';
  if (!rows.length) return;
  const sorted = rows.slice().sort((a, b) => rowRank(a) - rowRank(b) || Math.abs(b.excess || 0) - Math.abs(a.excess || 0));
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
  $('detail-meta').textContent = `${fmtDateLongIso(ch.day)} · data as of ${fmtTime(ch.as_of)}`;
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

function fmtDateLongIso(day) {
  const ms = parseDay(day);
  const d = new Date(ms);
  return `${WDAYS[d.getUTCDay()]} ${d.getUTCDate()} ${['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][d.getUTCMonth()]} ${d.getUTCFullYear()}`;
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
  const keys = ['major', 'spike', 'watch', ...(app.hideDrops ? [] : ['drop'])];
  const flagged = keys.reduce((n, k) => n + (c[k] || 0), 0);
  const counts = el('div', { class: 'card-counts' });
  for (const k of [...keys, 'new']) if (c[k]) counts.append(countChip(k, c[k]));
  if (c.storm) counts.append(badge('storm', plural(c.storm, 'storm')));
  wrap.append(tile('Flagged', [String(flagged), el('span', { class: 'vs' }, `of ${fmtInt(c.scored)} scored`)],
    [[c.new ? `${c.new} new` : null, c.storm ? plural(c.storm, 'storm') : null, c.noise ? `${c.noise} noise` : null].filter(Boolean).join(' · ') || 'nothing unusual'],
    { counts }));
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

function dailySpec(data, extra = {}) {
  const daily = data.daily || { start: [] };
  return { ...daily, dates: daily.start || [], releases: data.releases || app.channel?.releases || [], height: CHART_HEIGHT, ...extra };
}

function hourlySpec(data, extra = {}) {
  return { ...(data.hourly || { hours: [] }), height: CHART_HEIGHT, ...extra };
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
function visibleRows() {
  const rows = app.channel?.signatures || [];
  const f = app.filters;
  const text = f.text.trim().toLowerCase();
  const base = rows.filter((r) => (!f.hideNoise || !r.noise) && (r.observed || 0) >= f.minCrashes && (!text || r.signature.toLowerCase().includes(text)));
  const flaggedOf = (r) => { const s = sevOf(r); return (s !== 'ok' && f.sev.has(s)) || (r.is_new && f.sev.has('new')); };
  const plain = (r) => sevOf(r) === 'ok' && !r.is_new;
  const unflagged = base.filter((r) => plain(r) && !r.storm);
  const storms = base.filter((r) => plain(r) && r.storm);
  const shown = base.filter((r) => flaggedOf(r) || (f.showStorms && r.storm) || (f.showUnflagged && plain(r)));
  return { shown, unflaggedCount: unflagged.length, stormCount: storms.length, total: rows.length };
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
  since: (r) => (sevOf(r) === 'ok' && !r.is_new ? null : sinceValue(r)),
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
  const { shown, unflaggedCount, stormCount, total } = visibleRows();
  $('unflagged-count').textContent = String(unflaggedCount);
  $('storm-count').textContent = plural(stormCount, 'storm');
  const metaText = total
    ? `${shown.length} of ${total} scored signatures shown${shown.length ? '' : ' — no signature matches the current filters'}`
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
  for (const row of rows) tbody.append(buildRow(row, cols, withChannel, onRow));
  table.append(thead, tbody);
  return table;
}

function buildRow(row, cols, withChannel, onRow) {
  const sev = sevOf(row);
  // the row is clickable; keyboard users get a real button (the expander,
  // or the channel chip in the cross-channel table)
  const tr = el('tr', { class: 'row', tabindex: -1, 'data-sig': row.signature });
  // severity + badges
  const badges = el('div', { class: 'badge-set' });
  if (sev !== 'ok' || !row.is_new) badges.append(chip(sev));
  if (row.is_new) badges.append(chip('new'));
  if (row.storm) badges.append(badge('storm'));
  if (row.noise) badges.append(badge('noise'));
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
    if (st.panel && data.data_version != null && data.data_version === st.data?.data_version &&
        st.view === `${app.days}|${app.granularity}`) return;
    st.data = data;
    st.view = `${app.days}|${app.granularity}`;
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

/** Build the expanded panel once; later refreshes update its text and charts in place
 * (keeps chart zoom, log/table toggles and focus). */
function renderSignaturePanel(st) {
  const { td, data, row } = st;
  const r = data.row || row;
  if (st.panel && st.panel.isConnected) {
    st.modelEl.textContent = signatureModelText(data, r);
    const notes = signatureNotes(data, r);
    st.noteEl.textContent = notes;
    st.noteEl.hidden = !notes;
    st.charts.intraday?.update(hourlySpec(data, { emptyMessage: 'No hourly data for this signature', ariaLabel: `Crashes per hour today, ${midTruncate(r.signature, 60)}` }));
    st.charts.daily?.update(dailySpec(data, { ariaLabel: `Daily crashes, ${midTruncate(r.signature, 60)}` }));
    return;
  }
  td.textContent = '';
  if (st.statusEl) {
    st.statusEl.className = 'visually-hidden';
    st.statusEl.textContent = 'Details loaded';
    td.append(st.statusEl);
  }
  const panel = el('div', { class: 'detail-panel' });
  st.modelEl = el('p', { class: 'detail-model full' }, signatureModelText(data, r));
  panel.append(st.modelEl);
  const notes = signatureNotes(data, r);
  st.noteEl = el('p', { class: 'detail-note full' }, notes);
  st.noteEl.hidden = !notes;
  panel.append(st.noteEl);
  const intradayCard = el('div', { class: 'chart-card' }, el('h3', {}, 'Today by hour ', el('span', { class: 'sub' }, 'UTC')));
  const intraday = el('div');
  intradayCard.append(intraday);
  const dailyCard = el('div', { class: 'chart-card' }, el('h3', {}, 'Daily crashes'));
  const daily = el('div');
  dailyCard.append(daily);
  panel.append(intradayCard, dailyCard);
  td.append(panel);
  st.panel = panel;
  for (const c of Object.values(st.charts)) c.destroy?.();
  st.charts.intraday = barChart(intraday, hourlySpec(data, { emptyMessage: 'No hourly data for this signature', ariaLabel: `Crashes per hour today, ${midTruncate(r.signature, 60)}` }));
  st.charts.daily = lineChart(daily, dailySpec(data, { ariaLabel: `Daily crashes, ${midTruncate(r.signature, 60)}` }));
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
  if (!row) return;
  const f = app.filters;
  let changed = false;
  if (row.noise && f.hideNoise) { f.hideNoise = false; $('hide-noise').checked = false; changed = true; }
  if (sevOf(row) === 'ok' && !row.is_new && row.storm && !f.showStorms) { f.showStorms = true; $('show-storms').checked = true; changed = true; }
  else if (sevOf(row) === 'ok' && !row.is_new && !row.storm && !f.showUnflagged) { f.showUnflagged = true; $('show-unflagged').checked = true; changed = true; }
  if (f.text && !sig.toLowerCase().includes(f.text.toLowerCase())) { f.text = ''; $('sig-search').value = ''; changed = true; }
  if ((row.observed || 0) < f.minCrashes) { f.minCrashes = 0; $('min-crashes').value = '0'; changed = true; }
  const s = sevOf(row);
  if (s !== 'ok' && !f.sev.has(s)) { f.sev.add(s); document.querySelector(`#sig-filters input[value="${s}"]`).checked = true; changed = true; }
  if (changed || !document.querySelector(`#signature-table tr.row[data-sig="${cssEscape(sig)}"]`)) renderSignatures();
  const tr = document.querySelector(`#signature-table tr.row[data-sig="${cssEscape(sig)}"]`);
  if (!tr) return;
  if (!app.expanded.has(sig)) expandRow(row, tr);
  tr.scrollIntoView({ behavior: 'smooth', block: 'start' }); // scroll-padding keeps it below the toolbar
  tr.classList.add('is-target');
  tr.focus({ preventScroll: true });
  setTimeout(() => tr.classList.remove('is-target'), 2500);
}

// ---------------------------------------------------------------- controls
function bindControls() {
  // cards are updated in place: one delegated click handler
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
    const f = app.filters;
    if (e.target.name === 'sev') { if (e.target.checked) f.sev.add(e.target.value); else f.sev.delete(e.target.value); }
    if (e.target.id === 'hide-noise') f.hideNoise = e.target.checked;
    if (e.target.id === 'show-unflagged') f.showUnflagged = e.target.checked;
    if (e.target.id === 'show-storms') f.showStorms = e.target.checked;
    if (e.target.id === 'min-crashes') f.minCrashes = Math.max(0, Number(e.target.value) || 0);
    renderSignatures();
  });
  let timer = null;
  $('sig-search').addEventListener('input', (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { app.filters.text = e.target.value; renderSignatures(); }, 120);
  });
  $('min-crashes').addEventListener('input', (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { app.filters.minCrashes = Math.max(0, Number(e.target.value) || 0); renderSignatures(); }, 200);
  });
  window.addEventListener('hashchange', () => {
    const h = parseHash();
    if (!h || !app.summary || (app.selected && channelKey(h) === channelKey(app.selected))) return;
    if (isAll(h)) selectAll();
    else selectChannel(h.product, h.channel);
  });
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
