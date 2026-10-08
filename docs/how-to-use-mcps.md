# How to use MCP servers with Pieni

An **MCP server** is a small program that gives Pieni new abilities, called *tools*. For
example, one can read the documentation of any GitHub project, another can search
Microsoft's documentation, and a third can download web pages. You connect a server to
Pieni, and the AI model can then use its tools whenever your request needs them.

This guide has copy-and-paste steps for **Ubuntu Linux 24.04** and **Windows 11
(PowerShell)**. Each step shows both. Use the one for your computer. You can try everything
here without making accounts, because the online servers used are free and need no key.

- [Before you start](#before-you-start)
- [Part 1: try an online server (DeepWiki)](#part-1-try-an-online-server-deepwiki)
- [Part 2: try a second online server (Microsoft Learn)](#part-2-try-a-second-online-server-microsoft-learn)
- [Part 3: save your servers so you do not retype them](#part-3-save-your-servers-so-you-do-not-retype-them)
- [Part 4: the web page fetcher that comes with Pieni](#part-4-the-web-page-fetcher-that-comes-with-pieni)
- [If something goes wrong](#if-something-goes-wrong)
- [Safety](#safety)

## Before you start

You need three things. If you have already used Pieni, skip to the last one.

**1. Pieni installed.** The steps are in the [README](../README.md#install). In short:

Ubuntu:

```bash
sudo apt update
sudo apt install -y python3 python3-venv
cd ~/pieni
./scripts/install.sh
```

Windows 11 (PowerShell). If `python --version` says Python is missing, install it first
with `winget install Python.Python.3.12` and open a new PowerShell window:

```powershell
cd C:\path\to\pieni
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Replace `~/pieni` and `C:\path\to\pieni` with the folder where you put Pieni.

**2. An API key** from your AI provider (OpenAI, DeepSeek or OpenRouter). Put it in the
terminal window you will use. It lasts until you close the window. Use the line that matches
your provider, and replace `your-key` with the real key:

Ubuntu:

```bash
export DEEPSEEK_API_KEY="your-key"
```

Windows 11 (PowerShell):

```powershell
$env:DEEPSEEK_API_KEY = "your-key"
```

For OpenAI use `OPENAI_API_KEY`, and for OpenRouter use `OPENROUTER_API_KEY`. Never put
your key in a file you share.

**3. A terminal opened in the Pieni folder.** On Ubuntu, open the folder in Files,
right-click an empty spot and choose **Open in Terminal**. On Windows, open the folder in
File Explorer, click the address bar, type `powershell` and press Enter.

In the commands below, replace `deepseek` with your provider and `MODEL` with a model name
your provider offers.

## Part 1: try an online server (DeepWiki)

[DeepWiki](https://deepwiki.com) explains public GitHub projects. Its MCP server is at
`https://mcp.deepwiki.com/mcp` and has three tools: `read_wiki_structure`,
`read_wiki_contents` and `ask_wiki_question`.

### Step 1. Check that the server is reachable (no AI model needed)

This asks the server for its tool list and the table of contents of one project. Copy the
whole block.

Ubuntu:

```bash
.venv/bin/python - <<'EOF'
import pieni
server = pieni.McpServer("wiki", pieni.HttpTransport("https://mcp.deepwiki.com/mcp"))
print([tool["name"] for tool in server.tools])
failed, text = server.call("read_wiki_structure", {"repoName": "modelcontextprotocol/python-sdk"})
print(failed, text[:400])
server.close()
EOF
```

Windows 11 (PowerShell):

```powershell
@'
import pieni
server = pieni.McpServer("wiki", pieni.HttpTransport("https://mcp.deepwiki.com/mcp"))
print([tool["name"] for tool in server.tools])
failed, text = server.call("read_wiki_structure", {"repoName": "modelcontextprotocol/python-sdk"})
print(failed, text[:400])
server.close()
'@ | .venv\Scripts\python.exe -
```

You should see `['ask_wiki_question', 'read_wiki_contents', 'read_wiki_structure']` and then
`False Available pages for modelcontextprotocol/python-sdk: ...`. If you get an error, see
[If something goes wrong](#if-something-goes-wrong).

On Ubuntu, if the `.venv` folder does not exist, use `python3 -` instead of `.venv/bin/python -`.

### Step 2. Start Pieni with the server connected

Ubuntu:

```bash
./pieni deepseek -m MODEL --mcp "wiki=https://mcp.deepwiki.com/mcp"
```

Windows 11 (PowerShell):

```powershell
.venv\Scripts\python.exe pieni.py deepseek -m MODEL --mcp "wiki=https://mcp.deepwiki.com/mcp"
```

`wiki` is just a name you choose. `--mcp` means "connect this server".

### Step 3. See what you got

At the `pieni>` prompt type:

```text
/mcp
```

You should see:
`wiki [http]: ask_wiki_question, read_wiki_contents, read_wiki_structure`

### Step 4. Ask a question that needs it

```text
Use the wiki tools to list the main sections of the GitHub project modelcontextprotocol/python-sdk.
```

Pieni prints a line for each tool it uses, like
`wiki__read_wiki_structure(repoName="modelcontextprotocol/python-sdk") -> ok, 900 ms`.
The name has two parts: your server name (`wiki`) and the tool name. Then the model writes
its answer. Try another one:

```text
Ask the wiki: how does the FastMCP server register tools in modelcontextprotocol/python-sdk?
```

Type `/quit` when you are done.

### The same thing without typing questions (one-shot mode)

Ubuntu:

```bash
./pieni deepseek -m MODEL --mcp "wiki=https://mcp.deepwiki.com/mcp" -r "Use the wiki tools to list the main sections of modelcontextprotocol/python-sdk."
```

Windows 11 (PowerShell):

```powershell
.venv\Scripts\python.exe pieni.py deepseek -m MODEL --mcp "wiki=https://mcp.deepwiki.com/mcp" -r "Use the wiki tools to list the main sections of modelcontextprotocol/python-sdk."
```

Pieni answers once and exits. This is handy for scripts.

## Part 2: try a second online server (Microsoft Learn)

Microsoft's documentation server is at `https://learn.microsoft.com/api/mcp`. Its tools are
`microsoft_docs_search`, `microsoft_code_sample_search` and `microsoft_docs_fetch`. You can
connect several servers at once by repeating `--mcp`:

Ubuntu:

```bash
./pieni deepseek -m MODEL --mcp "wiki=https://mcp.deepwiki.com/mcp" --mcp "learn=https://learn.microsoft.com/api/mcp"
```

Windows 11 (PowerShell):

```powershell
.venv\Scripts\python.exe pieni.py deepseek -m MODEL --mcp "wiki=https://mcp.deepwiki.com/mcp" --mcp "learn=https://learn.microsoft.com/api/mcp"
```

Type `/mcp` to see both. Then try:

```text
Search the Microsoft docs for how to create an Azure Functions timer trigger and explain the first steps.
```

## Part 3: save your servers so you do not retype them

Instead of `--mcp` every time, put the servers in a file called `.mcp.json` in the folder
where you start Pieni. This is the same file format that many other AI tools use.

Ubuntu:

```bash
cat > .mcp.json <<'EOF'
{"mcpServers": {
  "wiki":  {"url": "https://mcp.deepwiki.com/mcp"},
  "learn": {"url": "https://learn.microsoft.com/api/mcp"}
}}
EOF
```

Windows 11 (PowerShell):

```powershell
@'
{"mcpServers": {
  "wiki":  {"url": "https://mcp.deepwiki.com/mcp"},
  "learn": {"url": "https://learn.microsoft.com/api/mcp"}
}}
'@ | Set-Content -Encoding utf8 .mcp.json
```

Now start Pieni with the normal command, with no `--mcp`, and type `/mcp`:

Ubuntu: `./pieni deepseek -m MODEL`

Windows: `.venv\Scripts\python.exe pieni.py deepseek -m MODEL`

To use the servers in every folder, save the same file as `.pieni/mcp.json` in your home
folder (`~/.pieni/mcp.json` on Ubuntu, `C:\Users\you\.pieni\mcp.json` on Windows). If both
files define a server with the same name, the one in the project folder wins, and `--mcp`
on the command line wins over both.

To turn all servers off for one run, add `--no-mcp` to the start command.

## Part 4: the web page fetcher that comes with Pieni

Pieni includes a small server of its own, `examples/mcp_fetch_server.py`. It has one tool,
`fetch`, which downloads a web page, pretends to be a normal web browser (some sites
refuse anything else), and returns the text of the page. It runs on your computer and starts
automatically when Pieni does. Nothing needs installing.

### Step 1. Check it works on its own (no AI model needed)

This sends the server a short conversation: introduce yourself, list tools, fetch
`https://example.com`. Copy the whole block.

Ubuntu:

```bash
printf '%s\n' \
'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
'{"jsonrpc":"2.0","method":"notifications/initialized"}' \
'{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"fetch","arguments":{"url":"https://example.com","max_chars":300}}}' \
| python3 examples/mcp_fetch_server.py
```

Windows 11 (PowerShell):

```powershell
@'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"fetch","arguments":{"url":"https://example.com","max_chars":300}}}
'@ | .venv\Scripts\python.exe examples\mcp_fetch_server.py
```

You should get three lines of text back. The last one contains `Example Domain`, which is the
title of the page.

### Step 2. Use it in Pieni

Ubuntu:

```bash
./pieni deepseek -m MODEL --mcp "fetch=python3 examples/mcp_fetch_server.py"
```

Windows 11 (PowerShell):

```powershell
.venv\Scripts\python.exe pieni.py deepseek -m MODEL --mcp "fetch=.venv\Scripts\python.exe examples\mcp_fetch_server.py"
```

Type `/mcp`. You should see `fetch [stdio]: fetch`. Then ask:

```text
Fetch https://example.com and tell me in one sentence what the page says.
```

You will see `fetch__fetch(url="https://example.com") -> ok, ...` and then the answer.

The two commands only work when you start Pieni in the Pieni folder, because they point to
`examples/mcp_fetch_server.py` by its short path. To use the fetcher from another folder,
give the full path, for example `python3 /home/you/pieni/examples/mcp_fetch_server.py` on
Ubuntu or `C:\path\to\pieni\.venv\Scripts\python.exe C:\path\to\pieni\examples\mcp_fetch_server.py`
on Windows.

### Step 3 (optional). Save it in `.mcp.json`

In the file, a Windows backslash must be written twice (`\\`). Here is a file with the
fetcher and both online servers.

Ubuntu:

```bash
cat > .mcp.json <<'EOF'
{"mcpServers": {
  "wiki":  {"url": "https://mcp.deepwiki.com/mcp"},
  "learn": {"url": "https://learn.microsoft.com/api/mcp"},
  "fetch": {"command": "python3", "args": ["examples/mcp_fetch_server.py"]}
}}
EOF
```

Windows 11 (PowerShell):

```powershell
@'
{"mcpServers": {
  "wiki":  {"url": "https://mcp.deepwiki.com/mcp"},
  "learn": {"url": "https://learn.microsoft.com/api/mcp"},
  "fetch": {"command": ".venv\\Scripts\\python.exe", "args": ["examples/mcp_fetch_server.py"]}
}}
'@ | Set-Content -Encoding utf8 .mcp.json
```

### What the fetcher refuses

To keep you safe, the fetcher only downloads `http://` and `https://` addresses, and it refuses
addresses that lead into your own computer or home network (such as `localhost` and
`192.168.x.x`). Otherwise an AI model could use it to look around inside your network.
If you really need that, for example to test your own local web page, add `--allow-private`
to the server command, for example `--mcp "fetch=python3 examples/mcp_fetch_server.py --allow-private"`.

## If something goes wrong

| What you see | What to do |
| --- | --- |
| `warning: skipped MCP server 'wiki': HTTP request failed ...` | Pieni could not reach the server. Check your internet connection, VPN, proxy or firewall, then run the Step 1 check again to see the exact error. |
| `warning: skipped MCP server 'fetch': cannot start ...` | Pieni could not start the program. Check the path. On Windows, start Pieni from the Pieni folder and use `.venv\Scripts\python.exe`. |
| `warning: skipped MCP server 'fetch': the server closed the connection` | The program started and stopped at once. Run the Step 1 check for the fetcher to see why. Pieni hides a server's own error messages. |
| `/mcp` says `no MCP servers connected` | You started Pieni without `--mcp`, there is no `.mcp.json` in this folder, or you used `--no-mcp`. |
| `pieni: ... .mcp.json: ...` and Pieni will not start | The file has a typing mistake. The usual causes are a missing comma or quote, or a single backslash on Windows. Delete the file and create it again from this guide. |
| The model answers but never uses the tool | Ask for it by name, for example "Use the wiki tools to ...". |
| The fetcher says `... is not a public address` | The page is on your own computer or network. See "What the fetcher refuses" above. |
| You changed `.mcp.json` and nothing changed | Type `/quit` and start Pieni again. Servers are connected at startup. |

## Safety

- **Servers you connect are trusted.** Pieni runs their tools without asking you first, and its
  usual safety checks do not apply to what a server does. Only connect servers you know.
- **Online servers see what the model sends them.** Everything the model puts into a tool call
  (a question, a project name, a web address) goes to that company. Do not ask about private
  projects or paste secrets.
- **A `.mcp.json` file can start programs.** If you download someone's project and start Pieni
  in it, a `.mcp.json` file there runs its commands on your computer. Read it first, or start
  Pieni with `--no-mcp`.
- **Results are only text.** A web page or tool result can contain sentences that look like
  instructions. The model might follow them. If it starts doing something odd after a tool
  call, stop it with Ctrl+C.

## More

- Teach Pieni new routines with plain text files: [How to use skills](how-to-use-skills.md).
- How MCP works inside agents: [How Pieni works, MCP](how-pieni-works.md#mcp-tools-from-other-programs).
