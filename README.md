# pieni

**v0.20** — written by Petri Kuittinen, 2026.

A tiny AI coding agent written in Python: one file of about 1900 lines of code
(`pieni.py`) plus a small Bash launcher (`pieni`). It is meant for learning how
agents work — read it, run it, fork it, change it. Despite its small size, pieni
has a best-effort destructive command guard (DCG), a Rich terminal UI with
Markdown rendering and prompt history, basic Agent Skills support, and a minimal
MCP client for extra tools. It supports hundreds
of models and can be extended. It can even generate you games or run web browser.
Small, but works.

"pieni" is Finnish and means "small".

Read [How Pieni works—and how AI harnesses and agents work in general](docs/how-pieni-works.md)
for a walkthrough of the loop, tools, permissions, context, skills, MCP, the terminal UI,
and providers.

New to this? The beginner guides have copy-and-paste steps for Ubuntu 24.04 and
Windows 11 PowerShell: [How to use skills](docs/how-to-use-skills.md) and
[How to use MCP servers](docs/how-to-use-mcps.md).

## Status

Pieni supports these providers (set the API key as an environment variable):

- OpenAI (`OPENAI_API_KEY`) — Responses API
- DeepSeek (`DEEPSEEK_API_KEY`) — Chat Completions
- OpenRouter (`OPENROUTER_API_KEY`) — openrouter SDK
- Any custom base URL (`OPENAI_API_KEY` when the server needs one), including
  local servers such as llama.cpp / llama-server, ollama, LM Studio, vLLM —
  Chat Completions

![pieni AI agent](pieni3.jpg "pieni AI agent")

## Install

On Ubuntu Linux, the installer does everything: it checks the launcher, installs
the Python dependencies if they are missing, and adds the `pieni` command to the
PATH as a symlink to this checkout. It needs no sudo.

```console
./scripts/install.sh
```

What it does, step by step:

1. Checks that `pieni`, `pieni.py`, and `requirements.txt` are in this checkout,
   and that the launcher is executable and free of shell syntax errors.
2. Checks `python3` (3.8 or newer).
3. Checks whether `openai`, `openrouter`, and `rich` are importable. If not, it creates
   `.venv` in this checkout and runs `pip install -r requirements.txt` there.
4. Symlinks `~/.local/bin/pieni` to this checkout's launcher and runs
   `pieni --help` to prove the installed command works.
5. Warns if `~/.local/bin` is not on your PATH, and prints how to uninstall.

Options: `--prefix DIR` installs into `DIR/bin` instead (`--prefix /usr/local`
for a system-wide command, which needs write access there), `--skip-deps` leaves
the Python packages alone, and `--dry-run` prints the actions without changing
anything. Uninstall with `rm ~/.local/bin/pieni`.

Because the command is a symlink to this checkout, `git pull` updates it, and
moving or deleting the checkout breaks it.

### Manual install

If you prefer to do it by hand, create a virtual environment and install the
direct dependencies (`openai`, `openrouter`, and `rich` for the terminal UI;
everything else is the standard library):

