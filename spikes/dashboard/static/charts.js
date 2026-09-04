// SVG charts for the dashboard (line, bars, sparkline, factor minis) and the
// DOM/formatting helpers shared with dashboard.js.  No dependencies.

import { iconNode, iconSvg } from "./icons.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];
export const WDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
export const DAY_MS = 86_400_000;
const HOUR_MS = 3_600_000;
const MINUS = "−";
const NBSP = " ";

const SEV_COLOR = {
  major: "var(--st-major)",
  spike: "var(--st-spike)",
  watch: "var(--st-watch)",
  drop: "var(--st-drop)",
  new: "var(--st-new)",
  ok: "var(--ink)",
};

// --------------------------------------------------------------------- helpers
function setAttrs(node, attrs) {
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) {
      continue;
    }
    if (k === "text") {
      node.textContent = v;
    } else {
      node.setAttribute(k, v === true ? "" : v);
    }
  }
}

const keep = c => c != null && c !== false && c !== "";

/** An HTML element; string children become text nodes, never markup. */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  setAttrs(node, attrs);
  node.append(...children.filter(keep));
  return node;
}

/** Replace the children of `node`; null, false and "" children are skipped. */
export function fill(node, ...children) {
  node.replaceChildren(...children.filter(keep));
}

export function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  setAttrs(node, attrs);
  return node;
}

export const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const at = (arr, i) => arr?.[i] ?? null;
const pad2 = n => String(n).padStart(2, "0");
const px = v => v.toFixed(1);

// ------------------------------------------------------------------ formatting
export function fmtInt(n) {
  return n == null || Number.isNaN(n)
    ? "—"
    : Math.round(n).toLocaleString("en-US");
}

export function fmtCompact(n) {
  if (n == null || Number.isNaN(n)) {
    return "—";
  }
  const a = Math.abs(n);
  const sign = n < 0 ? MINUS : "";
  if (a >= 1e6) {
    return `${sign}${(a / 1e6).toFixed(a >= 1e7 ? 0 : 1)}M`;
  }
  if (a >= 1e3) {
    return `${sign}${(a / 1e3).toFixed(a >= 1e5 ? 0 : 1)}k`;
  }
  return (
    sign +
    (a >= 10 || Number.isInteger(a) ? String(Math.round(a)) : a.toFixed(1))
  );
}

export function fmtSigned(n, compact = false) {
  if (n == null) {
    return "—";
  }
  const body = compact ? fmtCompact(Math.abs(n)) : fmtInt(Math.abs(n));
  return (n < 0 ? MINUS : "+") + body;
}

/** Signed percent while |ratio − 1| < 1, multiplicative beyond ("×2.4"). */
export function fmtRatio(r) {
  if (r == null || !Number.isFinite(r)) {
    return "";
  }
  const d = r - 1;
  if (Math.abs(d) < 1) {
    return `${d < 0 ? MINUS : "+"}${Math.round(Math.abs(d) * 100)}${NBSP}%`;
  }
  return `×${r >= 10 ? Math.round(r) : r.toFixed(1)}`;
}

export function fmtZ(z) {
  return z == null ? "—" : (z < 0 ? MINUS : "") + Math.abs(z).toFixed(1);
}

/** UTC midnight of a "YYYY-MM-DD" day, in ms. */
export function parseDay(s) {
  const [y, m, d] = s.split("-").map(Number);
  return Date.UTC(y, m - 1, d);
}

export function fmtDate(ms, withYear = false) {
  const d = new Date(ms);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}${withYear ? ` ${d.getUTCFullYear()}` : ""}`;
}

export function fmtDateLong(ms) {
  return `${WDAYS[new Date(ms).getUTCDay()]} ${fmtDate(ms, true)}`;
}

// ------------------------------------------------------------ scales and ticks
function niceStep(range, count) {
  const raw = range / Math.max(1, count);
  const p = 10 ** Math.floor(Math.log10(raw));
  return ([1, 2, 2.5, 5].find(m => raw / p <= m) ?? 10) * p;
}

function linearTicks(lo, hi, count) {
  const step = niceStep(hi - lo || 1, count);
  const out = [];
  for (
    let v = Math.ceil(lo / step - 1e-9) * step;
    v <= hi + step * 1e-6;
    v += step
  ) {
    out.push(Math.round(v * 1e6) / 1e6);
  }
  return out;
}

function logTicks(lo, hi) {
  const out = [];
  const p0 = Math.floor(Math.log10(lo));
  const p1 = Math.ceil(Math.log10(hi));
  const mantissas = p1 - p0 <= 2 ? [1, 2, 5] : [1];
  for (let p = p0; p <= p1; p++) {
    for (const m of mantissas) {
      const v = m * 10 ** p;
      if (v >= lo && v <= hi) {
        out.push(v);
      }
    }
  }
  return out;
}

/** Date axis ticks: Mondays up to 45 days, else month starts (and mid-months
 * up to 130 days), thinned to what fits in `width`. */
function dateTicks(xs, width) {
  if (!xs.length) {
    return [];
  }
  const first = xs[0];
  const last = xs.at(-1);
  const days = (last - first) / DAY_MS;
  const maxTicks = Math.max(2, Math.floor(width / 64));
  let ticks = [];
  if (days <= 45) {
    for (let t = first; t <= last; t += DAY_MS) {
      if (new Date(t).getUTCDay() === 1) {
        ticks.push({ ms: t, label: fmtDate(t) });
      }
    }
    if (ticks.length < 2) {
      ticks = xs.map(ms => ({ ms, label: fmtDate(ms) }));
    }
  } else {
    const d = new Date(first);
    let y = d.getUTCFullYear();
    let m = d.getUTCMonth();
    for (;;) {
      m += 1;
      if (m > 11) {
        m = 0;
        y += 1;
      }
      const t = Date.UTC(y, m, 1);
      if (t > last) {
        break;
      }
      ticks.push({ ms: t, label: m === 0 ? `Jan ${y}` : MONTHS[m] });
      const mid = Date.UTC(y, m, 15);
      if (days <= 130 && mid <= last) {
        ticks.push({ ms: mid, label: fmtDate(mid) });
      }
    }
    ticks.sort((a, b) => a.ms - b.ms);
  }
  const stride = Math.ceil(ticks.length / maxTicks);
  ticks = ticks.filter((_, i) => i % stride === 0);
  if (days > 200 && ticks.length && !ticks.some(t => /\d{4}/.test(t.label))) {
    ticks[0].label += ` ${new Date(ticks[0].ms).getUTCFullYear()}`;
  }
  return ticks;
}

function makeX(d0, d1, r0, r1) {
  if (d1 === d0) {
    return () => (r0 + r1) / 2;
  }
  const k = (r1 - r0) / (d1 - d0);
  return v => r0 + (v - d0) * k;
}

function makeY(lo, hi, top, bottom, log) {
  if (log) {
    const l0 = Math.log10(lo);
    const l1 = Math.log10(hi);
    return v =>
      bottom -
      ((Math.log10(Math.max(v, lo)) - l0) / (l1 - l0 || 1)) * (bottom - top);
  }
  return v => bottom - ((v - lo) / (hi - lo || 1)) * (bottom - top);
}

function nearestIndex(pxs, x) {
  let best = 0;
  for (let i = 1; i < pxs.length; i++) {
    if (Math.abs(pxs[i] - x) < Math.abs(pxs[best] - x)) {
      best = i;
    }
  }
  return best;
}

/** SVG path through the points; a null point lifts the pen. */
function pathFrom(points) {
  let d = "";
  let pen = false;
  for (const p of points) {
    if (!p) {
      pen = false;
      continue;
    }
    d += `${pen ? "L" : "M"}${px(p.x)},${px(p.y)}`;
    pen = true;
  }
  return d;
}

/** Closed area between two y series, split where either is null. */
function areaPath(pxs, hiPy, loPy) {
  let d = "";
  let run = [];
  const flush = () => {
    if (run.length >= 2) {
      d += `M${run.map(i => `${px(pxs[i])},${px(hiPy[i])}`).join("L")}`;
      d += `L${run
        .reverse()
        .map(i => `${px(pxs[i])},${px(loPy[i])}`)
        .join("L")}Z`;
    }
    run = [];
  };
  for (let i = 0; i < pxs.length; i++) {
    if (hiPy[i] == null || loPy[i] == null) {
      flush();
    } else {
      run.push(i);
    }
  }
  flush();
  return d;
}

function roundedBar(x, y, w, h, r) {
  if (h <= 0.5) {
    return "";
  }
  const rr = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + rr}a${rr},${rr} 0 0 1 ${rr},${-rr}h${w - 2 * rr}a${rr},${rr} 0 0 1 ${rr},${rr}V${y + h}Z`;
}

