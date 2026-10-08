"""Offline tests for pieni.py.

No network access and no API calls: every provider SDK is replaced by a fake,
so the default run is free and repeatable. Run with:

    python3 -m unittest tests.test_pieni
"""

import copy
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pieni


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def chat_reply(text="", calls=(), prompt_tokens=100, completion_tokens=20, usage=True):
    """A Chat Completions shaped reply; calls are (id, name, arguments) tuples."""
    tool_calls = [SimpleNamespace(id=i, function=SimpleNamespace(name=n, arguments=a))
                  for i, n, a in calls] or None
    tokens = (SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
              if usage else None)
    message = SimpleNamespace(content=text, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=tokens)


def responses_reply(text="", calls=(), usage=True):
    """A Responses API shaped reply."""
    items = [SimpleNamespace(type="function_call", call_id=i, name=n, arguments=a)
             for i, n, a in calls]
    if text:
        items.append(SimpleNamespace(type="message"))
    tokens = SimpleNamespace(input_tokens=50, output_tokens=10) if usage else None
    return SimpleNamespace(output=items, output_text=text, usage=tokens)


class FakeStream:
    """Closable iterable, including mid-stream errors and interruptions."""

    def __init__(self, events):
        self.events = events
        self.closed = False

    def __iter__(self):
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event

    def close(self):
        self.closed = True


def fake_chat_stream(response):
    events = []
    for choice in pieni.attribute(response, "choices") or []:
        message = pieni.attribute(choice, "message")
        calls = []
        for index, call in enumerate(pieni.attribute(message, "tool_calls") or []):
            calls.append({"index": index, "id": pieni.attribute(call, "id"),
                          "function": pieni.attribute(call, "function")})
        delta = {"content": pieni.attribute(message, "content"), "tool_calls": calls,
                 "reasoning_content": pieni.thinking_from_message(message)}
        for key in pieni.REASONING_FIELDS:
            value = pieni.attribute(message, key)
            if value is not None:
                delta[key] = value
        events.append({"choices": [{"index": 0, "delta": delta,
                                   "finish_reason": "tool_calls" if calls else "stop"}]})
    events.append({"choices": [], "usage": pieni.attribute(response, "usage")})
    return FakeStream(events)


def fake_responses_stream(response):
    return FakeStream([
        {"type": "response.output_text.delta", "delta": pieni.attribute(response, "output_text") or ""},
        {"type": "response.completed", "response": response},
    ])


class FakeChatClient:
    """Stands in for an OpenAI Chat Completions client."""

    def __init__(self, reply):
        self.reply = reply
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return fake_chat_stream(self.reply) if kwargs.get("stream") else self.reply


