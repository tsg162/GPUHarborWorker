/* GPUHarbor Dashboard — Single Page App */

const POLL_INTERVAL = 3000; // ms
let currentLogSource = null; // EventSource for live logs
let statusTimer = null;
let jobsTimer = null;

// ── Helpers ─────────────────────────────────────────────────

function $(sel) {
  return document.querySelector(sel);
}

function formatUptime(seconds) {
  if (!seconds && seconds !== 0) return "--";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const parts = [];
  if (d) parts.push(`${d}d`);
  if (h) parts.push(`${h}h`);
  if (m) parts.push(`${m}m`);
  parts.push(`${s}s`);
  return parts.join(" ");
}

function timeAgo(isoStr) {
  if (!isoStr) return "--";
  const diff = (Date.now() - new Date(isoStr).getTime()) / 1000;
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function barColorClass(pct) {
  if (pct > 85) return "bar-red";
  if (pct > 60) return "bar-yellow";
  return "bar-green";
}

function barValueColor(pct) {
  if (pct > 85) return "var(--red)";
  if (pct > 60) return "var(--yellow)";
  return "var(--green)";
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// ── Status Polling ──────────────────────────────────────────

async function fetchStatus() {
  try {
    const res = await fetch("/api/status");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderStatus(data);
    setConnected(true);
  } catch (err) {
    console.error("Status fetch failed:", err);
    setConnected(false);
  }
}

function setConnected(connected) {
  const el = $("#connection-status");
  if (connected) {
    el.textContent = "connected";
    el.className = "badge badge-success";
  } else {
    el.textContent = "disconnected";
    el.className = "badge badge-error";
  }
}

function renderStatus(data) {
  // Header
  $("#hostname").textContent = data.hostname || "--";
  $("#version").textContent = `v${data.worker_version || "?"}`;
  $("#uptime").textContent = formatUptime(data.uptime_seconds);

  // System stats
  $("#cpu-count").textContent = data.cpu_count || "--";
  $("#jobs-running").textContent = data.running_jobs ?? 0;
  $("#jobs-max").textContent = data.max_concurrent_jobs ?? "--";

  // RAM
  const ramTotal = data.ram_gb || 0;
  const ramUsed = data.ram_used_gb || 0;
  const ramPct = ramTotal > 0 ? Math.round((ramUsed / ramTotal) * 100) : 0;
  $("#ram-value").textContent = `${ramUsed} / ${ramTotal} GB`;
  $("#ram-value").style.color = barValueColor(ramPct);
  const ramBar = $("#ram-bar");
  ramBar.style.width = `${ramPct}%`;
  ramBar.className = `bar ${barColorClass(ramPct)}`;
  $("#ram-detail").textContent = `${ramPct}% used`;

  // Disk
  $("#disk-value").textContent = `${data.disk_free_gb ?? "--"} GB`;

  // GPUs
  renderGPUs(data.gpus || []);
}

function renderGPUs(gpus) {
  const container = $("#gpu-cards");

  if (!gpus.length) {
    container.innerHTML = '<div class="empty-state">No GPUs detected</div>';
    return;
  }

  container.innerHTML = gpus.map((gpu) => {
    const memTotal = gpu.memory_gb || 0;
    const memUsed = gpu.memory_used_gb || 0;
    const memPct = memTotal > 0 ? Math.round((memUsed / memTotal) * 100) : 0;
    const utilPct = gpu.utilization_pct || 0;

    return `
      <div class="gpu-card">
        <div class="gpu-card-header">
          <span class="gpu-name">${escapeHtml(gpu.model)}</span>
          <span class="gpu-index">GPU ${gpu.index}</span>
        </div>
        <div class="gpu-metrics">
          ${gpuBar("VRAM", `${memUsed} / ${memTotal} GB`, memPct)}
          ${gpuBar("Utilization", `${utilPct}%`, utilPct)}
        </div>
      </div>
    `;
  }).join("");
}

function gpuBar(label, valueText, pct) {
  return `
    <div class="gpu-metric">
      <div class="bar-label-row">
        <span class="bar-label">${label}</span>
        <span class="bar-value" style="color: ${barValueColor(pct)}">${valueText} (${pct}%)</span>
      </div>
      <div class="bar-container bar-wide">
        <div class="bar ${barColorClass(pct)}" style="width: ${pct}%"></div>
      </div>
    </div>
  `;
}

// ── Jobs Polling ────────────────────────────────────────────

async function fetchJobs() {
  try {
    const res = await fetch("/api/jobs?limit=50");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderJobs(data.jobs || []);
  } catch (err) {
    console.error("Jobs fetch failed:", err);
  }
}

function renderJobs(jobs) {
  const container = $("#jobs-list");

  if (!jobs.length) {
    container.innerHTML = '<div class="empty-state">No jobs submitted yet</div>';
    return;
  }

  // Sort: running first, then by created_at desc
  const stateOrder = {
    running: 0,
    checkpointing: 0,
    cancel_requested: 1,
    uploading_inputs: 1,
    created: 2,
    completed: 3,
    canceled: 4,
    failed: 4,
  };

  jobs.sort((a, b) => {
    const oa = stateOrder[a.state] ?? 5;
    const ob = stateOrder[b.state] ?? 5;
    if (oa !== ob) return oa - ob;
    return (b.created_at || "").localeCompare(a.created_at || "");
  });

  container.innerHTML = jobs.map((job) => {
    const stateClass = `state-${job.state}`;
    const name = job.name || job.job_id.slice(0, 12);
    const idShort = job.job_id.length > 12 ? job.job_id.slice(0, 12) + "..." : job.job_id;

    return `
      <div class="job-row" data-job-id="${escapeHtml(job.job_id)}">
        <div>
          <div class="job-name">${escapeHtml(name)}</div>
          <div class="job-id">${escapeHtml(idShort)}</div>
        </div>
        <div>
          <span class="badge ${stateClass}">${escapeHtml(job.state)}</span>
        </div>
        <div class="job-project">${escapeHtml(job.project || "--")}</div>
        <div class="job-time">${timeAgo(job.created_at)}</div>
        <div class="job-actions">
          <button class="btn btn-sm btn-logs" data-job-id="${escapeHtml(job.job_id)}" data-state="${escapeHtml(job.state)}">Logs</button>
          <button class="btn btn-sm btn-detail" data-job-id="${escapeHtml(job.job_id)}">Detail</button>
        </div>
      </div>
    `;
  }).join("");

  // Attach event listeners
  container.querySelectorAll(".btn-logs").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const jobId = btn.dataset.jobId;
      const state = btn.dataset.state;
      openLogs(jobId, state);
    });
  });

  container.querySelectorAll(".btn-detail").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      openJobDetail(btn.dataset.jobId);
    });
  });
}

