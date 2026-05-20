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
  sourceTab: "local",          // "local" | "youtube" | "music" — active source-type tab
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
  const v = sys.version || {};
  const ver = v.sha
    ? `${v.branch}@${v.sha}${v.dirty ? "*" : ""}${v.is_canonical ? "" : " · worktree"}`
    : "";
  $("#port-info").innerHTML =
    `${sys.host}:${sys.port}` +
    (ver ? `<br><span title="${escapeAttr(v.checkout || "")}">${escapeHtml(ver)}</span>` : "");

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

    <div id="youtube-card"></div>

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
      <div id="source-tabs-bar"></div>
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
    ${p.youtube_enabled ? '<span class="tag accent">YouTube</span>' : ""}
  `;

  renderHeaderActions(s.worker.paused);
  renderNowPlayingInto("now-playing-card");
  renderYoutubePanelInto("youtube-card", p, prevStatus);

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
  const refreshBtn = state.view === "project"
    ? `<button class="btn-secondary" id="btn-refresh-project">↻ Refresh</button>
       <span class="muted small" id="refresh-status" style="margin-left:8px;"></span>`
    : `<button class="btn-secondary" id="btn-refresh-all">↻ Refresh all</button>
       <span class="muted small" id="refresh-status" style="margin-left:8px;"></span>`;
  target.innerHTML = `
    ${refreshBtn}
    ${paused
      ? `<button class="btn-primary" id="btn-resume">▶ Resume</button>`
      : `<button class="btn-secondary" id="btn-pause">⏸ Pause</button>`}
  `;
  if ($("#btn-resume")) $("#btn-resume").onclick = () =>
    api("/api/resume", { method: "POST" }).then(pollStatus);
  if ($("#btn-pause")) $("#btn-pause").onclick = () =>
    api("/api/pause", { method: "POST" }).then(pollStatus);
  if ($("#btn-refresh-all")) $("#btn-refresh-all").onclick = async () => {
    const btn = $("#btn-refresh-all");
    const status = $("#refresh-status");
    btn.disabled = true;
    status.textContent = "Scanning all projects…";
    try {
      const res = await api("/api/refresh-all", { method: "POST", body: {} });
      const n = res.total_new || 0;
      const m = res.project_count || 0;
      status.textContent = n
        ? `Found ${n} new file${n === 1 ? "" : "s"} across ${m} project${m === 1 ? "" : "s"}`
        : `No new files in ${m} project${m === 1 ? "" : "s"}`;
      pollStatus();
    } catch (e) {
      status.textContent = "Refresh failed";
    } finally {
      btn.disabled = false;
    }
  };
  if ($("#btn-refresh-project")) $("#btn-refresh-project").onclick = async () => {
    const pid = state.selectedProjectId;
    if (!pid) return;
    const btn = $("#btn-refresh-project");
    const status = $("#refresh-status");
    btn.disabled = true;
    status.textContent = "Scanning…";
    try {
      const res = await api(`/api/projects/${pid}/refresh`, { method: "POST", body: {} });
      const n = res.total || 0;
      status.textContent = n ? `Found ${n} new file${n === 1 ? "" : "s"}` : "No new files";
      state.fileListLastFetchedAt = 0;  // bust the 15s cache
      fetchAndRenderFileList(pid);
      pollStatus();
    } catch (e) {
      status.textContent = "Refresh failed";
    } finally {
      btn.disabled = false;
    }
  };
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
  // YouTube ingest phases — map to friendlier labels with percent if present
  const YT_PHASE_LABELS = {
    preflight_yt:       "preparing YouTube ingest",
    download_start:     "downloading",
    download_progress:  "downloading",
    download_done:      "download complete",
    separate_start:     "separating vocals (Demucs)",
    separate_progress:  "separating vocals (Demucs)",
    separate_done:      "separation complete",
  };
  if (YT_PHASE_LABELS[ev.phase]) {
    phaseText = YT_PHASE_LABELS[ev.phase];
    if (typeof payload.percent === "number") {
      chunkInfo = `${Math.round(payload.percent)}%`;
    }
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

function renderSourceTabsInto(containerId) {
  const el = $("#" + containerId);
  if (!el) return;
  const all = fileListCache;
  const localCount  = all.filter(f => f.source === "folder").length;
  const ytCount     = all.filter(f => f.source === "youtube" && f.youtube_mode !== "music").length;
  const musicCount  = all.filter(f => f.source === "youtube" && f.youtube_mode === "music").length;
  const tabs = [
    { key: "local",   label: "Local Files", count: localCount  },
    { key: "youtube", label: "YouTube",      count: ytCount     },
    { key: "music",   label: "Music",        count: musicCount  },
  ];
  el.innerHTML = `<div class="source-tabs">${tabs.map(t => `
    <button class="source-tab-btn${state.sourceTab === t.key ? " active" : ""}" data-tab="${t.key}">
      ${t.label} <span class="tab-count">${t.count}</span>
    </button>`).join("")}</div>`;
  el.querySelectorAll(".source-tab-btn").forEach(btn => {
    btn.onclick = () => {
      state.sourceTab = btn.dataset.tab;
      renderFileListFromCache();
    };
  });
}

function renderFileListFromCache() {
  const list = $("#file-list");
  if (!list) return;

  // Render/update the source-type tab bar
  renderSourceTabsInto("source-tabs-bar");

  // Filter by active source tab first
  let rows = fileListCache;
  if (state.sourceTab === "local") {
    rows = rows.filter(r => r.source === "folder");
  } else if (state.sourceTab === "youtube") {
    rows = rows.filter(r => r.source === "youtube" && r.youtube_mode !== "music");
  } else if (state.sourceTab === "music") {
    rows = rows.filter(r => r.source === "youtube" && r.youtube_mode === "music");
  }

  // Then apply search/status filters
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

  // Music tab → music cards with audio players
  if (state.sourceTab === "music") {
    list.innerHTML = rows.map(f => renderMusicCard(f)).join("");
    bindMusicCardHandlers();
    return;
  }

  // Local / YouTube tabs → file table (existing layout)
  list.innerHTML = `<table class="file-table">
    <thead><tr>
      <th>File</th><th>Folder</th><th>Size</th><th>Modified</th><th>Status</th><th></th>
    </tr></thead>
    <tbody>${rows.map(fileRow).join("")}</tbody>
  </table>`;
  bindFileRowHandlers();
}

function renderMusicCard(file) {
  const job = youtubeListCache.find(u => u.folder === file.folder);
  const title = job?.title || file.name;
  const instrPath = file.folder ? file.folder + "/instrumental.mp3" : null;
  const pid = state.selectedProjectId;
  const statusBadgeHtml = {
    completed:   `<span class="badge badge-done">done</span>`,
    pending:     `<span class="badge badge-pending">pending</span>`,
    in_progress: `<span class="badge badge-progress">transcribing…</span>`,
    queued:      `<span class="badge badge-queued">queued</span>`,
    failed:      `<span class="badge badge-failed">failed</span>`,
    skipped:     `<span class="badge badge-skipped">skipped</span>`,
  }[file.status] || `<span class="badge badge-pending">${escapeHtml(file.status)}</span>`;

  const audioPlayers = file.status === "completed" ? `
    <div class="audio-row">
      <span class="audio-label">Vocals</span>
      <audio controls src="/api/audio?path=${encodeURIComponent(file.path)}"></audio>
    </div>
    <div class="audio-row">
      <span class="audio-label">Instrumental</span>
      <audio controls src="/api/audio?path=${encodeURIComponent(instrPath)}"></audio>
    </div>` : `<div style="margin-top:8px">${statusBadgeHtml}</div>`;

  return `<div class="music-card" data-path="${escapeAttr(file.path)}">
    <div class="music-card-header">
      <div>
        <div class="music-card-title">${escapeHtml(title)}</div>
        <div class="music-card-meta">${escapeHtml(file.name)} · ${fmt.bytes(file.size_bytes)}</div>
      </div>
      <span class="badge badge-music">♪ Music</span>
    </div>
    ${audioPlayers}
    <div class="music-card-actions">
      ${file.has_transcript
        ? `<button class="btn-secondary mc-view" data-path="${escapeAttr(file.path)}" data-pid="${escapeAttr(pid)}" style="font-size:12px;padding:4px 10px;">View lyrics</button>`
        : ""}
      ${file.status === "pending"
        ? `<button class="btn-icon mc-priority" data-path="${escapeAttr(file.path)}" title="Move to front of queue">↑</button>`
        : ""}
      ${file.has_transcript
        ? `<button class="btn-icon mc-redo" data-path="${escapeAttr(file.path)}" title="Re-transcribe">redo</button>`
        : ""}
    </div>
  </div>`;
}

function bindMusicCardHandlers() {
  const pid = state.selectedProjectId;
  $$("#file-list .music-card[data-path]").forEach(card => {
    const path = card.dataset.path;
    $(".mc-view", card)?.addEventListener("click", e => { e.stopPropagation(); openTranscript(pid, path); });
    $(".mc-priority", card)?.addEventListener("click", e => {
      e.stopPropagation();
      api(`/api/projects/${pid}/prioritize`, { method: "POST", body: { paths: [path] } });
    });
    $(".mc-redo", card)?.addEventListener("click", e => {
      e.stopPropagation();
      if (!confirm("Re-transcribe? The existing transcript will be deleted.")) return;
      api(`/api/projects/${pid}/retranscribe`, { method: "POST", body: { paths: [path] } });
    });
  });
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

// ---------- youtube panel ----------

let youtubeListCache = [];
let youtubeListLastFetchedFor = null;
let youtubeListLastFetchedAt = 0;

const YT_STATUS_TAG = {
  queued:       '<span class="tag">queued</span>',
  downloading:  '<span class="tag accent">downloading</span>',
  separating:   '<span class="tag accent">separating</span>',
  transcribing: '<span class="tag accent">transcribing</span>',
  done:         '<span class="tag success">done</span>',
  failed:       '<span class="tag danger">failed</span>',
};

function renderYoutubePanelInto(targetId, p, prevStatus) {
  const target = $("#" + targetId);
  if (!target) return;

  // Card skeleton, rendered once and patched after
  if (!target.querySelector(".yt-card")) {
    target.innerHTML = `
      <div class="card yt-card">
        <div class="flex-between" style="margin-bottom:8px;">
          <h3 class="card-title" style="margin:0;">YouTube ingest</h3>
          <div id="yt-toggle"></div>
        </div>
        <div id="yt-body"></div>
      </div>
    `;
  }

  // Header toggle (enabled / disabled) — always visible
  const tog = $("#yt-toggle");
  tog.innerHTML = p.youtube_enabled
    ? `<button class="btn-secondary" id="yt-disable-btn">Disable</button>`
    : `<button class="btn-primary" id="yt-enable-btn">Enable for this project</button>`;
  if ($("#yt-enable-btn")) $("#yt-enable-btn").onclick = () => toggleYoutube(p.id, true);
  if ($("#yt-disable-btn")) $("#yt-disable-btn").onclick = () => toggleYoutube(p.id, false);

  const body = $("#yt-body");
  if (!p.youtube_enabled) {
    if (!body.querySelector(".yt-disabled-msg")) {
      body.innerHTML = `<div class="muted small yt-disabled-msg">
        Disabled. Enabling lets you paste a YouTube URL to download (and optionally
        separate vocals via Demucs) into <code>${escapeHtml(p.folders[0] || "(no folder)")}/youtube/</code>.
        Requires <code>yt-dlp</code> and (for music mode) <code>demucs</code> installed.
      </div>`;
    }
    return;
  }

  // Enabled: render input + list. Skeleton once, patch list on update.
  if (!body.querySelector(".yt-input-row")) {
    body.innerHTML = `
      <div class="yt-input-row" style="display:flex; gap:8px; align-items:center; margin-bottom:12px;">
        <input type="url" id="yt-url-input" placeholder="https://www.youtube.com/watch?v=..."
               style="flex:1; padding:8px; border:1px solid var(--border); border-radius:6px;"/>
        <select id="yt-mode-select" style="padding:8px; border:1px solid var(--border); border-radius:6px;">
          <option value="speech">Speech</option>
          <option value="music">Music</option>
        </select>
        <button class="btn-primary" id="yt-submit-btn">Add URL</button>
      </div>
      <div id="yt-list"></div>
    `;
    $("#yt-mode-select").value = p.youtube_default_mode || "speech";
    $("#yt-submit-btn").onclick = () => submitYoutubeUrl(p.id);
    $("#yt-url-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") submitYoutubeUrl(p.id);
    });
  }

  // Fetch on view-enter, or when youtube_total changes vs. prev poll,
  // or when stale (>10s). Keeps the panel responsive without thrashing.
  const now = Date.now() / 1000;
  const stale = (now - youtubeListLastFetchedAt) > 10;
  const prevP = prevStatus?.projects?.find(x => x.id === p.id);
  const totalChanged = prevP && prevP.youtube_total !== p.youtube_total;
  const projectSwitched = youtubeListLastFetchedFor !== p.id;
  if (projectSwitched || stale || totalChanged) {
    fetchAndRenderYoutubeList(p.id);
  } else {
    renderYoutubeListFromCache();
  }
}

async function fetchAndRenderYoutubeList(pid) {
  try {
    const data = await api(`/api/projects/${pid}/youtube`);
    youtubeListCache = data.urls || [];
    youtubeListLastFetchedFor = pid;
    youtubeListLastFetchedAt = Date.now() / 1000;
    renderYoutubeListFromCache();
  } catch (e) { /* keep old list */ }
}

function renderYoutubeListFromCache() {
  const list = $("#yt-list");
  if (!list) return;
  if (youtubeListCache.length === 0) {
    list.innerHTML = `<div class="muted small">No URLs yet — paste one above to ingest.</div>`;
    return;
  }
  // Sort: in-flight first, then queued (priority within queued), then terminal
  const order = { downloading: 0, separating: 1, transcribing: 2, queued: 3, failed: 4, done: 5 };
  const sorted = [...youtubeListCache].sort((a, b) => {
    const so = (order[a.status] ?? 9) - (order[b.status] ?? 9);
    if (so !== 0) return so;
    // Within same status, priority rows first (for queued); then submission order.
    if ((a.priority ?? false) !== (b.priority ?? false)) return a.priority ? -1 : 1;
    return (a.submitted_at || "").localeCompare(b.submitted_at || "");
  });
  list.innerHTML = `<table class="file-table yt-table">
    <thead><tr><th>Title / URL</th><th>Mode</th><th>Status</th><th>Submitted</th><th></th></tr></thead>
    <tbody>${sorted.map(youtubeRow).join("")}</tbody>
  </table>`;
  bindYoutubeRowHandlers();
}

function youtubeRow(r) {
  const title = r.title || r.url;
  const submitted = r.submitted_at
    ? new Date(r.submitted_at).toLocaleString()
    : "—";
  const stageInfo = (r.status && r.stage && r.status !== r.stage)
    ? `<span class="muted small"> · ${escapeHtml(r.stage)}</span>` : "";
  const priorityBadge = r.priority
    ? ' <span class="tag accent" title="Up next">↑ up next</span>'
    : "";
  const failReason = r.failed_reason
    ? `<div class="muted small" style="margin-top:2px;">${escapeHtml(r.failed_reason)}</div>`
    : "";
  const statusTag = (YT_STATUS_TAG[r.status] || `<span class="tag">${r.status}</span>`) + stageInfo + priorityBadge;
  return `<tr data-url-id="${escapeAttr(r.id)}" class="yt-${r.status}">
    <td><div class="filename" title="${escapeAttr(r.url)}">${escapeHtml(title)}</div>${failReason}</td>
    <td><span class="tag">${escapeHtml(r.mode)}</span></td>
    <td>${statusTag}</td>
    <td class="muted small">${escapeHtml(submitted)}</td>
    <td><div class="actions">
      ${r.status === "queued" && !r.priority ?
        '<button class="btn-icon yt-action-priority" title="Run this URL before others in the queue">↑ up next</button>' : ""}
      ${r.status === "queued" && r.priority ?
        '<button class="btn-icon yt-action-unpriority" title="Drop priority — back to submission order">unprioritize</button>' : ""}
      ${r.status === "failed" ? '<button class="btn-icon yt-action-retry" title="Retry">retry</button>' : ""}
      ${(r.status === "queued" || r.status === "failed" || r.status === "done") ?
        '<button class="btn-icon yt-action-remove" title="Remove">×</button>' : ""}
    </div></td>
  </tr>`;
}

function bindYoutubeRowHandlers() {
  const pid = state.selectedProjectId;
  $$("#yt-list tr[data-url-id]").forEach(tr => {
    const id = tr.dataset.urlId;
    $(".yt-action-retry", tr)?.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await api(`/api/projects/${pid}/youtube/${id}/retry`, { method: "POST" });
        fetchAndRenderYoutubeList(pid);
      } catch (err) { alert("Retry failed: " + err.message); }
    });
    $(".yt-action-remove", tr)?.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm("Remove this URL from the queue?")) return;
      try {
        await api(`/api/projects/${pid}/youtube/${id}`, { method: "DELETE" });
        fetchAndRenderYoutubeList(pid);
      } catch (err) { alert("Remove failed: " + err.message); }
    });
    $(".yt-action-priority", tr)?.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await api(`/api/projects/${pid}/youtube/${id}/prioritize`, {
          method: "POST", body: { priority: true },
        });
        fetchAndRenderYoutubeList(pid);
        pollStatus();
      } catch (err) { alert("Prioritize failed: " + err.message); }
    });
    $(".yt-action-unpriority", tr)?.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await api(`/api/projects/${pid}/youtube/${id}/prioritize`, {
          method: "POST", body: { priority: false },
        });
        fetchAndRenderYoutubeList(pid);
      } catch (err) { alert("Unprioritize failed: " + err.message); }
    });
  });
}

async function submitYoutubeUrl(pid) {
  const url = $("#yt-url-input").value.trim();
  const mode = $("#yt-mode-select").value;
  if (!url) return;
  const btn = $("#yt-submit-btn");
  btn.disabled = true;
  try {
    const res = await fetch(`/api/projects/${pid}/youtube`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, mode }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      alert(data.message || data.error || `submit failed (${res.status})`);
    } else {
      $("#yt-url-input").value = "";
      fetchAndRenderYoutubeList(pid);
      pollStatus();
    }
  } catch (e) {
    alert("Submit failed: " + e.message);
  } finally {
    btn.disabled = false;
  }
}

async function toggleYoutube(pid, enable) {
  if (!enable && !confirm(
    "Disable YouTube ingest for this project?\n\n" +
    "Already-queued URLs will keep processing. Disabling just hides the input."
  )) return;
  try {
    await api(`/api/projects/${pid}`, {
      method: "PATCH",
      body: { youtube_enabled: enable },
    });
    pollStatus();
  } catch (e) {
    alert("Toggle failed: " + e.message);
  }
}

// ---------- modals ----------

function closeAllModals() {
  $$(".modal-backdrop").forEach(m => m.hidden = true);
}

function openModal(id) {
  closeAllModals();
  $("#" + id).hidden = false;
}

// Per-open state for the transcript modal (used by the Narrative tab).
const txModalState = { pid: null, path: null, transcriptLoaded: false };

async function openTranscript(pid, path) {
  openModal("transcript-modal");
  $("#tx-filename").textContent = path.split("/").pop();
  $("#tx-quality").innerHTML = "";
  $("#tx-content").textContent = "Loading…";
  $("#tx-analysis-panel").innerHTML = "";

  txModalState.pid = pid;
  txModalState.path = path;
  txModalState.transcriptLoaded = false;
  resetNarrativePane();

  // Three-panel tab switching (Transcript | Analysis | Narrative).
  // Panel ids follow the pattern tx-<tab>-panel; data-tab matches.
  const tabs = $$("#tx-tabs .modal-tab");
  const panels = {
    transcript: $("#tx-transcript-panel"),
    analysis:   $("#tx-analysis-panel"),
    narrative:  $("#tx-narrative-panel"),
  };
  tabs.forEach(btn => {
    btn.onclick = () => {
      tabs.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      Object.entries(panels).forEach(([key, el]) => {
        if (el) el.style.display = (key === btn.dataset.tab) ? "" : "none";
      });
      // Narrative tab: try to pre-load any cached narrative for the current style.
      if (btn.dataset.tab === "narrative") tryLoadCachedNarrative();
    };
  });
  // Reset to transcript tab
  tabs[0]?.click();

  try {
    const data = await api(`/api/projects/${pid}/transcript?path=${encodeURIComponent(path)}`);

    // Quality banner
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

    // Transcript text
    $("#tx-content").textContent = data.txt && data.txt.trim()
      ? data.txt
      : "(this file hasn't been transcribed yet — close this and use ↑ to prioritize it)";
    txModalState.transcriptLoaded = !!(data.txt && data.txt.trim());

    // Analysis panel content (Ollama)
    panels.analysis.innerHTML = renderAnalysisPanel(data.analysis, pid, path);
    bindAnalysisPanelHandlers(pid, path);

    // Narrative tab handlers (Opus). Bound once per modal open.
    bindNarrativeTabHandlers();

    $("#tx-retranscribe").onclick = async () => {
      if (!confirm("Re-transcribe this file? The existing transcript will be deleted.")) return;
      await api(`/api/projects/${pid}/retranscribe`, { method: "POST", body: { paths: [path] } });
      closeAllModals();
      pollStatus();
    };
    $("#tx-finder").onclick = async (e) => {
      // Browsers silently block window.open("file://..."), so we ask the
      // local Flask backend to shell out to macOS `open -R`. The button
      // briefly disables on click and surfaces an inline error label if
      // the reveal fails (e.g., file deleted).
      const btn = e.currentTarget;
      const orig = btn.textContent;
      btn.disabled = true;
      try {
        await api("/api/reveal", { method: "POST", body: { path } });
      } catch (err) {
        btn.textContent = "Couldn't open Finder";
        setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1800);
        return;
      }
      btn.disabled = false;
    };
  } catch (e) {
    $("#tx-content").textContent = "Error loading transcript: " + e.message;
  }
}

// ---------- Narrative tab (Claude Opus 4.7) ----------

function resetNarrativePane() {
  $("#tx-narrate-status").textContent = "";
  $("#tx-narrate-regen").hidden = true;
  $("#tx-scaffold-body").hidden = true;
  $("#tx-scaffold-body").innerHTML = "";
  $("#tx-narrative-body").innerHTML = `<div class="muted small">
    No narrative yet for this style. Click <strong>Generate</strong> to send the transcript to Claude Opus 4.7.
    Requires <code>ANTHROPIC_API_KEY</code> exported in the environment.
  </div>`;
}

function bindNarrativeTabHandlers() {
  $("#tx-narrative-style").onchange = () => {
    resetNarrativePane();
    tryLoadCachedNarrative();
  };
  $("#tx-narrate-btn").onclick = () => generateNarrative({ force: false });
  $("#tx-narrate-regen").onclick = () => {
    if (!confirm("Regenerate? This sends the transcript to Claude Opus 4.7 again and overwrites the cached output for this style.")) return;
    generateNarrative({ force: true });
  };
}

async function tryLoadCachedNarrative() {
  const { pid, path } = txModalState;
  if (!pid || !path) return;
  const style = $("#tx-narrative-style").value;
  try {
    const data = await api(
      `/api/projects/${pid}/narrative?path=${encodeURIComponent(path)}&style=${encodeURIComponent(style)}`
    );
    renderNarrativeResult(data);
    $("#tx-narrate-status").textContent = "cached";
  } catch (e) {
    // 404 is expected if no narrative cached yet — leave the empty prompt in place.
  }
}

async function generateNarrative({ force }) {
  const { pid, path, transcriptLoaded } = txModalState;
  if (!pid || !path) return;
  if (!transcriptLoaded) {
    alert("Transcribe this file first — there's no .txt yet.");
    return;
  }
  const style = $("#tx-narrative-style").value;
  const btn = $("#tx-narrate-btn");
  const regen = $("#tx-narrate-regen");
  const status = $("#tx-narrate-status");
  btn.disabled = true; regen.disabled = true;
  status.textContent = force ? "regenerating… (30–90s)" : "generating… (30–90s)";
  try {
    const res = await fetch(`/api/projects/${pid}/narrate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, style, force }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      status.textContent = "";
      alert(data.message || data.error || `narrate failed (${res.status})`);
      return;
    }
    renderNarrativeResult(data);
    status.textContent = data.cached ? "cached" : "generated";
  } catch (e) {
    status.textContent = "";
    alert("Generate failed: " + e.message);
  } finally {
    btn.disabled = false; regen.disabled = false;
  }
}

