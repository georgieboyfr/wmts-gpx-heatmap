"use strict";

// ---------- utilitaires ----------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const nf = new Intl.NumberFormat(LOCALE);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const fmtNum = (n) => (n == null ? "—" : nf.format(n));
function fmtBytes(b) {
  if (b == null) return "—";
  const units = t("bytes.units");
  let i = 0;
  while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
  return `${new Intl.NumberFormat(LOCALE, { maximumFractionDigits: i ? 1 : 0 }).format(b)} ${units[i]}`;
}
function fmtDuration(s) {
  if (s == null) return "—";
  s = Math.round(s);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, "0")} s`;
  const h = Math.floor(s / 3600);
  if (h < 48) return `${h} h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")} min`;
  return `${Math.floor(h / 24)} ${t("time.days")}`;
}
function fmtAgo(ts) {
  if (!ts) return "—";
  const d = Date.now() / 1000 - ts;
  if (Math.abs(d) < 5) return t("time.now");
  return d > 0 ? t("time.ago", { d: fmtDuration(d) }) : t("time.in", { d: fmtDuration(-d) });
}
function fmtDate(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString(LOCALE, { dateStyle: "short", timeStyle: "short" });
}
function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString(LOCALE);
}

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function api(path, { method = "GET", body, raw = false } = {}) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET") opts.headers["X-Requested-With"] = "admin-ui";
  if (body !== undefined) {
    if (raw) {
      opts.headers["Content-Type"] = "application/octet-stream";
      opts.body = body;
    } else {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
  }
  const r = await fetch(path, opts);
  const isJson = (r.headers.get("content-type") || "").includes("json");
  const data = isJson ? await r.json() : await r.text();
  if (r.status === 401 && !path.endsWith("/login")) {
    showLogin();
    throw new ApiError(t("session.expired"), 401);
  }
  if (!r.ok) {
    let detail = isJson ? data.detail : data;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join(" ; ");
    throw new ApiError(detail || r.statusText, r.status);
  }
  return data;
}

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "err" ? 7000 : 4000);
}

function confirmDialog(title, message, okLabel = t("dialog.confirm")) {
  const dlg = $("#confirm-dialog");
  $("#confirm-title").textContent = title;
  $("#confirm-message").textContent = message;
  $("#confirm-ok").textContent = okLabel;
  dlg.returnValue = "";
  dlg.showModal();
  return new Promise((resolve) => dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true }));
}

// Le statut est rafraîchi toutes les 1,5 s : ne reconstruire un bloc que si son contenu
// change, sinon un clic sur un de ses boutons pourrait tomber pendant la reconstruction.
function setHTML(el, html) {
  if (el.dataset.html !== html) {
    el.innerHTML = html;
    el.dataset.html = html;
  }
}

function showError(el, err) {
  if (!el) return toast(err.message, "err");
  el.textContent = err ? err.message : "";
  el.hidden = !err;
}

const state = {
  status: null,
  settings: null,
  tab: "dashboard",
  pollTimer: null,
  lastJobsSignature: "",
  showGarminLogin: false,
  files: { offset: 0, limit: 50, total: 0 },
  logs: { after: 0 },
  render: { dirty: false, gradient: [] },
  map: null,
};

// ---------- connexion ----------

async function checkSession() {
  const s = await api("/admin/api/session");
  if (s.authenticated) return start();
  showLogin(!s.admin_enabled);
}

function showLogin(disabled = false) {
  clearTimeout(state.pollTimer);
  $("#app").hidden = true;
  $("#login-view").hidden = false;
  $("#login-disabled").hidden = !disabled;
  $("#login-form button").disabled = disabled;
  $("#login-token").focus();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#login-form button");
  btn.disabled = true;
  try {
    await api("/admin/api/login", { method: "POST", body: { token: $("#login-token").value } });
    $("#login-token").value = "";
    showError($("#login-error"), null);
    start();
  } catch (err) {
    showError($("#login-error"), err);
  } finally {
    btn.disabled = false;
  }
});

$("#logout-btn").addEventListener("click", async () => {
  await api("/admin/api/logout", { method: "POST" });
  showLogin();
});

async function start() {
  $("#login-view").hidden = true;
  $("#app").hidden = false;
  await loadSettings();
  await poll();
}

// ---------- onglets ----------

function selectTab(tab) {
  state.tab = tab;
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab-panel").forEach((p) => (p.hidden = p.dataset.panel !== tab));
  history.replaceState(null, "", `#${tab}`);
  window.scrollTo(0, 0);
  if (tab === "data") loadFiles();
  if (tab === "render") initMap();
  if (tab === "logs") pollLogs(true);
}
$$(".tabs button").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));