// ----------------------------------------------------------------- chart frame
/** Toolbar (legend + buttons), plot area with tooltip, and a table view of
 * the data that is only built when shown. */
function frame(container, { legend = [], buttons = [] }) {
  container.classList.add("chart");
  const legendEl = el(
    "div",
    { class: "chart-legend" },
    ...legend.map(item =>
      el(
        "span",
        { class: "lg" },
        el("i", {
          class: `lg-key${item.kind ? ` lg-${item.kind}` : ""}`,
          style: `--c:${item.color}`,
        }),
        item.label
      )
    )
  );
  const actions = el("div", { class: "chart-actions" });
  const tip = el("div", { class: "chart-tooltip", hidden: true });
  const plot = el("div", { class: "chart-plot" }, tip);
  const table = el("div", {
    class: "chart-table",
    hidden: true,
    tabindex: 0,
    role: "region",
    "aria-label": "Chart data as a table",
  });
  container.replaceChildren(
    el("div", { class: "chart-toolbar" }, legendEl, actions),
    plot,
    table
  );

  const f = { container, plot, tip, buttons: {} };
  for (const b of buttons) {
    const btn = el(
      "button",
      { type: "button", class: "chart-btn", hidden: b.hidden },
      b.label
    );
    if (b.onToggle) {
      btn.setAttribute("aria-pressed", "false");
      btn.addEventListener("click", () => {
        const on = btn.getAttribute("aria-pressed") !== "true";
        btn.setAttribute("aria-pressed", String(on));
        b.onToggle(on);
      });
    } else {
      btn.addEventListener("click", b.onClick);
    }
    actions.append(btn);
    f.buttons[b.key] = btn;
  }

  /** Replace the drawing with a new svg, or with a message when empty. */
  f.show = content => {
    plot.querySelector("svg, .chart-empty")?.remove();
    plot.prepend(
      typeof content === "string"
        ? el("div", { class: "chart-empty" }, content)
        : content
    );
  };

  let pendingTable = null;
  const buildTable = () => {
    const { caption, columns, rows } = pendingTable;
    pendingTable = null;
    table.replaceChildren(
      el(
        "table",
        {},
        el("caption", { class: "visually-hidden" }, caption),
        el(
          "thead",
          {},
          el("tr", {}, ...columns.map(c => el("th", { scope: "col" }, c)))
        ),
        el(
          "tbody",
          {},
          ...rows().map(r => el("tr", {}, ...r.map(v => el("td", {}, v))))
        )
      )
    );
  };
  /** rows: () => string[][], called when the table is (or becomes) visible. */
  f.setTable = (caption, columns, rows) => {
    pendingTable = { caption, columns, rows };
    if (!table.hidden) {
      buildTable();
    }
  };
  f.showTable = on => {
    table.hidden = !on;
    plot.hidden = on;
    if (on && pendingTable) {
      buildTable();
    }
  };

  f.observe = render => {
    let lastWidth = 0;
    f.ro = new ResizeObserver(() => {
      const w = container.clientWidth;
      if (w > 0 && w !== lastWidth) {
        lastWidth = w;
        render();
      }
    });
    f.ro.observe(container);
  };
  f.destroy = () => {
    f.ro?.disconnect();
    container.textContent = "";
  };
  return f;
}

const TIP_KEY_CLASS = {
  rect: "tt-key tt-rect",
  dot: "tt-key tt-dot",
  dash: "tt-key tt-dash",
};

/** rows: [{ value, label, color, kind: 'rect'|'dot'|'dash'|'icon', source }] */
function showTip(f, x, y, title, rows) {
  const { tip, plot } = f;
  tip.replaceChildren(
    el("div", { class: "tt-title" }, title),
    ...rows.map(r =>
      el(
        "div",
        { class: "tt-row" },
        r.kind === "icon"
          ? iconSvg(r.source, 12)
          : el("i", {
              class: TIP_KEY_CLASS[r.kind] || "tt-key",
              style: `--c:${r.color || "transparent"}`,
            }),
        el("span", { class: "tt-val" }, r.value),
        el("span", { class: "tt-label" }, r.label)
      )
    )
  );
  tip.hidden = false;
  const tw = tip.offsetWidth;
  const th = tip.offsetHeight;
  const left = x + 14 + tw > plot.clientWidth ? x - tw - 14 : x + 14;
  tip.style.left = `${Math.max(0, left)}px`;
  tip.style.top = `${clamp(y - th / 2, 0, plot.clientHeight - th)}px`;
}

function hideTip(f) {
  f.tip.hidden = true;
}

/**
 * Pointer and keyboard reader over the plot, snapping to the nearest x.
 * onIndex(i | null) follows the pointer or the arrow keys; with onBrush, a
 * drag or Shift+arrows select a range (onBrush(a, b)), double-click,
 * Backspace or Delete reset it (onBrush(null)).  describe(i) is the point's
 * text for assistive technology.
 */
