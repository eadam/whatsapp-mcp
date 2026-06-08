import json
import os
import os.path
import sqlite3
import traceback
import warnings
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
from PIL import Image as PilImage
from PIL import ImageOps

import audio

# Configuration via environment variables with sensible defaults
MESSAGES_DB_PATH = os.getenv(
    "WHATSAPP_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "whatsapp-bridge", "store", "messages.db"),
)
WHATSMEOW_DB_PATH = os.getenv(
    "WHATSMEOW_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "whatsapp-bridge", "store", "whatsapp.db"),
)
WHATSAPP_API_BASE_URL = os.getenv("WHATSAPP_API_URL", "http://localhost:8080/api")

_BRIDGE_TOKEN_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "whatsapp-bridge", "store", ".bridge-token"
)


def _read_bridge_token() -> str | None:
    env = os.getenv("WHATSAPP_BRIDGE_TOKEN", "").strip()
    if env:
        return env
    try:
        with open(_BRIDGE_TOKEN_PATH, encoding="utf-8") as fh:
            value = fh.read().strip()
            return value or None
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _bridge_headers() -> dict[str, str]:
    token = _read_bridge_token()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


# ── Media-download helpers ────────────────────────────────────────────────────
# NOTE: _normalize_jid MUST be defined before _load_download_allowlist() because
# the allowlist loader calls it at module-import time.

# Container path where the Go bridge writes downloaded media.
# Must match messageStore.storeRoot (resolved path of /opt/whatsapp-mcp/whatsapp-bridge/store).
MEDIA_STORE_BASE = "/opt/whatsapp-mcp/whatsapp-bridge/store"

# Preview generation constraints.
PREVIEW_MAX_DIM = 1600  # px — longer side capped at this during thumbnail()
PREVIEW_MAX_BYTES = 1_500_000  # hard ceiling on preview output bytes
PREVIEW_JPEG_Q = 82  # starting JPEG quality; retried at 65 then 45 if still over cap

# Pillow format → MCP Image format string.
PILLOW_FORMAT_TO_FMT: dict[str, str] = {
    "JPEG": "jpeg",
    "PNG": "png",
    "GIF": "gif",
    "WEBP": "webp",
}


def _normalize_jid(raw: str) -> str:
    """Normalize a raw JID or phone number to canonical '<digits>@s.whatsapp.net' form.

    Mirrors Go's normalizeRecipient():
    - Strips leading +
    - Strips :device suffix
    - Asserts all-digit user part
    - Asserts length 6–15 digits
    - Appends @s.whatsapp.net

    Raises ValueError for groups (@g.us), @lid, malformed, or empty inputs.
    """
    s = raw.strip()
    if not s:
        raise ValueError("empty JID")

    # If already a JID with an explicit server, validate the server first.
    if "@" in s:
        user, server = s.split("@", 1)
        if server.lower() != "s.whatsapp.net":
            raise ValueError(f"non-personal JID server {server!r} (groups/@lid not allowed)")
        s = user  # continue normalizing the user part

    # Strip leading + and :device suffix.
    s = s.lstrip("+")
    if ":" in s:
        s = s.split(":")[0]

    if not s:
        raise ValueError(f"no digits in JID {raw!r}")
    if not s.isdigit():
        raise ValueError(f"JID user part is not all-digits: {s!r}")
    if not (6 <= len(s) <= 15):
        raise ValueError(f"JID digit count {len(s)} outside valid range 6-15: {s!r}")

    return f"{s}@s.whatsapp.net"


def _parse_max_download_bytes() -> int:
    """Parse WHATSAPP_MAX_DOWNLOAD_BYTES.

    Missing/empty → 5 MiB. "0" → deny all. Negative/invalid → deny all + log.
    """
    raw = os.environ.get("WHATSAPP_MAX_DOWNLOAD_BYTES", "").strip()
    if raw == "":
        return 5 * 1024 * 1024  # 5 MiB default
    try:
        v = int(raw)
        if v < 0:
            raise ValueError("negative value")
        return v  # 0 = deny all
    except ValueError:
        print(f"guard: WHATSAPP_MAX_DOWNLOAD_BYTES={raw!r} is invalid; denying all downloads (fail-closed)")
        return 0


# Evaluated once at module load time.
MEDIA_SIZE_CAP_BYTES: int = _parse_max_download_bytes()


