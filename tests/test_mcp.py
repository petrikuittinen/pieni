"""Offline tests for the MCP client and the bundled example server (loopback only)."""

import http.server
import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pieni
from tests import test_pieni as fixtures

EXAMPLE = str(Path(pieni.__file__).parent / "examples" / "mcp_fetch_server.py")
PAGE = b"<html><body><h1>Hello</h1><script>hidden()</script><p>World &amp; more</p></body></html>"


def serve(handler):
    """A loopback HTTP server running in a thread; returns it for shutdown."""
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class PageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


class StubMcpHandler(http.server.BaseHTTPRequestHandler):
    """A Streamable HTTP server: JSON for initialize, an event stream for tools/list."""

    def reply(self, status, headers, body=b""):
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append((message.get("method"), self.headers.get("Mcp-Session-Id"),
                                 self.headers.get("MCP-Protocol-Version")))
        if "id" not in message:
            return self.reply(202, {"Content-Length": "0"})
        if message["method"] == "initialize":
            body = json.dumps({"jsonrpc": "2.0", "id": message["id"],
                               "result": {"protocolVersion": "2025-03-26"}}).encode()
            return self.reply(200, {"Content-Type": "application/json", "Mcp-Session-Id": "abc123",
                                    "Content-Length": str(len(body))}, body)
        tools = [{"name": "echo", "description": "Echo", "inputSchema": {"type": "object"}}]
        events = ("event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"})
                  + "\n\ndata: " + json.dumps({"jsonrpc": "2.0", "id": message["id"],
                                               "result": {"tools": tools}}) + "\n\n")
        self.reply(200, {"Content-Type": "text/event-stream"}, events.encode())

    def do_DELETE(self):
        self.server.seen.append(("DELETE", self.headers.get("Mcp-Session-Id"), None))
        self.reply(204, {})

    def log_message(self, *args):
        pass


class FakeTransport:
    """Scripted stand-in for a transport: tool pages, a tools/call result, or an error."""

    kind = "fake"

    def __init__(self, pages=(), call_result=None, error=None):
        self.pages, self.call_result, self.error = list(pages), call_result, error
        self.sent, self.version, self.closed = [], None, False

    def send(self, message, wait_for=None, timeout=0):
        self.sent.append(message)
        if wait_for is None:
            return None
        method = message["method"]
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18"}
        elif method == "tools/list":
            index = int(message["params"].get("cursor", 0))
            result = {"tools": self.pages[index]}
            if index + 1 < len(self.pages):
                result["nextCursor"] = str(index + 1)
        elif self.error:
            return {"jsonrpc": "2.0", "id": wait_for, "error": self.error}
        else:
            result = self.call_result
        return {"jsonrpc": "2.0", "id": wait_for, "result": result}

    def close(self):
        self.closed = True


ECHO = {"name": "echo", "description": "Echo text.",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}


def fake_server(name="demo", tools=(ECHO,), **options):
    return pieni.McpServer(name, FakeTransport([list(tools)], **options))