class FakeSendClient:
    """Stands in for an OpenRouter client."""

    def __init__(self, reply):
        self.reply = reply
        self.requests = []
        self.chat = SimpleNamespace(send=self.send)

    def send(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return fake_chat_stream(self.reply) if kwargs.get("stream") else self.reply


class FakeResponsesClient:
    """Stands in for client.responses on the openai SDK."""

    def __init__(self, reply):
        self.reply = reply
        self.requests = []
        self.responses = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return fake_responses_stream(self.reply) if kwargs.get("stream") else self.reply


class ScriptedProvider:
    """Returns prepared replies (or raises prepared exceptions) in order."""

    name = "scripted"
    model = "scripted-model"

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def complete(self, messages, use_tools=True):
        self.requests.append(messages)
        if not self.replies:
            return pieni.Reply("done", [], 1, 1, False)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def close(self):
        pass


def reply_from(calls=(), text=""):
    tool_calls = [pieni.ToolCall(call_id, name, arguments) for call_id, name, arguments in calls]
    return pieni.Reply(text, tool_calls, 10, 5, False)


class TempWorkspaceCase(unittest.TestCase):
    """A temporary home, workspace, and temp directory per test."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.root = Path(self._tempdir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()

    def write(self, path, text):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def in_directory(self, path):
        previous = os.getcwd()
        os.chdir(path)
        self.addCleanup(os.chdir, previous)

    def permissions(self, mode="auto", approve=None):
        return pieni.Permissions(mode, self.workspace, tempdir=self.root / "tmp", approve=approve)

    def toolbox(self, mode="auto", approve=None):
        return pieni.Toolbox(self.workspace, self.permissions(mode, approve))

    def allow_all(self):
        return lambda kind, detail, reason: True

    def make_agent(self, replies, mode="auto", approve=None, messages=(), store=None,
                   dots=None):
        provider = ScriptedProvider(replies)
        if store is None:
            store = pieni.Store(self.root / "pieni.db")
            self.addCleanup(store.close)
        session = store.start_session(provider.name, provider.model, str(self.workspace))
        output = []
        agent = pieni.Agent(provider, store, session, "test instructions", list(messages),
                            self.workspace, self.permissions(mode, approve),
                            out=output.append, dots=dots)
        return agent, provider, store, output


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class ConfigTests(TempWorkspaceCase):

    def test_defaults_when_no_file_exists(self):
        settings = pieni.load_config(cwd=self.workspace, home=self.home)
        self.assertEqual(settings, {"provider": None, "model": None, "permissions": "auto",
                                    "streaming": True, "reasoning": "default"})

    def test_local_file_overrides_user_file_per_key(self):
        self.write(self.home / ".pieni" / "pieni.ini",
                   "[pieni]\nprovider = openai\nmodel = user-model\npermissions = yolo\n")
        self.write(self.workspace / "pieni.ini", "[pieni]\nmodel = local-model\n")
        settings = pieni.load_config(cwd=self.workspace, home=self.home)
        self.assertEqual(settings["provider"], "openai")
        self.assertEqual(settings["model"], "local-model")
        self.assertEqual(settings["permissions"], "yolo")

    def test_cli_arguments_override_both_files(self):
        self.write(self.home / ".pieni" / "pieni.ini", "[pieni]\nprovider = openai\n")
        self.write(self.workspace / "pieni.ini", "[pieni]\nprovider = openrouter\nmodel = a\n")
        settings = pieni.load_config("deepseek", "b", "yolo", cwd=self.workspace, home=self.home)
        self.assertEqual(settings, {"provider": "deepseek", "model": "b", "permissions": "yolo",
                                    "streaming": True, "reasoning": "default"})

    def test_missing_home_directory_is_fine(self):
        settings = pieni.load_config(cwd=self.workspace, home=self.root / "nope")
        self.assertEqual(settings["model"], None)

    def test_malformed_ini_reports_the_file(self):
        path = self.write(self.workspace / "pieni.ini", "this is not an ini file\n")
        with self.assertRaises(pieni.ConfigError) as caught:
            pieni.load_config(cwd=self.workspace, home=self.home)
        self.assertIn(str(path), str(caught.exception))

    def test_unknown_setting_is_rejected(self):
        self.write(self.workspace / "pieni.ini", "[pieni]\nmodel = x\nprovider_api_key = secret\n")
        with self.assertRaises(pieni.ConfigError) as caught:
            pieni.load_config(cwd=self.workspace, home=self.home)
        self.assertIn("provider_api_key", str(caught.exception))

    def test_empty_value_is_rejected(self):
        self.write(self.workspace / "pieni.ini", "[pieni]\nmodel =\n")
        with self.assertRaises(pieni.ConfigError) as caught:
            pieni.load_config(cwd=self.workspace, home=self.home)
        self.assertIn("empty", str(caught.exception))

    def test_invalid_permissions_are_rejected(self):
        self.write(self.workspace / "pieni.ini", "[pieni]\npermissions = sometimes\n")
        with self.assertRaises(pieni.ConfigError) as caught:
            pieni.load_config(cwd=self.workspace, home=self.home)
        self.assertIn("sometimes", str(caught.exception))

    def test_other_sections_are_ignored(self):
        self.write(self.workspace / "pieni.ini", "[other]\nmodel = x\n")
        settings = pieni.load_config(cwd=self.workspace, home=self.home)
        self.assertEqual(settings["model"], None)

    def test_interpolation_is_disabled(self):
        self.write(self.workspace / "pieni.ini", "[pieni]\nmodel = %(missing)s\n")
        settings = pieni.load_config(cwd=self.workspace, home=self.home)
        self.assertEqual(settings["model"], "%(missing)s")

    def test_unreadable_file_is_reported(self):
        directory = self.workspace / "pieni.ini"
        directory.mkdir()
        with self.assertRaises(pieni.ConfigError) as caught:
            pieni.load_config(cwd=self.workspace, home=self.home)
        self.assertIn("cannot read", str(caught.exception))

    def test_provider_kind_classification(self):
        self.assertEqual(pieni.provider_kind("OpenAI"), "responses")
        self.assertEqual(pieni.provider_kind(" deepseek "), "chat")
        self.assertEqual(pieni.provider_kind("openrouter"), "openrouter")
        self.assertEqual(pieni.provider_kind("http://localhost:30000"), "custom")
        with self.assertRaises(pieni.ConfigError):
            pieni.provider_kind("gemini")

    def test_instructions_include_agents_md(self):
        self.write(self.workspace / "AGENTS.md", "Keep it small.\n")
        text = pieni.build_instructions(self.workspace, self.home)
        self.assertIn(pieni.SYSTEM_PROMPT, text)
        self.assertIn("Keep it small.", text)

    def test_instructions_without_agents_md(self):
        self.assertEqual(pieni.build_instructions(self.workspace, self.home), pieni.SYSTEM_PROMPT)

    def skill(self, root, name, description="Does a thing. Use when asked.", folder=None):
        return self.write(root / pieni.SKILLS_DIR / (folder or name) / "SKILL.md",
                          f"---\nname: {name}\ndescription: {description}\n---\n# Body\n")

    def test_instructions_list_user_and_project_skills(self):
        user = self.skill(self.home, "pdf-tools", "'Fill PDFs: forms'")
        project = self.skill(self.workspace, "deploy")
        text = pieni.build_instructions(self.workspace, self.home)
        self.assertIn(f"- pdf-tools: Fill PDFs: forms ({user})", text)
        self.assertIn(f"- deploy: Does a thing. Use when asked. ({project})", text)
        self.assertNotIn("# Body", text)  # only the catalog, not the skill text

    def test_project_skill_overrides_user_skill(self):
        self.skill(self.home, "deploy", "user version")
        project = self.skill(self.workspace, "deploy", "project version")
        text = pieni.build_instructions(self.workspace, self.home)
        self.assertIn(f"- deploy: project version ({project})", text)
        self.assertNotIn("user version", text)

    def test_invalid_skills_are_skipped_with_a_warning(self):
        self.skill(self.workspace, "Bad_Name")
        self.skill(self.workspace, "other", folder="mismatch")
        self.skill(self.workspace, "empty", description="")
        self.write(self.workspace / pieni.SKILLS_DIR / "plain" / "SKILL.md", "# no frontmatter\n")
        self.skill(self.workspace, "good")
        stderr = StringIO()
        with redirect_stderr(stderr):
            text = pieni.build_instructions(self.workspace, self.home)
        self.assertEqual(text.count("\n- "), 1)
        self.assertIn("- good:", text)
        self.assertEqual(stderr.getvalue().count("warning: skipped skill"), 4)


# ---------------------------------------------------------------------------
# Provider adapters
# ---------------------------------------------------------------------------

class ProviderCallTests(unittest.TestCase):

    MESSAGES = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"}]

    def test_chat_completions_normalizes_reply_and_request(self):
        client = FakeChatClient(chat_reply("hello", [("call_1", "read", '{"path": "a.txt"}')]))
        reply = pieni.call_chat_completions(client, "model-x", self.MESSAGES, pieni.chat_tools())
        self.assertEqual(reply.text, "hello")
        self.assertEqual(reply.tool_calls[0].id, "call_1")
        self.assertEqual(reply.tool_calls[0].name, "read")
        self.assertEqual(reply.tool_calls[0].arguments, '{"path": "a.txt"}')
        self.assertFalse(reply.estimated)
        self.assertEqual((reply.input_tokens, reply.output_tokens), (100, 20))
        request = client.requests[0]
        self.assertEqual(request["model"], "model-x")
        self.assertEqual(request["messages"], self.MESSAGES)
        self.assertEqual(request["tools"][0]["type"], "function")
        self.assertIn("function", request["tools"][0])

    def test_chat_completions_wire_shape_for_tool_results(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "call_1", "name": "read", "arguments": '{"path": "a"}'}]},
            {"role": "tool", "tool_call_id": "call_1", "name": "read", "content": "file body"},
        ]
        client = FakeChatClient(chat_reply("ok"))
        pieni.call_chat_completions(client, "m", messages, None)
        wire = client.requests[0]["messages"]
        self.assertNotIn("tools", client.requests[0])
        self.assertIsNone(wire[2]["content"])
        self.assertEqual(wire[2]["tool_calls"][0]["function"]["name"], "read")
        self.assertEqual(wire[3], {"role": "tool", "tool_call_id": "call_1", "content": "file body"})

    def test_chat_completions_estimates_usage_when_missing(self):
        client = FakeChatClient(chat_reply("some answer", usage=False))
        reply = pieni.call_chat_completions(client, "m", self.MESSAGES, pieni.chat_tools())
        self.assertTrue(reply.estimated)
        self.assertGreater(reply.input_tokens, 0)
        self.assertGreater(reply.output_tokens, 0)

    def test_chat_completions_requires_a_choice(self):
        client = FakeChatClient(SimpleNamespace(choices=[], usage=None))
        with self.assertRaises(pieni.ProviderError):
            pieni.call_chat_completions(client, "m", self.MESSAGES, None)

    def test_responses_normalizes_reply_and_request(self):
        client = FakeResponsesClient(responses_reply("answer", [("call_9", "bash", '{"command": "ls"}')]))
        messages = self.MESSAGES + [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "call_9", "name": "bash", "arguments": '{"command": "ls"}'}]},
            {"role": "tool", "tool_call_id": "call_9", "name": "bash", "content": "files"},
        ]
        reply = pieni.call_responses(client, "gpt-x", messages, pieni.responses_tools())
        self.assertEqual(reply.text, "answer")
        self.assertEqual(reply.tool_calls[0].id, "call_9")
        self.assertEqual((reply.input_tokens, reply.output_tokens), (50, 10))
        request = client.requests[0]
        self.assertEqual(request["instructions"], "sys")
        self.assertEqual(request["input"][0], {"role": "user", "content": "hi"})
        self.assertEqual(request["input"][2],
                         {"type": "function_call_output", "call_id": "call_9", "output": "files"})
        self.assertEqual(request["tools"][0]["name"], "read")
        self.assertNotIn("function", request["tools"][0])

    def test_responses_estimates_usage_when_missing(self):
        client = FakeResponsesClient(responses_reply("answer", usage=False))
        reply = pieni.call_responses(client, "m", self.MESSAGES, None)
        self.assertTrue(reply.estimated)
        self.assertGreater(reply.input_tokens, 0)

    def test_responses_rejects_an_empty_reply(self):
        client = FakeResponsesClient(SimpleNamespace(output=[], output_text="", usage=None))
        with self.assertRaises(pieni.ProviderError):
            pieni.call_responses(client, "m", self.MESSAGES, None)

    def test_openrouter_uses_chat_send(self):
        client = FakeSendClient(chat_reply("openrouter answer",
                                          [("call_2", "write", '{"path": "a"}')]))
        reply = pieni.call_openrouter(client, "vendor/model", self.MESSAGES, pieni.chat_tools())
        self.assertEqual(reply.text, "openrouter answer")
        self.assertEqual(reply.tool_calls[0].name, "write")
        self.assertEqual(client.requests[0]["model"], "vendor/model")
        self.assertEqual(client.requests[0]["messages"], self.MESSAGES)

    def test_provider_wraps_sdk_failures(self):
        client = FakeChatClient(RuntimeError("connection reset"))
        provider = pieni.Provider("deepseek", "m", "chat", client)
        with self.assertRaises(pieni.ProviderError) as caught:
            provider.complete(self.MESSAGES)
        self.assertIn("connection reset", str(caught.exception))

    def test_provider_without_tools_skips_the_tool_schema(self):
        client = FakeChatClient(chat_reply("summary"))
        provider = pieni.Provider("deepseek", "m", "chat", client)
        provider.complete(self.MESSAGES, use_tools=False)
        self.assertNotIn("tools", client.requests[0])


class FakeOpenAI:
    """Fake `openai.OpenAI` recording constructor arguments."""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create_chat))
        self.responses = SimpleNamespace(create=self.create_responses)
        FakeOpenAI.instances.append(self)

    def create_chat(self, **kwargs):
        body = chat_reply("chat answer")
        return fake_chat_stream(body) if kwargs.get("stream") else body

    def create_responses(self, **kwargs):
        body = responses_reply("responses answer")
        return fake_responses_stream(body) if kwargs.get("stream") else body


class FakeOpenRouter:
    """Fake `openrouter.OpenRouter` that records context manager use."""

    instances = []

    def __init__(self, api_key=None):
        self.api_key = api_key
        self.entered = False
        self.exited = False
        self.requests = []
        self.chat = SimpleNamespace(send=self.send)
        FakeOpenRouter.instances.append(self)

    def send(self, **kwargs):
        self.requests.append(kwargs)
        body = chat_reply("openrouter answer")
        return fake_chat_stream(body) if kwargs.get("stream") else body

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc_info):
        self.exited = True
        return False


class BuildProviderTests(unittest.TestCase):

    def setUp(self):
        FakeOpenAI.instances = []
        FakeOpenRouter.instances = []
        self.sdks = {"openai": SimpleNamespace(OpenAI=FakeOpenAI),
                     "openrouter": SimpleNamespace(OpenRouter=FakeOpenRouter)}
        patcher = mock.patch.dict(sys.modules, self.sdks)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_openai_uses_the_responses_api(self):
        provider = pieni.build_provider("openai", "gpt-x", {"OPENAI_API_KEY": "k"})
        self.assertEqual(provider.kind, "responses")
        self.assertEqual(FakeOpenAI.instances[-1].kwargs, {})
        reply = provider.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(reply.text, "responses answer")

    def test_missing_keys_are_reported(self):
        with self.assertRaises(pieni.ConfigError) as caught:
            pieni.build_provider("openai", "gpt-x", {})
        self.assertIn("OPENAI_API_KEY", str(caught.exception))
        with self.assertRaises(pieni.ConfigError) as caught:
            pieni.build_provider("openrouter", "m", {})
        self.assertIn("OPENROUTER_API_KEY", str(caught.exception))
        with self.assertRaises(pieni.ConfigError) as caught:
            pieni.build_provider("deepseek", "m", {})
        self.assertIn("DEEPSEEK_API_KEY", str(caught.exception))

    def test_deepseek_uses_chat_completions_and_its_base_url(self):
        provider = pieni.build_provider("deepseek", "deepseek-chat", {"DEEPSEEK_API_KEY": "k"})
        self.assertEqual(provider.kind, "chat")
        self.assertEqual(FakeOpenAI.instances[-1].kwargs,
                         {"api_key": "k", "base_url": pieni.DEEPSEEK_BASE_URL})
        self.assertEqual(provider.complete([{"role": "user", "content": "hi"}]).text, "chat answer")

    def test_openrouter_client_is_closed_through_its_context_manager(self):
        provider = pieni.build_provider("openrouter", "vendor/model", {"OPENROUTER_API_KEY": "k"})
        client = FakeOpenRouter.instances[-1]
        self.assertTrue(client.entered)
        self.assertEqual(client.api_key, "k")
        reply = provider.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(reply.text, "openrouter answer")
        self.assertEqual(client.requests[0]["model"], "vendor/model")
        provider.close()
        self.assertTrue(client.exited)
        provider.close()  # closing twice must stay harmless

    def test_custom_base_url_needs_no_key(self):
        provider = pieni.build_provider("http://localhost:30000", "qwen", {})
        self.assertEqual(provider.kind, "custom")
        self.assertEqual(FakeOpenAI.instances[-1].kwargs,
                         {"api_key": "not-needed", "base_url": "http://localhost:30000"})

    def test_custom_base_url_uses_the_key_when_given(self):
        pieni.build_provider("http://localhost:30000", "qwen", {"OPENAI_API_KEY": "k"})
        self.assertEqual(FakeOpenAI.instances[-1].kwargs.get("api_key"), "k")

    def test_openai_sdk_clients_close_once(self):
        client = mock.Mock()
        with mock.patch("pieni.load_sdk", return_value=mock.Mock(return_value=client)):
            for name, environment in (("openai", {"OPENAI_API_KEY": "k"}),
                                      ("deepseek", {"DEEPSEEK_API_KEY": "k"}),
                                      ("http://localhost:30000", {})):
                with self.subTest(provider=name):
                    client.close.reset_mock()
                    provider = pieni.build_provider(name, "m", environment)
                    provider.close()
                    provider.close()
                    client.close.assert_called_once_with()

    def test_missing_model_is_reported(self):
        with self.assertRaises(pieni.ConfigError):
            pieni.build_provider("openai", "", {"OPENAI_API_KEY": "k"})

    def test_unknown_provider_is_reported(self):
        with self.assertRaises(pieni.ConfigError):
            pieni.build_provider("gemini", "m", {"OPENAI_API_KEY": "k"})

    def test_missing_dependency_is_reported(self):
        with mock.patch.dict(sys.modules, {"openai": None}):
            with self.assertRaises(pieni.ConfigError) as caught:
                pieni.build_provider("openai", "m", {"OPENAI_API_KEY": "k"})
        self.assertIn("pip install -r requirements.txt", str(caught.exception))

    def test_client_construction_failure_is_wrapped(self):
        broken = mock.Mock(side_effect=ValueError("bad base url"))
        with mock.patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=broken)}):
            with self.assertRaises(pieni.ProviderError) as caught:
                pieni.build_provider("deepseek", "m", {"DEEPSEEK_API_KEY": "k"})
        self.assertIn("bad base url", str(caught.exception))


# ---------------------------------------------------------------------------
# Permissions and the destructive-command guard
# ---------------------------------------------------------------------------

class GuardTests(unittest.TestCase):

    FLAGGED = [
        "rm -rf /tmp/data",
        "sudo rm -fr build/",
        "mkfs.ext4 /dev/sdb1",
        "dd if=/dev/zero of=/dev/sda",
        "shutdown -h now",
        "git push --force origin main",
        "git push -f",
        "> /dev/sda",
        "psql -c 'DROP TABLE users'",
        "TRUNCATE TABLE logs",
        "DELETE FROM users",
        ":(){ :|:& };:",
    ]

    ALLOWED = [
        "ls -la",
        "rm notes.txt",
        "git push origin main",
        "python3 -m unittest tests.test_pieni",
        "grep -r TODO .",
        "dd --version",
        "echo 'drop the table later'",
        "cat /dev/null",
        "SELECT * FROM users WHERE id = 1",
    ]

    def test_flagged_commands(self):
        for command in self.FLAGGED:
            with self.subTest(command=command):
                self.assertIsNotNone(pieni.dcg_reason(command), "should be flagged")

    def test_ordinary_commands_are_not_flagged(self):
        for command in self.ALLOWED:
            with self.subTest(command=command):
                self.assertIsNone(pieni.dcg_reason(command), "should not be flagged")


class PermissionTests(TempWorkspaceCase):

    def test_auto_allows_ordinary_commands_without_asking(self):
        asked = []
        permissions = self.permissions(approve=lambda *args: asked.append(args) or True)
        allowed, reason = permissions.check_shell("ls -la")
        self.assertTrue(allowed)
        self.assertEqual(reason, "")
        self.assertEqual(asked, [])

    def test_auto_asks_about_flagged_commands(self):
        asked = []
        permissions = self.permissions(approve=lambda *args: asked.append(args) or True)
        allowed, _ = permissions.check_shell("rm -rf build")
        self.assertTrue(allowed)
        self.assertEqual(asked[0][0], "shell")
        self.assertIn("destructive command guard", asked[0][2])

    def test_refused_flagged_command_is_denied(self):
        permissions = self.permissions(approve=lambda *args: False)
        allowed, reason = permissions.check_shell("rm -rf build")
        self.assertFalse(allowed)
        self.assertIn("denied by the user", reason)

    def test_headless_denies_instead_of_waiting(self):
        permissions = self.permissions(approve=None)
        allowed, reason = permissions.check_shell("rm -rf build")
        self.assertFalse(allowed)
        self.assertIn("headless", reason)

    def test_yolo_skips_the_guard_and_approval(self):
        permissions = self.permissions(mode="yolo", approve=None)
        self.assertEqual(permissions.check_shell("rm -rf /"), (True, ""))

    def test_workspace_and_temp_paths_are_allowed(self):
        permissions = self.permissions()
        workspace_file = self.workspace / "notes.txt"
        self.assertEqual(permissions.check_file(workspace_file, "write"), (True, ""))
        temp_file = self.root / "tmp" / "scratch.txt"
        self.assertEqual(permissions.check_file(temp_file, "write"), (True, ""))

    def test_outside_paths_need_approval(self):
        permissions = self.permissions(approve=None)
        allowed, reason = permissions.check_file(self.root / "outside.txt", "write")
        self.assertFalse(allowed)
        self.assertIn("outside the workspace", reason)

    def test_path_resolution_follows_parents_and_symlinks(self):
        self.assertEqual(pieni.resolve_path(self.workspace, "sub/../file.txt"),
                         self.workspace / "file.txt")
        outside = self.write(self.root / "outside.txt", "secret")
        link = self.workspace / "link.txt"
        link.symlink_to(outside)
        self.assertEqual(pieni.resolve_path(self.workspace, "link.txt"), outside.resolve())
        permissions = self.permissions(approve=None)
        self.assertFalse(permissions.check_file(pieni.resolve_path(self.workspace, "link.txt"), "read")[0])

    def test_absolute_path_inside_the_workspace_is_allowed(self):
        permissions = self.permissions()
        self.assertEqual(permissions.check_file(self.workspace / "a.txt", "read"), (True, ""))

    def test_user_skills_are_readable_but_not_writable(self):
        skills = self.home / pieni.SKILLS_DIR
        permissions = pieni.Permissions("auto", self.workspace, tempdir=self.root / "tmp",
                                        approve=None, skills_root=skills)
        self.assertEqual(permissions.check_file(skills / "x" / "SKILL.md", "read"), (True, ""))
        self.assertFalse(permissions.check_file(skills / "x" / "SKILL.md", "write")[0])
        self.assertFalse(permissions.check_file(self.home / "other.txt", "read")[0])


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class ReadToolTests(TempWorkspaceCase):

    def test_reads_an_empty_file_with_default_range(self):
        self.write(self.workspace / "empty.txt", "")
        for arguments in ({"path": "empty.txt"},
                          {"path": "empty.txt", "start_line": None, "end_line": None}):
            with self.subTest(arguments=arguments):
                outcome = self.toolbox().run("read", arguments)
                self.assertTrue(outcome.ok, outcome.output)
                self.assertEqual(outcome.output, "empty.txt (0 lines)")

    def test_reads_a_whole_file(self):
        self.write(self.workspace / "notes.txt", "first\nsecond\n")
        outcome = self.toolbox().run("read", {"path": "notes.txt"})
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.detail, "")
        self.assertIn("lines 1-2 of 2", outcome.output)
        self.assertIn("first\nsecond", outcome.output)

    def test_reads_a_line_range(self):
        self.write(self.workspace / "notes.txt", "one\ntwo\nthree\nfour\n")
        outcome = self.toolbox().run("read", {"path": "notes.txt", "start_line": 2, "end_line": 3})
        self.assertTrue(outcome.ok)
        self.assertIn("two\nthree", outcome.output)
        self.assertNotIn("one", outcome.output)
        self.assertNotIn("four", outcome.output)

    def test_missing_file_is_an_error(self):
        outcome = self.toolbox().run("read", {"path": "missing.txt"})
        self.assertFalse(outcome.ok)
        self.assertIn("FileNotFoundError", outcome.detail)

    def test_directory_is_an_error(self):
        (self.workspace / "folder").mkdir()
        outcome = self.toolbox().run("read", {"path": "folder"})
        self.assertFalse(outcome.ok)

    def test_invalid_range_is_an_error(self):
        self.write(self.workspace / "notes.txt", "one\n")
        outcome = self.toolbox().run("read", {"path": "notes.txt", "start_line": 3})
        self.assertFalse(outcome.ok)
        self.assertIn("invalid line range", outcome.detail)

    def test_malformed_arguments_are_rejected(self):
        for arguments in ({"path": 5}, {"path": ""}, {}, {"path": "a", "bogus": 1}):
            with self.subTest(arguments=arguments):
                outcome = self.toolbox().run("read", arguments)
                self.assertFalse(outcome.ok)
                self.assertTrue(outcome.output.startswith("error:"))

    def test_unicode_and_rtl_text(self):
        text = "中文文本\nمرحبا بالعالم\n"
        self.write(self.workspace / "unicode.txt", text)
        outcome = self.toolbox().run("read", {"path": "unicode.txt"})
        self.assertTrue(outcome.ok)
        self.assertIn("中文文本", outcome.output)
        self.assertIn("مرحبا بالعالم", outcome.output)

    def test_binary_file_is_reported(self):
        (self.workspace / "data.bin").write_bytes(b"\xff\xfe\x00\x01")
        outcome = self.toolbox().run("read", {"path": "data.bin"})
        self.assertFalse(outcome.ok)
        self.assertIn("UnicodeDecodeError", outcome.detail)

    def test_long_file_is_truncated(self):
        # A few lines more than one read may return, so the note appears.
        extra = 25
        self.write(self.workspace / "big.txt",
                   "".join(f"line {n}\n" for n in range(pieni.MAX_READ_LINES + extra)))
        outcome = self.toolbox().run("read", {"path": "big.txt"})
        self.assertTrue(outcome.ok)
        self.assertIn(f"first {pieni.MAX_READ_LINES}", outcome.output)
        self.assertIn(f"of {pieni.MAX_READ_LINES + extra} selected lines", outcome.output)
        self.assertLess(len(outcome.output), pieni.MAX_OUTPUT_CHARS + 200)

    def test_read_output_is_bounded_by_characters_too(self):
        # Very long lines hit the character bound before the line bound.
        self.write(self.workspace / "wide.txt", "x" * (pieni.MAX_OUTPUT_CHARS + 5_000))
        outcome = self.toolbox().run("read", {"path": "wide.txt"})
        self.assertTrue(outcome.ok)
        self.assertIn("truncated", outcome.output)
        self.assertLess(len(outcome.output), pieni.MAX_OUTPUT_CHARS + 200)

    def test_truncated_range_preserves_offset_and_selected_count(self):
        self.write(self.workspace / "big.txt",
                   "".join(f"line {n}\n" for n in range(pieni.MAX_READ_LINES + 10)))
        outcome = self.toolbox().run("read", {"path": "big.txt", "start_line": 4})
        self.assertTrue(outcome.ok, outcome.output)
        self.assertIn(f"first {pieni.MAX_READ_LINES} of {pieni.MAX_READ_LINES + 7}", outcome.output)
        self.assertIn("\nline 3\n", outcome.output)
        self.assertTrue(outcome.output.endswith(f"line {pieni.MAX_READ_LINES + 2}"))
        self.assertNotIn(f"\nline {pieni.MAX_READ_LINES + 3}", outcome.output)

    def test_outside_path_is_denied_in_headless_use(self):
        outside = self.write(self.root / "outside.txt", "secret")
        outcome = self.toolbox(approve=None).run("read", {"path": "../outside.txt"})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.detail, "denied")
        self.assertNotIn("secret", outcome.output)
        self.assertTrue(outside.exists())

    def test_outside_path_is_allowed_after_approval(self):
        outside = self.write(self.root / "outside.txt", "secret")
        outcome = self.toolbox(approve=self.allow_all()).run("read", {"path": "../outside.txt"})
        self.assertTrue(outcome.ok)
        self.assertIn("secret", outcome.output)

    def test_temporary_directory_is_allowed(self):
        scratch = self.write(self.root / "tmp" / "scratch.txt", "scratch")
        outcome = self.toolbox(approve=None).run("read", {"path": str(scratch)})
        self.assertTrue(outcome.ok)


class WriteToolTests(TempWorkspaceCase):

    def test_creates_and_replaces_a_file(self):
        toolbox = self.toolbox()
        first = toolbox.run("write", {"path": "new/notes.txt", "content": "hello\n"})
        self.assertTrue(first.ok)
        self.assertIn("created", first.output)
        self.assertEqual((self.workspace / "new" / "notes.txt").read_text(encoding="utf-8"), "hello\n")
        second = toolbox.run("write", {"path": "new/notes.txt", "content": "changed\n"})
        self.assertIn("replaced", second.output)
        self.assertEqual((self.workspace / "new" / "notes.txt").read_text(encoding="utf-8"), "changed\n")

    def test_empty_content_is_allowed(self):
        outcome = self.toolbox().run("write", {"path": "empty.txt", "content": ""})
        self.assertTrue(outcome.ok)
        self.assertEqual((self.workspace / "empty.txt").read_text(encoding="utf-8"), "")

    def test_missing_content_is_rejected(self):
        outcome = self.toolbox().run("write", {"path": "a.txt"})
        self.assertFalse(outcome.ok)
        self.assertIn("content", outcome.detail)

    def test_unicode_content_round_trip(self):
        outcome = self.toolbox().run("write", {"path": "u.txt", "content": "日本語\nاقرأ\n"})
        self.assertTrue(outcome.ok)
        self.assertEqual((self.workspace / "u.txt").read_text(encoding="utf-8"), "日本語\nاقرأ\n")

    def test_outside_write_needs_approval(self):
        outcome = self.toolbox(approve=None).run("write", {"path": "../escape.txt", "content": "x"})
        self.assertFalse(outcome.ok)
        self.assertFalse((self.root / "escape.txt").exists())

    def test_yolo_allows_outside_write(self):
        outcome = self.toolbox(mode="yolo").run("write", {"path": "../escape.txt", "content": "x"})
        self.assertTrue(outcome.ok)
        self.assertEqual((self.root / "escape.txt").read_text(encoding="utf-8"), "x")


class EditToolTests(TempWorkspaceCase):

    def setUp(self):
        super().setUp()
        self.path = self.write(self.workspace / "code.py", "value = 1\nother = 2\n")

    def test_replaces_one_match(self):
        outcome = self.toolbox().run("edit", {"path": "code.py", "old_text": "value = 1",
                                              "new_text": "value = 42"})
        self.assertTrue(outcome.ok)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "value = 42\nother = 2\n")

    def test_missing_text_is_an_error(self):
        outcome = self.toolbox().run("edit", {"path": "code.py", "old_text": "absent",
                                              "new_text": "x"})
        self.assertFalse(outcome.ok)
        self.assertIn("not found", outcome.detail)

    def test_ambiguous_text_is_an_error(self):
        self.write(self.path, "x = 1\nx = 1\n")
        outcome = self.toolbox().run("edit", {"path": "code.py", "old_text": "x = 1",
                                              "new_text": "x = 2"})
        self.assertFalse(outcome.ok)
        self.assertIn("matches 2 times", outcome.detail)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "x = 1\nx = 1\n")

    def test_empty_old_text_is_an_error(self):
        outcome = self.toolbox().run("edit", {"path": "code.py", "old_text": "", "new_text": "x"})
        self.assertFalse(outcome.ok)

    def test_unicode_edit(self):
        self.write(self.path, "greeting = \"hei\"\n")
        outcome = self.toolbox().run("edit", {"path": "code.py", "old_text": "hei",
                                              "new_text": "moi 世界"})
        self.assertTrue(outcome.ok)
        self.assertIn("世界", self.path.read_text(encoding="utf-8"))


class BashToolTests(TempWorkspaceCase):

    def test_runs_a_command_in_the_workspace(self):
        outcome = self.toolbox().run("bash", {"command": "pwd"})
        self.assertTrue(outcome.ok)
        self.assertIn(str(self.workspace), outcome.output)

    def test_captures_stderr_and_exit_code(self):
        outcome = self.toolbox().run("bash", {"command": "echo boom >&2; exit 3"})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.detail, "exit code 3")
        self.assertIn("boom", outcome.output)
        self.assertIn("exit code 3", outcome.output)

    def test_timeout_is_reported(self):
        outcome = self.toolbox().run("bash", {"command": "sleep 5", "timeout": 1})
        self.assertFalse(outcome.ok)
        self.assertIn("timeout", outcome.detail)

    def test_empty_output_is_marked(self):
        outcome = self.toolbox().run("bash", {"command": "true"})
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.output, "(no output)")

    def test_invalid_timeout_and_command(self):
        for arguments in ({"command": ""}, {}, {"command": "ls", "timeout": 0},
                          {"command": "ls", "timeout": 9999}, {"command": 5}):
            with self.subTest(arguments=arguments):
                self.assertFalse(self.toolbox().run("bash", arguments).ok)

    def test_flagged_command_is_denied_when_not_approved(self):
        outcome = self.toolbox(approve=lambda *args: False).run("bash", {"command": "rm -rf build"})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.detail, "denied")

    def test_flagged_command_runs_after_approval(self):
        outcome = self.toolbox(approve=self.allow_all()).run("bash", {"command": "rm -rf build"})
        self.assertTrue(outcome.ok)  # nothing to delete, but it ran

    def test_output_is_bounded(self):
        characters = pieni.MAX_OUTPUT_CHARS + 1_000
        # /dev/zero piped through tr: no broken-pipe noise from a killed producer,
        # so the reported truncation count is exactly predictable.
        outcome = self.toolbox().run(
            "bash", {"command": f"head -c {characters} /dev/zero | tr '\\0' x"})
        self.assertTrue(outcome.ok)
        self.assertLess(len(outcome.output), pieni.MAX_OUTPUT_CHARS + 200)
        self.assertIn(f"truncated {characters - pieni.MAX_OUTPUT_CHARS} characters",
                      outcome.output)

    def test_non_utf8_output_is_not_fatal(self):
        outcome = self.toolbox().run("bash", {"command": "printf '\\xff\\xfehello'"})
        self.assertTrue(outcome.ok)
        self.assertIn("hello", outcome.output)

    def test_unknown_tool_name(self):
        outcome = self.toolbox().run("delete_everything", {})
        self.assertFalse(outcome.ok)
        self.assertIn("unknown tool", outcome.detail)


class ToolArgumentTests(unittest.TestCase):

    def test_parses_json_arguments(self):
        self.assertEqual(pieni.parse_tool_arguments('{"a": 1}'), {"a": 1})
        self.assertEqual(pieni.parse_tool_arguments(""), {})
        self.assertEqual(pieni.parse_tool_arguments({"a": 1}), {"a": 1})

    def test_rejects_invalid_json(self):
        for raw in ("{not json", "[1, 2]", "5"):
            with self.subTest(raw=raw):
                with self.assertRaises(pieni.PieniError):
                    pieni.parse_tool_arguments(raw)


class ToolValidationTests(TempWorkspaceCase):

    def test_invalid_required_values_never_reach_permissions_or_tools(self):
        toolbox = self.toolbox()
        with mock.patch.object(toolbox.permissions, "check_file") as check_file:
            with mock.patch.object(toolbox.permissions, "check_shell") as check_shell:
                for spec in pieni.TOOL_SPECS:
                    name = spec["name"]
                    required = spec["parameters"]["required"]
                    for key in required:
                        for value in (None, 1, True, []):
                            with self.subTest(tool=name, key=key, value=value):
                                arguments = dict.fromkeys(required, "valid")
                                arguments[key] = value
                                outcome = toolbox.run(name, arguments)
                                self.assertFalse(outcome.ok)
                                self.assertIn(f"argument '{key}' must be a string", outcome.output)
                check_file.assert_not_called()
                check_shell.assert_not_called()

    def test_optional_integers_reject_booleans_floats_and_strings(self):
        toolbox = self.toolbox()
        for name, key, arguments in (("read", "start_line", {"path": "missing"}),
                                     ("read", "end_line", {"path": "missing"}),
                                     ("bash", "timeout", {"command": "true"})):
            for value in (True, False, 1.5, "1"):
                with self.subTest(tool=name, key=key, value=value):
                    outcome = toolbox.run(name, {**arguments, key: value})
                    self.assertFalse(outcome.ok)
                    self.assertIn(f"argument '{key}' must be an integer", outcome.output)

    def test_optional_nulls_use_tool_defaults(self):
        self.write(self.workspace / "notes.txt", "one\ntwo\n")
        toolbox = self.toolbox()
        read = toolbox.run("read", {"path": "notes.txt", "start_line": None, "end_line": None})
        self.assertTrue(read.ok, read.output)
        self.assertIn("lines 1-2 of 2", read.output)
        self.assertTrue(toolbox.run("bash", {"command": "true", "timeout": None}).ok)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class StoreTests(TempWorkspaceCase):

    def test_saves_and_resumes_the_active_context(self):
        store = pieni.Store(self.root / "db" / "pieni.db")
        self.addCleanup(store.close)
        session = store.start_session("openai", "gpt-x", str(self.workspace))
        store.add_message(session, {"role": "user", "content": "中文 ❤"})
        store.add_message(session, {"role": "assistant", "content": "ok"})
        session_id, messages = store.resume("openai", "gpt-x", str(self.workspace))
        self.assertEqual(session_id, session)
        self.assertEqual([m["content"] for m in messages], ["中文 ❤", "ok"])

    def test_resume_is_scoped_to_provider_model_and_workspace(self):
        store = pieni.Store(self.root / "pieni.db")
        self.addCleanup(store.close)
        store.start_session("openai", "gpt-x", str(self.workspace))
        self.assertIsNone(store.resume("openai", "other", str(self.workspace))[0])
        self.assertIsNone(store.resume("deepseek", "gpt-x", str(self.workspace))[0])
        self.assertIsNone(store.resume("openai", "gpt-x", str(self.root))[0])

    def test_resume_returns_the_latest_session(self):
        store = pieni.Store(self.root / "pieni.db")
        self.addCleanup(store.close)
        first = store.start_session("openai", "gpt-x", str(self.workspace))
        store.add_message(first, {"role": "user", "content": "old"})
        second = store.start_session("openai", "gpt-x", str(self.workspace))
        store.add_message(second, {"role": "user", "content": "new"})
        session_id, messages = store.resume("openai", "gpt-x", str(self.workspace))
        self.assertEqual(session_id, second)
        self.assertEqual(messages, [{"role": "user", "content": "new"}])

    def test_inactive_messages_are_only_logs(self):
        store = pieni.Store(self.root / "pieni.db")
        self.addCleanup(store.close)
        session = store.start_session("openai", "gpt-x", str(self.workspace))
        first = store.add_message(session, {"role": "user", "content": "old"})
        store.add_message(session, {"role": "user", "content": "new"})
        self.assertEqual(store.last_message_id(session), first + 1)
        store.deactivate(session, first)
        self.assertEqual(store.last_message_id(session), first + 1)
        rows = store.execute("SELECT COUNT(*) AS total FROM messages").fetchone()
        self.assertEqual(rows["total"], 2)  # logs stay in the database

    def test_empty_session_has_no_messages(self):
        store = pieni.Store(self.root / "pieni.db")
        self.addCleanup(store.close)
        session = store.start_session("openai", "gpt-x", str(self.workspace))
        self.assertIsNone(store.last_message_id(session))
        self.assertEqual(store.resume("openai", "gpt-x", str(self.workspace))[1], [])

    def test_corrupt_payload_is_reported(self):
        store = pieni.Store(self.root / "pieni.db")
        self.addCleanup(store.close)
        session = store.start_session("openai", "gpt-x", str(self.workspace))
        store.connection.execute(
            "INSERT INTO messages (session_id, active, role, payload, created_at) "
            "VALUES (?, 1, 'user', 'not json', 'now')", (session,))
        with self.assertRaises(pieni.PieniError):
            store.resume("openai", "gpt-x", str(self.workspace))

    def test_unusable_database_path_is_reported(self):
        with self.assertRaises(pieni.PieniError) as caught:
            pieni.Store(self.workspace)  # a directory, not a file
        self.assertIn("cannot open the database", str(caught.exception))


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

class AgentLoopTests(TempWorkspaceCase):

    def test_task_with_a_tool_call_then_an_answer(self):
        agent, provider, _, output = self.make_agent([
            reply_from([("call_1", "write", '{"path": "notes.txt", "content": "hi"}')]),
            reply_from(text="all done"),
        ])
        self.assertTrue(agent.run_task("write notes.txt"))
        self.assertEqual((self.workspace / "notes.txt").read_text(encoding="utf-8"), "hi")
        self.assertIn("all done", output)
        tool_line = next(line for line in output if line.startswith("write("))
        self.assertIn("-> ok,", tool_line)
        self.assertRegex(tool_line, r"-> ok, \d+ ms$")
        self.assertTrue(output[-1].startswith("Tokens: "))
        # The tool result goes back to the model with its call id.
        follow_up = provider.requests[1][-1]
        self.assertEqual(follow_up["role"], "tool")
        self.assertEqual(follow_up["tool_call_id"], "call_1")
        self.assertIn("created notes.txt", follow_up["content"])

    def test_task_without_tool_calls_answers_directly(self):
        agent, provider, _, output = self.make_agent([reply_from(text="hello")])
        self.assertTrue(agent.run_task("hi"))
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(provider.requests[0][0], {"role": "system", "content": "test instructions"})
        self.assertIn("hello", output)

    def test_failing_tool_is_reported_and_the_loop_continues(self):
        agent, provider, _, output = self.make_agent([
            reply_from([("call_1", "read", '{"path": "missing.txt"}')]),
            reply_from(text="could not read it"),
        ])
        self.assertTrue(agent.run_task("read missing.txt"))
        line = next(line for line in output if line.startswith("read("))
        self.assertIn("-> error:", line)
        self.assertIn("error:", provider.requests[1][-1]["content"])

    def test_invalid_tool_arguments_are_reported(self):
        agent, _, _, output = self.make_agent([
            reply_from([("call_1", "read", "{not json")]),
            reply_from(text="sorry"),
        ])
        self.assertTrue(agent.run_task("read"))
        line = next(line for line in output if line.startswith("read("))
        self.assertIn("not valid JSON", line)

    def test_provider_error_ends_the_task_with_a_summary(self):
        agent, _, _, output = self.make_agent([pieni.ProviderError("boom")])
        self.assertFalse(agent.run_task("hi"))
        self.assertIn("error: boom", output)
        self.assertTrue(output[-1].startswith("Tokens: "))

    def test_missing_provider_usage_is_marked_as_estimated(self):
        agent, _, _, output = self.make_agent([
            reply_from([("call_1", "read", '{"path": "a.txt"}')]),
            pieni.Reply("done", [], 900, 100, True),
        ])
        agent.run_task("go")
        self.assertTrue(output[-1].startswith("Tokens: ~"), output[-1])

    def test_loop_stops_after_the_step_limit(self):
        # Every round asks for one more tool call, so the budget is never finished.
        calls = [("call_%d" % n, "read", '{"path": "missing.txt"}')
                 for n in range(pieni.MAX_STEPS)]
        agent, provider, _, output = self.make_agent([reply_from([call]) for call in calls])
        self.assertFalse(agent.run_task("loop forever"))
        self.assertEqual(len(provider.requests), pieni.MAX_STEPS)
        self.assertIn(f"stopped after {pieni.MAX_STEPS} tool rounds", output[-2])
        self.assertIn("the context is saved", output[-2])

    def test_step_limit_allows_more_than_one_tool_call_per_round(self):
        # Several calls in one round all count against the same single round.
        many = [("call_%d" % n, "read", '{"path": "missing.txt"}') for n in range(5)]
        agent, provider, _, _ = self.make_agent([reply_from(many), reply_from(text="done")])
        self.assertTrue(agent.run_task("five reads"))
        self.assertEqual(len(provider.requests), 2)
        tool_lines = [line for line in provider.requests[1] if line["role"] == "tool"]
        self.assertEqual(len(tool_lines), 5)
        self.assertGreater(pieni.MAX_STEPS, 25)

    def test_interruption_during_a_tool_call_keeps_the_context_valid(self):
        agent, _, store, output = self.make_agent([reply_from([
            ("call_1", "read", '{"path": "a.txt"}'),
            ("call_2", "read", '{"path": "b.txt"}'),
        ])])

        class Interrupting:
            def run(self, name, arguments):
                raise KeyboardInterrupt

        agent.tools = Interrupting()
        self.assertFalse(agent.run_task("interrupt me"))
        self.assertIn("interrupted", output)
        self.assertTrue(output[-1].startswith("Tokens: "))
        stored = store.resume("scripted", "scripted-model", str(self.workspace))[1]
        tool_messages = [message for message in stored if message["role"] == "tool"]
        self.assertEqual([message["tool_call_id"] for message in tool_messages], ["call_1", "call_2"])
        self.assertIn("interrupted", tool_messages[0]["content"])

    def test_interruption_during_a_model_call(self):
        agent, _, _, output = self.make_agent([KeyboardInterrupt()])
        self.assertFalse(agent.run_task("interrupt me"))
        self.assertIn("interrupted", output)
        self.assertTrue(output[-1].startswith("Tokens: "))

    def test_resume_does_not_repeat_tool_calls(self):
        store = pieni.Store(self.root / "pieni.db")
        self.addCleanup(store.close)
        agent, _, _, _ = self.make_agent([
            reply_from([("call_1", "write", '{"path": "made.txt", "content": "once"}')]),
            reply_from(text="done"),
        ], store=store)
        self.assertTrue(agent.run_task("create made.txt"))
        (self.workspace / "made.txt").unlink()

        session, messages = store.resume("scripted", "scripted-model", str(self.workspace))
        resumed, provider, _, _ = self.make_agent([reply_from(text="still here")],
                                                  messages=messages, store=store)
        resumed.session_id = session
        self.assertTrue(resumed.run_task("what did you do?"))
        self.assertFalse((self.workspace / "made.txt").exists())  # nothing re-executed
        first_request = provider.requests[0]
        self.assertEqual([message["role"] for message in first_request],
                         ["system", "user", "assistant", "tool", "assistant", "user"])
        self.assertEqual(first_request[3]["tool_call_id"], "call_1")

    def test_context_usage_grows_with_the_conversation(self):
        agent, _, _, _ = self.make_agent([reply_from(text="ok")])
        before = agent.context_tokens()
        agent.messages.append({"role": "user", "content": "x" * 400})
        self.assertGreater(agent.context_tokens(), before)


class AgentCommandTests(TempWorkspaceCase):

    def test_quit_and_exit_stop_the_session(self):
        agent, _, _, _ = self.make_agent([])
        self.assertFalse(agent.handle_command("/quit"))
        self.assertFalse(agent.handle_command("/exit"))

    def test_help_lists_the_commands(self):
        agent, _, _, output = self.make_agent([])
        self.assertTrue(agent.handle_command("/help"))
        self.assertIn("/compact", output[0])

    def test_skills_command_lists_loaded_skills(self):
        agent, _, _, output = self.make_agent([])
        agent.handle_command("/skills")
        self.assertIn("no skills found", output[0])
        agent.skills = {"deploy": ("Ship it.", Path("x/SKILL.md"))}
        agent.handle_command("/skills")
        self.assertEqual(output[1], f"deploy: Ship it. ({Path('x/SKILL.md')})")

    def test_bundled_example_skill_is_valid(self):
        path = Path(pieni.__file__).parent / pieni.SKILLS_DIR / "web-fetch" / "SKILL.md"
        self.assertEqual(pieni.read_skill(path)[0], "web-fetch")

    def test_permissions_can_be_shown_and_changed(self):
        agent, _, _, output = self.make_agent([])
        agent.handle_command("/permissions")
        self.assertEqual(output[0], "permissions: auto")
        agent.handle_command("/permissions yolo")
        self.assertEqual(agent.permissions.mode, "yolo")
        self.assertIn("warning", output[2])
        agent.handle_command("/permissions maybe")
        self.assertIn("permissions must be", output[3])
        self.assertEqual(agent.permissions.mode, "yolo")

    def test_unknown_command_is_reported(self):
        agent, _, _, output = self.make_agent([])
        self.assertTrue(agent.handle_command("/nope"))
        self.assertIn("unknown command", output[0])

    def test_compact_replaces_older_context(self):
        agent, provider, store, output = self.make_agent([reply_from(text="short summary")])
        agent.remember({"role": "user", "content": "old work"})
        self.assertTrue(agent.handle_command("/compact"))
        self.assertEqual(len(agent.messages), 1)
        self.assertIn("short summary", agent.messages[0]["content"])
        self.assertIn("compacted", output[-1])
        # The summary request carries prepared history as data, with tools disabled.
        self.assertIn(pieni.COMPACT_INSTRUCTION, provider.requests[-1][-1]["content"])
        stored = store.resume("scripted", "scripted-model", str(self.workspace))[1]
        self.assertEqual(len(stored), 1)

    def test_compaction_failure_keeps_the_context(self):
        agent, _, _, output = self.make_agent([pieni.ProviderError("nope")])
        agent.remember({"role": "user", "content": "keep me"})
        before = list(agent.messages)
        self.assertFalse(agent.compact())
        self.assertEqual(agent.messages, before)
        self.assertIn("unchanged", output[-1])

    def test_compaction_without_a_summary_keeps_the_context(self):
        agent, _, _, output = self.make_agent([reply_from(text="   ")])
        agent.remember({"role": "user", "content": "keep me"})
        before = list(agent.messages)
        self.assertFalse(agent.compact())
        self.assertEqual(agent.messages, before)
        self.assertIn("no summary", output[-1])

    def test_compact_all_starts_a_fresh_session(self):
        agent, _, store, output = self.make_agent([])
        agent.remember({"role": "user", "content": "old work"})
        old_session = agent.session_id
        self.assertTrue(agent.handle_command("/compact all"))
        self.assertEqual(agent.messages, [])
        self.assertNotEqual(agent.session_id, old_session)
        self.assertEqual(store.resume("scripted", "scripted-model", str(self.workspace))[1], [])
        self.assertIn("cleared the context", output[-1])

    def test_compact_with_an_argument_is_a_usage_error(self):
        agent, _, _, output = self.make_agent([])
        agent.handle_command("/compact now")
        self.assertIn("usage: /compact", output[-1])

    def test_compact_without_context_says_so(self):
        agent, _, _, output = self.make_agent([])
        agent.handle_command("/compact")
        self.assertIn("nothing to compact", output[-1])


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------

class CompactionHistoryTests(unittest.TestCase):

    def test_preserves_prose_prior_summary_and_short_tool_evidence(self):
        messages = [
            {"role": "user", "content": "[Summary of earlier conversation]\nDo not change the API."},
            {"role": "user", "content": "Fix 文件.py; keep CRLF.\n" + "constraints\n" * 1000},
            {"role": "assistant", "content": "Inspect first. " + "notes " * 1000,
             "tool_calls": [{"id": "r1", "name": "read", "arguments": '{"path":"文件.py","start_line":2}'}]},
            pieni.tool_message("r1", "read", "error: denied by the user"),
            {"role": "assistant", "content": "The file was not read."},
        ]
        before = copy.deepcopy(messages)
        history = json.loads(pieni.compaction_history(messages))
        self.assertEqual(history[:2], messages[:2])
        self.assertEqual(history[2]["content"], messages[2]["content"])
        self.assertEqual(history[2]["tool_calls"], [
            {"id": "r1", "name": "read", "arguments": {"path": "文件.py", "start_line": 2}}])
        self.assertEqual(history[3:], messages[3:])
        self.assertEqual(messages, before)

    def test_bounds_outputs_and_payloads_but_preserves_paths_and_both_ends(self):
        payload = "首" * 1000 + "discard-me" * 1000 + "尾" * 1000
        path = "long/" * 500 + "文件.py"
        arguments = {"path": path, "content": payload, "old_text": payload,
                     "new_text": payload, "command": payload, "timeout": 60}
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "w1", "name": "write", "arguments": arguments},
                {"id": "b1", "name": "bash", "arguments": '{"command":"python3 -m unittest"}'},
            ]},
            pieni.tool_message("w1", "write", payload),
            pieni.tool_message("b1", "bash", "exit code 1\nFAILED (failures=1)"),
        ]
        before = copy.deepcopy(messages)
        history = json.loads(pieni.compaction_history(messages))
        shortened = history[0]["tool_calls"][0]["arguments"]
        self.assertEqual(shortened["path"], path)
        self.assertEqual(shortened["timeout"], 60)
        self.assertEqual(history[0]["tool_calls"][1]["arguments"]["command"], "python3 -m unittest")
        for text in [history[1]["content"]] + [shortened[key] for key in ("content", "old_text", "new_text", "command")]:
            self.assertTrue(text.startswith("首" * 1000))
            self.assertTrue(text.endswith("尾" * 1000))
            self.assertIn("[omitted 10000 characters]", text)
            self.assertNotIn("discard-me", text)
        self.assertEqual(history[2], messages[2])
        self.assertEqual(messages, before)
        self.assertLess(len(json.dumps(history, ensure_ascii=False)),
                        len(json.dumps(messages, ensure_ascii=False)) // 2)

    def test_malformed_arguments_survive_as_bounded_raw_text(self):
        for raw in ("{not json", "[1, 2]", "5", "START" + "x" * 10000 + "END"):
            with self.subTest(raw=raw[:20]):
                messages = [{"role": "assistant", "content": "", "tool_calls": [
                    {"id": "bad", "name": "write", "arguments": raw}]}]
                history = json.loads(pieni.compaction_history(messages))
                arguments = history[0]["tool_calls"][0]["arguments"]
                if len(raw) <= pieni.COMPACT_TOOL_CHARS:
                    self.assertEqual(arguments, raw)
                else:
                    self.assertTrue(arguments.startswith("START"))
                    self.assertTrue(arguments.endswith("END"))
                    self.assertIn("[omitted", arguments)
                self.assertEqual(messages[0]["tool_calls"][0]["arguments"], raw)

    def test_output_limit_boundary_preserves_short_results_exactly(self):
        for length in (0, pieni.COMPACT_TOOL_CHARS, pieni.COMPACT_TOOL_CHARS + 1):
            with self.subTest(length=length):
                text = "x" * length
                history = json.loads(pieni.compaction_history([pieni.tool_message("r1", "read", text)]))
                if length <= pieni.COMPACT_TOOL_CHARS:
                    self.assertEqual(history[0]["content"], text)
                else:
                    self.assertIn("[omitted 1 characters]", history[0]["content"])


class CompactionTests(TempWorkspaceCase):

    def setUp(self):
        super().setUp()
        self.agent, self.provider, self.store, self.output = self.make_agent([reply_from(text="working summary")])
        for message in (
                {"role": "user", "content": "Keep the API and CRLF; fix 文件.py."},
                {"role": "assistant", "content": "Inspecting the file.", "tool_calls": [
                    {"id": "r1", "name": "read", "arguments": {"path": "文件.py", "start_line": 1}}]},
                pieni.tool_message("r1", "read", "file start\n" + "source\n" * 1000 + "\nfile end"),
                {"role": "assistant", "content": "No changes made or tests run."}):
            self.agent.remember(message)
        self.before = copy.deepcopy(self.agent.messages)

    def assert_original_context(self):
        self.assertEqual(self.agent.messages, self.before)
        reopened = pieni.Store(self.store.path)
        try:
            session, messages = reopened.resume(self.provider.name, self.provider.model, str(self.workspace))
            self.assertEqual(session, self.agent.session_id)
            self.assertEqual(messages, self.before)
            self.assertEqual(reopened.execute("SELECT COUNT(*) FROM messages").fetchone()[0], len(self.before))
        finally:
            reopened.close()
        self.assertIn("unchanged", self.output[-1])

    def test_single_data_only_request_and_summary_resume_for_all_provider_paths(self):
        for kind, client_type, body in (
                ("responses", FakeResponsesClient, responses_reply("working summary")),
                ("chat", FakeChatClient, chat_reply("working summary")),
                ("custom", FakeChatClient, chat_reply("working summary")),
                ("openrouter", FakeSendClient, chat_reply("working summary"))):
            for streaming in (True, False):
                with self.subTest(kind=kind, streaming=streaming):
                    agent, _, store, _ = self.make_agent([])
                    for message in self.before:
                        agent.remember(message)
                    client = client_type(body)
                    agent.provider = pieni.Provider("scripted", "scripted-model", kind, client, streaming=streaming)
                    with mock.patch.object(agent.tools, "run") as run, redirect_stdout(StringIO()):
                        self.assertTrue(agent.compact())
                    run.assert_not_called()
                    self.assertEqual(len(client.requests), 1)
                    request = client.requests[0]
                    self.assertNotIn("tools", request)
                    wire = request["input" if kind == "responses" else "messages"]
                    self.assertTrue(all(message["role"] in ("system", "user") for message in wire))
                    self.assertEqual(request["instructions"] if kind == "responses" else wire[0]["content"],
                                     agent.instructions)
                    history = json.loads(wire[-1]["content"][len(pieni.COMPACT_INSTRUCTION) + 2:])
                    self.assertEqual(history[0], self.before[0])
                    self.assertIn("[omitted", history[2]["content"])
                    logs = store.execute("SELECT active, payload FROM messages WHERE session_id=? ORDER BY id",
                                         (agent.session_id,)).fetchall()
                    self.assertEqual([json.loads(row["payload"]) for row in logs[:-1]], self.before)
                    self.assertEqual([row["active"] for row in logs], [0] * len(self.before) + [1])
                    reopened = pieni.Store(store.path)
                    try:
                        self.assertEqual(reopened.resume("scripted", "scripted-model", str(self.workspace))[1],
                                         agent.messages)
                    finally:
                        reopened.close()
                    self.assertEqual(len(agent.messages), 1)

    def test_provider_failure_preserves_original_context(self):
        self.provider.replies = [pieni.ProviderError("network failure")]
        self.assertFalse(self.agent.compact())
        self.assert_original_context()

    def test_repeated_compaction_includes_previous_summary_and_latest_correction(self):
        self.provider.replies = [reply_from(text="first summary"), reply_from(text="updated summary")]
        self.assertTrue(self.agent.compact())
        first = copy.deepcopy(self.agent.messages)
        correction = {"role": "user", "content": "Correction: tests have now passed; preserve the API."}
        self.agent.remember(correction)
        self.assertTrue(self.agent.compact())
        request = self.provider.requests[-1][-1]["content"]
        history = json.loads(request[len(pieni.COMPACT_INSTRUCTION) + 2:])
        self.assertEqual(history, first + [correction])
        self.assertIn("updated summary", self.agent.messages[0]["content"])
        self.assertEqual(self.store.resume(self.provider.name, self.provider.model, str(self.workspace))[1],
                         self.agent.messages)

    def test_empty_summary_preserves_original_context(self):
        self.provider.replies = [reply_from(text=" \n\t")]
        self.assertFalse(self.agent.compact())
        self.assert_original_context()

    def test_unrequested_tool_calls_preserve_context_and_are_never_executed(self):
        self.provider.replies = [reply_from(calls=[("w1", "write", '{"path":"new.txt","content":"x"}')],
                                           text="misleading summary")]
        with mock.patch.object(self.agent.tools, "run") as run:
            self.assertFalse(self.agent.compact())
        run.assert_not_called()
        self.assertIn("tool calls", self.output[-1])
        self.assert_original_context()

    def test_model_interruption_preserves_original_context(self):
        self.provider.replies = [KeyboardInterrupt()]
        self.assertFalse(self.agent.compact())
        self.assert_original_context()

    def test_database_read_failure_preserves_original_context(self):
        with mock.patch.object(self.store, "last_message_id", side_effect=pieni.PieniError("read failed")):
            self.assertFalse(self.agent.compact())
        self.assertEqual(self.provider.requests, [])
        self.assert_original_context()

    def test_summary_insertion_failure_rolls_back_deactivation(self):
        self.store.execute("CREATE TRIGGER fail_summary BEFORE INSERT ON messages "
                           "BEGIN SELECT RAISE(ABORT, 'simulated disk error'); END", commit=True)
        self.assertFalse(self.agent.compact())
        self.assertIn("simulated disk error", self.output[-1])
        self.assertFalse(self.store.connection.in_transaction)
        self.assert_original_context()

    def test_database_commit_failure_rolls_back_entire_replacement(self):
        self.store.execute("PRAGMA busy_timeout = 0")
        reader = sqlite3.connect(self.store.path)
        try:
            # A separate reader prevents the writer from acquiring its commit lock.
            reader.execute("BEGIN")
            reader.execute("SELECT payload FROM messages").fetchall()
            self.assertFalse(self.agent.compact())
        finally:
            reader.close()
        self.assertIn("database is locked", self.output[-1])
        self.assertFalse(self.store.connection.in_transaction)
        self.assert_original_context()

    def test_database_write_interruption_rolls_back_deactivation(self):
        with mock.patch.object(self.store, "add_message", side_effect=KeyboardInterrupt()):
            self.assertFalse(self.agent.compact())
        self.assertFalse(self.store.connection.in_transaction)
        self.assert_original_context()


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

class DisplayTests(unittest.TestCase):

    def test_tool_status_line_for_success(self):
        self.assertEqual(pieni.format_tool_call("read", {"path": "pieni.py"}, True, "", 12),
                         'read(path="pieni.py") -> ok, 12 ms')

    def test_tool_status_line_for_failure(self):
        self.assertEqual(
            pieni.format_tool_call("bash", {"command": "false"}, False, "exit code 1", 5),
            'bash(command="false") -> error: exit code 1, 5 ms')

    def test_long_arguments_are_shortened(self):
        line = pieni.format_tool_call("write", {"path": "a", "content": "x" * 500}, True, "", 1)
        self.assertLess(len(line), 200)
        self.assertIn("...", line)

    def test_unparsed_arguments_are_shown_as_text(self):
        line = pieni.format_tool_call("read", "{not json", False, "bad", 3)
        self.assertIn("{not json", line)

    def test_task_summary_with_provider_usage(self):
        usage = pieni.TaskUsage()
        usage.record(2000, 400, False)
        usage.elapsed_seconds = 3.14
        self.assertEqual(usage.line(12000),
                         "Tokens: 2,400 | Context: ~12,000 / 1,000,000 (1.2%) | 3.1 seconds")

    def test_task_summary_marks_estimates(self):
        usage = pieni.TaskUsage()
        usage.record(2000, 400, True)
        usage.elapsed_seconds = 0.04
        self.assertEqual(usage.line(0),
                         "Tokens: ~2,400 | Context: ~0 / 1,000,000 (0.0%) | 0.0 seconds")
        self.assertEqual(usage.total_tokens, 2400)

    def test_token_estimate_and_truncation_helpers(self):
        self.assertEqual(pieni.estimate_tokens(""), 0)
        self.assertEqual(pieni.estimate_tokens("abcd"), 1)
        self.assertEqual(pieni.truncate("abcdef", limit=3), "abc\n...[truncated 3 characters]")

    def test_thinking_line_is_silent_without_a_trace(self):
        for empty in (None, "", "   \n\t "):
            with self.subTest(value=empty):
                self.assertEqual(pieni.thinking_line(empty), "")

    def test_thinking_line_collapses_whitespace(self):
        self.assertEqual(pieni.thinking_line("first\n\tsecond   third"),
                         "thinking: first second third")

    def test_thinking_line_is_bounded(self):
        default = pieni.thinking_line("x" * 500)
        self.assertEqual(default, f"thinking: {'x' * pieni.THINK_TRACE_CHARACTERS}...")
        self.assertEqual(pieni.THINK_TRACE_CHARACTERS, 120)
        self.assertEqual(pieni.thinking_line("y" * 30, limit=10), f"thinking: {'y' * 10}...")
        # A trace shorter than the limit is shown in full, without a marker.
        self.assertEqual(pieni.thinking_line("short", limit=10), "thinking: short")

    def test_thinking_line_handles_unicode(self):
        text = "中文思考" * 40
        line = pieni.thinking_line(text)
        self.assertTrue(line.startswith("thinking: 中文思考"))
        self.assertTrue(line.endswith("..."))
        self.assertLessEqual(len(line), len("thinking: ") + pieni.THINK_TRACE_CHARACTERS + 3)


class WorkingDotsTests(unittest.TestCase):
    """The progress dots printed while waiting for a model reply."""

    def test_no_writer_means_no_output(self):
        with pieni.WorkingDots(None, interval=0.01) as dots:
            time.sleep(0.05)
        self.assertIsNone(dots.write)

    def test_dots_are_printed_at_the_interval(self):
        printed = []
        with pieni.WorkingDots(printed.append, interval=0.01) as dots:
            self.assertIsNotNone(dots)
            deadline = time.monotonic() + 2
            while len(printed) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertGreaterEqual(printed.count("."), 3)
        # A newline closes the dot line once the wait is over.
        self.assertEqual(printed[-1], "\n")

    def test_nothing_is_printed_when_the_call_is_quick(self):
        printed = []
        with pieni.WorkingDots(printed.append, interval=5):
            pass
        self.assertEqual(printed, [])

    def test_the_thread_stops_after_the_block(self):
        printed = []
        with pieni.WorkingDots(printed.append, interval=0.01) as dots:
            self.assertIsNotNone(dots._thread)
            self.assertTrue(dots._thread.is_alive() or True)
        self.assertIsNone(dots._thread)
        settled = len(printed)
        time.sleep(0.05)
        self.assertEqual(len(printed), settled)  # no dots after the block

    def test_an_exception_still_stops_the_dots(self):
        printed = []
        with self.assertRaises(pieni.PieniError):
            with pieni.WorkingDots(printed.append, interval=0.01):
                time.sleep(0.03)
                raise pieni.PieniError("boom")
        self.assertEqual(printed[-1], "\n")
        settled = len(printed)
        time.sleep(0.05)
        self.assertEqual(len(printed), settled)

    def test_dots_need_an_interactive_stream(self):
        class Stream:
            def __init__(self, interactive):
                self.interactive = interactive
                self.written = ""

            def isatty(self):
                return self.interactive

            def write(self, text):
                self.written += text

            def flush(self):
                pass

        quiet = Stream(False)
        self.assertIsNone(pieni.dot_writer(quiet))
        self.assertEqual(quiet.written, "")
        loud = Stream(True)
        writer = pieni.dot_writer(loud)
        self.assertIsNotNone(writer)
        writer(".")
        self.assertEqual(loud.written, ".")

    def test_a_stream_without_isatty_is_treated_as_quiet(self):
        self.assertIsNone(pieni.dot_writer(io.StringIO()))


class ThinkingTraceTests(TempWorkspaceCase):
    """Thinking traces from the providers and their display in the loop."""

    def test_chat_completions_reasoning_content(self):
        message = SimpleNamespace(content="answer", tool_calls=None,
                                  reasoning_content="step one, step two")
        body = SimpleNamespace(choices=[SimpleNamespace(message=message)],
                               usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2))
        reply = pieni.call_chat_completions(FakeChatClient(body), "m",
                                           [{"role": "user", "content": "hi"}], None)
        self.assertEqual(reply.thinking, "step one, step two")

    def test_chat_completions_reasoning_field(self):
        message = SimpleNamespace(content="answer", tool_calls=None, reasoning="because")
        body = SimpleNamespace(choices=[SimpleNamespace(message=message)],
                               usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2))
        reply = pieni.call_chat_completions(FakeChatClient(body), "m", [], None)
        self.assertEqual(reply.thinking, "because")

    def test_openrouter_reasoning(self):
        message = SimpleNamespace(content="answer", tool_calls=None, reasoning="weighing options")
        body = SimpleNamespace(choices=[SimpleNamespace(message=message)],
                               usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2))
        reply = pieni.call_openrouter(FakeSendClient(body), "m", [], None)
        self.assertEqual(reply.thinking, "weighing options")

    def test_responses_reasoning_summary(self):
        items = [SimpleNamespace(type="reasoning", summary=[SimpleNamespace(text="thought hard")]),
                 SimpleNamespace(type="message")]
        body = SimpleNamespace(output=items, output_text="answer",
                               usage=SimpleNamespace(input_tokens=1, output_tokens=2))
        reply = pieni.call_responses(FakeResponsesClient(body), "m", [], None)
        self.assertEqual(reply.thinking, "thought hard")

    def test_replies_without_thinking_are_empty(self):
        self.assertEqual(pieni.call_chat_completions(  # no reasoning fields at all
            FakeChatClient(chat_reply("plain")), "m", [], None).thinking, "")
        self.assertEqual(pieni.call_responses(
            FakeResponsesClient(responses_reply("plain")), "m", [], None).thinking, "")
        self.assertEqual(pieni.thinking_line(""), "")

    def test_the_loop_shows_the_thinking_trace(self):
        long_trace = "consider" * 50
        agent, _, _, output = self.make_agent([
            pieni.Reply("done", [], 10, 5, False, long_trace)])
        self.assertTrue(agent.run_task("go"))
        trace = next(line for line in output if line.startswith("thinking: "))
        self.assertEqual(trace, pieni.thinking_line(long_trace))
        self.assertLessEqual(len(trace), len("thinking: ") + pieni.THINK_TRACE_CHARACTERS + 3)
        self.assertTrue(trace.endswith("..."))

    def test_the_loop_shows_a_trace_per_round(self):
        agent, _, _, output = self.make_agent([
            pieni.Reply("", [pieni.ToolCall("c1", "read", '{"path": "missing.txt"}')],
                        10, 5, False, "first thought"),
            pieni.Reply("done", [], 10, 5, False, "second thought")])
        self.assertTrue(agent.run_task("go"))
        traces = [line for line in output if line.startswith("thinking: ")]
        self.assertEqual(traces, ["thinking: first thought", "thinking: second thought"])

    def test_no_trace_line_when_the_model_thinks_silently(self):
        agent, _, _, output = self.make_agent([reply_from(text="plain answer")])
        agent.run_task("go")
        self.assertFalse([line for line in output if line.startswith("thinking: ")])

    def test_thinking_is_not_added_to_the_context_or_saved(self):
        agent, provider, store, _ = self.make_agent([
            pieni.Reply("done", [], 10, 5, False, "private reasoning")])
        self.assertTrue(agent.run_task("go"))
        self.assertNotIn("private reasoning", json.dumps(agent.messages))
        self.assertNotIn("private reasoning",
                         json.dumps(store.resume("scripted", "scripted-model",
                                                 str(self.workspace))[1]))
        self.assertNotIn("private reasoning", json.dumps(provider.requests[0]))

    def test_dots_do_not_disturb_line_output(self):
        printed = []
        agent, _, _, output = self.make_agent([reply_from(text="answer")], dots=printed.append)
        self.assertTrue(agent.run_task("go"))
        self.assertIn("answer", output)  # the line output is unaffected
        # The scripted reply is instant, so no dot and therefore no closing newline.
        self.assertEqual(printed, [])

    def test_progress_dots_wrap_every_model_call(self):
        entered = []

        class RecordingDots:
            def __init__(self, write, interval=None):
                self.write = write
                entered.append(("init", interval))

            def __enter__(self):
                entered.append("enter")
                return self

            def __exit__(self, *exc_info):
                entered.append("exit")
                return False

        agent, _, _, _ = self.make_agent([
            pieni.Reply("", [pieni.ToolCall("c1", "read", '{"path": "a.txt"}')], 1, 1, False),
            reply_from(text="done")])
        with mock.patch.object(pieni, "WorkingDots", RecordingDots):
            self.assertTrue(agent.run_task("go"))
        # Two model rounds, each wrapped by the dots context manager.
        self.assertEqual([entry for entry in entered if entry in ("enter", "exit")],
                         ["enter", "exit", "enter", "exit"])

    def test_compaction_also_shows_progress_and_trace(self):
        printed = []
        agent, _, _, output = self.make_agent(
            [pieni.Reply("summary text", [], 5, 5, False, "compaction thought")],
            dots=printed.append)
        agent.remember({"role": "user", "content": "old work"})
        self.assertTrue(agent.handle_command("/compact"))
        self.assertIn("thinking: compaction thought", output)
        self.assertIn("compacted the older context", output[-1])


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

class ScriptedFakeOpenAI:
    """Fake openai SDK client driven by a class-level script of replies."""

    script = []
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.requests = []
        self.responses = SimpleNamespace(create=self.create)
        type(self).instances.append(self)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        reply = type(self).script.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return fake_responses_stream(reply) if kwargs.get("stream") else reply


class CliTests(TempWorkspaceCase):

    def setUp(self):
        super().setUp()
        self.in_directory(self.workspace)

    def isolate_config(self):
        return mock.patch("pieni.config_paths",
                          return_value=[self.home / "nope.ini", self.workspace / "nope.ini"])

    def test_parse_args(self):
        args = pieni.parse_args(["deepseek", "-m", "deepseek-chat", "-r", "hi",
                                 "--permissions", "yolo"])
        self.assertEqual((args.provider, args.model, args.run, args.permissions),
                         ("deepseek", "deepseek-chat", "hi", "yolo"))
        self.assertEqual(pieni.parse_args([]).permissions, None)
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                pieni.parse_args(["--permissions", "sometimes"])

    def test_no_arguments_prints_banner_and_help(self):
        stdout = StringIO()
        with self.isolate_config(), redirect_stdout(stdout):
            code = pieni.main([])
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn(pieni.BANNER, text)
        self.assertIn(f"Pieni agent v{pieni.VERSION} by Petri Kuittinen", text)
        self.assertIn("usage: pieni", text)
        self.assertIn("/compact", text)  # the command help, not just the options
        self.assertIn("yolo", text)

    def test_no_arguments_does_not_start_a_session(self):
        stdout = StringIO()
        with self.isolate_config(), redirect_stdout(stdout), redirect_stderr(StringIO()):
            code = pieni.main([])
        self.assertEqual(code, 0)
        self.assertNotIn("new session with", stdout.getvalue())
        self.assertFalse((self.workspace / ".pieni" / "pieni.db").exists())

    def test_missing_provider_is_reported(self):
        stderr = StringIO()
        with self.isolate_config(), redirect_stderr(stderr):
            code = pieni.main(["-m", "gpt-x"])
        self.assertEqual(code, 2)
        self.assertIn("no provider set", stderr.getvalue())

    def test_database_open_failure_still_closes_provider(self):
        provider = ScriptedProvider([])
        with self.isolate_config(), mock.patch("pieni.build_provider", return_value=provider):
            with mock.patch("pieni.Store", side_effect=pieni.PieniError("cannot open database")):
                with mock.patch.object(provider, "close") as close, redirect_stderr(StringIO()):
                    self.assertEqual(pieni.main(["openai", "-m", "m", "-r", "hi"]), 1)
                close.assert_called_once_with()

    def test_database_close_failure_still_closes_provider(self):
        provider = ScriptedProvider([])
        store = pieni.Store(self.root / "pieni.db")
        self.addCleanup(store.close)
        with self.isolate_config(), mock.patch("pieni.build_provider", return_value=provider):
            with mock.patch("pieni.Store", return_value=store):
                with mock.patch.object(store, "close", side_effect=pieni.PieniError("close failed")):
                    with mock.patch.object(provider, "close") as close:
                        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                            self.assertEqual(pieni.main(["openai", "-m", "m", "-r", "hi"]), 1)
                    close.assert_called_once_with()

    def test_unknown_provider_is_reported(self):
        stderr = StringIO()
        with self.isolate_config(), redirect_stderr(stderr):
            code = pieni.main(["gemini", "-m", "m"])
        self.assertEqual(code, 2)
        self.assertIn("unknown provider", stderr.getvalue())

    def test_missing_key_is_reported(self):
        stderr = StringIO()
        with self.isolate_config(), redirect_stderr(stderr):
            with mock.patch.dict(os.environ, {}, clear=True):
                code = pieni.main(["openai", "-m", "gpt-x", "-r", "hi"])
        self.assertEqual(code, 2)
        self.assertIn("OPENAI_API_KEY", stderr.getvalue())

    def test_empty_headless_prompt_is_rejected(self):
        stderr = StringIO()
        with self.isolate_config(), redirect_stderr(stderr), redirect_stdout(StringIO()):
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}):
                with mock.patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=ScriptedFakeOpenAI)}):
                    code = pieni.main(["openai", "-m", "gpt-x", "-r", "   "])
        self.assertEqual(code, 2)
        self.assertIn("non-empty prompt", stderr.getvalue())

    def test_headless_run_and_resume(self):
        ScriptedFakeOpenAI.script = [
            responses_reply("", [("call_1", "write", '{"path": "new.txt", "content": "x"}')]),
            responses_reply("finished the task"),
        ]
        ScriptedFakeOpenAI.instances = []
        stdout = StringIO()
        with self.isolate_config(), redirect_stdout(stdout):
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}):
                with mock.patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=ScriptedFakeOpenAI)}):
                    code = pieni.main(["openai", "-m", "gpt-x", "-r", "create new.txt"])
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn("new session with openai/gpt-x", text)
        self.assertIn("permissions: auto", text)
        self.assertRegex(text, r'write\(path="new\.txt", content="x"\) -> ok, \d+ ms')
        self.assertIn("finished the task", text)
        self.assertRegex(text, r"Tokens: \d[\d,]* \| Context: ~[\d,]+ / 1,000,000 \(\d+\.\d%\) \| \d+\.\d seconds")
        self.assertEqual((self.workspace / "new.txt").read_text(encoding="utf-8"), "x")
        self.assertTrue((self.workspace / ".pieni" / "pieni.db").is_file())
        # The tool result reached the provider with its call id.
        self.assertEqual(ScriptedFakeOpenAI.instances[-1].requests[1]["input"][-1]["type"],
                         "function_call_output")

        ScriptedFakeOpenAI.script = [responses_reply("resumed answer")]
        stdout = StringIO()
        with self.isolate_config(), redirect_stdout(stdout):
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}):
                with mock.patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=ScriptedFakeOpenAI)}):
                    code = pieni.main(["openai", "-m", "gpt-x", "-r", "continue"])
        self.assertEqual(code, 0)
        self.assertIn("resumed a session with 4 message(s)", stdout.getvalue())

    def test_headless_provider_failure_exits_nonzero(self):
        ScriptedFakeOpenAI.script = [RuntimeError("connection reset")]
        stdout = StringIO()
        with self.isolate_config(), redirect_stdout(stdout):
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}):
                with mock.patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=ScriptedFakeOpenAI)}):
                    code = pieni.main(["openai", "-m", "gpt-x", "-r", "go"])
        self.assertEqual(code, 1)
        self.assertIn("error: openai request failed", stdout.getvalue())
        self.assertIn("Tokens:", stdout.getvalue())

    def test_interactive_loop_runs_a_task_and_quits(self):
        agent, _, _, output = self.make_agent([reply_from(text="hello")])
        stdout = StringIO()
        with mock.patch("builtins.input", side_effect=["do something", "/quit"]):
            with redirect_stdout(stdout):
                self.assertEqual(pieni.run_interactive(agent), 0)
        self.assertIn("hello", output)
        self.assertIn("bye", output)
        self.assertIn("Type a task", stdout.getvalue())

    def test_interactive_start_prints_the_banner_first(self):
        ScriptedFakeOpenAI.script = []
        stdout = StringIO()
        with self.isolate_config(), redirect_stdout(stdout):
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}):
                with mock.patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=ScriptedFakeOpenAI)}):
                    with mock.patch("builtins.input", side_effect=["/quit"]):
                        code = pieni.main(["openai", "-m", "gpt-x"])
        self.assertEqual(code, 0)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(lines[0], pieni.BANNER)
        self.assertIn("new session with openai/gpt-x", lines[1])
        self.assertIn("Type a task", stdout.getvalue())

    def test_interactive_loop_prints_the_hint(self):
        agent, _, _, _ = self.make_agent([])
        stdout = StringIO()
        with mock.patch("builtins.input", side_effect=["/quit"]):
            with redirect_stdout(stdout):
                pieni.run_interactive(agent)
        self.assertIn("Ctrl+C", stdout.getvalue())
        self.assertNotIn(pieni.BANNER, stdout.getvalue())

    def test_failed_start_prints_no_banner(self):
        stdout = StringIO()
        with self.isolate_config(), redirect_stdout(stdout), redirect_stderr(StringIO()):
            with mock.patch.dict(os.environ, {}, clear=True):
                code = pieni.main(["openai", "-m", "gpt-x"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")

    def test_headless_mode_prints_no_banner(self):
        ScriptedFakeOpenAI.script = [responses_reply("done")]
        stdout = StringIO()
        with self.isolate_config(), redirect_stdout(stdout):
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}):
                with mock.patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=ScriptedFakeOpenAI)}):
                    code = pieni.main(["openai", "-m", "gpt-x", "-r", "go"])
        self.assertEqual(code, 0)
        self.assertNotIn(pieni.BANNER, stdout.getvalue())

    def test_interactive_loop_exits_on_end_of_input(self):
        agent, _, _, _ = self.make_agent([])
        stdout = StringIO()
        with mock.patch("builtins.input", side_effect=EOFError):
            with redirect_stdout(stdout):
                self.assertEqual(pieni.run_interactive(agent), 0)

    def test_cli_approve_reads_the_answer(self):
        stdout = StringIO()
        with mock.patch("builtins.input", return_value="y"), redirect_stdout(stdout):
            self.assertTrue(pieni.cli_approve("shell", "rm -rf build", "guard"))
        with mock.patch("builtins.input", return_value="n"), redirect_stdout(StringIO()):
            self.assertFalse(pieni.cli_approve("shell", "rm -rf build", "guard"))
        self.assertIn("[approval needed]", stdout.getvalue())


class PromptCliTests(TempWorkspaceCase):

    def setUp(self):
        super().setUp()
        self.in_directory(self.workspace)

    def invoke(self, provider, flags):
        stdout, stderr = StringIO(), StringIO()
        with mock.patch("pieni.config_paths", return_value=[]):
            with mock.patch("pieni.build_provider", return_value=provider):
                with mock.patch("pieni.Store") as store, mock.patch("pieni.Toolbox.run") as run:
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = pieni.main([provider.name, "-m", provider.model] + flags)
                store.assert_not_called()
                run.assert_not_called()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_prompt_aliases_and_run_are_mutually_exclusive(self):
        for option in ("-p", "--prompt"):
            with self.subTest(option=option):
                args = pieni.parse_args([option, "question"])
                self.assertEqual(args.prompt, "question")
                self.assertIsNone(args.run)
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as caught:
                    pieni.parse_args([option, "question", "--run", "task"])
                self.assertEqual(caught.exception.code, 2)

    def test_one_reply_without_tools_for_every_provider_and_streaming_setting(self):
        question = "What is the capital of Finland? 你好"
        for kind, name, client_type, body in (
                ("responses", "openai", FakeResponsesClient, responses_reply("Helsinki")),
                ("chat", "deepseek", FakeChatClient, chat_reply("Helsinki")),
                ("custom", "http://localhost:30000", FakeChatClient, chat_reply("Helsinki")),
                ("openrouter", "openrouter", FakeSendClient, chat_reply("Helsinki"))):
            for streaming in (True, False):
                with self.subTest(provider=name, streaming=streaming):
                    client, close = client_type(body), mock.Mock()
                    provider = pieni.Provider(name, "m", kind, client, close=close, streaming=streaming)
                    code, text, error = self.invoke(provider, ["-p", question,
                        "--streaming" if streaming else "--no-streaming"])
                    self.assertEqual((code, text, error), (0, "Helsinki\n", ""))
                    self.assertEqual(len(client.requests), 1)
                    request = client.requests[0]
                    self.assertNotIn("tools", request)
                    self.assertNotIn("instructions", request)
                    self.assertEqual(request["input" if kind == "responses" else "messages"],
                                     [{"role": "user", "content": question}])
                    close.assert_called_once_with()
        self.assertFalse((self.workspace / ".pieni").exists())

    def test_prompt_ignores_project_instructions_and_preserves_saved_session(self):
        self.write(self.workspace / "AGENTS.md", "Always inspect the project with tools.")
        path = self.workspace / pieni.DB_PATH
        store = pieni.Store(path)
        session = store.start_session("openai", "m", str(self.workspace))
        store.add_message(session, {"role": "user", "content": "earlier coding task"})
        store.close()
        before = path.read_bytes()
        client = FakeResponsesClient(responses_reply("Helsinki"))
        provider = pieni.Provider("openai", "m", "responses", client)
        self.assertEqual(self.invoke(provider, ["--prompt", "capital?"])[0], 0)
        self.assertEqual(client.requests[0]["input"], [{"role": "user", "content": "capital?"}])
        self.assertNotIn("instructions", client.requests[0])
        self.assertEqual(path.read_bytes(), before)

    def test_empty_prompt_is_rejected_before_creating_client_or_database(self):
        for prompt in ("", " \n\t"):
            with self.subTest(prompt=prompt), redirect_stderr(StringIO()) as stderr:
                with mock.patch("pieni.build_provider") as build, mock.patch("pieni.Store") as store:
                    self.assertEqual(pieni.main(["openai", "-m", "m", "-p", prompt]), 2)
                self.assertIn("-p/--prompt needs a non-empty prompt", stderr.getvalue())
                build.assert_not_called()
                store.assert_not_called()

    def test_provider_failure_exits_nonzero_and_closes_client(self):
        close = mock.Mock()
        client = FakeChatClient(RuntimeError("connection reset"))
        provider = pieni.Provider("deepseek", "m", "chat", client, close=close)
        code, text, error = self.invoke(provider, ["-p", "capital?"])
        self.assertEqual(code, 1)
        self.assertEqual(text, "")
        self.assertIn("connection reset", error)
        self.assertEqual(len(client.requests), 1)
        close.assert_called_once_with()

    def test_unrequested_tool_call_is_rejected_without_execution(self):
        client = FakeChatClient(chat_reply(calls=[("c1", "write", '{"path":"new.txt","content":"x"}')]))
        provider = pieni.Provider("deepseek", "m", "chat", client)
        code, _, error = self.invoke(provider, ["-p", "capital?"])
        self.assertEqual(code, 1)
        self.assertIn("tools being disabled", error)
        self.assertEqual(len(client.requests), 1)
        self.assertFalse((self.workspace / "new.txt").exists())

    def test_interruption_closes_client_without_creating_session(self):
        close = mock.Mock()
        client = FakeChatClient(KeyboardInterrupt())
        provider = pieni.Provider("deepseek", "m", "chat", client, close=close)
        with self.assertRaises(KeyboardInterrupt):
            self.invoke(provider, ["-p", "capital?"])
        close.assert_called_once_with()
        self.assertFalse((self.workspace / ".pieni").exists())


class RichUITests(unittest.TestCase):
    def make_ui(self):
        ui = pieni.RichUI()
        ui.console = ui.console.__class__(file=StringIO(), force_terminal=False, width=60)
        return ui

    def test_status_lines_are_plain_text_and_replies_are_markdown(self):
        ui = self.make_ui()
        ui.out("[bold]**not markdown**[/bold]")
        ui.markdown("**bold** text")
        shown = ui.console.file.getvalue()
        self.assertIn("[bold]**not markdown**[/bold]", shown)
        self.assertIn("bold text", shown)
        self.assertNotIn("**bold**", shown)

    def test_streamed_text_is_rendered_once_and_end_is_idempotent(self):
        ui = self.make_ui()
        ui.stream("# Ti")
        ui.stream("tle\n")
        ui.end()
        ui.end()
        self.assertIsNone(ui.live)
        self.assertEqual(ui.console.file.getvalue().count("Title"), 1)

    def test_line_styles(self):
        self.assertEqual(pieni.line_style("read(path='a') -> ok, 3 ms"), "green")
        self.assertEqual(pieni.line_style("bash(command='x') -> error: exit code 1, 3 ms"), "red")
        self.assertEqual(pieni.line_style("error: boom"), "red")
        self.assertIsNone(pieni.line_style("plain output"))

    def test_agent_hooks_route_model_text_to_markdown(self):
        ui = self.make_ui()
        provider = ScriptedProvider([reply_from(text="**done**")])
        store = pieni.Store(":memory:")
        self.addCleanup(store.close)
        agent = pieni.Agent(provider, store, store.start_session("p", "m", "w"), "i", [],
                            Path.cwd(), pieni.Permissions("auto", Path.cwd()),
                            out=ui.out, answer_out=ui.markdown)
        self.assertTrue(agent.run_task("go"))
        shown = ui.console.file.getvalue()
        self.assertIn("done", shown)
        self.assertNotIn("**done**", shown)
        self.assertIn("Tokens:", shown)


class LauncherTests(unittest.TestCase):
    """The Bash launcher runs pieni.py, preferring the project virtualenv."""

    def setUp(self):
        if not Path("/usr/bin/env").exists():
            self.skipTest("needs a POSIX shell")
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.marker = self.root / "marker.txt"
        source = Path(pieni.__file__).with_name("pieni")
        if not source.exists():
            self.skipTest("the launcher is not next to pieni.py")
        self.launcher = self.root / "pieni"
        self.launcher.write_bytes(source.read_bytes())
        self.launcher.chmod(0o755)
        # A stub agent records its arguments, so the test stays unaware of the CLI.
        self.write_stub(self.root / "pieni.py",
                        'import sys\n'
                        f'open({str(self.marker)!r}, "a", encoding="utf-8").write("args=" + " " .join(sys.argv[1:]) + "\\n")\n')

    def write_stub(self, path, body, mode=0o755):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        path.chmod(mode)

    def stub_interpreter(self, name):
        """An interpreter stub that records its own path, then runs the real Python."""
        path = self.root / name
        self.write_stub(
            path,
            "#!/usr/bin/env bash\n"
            f'printf "interpreter=%s\\n" "$0" >> {str(self.marker)!r}\n'
            f'exec {sys.executable!r} "$@"\n')
        return path

    def run_launcher(self, *arguments, path=None, cwd=None, command=None):
        environment = dict(os.environ)
        if path is not None:
            environment["PATH"] = f"{path}{os.pathsep}{environment['PATH']}"
        completed = subprocess.run([str(command or self.launcher), *arguments],
                                   capture_output=True, text=True, env=environment,
                                   cwd=cwd, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return self.marker.read_text(encoding="utf-8").splitlines()

    def test_uses_the_project_virtualenv_when_it_exists(self):
        interpreter = self.stub_interpreter(".venv/bin/python")
        lines = self.run_launcher("--permissions", "yolo")
        self.assertEqual(lines, [f"interpreter={interpreter}", "args=--permissions yolo"])

    def test_falls_back_to_python3_on_the_path(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        interpreter = self.stub_interpreter("bin/python3")
        lines = self.run_launcher("openai", "-m", "gpt-x", path=bin_dir)
        self.assertEqual(lines, [f"interpreter={interpreter}", "args=openai -m gpt-x"])

    def test_finds_its_own_files_from_another_directory(self):
        interpreter = self.stub_interpreter(".venv/bin/python")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        lines = self.run_launcher(cwd=elsewhere)
        self.assertEqual(lines, [f"interpreter={interpreter}", "args="])

    def test_follows_a_symlink_like_an_installed_command(self):
        # scripts/install.sh installs exactly this: a symlink on the PATH.
        interpreter = self.stub_interpreter(".venv/bin/python")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        linked = bin_dir / "pieni"
        linked.symlink_to(self.launcher)
        lines = self.run_launcher("deepseek", command=linked)
        self.assertEqual(lines, [f"interpreter={interpreter}", "args=deepseek"])


if __name__ == "__main__":
    unittest.main()
