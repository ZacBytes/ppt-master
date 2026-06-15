/* ─── State ──────────────────────────────────────────────────────────── */
const state = {
  data: null,
  taskStatus: "idle",
  wfStep: "create",        // create | outline | assets | build | export
  activeSlide: 0,
  uploads: [],             // local paths returned from /api/upload
  outline: null,           // parsed outline.json
  outlinePollTimer: null,
  templates: [],
  selectedTemplate: "",
  imageOn: false,
  aiScope: "slide",        // slide | deck
  eventSource: null,
  // streaming
  streamingNode: null,
  streamingText: "",
  // render signatures
  sigs: { projects: "", slides: "", exports: "" },
};

/* ─── Helpers ────────────────────────────────────────────────────────── */
const $ = sel => document.querySelector(sel);
const $$ = sel => [...document.querySelectorAll(sel)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = bytes => bytes < 1048576 ? `${Math.max(1,Math.round(bytes/1024))} KB` : `${(bytes/1048576).toFixed(1)} MB`;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body instanceof FormData ? undefined : {"Content-Type":"application/json"},
    ...opts,
  });
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(payload.error || `Request failed (${res.status})`);
  return payload;
}

function phaseToStep(phase) {
  if (!phase) return "create";
  const map = {
    source_input: "create",
    project_initialized: "create",
    template_resolved: "create",
    awaiting_confirmations: "outline",
    strategy_locked: "assets",
    assets_ready: "build",
    executing_slides: "build",
    quality_checked: "build",
    post_processing: "build",
    exported: "export",
  };
  return map[phase] ?? "create";
}

/* ─── SSE connection ─────────────────────────────────────────────────── */
function connectEvents() {
  if (state.eventSource) state.eventSource.close();
  const es = new EventSource("/api/events");
  state.eventSource = es;

  es.onopen = () => setOnline(true);
  es.onerror = () => setOnline(false);

  es.addEventListener("assistant_start", () => beginAiStream());
  es.addEventListener("assistant_delta", e => appendAiStream(JSON.parse(e.data).text || ""));
  es.addEventListener("assistant_done", e => finishAiStream(JSON.parse(e.data).content || state.streamingText));
  es.addEventListener("tool_start", e => setThinking(`Running ${friendlyTool(JSON.parse(e.data).name)}…`));
  es.addEventListener("tool_done", () => setThinking("Working"));
  es.addEventListener("llm_retry", e => {
    const {attempt, delay} = JSON.parse(e.data);
    setThinking(`Retry ${attempt}/4 in ${Math.ceil(delay)}s`);
  });
  es.addEventListener("task_started", e => {
    const {task} = JSON.parse(e.data);
    if (state.data) { state.data.task = task; state.taskStatus = "running"; renderThinking(true); }
  });
  ["task_complete","task_error","state"].forEach(name => {
    es.addEventListener(name, async e => {
      const payload = JSON.parse(e.data);
      if (payload.state) {
        state.data = payload.state;
        render(payload.state);
      } else {
        await refreshState();
      }
      if (name === "task_complete") {
        toast("Agent turn complete", "success");
        // After a turn completes check if outline is now available
        if (state.wfStep === "outline" && !state.outline) startOutlinePoll();
      }
      if (name === "task_error") {
        toast(payload.state?.task?.error || "Agent turn failed.", "error");
      }
    });
  });
}

function setOnline(ok) {
  const dot = $("#connDot");
  const label = $("#connLabel");
  dot.className = "conn-dot " + (ok ? "online" : "error");
  label.textContent = ok ? "Studio online" : "Disconnected";
}

/* ─── Global render ──────────────────────────────────────────────────── */
async function refreshState({silent = true} = {}) {
  try {
    const data = await api("/api/state");
    const prev = state.taskStatus;
    state.data = data;
    state.taskStatus = data.task.status;
    render(data);
    if (prev === "running" && data.task.status === "complete") toast("Agent turn complete", "success");
    if (data.task.status === "error" && prev !== "error") toast(data.task.error || "Agent turn failed.", "error");
  } catch (err) {
    setOnline(false);
    if (!silent) toast(err.message, "error");
  }
}

function render(data) {
  setOnline(true);
  renderProjects(data);
  renderTopbar(data);
  renderStep(data);
  renderThinking(data.task.status === "running");


  // Determine correct step from backend phase
  const backendStep = phaseToStep(data.workflow?.phase);
  // Only advance step forward, never regress during active session
  if (data.active_project || data.active_draft) {
    const order = ["create","outline","assets","build","export"];
    const cur = order.indexOf(state.wfStep);
    const desired = order.indexOf(backendStep);
    if (desired > cur || state.wfStep === "create") {
      state.wfStep = backendStep;
    }
  }
  showStep(state.wfStep, data);
}

