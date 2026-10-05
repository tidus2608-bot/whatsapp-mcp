"""Run the real MCP server over stdio, the way Claude Desktop and Cursor do (audit findings B1 and B2)."""
import json
import os
import subprocess
import sys

from conftest import SERVER_DIR


def call_list_chats(db_path):
    """Start the server with its database at db_path, call list_chats and return every stdout line."""
    script = (
        "import whatsapp, main\n"
        f"whatsapp.MESSAGES_DB_PATH = {str(db_path)!r}\n"
        "main.mcp.run(transport='stdio')\n"
    )
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_chats", "arguments": {}}},
    ]
    proc = subprocess.Popen(
        [sys.executable, "-c", script], cwd=SERVER_DIR,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        for request in requests:
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
        out = []
        while True:
            line = proc.stdout.readline()
            out.append(line)
            if not line or '"id":2' in line.replace(" ", ""):
                return out
    finally:
        proc.kill()
        proc.communicate(timeout=10)


def test_missing_database_is_a_tool_error_and_stdout_stays_clean(tmp_path):
    lines = call_list_chats(tmp_path / "missing.db")

    messages = [json.loads(line) for line in lines]  # every stdout line must be JSON-RPC
    result = messages[-1]["result"]
    assert result["isError"] is True
    assert "Start the WhatsApp bridge" in result["content"][0]["text"]


def test_working_database_returns_chats(db):
    lines = call_list_chats(db)
    result = json.loads(lines[-1])["result"]
    assert result["isError"] is False
    # One content item per chat
    texts = [item["text"] for item in result["content"]]
    assert len(texts) == 2 and any("Alice" in text for text in texts)
