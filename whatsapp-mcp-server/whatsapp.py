import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, List, Tuple
import logging
import os.path
import re
import requests
import json
import audio

# Never print(): stdout carries the MCP protocol. Log records go to stderr.
logger = logging.getLogger(__name__)

BRIDGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge')
MESSAGES_DB_PATH = os.path.join(BRIDGE_DIR, 'store', 'messages.db')
# Written by the bridge on first start; sent with every API request
API_TOKEN_PATH = os.path.join(BRIDGE_DIR, 'store', 'api_token')
# The bridge only sends files from here (plus any folders in its WHATSAPP_MEDIA_DIRS)
MEDIA_OUTBOX_DIR = os.path.join(BRIDGE_DIR, 'outbox')
WHATSAPP_API_BASE_URL = "http://127.0.0.1:8080/api"

# Column order expected by _message_from_row
MESSAGE_COLUMNS = "messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type"

class WhatsAppError(Exception):
    """The message database or the bridge could not be used. Shown to the caller as a tool error."""

@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: Optional[str] = None
    media_type: Optional[str] = None

@dataclass
class Chat:
    jid: str
    name: Optional[str]
    last_message_time: Optional[datetime]
    last_message: Optional[str] = None
    last_sender: Optional[str] = None
    last_is_from_me: Optional[bool] = None

    @property
    def is_group(self) -> bool:
        """Determine if chat is a group based on JID pattern."""
        return self.jid.endswith("@g.us")

@dataclass
class Contact:
    phone_number: str
    name: Optional[str]
    jid: str

@dataclass
class MessageContext:
    message: Message
    before: List[Message]
    after: List[Message]

@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Open the bridge's message database read-only.

    Raises WhatsAppError instead of returning empty results, so a missing or broken
    database is reported as an error rather than as "no messages".
    """
    if not os.path.isfile(MESSAGES_DB_PATH):
        raise WhatsAppError(
            f"WhatsApp message database not found at {os.path.abspath(MESSAGES_DB_PATH)}. "
            "Start the WhatsApp bridge (whatsapp-bridge) so it can create it."
        )
    try:
        conn = sqlite3.connect(f"{Path(MESSAGES_DB_PATH).resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise WhatsAppError(f"Could not open WhatsApp message database: {e}") from e
    try:
        yield conn
    except sqlite3.Error as e:
        raise WhatsAppError(f"Database error: {e}") from e
    finally:
        conn.close()

def _message_from_row(row: tuple) -> Message:
    return Message(
        timestamp=datetime.fromisoformat(row[0]),
        sender=row[1],
        chat_name=row[2],
        content=row[3],
        is_from_me=row[4],
        chat_jid=row[5],
        id=row[6],
        media_type=row[7]
    )

def _chat_from_row(row: tuple) -> Chat:
    return Chat(
        jid=row[0],
        name=row[1],
        last_message_time=datetime.fromisoformat(row[2]) if row[2] else None,
        last_message=row[3],
        last_sender=row[4],
        last_is_from_me=row[5]
    )

def _jid_user(phone_or_jid: str) -> str:
    """Reduce a phone number or JID to the bare number the bridge stores as the sender."""
    user = phone_or_jid.strip().split('@')[0].split(':')[0]
    return re.sub(r"[\s()+-]", "", user)

def _sender_filter(column: str, phone_or_jid: str) -> Tuple[str, List[str]]:
    """SQL matching a sender stored as a bare number, or as a full JID (rows from older history syncs)."""
    user = _jid_user(phone_or_jid)
    return f"({column} = ? OR {column} LIKE ? OR {column} LIKE ?)", [user, f"{user}@%", f"{user}:%"]

def _filter_time(value: str, name: str) -> str:
    """Convert an ISO-8601 filter value to a UTC time string for SQLite's julianday().

    The bridge stores timestamps with their UTC offset, so comparing through julianday()
    respects time zones where comparing the raw text would not. Values without an offset
    are taken as this machine's local time.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Invalid date format for '{name}': {value}. Please use ISO-8601 format.")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