/* ─── Project list ───────────────────────────────────────────────────── */
function renderProjects(data) {
  const sig = JSON.stringify([data.active_project, data.active_draft, data.projects]);
  if (sig === state.sigs.projects) return;
  state.sigs.projects = sig;

  const list = $("#projectList");
  const draft = data.active_draft
    ? `<div class="project-item active">
        <strong>${esc(data.active_label || "New presentation")}</strong>
        <div class="project-meta-row"><span>Draft</span><span>Working…</span></div>
       </div>`
    : "";

  if (!data.projects.length && !draft) {
    list.innerHTML = `<div class="project-item"><strong>No projects yet</strong><div class="project-meta-row"><span>Create a presentation to start</span></div></div>`;
    return;
  }
  list.innerHTML = draft + data.projects.map(p => {
    const act = p.path === data.active_project ? "active" : "";
    return `<button class="project-item ${act}" data-path="${esc(p.path)}">
      <strong>${esc(cleanName(p.name))}</strong>
      <div class="project-meta-row"><span>${esc(p.phase?.replace(/_/g," ") || "")}</span><span>${p.slides} slides</span></div>
    </button>`;
  }).join("");
  $$(".project-item[data-path]").forEach(btn =>
    btn.addEventListener("click", () => openProject(btn.dataset.path))
  );
}

/* ─── Topbar ─────────────────────────────────────────────────────────── */
function renderTopbar(data) {
  const name = data.active_label || (data.active_project ? data.active_project.split(/[/\\]/).pop() : "");
  const hasProject = !!(name);

  const welcome = $("#welcomeScreen");
  const projectView = $("#projectView");
  if (hasProject || data.active_draft) {
    welcome.classList.add("hidden");
    projectView.classList.remove("hidden");
    $("#projectTitle").textContent = name || "New presentation";
    $("#projectEyebrow").textContent = data.active_draft ? "DRAFT" : "ACTIVE PROJECT";
    if (data.model) $("#modelLabel").textContent = data.model;
  } else {
    welcome.classList.remove("hidden");
    projectView.classList.add("hidden");
  }
}

/* ─── Step navigation ────────────────────────────────────────────────── */
function showStep(step, data) {
  const steps = ["create","outline","assets","build","export"];
  const panels = {
    create: "#panelCreate", outline: "#panelOutline",
    assets: "#panelAssets", build: "#panelBuild", export: "#panelExport",
  };
  steps.forEach(s => {
    const panel = $(panels[s]);
    if (panel) panel.classList.toggle("hidden", s !== step);
    const navItem = $(`.step-item[data-step="${s}"]`);
    if (navItem) {
      const idx = steps.indexOf(s);
      const cur = steps.indexOf(step);
      navItem.classList.remove("active","complete");
      if (idx === cur) navItem.classList.add("active");
      else if (idx < cur) navItem.classList.add("complete");
    }
    const conn = $$(`.step-connector`)[steps.indexOf(s)];
    if (conn) {
      const idx = steps.indexOf(s);
      const cur = steps.indexOf(step);
      conn.classList.toggle("complete", idx < cur);
      conn.classList.toggle("active", idx === cur);
    }
  });

  // Step-specific rendering
  if (step === "outline") renderOutlinePanel(data);
  if (step === "build") renderBuildPanel(data);
  if (step === "export") renderExportPanel(data);
}

function renderStep(data) {
  showStep(state.wfStep, data);
}

/* ─── Thinking chip ──────────────────────────────────────────────────── */
function renderThinking(running) {
  const chip = $("#thinkingChip");
  chip.classList.toggle("hidden", !running);
  const sendBtn = $("#btnCreate");
  if (sendBtn) sendBtn.disabled = running;
  const approveBtn = $("#btnApproveOutline");
  if (approveBtn) approveBtn.disabled = running || !state.outline;
}
function setThinking(label) {
  const lbl = $("#thinkingLabel");
  if (lbl) lbl.textContent = label;
}
function friendlyTool(name="") {
  return name.replace(/_/g," ").replace(/\b\w/g,c=>c.toUpperCase());
}

/* ─── CREATE step ────────────────────────────────────────────────────── */
async function loadTemplates() {
  try {
    const templates = await api("/api/templates");
    state.templates = templates;
    renderTemplateGrid(templates);
  } catch (_) { /* silently fail */ }
}

