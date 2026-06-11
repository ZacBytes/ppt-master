'use client';

import { useState, useEffect, useRef, useCallback, ChangeEvent, KeyboardEvent, DragEvent } from 'react';

// ─── Types ────────────────────────────────────────────────────────────────────

interface Slide { name: string; url: string; modified: number; }
interface Export { name: string; size: number; url: string; modified: string; }
interface Project { name: string; path: string; phase: string; slides: number; modified: string; }
interface Workflow { phase: string; confirmations_approved: boolean; }
interface Usage { total_tokens: number; }
interface Task { id: string; status: 'idle' | 'running' | 'complete' | 'error'; error: string; }
interface Preview { running: boolean; url: string; }
interface Message { role: 'user' | 'assistant'; content: string; }

interface AppState {
  task: Task;
  workflow: Workflow;
  messages: Message[];
  usage: Usage;
  projects: Project[];
  active_project: string;
  active_label: string;
  active_draft: boolean;
  slides: Slide[];
  exports: Export[];
  preview: Preview;
  model: string;
  image_mode: string;
  phase_labels: Record<string, string>;
  phase_order: string[];
  key_configured: boolean;
}

interface Toast { id: number; message: string; error: boolean; }
interface StreamingState { active: boolean; text: string; workingLabel: string; }

// ─── Helpers ─────────────────────────────────────────────────────────────────

function escapeHtml(value: string): string {
  return String(value).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[c] ?? c));
}

function renderMarkdown(source: string): string {
  let text = escapeHtml(String(source || ''));
  const codeBlocks: string[] = [];
  text = text.replace(/```([\s\S]*?)```/g, (_, code) => {
    codeBlocks.push(`<pre><code>${code.trim()}</code></pre>`);
    return `@@CODE${codeBlocks.length - 1}@@`;
  });
  text = text
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/^\s*[-*] (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>\n?)+/g, (m) => `<ul>${m}</ul>`)
    .split(/\n{2,}/)
    .map((block) =>
      /^<(h\d|ul|pre)/.test(block) || /^@@CODE/.test(block)
        ? block
        : `<p>${block.replace(/\n/g, '<br>')}</p>`,
    )
    .join('');
  return text.replace(/@@CODE(\d+)@@/g, (_, i) => codeBlocks[Number(i)]);
}

function cleanProjectName(name: string): string {
  return name.replace(/_(ppt169|ppt43|xhs|story|a4)_\d{8}$/i, '').replace(/_/g, ' ');
}

function compactNumber(value: number): string {
  return new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(value);
}

