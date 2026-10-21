/* JEV vs KEV triage benchmark — front-end (no build step). */
"use strict";

const KEYS = ["jev", "kev"];
const state = {
  config: null,
  samples: [],
  single: null,       // last single-ticket run document
  run: null,          // run shown in Batch/Dashboard
  prompt: null,
  promptPart: "questions",
  filter: "all",
  charts: [],
};

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const fmt = {
  pct: (v) => (v == null ? "—" : `${Math.round(v * 100)}%`),
  ms: (v) => (v == null ? "—" : `${Math.round(v).toLocaleString()} ms`),
  tok: (v) => (v == null ? "—" : Math.round(v).toLocaleString()),
  usd: (v) => (v == null ? "—" : v < 0.01 ? `$${v.toFixed(6)}` : v < 1 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`),
  num: (v) => (v == null ? "—" : v.toFixed(2)),
};

function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2200);
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* not JSON */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res;
}

const label = (k) => state.config?.models?.[k]?.label ?? k.toUpperCase();
const teamName = (id) => state.config?.teams.find((t) => t.id === id)?.name ?? id ?? "—";
const levels = () => state.config.severity.levels;
const outcome = (critical) => state.config.noul.outcomes[critical ? "true" : "false"];
// Status palette, always paired with a text label.
const NOUL_VAR = { true: "--critical", false: "--neutral" };
const SEVERITY_VARS = ["--neutral", "--warning", "--serious", "--critical"];
const severityColor = (i) => css(SEVERITY_VARS[Math.min(Math.max(i, 0), SEVERITY_VARS.length - 1)]);
const noulPill = (critical) =>
  critical == null ? "—" : `<span class="pill noul" style="background:${css(NOUL_VAR[critical])};${critical ? "color:#fff" : ""}">${esc(outcome(critical).name)}</span>`;
const statusPill = (s) => `<span class="pill st-${esc(s)}">${esc(s.charAt(0).toUpperCase() + s.slice(1))}</span>`;
const levelName = (id) => levels().find((l) => l.id === id)?.name ?? "—";
// Human-review rule: which confidences are checked against the threshold.
const modeChecks = (mode) => ({ team: mode !== "critical", noul: mode !== "team" });
const modeLabel = (mode) => state.config?.hitl_modes?.[mode] ?? mode;
const runMode = (run) => run?.settings?.hitl_mode ?? "team_or_critical";
function ruleText(mode, threshold = state.config.hitl_threshold) {
  const parts = { team_or_critical: "team or NOUL confidence", team: "team confidence", critical: "NOUL confidence" };
  return `human review when ${parts[mode] ?? parts.team_or_critical} < ${fmt.pct(threshold)}`;
}

// ---------------------------------------------------------------------------
// Theme & tabs
// ---------------------------------------------------------------------------
function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  Chart.defaults.color = css("--ink-2");
  Chart.defaults.borderColor = css("--grid");
  if (state.run && !$("panel-dashboard").hidden) renderDashboard(state.run);
  if (state.single) renderSingle(state.single.results[0], state.single);
}

$("theme-btn").addEventListener("click", () => {
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const current = document.documentElement.dataset.theme || (dark ? "dark" : "light");
  const next = current === "dark" ? "light" : "dark";
  try { localStorage.setItem("theme", next); } catch { /* storage unavailable */ }
  applyTheme(next);
});

function showTab(name) {
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  document.querySelectorAll("nav.tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll(".panel").forEach((p) => (p.hidden = p.id !== `panel-${name}`));
  if (name === "dashboard") {
    loadRuns();
    if (state.run) renderDashboard(state.run);
  }
}
document.querySelectorAll("nav.tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
async function init() {
  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
  let saved = null;
  try { saved = localStorage.getItem("theme"); } catch { /* storage unavailable */ }
  applyTheme(saved);

  const [config, samples] = await Promise.all([api("/api/config").then((r) => r.json()), api("/api/samples").then((r) => r.json())]);
  state.config = config;
  state.samples = samples;

  $("jev-label").textContent = config.models.jev.label;
  $("kev-label").textContent = config.models.kev.label;
  $("jev-label").title = config.models.jev.slug;
  $("kev-label").title = config.models.kev.slug;
  $("threshold-chip").textContent = `Default rule: ${ruleText(config.hitl_mode, config.hitl_threshold)}`;
  document.querySelectorAll("select.hitl-mode").forEach((sel) => {
    sel.innerHTML = Object.entries(config.hitl_modes).map(([v, t], i) => `<option value="${esc(v)}">${i + 1} · ${esc(t)}</option>`).join("");
    sel.value = config.hitl_mode;
  });
  $("key-banner").hidden = config.api_key_configured;
  $("t-runs").value = config.runs_per_ticket;
  $("batch-runs").value = config.runs_per_ticket;

  const batches = await api("/api/samples/batches").then((r) => r.json());
  $("sample-csv").innerHTML = batches.map((b) => `<option value="${esc(b.name)}">${esc(b.name)} (${b.rows} tickets)</option>`).join("");
  const syncDl = () => ($("sample-csv-dl").href = `/api/samples/batch.csv?name=${encodeURIComponent($("sample-csv").value)}`);
  $("sample-csv").addEventListener("change", syncDl);
  syncDl();

  const sel = $("sample-select");
  for (const s of samples) sel.insertAdjacentHTML("beforeend", `<option value="${esc(s.id)}">${esc(s.id)} — ${esc(s.title)}</option>`);
  renderConfig();
  const tab = location.hash.slice(1);
  if (document.getElementById(`panel-${tab}`)) showTab(tab);
}

// ---------------------------------------------------------------------------
// Single ticket
// ---------------------------------------------------------------------------
function readTicket() {
  return {
    id: $("sample-select").value || null,
    subject: $("t-subject").value.trim(),
    body: $("t-body").value.trim(),
    customer_tier: $("t-tier").value || null,
    channel: $("t-channel").value || null,
  };
}

function fillTicket(t) {
  $("t-subject").value = t.subject ?? "";
  $("t-body").value = t.body ?? "";
  $("t-tier").value = t.customer_tier ?? "";
  $("t-channel").value = t.channel ?? "";
}

$("sample-select").addEventListener("change", (e) => {
  const s = state.samples.find((x) => x.id === e.target.value);
  if (s) fillTicket(s);
});

$("view-prompt-btn").addEventListener("click", async () => {
  const t = readTicket();
  if (!t.subject) t.subject = "(subject)";
  if (!t.body) t.body = "(ticket body)";
  try {
    state.prompt = await api("/api/prompt", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(t) }).then((r) => r.json());
    showPromptPart("questions");
    $("prompt-dialog").showModal();
  } catch (err) {
    toast(err.message);
  }
});

function showPromptPart(part) {
  state.promptPart = part;
  document.querySelectorAll("#prompt-dialog .seg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.part === part)));
  const p = state.prompt;
  const value = part === "request" ? { endpoint: `POST ${p.endpoint}`, models: p.models, body: p.request } : p[part];
  $("prompt-text").textContent = JSON.stringify(value, null, 2);
}
document.querySelectorAll("#prompt-dialog .seg button").forEach((b) => b.addEventListener("click", () => showPromptPart(b.dataset.part)));
$("close-prompt").addEventListener("click", () => $("prompt-dialog").close());
$("copy-prompt").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("prompt-text").textContent);
    toast("Copied to clipboard");
  } catch {
    toast("Clipboard not available");
  }
});

