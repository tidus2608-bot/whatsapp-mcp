"""Message database queries. Each test names the audit finding it covers."""
import os

import pytest

import whatsapp
from conftest import ALICE, GROUP


def lines(text):
    return [line for line in text.splitlines() if line]


# B2: a missing database is an error, not an empty result

def test_missing_database_raises_instead_of_returning_empty(tmp_path, monkeypatch):
    missing = tmp_path / "store" / "messages.db"
    monkeypatch.setattr(whatsapp, "MESSAGES_DB_PATH", str(missing))

    for call in (
        lambda: whatsapp.list_chats(),
        lambda: whatsapp.list_messages(),
        lambda: whatsapp.search_contacts("a"),
        lambda: whatsapp.get_chat(ALICE),
        lambda: whatsapp.get_contact_chats(ALICE),
        lambda: whatsapp.get_last_interaction(ALICE),
        lambda: whatsapp.get_direct_chat_by_contact("4477"),
        lambda: whatsapp.get_message_context("m0"),
    ):
        with pytest.raises(whatsapp.WhatsAppError, match="Start the WhatsApp bridge"):
            call()


def test_database_is_opened_read_only(tmp_path, monkeypatch):
    # The old code's sqlite3.connect() created an empty database when the path was wrong
    path = tmp_path / "messages.db"
    path.write_bytes(b"")
    monkeypatch.setattr(whatsapp, "MESSAGES_DB_PATH", str(path))
    with whatsapp._connect() as conn:
        with pytest.raises(Exception, match="readonly"):
            conn.execute("CREATE TABLE t (x)")


def test_sql_errors_raise(tmp_path, monkeypatch):
    path = tmp_path / "messages.db"
    path.write_bytes(b"")  # valid but empty database: no tables
    monkeypatch.setattr(whatsapp, "MESSAGES_DB_PATH", str(path))
    with pytest.raises(whatsapp.WhatsAppError, match="no such table"):
        whatsapp.list_chats()


# B1: nothing written to stdout (it carries the MCP protocol)

def test_queries_and_failures_do_not_print(db, tmp_path, monkeypatch, capsys):
    whatsapp.list_messages(query="msg")
    whatsapp.get_last_interaction(ALICE)
    monkeypatch.setattr(whatsapp, "MESSAGES_DB_PATH", str(tmp_path / "missing.db"))
    assert whatsapp.get_sender_name("447700900001") == "447700900001"
    with pytest.raises(whatsapp.WhatsAppError):
        whatsapp.list_chats()
    assert capsys.readouterr().out == ""


# B3: include_last_message=False

def test_list_chats_without_last_message(db):
    chats = whatsapp.list_chats(include_last_message=False)
    assert [c.jid for c in chats] == [GROUP, ALICE]
    assert all(c.last_message is None for c in chats)


def test_get_chat_without_last_message(db):
    chat = whatsapp.get_chat(ALICE, include_last_message=False)
    assert chat is not None and chat.name == "Alice" and chat.last_message is None


def test_get_chat_with_last_message(db):
    assert whatsapp.get_chat(ALICE).last_message == "msg 4"


# B5: each chat listed once

def test_get_contact_chats_lists_each_chat_once(db):
    chats = whatsapp.get_contact_chats(ALICE)
    assert sorted(c.jid for c in chats) == sorted([ALICE, GROUP])


# B6: context is chronological and not duplicated

def test_message_context_before_is_chronological(db):
    context = whatsapp.get_message_context("m4", before=3, after=0)
    assert [m.content for m in context.before] == ["msg 1", "msg 2", "msg 3"]


def test_list_messages_merges_overlapping_context(db):
    output = lines(whatsapp.list_messages(chat_jid=ALICE, query="msg", limit=3))
    contents = [line.rsplit(": ", 1)[1] for line in output]
    # Matches m4, m3, m2 plus one message of context either side, each shown once, in order
    assert contents == ["msg 1", "msg 2", "msg 3", "msg 4"]


def test_list_messages_without_context_is_newest_first(db):
    output = lines(whatsapp.list_messages(chat_jid=ALICE, include_context=False))
    assert [line.rsplit(": ", 1)[1] for line in output] == ["msg 4", "msg 3", "msg 2", "msg 1", "msg 0"]


# B7: date filters respect time zones

def test_date_filter_respects_utc_offsets(db):
    # Alice's messages are 08:00:01-08:00:05 UTC (stored as 10:00:0x+02:00)
    after_them = whatsapp.list_messages(chat_jid=ALICE, after="2025-04-01T09:00:00+00:00", include_context=False)
    assert after_them == "No messages to display."

    in_window = whatsapp.list_messages(
        chat_jid=ALICE, after="2025-04-01T08:00:02Z", before="2025-04-01T08:00:05Z", include_context=False
    )
    assert [line.rsplit(": ", 1)[1] for line in lines(in_window)] == ["msg 3", "msg 2"]


def test_invalid_date_is_rejected(db):
    with pytest.raises(ValueError, match="ISO-8601"):
        whatsapp.list_messages(after="yesterday")


# B8: senders stored as bare numbers and as full JIDs are both found

@pytest.mark.parametrize("sender", ["447700900001", "+44 7700 900001", ALICE, "447700900001:3@s.whatsapp.net"])
def test_sender_filter_matches_both_storage_forms(db, sender):
    output = whatsapp.list_messages(chat_jid=GROUP, sender_phone_number=sender, include_context=False)
    assert {line.rsplit(": ", 1)[1] for line in lines(output)} == {"from history sync", "live message"}


def test_last_interaction_matches_bare_number_sender(db):
    # Sender is stored as "447700900001" while the tool is given the full JID
    assert whatsapp.get_last_interaction(ALICE).endswith("live message\n")