// ---------- état général (rafraîchi en continu) ----------

async function poll() {
  clearTimeout(state.pollTimer);
  try {
    state.status = await api("/admin/api/status");
    renderStatus();
  } catch (err) {
    if (err.status === 401) return;
    $("#service-line").textContent = t("service.unreachable");
  }
  state.pollTimer = setTimeout(poll, document.hidden ? 8000 : 1500);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && !$("#app").hidden) poll(); });

function renderStatus() {
  const { service, stats, garmin, jobs } = state.status;
  $("#service-line").textContent = t("service.line", { version: service.version, uptime: fmtDuration(service.uptime_s), zmin: service.zoom[0], zmax: service.zoom[1] });
  $("#caps-link").href = "/wmts?SERVICE=WMTS&REQUEST=GetCapabilities";
  renderStats(stats, garmin);
  $$('.action-row [data-action="garmin-sync"]').forEach((b) => {
    b.disabled = garmin.state === "disconnected" || !!garmin.sync_job;
    b.title = garmin.state === "disconnected" ? t("garmin.not_connected_hint") : "";
  });
  renderJobs(jobs);
  renderDashGarmin(garmin);
  renderGarmin(garmin);

  // une tâche vient de se terminer : rafraîchir ce qui en dépend
  const signature = jobs.filter((j) => ["done", "failed", "cancelled"].includes(j.status)).map((j) => j.id).join(",");
  if (state.lastJobsSignature && signature !== state.lastJobsSignature && state.tab === "data") loadFiles();
  state.lastJobsSignature = signature;
}

function renderStats(stats, garmin) {
  const failed = stats.files_failed ? `<span class="err">${t("stats.failed", { n: fmtNum(stats.files_failed) })}</span>` : t("stats.no_error");
  const legacy = stats.files_legacy ? ` · ${t("stats.legacy", { n: fmtNum(stats.files_legacy) })}` : "";
  const g = garminState(garmin);
  $("#stats-grid").innerHTML = `
    <div class="stat"><div class="label">${t("stats.files")}</div><div class="value">${fmtNum(stats.files)}</div>
      <div class="sub">${failed}${legacy}</div></div>
    <div class="stat"><div class="label">${t("stats.points")}</div><div class="value">${fmtNum(stats.points)}</div>
      <div class="sub">${t("stats.tracks", { n: fmtNum(stats.segments), count: stats.segments })}</div></div>
    <div class="stat"><div class="label">${t("stats.tiles")}</div><div class="value">${fmtNum(stats.tiles)}</div>
      <div class="sub">${t("stats.zooms", { range: state.status.service.zoom.join("–") })}</div></div>
    <div class="stat"><div class="label">${t("stats.garmin")}</div><div class="value" style="font-size:18px">
      <span class="badge ${g.cls}">${g.label}</span></div>
      <div class="sub">${garmin.last_sync_at ? t("stats.last_sync", { ago: fmtAgo(garmin.last_sync_at) }) : t("stats.never_synced")}</div></div>`;
  const dot = $("#garmin-tab-dot");
  dot.className = `dot ${g.cls === "ok" ? "" : g.cls}`;
}

const JOB_STATUS = {
  queued: [t("job.queued"), "info"], running: [t("job.running"), "info"], done: [t("job.done"), "ok"],
  failed: [t("job.failed"), "err"], cancelled: [t("job.cancelled"), "warn"],
};

function renderPhase(p) {
  const done = !p.current;
  const pct = p.total ? Math.min(100, (p.done / p.total) * 100) : 100;
  const indeterminate = p.current && !p.total;
  const count = p.total ? `${fmtNum(p.done)} / ${fmtNum(p.total)}` : (p.done ? fmtNum(p.done) : "");
  const eta = p.eta_s != null ? ` · ~${fmtDuration(p.eta_s)}` : "";
  return `<div class="phase ${done ? "done" : ""}">
      <span>${esc(ts(p.name))}</span>
      <div class="bar ${indeterminate ? "indeterminate" : ""}"><span style="width:${indeterminate ? 35 : pct}%"></span></div>
      <span class="count">${count}${eta}</span></div>`;
}

function renderResult(job) {
  if (job.error) return `<span class="err-detail">${esc(job.error)}</span>`;
  if (job.status === "cancelled" && ["reindex", "rerender"].includes(job.kind))
    return `<span class="muted">${t("job.partial_tiles")}</span>`;
  if (!job.result) return "";
  return Object.entries(job.result).map(([k, v]) => `${esc(ts(k))}${t("result.sep")}<strong>${esc(typeof v === "number" ? fmtNum(v) : v)}</strong>`).join(" · ");
}

