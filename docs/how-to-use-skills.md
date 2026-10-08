# How to use skills in Pieni

A **skill** is a small text file that teaches Pieni how to do one kind of job your way,
for example "turn my rough meeting notes into a tidy summary" or "download a web page".
Pieni lists its skills to the AI model at startup, and the model reads a skill only when
your request matches it. You do not have to remember anything or type special commands.

This guide has copy-and-paste steps for **Ubuntu Linux 24.04** and **Windows 11
(PowerShell)**. Each step shows both. Use the one for your computer.

- [Before you start](#before-you-start)
- [Part 1: try the ready-made skill](#part-1-try-the-ready-made-skill)
- [Part 2: make your own skill](#part-2-make-your-own-skill)
- [Part 3: keep a skill for all your projects](#part-3-keep-a-skill-for-all-your-projects)
- [Writing good skills](#writing-good-skills)
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

In the rest of this guide, "start Pieni" means this command. Replace `deepseek` with your
provider and `MODEL` with a model name your provider offers:

Ubuntu:

```bash
./pieni deepseek -m MODEL
```

Windows 11 (PowerShell):

```powershell
.venv\Scripts\python.exe pieni.py deepseek -m MODEL
```

You will see a banner and the prompt `pieni>`. Type `/quit` to leave.

## Part 1: try the ready-made skill

Pieni comes with one example skill called **web-fetch**. It teaches the model to download a
web page with `curl`, pretending to be a normal web browser because some sites refuse
requests that do not.

1. Start Pieni (see above).
2. Type this and press Enter:

   ```text
   /skills
   ```

   You should see a line starting with `web-fetch:`. If you see "no skills found", you are
   not in the Pieni folder. Type `/quit`, change to the Pieni folder, and start again.

3. Ask for something that needs a web page:

   ```text
   Fetch https://example.com and tell me in one sentence what the page says.
   ```

   Pieni shows each step it takes, such as a `read(...)` of the skill file and a `bash(...)`
   running `curl`, each ending in `-> ok`. Then the model answers. Seeing the skill file
   read first means the skill was used.

**Windows note.** On Windows, Pieni runs commands with `cmd.exe`, while the skill is written
with Linux-style commands. Windows 11 includes `curl.exe`, and the model usually adapts, but if
the download fails, use the [fetch MCP server](how-to-use-mcps.md) instead. It works the same on
both systems.

## Part 2: make your own skill

We will make a skill called **meeting-notes**. When you paste rough notes from a meeting,
Pieni will turn them into a summary, decisions and action items.

A skill is a folder with one file inside, named exactly `SKILL.md`. The folder name and the
`name:` line inside the file must be the same. We will create it in the `.agents/skills`
folder inside the Pieni folder, which makes it available when you start Pieni from there.

**Step 1. Create the skill.** Copy the whole block and paste it into your terminal.

Ubuntu:

```bash
mkdir -p .agents/skills/meeting-notes
cat > .agents/skills/meeting-notes/SKILL.md <<'EOF'
---
name: meeting-notes
description: Turn rough meeting notes into a clean summary. Use when the user pastes notes from a meeting or asks for meeting minutes.
---
# Meeting notes

Rewrite the notes as:

1. **Summary**: two sentences.
2. **Decisions**: a bullet list.
3. **Action items**: who does what, and by when if known.

If something is unclear, write "unclear" instead of guessing.
EOF
```

Windows 11 (PowerShell):

```powershell
New-Item -ItemType Directory -Force .agents\skills\meeting-notes | Out-Null
@'
---
name: meeting-notes
description: Turn rough meeting notes into a clean summary. Use when the user pastes notes from a meeting or asks for meeting minutes.
---
# Meeting notes

Rewrite the notes as:

1. **Summary**: two sentences.
2. **Decisions**: a bullet list.
3. **Action items**: who does what, and by when if known.

If something is unclear, write "unclear" instead of guessing.
'@ | Set-Content -Encoding utf8 .agents\skills\meeting-notes\SKILL.md
```

**Step 2. Start Pieni again.** Pieni reads skills only when it starts, so a new or changed
skill needs a restart. Type `/quit` if it is running, then start Pieni as before.

**Step 3. Check that Pieni sees it.** Type:

```text
/skills
```

Your skill should be in the list:
`meeting-notes: Turn rough meeting notes into a clean summary. ...`

**Step 4. Try it.** Paste this as one message:

```text
Here are my notes from today: Talked with Anna about the website. Launch is on Friday. Pekka writes the texts, Anna does the photos. Budget still unclear.
```

The model should read your skill and answer with a summary, decisions and action items,
and write "unclear" for the budget. If it answers in a different style, try again with
`Use the meeting-notes skill.` at the start of your message.

**Step 5. Change it.** Open `.agents/skills/meeting-notes/SKILL.md` in any text editor
(Text Editor on Ubuntu, Notepad on Windows), change the instructions, save, and restart
Pieni. For example, ask for the action items as a table.

### The rules for SKILL.md

- It starts with a line containing only `---`, then the `name:` and `description:` lines, then
  another line with only `---`. After that you can write ordinary text.
- `name` uses only small letters, numbers and hyphens (`meeting-notes`, not `Meeting Notes`),
  and it must match the folder name.
- `description` is one line, up to 1024 characters. It must say **what the skill does and
  when to use it**. The model decides from this line alone whether to open the skill.
- Files saved by Notepad or PowerShell are fine, even if they start with an invisible byte
  order mark.

## Part 3: keep a skill for all your projects

Skills in a project's `.agents/skills` folder only work when you start Pieni in that
project. Skills in your **home folder** work everywhere. To make `meeting-notes` available
everywhere, move it there:

Ubuntu:

```bash
mkdir -p ~/.agents/skills
mv .agents/skills/meeting-notes ~/.agents/skills/
```

Windows 11 (PowerShell):

```powershell
New-Item -ItemType Directory -Force $HOME\.agents\skills | Out-Null
Move-Item .agents\skills\meeting-notes $HOME\.agents\skills\
```

Restart Pieni and type `/skills`. If a project and your home folder both have a skill with
the same name, the project's version is used.

## Writing good skills

- **One job per skill.** "Meeting notes" is better than "Office helper".
- **Say when to use it.** `Use when the user pastes notes from a meeting` helps the model
  choose. `Helps with notes` does not.
- **Be concrete.** Numbered steps, an example of the result you want, and a rule such as
  "never guess" work well.
- **Keep it short.** The whole file is added to the conversation when the model uses it.
  Put long reference material in a second file and tell the skill to read it when needed.
- **Name the skill if the model ignores it.** Starting your message with
  `Use the meeting-notes skill.` always works better than hoping.

## If something goes wrong

| What you see | What to do |
| --- | --- |
| `/skills` says `no skills found` | You started Pieni in the wrong folder. Skills are looked up in the folder where you start Pieni (`.agents/skills`) and in your home folder (`~/.agents/skills` on Ubuntu, `C:\Users\you\.agents\skills` on Windows). |
| `warning: skipped skill ...: name 'x' must be lowercase-with-hyphens and match its folder` | Make the folder name and the `name:` line identical, using small letters and hyphens only. |
| `warning: skipped skill ...: missing --- frontmatter` | The file must begin with a line containing only `---`, and the first block must end with another one. |
| `warning: skipped skill ...: description must be 1-1024 characters` | Add a `description:` line, on a single line. |
| The skill is listed but the model does not use it | Start your message with `Use the NAME skill.` and make the `description:` say when to use it. |
| You changed a skill and nothing changed | Type `/quit` and start Pieni again. Skills are read at startup. |

## Safety

A skill is a set of instructions that the model follows, and the model can run commands on
your computer. Only use skills you wrote or have read and trust. A skill that came with
a project you downloaded can steer Pieni just like text you typed yourself. In `auto`
mode, Pieni still asks before commands that look destructive, but that check is only a
safety net.

To remove a skill, delete its folder (for example `.agents/skills/meeting-notes`) and restart Pieni.

## More

- Add tools, not just instructions: [How to use MCP servers](how-to-use-mcps.md).
- How skills work inside agents in general: [How Pieni works, Skills](how-pieni-works.md#skills-instructions-loaded-on-demand).
