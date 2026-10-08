# Pieni: a small educational coding agent

## Goal

A readable agent that demonstrates the basic cycle: receive a prompt, call a
model, execute requested tools, and continue until the model answers. It should
be easy to run, understand, fork, and extend; it is not a production agent platform.

All agent code lives in `pieni.py`, with a small Bash launcher named `pieni` and
tests in separate files. The original ~750-line target was exceeded on purpose as
features were added; the guideline is now **well under 2,000 lines** (about 1,900
today, so little room is left). Dependencies: the standard library plus three direct
packages in
`requirements.txt`: `openai`, `openrouter`, and `rich` (terminal display only).

## Status: the planned scope is implemented

The planned scope has shipped and is covered by the offline test suite; the README
and `docs/how-pieni-works.md` describe the behavior. This file now only records the
decisions that still constrain changes.

### Implemented

- **Loop and tools:** one agent mode for interactive and headless use; `read`,
  `write`, `edit`, `bash` with validated arguments, bounded output (5,000 lines,
  65,536 characters), a 500-round task cap (`MAX_STEPS`), tool-call IDs preserved,
  and no re-execution of historical calls on resume.
- **Instructions:** a short system prompt plus `AGENTS.md` from the starting
  workspace root only (no recursive discovery).
- **Skills:** Agent Skills folders (`SKILL.md`) one level under `~/.agents/skills`
  and `.agents/skills`, project overriding user. Only name, description, and path
  enter the system prompt; the model loads a skill with `read` (which may read the
  user skills directory without approval). `/skills` lists them. Bundled example:
  `.agents/skills/web-fetch`.
- **MCP client:** tools only, over stdio and Streamable HTTP, written with the standard
  library. Servers come from `~/.pieni/mcp.json`, `[mcp.NAME]` INI sections,
  `.mcp.json` in the workspace, and repeatable `--mcp NAME=COMMAND|URL`, later sources
  winning; `--no-mcp` disables them; `${VAR}` is expanded in `env` and `headers`.
  Tools appear to the model as `server__tool` and the server validates arguments.
  A server that fails to start is skipped with a warning. `/mcp` lists servers.
  Bundled example: `examples/mcp_fetch_server.py` (stdio or `--http`).
- **Configuration:** `configparser` INI, user file then launch-directory file then
  CLI; keys `provider`, `model`, `permissions`, `streaming`, `reasoning`. API keys
  stay in environment variables.
- **Providers and CLI:** OpenAI (Responses), DeepSeek and custom base URLs (Chat
  Completions through `openai`), OpenRouter (its SDK); `-r/--run` headless task,
  `-p/--prompt` standalone reply, `--permissions`, `--streaming/--no-streaming`,
  `--reasoning`. Streaming by default, with complete tool calls assembled before
  execution and no partial replies saved.
- **Interaction:** tool status lines with timing, per-task token/context/time
  summary, a labeled thinking trace, Ctrl+C handling, SQLite save/resume in
  `.pieni/pieni.db`, and the commands `/compact`, `/compact all`, `/permissions`,
  `/reasoning`, `/skills`, `/mcp`, `!COMMAND`, `/help`, `/quit`, `/exit`.
- **Terminal UI:** on an interactive terminal, `rich` renders model replies as
  Markdown (live while streaming) and colors status lines; other text is never
  Markdown. Up/Down recall earlier prompts through `readline` where available.
  Piped output, `-r`, and `-p` stay plain.
- **Packaging and tests:** `scripts/install.sh` (Ubuntu installer), offline
  `unittest` suites with mocked SDKs, a loopback-server check of the real SDK wire
  formats, and opt-in live smoke tests.

### Permissions

Constraints that still apply:

- Default `auto` runs shell commands unless the destructive-command guard (DCG)
  flags them; flagged commands need approval. DCG stays a small regex function,
  not a shell or SQL parser, and not a security boundary or OS sandbox.
- File tools allow the starting workspace and the temp directory (resolving `..`
  and symlinks); other paths need approval. The only exception is read access to
  `~/.agents/skills`.
- `yolo` skips approval and guard checks, not argument validation or timeouts.
- Headless mode denies anything needing approval and reports the denial to the
  model. Approval covers only the proposed action.
- MCP is the exception: servers the user configured are trusted, and their tool
  calls are neither checked nor prompted. A project `.mcp.json` therefore runs
  commands at startup; this is documented, and `--no-mcp` is the guard.

### Providers and CLI

Constraints that still apply: keep a thin adapter that normalizes only messages,
tool calls/results, final text, and usage; no provider framework, hard-coded model
catalog, or invented capabilities. API keys are never command-line arguments.
Incomplete streams fail clearly; there is no silent non-streaming retry.

### Compaction

Manual only. The system prompt, workspace instructions (including the skill
catalog), and tool definitions are kept; bulky tool output is shortened to 2,000
characters before summarizing; the active context is replaced in one SQLite
transaction, and a failed compaction leaves it unchanged.

### Verification notes

- Local Qwen smoke tests cover streaming and buffered replies, all four tools,
  Unicode files, and resume. Hosted streaming and hosted reasoning behavior remain
  unverified after later refactors.
- The Rich UI, skills, and MCP are covered by offline tests (MCP over loopback
  stdio and HTTP only); they have not been exercised in a long real-model session
  or against third-party MCP servers.
- The 14 `bash`-dependent tests fail on Windows because they expect a POSIX shell.

## Deferred, not promised

- Multiline editing, key bindings beyond history, and ESC handling; saving prompt
  history between sessions.
- Live streaming of reasoning, tokenizer-based accounting, model context-window
  discovery, and automatic compaction.
- Runtime provider/model switching.
- Web search as a built-in tool (the `web-fetch` skill covers simple downloads).
- MCP resources, prompts, sampling, OAuth, `listChanged` refresh, reloading servers
  mid-session, and approval prompts for MCP tools.
- Plugins, multiple agent modes, and a true OS sandbox.
- Beyond basic skills: running skill scripts as a managed step, `allowed-tools`,
  `.claude/skills` and `.github/skills`, a `/skill NAME` command, and reloading
  skills during a session.

Revisit a deferred feature only after its educational value justifies the added
code and the file stays well under 2,000 lines. There is no committed second
release scope.
