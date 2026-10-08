#!/usr/bin/env python3
"""AnythingLLM MCP：读取第一个工作区的文件信息。

Streamable HTTP，同一 /mcp：
- 2025-03-26 .. 2025-11-25：initialize 握手、Mcp-Session-Id、GET 长连接
- 2026-07-28：无握手、无会话、无 GET 长连接；请求自带 Mcp-Method / _meta
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8765"))
BASE = os.environ.get("ANYTHINGLLM_BASE", "http://127.0.0.1:3001/api").rstrip("/")
API_KEY = os.environ.get("ANYTHINGLLM_API_KEY", "【请同学们自己在AnythingLLM中创建】")

LEGACY_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")
MODERN_VERSION = "2026-07-28"
SERVER_INFO = {"name": "anythingllm-files", "version": "1.0.0"}
TOOL = {
    "name": "first_workspace_files",
    "description": "提取 AnythingLLM 第一个工作区中的文件信息。",
    "inputSchema": {"type": "object", "properties": {}},
}

_sessions = set()
_lock = threading.Lock()


def _ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


def _meta(params):
    if isinstance(params, dict):
        meta = params.get("_meta")
        if isinstance(meta, dict):
            return meta
    return {}


def _decode_header(value):
    if value.startswith("=?base64?") and value.endswith("?="):
        import base64

        return base64.b64decode(value[9:-2]).decode("utf-8")
    return value


def _files():
    try:
        API_KEY.encode("latin-1")
    except UnicodeEncodeError:
        raise RuntimeError(
            "API key 不是 ASCII。请在 AnythingLLM 中创建 Developer API Key，"
            "并设置环境变量 ANYTHINGLLM_API_KEY。"
        ) from None

    def get(path):
        req = urllib.request.Request(
            BASE + path,
            headers={"Authorization": "Bearer " + API_KEY, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"AnythingLLM HTTP {exc.code}: {detail}") from None

    data = get("/v1/workspaces")
    workspaces = data.get("workspaces") or []
    if not workspaces:
        return {"workspace": None, "fileCount": 0, "files": []}
    ws = workspaces[0]
    docs = ws.get("documents")
    if docs is None:
        detail = get("/v1/workspace/" + urllib.parse.quote(ws["slug"]))
        ws = detail.get("workspace") or detail
        if isinstance(ws, list):
            ws = ws[0] if ws else {}
        docs = ws.get("documents") or []
    files = []
    for doc in docs:
        meta = doc.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        files.append(
            {
                "filename": doc.get("filename"),
                "docpath": doc.get("docpath"),
                "docId": doc.get("docId"),
                "title": meta.get("title"),
                "published": meta.get("published"),
                "wordCount": meta.get("wordCount"),
                "tokenCountEstimate": meta.get("token_count_estimate"),
                "pinned": doc.get("pinned"),
                "createdAt": doc.get("createdAt"),
            }
        )
    return {
        "workspace": {"id": ws.get("id"), "name": ws.get("name"), "slug": ws.get("slug")},
        "fileCount": len(files),
        "files": files,
    }


def _tool_result(modern):
    try:
        text = json.dumps(_files(), ensure_ascii=False, indent=2)
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as exc:
        result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    if modern:
        result["resultType"] = "complete"
    return result


def _legacy_result(msg):
    req_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "ping":
        return _ok(req_id, {})
    if method == "tools/list":
        return _ok(req_id, {"tools": [TOOL]})
    if method == "tools/call":
        if params.get("name") != TOOL["name"]:
            return _err(req_id, -32602, "Unknown tool")
        return _ok(req_id, _tool_result(False))
    return _err(req_id, -32601, "Method not found")


def _modern_result(msg):
    req_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "ping":
        return _ok(req_id, {})
    if method == "server/discover":
        return _ok(
            req_id,
            {
                "resultType": "complete",
                "supportedVersions": [MODERN_VERSION, *LEGACY_VERSIONS],
                "capabilities": {"tools": {"listChanged": False}},
                "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
                "instructions": "调用 first_workspace_files 读取第一个工作区的文件信息。",
                "ttlMs": 3600000,
                "cacheScope": "public",
            },
        )
    if method == "tools/list":
        return _ok(
            req_id,
            {
                "resultType": "complete",
                "tools": [TOOL],
                "ttlMs": 300000,
                "cacheScope": "public",
            },
        )
    if method == "tools/call":
        if params.get("name") != TOOL["name"]:
            return _err(req_id, -32602, "Unknown tool")
        return _ok(req_id, _tool_result(True))
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.command, fmt % args))

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = urllib.parse.urlparse(origin).hostname
        if host in ("localhost", "127.0.0.1", "::1"):
            return True
        body = _err(None, -32000, "Invalid Origin")
        self._send(403, body)
        return False

    def _send(self, status, payload=None, extra=None, sse=False):
        if sse and payload is not None:
            data = ("event: message\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Content-Length", str(len(data)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)
            return
        raw = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return None, _err(None, -32700, "Parse error")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return None, _err(None, -32700, "Parse error")
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return None, _err(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
        return msg, None

    def do_GET(self):
        if not self._origin_ok():
            return
        if self.path.split("?", 1)[0].rstrip("/") != "/mcp":
            self._send(404, _err(None, -32601, "Not found"))
            return
        # 2026-07-28 取消了独立 GET 长连接。带现代协议头时直接拒绝。
        if self.headers.get("MCP-Protocol-Version") == MODERN_VERSION:
            self._send(405, _err(None, -32601, "GET stream is not used in protocol 2026-07-28"), {"Allow": "POST"})
            return
        sid = self.headers.get("Mcp-Session-Id")
        with _lock:
            ok = sid in _sessions
        if not ok:
            self._send(400, _err(None, -32000, "Session required"))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b": connected\r\n\r\n")
            self.wfile.flush()
            while True:
                with _lock:
                    alive = sid in _sessions
                if not alive:
                    break
                self.wfile.write(b": keepalive\r\n\r\n")
                self.wfile.flush()
                time.sleep(15)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        finally:
            self.close_connection = True

    def do_DELETE(self):
        if not self._origin_ok():
            return
        sid = self.headers.get("Mcp-Session-Id")
        with _lock:
            existed = sid in _sessions
            _sessions.discard(sid)
        self._send(200 if existed else 404)

    def do_POST(self):
        if not self._origin_ok():
            return
        if self.path.split("?", 1)[0].rstrip("/") != "/mcp":
            self._send(404, _err(None, -32601, "Not found"))
            return
        msg, bad = self._read_json()
        if bad:
            self._send(400, bad)
            return
        if "id" not in msg:
            self._send(202)
            return

        header_version = self.headers.get("MCP-Protocol-Version")
        body_version = _meta(msg.get("params")).get("io.modelcontextprotocol/protocolVersion")
        modern = (
            header_version == MODERN_VERSION
            or body_version == MODERN_VERSION
            or self.headers.get("Mcp-Method") is not None
        )
        if modern:
            self._modern(msg, header_version, body_version)
        else:
            self._legacy(msg, header_version)

    def _modern(self, msg, header_version, body_version):
        req_id = msg.get("id")
        if header_version != MODERN_VERSION:
            self._send(
                400,
                _err(
                    req_id,
                    -32022,
                    "Unsupported protocol version",
                    {"supported": [MODERN_VERSION, *LEGACY_VERSIONS], "requested": header_version},
                ),
            )
            return
        if body_version != header_version:
            self._send(400, _err(req_id, -32020, "Header mismatch: MCP-Protocol-Version"))
            return
        method_header = self.headers.get("Mcp-Method")
        if method_header != msg.get("method"):
            self._send(400, _err(req_id, -32020, "Header mismatch: Mcp-Method"))
            return
        if msg.get("method") == "tools/call":
            name = (msg.get("params") or {}).get("name")
            raw_name = self.headers.get("Mcp-Name")
            if not raw_name:
                self._send(400, _err(req_id, -32020, "Header mismatch: Mcp-Name missing"))
                return
            try:
                decoded = _decode_header(raw_name)
            except Exception:
                self._send(400, _err(req_id, -32020, "Header mismatch: Mcp-Name malformed"))
                return
            if decoded != name:
                self._send(400, _err(req_id, -32020, "Header mismatch: Mcp-Name"))
                return
        result = _modern_result(msg)
        if result is None:
            self._send(404, _err(req_id, -32601, "Method not found"))
            return
        accept = self.headers.get("Accept") or ""
        sse = "text/event-stream" in accept and "application/json" not in accept
        self._send(200, result, sse=sse)

    def _legacy(self, msg, header_version):
        req_id = msg.get("id")
        method = msg.get("method")
        if header_version and header_version not in LEGACY_VERSIONS:
            self._send(
                400,
                _err(
                    req_id,
                    -32022,
                    "Unsupported protocol version",
                    {"supported": [MODERN_VERSION, *LEGACY_VERSIONS], "requested": header_version},
                ),
            )
            return
        if method == "initialize":
            params = msg.get("params") or {}
            requested = params.get("protocolVersion")
            agreed = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[-1]
            sid = uuid.uuid4().hex
            with _lock:
                _sessions.add(sid)
            self._send(
                200,
                _ok(
                    req_id,
                    {
                        "protocolVersion": agreed,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": SERVER_INFO,
                    },
                ),
                {"Mcp-Session-Id": sid},
            )
            return
        sid = self.headers.get("Mcp-Session-Id")
        with _lock:
            ok = sid in _sessions
        if not ok:
            self._send(400, _err(req_id, -32000, "Session required"))
            return
        self._send(200, _legacy_result(msg))


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"anythingllm-files MCP  http://{HOST}:{PORT}/mcp")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