function renderNarrativeResult(data) {
  // Scaffold pane: render structured fields if present.
  const s = data.scaffold || {};
  const parts = [];
  if (s.summary_one_line) {
    parts.push(`<p class="scaffold-summary">${escapeHtml(s.summary_one_line)}</p>`);
  }
  if (Array.isArray(s.themes) && s.themes.length) {
    parts.push(`<section><h4>Themes</h4><div class="tag-row">${
      s.themes.map(t => `<span class="topic-pill">${escapeHtml(t)}</span>`).join("")
    }</div></section>`);
  }
  if (Array.isArray(s.characters) && s.characters.length) {
    parts.push(`<section><h4>Characters</h4><ul>${
      s.characters.map(c => `<li><strong>${escapeHtml(c.name || "?")}</strong>${
        c.role ? ` <span class="muted small">(${escapeHtml(c.role)})</span>` : ""
      }${c.description ? ` — ${escapeHtml(c.description)}` : ""}</li>`).join("")
    }</ul></section>`);
  }
  if (s.emotional_arc) {
    parts.push(`<section><h4>Emotional arc</h4><p>${escapeHtml(s.emotional_arc)}</p></section>`);
  }
  if (Array.isArray(s.story_beats) && s.story_beats.length) {
    parts.push(`<section><h4>Story beats</h4><ol>${
      s.story_beats.map(b => `<li><strong>${escapeHtml(b.moment || "")}</strong>${
        b.significance ? ` — ${escapeHtml(b.significance)}` : ""
      }</li>`).join("")
    }</ol></section>`);
  }
  if (Array.isArray(s.notable_quotes) && s.notable_quotes.length) {
    parts.push(`<section><h4>Notable quotes</h4>${
      s.notable_quotes.map(q => `<blockquote>"${escapeHtml(q.quote || "")}"${
        (q.context || q.why_resonant)
          ? `<div class="muted small">${[q.context, q.why_resonant].filter(Boolean).map(escapeHtml).join(" — ")}</div>`
          : ""
      }</blockquote>`).join("")
    }</section>`);
  }
  const scaffoldEl = $("#tx-scaffold-body");
  scaffoldEl.innerHTML = parts.length
    ? `<details class="scaffold-details" open><summary>Scaffold (themes, beats, characters)</summary>${parts.join("")}</details>`
    : "";
  scaffoldEl.hidden = parts.length === 0;

  // Narrative pane: render the Markdown loosely.
  $("#tx-narrative-body").innerHTML = renderLooseMarkdown(data.narrative || "");
  $("#tx-narrate-regen").hidden = false;
}