def get_sender_name(sender_jid: str) -> str:
    try:
        with _connect() as conn:
            cursor = conn.cursor()

            # First try matching by exact JID
            cursor.execute("""
                SELECT name
                FROM chats
                WHERE jid = ?
                LIMIT 1
            """, (sender_jid,))

            result = cursor.fetchone()

            # If no result, try looking for the number within JIDs
            if not result:
                # Extract the phone number part if it's a JID
                if '@' in sender_jid:
                    phone_part = sender_jid.split('@')[0]
                else:
                    phone_part = sender_jid

                cursor.execute("""
                    SELECT name
                    FROM chats
                    WHERE jid LIKE ?
                    LIMIT 1
                """, (f"%{phone_part}%",))

                result = cursor.fetchone()

        if result and result[0]:
            return result[0]
        else:
            return sender_jid

    except WhatsAppError as e:
        # A name is a nicety: fall back to the raw sender rather than failing the whole listing
        logger.warning("Could not look up sender name: %s", e)
        return sender_jid

def format_message(message: Message, show_chat_info: bool = True) -> str:
    """Format a single message as one line of text."""
    output = ""

    if show_chat_info and message.chat_name:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {message.chat_name} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "

    content_prefix = ""
    if hasattr(message, 'media_type') and message.media_type:
        content_prefix = f"[{message.media_type} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "

    try:
        sender_name = get_sender_name(message.sender) if not message.is_from_me else "Me"
        output += f"From: {sender_name}: {content_prefix}{message.content}\n"
    except Exception as e:
        logger.warning("Error formatting message: %s", e)
    return output

def format_messages_list(messages: List[Message], show_chat_info: bool = True) -> str:
    output = ""
    if not messages:
        output += "No messages to display."
        return output

    for message in messages:
        output += format_message(message, show_chat_info)
    return output