def _load_download_allowlist() -> tuple[set[str], bool]:
    """Parse WHATSAPP_MEDIA_DOWNLOAD_ALLOWED_CHATS.

    Returns (allowed_jids, allow_all).
    - "*"   → (set(), True)  — allow-all sentinel
    - empty → (set(), False) — deny all, log warning
    - else  → (set of canonical JIDs, False)
    """
    raw = os.environ.get("WHATSAPP_MEDIA_DOWNLOAD_ALLOWED_CHATS", "").strip()
    if raw == "*":
        return (set(), True)

    jids: set[str] = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            jids.add(_normalize_jid(entry))
        except ValueError as e:
            print(f"media-download allowlist: skipping {entry!r}: {e}")

    if not jids:
        print("guard: WARNING media-download allowlist EMPTY — all downloads denied (fail-closed)")

    return (jids, False)


MEDIA_DOWNLOAD_ALLOWED_JIDS: set[str]
MEDIA_DOWNLOAD_ALLOW_ALL: bool
MEDIA_DOWNLOAD_ALLOWED_JIDS, MEDIA_DOWNLOAD_ALLOW_ALL = _load_download_allowlist()


@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: str | None = None
    media_type: str | None = None
    # ID of the message this one is replying to (NULL for non-replies).
    quoted_message_id: str | None = None


@dataclass
class Chat:
    jid: str
    name: str | None
    last_message_time: datetime | None
    last_message: str | None = None
    last_sender: str | None = None
    last_is_from_me: bool | None = None

    @property
    def is_group(self) -> bool:
        """Determine if chat is a group based on JID pattern."""
        return self.jid.endswith("@g.us")


@dataclass
class Contact:
    phone_number: str
    name: str | None
    jid: str


@dataclass
class MessageContext:
    message: Message
    before: list[Message]
    after: list[Message]


def msg_to_dict(message: Message, include_sender_name: bool = True) -> dict[str, Any]:
    """Convert a Message dataclass to a dictionary for JSON serialization."""
    # Extract phone number from JID (e.g., "1234567890@s.whatsapp.net" -> "1234567890")
    sender_phone = message.sender.split("@")[0] if "@" in message.sender else message.sender

    sender_name = None
    sender_display = None
    if include_sender_name:
        if message.is_from_me:
            sender_name = "Me"
            sender_display = "Me"
        else:
            resolved_name = get_sender_name(message.sender)
            # Check if we got an actual name (not just the JID back)
            if resolved_name and resolved_name != message.sender and resolved_name != sender_phone:
                sender_name = resolved_name
                sender_display = f"{resolved_name} ({sender_phone})"
            else:
                sender_name = sender_phone
                sender_display = sender_phone

    return {
        "id": message.id,
        "timestamp": message.timestamp.isoformat(),
        "sender_jid": message.sender,
        "sender_phone": sender_phone,
        "sender_name": sender_name,
        "sender_display": sender_display,  # "Name (phone)" or just phone if no name
        "content": message.content,
        "is_from_me": message.is_from_me,
        "chat_jid": message.chat_jid,
        "chat_name": message.chat_name,
        "media_type": message.media_type,
        "quoted_message_id": message.quoted_message_id,
    }


def chat_to_dict(chat: "Chat") -> dict[str, Any]:
    """Convert a Chat dataclass to a dictionary for JSON serialization."""
    return {
        "jid": chat.jid,
        "name": chat.name,
        "is_group": chat.is_group,
        "last_message_time": chat.last_message_time.isoformat() if chat.last_message_time else None,
        "last_message": chat.last_message,
        "last_sender": chat.last_sender,
        "last_is_from_me": chat.last_is_from_me,
    }


def contact_to_dict(contact: "Contact") -> dict[str, Any]:
    """Convert a Contact dataclass to a dictionary for JSON serialization."""
    return {"phone_number": contact.phone_number, "name": contact.name, "jid": contact.jid}


def _sender_aliases(value: str) -> list[str]:
    # messages.sender is written inconsistently: the same contact may appear as
    # bare phone ("13232432100"), full phone JID ("13232432100@s.whatsapp.net"),
    # bare LID ("231241139937355"), or full LID JID ("231241139937355@lid").
    # whatsmeow_lid_map (whatsapp.db) maps pn<->lid; we emit all four forms so
    # an IN-based filter catches every row regardless of which form was stored.
    bare = value.split("@", 1)[0]
    pn: str | None = None
    lid: str | None = None
    if os.path.isfile(WHATSMEOW_DB_PATH):
        try:
            conn = sqlite3.connect(WHATSMEOW_DB_PATH)
            try:
                row = conn.execute("SELECT lid FROM whatsmeow_lid_map WHERE pn = ?", (bare,)).fetchone()
                if row:
                    pn, lid = bare, row[0]
                else:
                    row = conn.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ?", (bare,)).fetchone()
                    if row:
                        lid, pn = bare, row[0]
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    aliases: list[str] = []
    if pn:
        aliases += [pn, f"{pn}@s.whatsapp.net"]
    if lid:
        aliases += [lid, f"{lid}@lid"]
    if not aliases:
        # No mapping found; emit the bare form plus both possible suffixes so
        # we still match whichever form the bridge happened to store.
        aliases = [bare, f"{bare}@s.whatsapp.net", f"{bare}@lid"]
    return aliases