function hoverLayer(
  root,
  box,
  pxs,
  { onIndex, onBrush, describe, crosshair = true }
) {
  const last = pxs.length - 1;
  const height = Math.max(0, box.bottom - box.top);
  const cross = svg("line", {
    class: "crosshair",
    y1: box.top,
    y2: box.bottom,
    visibility: "hidden",
  });
  const brush = svg("rect", {
    class: "brush",
    y: box.top,
    height,
    visibility: "hidden",
  });
  const overlay = svg("rect", {
    class: "overlay",
    x: box.left,
    y: box.top,
    width: Math.max(0, box.right - box.left),
    height,
    tabindex: 0,
    role: "slider",
    "aria-orientation": "horizontal",
    "aria-valuemin": 0,
    "aria-valuemax": Math.max(0, last),
    "aria-valuenow": Math.max(0, last),
    "aria-valuetext": describe && last >= 0 ? describe(last) : null,
    "aria-label": onBrush
      ? "Chart reader: arrow keys move between points, Escape closes; Shift+arrows select a range and Enter zooms on it, Backspace resets the zoom"
      : "Chart reader: arrow keys move between points, Escape closes",
  });

  let index = -1;
  const set = i => {
    index = i ?? -1;
    if (index < 0) {
      cross.setAttribute("visibility", "hidden");
      onIndex(null);
      return;
    }
    cross.setAttribute("x1", pxs[i]);
    cross.setAttribute("x2", pxs[i]);
    cross.setAttribute("visibility", crosshair ? "visible" : "hidden");
    overlay.setAttribute("aria-valuenow", i);
    if (describe) {
      overlay.setAttribute("aria-valuetext", describe(i));
    }
    onIndex(i);
  };
  const localX = e => e.clientX - root.getBoundingClientRect().left;

  let drag = null; // { x0, x1 } while the pointer is down
  let anchor = null; // start of a keyboard selection
  const showBrush = (x0, x1) => {
    const lo = Math.max(box.left, Math.min(x0, x1));
    const hi = Math.min(box.right, Math.max(x0, x1));
    brush.setAttribute("x", lo);
    brush.setAttribute("width", Math.max(0, hi - lo));
    brush.setAttribute("visibility", "visible");
  };
  const clearBrush = () => {
    anchor = null;
    brush.setAttribute("visibility", "hidden");
  };

  overlay.addEventListener("pointermove", e => {
    if (!drag) {
      set(nearestIndex(pxs, localX(e)));
      return;
    }
    drag.x1 = localX(e);
    if (Math.abs(drag.x1 - drag.x0) >= 6) {
      showBrush(drag.x0, drag.x1);
      set(null);
    }
  });
  overlay.addEventListener("pointerleave", () => {
    if (!drag) {
      set(null);
    }
  });
  overlay.addEventListener("focus", () => set(index >= 0 ? index : last));
  overlay.addEventListener("blur", () => {
    set(null);
    clearBrush();
  });
  if (onBrush) {
    overlay.addEventListener("pointerdown", e => {
      if (e.button !== 0) {
        return;
      }
      drag = { x0: localX(e), x1: localX(e) };
      overlay.setPointerCapture(e.pointerId);
    });
    overlay.addEventListener("pointerup", e => {
      if (!drag) {
        return;
      }
      const { x0, x1 } = drag;
      drag = null;
      clearBrush();
      overlay.releasePointerCapture(e.pointerId);
      if (Math.abs(x1 - x0) < 6) {
        return;
      }
      const a = nearestIndex(pxs, Math.min(x0, x1));
      const b = nearestIndex(pxs, Math.max(x0, x1));
      if (b > a) {
        onBrush(a, b);
      }
    });
    overlay.addEventListener("dblclick", () => onBrush(null));
  }
  overlay.addEventListener("keydown", e => {
    const cur = index >= 0 ? index : last;
    const step = { ArrowLeft: -1, ArrowRight: 1 }[e.key];
    if (onBrush && e.shiftKey && step) {
      e.preventDefault();
      anchor ??= cur;
      const next = clamp(cur + step, 0, last);
      set(next);
      showBrush(
        pxs[Math.min(anchor, next)] - 3,
        pxs[Math.max(anchor, next)] + 3
      );
      return;
    }
    if (onBrush && e.key === "Enter" && anchor != null && anchor !== cur) {
      e.preventDefault();
      const range = [Math.min(anchor, cur), Math.max(anchor, cur)];
      clearBrush();
      onBrush(...range);
      return;
    }
    if (onBrush && (e.key === "Backspace" || e.key === "Delete")) {
      e.preventDefault();
      clearBrush();
      onBrush(null);
      return;
    }
    if (e.key === "Escape") {
      if (anchor == null) {
        set(null);
      }
      clearBrush();
      return;
    }
    const to = { ArrowLeft: cur - 1, ArrowRight: cur + 1, Home: 0, End: last }[
      e.key
    ];
    if (to == null) {
      return;
    }
    e.preventDefault();
    if (!e.shiftKey) {
      clearBrush();
    }
    set(clamp(to, 0, last));
  });
  root.append(brush, cross, overlay);
}

function yAxis(root, ticks, y, left, right, format) {
  for (const t of ticks) {
    const py = y(t);
    root.append(
      svg("line", { class: "gridline", x1: left, x2: right, y1: py, y2: py }),
      svg("text", {
        x: left - 6,
        y: py + 3.5,
        "text-anchor": "end",
        text: format(t),
      })
    );
  }
}

/** Width of the y axis labels for these ticks. */
function axisWidth(ticks) {
  return 10 + Math.max(...ticks.map(t => fmtInt(t).length)) * 6.6;
}

/** Whether keyboard focus is on the chart reader (a re-render drops it). */
function readerFocused(f) {
  return (
    f.plot.contains(document.activeElement) &&
    document.activeElement.classList.contains("overlay")
  );
}

// ------------------------------------------------------- platform event badges
// /dashboard/api/events groups events per (day, source):
// { day, source, platform, label, items: [{ title, detail, url, search, at }] }
// A badge is one day's sources stacked in the strip above the plot; its
// tooltip lists the items.
const BADGE = 14;
const BADGE_GAP = 2;
const MAX_BADGE_ROWS = 5;
const SOURCE_ORDER = [
  "windows",
  "nvidia",
  "amd",
  "intel",
  "antivirus",
  "apple",
  "linux",
  "android",
];

function eventRows(groups, withDay = false) {
  return groups.flatMap(g =>
    g.items.map(it => ({
      value: it.title,
      label: [g.label, withDay ? fmtDate(parseDay(g.day)) : null, it.detail]
        .filter(Boolean)
        .join(" · "),
      kind: "icon",
      source: g.source,
    }))
  );
}

function eventText(groups) {
  return groups
    .flatMap(g => g.items.map(it => `${g.label}: ${it.title}`))
    .join("; ");
}

/** Bucket index (day or week) of each event group: Map index -> groups. */
function bucketEvents(events, xs, weekly) {
  const map = new Map();
  if (!events?.length || !xs.length) {
    return map;
  }
  const first = xs[0];
  const last = xs.at(-1) + (weekly ? 6 * DAY_MS : 0);
  for (const g of events) {
    const ms = parseDay(g.day);
    if (ms < first || ms > last) {
      continue;
    }
    let i;
    if (weekly) {
      i = xs.findLastIndex(x => x <= ms);
    } else {
      i = Math.round((ms - first) / DAY_MS);
      if (xs[i] !== ms) {
        i = xs.indexOf(ms);
      }
    }
    if (i >= 0) {
      if (!map.has(i)) {
        map.set(i, []);
      }
      map.get(i).push(g);
    }
  }
  return map;
}

/**
 * Badge columns: one per position, sources stacked; columns closer than one
 * badge are merged and show each source once.
 * positions: [{ px, title, groups, dim, rule, withDay }] sorted by px.
 * Returns [{ members, sources, x0 }].
 */
