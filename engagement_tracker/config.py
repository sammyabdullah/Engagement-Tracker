"""Shared configuration constants.

Engagement-tier thresholds live in scoring.py (next to the classification
logic they tune), not here.
"""

# The mailboxes to pull from, in the order they'll be synced. Override at
# runtime with --mailboxes if you ever need to run against a subset.
MAILBOXES = [
    "sammy@blossomstreetventures.com",
    "s@blossomstreetventures.com",
    "partner@blossomstreetventures.com",
    "founder@blossomstreetventures.com",
    "cofounder@blossomstreetventures.com",
]

# gmail.metadata looks like the narrowest fit (headers/internalDate/threadId
# only, via format="metadata") but Gmail's API rejects the `q` search
# parameter under that scope entirely - and this tool relies on `q` (in:sent,
# after:, etc.) to enumerate messages. gmail.readonly is the narrowest scope
# that actually supports search. The code still never requests message
# bodies or attachments (always format="metadata"/metadataHeaders) even
# though the grant itself is broader than strictly needed.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

DEFAULT_SERVICE_ACCOUNT_KEY = "service_account.json"
DEFAULT_DB_PATH = "engagement_tracker_cache.sqlite"

# Headers fetched per message. Superset covering both sent (To/Cc/Bcc) and
# received (From) needs, plus Message-Id for cross-mailbox de-duplication
# and the autoreply signals used to filter out-of-office/bulk replies.
METADATA_HEADERS = [
    "From",
    "To",
    "Cc",
    "Bcc",
    "Message-Id",
    "Auto-Submitted",
    "X-Autoreply",
    "Precedence",
]

LIST_PAGE_SIZE = 500

# Gmail's per-mailbox rate limit is ~250 quota units/sec, and messages.get
# costs 5 units - a ceiling of ~50 get calls/sec/mailbox. A 100-request
# batch (the API's raw cap) blows straight through that in one shot, so
# nearly every batch was coming back with a chunk of 403s to retry. Sizing
# batches under the ceiling, paced by MAX_REQUESTS_PER_SECOND below, avoids
# that retry storm instead of just backing off after the fact.
BATCH_SIZE = 40
MAX_REQUESTS_PER_SECOND = 45
MAX_RETRIES = 5

# Day-granularity overlap applied to incremental syncs. Gmail's after:
# search operator only supports date (not time-of-day) precision, so a
# re-sync re-requests this many days before the last sync's newest message
# to avoid gaps; duplicates are harmless (de-duped on insert by primary key).
INCREMENTAL_OVERLAP_DAYS = 1

# How far back the *first-ever* sync of a mailbox reaches, in days. Applies
# once per mailbox/direction - every sync after that is incremental
# regardless of this value. None means no bound (full lifetime history).
# 730 days (~2 years) balances useful history against first-sync runtime on
# large mailboxes.
SYNC_LOOKBACK_DAYS = 730