function renderTemplateGrid(templates) {
  const grid = $("#templateGrid");
  if (!grid) return;
  const existing = grid.innerHTML; // keep Free Design card

  const cards = templates.map(t => {
    const thumb = t.slides[0] ? `<img src="${esc(t.slides[0])}" alt="${esc(t.name)}" loading="lazy">` : "";
    const sel = state.selectedTemplate === t.id ? "selected" : "";
    return `<button class="template-item ${sel}" data-template="${esc(t.id)}">
      <div class="template-preview">${thumb}</div>
      <div class="template-meta">
        <span class="template-color-dot" style="background:${esc(t.primary_color)}"></span>
        <span class="template-name">${esc(t.name)}</span>
      </div>
    </button>`;
  }).join("");

  grid.innerHTML = existing + cards;
  bindTemplateCards();
}

function bindTemplateCards() {
  $$(".template-item").forEach(btn => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.template || "";
      state.selectedTemplate = id;
      $$(".template-item").forEach(b => b.classList.toggle("selected", b.dataset.template === id));
    });
  });
}

async function handleCreate() {
  const topic = $("#topicInput")?.value.trim();
  if (!topic) { toast("Describe your presentation first.", "error"); return; }

  const slideCount = parseInt($("#slideCountInput")?.value || "12");
  const audience = $("#audienceInput")?.value.trim() || "";
  const tone = $("#toneSelect")?.value || "Professional";
  const template = state.selectedTemplate || "";

  const files = state.uploads;
  const brief = files.length
    ? `${topic}\n\nSource files: ${files.map(f=>`"${f}"`).join(", ")}`
    : topic;

  try {
    state.wfStep = "outline";
    showStep("outline", state.data || {});
    const result = await api("/api/projects/new", {
      method: "POST",
      body: JSON.stringify({ brief, slide_count: slideCount, audience, tone, template }),
    });
    state.data = result.state;
    state.taskStatus = "running";
    state.uploads = [];
    renderUploadTray([]);
    render(result.state);
    // Start polling for outline.json
    startOutlinePoll();
  } catch (err) {
    state.wfStep = "create";
    showStep("create", state.data || {});
    toast(err.message, "error");
  }
}

/* ─── OUTLINE step ───────────────────────────────────────────────────── */
function startOutlinePoll() {
  if (state.outlinePollTimer) clearInterval(state.outlinePollTimer);
  $("#outlineGenerating")?.classList.remove("hidden");
  state.outlinePollTimer = setInterval(pollOutline, 3000);
  pollOutline();
}

async function pollOutline() {
  try {
    const outline = await api("/api/workflow/outline");
    clearInterval(state.outlinePollTimer);
    state.outlinePollTimer = null;
    state.outline = outline;
    $("#outlineGenerating")?.classList.add("hidden");
    renderOutlineList(outline);
    const btn = $("#btnApproveOutline");
    if (btn) btn.disabled = (state.taskStatus === "running");
  } catch (_) { /* not ready yet */ }
}

function renderOutlinePanel(data) {
  if (state.outline) {
    renderOutlineList(state.outline);
    const btn = $("#btnApproveOutline");
    if (btn) btn.disabled = data?.task?.status === "running";
  } else {
    if (data?.task?.status !== "running") {
      // Might be opening an existing project — try to load outline
      api("/api/workflow/outline").then(o => {
        state.outline = o;
        renderOutlineList(o);
        const btn = $("#btnApproveOutline");
        if (btn) btn.disabled = false;
      }).catch(() => {});
    }
  }
}

function renderOutlineList(outline) {
  const container = $("#outlineList");
  if (!container) return;
  const slides = outline?.slides || [];
  if (!slides.length) {
    container.innerHTML = `<div style="text-align:center;padding:40px;color:var(--text-3)">Waiting for outline…</div>`;
    return;
  }
  container.innerHTML = slides.map((slide, i) => renderSlideCard(slide, i)).join("");
  bindOutlineEvents();
}