```console
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

On Windows use `.venv\Scripts\pip` instead of `.venv/bin/pip`. The `./pieni`
launcher uses `.venv/bin/python` automatically when that file exists, so you do
not have to activate the environment:

```console
./pieni openai -m "gpt-6-luna"
```

## Configuration

Pieni reads INI settings with `configparser`, in this order:

1. `~/.pieni/pieni.ini` — your settings.
2. `pieni.ini` in the directory where you launch Pieni — overrides your settings.
3. Command-line arguments — override both files.

Example:

```ini
[pieni]
provider = deepseek
model = deepseek-chat
permissions = auto
streaming = true
reasoning = default
```

Missing files are fine. Malformed INI files, unknown settings, or invalid values
are reported with a clear error. API keys belong in environment variables, never
in these files.

Streaming defaults to `true` for every model and provider, including custom/local
endpoints. Set `streaming = false` to wait for complete replies. Standard INI
booleans (`true/false`, `yes/no`, `on/off`, `1/0`) are accepted, case-insensitively.
`--streaming` and `--no-streaming` override the file setting; when neither is given,
the configured value is preserved.

`reasoning` accepts `default`, `none`, `minimal`, `low`, `medium`, `high`, `xhigh`,
or `max`. `default` leaves effort to the provider; `none` requests no reasoning.
Use `--reasoning high` to override the INI setting, or `--reasoning default` to
restore the provider default. Effort applies to interactive tasks, headless tasks,
standalone prompts, and compaction, with streaming enabled or disabled. Models
support different effort levels; unsupported settings produce a provider error
without retrying with another setting.

## CLI usage

```console
./pieni                                       # no arguments: banner and help, then exit
./pieni openai -m "gpt-6-luna"                # interactive session
./pieni deepseek -m "deepseek-chat"
./pieni openrouter -m "vendor/model"
./pieni http://localhost:30000 -m "qwen3-30b" # local server
./pieni openai -m "gpt-6-luna" --permissions yolo
./pieni openai -m "MODEL" --no-streaming       # wait for a complete reply
./pieni openai -m "MODEL" --reasoning high     # set reasoning effort
./pieni deepseek -m "MODEL" --streaming       # override streaming = false
./pieni openai -m "MODEL" --mcp fetch="python examples/mcp_fetch_server.py"  # add an MCP server
./pieni openai -m "gpt-6-luna" -p "What is the capital of Finland?"
```

Started with no arguments, Pieni prints its banner, usage, and command list, then
exits with status 0. Give it at least a provider, or set `provider` and `model` in
`pieni.ini` and pass any other argument.

`-p/--prompt "question"` sends one standalone prompt and exits after the reply.
It sends only your prompt, with tools disabled, without reading project
instructions or loading/saving a session. Output is the reply text; the configured
streaming setting and its CLI overrides apply. `-p` and `-r` are mutually exclusive.

## Interactive mode

An interactive session greets you with the version banner and the startup state:

```console
./pieni deepseek -m "deepseek-chat"
Pieni agent v0.20 by Petri Kuittinen
resumed a session with 6 message(s) (deepseek/deepseek-chat)
permissions: auto
Type a task, or /help for commands. Ctrl+C interrupts, Ctrl+D exits.
pieni>
```

Headless runs (`-r`) print no banner, so their output stays script-friendly.

### Rich terminal UI

On an interactive terminal Pieni uses [Rich](https://rich.readthedocs.io/) for display:

- Model replies are rendered as Markdown, with headings, lists, bold text, and
  syntax-highlighted code blocks. While a reply streams, the view updates live.
- Everything that is not a model reply stays plain text, never Markdown: tool lines,
  errors, `!command` output, `/help`, and the token summary. They are only colored
  (green `ok`, red errors, dim token and thinking lines, cyan startup lines).
- Up and Down recall earlier prompts, as in a Bash shell, through Python's
  `readline` module. Windows consoles provide their own history. History is kept for
  the running session only.

When output is piped or redirected, and for `-r` and `-p`, Pieni prints plain text.
Some shell windows, such as Git Bash (mintty), connect Python through a pipe and so
look "redirected"; use PowerShell, cmd, or Windows Terminal for the colored UI there.

## Headless mode

```console
./pieni openai -m "gpt-6-luna" -r "Explain this repository in five bullets."
```

`-r/--run` runs one task, prints the answer and the task summary, and exits. The
exit code is nonzero when the task could not finish, and actions that would need
approval are denied instead of waiting for input.

Headless mode is the path verified in v0.13 across all providers:

```console
./pieni openai -m "gpt-6-luna" -r "Summarize README.md."
./pieni deepseek -m "deepseek-chat" -r "Summarize README.md."
./pieni openrouter -m "vendor/model" -r "Summarize README.md."
./pieni http://localhost:30000 -m "qwen3-30b" -r "Summarize README.md."
```

Model names are examples only; use whatever your provider or local server offers.

## Commands

The same list is printed when Pieni is started with no arguments.

```console
/compact        replace older context with a model-written summary
/compact all    clear the context and start a fresh session
/permissions    show permissions; /permissions auto|yolo changes them
/reasoning      show effort; /reasoning EFFORT changes it (default resets it)
/skills         list the skills loaded at startup
/mcp            list the connected MCP servers and their tools
!COMMAND        run a local shell command, without adding it to the conversation
/help           concise help
/quit, /exit    leave
```

Ctrl+C interrupts the current task and returns to the prompt; Ctrl+C or Ctrl+D at
an idle prompt exits. Completed actions are not rolled back.

`/reasoning high` changes effort for subsequent requests. `/reasoning default`
restores the provider default. These changes apply only to the running process;
they do not edit INI files or become session settings.

Prefix every direct shell command with `!`:

```console
pieni> !ls -lafG
pieni> !mv old.text new_name.txt
pieni> !cat /tmp/example.c
```

Pieni prints captured stdout and stderr after the command finishes, followed by
`!COMMAND -> ok, 12 ms` or `!COMMAND -> error (exit code 1), 4 ms`. Empty output is
marked `(no output)`; long output uses the existing 65,536-character limit and
truncation marker. Commands use the workspace directory, current permissions,
and a 60-second timeout. Each runs in a separate shell, so `cd` does not carry
over. Ctrl+C interrupts and returns to the prompt without retrying the command.
Commands and output stay in the terminal: they are not sent to the model or
saved in the conversation. Input without `!` is an agent prompt.

## Tools

The model gets four tools, and Pieni validates their arguments, bounds their
output, and reports failures back to the model:

- `read` — read a UTF-8 file, optionally a line range.
- `write` — create or replace a UTF-8 file.
- `edit` — replace one exact text match; fails when the text is missing or
  ambiguous.
- `bash` — run a shell command in the workspace with a timeout.

Tools offered by [MCP servers](#mcp-servers) are added to this list.

A task gets at most **500 model rounds**. One round may ask for several tool calls
at once, so this budgets the conversation with the model, not the number of
individual tool calls. When the rounds run out, Pieni prints the task summary and
keeps the context, so you can ask it to continue.

## Skills

A skill is a folder with a `SKILL.md` file, in the shared
[Agent Skills](https://agentskills.io/specification) format: reusable instructions
that the model loads only when a task needs them. Pieni looks in
`~/.agents/skills/` and `.agents/skills/` in the workspace; a project skill
overrides a user skill with the same name.

```markdown
---
name: deploy
description: Deploy the site to staging. Use when the user asks to deploy or release.
---
Steps the model should follow...
```

At startup only each skill's name, description, and path go into the system prompt.
When a task matches, the model reads the full `SKILL.md` with the `read` tool.
`name` must be lowercase letters, digits, and hyphens, and match the folder name;
`description` is 1-1024 characters. Other fields are ignored, and invalid skills
are skipped with a warning on stderr. Skills are loaded once at startup; `/skills`
lists them. The repository includes an example, `.agents/skills/web-fetch`, which
teaches the model to download pages with `curl` or `wget`, sending a browser
`User-Agent` because some sites reject requests without one. A skill is trusted
instruction text: read skills from other people before using them. The
[guide](docs/how-pieni-works.md#skills-instructions-loaded-on-demand) explains how
skills work in agents in general. For step-by-step instructions, with examples you can
copy and paste on Ubuntu and Windows, see [How to use skills](docs/how-to-use-skills.md).

## MCP servers

[MCP](https://modelcontextprotocol.io/) (Model Context Protocol) lets a separate
program offer tools to any agent. At startup Pieni connects to the servers you
configure, lists their tools, and offers them to the model next to its four built-in
tools. Both standard transports work: **stdio** (Pieni starts the server as a child
process) and **Streamable HTTP** (a `url`). Only MCP *tools* are supported, not
resources, prompts, sampling, or OAuth.

Configure servers in any of these places; when a name appears more than once, the
later source wins:

1. `~/.pieni/mcp.json`
2. `[mcp.NAME]` sections in `~/.pieni/pieni.ini`
3. `.mcp.json` in the workspace
4. `[mcp.NAME]` sections in `pieni.ini` in the workspace
5. `--mcp NAME=COMMAND` or `--mcp NAME=URL` on the command line (repeatable)

The JSON files use the same `mcpServers` shape as other agents:

```json
{"mcpServers": {
  "fetch": {"command": "python", "args": ["examples/mcp_fetch_server.py"]},
  "remote": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${EXAMPLE_TOKEN}"}}
}}
```

```ini
[mcp.fetch]
command = python examples/mcp_fetch_server.py