function layoutEventBadges(left, right, positions) {
  const place = c => {
    c.sources = [
      ...new Set(c.members.flatMap(p => p.groups.map(g => g.source))),
    ]
      .sort((a, b) => SOURCE_ORDER.indexOf(a) - SOURCE_ORDER.indexOf(b))
      .slice(0, MAX_BADGE_ROWS);
    const mid = (c.members[0].px + c.members.at(-1).px) / 2;
    c.x0 = clamp(mid - BADGE / 2, left, right - BADGE);
  };
  let clusters = positions.map(p => ({ members: [p] }));
  clusters.forEach(place);
  // a merged column moves and may touch the next one: repeat until stable
  let merged = true;
  while (merged && clusters.length > 1) {
    merged = false;
    const next = [clusters[0]];
    for (const c of clusters.slice(1)) {
      const prev = next.at(-1);
      if (c.x0 < prev.x0 + BADGE + 3) {
        prev.members.push(...c.members);
        place(prev);
        merged = true;
      } else {
        next.push(c);
      }
    }
    clusters = next;
  }
  return clusters;
}

/** Height of the badge strip (0 without badges). */
function stripHeight(clusters) {
  const rows = Math.max(0, ...clusters.map(c => c.sources.length));
  return rows ? rows * (BADGE + BADGE_GAP) + 6 : 0;
}

/** Badges above the plot with a dashed rule per day; hover or focus lists
 * the events, a click opens the first item's crash-stats search or notes. */
function drawEventBadges(root, f, box, clusters) {
  const floor = stripHeight(clusters) - 3;
  for (const c of clusters) {
    const groups = c.members.flatMap(p => p.groups);
    const multi = c.members.length > 1;
    const days = groups.map(g => parseDay(g.day));
    const title = multi
      ? `${fmtDate(Math.min(...days))} – ${fmtDate(Math.max(...days), true)}`
      : c.members[0].title;
    const withDay = multi || c.members.some(p => p.withDay);
    const h = c.sources.length * (BADGE + BADGE_GAP) - BADGE_GAP;
    const y0 = floor - h;
    const g = svg("g", {
      class: `event-badge${c.members.every(p => p.dim) ? " is-dim" : ""}`,
      tabindex: 0,
      role: "img",
      "aria-label": `${title}: ${eventText(groups)}`,
    });
    g.append(
      svg("rect", {
        class: "hit",
        x: c.x0 - 2,
        y: y0 - 2,
        width: BADGE + 4,
        height: h + 4,
        rx: 3,
      }),
      ...c.sources.map((src, k) =>
        iconNode(src, BADGE, c.x0, y0 + k * (BADGE + BADGE_GAP))
      )
    );
    for (const p of c.members) {
      if (p.rule !== false) {
        root.append(
          svg("line", {
            class: "event-rule",
            x1: p.px,
            x2: p.px,
            y1: box.top,
            y2: box.bottom,
          })
        );
      }
    }
    const show = () =>
      showTip(
        f,
        c.x0 + BADGE / 2,
        box.top + 12,
        title,
        eventRows(groups, withDay)
      );
    const hide = () => hideTip(f);
    g.addEventListener("pointerenter", show);
    g.addEventListener("pointerleave", hide);
    g.addEventListener("focus", show);
    g.addEventListener("blur", hide);
    const linked = groups.flatMap(x => x.items).find(it => it.search || it.url);
    if (linked) {
      g.classList.add("has-link");
      const open = () =>
        window.open(linked.search || linked.url, "_blank", "noopener");
      g.addEventListener("click", open);
      g.addEventListener("keydown", e => {
        if (e.key === "Enter") {
          open();
        }
      });
    }
    root.append(g);
  }
}

// ------------------------------------------------------------------ line chart
/**
 * spec: { dates[], granularity, observed[], expected[], lo3[], hi3[], lo5[],
 *   hi5[], z[], partial[], future[], projected[], severity[],
 *   releases[{ date, version, upcoming }], events[], height, label, ariaLabel }
 */
const SLICED = [
  "dates",
  "observed",
  "expected",
  "lo3",
  "hi3",
  "lo5",
  "hi5",
  "z",
  "partial",
  "future",
  "projected",
  "severity",
];

function sliceSpec(spec, a, b) {
  const out = { ...spec };
  for (const k of SLICED) {
    if (Array.isArray(spec[k])) {
      out[k] = spec[k].slice(a, b + 1);
    }
  }
  return out;
}

/** Clipped points: an arrow at the top edge and the value, laid out on up to
 * two rows without overlaps. */
function drawClips(root, box, clips) {
  const placed = [];
  for (const c of clips) {
    const g = svg("g", { opacity: c.faint ? 0.5 : 1 });
    g.append(
      svg("path", { d: `M${c.px},${box.top - 1}l-4.5,7h9z`, fill: c.color })
    );
    const text = fmtCompact(c.value);
    const w = text.length * 6.2 + 2;
    const sides = [
      ["start", c.px + 7],
      ["end", c.px - 7],
    ];
    if (c.px + 7 + w > box.right) {
      sides.reverse();
    }
    const candidates = [0, 1].flatMap(row =>
      sides.map(([anchor, tx]) => [anchor, tx, row])
    );
    for (const [anchor, tx, row] of candidates) {
      const l = anchor === "start" ? tx : tx - w;
      const r = anchor === "start" ? tx + w : tx;
      if (l < box.left - 4 || r > box.right + 6) {
        continue;
      }
      if (placed.some(([a, b, rw]) => rw === row && l < b + 3 && r > a - 3)) {
        continue;
      }
      g.append(
        svg("text", {
          class: "clip-label",
          x: tx,
          y: box.top + 6 + row * 11,
          "text-anchor": anchor,
          text,
        })
      );
      placed.push([l, r, row]);
      break;
    }
    root.append(g);
  }
}

