"""SQLite cache of raw Gmail message metadata, keyed per mailbox.

Schema notes:
- `messages.message_id` and `thread_id` are Gmail IDs, scoped to a single
  mailbox's namespace - the same physical email delivered to two of your
  mailboxes gets two different, unrelated IDs. All ID-based joins here are
  therefore always scoped by `mailbox`. Cross-mailbox de-duplication (for a
  message that landed in more than one of your mailboxes) instead uses the
  RFC822 `Message-Id` header, which is set by the originating mail server
  and is the same in every copy.
- `recipients` is a separate table because a single sent message can have
  many To/Cc/Bcc addresses.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    mailbox TEXT NOT NULL,
    message_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    internal_date INTEGER NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('sent', 'received')),
    from_address TEXT,
    rfc822_message_id TEXT,
    is_autoreply INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mailbox, message_id)
);

CREATE TABLE IF NOT EXISTS recipients (
    mailbox TEXT NOT NULL,
    message_id TEXT NOT NULL,
    recipient_email TEXT NOT NULL,
    PRIMARY KEY (mailbox, message_id, recipient_email)
);

CREATE INDEX IF NOT EXISTS idx_recipients_email ON recipients (recipient_email);
CREATE INDEX IF NOT EXISTS idx_messages_from ON messages (mailbox, from_address);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages (mailbox, thread_id);

CREATE TABLE IF NOT EXISTS sync_state (
    mailbox TEXT NOT NULL,
    direction TEXT NOT NULL,
    last_internal_date INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mailbox, direction)
);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def get_sync_state(conn, mailbox, direction):
    """Return the last-seen internalDate (ms) for this mailbox/direction, or None if never synced."""
    row = conn.execute(
        "SELECT last_internal_date FROM sync_state WHERE mailbox = ? AND direction = ?",
        (mailbox, direction),
    ).fetchone()
    return row[0] if row else None


def set_sync_state(conn, mailbox, direction, last_internal_date):
    conn.execute(
        """
        INSERT INTO sync_state (mailbox, direction, last_internal_date)
        VALUES (?, ?, ?)
        ON CONFLICT (mailbox, direction) DO UPDATE SET
            last_internal_date = MAX(last_internal_date, excluded.last_internal_date)
        """,
        (mailbox, direction, last_internal_date),
    )


def insert_message(conn, mailbox, message_id, thread_id, internal_date, direction,
                    from_address, rfc822_message_id, is_autoreply, recipient_emails):
    conn.execute(
        """
        INSERT OR IGNORE INTO messages
            (mailbox, message_id, thread_id, internal_date, direction,
             from_address, rfc822_message_id, is_autoreply)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (mailbox, message_id, thread_id, internal_date, direction,
         from_address, rfc822_message_id, int(is_autoreply)),
    )
    for recipient in recipient_emails:
        conn.execute(
            "INSERT OR IGNORE INTO recipients (mailbox, message_id, recipient_email) "
            "VALUES (?, ?, ?)",
            (mailbox, message_id, recipient),
        )


def get_sent_rows(conn, mailbox, contact_email):
    """Return [(thread_id, internal_date, rfc822_message_id), ...] I sent to contact_email from mailbox."""
    return conn.execute(
        """
        SELECT m.thread_id, m.internal_date, m.rfc822_message_id
        FROM messages m
        JOIN recipients r ON m.mailbox = r.mailbox AND m.message_id = r.message_id
        WHERE m.mailbox = ? AND m.direction = 'sent' AND r.recipient_email = ?
        ORDER BY m.internal_date
        """,
        (mailbox, contact_email),
    ).fetchall()


def get_received_rows(conn, mailbox, contact_email):
    """Return [(thread_id, internal_date, rfc822_message_id), ...] received from contact_email in mailbox, excluding autoreplies."""
    return conn.execute(
        """
        SELECT thread_id, internal_date, rfc822_message_id
        FROM messages
        WHERE mailbox = ? AND direction = 'received' AND from_address = ? AND is_autoreply = 0
        ORDER BY internal_date
        """,
        (mailbox, contact_email),
    ).fetchall()
