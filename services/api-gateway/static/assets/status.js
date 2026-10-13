// iYup status page. Talks only to this gateway's own JSON API.

const WINDOWS = { "1h": 60, "24h": 48, "7d": 56 }; // window -> bucket count
const WINDOW_WORDS = { "1h": "hour", "24h": "24 hours", "7d": "7 days" }; // "the last …"
const AXIS_START = { "1h": "1 hour ago", "24h": "24 hours ago", "7d": "7 days ago" };
const HISTORY_REFRESH_MS = 60_000;
const REQUEST_TIMEOUT_MS = 10_000;

const REASONS = {
  ok: "OK",
  http_4xx: "client error",
  http_5xx: "server error",
  unexpected_status: "unexpected status",
  body_mismatch: "expected text missing from the page",
  unexpected_answer: "unexpected DNS answer",
  timeout: "timed out",
  dns: "DNS lookup failed",
  connection_refused: "connection refused",
  tls: "TLS / certificate problem",
  network: "network error",
  error: "request failed",
  pending: "waiting for the first check",
  not_monitored: "not checked by any ping-agent",
};

const ICONS = {
  up: "M5 12.5l4.5 4.5L19 7.5",
  slow: "M12 7v5l3 2M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z",
  maintenance: "M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17l3 3 5.3-5.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z",
  down: "M7 7l10 10M17 7L7 17",
  unknown: "M9.2 9.2a2.9 2.9 0 1 1 4.1 2.6c-.8.4-1.3 1-1.3 1.9v.6M12 17.6v.1",
};

const CRITTER_COLORS = {
  operational: "#a6d8ae",
  degraded: "#f3c86f",
  outage: "#f19476",
  unknown: "#cfcbe0",
  unreachable: "#cfcbe0",
  loading: "#cfcbe0",
  maintenance: "#b9d3ec",
};

const $ = (id) => document.getElementById(id);

const state = {
  window: initialWindow(),
  status: null,
  statusError: null,
  statusAt: 0,
  history: null,
  historyError: null,
  outages: null,
  outagesError: null,
  slo: null,
  previous: new Map(), // url -> state, to announce changes
  rows: new Map(), // url -> row refs
  timers: { status: 0, history: 0 },
};

// ---------- formatting ----------

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const dateTimeFmt = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});
const numberFmt = new Intl.NumberFormat();

function plural(n, word, many = `${word}s`) {
  return `${numberFmt.format(n)} ${n === 1 ? word : many}`;
}

function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 ? `${m}m ${s % 60}s` : `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d}d ${h % 24}h` : `${d}d`;
}

function fmtAgo(seconds) {
  return seconds < 5 ? "just now" : `${fmtDuration(seconds)} ago`;
}

// Never round real downtime up to a perfect score.
function fmtPct(value) {
  if (value == null) return "—";
  if (value >= 100) return "100%";
  const text = value >= 99 ? value.toFixed(2) : value.toFixed(1);
  return `${text === "100.00" ? "99.99" : text}%`;
}

function fmtMs(value) {
  if (value == null) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
}

function fmtWhen(iso, withDate) {
  const date = new Date(iso);
  return (withDate ? dateTimeFmt : timeFmt).format(date);
}

function secondsSince(iso) {
  return (Date.now() - new Date(iso).getTime()) / 1000;
}

// What a visitor needs to know about a failed request. The API's own detail is
// for developers (see /status); it can name internal hosts, so it stays off the page.
function describeError(error) {
  if (!error.status) return "the status service isn't responding";
  if (error.status === 502) return "the status service can't reach its monitoring agent";
  return `the status service had a problem (HTTP ${error.status})`;
}

function describeReason(target) {
  const label = REASONS[target.reason] || target.reason || "unknown";
  if (target.http_status && (target.reason === "http_4xx" || target.reason === "http_5xx")) {
    return `${label} (HTTP ${target.http_status})`;
  }
  return label;
}

// ---------- DOM helpers ----------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children) {
    if (child != null) node.append(child);
  }
  return node;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

function announce(message) {
  const node = $("announcer");
  node.textContent = "";
  window.setTimeout(() => {
    node.textContent = message;
  }, 50);
}

// ---------- data ----------