def _resolve_lid_to_phone(lid_or_jid: str) -> str | None:
    """Resolve a WhatsApp LID (linked device identifier) to a phone number.

    WhatsApp's newer protocol uses opaque LIDs (e.g. '35047067385985') as sender
    identifiers instead of phone numbers. The whatsmeow_lid_map table maps these
    back to real phone numbers.

    Returns the phone number if found, None otherwise.
    """
    if not os.path.exists(WHATSMEOW_DB_PATH):
        return None
    # Extract the numeric part from JID-style strings (e.g. '35047067385985@lid')
    lid = lid_or_jid.split("@")[0] if "@" in lid_or_jid else lid_or_jid
    try:
        conn = sqlite3.connect(WHATSMEOW_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ? LIMIT 1", (lid,))
        row = cursor.fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        if "conn" in locals():
            conn.close()


def _resolve_name_from_whatsmeow(jid: str) -> str | None:
    """Look up a contact name from whatsmeow's contact store (whatsapp.db).

    Handles both standard JIDs (12345@s.whatsapp.net) and LIDs (opaque numeric
    identifiers used by WhatsApp's linked device protocol). LIDs are first
    resolved to phone numbers via whatsmeow_lid_map, then looked up in contacts.

    Falls back gracefully if the DB or table doesn't exist.
    """
    if not os.path.exists(WHATSMEOW_DB_PATH):
        return None

    lookup_jid = jid
    jid_prefix = jid.split("@")[0] if "@" in jid else jid
    jid_suffix = jid.split("@")[1] if "@" in jid else ""

    # If this is a LID (@lid suffix) or a raw number, try LID map first.
    # LIDs overlap in length with phone numbers (12-15 digits) so we always
    # attempt LID resolution and fall through to direct contact lookup if not found.
    if jid_suffix in ("lid", ""):
        phone = _resolve_lid_to_phone(jid_prefix)
        if phone:
            lookup_jid = phone + "@s.whatsapp.net"
        elif jid_suffix == "lid":
            # Definitely a LID but not in the map — can't resolve
            return None

    try:
        conn = sqlite3.connect(WHATSMEOW_DB_PATH)
        cursor = conn.cursor()
        # whatsmeow_contacts columns: our_jid, their_jid, first_name, full_name, push_name, business_name
        cursor.execute(
            "SELECT full_name, push_name, first_name, business_name FROM whatsmeow_contacts WHERE their_jid = ? LIMIT 1",
            (lookup_jid,),
        )
        row = cursor.fetchone()
        if row:
            # Prefer full_name, then push_name, then first_name, then business_name
            return row[0] or row[1] or row[2] or row[3] or None
        return None
    except sqlite3.Error:
        return None
    finally:
        if "conn" in locals():
            conn.close()


def get_sender_name(sender_jid: str) -> str:
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        # First try matching by exact JID
        cursor.execute(
            """
            SELECT name
            FROM chats
            WHERE jid = ?
            LIMIT 1
        """,
            (sender_jid,),
        )

        result = cursor.fetchone()

        # If no result, try looking for the number within JIDs
        if not result:
            # Extract the phone number part if it's a JID
            if "@" in sender_jid:
                phone_part = sender_jid.split("@")[0]
            else:
                phone_part = sender_jid

            cursor.execute(
                """
                SELECT name
                FROM chats
                WHERE jid LIKE ?
                LIMIT 1
            """,
                (f"%{phone_part}%",),
            )

            result = cursor.fetchone()

        if result and result[0] and not result[0].replace("+", "").isdigit():
            return result[0]

        # Fall back to whatsmeow contact store
        whatsmeow_name = _resolve_name_from_whatsmeow(sender_jid)
        if whatsmeow_name:
            return whatsmeow_name

        # Try with @s.whatsapp.net suffix if bare number
        if "@" not in sender_jid:
            whatsmeow_name = _resolve_name_from_whatsmeow(sender_jid + "@s.whatsapp.net")
            if whatsmeow_name:
                return whatsmeow_name

        return sender_jid

    except sqlite3.Error as e:
        print(f"Database error while getting sender name: {e}")
        return sender_jid
    finally:
        if "conn" in locals():
            conn.close()


def format_message(message: Message, show_chat_info: bool = True) -> None:
    """Print a single message with consistent formatting."""
    output = ""

    if show_chat_info and message.chat_name:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {message.chat_name} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "

    content_prefix = ""
    if hasattr(message, "media_type") and message.media_type:
        content_prefix = f"[{message.media_type} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "

    try:
        sender_name = get_sender_name(message.sender) if not message.is_from_me else "Me"
        output += f"From: {sender_name}: {content_prefix}{message.content}\n"
    except Exception as e:
        print(f"Error formatting message: {e}")
    return output


def format_messages_list(messages: list[Message], show_chat_info: bool = True) -> None:
    output = ""
    if not messages:
        output += "No messages to display."
        return output

    for message in messages:
        output += format_message(message, show_chat_info)
    return output


def list_messages(
    after: str | None = None,
    before: str | None = None,
    sender_phone_number: str | None = None,
    chat_jid: str | None = None,
    query: str | None = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1,
    sort_by: str = "newest",
) -> list[dict[str, Any]]:
    """Get messages matching the specified criteria with optional context.

    Args:
        after: Optional ISO-8601 formatted string to only return messages after this date
        before: Optional ISO-8601 formatted string to only return messages before this date
        sender_phone_number: Optional phone number to filter messages by sender
        chat_jid: Optional chat JID to filter messages by chat
        query: Optional search term to filter messages by content
        limit: Maximum number of messages to return (default 20)
        page: Page number for pagination (default 0)
        include_context: Whether to include messages before and after matches (default True)
        context_before: Number of messages to include before each match (default 1)
        context_after: Number of messages to include after each match (default 1)
        sort_by: Sort order - "newest" (default) or "oldest" for chronological ordering

    Returns:
        List of message dictionaries with id, timestamp, sender, content, etc.
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        # Build base query
        query_parts = [
            "SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type, messages.quoted_message_id FROM messages"
        ]
        query_parts.append("JOIN chats ON messages.chat_jid = chats.jid")
        where_clauses = []
        params = []

        # Add filters
        if after:
            try:
                after = datetime.fromisoformat(after)
            except ValueError:
                raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")

            where_clauses.append("messages.timestamp > ?")
            params.append(after)

        if before:
            try:
                before = datetime.fromisoformat(before)
            except ValueError:
                raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.")

            where_clauses.append("messages.timestamp < ?")
            params.append(before)

        if sender_phone_number:
            aliases = _sender_aliases(sender_phone_number)
            placeholders = ",".join("?" * len(aliases))
            where_clauses.append(f"messages.sender IN ({placeholders})")
            params.extend(aliases)

        if chat_jid:
            where_clauses.append("messages.chat_jid = ?")
            params.append(chat_jid)

        if query:
            # SQLite's LOWER() only handles ASCII, so LIKE LOWER(...) silently
            # excludes Unicode matches. instr() on the raw column preserves them.
            where_clauses.append("(instr(LOWER(messages.content), LOWER(?)) > 0 OR instr(messages.content, ?) > 0)")
            params.extend([query, query])

        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))

        # Add sorting and pagination
        offset = page * limit
        order = "DESC" if sort_by == "newest" else "ASC"
        query_parts.append(f"ORDER BY messages.timestamp {order}")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])

        cursor.execute(" ".join(query_parts), tuple(params))
        messages = cursor.fetchall()

        result = []
        for msg in messages:
            message = Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7],
                quoted_message_id=msg[8] if len(msg) > 8 else None,
            )
            result.append(message)

        if include_context and result:
            # Add context for each message, deduplicated by message ID
            seen_ids = set()
            messages_with_context = []
            for msg in result:
                context = get_message_context(msg.id, context_before, context_after)
                for ctx_msg in context.before:
                    if ctx_msg.id not in seen_ids:
                        seen_ids.add(ctx_msg.id)
                        messages_with_context.append(ctx_msg)
                if context.message.id not in seen_ids:
                    seen_ids.add(context.message.id)
                    messages_with_context.append(context.message)
                for ctx_msg in context.after:
                    if ctx_msg.id not in seen_ids:
                        seen_ids.add(ctx_msg.id)
                        messages_with_context.append(ctx_msg)

            return [msg_to_dict(msg) for msg in messages_with_context]

        # Return messages without context
        return [msg_to_dict(msg) for msg in result]

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if "conn" in locals():
            conn.close()


def get_message_context(message_id: str, before: int = 5, after: int = 5) -> MessageContext:
    """Get context around a specific message."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        # Get the target message first
        cursor.execute(
            """
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.chat_jid, messages.media_type, messages.quoted_message_id
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.id = ?
        """,
            (message_id,),
        )
        msg_data = cursor.fetchone()

        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")

        target_message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[8],
            quoted_message_id=msg_data[9] if len(msg_data) > 9 else None,
        )

        # Get messages before
        cursor.execute(
            """
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type, messages.quoted_message_id
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp < ?
            ORDER BY messages.timestamp DESC
            LIMIT ?
        """,
            (msg_data[7], msg_data[0], before),
        )

        before_messages = []
        for msg in cursor.fetchall():
            before_messages.append(
                Message(
                    timestamp=datetime.fromisoformat(msg[0]),
                    sender=msg[1],
                    chat_name=msg[2],
                    content=msg[3],
                    is_from_me=msg[4],
                    chat_jid=msg[5],
                    id=msg[6],
                    media_type=msg[7],
                    quoted_message_id=msg[8] if len(msg) > 8 else None,
                )
            )

        # Get messages after
        cursor.execute(
            """
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type, messages.quoted_message_id
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp > ?
            ORDER BY messages.timestamp ASC
            LIMIT ?
        """,
            (msg_data[7], msg_data[0], after),
        )

        after_messages = []
        for msg in cursor.fetchall():
            after_messages.append(
                Message(
                    timestamp=datetime.fromisoformat(msg[0]),
                    sender=msg[1],
                    chat_name=msg[2],
                    content=msg[3],
                    is_from_me=msg[4],
                    chat_jid=msg[5],
                    id=msg[6],
                    media_type=msg[7],
                    quoted_message_id=msg[8] if len(msg) > 8 else None,
                )
            )

        return MessageContext(message=target_message, before=before_messages, after=after_messages)

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        raise
    finally:
        if "conn" in locals():
            conn.close()