function renderSlideCard(slide, i) {
  const points = (slide.key_points || []).map((pt, pi) =>
    `<div class="slide-point">
      <input class="slide-point-input" data-slide="${i}" data-point="${pi}" value="${esc(pt)}" placeholder="Key point…">
    </div>`
  ).join("");
  const layoutCls = slide.layout || "content";
  return `<div class="slide-card" data-index="${i}">
    <div class="slide-card-index">${slide.index || i+1}</div>
    <div class="slide-card-body">
      <div class="slide-card-title-row">
        <input class="slide-card-title-input" data-slide="${i}" data-field="title" value="${esc(slide.title || "")}" placeholder="Slide title…">
        <span class="layout-badge ${layoutCls}">${esc(slide.layout || "content")}</span>
      </div>
      <textarea class="slide-card-purpose" data-slide="${i}" data-field="purpose" rows="2" placeholder="What does this slide achieve?">${esc(slide.purpose || "")}</textarea>
      <div class="slide-card-points">
        ${points}
        <button class="add-point-btn" data-slide="${i}">+ Add point</button>
      </div>
    </div>
    <div class="slide-card-actions">
      <button class="slide-card-btn" data-action="move-up" data-index="${i}" title="Move up">↑</button>
      <button class="slide-card-btn" data-action="move-down" data-index="${i}" title="Move down">↓</button>
      <button class="slide-card-btn danger" data-action="delete" data-index="${i}" title="Delete">✕</button>
    </div>
  </div>`;
}

function bindOutlineEvents() {
  // Edit fields → update state.outline
  $$(".slide-card-title-input").forEach(inp => inp.addEventListener("input", () => {
    const i = +inp.dataset.slide;
    state.outline.slides[i].title = inp.value;
  }));
  $$(".slide-card-purpose").forEach(ta => ta.addEventListener("input", () => {
    const i = +ta.dataset.slide;
    state.outline.slides[i].purpose = ta.value;
  }));
  $$(".slide-point-input").forEach(inp => inp.addEventListener("input", () => {
    const i = +inp.dataset.slide; const pi = +inp.dataset.point;
    state.outline.slides[i].key_points[pi] = inp.value;
  }));
  $$(".add-point-btn").forEach(btn => btn.addEventListener("click", () => {
    const i = +btn.dataset.slide;
    state.outline.slides[i].key_points = state.outline.slides[i].key_points || [];
    state.outline.slides[i].key_points.push("");
    renderOutlineList(state.outline);
  }));
  $$(".slide-card-btn[data-action]").forEach(btn => btn.addEventListener("click", () => {
    const {action, index} = btn.dataset;
    const i = +index;
    const slides = state.outline.slides;
    if (action === "delete" && slides.length > 1) {
      slides.splice(i, 1);
      slides.forEach((s,j) => s.index = j+1);
      renderOutlineList(state.outline);
    } else if (action === "move-up" && i > 0) {
      [slides[i], slides[i-1]] = [slides[i-1], slides[i]];
      slides.forEach((s,j) => s.index = j+1);
      renderOutlineList(state.outline);
    } else if (action === "move-down" && i < slides.length-1) {
      [slides[i], slides[i+1]] = [slides[i+1], slides[i]];
      slides.forEach((s,j) => s.index = j+1);
      renderOutlineList(state.outline);
    }
  }));
}

async function approveOutline() {
  if (state.taskStatus === "running") return;
  // Save any edits to outline.json
  try {
    if (state.outline) {
      await api("/api/workflow/outline", {method:"PATCH", body: JSON.stringify(state.outline)});
    }
    await api("/api/confirm", {method:"POST", body:"{}"});
    state.taskStatus = "running";
    // Transition to assets or build based on image mode
    const imageMode = state.data?.image_mode;
    state.wfStep = (imageMode === "enabled") ? "assets" : "build";
    showStep(state.wfStep, state.data);
    renderThinking(true);
  } catch (err) {
    toast(err.message, "error");
  }
}

/* ─── ASSETS step ────────────────────────────────────────────────────── */
async function skipAssets() {
  try {
    await api("/api/confirm", {method:"POST", body:"{}"});
    state.wfStep = "build";
    showStep("build", state.data);
  } catch (err) {
    toast(err.message, "error");
  }
}