export function lineChart(container, spec) {
  const state = { spec, log: false, zoom: null }; // zoom: [first, last] indices, or null
  const f = frame(container, {
    legend: [
      { label: spec.label || "Observed", color: "var(--ink)" },
      { label: "Expected", color: "var(--expected)", kind: "dash" },
      { label: "±3 band (watch)", color: "var(--band3)", kind: "rect" },
      { label: "±5 band (spike)", color: "var(--band5)", kind: "rect" },
      { label: "Version", color: "var(--axis)", kind: "rule" },
      {
        label: "Forecast to the next version",
        color: "var(--forecast-key)",
        kind: "rect",
      },
    ],
    buttons: [
      {
        key: "zoom",
        label: "Reset zoom",
        hidden: true,
        onClick: () => setZoom(null),
      },
      {
        key: "log",
        label: "Log scale",
        onToggle: on => {
          state.log = on;
          render();
        },
      },
      { key: "table", label: "Table", onToggle: on => f.showTable(on) },
    ],
  });

  function setZoom(range) {
    state.zoom = range;
    f.buttons.zoom.hidden = !range;
    render();
  }

  function render() {
    const hadFocus = readerFocused(f);
    const s = state.zoom ? sliceSpec(state.spec, ...state.zoom) : state.spec;
    const width = f.container.clientWidth;
    if (width < 40) {
      return;
    }
    const height = s.height || 260;
    const n = s.dates?.length || 0;
    if (!n) {
      f.show("No daily history for this series yet");
      return;
    }
    const weekly = s.granularity === "week";
    const xs = s.dates.map(parseDay);
    const eventMap = bucketEvents(s.events, xs, weekly);

    // y domain; above the linear scale's clip, points become arrows so one
    // huge day cannot flatten the rest
    const bandTop = Math.max(
      1,
      ...s.hi5.filter(v => v != null),
      ...s.expected.filter(v => v != null)
    );
    const clipAt = 3 * bandTop;
    const all = [];
    for (let i = 0; i < n; i++) {
      const proj = s.partial?.[i] ? at(s.projected, i) : null;
      all.push(
        ...[at(s.observed, i), at(s.expected, i), at(s.hi5, i), proj].filter(
          v => v != null
        )
      );
    }
    const isClipped = v => !state.log && v != null && v > clipAt;
    let yMin = 0;
    let yMax;
    if (state.log) {
      const pos = all.filter(v => v > 0);
      yMin =
        10 **
        Math.floor(
          Math.log10(
            Math.max(0.5, Math.min(...pos, ...s.lo5.filter(v => v > 0)))
          )
        );
      yMax = Math.max(...pos, yMin * 10) * 1.3;
    } else {
      yMax = 1.1 * Math.max(bandTop, ...all.filter(v => v <= clipAt));
    }
    const yTicks = state.log
      ? logTicks(yMin, yMax)
      : linearTicks(0, yMax, Math.max(3, Math.floor((height - 46) / 44)));
    const left = axisWidth(yTicks);
    const right = width - 16;
    const x = makeX(xs[0], xs[n - 1], left + 6, right - 6);
    const pxs = xs.map(x);

    function bucketTitle(i) {
      const title = weekly
        ? `Week of ${fmtDate(xs[i], true)}`
        : fmtDateLong(xs[i]);
      return s.future?.[i] ? `${title} · forecast` : title;
    }

    // the badge strip above the plot grows with its tallest column, and the
    // svg with it, so the plot keeps its height
    const badgeClusters = layoutEventBadges(
      left,
      right,
      [...eventMap]
        .sort((a, b) => a[0] - b[0])
        .map(([i, groups]) => ({
          px: pxs[i],
          title: bucketTitle(i),
          groups,
          withDay: weekly,
        }))
    );
    const strip = stripHeight(badgeClusters);
    const svgHeight = height + strip;
    const box = { left, right, top: 22 + strip, bottom: svgHeight - 24 };
    const y = makeY(yMin, yMax, box.top, box.bottom, state.log);
    const yc = v => Math.max(box.top, y(v));
    const point = (v, i) => (v == null ? null : { x: pxs[i], y: yc(v) });

    // a group, not an image: it contains the keyboard-operable chart reader
    const root = svg("svg", {
      width,
      height: svgHeight,
      role: "group",
      "aria-label": s.ariaLabel || "Daily crashes against the expected band",
    });
    yAxis(root, yTicks, y, box.left, box.right, fmtInt);
    root.append(
      svg("line", {
        class: "baseline",
        x1: box.left,
        x2: box.right,
        y1: box.bottom,
        y2: box.bottom,
      })
    );
    for (const t of dateTicks(xs, box.right - box.left)) {
      const tx = x(t.ms);
      if (tx >= box.left && tx <= box.right) {
        root.append(
          svg("text", {
            x: tx,
            y: box.bottom + 16,
            "text-anchor": "middle",
            text: t.label,
          })
        );
      }
    }

    // bands (outer first) and the expected path; past today they continue
    // as a fainter forecast segment over a shaded zone
    const py = arr => arr.map((v, i) => (v == null ? null : yc(v)));
    const segment = (arr, from, to) =>
      (arr || []).map((v, i) => (i >= from && i <= to ? v : null));
    const drawExpected = (from, to, cls) => {
      root.append(
        svg("path", {
          class: `band5${cls}`,
          d: areaPath(
            pxs,
            py(segment(s.hi5, from, to)),
            py(segment(s.lo5, from, to))
          ),
        }),
        svg("path", {
          class: `band3${cls}`,
          d: areaPath(
            pxs,
            py(segment(s.hi3, from, to)),
            py(segment(s.lo3, from, to))
          ),
        }),
        svg("path", {
          class: `series-expected${cls}`,
          d: pathFrom(segment(s.expected, from, to).map(point)),
        })
      );
    };
    const firstFuture = s.future ? s.future.findIndex(Boolean) : -1;
    if (firstFuture > 0) {
      const edge = (pxs[firstFuture - 1] + pxs[firstFuture]) / 2;
      root.append(
        svg("rect", {
          class: "forecast-zone",
          x: edge,
          y: box.top,
          width: Math.max(0, box.right - edge),
          height: box.bottom - box.top,
        })
      );
      drawExpected(0, firstFuture - 1, "");
      drawExpected(firstFuture - 1, n - 1, " future");
    } else {
      drawExpected(0, n - 1, firstFuture === 0 ? " future" : "");
    }

    // version rules; labels skip when they would overlap the previous one
    const pad = weekly ? 6 * DAY_MS : 0;
    let lastLabelRight = -Infinity;
    for (const r of s.releases || []) {
      const ms = parseDay(r.date);
      if (ms < xs[0] - pad || ms > xs[n - 1] + pad) {
        continue;
      }
      const rx = x(clamp(ms, xs[0], xs[n - 1]));
      root.append(
        svg("line", {
          class: r.upcoming ? "rule upcoming" : "rule",
          x1: rx,
          x2: rx,
          y1: box.top,
          y2: box.bottom,
        })
      );
      const w = r.version.length * 6 + 6;
      if (rx - w / 2 > lastLabelRight) {
        root.append(
          svg("text", {
            class: "lbl",
            x: rx,
            y: box.top - 8,
            "text-anchor": "middle",
            text: r.version,
          })
        );
        lastLabelRight = rx + w / 2;
      }
    }

    drawEventBadges(root, f, box, badgeClusters);
    root.append(
      svg("path", {
        class: "series-observed",
        d: pathFrom(s.observed.map(point)),
      })
    );

    // markers: partial bucket (hollow, with its projection), out-of-band
    // points (filled) and clipped points (arrows)
    const clips = [];
    for (let i = 0; i < n; i++) {
      const obs = at(s.observed, i);
      if (obs == null) {
        continue;
      }
      const sev = s.severity?.[i] || "ok";
      const color = SEV_COLOR[sev] || SEV_COLOR.ok;
      const cx = pxs[i];
      if (isClipped(obs)) {
        clips.push({ px: cx, color, value: obs });
        continue;
      }
      if (s.partial?.[i]) {
        const proj = at(s.projected, i);
        if (proj != null) {
          const pyProj = yc(proj);
          root.append(
            svg("line", {
              class: "extension",
              x1: cx,
              x2: cx,
              y1: y(obs),
              y2: pyProj,
              stroke: color,
            })
          );
          if (isClipped(proj)) {
            clips.push({ px: cx, color, value: proj, faint: true });
          } else {
            root.append(
              svg("line", {
                class: "extension",
                x1: cx - 5,
                x2: cx + 5,
                y1: pyProj,
                y2: pyProj,
                stroke: color,
              })
            );
          }
        }
        root.append(
          svg("circle", {
            class: "hollow",
            cx,
            cy: y(obs),
            r: 4.5,
            stroke: color,
          })
        );
      } else if (sev !== "ok") {
        // the ink outline keeps 3:1 against the surface whatever the colour
        root.append(
          svg("circle", { class: "ring", cx, cy: y(obs), r: 6 }),
          svg("circle", {
            cx,
            cy: y(obs),
            r: 4,
            fill: color,
            stroke: "var(--ink)",
            "stroke-width": 1,
          })
        );
      }
    }
    drawClips(root, box, clips);

    function tipRows(i) {
      const partial = !!s.partial?.[i];
      const future = !!s.future?.[i];
      const sev = s.severity?.[i] || "ok";
      const rows = [];
      if (!future) {
        rows.push({
          value: fmtInt(at(s.observed, i)),
          label: partial ? "observed so far" : "observed",
          color: "var(--ink)",
        });
      }
      if (partial && at(s.projected, i) != null) {
        rows.push({
          value: fmtInt(s.projected[i]),
          label: "projected",
          color: "var(--ink)",
          kind: "dot",
        });
      }
      rows.push({
        value: fmtInt(at(s.expected, i)),
        label: future ? "expected (forecast)" : "expected",
        color: "var(--expected)",
        kind: "dash",
      });
      if (at(s.lo3, i) != null) {
        rows.push({
          value: `${fmtInt(s.lo3[i])} – ${fmtInt(s.hi3[i])}`,
          label: "±3 band",
          color: "var(--band3)",
          kind: "rect",
        });
      }
      if (at(s.lo5, i) != null) {
        rows.push({
          value: `${fmtInt(s.lo5[i])} – ${fmtInt(s.hi5[i])}`,
          label: "±5 band",
          color: "var(--band5)",
          kind: "rect",
        });
      }
      if (at(s.z, i) != null) {
        rows.push({
          value: `z ${fmtZ(s.z[i])}`,
          label: sev === "ok" ? "within band" : sev,
          color: sev === "ok" ? "var(--axis)" : SEV_COLOR[sev],
          kind: "dot",
        });
      }
      rows.push(...eventRows(eventMap.get(i) || [], weekly));
      return rows;
    }

    const focus = svg("circle", {
      r: 5,
      fill: "none",
      stroke: "var(--ink)",
      "stroke-width": 1.5,
      visibility: "hidden",
    });
    root.append(focus);
    hoverLayer(root, box, pxs, {
      onIndex: i => {
        if (i == null) {
          hideTip(f);
          focus.setAttribute("visibility", "hidden");
          return;
        }
        const obs = at(s.observed, i);
        if (obs == null) {
          focus.setAttribute("visibility", "hidden");
        } else {
          focus.setAttribute("cx", pxs[i]);
          focus.setAttribute("cy", yc(obs));
          focus.setAttribute("visibility", "visible");
        }
        showTip(
          f,
          pxs[i],
          obs == null ? (box.top + box.bottom) / 2 : yc(obs),
          bucketTitle(i),
          tipRows(i)
        );
      },
      onBrush: (a, b) => {
        if (a == null) {
          setZoom(null);
          return;
        }
        const base = state.zoom ? state.zoom[0] : 0;
        if (b > a) {
          setZoom([base + a, base + b]);
        }
      },
      describe: i =>
        `${bucketTitle(i)}: ${tipRows(i)
          .map(r => `${r.label} ${r.value}`)
          .join(", ")}`,
    });
    f.show(root);
    if (hadFocus) {
      root.querySelector(".overlay")?.focus({ preventScroll: true });
    }

    const range = (lo, hi, i) =>
      at(lo, i) == null ? "—" : `${fmtInt(lo[i])} – ${fmtInt(hi[i])}`;
    f.setTable(
      "Daily crashes, table view",
      [
        weekly ? "Week" : "Day",
        "Observed",
        "Expected",
        "±3 band",
        "±5 band",
        "z",
        "Severity",
        "Platform events",
      ],
      () =>
        xs.map((_, i) => {
          const partial = !!s.partial?.[i];
          const proj =
            partial && at(s.projected, i) != null
              ? ` → ${fmtInt(s.projected[i])}`
              : "";
          return [
            bucketTitle(i) + (partial ? " (in progress)" : ""),
            s.future?.[i] ? "—" : fmtInt(at(s.observed, i)) + proj,
            fmtInt(at(s.expected, i)),
            range(s.lo3, s.hi3, i),
            range(s.lo5, s.hi5, i),
            fmtZ(at(s.z, i)),
            s.severity?.[i] || "ok",
            eventText(eventMap.get(i) || []) || "—",
          ];
        })
    );
  }

  f.observe(render);
  render();
  return {
    /** New data; a zoomed date range is kept when it still exists. */
    update(next) {
      const zoomDates = state.zoom
        ? state.zoom.map(i => state.spec.dates[i])
        : null;
      state.spec = { ...state.spec, ...next };
      if (!zoomDates) {
        render();
        return;
      }
      const [a, b] = zoomDates.map(d => state.spec.dates.indexOf(d));
      setZoom(a >= 0 && b > a ? [a, b] : null);
    },
    destroy: f.destroy,
  };
}