// Minimal Markdown-ish renderer: paragraphs, # / ## / ### headings, **bold**, *italic*.
function renderLooseMarkdown(md) {
  if (!md) return "";
  const esc = escapeHtml(md);
  const blocks = esc.split(/\n{2,}/);
  return blocks.map(block => {
    const trimmed = block.trim();
    if (!trimmed) return "";
    const h = trimmed.match(/^(#{1,3})\s+(.+)$/);
    if (h) {
      const level = h[1].length + 1; // # → h2, ## → h3, ### → h4
      return `<h${level}>${applyInline(h[2])}</h${level}>`;
    }
    return `<p>${applyInline(trimmed.replace(/\n/g, "<br/>"))}</p>`;
  }).join("");
}

function applyInline(s) {
  return s
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>");
}

function renderAnalysisPanel(analysis, pid, path) {
  if (!analysis) {
    return `<div class="analysis-empty">
      <div class="muted">No analysis yet.</div>
      <button class="analyze-btn" id="btn-analyze">
        ✦ Analyze with qwen2.5-coder
      </button>
    </div>`;
  }
  const topicsHtml = (analysis.topics || []).length
    ? `<div class="topics-row">${analysis.topics.map(t => `<span class="topic-pill">${escapeHtml(t)}</span>`).join("")}</div>`
    : "";
  const summaryHtml = analysis.summary
    ? `<div class="analysis-summary">${escapeHtml(analysis.summary)}</div>`
    : "";
  const errorHtml = analysis.error
    ? `<div class="warning-banner" style="margin-top:8px;">${escapeHtml(analysis.error)}</div>`
    : "";
  return `<div class="analysis-panel">
    ${summaryHtml}
    ${topicsHtml}
    ${errorHtml}
    <div class="analysis-meta">
      Analyzed with <strong>${escapeHtml(analysis.model)}</strong>
      · ${(analysis.word_count || 0).toLocaleString()} words
      · ${analysis.analyzed_at ? new Date(analysis.analyzed_at).toLocaleString() : ""}
    </div>
    <div style="margin-top:12px;">
      <button class="analyze-btn" id="btn-analyze" style="font-size:12px;padding:5px 12px;">
        Re-analyze
      </button>
    </div>
  </div>`;
}

function bindAnalysisPanelHandlers(pid, path) {
  const btn = $("#btn-analyze");
  if (!btn) return;
  btn.onclick = async () => {
    btn.disabled = true;
    btn.textContent = "Analyzing…";
    try {
      // >>> LOCAL LLM CALL — sends this transcript to qwen2.5-coder:14b <<<
      await api(`/api/projects/${pid}/analyze`, { method: "POST", body: { path } });
      // Reload the modal with fresh analysis
      await openTranscript(pid, path);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "✦ Analyze with qwen2.5-coder";
      alert("Analysis failed: " + e.message);
    }
  };
}

async function openNewProjectModal() {
  openModal("new-project-modal");

  // Whisper models
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

  // >>> LOCAL LLM QUERY — check if Ollama is running and list models <<<
  // Populates the AI Analysis model dropdown with installed Ollama models.
  try {
    const ollamaData = await api("/api/ollama/models");
    const ollamaSelect = $("#np-ollama-model");
    const statusEl = $("#np-ollama-status");
    if (ollamaData.running && ollamaData.models.length) {
      ollamaSelect.innerHTML = ollamaData.models.map(m =>
        `<option value="${escapeAttr(m)}"${m === "qwen2.5-coder:14b" ? " selected" : ""}>${escapeHtml(m)}</option>`
      ).join("");
      if (statusEl) statusEl.textContent = `${ollamaData.models.length} model${ollamaData.models.length===1?"":"s"} available`;
    } else {
      if (statusEl) statusEl.textContent = "Ollama not running — start it to enable analysis";
    }
  } catch (e) {
    const statusEl = $("#np-ollama-status");
    if (statusEl) statusEl.textContent = "Could not reach Ollama";
  }
}

function bindNewProjectHandlers() {
  $("#add-project-btn").onclick = openNewProjectModal;
  $("#np-cancel").onclick = closeAllModals;
  $("#np-browse").onclick = async () => {
    try {
      const res = await api("/api/dialog/pick-folder", { method: "POST", body: {} });
      if (res.cancelled || !res.path) return;
      const ta = $("#np-folders");
      const cur = ta.value.replace(/\s+$/, "");
      ta.value = cur ? `${cur}\n${res.path}` : res.path;
      ta.focus();
    } catch (e) {
      alert("Couldn't open Finder picker: " + e.message);
    }
  };
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
      youtube_enabled: $("#np-youtube").checked,
      youtube_default_mode: $("#np-youtube-mode").value,
      ollama: {
        enabled: $("#np-ollama-enabled").checked,
        model: $("#np-ollama-model").value || "qwen2.5-coder:14b",
        analyses: ["summary", "topics"],
        base_url: "http://localhost:11434",
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

  bindPlaylistHandlers();

  $$(".modal-backdrop").forEach(bd => {
    bd.addEventListener("click", (e) => {
      if (e.target === bd) closeAllModals();
    });
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeAllModals();
  });
}

// ---------- Add-from-playlist modal ----------

function openPlaylistModal() {
  openModal("playlist-modal");
  $("#pl-url").value = "";
  $("#pl-url-status").textContent = "";
  $("#pl-create-status").textContent = "";
  $("#pl-step-url").hidden = false;
  $("#pl-step-preview").hidden = true;
  setTimeout(() => $("#pl-url").focus(), 50);
}

// fetch wrapper that surfaces the server's JSON {message, error} on non-2xx
// instead of just the HTTP status. Kept local to the playlist modal — the
// shared api() helper is intentionally minimal for the rest of the app.
async function _fetchJson(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data = null;
  try { data = await r.json(); } catch (_) { /* non-JSON response */ }
  if (!r.ok) {
    const msg = (data && (data.message || data.error)) || `HTTP ${r.status}`;
    throw new Error(msg);
  }
  return data;
}

async function previewPlaylist() {
  const url = $("#pl-url").value.trim();
  if (!url) { $("#pl-url-status").textContent = "Paste a playlist URL first."; return; }
  if (!/[?&]list=/.test(url)) {
    $("#pl-url-status").textContent = "That looks like a single-video URL. A playlist URL contains \"list=PL…\" — open the playlist page on YouTube and copy that URL.";
    return;
  }
  const btn = $("#pl-preview");
  btn.disabled = true;
  $("#pl-url-status").textContent = "Fetching playlist… (can take 5-15s for large playlists)";
  try {
    const res = await _fetchJson("/api/projects/from-playlist/preview", { playlist_url: url });
    const pl = res.playlist;
    const sampleHtml = pl.sample.length
      ? `<ul class="pl-sample">${pl.sample.map(s =>
          `<li>${escapeHtml(s.title)}${s.uploader ? ` <span class="muted small">— ${escapeHtml(s.uploader)}</span>` : ""}</li>`
        ).join("")}${pl.item_count > pl.sample.length ? `<li class="muted small">…and ${pl.item_count - pl.sample.length} more</li>` : ""}</ul>`
      : `<p class="muted small">No previewable items.</p>`;
    const skipNote = pl.skipped_count
      ? `<p class="muted small">${pl.skipped_count} entr${pl.skipped_count===1?"y":"ies"} unavailable (private / deleted / region-blocked) and will be skipped.</p>`
      : "";
    const existsNote = pl.project_exists
      ? `<p class="muted small">⚠ A project with id <code>${escapeHtml(pl.suggested_project_id)}</code> already exists. A new project will be created with a unique suffix.</p>`
      : "";
    $("#pl-preview-body").innerHTML = `
      <h3 style="margin:0 0 4px 0;">${escapeHtml(pl.title)}</h3>
      ${pl.uploader ? `<p class="muted small" style="margin:0 0 12px 0;">by ${escapeHtml(pl.uploader)}</p>` : ""}
      <p><strong>${pl.item_count}</strong> video${pl.item_count===1?"":"s"} will be queued.</p>
      ${skipNote}
      ${existsNote}
      <p class="muted small">Folder: <code>${escapeHtml(pl.suggested_folder)}</code></p>
      ${sampleHtml}
    `;
    // Stash the URL so the create step uses the exact same one (server re-fetches).
    $("#pl-create").dataset.playlistUrl = url;
    $("#pl-step-url").hidden = true;
    $("#pl-step-preview").hidden = false;
  } catch (e) {
    $("#pl-url-status").textContent = "Couldn't fetch playlist: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function createPlaylistProject() {
  const url = $("#pl-create").dataset.playlistUrl;
  const mode = $("#pl-mode").value;
  if (!url) return;
  const btn = $("#pl-create");
  btn.disabled = true;
  $("#pl-create-status").textContent = "Creating project and queuing videos…";
  try {
    const res = await _fetchJson("/api/projects/from-playlist", { playlist_url: url, mode });
    closeAllModals();
    switchView("project", res.project.id);
  } catch (e) {
    $("#pl-create-status").textContent = "Failed: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

function bindPlaylistHandlers() {
  $("#add-playlist-btn").onclick = openPlaylistModal;
  $("#pl-cancel-1").onclick = closeAllModals;
  $("#pl-back").onclick = () => {
    $("#pl-step-preview").hidden = true;
    $("#pl-step-url").hidden = false;
  };
  $("#pl-preview").onclick = previewPlaylist;
  $("#pl-create").onclick = createPlaylistProject;
  $("#pl-url").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); previewPlaylist(); }
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