function renderJobs(jobs) {
  // une carte par tâche, conservée d'un rafraîchissement à l'autre (bouton Annuler stable)
  const active = jobs.filter((j) => j.status === "queued" || j.status === "running").reverse();
  const container = $("#active-jobs");
  const ids = new Set(active.map((j) => String(j.id)));
  $$("[data-job]", container).forEach((n) => { if (!ids.has(n.dataset.job)) n.remove(); });
  $(".empty", container)?.remove();
  for (const j of active) {
    let node = $(`[data-job="${j.id}"]`, container);
    if (!node) {
      node = document.createElement("div");
      node.className = "job";
      node.dataset.job = j.id;
      node.innerHTML = `<div class="job-head"><div class="job-title"></div>
        <button class="btn ghost small" data-cancel="${j.id}">${t("job.cancel")}</button></div><div class="job-phases"></div>`;
      container.append(node);
    }
    const [label, cls] = JOB_STATUS[j.status];
    $(".job-title", node).innerHTML = `<strong>${esc(ts(j.label))}</strong>
      <span class="badge ${cls}">${j.cancel_requested ? t("job.cancelling") : label}</span>
      ${j.started_at ? `<span class="muted small">${t("job.since", { duration: fmtDuration(j.duration_s) })}</span>` : ""}`;
    $("[data-cancel]", node).disabled = j.cancel_requested;
    $(".job-phases", node).innerHTML = j.phases.map(renderPhase).join("") || `<div class="muted small">${t("job.waiting_start")}</div>`;
  }
  if (!active.length) container.innerHTML = `<div class="empty">${t("job.none_active")}</div>`;

  const finished = jobs.filter((j) => !(j.status === "queued" || j.status === "running")).slice(0, 15);
  $("#history-body").innerHTML = finished.length ? finished.map((j) => {
    const [label, cls] = JOB_STATUS[j.status];
    return `<tr><td class="name">${esc(ts(j.label))}</td><td><span class="badge ${cls}">${label}</span></td>
      <td class="muted">${fmtDate(j.started_at || j.created_at)}</td><td class="num">${fmtDuration(j.duration_s)}</td>
      <td>${renderResult(j)}</td></tr>`;
  }).join("") : `<tr><td colspan="5" class="empty">${t("job.none")}</td></tr>`;
}

document.addEventListener("click", async (e) => {
  const cancel = e.target.closest("[data-cancel]");
  if (cancel) {
    cancel.disabled = true;
    try { await api(`/admin/api/jobs/${cancel.dataset.cancel}/cancel`, { method: "POST" }); poll(); }
    catch (err) { toast(err.message, "err"); }
  }
  const action = e.target.closest("[data-action]");
  if (action) runAction(action.dataset.action, action);
});