// ── Log Viewer ──────────────────────────────────────────────

function openLogs(jobId, state) {
  closeLogs();

  const section = $("#log-section");
  section.classList.remove("hidden");
  $("#log-job-id").textContent = jobId.slice(0, 16);
  const viewer = $("#log-viewer");
  viewer.innerHTML = '<span class="dim">Loading logs...</span>';

  const isLive = state === "running" || state === "checkpointing" || state === "uploading_inputs";

  if (isLive) {
    // SSE streaming
    const es = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/logs?follow=true`);
    currentLogSource = es;
    viewer.innerHTML = "";

    es.addEventListener("log", (e) => {
      const line = document.createElement("span");
      line.className = "log-line";
      line.textContent = e.data + "\n";
      viewer.appendChild(line);
      viewer.scrollTop = viewer.scrollHeight;
    });

    es.addEventListener("done", () => {
      const line = document.createElement("span");
      line.className = "log-line dim";
      line.textContent = "\n--- stream ended ---\n";
      viewer.appendChild(line);
      es.close();
      currentLogSource = null;
    });

    es.addEventListener("error", () => {
      es.close();
      currentLogSource = null;
      // Fall back to static fetch
      fetchStaticLogs(jobId, viewer);
    });
  } else {
    fetchStaticLogs(jobId, viewer);
  }

  // Scroll to logs
  section.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function fetchStaticLogs(jobId, viewer) {
  try {
    const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/logs?tail=500`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const lines = data.logs || [];
    if (!lines.length) {
      viewer.innerHTML = '<span class="dim">No logs available</span>';
      return;
    }
    viewer.innerHTML = lines.map((l) => {
      const span = document.createElement("span");
      span.className = "log-line";
      span.textContent = l + "\n";
      return span.outerHTML;
    }).join("");
    viewer.scrollTop = viewer.scrollHeight;
  } catch (err) {
    viewer.innerHTML = `<span class="dim">Failed to load logs: ${escapeHtml(err.message)}</span>`;
  }
}

