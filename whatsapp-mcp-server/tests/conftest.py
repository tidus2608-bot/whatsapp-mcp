import os
import sqlite3
import sys

import pytest

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SERVER_DIR)

import whatsapp  # noqa: E402

# Same tables the bridge creates (whatsapp-bridge/main.go, NewMessageStore)
SCHEMA = """
CREATE TABLE chats (
    jid TEXT PRIMARY KEY,
    name TEXT,
    last_message_time TIMESTAMP
);
CREATE TABLE messages (
    id TEXT,
    chat_jid TEXT,
    sender TEXT,
    content TEXT,
    timestamp TIMESTAMP,
    is_from_me BOOLEAN,
    media_type TEXT,
    filename TEXT,
    url TEXT,
    media_key BLOB,
    file_sha256 BLOB,
    file_enc_sha256 BLOB,
    file_length INTEGER,
    PRIMARY KEY (id, chat_jid),
    FOREIGN KEY (chat_jid) REFERENCES chats(jid)
);
"""

ALICE = "447700900001@s.whatsapp.net"
GROUP = "120363000000000001@g.us"


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A message database shaped like the bridge's, with timestamps in its format (UTC offset included).

    Alice's chat: m0..m4, one second apart from 10:00:01+02:00 (08:00:01 UTC).
    Group chat: two messages from Alice, one stored as a bare number (live messages)
    and one as a full JID (older history syncs).
    """
    path = tmp_path / "messages.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO chats VALUES (?, 'Alice', '2025-04-01 10:00:05+02:00')", (ALICE,))
    conn.execute("INSERT INTO chats VALUES (?, 'Family', '2025-04-02 09:00:01+02:00')", (GROUP,))
    for i in range(5):
        conn.execute(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?, ?, ?, ?, ?, 0)",
            (f"m{i}", ALICE, "447700900001", f"msg {i}", f"2025-04-01 10:00:0{i + 1}+02:00"),
        )
    conn.execute(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?, ?, ?, ?, ?, 0)",
        ("g0", GROUP, "447700900001@s.whatsapp.net", "from history sync", "2025-04-02 09:00:00+02:00"),
    )
    conn.execute(
        "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?, ?, ?, ?, ?, 0)",
        ("g1", GROUP, "447700900001", "live message", "2025-04-02 09:00:01+02:00"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(whatsapp, "MESSAGES_DB_PATH", str(path))
    return path


@pytest.fixture
def bridge_files(tmp_path, monkeypatch):
    """Point the API token and outbox at temporary locations."""
    token_path = tmp_path / "api_token"
    token_path.write_text("file-token\n")
    outbox = tmp_path / "outbox"
    monkeypatch.setattr(whatsapp, "API_TOKEN_PATH", str(token_path))
    monkeypatch.setattr(whatsapp, "MEDIA_OUTBOX_DIR", str(outbox))
    monkeypatch.delenv("WHATSAPP_API_TOKEN", raising=False)
    return token_path, outbox
