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

# gmail.metadata is the narrowest scope that still exposes headers,
# internalDate, and threadId via format="metadata" - no message bodies or
# attachments, which this tool never needs (no LLM/content analysis).
SCOPES = ["https://www.googleapis.com/auth/gmail.metadata"]

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
BATCH_SIZE = 100  # Gmail API batch request cap
MAX_RETRIES = 5

# Day-granularity overlap applied to incremental syncs. Gmail's after:
# search operator only supports date (not time-of-day) precision, so a
# re-sync re-requests this many days before the last sync's newest message
# to avoid gaps; duplicates are harmless (de-duped on insert by primary key).
INCREMENTAL_OVERLAP_DAYS = 1