/* ─── BUILD step ─────────────────────────────────────────────────────── */
function renderBuildPanel(data) {
  const slides = data?.slides || [];
  const expected = data?.workflow?.expected_slide_count || 0;

  // Progress
  const bar = $("#buildProgressBar");
  if (data?.task?.status === "running" && slides.length < (expected || 99)) {
    bar?.classList.remove("hidden");
    const pct = expected ? Math.round(slides.length / expected * 100) : 0;
    const fill = $("#buildProgressFill");
    if (fill) fill.style.setProperty("--w", pct + "%"), fill.style.cssText = `--w:${pct}%;`, fill.style.width = pct + "%";
    const lbl = $("#buildProgressLabel");
    if (lbl) lbl.textContent = expected ? `Building slide ${slides.length + 1} of ${expected}…` : "Building…";
  } else {
    bar?.classList.add("hidden");
  }

  // Slides
  const sig = JSON.stringify([state.activeSlide, slides.map(s => [s.name, s.modified]), data?.preview]);
  if (sig === state.sigs.slides) return;
  state.sigs.slides = sig;

  if (state.activeSlide >= slides.length) state.activeSlide = Math.max(0, slides.length - 1);

  const preview = data?.preview;
  const previewRunning = preview?.running && preview?.url;
  const frame = $("#previewFrame");
  const img = $("#activeSlideImg");
  const empty = $("#canvasEmpty");

  if (previewRunning) {
    empty?.classList.add("hidden");
    img?.classList.add("hidden");
    frame?.classList.remove("hidden");
    if (frame && frame.src !== preview.url) frame.src = preview.url;
  } else if (slides.length) {
    empty?.classList.add("hidden");
    frame?.classList.add("hidden");
    img?.classList.remove("hidden");
    const slide = slides[state.activeSlide];
    if (img) img.src = `${slide.url}?v=${slide.modified}`;
  } else {
    empty?.classList.remove("hidden");
    frame?.classList.add("hidden");
    img?.classList.add("hidden");
  }

  const counter = $("#slideCounter");
  if (counter) counter.textContent = slides.length ? `${state.activeSlide + 1} / ${slides.length}` : "0 / 0";

  // Filmstrip
  const strip = $("#filmstrip");
  if (strip) {
    strip.innerHTML = slides.map((slide, i) =>
      `<button class="thumb-btn ${i === state.activeSlide ? "active" : ""}" data-slide="${i}">
        <img src="${slide.url}?v=${slide.modified}" alt="${esc(slide.name)}" loading="lazy">
      </button>`
    ).join("");
    $$(".thumb-btn").forEach(btn => btn.addEventListener("click", () => {
      state.activeSlide = +btn.dataset.slide;
      renderBuildPanel(state.data);
    }));
  }
}

/* ─── EXPORT step ────────────────────────────────────────────────────── */
function renderExportPanel(data) {
  const exports = data?.exports || [];
  const sig = JSON.stringify(exports);
  if (sig === state.sigs.exports) return;
  state.sigs.exports = sig;

  const list = $("#exportsList");
  if (!list) return;
  if (!exports.length) {
    list.innerHTML = `<div style="text-align:center;padding:40px;color:var(--text-3)">No exports yet — run post-processing to generate the PPTX.</div>`;
    return;
  }
  list.innerHTML = exports.map(f => {
    const isPptx = f.name.toLowerCase().endsWith(".pptx");
    return `<a class="export-card" href="${esc(f.url)}" download>
      <div class="export-icon">${isPptx ? "🗂" : "↓"}</div>
      <div class="export-card-info">
        <strong>${esc(f.name)}</strong>
        <span>${fmt(f.size)}</span>
      </div>
      <div class="export-download">
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M10 4v8M6 8l4 4 4-4M4 14h12v2H4z"/>
        </svg>
        Download
      </div>
    </a>`;
  }).join("");
}

/* ─── AI Edit sidebar ────────────────────────────────────────────────── */
function beginAiStream() {
  // When in build/edit mode, stream to the AI sidebar
  if (!["build","export"].includes(state.wfStep)) return;
  const msgs = $("#aiMessages");
  if (!msgs) return;
  const el = document.createElement("div");
  el.className = "ai-msg assistant";
  el.textContent = "";
  msgs.appendChild(el);
  state.streamingNode = el;
  state.streamingText = "";
  msgs.scrollTop = msgs.scrollHeight;
}

function appendAiStream(text) {
  if (!state.streamingNode) beginAiStream();
  state.streamingText += text;
  if (state.streamingNode) {
    state.streamingNode.textContent = state.streamingText;
    const msgs = $("#aiMessages");
    if (msgs) msgs.scrollTop = msgs.scrollHeight;
  }
}

function finishAiStream(content) {
  if (state.streamingNode) {
    state.streamingNode.textContent = content;
    state.streamingNode = null;
    state.streamingText = "";
  }
}

async function sendAiEdit() {
  const input = $("#aiInput");
  const text = input?.value.trim();
  if (!text || state.taskStatus === "running") return;
  if (input) input.value = "";

  const msgs = $("#aiMessages");
  if (msgs) {
    const el = document.createElement("div");
    el.className = "ai-msg user";
    el.textContent = text;
    msgs.appendChild(el);
    msgs.scrollTop = msgs.scrollHeight;
  }

  const scopePrefix = state.aiScope === "slide"
    ? `Edit the current slide (slide ${state.activeSlide + 1}): `
    : "Edit the full deck: ";

  try {
    await api("/api/chat", {method:"POST", body: JSON.stringify({message: scopePrefix + text})});
    state.taskStatus = "running";
    renderThinking(true);
  } catch (err) { toast(err.message, "error"); }
}

