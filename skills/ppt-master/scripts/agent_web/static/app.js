const state = {
  data: null,
  uploads: [],
  activeSlide: 0,
  eventSource: null,
  reconnectTimer: null,
  streamingNode: null,
  streamingText: "",
  lastMessageCount: 0,
  messageSignature: "",
  projectSignature: "",
  phaseSignature: "",
  previewSignature: "",
  exportSignature: "",
  taskStatus: "idle",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const elements = {
  rail: $("#rail"),
  projectList: $("#projectList"),
  projectTitle: $("#projectTitle"),
  projectEyebrow: $("#projectEyebrow"),
  connectionDot: $("#connectionDot"),
  connectionLabel: $("#connectionLabel"),
  modelLabel: $("#modelLabel"),
  imageModeLabel: $("#imageModeLabel"),
  welcome: $("#welcomeState"),
  stream: $("#messageStream"),
  messageInput: $("#messageInput"),
  sendButton: $("#sendButton"),
  thinkingChip: $("#thinkingChip"),
  confirmCard: $("#confirmCard"),
  uploadTray: $("#uploadTray"),
  phaseTrack: $("#phaseTrack"),
  phaseBadge: $("#phaseBadge"),
  previewEmpty: $("#previewEmpty"),
  previewFrame: $("#previewFrame"),
  slideCanvas: $("#slideCanvas"),
  activeSlideImage: $("#activeSlideImage"),
  slideToolbar: $("#slideToolbar"),
  slideCounter: $("#slideCounter"),
  filmstrip: $("#filmstrip"),
  slideCount: $("#slideCount"),
  tokenCount: $("#tokenCount"),
  imageModeStat: $("#imageModeStat"),
  exportList: $("#exportList"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body instanceof FormData ? undefined : {"Content-Type": "application/json"},
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

async function refreshState({silent = true} = {}) {
  try {
    const data = await api("/api/state");
    const previousTask = state.taskStatus;
    state.data = data;
    state.taskStatus = data.task.status;
    render(data);
    if (previousTask === "running" && data.task.status === "complete") {
      toast("Agent turn complete");
    }
    if (data.task.status === "error" && previousTask !== "error") {
      toast(data.task.error || "The agent turn failed.", true);
    }
  } catch (error) {
    renderConnection(false);
    if (!silent) toast(error.message, true);
  }
}

function connectEvents() {
  if (state.eventSource) state.eventSource.close();
  const source = new EventSource("/api/events");
  state.eventSource = source;
  source.onopen = () => renderConnection(true);
  source.onerror = () => {
    renderConnection(false);
  };
  source.addEventListener("assistant_start", () => beginStreamingMessage());
  source.addEventListener("assistant_delta", event => {
    const payload = JSON.parse(event.data);
    appendStreamingText(payload.text || "");
  });
  source.addEventListener("assistant_done", event => {
    const payload = JSON.parse(event.data);
    finishStreamingMessage(payload.content || state.streamingText);
  });
  source.addEventListener("tool_start", event => {
    const payload = JSON.parse(event.data);
    setWorkingLabel(`Running ${friendlyToolName(payload.name)}...`);
  });
  source.addEventListener("tool_done", () => setWorkingLabel("Working"));
  source.addEventListener("task_started", event => {
    const payload = JSON.parse(event.data);
    if (state.data) {
      state.data.task = payload.task;
      state.taskStatus = "running";
      render(state.data);
    }
  });
  ["task_complete", "task_error", "state"].forEach(name => {
    source.addEventListener(name, async event => {
      const payload = JSON.parse(event.data);
      if (payload.state) {
        state.data = payload.state;
        render(payload.state);
      } else {
        await refreshState();
      }
      if (name === "task_complete") toast("Agent turn complete");
      if (name === "task_error") {
        toast(payload.state?.task?.error || "The agent turn failed.", true);
      }
    });
  });
}

function render(data) {
  renderConnection(true);
  renderHeader(data);
  renderProjects(data);
  renderMessages(data);
  renderPhase(data);
  renderPreview(data);
  renderStats(data);
  renderExports(data);
  renderSettings(data);
  const running = data.task.status === "running";
  elements.thinkingChip.classList.toggle("hidden", !running);
  elements.sendButton.disabled = running;
}

function renderConnection(online) {
  elements.connectionDot.className = `status-dot ${online ? "online" : "error"}`;
  elements.connectionLabel.textContent = online ? "Studio online" : "Disconnected";
}

function renderHeader(data) {
  const projectName = data.active_label ||
    (data.active_project ? data.active_project.split(/[\\/]/).pop() : "");
  elements.projectTitle.textContent = projectName || "Create something worth presenting";
  elements.projectEyebrow.textContent = projectName ? "ACTIVE PROJECT" : "LOCAL WORKSPACE";
  elements.modelLabel.textContent = data.model;
  const imageLabels = {disabled: "SVG-native", "prompts-only": "Prompts only", enabled: "AI images"};
  elements.imageModeLabel.textContent = imageLabels[data.image_mode] || data.image_mode;
}

function renderProjects(data) {
  const signature = JSON.stringify([
    data.active_project,
    data.active_draft,
    data.projects.map(project => [
      project.path,
      project.phase,
      project.slides,
      project.modified,
    ]),
  ]);
  if (signature === state.projectSignature) return;
  state.projectSignature = signature;
  const draft = data.active_draft ? `
    <div class="project-item active draft-item">
      <strong>${escapeHtml(data.active_label || "New presentation")}</strong>
      <span><i>Draft</i><i>Working</i></span>
    </div>` : "";
  if (!data.projects.length && !draft) {
    elements.projectList.innerHTML = `<div class="project-item"><strong>No projects yet</strong><span><i>Start a new presentation</i></span></div>`;
    return;
  }
  elements.projectList.innerHTML = draft + data.projects.map(project => {
    const active = project.path === data.active_project ? "active" : "";
    return `<button class="project-item ${active}" data-project="${escapeAttr(project.path)}">
      <strong>${escapeHtml(cleanProjectName(project.name))}</strong>
      <span><i>${escapeHtml(labelForPhase(project.phase, data))}</i><i>${project.slides} slides</i></span>
    </button>`;
  }).join("");
  $$(".project-item[data-project]").forEach(button => {
    button.addEventListener("click", () => openProject(button.dataset.project));
  });
}

function renderMessages(data) {
  if (state.streamingNode && data.task.status === "running") return;
  const hasMessages = data.messages.length > 0;
  elements.welcome.classList.toggle("hidden", hasMessages);
  elements.stream.classList.toggle("active", hasMessages);
  const signature = JSON.stringify(data.messages);
  if (!hasMessages && state.messageSignature) {
    elements.stream.innerHTML = "";
    state.messageSignature = "";
    state.lastMessageCount = 0;
  } else if (hasMessages && signature !== state.messageSignature) {
    const shouldScroll = data.messages.length !== state.lastMessageCount;
    state.messageSignature = signature;
    elements.stream.innerHTML = data.messages.map(message => `
      <article class="message ${message.role}">
        <div class="message-avatar">${message.role === "assistant" ? "P" : "You"}</div>
        <div class="message-body">
          <div class="message-meta">${message.role === "assistant" ? "PPT Master" : "You"}</div>
          <div class="message-content">${renderMarkdown(message.content)}</div>
        </div>
      </article>
    `).join("");
    if (shouldScroll) {
      requestAnimationFrame(() => {
        elements.stream.scrollTop = elements.stream.scrollHeight;
      });
    }
    state.lastMessageCount = data.messages.length;
  }
  const awaiting = data.workflow.phase === "awaiting_confirmations" &&
    !data.workflow.confirmations_approved &&
    data.task.status !== "running";
  elements.confirmCard.classList.toggle("hidden", !awaiting);
}

function beginStreamingMessage() {
  if (state.streamingNode) return;
  elements.welcome.classList.add("hidden");
  elements.stream.classList.add("active");
  const article = document.createElement("article");
  article.className = "message assistant streaming";
  article.innerHTML = `
    <div class="message-avatar">P</div>
    <div class="message-body">
      <div class="message-meta">PPT Master</div>
      <div class="message-content"><span class="stream-cursor"></span></div>
    </div>`;
  elements.stream.appendChild(article);
  state.streamingNode = article;
  state.streamingText = "";
  elements.stream.scrollTop = elements.stream.scrollHeight;
}

function appendStreamingText(text) {
  if (!state.streamingNode) beginStreamingMessage();
  state.streamingText += text;
  const content = state.streamingNode.querySelector(".message-content");
  content.textContent = state.streamingText;
  const cursor = document.createElement("span");
  cursor.className = "stream-cursor";
  content.appendChild(cursor);
  elements.stream.scrollTop = elements.stream.scrollHeight;
}

function finishStreamingMessage(content) {
  if (!state.streamingNode) return;
  state.streamingNode.querySelector(".message-content").innerHTML =
    renderMarkdown(content);
  state.streamingNode.classList.remove("streaming");
  state.streamingNode = null;
  state.streamingText = "";
}

function setWorkingLabel(label) {
  const labelNode = elements.thinkingChip.lastChild;
  if (labelNode) labelNode.textContent = label;
}

function friendlyToolName(name = "") {
  return name.replaceAll("_", " ").replace(/\b\w/g, char => char.toUpperCase());
}

function appendOptimisticUserMessage(content) {
  elements.welcome.classList.add("hidden");
  elements.stream.classList.add("active");
  const article = document.createElement("article");
  article.className = "message user";
  article.innerHTML = `
    <div class="message-avatar">You</div>
    <div class="message-body">
      <div class="message-meta">You</div>
      <div class="message-content">${renderMarkdown(content)}</div>
    </div>`;
  elements.stream.appendChild(article);
  elements.stream.scrollTop = elements.stream.scrollHeight;
}

function renderPhase(data) {
  const phases = data.phase_order || Object.keys(data.phase_labels);
  const current = phases.indexOf(data.workflow.phase);
  elements.phaseBadge.textContent = data.phase_labels[data.workflow.phase] || data.workflow.phase;
  const signature = `${data.workflow.phase}|${phases.join("|")}`;
  if (signature === state.phaseSignature) return;
  state.phaseSignature = signature;
  elements.phaseTrack.innerHTML = phases.map((phase, index) => {
    const cls = index < current ? "complete" : index === current ? "active" : "";
    return `<div class="phase-step ${cls}">
      <div class="phase-line"></div>
      <span>${escapeHtml(data.phase_labels[phase])}</span>
    </div>`;
  }).join("");
}

function renderPreview(data) {
  const slides = data.slides || [];
  if (state.activeSlide >= slides.length) state.activeSlide = Math.max(0, slides.length - 1);
  const previewRunning = data.preview && data.preview.running && data.preview.url;
  const signature = JSON.stringify([
    previewRunning ? data.preview.url : "",
    state.activeSlide,
    slides.map(slide => [slide.name, slide.modified]),
  ]);
  if (signature === state.previewSignature) return;
  state.previewSignature = signature;
  if (previewRunning) {
    elements.previewEmpty.classList.add("hidden");
    elements.slideCanvas.classList.add("hidden");
    elements.previewFrame.classList.remove("hidden");
    if (elements.previewFrame.src !== data.preview.url) elements.previewFrame.src = data.preview.url;
  } else if (slides.length) {
    elements.previewEmpty.classList.add("hidden");
    elements.previewFrame.classList.add("hidden");
    elements.slideCanvas.classList.remove("hidden");
    const slide = slides[state.activeSlide];
    elements.activeSlideImage.src = `${slide.url}?v=${slide.modified}`;
  } else {
    elements.previewEmpty.classList.remove("hidden");
    elements.previewFrame.classList.add("hidden");
    elements.slideCanvas.classList.add("hidden");
  }
  elements.slideToolbar.classList.toggle("hidden", slides.length === 0);
  elements.slideCounter.textContent = slides.length ? `${state.activeSlide + 1} / ${slides.length}` : "0 / 0";
  elements.filmstrip.innerHTML = slides.map((slide, index) => `
    <button class="thumb ${index === state.activeSlide ? "active" : ""}" data-slide="${index}">
      <img src="${slide.url}?v=${slide.modified}" alt="${escapeAttr(slide.name)}">
    </button>
  `).join("");
  $$(".thumb").forEach(button => button.addEventListener("click", () => {
    state.activeSlide = Number(button.dataset.slide);
    renderPreview(state.data);
  }));
}

function renderStats(data) {
  elements.slideCount.textContent = data.slides.length;
  elements.tokenCount.textContent = compactNumber(data.usage.total_tokens || 0);
  elements.imageModeStat.textContent = data.image_mode === "disabled" ? "Off" :
    data.image_mode === "prompts-only" ? "Prompt" : "On";
}

function renderExports(data) {
  const signature = JSON.stringify(data.exports);
  if (signature === state.exportSignature) return;
  state.exportSignature = signature;
  elements.exportList.innerHTML = data.exports.slice(0, 3).map(file => `
    <a class="export-card" href="${file.url}">
      <span class="export-icon">${file.name.toLowerCase().endsWith(".pptx") ? "P" : "↓"}</span>
      <span><strong>${escapeHtml(file.name)}</strong><span>${formatBytes(file.size)}</span></span>
      <span class="download">Download</span>
    </a>
  `).join("");
}

function renderSettings(data) {
  $("#settingsModel").textContent = data.model;
  $("#settingsImages").textContent = data.image_mode;
  $("#settingsKey").textContent = data.key_configured ? "Configured" : "Missing";
}

async function sendMessage(text = elements.messageInput.value.trim()) {
  if (!text || state.data?.task.status === "running") return;
  const files = state.uploads.map(file => `"${file}"`).join(", ");
  const message = files ? `${text}\n\nLocal source files: ${files}` : text;
  elements.messageInput.value = "";
  autoSizeInput();
  state.uploads = [];
  renderUploadTray();
  appendOptimisticUserMessage(message);
  try {
    await api("/api/chat", {method: "POST", body: JSON.stringify({message})});
    if (state.data) {
      state.data.task.status = "running";
      render(state.data);
    }
  } catch (error) {
    toast(error.message, true);
  }
}

async function uploadFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  const body = new FormData();
  files.forEach(file => body.append("files", file));
  try {
    toast(`Uploading ${files.length} source${files.length > 1 ? "s" : ""}…`);
    const result = await api("/api/upload", {method: "POST", body});
    state.uploads.push(...result.files);
    renderUploadTray();
    toast("Sources ready");
  } catch (error) {
    toast(error.message, true);
  }
}

function renderUploadTray() {
  elements.uploadTray.classList.toggle("hidden", state.uploads.length === 0);
  elements.uploadTray.innerHTML = state.uploads.map(path => `
    <div class="upload-pill"><b>↥</b><span>${escapeHtml(path.split(/[\\/]/).pop())}</span></div>
  `).join("");
}

async function openProject(path) {
  try {
    const data = await api("/api/projects/open", {method: "POST", body: JSON.stringify({path})});
    state.activeSlide = 0;
    elements.rail.classList.remove("open");
    state.data = data;
    resetRenderSignatures();
    render(data);
    toast("Project opened");
  } catch (error) {
    toast(error.message, true);
  }
}

async function createProject() {
  const brief = $("#newProjectBrief").value.trim();
  if (!brief) return toast("Add a presentation brief first.", true);
  try {
    closeModal($("#newProjectModal"));
    const result = await api("/api/projects/new", {method: "POST", body: JSON.stringify({brief})});
    state.activeSlide = 0;
    state.streamingNode = null;
    state.streamingText = "";
    resetRenderSignatures();
    state.data = result.state;
    render(result.state);
    appendOptimisticUserMessage(brief);
  } catch (error) {
    toast(error.message, true);
  }
}

function resetRenderSignatures() {
  state.messageSignature = "";
  state.projectSignature = "";
  state.phaseSignature = "";
  state.previewSignature = "";
  state.exportSignature = "";
  state.lastMessageCount = 0;
  elements.stream.innerHTML = "";
}

async function startPreview() {
  if (!state.data?.active_project) return toast("Open a project before starting preview.", true);
  try {
    const result = await api("/api/preview/start", {method: "POST", body: "{}"});
    await refreshState({silent: false});
    toast(`Preview ready at ${result.url}`);
  } catch (error) {
    toast(error.message, true);
  }
}

async function triggerExport() {
  if (!state.data?.active_project) return toast("Open a project before exporting.", true);
  await sendMessage("Complete all currently eligible quality and post-processing steps, then export the active project to PPTX. Respect every workflow gate.");
}

function autoSizeInput() {
  elements.messageInput.style.height = "auto";
  elements.messageInput.style.height = `${Math.min(elements.messageInput.scrollHeight, 140)}px`;
}

function openModal(element) { element.classList.remove("hidden"); }
function closeModal(element) { element.classList.add("hidden"); }

function renderMarkdown(source) {
  let text = escapeHtml(String(source || ""));
  const codeBlocks = [];
  text = text.replace(/```([\s\S]*?)```/g, (_, code) => {
    codeBlocks.push(`<pre><code>${code.trim()}</code></pre>`);
    return `@@CODE${codeBlocks.length - 1}@@`;
  });
  text = text
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h2>$1</h2>")
    .replace(/^# (.+)$/gm, "<h1>$1</h1>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/^\s*[-*] (.+)$/gm, "<li>$1</li>")
    .replace(/(<li>.*<\/li>\n?)+/g, match => `<ul>${match}</ul>`)
    .split(/\n{2,}/)
    .map(block => /^<(h\d|ul|pre)/.test(block) || /^@@CODE/.test(block) ? block : `<p>${block.replace(/\n/g, "<br>")}</p>`)
    .join("");
  return text.replace(/@@CODE(\d+)@@/g, (_, index) => codeBlocks[Number(index)]);
}

function toast(message, error = false) {
  const item = document.createElement("div");
  item.className = `toast ${error ? "error" : ""}`;
  item.textContent = message;
  $("#toastRegion").appendChild(item);
  setTimeout(() => item.remove(), 3900);
}

function cleanProjectName(name) {
  return name.replace(/_(ppt169|ppt43|xhs|story|a4)_\d{8}$/i, "").replace(/_/g, " ");
}
function labelForPhase(phase, data) { return data.phase_labels[phase] || phase; }
function compactNumber(value) { return new Intl.NumberFormat("en", {notation: "compact", maximumFractionDigits: 1}).format(value); }
function formatBytes(bytes) { return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`; }
function escapeHtml(value) { return String(value).replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[char])); }
function escapeAttr(value) { return escapeHtml(value).replace(/`/g, "&#096;"); }

elements.sendButton.addEventListener("click", () => sendMessage());
elements.messageInput.addEventListener("input", autoSizeInput);
elements.messageInput.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
});
$("#fileInput").addEventListener("change", event => uploadFiles(event.target.files));
$("#composer").addEventListener("dragover", event => { event.preventDefault(); event.currentTarget.classList.add("dragging"); });
$("#composer").addEventListener("dragleave", event => event.currentTarget.classList.remove("dragging"));
$("#composer").addEventListener("drop", event => {
  event.preventDefault();
  event.currentTarget.classList.remove("dragging");
  uploadFiles(event.dataTransfer.files);
});
$("#confirmButton").addEventListener("click", async () => {
  try {
    await api("/api/confirm", {method: "POST", body: "{}"});
    if (state.data) {
      state.data.task.status = "running";
      render(state.data);
    }
  } catch (error) { toast(error.message, true); }
});
$("#newProjectButton").addEventListener("click", () => openModal($("#newProjectModal")));
$("#createProjectButton").addEventListener("click", createProject);
$("#settingsButton").addEventListener("click", () => openModal($("#settingsModal")));
$("#refreshProjects").addEventListener("click", () => refreshState({silent: false}));
$("#previewButton").addEventListener("click", startPreview);
$("#editorButton").addEventListener("click", startPreview);
$("#exportButton").addEventListener("click", triggerExport);
$("#previousSlide").addEventListener("click", () => { if (state.activeSlide > 0) { state.activeSlide--; renderPreview(state.data); } });
$("#nextSlide").addEventListener("click", () => { if (state.activeSlide < (state.data?.slides.length || 1) - 1) { state.activeSlide++; renderPreview(state.data); } });
$("#mobileMenu").addEventListener("click", () => elements.rail.classList.toggle("open"));
$$("[data-close-modal]").forEach(button => button.addEventListener("click", () => closeModal($("#newProjectModal"))));
$$("[data-close-settings]").forEach(button => button.addEventListener("click", () => closeModal($("#settingsModal"))));
$$(".starter-card").forEach(button => button.addEventListener("click", () => {
  elements.messageInput.value = button.dataset.prompt;
  autoSizeInput();
  elements.messageInput.focus();
}));
document.addEventListener("keydown", event => {
  if (event.key === "Escape") {
    $$(".modal-backdrop").forEach(closeModal);
    elements.rail.classList.remove("open");
  }
});

refreshState({silent: false});
connectEvents();
setInterval(() => {
  if (!state.eventSource || state.eventSource.readyState === EventSource.CLOSED) {
    refreshState();
  }
}, 20000);