// -------------------------------------------------------- time zone preference
// Hour buckets are Socorro's UTC hours; intraday charts can label them in the
// browser's local time.  Stored in localStorage, shared by every intraday
// chart on the page through a `dashboard:timezone` event.
const TZ_KEY = "dashboard.timeZone";
const TZ_EVENT = "dashboard:timezone";

export function useLocalTime() {
  try {
    return localStorage.getItem(TZ_KEY) === "local";
  } catch {
    return false;
  }
}

export function setLocalTime(on) {
  try {
    localStorage.setItem(TZ_KEY, on ? "local" : "utc");
  } catch {
    // storage unavailable: the choice lasts for the page
  }
  window.dispatchEvent(new CustomEvent(TZ_EVENT, { detail: { local: !!on } }));
}

/** "UTC", or the local zone as e.g. "CEST (UTC+2)". */
export function zoneLabel(local = useLocalTime()) {
  if (!local) {
    return "UTC";
  }
  const now = new Date();
  const short = (
    now
      .toLocaleTimeString("en-US", { timeZoneName: "short" })
      .split(" ")
      .pop() || ""
  ).replace(/^GMT/, "UTC");
  const off = -now.getTimezoneOffset();
  const hh = Math.floor(Math.abs(off) / 60);
  const mm = Math.abs(off) % 60;
  const offset = `UTC${off >= 0 ? "+" : MINUS}${hh}${mm ? `:${pad2(mm)}` : ""}`;
  return short && short !== offset ? `${short} (${offset})` : offset;
}

/** Short local zone name: "CEST", or "UTC+2" when the browser has none. */
export function localZoneShort() {
  return zoneLabel(true).split(" (")[0];
}