function initialWindow() {
  const fromQuery = new URLSearchParams(location.search).get("window");
  if (fromQuery in WINDOWS) return fromQuery;
  try {
    const saved = localStorage.getItem("iyup.window");
    if (saved in WINDOWS) return saved;
  } catch {
    // Storage can be unavailable (private mode, blocked cookies); the default is fine.
  }
  return "24h";
}

async function getJSON(url) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(url, {
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal: controller.signal,
    });
    let body = null;
    try {
      body = await response.json();
    } catch {
      // Non-JSON error page; fall through to the status code.
    }
    if (!response.ok) {
      const error = new Error(body && body.detail ? String(body.detail) : `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return body;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("the request timed out");
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

async function refreshStatus() {
  try {
    state.status = await getJSON("/status");
    state.statusAt = Date.now();
    state.statusError = null;
  } catch (error) {
    state.statusError = error;
  }
  render();
}

async function refreshHistory() {
  const w = state.window;
  const [history, outages, slo] = await Promise.allSettled([
    getJSON(`/history?window=${w}&buckets=${WINDOWS[w]}`),
    getJSON(`/outages?window=${w}&limit=20`),
    getJSON("/slo?window=30d"),
  ]);
  if (w !== state.window) return; // the window changed while we were waiting
  state.slo = slo.status === "fulfilled" ? slo.value : null;
  state.history = history.status === "fulfilled" ? history.value : null;
  state.historyError = history.status === "rejected" ? history.reason : null;
  state.outages = outages.status === "fulfilled" ? outages.value : null;
  state.outagesError = outages.status === "rejected" ? outages.reason : null;
  render();
}

function statusInterval() {
  const seconds = state.status?.monitor?.interval_seconds || 30;
  return Math.min(60, Math.max(10, seconds)) * 1000;
}

function schedule() {
  window.clearTimeout(state.timers.status);
  window.clearTimeout(state.timers.history);
  if (document.hidden) return;
  state.timers.status = window.setTimeout(async () => {
    await refreshStatus();
    schedule();
  }, statusInterval());
  state.timers.history = window.setTimeout(async () => {
    await refreshHistory();
    schedule();
  }, HISTORY_REFRESH_MS);
}

async function refreshAll() {
  const button = $("refresh");
  button.classList.add("is-spinning");
  button.disabled = true;
  await Promise.all([refreshStatus(), refreshHistory()]);
  button.classList.remove("is-spinning");
  button.disabled = false;
  schedule();
}

// ---------- rendering: hero ----------

function heroCopy(status) {
  const { summary, targets } = status;
  const names = (st) => targets.filter((t) => t.state === st).map((t) => t.name);
  const list = (items) =>
    items.length <= 2 ? items.join(" and ") : `${items.slice(0, 2).join(", ")} and ${plural(items.length - 2, "other")}`;

  switch (status.status) {
    case "operational": {
      const up = summary.total - summary.maintenance;
      const aside = summary.maintenance ? ` ${plural(summary.maintenance, "target")} in planned maintenance.` : "";
      return {
        headline: "Yup.",
        subline: (up === 1 ? `${targets.find((t) => !t.maintenance?.active).name} is up.` : `All ${up} targets are up.`) + aside,
      };
    }
    case "degraded": {
      const down = targets.filter((t) => t.state === "down" && !t.maintenance?.active).map((t) => t.name);
      if (!down.length) {
        const slow = targets.filter((t) => t.slow).map((t) => t.name);
        return { headline: "Yup… mostly.", subline: `Everything is up, but ${list(slow)} ${slow.length === 1 ? "is" : "are"} slow.` };
      }
      const others = summary.up;
      return {
        headline: "Yup… mostly.",
        subline: `${list(down)} ${down.length === 1 ? "is" : "are"} down.${others ? ` ${plural(others, "other")} ${others === 1 ? "is" : "are"} up.` : ""}`,
      };
    }
    case "maintenance":
      return { headline: "Back soon.", subline: "Everything is in planned maintenance." };
    case "outage":
      return {
        headline: "Nope.",
        subline: summary.total === 1 ? `${targets[0].name} is down.` : `All ${summary.total} targets are down.`,
      };
    default: {
      if (summary.total === 0) {
        return { headline: "Nothing to watch.", subline: "The ping-agent isn't checking any targets yet." };
      }
      const stale = targets.filter((t) => t.stale);
      if (stale.length) {
        return {
          headline: "Hold on…",
          subline: `No fresh checks for ${list(stale.map((t) => t.name))}. The ping-agent may be stuck or overloaded.`,
        };
      }
      return { headline: "Hold on…", subline: `Waiting for the first check of ${plural(summary.unknown, "target")}.` };
    }
  }
}

function renderHero() {
  const hero = document.querySelector(".hero");
  const { status, statusError } = state;
  let copy;
  let heroState;

  if (!status) {
    heroState = statusError ? "unreachable" : "loading";
    copy = statusError
      ? { headline: "Can't tell.", subline: `Right now ${describeError(statusError)}. Retrying automatically.` }
      : { headline: "Checking…", subline: "Asking the ping-agent how everything is doing." };
  } else {
    heroState = status.status;
    copy = heroCopy(status);
    const every = status.monitor?.interval_seconds;
    if (every) copy.subline += ` Checked every ${fmtDuration(every)}.`;
  }

  hero.dataset.state = heroState;
  $("headline").textContent = copy.headline;
  $("subline").textContent = copy.subline;

  const down = status?.summary?.down || 0;
  document.title = down ? `(${down} down) ${copy.headline} · iYup status` : `${copy.headline} · iYup status`;
  setFavicon(CRITTER_COLORS[heroState]);
}

function setFavicon(color) {
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">` +
    `<rect x="3" y="5" width="26" height="24" rx="11" fill="${color}" stroke="#2b2622" stroke-width="2.5"/>` +
    `<circle cx="12" cy="16" r="2" fill="#2b2622"/><circle cx="20" cy="16" r="2" fill="#2b2622"/></svg>`;
  $("favicon").href = `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

function renderMaintenance() {
  const box = $("maintenance");
  const windows = state.status?.maintenance || [];
  if (!windows.length) {
    box.hidden = true;
    box.replaceChildren();
    return;
  }
  const items = windows.map((w) => {
    const who = w.targets.length <= 3 ? w.targets.join(", ") : `${w.targets.slice(0, 3).join(", ")} and ${plural(w.targets.length - 3, "other")}`;
    const multiDay = Math.abs(secondsSince(w.ends_at) - secondsSince(w.starts_at)) >= 86400 || !w.active;
    const when = w.active
      ? `until ${fmtWhen(w.ends_at, multiDay)}`
      : `${fmtWhen(w.starts_at, true)} – ${fmtWhen(w.ends_at, multiDay && secondsSince(w.ends_at) < -86400)}`;
    return el(
      "li",
      { class: w.active ? "maint-item is-active" : "maint-item" },
      icon("maintenance"),
      el("span", {}, el("strong", { text: w.active ? "Planned maintenance now" : "Upcoming maintenance" }), ` · ${who}, ${when}${w.comment ? ` · ${w.comment}` : ""}`),
    );
  });
  box.replaceChildren(el("ul", {}, ...items));
  box.hidden = false;
}

function renderBanner() {
  const banner = $("banner");
  const staleData = state.status && state.statusError;
  document.body.classList.toggle("is-stale", Boolean(staleData));
  if (staleData) {
    banner.textContent = `Couldn't refresh: ${describeError(state.statusError)}. Showing what we knew ${fmtAgo(
      (Date.now() - state.statusAt) / 1000,
    )}.`;
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }
}

function renderFreshness() {
  const node = $("updated");
  if (!state.statusAt) {
    node.textContent = state.statusError ? "Not connected" : "Connecting…";
    return;
  }
  const ago = fmtAgo((Date.now() - state.statusAt) / 1000);
  node.textContent = state.statusError ? `Last update ${ago}` : `Updated ${ago}`;
}

// ---------- rendering: targets ----------

function historyProblem() {
  const error = state.historyError;
  if (!error) return null;
  if (error.status === 501) return "History needs Prometheus: PROMETHEUS_URL isn't set on the gateway.";
  return "History is unavailable right now: the history store isn't answering. Current status is unaffected.";
}

function renderTargets() {
  const list = $("targets");
  const status = state.status;
  if (!status) {
    // Never leave loading skeletons up once we know there is nothing coming.
    if (state.statusError && list.querySelector(".target--skeleton")) {
      list.setAttribute("aria-busy", "false");
      list.replaceChildren(el("li", { class: "history-empty", text: "No target data yet. This page retries on its own." }));
    }
    return;
  }

  list.setAttribute("aria-busy", "false");
  // Direct children only: cards contain their own .history-empty messages.
  list.querySelectorAll(":scope > .target--skeleton, :scope > li.history-empty").forEach((node) => node.remove());

  const note = $("history-note");
  const problem = historyProblem();
  note.textContent = problem || "";
  note.hidden = !problem;

  const historyByUrl = new Map((state.history?.targets || []).map((row) => [row.url, row]));
  const seen = new Set();

  if (status.targets.length === 0) {
    list.replaceChildren(el("li", { class: "history-empty", text: "No targets are configured." }));
    state.rows.clear();
    return;
  }

  status.targets.forEach((target, index) => {
    seen.add(target.url);
    let refs = state.rows.get(target.url);
    if (!refs) {
      refs = createRow(target.url);
      state.rows.set(target.url, refs);
    }
    if (list.children[index] !== refs.li) list.insertBefore(refs.li, list.children[index] || null);
    updateRow(refs, target, historyByUrl.get(target.url));

    const before = state.previous.get(target.url);
    if (before && before !== target.state && target.state !== "unknown") {
      announce(target.state === "up" ? `${target.name} is back up.` : `${target.name} is down: ${describeReason(target)}.`);
    }
    state.previous.set(target.url, target.state);
  });

  for (const [url, refs] of state.rows) {
    if (!seen.has(url)) {
      refs.li.remove();
      state.rows.delete(url);
    }
  }
}

function createRow(url) {
  const refs = {};
  refs.pill = el("span", { class: "pill" });
  refs.name = el("h3", { class: "target__name" });
  refs.kind = el("span", { class: "kind-tag", hidden: true });
  refs.url = /^https?:/.test(url)
    ? el("a", { class: "target__url", href: url, rel: "noopener noreferrer", target: "_blank", text: url })
    : el("span", { class: "target__url", text: url });
  refs.uptime = el("span", { class: "figure__value" });
  refs.uptimeLabel = el("span", { class: "figure__label" });
  refs.p95 = el("span", { class: "figure__value" });
  refs.p95Label = el("span", { class: "figure__label" });
  refs.history = el("div", { class: "history" });
  refs.summary = el("summary");
  refs.body = el("dl", { class: "detail-grid" });
  refs.details = el("details", { class: "target__details" }, refs.summary, refs.body);

  refs.li = el(
    "li",
    { class: "target" },
    el(
      "div",
      { class: "target__head" },
      el("div", { class: "target__id" }, el("div", { class: "target__title" }, refs.name, refs.kind, refs.pill), refs.url),
      el(
        "div",
        { class: "figures" },
        el("div", { class: "figure" }, refs.uptime, refs.uptimeLabel),
        el("div", { class: "figure" }, refs.p95, refs.p95Label),
      ),
    ),
    refs.history,
    refs.details,
  );
  return refs;
}

function pill(target) {
  if (target.maintenance?.active) return { kind: "maintenance", text: "Maintenance" };
  if (target.state === "up") return target.slow ? { kind: "slow", text: "Slow" } : { kind: "up", text: "Up" };
  if (target.state === "down") return { kind: "down", text: "Down" };
  return { kind: "unknown", text: target.stale ? "Stale" : target.reason === "pending" ? "Pending" : "Unknown" };
}

function updateRow(refs, target, history) {
  const p = pill(target);
  refs.li.dataset.state = p.kind === "slow" ? "slow" : p.kind === "maintenance" ? "maintenance" : target.state;
  refs.pill.className = `pill pill--${p.kind}`;
  refs.pill.replaceChildren(icon(p.kind), document.createTextNode(p.text));
  refs.name.textContent = target.name;
  refs.kind.textContent = target.kind === "http" ? "" : target.kind.toUpperCase();
  refs.kind.hidden = target.kind === "http";

  if (history) {
    refs.uptime.textContent = fmtPct(history.availability);
    refs.uptimeLabel.textContent = `uptime, ${state.window}`;
    refs.p95.textContent = fmtMs(history.latency_p95_ms);
    refs.p95Label.textContent = `p95 latency, ${state.window}`;
  } else {
    // Without Prometheus, fall back to the agent's own lifetime numbers and say so.
    refs.uptime.textContent = fmtPct(target.availability);
    refs.uptimeLabel.textContent = "uptime since agent start";
    refs.p95.textContent = fmtMs(target.latency_percentiles_ms.p95);
    refs.p95Label.textContent = "p95 since agent start";
  }

  renderHistory(refs, target, history);
  renderSummary(refs.summary, target);
  renderDetails(refs.body, target);
}

// When did the current state begin? The agent's state_since restarts with the
// agent; outage history survives restarts. Outage times can be up to one step
// early, so they are shifted by that step: the result may understate how long
// a state has lasted, never overstate it.
function stateSince(target) {
  const agent = { since: target.state_since, beforeWindow: false };
  const data = state.outages;
  if (!data) return agent;
  const outages = data.outages.filter((o) => o.url === target.url);
  const step = data.resolution_seconds * 1000;
  let candidate = null;
  let beforeWindow = false;
  if (target.state === "down") {
    const ongoing = outages.find((o) => o.ongoing);
    candidate = ongoing?.started_at;
    beforeWindow = Boolean(ongoing?.started_before_window);
  } else if (target.state === "up") {
    candidate = outages.find((o) => !o.ongoing && o.ended_at)?.ended_at; // newest first
  }
  if (!candidate) return agent;
  const shifted = new Date(new Date(candidate).getTime() + step);
  if (!target.state_since || shifted < new Date(target.state_since)) {
    return { since: shifted.toISOString(), beforeWindow };
  }
  return agent;
}

function renderSummary(summary, target) {
  const parts = [];
  const { since, beforeWindow } = stateSince(target);
  // An outage that began before the window has lasted longer than we can see.
  const lasted = since ? `${beforeWindow ? "over " : ""}${fmtDuration(secondsSince(since))}` : null;
  if (target.maintenance?.active) {
    const until = fmtWhen(target.maintenance.ends_at, secondsSince(target.maintenance.ends_at) < -86400);
    parts.push(el("span", { class: "maint", text: `Planned maintenance until ${until}` }));
    if (target.maintenance.comment) parts.push(target.maintenance.comment);
  }
  if (target.state === "up") {
    if (lasted && !target.maintenance?.active) parts.push(`Up for ${lasted}`);
    if (target.latency_ms != null) {
      parts.push(target.slow ? el("span", { class: "slowtext", text: `${fmtMs(target.latency_ms)}, slower than ${fmtMs(target.slow_threshold_ms)}` }) : fmtMs(target.latency_ms));
    }
  } else if (target.state === "down") {
    if (lasted) parts.push(`Down for ${lasted}`);
    parts.push(el("span", { class: "reason", text: describeReason(target) }));
  } else if (target.stale) {
    parts.push(`No fresh check for ${fmtDuration(target.last_checked_seconds_ago)}`);
    if (target.reason) parts.push(`last result: ${describeReason(target)}`);
  } else {
    parts.push(target.reason === "not_monitored" ? "The ping-agent isn't checking this URL" : "Waiting for the first check");
  }
  if (target.regions_total > 1 && target.regions_failing > 0 && target.state === "up") {
    const failing = target.regions.filter((r) => r.state === "down").map((r) => r.region);
    parts.push(el("span", { class: "regional", text: `failing from ${failing.join(", ")} (${target.regions_failing} of ${target.regions_total} regions)` }));
  }
  if (target.last_checked_at && !target.stale) parts.push(`checked ${fmtAgo(secondsSince(target.last_checked_at))}`);

  const nodes = [];
  parts.forEach((part, i) => {
    if (i) nodes.push(document.createTextNode(" · "));
    nodes.push(typeof part === "string" ? document.createTextNode(part) : part);
  });
  const label = el("span", {}, ...nodes);
  summary.replaceChildren(label);
}

function detail(term, ...value) {
  return el("div", { class: "detail" }, el("dt", { text: term }), el("dd", {}, ...value));
}

function regionList(target) {
  const list = el("ul", { class: "region-list" });
  for (const r of target.regions) {
    const ok = r.state === "up";
    const text =
      r.state === "up" ? `${fmtMs(r.latency_ms)}${r.slow ? " (slow)" : ""}` : r.state === "down" ? describeReason(r) : r.stale ? "no fresh check" : "waiting";
    const item = el("li", { class: `region region--${r.state}` }, icon(ok ? "up" : r.state === "down" ? "down" : "unknown"));
    item.append(el("strong", { text: r.region }), document.createTextNode(` ${text}`));
    list.append(item);
  }
  return list;
}

function budgetDetail(target) {
  const row = state.slo?.targets.find((t) => t.url === target.url);
  if (!row || row.budget_remaining == null) return null;
  const left = row.budget_remaining;
  const used = fmtDuration(row.downtime_seconds);
  const allowed = fmtDuration(row.allowed_downtime_seconds);
  const text = left >= 0 ? `${Math.round(left)}% left · ${used} of ${allowed} used` : `Overspent · ${used} down, ${allowed} allowed`;
  const coverage = row.coverage_seconds < 30 * 86400 ? ` (based on ${fmtDuration(row.coverage_seconds)} of data)` : "";
  const value = el("span", { class: left < 0 ? "reason" : left < 25 ? "slowtext" : "", text: text + coverage });
  return detail(`Error budget, 30 days at ${row.objective}%`, value);
}

function renderDetails(body, target) {
  const items = [];
  if (target.kind === "http") {
    items.push(detail("Last response", target.http_status ? `HTTP ${target.http_status}` : "No response"));
  } else {
    items.push(detail("Check", target.kind === "dns" ? "DNS lookup" : target.url.startsWith("tls://") ? "TCP connect + TLS handshake" : "TCP connect"));
  }
  const budget = budgetDetail(target);
  if (budget) items.push(budget);

  const days = target.ssl_cert_expiry_days;
  if (days != null) {
    const text = days < 0 ? `Expired ${fmtDuration(-days * 86400)} ago` : `Expires in ${fmtDuration(days * 86400)}`;
    items.push(detail("TLS certificate", text));
  } else if (target.url.startsWith("http://")) {
    items.push(detail("TLS certificate", "Plain HTTP"));
  }

  const okShare = target.availability != null ? `, ${fmtPct(target.availability)} ok` : "";
  items.push(
    detail(
      target.regions.length > 1 ? "Checks since agent start, all regions" : "Checks since agent start",
      `${numberFmt.format(target.total_checks)}${okShare}`,
      target.retries ? ` · ${plural(target.retries, "retry", "retries")}` : "",
    ),
  );
  if (target.last_checked_at) items.push(detail("Last check", fmtWhen(target.last_checked_at, true)));
  if (target.regions.length > 1) {
    const regions = detail(`Checked from ${target.regions.length} regions`, regionList(target));
    regions.classList.add("detail--wide");
    items.push(regions);
  }

  const phases = target.connection_ms;
  if (target.latency_ms != null && (phases.dns != null || phases.tcp != null || phases.tls != null)) {
    const parts = [
      ["DNS", phases.dns, "var(--phase-dns)"],
      ["TCP", phases.tcp, "var(--phase-tcp)"],
      ["TLS", phases.tls, "var(--phase-tls)"],
    ].filter(([, ms]) => ms != null);
    const network = parts.reduce((sum, [, ms]) => sum + ms, 0);
    parts.push(["Server", Math.max(0, target.latency_ms - network), "var(--phase-server)"]);
    const total = parts.reduce((sum, [, ms]) => sum + ms, 0) || 1;

    const bar = el("div", { class: "phases", role: "img", "aria-label": parts.map(([n, ms]) => `${n} ${fmtMs(ms)}`).join(", ") });
    const legend = el("ul", { class: "phases__legend" });
    for (const [name, ms, color] of parts) {
      const segment = el("span");
      segment.style.width = `${(ms / total) * 100}%`;
      segment.style.background = color;
      bar.append(segment);
      const item = el("li", { text: `${name} ${fmtMs(ms)}` });
      item.style.setProperty("--swatch", color);
      legend.append(item);
    }
    const wide = detail(`Where the ${fmtMs(target.latency_ms)} went (latest check)`, bar, legend);
    wide.classList.add("detail--wide");
    items.push(wide);
  }
  body.replaceChildren(...items);
}

// ---------- rendering: uptime bars ----------

function renderHistory(refs, target, history) {
  const container = refs.history;
  if (!state.history || !history) {
    const message = state.history
      ? "No history for this target yet."
      : state.historyError
        ? "History unavailable."
        : "Loading history…";
    if (container.dataset.mode !== message) {
      container.dataset.mode = message;
      container.replaceChildren(el("p", { class: "history-empty", text: message }));
    }
    return;
  }
  container.dataset.mode = "bars";

  const starts = state.history.bucket_starts;
  const size = state.history.bucket_seconds;
  let bars = container.querySelector(".bars");
  if (!bars || bars.children.length !== history.buckets.length) {
    bars = el("div", { class: "bars", role: "img", tabindex: "0" });
    for (let i = 0; i < history.buckets.length; i++) bars.append(el("span", { class: "bar" }));
    attachBarInteractions(bars);
    container.replaceChildren(
      bars,
      el(
        "div",
        { class: "bars__axis", "aria-hidden": "true" },
        el("span", { text: AXIS_START[state.window] }),
        el("span", { text: "now" }),
      ),
    );
  }

  let withDowntime = 0;
  const withDate = state.window !== "1h"; // a bare "11:30 PM" is ambiguous across midnight
  history.buckets.forEach((value, i) => {
    const bar = bars.children[i];
    const from = fmtWhen(starts[i], withDate);
    const to = fmtWhen(new Date(new Date(starts[i]).getTime() + size * 1000).toISOString(), size >= 86400);
    bar.dataset.tip = `${from} – ${to}\n${value == null ? "No data" : `${fmtPct(value)} up`}`;
    bar.className = value == null ? "bar bar--nodata" : "bar";
    const downtime = value == null ? 0 : 100 - value;
    let cap = bar.firstElementChild;
    if (downtime > 0) {
      withDowntime++;
      if (!cap) {
        cap = el("span", { class: "bar__down" });
        bar.append(cap);
      }
      cap.style.height = `${Math.max(downtime, 18)}%`;
    } else if (cap) {
      cap.remove();
    }
  });

  bars.setAttribute(
    "aria-label",
    `${target.name}: ${fmtPct(history.availability)} uptime over the last ${WINDOW_WORDS[state.window]}. ` +
      `${withDowntime} of ${history.buckets.length} periods had downtime. Use arrow keys to inspect periods.`,
  );
}

function showTip(bar) {
  const tip = $("tooltip");
  tip.textContent = bar.dataset.tip || "";
  tip.style.whiteSpace = "pre-line";
  tip.hidden = false;
  const rect = bar.getBoundingClientRect();
  const width = tip.offsetWidth;
  const left = Math.min(window.innerWidth - width - 8, Math.max(8, rect.left + rect.width / 2 - width / 2));
  tip.style.left = `${left}px`;
  tip.style.top = `${Math.max(8, rect.top - tip.offsetHeight - 8)}px`;
}

function hideTip() {
  $("tooltip").hidden = true;
}

function setActive(bars, index) {
  const items = bars.children;
  const next = Math.max(0, Math.min(items.length - 1, index));
  bars.querySelector(".bar.is-active")?.classList.remove("is-active");
  items[next].classList.add("is-active");
  bars.dataset.active = String(next);
  showTip(items[next]);
  announce(items[next].dataset.tip.replace("\n", ", "));
}

function attachBarInteractions(bars) {
  bars.addEventListener("pointermove", (event) => {
    const bar = event.target.closest(".bar");
    if (bar) showTip(bar);
  });
  bars.addEventListener("pointerleave", hideTip);
  bars.addEventListener("focus", () => setActive(bars, bars.children.length - 1));
  bars.addEventListener("blur", () => {
    bars.querySelector(".bar.is-active")?.classList.remove("is-active");
    hideTip();
  });
  bars.addEventListener("keydown", (event) => {
    const current = Number(bars.dataset.active || bars.children.length - 1);
    const moves = { ArrowLeft: current - 1, ArrowRight: current + 1, Home: 0, End: bars.children.length - 1 };
    if (event.key in moves) {
      event.preventDefault();
      setActive(bars, moves[event.key]);
    } else if (event.key === "Escape") {
      hideTip();
    }
  });
}

// ---------- rendering: outages ----------

function renderOutages() {
  const container = $("outages");
  const data = state.outages;
  $("outages-window").textContent = data
    ? `Last ${WINDOW_WORDS[state.window]} · accurate to ${fmtDuration(data.resolution_seconds)}`
    : "";

  if (!data) {
    const error = state.outagesError;
    const text = !error
      ? "Loading outage history…"
      : error.status === 501
        ? "Outage history needs Prometheus: PROMETHEUS_URL isn't set on the gateway."
        : "Outage history is unavailable right now: the history store isn't answering.";
    container.replaceChildren(el("p", { class: "empty", text }));
    return;
  }

  if (data.total === 0) {
    const shield = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    shield.setAttribute("viewBox", "0 0 32 32");
    shield.setAttribute("aria-hidden", "true");
    const body = document.createElementNS("http://www.w3.org/2000/svg", "path");
    body.setAttribute("d", "M16 3l11 4v8c0 7-4.7 12-11 14C9.7 27 5 22 5 15V7z");
    const check = document.createElementNS("http://www.w3.org/2000/svg", "path");
    check.setAttribute("d", "M11 16l3.5 3.5L21 13");
    shield.append(body, check);
    container.replaceChildren(
      el(
        "div",
        { class: "empty" },
        shield,
        el("span", {}, el("strong", { text: `No outages in the last ${WINDOW_WORDS[state.window]}.` }), " Quiet is good."),
      ),
    );
    return;
  }

  const withDate = state.window !== "1h";
  const items = data.outages.map((outage) => {
    const start = fmtWhen(outage.started_at, withDate);
    const when = outage.ongoing
      ? `${outage.started_before_window ? "Before" : "Since"} ${start}`
      : `${outage.started_before_window ? "Before " : ""}${start} – ${fmtWhen(outage.ended_at, false)}`;
    return el(
      "li",
      { class: "outage" },
      el(
        "div",
        {},
        el("div", { class: "outage__name" }, outage.name, outage.ongoing ? el("span", { class: "badge-live", text: "ongoing" }) : null),
        el("div", { class: "outage__when", text: when }),
      ),
      // Ongoing outages keep growing between history refreshes; count them live.
      el("div", {
        class: "outage__duration",
        text:
          (outage.started_before_window ? "over " : "") +
          fmtDuration(outage.ongoing ? secondsSince(outage.started_at) : outage.duration_seconds),
      }),
    );
  });

  const nodes = [el("ul", { class: "outage-list" }, ...items)];
  if (data.truncated) {
    nodes.push(el("p", { class: "outages__more", text: `Showing the latest ${data.outages.length} of ${data.total}.` }));
  }
  container.replaceChildren(...nodes);
}

// ---------- wiring ----------

function render() {
  renderHero();
  renderBanner();
  renderMaintenance();
  renderFreshness();
  renderTargets();
  renderOutages();
}

function selectWindow(next) {
  if (!(next in WINDOWS) || next === state.window) return;
  state.window = next;
  try {
    localStorage.setItem("iyup.window", next);
  } catch {
    // Not persisted; the choice still applies to this visit.
  }
  const url = new URL(location.href);
  url.searchParams.set("window", next);
  history.replaceState(null, "", url);
  syncWindowButtons();
  state.history = null;
  state.outages = null;
  state.historyError = null;
  state.outagesError = null;
  render();
  refreshHistory().then(schedule);
}

function syncWindowButtons() {
  document.querySelectorAll(".segmented [role=radio]").forEach((button) => {
    const checked = button.dataset.window === state.window;
    button.setAttribute("aria-checked", String(checked));
    button.tabIndex = checked ? 0 : -1;
  });
}

function wire() {
  const group = document.querySelector(".segmented");
  group.addEventListener("click", (event) => {
    const button = event.target.closest("[role=radio]");
    if (button) selectWindow(button.dataset.window);
  });
  group.addEventListener("keydown", (event) => {
    const keys = Object.keys(WINDOWS);
    const offset = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    if (!offset) return;
    event.preventDefault();
    const next = keys[(keys.indexOf(state.window) + offset + keys.length) % keys.length];
    selectWindow(next);
    group.querySelector(`[data-window="${next}"]`).focus();
  });

  $("refresh").addEventListener("click", refreshAll);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) schedule();
    else refreshAll();
  });
  window.addEventListener("scroll", hideTip, { passive: true });
  window.setInterval(renderFreshness, 1000);
  window.setInterval(renderOutages, 15_000);

  syncWindowButtons();
}

wire();
refreshAll();