function formatBytes(bytes: number): string {
  return bytes < 1024 * 1024
    ? `${Math.max(1, Math.round(bytes / 1024))} KB`
    : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function imageLabels(mode: string): string {
  return ({ disabled: 'SVG-native', 'prompts-only': 'Prompts only', enabled: 'AI images' } as Record<string, string>)[mode] ?? mode;
}

async function apiFetch(path: string, options: RequestInit = {}): Promise<unknown> {
  const isFormData = options.body instanceof FormData;
  const response = await fetch(path, {
    headers: isFormData ? undefined : { 'Content-Type': 'application/json' },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error((payload as { error?: string }).error ?? `Request failed (${response.status})`);
  return payload;
}

// ─── Studio Component ─────────────────────────────────────────────────────────

export default function Studio() {
  const [data, setData] = useState<AppState | null>(null);
  const [connected, setConnected] = useState(false);
  const [activeSlide, setActiveSlide] = useState(0);
  const [uploads, setUploads] = useState<string[]>([]);
  const [streaming, setStreaming] = useState<StreamingState>({ active: false, text: '', workingLabel: 'Working' });
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [railOpen, setRailOpen] = useState(false);
  const [showNewProject, setShowNewProject] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [newBrief, setNewBrief] = useState('');
  const [inputText, setInputText] = useState('');
  const [dragging, setDragging] = useState(false);

  const streamRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const toastCounter = useRef(0);
  const esRef = useRef<EventSource | null>(null);

  const addToast = useCallback((message: string, error = false) => {
    const id = ++toastCounter.current;
    setToasts((prev) => [...prev, { id, message, error }]);
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 3900);
  }, []);

  const refreshState = useCallback(async (silent = true) => {
    try {
      const result = await apiFetch('/api/state') as AppState;
      setData((prev) => {
        if (prev?.task.status === 'running' && result.task.status === 'complete') {
          setTimeout(() => addToast('Agent turn complete'), 0);
        }
        if (result.task.status === 'error' && prev?.task.status !== 'error') {
          setTimeout(() => addToast(result.task.error || 'The agent turn failed.', true), 0);
        }
        return result;
      });
      setConnected(true);
    } catch {
      setConnected(false);
      if (!silent) addToast('Could not reach the studio server.', true);
    }
  }, [addToast]);

  // SSE connection
  useEffect(() => {
    function connect() {
      if (esRef.current) esRef.current.close();
      const es = new EventSource('/api/events');
      esRef.current = es;

      es.onopen = () => setConnected(true);
      es.onerror = () => setConnected(false);

      es.addEventListener('assistant_start', () => {
        setStreaming({ active: true, text: '', workingLabel: 'Working' });
      });
      es.addEventListener('assistant_delta', (e) => {
        const payload = JSON.parse(e.data);
        setStreaming((prev) => ({ ...prev, text: prev.text + (payload.text ?? '') }));
      });
      es.addEventListener('assistant_done', (e) => {
        const payload = JSON.parse(e.data);
        setStreaming({ active: false, text: payload.content ?? '', workingLabel: 'Working' });
        refreshState();
      });
      es.addEventListener('tool_start', (e) => {
        const payload = JSON.parse(e.data);
        const name = (payload.name ?? '').replaceAll('_', ' ').replace(/\b\w/g, (c: string) => c.toUpperCase());
        setStreaming((prev) => ({ ...prev, workingLabel: `Running ${name}...` }));
      });
      es.addEventListener('tool_done', () => {
        setStreaming((prev) => ({ ...prev, workingLabel: 'Working' }));
      });
      es.addEventListener('task_started', (e) => {
        const payload = JSON.parse(e.data);
        setData((prev) => prev ? { ...prev, task: payload.task } : prev);
      });
      ['task_complete', 'task_error', 'state'].forEach((name) => {
        es.addEventListener(name, (e) => {
          const payload = JSON.parse(e.data);
          if (payload.state) {
            setData(payload.state);
            setActiveSlide(0);
          } else {
            refreshState();
          }
          if (name === 'task_complete') addToast('Agent turn complete');
          if (name === 'task_error') addToast(payload.state?.task?.error ?? 'The agent turn failed.', true);
        });
      });
    }

    connect();
    refreshState(false);

    const poll = setInterval(() => {
      if (!esRef.current || esRef.current.readyState === EventSource.CLOSED) refreshState();
    }, 20000);

    return () => {
      esRef.current?.close();
      clearInterval(poll);
    };
  }, [refreshState, addToast]);

  // Scroll stream to bottom when messages change
  useEffect(() => {
    if (streamRef.current) streamRef.current.scrollTop = streamRef.current.scrollHeight;
  }, [data?.messages.length, streaming.active, streaming.text]);

  // Auto-size textarea
  const autoSize = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
  }, []);

  const sendMessage = useCallback(async (text?: string) => {
    const content = text ?? inputText.trim();
    if (!content || data?.task.status === 'running') return;
    const filePaths = uploads.map((f) => `"${f}"`).join(', ');
    const message = filePaths ? `${content}\n\nLocal source files: ${filePaths}` : content;
    setInputText('');
    setUploads([]);
    if (textareaRef.current) { textareaRef.current.style.height = 'auto'; }
    setData((prev) => prev ? {
      ...prev,
      task: { ...prev.task, status: 'running' },
      messages: [...prev.messages, { role: 'user', content: message }],
    } : prev);
    try {
      await apiFetch('/api/chat', { method: 'POST', body: JSON.stringify({ message }) });
    } catch (e) {
      addToast((e as Error).message, true);
    }
  }, [inputText, uploads, data, addToast]);

  const uploadFiles = useCallback(async (fileList: FileList | File[]) => {
    const files = Array.from(fileList);
    if (!files.length) return;
    const body = new FormData();
    files.forEach((f) => body.append('files', f));
    try {
      addToast(`Uploading ${files.length} source${files.length > 1 ? 's' : ''}…`);
      const result = await apiFetch('/api/upload', { method: 'POST', body }) as { files: string[] };
      setUploads((prev) => [...prev, ...result.files]);
      addToast('Sources ready');
    } catch (e) {
      addToast((e as Error).message, true);
    }
  }, [addToast]);

  const openProject = useCallback(async (path: string) => {
    try {
      const result = await apiFetch('/api/projects/open', { method: 'POST', body: JSON.stringify({ path }) }) as AppState;
      setActiveSlide(0);
      setRailOpen(false);
      setData(result);
      addToast('Project opened');
    } catch (e) {
      addToast((e as Error).message, true);
    }
  }, [addToast]);

  const createProject = useCallback(async () => {
    if (!newBrief.trim()) { addToast('Add a presentation brief first.', true); return; }
    setShowNewProject(false);
    try {
      const result = await apiFetch('/api/projects/new', { method: 'POST', body: JSON.stringify({ brief: newBrief }) }) as { task_id: string; state: AppState };
      setActiveSlide(0);
      setStreaming({ active: false, text: '', workingLabel: 'Working' });
      setData(result.state);
      setData((prev) => prev ? { ...prev, messages: [...prev.messages, { role: 'user', content: newBrief }] } : prev);
      setNewBrief('');
    } catch (e) {
      addToast((e as Error).message, true);
    }
  }, [newBrief, addToast]);

  const confirmAndContinue = useCallback(async () => {
    try {
      await apiFetch('/api/confirm', { method: 'POST', body: '{}' });
      setData((prev) => prev ? { ...prev, task: { ...prev.task, status: 'running' } } : prev);
    } catch (e) {
      addToast((e as Error).message, true);
    }
  }, [addToast]);

  const startPreview = useCallback(async () => {
    if (!data?.active_project) { addToast('Open a project before starting preview.', true); return; }
    try {
      const result = await apiFetch('/api/preview/start', { method: 'POST', body: '{}' }) as { url: string };
      await refreshState(false);
      addToast(`Preview ready at ${result.url}`);
    } catch (e) {
      addToast((e as Error).message, true);
    }
  }, [data, refreshState, addToast]);

  const triggerExport = useCallback(() => {
    if (!data?.active_project) { addToast('Open a project before exporting.', true); return; }
    sendMessage('Complete all currently eligible quality and post-processing steps, then export the active project to PPTX. Respect every workflow gate.');
  }, [data, sendMessage, addToast]);

  // ─── Derived state ────────────────────────────────────────────────────────

  const running = data?.task.status === 'running';
  const slides = data?.slides ?? [];
  const clampedSlide = Math.min(activeSlide, Math.max(0, slides.length - 1));
  const hasMessages = (data?.messages.length ?? 0) > 0 || streaming.active;
  const awaitingConfirm =
    data?.workflow.phase === 'awaiting_confirmations' &&
    !data?.workflow.confirmations_approved &&
    !running;
  const previewRunning = data?.preview?.running && data?.preview?.url;
  const phases = data?.phase_order ?? [];
  const currentPhaseIdx = phases.indexOf(data?.workflow.phase ?? '');

  // ─── Render ───────────────────────────────────────────────────────────────

  return (
    <>
      <div className="ambient ambient-a" />
      <div className="ambient ambient-b" />
      <div className="noise" />

      <div className="app-shell">
        {/* ── Rail ── */}
        <aside className={`rail${railOpen ? ' open' : ''}`}>
          <div className="brand">
            <div className="brand-mark">
              <svg viewBox="0 0 48 32" aria-hidden="true">
                <rect x="2" y="2" width="44" height="28" rx="4" />
                <path d="M10 11h15M10 17h26M10 23h19" />
              </svg>
            </div>
            <div>
              <strong>PPT Master</strong>
              <span>Studio</span>
            </div>
          </div>

          <button className="primary-action" onClick={() => setShowNewProject(true)}>
            <span className="plus">+</span>
            New presentation
          </button>

          <div className="rail-section">
            <div className="section-label">
              <span>Projects</span>
              <button className="icon-button" title="Refresh" onClick={() => refreshState(false)}>↻</button>
            </div>
            <div className="project-list">
              {!data ? (
                <>
                  <div className="skeleton project-skeleton" />
                  <div className="skeleton project-skeleton" />
                </>
              ) : data.active_draft ? (
                <>
                  <div className="project-item active draft-item">
                    <strong>{data.active_label || 'New presentation'}</strong>
                    <span><i>Draft</i><i>Working</i></span>
                  </div>
                  {data.projects.map((p) => (
                    <button
                      key={p.path}
                      className={`project-item${p.path === data.active_project ? ' active' : ''}`}
                      onClick={() => openProject(p.path)}
                    >
                      <strong>{cleanProjectName(p.name)}</strong>
                      <span>
                        <i>{data.phase_labels[p.phase] ?? p.phase}</i>
                        <i>{p.slides} slides</i>
                      </span>
                    </button>
                  ))}
                </>
              ) : data.projects.length === 0 ? (
                <div className="project-item">
                  <strong>No projects yet</strong>
                  <span><i>Start a new presentation</i></span>
                </div>
              ) : (
                data.projects.map((p) => (
                  <button
                    key={p.path}
                    className={`project-item${p.path === data.active_project ? ' active' : ''}`}
                    onClick={() => openProject(p.path)}
                  >
                    <strong>{cleanProjectName(p.name)}</strong>
                    <span>
                      <i>{data.phase_labels[p.phase] ?? p.phase}</i>
                      <i>{p.slides} slides</i>
                    </span>
                  </button>
                ))
              )}
            </div>
          </div>

          <div className="rail-footer">
            <div className="connection-card">
              <span className={`status-dot${connected ? ' online' : ' error'}`} />
              <div>
                <strong>{connected ? 'Studio online' : 'Disconnected'}</strong>
                <span>{data?.model ?? 'Novita AI'}</span>
              </div>
            </div>
            <button className="rail-link" onClick={() => setShowSettings(true)}>
              <span>⌘</span>
              Runtime settings
            </button>
          </div>
        </aside>

        {/* ── Workspace ── */}
        <main className="workspace">
          <header className="topbar">
            <button className="mobile-menu" aria-label="Open projects" onClick={() => setRailOpen((o) => !o)}>☰</button>
            <div className="project-heading">
              <div className="eyebrow">{data?.active_label ? 'ACTIVE PROJECT' : 'LOCAL WORKSPACE'}</div>
              <h1>{data?.active_label || 'Create something worth presenting'}</h1>
            </div>
            <div className="topbar-actions">
              <div className="mode-pill">
                <span className="mode-light" />
                <span>{imageLabels(data?.image_mode ?? 'disabled')}</span>
              </div>
              <button className="secondary-button" onClick={startPreview}>Open preview</button>
              <button className="export-button" onClick={triggerExport}>Export deck</button>
            </div>
          </header>

          <section className="stage-grid">
            {/* ── Conversation Panel ── */}
            <section className="conversation-panel panel">
              <div className="conversation-head">
                <div>
                  <span className="kicker">CREATIVE DIRECTION</span>
                  <h2>Build with the agent</h2>
                </div>
                {running && (
                  <div className="thinking-chip">
                    <span /><span /><span />
                    {streaming.workingLabel}
                  </div>
                )}
              </div>

              {/* Welcome state */}
              {!hasMessages && (
                <div className="welcome">
                  <div className="welcome-orbit">
                    <div className="orbit-line" />
                    <div className="orbit-line orbit-two" />
                    <div className="welcome-glyph">P</div>
                  </div>
                  <span className="kicker">FROM SOURCE TO SLIDES</span>
                  <h2>Turn your thinking into an editable deck.</h2>
                  <p>Drop in a document or describe the story. The studio handles strategy, structure, visual construction, quality checks, and export.</p>
                  <div className="starter-grid">
                    {[
                      { icon: '↗', title: 'Executive strategy', desc: 'Sharp narrative, clear decisions', prompt: 'Create an executive strategy deck from my uploaded source.' },
                      { icon: '✦', title: 'Product launch', desc: 'Bold visuals, memorable moments', prompt: 'Create a visually rich product launch presentation from my brief.' },
                      { icon: '◎', title: 'Research readout', desc: 'Evidence first, elegant clarity', prompt: 'Create a concise research presentation from my uploaded material.' },
                    ].map((s) => (
                      <button key={s.title} className="starter-card" onClick={() => { setInputText(s.prompt); textareaRef.current?.focus(); }}>
                        <span className="starter-icon">{s.icon}</span>
                        <strong>{s.title}</strong>
                        <small>{s.desc}</small>
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {/* Message stream */}
              <div ref={streamRef} className={`message-stream${hasMessages ? ' active' : ''}`}>
                {data?.messages.map((msg, i) => (
                  <article key={i} className={`message ${msg.role}`}>
                    <div className="message-avatar">{msg.role === 'assistant' ? 'P' : 'You'}</div>
                    <div className="message-body">
                      <div className="message-meta">{msg.role === 'assistant' ? 'PPT Master' : 'You'}</div>
                      <div className="message-content" dangerouslySetInnerHTML={{ __html: renderMarkdown(msg.content) }} />
                    </div>
                  </article>
                ))}
                {streaming.active && (
                  <article className="message assistant streaming">
                    <div className="message-avatar">P</div>
                    <div className="message-body">
                      <div className="message-meta">PPT Master</div>
                      <div className="message-content">
                        {streaming.text}
                        <span className="stream-cursor" />
                      </div>
                    </div>
                  </article>
                )}
              </div>

              {/* Confirm card */}
              {awaitingConfirm && (
                <div className="confirm-card">
                  <div className="confirm-art"><span /><span /></div>
                  <div>
                    <span className="kicker">STRATEGY READY</span>
                    <strong>Review the Eight Confirmations</strong>
                    <p>Approve the direction to continue into slide production.</p>
                  </div>
                  <button onClick={confirmAndContinue}>Confirm direction</button>
                </div>
              )}

              {/* Composer */}
              <div className="composer-wrap">
                {uploads.length > 0 && (
                  <div className="upload-tray">
                    {uploads.map((path, i) => (
                      <div key={i} className="upload-pill">
                        <b>↥</b>
                        <span>{path.split(/[\\/]/).pop()}</span>
                      </div>
                    ))}
                  </div>
                )}
                <div
                  className={`composer${dragging ? ' dragging' : ''}`}
                  onDragOver={(e: DragEvent) => { e.preventDefault(); setDragging(true); }}
                  onDragLeave={() => setDragging(false)}
                  onDrop={(e: DragEvent) => { e.preventDefault(); setDragging(false); uploadFiles(e.dataTransfer.files); }}
                >
                  <label className="attach-button" title="Attach local files">
                    <span>+</span>
                    <input
                      type="file"
                      multiple
                      style={{ display: 'none' }}
                      onChange={(e: ChangeEvent<HTMLInputElement>) => { if (e.target.files) uploadFiles(e.target.files); }}
                    />
                  </label>
                  <textarea
                    ref={textareaRef}
                    rows={1}
                    value={inputText}
                    placeholder="Describe the presentation or attach source material…"
                    onChange={(e) => { setInputText(e.target.value); autoSize(); }}
                    onKeyDown={(e: KeyboardEvent<HTMLTextAreaElement>) => {
                      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
                    }}
                  />
                  <button className="send-button" disabled={running} aria-label="Send" onClick={() => sendMessage()}>
                    <svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6" /></svg>
                  </button>
                </div>
                <div className="composer-meta">
                  <span>Local files only · no web search</span>
                  <span><kbd>Enter</kbd> send · <kbd>Shift</kbd> + <kbd>Enter</kbd> newline</span>
                </div>
              </div>
            </section>

            {/* ── Production Panel ── */}
            <aside className="production-panel panel">
              <div className="production-head">
                <div>
                  <span className="kicker">PRODUCTION</span>
                  <h2>Deck progress</h2>
                </div>
                <span className="phase-badge">{data ? (data.phase_labels[data.workflow.phase] ?? data.workflow.phase) : 'Source'}</span>
              </div>

              {/* Phase track */}
              <div className="phase-track">
                {phases.map((phase, i) => {
                  const cls = i < currentPhaseIdx ? 'complete' : i === currentPhaseIdx ? 'active' : '';
                  return (
                    <div key={phase} className={`phase-step ${cls}`}>
                      <div className="phase-line" />
                      <span>{data?.phase_labels[phase] ?? phase}</span>
                    </div>
                  );
                })}
              </div>

              {/* Preview shell */}
              <div className="preview-shell">
                {previewRunning ? (
                  <iframe className="preview-frame" src={data!.preview.url} title="Live slide editor" />
                ) : slides.length > 0 ? (
                  <div className="slide-canvas">
                    <img
                      src={`${slides[clampedSlide].url}?v=${slides[clampedSlide].modified}`}
                      alt={slides[clampedSlide].name}
                    />
                  </div>
                ) : (
                  <div className="preview-empty">
                    <div className="preview-stack"><span /><span /><span /></div>
                    <strong>Your deck will appear here</strong>
                    <p>Slides arrive one by one as the Executor builds them.</p>
                  </div>
                )}
              </div>

              {/* Slide toolbar */}
              {slides.length > 0 && (
                <div className="slide-toolbar">
                  <button onClick={() => setActiveSlide((s) => Math.max(0, s - 1))}>←</button>
                  <span>{clampedSlide + 1} / {slides.length}</span>
                  <button onClick={() => setActiveSlide((s) => Math.min(slides.length - 1, s + 1))}>→</button>
                  <button className="toolbar-wide" onClick={startPreview}>Edit in live preview</button>
                </div>
              )}

              {/* Filmstrip */}
              {slides.length > 0 && (
                <div className="filmstrip">
                  {slides.map((slide, i) => (
                    <button key={slide.name} className={`thumb${i === clampedSlide ? ' active' : ''}`} onClick={() => setActiveSlide(i)}>
                      <img src={`${slide.url}?v=${slide.modified}`} alt={slide.name} />
                    </button>
                  ))}
                </div>
              )}

              {/* Stats */}
              <div className="production-stats">
                <div><span>Slides</span><strong>{slides.length}</strong></div>
                <div><span>Tokens</span><strong>{compactNumber(data?.usage.total_tokens ?? 0)}</strong></div>
                <div>
                  <span>Images</span>
                  <strong>{data?.image_mode === 'disabled' ? 'Off' : data?.image_mode === 'prompts-only' ? 'Prompt' : 'On'}</strong>
                </div>
              </div>

              {/* Export list */}
              {(data?.exports.length ?? 0) > 0 && (
                <div className="export-list">
                  {data!.exports.slice(0, 3).map((file) => (
                    <a key={file.name} className="export-card" href={file.url}>
                      <span className="export-icon">{file.name.toLowerCase().endsWith('.pptx') ? 'P' : '↓'}</span>
                      <span><strong>{file.name}</strong><span>{formatBytes(file.size)}</span></span>
                      <span className="download">Download</span>
                    </a>
                  ))}
                </div>
              )}
            </aside>
          </section>
        </main>
      </div>

      {/* ── New Project Modal ── */}
      {showNewProject && (
        <div className="modal-backdrop" onClick={(e) => { if (e.target === e.currentTarget) setShowNewProject(false); }}>
          <div className="modal">
            <button className="modal-close" onClick={() => setShowNewProject(false)}>×</button>
            <span className="kicker">NEW PRESENTATION</span>
            <h2>What are we making?</h2>
            <p>Give the agent enough substance to work without web research. You can attach source files after creation.</p>
            <textarea
              rows={6}
              value={newBrief}
              onChange={(e) => setNewBrief(e.target.value)}
              placeholder="Example: A 12-slide investor update for our Q2 board meeting. Audience is senior leadership. Focus on growth, retention, and the next two strategic bets…"
            />
            <div className="modal-actions">
              <button className="secondary-button" onClick={() => setShowNewProject(false)}>Cancel</button>
              <button className="primary-action" onClick={createProject}>Create presentation</button>
            </div>
          </div>
        </div>
      )}

      {/* ── Settings Modal ── */}
      {showSettings && (
        <div className="modal-backdrop" onClick={(e) => { if (e.target === e.currentTarget) setShowSettings(false); }}>
          <div className="modal settings-modal">
            <button className="modal-close" onClick={() => setShowSettings(false)}>×</button>
            <span className="kicker">RUNTIME</span>
            <h2>Local agent settings</h2>
            <div className="setting-row"><span>Model</span><strong>{data?.model ?? '—'}</strong></div>
            <div className="setting-row"><span>Image generation</span><strong>{data?.image_mode ?? '—'}</strong></div>
            <div className="setting-row"><span>Web search</span><strong>Unavailable</strong></div>
            <div className="setting-row"><span>API key</span><strong>{data?.key_configured ? 'Configured' : 'Missing'}</strong></div>
            <p className="settings-note">Settings come from the repository&apos;s ignored <code>.env</code> file. Restart the studio after changing them.</p>
          </div>
        </div>
      )}

      {/* ── Toasts ── */}
      <div className="toast-region">
        {toasts.map((t) => (
          <div key={t.id} className={`toast${t.error ? ' error' : ''}`}>{t.message}</div>
        ))}
      </div>
    </>
  );
}