/* ─── File uploads ───────────────────────────────────────────────────── */
async function uploadFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  const form = new FormData();
  files.forEach(f => form.append("files", f));
  try {
    toast(`Uploading ${files.length} file${files.length>1?"s":""}…`);
    const result = await api("/api/upload", {method:"POST", body: form});
    state.uploads.push(...result.files);
    renderUploadTray(state.uploads);
    toast("Files ready");
  } catch (err) { toast(err.message, "error"); }
}

function renderUploadTray(paths) {
  const tray = $("#createUploadTray");
  if (!tray) return;
  tray.innerHTML = paths.map((p, i) =>
    `<div class="upload-pill">
      <span>${esc(p.split(/[/\\]/).pop())}</span>
      <button class="upload-pill-remove" data-idx="${i}">✕</button>
    </div>`
  ).join("");
  $$(".upload-pill-remove").forEach(btn => btn.addEventListener("click", () => {
    state.uploads.splice(+btn.dataset.idx, 1);
    renderUploadTray(state.uploads);
  }));
}

/* ─── Project management ─────────────────────────────────────────────── */
async function openProject(path) {
  try {
    const data = await api("/api/projects/open", {method:"POST", body: JSON.stringify({path})});
    state.activeSlide = 0; state.outline = null; state.uploads = [];
    state.sigs = {projects:"",slides:"",exports:""};
    state.data = data;
    const step = phaseToStep(data.workflow?.phase);
    state.wfStep = step;
    closeSidebar();
    render(data);
    toast("Project opened");
    // Try to load outline if we're in outline step
    if (step === "outline") startOutlinePoll();
  } catch (err) { toast(err.message, "error"); }
}

function startNewPresentation(prefill = "") {
  state.outline = null;
  state.uploads = [];
  state.selectedTemplate = "";
  state.wfStep = "create";
  // If no active project yet, show the create panel inside project view
  // We'll create a blank state to show the project view
  if (!state.data?.active_project && !state.data?.active_draft) {
    // Just navigate to create form in the project view
    const welcome = $("#welcomeScreen");
    const pv = $("#projectView");
    welcome?.classList.add("hidden");
    pv?.classList.remove("hidden");
    if (pv) {
      $("#projectTitle").textContent = "New presentation";
      $("#projectEyebrow").textContent = "GETTING STARTED";
    }
  }
  showStep("create", state.data || {});
  renderUploadTray([]);
  // Reset form
  const ta = $("#topicInput");
  if (ta) ta.value = prefill;
  $$(".template-item").forEach(b => b.classList.toggle("selected", b.dataset.template === ""));
  state.selectedTemplate = "";
  closeSidebar();
  if (prefill && ta) ta.focus();
}

async function triggerExport() {
  if (!state.data?.active_project) { toast("Open a project first.", "error"); return; }
  try {
    await api("/api/chat", {method:"POST", body: JSON.stringify({
      message: "Complete quality checking and post-processing, then export the active project to PPTX. Respect all workflow gates.",
    })});
    state.taskStatus = "running";
    renderThinking(true);
  } catch (err) { toast(err.message, "error"); }
}

async function startPreview() {
  if (!state.data?.active_project) { toast("Open a project first.", "error"); return; }
  try {
    const result = await api("/api/preview/start", {method:"POST", body:"{}"});
    await refreshState({silent:false});
    toast(`Preview ready at ${result.url}`);
  } catch (err) { toast(err.message, "error"); }
}

/* ─── Sidebar toggle ─────────────────────────────────────────────────── */
function openMobileSidebar() {
  $("#sidebar")?.classList.add("open");
  $("#sidebarOverlay")?.classList.remove("hidden");
}
function closeSidebar() {
  $("#sidebar")?.classList.remove("open");
  $("#sidebarOverlay")?.classList.add("hidden");
}

/* ─── Utility ────────────────────────────────────────────────────────── */
function cleanName(name) {
  return name.replace(/_(ppt169|ppt43|xhs|story|a4)_\d{8}$/i,"").replace(/_/g," ");
}

function toast(msg, type = "") {
  const region = $("#toastRegion");
  if (!region) return;
  const el = document.createElement("div");
  el.className = "toast " + (type || "");
  el.textContent = msg;
  region.appendChild(el);
  setTimeout(() => el.remove(), 3900);
}

/* ─── Event bindings ─────────────────────────────────────────────────── */

// New project
$("#btnNewProject")?.addEventListener("click", () => startNewPresentation());
$("#btnWelcomeNew")?.addEventListener("click", () => startNewPresentation());