async function runAction(action, btn) {
  if (action === "reindex" && !await confirmDialog(t("reindex.confirm.title"), t("reindex.confirm.msg"), t("reindex.confirm.ok"))) return;
  if (btn) btn.disabled = true;
  try {
    const r = await api(`/admin/api/actions/${action}`, { method: "POST" });
    toast(r.created ? t(`action.${action}`) : t("action.already"), r.created ? "ok" : "");
    poll();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ---------- Garmin ----------

const GARMIN_STATES = {
  connected: [t("garmin.connected"), "ok"],
  disconnected: [t("garmin.disconnected"), ""],
  needs_login: [t("garmin.needs_login"), "err"],
  mfa_pending: [t("garmin.mfa_pending"), "warn"],
};
function garminState(g) {
  const [label, cls] = GARMIN_STATES[g.state] || [g.state, ""];
  return { label, cls };
}

function renderDashGarmin(g) {
  const s = garminState(g);
  let html = `<p><span class="badge ${s.cls}">${s.label}</span> ${g.account_name ? esc(g.account_name) : ""}</p>`;
  if (g.state === "connected") {
    html += `<dl class="kv">
      <dt>${t("garmin.auto_sync")}</dt><dd>${g.enabled ? t("garmin.enabled_next", { next: g.next_sync_at ? fmtAgo(g.next_sync_at) : "—" }) : t("garmin.disabled")}</dd>
      <dt>${t("garmin.last_sync")}</dt><dd>${g.last_sync_at ? t("garmin.last_sync_dash", { ago: fmtAgo(g.last_sync_at), n: fmtNum(g.last_sync_new), count: g.last_sync_new }) : t("garmin.never")}</dd>
      <dt>${t("garmin.downloaded")}</dt><dd>${fmtNum(g.total_downloaded)}</dd></dl>`;
  }
  if (g.last_error) html += `<div class="notice err">${esc(g.last_error)}</div>`;
  $("#dash-garmin-info").innerHTML = html;
  setHTML($("#dash-garmin-actions"), g.state !== "connected"
    ? `<button class="btn primary" data-goto="garmin">${t("garmin.connect_btn")}</button>`
    : `<button class="btn ghost small" data-goto="garmin">${t("garmin.manage")}</button>`);
}
document.addEventListener("click", (e) => {
  const go = e.target.closest("[data-goto]");
  if (go) selectTab(go.dataset.goto);
});

function renderGarmin(g) {
  const s = garminState(g);
  const pw = g.password_source === "env" ? t("garmin.pw_env") : g.password_remembered ? t("garmin.yes") : t("garmin.no");
  let html = `<p><span class="badge ${s.cls}">${s.label}</span></p>
    <dl class="kv">
      <dt>${t("garmin.account")}</dt><dd>${esc(g.account_name || g.email || "—")}</dd>
      <dt>${t("garmin.token")}</dt><dd>${g.token_present ? t("garmin.token_present") : t("garmin.token_absent")}</dd>
      <dt>${t("garmin.pw_remembered")}</dt><dd>${pw}</dd>
      <dt>${t("garmin.connected_at")}</dt><dd>${fmtDate(g.connected_at)}</dd>
      <dt>${t("garmin.auto_sync")}</dt><dd>${g.enabled ? t("garmin.enabled") : t("garmin.disabled")}${g.enabled && g.next_sync_at ? t("garmin.next", { next: fmtAgo(g.next_sync_at) }) : ""}</dd>
      <dt>${t("garmin.last_sync")}</dt><dd>${g.last_sync_at ? t("garmin.last_sync_full", { date: fmtDate(g.last_sync_at), n: fmtNum(g.last_sync_new) }) : t("garmin.never")}</dd>
      <dt>${t("garmin.downloaded")}</dt><dd>${fmtNum(g.total_downloaded)}</dd>
    </dl>`;
  if (g.last_error) html += `<div class="notice err">${t("garmin.last_error", { ago: fmtAgo(g.last_error_at), error: esc(g.last_error) })}</div>`;
  $("#garmin-status-info").innerHTML = html;
  $("#garmin-status-job").innerHTML = g.sync_job
    ? `<div class="job">${g.sync_job.phases.map(renderPhase).join("") || t("job.waiting")}</div>` : "";
  const buttons = [];
  if (g.state === "connected" || g.state === "needs_login") {
    buttons.push(`<button class="btn" data-action="garmin-sync" ${g.sync_job ? "disabled" : ""}>${t("garmin.sync_now")}</button>`);
    buttons.push(`<button class="btn ghost" id="garmin-relogin">${t("garmin.relogin")}</button>`);
    if (g.password_source === "admin") buttons.push(`<button class="btn ghost" id="garmin-forget">${t("garmin.forget")}</button>`);
    buttons.push(`<button class="btn danger" id="garmin-logout">${t("garmin.logout")}</button>`);
  }
  setHTML($("#garmin-status-actions"), buttons.join(""));

  const showMfa = g.state === "mfa_pending";
  const showLoginForm = !showMfa && (g.state === "disconnected" || g.state === "needs_login" || state.showGarminLogin);
  $("#garmin-mfa-card").hidden = !showMfa;
  $("#garmin-login-card").hidden = !showLoginForm;
  $("#garmin-login-cancel").hidden = !(state.showGarminLogin && g.state === "connected");
  $("#garmin-login-title").textContent = g.state === "needs_login" ? t("garmin.relogin_title") : t("garmin.login_title");
  if (showLoginForm && !$("#garmin-email").value && g.email) $("#garmin-email").value = g.email;
  if (showMfa) {
    const left = Math.max(0, Math.round(g.mfa_expires_at - Date.now() / 1000));
    $("#garmin-mfa-timer").textContent = t("garmin.mfa_timer", { left: fmtDuration(left) });
  }
}

document.addEventListener("click", async (e) => {
  if (e.target.id === "garmin-relogin") { state.showGarminLogin = true; renderGarmin(state.status.garmin); $("#garmin-password").focus(); }
  if (e.target.id === "garmin-logout") {
    if (!await confirmDialog(t("garmin.logout.confirm.title"), t("garmin.logout.confirm.msg"), t("garmin.logout"))) return;
    await garminCall("/admin/api/garmin/logout", undefined, t("garmin.logged_out"));
  }
  if (e.target.id === "garmin-forget") await garminCall("/admin/api/garmin/forget-password", undefined, t("garmin.forgotten"));
});
$("#garmin-login-cancel").addEventListener("click", () => { state.showGarminLogin = false; renderGarmin(state.status.garmin); });

async function garminCall(path, body, okMessage, errorEl) {
  try {
    const g = await api(path, { method: "POST", body });
    state.status.garmin = g;
    renderGarmin(g);
    renderDashGarmin(g);
    if (okMessage) toast(okMessage, "ok");
    showError(errorEl, null);
    return g;
  } catch (err) {
    showError(errorEl, err);
    return null;
  }
}

$("#garmin-login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#garmin-login-btn");
  btn.disabled = true;
  btn.textContent = t("garmin.connecting");
  const remember = $("input[name=remember]:checked").value === "yes";
  const g = await garminCall("/admin/api/garmin/login",
    { email: $("#garmin-email").value, password: $("#garmin-password").value, remember },
    null, $("#garmin-login-error"));
  btn.disabled = false;
  btn.textContent = t("login.submit");
  if (!g) return;
  $("#garmin-password").value = "";
  state.showGarminLogin = false;
  if (g.state === "mfa_pending") { renderGarmin(g); $("#garmin-mfa-code").focus(); }
  else toast(t("garmin.logged_in"), "ok");
});