[mcp.remote]
url = https://example.com/mcp
```

```console
./pieni openai -m "MODEL" --mcp fetch="python examples/mcp_fetch_server.py"
```

JSON entries may also set `env`, and `${VAR}` in `env` and `headers` values is
expanded from the environment, so secrets stay out of the file. INI sections take only
`command` or `url`. The model sees each tool as `NAME__TOOL` (for example
`fetch__fetch`). `/mcp` lists the connected servers and their tools, and `--no-mcp`
starts none, whatever the configuration says. Servers are connected once at startup;
one that fails to start is skipped with a warning on stderr, and a stdio server's own
stderr output is discarded.

**The example server.** `examples/mcp_fetch_server.py` uses only the standard library
and has one tool, `fetch(url, max_chars)`, which downloads a page, sends a browser
`User-Agent`, and returns plain text. It speaks stdio by default, or HTTP with
`--http PORT`. It refuses non-HTTP URLs and loopback, private, and link-local addresses
(checked on every redirect) unless started with `--allow-private`, so a model cannot
use it to probe your network. It is an example, not a hardened proxy.

**Trust.** Servers you configure are trusted: their tool calls run without approval
prompts, also in headless `-r` mode, and neither the file-path checks nor the
destructive-command guard apply to what a server does. A `.mcp.json` in a repository
you have just cloned starts its commands the moment you launch Pieni there, so read
it first or pass `--no-mcp`. Tool descriptions and results are untrusted text from
the server, and an HTTP server receives every argument the model sends. The
[guide](docs/how-pieni-works.md#mcp-tools-from-other-programs) explains the protocol
and these risks. For step-by-step instructions, with two free online servers to try and
copy-and-paste commands for Ubuntu and Windows, see
[How to use MCP servers](docs/how-to-use-mcps.md).

## Limits

Every tunable default is a constant near the top of `pieni.py`:

| Constant | Default | Meaning |
| --- | --- | --- |
| `MAX_STEPS` | 500 | model rounds per task |
| `MAX_READ_LINES` | 5,000 | lines returned by one `read`; the rest is marked as truncated |
| `MAX_OUTPUT_CHARS` | 65,536 | characters of one tool result passed back to the model |
| `THINK_TRACE_CHARACTERS` | 120 | characters of a model thinking trace shown |
| `THINK_DOT_INTERVAL` | 1.0 | seconds between progress dots while waiting for the model |
| `BASH_TIMEOUT` | 60 | seconds for one `bash` command (the tool may ask for 1-600) |
| `CONTEXT_WINDOW` | 1,000,000 | window assumed by the context display (display only) |
| `CHARS_PER_TOKEN` | 4 | characters per token in the fallback usage estimate |
| `DEFAULT_PERMISSIONS` | `auto` | permissions when neither file nor CLI sets them |
| `DEFAULT_STREAMING` | `True` | streaming when neither file nor CLI sets it |
| `DB_PATH` | `.pieni/pieni.db` | saved conversations, under the workspace |
| `PROMPT` | `pieni> ` | interactive prompt |
| `VERSION` | `0.20` | shown in the banner |

`MAX_READ_LINES` and `MAX_OUTPUT_CHARS` are independent: a `read` returns at most
5,000 lines, and whatever a tool produces is cut off at 65,536 characters with a
`[truncated N characters]` marker. A single `read` can therefore fill a large part
of the model's context, and several large results in one task add up; the 1,000,000
figure above is only the display assumption.

## Output

Model reply text is streamed as it arrives, in both interactive and headless
mode (as live Markdown on an interactive terminal, plain text otherwise). Pieni assembles full tool arguments before executing any tools and saves
only completed replies. Final answers are not printed twice. Provider token
usage is taken from the completed stream when available, otherwise estimated.

An interrupted or failed stream is closed and reported without automatically
retrying; partially displayed text is not saved as a completed reply. Task usage
includes a labeled estimate for the failed attempt. If an endpoint does not
support streaming, select `--no-streaming` or `streaming = false` explicitly.

With streaming disabled, Pieni prints one `.` per second while waiting on an
interactive terminal, so a slow reply does not look like a hang:

```console
pieni> explain this repository
..
```

When the model reports its thinking, Pieni shows the beginning of it, cut to
`THINK_TRACE_CHARACTERS` (120) characters:

```console
thinking: I should read the file first, then decide whether editing is enough. The task also mentions tests, so I will run them af...
```

Two honest limits on that trace:

- Reasoning effort controls how much a model thinks; it does not request a visible
  reasoning summary. The line appears only when the model returns thinking
  (`reasoning_content` on DeepSeek and compatible servers, `reasoning` on
  OpenRouter, `reasoning` items in the OpenAI Responses API). Otherwise it is skipped.
- Thinking itself is not streamed: the bounded trace appears after the reply
  completes. What you see is the model's own thinking text, never a Pieni summary
  of it. Progress dots are used only when reply streaming is disabled.

The short displayed trace is not saved as a separate message. DeepSeek's complete
`reasoning_content` and OpenRouter's returned reasoning fields and blocks are
saved with assistant messages and sent back for tool continuation and session
resume. These fields can contain sensitive reasoning text, count toward context,
and are omitted from the data sent for compaction. Old sessions without these
fields still load; a provider may reject an older tool conversation that lacks
required reasoning. Use `/compact` or `/compact all` to replace that context.

After every tool call, Pieni prints a status line with the outcome and the elapsed
time in milliseconds:

```console
read(path="pieni.py") -> ok, 12 ms
bash(command="python3 -m unittest test_pieni") -> error: exit code 1, 340 ms
```
After every completed, failed, or interrupted task, it prints token usage, context
usage against an assumed 1,000,000-token window, and elapsed time:

```console
Tokens: ~2,400 | Context: ~12,000 / 1,000,000 (1.2%) | 3.1 seconds
```

Token counts come from the provider when it reports usage and are otherwise a
marked text estimate. The 1,000,000-token context window is a display assumption,
not a claim about the selected model.

## Permissions and safety

- `auto` (default) runs shell commands unless the destructive-command guard (DCG)
  matches an obviously destructive pattern (`rm -rf`, `mkfs`, `dd of=…`,
  `shutdown`, `git push --force`, `DROP TABLE`, …). Flagged commands need your
  approval.
- File tools work inside the launch directory and the system temp directory;
  paths outside them need approval, with `..` and symlinks resolved first. The one
  exception is reading `~/.agents/skills`, so user-level skills can be loaded.
- `yolo` skips approval and guard checks, but not argument validation or timeouts.
- Tools from MCP servers you configured run without approval, and these checks do not
  apply to them (see [MCP servers](#mcp-servers)).
- The guard is best-effort pattern matching. It can miss destructive commands and
  flag harmless ones, and it is **not** a sandbox: `bash` runs with your user's
  filesystem and network access.

## Saved data

Conversations — prompts, replies, tool calls, results, and the active context —
are stored in SQLite at `.pieni/pieni.db` inside the workspace, and the latest
session for the provider/model pair is resumed on startup. Treat that file as
sensitive local data; API keys are never written to it.

## Tests

The default suite is offline and free: every provider SDK is replaced by a fake,
so no network access or API calls are needed. Run the agent and streaming tests
alongside the installer tests:

```console
python3 -m unittest tests.test_pieni tests.test_streaming tests.test_controls tests.test_mcp
python3 -m unittest tests.test_install  # installer, against temporary copies
```

`test_install.py` (22 tests) copies the checkout into a temporary directory, runs
`scripts/install.sh` there with a temporary prefix, and never touches your home
directory or the network; a stub interpreter stands in for python3 when the
dependency path has to be exercised.

`test_sdk_wire.py` checks streaming and buffered request/response shapes against
the installed `openai` and `openrouter` packages, using a loopback HTTP server on
127.0.0.1 instead of the providers. It needs the virtualenv on the path:

```console
.venv/bin/python -m unittest tests.test_sdk_wire
```

It skips when the SDKs are not importable.

Optional live smoke tests check streaming and buffered headless tasks, all four
tools, Unicode files, and session resume. They are skipped unless you opt in with
a provider and model (plus the matching API key for hosted providers):

```console
PIENI_LIVE_PROVIDER=deepseek PIENI_LIVE_MODEL=deepseek-chat \
  DEEPSEEK_API_KEY=... .venv/bin/python -m unittest test_live
```

It is not part of the default run and costs whatever your provider charges.
For a local server, supply its base URL and the model name it serves instead;
the tests use temporary workspaces.

## Not included

Live reasoning streaming, automatic compaction, runtime provider/model switching,
web search, plugins, multiple agent modes,
MCP resources/prompts/OAuth,
and a real OS sandbox. See `PLANS.md` for the scope and `AGENTS.md` for the rules.