def list_chats(
    query: str | None = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active",
) -> list[dict[str, Any]]:
    """Get chats matching the specified criteria.

    Returns:
        List of chat dictionaries with jid, name, is_group, last_message, etc.
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        # Build base query. The last-message columns are referenced by tuple
        # index downstream, so we keep the result shape constant and emit
        # static NULLs when the messages table is not joined — otherwise the
        # SELECT references messages.* with no FROM/JOIN and SQLite errors
        # out with "no such column: messages.content".
        if include_last_message:
            last_message_select = (
                "messages.content as last_message, "
                "messages.sender as last_sender, "
                "messages.is_from_me as last_is_from_me"
            )
        else:
            last_message_select = "NULL as last_message, NULL as last_sender, NULL as last_is_from_me"

        query_parts = [
            f"""
            SELECT
                chats.jid,
                chats.name,
                chats.last_message_time,
                {last_message_select}
            FROM chats
        """
        ]

        if include_last_message:
            query_parts.append("""
                LEFT JOIN messages ON chats.jid = messages.chat_jid
                AND chats.last_message_time = messages.timestamp
            """)

        where_clauses = []
        params = []

        if query:
            # instr() on the raw column matches Unicode; LOWER()+LIKE only covers ASCII.
            where_clauses.append(
                "(instr(LOWER(chats.name), LOWER(?)) > 0 OR instr(chats.name, ?) > 0 OR chats.jid LIKE ?)"
            )
            params.extend([query, query, f"%{query}%"])

        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))

        # Add sorting
        order_by = "chats.last_message_time DESC" if sort_by == "last_active" else "chats.name"
        query_parts.append(f"ORDER BY {order_by}")

        # Add pagination
        offset = (page) * limit
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])

        cursor.execute(" ".join(query_parts), tuple(params))
        chats = cursor.fetchall()

        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=chat_data[1],
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5],
            )
            result.append(chat_to_dict(chat))

        return result

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if "conn" in locals():
            conn.close()


def search_contacts(query: str) -> list[dict[str, Any]]:
    """Search contacts by name or phone number.

    Searches both the messages.db chats table and whatsmeow's contact store
    (whatsapp.db) to find contacts. Results are deduplicated by JID.
    """
    seen_jids: set[str] = set()
    result: list[dict[str, Any]] = []
    # JIDs are all ASCII so LIKE is safe; names use instr() because SQLite's
    # LOWER() only folds case for ASCII and would drop Unicode matches.
    jid_pattern = "%" + query + "%"

    # 1) Search messages.db chats table (existing behavior)
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT jid, name
            FROM chats
            WHERE
                (instr(LOWER(name), LOWER(?)) > 0 OR instr(name, ?) > 0 OR jid LIKE ?)
                AND jid NOT LIKE '%@g.us'
            ORDER BY name, jid
            LIMIT 50
        """,
            (query, query, jid_pattern),
        )
        for jid, name in cursor.fetchall():
            if jid not in seen_jids:
                seen_jids.add(jid)
                contact = Contact(phone_number=jid.split("@")[0], name=name, jid=jid)
                result.append(contact_to_dict(contact))
    except sqlite3.Error as e:
        print(f"Database error (messages.db): {e}")
    finally:
        if "conn" in locals():
            conn.close()

    # 2) Search whatsmeow contact store (whatsapp.db)
    if os.path.exists(WHATSMEOW_DB_PATH):
        try:
            conn2 = sqlite3.connect(WHATSMEOW_DB_PATH)
            cursor2 = conn2.cursor()
            cursor2.execute(
                """
                SELECT their_jid, full_name, push_name, first_name, business_name
                FROM whatsmeow_contacts
                WHERE
                    instr(LOWER(full_name), LOWER(?)) > 0 OR instr(full_name, ?) > 0
                    OR instr(LOWER(push_name), LOWER(?)) > 0 OR instr(push_name, ?) > 0
                    OR instr(LOWER(first_name), LOWER(?)) > 0 OR instr(first_name, ?) > 0
                    OR instr(LOWER(business_name), LOWER(?)) > 0 OR instr(business_name, ?) > 0
                    OR their_jid LIKE ?
                LIMIT 50
            """,
                (query, query, query, query, query, query, query, query, jid_pattern),
            )
            for their_jid, full_name, push_name, first_name, business_name in cursor2.fetchall():
                if their_jid not in seen_jids:
                    seen_jids.add(their_jid)
                    name = full_name or push_name or first_name or business_name or ""
                    contact = Contact(phone_number=their_jid.split("@")[0], name=name, jid=their_jid)
                    result.append(contact_to_dict(contact))
        except sqlite3.Error as e:
            print(f"Database error (whatsapp.db): {e}")
        finally:
            if "conn2" in locals():
                conn2.close()

    return result


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> list[dict[str, Any]]:
    """Get all chats involving the contact.

    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        aliases = _sender_aliases(jid)
        placeholders = ",".join("?" * len(aliases))
        cursor.execute(
            f"""
            SELECT DISTINCT
                c.jid,
                c.name,
                c.last_message_time,
                last_msg.content as last_message,
                last_msg.sender as last_sender,
                last_msg.is_from_me as last_is_from_me
            FROM chats c
            LEFT JOIN messages last_msg ON c.jid = last_msg.chat_jid
                AND c.last_message_time = last_msg.timestamp
            WHERE EXISTS (
                SELECT 1
                FROM messages contact_msg
                WHERE contact_msg.chat_jid = c.jid
                    AND contact_msg.sender IN ({placeholders})
            ) OR c.jid = ?
            ORDER BY c.last_message_time DESC
            LIMIT ? OFFSET ?
        """,
            (*aliases, jid, limit, page * limit),
        )

        chats = cursor.fetchall()

        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=chat_data[1],
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5],
            )
            result.append(chat_to_dict(chat))

        return result

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if "conn" in locals():
            conn.close()


def get_last_interaction(jid: str) -> dict[str, Any] | None:
    """Get most recent message involving the contact.

    Args:
        jid: The JID of the contact to search for

    Returns:
        Message dictionary or None if no messages found
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        aliases = _sender_aliases(jid)
        placeholders = ",".join("?" * len(aliases))
        cursor.execute(
            f"""
            SELECT
                m.timestamp,
                m.sender,
                c.name,
                m.content,
                m.is_from_me,
                c.jid,
                m.id,
                m.media_type
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.sender IN ({placeholders}) OR c.jid = ?
            ORDER BY m.timestamp DESC
            LIMIT 1
        """,
            (*aliases, jid),
        )

        msg_data = cursor.fetchone()

        if not msg_data:
            return None

        message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[7],
        )

        return msg_to_dict(message)

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if "conn" in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True) -> dict[str, Any] | None:
    """Get chat metadata by JID.

    Returns:
        Chat dictionary or None if not found
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        # See list_chats: keep result tuple shape stable across the
        # include_last_message branch by emitting static NULLs when we
        # don't JOIN the messages table.
        if include_last_message:
            last_message_select = "m.content as last_message, m.sender as last_sender, m.is_from_me as last_is_from_me"
        else:
            last_message_select = "NULL as last_message, NULL as last_sender, NULL as last_is_from_me"

        query = f"""
            SELECT
                c.jid,
                c.name,
                c.last_message_time,
                {last_message_select}
            FROM chats c
        """

        if include_last_message:
            query += """
                LEFT JOIN messages m ON c.jid = m.chat_jid
                AND c.last_message_time = m.timestamp
            """

        query += " WHERE c.jid = ?"

        cursor.execute(query, (chat_jid,))
        chat_data = cursor.fetchone()

        if not chat_data:
            return None

        chat = Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5],
        )
        return chat_to_dict(chat)

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if "conn" in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str) -> dict[str, Any] | None:
    """Get chat metadata by sender phone number."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()

        cursor.execute(
            """
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
        """,
            (f"%{sender_phone_number}%",),
        )

        chat_data = cursor.fetchone()

        if not chat_data:
            return None

        chat = Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5],
        )
        return chat_to_dict(chat)

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if "conn" in locals():
            conn.close()