def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1
) -> str:
    """Get messages matching the specified criteria with optional context."""
    # Build base query
    query_parts = [f"SELECT {MESSAGE_COLUMNS} FROM messages"]
    query_parts.append("JOIN chats ON messages.chat_jid = chats.jid")
    where_clauses = []
    params = []

    # Add filters
    if after:
        where_clauses.append("julianday(messages.timestamp) > julianday(?)")
        params.append(_filter_time(after, 'after'))

    if before:
        where_clauses.append("julianday(messages.timestamp) < julianday(?)")
        params.append(_filter_time(before, 'before'))

    if sender_phone_number:
        clause, clause_params = _sender_filter("messages.sender", sender_phone_number)
        where_clauses.append(clause)
        params.extend(clause_params)

    if chat_jid:
        where_clauses.append("messages.chat_jid = ?")
        params.append(chat_jid)

    if query:
        where_clauses.append("LOWER(messages.content) LIKE LOWER(?)")
        params.append(f"%{query}%")

    if where_clauses:
        query_parts.append("WHERE " + " AND ".join(where_clauses))

    # Add pagination
    offset = page * limit
    query_parts.append("ORDER BY messages.timestamp DESC")
    query_parts.append("LIMIT ? OFFSET ?")
    params.extend([limit, offset])

    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(" ".join(query_parts), tuple(params))
        result = [_message_from_row(row) for row in cursor.fetchall()]

    if include_context and result:
        # Merge overlapping context windows: each message appears once, chats are ordered
        # by their newest match and messages within a chat run oldest to newest.
        seen = set()
        chat_order = {}
        messages_with_context = []
        for msg in result:
            context = get_message_context(msg.id, context_before, context_after, chat_jid=msg.chat_jid)
            for m in [*context.before, context.message, *context.after]:
                key = (m.chat_jid, m.id)
                if key in seen:
                    continue
                seen.add(key)
                chat_order.setdefault(m.chat_jid, len(chat_order))
                messages_with_context.append(m)

        messages_with_context.sort(key=lambda m: (chat_order[m.chat_jid], m.timestamp.timestamp()))
        return format_messages_list(messages_with_context, show_chat_info=True)

    # Format and display messages without context
    return format_messages_list(result, show_chat_info=True)


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5,
    chat_jid: Optional[str] = None
) -> MessageContext:
    """Get context around a specific message.

    Message IDs are only unique within a chat, so pass chat_jid when it is known.
    """
    with _connect() as conn:
        cursor = conn.cursor()

        # Get the target message first
        target_query = f"""
            SELECT {MESSAGE_COLUMNS}
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.id = ?
        """
        target_params = [message_id]
        if chat_jid:
            target_query += " AND messages.chat_jid = ?"
            target_params.append(chat_jid)
        cursor.execute(target_query, tuple(target_params))
        msg_data = cursor.fetchone()

        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")

        target_message = _message_from_row(msg_data)

        # Get messages before (newest first, so LIMIT keeps the closest ones)
        cursor.execute(f"""
            SELECT {MESSAGE_COLUMNS}
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp < ?
            ORDER BY messages.timestamp DESC
            LIMIT ?
        """, (target_message.chat_jid, msg_data[0], before))

        # ...then put them back in chronological order
        before_messages = [_message_from_row(row) for row in cursor.fetchall()]
        before_messages.reverse()

        # Get messages after
        cursor.execute(f"""
            SELECT {MESSAGE_COLUMNS}
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp > ?
            ORDER BY messages.timestamp ASC
            LIMIT ?
        """, (target_message.chat_jid, msg_data[0], after))

        after_messages = [_message_from_row(row) for row in cursor.fetchall()]

    return MessageContext(
        message=target_message,
        before=before_messages,
        after=after_messages
    )


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Chat]:
    """Get chats matching the specified criteria."""
    # Without the join there is no messages table to read the last message from
    last_message_columns = (
        "messages.content, messages.sender, messages.is_from_me"
        if include_last_message else "NULL, NULL, NULL"
    )

    # Build base query
    query_parts = [f"""
        SELECT
            chats.jid,
            chats.name,
            chats.last_message_time,
            {last_message_columns}
        FROM chats
    """]

    if include_last_message:
        query_parts.append("""
            LEFT JOIN messages ON chats.jid = messages.chat_jid
            AND chats.last_message_time = messages.timestamp
        """)

    where_clauses = []
    params = []

    if query:
        where_clauses.append("(LOWER(chats.name) LIKE LOWER(?) OR chats.jid LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])

    if where_clauses:
        query_parts.append("WHERE " + " AND ".join(where_clauses))

    # Add sorting
    order_by = "chats.last_message_time DESC" if sort_by == "last_active" else "chats.name"
    query_parts.append(f"ORDER BY {order_by}")

    # Add pagination
    offset = page * limit
    query_parts.append("LIMIT ? OFFSET ?")
    params.extend([limit, offset])

    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(" ".join(query_parts), tuple(params))
        return [_chat_from_row(row) for row in cursor.fetchall()]


def search_contacts(query: str) -> List[Contact]:
    """Search contacts by name or phone number."""
    # Split query into characters to support partial matching
    search_pattern = '%' + query + '%'

    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT
                jid,
                name
            FROM chats
            WHERE
                (LOWER(name) LIKE LOWER(?) OR LOWER(jid) LIKE LOWER(?))
                AND jid NOT LIKE '%@g.us'
            ORDER BY name, jid
            LIMIT 50
        """, (search_pattern, search_pattern))

        contacts = cursor.fetchall()

    result = []
    for contact_data in contacts:
        contact = Contact(
            phone_number=contact_data[0].split('@')[0],
            name=contact_data[1],
            jid=contact_data[0]
        )
        result.append(contact)

    return result


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Chat]:
    """Get all chats involving the contact, once each.

    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    sender_clause, sender_params = _sender_filter("sender", jid)

    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            LEFT JOIN messages m ON c.jid = m.chat_jid
                AND c.last_message_time = m.timestamp
            WHERE c.jid = ?
                OR c.jid IN (SELECT chat_jid FROM messages WHERE {sender_clause})
            GROUP BY c.jid
            ORDER BY c.last_message_time DESC
            LIMIT ? OFFSET ?
        """, (jid, *sender_params, limit, page * limit))

        return [_chat_from_row(row) for row in cursor.fetchall()]


def get_last_interaction(jid: str) -> Optional[str]:
    """Get most recent message involving the contact."""
    sender_clause, sender_params = _sender_filter("messages.sender", jid)

    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT {MESSAGE_COLUMNS}
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE {sender_clause} OR chats.jid = ?
            ORDER BY messages.timestamp DESC
            LIMIT 1
        """, (*sender_params, jid))

        msg_data = cursor.fetchone()

    if not msg_data:
        return None

    return format_message(_message_from_row(msg_data))


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    """Get chat metadata by JID."""
    # Without the join there is no messages table to read the last message from
    last_message_columns = (
        "m.content as last_message, m.sender as last_sender, m.is_from_me as last_is_from_me"
        if include_last_message else "NULL, NULL, NULL"
    )

    query = f"""
        SELECT
            c.jid,
            c.name,
            c.last_message_time,
            {last_message_columns}
        FROM chats c
    """

    if include_last_message:
        query += """
            LEFT JOIN messages m ON c.jid = m.chat_jid
            AND c.last_message_time = m.timestamp
        """

    query += " WHERE c.jid = ?"

    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(query, (chat_jid,))
        chat_data = cursor.fetchone()

    if not chat_data:
        return None

    return _chat_from_row(chat_data)


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    """Get chat metadata by sender phone number."""
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            LEFT JOIN messages m ON c.jid = m.chat_jid
                AND c.last_message_time = m.timestamp
            WHERE c.jid LIKE ? AND c.jid NOT LIKE '%@g.us'
            LIMIT 1
        """, (f"%{sender_phone_number}%",))

        chat_data = cursor.fetchone()

    if not chat_data:
        return None

    return _chat_from_row(chat_data)

def _api_headers() -> dict:
    """Authorization header for the bridge's REST API."""
    token = os.environ.get("WHATSAPP_API_TOKEN", "").strip()
    if not token:
        try:
            with open(API_TOKEN_PATH) as f:
                token = f.read().strip()
        except OSError:
            token = ""
    if not token:
        raise WhatsAppError(
            f"WhatsApp bridge API token not found at {os.path.abspath(API_TOKEN_PATH)}. "
            "Start the WhatsApp bridge (whatsapp-bridge) first, "
            "or set WHATSAPP_API_TOKEN to the same value for the bridge and this server."
        )
    return {"Authorization": f"Bearer {token}"}

