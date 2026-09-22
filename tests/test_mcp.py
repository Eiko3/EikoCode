"""外部工具接入测试（离线，v6）。

覆盖 checklist.md 组 42–47 与 E22 / E23：
- 消息层：构造解析互逆、id 线程安全、错误码归一、通知与服务器请求
- stdio 传输：对真实子进程夹具的往返、关闭与强杀
- HTTP 传输：本地假服务端、会话标识回带、超时与网络异常归一
- 客户端：三阶段顺序、握手只一次、传输推断
- 配置：字段、覆盖、env 引用、四类配置错误、错误收集不崩配置
- 适配层：命名空间、默认权限、非文本内容、kill
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from eikocode import config as config_module
from eikocode.config import McpServerConfig, load_config, parse_mcp_servers
from eikocode.errors import ErrorKind, EikoCodeError
from eikocode.mcp import protocol
from eikocode.mcp.adapter import RemoteTool, UNSUPPORTED_CONTENT
from eikocode.mcp.client import McpClient, make_transport
from eikocode.mcp.transport_stdio import StdioTransport
from eikocode.tools.base import PermissionLevel
from eikocode.tools.registry import ToolRegistry

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mcp_echo_server.py"


def _server_config(**overrides) -> McpServerConfig:
    defaults = dict(
        name="echo",
        argv=(sys.executable, "-u", str(FIXTURE)),
        url="",
        env=(),
        timeout=10.0,
        permission="write",
    )
    defaults.update(overrides)
    return McpServerConfig(**defaults)


class _FakeUi:
    def __init__(self):
        self.notices: list[str] = []

    def notice(self, t):
        self.notices.append(t)

    def error(self, t):
        self.notices.append(t)


# --------------------------------------------------------------------------- #
# 组 42：JSON-RPC 消息层
# --------------------------------------------------------------------------- #
def test_request_response_roundtrip():
    request = protocol.make_request("tools/list", {})
    assert request["method"] == "tools/list"
    assert protocol.is_response({"jsonrpc": "2.0", "id": request["id"], "result": {}})
    assert not protocol.is_response(request)


def test_ids_are_thread_safe_and_unique():
    ids: list[int] = []
    lock = threading.Lock()

    def grab():
        local = [protocol.next_id() for _ in range(50)]
        with lock:
            ids.extend(local)

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ids) == len(set(ids)) == 400


@pytest.mark.parametrize(
    "code,expected",
    [
        (protocol.PARSE_ERROR, ErrorKind.PROTOCOL),
        (protocol.INVALID_REQUEST, ErrorKind.PROTOCOL),
        (protocol.METHOD_NOT_FOUND, ErrorKind.PROTOCOL),
        (protocol.INVALID_PARAMS, ErrorKind.PROTOCOL),
        (protocol.INTERNAL_ERROR, ErrorKind.TOOL_ERROR),
    ],
)
def test_error_codes_map_to_kinds(code, expected):
    response = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": code, "message": "x"},
    }
    with pytest.raises(EikoCodeError) as excinfo:
        protocol.raise_for_error(response)
    assert excinfo.value.kind is expected


def test_notification_is_not_a_response():
    notification = protocol.make_notification("notifications/initialized")
    assert not protocol.is_response(notification)
    assert "id" not in notification


def test_server_request_rejected_with_32601():
    """服务端主动请求 → 回方法不存在并提示（提议默认）。"""
    ui = _FakeUi()
    client = McpClient(_server_config())
    fake = SimpleNamespace(drain_notifications=lambda: [
        {"jsonrpc": "2.0", "id": 99, "method": "sampling/createMessage", "params": {}}
    ])
    sent: list[dict] = []
    fake.notify = lambda payload: sent.append(payload)
    client._transport = fake

    notes = client.drain_notifications()
    assert notes and notes[0]["method"] == "sampling/createMessage"
    assert sent and sent[0]["error"]["code"] == protocol.METHOD_NOT_FOUND


# --------------------------------------------------------------------------- #
# 组 43：stdio 传输
# --------------------------------------------------------------------------- #
def _stdio():
    transport = StdioTransport([sys.executable, "-u", str(FIXTURE)])
    transport.start()
    return transport


def test_stdio_roundtrip():
    transport = _stdio()
    try:
        response = transport.request(
            protocol.make_request("initialize", {"protocolVersion": protocol.PROTOCOL_VERSION}),
            timeout=10.0,
        )
        assert response["result"]["serverInfo"]["name"] == "echo-fixture"
    finally:
        transport.close()


def test_stdio_close_reclaims_process():
    transport = _stdio()
    transport.close()
    # 关闭后进程已退出（温和或强杀）
    assert transport._proc is None


def test_stdio_kill():
    transport = _stdio()
    transport.kill()
    assert transport._proc is None or transport._proc.poll() is not None
    transport.close()


# --------------------------------------------------------------------------- #
# 组 44：HTTP 传输（本地假服务端）
# --------------------------------------------------------------------------- #
class _McpHttpHandler(BaseHTTPRequestHandler):
    received_sessions: list[str | None] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        data = json.loads(self.rfile.read(length) or b"{}")
        _McpHttpHandler.received_sessions.append(self.headers.get("Mcp-Session-Id"))

        method = data.get("method")
        rid = data.get("id")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake-http"}}
            headers = {"Mcp-Session-Id": "sess-abc-123"}
        elif method == "tools/list":
            result = {"tools": [{"name": "search", "description": "d", "inputSchema": {"type": "object", "properties": {}}}]}
            headers = {}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "http-ok"}]}
            headers = {}
        else:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "nf"}}).encode())
            return

        body = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _McpHttpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _McpHttpHandler.received_sessions = []
    yield f"http://127.0.0.1:{server.server_address[1]}/mcp"
    server.shutdown()


def test_http_roundtrip_and_session_header(http_server):
    transport = make_transport(_server_config(name="h", argv=(), url=http_server))
    transport.start()
    try:
        init = transport.request(protocol.make_request("initialize", {}), timeout=10.0)
        assert init["result"]["serverInfo"]["name"] == "fake-http"
        transport.notify(protocol.make_notification("notifications/initialized"))
        listed = transport.request(protocol.make_request("tools/list", {}), timeout=10.0)
        assert listed["result"]["tools"][0]["name"] == "search"
        # 第二次请求回带了服务端下发的会话标识
        assert _McpHttpHandler.received_sessions == [None, "sess-abc-123", "sess-abc-123"]
    finally:
        transport.close()


def test_http_connection_error_normalized():
    transport = make_transport(_server_config(name="h", argv=(), url="http://127.0.0.1:9/mcp"))
    transport.start()
    with pytest.raises(EikoCodeError) as excinfo:
        transport.request(protocol.make_request("initialize", {}), timeout=3.0)
    assert excinfo.value.kind is ErrorKind.NETWORK
    transport.close()


# --------------------------------------------------------------------------- #
# 组 45：客户端三阶段
# --------------------------------------------------------------------------- #
class _FakeTransport:
    """记录方法调用序列的假传输，断言三阶段顺序与握手次数。"""

    def __init__(self):
        self.methods: list[str] = []
        self.handshakes = 0

    def start(self):
        self.methods.append("start")

    def request(self, payload: dict, timeout: float):
        method = payload["method"]
        self.methods.append(method)
        if method == "initialize":
            self.handshakes += 1
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {"protocolVersion": "x"}}
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"tools": [{"name": "t1", "description": "d", "inputSchema": {"type": "object", "properties": {}}}]},
            }
        return {
            "jsonrpc": "2.0",
            "id": payload["id"],
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }

    def notify(self, payload):
        self.methods.append(payload["method"])

    def close(self):
        pass

    def kill(self):
        pass

    def drain_notifications(self):
        return []


def test_client_three_stages_in_order():
    client = McpClient(_server_config())
    client._transport = fake = _FakeTransport()
    tools = client.connect()

    assert fake.methods == [
        "start",
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    assert tools[0]["name"] == "t1"


def test_client_handshake_once():
    client = McpClient(_server_config())
    client._transport = fake = _FakeTransport()
    client.connect()
    client.call_tool("t1", {})
    client.call_tool("t1", {})
    assert fake.handshakes == 1


def test_transport_inference():
    assert isinstance(make_transport(_server_config(argv=("x",))), StdioTransport)
    assert not isinstance(
        make_transport(_server_config(name="h", argv=(), url="http://x/mcp")), StdioTransport
    )
    with pytest.raises(EikoCodeError):
        make_transport(_server_config(name="h", argv=(), url=""))
    with pytest.raises(EikoCodeError):
        make_transport(_server_config(name="h", argv=("x",), url="http://x"))


# --------------------------------------------------------------------------- #
# 组 46：配置解析
# --------------------------------------------------------------------------- #
def test_mcp_parse_fields():
    servers = parse_mcp_servers([
        {
            "name": "docs",
            "command": "npx",
            "args": ["-y", "server-docs"],
            "env": {"PLAIN": "value"},
            "timeout": 30.0,
            "permission": "read",
        }
    ])
    assert servers[0].argv == ("npx", "-y", "server-docs")
    assert servers[0].timeout == 30.0
    assert servers[0].permission == "read"


def test_mcp_env_reference_expands(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "abc123")
    servers = parse_mcp_servers([
        {"name": "s", "command": "x", "env": {"TOKEN": "${MY_TOKEN}"}}
    ])
    assert dict(servers[0].env)["TOKEN"] == "abc123"


def test_mcp_env_reference_missing_reports_name():
    with pytest.raises(EikoCodeError) as excinfo:
        parse_mcp_servers([
            {"name": "s", "command": "x", "env": {"TOKEN": "${NOPE_MISSING}"}}
        ])
    assert "NOPE_MISSING" in excinfo.value.detail


def test_mcp_config_errors():
    with pytest.raises(EikoCodeError) as e1:
        parse_mcp_servers([{"command": "x"}])
    assert "name" in e1.value.detail

    with pytest.raises(EikoCodeError) as e2:
        parse_mcp_servers([{"name": "dup", "command": "x"}, {"name": "dup", "url": "http://a"}])
    assert "dup" in e2.value.detail

    with pytest.raises(EikoCodeError) as e3:
        parse_mcp_servers([{"name": "both", "command": "x", "url": "http://a"}])
    assert "both" in e3.value.detail

    with pytest.raises(EikoCodeError) as e4:
        parse_mcp_servers([{"name": "none"}])
    assert "none" in e4.value.detail


def test_mcp_project_overrides_user(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "USER_CONFIG_PATH", tmp_path / "user.toml")
    (tmp_path / "user.toml").write_text(
        '[[mcp_servers]]\nname = "s"\ncommand = "user-cmd"\n',
        encoding="utf-8",
    )
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".eikocode.toml").write_text(
        '[[mcp_servers]]\nname = "s"\nurl = "http://proj/mcp"\n',
        encoding="utf-8",
    )
    config = load_config(cwd=project)
    assert len(config.mcp_servers) == 1
    assert config.mcp_servers[0].url == "http://proj/mcp"


def test_mcp_bad_config_collected_not_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "USER_CONFIG_PATH", tmp_path / "user.toml")
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".eikocode.toml").write_text(
        '[[mcp_servers]]\ncommand = "no-name"\n',
        encoding="utf-8",
    )
    config = load_config(cwd=project)
    assert config.mcp_servers == ()
    assert config.mcp_errors and "name" in config.mcp_errors[0]


# --------------------------------------------------------------------------- #
# 组 47：适配层
# --------------------------------------------------------------------------- #
def test_remote_tool_namespace_and_default_permission():
    registry = ToolRegistry()
    client_a = SimpleNamespace(call_tool=lambda n, a: {"content": [{"type": "text", "text": "a"}]})
    client_b = SimpleNamespace(call_tool=lambda n, a: {"content": [{"type": "text", "text": "b"}]})
    spec = {"name": "search", "description": "s", "inputSchema": {"type": "object", "properties": {}}}
    registry.register(RemoteTool("alpha", spec, client_a))  # 未声明权限 → write
    registry.register(RemoteTool("beta", spec, client_b))

    tool_a = registry.get("alpha__search")
    tool_b = registry.get("beta__search")
    assert tool_a is not None and tool_b is not None
    assert tool_a.permission is PermissionLevel.WRITE
    assert tool_a.execute({"q": "x"}) == "a"
    assert tool_b.execute({"q": "x"}) == "b"


def test_remote_tool_permission_from_config():
    tool = RemoteTool("docs", {"name": "t"}, SimpleNamespace(), "read")
    assert tool.permission is PermissionLevel.READ


def test_remote_tool_non_text_content():
    client = SimpleNamespace(call_tool=lambda n, a: {"content": [{"type": "image", "data": "x"}]})
    tool = RemoteTool("s", {"name": "t"}, client)
    with pytest.raises(EikoCodeError) as excinfo:
        tool.execute({})
    assert UNSUPPORTED_CONTENT in excinfo.value.detail


def test_remote_tool_is_error():
    client = SimpleNamespace(call_tool=lambda n, a: {"isError": True, "content": [{"type": "text", "text": "远端炸了"}]})
    tool = RemoteTool("s", {"name": "t"}, client)
    with pytest.raises(EikoCodeError) as excinfo:
        tool.execute({})
    assert "远端炸了" in excinfo.value.detail


def test_remote_tool_kill():
    killed = []
    client = SimpleNamespace(call_tool=lambda n, a: {}, _transport=SimpleNamespace(kill=lambda: killed.append(1)))
    tool = RemoteTool("s", {"name": "t"}, client)
    tool.kill()
    assert killed == [1]


# --------------------------------------------------------------------------- #
# E22：本地 stdio 三阶段（真实子进程夹具）
# --------------------------------------------------------------------------- #
def test_e22_stdio_three_stages():
    client = McpClient(_server_config())
    tools = client.connect()
    try:
        assert [t["name"] for t in tools] == ["echo"]
        result = client.call_tool("echo", {"text": "hello"})
        assert result["content"][0]["text"] == "echo:hello"

        registry = ToolRegistry()
        registry.register(RemoteTool("echo", tools[0], client, "write"))
        tool = registry.get("echo__echo")
        assert tool is not None
        assert tool.execute({"text": "world"}) == "echo:world"
    finally:
        client.close()
