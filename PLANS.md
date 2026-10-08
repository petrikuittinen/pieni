# Pieni: a small educational coding agent

## Goal

Build a readable agent that demonstrates the basic cycle: receive a prompt,
call a model, execute requested tools, and continue until the model answers.
It should be useful to run and easy to understand, fork, and extend—not a
production agent platform.

All agent code belongs in `pieni.py`, aiming for about 750 readable lines.
Use only the standard library and two direct external dependencies, `openai` and
`openrouter`, listed in `requirements.txt` (`pip install -r requirements.txt`).
Provide a small Bash launcher named `pieni`; tests live in separate Python files. No agent code is to be generated during this planning task.

## First version

### Agent loop and tools

- One agent mode, shared by interactive and headless use.
- A short system prompt emphasizing KISS, YAGNI, DRY, and testing changes.
- Load `AGENTS.md` from the starting workspace root if present. No recursive
  instruction discovery in this version.
- Skills: `SKILL.md` folders (Agent Skills format) one level under `~/.agents/skills`
  and `.agents/skills`, project overriding user. Only name, description, and path
  enter the system prompt; the model loads a skill with `read`, which may read the
  user skills directory without approval. No scripts runner or other skill fields.
- Four tools: `read`, `write`, `edit`, and `bash`.
  - `read`: read UTF-8 text, optionally selecting a line range.
  - `write`: create or replace a UTF-8 file.
  - `edit`: replace one exact text match; fail if it is missing or ambiguous.
  - `bash`: execute a shell command from the workspace, with a timeout.
- Validate tool arguments and return concise success/error results. Bound read
  and shell output, marking truncation rather than flooding model context.
- Preserve tool-call IDs and results when continuing the model conversation.
  Do not re-execute historical tool calls when resuming a session.
- Cap a task at 500 model rounds (`MAX_STEPS`) so a confused model cannot loop
  forever. One round may carry several tool calls, so this bounds the conversation
  rather than individual calls. On reaching the cap, print the reason and keep the
  context so the user can ask Pieni to continue.
- Bound tool output: 5,000 lines per `read` (`MAX_READ_LINES`) and 65,536 characters
  per tool result (`MAX_OUTPUT_CHARS`), marking truncation in both cases.

### Permissions

- Default to `auto`: automatically accept shell commands unless the destructive
  command guard (DCG) flags them. Flagged commands require explicit user approval.
- Keep DCG as a small, readable regex-based function covering obvious destructive
  commands, such as recursive forced deletion and SQL `DROP TABLE`. Test both
  flagged and ordinary commands; do not attempt a complete shell/SQL parser.
- File tools automatically allow resolved paths within the starting workspace
  and the process's standard temporary directory. Outside paths require approval;
  account for `..` and symlinks when checking file-tool paths.
- Run shell commands from the workspace, but do not claim this restricts their
  filesystem access. DCG can miss destructive commands and can flag harmless
  ones; it is not a security boundary or OS sandbox.
- `yolo` skips approval and guard checks, not argument validation or timeouts.
  Make its risk explicit in help.
- In headless mode, deny any action needing approval rather than waiting for
  input. Return the denial to the model; exit nonzero if the task cannot finish.
- Approval applies only to the proposed action, not to all future actions.

### Configuration

- Use standard-library `configparser` with the default filename `pieni.ini`.
  Load optional files in this order:
  1. `~/.pieni/pieni.ini` (user settings).
  2. `pieni.ini` in the current directory when Pieni is launched (local settings).
- Merge settings per key: local values override user values; omitted local keys
  retain user values. Explicit CLI arguments override both files; built-in
  defaults apply only when neither file nor CLI supplies a value.
- Keep one `[pieni]` section with `provider`, `model`, `permissions`, `streaming`, and `reasoning`
  settings. Provider/model may come from configuration instead of CLI arguments;
  report a clear error if either is still missing. Default permissions remain
  `auto`. Streaming defaults to `true` for every provider/model, including custom
  endpoints. Accept standard INI booleans (`true/false`, `yes/no`, `on/off`, `1/0`);
  reject invalid values clearly.
- Reasoning defaults to `default`, omitting the effort parameter. Accept `none`,
  `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`, plus `default` to reset.
  `--reasoning EFFORT` overrides INI settings for every interface and request,
  including compaction. Unsupported model settings fail without fallback retries.
  Send `reasoning.effort` on Responses and OpenRouter, and `reasoning_effort` on
  custom Chat Completions. DeepSeek uses `thinking.type = disabled` for `none`;
  other explicit efforts enable thinking and set `reasoning_effort`.
- Missing files are fine. Report unreadable files, malformed INI, and invalid
  settings clearly. Read UTF-8 and disable interpolation to keep values literal.
- API keys remain in environment variables, not configuration files. No config
  generation, alternate-file flag, or additional configuration framework yet.

### Providers and CLI