// Example cards
$$(".example-card").forEach(btn => btn.addEventListener("click", () => {
  startNewPresentation(btn.dataset.prompt || "");
}));

// Create form
$("#btnCreate")?.addEventListener("click", handleCreate);

// File upload
const fileInput = $("#createFileInput");
fileInput?.addEventListener("change", e => uploadFiles(e.target.files));

const dropZone = $("#createDropZone");
dropZone?.addEventListener("dragover", e => { e.preventDefault(); dropZone.classList.add("dragging"); });
dropZone?.addEventListener("dragleave", () => dropZone.classList.remove("dragging"));
dropZone?.addEventListener("drop", e => {
  e.preventDefault(); dropZone.classList.remove("dragging");
  uploadFiles(e.dataTransfer.files);
});

// Template cards (initial — Free Design)
$("#templateFreeDesign")?.addEventListener("click", () => {
  state.selectedTemplate = "";
  $$(".template-item").forEach(b => b.classList.toggle("selected", b.dataset.template === ""));
});

// Image toggle
$("#imageToggle")?.addEventListener("click", () => {
  state.imageOn = !state.imageOn;
  const btn = $("#imageToggle");
  btn.dataset.on = String(state.imageOn);
  btn.setAttribute("aria-pressed", String(state.imageOn));
  $("#imageToggleLabel").textContent = state.imageOn ? "On" : "Off";
});

// Outline: approve
$("#btnApproveOutline")?.addEventListener("click", approveOutline);

// Outline: add slide
$("#btnAddSlide")?.addEventListener("click", () => {
  if (!state.outline) return;
  const slides = state.outline.slides;
  slides.push({
    index: slides.length + 1,
    title: "New slide",
    purpose: "",
    key_points: [],
    layout: "content",
  });
  renderOutlineList(state.outline);
});

// Assets
$("#btnSkipAssets")?.addEventListener("click", skipAssets);
$("#btnProceedAssets")?.addEventListener("click", async () => {
  state.wfStep = "build";
  showStep("build", state.data);
});

// Build: slide nav
$("#btnPrevSlide")?.addEventListener("click", () => {
  if (state.activeSlide > 0) { state.activeSlide--; renderBuildPanel(state.data); }
});
$("#btnNextSlide")?.addEventListener("click", () => {
  const max = (state.data?.slides?.length || 1) - 1;
  if (state.activeSlide < max) { state.activeSlide++; renderBuildPanel(state.data); }
});

// Build: editor / export
$$("[id^='btnOpenEditor'], #btnOpenEditorExport").forEach(btn => btn?.addEventListener("click", startPreview));
$$("[id^='btnExport']").forEach(btn => btn?.addEventListener("click", triggerExport));

// AI sidebar
$("#btnCloseSidebar")?.addEventListener("click", () => {
  $("#aiSidebar")?.classList.add("hidden");
  $("#btnOpenSidebar")?.classList.remove("hidden");
});
$("#btnOpenSidebar")?.addEventListener("click", () => {
  $("#aiSidebar")?.classList.remove("hidden");
  $("#btnOpenSidebar")?.classList.add("hidden");
});

$$(".scope-btn").forEach(btn => btn.addEventListener("click", () => {
  state.aiScope = btn.dataset.scope;
  $$(".scope-btn").forEach(b => b.classList.toggle("active", b.dataset.scope === state.aiScope));
}));

$("#btnAiSend")?.addEventListener("click", sendAiEdit);
$("#aiInput")?.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendAiEdit(); }
});

// Refresh projects
$("#btnRefresh")?.addEventListener("click", () => refreshState({silent:false}));

// Settings
const PROVIDER_DEFAULTS = {
  novita: "google/gemma-4-31b-it",
  openrouter: "google/gemini-2.0-flash-001",
};

let settingsProvider = "novita";

function openSettings() {
  $("#settingsModal")?.classList.remove("hidden");
  loadSettings();
}

function updateProviderUI() {
  $$(".provider-btn").forEach(b => b.classList.toggle("active", b.dataset.provider === settingsProvider));
  const orField = $("#orKeyField");
  if (orField) orField.style.display = settingsProvider === "openrouter" ? "" : "none";
  const modelInput = $("#settingsModel");
  if (modelInput && !modelInput.value) modelInput.value = PROVIDER_DEFAULTS[settingsProvider];
}