def send_message(
    recipient: str,
    message: str,
    quoted_message_id: str = "",
    quoted_sender_jid: str = "",
    quoted_content: str = "",
) -> tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"

        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload: dict[str, Any] = {
            "recipient": recipient,
            "message": message,
        }
        if quoted_message_id:
            payload["quoted_message_id"] = quoted_message_id
            payload["quoted_sender_jid"] = quoted_sender_jid
            payload["quoted_content"] = quoted_content

        response = requests.post(url, json=payload, headers=_bridge_headers())

        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"

    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"


def send_file(recipient: str, media_path: str) -> tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"

        if not media_path:
            return False, "Media path must be provided"

        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {"recipient": recipient, "media_path": media_path}

        response = requests.post(url, json=payload, headers=_bridge_headers())

        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"

    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"


def send_audio_message(recipient: str, media_path: str) -> tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"

        if not media_path:
            return False, "Media path must be provided"

        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        if not media_path.endswith(".ogg"):
            try:
                media_path = audio.convert_to_opus_ogg_temp(media_path)
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"

        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {"recipient": recipient, "media_path": media_path}

        response = requests.post(url, json=payload, headers=_bridge_headers())

        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"

    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"


def download_media(message_id: str, chat_jid: str) -> str | None:
    """Download media from a message and return the local file path.

    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message

    Returns:
        The local file path if download was successful, None otherwise
    """
    try:
        url = f"{WHATSAPP_API_BASE_URL}/download"
        payload = {"message_id": message_id, "chat_jid": chat_jid}

        response = requests.post(url, json=payload, headers=_bridge_headers())

        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                path = result.get("path")
                print(f"Media downloaded successfully: {path}")
                return path
            else:
                print(f"Download failed: {result.get('message', 'Unknown error')}")
                return None
        else:
            print(f"Error: HTTP {response.status_code} - {response.text}")
            return None

    except requests.RequestException as e:
        print(f"Request error: {str(e)}")
        return None
    except json.JSONDecodeError:
        print(f"Error parsing response: {response.text}")
        return None
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return None