$("ticket-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ticket = readTicket();
  if (!ticket.subject || !ticket.body) return toast("Subject and description are required");
  const btn = $("run-btn");
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span> Running both models…`;
  $("single-result").innerHTML = `<div class="empty"><span class="spinner"></span> Calling ${esc(label("jev"))} and ${esc(label("kev"))} in parallel…</div>`;
  try {
    const runs = Math.max(1, Math.min(10, Number($("t-runs").value) || 1));
    const run = await api("/api/triage", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ticket, runs, hitl_mode: $("t-hitl").value }) }).then((r) => r.json());
    state.single = run;
    renderSingle(run.results[0], run);
  } catch (err) {
    $("single-result").innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Run triage";
  }
});

function confBar(v, withMark = true, th = state.config.hitl_threshold) {
  if (v == null) return `<span class="muted">—</span>`;
  const low = withMark && v < th;
  return `<div class="conf" title="${withMark ? `Review threshold ${fmt.pct(th)}` : "Not checked by the current human-review rule"}">
    <div class="track"><div class="fill ${low ? "low" : "ok"}" style="width:${v * 100}%"></div>${withMark ? `<div class="mark" style="left:${th * 100}%"></div>` : ""}</div>
    <span class="v">${fmt.pct(v)}</span></div>`;
}

function probBar(p) {
  // P(critical) on a neutral track; the dashed tick marks the decision threshold.
  const th = state.config.noul.decision_threshold ?? 0.5;
  return `<div class="conf" title="Critical when P ≥ ${fmt.pct(th)}">
    <div class="track"><div class="fill" style="width:${p * 100}%;background:${css(NOUL_VAR[p >= th])}"></div><div class="mark" style="left:${th * 100}%"></div></div>
    <span class="v">${fmt.pct(p)}</span></div>`;
}

function severityBar(p) {
  if (p.severity_score == null) return "—";
  const lv = levels();
  const idx = lv.findIndex((l) => l.id === p.severity_level);
  const cells = lv.map((l, i) => {
    const prob = Number(p.severity_probabilities?.[String(i)] ?? 0);
    return `<span title="${esc(l.name)}: ${fmt.pct(prob)}" style="background:${severityColor(i)};opacity:${(0.15 + 0.85 * prob).toFixed(2)};height:14px"></span>`;
  });
  return `<div class="sev" aria-label="Severity ${esc(p.severity_level)}">${cells.join("")}</div>
    <div class="sev-label"><strong style="color:${severityColor(idx)}">●</strong> <strong>${esc(levelName(p.severity_level))}</strong>
      · score ${fmt.num(p.severity_score)} / ${lv.length - 1} · confidence ${fmt.pct(p.severity_confidence)}</div>`;
}

function topTeams(p) {
  const probs = Object.entries(p.team_probabilities ?? {}).sort((a, b) => b[1] - a[1]).slice(0, 3);
  if (probs.length < 2) return "";
  return `<div class="small muted">${probs.map(([k, v]) => `${esc(k)} ${fmt.pct(v)}`).join(" · ")}</div>`;
}

function modelCard(key, m, consistency, mode = "team_or_critical", th = state.config.hitl_threshold) {
  const checks = modeChecks(mode);
  const notChecked = `<div class="small muted">not checked by rule</div>`;
  const spec = state.config.models[key];
  const mt = m.metrics;
  const head = `<div class="head"><div><div class="name">${esc(spec.label)}</div>
      <div class="slug">${esc(mt.model_version || spec.slug)} · ${esc(spec.role)}${mt.provider ? ` · via ${esc(mt.provider)}` : ""}</div></div>
    ${m.hitl.required ? `<span class="pill hitl">⚠ Human review</span>` : `<span class="pill auto">✔ Auto-routed</span>`}</div>`;
  const metrics = `<div class="metrics">
    <div class="metric"><div class="k">TTFB</div><div class="v">${fmt.ms(mt.ttfb_ms)}</div></div>
    <div class="metric"><div class="k">Latency</div><div class="v">${fmt.ms(mt.latency_ms)}</div></div>
    <div class="metric"><div class="k">Cost</div><div class="v" title="${esc(mt.cost_source ?? "no pricing")}">${fmt.usd(mt.cost_usd)}</div></div>
    <div class="metric"><div class="k">Tokens in</div><div class="v">${fmt.tok(mt.input_tokens)}</div></div>
    <div class="metric"><div class="k">Tokens out</div><div class="v">${fmt.tok(mt.output_tokens)}</div></div>
    <div class="metric"><div class="k">Attempts</div><div class="v">${mt.attempts ?? "—"}</div></div>
  </div>`;
  const raw = m.raw ? `<details class="raw"><summary>Raw API response</summary><pre>${esc(JSON.stringify(m.raw, null, 2))}</pre></details>` : "";

  if (!m.parsed) {
    return `<div class="card model-card ${key}">${head}<div class="error-box">${esc(m.call_error || "No answer")}</div>${metrics}${raw}</div>`;
  }
  const p = m.parsed;
  const o = p.critical == null ? null : outcome(p.critical);
  const hitl = m.hitl.required
    ? `<div class="hitl-box hitl"><strong>⚠ Human review required</strong><ul>${m.hitl.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>`
    : `<div class="hitl-box auto"><strong>✔ Confident enough to auto-route</strong> — rule: ${esc(ruleText(mode, th))}</div>`;
  const cons = consistency
    ? `<div class="small muted" style="margin-top:8px">Consistency over ${consistency.runs} runs: team ${fmt.pct(consistency.team)}, NOUL ${fmt.pct(consistency.noul)}, severity σ ${consistency.severity_stdev}</div>`
    : "";
  return `<div class="card model-card ${key}">${head}
    <dl class="kv">
      <dt>Team</dt><dd><div class="team-name">${esc(teamName(p.team))}</div>${topTeams(p)}</dd>
      <dt>Team conf.</dt><dd>${confBar(p.team_confidence, checks.team, th)}${checks.team ? "" : notChecked}</dd>
      <dt>NOUL</dt><dd>${noulPill(p.critical)} ${o ? `<span class="small muted">SLA ${esc(o.response_sla)}</span>` : ""}</dd>
      <dt>P(critical)</dt><dd>${probBar(p.noul)}</dd>
      <dt>NOUL conf.</dt><dd>${confBar(p.noul_confidence, checks.noul, th)}${checks.noul ? "" : notChecked}</dd>
      <dt>Severity</dt><dd>${severityBar(p)}</dd>
    </dl>
    ${hitl}
    ${m.validation_errors?.length ? `<div class="error-box" style="margin-top:10px">${m.validation_errors.map(esc).join("<br>")}</div>` : ""}
    ${cons}
    ${metrics}
    ${raw}
  </div>`;
}

function divergenceBanner(c) {
  if (c.status === "error") return `<div class="divergence">${statusPill("error")} Could not compare — at least one model failed.</div>`;
  const mark = (ok) => (ok ? `<span class="ok">✔</span>` : `<span class="bad">✖</span>`);
  const text = {
    aligned: `${esc(label("kev"))} agrees with ${esc(label("jev"))}`,
    partial: `${esc(label("kev"))} partially deviates from ${esc(label("jev"))}`,
    diverges: `${esc(label("kev"))} diverges from ${esc(label("jev"))}`,
  }[c.status];
  const d = c.severity_delta;
  const signed = (v, f) => (v == null ? "—" : `${v > 0 ? "+" : ""}${f(v)}`);
  return `<div class="divergence">${statusPill(c.status)} <span>${text}</span>
    <div class="checks"><span>${mark(c.team_match)} Team</span><span>${mark(c.noul_match)} NOUL</span>
    <span>${mark(d != null && Math.abs(d) <= 0.5)} Severity Δ ${signed(d, fmt.num)}</span>
    <span class="muted">P(critical) Δ ${signed(c.noul_delta, (v) => `${Math.round(v * 100)} pts`)}</span>
    <span>${mark(c.hitl_match)} Human-review decision</span></div></div>`;
}

function metricCompare(r) {
  const rows = [
    ["TTFB (time to first byte)", "ttfb_ms", fmt.ms, true],
    ["Total latency", "latency_ms", fmt.ms, true],
    ["Input tokens", "input_tokens", fmt.tok, true],
    ["Output tokens", "output_tokens", fmt.tok, true],
    ["Total tokens", "total_tokens", fmt.tok, true],
    ["Cost", "cost_usd", fmt.usd, true],
  ];
  const body = rows
    .map(([name, k, f, lower]) => {
      const a = r.primary.jev.metrics[k], b = r.primary.kev.metrics[k];
      let wa = "", wb = "";
      if (a != null && b != null && a !== b) {
        if (lower ? a < b : a > b) wa = "win";
        else wb = "win";
      }
      const ratio = a && b ? `${(lower ? a / b : b / a).toFixed(2)}×` : "";
      return `<tr><td>${name}</td><td class="num ${wa}">${f(a)}</td><td class="num ${wb}">${f(b)}</td><td class="num muted" title="KEV relative advantage (>1 = KEV better)">${ratio}</td></tr>`;
    })
    .join("");
  return `<table class="compare"><thead><tr><th>Metric</th><th class="num" style="color:var(--jev)">${esc(label("jev"))}</th>
    <th class="num" style="color:var(--kev)">${esc(label("kev"))}</th><th class="num">KEV advantage</th></tr></thead><tbody>${body}</tbody></table>
    <p class="small muted" style="margin:6px 0 0">Green = better value. KEV advantage &gt; 1× means KEV is faster / cheaper / leaner.
    The Decisions API returns one JSON document (no token streaming), so TTFB replaces time-to-first-token.</p>`;
}

function renderSingle(r, run, mode = runMode(run), th = run?.settings?.hitl_threshold ?? state.config.hitl_threshold) {
  const canExport = run && run.summary;
  $("single-result").innerHTML = `
    ${divergenceBanner(r.comparison)}
    <div class="small muted" style="margin:-6px 0 10px">Rule for this run: <strong>${esc(modeLabel(mode))}</strong> — ${esc(ruleText(mode, th))}</div>
    <div class="models">${KEYS.map((k) => modelCard(k, r.primary[k], r.consistency?.[k], mode, th)).join("")}</div>
    <div class="card" style="margin-top:14px">
      <div class="row" style="margin-bottom:10px"><h2 style="margin:0">Speed, tokens &amp; cost</h2><span class="grow"></span>
      ${canExport ? `<button class="btn sm" id="single-report">Export report (HTML)</button><button class="btn sm" id="single-json">Export JSON</button>` : ""}</div>
      ${metricCompare(r)}
    </div>`;
  if (canExport) {
    $("single-report").addEventListener("click", () => exportAdhocReport(run));
    $("single-json").addEventListener("click", () => download(new Blob([JSON.stringify(run, null, 2)], { type: "application/json" }), `triage_${run.id}.json`));
  }
}

function download(blob, filename) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

async function exportAdhocReport(run) {
  try {
    const blob = await api("/api/report", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run }) }).then((r) => r.blob());
    download(blob, `jev-vs-kev-report_${run.id}.html`);
  } catch (err) {
    toast(err.message);
  }
}

// ---------------------------------------------------------------------------
// Batch
// ---------------------------------------------------------------------------
async function startBatch(useSample) {
  const fd = new FormData();
  const file = $("csv-file").files[0];
  if (!useSample && !file) return toast("Choose a CSV file first");
  if (useSample) {
    fd.append("use_sample", "true");
    fd.append("sample", $("sample-csv").value);
  }
  else fd.append("file", file);
  fd.append("runs", $("batch-runs").value || "1");
  fd.append("hitl_mode", $("batch-hitl").value);
  if ($("batch-name").value.trim()) fd.append("name", $("batch-name").value.trim());

  const btns = [$("batch-upload-btn"), $("batch-sample-btn")];
  btns.forEach((b) => (b.disabled = true));
  $("batch-progress").hidden = false;
  $("batch-results").hidden = true;
  setProgress(0, 0);
  try {
    const { id, total } = await api("/api/batch", { method: "POST", body: fd }).then((r) => r.json());
    setProgress(0, total);
    await new Promise((resolve) => {
      const es = new EventSource(`/api/batch/${id}/stream`);
      es.onmessage = (e) => {
        const d = JSON.parse(e.data);
        setProgress(d.done, d.total, d.elapsed_s, d.cost_usd);
      };
      es.addEventListener("end", () => { es.close(); resolve(); });
      es.onerror = () => { es.close(); resolve(); };
    });
    const run = await api(`/api/runs/${id}`).then((r) => r.json());
    state.run = run;
    renderBatch(run);
    loadRuns(id);
    toast(run.status === "done" ? "Batch finished" : `Batch ended with status ${run.status}`);
  } catch (err) {
    toast(err.message);
  } finally {
    btns.forEach((b) => (b.disabled = false));
    $("batch-progress").hidden = true;
  }
}
$("batch-upload-btn").addEventListener("click", () => startBatch(false));
$("batch-sample-btn").addEventListener("click", () => startBatch(true));

const fmtDuration = (s) => {
  if (s == null) return "—";
  if (s < 60) return `${s.toFixed(1)} s`;
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return m < 60 ? `${m}m ${String(r).padStart(2, "0")}s` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
};

function setProgress(done, total, elapsed, cost) {
  if (!total) {
    $("batch-progress-text").textContent = "Uploading…";
    $("batch-progress-bar").style.width = "0";
    return;
  }
  const eta = elapsed && done ? ` · ~${fmtDuration((elapsed / done) * (total - done))} left` : "";
  const spent = cost ? ` · spent ${KEYS.map((k) => `${label(k)} ${fmt.usd(cost[k])}`).join(" · ")}` : "";
  $("batch-progress-text").textContent = `${done} / ${total} tickets · ${fmtDuration(elapsed ?? 0)} elapsed${eta}${spent}`;
  $("batch-progress-bar").style.width = `${(100 * done) / total}%`;
}

function totalsTiles(run) {
  const t = run.totals;
  if (!t) return "";
  const costTile = (k) => `<div class="card kpi" style="border-top:4px solid var(--${k})"><div class="k">$ Total cost — ${esc(label(k))}</div>
      <div class="v" style="color:var(--${k})">${fmt.usd(t.cost_usd[k])}</div>
      <div class="d">${fmt.usd(t.cost_per_ticket_usd[k])} per ticket · ${fmt.usd(t.cost_per_ticket_usd[k] != null ? t.cost_per_ticket_usd[k] * 1000 : null)} per 1k</div></div>`;
  return `<div class="kpis totals" style="margin-bottom:14px">
    <div class="card kpi"><div class="k">⏱ Total batch time</div><div class="v">${fmtDuration(t.duration_s)}</div>
      <div class="d">${t.tickets} tickets · ${t.calls} model calls · concurrency ${t.concurrency}</div></div>
    <div class="card kpi"><div class="k">Throughput</div><div class="v">${t.tickets_per_min ?? "—"}<span class="small muted"> tickets/min</span></div>
      <div class="d">${t.avg_s_per_ticket != null ? `${t.avg_s_per_ticket.toFixed(2)} s per ticket (wall-clock)` : ""}</div></div>
    ${KEYS.map(costTile).join("")}
    <div class="card kpi"><div class="k" title="Each call's latency added up as if run one after another. Calls actually run in parallel (both models per ticket, ${t.concurrency} tickets at a time), so this exceeds the total batch time.">Sum of model latencies ⓘ</div>
      ${KEYS.map((k) => {
        const avg = run.summary?.models?.[k]?.latency_ms?.avg;
        return `<div class="row" style="justify-content:space-between;margin-top:4px">
          <span class="small"><i style="display:inline-block;width:10px;height:10px;border-radius:2px;background:var(--${k});margin-right:6px"></i>${esc(label(k))}</span>
          <span style="font-variant-numeric:tabular-nums"><strong style="color:var(--${k})">${fmtDuration(t.model_time_s[k])}</strong>
          <span class="small muted"> · ${fmt.ms(avg)}/call</span></span></div>`;
      }).join("")}
      <div class="d" style="margin-top:4px">Calls run in parallel, so this exceeds the total batch time</div></div>
  </div>`;
}

function exportLinks(run) {
  return `<a class="btn sm" href="/api/runs/${run.id}/report.html" target="_blank" rel="noopener">Open report</a>
    <a class="btn sm" href="/api/runs/${run.id}/report.html?download=true">Download report (HTML)</a>
    <a class="btn sm" href="/api/runs/${run.id}/export.csv">CSV</a>
    <a class="btn sm" href="/api/runs/${run.id}/export.json">JSON</a>`;
}

function matchesFilter(r, f) {
  if (f === "diverges") return r.comparison.status === "diverges" || r.comparison.status === "partial";
  if (f === "hitl") return KEYS.some((k) => r.primary[k].hitl.required);
  if (f === "error") return r.comparison.status === "error";
  return true;
}

function renderBatch(run) {
  $("batch-results").hidden = false;
  const name = run.name ?? "Results";
  $("batch-title").textContent = name.includes(String(run.results.length)) ? name : `${name} — ${run.results.length} tickets`;
  $("batch-totals").innerHTML = totalsTiles(run);
  $("batch-title").insertAdjacentHTML("beforeend", ` <span class="small muted" style="font-weight:500">· ${esc(modeLabel(runMode(run)))}: ${esc(ruleText(runMode(run), run.settings?.hitl_threshold))}</span>`);
  $("batch-exports").innerHTML = exportLinks(run) + `<button class="btn sm primary" id="to-dashboard">Open in dashboard →</button>`;
  $("to-dashboard").addEventListener("click", () => showTab("dashboard"));
  renderBatchTable(run);
}

function renderBatchTable(run) {
  const hasGt = run.results.some((r) => r.ground_truth);
  const cell = (m) => {
    const p = m.parsed;
    if (!p) return `<td colspan="3"><span class="pill st-error" title="${esc(m.call_error ?? "")}">Error</span></td>`;
    return `<td>${esc(p.team ?? "—")}${m.hitl.required ? ` <span title="${esc(m.hitl.reasons.join("; "))}">⚠</span>` : ""}</td>
      <td>${noulPill(p.critical)} <span class="small muted">${fmt.pct(p.noul)}</span></td>
      <td>${esc(levelName(p.severity_level))} <span class="small muted">${fmt.num(p.severity_score)}</span></td>`;
  };
  const gtText = (gt) => {
    if (!gt) return "—";
    const crit = gt.critical == null ? "—" : outcome(gt.critical).name;
    const sev = gt.severity == null ? "—" : levels()[gt.severity]?.name;
    return `${esc(gt.team ?? "—")} · ${esc(crit)} · ${esc(sev)}`;
  };
  const rows = run.results
    .map((r, i) => ({ r, i }))
    .filter(({ r }) => matchesFilter(r, state.filter))
    .map(({ r, i }) => `<tr class="clickable" data-i="${i}">
        <td>${esc(r.ticket.id)}</td><td>${esc(r.ticket.subject)}</td>
        ${cell(r.primary.jev)}${cell(r.primary.kev)}
        <td>${statusPill(r.comparison.status)}</td>
        <td class="num">${fmt.tok(r.primary.jev.metrics.latency_ms)}</td><td class="num">${fmt.tok(r.primary.kev.metrics.latency_ms)}</td>
        ${hasGt ? `<td class="small">${gtText(r.ground_truth)}</td>` : ""}
      </tr>`)
    .join("");
  $("batch-table").innerHTML = `<thead><tr><th>ID</th><th>Subject</th>
      <th class="jev">${esc(label("jev"))} team</th><th class="jev">NOUL · P</th><th class="jev">Severity</th>
      <th class="kev">${esc(label("kev"))} team</th><th class="kev">NOUL · P</th><th class="kev">Severity</th>
      <th>KEV vs JEV</th><th class="num jev">ms</th><th class="num kev">ms</th>${hasGt ? "<th>Expected</th>" : ""}</tr></thead>
    <tbody>${rows || `<tr><td colspan="13" class="muted">No tickets match this filter.</td></tr>`}</tbody>`;
  $("batch-table").querySelectorAll("tr.clickable").forEach((tr) =>
    tr.addEventListener("click", () => {
      const r = run.results[Number(tr.dataset.i)];
      fillTicket(r.ticket);
      $("sample-select").value = "";
      state.single = null;
      renderSingle(r, null, runMode(run), run.settings?.hitl_threshold);
      showTab("single");
      window.scrollTo({ top: 0, behavior: "smooth" });
    })
  );
}

$("batch-filters").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-filter]");
  if (!b) return;
  state.filter = b.dataset.filter;
  $("batch-filters").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  if (state.run) renderBatchTable(state.run);
});

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------
async function loadRuns(selectId) {
  try {
    const runs = await api("/api/runs").then((r) => r.json());
    const sel = $("run-select");
    const keep = selectId ?? state.run?.id ?? sel.value;
    const verdictTxt = { replace: "✔ KEV can replace", keep_baseline: "✖ keep JEV", insufficient_data: "insufficient data" };
    sel.innerHTML = runs.length
      ? runs.map((r) => `<option value="${esc(r.id)}">${esc(r.name ?? r.id)} · ${esc((r.created_at ?? "").replace("T", " ").slice(0, 16))} · ${r.tickets} tickets · ${esc(verdictTxt[r.decision] ?? r.status)}</option>`).join("")
      : `<option value="">No saved runs yet</option>`;
    if (keep && runs.some((r) => r.id === keep)) sel.value = keep;
    if (!state.run && runs.length) await selectRun(sel.value);
  } catch (err) {
    toast(err.message);
  }
}

async function selectRun(id) {
  if (!id) return;
  state.run = await api(`/api/runs/${id}`).then((r) => r.json());
  renderBatch(state.run);
  if (!$("panel-dashboard").hidden) renderDashboard(state.run);
}
$("run-select").addEventListener("change", (e) => selectRun(e.target.value));
$("run-refresh").addEventListener("click", () => loadRuns());

function destroyCharts() {
  state.charts.forEach((c) => c.destroy());
  state.charts = [];
}

function hbars(title, a, b, f, lowerBetter) {
  const max = Math.max(a ?? 0, b ?? 0) || 1;
  const win = a != null && b != null && a !== b ? ((lowerBetter ? a < b : a > b) ? "jev" : "kev") : null;
  const bar = (k, v) => `<div class="row" style="gap:8px;margin-top:4px">
      <div style="flex:1;height:12px;background:var(--surface);border-radius:3px">
        <div style="height:100%;width:${v == null ? 0 : Math.max(2, (100 * v) / max)}%;background:var(--${k});border-radius:3px"></div></div>
      <span class="small" style="width:92px;text-align:right;font-variant-numeric:tabular-nums;${win === k ? "font-weight:750;color:var(--good-text)" : ""}">${f(v)}${win === k ? " ★" : ""}</span></div>`;
  return `<div class="mini"><div class="t">${title}</div>${bar("jev", a)}${bar("kev", b)}</div>`;
}

function kpiVs(title, a, b, f, note) {
  return `<div class="card kpi"><div class="k">${title}</div>
    <div class="vs"><span style="color:var(--jev)">${f(a)}</span><span class="muted small">vs</span><span style="color:var(--kev)">${f(b)}</span></div>
    <div class="d">${note ?? `${esc(label("jev"))} vs ${esc(label("kev"))}`}</div></div>`;
}

function confusion(cells, ids, names, showNames = false) {
  const grid = new Map(cells.map((c) => [`${c.jev}|${c.kev}`, c.count]));
  const present = ids.filter((i) => cells.some((c) => c.jev === i || c.kev === i));
  const use = present.length ? present : ids;
  const max = Math.max(1, ...cells.map((c) => c.count));
  const name = (i) => (showNames ? names[i] ?? i : i);
  const head = use.map((i) => `<th title="${esc(names[i] ?? i)}">${esc(name(i))}</th>`).join("");
  const rows = use
    .map((a) => {
      const tds = use
        .map((b) => {
          const n = grid.get(`${a}|${b}`) ?? 0;
          const alpha = n ? 0.15 + (0.85 * n) / max : 0;
          return `<td class="${a === b ? "diag" : ""}" style="background:rgba(42,120,214,${alpha.toFixed(2)});${alpha > 0.55 ? "color:#fff" : ""}" title="${esc(label("jev"))}: ${esc(names[a] ?? a)} → ${esc(label("kev"))}: ${esc(names[b] ?? b)} · ${n}">${n || ""}</td>`;
        })
        .join("");
      return `<tr><th title="${esc(names[a] ?? a)}">${esc(name(a))}</th>${tds}</tr>`;
    })
    .join("");
  return `<div class="table-wrap"><table class="cm"><caption>Rows: ${esc(label("jev"))} · Columns: ${esc(label("kev"))} · green outline = agreement</caption>
    <thead><tr><th></th>${head}</tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderDashboard(run) {
  destroyCharts();
  const s = run.summary;
  if (!s) {
    $("dashboard").innerHTML = `<div class="empty">This run has no summary (status: ${esc(run.status)}).</div>`;
    return;
  }
  if (!s.models.jev.ttfb_ms) {
    $("dashboard").innerHTML = `<div class="empty">This run was recorded with an older format (chat completions) and cannot be shown. Run a new batch.</div>`;
    return;
  }
  $("dash-exports").innerHTML = exportLinks(run);
  const { jev, kev } = s.models;
  const p = s.pair;
  const v = s.verdict;
  const hasGt = (jev.accuracy.labelled || kev.accuracy.labelled) > 0;
  const hasCons = jev.consistency && kev.consistency;
  const legend = `<div class="legend"><span><i style="background:var(--jev)"></i>${esc(label("jev"))}</span><span><i style="background:var(--kev)"></i>${esc(label("kev"))}</span></div>`;
  const statusOrder = ["aligned", "partial", "diverges", "error"];
  const statusVar = { aligned: "--good", partial: "--warning", diverges: "--critical", error: "--neutral" };
  const totalStatus = statusOrder.reduce((n, k) => n + (p.status_counts[k] ?? 0), 0) || 1;
  const noulNames = { true: outcome(true).name, false: outcome(false).name };

  $("dashboard").innerHTML = `
  <div class="stack">
    ${run.totals ? `<div><h2 style="font-size:15px;margin:0 0 10px">Batch run — time &amp; cost</h2>${totalsTiles(run)}</div>` : ""}
    <div class="card verdict ${v.decision === "replace" ? "replace" : ""}">
      <div class="small muted">Decision — can ${esc(label("kev"))} replace ${esc(label("jev"))}?</div>
      <p class="headline">${v.decision === "replace" ? "✔" : "✖"} ${esc(v.headline)}</p>
      <div class="small muted" style="margin:-4px 0 8px">Human-review rule: <strong>${esc(modeLabel(runMode(run)))}</strong> — ${esc(ruleText(runMode(run), run.settings?.hitl_threshold))}</div>
      ${v.checks.map((c) => `<div class="check"><span class="${c.passed ? "ok" : "bad"}">${c.passed ? "✔" : "✖"}</span>
        <span>${esc(c.name)}: <strong>${Number.isInteger(c.value) ? c.value : fmt.pct(c.value)}</strong> <span class="muted">(target ${esc(c.target)})</span></span></div>`).join("")}
      ${v.notes.map((n) => `<div class="small muted" style="margin-top:4px">${esc(n)}</div>`).join("")}
    </div>

    <div class="kpis">
      <div class="card kpi"><div class="k">Full agreement (team + NOUL)</div><div class="v">${fmt.pct(p.full_agreement)}</div><div class="d">${p.compared} tickets compared</div></div>
      <div class="card kpi"><div class="k">Team agreement</div><div class="v">${fmt.pct(p.team_agreement)}</div><div class="d">NOUL agreement ${fmt.pct(p.noul_agreement)}</div></div>
      <div class="card kpi"><div class="k">Mean |Δ| KEV vs JEV</div><div class="v">${fmt.num(p.severity_mean_abs_delta)}</div><div class="d">severity levels · P(critical) ${fmt.pct(p.noul_mean_abs_delta)}</div></div>
      ${kpiVs("Human-review rate", jev.hitl_rate, kev.hitl_rate, fmt.pct, `${jev.hitl_count} vs ${kev.hitl_count} tickets · ${esc(modeLabel(runMode(run)))}`)}
      ${kpiVs("Avg latency", jev.latency_ms.avg, kev.latency_ms.avg, fmt.ms, `TTFB ${fmt.ms(jev.ttfb_ms.avg)} vs ${fmt.ms(kev.ttfb_ms.avg)}`)}
      ${kpiVs("Cost per 1k tickets", jev.cost.per_1k_tickets_usd, kev.cost.per_1k_tickets_usd, fmt.usd)}
      ${hasGt ? kpiVs("Accuracy (team + NOUL)", jev.accuracy.both, kev.accuracy.both, fmt.pct, `vs ground truth · ${jev.accuracy.labelled} labelled`) : ""}
    </div>

    <div class="card">
      <h2>How often KEV deviates from JEV</h2>
      <div class="statusbar">${statusOrder.filter((k) => p.status_counts[k]).map((k) => `<div style="flex:${p.status_counts[k]};background:var(${statusVar[k]});color:${k === "partial" || k === "error" ? "#0b0b0b" : "#fff"}" title="${k}: ${p.status_counts[k]}">${k} · ${p.status_counts[k]} (${fmt.pct(p.status_counts[k] / totalStatus)})</div>`).join("")}</div>
      <p class="small muted" style="margin:8px 0 0">Aligned = same team &amp; NOUL decision and severity within ±0.5 levels · Partial = team or NOUL differs (or severity drifts) · Diverges = both differ.</p>
    </div>

    <div class="card">
      <h2>Speed, tokens &amp; cost</h2>${legend}
      <div class="mini-grid">
        ${hbars("Avg latency", jev.latency_ms.avg, kev.latency_ms.avg, fmt.ms, true)}
        ${hbars("p95 latency", jev.latency_ms.p95, kev.latency_ms.p95, fmt.ms, true)}
        ${hbars("Avg TTFB", jev.ttfb_ms.avg, kev.ttfb_ms.avg, fmt.ms, true)}
        ${hbars("p95 TTFB", jev.ttfb_ms.p95, kev.ttfb_ms.p95, fmt.ms, true)}
        ${hbars("Input tokens / call", jev.tokens.input_avg, kev.tokens.input_avg, fmt.tok, true)}
        ${hbars("Output tokens / call", jev.tokens.output_avg, kev.tokens.output_avg, fmt.tok, true)}
        ${hbars("Cost / call", jev.cost.per_call_usd, kev.cost.per_call_usd, fmt.usd, true)}
        ${hbars("Cost / 1k tickets", jev.cost.per_1k_tickets_usd, kev.cost.per_1k_tickets_usd, fmt.usd, true)}
      </div>
      <p class="small muted" style="margin:8px 0 0">★ = better value. Each panel has its own scale. TTFB replaces TTFT (the Decisions API does not stream).
        Providers — ${esc(label("jev"))}: ${esc(Object.keys(jev.providers ?? {}).join(", ") || "—")} · ${esc(label("kev"))}: ${esc(Object.keys(kev.providers ?? {}).join(", ") || "—")}.
        Calls failed / invalid / retried — ${esc(label("jev"))}: ${jev.failed_calls} / ${jev.invalid_outputs} / ${jev.retried_calls} ·
        ${esc(label("kev"))}: ${kev.failed_calls} / ${kev.invalid_outputs} / ${kev.retried_calls}</p>
    </div>

    <div class="dash-grid">
      <div class="card"><h2>Triage quality</h2>${legend}<div class="chart-box"><canvas id="ch-quality"></canvas></div></div>
      <div class="card"><h2>NOUL — tickets flagged critical</h2><div class="chart-box"><canvas id="ch-noul"></canvas></div></div>
      <div class="card"><h2>Team distribution</h2>${legend}<div class="chart-box"><canvas id="ch-team"></canvas></div></div>
      <div class="card"><h2>Severity distribution</h2>${legend}<div class="chart-box"><canvas id="ch-sev"></canvas></div></div>
      <div class="card"><h2>P(critical) — ${esc(label("jev"))} vs ${esc(label("kev"))}</h2>
        <div class="chart-box sq"><canvas id="ch-noul-scatter"></canvas></div>
        <p class="small muted" style="margin:6px 0 0">Each dot is a ticket. On the dashed diagonal = same probability. The shaded band is where NOUL confidence &lt; ${fmt.pct(run.settings?.hitl_threshold ?? state.config.hitl_threshold)}${modeChecks(runMode(run)).noul ? ` (${esc(label("kev"))}, y axis, goes to human review)` : " (not checked by this run's rule)"}.</p></div>
      <div class="card"><h2>Severity — ${esc(label("jev"))} vs ${esc(label("kev"))}</h2>
        <div class="chart-box sq"><canvas id="ch-scatter"></canvas></div>
        <p class="small muted" style="margin:6px 0 0">Each dot is a ticket (probability-weighted position on the severity scale). On the dashed diagonal = same score.</p></div>
      <div class="card"><h2>Team assignment: where KEV deviates</h2>${confusion(p.team_confusion, state.config.teams.map((t) => t.id), Object.fromEntries(state.config.teams.map((t) => [t.id, t.name])))}</div>
      <div class="card"><h2>NOUL decision: where KEV deviates</h2>${confusion(p.noul_confusion, ["true", "false"], noulNames, true)}</div>
      ${hasGt ? `<div class="card"><h2>Calibration — team confidence</h2>${legend}<div class="chart-box sq"><canvas id="ch-cal-team"></canvas></div>
        <p class="small muted" style="margin:6px 0 0">Points on the diagonal = well calibrated. Below it = over-confident. Red dashed line = review threshold.</p></div>
      <div class="card"><h2>Calibration — NOUL confidence</h2>${legend}<div class="chart-box sq"><canvas id="ch-cal-noul"></canvas></div></div>` : ""}
    </div>
  </div>`;

  const grid = css("--grid"), jevC = css("--jev"), kevC = css("--kev"), surface = css("--surface"), ink2 = css("--ink-2");
  const pctAxis = { min: 0, max: 1, ticks: { callback: (x) => `${Math.round(x * 100)}%` }, grid: { color: grid } };
  const barDs = (k, data) => ({ label: label(k), data, backgroundColor: k === "jev" ? jevC : kevC, borderRadius: 4, borderSkipped: "start", maxBarThickness: 28 });
  const baseOpts = { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, interaction: { mode: "index", intersect: false } };
  const dotDs = (data, color) => ({ label: "Tickets", data, backgroundColor: color + "b3", borderColor: surface, borderWidth: 2, pointRadius: 6, pointHoverRadius: 8 });
  const diagDs = (lo, hi) => ({ type: "line", label: "Same answer", data: [{ x: lo, y: lo }, { x: hi, y: hi }], borderColor: css("--axis"), borderDash: [5, 5], borderWidth: 1.5, pointRadius: 0 });

  // Quality: every value shares a 0–100% unit.
  const qual = [
    ["Human-review rate ↓", jev.hitl_rate, kev.hitl_rate],
    ["Avg team conf.", jev.avg_team_confidence, kev.avg_team_confidence],
    ["Avg NOUL conf.", jev.avg_noul_confidence, kev.avg_noul_confidence],
    ["Avg severity conf.", jev.avg_severity_confidence, kev.avg_severity_confidence],
  ];
  if (hasGt) qual.push(["Team accuracy", jev.accuracy.team, kev.accuracy.team], ["NOUL accuracy", jev.accuracy.noul, kev.accuracy.noul], ["Team+NOUL acc.", jev.accuracy.both, kev.accuracy.both]);
  if (hasCons) qual.push(["Team consistency", jev.consistency.team, kev.consistency.team], ["NOUL consistency", jev.consistency.noul, kev.consistency.noul]);
  state.charts.push(new Chart($("ch-quality"), {
    type: "bar",
    data: { labels: qual.map((q) => q[0]), datasets: [barDs("jev", qual.map((q) => q[1])), barDs("kev", qual.map((q) => q[2]))] },
    options: { ...baseOpts, scales: { y: pctAxis, x: { grid: { display: false } } },
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${fmt.pct(c.raw)}` } } } },
  }));

  // NOUL: stacked horizontal bar per model, critical first.
  state.charts.push(new Chart($("ch-noul"), {
    type: "bar",
    data: {
      labels: KEYS.map(label),
      datasets: ["true", "false"].map((o) => ({
        label: noulNames[o],
        data: KEYS.map((k) => s.models[k].distribution.critical[o] ?? 0),
        backgroundColor: css(NOUL_VAR[o === "true"]),
        borderColor: surface, borderWidth: { right: 2 }, borderSkipped: false, borderRadius: 3,
      })),
    },
    options: { ...baseOpts, indexAxis: "y", interaction: { mode: "nearest", intersect: true },
      scales: { x: { stacked: true, grid: { color: grid }, title: { display: true, text: "Tickets" }, ticks: { precision: 0 } }, y: { stacked: true, grid: { display: false } } },
      plugins: { legend: { display: true, position: "bottom", labels: { boxWidth: 10, color: ink2 } } } },
  }));

  // Teams: grouped bars, team order from YAML.
  const teams = state.config.teams;
  state.charts.push(new Chart($("ch-team"), {
    type: "bar",
    data: { labels: teams.map((t) => t.name), datasets: KEYS.map((k) => barDs(k, teams.map((t) => s.models[k].distribution.team[t.id] ?? 0))) },
    options: { ...baseOpts, scales: { y: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: grid }, title: { display: true, text: "Tickets" } },
      x: { grid: { display: false }, ticks: { autoSkip: false, maxRotation: 40 } } } },
  }));

  // Severity levels: grouped bars in scale order.
  const lv = levels();
  state.charts.push(new Chart($("ch-sev"), {
    type: "bar",
    data: { labels: lv.map((l) => l.name), datasets: KEYS.map((k) => barDs(k, lv.map((l) => s.models[k].distribution.severity[l.id] ?? 0))) },
    options: { ...baseOpts, scales: { y: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: grid }, title: { display: true, text: "Tickets" } }, x: { grid: { display: false } } } },
  }));

  // P(critical) scatter with the human-review band.
  const th = state.config.hitl_threshold;
  const band = (lo, hi) => ({ type: "line", label: "Review band", data: [{ x: 0, y: hi }, { x: 1, y: hi }], fill: { target: { value: lo }, above: css("--hitl-bg") },
    borderWidth: 0, pointRadius: 0 });
  state.charts.push(new Chart($("ch-noul-scatter"), {
    type: "scatter",
    data: { datasets: [band(1 - th, th), diagDs(0, 1), dotDs(p.noul_scatter.map((pt) => ({ x: pt.jev, y: pt.kev, id: pt.id })), jevC)] },
    options: { ...baseOpts, interaction: { mode: "nearest", intersect: true },
      scales: { x: { ...pctAxis, title: { display: true, text: `${label("jev")} P(critical)` } }, y: { ...pctAxis, title: { display: true, text: `${label("kev")} P(critical)` } } },
      plugins: { legend: { display: false }, tooltip: { filter: (c) => c.dataset.label === "Tickets",
        callbacks: { label: (c) => `${c.raw.id}: ${label("jev")} ${fmt.pct(c.raw.x)} / ${label("kev")} ${fmt.pct(c.raw.y)}` } } } },
  }));

  // Severity scatter on the level scale.
  const sevAxis = (t) => ({ min: -0.2, max: lv.length - 0.8, grid: { color: grid }, title: { display: true, text: t },
    afterBuildTicks: (axis) => { axis.ticks = lv.map((_, i) => ({ value: i })); }, ticks: { callback: (x) => lv[x]?.name ?? "" } });
  state.charts.push(new Chart($("ch-scatter"), {
    type: "scatter",
    data: { datasets: [diagDs(0, lv.length - 1), dotDs(p.severity_scatter.map((pt) => ({ x: pt.jev, y: pt.kev, id: pt.id })), jevC)] },
    options: { ...baseOpts, interaction: { mode: "nearest", intersect: true },
      scales: { x: sevAxis(`${label("jev")} severity`), y: sevAxis(`${label("kev")} severity`) },
      plugins: { legend: { display: false }, tooltip: { filter: (c) => c.dataset.label === "Tickets",
        callbacks: { label: (c) => `${c.raw.id}: ${label("jev")} ${fmt.num(c.raw.x)} / ${label("kev")} ${fmt.num(c.raw.y)}` } } } },
  }));

  if (hasGt) {
    for (const dim of ["team", "noul"]) {
      const series = KEYS.map((k) => ({
        label: label(k),
        data: s.models[k].calibration[dim].filter((b) => b.count && b.accuracy != null).map((b) => ({ x: b.avg_confidence, y: b.accuracy, n: b.count, bin: b.bin })),
        borderColor: k === "jev" ? jevC : kevC, backgroundColor: k === "jev" ? jevC : kevC,
        borderWidth: 2, pointRadius: 5, pointBorderColor: surface, pointBorderWidth: 2, pointHoverRadius: 7, tension: 0,
      }));
      state.charts.push(new Chart($(`ch-cal-${dim}`), {
        type: "line",
        data: { datasets: [
          ...series,
          { label: "Perfect calibration", data: [{ x: 0, y: 0 }, { x: 1, y: 1 }], borderColor: css("--axis"), borderDash: [5, 5], borderWidth: 1.5, pointRadius: 0 },
          { label: "Review threshold", data: [{ x: th, y: 0 }, { x: th, y: 1 }], borderColor: css("--critical"), borderDash: [2, 3], borderWidth: 1, pointRadius: 0 },
        ] },
        options: { ...baseOpts, interaction: { mode: "nearest", intersect: true },
          scales: { x: { type: "linear", ...pctAxis, title: { display: true, text: "Stated confidence" } }, y: { ...pctAxis, title: { display: true, text: "Actual accuracy" } } },
          plugins: { legend: { display: false }, tooltip: { filter: (c) => c.datasetIndex < 2,
            callbacks: { label: (c) => `${c.dataset.label} · bin ${c.raw.bin}: n=${c.raw.n}, confidence ${fmt.pct(c.raw.x)}, accuracy ${fmt.pct(c.raw.y)}` } } } },
      }));
    }
  }
}