$("#garmin-mfa-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const g = await garminCall("/admin/api/garmin/mfa", { code: $("#garmin-mfa-code").value }, t("garmin.logged_in"), $("#garmin-mfa-error"));
  if (g) $("#garmin-mfa-code").value = "";
});
$("#garmin-mfa-cancel").addEventListener("click", () => garminCall("/admin/api/garmin/mfa/cancel"));

// ---------- réglages ----------

async function loadSettings() {
  state.settings = await api("/admin/api/settings");
  fillGarminSettings();
  if (!state.render.dirty) fillRenderForm();
}

function fillGarminSettings() {
  const g = state.settings.effective.garmin;
  $("#gs-enabled").checked = g.enabled;
  $("#gs-interval").value = g.sync_interval_minutes;
  $("#gs-lookback").value = g.lookback_days_first_sync;
  $("#gs-batch").value = g.ingest_batch_size;
  $("#gs-source").textContent = state.settings.overrides.garmin ? t("gs.source_admin") : t("gs.source_config");
}

$("#garmin-settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/admin/api/settings/garmin", { method: "PUT", body: {
      enabled: $("#gs-enabled").checked,
      sync_interval_minutes: Number($("#gs-interval").value),
      lookback_days_first_sync: Number($("#gs-lookback").value),
      ingest_batch_size: Number($("#gs-batch").value),
    } });
    toast(t("gs.saved"), "ok");
    await loadSettings();
    poll();
  } catch (err) { toast(err.message, "err"); }
});
$("#gs-enabled").addEventListener("change", () => $("#garmin-settings-form").requestSubmit());
$("#gs-reset").addEventListener("click", async () => {
  try {
    await api("/admin/api/settings/garmin", { method: "DELETE" });
    toast(t("gs.reset_done"), "ok");
    await loadSettings();
  } catch (err) { toast(err.message, "err"); }
});

// ---------- données ----------

let searchTimer;
$("#files-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.files.offset = 0; loadFiles(); }, 300);
});
$("#files-status").addEventListener("change", () => { state.files.offset = 0; loadFiles(); });
$("#files-prev").addEventListener("click", () => { state.files.offset = Math.max(0, state.files.offset - state.files.limit); loadFiles(); });
$("#files-next").addEventListener("click", () => { state.files.offset += state.files.limit; loadFiles(); });

const FILE_STATUS = { ok: [t("file.ok"), "ok"], failed: [t("file.failed"), "err"], legacy: [t("file.legacy"), "warn"] };