async function loadSettings() {
  try {
    const s = await api("/api/settings");
    settingsProvider = s.provider || "novita";
    updateProviderUI();

    // Novita key (shared: text when Novita selected + image generation)
    const novitaKey = $("#settingsNovitaKey");
    if (novitaKey) {
      novitaKey.value = s.novita_key_set ? s.novita_key_preview : "";
      novitaKey.placeholder = s.novita_key_set ? "Paste to replace" : "Paste key…";
      novitaKey.addEventListener("focus", () => { if (novitaKey.value === s.novita_key_preview) novitaKey.value = ""; }, { once: true });
    }
    const novitaStatus = $("#novitaKeyStatus");
    if (novitaStatus) novitaStatus.textContent = s.novita_key_set ? `Configured (${s.novita_key_preview})` : "Not set";

    // OpenRouter key (only visible when OpenRouter selected)
    const textKey = $("#settingsTextKey");
    if (textKey) {
      textKey.value = s.text_key_set && settingsProvider === "openrouter" ? s.text_key_preview : "";
      textKey.placeholder = s.text_key_set ? "Paste to replace" : "Paste key…";
      textKey.addEventListener("focus", () => { if (textKey.value === s.text_key_preview) textKey.value = ""; }, { once: true });
    }
    const textStatus = $("#textKeyStatus");
    if (textStatus) textStatus.textContent = s.text_key_set ? `Configured (${s.text_key_preview})` : "Not set";

    const modelInput = $("#settingsModel");
    if (modelInput) modelInput.value = s.model || PROVIDER_DEFAULTS[settingsProvider];
    const imageModeEl = $("#settingsImageMode");
    if (imageModeEl) imageModeEl.value = s.image_mode || "disabled";
  } catch (err) { toast(err.message, "error"); }
}

async function saveSettings() {
  const novitaKeyEl = $("#settingsNovitaKey");
  const textKeyEl = $("#settingsTextKey");
  const novitaKeyVal = novitaKeyEl?.value.trim() || "";
  const textKeyVal = textKeyEl?.value.trim() || "";
  const model = $("#settingsModel")?.value.trim();
  const imageMode = $("#settingsImageMode")?.value;
  // Don't send preview strings as new keys
  const novitaKey = novitaKeyVal.endsWith("…") ? "" : novitaKeyVal;
  const textKey = textKeyVal.endsWith("…") ? "" : textKeyVal;
  try {
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ provider: settingsProvider, text_api_key: textKey, novita_api_key: novitaKey, model, image_mode: imageMode }),
    });
    toast("Settings saved", "success");
    $("#settingsModal")?.classList.add("hidden");
    refreshState();
  } catch (err) { toast(err.message, "error"); }
}

$$(".provider-btn").forEach(btn => btn.addEventListener("click", () => {
  settingsProvider = btn.dataset.provider;
  updateProviderUI();
}));

// Show/hide toggles
[["#btnShowTextKey","#settingsTextKey"],["#btnShowNovitaKey","#settingsNovitaKey"]].forEach(([btn, inp]) => {
  $(btn)?.addEventListener("click", () => {
    const el = $(inp);
    if (el) el.type = el.type === "password" ? "text" : "password";
  });
});

$("#btnSettings")?.addEventListener("click", openSettings);
$("#btnCloseSettings")?.addEventListener("click", () => $("#settingsModal")?.classList.add("hidden"));
$("#btnCancelSettings")?.addEventListener("click", () => $("#settingsModal")?.classList.add("hidden"));
$("#btnSaveSettings")?.addEventListener("click", saveSettings);

// Mobile nav
$("#btnHamburger")?.addEventListener("click", openMobileSidebar);
$("#btnHamburger2")?.addEventListener("click", openMobileSidebar);
$("#sidebarOverlay")?.addEventListener("click", closeSidebar);

// Keyboard
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    closeSidebar();
    $("#settingsModal")?.classList.add("hidden");
  }
  if (state.wfStep === "build" && !e.target.matches("input,textarea,select")) {
    const slides = state.data?.slides || [];
    if (e.key === "ArrowLeft" && state.activeSlide > 0) {
      state.activeSlide--; renderBuildPanel(state.data);
    }
    if (e.key === "ArrowRight" && state.activeSlide < slides.length - 1) {
      state.activeSlide++; renderBuildPanel(state.data);
    }
  }
});

/* ─── Boot ───────────────────────────────────────────────────────────── */
refreshState({silent: false}).then(() => {
  // Load templates in background
  loadTemplates();
  // If in outline step but no outline yet, try to load
  if (state.wfStep === "outline" && !state.outline) startOutlinePoll();
});
connectEvents();

// Keep-alive poll
setInterval(() => {
  if (!state.eventSource || state.eventSource.readyState === EventSource.CLOSED) {
    refreshState();
  }
}, 20000);