def download_media_as_image(message_id: str, chat_jid: str) -> dict[str, Any]:
    """Download an image from a WhatsApp message and return it as bytes for inline display.

    Implements the full secure receive pipeline:
    1. JID normalization + allowlist gate (Python-side, before any bridge call)
    2. Bridge call to /api/download (Go-side confinement, atomic write, size limits)
    3. Python-side path confinement re-check
    4. Pillow validation (decompression bomb, format check, dimension check)
    5. Preview generation: resize to PREVIEW_MAX_DIM px and/or compress to
       PREVIEW_MAX_BYTES. Animated GIF and WebP that exceed either threshold
       are flattened to a static JPEG — animation and transparency are lost.

    Returns a dict with:
    - On success: {"ok": True, "data": bytes, "format": str, "filename": str, "file_size_bytes": int}
    - On failure: {"code": str, ...optional extra fields}

    Stable error codes (never expose internal paths, tracebacks, or raw exceptions):
      invalid_jid, not_allowed, media_too_large, bridge_unreachable,
      path_confinement, not_found, download_failed, invalid_bridge_response,
      path_resolution_failed, media_file_missing, media_read_failed,
      decompression_bomb, invalid_image, image_too_large_dimensions,
      unsupported_format, preview_generation_failed, preview_too_large,
      internal_error
    """
    try:
        # ── Step 1: Normalize JID ─────────────────────────────────────────────
        try:
            normalized_jid = _normalize_jid(chat_jid)
        except ValueError:
            return {"code": "invalid_jid"}

        # ── Step 2: Allowlist check ───────────────────────────────────────────
        if not MEDIA_DOWNLOAD_ALLOW_ALL and normalized_jid not in MEDIA_DOWNLOAD_ALLOWED_JIDS:
            return {"code": "not_allowed"}

        # ── Step 3: Deny-all fast-path (cap == 0, before any I/O) ────────────
        if MEDIA_SIZE_CAP_BYTES == 0:
            return {"code": "media_too_large"}

        # ── Step 4: Bridge call ───────────────────────────────────────────────
        bridge_url = f"{WHATSAPP_API_BASE_URL}/download"
        payload = {"message_id": message_id, "chat_jid": normalized_jid}
        try:
            resp = requests.post(bridge_url, json=payload, headers=_bridge_headers(), timeout=(5, 60))
        except requests.RequestException:
            return {"code": "bridge_unreachable"}

        # ── Step 5: Handle non-200 ────────────────────────────────────────────
        if resp.status_code != 200:
            try:
                code = resp.json().get("code")
            except Exception:
                code = None
            if not code:
                code = {403: "path_confinement", 404: "not_found"}.get(resp.status_code, "download_failed")
            return {"code": code}

        # ── Step 6: Parse bridge response ─────────────────────────────────────
        try:
            data = resp.json()
            media_path = data["path"]
            filename = data["filename"]
        except Exception:
            return {"code": "invalid_bridge_response"}

        # ── Step 7: Python-side path confinement re-check ─────────────────────
        try:
            real = Path(media_path).resolve()
        except Exception:
            return {"code": "path_resolution_failed"}
        base = Path(MEDIA_STORE_BASE).resolve()
        if not real.is_relative_to(base):
            print(f"download_media_as_image: path confinement failed: {real!r} not under {base!r}")
            return {"code": "path_confinement"}

        # ── Step 8: Stat and size check ───────────────────────────────────────
        try:
            file_size = real.stat().st_size
        except FileNotFoundError:
            return {"code": "media_file_missing"}
        except Exception:
            return {"code": "media_read_failed"}
        if file_size > MEDIA_SIZE_CAP_BYTES:
            return {"code": "media_too_large", "file_size_bytes": file_size}

        # ── Step 9: Read bytes ────────────────────────────────────────────────
        try:
            image_bytes = real.read_bytes()
        except Exception:
            return {"code": "media_read_failed"}

        # ── Step 10: Pillow validation ────────────────────────────────────────
        # Capture format and size BEFORE calling verify() — verify() invalidates
        # the decoder state and makes a second open necessary for preview.
        with warnings.catch_warnings():
            warnings.simplefilter("error", PilImage.DecompressionBombWarning)
            try:
                img = PilImage.open(BytesIO(image_bytes))
                pillow_format = img.format
                w, h = img.size
                img.verify()
            except (PilImage.DecompressionBombWarning, PilImage.DecompressionBombError):
                return {"code": "decompression_bomb"}
            except Exception:
                return {"code": "invalid_image"}

        if w * h > 50_000_000:
            return {"code": "image_too_large_dimensions"}

        fmt = PILLOW_FORMAT_TO_FMT.get(pillow_format or "")
        if fmt is None:
            return {"code": "unsupported_format"}

        # ── Step 11: Preview generation ───────────────────────────────────────
        # Re-open from bytes (verify() consumed the decoder state).
        # Animated GIF/WebP that need downscaling are returned as a static JPEG
        # preview — animation and transparency are not preserved.
        try:
            img2 = PilImage.open(BytesIO(image_bytes))
            img2 = ImageOps.exif_transpose(img2)  # correct EXIF rotation before anything else

            needs_resize = max(w, h) > PREVIEW_MAX_DIM or len(image_bytes) > PREVIEW_MAX_BYTES
            if needs_resize:
                img2.thumbnail((PREVIEW_MAX_DIM, PREVIEW_MAX_DIM), PilImage.LANCZOS)
                # Always output JPEG for resized previews. GIF/WebP → static JPEG
                # (first frame, transparency flattened to white for RGB modes).
                if img2.mode not in ("RGB", "L"):
                    img2 = img2.convert("RGB")
                buf = BytesIO()
                for quality in (PREVIEW_JPEG_Q, 65, 45):
                    buf.seek(0)
                    buf.truncate()
                    img2.save(buf, format="JPEG", quality=quality, optimize=True)
                    if len(buf.getvalue()) <= PREVIEW_MAX_BYTES:
                        break
                preview_bytes = buf.getvalue()
                if len(preview_bytes) > PREVIEW_MAX_BYTES:
                    return {"code": "preview_too_large"}
                preview_fmt = "jpeg"
            else:
                preview_bytes = image_bytes
                preview_fmt = fmt
        except Exception:
            return {"code": "preview_generation_failed"}

        # ── Step 12: Return ───────────────────────────────────────────────────
        return {
            "ok": True,
            "data": preview_bytes,
            "format": preview_fmt,
            "filename": filename,
            "file_size_bytes": file_size,
        }

    except Exception:
        print(f"download_media_as_image: unexpected error:\n{traceback.format_exc()}")
        return {"code": "internal_error"}