- Accept `openai`, `openrouter`, `deepseek`, or a custom base URL, with a model
  name supplied through CLI or configuration. Do not hard-code a model catalog
  or invent provider capabilities.
- Keep a thin adapter with these provider-specific calls:
  - OpenAI: `from openai import OpenAI`, initialize `OpenAI()` (reads
    `OPENAI_API_KEY`), and call `client.responses.create(...)`.
  - DeepSeek: use `OpenAI(api_key=os.environ.get("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com")` and
    `client.chat.completions.create(...)`; no separate DeepSeek dependency.
  - OpenRouter: install `openrouter` (`pip install openrouter`), import
    `OpenRouter` from `openrouter`, and manage the client with
    `with OpenRouter(api_key=os.getenv("OPENROUTER_API_KEY")) as client:`.
    Call `client.chat.send(model=model, messages=..., ...)` and read final text
    from `response.choices[0].message.content` when streaming is disabled.
  - Custom base URLs: use the `openai` SDK's Chat Completions API.
- Normalize only the messages, tool calls/results, final text, and usage needed
  by the shared loop; do not build a general provider framework. Keep model names
  configurable rather than hard-coding sample model IDs.
- Named providers use `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, or `DEEPSEEK_API_KEY`.
  Custom endpoints use `OPENAI_API_KEY` if needed; do not require a key for a
  local endpoint that accepts unauthenticated requests.
- Run as `pieni PROVIDER -m MODEL` or `python3 pieni.py PROVIDER -m MODEL`.
  Provider and `-m` may be omitted when supplied by configuration.
- `-r/--run "prompt"` runs one headless task. `--permissions auto|yolo` selects
  permissions for either interface; default is `auto`.
- `-p/--prompt "prompt"` asks for one standalone reply with tools disabled and
  exits. Send only the user prompt, without project instructions or saved context;
  do not create or modify a session. Respect the streaming setting and CLI
  overrides. This option is mutually exclusive with `-r/--run`.
- `--streaming` enables streaming; `--no-streaming` disables it. These mutually
  exclusive CLI flags override the layered INI setting. When neither flag is
  supplied, retain the file setting or the built-in `true` default.
- Send `stream=True` on all provider paths when enabled. Display text fragments
  immediately in interactive and headless use, assemble complete tool calls before
  execution, and use final provider usage when available. Close streams on success,
  failure, or interruption. Incomplete replies fail clearly without saving partial
  assistant/tool-call messages or silently retrying in nonstreaming mode; users can
  explicitly disable streaming for endpoints that do not support it.
- API keys are not command-line arguments. Use placeholders in documentation
  examples so model names do not become stale requirements.

### Interaction and history

- Start with a plain terminal prompt, not a full TUI. On an interactive terminal,
  `rich` renders model replies as Markdown (live while streaming) and colors status
  lines; other text is never Markdown. Up/Down recall earlier prompts via `readline`
  where available. Piped output stays plain. Print the full final answer.
  Never invent or summarize reasoning: show only the thinking text the model itself
  returns, and skip the line when there is none.
- Stream reply text by default, without printing the completed answer twice.
  When streaming is disabled, show progress while waiting: one `.` per
  `THINK_DOT_INTERVAL` second, only on an interactive terminal, followed by a newline
  before the next line. Show a completed reply's thinking trace as one line cut to
  `THINK_TRACE_CHARACTERS` characters. The displayed trace is not saved separately.
  Preserve complete DeepSeek `reasoning_content` and OpenRouter reasoning fields
  and ordered blocks in assistant messages for tool continuation and SQLite resume;
  omit those fields from compaction input. Live reasoning remains deferred.
- After each tool call finishes, display its name/arguments, `ok` or `error`
  (with a short cause), and elapsed time in milliseconds, for example:
  `read(path="pieni.py") -> ok, 12 ms`. Denied, failed, timed-out, and interrupted
  calls also get a result line; do not report them as successful.
- After every completed, failed, or interrupted task, in both interactive and
  headless use, display task token usage, current context usage, and elapsed time:
  `Tokens: ~2,400 | Context: ~12,000 / 1,000,000 (1.2%) | 3.1 seconds`.
  Task usage totals input and output tokens across that task's model calls;
  context usage estimates the active context, including instructions and tools,
  rather than all saved logs. Use provider usage when available and a simple,
  clearly labeled text-based estimate otherwise; no tokenizer dependency.
  On interruption, report available usage plus estimated partial work, not an
  invented exact count. Assume a 1,000,000-token context window for display only,
  not as a claim about the selected model's actual limit. Measure elapsed time
  with a monotonic clock; task duration uses one decimal place and `seconds`.
- Ctrl+C interrupts the current task and returns to the prompt; interruption at
  an idle prompt exits. Headless interruption exits nonzero. Completed actions
  are not rolled back.
- Save prompts, replies, tool calls/results, and active context in standard-library
  SQLite at `.pieni/pieni.db` under the workspace. Do not save API keys.
- On startup, restore the latest session for the selected provider and model in
  that workspace, or start fresh if none exists. Report whether a session resumed.
- Manual commands only:
  - `/compact`: ask the model for a short working summary and replace older
    conversation context only after a successful response.
  - `/compact all`: clear conversation context and start a fresh session.
  - `/permissions auto|yolo`: change permissions.
  - `/reasoning`: show effort; `/reasoning EFFORT`: change effort until exit.
    Invalid values or extra arguments leave it unchanged; changes are not saved
    as configuration or session settings.
  - `/skills`: list the skills found at startup, or say where Pieni looks.
  - `!COMMAND`: run directly in the workspace using the shell tool's permission
    checks, 60-second timeout, decoding, and output bounds. Print stdout/stderr
    and `ok` or an error cause/exit code with elapsed milliseconds. Interruption
    returns to the prompt without retrying. Shells are separate; `cd` does not
    persist. Do not call the model, save command/output, or show a token summary.
  - `/help`: concise help.
  - `/quit` and `/exit`: exit.
- Compaction retains the system prompt, workspace instructions, and tool
  definitions. Persist the new active context; keep old messages as logs, not
  automatically restored context. A failed compaction leaves context unchanged.
  Before summarizing, shorten bulky tool output and argument text to 2,000 retained
  characters (half from each end), keeping file paths and ordinary conversation.
  Send that temporary history as JSON data with tools disabled. Preserve constraints,
  verified outcomes, failures, and next steps in the summary; replace active context
  in one SQLite transaction so database failures leave the old context resumable.
- Keep failures understandable: invalid configuration, network/API failures,
  unsupported tool calling, file/DB errors, and oversized context. No elaborate
  retry system or automatic context-size detection.

## Small implementation milestones

These are reviewable steps, not separate subsystems or a large PR program.

1. **Working loop:** layered INI/CLI configuration, thin provider adapter, four tools, permissions,
   and headless execution, with offline tests using mocked model responses.
2. **Interactive use:** plain prompt, interruption, tool status/timing and task
   usage/timing summaries in both interfaces, SQLite save/resume, and the small
   command set including manual compaction.
3. **Finish the example:** Bash launcher, dependency declaration, accurate README,
   and remaining failure-path tests. Optional live smoke tests with an explicitly
   selected provider/model; no paid calls in the default test run.
   Done: launcher, `requirements.txt`, README, `test_sdk_wire.py`, and
   `test_live.py` (opt-in); `pieni.py` has about 1500 lines of code after
   deduplication, the standalone prompt option, reasoning controls, and shell shortcuts, above the
   ~750 guideline. File-tool validation and
   permission checks share one path;
   Chat Completions and OpenRouter share request/reply handling.
   Streaming defaults, layered configuration, and failure-path tests are also
   implemented (`test_streaming.py`). Local Qwen smoke tests cover streaming and
   buffered replies, all four tools, Unicode files, and resume; hosted streaming
   remains unverified after the refactor.
   Also added `scripts/install.sh` (Ubuntu installer, tested by `test_install.py`)
   with the launcher following symlinks so an installed command still finds its
   own files; that is packaging, not agent scope.
   Reasoning controls and terminal-only `!COMMAND` shortcuts are implemented
   (`test_controls.py`), including provider reasoning replay and offline SDK
   payload checks. Hosted reasoning behavior remains unverified.

Mock each provider's SDK call, including OpenRouter client cleanup, tool-call
continuation, usage extraction, and missing-key/provider errors. No live API
requests in default tests.

Check the request and response shapes against the installed `openai` and
`openrouter` packages with a loopback HTTP server on 127.0.0.1, which keeps that
check offline and free while still using the real SDKs. This was done for all
three wire formats: Responses instructions/input items and the flat tool schema,
Chat Completions tool nesting and tool results, and OpenRouter `chat.send`.

Test user/local/CLI configuration precedence, missing and malformed INI files,
invalid settings, streaming defaults/INI booleans/CLI overrides, fragmented text
and multiple tool calls, stream cleanup/failure/interruption, no duplicated answers,
successful and failing tool calls, malformed arguments, DCG approval/denial,
headless behavior, path boundaries/symlinks, empty and Unicode text, output limits,
resume without repeated tool execution, failed compaction, and tool/task summary
formatting on success, failure, and interruption (including missing provider usage).
Keep the suite comprehensive for this scope, not a separate testing framework.

## Deferred, not promised

- Advanced terminal editing, history navigation, multiline key bindings, and ESC.
- Live streaming of reasoning as it arrives (the thinking trace is shown after the
  reply completes, not token by token), exact tokenizer-based accounting,
  model-specific context-window discovery, and automatic compaction.
- Runtime provider/model switching.
- Web search, including provider-native search or a Tavily fallback.
- MCP, skills, plugins, multiple agent modes, and a true OS sandbox.

Revisit a deferred feature only after the small core works and its educational
value justifies the added code. There is no committed second release scope.