function closeLogs() {
  if (currentLogSource) {
    currentLogSource.close();
    currentLogSource = null;
  }
  $("#log-section").classList.add("hidden");
  $("#log-viewer").innerHTML = "";
}

// ── Job Detail Modal ────────────────────────────────────────

async function openJobDetail(jobId) {
  try {
    const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const job = await res.json();
    showJobDetailModal(job);
  } catch (err) {
    console.error("Job detail fetch failed:", err);
  }
}

function showJobDetailModal(job) {
  // Remove existing modal
  const existing = document.querySelector(".job-detail-overlay");
  if (existing) existing.remove();

  const stateClass = `state-${job.state}`;

  // Build metrics section
  let metricsHtml = "";
  if (job.metrics && typeof job.metrics === "object" && Object.keys(job.metrics).length) {
    const m = job.metrics;
    metricsHtml = `
      <h4 style="color: var(--cyan); margin: 1rem 0 0.5rem; font-size: 0.9rem;">Latest Metrics</h4>
      <div class="detail-grid">
        ${m.step != null ? `<dt>Step</dt><dd>${m.step}</dd>` : ""}
        ${m.epoch != null ? `<dt>Epoch</dt><dd>${m.epoch}</dd>` : ""}
        ${m.loss != null ? `<dt>Loss</dt><dd>${m.loss}</dd>` : ""}
        ${m.samples_per_sec != null ? `<dt>Samples/sec</dt><dd>${m.samples_per_sec}</dd>` : ""}
        ${m.gpu_util != null ? `<dt>GPU Util</dt><dd>${m.gpu_util}%</dd>` : ""}
        ${m.gpu_mem_gb != null ? `<dt>GPU Mem</dt><dd>${m.gpu_mem_gb} GB</dd>` : ""}
      </div>
    `;
  }

  // Build error section
  let errorHtml = "";
  if (job.error_message) {
    errorHtml = `
      <h4 style="color: var(--red); margin: 1rem 0 0.5rem; font-size: 0.9rem;">Error</h4>
      <div style="background: #1a0a0a; border: 1px solid var(--bar-red); border-radius: var(--radius); padding: 0.75rem; font-family: var(--font-mono); font-size: 0.8rem; white-space: pre-wrap; word-break: break-all;">${escapeHtml(job.error_message)}</div>
    `;
  }

  const overlay = document.createElement("div");
  overlay.className = "job-detail-overlay";
  overlay.innerHTML = `
    <div class="job-detail-panel">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem;">
        <h3 style="margin: 0;">${escapeHtml(job.name || job.job_id)}</h3>
        <button class="btn btn-sm detail-close">Close</button>
      </div>
      <div class="detail-grid">
        <dt>Job ID</dt><dd>${escapeHtml(job.job_id)}</dd>
        <dt>State</dt><dd><span class="badge ${stateClass}">${escapeHtml(job.state)}</span></dd>
        <dt>Project</dt><dd>${escapeHtml(job.project || "--")}</dd>
        <dt>Server</dt><dd>${escapeHtml(job.server_name || "--")}</dd>
        <dt>Created</dt><dd>${job.created_at || "--"}</dd>
        <dt>Started</dt><dd>${job.started_at || "--"}</dd>
        <dt>Completed</dt><dd>${job.completed_at || "--"}</dd>
      </div>
      ${metricsHtml}
      ${errorHtml}
    </div>
  `;

  // Close handlers
  overlay.querySelector(".detail-close").addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) overlay.remove();
  });

  document.body.appendChild(overlay);
}

// ── Init ────────────────────────────────────────────────────

function init() {
  // Log close button
  $("#log-close").addEventListener("click", closeLogs);

  // Initial fetch
  fetchStatus();
  fetchJobs();

  // Polling
  statusTimer = setInterval(fetchStatus, POLL_INTERVAL);
  jobsTimer = setInterval(fetchJobs, POLL_INTERVAL * 2); // jobs less frequent

  // Keyboard shortcut: Escape closes modals/logs
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeLogs();
      const overlay = document.querySelector(".job-detail-overlay");
      if (overlay) overlay.remove();
    }
  });
}

document.addEventListener("DOMContentLoaded", init);
