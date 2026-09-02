// charts.js — small self-contained SVG charts for the crash-spikes dashboard.
// No dependencies, no build step.  Exports lineChart(), barChart(), sparkline(),
// miniFactors() plus the formatters shared with dashboard.js.

const SVG_NS = 'http://www.w3.org/2000/svg';
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const DAY_MS = 86400000;
const MINUS = '−';
const NBSP = ' ';

export const SEV_COLOR = {
  major: 'var(--st-major)', spike: 'var(--st-spike)', watch: 'var(--st-watch)',
  drop: 'var(--st-drop)', new: 'var(--st-new)', ok: 'var(--ink)',
};

// ---------------------------------------------------------------- DOM helpers
function setAttrs(node, attrs) {
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'text') node.textContent = v;
    else node.setAttribute(k, v === true ? '' : v);
  }
}

/** Create an HTML element; string children become text nodes (never HTML). */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  setAttrs(node, attrs);
  for (const c of children) if (c != null && c !== false) node.append(c);
  return node;
}

export function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  setAttrs(node, attrs);
  return node;
}

// ---------------------------------------------------------------- formatting
export function fmtInt(n) {
  return n == null || Number.isNaN(n) ? '—' : Math.round(n).toLocaleString('en-US');
}

export function fmtCompact(n) {
  if (n == null || Number.isNaN(n)) return '—';
  const a = Math.abs(n);
  const sign = n < 0 ? MINUS : '';
  if (a >= 1e6) return sign + (a / 1e6).toFixed(a >= 1e7 ? 0 : 1) + 'M';
  if (a >= 1e3) return sign + (a / 1e3).toFixed(a >= 1e5 ? 0 : 1) + 'k';
  return sign + (a >= 10 || Number.isInteger(a) ? String(Math.round(a)) : a.toFixed(1));
}

export function fmtSigned(n, compact = false) {
  if (n == null) return '—';
  const body = compact ? fmtCompact(Math.abs(n)) : fmtInt(Math.abs(n));
  return (n < 0 ? MINUS : '+') + body;
}

/** Signed percent while |ratio − 1| < 1, multiplicative beyond ("×2.4"). */
export function fmtRatio(r) {
  if (r == null || !Number.isFinite(r)) return '';
  const d = r - 1;
  if (Math.abs(d) < 1) return (d < 0 ? MINUS : '+') + Math.round(Math.abs(d) * 100) + NBSP + '%';
  return '×' + (r >= 10 ? String(Math.round(r)) : r.toFixed(1));
}

export function fmtZ(z) {
  return z == null ? '—' : (z < 0 ? MINUS : '') + Math.abs(z).toFixed(1);
}

export function parseDay(s) {
  const [y, m, d] = s.split('-').map(Number);
  return Date.UTC(y, m - 1, d);
}

