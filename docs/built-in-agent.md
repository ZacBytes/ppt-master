# Built-in OpenRouter Agent

PPT Master includes a local agent runtime for users who do not want to install
Claude Code, Codex, Cursor, or another external coding agent. The runtime uses
OpenRouter for language-model calls and executes PPT Master tools locally.

## 1. Capabilities

| Capability | Built-in behavior |
|---|---|
| Presentation workflow | Enforces the serial `SKILL.md` pipeline |
| Model | Defaults to `google/gemma-4-31b-it` |
| Source files | Local PDF, DOCX, XLSX, CSV, PPTX, Markdown, images, and pasted text |
| Web search | Unavailable |
| URL fetching | Unavailable |
| Image search | Unavailable |
| Text-to-image | Optional; disabled by default |
| Live preview | Starts the local SVG editor |
| Visual inspection | Sends local images to the configured multimodal model |
| Resume | Stores redacted state under the project `.agent/` directory |

The runtime never exposes a general shell tool. It provides repository-confined
file operations and an allowlist of PPT Master scripts.

## 2. Configuration

Create an ignored `.env` file in the repository root:

```dotenv
OPENROUTER_API_KEY=your-new-openrouter-key
PPT_MASTER_AGENT_MODEL=google/gemma-4-31b-it
PPT_MASTER_IMAGE_GENERATION=disabled
```

Do not reuse a key that has been pasted into chat, an issue, or a committed
file. Revoke that key and create a replacement first.

Image modes:

| Mode | Behavior |
|---|---|
| `disabled` | Use editable SVG shapes, charts, diagrams, icons, typography, and local assets |
| `prompts-only` | Write reusable image prompts without making image-generation calls |
| `enabled` | Permit `image_gen.py`; configure its image backend separately |

## 3. Start

Browser studio:

```bash
python skills/ppt-master/scripts/ppt_agent_web.py
```

The studio opens at `http://127.0.0.1:5080` and provides source uploads,
project switching, chat, confirmation controls, workflow progress, slide
preview, live-editor embedding, and export downloads.

Terminal interface:

From the repository root:

```bash
python skills/ppt-master/scripts/ppt_agent.py
```

Resume an existing project:

```bash
python skills/ppt-master/scripts/ppt_agent.py \
  --project projects/<project_name>
```

Run one non-interactive turn:

```bash
python skills/ppt-master/scripts/ppt_agent.py \
  --message "Create a presentation from sources/report.pdf"
```

Useful commands:

| Command | Action |
|---|---|
| `/status` | Show project, phase, model, image mode, and token usage |
| `/open <project>` | Open an existing project and its saved agent state |
| `/new <request>` | Start a new presentation request |
| `/resume` | Continue from the current workflow phase |
| `/preview` | Start the local live preview |
| `/export` | Continue eligible quality and export steps |
| `/clear` | Clear conversation history while preserving workflow state |
| `/help` | Show local runtime guidance |
| `/quit` | Exit cleanly |

## 4. No-Web Workflow

The built-in agent cannot search, browse, fetch URLs, or download stock images.

Provide one of:

- Local source files
- Pasted source text
- A substantive brief containing the facts the deck should use

A URL by itself is treated as reference text. Download or paste its contents
before starting the agent.

## 5. Workflow Enforcement

The runtime enforces:

- One-step phase transitions
- Explicit approval before leaving the Eight Confirmations
- Sequential SVG page numbering
- A fresh `spec_lock.md` read before every SVG page write
- No script-generated SVG batches
- Ordered `total_md_split.py`, `finalize_svg.py`, and `svg_to_pptx.py`
- Image-generation mode checks
- Workspace path confinement and secret redaction

Session files contain no API keys. They are stored under:

```text
projects/<project>/.agent/
```

Before a project is selected, temporary session state uses:

```text
.ppt-master-agent/
```

Both locations are ignored by Git.

## 6. Limitations

- Output quality still depends on the selected model.
- The built-in runtime does not research missing facts.
- Cloud narration and enabled image generation may use their own provider
  network APIs; this is separate from web search.
- A process restarted after an interruption can resume workflow state, but a
  previously started live-preview process may need to be started again.