// ---------------------------------------------------------------------------
// Definitions
// ---------------------------------------------------------------------------
function renderConfig() {
  const c = state.config;
  const n = c.noul;
  $("cfg-fp").textContent = c.taxonomy_fingerprint;
  $("config-defs").innerHTML = `
    <div class="card"><h2>Teams <span class="muted small">choice</span></h2><p class="small muted" style="margin-top:0">${esc(c.teams_instructions ?? "")}</p>
      <table class="defs"><tbody>${c.teams.map((t) => `<tr><td><strong>${esc(t.name)}</strong><br><code class="small">${esc(t.id)}</code></td><td>${esc(t.description)}</td></tr>`).join("")}</tbody></table></div>
    <div class="card"><h2>NOUL — criticality <span class="muted small">noul</span></h2><p class="small muted" style="margin-top:0">${esc(n.instructions)}</p>
      <table class="defs"><tbody>${["true", "false"].map((k) => `<tr><td>${noulPill(k === "true")}</td><td>SLA ${esc(n.outcomes[k].response_sla)} · ${esc(n.outcomes[k].action)}<br><span class="muted">${esc(n.criteria[k])}</span></td></tr>`).join("")}</tbody></table>
      <p class="small muted">Critical when P(critical) ≥ ${fmt.pct(n.decision_threshold ?? 0.5)}. NOUL confidence = max(P, 1 − P). Default human-review rule: ${esc(modeLabel(c.hitl_mode))} below ${fmt.pct(c.hitl_threshold)}.</p></div>
    <div class="card"><h2>Severity <span class="muted small">score</span></h2><p class="small muted" style="margin-top:0">${esc(c.severity.instructions)}</p>
      <table class="defs"><tbody>${c.severity.levels.map((l, i) => `<tr><td style="white-space:nowrap"><span class="pill noul" style="background:${severityColor(i)}">${i} · ${esc(l.name)}</span></td><td><span class="muted">${esc(l.definition)}</span></td></tr>`).join("")}</tbody></table></div>`;
  $("config-yaml").innerHTML = Object.entries(c.raw_yaml).map(([name, text]) => `<div class="card"><h3>config/${esc(name)}</h3><pre>${esc(text)}</pre></div>`).join("");
}

init().catch((err) => {
  document.querySelector("main").insertAdjacentHTML("afterbegin", `<div class="error-box">Failed to load: ${esc(err.message)}</div>`);
});
