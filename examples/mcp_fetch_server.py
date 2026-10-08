#!/usr/bin/env python3
"""A tiny MCP server (standard library only) with one tool, `fetch`, that downloads a page as text.

  python mcp_fetch_server.py               speak MCP over stdin/stdout
  python mcp_fetch_server.py --http 8765   serve POST /mcp on 127.0.0.1:8765 (0 = any free port)

Private, loopback, and link-local addresses are refused unless --allow-private is given,
so a model cannot use this server to probe your network. It is an example, not a hardened proxy.
"""

import argparse
import html
import ipaddress
import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

PROTOCOL = "2025-06-18"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
MAX_BYTES = 1_000_000
DEFAULT_CHARS = 20_000
TOOL = {
    "name": "fetch",
    "description": "Download an http(s) URL and return its text (HTML is reduced to plain text).",
    "inputSchema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The http or https URL to fetch."},
            "max_chars": {"type": "integer",
                          "description": f"Maximum characters to return (default {DEFAULT_CHARS})."},
        },
        "required": ["url"],
    },
}
allow_private = False


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # redirects are followed by hand so every hop is checked


OPENER = urllib.request.build_opener(NoRedirect)


def check_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("only http and https URLs are allowed")
    if not allow_private:  # best effort: the name is resolved again when connecting
        for info in socket.getaddrinfo(parts.hostname, parts.port or 80):
            if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
                raise ValueError(f"{parts.hostname} is not a public address")


def html_to_text(page):
    page = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", page)
    page = re.sub(r"(?i)</?(p|div|br|li|ul|ol|h[1-6]|tr|table|section|article)\b[^>]*>", "\n", page)
    page = html.unescape(re.sub(r"(?s)<[^>]*>", " ", page))
    page = re.sub(r"[ \t\r\f\v]+", " ", page)
    return re.sub(r"\n\s*\n+", "\n\n", page).strip()


def fetch(url, max_chars):
    for _ in range(6):  # the first request plus up to five redirects
        check_url(url)
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,text/plain,*/*;q=0.8"})
        try:
            with OPENER.open(request, timeout=30) as response:
                body = response.read(MAX_BYTES + 1)
                kind = response.headers.get("Content-Type", "")
                charset = response.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location")
            if exc.code in (301, 302, 303, 307, 308) and location:
                url = urllib.parse.urljoin(url, location)
                continue
            raise
        text = body[:MAX_BYTES].decode(charset, errors="replace")
        if "html" in kind:
            text = html_to_text(text)
        cut = len(text) > max_chars or len(body) > MAX_BYTES
        return text[:max_chars] + ("\n...[truncated]" if cut else "")
    raise ValueError("too many redirects")


def call_tool(params):
    if params.get("name") != "fetch":
        raise RpcError(-32602, f"unknown tool: {params.get('name')}")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments.get("url"), str):
        raise RpcError(-32602, "url must be a string")
    try:
        text = fetch(arguments["url"], int(arguments.get("max_chars") or DEFAULT_CHARS))
        return {"content": [{"type": "text", "text": text}]}
    except (OSError, ValueError, LookupError) as exc:  # the model can read and react to these
        return {"content": [{"type": "text", "text": f"fetch failed: {exc}"}], "isError": True}


def handle(message):
    """The JSON-RPC reply for one message, or None for a notification."""
    ident = message.get("id")
    if ident is None:
        return None
    method = message.get("method")
    try:
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fetch", "version": "0.1"}}
        elif method == "tools/list":
            result = {"tools": [TOOL]}
        elif method == "tools/call":
            result = call_tool(message.get("params") or {})
        elif method == "ping":
            result = {}
        else:
            raise RpcError(-32601, f"method not found: {method}")
    except RpcError as exc:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": exc.code, "message": str(exc)}}
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def serve_stdio():
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        reply = handle(message) if isinstance(message, dict) else None
        if reply:
            print(json.dumps(reply), flush=True)  # ASCII only: one line per message


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        origin = self.headers.get("Origin")  # refuse web pages (DNS rebinding)
        if origin and urllib.parse.urlsplit(origin).hostname not in ("127.0.0.1", "localhost"):
            return self.send_error(403)
        if self.path != "/mcp":
            return self.send_error(404)
        try:
            message = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except ValueError:
            return self.send_error(400)
        reply = handle(message) if isinstance(message, dict) else None
        body = json.dumps(reply).encode() if reply else b""
        self.send_response(200 if reply else 202)
        if reply:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    global allow_private
    parser = argparse.ArgumentParser(description="MCP server with a fetch tool.")
    parser.add_argument("--http", type=int, metavar="PORT", help="serve over HTTP instead of stdio")
    parser.add_argument("--allow-private", action="store_true",
                        help="allow loopback and private addresses")
    arguments = parser.parse_args()
    allow_private = arguments.allow_private
    if arguments.http is None:
        serve_stdio()
        return
    server = HTTPServer(("127.0.0.1", arguments.http), Handler)
    print(f"listening on http://127.0.0.1:{server.server_port}/mcp", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