class ExampleServerTests(unittest.TestCase):
    def setUp(self):
        page_server = serve(PageHandler)
        self.addCleanup(page_server.server_close)
        self.addCleanup(page_server.shutdown)
        self.base = f"http://127.0.0.1:{page_server.server_port}"

    def connect(self, *flags):
        server = pieni.McpServer("fetch", pieni.StdioTransport([sys.executable, EXAMPLE, *flags]))
        self.addCleanup(server.close)
        return server

    def test_lists_the_fetch_tool(self):
        server = self.connect()
        self.assertEqual([tool["name"] for tool in server.tools], ["fetch"])
        self.assertIn("url", server.tools[0]["inputSchema"]["required"])

    def test_fetch_reduces_html_to_text_and_follows_redirects(self):
        server = self.connect("--allow-private")
        for path in ("/page", "/redirect"):
            failed, text = server.call("fetch", {"url": self.base + path})
            self.assertFalse(failed)
            self.assertIn("Hello", text)
            self.assertIn("World & more", text)
            self.assertNotIn("hidden", text)
            self.assertNotIn("<", text)

    def test_max_chars_truncates(self):
        failed, text = self.connect("--allow-private").call(
            "fetch", {"url": self.base + "/page", "max_chars": 5})
        self.assertFalse(failed)
        self.assertTrue(text.endswith("[truncated]"))

    def test_private_addresses_and_other_schemes_are_refused(self):
        server = self.connect()
        failed, text = server.call("fetch", {"url": self.base + "/page"})
        self.assertTrue(failed)
        self.assertIn("not a public address", text)
        failed, text = server.call("fetch", {"url": "file:///etc/hosts"})
        self.assertTrue(failed)
        self.assertIn("only http and https", text)

    def test_protocol_errors_raise(self):
        server = self.connect()
        with self.assertRaisesRegex(pieni.McpError, "unknown tool"):
            server.call("nothing", {})
        with self.assertRaisesRegex(pieni.McpError, "url must be a string"):
            server.call("fetch", {"url": 5})

    def test_http_mode_and_origin_check(self):
        process = subprocess.Popen([sys.executable, EXAMPLE, "--http", "0", "--allow-private"],
                                   stderr=subprocess.PIPE, text=True)
        self.addCleanup(process.stderr.close)
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        url = process.stderr.readline().split()[-1]
        server = pieni.McpServer("fetch", pieni.HttpTransport(url))
        self.assertEqual([tool["name"] for tool in server.tools], ["fetch"])
        failed, text = server.call("fetch", {"url": self.base + "/page"})
        self.assertFalse(failed)
        self.assertIn("Hello", text)
        request = urllib.request.Request(url, b"{}", {"Origin": "http://evil.example"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()


class TransportTests(unittest.TestCase):
    def stdio(self, code):
        transport = pieni.StdioTransport([sys.executable, "-c", code])
        self.addCleanup(transport.close)
        return transport

    def test_stdio_skips_notifications_and_stale_replies(self):
        transport = self.stdio(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/message'}), flush=True)\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'id': 999, 'result': {}}), flush=True)\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': {'ok': 1}}), flush=True)\n")
        reply = transport.send({"jsonrpc": "2.0", "id": 7, "method": "ping"}, wait_for=7)
        self.assertEqual(reply["result"], {"ok": 1})

    def test_stdio_times_out_on_a_silent_server(self):
        transport = self.stdio("import time; time.sleep(30)")
        with self.assertRaisesRegex(pieni.McpError, "no reply within"):
            transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"}, wait_for=1, timeout=0.5)

    def test_stdio_fails_fast_once_the_server_has_exited(self):
        transport = self.stdio("pass")
        for _ in range(2):
            with self.assertRaises(pieni.McpError):
                transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"}, wait_for=1, timeout=10)

    def test_missing_program_is_an_mcp_error(self):
        with self.assertRaisesRegex(pieni.McpError, "cannot start"):
            pieni.StdioTransport(["definitely-not-a-program-xyz"])

    def test_http_session_version_event_stream_and_delete(self):
        stub = serve(StubMcpHandler)
        stub.seen = []
        self.addCleanup(stub.server_close)
        self.addCleanup(stub.shutdown)
        server = pieni.McpServer("stub", pieni.HttpTransport(f"http://127.0.0.1:{stub.server_port}/mcp"))
        self.assertEqual([tool["name"] for tool in server.tools], ["echo"])
        server.close()
        self.assertEqual(stub.seen, [("initialize", None, None),
                                     ("notifications/initialized", "abc123", "2025-03-26"),
                                     ("tools/list", "abc123", "2025-03-26"),
                                     ("DELETE", "abc123", None)])

    def test_unreachable_http_server_is_an_mcp_error(self):
        transport = pieni.HttpTransport("http://127.0.0.1:1/mcp")
        with self.assertRaisesRegex(pieni.McpError, "HTTP request failed"):
            transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"}, wait_for=1, timeout=5)