export function fmtDate(ms, withYear = false) {
  const d = new Date(ms);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}` + (withYear ? ` ${d.getUTCFullYear()}` : '');
}

export function fmtDateLong(ms) {
  return `${WDAYS[new Date(ms).getUTCDay()]} ${fmtDate(ms, true)}`;
}

// ---------------------------------------------------------------- scales & ticks
function niceStep(range, count) {
  const raw = range / Math.max(1, count);
  const p = 10 ** Math.floor(Math.log10(raw));
  const r = raw / p;
  return (r <= 1 ? 1 : r <= 2 ? 2 : r <= 2.5 ? 2.5 : r <= 5 ? 5 : 10) * p;
}

function linearTicks(lo, hi, count) {
  const step = niceStep(hi - lo || 1, count);
  const out = [];
  for (let v = Math.ceil(lo / step - 1e-9) * step; v <= hi + step * 1e-6; v += step) {
    out.push(Math.round(v * 1e6) / 1e6);
  }
  return out;
}

function logTicks(lo, hi) {
  const out = [];
  const p0 = Math.floor(Math.log10(lo));
  const p1 = Math.ceil(Math.log10(hi));
  const sub = p1 - p0 <= 2 ? [1, 2, 5] : [1];
  for (let p = p0; p <= p1; p++) {
    for (const m of sub) {
      const v = m * 10 ** p;
      if (v >= lo && v <= hi) out.push(v);
    }
  }
  return out;
}

function dateTicks(xs, width) {
  if (!xs.length) return [];
  const first = xs[0];
  const last = xs[xs.length - 1];
  const days = (last - first) / DAY_MS;
  const maxTicks = Math.max(2, Math.floor(width / 64));
  let ticks = [];
  if (days <= 45) {
    for (let t = first; t <= last; t += DAY_MS) {
      if (new Date(t).getUTCDay() === 1) ticks.push({ ms: t, label: fmtDate(t) });
    }
    if (ticks.length < 2) ticks = xs.map((ms) => ({ ms, label: fmtDate(ms) }));
  } else {
    const d = new Date(first);
    let y = d.getUTCFullYear();
    let m = d.getUTCMonth();
    for (;;) {
      m += 1;
      if (m > 11) { m = 0; y += 1; }
      const t = Date.UTC(y, m, 1);
      if (t > last) break;
      ticks.push({ ms: t, label: m === 0 ? `Jan ${y}` : MONTHS[m] });
      if (days <= 130) {
        const mid = Date.UTC(y, m, 15);
        if (mid <= last) ticks.push({ ms: mid, label: fmtDate(mid) });
      }
    }
    ticks.sort((a, b) => a.ms - b.ms);
  }
  let stride = 1;
  while (ticks.length / stride > maxTicks) stride += 1;
  ticks = ticks.filter((_, i) => i % stride === 0);
  if (days > 200 && ticks.length && !ticks.some((t) => /\d{4}/.test(t.label))) {
    ticks[0].label += ` ${new Date(ticks[0].ms).getUTCFullYear()}`;
  }
  return ticks;
}

function makeX(d0, d1, r0, r1) {
  const k = (r1 - r0) / (d1 - d0 || 1);
  return (v) => (d1 === d0 ? (r0 + r1) / 2 : r0 + (v - d0) * k);
}

function makeY(lo, hi, top, bottom, log) {
  if (log) {
    const l0 = Math.log10(lo);
    const l1 = Math.log10(hi);
    return (v) => bottom - ((Math.log10(Math.max(v, lo)) - l0) / (l1 - l0 || 1)) * (bottom - top);
  }
  return (v) => bottom - ((v - lo) / (hi - lo || 1)) * (bottom - top);
}

function nearestIndex(pxs, px) {
  let best = 0;
  let dist = Infinity;
  for (let i = 0; i < pxs.length; i++) {
    const d = Math.abs(pxs[i] - px);
    if (d < dist) { dist = d; best = i; }
  }
  return best;
}

function pathFrom(points) {
  let d = '';
  let pen = false;
  for (const p of points) {
    if (!p) { pen = false; continue; }
    d += `${pen ? 'L' : 'M'}${p.x.toFixed(1)},${p.y.toFixed(1)}`;
    pen = true;
  }
  return d;
}

function areaPath(pxs, hiPy, loPy) {
  let d = '';
  let run = [];
  const flush = () => {
    if (run.length < 2) { run = []; return; }
    d += 'M' + run.map((i) => `${pxs[i].toFixed(1)},${hiPy[i].toFixed(1)}`).join('L');
    d += 'L' + run.slice().reverse().map((i) => `${pxs[i].toFixed(1)},${loPy[i].toFixed(1)}`).join('L') + 'Z';
    run = [];
  };
  for (let i = 0; i < pxs.length; i++) {
    if (hiPy[i] == null || loPy[i] == null) flush();
    else run.push(i);
  }
  flush();
  return d;
}

function roundedBar(x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h);
  if (h <= 0.5) return '';
  return `M${x},${y + h}V${y + rr}a${rr},${rr} 0 0 1 ${rr},${-rr}h${w - 2 * rr}a${rr},${rr} 0 0 1 ${rr},${rr}V${y + h}Z`;
}

// ---------------------------------------------------------------- chart frame
function frame(container, { legend = [], buttons = [] }) {
  container.classList.add('chart');
  container.textContent = '';
  const legendEl = el('div', { class: 'chart-legend' });
  for (const item of legend) {
    const key = el('i', { class: `lg-key${item.kind ? ` lg-${item.kind}` : ''}`, style: `--c:${item.color}` });
    legendEl.append(el('span', { class: 'lg' }, key, item.label));
  }
  const actions = el('div', { class: 'chart-actions' });
  const plot = el('div', { class: 'chart-plot' });
  const tip = el('div', { class: 'chart-tooltip', hidden: true });
  const table = el('div', { class: 'chart-table', hidden: true, tabindex: 0, role: 'region', 'aria-label': 'Chart data as a table' });
  plot.append(tip);
  container.append(el('div', { class: 'chart-toolbar' }, legendEl, actions), plot, table);
  const f = { container, plot, tip, table, legendEl, buttons: {}, lastWidth: 0 };
  for (const b of buttons) {
    const btn = el('button', { type: 'button', class: 'chart-btn' }, b.label);
    if (b.onToggle) {
      btn.setAttribute('aria-pressed', 'false');
      btn.addEventListener('click', () => {
        const on = btn.getAttribute('aria-pressed') !== 'true';
        btn.setAttribute('aria-pressed', String(on));
        b.onToggle(on);
      });
    } else if (b.onClick) btn.addEventListener('click', b.onClick);
    if (b.hidden) btn.hidden = true;
    actions.append(btn);
    f.buttons[b.key] = btn;
  }
  f.showTable = (on) => { table.hidden = !on; plot.hidden = on; };
  f.setEmpty = (msg) => {
    plot.querySelector('svg')?.remove();
    plot.querySelector('.chart-empty')?.remove();
    if (msg) plot.append(el('div', { class: 'chart-empty' }, msg));
  };
  f.observe = (render) => {
    const ro = new ResizeObserver(() => {
      const w = container.clientWidth;
      if (w > 0 && w !== f.lastWidth) { f.lastWidth = w; render(); }
    });
    ro.observe(container);
    f.ro = ro;
  };
  return f;
}

function showTip(f, x, y, title, rows) {
  const tip = f.tip;
  tip.textContent = '';
  tip.append(el('div', { class: 'tt-title' }, title));
  for (const r of rows) {
    const cls = r.kind === 'rect' ? ' tt-rect' : r.kind === 'dot' ? ' tt-dot' : r.kind === 'dash' ? ' tt-dash' : '';
    const key = el('i', { class: `tt-key${cls}`, style: `--c:${r.color || 'transparent'}` });
    tip.append(el('div', { class: 'tt-row' }, key, el('span', { class: 'tt-val' }, r.value), el('span', { class: 'tt-label' }, r.label)));
  }
  tip.hidden = false;
  const W = f.plot.clientWidth;
  const tw = tip.offsetWidth;
  const th = tip.offsetHeight;
  let left = x + 14;
  if (left + tw > W) left = x - tw - 14;
  if (left < 0) left = 0;
  const top = Math.max(0, Math.min(y - th / 2, f.plot.clientHeight - th));
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function fillTable(f, caption, columns, rows) {
  f.table.textContent = '';
  const thead = el('thead');
  const tr = el('tr');
  for (const c of columns) tr.append(el('th', { scope: 'col' }, c));
  thead.append(tr);
  const tbody = el('tbody');
  for (const r of rows) {
    const trr = el('tr');
    for (const v of r) trr.append(el('td', {}, v));
    tbody.append(trr);
  }
  f.table.append(el('table', {}, el('caption', { class: 'visually-hidden' }, caption), thead, tbody));
}

/** Pointer + keyboard hover layer snapping to the nearest x; calls onIndex(i|null). */
function hoverLayer(root, box, pxs, onIndex, onBrush, describe) {
  const cross = svg('line', { class: 'crosshair', y1: box.top, y2: box.bottom, visibility: 'hidden' });
  // the overlay is a slider for assistive tech: arrow keys move along the
  // x axis and the current point's values are announced as its value text
  const overlay = svg('rect', {
    class: 'overlay', x: box.left, y: box.top, width: Math.max(0, box.right - box.left),
    height: Math.max(0, box.bottom - box.top), tabindex: 0, role: 'slider',
    'aria-orientation': 'horizontal', 'aria-valuemin': 0, 'aria-valuemax': Math.max(0, pxs.length - 1),
    'aria-valuenow': Math.max(0, pxs.length - 1),
    'aria-label': onBrush
      ? 'Chart reader: arrow keys move between points, Escape closes; Shift+arrows select a range and Enter zooms on it, Backspace resets the zoom'
      : 'Chart reader: arrow keys move between points, Escape closes',
  });
  let index = -1;
  // brush: drag across the plot to zoom on a range
  let drag = null;
  const brush = svg('rect', { class: 'brush', y: box.top, height: Math.max(0, box.bottom - box.top), visibility: 'hidden' });
  const localX = (e) => e.clientX - root.getBoundingClientRect().left;
  if (onBrush) {
    overlay.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      drag = { x0: localX(e), x1: localX(e), moved: false };
      overlay.setPointerCapture(e.pointerId);
    });
    overlay.addEventListener('pointerup', (e) => {
      if (!drag) return;
      const { x0, x1, moved } = drag;
      drag = null;
      brush.setAttribute('visibility', 'hidden');
      overlay.releasePointerCapture(e.pointerId);
      if (!moved || Math.abs(x1 - x0) < 6) return;
      const a = nearestIndex(pxs, Math.min(x0, x1));
      const b = nearestIndex(pxs, Math.max(x0, x1));
      if (b > a) onBrush(a, b);
    });
    overlay.addEventListener('dblclick', () => onBrush(null));
  }
  overlay.addEventListener('pointermove', (e) => {
    if (drag) {
      drag.x1 = localX(e);
      if (Math.abs(drag.x1 - drag.x0) >= 6) {
        drag.moved = true;
        const lo = Math.max(box.left, Math.min(drag.x0, drag.x1));
        const hi = Math.min(box.right, Math.max(drag.x0, drag.x1));
        brush.setAttribute('x', lo);
        brush.setAttribute('width', Math.max(0, hi - lo));
        brush.setAttribute('visibility', 'visible');
        set(null);
      }
      return;
    }
    set(nearestIndex(pxs, localX(e)));
  });
  const set = (i) => {
    index = i == null ? -1 : i; // null would pass `index >= 0`
    if (i == null || i < 0) { cross.setAttribute('visibility', 'hidden'); onIndex(null); return; }
    cross.setAttribute('x1', pxs[i]);
    cross.setAttribute('x2', pxs[i]);
    cross.setAttribute('visibility', 'visible');
    overlay.setAttribute('aria-valuenow', i);
    if (describe) overlay.setAttribute('aria-valuetext', describe(i));
    onIndex(i);
  };
  if (describe && pxs.length) overlay.setAttribute('aria-valuetext', describe(pxs.length - 1));
  overlay.addEventListener('pointerleave', () => { if (!drag) set(null); });
  overlay.addEventListener('focus', () => set(index >= 0 ? index : pxs.length - 1));
  overlay.addEventListener('blur', () => set(null));
  // keyboard zoom: Shift+Arrow extends a selection from an anchor, Enter zooms
  // on it, Backspace/Delete resets the zoom, Escape drops the selection
  let anchor = null;
  const showBrush = (a, b) => {
    const lo = pxs[Math.min(a, b)], hi = pxs[Math.max(a, b)];
    brush.setAttribute('x', Math.max(box.left, lo - 3));
    brush.setAttribute('width', Math.max(0, Math.min(box.right, hi + 3) - Math.max(box.left, lo - 3)));
    brush.setAttribute('visibility', 'visible');
  };
  const clearBrush = () => { anchor = null; brush.setAttribute('visibility', 'hidden'); };
  overlay.addEventListener('keydown', (e) => {
    const n = pxs.length;
    const cur = index >= 0 ? index : n - 1;
    if (onBrush && e.shiftKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
      e.preventDefault();
      if (anchor == null) anchor = cur;
      const next = Math.max(0, Math.min(n - 1, cur + (e.key === 'ArrowRight' ? 1 : -1)));
      set(next);
      showBrush(anchor, next);
      return;
    }
    if (onBrush && e.key === 'Enter' && anchor != null && anchor !== cur) {
      e.preventDefault();
      const a = Math.min(anchor, cur), b = Math.max(anchor, cur);
      clearBrush();
      onBrush(a, b);
      return;
    }
    if (onBrush && (e.key === 'Backspace' || e.key === 'Delete')) { e.preventDefault(); clearBrush(); onBrush(null); return; }
    if (e.key === 'Escape') { if (anchor != null) clearBrush(); else set(null); return; }
    const map = { ArrowLeft: cur - 1, ArrowRight: cur + 1, Home: 0, End: n - 1 };
    if (!(e.key in map)) return;
    e.preventDefault();
    if (anchor != null && !e.shiftKey) clearBrush();
    set(Math.max(0, Math.min(n - 1, map[e.key])));
  });
  overlay.addEventListener('blur', clearBrush);
  root.append(brush, cross, overlay);
  return { cross, overlay };
}

function yAxis(root, ticks, y, left, right, format) {
  for (const t of ticks) {
    const py = y(t);
    root.append(svg('line', { class: 'gridline', x1: left, x2: right, y1: py, y2: py }));
    root.append(svg('text', { x: left - 6, y: py + 3.5, 'text-anchor': 'end', text: format(t) }));
  }
}

// ---------------------------------------------------------------- line chart
/**
 * spec: { dates[], granularity, observed[], expected[], lo3[], hi3[], lo5[], hi5[], z[],
 *         partial[], projected[], severity[], releases[{date, version}], height, label }
 */
const SLICED = ['dates', 'observed', 'expected', 'lo3', 'hi3', 'lo5', 'hi5', 'z', 'partial', 'projected', 'severity'];

function sliceSpec(spec, a, b) {
  const out = { ...spec };
  for (const k of SLICED) if (Array.isArray(spec[k])) out[k] = spec[k].slice(a, b + 1);
  return out;
}

export function lineChart(container, spec) {
  // zoom: [first, last] indices into the full arrays, or null
  const state = { spec, log: false, zoom: null };
  const f = frame(container, {
    legend: [
      { label: spec.label || 'Observed', color: 'var(--ink)' },
      { label: 'Expected', color: 'var(--expected)', kind: 'dash' },
      { label: '±3 band (watch)', color: 'var(--band3)', kind: 'rect' },
      { label: '±5 band (spike)', color: 'var(--band5)', kind: 'rect' },
      { label: 'Release', color: 'var(--axis)', kind: 'rule' },
    ],
    buttons: [
      { key: 'zoom', label: 'Reset zoom', hidden: true, onClick: () => setZoom(null) },
      { key: 'log', label: 'Log scale', onToggle: (on) => { state.log = on; render(); } },
      { key: 'table', label: 'Table', onToggle: (on) => f.showTable(on) },
    ],
  });

  function setZoom(range) {
    state.zoom = range;
    f.buttons.zoom.hidden = !range;
    render();
  }

  function render() {
    // keep keyboard focus on the chart reader across re-renders (setEmpty
    // below already drops the old svg)
    const hadFocus = f.plot.contains(document.activeElement) && document.activeElement.classList.contains('overlay');
    const s = state.zoom ? sliceSpec(state.spec, state.zoom[0], state.zoom[1]) : state.spec;
    const width = f.container.clientWidth;
    if (width < 40) return;
    const height = s.height || 260;
    const n = s.dates?.length || 0;
    f.setEmpty(n ? null : 'No daily history for this series yet');
    if (!n) return;
    const weekly = s.granularity === 'week';
    const xs = s.dates.map(parseDay);
    const get = (arr, i) => (arr && arr[i] != null ? arr[i] : null);

    // y domain: clamp to max(3 × band top, 1.1 × max of the non-clipped points)
    const bandTop = Math.max(1, ...s.hi5.filter((v) => v != null), ...s.expected.filter((v) => v != null));
    const clipAt = 3 * bandTop;
    const all = [];
    for (let i = 0; i < n; i++) {
      for (const v of [get(s.observed, i), get(s.expected, i), get(s.hi5, i), s.partial?.[i] ? get(s.projected, i) : null]) {
        if (v != null) all.push(v);
      }
    }
    const isClipped = (v) => !state.log && v != null && v > clipAt;
    let yMin = 0;
    let yMax;
    if (state.log) {
      const pos = all.filter((v) => v > 0);
      yMin = 10 ** Math.floor(Math.log10(Math.max(0.5, Math.min(...pos, ...s.lo5.filter((v) => v > 0)))));
      yMax = Math.max(...pos, yMin * 10) * 1.3;
    } else {
      // points above 3 × band top are clipped so one huge day cannot flatten the rest
      const kept = all.filter((v) => v <= clipAt);
      yMax = 1.1 * Math.max(bandTop, ...kept);
    }
    const yTicks = state.log ? logTicks(yMin, yMax) : linearTicks(0, yMax, Math.max(3, Math.floor((height - 46) / 44)));
    const left = 10 + Math.max(...yTicks.map((t) => fmtInt(t).length)) * 6.6;
    const top = 22;
    const right = 16;
    const bottom = 24;
    const box = { left, right: width - right, top, bottom: height - bottom };
    const x = makeX(xs[0], xs[n - 1], box.left + 6, box.right - 6);
    const y = makeY(yMin, yMax, box.top, box.bottom, state.log);
    const yc = (v) => Math.max(box.top, y(v));
    const pxs = xs.map(x);

    // a group, not an image: it contains the keyboard-operable chart reader
    const root = svg('svg', { width, height, role: 'group', 'aria-label': s.ariaLabel || 'Daily crashes against the expected band' });
    yAxis(root, yTicks, y, box.left, box.right, fmtInt);
    root.append(svg('line', { class: 'baseline', x1: box.left, x2: box.right, y1: box.bottom, y2: box.bottom }));
    for (const t of dateTicks(xs, box.right - box.left)) {
      const px = x(t.ms);
      if (px < box.left || px > box.right) continue;
      root.append(svg('text', { x: px, y: box.bottom + 16, 'text-anchor': 'middle', text: t.label }));
    }

    // bands (outer first)
    const py = (arr) => arr.map((v) => (v == null ? null : yc(v)));
    root.append(svg('path', { class: 'band5', d: areaPath(pxs, py(s.hi5), py(s.lo5)) }));
    root.append(svg('path', { class: 'band3', d: areaPath(pxs, py(s.hi3), py(s.lo3)) }));
    root.append(svg('path', { class: 'series-expected', d: pathFrom(s.expected.map((v, i) => (v == null ? null : { x: pxs[i], y: yc(v) }))) }));

    // release rules with collision-avoiding labels
    let lastLabelRight = -Infinity;
    for (const r of s.releases || []) {
      const ms = parseDay(r.date);
      if (ms < xs[0] - (weekly ? 6 * DAY_MS : 0) || ms > xs[n - 1] + (weekly ? 6 * DAY_MS : 0)) continue;
      const px = x(Math.max(xs[0], Math.min(xs[n - 1], ms)));
      root.append(svg('line', { class: 'rule', x1: px, x2: px, y1: box.top, y2: box.bottom }));
      const w = r.version.length * 6 + 6;
      if (px - w / 2 > lastLabelRight) {
        root.append(svg('text', { class: 'lbl', x: px, y: box.top - 8, 'text-anchor': 'middle', text: r.version }));
        lastLabelRight = px + w / 2;
      }
    }

    // observed line
    root.append(svg('path', { class: 'series-observed', d: pathFrom(s.observed.map((v, i) => (v == null ? null : { x: pxs[i], y: yc(v) }))) }));

    // markers: out-of-band points, partial bucket, clipped points
    const clips = [];
    for (let i = 0; i < n; i++) {
      const obs = get(s.observed, i);
      if (obs == null) continue;
      const sev = s.severity?.[i] || 'ok';
      const color = SEV_COLOR[sev] || SEV_COLOR.ok;
      const px = pxs[i];
      if (isClipped(obs)) {
        clips.push({ px, color, value: obs });
        continue;
      }
      if (s.partial?.[i]) {
        const proj = get(s.projected, i);
        if (proj != null) {
          const pyProj = yc(proj);
          root.append(svg('line', { class: 'extension', x1: px, x2: px, y1: y(obs), y2: pyProj, stroke: color }));
          if (isClipped(proj)) clips.push({ px, color, value: proj, faint: true });
          else root.append(svg('line', { class: 'extension', x1: px - 5, x2: px + 5, y1: pyProj, y2: pyProj, stroke: color }));
        }
        root.append(svg('circle', { class: 'hollow', cx: px, cy: y(obs), r: 4.5, stroke: color }));
      } else if (sev !== 'ok') {
        root.append(svg('circle', { class: 'ring', cx: px, cy: y(obs), r: 6 }));
        root.append(svg('circle', { cx: px, cy: y(obs), r: 4, fill: color }));
      }
    }

    drawClips(root, box, clips);

    // hover
    const focus = svg('circle', { r: 5, fill: 'none', stroke: 'var(--ink)', 'stroke-width': 1.5, visibility: 'hidden' });
    root.append(focus);
    hoverLayer(root, box, pxs, (i) => {
      if (i == null) { f.tip.hidden = true; focus.setAttribute('visibility', 'hidden'); return; }
      const obs = get(s.observed, i);
      if (obs != null) {
        focus.setAttribute('cx', pxs[i]);
        focus.setAttribute('cy', yc(obs));
        focus.setAttribute('visibility', 'visible');
      } else focus.setAttribute('visibility', 'hidden');
      showTip(f, pxs[i], obs != null ? yc(obs) : (box.top + box.bottom) / 2, bucketTitle(i), tipRows(i));
    }, (a, b) => {
      if (a == null) { setZoom(null); return; }
      const base = state.zoom ? state.zoom[0] : 0;
      if (b - a >= 1) setZoom([base + a, base + b]);
    }, (i) => `${bucketTitle(i)}: ${tipRows(i).map((r) => `${r.label} ${r.value}`).join(', ')}`);
    f.plot.querySelector('svg')?.remove();
    f.plot.prepend(root);
    if (hadFocus) root.querySelector('.overlay')?.focus({ preventScroll: true });

    function bucketTitle(i) {
      return weekly ? `Week of ${fmtDate(xs[i], true)}` : fmtDateLong(xs[i]);
    }
    function tipRows(i) {
      const partial = !!s.partial?.[i];
      const sev = s.severity?.[i] || 'ok';
      const rows = [{ value: fmtInt(get(s.observed, i)), label: partial ? 'observed so far' : 'observed', color: 'var(--ink)' }];
      if (partial && get(s.projected, i) != null) rows.push({ value: fmtInt(s.projected[i]), label: 'projected', color: 'var(--ink)', kind: 'dot' });
      rows.push({ value: fmtInt(get(s.expected, i)), label: 'expected', color: 'var(--expected)', kind: 'dash' });
      if (get(s.lo3, i) != null) rows.push({ value: `${fmtInt(s.lo3[i])} – ${fmtInt(s.hi3[i])}`, label: '±3 band', color: 'var(--band3)', kind: 'rect' });
      if (get(s.lo5, i) != null) rows.push({ value: `${fmtInt(s.lo5[i])} – ${fmtInt(s.hi5[i])}`, label: '±5 band', color: 'var(--band5)', kind: 'rect' });
      if (get(s.z, i) != null) rows.push({ value: `z ${fmtZ(s.z[i])}`, label: sev === 'ok' ? 'within band' : sev, color: sev === 'ok' ? 'var(--axis)' : SEV_COLOR[sev], kind: 'dot' });
      return rows;
    }
    const tableRows = [];
    for (let i = 0; i < n; i++) {
      const partial = !!s.partial?.[i];
      tableRows.push([
        bucketTitle(i) + (partial ? ' (in progress)' : ''),
        fmtInt(get(s.observed, i)) + (partial && get(s.projected, i) != null ? ` → ${fmtInt(s.projected[i])}` : ''),
        fmtInt(get(s.expected, i)),
        get(s.lo3, i) == null ? '—' : `${fmtInt(s.lo3[i])} – ${fmtInt(s.hi3[i])}`,
        get(s.lo5, i) == null ? '—' : `${fmtInt(s.lo5[i])} – ${fmtInt(s.hi5[i])}`,
        fmtZ(get(s.z, i)),
        s.severity?.[i] || 'ok',
      ]);
    }
    fillTable(f, 'Daily crashes, table view', [weekly ? 'Week' : 'Day', 'Observed', 'Expected', '±3 band', '±5 band', 'z', 'Severity'], tableRows);
  }

  /** Clipped points: an arrow at the top edge plus the value, labels laid out without overlaps. */
  function drawClips(root, box, clips) {
    const placed = [];
    for (const c of clips) {
      const g = svg('g', { opacity: c.faint ? 0.5 : 1 });
      g.append(svg('path', { d: `M${c.px},${box.top - 1}l-4.5,7h9z`, fill: c.color }));
      const text = fmtCompact(c.value);
      const w = text.length * 6.2 + 2;
      const sides = [['start', c.px + 7], ['end', c.px - 7]];
      if (c.px + 7 + w > box.right) sides.reverse();
      const candidates = [0, 1].flatMap((row) => sides.map(([anchor, tx]) => [anchor, tx, row]));
      for (const [anchor, tx, row] of candidates) {
        const l = anchor === 'start' ? tx : tx - w;
        const r = anchor === 'start' ? tx + w : tx;
        if (l < box.left - 4 || r > box.right + 6) continue;
        if (placed.some(([a, b, rw]) => rw === row && l < b + 3 && r > a - 3)) continue;
        g.append(svg('text', { class: 'clip-label', x: tx, y: box.top + 6 + row * 11, 'text-anchor': anchor, text }));
        placed.push([l, r, row]);
        break;
      }
      root.append(g);
    }
  }

  f.observe(render);
  render();
  return {
    update(next) {
      // keep the zoomed date range when the data is refreshed
      const zoomDates = state.zoom ? [state.spec.dates[state.zoom[0]], state.spec.dates[state.zoom[1]]] : null;
      state.spec = { ...state.spec, ...next };
      if (zoomDates) {
        const a = state.spec.dates.indexOf(zoomDates[0]);
        const b = state.spec.dates.indexOf(zoomDates[1]);
        setZoom(a >= 0 && b > a ? [a, b] : null);
      } else render();
    },
    destroy() { f.ro?.disconnect(); container.textContent = ''; },
  };
}

// ---------------------------------------------------------------- time zone preference
// Hour buckets are Socorro's UTC hours; the intraday charts can label them in
// the browser's local time.  The choice is stored in localStorage and shared
// by every intraday chart on the page (a `dashboard:timezone` event).
const TZ_KEY = 'dashboard.timeZone';
const TZ_EVENT = 'dashboard:timezone';

export function useLocalTime() {
  try { return localStorage.getItem(TZ_KEY) === 'local'; } catch { return false; }
}

export function setLocalTime(on) {
  try { localStorage.setItem(TZ_KEY, on ? 'local' : 'utc'); } catch { /* storage unavailable */ }
  window.dispatchEvent(new CustomEvent(TZ_EVENT, { detail: { local: !!on } }));
}

/** "UTC", or the local zone as e.g. "CEST (UTC+2)". */
export function zoneLabel(local = useLocalTime()) {
  if (!local) return 'UTC';
  const now = new Date();
  const short = (now.toLocaleTimeString('en-US', { timeZoneName: 'short' }).split(' ').pop() || '').replace(/^GMT/, 'UTC');
  const off = -now.getTimezoneOffset();
  const sign = off >= 0 ? '+' : '−';
  const hh = Math.floor(Math.abs(off) / 60);
  const mm = Math.abs(off) % 60;
  const offset = `UTC${sign}${hh}${mm ? `:${String(mm).padStart(2, '0')}` : ''}`;
  return short && short !== offset ? `${short} (${offset})` : offset;
}

/** Short name of the local zone for the toggle button: "CEST" when the browser knows it, else "UTC+2". */
export function localZoneShort() {
  const label = zoneLabel(true);
  return label.includes(' (') ? label.slice(0, label.indexOf(' (')) : label;
}

/** Label of the start of UTC hour *h* of *day* ("YYYY-MM-DD"), in UTC or local time. */
export function hourLabel(day, h, local = useLocalTime()) {
  if (!local) return `${String(h % 24).padStart(2, '0')}:00`;
  const ms = (day ? parseDay(day) : Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth(), new Date().getUTCDate())) + h * 3600000;
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

// ---------------------------------------------------------------- bar chart (intraday)
/** spec: { hours[], today[], yesterday[], expected_today[], in_progress_hour, height } */
export function barChart(container, spec) {
  const state = { spec };
  const f = frame(container, {
    legend: [
      { label: 'Today', color: 'var(--ink)', kind: 'rect' },
      { label: 'Expected', color: 'var(--expected)', kind: 'dash' },
      { label: 'Yesterday', color: 'var(--yesterday)' },
    ],
    buttons: [
      { key: 'tz', label: 'Local time', onToggle: (on) => setLocalTime(on) },
      { key: 'table', label: 'Table', onToggle: (on) => f.showTable(on) },
    ],
  });
  f.buttons.tz.textContent = `Local time (${localZoneShort()})`;
  const syncTz = () => { f.buttons.tz.setAttribute('aria-pressed', String(useLocalTime())); };
  syncTz();
  const onTz = () => { syncTz(); render(); };
  window.addEventListener(TZ_EVENT, onTz);

  function render() {
    const hadFocus = f.plot.contains(document.activeElement) && document.activeElement.classList.contains('overlay');
    const s = state.spec;
    const local = useLocalTime();
    const zone = zoneLabel(local);
    const hl = (h) => hourLabel(s.day, h, local);
    // tick every 6 hours of the displayed clock
    const tickAt = (h) => (local ? Number(hl(h).slice(0, 2)) % 6 === 0 && hl(h).slice(3) === '00' : h % 6 === 0);
    const width = f.container.clientWidth;
    if (width < 40) return;
    const height = s.height || 240;
    const hours = s.hours || [];
    const n = hours.length;
    f.setEmpty(n ? null : (s.emptyMessage || 'No hourly data for this series'));
    if (!n) return;
    const get = (arr, i) => (arr && arr[i] != null ? arr[i] : null);
    const vals = [];
    for (let i = 0; i < n; i++) for (const arr of [s.today, s.yesterday, s.expected_today]) if (get(arr, i) != null) vals.push(arr[i]);
    const yMax = Math.max(1, ...vals) * 1.1;
    const yTicks = linearTicks(0, yMax, Math.max(3, Math.floor((height - 46) / 44)));
    const left = 10 + Math.max(...yTicks.map((t) => fmtInt(t).length)) * 6.6;
    const box = { left, right: width - 12, top: 20, bottom: height - 24 };
    const slot = (box.right - box.left) / n;
    const barW = Math.min(24, Math.max(2, slot - 2));
    const y = makeY(0, yMax, box.top, box.bottom, false);
    const cx = (i) => box.left + slot * (i + 0.5);
    const pxs = hours.map((_, i) => cx(i));

    const root = svg('svg', { width, height, role: 'group', 'aria-label': s.ariaLabel || 'Crashes per hour today against the expected profile' });
    yAxis(root, yTicks, y, box.left, box.right, fmtInt);
    const band = svg('rect', { class: 'hover-band', y: box.top, height: box.bottom - box.top, width: slot, visibility: 'hidden' });
    root.append(band);
    root.append(svg('line', { class: 'baseline', x1: box.left, x2: box.right, y1: box.bottom, y2: box.bottom }));
    for (let i = 0; i < n; i++) {
      // no tick label under the zone label at the right end of the axis
      const zoneWidth = zone.length * 6.5 + 10;
      if (tickAt(hours[i]) && cx(i) + 20 < box.right - zoneWidth) root.append(svg('text', { x: cx(i), y: box.bottom + 16, 'text-anchor': 'middle', text: hl(hours[i]) }));
    }
    root.append(svg('text', { class: 'lbl', x: box.right, y: box.bottom + 16, 'text-anchor': 'end', text: zone }));

    const bars = [];
    for (let i = 0; i < n; i++) {
      const v = get(s.today, i);
      if (v == null) { bars.push(null); continue; }
      const inProgress = hours[i] === s.in_progress_hour;
      const top = y(v);
      const h = box.bottom - top;
      const bar = svg('path', { class: inProgress ? 'bar bar-progress' : 'bar', d: inProgress ? roundedBar(cx(i) - barW / 2 + 1, top + 1, barW - 2, Math.max(0, h - 1), 3) : roundedBar(cx(i) - barW / 2, top, barW, h, 4) });
      root.append(bar);
      bars.push(bar);
      if (inProgress) {
        const anchor = cx(i) + 34 > box.right ? 'end' : cx(i) - 34 < box.left ? 'start' : 'middle';
        const lx = anchor === 'end' ? box.right : anchor === 'start' ? box.left : cx(i);
        root.append(svg('text', { class: 'lbl', x: lx, y: box.top - 7, 'text-anchor': anchor, text: 'in progress' }));
        root.append(svg('line', { class: 'rule', x1: cx(i), x2: cx(i), y1: box.top - 3, y2: top - 2 }));
      }
    }
    root.append(svg('path', { class: 'series-yesterday', d: pathFrom((s.yesterday || []).map((v, i) => (v == null ? null : { x: cx(i), y: y(v) }))) }));
    root.append(svg('path', { class: 'series-expected', d: pathFrom((s.expected_today || []).map((v, i) => (v == null ? null : { x: cx(i), y: y(v) }))) }));

    hoverLayer(root, box, pxs, (i) => {
      bars.forEach((b) => b?.classList.remove('is-hover'));
      if (i == null) { f.tip.hidden = true; band.setAttribute('visibility', 'hidden'); return; }
      band.setAttribute('x', box.left + slot * i);
      band.setAttribute('visibility', 'visible');
      bars[i]?.classList.add('is-hover');
      const today = get(s.today, i);
      const inProgress = hours[i] === s.in_progress_hour;
      const rows = [
        { value: fmtInt(today), label: today == null ? 'today (not yet)' : inProgress ? 'today (in progress)' : 'today', color: 'var(--ink)', kind: 'rect' },
        { value: fmtInt(get(s.expected_today, i)), label: inProgress ? 'expected (full hour)' : 'expected', color: 'var(--expected)', kind: 'dash' },
        { value: fmtInt(get(s.yesterday, i)), label: 'yesterday', color: 'var(--yesterday)' },
      ];
      const anchorY = today != null ? y(today) : y(get(s.expected_today, i) || 0);
      showTip(f, pxs[i], anchorY, `${hl(hours[i])}–${hl(hours[i] + 1)} ${zone}`, rows);
    }, undefined, (i) => {
      const today = get(s.today, i);
      const parts = [`${hl(hours[i])} ${zone}`];
      parts.push(today == null ? 'today not yet' : `today ${fmtInt(today)}${hours[i] === s.in_progress_hour ? ' (in progress)' : ''}`);
      parts.push(`expected ${fmtInt(get(s.expected_today, i))}`);
      if (get(s.yesterday, i) != null) parts.push(`yesterday ${fmtInt(get(s.yesterday, i))}`);
      return parts.join(', ');
    });
    // crosshair is redundant with the slot band on bars
    root.querySelector('.crosshair')?.remove();
    f.plot.querySelector('svg')?.remove();
    f.plot.prepend(root);
    if (hadFocus) root.querySelector('.overlay')?.focus({ preventScroll: true });

    fillTable(f, 'Crashes per hour, table view', [`Hour (${zone})`, 'Today', 'Expected', 'Yesterday'],
      hours.map((h, i) => [
        hl(h) + (h === s.in_progress_hour ? ' (in progress)' : ''),
        fmtInt(get(s.today, i)), fmtInt(get(s.expected_today, i)), fmtInt(get(s.yesterday, i)),
      ]));
  }

  f.observe(render);
  render();
  return {
    update(next) { state.spec = { ...state.spec, ...next }; render(); },
    destroy() { window.removeEventListener(TZ_EVENT, onTz); f.ro?.disconnect(); container.textContent = ''; },
  };
}

// ---------------------------------------------------------------- sparkline
/** spec: { dates[], observed[], expected[], severity, partial, width, height } */
export function sparkline(container, spec) {
  const { dates = [], observed = [], expected = [], severity = 'ok', partial = true, width = 120, height = 26 } = spec;
  container.textContent = '';
  const n = dates.length;
  if (!n) return;
  const vals = [...observed, ...expected].filter((v) => v != null);
  const max = Math.max(1, ...vals);
  const x = (i) => 3 + (i * (width - 6)) / Math.max(1, n - 1);
  const y = (v) => height - 3 - (Math.min(v, max) / max) * (height - 6);
  const last = observed[n - 1];
  const label = `28 days: ${fmtInt(last)} ${partial ? 'so far ' : ''}today vs ${fmtInt(expected[n - 1])} expected`;
  const root = svg('svg', { class: 'spark', width, height, role: 'img', 'aria-label': label });
  root.append(svg('title', { text: label }));
  const exp = expected.map((v, i) => (v == null ? null : { x: x(i), y: y(v) })).filter(Boolean);
  if (exp.length > 1) {
    const d = pathFrom(exp) + `L${exp[exp.length - 1].x.toFixed(1)},${height - 3}L${exp[0].x.toFixed(1)},${height - 3}Z`;
    root.append(svg('path', { d, class: 'band3' }));
    root.append(svg('path', { d: pathFrom(exp), class: 'series-expected', 'stroke-width': 1 }));
  }
  root.append(svg('path', { class: 'series-observed', 'stroke-width': 1.5, d: pathFrom(observed.map((v, i) => (v == null ? null : { x: x(i), y: y(v) }))) }));
  if (last != null) {
    const color = SEV_COLOR[severity] || SEV_COLOR.ok;
    root.append(svg('circle', { cx: x(n - 1), cy: y(last), r: 3, fill: partial ? 'var(--surface)' : color, stroke: color, 'stroke-width': partial ? 1.5 : 0 }));
  }
  container.append(root);
}

// ---------------------------------------------------------------- factor mini charts
/** spec: { values[], labels[], highlight, kind: 'bar'|'line', width, height, ticks[] } */
export function miniFactors(container, spec) {
  const { values = [], labels = [], highlight = -1, kind = 'bar', width = 260, height = 78, ticks } = spec;
  container.textContent = '';
  container.classList.add('mini');
  const n = values.length;
  if (!n) return;
  const lo = Math.min(1, ...values) - 0.02;
  const hi = Math.max(1, ...values) + 0.02;
  const box = { left: 34, right: width - 6, top: 6, bottom: height - 16 };
  const y = makeY(lo, hi, box.top, box.bottom, false);
  const slot = (box.right - box.left) / n;
  const cx = (i) => box.left + slot * (i + 0.5);
  const label = `${kind === 'bar' ? 'Weekday' : 'Release-cycle'} factors, ${values.map((v, i) => `${labels[i] ?? i + 1}: ${v.toFixed(2)}`).join(', ')}`;
  const root = svg('svg', { width, height, role: 'img', 'aria-label': label });
  root.append(svg('title', { text: label }));
  for (const t of [lo + 0.02, 1, hi - 0.02]) {
    root.append(svg('line', { class: t === 1 ? 'baseline' : 'gridline', x1: box.left, x2: box.right, y1: y(t), y2: y(t) }));
    root.append(svg('text', { x: box.left - 5, y: y(t) + 3.5, 'text-anchor': 'end', text: `×${t.toFixed(2)}` }));
  }
  if (kind === 'bar') {
    const w = Math.min(24, slot - 4);
    for (let i = 0; i < n; i++) {
      const top = Math.min(y(1), y(values[i]));
      const h = Math.abs(y(1) - y(values[i]));
      root.append(svg('rect', { class: i === highlight ? 'bar is-today' : 'bar', x: cx(i) - w / 2, y: top, width: w, height: Math.max(1, h), rx: 2 }));
      root.append(svg('text', { x: cx(i), y: height - 3, 'text-anchor': 'middle', text: labels[i] ?? i + 1 }));
    }
  } else {
    root.append(svg('path', { class: 'series-observed', d: pathFrom(values.map((v, i) => ({ x: cx(i), y: y(v) }))) }));
    if (highlight >= 0) {
      root.append(svg('circle', { class: 'ring', cx: cx(highlight), cy: y(values[highlight]), r: 5.5 }));
      root.append(svg('circle', { cx: cx(highlight), cy: y(values[highlight]), r: 3.5, fill: 'var(--ink)' }));
    }
    for (const t of ticks || []) root.append(svg('text', { x: cx(t - 1), y: height - 3, 'text-anchor': 'middle', text: String(t) }));
  }
  container.append(root);
}