async function loadFiles() {
  const { offset, limit } = state.files;
  const q = encodeURIComponent($("#files-search").value.trim());
  const status = $("#files-status").value;
  let data;
  try { data = await api(`/admin/api/files?q=${q}&status=${status}&offset=${offset}&limit=${limit}`); }
  catch (err) { return toast(err.message, "err"); }
  state.files.total = data.total;
  $("#files-body").innerHTML = data.items.length ? data.items.map((f) => {
    const [label, cls] = FILE_STATUS[f.status] || [f.status, ""];
    return `<tr>
      <td><div class="name">${esc(f.name)}</div>${f.error ? `<div class="err-detail">${esc(f.error)}</div>` : ""}</td>
      <td><span class="badge ${cls}">${label}</span></td>
      <td class="num">${fmtNum(f.points)}</td>
      <td class="num">${fmtBytes(f.size)}</td>
      <td class="muted">${fmtDate(f.imported_at)}</td>
      <td>${f.status === "failed" ? `<button class="btn ghost small" data-retry="${esc(f.path)}">${t("data.retry")}</button>` : ""}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="6" class="empty">${t("data.none")}</td></tr>`;
  const end = Math.min(offset + limit, data.total);
  $("#files-count").textContent = data.total ? t("data.count", { from: fmtNum(offset + 1), to: fmtNum(end), total: fmtNum(data.total) }) : "";
  $("#files-prev").disabled = offset === 0;
  $("#files-next").disabled = end >= data.total;
  const failed = state.status ? state.status.stats.files_failed : 0;
  $("#retry-all-btn").hidden = !failed;
  $("#retry-all-btn").textContent = t("data.retry_all", { n: fmtNum(failed) });
}

document.addEventListener("click", async (e) => {
  const retry = e.target.closest("[data-retry]");
  if (retry) await retryFiles({ paths: [retry.dataset.retry] });
});
$("#retry-all-btn").addEventListener("click", () => retryFiles({ all_failed: true }));

async function retryFiles(body) {
  try {
    await api("/admin/api/files/retry", { method: "POST", body });
    toast(t("data.retry_started"), "ok");
    poll();
  } catch (err) { toast(err.message, "err"); }
}

const dropzone = $("#dropzone");
["dragenter", "dragover"].forEach((ev) => dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => dropzone.addEventListener(ev, () => dropzone.classList.remove("over")));
dropzone.addEventListener("drop", (e) => { e.preventDefault(); uploadFiles(e.dataTransfer.files); });
$("#upload-input").addEventListener("change", (e) => { uploadFiles(e.target.files); e.target.value = ""; });

async function uploadFiles(fileList) {
  const files = Array.from(fileList).filter((f) => f.name.toLowerCase().endsWith(".gpx"));
  if (!files.length) return toast(t("data.no_gpx"), "err");
  const list = $("#upload-list");
  let ok = 0;
  for (const file of files) {
    const li = document.createElement("li");
    li.innerHTML = `<span>${esc(file.name)}</span><span class="muted">${t("data.sending")}</span>`;
    list.prepend(li);
    try {
      const r = await api(`/admin/api/files/upload?name=${encodeURIComponent(file.name)}`, { method: "PUT", body: file, raw: true });
      li.lastChild.innerHTML = `<span class="badge ok">${r.replaced ? t("data.replaced") : t("data.sent")}</span>`;
      ok++;
    } catch (err) {
      li.lastChild.innerHTML = `<span class="badge err">${esc(err.message)}</span>`;
    }
  }
  toast(t("data.upload_done", { n: ok, count: ok }), ok ? "ok" : "err");
  poll();
}

// ---------- rendu ----------

const RENDER_NUMERIC = ["line_width_px", "blur_sigma_px", "color_saturation", "contrast_boost"];

// curseurs + champs numériques synchronisés
$$(".slider-field").forEach((el) => {
  const { key, min, max, step } = el.dataset;
  el.insertAdjacentHTML("beforeend", `<div class="slider-row">
    <input type="range" min="${min}" max="${max}" step="${step}" id="r-${key}-range">
    <input type="number" min="0" step="${step}" id="r-${key}" required></div>`);
  const range = $(`#r-${key}-range`, el);
  const num = $(`#r-${key}`, el);
  range.addEventListener("input", () => { num.value = range.value; renderChanged(); });
  num.addEventListener("input", () => { range.value = num.value; renderChanged(); });
});
["r-min_zoom", "r-max_zoom"].forEach((id) => $(`#${id}`).addEventListener("input", renderChanged));

function fillRenderForm() {
  const { render, color_gradient } = state.settings.effective;
  $("#r-min_zoom").value = render.min_zoom;
  $("#r-max_zoom").value = render.max_zoom;
  RENDER_NUMERIC.forEach((k) => { $(`#r-${k}`).value = render[k]; $(`#r-${k}-range`).value = render[k]; });
  state.render.gradient = color_gradient.map(([f, c]) => [f, [...c]]);
  state.render.dirty = false;
  renderGradientEditor();
  const ov = state.settings.overrides;
  $("#render-source").textContent = ov.render || ov.color_gradient ? t("render.source_admin") : t("render.source_config");
  showError($("#render-error"), null);
  updatePreviewLayer();
}

function readRenderForm() {
  const render = { min_zoom: Number($("#r-min_zoom").value), max_zoom: Number($("#r-max_zoom").value) };
  RENDER_NUMERIC.forEach((k) => (render[k] = Number($(`#r-${k}`).value)));
  render.line_width_px = Math.round(render.line_width_px);
  const gradient = state.render.gradient.map(([f, c]) => [f, c]).sort((a, b) => a[0] - b[0]);
  return { render, color_gradient: gradient };
}

const toHex = (c) => "#" + c.slice(0, 3).map((v) => Math.round(v).toString(16).padStart(2, "0")).join("");
const fromHex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));