class McpServerTests(fixtures.TempWorkspaceCase):
    def test_handshake_and_paged_tool_list(self):
        transport = FakeTransport([[ECHO, "junk", {"description": "no name"}], [{"name": "second"}]])
        server = pieni.McpServer("demo", transport)
        self.assertEqual([tool["name"] for tool in server.tools], ["echo", "second"])
        self.assertEqual([message["method"] for message in transport.sent],
                         ["initialize", "notifications/initialized", "tools/list", "tools/list"])
        self.assertEqual(transport.sent[0]["params"]["protocolVersion"], pieni.MCP_PROTOCOL)
        self.assertEqual(transport.sent[3]["params"], {"cursor": "1"})
        self.assertEqual(transport.version, "2025-06-18")

    def test_call_results(self):
        content = [{"type": "text", "text": "a"}, {"type": "image", "data": "x"}, {"type": "text", "text": "b"}]
        self.assertEqual(fake_server(call_result={"content": content}).call("echo", {}),
                         (False, "a\n[image content omitted]\nb"))
        self.assertEqual(fake_server(call_result={"content": [{"type": "text", "text": "no"}],
                                                  "isError": True}).call("echo", {}), (True, "no"))
        self.assertEqual(fake_server(call_result={}).call("echo", {}), (False, "(no output)"))

    def test_jsonrpc_error_becomes_mcp_error(self):
        server = fake_server(error={"code": -32602, "message": "bad arguments"})
        with self.assertRaisesRegex(pieni.McpError, "bad arguments"):
            server.call("echo", {})

    def test_toolbox_exposes_and_runs_mcp_tools(self):
        long_name = {"name": "x" * 80}
        server = fake_server("my server", [{"name": "get.item"}, long_name, ECHO],
                             call_result={"content": [{"type": "text", "text": "hi"}]})
        toolbox = pieni.Toolbox(self.workspace, self.permissions(), mcp=[server])
        names = [spec["name"] for spec in toolbox.specs]
        self.assertEqual(names[:4], ["read", "write", "edit", "bash"])
        self.assertIn("my_server__get_item", names)
        self.assertTrue(all(len(name) <= 64 for name in names))
        specs = {spec["name"]: spec for spec in toolbox.specs}
        self.assertEqual(specs["my_server__get_item"]["parameters"], {"type": "object", "properties": {}})
        self.assertEqual(specs["my_server__echo"]["parameters"], ECHO["inputSchema"])
        outcome = toolbox.run("my_server__echo", {"text": "hi"})
        self.assertEqual((outcome.ok, outcome.output), (True, "hi"))
        self.assertEqual(server.transport.sent[-1]["params"],
                         {"name": "echo", "arguments": {"text": "hi"}})
        self.assertIn("my_server__echo", toolbox.run("missing", {}).output)

    def test_toolbox_reports_server_errors_without_raising(self):
        failing = fake_server(call_result={"content": [{"type": "text", "text": "boom"}], "isError": True})
        outcome = pieni.Toolbox(self.workspace, self.permissions(), mcp=[failing]).run("demo__echo", {})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.output, "error: boom")
        broken = fake_server(error={"message": "kaput"})
        outcome = pieni.Toolbox(self.workspace, self.permissions(), mcp=[broken]).run("demo__echo", {})
        self.assertFalse(outcome.ok)
        self.assertIn("kaput", outcome.output)

    def test_mcp_output_is_truncated(self):
        server = fake_server(call_result={"content": [{"type": "text", "text": "x" * 70_000}]})
        outcome = pieni.Toolbox(self.workspace, self.permissions(), mcp=[server]).run("demo__echo", {})
        self.assertIn("truncated", outcome.output)

    def make_agent(self, replies, server, **kwargs):
        provider = fixtures.ScriptedProvider(replies)
        store = pieni.Store(self.root / "pieni.db")
        self.addCleanup(store.close)
        session = store.start_session(provider.name, provider.model, str(self.workspace))
        output = []
        agent = pieni.Agent(provider, store, session, "instructions", [], self.workspace,
                            self.permissions(), out=output.append, dots=None, mcp=[server], **kwargs)
        return agent, provider, output

    def test_agent_loop_calls_an_mcp_tool_and_the_provider_sees_it(self):
        server = fake_server(call_result={"content": [{"type": "text", "text": "echo: hi"}]})
        agent, provider, output = self.make_agent(
            [fixtures.reply_from([("c1", "demo__echo", '{"text": "hi"}')])], server)
        self.assertTrue(agent.run_task("say hi"))
        self.assertIn("demo__echo", [spec["name"] for spec in provider.specs])
        self.assertIn("demo__echo", json.dumps(agent.specs))
        tool_message = [m for m in agent.messages if m["role"] == "tool"][0]
        self.assertEqual(tool_message["content"], "echo: hi")
        self.assertTrue(any(line.startswith('demo__echo(text="hi") -> ok') for line in output))

    def test_mcp_command_lists_servers_and_tools(self):
        agent, _, output = self.make_agent([], fake_server())
        agent.handle_command("/mcp")
        self.assertEqual(output[-1], "demo [fake]: echo")
        agent.mcp = []
        agent.handle_command("/mcp")
        self.assertIn("no MCP servers connected", output[-1])
        agent.handle_command("/help")
        self.assertIn("/mcp", output[-1])

    def test_provider_sends_mcp_tools_in_each_wire_shape(self):
        server = fake_server()
        reply = fixtures.chat_reply("ok")
        client = fixtures.FakeChatClient(reply)
        provider = pieni.Provider("custom", "m", "custom", client, streaming=False)
        provider.specs = pieni.Toolbox(self.workspace, self.permissions(), mcp=[server]).specs
        provider.complete([{"role": "user", "content": "hi"}])
        sent = [tool["function"]["name"] for tool in client.requests[0]["tools"]]
        self.assertEqual(sent, ["read", "write", "edit", "bash", "demo__echo"])
        responses = fixtures.FakeResponsesClient(fixtures.responses_reply("ok"))
        flat = pieni.Provider("openai", "m", "responses", responses, streaming=False)
        flat.specs = provider.specs
        flat.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(responses.requests[0]["tools"][-1]["name"], "demo__echo")


