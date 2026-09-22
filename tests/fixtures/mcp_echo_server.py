# -*- coding: utf-8 -*-
"""v6 测试夹具：最小的 MCP 服务（stdio，纯标准库）。

按行读 JSON-RPC 请求，支持三阶段：
- initialize → 协商版本
- notifications/initialized → 忽略（通知无应答）
- tools/list → 一个 echo 工具
- tools/call → echo 回显文本 / boom 返回错误 / image 返回不支持的内容类型
"""
import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "回显输入文本",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文本"}},
            "required": ["text"],
        },
    }
]


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method", "")
        rid = req.get("id")
        if rid is None:
            continue  # 通知：不响应
        if method == "initialize":
            send({
                "jsonrpc": "2.0", "id": rid,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "echo-fixture", "version": "1.0"},
                },
            })
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = (req.get("params") or {}).get("name")
            arguments = (req.get("params") or {}).get("arguments") or {}
            if name == "echo":
                send({
                    "jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": "echo:" + str(arguments.get("text", ""))}]},
                })
            elif name == "boom":
                send({
                    "jsonrpc": "2.0", "id": rid,
                    "result": {"isError": True, "content": [{"type": "text", "text": "远端炸了"}]},
                })
            elif name == "image":
                send({
                    "jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "image", "data": "xx", "mimeType": "image/png"}]},
                })
            else:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "unknown tool"}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    main()