def _send_via_bridge(payload: dict) -> Tuple[bool, str]:
    """Ask the bridge to send a message or file. Returns (success, status message)."""
    try:
        url = f"{WHATSAPP_API_BASE_URL}/send"
        response = requests.post(url, json=payload, headers=_api_headers())

        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"

    except WhatsAppError as e:
        return False, str(e)
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_message(recipient: str, message: str) -> Tuple[bool, str]:
    # Validate input
    if not recipient:
        return False, "Recipient must be provided"

    return _send_via_bridge({
        "recipient": recipient,
        "message": message,
    })

def send_file(recipient: str, media_path: str) -> Tuple[bool, str]:
    # Validate input
    if not recipient:
        return False, "Recipient must be provided"

    if not media_path:
        return False, "Media path must be provided"

    if not os.path.isfile(media_path):
        return False, f"Media file not found: {media_path}"

    return _send_via_bridge({
        "recipient": recipient,
        "media_path": media_path
    })

def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    # Validate input
    if not recipient:
        return False, "Recipient must be provided"

    if not media_path:
        return False, "Media path must be provided"

    if not os.path.isfile(media_path):
        return False, f"Media file not found: {media_path}"

    converted_path = None
    if not media_path.endswith(".ogg"):
        try:
            # Convert into the outbox, the folder the bridge always accepts files from
            os.makedirs(MEDIA_OUTBOX_DIR, exist_ok=True)
            converted_path = audio.convert_to_opus_ogg_temp(media_path, output_dir=MEDIA_OUTBOX_DIR)
        except Exception as e:
            return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"
        media_path = converted_path

    try:
        return _send_via_bridge({
            "recipient": recipient,
            "media_path": media_path
        })
    finally:
        # The bridge has read the file by the time it responds
        if converted_path and os.path.exists(converted_path):
            os.unlink(converted_path)

def download_media(message_id: str, chat_jid: str) -> Tuple[bool, str, Optional[str]]:
    """Download media from a message.

    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message

    Returns:
        (success, status message, local file path or None)
    """
    try:
        url = f"{WHATSAPP_API_BASE_URL}/download"
        payload = {
            "message_id": message_id,
            "chat_jid": chat_jid
        }

        response = requests.post(url, json=payload, headers=_api_headers())

        try:
            result = response.json()
        except ValueError:
            return False, f"Error: HTTP {response.status_code} - {response.text}", None

        if response.status_code == 200 and result.get("success", False):
            return True, result.get("message", "Media downloaded successfully"), result.get("path")
        return False, result.get("message") or f"Error: HTTP {response.status_code}", None

    except WhatsAppError as e:
        return False, str(e), None
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}", None
    except Exception as e:
        return False, f"Unexpected error: {str(e)}", None
