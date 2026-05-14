// Transcribe Studio — single-page UI controller.
// Polls /api/status every 2s. Rebuilds DOM only when the view changes;
// every other poll just patches the live bits (no flicker).

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const fmt = {
  duration(sec) {
    if (!sec) return "—";
    const s = Math.floor(sec);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    return h ? `${h}h ${m}m` : m ? `${m}m ${r}s` : `${r}s`;
  },
  bytes(b) {
    if (!b) return "—";
    const k = 1024;
    if (b < k*k) return `${(b/k).toFixed(0)} KB`;
    if (b < k*k*k) return `${(b/k/k).toFixed(1)} MB`;
    return `${(b/k/k/k).toFixed(2)} GB`;
  },
};

const state = {
  status: null,
  view: "dashboard",
  selectedProjectId: null,
  filters: { search: "", status: "all" },
  rendered: { view: null, projectId: null },  // what's currently in the DOM
  fileListLastFetchedFor: null,
  fileListLastFetchedAt: 0,
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

// ---------- polling ----------

async function pollStatus() {
  try {
    const prev = state.status;
    state.status = await api("/api/status");
    renderSidebar();

    const viewChanged = state.rendered.view !== state.view
                     || state.rendered.projectId !== state.selectedProjectId;
    if (viewChanged) {
      renderContentStructure();
      state.rendered.view = state.view;
      state.rendered.projectId = state.selectedProjectId;
    }
    updateLiveData(prev);
  } catch (e) {
    setAgentPill("error", "backend offline");
  }
}

function startPolling() {
  pollStatus();
  setInterval(pollStatus, 2000);
}

// ---------- sidebar ----------

function setAgentPill(klass, text) {
  const pill = $("#agent-pill");
  if (!pill) return;
  pill.classList.remove("running", "paused", "idle", "error");
  pill.classList.add(klass);
  $(".status-text", pill).textContent = text;
}

function renderSidebar() {
  const s = state.status;
  if (!s) return;
  const w = s.worker;
  const sys = s.system;
  $("#port-info").textContent = `${sys.host}:${sys.port}`;

  if (w.paused) setAgentPill("paused", "paused");
  else if (w.running) setAgentPill("running", "running");
  else if (sys.ac_power) setAgentPill("idle", "idle");
  else setAgentPill("idle", "on battery");

  const nav = $("#project-nav");
  // Always re-sync project list — but only if it actually changed (avoid flicker)
  const wantedIds = s.projects.map(p => p.id);
  const haveIds = $$(".nav-item[data-pid]", nav).map(el => el.dataset.pid);
  const sameSet = wantedIds.length === haveIds.length
                  && wantedIds.every((id, i) => id === haveIds[i]);
  if (!sameSet) {
    $$(".nav-item[data-pid]", nav).forEach(el => el.remove());
    s.projects.forEach(p => {
      const btn = document.createElement("button");
      btn.className = "nav-item";
      btn.dataset.view = "project";
      btn.dataset.pid = p.id;
      btn.innerHTML = `<span class="dot"></span>
                       <span class="label">${escapeHtml(p.name)}</span>
                       <span class="progress" data-progress></span>`;
      btn.onclick = () => switchView("project", p.id);
      nav.appendChild(btn);
    });
  }
  // Update progress numbers in-place
  s.projects.forEach(p => {
    const el = nav.querySelector(`.nav-item[data-pid="${p.id}"] [data-progress]`);
    if (el) el.textContent = `${p.counts.completed}/${p.total}`;
  });
  // Active state
  $$(".nav-item", nav).forEach(el => {
    const isActive = (el.dataset.view === state.view) &&
                     (state.view === "dashboard" || el.dataset.pid === state.selectedProjectId);
    el.classList.toggle("active", isActive);
  });

  // Dashboard click handler (idempotent)
  const dash = $('[data-view="dashboard"]', nav);
  if (dash && !dash.dataset.bound) {
    dash.onclick = () => switchView("dashboard", null);
    dash.dataset.bound = "1";
  }
}

function switchView(view, pid) {
  state.view = view;
  state.selectedProjectId = pid;
  pollStatus();
}

// ---------- content (structure rendered once per view; live data patched) ----------

function renderContentStructure() {
  if (state.view === "dashboard") return renderDashboardStructure();
  return renderProjectStructure();
}

function updateLiveData(prevStatus) {
  if (state.view === "dashboard") return updateDashboardLive(prevStatus);
  return updateProjectLive(prevStatus);
}

// ---------- dashboard ----------

function renderDashboardStructure() {
  const s = state.status;

  $("#content").innerHTML = `
    <header class="page-header">
      <div>
        <h2>All projects</h2>
        <div class="subtitle" id="dash-subtitle"></div>
      </div>
      <div class="actions" id="header-actions"></div>
    </header>

    <div class="card">
      <h3 class="card-title">Overview</h3>
      <div class="metric-row">
        <div class="metric"><div class="metric-value" id="m-done">—</div><div class="metric-label">Done</div></div>
        <div class="metric"><div class="metric-value" id="m-pending">—</div><div class="metric-label">Pending</div></div>
        <div class="metric"><div class="metric-value" id="m-failed">—</div><div class="metric-label">Failed</div></div>
        <div class="metric"><div class="metric-value" id="m-skipped">—</div><div class="metric-label">Skipped</div></div>
        <div class="metric"><div class="metric-value" id="m-pct">—</div><div class="metric-label">Complete</div></div>
      </div>
      <div class="progress-bar"><div class="fill" id="m-fill" style="width:0"></div></div>
    </div>

    <div id="now-playing-card"></div>

    <div class="card">
      <h3 class="card-title">Projects</h3>
      <div id="project-cards"></div>
    </div>
  `;
}

function updateDashboardLive(prevStatus) {
  const s = state.status;
  if (!s) return;

  const totalFiles = s.projects.reduce((n, p) => n + p.total, 0);
  const totalDone  = s.projects.reduce((n, p) => n + p.counts.completed, 0);
  const totalPend  = s.projects.reduce((n, p) => n + (p.counts.pending||0) + (p.counts.queued||0) + (p.counts.in_progress||0), 0);
  const totalFail  = s.projects.reduce((n, p) => n + p.counts.failed, 0);
  const totalSkip  = s.projects.reduce((n, p) => n + p.counts.skipped, 0);
  const pct        = totalFiles ? Math.round(totalDone/totalFiles*100) : 0;

  setText("dash-subtitle",
    `${s.projects.length} project${s.projects.length===1?"":"s"} · ${totalFiles} files`);

  setText("m-done", totalDone);
  setText("m-pending", totalPend);
  setText("m-failed", totalFail);
  setText("m-skipped", totalSkip);
  setText("m-pct", pct + "%");
  $("#m-fill").style.width = pct + "%";

  renderHeaderActions(s.worker.paused);
  renderNowPlayingInto("now-playing-card");
  renderProjectCardsInto("project-cards");
}

function renderProjectCardsInto(id) {
  const target = $("#" + id);
  if (!target) return;
  const s = state.status;

  if (s.projects.length === 0) {
    target.innerHTML = `<div class="empty-state">No projects yet. Click <strong>＋ New project</strong> on the left to add one.</div>`;
    return;
  }

  // Diff: rebuild only if project ids changed; otherwise patch values
  const wantedIds = s.projects.map(p => p.id);
  const haveIds = $$(".project-summary", target).map(el => el.dataset.pid);
  const sameSet = wantedIds.length === haveIds.length
                  && wantedIds.every((id, i) => id === haveIds[i]);

  if (!sameSet) {
    target.innerHTML = s.projects.map(projectCardHtml).join("");
    $$(".project-summary", target).forEach(el => {
      el.onclick = () => switchView("project", el.dataset.pid);
    });
  } else {
    // Patch numbers in place
    s.projects.forEach(p => {
      const card = target.querySelector(`.project-summary[data-pid="${p.id}"]`);
      if (!card) return;
      card.querySelector("[data-counts]").textContent = `${p.counts.completed} / ${p.total}`;
      const pct = p.total ? Math.round(p.progress * 100) : 0;
      card.querySelector("[data-pct]").textContent = `${pct}%`;
      card.querySelector("[data-fill]").style.width = `${pct}%`;
    });
  }
}

function projectCardHtml(p) {
  const pct = p.total ? Math.round(p.progress * 100) : 0;
  return `
    <div class="project-summary" data-pid="${p.id}" style="cursor:pointer; padding:14px 0; border-bottom:1px solid var(--border);">
      <div class="flex-between">
        <div>
          <div style="font-weight:600;">${escapeHtml(p.name)}</div>
          <div class="muted small">${p.model} · ${p.language}${p.translate ? " → en" : ""}${p.vad ? " · VAD" : ""}${!p.auto_run ? " · paused" : ""}</div>
        </div>
        <div style="text-align:right;">
          <div data-counts style="font-variant-numeric:tabular-nums; font-weight:600;">${p.counts.completed} / ${p.total}</div>
          <div data-pct class="muted small">${pct}%</div>
        </div>
      </div>
      <div class="progress-bar" style="margin-top:8px;"><div class="fill" data-fill style="width:${pct}%"></div></div>
    </div>
  `;
}

// ---------- project view ----------

function renderProjectStructure() {
  const s = state.status;
  const p = s?.projects.find(x => x.id === state.selectedProjectId);
  if (!p) {
    $("#content").innerHTML = `<div class="empty-state">Project not found.</div>`;
    return;
  }
  $("#content").innerHTML = `
    <header class="page-header">
      <div>
        <h2>${escapeHtml(p.name)}</h2>
        <div class="subtitle" id="proj-subtitle">${p.folders.map(escapeHtml).join(" · ")}</div>
      </div>
      <div class="actions" id="header-actions"></div>
    </header>

    <div class="card">
      <h3 class="card-title">Progress</h3>
      <div class="metric-row">
        <div class="metric"><div class="metric-value" id="p-done">—</div><div class="metric-label">Done</div></div>
        <div class="metric"><div class="metric-value" id="p-pending">—</div><div class="metric-label">Pending</div></div>
        <div class="metric"><div class="metric-value" id="p-failed">—</div><div class="metric-label">Failed</div></div>
        <div class="metric"><div class="metric-value" id="p-skipped">—</div><div class="metric-label">Skipped</div></div>
        <div class="metric"><div class="metric-value" id="p-pct">—</div><div class="metric-label">Complete</div></div>
      </div>
      <div class="progress-bar"><div class="fill" id="p-fill" style="width:0"></div></div>
      <div class="phase-row" id="p-tags" style="margin-top:14px;"></div>
    </div>

    <div id="now-playing-card"></div>

    <div class="card" id="files-card">
      <div class="flex-between" style="margin-bottom:12px;">
        <h3 class="card-title" style="margin:0;">Files</h3>
        <div class="search-row" style="margin:0;">
          <input type="text" id="file-search" placeholder="Search…" value="${escapeAttr(state.filters.search)}" />
          <select id="file-status">
            <option value="all">All statuses</option>
            <option value="pending">Pending</option>
            <option value="completed">Completed</option>
            <option value="failed">Failed</option>
            <option value="in_progress">In progress</option>
            <option value="queued">Queued</option>
            <option value="skipped">Skipped</option>
          </select>
        </div>
      </div>
      <div id="file-list"><div class="muted">Loading…</div></div>
    </div>
  `;

  $("#file-search").oninput = (e) => {
    state.filters.search = e.target.value.toLowerCase();
    renderFileListFromCache();
  };
  $("#file-status").value = state.filters.status;
  $("#file-status").onchange = (e) => {
    state.filters.status = e.target.value;
    renderFileListFromCache();
  };

  state.fileListLastFetchedFor = null;  // force re-fetch on first poll
  fetchAndRenderFileList(p.id);
}

function updateProjectLive(prevStatus) {
  const s = state.status;
  const p = s?.projects.find(x => x.id === state.selectedProjectId);
  if (!p) return;

  const counts = p.counts;
  const pending = (counts.pending||0) + (counts.queued||0) + (counts.in_progress||0);
  const pct = p.total ? Math.round(p.progress * 100) : 0;

  setText("p-done", counts.completed);
  setText("p-pending", pending);
  setText("p-failed", counts.failed);
  setText("p-skipped", counts.skipped);
  setText("p-pct", pct + "%");
  $("#p-fill").style.width = pct + "%";

  $("#p-tags").innerHTML = `
    <span class="tag accent">${p.model}</span>
    <span class="tag">${p.language}${p.translate ? " → en" : ""}</span>
    ${p.vad ? '<span class="tag">VAD</span>' : '<span class="tag warn">no VAD</span>'}
    ${p.no_context ? '<span class="tag">no-context</span>' : ""}
    <span class="tag">${p.ordering.replace("_", " ")}</span>
    ${p.auto_run ? '<span class="tag success">auto-run</span>' : '<span class="tag">manual</span>'}
  `;

  renderHeaderActions(s.worker.paused);
  renderNowPlayingInto("now-playing-card");

  // Refresh file list if:
  //   - >15s since last fetch
  //   - we just transitioned in/out of a running state (a file probably completed)
  const now = Date.now() / 1000;
  const stale = now - state.fileListLastFetchedAt > 15;
  const transitioned = !!(prevStatus
    && prevStatus.worker.current_event?.file !== s.worker.current_event?.file);
  if (stale || transitioned) {
    fetchAndRenderFileList(p.id);
  }
}

// ---------- header actions (pause/resume) ----------

function renderHeaderActions(paused) {
  const target = $("#header-actions");
  if (!target) return;
  target.innerHTML = paused
    ? `<button class="btn-primary" id="btn-resume">▶ Resume</button>`
    : `<button class="btn-secondary" id="btn-pause">⏸ Pause</button>`;
  if ($("#btn-resume")) $("#btn-resume").onclick = () =>
    api("/api/resume", { method: "POST" }).then(pollStatus);
  if ($("#btn-pause")) $("#btn-pause").onclick = () =>
    api("/api/pause", { method: "POST" }).then(pollStatus);
}

// ---------- now playing ----------

function renderNowPlayingInto(targetId) {
  const target = $("#" + targetId);
  if (!target) return;
  const s = state.status;
  const w = s.worker;
  if (!w.running) {
    if (!target.querySelector(".now-playing--idle")) {
      target.innerHTML = `<div class="card"><h3 class="card-title">Now transcribing</h3>
        <div class="empty-state now-playing--idle" style="padding:16px 0;" id="np-idle-msg"></div></div>`;
    }
    setText("np-idle-msg", w.paused
      ? "Paused."
      : `Idle — ${w.not_running_reason || "waiting"}`);
    return;
  }

  const ev = w.current_event || {};
  const payload = ev.payload || {};
  const filename = ev.file || "";
  const elapsed = fmt.duration(w.current_elapsed_sec);
  const project = s.projects.find(p => p.id === w.current_project);

  let phaseText = ev.phase || "running";
  let chunkInfo = "";
  if (ev.phase === "chunk" && payload.chunk && payload.of) {
    chunkInfo = `Chunk ${payload.chunk} / ${payload.of}`;
    phaseText = "transcribing";
  }

  // Build skeleton if not present, otherwise patch in place
  if (!target.querySelector(".now-playing")) {
    target.innerHTML = `<div class="card now-playing">
      <h3 class="card-title">Now transcribing</h3>
      <div class="filename" id="np-filename"></div>
      <div class="meta" id="np-meta"></div>
      <div class="chunk-progress" id="np-chunkbar" hidden><div class="fill" id="np-chunkfill" style="width:0"></div></div>
      <div class="phase-row" id="np-tags"></div>
      <pre class="log-tail" id="np-log"></pre>
    </div>`;
  }
  setText("np-filename", filename);
  setText("np-meta",
    `${project ? project.name : ""} · ${phaseText}${chunkInfo ? " · " + chunkInfo : ""} · ${elapsed}`);

  if (payload.chunk && payload.of) {
    $("#np-chunkbar").hidden = false;
    const pct = ((payload.chunk - 1) / payload.of) * 100;
    $("#np-chunkfill").style.width = pct + "%";
  } else {
    $("#np-chunkbar").hidden = true;
  }

  if (project) {
    $("#np-tags").innerHTML = `
      <span class="tag accent">${project.model}</span>
      <span class="tag">${project.language}${project.translate ? " → en" : ""}</span>
      ${project.vad ? '<span class="tag">VAD</span>' : ""}
      ${project.no_context ? '<span class="tag warn">no-context</span>' : ""}
    `;
  }

  // Append-only log update — preserves scroll if user scrolled up
  const logEl = $("#np-log");
  const newLog = (w.log_tail || []).slice(-60).join("\n") || "waiting for output...";
  if (logEl.textContent !== newLog) {
    const wasAtBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 20;
    logEl.textContent = newLog;
    if (wasAtBottom) logEl.scrollTop = logEl.scrollHeight;
  }
}

// ---------- file list ----------

let fileListCache = [];

async function fetchAndRenderFileList(pid) {
  try {
    fileListCache = await api(`/api/projects/${pid}/files`);
    state.fileListLastFetchedFor = pid;
    state.fileListLastFetchedAt = Date.now() / 1000;
    renderFileListFromCache();
  } catch (e) { /* keep old list */ }
}

function renderFileListFromCache() {
  const list = $("#file-list");
  if (!list) return;
  let rows = fileListCache;
  if (state.filters.status !== "all") {
    rows = rows.filter(r => r.status === state.filters.status);
  }
  if (state.filters.search) {
    rows = rows.filter(r => r.name.toLowerCase().includes(state.filters.search));
  }
  if (rows.length === 0) {
    list.innerHTML = `<div class="empty-state">No files match.</div>`;
    return;
  }
  list.innerHTML = `<table class="file-table">
    <thead><tr>
      <th>File</th><th>Folder</th><th>Size</th><th>Modified</th><th>Status</th><th></th>
    </tr></thead>
    <tbody>${rows.map(fileRow).join("")}</tbody>
  </table>`;
  bindFileRowHandlers();
}

function fileRow(r) {
  const folder = r.folder.split("/").slice(-2).join("/");
  const date = new Date(r.mtime * 1000).toLocaleDateString();
  const statusTag = ({
    completed:   '<span class="tag success">done</span>',
    pending:     '<span class="tag">pending</span>',
    in_progress: '<span class="tag accent">in progress</span>',
    queued:      '<span class="tag accent">queued</span>',
    failed:      '<span class="tag danger">failed</span>',
    skipped:     '<span class="tag warn">skipped</span>',
  })[r.status] || `<span class="tag">${r.status}</span>`;

  return `<tr data-path="${escapeAttr(r.path)}" class="${r.status}">
    <td><div class="filename">${escapeHtml(r.name)}</div></td>
    <td class="muted small">${escapeHtml(folder)}</td>
    <td class="muted small">${fmt.bytes(r.size_bytes)}</td>
    <td class="muted small">${date}</td>
    <td>${statusTag}</td>
    <td><div class="actions">
      ${r.has_transcript ? '<button class="btn-icon action-view" title="View transcript">view</button>' : ""}
      ${r.status === "pending" ? '<button class="btn-icon action-priority" title="Move to front of queue">↑</button>' : ""}
      ${r.has_transcript ? '<button class="btn-icon action-redo" title="Re-transcribe">redo</button>' : ""}
      ${r.status === "pending" || r.status === "failed" ? '<button class="btn-icon action-skip" title="Skip">skip</button>' : ""}
    </div></td>
  </tr>`;
}

function bindFileRowHandlers() {
  const pid = state.selectedProjectId;
  $$("#file-list tr[data-path]").forEach(tr => {
    const path = tr.dataset.path;
    const row = fileListCache.find(r => r.path === path);
    $(".action-view", tr)?.addEventListener("click", e => { e.stopPropagation(); openTranscript(pid, path); });
    $(".action-priority", tr)?.addEventListener("click", e => {
      e.stopPropagation();
      api(`/api/projects/${pid}/prioritize`, { method: "POST", body: { paths: [path] } });
    });
    $(".action-redo", tr)?.addEventListener("click", e => {
      e.stopPropagation();
      if (!confirm("Re-transcribe this file? The existing transcript will be deleted.")) return;
      api(`/api/projects/${pid}/retranscribe`, { method: "POST", body: { paths: [path] } });
    });
    $(".action-skip", tr)?.addEventListener("click", e => {
      e.stopPropagation();
      api(`/api/projects/${pid}/skip`, { method: "POST", body: { paths: [path] } });
    });
    if (row && row.has_transcript) {
      tr.style.cursor = "pointer";
      tr.onclick = () => openTranscript(pid, path);
    }
  });
}

// ---------- modals ----------

function closeAllModals() {
  $$(".modal-backdrop").forEach(m => m.hidden = true);
}

function openModal(id) {
  closeAllModals();
  $("#" + id).hidden = false;
}

async function openTranscript(pid, path) {
  openModal("transcript-modal");
  $("#tx-filename").textContent = path.split("/").pop();
  $("#tx-quality").innerHTML = "";
  $("#tx-content").textContent = "Loading…";

  try {
    const data = await api(`/api/projects/${pid}/transcript?path=${encodeURIComponent(path)}`);
    const qual = data.quality;
    let qualHtml = "";
    if (qual && qual.warnings && qual.warnings.length) {
      qualHtml = `<div class="warning-banner"><strong>⚠ Quality warning:</strong>
        ${qual.warnings.map(w => `${w.type === "consecutive_repeat"
          ? `whisper looped — "${escapeHtml(w.phrase)}" repeated ${w.count} times in a row.`
          : `low diversity (${qual.unique_lines}/${qual.lines} unique lines).`} <em>${escapeHtml(w.fix)}</em>`).join("<br/>")}
      </div>`;
    }
    $("#tx-quality").innerHTML = qualHtml;
    $("#tx-content").textContent = data.txt && data.txt.trim()
      ? data.txt
      : "(this file hasn't been transcribed yet — close this and use ↑ to prioritize it)";

    $("#tx-retranscribe").onclick = async () => {
      if (!confirm("Re-transcribe this file? The existing transcript will be deleted.")) return;
      await api(`/api/projects/${pid}/retranscribe`, { method: "POST", body: { paths: [path] } });
      closeAllModals();
      pollStatus();
    };
    $("#tx-finder").onclick = () => {
      window.open(`file://${path.split("/").slice(0,-1).join("/")}/`, "_blank");
    };
  } catch (e) {
    $("#tx-content").textContent = "Error loading transcript: " + e.message;
  }
}

async function openNewProjectModal() {
  openModal("new-project-modal");
  const eng = await api("/api/engine");
  const select = $("#np-model");
  select.innerHTML = "";
  if (!eng.installed_models.length) {
    const opt = document.createElement("option");
    opt.value = "ggml-large-v3.bin";
    opt.textContent = "ggml-large-v3.bin (download required)";
    select.appendChild(opt);
  } else {
    eng.installed_models.forEach(m => {
      const opt = document.createElement("option");
      opt.value = m.name;
      opt.textContent = `${m.name} · ${(m.size_bytes/1024/1024).toFixed(0)} MB${m.lang === "en" ? " · English-only" : " · multilingual"}`;
      if (m.name === "ggml-large-v3.bin") opt.selected = true;
      select.appendChild(opt);
    });
  }
}

function bindNewProjectHandlers() {
  $("#add-project-btn").onclick = openNewProjectModal;
  $("#np-cancel").onclick = closeAllModals;
  $("#np-create").onclick = async () => {
    const body = {
      name: $("#np-name").value.trim(),
      folders: $("#np-folders").value.split("\n").map(s => s.trim()).filter(Boolean),
      auto_run: $("#np-autorun").checked,
      require_ac_power: $("#np-acpower").checked,
      required_volumes: $("#np-volumes").value.split("\n").map(s => s.trim()).filter(Boolean),
      exclude_patterns: $("#np-excludes").value.split("\n").map(s => s.trim()).filter(Boolean),
      config: {
        model: $("#np-model").value,
        language: $("#np-language").value,
        translate_to_english: $("#np-translate").checked,
        vad: $("#np-vad").checked,
        no_context: $("#np-nocontext").checked,
      },
    };
    if (!body.name) return alert("Name required");
    if (!body.folders.length) return alert("At least one folder required");
    try {
      const proj = await api("/api/projects", { method: "POST", body });
      closeAllModals();
      switchView("project", proj.id);
    } catch (e) {
      alert("Failed to create project: " + e.message);
    }
  };
  $("#tx-close").onclick = closeAllModals;

  $$(".modal-backdrop").forEach(bd => {
    bd.addEventListener("click", (e) => {
      if (e.target === bd) closeAllModals();
    });
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeAllModals();
  });
}

// ---------- utilities ----------

function setText(id, val) {
  const el = $("#" + id);
  if (el && el.textContent !== String(val)) el.textContent = val;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}
function escapeAttr(s) { return escapeHtml(s); }

window.api = api;
window.pollStatus = pollStatus;

document.addEventListener("DOMContentLoaded", () => {
  bindNewProjectHandlers();
  startPolling();
});