class McpConfigTests(fixtures.TempWorkspaceCase):
    def load(self, cli=()):
        return pieni.load_mcp(self.workspace, self.home, cli)

    def test_sources_merge_with_later_ones_winning(self):
        self.write(self.home / ".pieni" / "mcp.json", json.dumps({"mcpServers": {
            "a": {"command": "user-json-a"}, "b": {"command": "user-json-b"}, "c": {"command": "x"}}}))
        self.write(self.home / ".pieni" / "pieni.ini", "[pieni]\nprovider = openai\n[mcp.b]\ncommand = user-ini-b\n")
        self.write(self.workspace / ".mcp.json", json.dumps({"mcpServers": {"c": {"command": "proj-json-c"}}}))
        self.write(self.workspace / "pieni.ini", "[mcp.d]\nurl = http://localhost:9/mcp\n")
        servers = self.load(["a=cli-a --flag", "e=https://example.test/mcp"])
        self.assertEqual(servers["a"]["command"], ["cli-a", "--flag"])
        self.assertEqual(servers["b"]["command"], ["user-ini-b"])
        self.assertEqual(servers["c"]["command"], ["proj-json-c"])
        self.assertEqual(servers["d"], {"url": "http://localhost:9/mcp", "headers": {}})
        self.assertEqual(servers["e"]["url"], "https://example.test/mcp")

    def test_missing_files_give_no_servers_and_pieni_section_is_unaffected(self):
        self.assertEqual(self.load(), {})
        path = self.write(self.workspace / "pieni.ini", "[pieni]\nprovider = openai\n[mcp.x]\ncommand = foo\n")
        self.assertEqual(pieni.read_config_file(path), {"provider": "openai"})
        self.assertEqual(self.load()["x"]["command"], ["foo"])

    def test_args_env_and_header_expansion(self):
        self.write(self.workspace / ".mcp.json", json.dumps({"mcpServers": {
            "local": {"command": "python", "args": ["server.py"], "env": {"KEY": "${PIENI_TEST_KEY}"}},
            "remote": {"url": "https://example.test/mcp",
                       "headers": {"Authorization": "Bearer ${PIENI_TEST_KEY}"}}}}))
        with mock.patch.dict(os.environ, {"PIENI_TEST_KEY": "secret"}):
            servers = self.load()
        self.assertEqual(servers["local"], {"command": ["python", "server.py"], "env": {"KEY": "secret"}})
        self.assertEqual(servers["remote"]["headers"], {"Authorization": "Bearer secret"})

    def test_ini_commands_may_quote_arguments(self):
        self.write(self.workspace / "pieni.ini", '[mcp.q]\ncommand = python "my server.py" --x\n')
        self.assertEqual(self.load()["q"]["command"], ["python", "my server.py", "--x"])

    def test_invalid_configuration_is_reported(self):
        path = self.workspace / ".mcp.json"
        for text in ("{not json", "[]", '{"mcpServers": []}', '{"mcpServers": {"x": {}}}',
                     '{"mcpServers": {"x": "python"}}'):
            with self.subTest(text=text):
                self.write(path, text)
                with self.assertRaises(pieni.ConfigError):
                    self.load()
        path.unlink()
        for item in ("nonsense", "x=", "=python"):
            with self.subTest(item=item):
                with self.assertRaises(pieni.ConfigError):
                    self.load([item])

    def test_failing_servers_are_skipped_with_a_warning(self):
        configs = {
            "bad": {"command": ["definitely-not-a-program-xyz"], "env": {}},
            "dead": {"command": [sys.executable, "-c", "pass"], "env": {}},
            "down": {"url": "http://127.0.0.1:1/mcp", "headers": {}},
            "good": {"command": [sys.executable, EXAMPLE], "env": {}},
        }
        stderr = StringIO()
        with redirect_stderr(stderr):
            servers = pieni.connect_mcp(configs)
        for server in servers:
            self.addCleanup(server.close)
        self.assertEqual([server.name for server in servers], ["good"])
        self.assertEqual(stderr.getvalue().count("warning: skipped MCP server"), 3)