/** "HH:MM" of UTC hour `h` of `day` (default today), in UTC or local time. */
export function hourLabel(day, h, local = useLocalTime()) {
  if (!local) {
    return `${pad2(h % 24)}:00`;
  }
  const now = Date.now();
  const dayMs = day ? parseDay(day) : now - (now % DAY_MS);
  const d = new Date(dayMs + h * HOUR_MS);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

// -------------------------------------------------------- bar chart (intraday)
/**
 * spec: { hours[], today[], yesterday[], expected_today[], in_progress_hour,
 *   day, events[], height, emptyMessage, ariaLabel }
 */
export function barChart(container, spec) {
  const state = { spec };
  const f = frame(container, {
    legend: [
      { label: "Today", color: "var(--ink)", kind: "rect" },
      { label: "Expected", color: "var(--expected)", kind: "dash" },
      { label: "Yesterday", color: "var(--yesterday)" },
    ],
    buttons: [
      {
        key: "tz",
        label: `Local time (${localZoneShort()})`,
        onToggle: on => setLocalTime(on),
      },
      { key: "table", label: "Table", onToggle: on => f.showTable(on) },
    ],
  });
  const onTz = () => {
    f.buttons.tz.setAttribute("aria-pressed", String(useLocalTime()));
    render();
  };
  window.addEventListener(TZ_EVENT, onTz);

  function render() {
    const hadFocus = readerFocused(f);
    const s = state.spec;
    const local = useLocalTime();
    const zone = zoneLabel(local);
    const hl = h => hourLabel(s.day, h, local);
    const width = f.container.clientWidth;
    if (width < 40) {
      return;
    }
    const height = s.height || 240;
    const hours = s.hours || [];
    const n = hours.length;
    if (!n) {
      f.show(s.emptyMessage || "No hourly data for this series");
      return;
    }
    const vals = [s.today, s.yesterday, s.expected_today].flatMap(arr =>
      (arr || []).filter(v => v != null)
    );
    const yMax = Math.max(1, ...vals) * 1.1;
    const yTicks = linearTicks(
      0,
      yMax,
      Math.max(3, Math.floor((height - 46) / 44))
    );
    const left = axisWidth(yTicks);
    const right = width - 12;

    // platform events of the two days on the chart: today's badges, then
    // yesterday's (dimmed); an event with a known time also gets a rule
    const dayMs = s.day ? parseDay(s.day) : null;
    const onDay = ms =>
      dayMs == null ? [] : (s.events || []).filter(g => parseDay(g.day) === ms);
    const todayEvents = onDay(dayMs);
    const ydayEvents = onDay(dayMs - DAY_MS);
    const timed = todayEvents.flatMap(g =>
      g.items
        .filter(it => it.at)
        .map(it => ({ g, it, hour: (Date.parse(it.at) - dayMs) / HOUR_MS }))
    );
    const badgePositions = [];
    let cursor = left;
    if (todayEvents.length) {
      badgePositions.push({
        px: cursor + BADGE / 2,
        title: `Today, ${fmtDateLong(dayMs)}`,
        groups: todayEvents,
        dim: false,
        rule: false,
      });
      cursor += BADGE + 26;
    }
    const ydayLabelX = cursor;
    if (ydayEvents.length) {
      cursor += "yesterday".length * 6.2 + 6;
      badgePositions.push({
        px: cursor + BADGE / 2,
        title: `Yesterday, ${fmtDateLong(dayMs - DAY_MS)}`,
        groups: ydayEvents,
        dim: true,
        rule: false,
      });
    }
    const badgeClusters = layoutEventBadges(left, right, badgePositions);
    const strip = stripHeight(badgeClusters);
    const svgHeight = height + strip;
    const box = { left, right, top: 20 + strip, bottom: svgHeight - 24 };
    const slot = (right - left) / n;
    const barW = clamp(slot - 2, 2, 24);
    const y = makeY(0, yMax, box.top, box.bottom, false);
    const cx = i => left + slot * (i + 0.5);
    const pxs = hours.map((_, i) => cx(i));
    const point = (v, i) => (v == null ? null : { x: cx(i), y: y(v) });

    const root = svg("svg", {
      width,
      height: svgHeight,
      role: "group",
      "aria-label":
        s.ariaLabel || "Crashes per hour today against the expected profile",
    });
    yAxis(root, yTicks, y, left, right, fmtInt);
    const band = svg("rect", {
      class: "hover-band",
      y: box.top,
      height: box.bottom - box.top,
      width: slot,
      visibility: "hidden",
    });
    root.append(
      band,
      svg("line", {
        class: "baseline",
        x1: left,
        x2: right,
        y1: box.bottom,
        y2: box.bottom,
      })
    );
    // a tick every 6 hours of the displayed clock, none under the zone label
    const zoneWidth = zone.length * 6.5 + 10;
    for (let i = 0; i < n; i++) {
      const label = hl(hours[i]);
      if (
        label.endsWith(":00") &&
        Number(label.slice(0, 2)) % 6 === 0 &&
        cx(i) + 20 < right - zoneWidth
      ) {
        root.append(
          svg("text", {
            x: cx(i),
            y: box.bottom + 16,
            "text-anchor": "middle",
            text: label,
          })
        );
      }
    }
    root.append(
      svg("text", {
        class: "lbl",
        x: right,
        y: box.bottom + 16,
        "text-anchor": "end",
        text: zone,
      })
    );

    const bars = hours.map((h, i) => {
      const v = at(s.today, i);
      if (v == null) {
        return null;
      }
      const inProgress = h === s.in_progress_hour;
      const top = y(v);
      const hgt = box.bottom - top;
      const bar = svg("path", {
        class: inProgress ? "bar bar-progress" : "bar",
        d: inProgress
          ? roundedBar(
              cx(i) - barW / 2 + 1,
              top + 1,
              barW - 2,
              Math.max(0, hgt - 1),
              3
            )
          : roundedBar(cx(i) - barW / 2, top, barW, hgt, 4),
      });
      root.append(bar);
      if (inProgress) {
        let anchor = "middle";
        if (cx(i) + 34 > right) {
          anchor = "end";
        } else if (cx(i) - 34 < left) {
          anchor = "start";
        }
        const lx = { end: right, start: left, middle: cx(i) }[anchor];
        root.append(
          svg("text", {
            class: "lbl",
            x: lx,
            y: box.top - 7,
            "text-anchor": anchor,
            text: "in progress",
          }),
          svg("line", {
            class: "rule",
            x1: cx(i),
            x2: cx(i),
            y1: box.top - 3,
            y2: top - 2,
          })
        );
      }
      return bar;
    });
    root.append(
      svg("path", {
        class: "series-yesterday",
        d: pathFrom((s.yesterday || []).map(point)),
      }),
      svg("path", {
        class: "series-expected",
        d: pathFrom((s.expected_today || []).map(point)),
      })
    );

    if (badgeClusters.length) {
      if (ydayEvents.length) {
        root.append(
          svg("text", {
            class: "lbl",
            x: ydayLabelX,
            y: strip - 6,
            text: "yesterday",
          })
        );
      }
      drawEventBadges(root, f, box, badgeClusters);
      for (const t of timed) {
        if (t.hour >= 0 && t.hour <= 24) {
          const tx = left + slot * t.hour;
          root.append(
            svg("line", {
              class: "event-rule",
              x1: tx,
              x2: tx,
              y1: box.top,
              y2: box.bottom,
            })
          );
        }
      }
    }

    const todayLabel = i => {
      if (at(s.today, i) == null) {
        return "today (not yet)";
      }
      return hours[i] === s.in_progress_hour ? "today (in progress)" : "today";
    };
    let hovered = null;
    hoverLayer(root, box, pxs, {
      crosshair: false, // the slot band shows the position
      onIndex: i => {
        hovered?.classList.remove("is-hover");
        hovered = null;
        if (i == null) {
          hideTip(f);
          band.setAttribute("visibility", "hidden");
          return;
        }
        band.setAttribute("x", left + slot * i);
        band.setAttribute("visibility", "visible");
        hovered = bars[i];
        hovered?.classList.add("is-hover");
        const today = at(s.today, i);
        const inProgress = hours[i] === s.in_progress_hour;
        const rows = [
          {
            value: fmtInt(today),
            label: todayLabel(i),
            color: "var(--ink)",
            kind: "rect",
          },
          {
            value: fmtInt(at(s.expected_today, i)),
            label: inProgress ? "expected (full hour)" : "expected",
            color: "var(--expected)",
            kind: "dash",
          },
          {
            value: fmtInt(at(s.yesterday, i)),
            label: "yesterday",
            color: "var(--yesterday)",
          },
          ...timed
            .filter(t => Math.floor(t.hour) === hours[i])
            .map(t => ({
              value: t.it.title,
              label: t.g.label,
              kind: "icon",
              source: t.g.source,
            })),
        ];
        const anchorY = y(today ?? at(s.expected_today, i) ?? 0);
        showTip(
          f,
          pxs[i],
          anchorY,
          `${hl(hours[i])}–${hl(hours[i] + 1)} ${zone}`,
          rows
        );
      },
      describe: i => {
        const today = at(s.today, i);
        const parts = [
          `${hl(hours[i])} ${zone}`,
          today == null
            ? "today not yet"
            : `today ${fmtInt(today)}${hours[i] === s.in_progress_hour ? " (in progress)" : ""}`,
          `expected ${fmtInt(at(s.expected_today, i))}`,
        ];
        if (at(s.yesterday, i) != null) {
          parts.push(`yesterday ${fmtInt(at(s.yesterday, i))}`);
        }
        return parts.join(", ");
      },
    });
    f.show(root);
    if (hadFocus) {
      root.querySelector(".overlay")?.focus({ preventScroll: true });
    }

    f.setTable(
      "Crashes per hour, table view",
      [`Hour (${zone})`, "Today", "Expected", "Yesterday"],
      () =>
        hours.map((h, i) => [
          hl(h) + (h === s.in_progress_hour ? " (in progress)" : ""),
          fmtInt(at(s.today, i)),
          fmtInt(at(s.expected_today, i)),
          fmtInt(at(s.yesterday, i)),
        ])
    );
  }

  f.observe(render);
  render();
  return {
    update(next) {
      state.spec = { ...state.spec, ...next };
      render();
    },
    destroy() {
      window.removeEventListener(TZ_EVENT, onTz);
      f.destroy();
    },
  };
}

// ------------------------------------------------------------------- sparkline
/** spec: { dates[], observed[], expected[], severity, partial, width, height }
 */
export function sparkline(container, spec) {
  const {
    dates = [],
    observed = [],
    expected = [],
    severity = "ok",
    partial = true,
    width = 120,
    height = 26,
  } = spec;
  const n = dates.length;
  if (!n) {
    container.textContent = "";
    return;
  }
  const max = Math.max(
    1,
    ...observed.filter(v => v != null),
    ...expected.filter(v => v != null)
  );
  const x = i => 3 + (i * (width - 6)) / Math.max(1, n - 1);
  const y = v => height - 3 - (Math.min(v, max) / max) * (height - 6);
  const point = (v, i) => (v == null ? null : { x: x(i), y: y(v) });
  const last = observed[n - 1];
  const label = `28 days: ${fmtInt(last)} ${partial ? "so far " : ""}today vs ${fmtInt(expected[n - 1])} expected`;
  const root = svg("svg", {
    class: "spark",
    width,
    height,
    role: "img",
    "aria-label": label,
  });
  root.append(svg("title", { text: label }));
  const exp = expected.map(point).filter(Boolean);
  if (exp.length > 1) {
    const floor = `L${px(exp.at(-1).x)},${height - 3}L${px(exp[0].x)},${height - 3}Z`;
    root.append(
      svg("path", { d: pathFrom(exp) + floor, class: "band3" }),
      svg("path", {
        d: pathFrom(exp),
        class: "series-expected",
        "stroke-width": 1,
      })
    );
  }
  root.append(
    svg("path", {
      class: "series-observed",
      "stroke-width": 1.5,
      d: pathFrom(observed.map(point)),
    })
  );
  if (last != null) {
    const color = SEV_COLOR[severity] || SEV_COLOR.ok;
    root.append(
      svg("circle", {
        cx: x(n - 1),
        cy: y(last),
        r: 3,
        fill: partial ? "var(--surface)" : color,
        stroke: color,
        "stroke-width": partial ? 1.5 : 0,
      })
    );
  }
  container.replaceChildren(root);
}

// ---------------------------------------------------------- factor mini charts
/**
 * spec: { values[], labels[], highlight, kind: 'bar'|'line', width, height,
 *   ticks[] }
 */
export function miniFactors(container, spec) {
  const {
    values = [],
    labels = [],
    highlight = -1,
    kind = "bar",
    width = 260,
    height = 78,
    ticks = [],
  } = spec;
  container.classList.add("mini");
  const n = values.length;
  if (!n) {
    container.textContent = "";
    return;
  }
  const lo = Math.min(1, ...values) - 0.02;
  const hi = Math.max(1, ...values) + 0.02;
  const box = { left: 34, right: width - 6, top: 6, bottom: height - 16 };
  const y = makeY(lo, hi, box.top, box.bottom, false);
  const slot = (box.right - box.left) / n;
  const cx = i => box.left + slot * (i + 0.5);
  const name = i => labels[i] ?? i + 1;
  const label = `${kind === "bar" ? "Weekday" : "Release-cycle"} factors, ${values.map((v, i) => `${name(i)}: ${v.toFixed(2)}`).join(", ")}`;
  const root = svg("svg", { width, height, role: "img", "aria-label": label });
  root.append(svg("title", { text: label }));
  for (const t of [lo + 0.02, 1, hi - 0.02]) {
    root.append(
      svg("line", {
        class: t === 1 ? "baseline" : "gridline",
        x1: box.left,
        x2: box.right,
        y1: y(t),
        y2: y(t),
      }),
      svg("text", {
        x: box.left - 5,
        y: y(t) + 3.5,
        "text-anchor": "end",
        text: `×${t.toFixed(2)}`,
      })
    );
  }
  if (kind === "bar") {
    const w = Math.min(24, slot - 4);
    values.forEach((v, i) => {
      root.append(
        svg("rect", {
          class: i === highlight ? "bar is-today" : "bar",
          x: cx(i) - w / 2,
          y: Math.min(y(1), y(v)),
          width: w,
          height: Math.max(1, Math.abs(y(1) - y(v))),
          rx: 2,
        }),
        svg("text", {
          x: cx(i),
          y: height - 3,
          "text-anchor": "middle",
          text: name(i),
        })
      );
    });
  } else {
    root.append(
      svg("path", {
        class: "series-observed",
        d: pathFrom(values.map((v, i) => ({ x: cx(i), y: y(v) }))),
      })
    );
    if (highlight >= 0) {
      root.append(
        svg("circle", {
          class: "ring",
          cx: cx(highlight),
          cy: y(values[highlight]),
          r: 5.5,
        }),
        svg("circle", {
          cx: cx(highlight),
          cy: y(values[highlight]),
          r: 3.5,
          fill: "var(--ink)",
        })
      );
    }
    for (const t of ticks) {
      root.append(
        svg("text", {
          x: cx(t - 1),
          y: height - 3,
          "text-anchor": "middle",
          text: String(t),
        })
      );
    }
  }
  container.replaceChildren(root);
}
