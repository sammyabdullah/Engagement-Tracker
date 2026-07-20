"""Full incremental sync of a mailbox's sent/received message metadata into SQLite.

Per-contact Gmail queries don't scale to a 10K-contact list across 5
mailboxes, so instead we sync each mailbox's entire sent + received message
metadata (headers, dates, thread ids - never bodies) once, and incrementally
thereafter, then compute every contact's stats from the local cache.
"""

import datetime
import sys
import time
from email.utils import getaddresses

from tqdm import tqdm

from . import cache
from .backoff import call_with_backoff, execute_batch_with_retry
from .config import (
    BATCH_SIZE,
    INCREMENTAL_OVERLAP_DAYS,
    LIST_PAGE_SIZE,
    MAX_REQUESTS_PER_SECOND,
    METADATA_HEADERS,
    SYNC_LOOKBACK_DAYS,
)

SENT_QUERY_BASE = "in:sent"
RECEIVED_QUERY_BASE = "-in:sent -in:chats -in:draft -in:spam -in:trash"

SQLITE_ID_CHUNK = 500


def get_header(headers, name):
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return None


def extract_addresses(header_value):
    """Parse a To/Cc/Bcc header value into a list of lowercased, deduped addresses."""
    if not header_value:
        return []
    addresses = set()
    for _name, addr in getaddresses([header_value]):
        addr = addr.strip().lower()
        if addr:
            addresses.add(addr)
    return sorted(addresses)


def extract_from_address(header_value):
    if not header_value:
        return None
    parsed = getaddresses([header_value])
    if not parsed:
        return None
    addr = parsed[0][1].strip().lower()
    return addr or None


def is_autoreply(headers):
    auto_submitted = get_header(headers, "Auto-Submitted")
    if auto_submitted and auto_submitted.strip().lower() != "no":
        return True
    if get_header(headers, "X-Autoreply") is not None:
        return True
    precedence = get_header(headers, "Precedence")
    if precedence and precedence.strip().lower() in ("bulk", "auto_reply", "junk"):
        return True
    return False


def list_message_ids(service, query):
    """Paginate users.messages.list for `query`, returning all matching message ids."""
    ids = []
    page_token = None
    while True:
        request_params = {
            "userId": "me",
            "q": query,
            "maxResults": LIST_PAGE_SIZE,
            "fields": "nextPageToken,messages/id",
        }
        if page_token:
            request_params["pageToken"] = page_token

        response = call_with_backoff(
            lambda: service.users().messages().list(**request_params).execute()
        )
        ids.extend(m["id"] for m in response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return ids


def filter_uncached_ids(conn, mailbox, message_ids):
    uncached = []
    for i in range(0, len(message_ids), SQLITE_ID_CHUNK):
        chunk = message_ids[i:i + SQLITE_ID_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT message_id FROM messages WHERE mailbox = ? AND message_id IN ({placeholders})",
            (mailbox, *chunk),
        ).fetchall()
        existing = {r[0] for r in rows}
        uncached.extend(mid for mid in chunk if mid not in existing)
    return uncached


def fetch_and_store_messages(conn, service, mailbox, message_ids, direction):
    """Batch-fetch metadata for message_ids and insert into the cache."""

    def build_request(message_id):
        return service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=METADATA_HEADERS,
            fields="id,threadId,internalDate,payload/headers",
        )

    stored = 0
    chunks = [message_ids[i:i + BATCH_SIZE] for i in range(0, len(message_ids), BATCH_SIZE)]
    for chunk in tqdm(chunks, desc=f"  {mailbox} {direction}", unit="batch", disable=len(chunks) == 0):
        batch_start = time.monotonic()
        responses = execute_batch_with_retry(service, build_request, chunk)
        for message_id, message in responses.items():
            headers = message.get("payload", {}).get("headers", [])
            thread_id = message["threadId"]
            internal_date = int(message["internalDate"])
            rfc822_message_id = get_header(headers, "Message-Id")

            if direction == "sent":
                recipients = set()
                for field in ("To", "Cc", "Bcc"):
                    recipients.update(extract_addresses(get_header(headers, field)))
                cache.insert_message(
                    conn, mailbox, message_id, thread_id, internal_date, direction,
                    from_address=None, rfc822_message_id=rfc822_message_id,
                    is_autoreply=False, recipient_emails=recipients,
                )
            else:
                from_address = extract_from_address(get_header(headers, "From"))
                cache.insert_message(
                    conn, mailbox, message_id, thread_id, internal_date, direction,
                    from_address=from_address, rfc822_message_id=rfc822_message_id,
                    is_autoreply=is_autoreply(headers), recipient_emails=(),
                )
            stored += 1
        conn.commit()

        # Pace requests to stay under Gmail's per-mailbox rate limit instead
        # of bursting and relying on retry-after-the-fact backoff.
        min_interval = len(chunk) / MAX_REQUESTS_PER_SECOND
        elapsed = time.monotonic() - batch_start
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
    return stored


def sync_direction(conn, service, mailbox, direction, base_query, progress_label):
    last_synced = cache.get_sync_state(conn, mailbox, direction)
    query = base_query
    if last_synced is not None:
        cutoff_ms = last_synced - INCREMENTAL_OVERLAP_DAYS * 86400000
        cutoff_date = datetime.datetime.fromtimestamp(
            cutoff_ms / 1000, tz=datetime.timezone.utc
        ).strftime("%Y/%m/%d")
        query = f"{base_query} after:{cutoff_date}"
    elif SYNC_LOOKBACK_DAYS is not None:
        # First-ever sync for this mailbox/direction: bound it instead of
        # pulling the mailbox's entire lifetime history. Only applies once -
        # every sync after this is incremental regardless of this setting.
        lookback_date = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=SYNC_LOOKBACK_DAYS)
        ).strftime("%Y/%m/%d")
        query = f"{base_query} after:{lookback_date}"

    print(f"  [{mailbox}] listing {progress_label} ({'incremental' if last_synced else 'full'})...")
    all_ids = list_message_ids(service, query)
    new_ids = filter_uncached_ids(conn, mailbox, all_ids)
    print(f"  [{mailbox}] {len(all_ids)} {progress_label} matched, {len(new_ids)} new; fetching metadata...")

    fetch_and_store_messages(conn, service, mailbox, new_ids, direction)

    new_max = conn.execute(
        "SELECT MAX(internal_date) FROM messages WHERE mailbox = ? AND direction = ?",
        (mailbox, direction),
    ).fetchone()[0]
    if new_max is not None:
        cache.set_sync_state(conn, mailbox, direction, new_max)
        conn.commit()


def sync_mailbox(conn, service, mailbox):
    sync_direction(conn, service, mailbox, "sent", SENT_QUERY_BASE, "sent messages")
    sync_direction(conn, service, mailbox, "received", RECEIVED_QUERY_BASE, "received messages")


def sync_all(conn, services):
    for mailbox, service in services.items():
        print(f"Syncing {mailbox}...")
        try:
            sync_mailbox(conn, service, mailbox)
        except Exception as e:
            print(f"  Error syncing {mailbox}: {e}", file=sys.stderr)
            raise