class Utf8BomTests(fixtures.TempWorkspaceCase):
    """Windows PowerShell 5.1 and some editors save UTF-8 files with a byte order mark."""

    def test_skills_mcp_json_and_ini_files_with_a_bom_are_read(self):
        bom = "\ufeff"
        skill = self.workspace / pieni.SKILLS_DIR / "demo" / "SKILL.md"
        self.write(skill, bom + "---\nname: demo\ndescription: A demo.\n---\nBody\n")
        self.write(self.workspace / ".mcp.json", bom + '{"mcpServers": {"a": {"command": "x"}}}')
        ini = self.write(self.workspace / "pieni.ini", bom + "[pieni]\nprovider = openai\n[mcp.b]\nurl = http://h/mcp\n")
        self.assertEqual(pieni.read_skill(skill), ("demo", "A demo."))
        servers = pieni.load_mcp(self.workspace, self.home)
        self.assertEqual(sorted(servers), ["a", "b"])
        self.assertEqual(pieni.read_config_file(ini), {"provider": "openai"})


class McpCliTests(fixtures.TempWorkspaceCase):
    def test_arguments(self):
        args = pieni.parse_args(["openai", "--mcp", "a=b", "--mcp", "c=d", "--no-mcp"])
        self.assertEqual((args.mcp, args.no_mcp), (["a=b", "c=d"], True))
        args = pieni.parse_args(["openai"])
        self.assertEqual((args.mcp, args.no_mcp), (None, False))

    def run_main(self, *extra):
        page_server = serve(PageHandler)
        self.addCleanup(page_server.server_close)
        self.addCleanup(page_server.shutdown)
        url = f"http://127.0.0.1:{page_server.server_port}/page"
        fixtures.ScriptedFakeOpenAI.script = [
            fixtures.responses_reply("", [("call_1", "fetch__fetch", json.dumps({"url": url}))]),
            fixtures.responses_reply("all done")]
        fixtures.ScriptedFakeOpenAI.instances = []
        self.in_directory(self.workspace)
        stdout = StringIO()
        command = f'fetch="{sys.executable}" "{EXAMPLE}" --allow-private'
        with mock.patch("pieni.config_paths", return_value=[self.home / "n.ini", self.workspace / "n.ini"]), \
                mock.patch("pieni.os.path.expanduser", return_value=str(self.home)), \
                mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}), \
                mock.patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=fixtures.ScriptedFakeOpenAI)}), \
                redirect_stdout(stdout):
            code = pieni.main(["openai", "-m", "gpt-x", "-r", "fetch it", "--mcp", command, *extra])
        return code, stdout.getvalue(), fixtures.ScriptedFakeOpenAI.instances[-1].requests

    def test_headless_task_uses_an_mcp_server_end_to_end(self):
        code, output, requests = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("fetch__fetch(url=", output)
        self.assertIn("-> ok", output)
        self.assertIn("fetch__fetch", [tool["name"] for tool in requests[0]["tools"]])
        results = [item for item in requests[1]["input"] if item.get("type") == "function_call_output"]
        self.assertIn("Hello", results[0]["output"])

    def test_no_mcp_ignores_the_configuration(self):
        fixtures.ScriptedFakeOpenAI.script = [fixtures.responses_reply("plain")]
        fixtures.ScriptedFakeOpenAI.instances = []
        self.in_directory(self.workspace)
        with mock.patch("pieni.config_paths", return_value=[self.home / "n.ini", self.workspace / "n.ini"]), \
                mock.patch("pieni.os.path.expanduser", return_value=str(self.home)), \
                mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}), \
                mock.patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=fixtures.ScriptedFakeOpenAI)}), \
                redirect_stdout(StringIO()):
            code = pieni.main(["openai", "-m", "gpt-x", "-r", "hi", "--mcp", "x=definitely-not-a-program",
                               "--no-mcp"])
        self.assertEqual(code, 0)
        tools = fixtures.ScriptedFakeOpenAI.instances[-1].requests[0]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["read", "write", "edit", "bash"])


if __name__ == "__main__":
    unittest.main()