function renderGradientEditor() {
  $("#gradient-stops").innerHTML = state.render.gradient.map(([f, c], i) => `
    <div class="stop-row" data-i="${i}">
      <input type="number" min="0" max="1" step="0.01" value="${f}" data-k="pos" title="${t("render.stop_pos")}">
      <input type="color" value="${toHex(c)}" data-k="color" title="${t("render.stop_color")}">
      <input type="range" min="0" max="255" value="${c[3]}" data-k="alpha" title="${t("render.stop_alpha")}">
      <button type="button" class="btn ghost small" data-k="del" title="${t("render.stop_del")}" ${state.render.gradient.length <= 2 ? "disabled" : ""}>✕</button>
    </div>`).join("");
  drawGradientBar();
}

function drawGradientBar() {
  const stops = [...state.render.gradient].sort((a, b) => a[0] - b[0])
    .map(([f, c]) => `rgba(${c[0]},${c[1]},${c[2]},${(c[3] / 255).toFixed(3)}) ${(f * 100).toFixed(1)}%`);
  $("#gradient-bar").innerHTML = `<div style="background: linear-gradient(to right, ${stops.join(", ")})"></div>`;
}

$("#gradient-stops").addEventListener("input", (e) => {
  const row = e.target.closest(".stop-row");
  if (!row) return;
  const stop = state.render.gradient[Number(row.dataset.i)];
  const k = e.target.dataset.k;
  if (k === "pos") stop[0] = Math.min(1, Math.max(0, Number(e.target.value)));
  if (k === "color") stop[1].splice(0, 3, ...fromHex(e.target.value));
  if (k === "alpha") stop[1][3] = Number(e.target.value);
  drawGradientBar();
  renderChanged();
});
$("#gradient-stops").addEventListener("click", (e) => {
  if (e.target.dataset.k !== "del") return;
  state.render.gradient.splice(Number(e.target.closest(".stop-row").dataset.i), 1);
  renderGradientEditor();
  renderChanged();
});
$("#add-stop").addEventListener("click", () => {
  const g = [...state.render.gradient].sort((a, b) => a[0] - b[0]);
  // nouveau palier au milieu du plus grand écart entre deux paliers voisins
  let best = 0;
  for (let i = 1; i < g.length - 1; i++) if (g[i + 1][0] - g[i][0] > g[best + 1][0] - g[best][0]) best = i;
  const a = g[best], b = g[best + 1];
  state.render.gradient = [...g, [Number(((a[0] + b[0]) / 2).toFixed(2)), a[1].map((v, i) => Math.round((v + b[1][i]) / 2))]];
  renderGradientEditor();
  renderChanged();
});

let previewTimer;
function renderChanged() {
  state.render.dirty = true;
  clearTimeout(previewTimer);
  previewTimer = setTimeout(updatePreviewLayer, 350);
}

$("#render-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/admin/api/settings/render", { method: "PUT", body: readRenderForm() });
    state.render.dirty = false;
    toast(t("render.saved"), "ok");
    await loadSettings();
    poll();
  } catch (err) { showError($("#render-error"), err); }
});
$("#render-revert").addEventListener("click", () => { state.render.dirty = false; fillRenderForm(); });
$("#render-reset").addEventListener("click", async () => {
  if (!await confirmDialog(t("render.reset.confirm.title"), t("render.reset.confirm.msg"), t("common.back_to_config"))) return;
  try {
    await api("/admin/api/settings/render", { method: "DELETE" });
    state.render.dirty = false;
    toast(t("render.reset_done"), "ok");
    await loadSettings();
    poll();
  } catch (err) { toast(err.message, "err"); }
});

// carte d'aperçu (Leaflet)
function initMap() {
  if (state.map || typeof L === "undefined") {
    if (typeof L === "undefined") $("#map").innerHTML = `<div class="notice warn" style="margin:12px">${t("map.no_leaflet")}</div>`;
    if (state.map) setTimeout(() => state.map.invalidateSize(), 50);
    return;
  }
  const map = L.map("map", { minZoom: 2, maxZoom: 18, zoomControl: true });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    maxZoom: 19, className: "basemap-dark",
  }).addTo(map);
  state.map = map;
  const b = state.status && state.status.stats.data_bounds;
  if (b) map.fitBounds([[b[1], b[0]], [b[3], b[2]]]);
  else map.setView([46.6, 2.4], 6);
  $$("input[name=map-layer]").forEach((r) => r.addEventListener("change", updatePreviewLayer));
  map.on("zoomend", updateMapStatus);
  updatePreviewLayer();
}

