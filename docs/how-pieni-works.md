# How Pieni works — and how AI harnesses and agents work in general

A model alone cannot do anything. It cannot read or change files, run commands, or use
the internet. To do that it needs an **AI harness**: the program that wraps the model,
decides what to send it, runs the tools it asks for, and decides what is allowed. A
model plus a harness is what this guide calls an **agent**. Writing your own harness is
a reasonable project; making one work with many models, many providers, and many
features is a large one.

Pieni is a tiny agent and harness written for education, which makes it a good place to
study how agentic coding actually works and how to build your own agent. Agentic coding
means the model works in steps — read a file, run a command, look at the result, decide
what to do next — instead of answering in one shot. Those steps are the same in the big
agents, so understanding them here should also help you use those agents more
deliberately.

Pieni is just [one Python file](../pieni.py), with about 1500 lines of code.
This guide follows its actual implementation.

## Contents

- [What surrounds the model](#what-surrounds-the-model)
- [A tool call, from request to result](#a-tool-call-from-request-to-result)
- [The agentic loop](#the-agentic-loop)
- [File permissions: the minimal sandbox](#file-permissions-the-minimal-sandbox)
- [The destructive-command guard](#the-destructive-command-guard)
- [Context, saved history, and compaction](#context-saved-history-and-compaction)
- [Provider adapters](#provider-adapters)
- [Reading and testing the implementation](#reading-and-testing-the-implementation)

## What surrounds the model

An agent needs answers to fairly ordinary programming questions:

- What goes into the next API request?
- Which functions can the model request, with which arguments?
- Who checks permission before a function changes something?
- What happens when a command fails or the user presses Ctrl+C?
- Which conversation messages survive a restart?

Here is how Pieni divides that work. Everything inside the harness box lives in
`pieni.py`; the SDKs and model server do not.

![Pieni's structure: the harness connects the user, model provider, tools, permissions, and saved history.](agent-structure.png)

The model server can be on another machine. The file reads and shell commands
still happen on the machine running Pieni. Sending `parser.py` through the `read`
tool puts its contents in a later model request; it does not mount your directory
on the provider's server. This distinction matters for both debugging and privacy.

### Why other agents are much larger

The popular agents — Codex, Claude Code, Hermes Agent — are built from a million lines of code or more.
Their surrounding systems include interfaces, integrations, execution policies,
and long-running state management. Some concrete differences:

| Agent | Work beyond Pieni's small loop |
| --- | --- |
| Codex | A richer terminal interface, review workflows, model controls, and script/CI use. Its sandbox can constrain spawned commands, not just built-in file operations. |
| Claude Code | Dedicated search and web tools, code-intelligence integrations, subagents, hooks, MCP, file checkpoints, and automatic context management. |
| Hermes Agent | Messaging gateways, scheduled tasks, persistent memory and skills, a tool registry, and multiple terminal backends such as local execution, Docker, and SSH. |

These are documented in the [Codex CLI guide](https://learn.chatgpt.com/docs/codex/cli)
and [sandbox guide](https://learn.chatgpt.com/docs/sandboxing),
[Claude Code's architecture guide](https://code.claude.com/docs/en/how-claude-code-works),
and [Hermes's architecture guide](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture/).
For example, delivering a Hermes task result to a messaging platform requires
authorization, session routing, and delivery code that Pieni's terminal does not need.

Pieni leaves out most of the components above so it can stay small. It can still do real
work: the loop is the same loop, and the model it calls can be as strong as any model you
point it at. What is missing is the breadth of features and the extra safety layers, so
small also means less padding when something goes wrong.

## A tool call, from request to result

Tool calls are what let the agent do things: change files, look around the file system, run
the tests, drive a web browser.

Pieni exposes four tool schemas in `TOOL_SPECS`:

| Tool | Arguments | Operation |
| --- | --- | --- |
| `read` | `path`, optional `start_line` and `end_line` | Read UTF-8 text; line ranges are inclusive and start at 1. |
| `write` | `path`, `content` | Create a file or replace its entire contents; create parent directories if needed. |
| `edit` | `path`, `old_text`, `new_text` | Replace exactly one occurrence. Zero or multiple matches are errors. |
| `bash` | `command`, optional `timeout` | Run a shell command from the workspace and return stdout/stderr. |

A **schema** is the description of a tool that the model is given: the tool's name, what
it does, and which arguments it accepts, together with their types. Think of a form with
labeled boxes. The model fills in the form, and the harness checks it before doing
anything. For example, `read` requires a string `path`. `start_line` must be an integer;
`true` is not accepted as an integer even though Python normally treats booleans as
integer subclasses.

When the model wants to inspect the parser, its reply might contain a tool call.
After the provider adapter translates it, Pieni's internal representation is:

```python
ToolCall(id="call_1", name="read", arguments='{"path":"parser.py"}')
```

This is a request, not an executed read. The path through the harness is:

1. `Agent.run_tool()` parses the JSON arguments.
2. `Toolbox.run()` checks the tool name, required arguments, types, and unknown keys.
3. For a file tool, it resolves the path and checks permissions.
4. The handler does the operation, returning a `ToolOutcome`.
5. The agent displays the outcome and saves a result tied to `call_1`.

For a small file, that result might be:

```json
{
  "role": "tool",
  "tool_call_id": "call_1",
  "name": "read",
  "content": "parser.py (lines 1-2 of 2)\ndef parse_number(text):\n    return int(text or \"0\")"
}
```

The next API request includes both the assistant's call and this result. The ID
connects them; it is especially important when one reply asks for several tools.
Each provider writes the call and the result in its own JSON shape, but the
call/result pairing is the usual function-calling mechanism. See [OpenAI's function-calling guide](https://developers.openai.com/api/docs/guides/function-calling).

If the file does not exist, the tool result contains an error instead. The harness
does not invent source code to fill the gap. The model can then request a directory
listing or ask the user which file they meant.

Tool descriptions are not enforcement. Even if a schema calls `write` a file
writer, Pieni still has to reject malformed arguments and unauthorized paths in
Python. Likewise, a valid call can be a bad idea: the schema cannot establish
whether replacing a whole file respects the user's request.

### What the four tools can and cannot do

There is no dedicated file-search tool, but the model can request:

```json
{"command":"rg -n 'parse_number' ."}
```

through `bash`, if `rg` is installed. That uses an external executable; Pieni does
not implement search itself. Similarly, a generated script could use an installed
browser library. That does not give Pieni a native browser tool or screenshot
input support.

Useful tools found in larger harnesses but absent here include:

- A structured search tool returning paths and line numbers with its own limits.
- A patch tool applying a multi-file diff, rather than one exact string replacement.
- A web-search or page-fetch tool with structured sources and citations.
- Browser and image tools for examining a page, screenshot, or diagram.
- Background process tools: start a development server, retain a process ID,
  inspect output, and stop it later.
- Subagent tools with separate contexts and controlled result sharing.
- MCP (Model Context Protocol) clients, which expose outside services as extra tools the agent can discover at runtime.

Some tasks can be approximated with shell commands. Dedicated tools make the
arguments, results, lifecycle, and permission checks more explicit. A shell that
can run arbitrary programs is already powerful; four tool names do not mean four
narrow capabilities.

Because `bash` runs any shell command the model writes, it is the tool that does most of
the heavy lifting, and how much it can lift depends entirely on what is installed on that
particular machine. With nothing but a shell it still gives file inspection, text
processing, and scripting. Add the usual developer tools and the same tool call can search
a tree with `grep`, `rg`, or a similar command; download a page with `curl` or `wget`; run
a test suite; drive a headless browser; or install a package. None of that is implemented
in `pieni.py`. Pieni hands the command to the shell and returns what comes back, so the
agent's reach is the machine's reach, not the harness's.

That is also what makes `bash` the most dangerous of the four. `read`, `write`, and `edit`
each do one thing Pieni can inspect, and the file-tool permission checks cover them. A
shell command gets none of that treatment: it can fetch and run a script from the
internet, read files the file tools would refuse, or send your source tree somewhere else,
without matching a destructive pattern and therefore without an approval prompt in `auto`
mode. The [destructive-command guard](#the-destructive-command-guard) below is the
harness's attempt to narrow that gap, and it only reads the command text.

Pieni also has practical limits: `read` returns at most 5,000 lines, tool handlers
bound output to 65,536 characters, and shell commands default to a 60-second
timeout, adjustable from 1 to 600 seconds. A timed-out command may have changed
files before it stopped. These limits are not an OS-level resource or process-tree
sandbox. The file tools are text tools, not byte-preserving binary editors;
`read` removes line endings in its displayed output, and Python text I/O can
normalize CRLF during an edit.

## The agentic loop

The parser task could produce this sequence:

1. Read `parser.py` and its tests.
2. Run `python3 -m unittest discover -s tests`.
3. Observe that `test_empty_input` fails because the parser returns `0`.
4. Request `edit` with `old_text="return int(text or \"0\")"` and
   `new_text="return int(text)"`.
5. Run the tests again and inspect the result.
6. Reply that the parser now raises `ValueError` for empty input, stating what
   was actually tested.

Pieni does not hard-code these phases. The model chooses them. If the edit fails
because its expected text occurs twice, the next model call sees that error and
can request more surrounding context before trying a more specific replacement.

Here is the task loop, including the denial and error paths. Tool calls in a
single reply run sequentially, not in parallel.

![The agentic loop: request the model, validate and execute tool calls, save results, and repeat until an answer or stop condition.](agentic-loop.png)

`Agent.steps()` is essentially “ask, save reply, run requested tools, repeat.”
On the next round it sends the system instructions and active conversation again.
This does not retrain the model. It supplies the working history for another
inference request.

Three details are worth noticing:

- A reply without tool calls ends the task. Pieni does not independently prove
  the task is complete. A model can stop too early or report an unverified success.
- The limit is **500 model rounds**, not 500 individual tool calls. It is not a
  monetary budget or a deadline for a single model request.
- Completed actions are not rolled back on interruption. Pieni records error
  results for unresolved calls so the conversation does not contain calls without
  corresponding results. It does not automatically rerun them.

Streaming changes display, not this control flow. Text can appear while the
provider is still replying, but Pieni assembles the full reply and tool arguments
before executing tools. A broken stream is not saved as a completed assistant
reply. Thinking text, when returned by the provider, is displayed as a short trace;
it is not saved or sent back as conversation history.

### A task is different from a standalone prompt

```console
pieni openai -m MODEL -r "Fix parser.py and run the tests."
pieni openai -m MODEL -p "What is the capital of Finland?"
```

The first runs the loop and saves the conversation. The second sends only that
user prompt, with tools disabled: no project `AGENTS.md`, no resumed session,
and no saved conversation. It makes one model request and exits.

## File permissions: the minimal sandbox

A **sandbox** is a boundary that keeps a program away from everything outside an agreed
area. Strong sandboxes are enforced by the operating system, a container, or a virtual
machine. Pieni has something much weaker: permission checks in its own Python code,
covering its three file tools, **not containment of the whole agent**. A command run
through `bash` is not contained at all.

The boundary applies to the workspace: the directory where Pieni starts, which is not
necessarily a Git root or the directory containing the installed launcher. In `auto`
mode, `read`, `write`, and `edit` automatically allow resolved paths inside that
workspace and the system temporary directory. The latter comes from
`tempfile.gettempdir()`, usually `/tmp` on Linux; it can differ with the environment or
platform.

Assume the workspace is `/home/eye/project` and the temp directory is `/tmp`:

| Proposed file-tool path | Result in `auto` mode |
| --- | --- |
| `src/parser.py` | Inside the workspace; allowed. |
| `src/../tests/test_parser.py` | Resolves inside the workspace; allowed. |
| `/tmp/pieni-example/output.txt` | Inside the temp directory; allowed. |
| `../other-project/config.ini` | Outside both; requires approval. |
| `/home/eye/project-old/config.ini` | Not a child of `project`; requires approval. |
| `linked-config`, a symlink to a file in another project | The resolved target is outside; requires approval. |

`resolve_path()` expands `~`, resolves `..`, and follows symlinks. `is_inside()`
checks path ancestry, not a string prefix, so `project-old` is not mistaken for
`project`. Interactive approval authorizes that proposed operation. Headless runs
have no approval callback and deny it. `yolo` bypasses these permission checks.

This is useful for catching accidental file-tool access outside the project.
It is not a guarantee that outside files cannot be accessed:

- `bash` has a different check: the destructive-command guard. A request such as
  `cat ../other-project/config.ini` has no matching destructive pattern and can run
  without a file-tool approval prompt.
- Starting a shell with `cwd=workspace` sets its initial directory. It does not
  prevent `cd ..`, absolute paths, scripts, or network access.
- The permitted temp directory is the whole temp tree, not a private directory
  allocated to Pieni. Ordinary OS permissions still apply there.
- Resolving and checking a path is separate from opening it. This does not close
  filesystem race conditions or provide a complete hard-link defense.

For actual containment, constrain the process and its children using an appropriate
OS sandbox, container, or VM, with limited mounts, credentials, and network access.
A container with your whole home directory mounted read/write would still expose
that directory. Do not treat instructions in `AGENTS.md` as a substitute for
enforcement. Codex's [sandbox documentation](https://learn.chatgpt.com/docs/sandboxing)
describes restrictions inherited by spawned commands; Claude Code's
[sandbox documentation](https://code.claude.com/docs/en/sandboxing) also explains
which operations run outside its shell sandbox. Their boundaries depend on configuration.

## The destructive-command guard

Before a shell request runs in `auto` mode, `Permissions.check_shell()` calls
`dcg_reason()`. That function searches the command string using the small
`DESTRUCTIVE_PATTERNS` list. A match returns a reason and triggers an approval
request. No match means automatic execution; it does **not** mean the command has
been established as safe.

Examples below describe strings the guard examines, not commands to try:

| Command text | Why Pieni flags it |
| --- | --- |
| `rm -rf build` | `rm` with recursive/force flags. |
| `git push --force origin main` | Force push. |
| `dd if=input.img of=output.img` | `dd` writing to a file or device. |
| `sqlite3 example.db 'DROP TABLE records'` | SQL `DROP TABLE`. |
| `sqlite3 example.db 'DELETE FROM records'` | SQL deletion without a detected `WHERE`. |

It also has patterns for formatting filesystems, a common fork-bomb form, system
shutdown/reboot, redirects to some raw-device paths, and SQL `TRUNCATE`.
The patterns are case-insensitive and examine the command text, not its effects.

That explains both kinds of mistake:

- **False positive:** `printf '%s\n' 'DROP TABLE example'` merely prints text,
  but the SQL pattern still matches. The guard does not understand quoting.
- **False negative:** `git reset --hard` is not covered. Nor does the guard inspect
  what a Python script, a Makefile target, or a downloaded executable will do.
- Shell expansion, variables, alternate flag spellings, wrappers, and indirect
  commands can evade a regex. SQL can be hidden in a script, and a `WHERE` clause
  does not make a deletion harmless.
- Ordinary redirection, `write`, and `edit` can overwrite important data without
  using any flagged shell pattern. The DCG does not inspect file-tool contents.

Pieni does not implement a shell parser, recursive script inspection, a general
policy for overwrites or network actions, backups, or an OS execution boundary.
Adding more regexes can catch more common mistakes; it cannot establish what an
arbitrary program will do. In `yolo` mode the DCG is skipped entirely. In a headless
`auto` run, a flagged command is denied and that denial is returned to the model.

There is another risk that command patterns do not address: a source file or web
page can contain instructions directed at the agent. Such text is task data,
but the model may follow it anyway. A path check does not solve prompt injection,
and a command can disclose data over the network without matching a destructive
pattern.

## Context, saved history, and compaction

Pieni keeps two related things:

- **Active context:** messages supplied to subsequent model calls, plus system
  instructions and tool schemas.
- **Historical logs:** persisted messages in `.pieni/pieni.db`, including messages
  no longer active after compaction.

`Store` saves messages as JSON in SQLite. Startup resumes the latest session for
the selected provider, model, and workspace. Restoring historical tool messages
does not execute the calls again. If someone has changed `parser.py` since the
session was saved, the old read result is stale; the model must read the file again
to establish its current contents.

History can become large quickly. Reading a 3,000-line file and running a verbose
test suite adds those results to later requests. Three different limits are easy
to confuse:

1. Tool output limits bound a result when the operation finishes.
2. Compaction preparation trims tool data before a summary request.
3. A model-written summary replaces the active conversation after `/compact`.

Pieni does not automatically compact near a model's limit. Two terms matter here: a
**token** is a piece of text, about four characters of English on average, and the
**context window** is how many tokens a model can read in one request. The
1,000,000-token window Pieni displays is an assumption for the display, not discovered
model capacity, and the context count is a rough character-based estimate.

### What `/compact` actually does

`compaction_history()` builds a temporary copy of the active messages. It leaves
user and assistant prose, previous summaries, tool names and IDs, and parsed
`path` argument values intact. Tool result text and other top-level string argument
values longer than `COMPACT_TOOL_CHARS` are reduced to their first 1,000 and last
1,000 characters, with an omission marker in between.

For example, a 12,000-character `edit.new_text` becomes:

```text
first 1,000 characters
...[omitted 10000 characters]...
last 1,000 characters
```

The marker is extra text: 2,000 is the retained-data budget, not the final string
length. Short results, such as a permission denial, survive unchanged. Arguments
are parsed so that clipping a large file body does not clip its separate `path`
field. Malformed JSON falls back to bounded raw argument text.

Pieni serializes this prepared transcript into a single JSON document inside a
user message. It sends the system instructions and a summary request with tools
disabled. The old tool calls are quoted data, not native calls in this request;
the shortened arguments therefore need not remain executable tool-call JSON.
Quoting and labeling the history as data is not a complete prompt-injection defense.

The summary prompt asks for the goal, constraints, decisions, changed files,
important symbols, verified outcomes, failures, unfinished work, and next step.
It also asks the model to incorporate later corrections to earlier summaries and
not confuse a requested action with a confirmed result.

A useful summary of a failing parser task would say:

```text
Goal: reject empty input in parser.py without changing the public API.
No files changed yet. test_empty_input failed: expected ValueError, got 0.
Next: inspect parse_number, change the empty-input handling, and rerun tests.
```

“Fixed parser.py and ran tests” would be wrong if the edit was denied or the test
command failed. The prompt helps distinguish those situations; it cannot guarantee
that the model will summarize correctly.

After receiving a nonblank summary with no tool calls, `Store.replace_context()`
deactivates the previous active messages and inserts one new summary message
in a single SQLite transaction. Only after that commits does the in-memory list
become the summary alone. Provider failure, interruption, a blank reply, unexpected
tool calls, or a database failure leaves the previous active context unchanged.

The original persisted messages remain as logs. They are the original **saved**
messages, which may already contain output truncation from the tool handlers—not
an unlimited transcript of every byte a command produced. They are not automatically
retrieved by the model after compaction.

### What gets lost

Head/tail trimming is cheap and readable, but an important assertion failure in
the middle of a long result can disappear. An exact patch body also disappears
from active context once the conversation becomes a summary. The model can reread
files with tools, but there is no built-in search of inactive SQLite logs.

Repeated summaries can compound omissions. User prose is not trimmed during
preparation, so an enormous user message can still exceed the provider's limit.
There is no “keep the last few turns” policy: Pieni replaces the whole active
conversation with the summary. `/compact all` is different again: it starts a new
session with no conversation messages and does not request a summary or delete
old logs.

Larger harnesses can separate output pruning, protected recent turns, summarization,
and retrieval. For example, [Claude Code documents clearing older tool outputs
before summarizing](https://code.claude.com/docs/en/how-claude-code-works#when-context-fills-up).
Pieni uses just temporary trimming and a manual summary to keep the mechanism small.

Saved conversations are sensitive: file contents and command output can contain
credentials. Pieni does not deliberately save its API-key configuration, but it
does not scrub secrets out of tool output or user messages.

## Provider adapters

The loop should not need to know whether a provider calls an operation
`responses.create()` or `chat.send()`. `Provider.complete()` translates the same
internal messages and tool definitions into an API request, then normalizes the
reply into:

```python
Reply(text, tool_calls, input_tokens, output_tokens, estimated, thinking, provider_reasoning)
```

`tool_calls` contains `ToolCall` objects. If the provider reports usage, Pieni uses
it; otherwise it labels a simple estimate. `thinking` is optional display-only
text, not a portable way to preserve a model's reasoning state. The optional
`provider_reasoning` dictionary holds full DeepSeek `reasoning_content` or
OpenRouter reasoning fields and ordered blocks. Those fields are saved with
assistant messages and replayed for tool continuation and resume; compaction
omits them from its summary input.

`--reasoning EFFORT`, the INI `reasoning` key, and `/reasoning EFFORT` control
effort on subsequent requests. `default` omits the effort parameter and lets
the provider choose; runtime changes do not edit configuration or session settings.
The adapters translate the setting to each provider's request fields.

Different models support different reasoning effort settings and defaults.
Some older models have no reasoning mode at all; changing effort cannot give
them one. Use `default` when the model does not support effort controls, since
an explicit unsupported value may cause a provider error.

### The three wire formats

A wire format is the exact JSON shape an API expects.

| Provider selection | SDK and call | Translation |
| --- | --- | --- |
| `openai` | `openai`: `client.responses.create()` | System text becomes `instructions`; conversation becomes `input` items. Tools use a flat function definition. |
| `deepseek` | `openai`: `client.chat.completions.create()` at `https://api.deepseek.com` | Chat messages and nested function schemas; key from `DEEPSEEK_API_KEY`. |
| `openrouter` | `openrouter`: `client.chat.send()` | Shares Pieni's Chat Completions conversion and reply handling; key from `OPENROUTER_API_KEY`. |
| An HTTP(S) URL | `openai`: Chat Completions at that base URL | Same conversion as DeepSeek; `OPENAI_API_KEY` if set, otherwise a placeholder for local servers. |

Here is the same tool result written in two of them:

```json
{
  "role": "tool",
  "tool_call_id": "call_1",
  "content": "error: old_text not found in parser.py"
}
```

is a Chat Completions message. In Responses it becomes:

```json
{
  "type": "function_call_output",
  "call_id": "call_1",
  "output": "error: old_text not found in parser.py"
}
```

OpenAI function tools have `name`, `description`, and `parameters` beside
`type: "function"`. Chat Completions wraps those fields inside `function`.
`responses_tools()` and `chat_tools()` handle that difference without duplicating
the definitions in `TOOL_SPECS`.

DeepSeek's [API introduction](https://api-docs.deepseek.com/) documents its
OpenAI-compatible endpoint. OpenRouter's [tool-calling guide](https://openrouter.ai/docs/guides/features/tool-calling)
describes the request/call/result exchange. Compatibility still depends on the
selected model and server: accepting chat messages does not establish support
for tool calls, fragmented streamed arguments, or usage reporting.

The stream collectors join text and argument fragments and keep each tool's ID.
The Chat Completions and OpenRouter paths share `call_chat()`; the Responses path
has a separate converter and collector. Streams are closed on completion, failure,
or interruption. Pieni does not silently retry a failed stream without streaming;
use `--no-streaming` explicitly if that is what your endpoint supports.

### Adding your own provider

First check whether you need new code at all. An OpenAI-compatible local server
can already be selected by URL:

```console
pieni http://localhost:30000 -m YOUR_SERVED_MODEL -p "Reply with one sentence."
pieni http://localhost:30000 -m YOUR_SERVED_MODEL -r "Read README.md and describe this project."
```

The first checks ordinary replies. The second checks tool calling in addition.
Use a disposable workspace for unfamiliar models or servers. Neither command
proves the provider supports every tool, error path, or streaming variant.

For a hosted compatible endpoint, set `OPENAI_API_KEY` in your environment and
pass its documented base URL; append `/v1` only if the endpoint requires it.
Pieni uses that key for custom URLs regardless of the vendor's usual variable
name. Do not embed it in a URL, INI file, or tool command. The base URL chooses
where SDK requests are sent, so use a server you trust.

If you want a named shortcut such as `example`, the change is small but touches
more than one dictionary:

1. Add the name to `provider_kind()` so it selects the Chat Completions path.
2. Add its key variable and base-URL handling in `build_provider()`. That function
   currently assumes the named `chat` provider is DeepSeek; classification alone
   is not enough.
3. Update the CLI/help text and provider tests.

The compatible client would be constructed along these lines; replace the example
endpoint with the provider's actual documented URL:

```python
client = OpenAI(api_key=environment["EXAMPLE_API_KEY"],
                base_url="https://api.example.invalid/v1")
return Provider("example", model, "chat", client, close=client.close,
                streaming=streaming)
```

For an incompatible API, add one request/reply function with the same contract as
`call_responses()` or `call_chat_completions()`, wire it into `Provider.complete()`,
and construct its client in `build_provider()`. Translate message roles, tool
schemas, call/result IDs, streaming events, usage, and errors. Reuse existing
dependencies when possible; this project intentionally permits only `openai` and
`openrouter` as direct external dependencies, so adding another vendor SDK would
be a deliberate change to its scope.

Test a plain reply, one tool call followed by an answer, multiple calls, malformed
arguments, a tool failure, missing credentials, missing usage, fragmented streamed
arguments, interrupted streams, and client cleanup. An adapter that works for
“hello” may still lose a call ID or execute an incomplete command.

## Reading and testing the implementation

Use function names rather than fixed line numbers, which change as the file does:

| Question | Where to read in [pieni.py](../pieni.py) |
| --- | --- |
| How is a task started? | `run_agent()`, `Agent.run_task()` |
| Where is the loop? | `Agent.steps()`, `Agent.run_tools()`, `Agent.run_tool()` |
| How are tools defined and checked? | `TOOL_SPECS`, `Toolbox.run()`, `Toolbox.validate()` |
| Where are access decisions made? | `Permissions`, `resolve_path()`, `dcg_reason()` |
| What does the model receive? | `Agent.wire_messages()`, `build_instructions()`, provider converters |
| What survives restart or compaction? | `Store`, `compaction_history()`, `Agent.compact()` |

`AGENTS.md` is loaded only from the starting workspace root and combined with the
system prompt. Pieni does not recursively discover nested instruction files.
Skills (`SKILL.md` folders in `~/.agents/skills` and `.agents/skills`) are added the
same way, but only as a name/description catalog; the model reads a skill's full
text with the `read` tool when it decides the task needs it.
Configuration is user INI first, launch-directory INI second, explicit CLI values
last. Those are harness decisions, not behaviors learned by the model.

Interactive input beginning with `!` runs directly through the existing shell
tool, with the same permissions, timeout, and output limits. The terminal shows
stdout/stderr, success or exit code, and elapsed milliseconds. These commands
do not call the model or enter the saved conversation.

Run the default offline suite from the checkout:

```console
.venv/bin/python -m unittest discover -s tests
```

The [tests](../tests/) use standard-library `unittest`. Mock replies let them check
the loop deterministically: request a read, inspect the returned result, request
an edit, then finish. SDK wire tests use a loopback HTTP server rather than a paid
provider. Installer tests operate on temporary copies. Live model tests are
separate and opt-in.

To follow a real task, compare the displayed tool status with what the model says.
`edit(...) -> error` means no successful edit was confirmed. `bash(...) -> ok`
means the command exited with status zero, not that its output proves the user's
goal. A final answer is another model response; evidence comes from the actual
file changes and checks the harness performed.
