"""Per-contact engagement stats and tier classification.

Tune these thresholds after looking at real output - that's the whole
reason they're constants instead of inline numbers.
"""

import datetime
from dataclasses import dataclass
from typing import Optional

from . import cache

# --- Engagement tier thresholds (tune freely) ---------------------------

RESPONSIVE = "RESPONSIVE"
OCCASIONAL = "OCCASIONAL"
NEVER_REPLIED = "NEVER_REPLIED"
COLD = "COLD"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

# RESPONSIVE requires replying to *more than* this fraction of threads I've
# started (strict >, so exactly half doesn't qualify).
RESPONSIVE_THREAD_REPLY_RATE_THRESHOLD = 0.5

# RESPONSIVE also requires a reply within this many days (~6 months).
RESPONSIVE_RECENCY_DAYS = 182

# COLD: replied before, but nothing in this many days or more (~12 months).
COLD_THRESHOLD_DAYS = 365

# NEVER_REPLIED requires at least this many sends with zero replies ever.
# Below this, a zero-reply contact is INSUFFICIENT_DATA instead.
NEVER_REPLIED_MIN_SENT = 3

# Tiers to surface at the top of the sorted output (highest-priority-to-review).
HIGH_PRIORITY_TIERS = {NEVER_REPLIED, COLD}


@dataclass
class ContactStats:
    total_sent_count: int
    total_reply_count: int
    first_contact_date: Optional[str]
    last_sent_date: Optional[str]
    last_reply_date: Optional[str]
    avg_reply_latency_days: Optional[float]
    days_since_last_reply: Optional[int]
    thread_reply_rate: float
    engagement_tier: str = ""


def _ms_to_date_str(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).strftime("%Y-%m-%d")


def _dedupe_by_rfc822_id(rows):
    """rows: [(mailbox, thread_id, internal_date, rfc822_message_id), ...].

    Collapses copies of the same physical email that landed in more than one
    of your mailboxes (e.g. you cc'd yourself across accounts), keyed on the
    RFC822 Message-Id header, which - unlike Gmail's per-mailbox message/thread
    ids - is identical across every copy of one physical message.
    """
    seen_ids = set()
    result = []
    for row in rows:
        rfc_id = row[3]
        if rfc_id:
            if rfc_id in seen_ids:
                continue
            seen_ids.add(rfc_id)
        result.append(row)
    return result


def classify(stats):
    if stats.total_sent_count == 0:
        return INSUFFICIENT_DATA

    if stats.total_reply_count > 0:
        days_since = stats.days_since_last_reply
        if (
            stats.thread_reply_rate > RESPONSIVE_THREAD_REPLY_RATE_THRESHOLD
            and days_since is not None
            and days_since <= RESPONSIVE_RECENCY_DAYS
        ):
            return RESPONSIVE
        if days_since is not None and days_since >= COLD_THRESHOLD_DAYS:
            return COLD
        return OCCASIONAL

    if stats.total_sent_count >= NEVER_REPLIED_MIN_SENT:
        return NEVER_REPLIED
    return INSUFFICIENT_DATA


def compute_contact_stats(conn, mailboxes, contact_email, now=None):
    """Aggregate sent/reply stats for one contact across all mailboxes."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    now_ms = int(now.timestamp() * 1000)

    all_sent = []  # (mailbox, thread_id, internal_date, rfc822_message_id)
    all_genuine_replies = []
    per_mailbox_sent_by_thread = {}  # mailbox -> {thread_id: [internal_date, ...]}

    for mailbox in mailboxes:
        sent_rows = cache.get_sent_rows(conn, mailbox, contact_email)
        received_rows = cache.get_received_rows(conn, mailbox, contact_email)

        sent_thread_ids = {row[0] for row in sent_rows}
        by_thread = {}
        for thread_id, internal_date, rfc_id in sent_rows:
            by_thread.setdefault(thread_id, []).append(internal_date)
            all_sent.append((mailbox, thread_id, internal_date, rfc_id))
        per_mailbox_sent_by_thread[mailbox] = by_thread

        for thread_id, internal_date, rfc_id in received_rows:
            if thread_id in sent_thread_ids:
                all_genuine_replies.append((mailbox, thread_id, internal_date, rfc_id))

    deduped_sent = _dedupe_by_rfc822_id(all_sent)
    deduped_replies = _dedupe_by_rfc822_id(all_genuine_replies)

    total_sent_count = len(deduped_sent)
    total_reply_count = len(deduped_replies)

    distinct_sent_threads = {(mailbox, tid) for mailbox, tid, _d, _r in all_sent}
    distinct_replied_threads = {(mailbox, tid) for mailbox, tid, _d, _r in all_genuine_replies}
    thread_reply_rate = (
        len(distinct_replied_threads) / len(distinct_sent_threads) if distinct_sent_threads else 0.0
    )

    if total_sent_count == 0:
        stats = ContactStats(
            total_sent_count=0, total_reply_count=0,
            first_contact_date=None, last_sent_date=None, last_reply_date=None,
            avg_reply_latency_days=None, days_since_last_reply=None,
            thread_reply_rate=0.0,
        )
        stats.engagement_tier = classify(stats)
        return stats

    first_contact_ms = min(row[2] for row in deduped_sent)
    last_sent_ms = max(row[2] for row in deduped_sent)

    if deduped_replies:
        last_reply_ms = max(row[2] for row in deduped_replies)
        days_since_last_reply = round((now_ms - last_reply_ms) / 86400000)
    else:
        last_reply_ms = None
        days_since_last_reply = None

    latencies = []
    for mailbox, thread_id, reply_date, _rfc in deduped_replies:
        prior_sent_dates = [
            d for d in per_mailbox_sent_by_thread[mailbox].get(thread_id, []) if d <= reply_date
        ]
        if prior_sent_dates:
            latencies.append((reply_date - max(prior_sent_dates)) / 86400000)
    avg_reply_latency_days = round(sum(latencies) / len(latencies), 1) if latencies else None

    stats = ContactStats(
        total_sent_count=total_sent_count,
        total_reply_count=total_reply_count,
        first_contact_date=_ms_to_date_str(first_contact_ms),
        last_sent_date=_ms_to_date_str(last_sent_ms),
        last_reply_date=_ms_to_date_str(last_reply_ms) if last_reply_ms is not None else None,
        avg_reply_latency_days=avg_reply_latency_days,
        days_since_last_reply=days_since_last_reply,
        thread_reply_rate=thread_reply_rate,
    )
    stats.engagement_tier = classify(stats)
    return stats