// bandeau sur la carte : tuiles en cours de calcul, ou zoom hors de la plage rendue
function updateMapStatus() {
  const el = $("#map-status");
  const layer = state.heatLayer;
  if (!layer) { el.hidden = true; return; }
  const { pending, total } = layer.progress;
  const { minZoom, maxZoom } = layer.options;
  const z = state.map.getZoom();
  if (pending > 0) {
    const what = $("input[name=map-layer]:checked").value === "preview" ? t("map.computing") : t("map.loading");
    el.innerHTML = `<span class="spinner"></span>${what}… ${total - pending}/${total}`;
  } else if (z < minZoom || z > maxZoom) {
    el.textContent = t("map.out_of_range", { z, min: minZoom, max: maxZoom });
  } else {
    el.hidden = true;
    return;
  }
  el.hidden = false;
}

function updatePreviewLayer() {
  const map = state.map;
  if (!map) return;
  if (state.heatLayer) map.removeLayer(state.heatLayer);
  const mode = $("input[name=map-layer]:checked").value;
  let url, minZoom, maxZoom;
  if (mode === "preview") {
    const cfg = readRenderForm();
    url = `/admin/api/preview/{z}/{x}/{y}.png?s=${encodeURIComponent(JSON.stringify(cfg))}`;
    [minZoom, maxZoom] = [cfg.render.min_zoom, cfg.render.max_zoom];
  } else {
    const layer = state.status ? state.status.service.layer : "heatmap";
    url = `/wmts/1.0.0/${encodeURIComponent(layer)}/default/GoogleMapsCompatible/{z}/{y}/{x}.png?t=${Date.now()}`;
    [minZoom, maxZoom] = state.status ? state.status.service.zoom : [0, 18];
  }
  const layer = L.tileLayer(url, { minZoom, maxZoom, maxNativeZoom: maxZoom, opacity: 1 });
  // compteur remis à zéro à chaque vague de tuiles (déplacement, zoom) une fois la précédente finie
  layer.progress = { pending: 0, total: 0 };
  layer.on("tileloadstart", () => {
    if (layer.progress.pending === 0) layer.progress.total = 0;
    layer.progress.pending++;
    layer.progress.total++;
    updateMapStatus();
  });
  layer.on("tileload tileerror tileabort", () => {
    layer.progress.pending = Math.max(0, layer.progress.pending - 1);
    updateMapStatus();
  });
  state.heatLayer = layer.addTo(map);
  updateMapStatus();
}

// ---------- journal ----------

let logsTimer;
async function pollLogs(reset = false) {
  clearTimeout(logsTimer);
  if (state.tab !== "logs") return;
  const view = $("#log-view");
  if (reset) { state.logs.after = 0; view.innerHTML = ""; }
  if (!$("#log-pause").checked) {
    try {
      const entries = await api(`/admin/api/logs?after=${state.logs.after}&level=${$("#log-level").value}`);
      if (entries.length) {
        const atBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 40;
        const filter = $("#log-filter").value.toLowerCase();
        view.insertAdjacentHTML("beforeend", entries.map((e) => `
          <div class="log-line ${e.level}" ${filter && !(e.message + e.logger).toLowerCase().includes(filter) ? "hidden" : ""}>
            <span class="muted">${fmtTime(e.ts)}</span><span class="lvl">${e.level}</span>
            <span class="src">${esc(e.logger)}</span><span class="msg">${esc(e.message)}</span></div>`).join(""));
        state.logs.after = entries[entries.length - 1].seq;
        while (view.children.length > 1500) view.firstElementChild.remove();
        if (atBottom) view.scrollTop = view.scrollHeight;
      }
    } catch (err) { if (err.status === 401) return; }
  }
  logsTimer = setTimeout(pollLogs, 2000);
}
$("#log-level").addEventListener("change", () => pollLogs(true));
$("#log-clear").addEventListener("click", () => { $("#log-view").innerHTML = ""; });
$("#log-filter").addEventListener("input", () => {
  const filter = $("#log-filter").value.toLowerCase();
  $$("#log-view .log-line").forEach((l) => (l.hidden = !!filter && !l.textContent.toLowerCase().includes(filter)));
});

// ---------- démarrage ----------

applyTranslations();

window.selectTab = selectTab;
const initialTab = location.hash.slice(1);
checkSession().then(() => {
  if (["dashboard", "garmin", "data", "render", "logs"].includes(initialTab)) selectTab(initialTab);
}).catch((err) => toast(err.message, "err"));
