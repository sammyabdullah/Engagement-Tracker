# engagement_tracker

Score a contact list by reply engagement across your Gmail mailboxes. Pure
Gmail API calls and local logic, run weekly — no LLM/AI API involved, same
philosophy as `email_frequency_counter` and `mailerdemon`.

## What it does

1. Reads a contact list CSV (any columns; you tell it which one holds the
   email address, default `email`).
2. Authenticates to each of your Gmail mailboxes via a domain-wide-delegated
   service account (see Setup below) — no per-mailbox interactive login.
3. Does a full sync of each mailbox's sent + received message **metadata**
   (thread id, dates, To/Cc/Bcc/From headers, RFC822 Message-Id — never
   bodies or attachments) into a local SQLite cache. Re-runs only fetch
   messages newer than the last sync, so this stays fast at scale (10K
   contacts × 5 mailboxes would be far too slow to query per-contact every
   run).
4. For every contact, computes send/reply stats from the local cache — no
   further Gmail calls needed — and classifies them into an engagement
   tier.
5. Writes a scored CSV and prints a console summary.

## Reply matching: how it works, and its limits

A reply is counted as **genuine** only when an inbound message from the
contact shares a Gmail thread id with a message you sent to that contact —
i.e. real back-and-forth, not a coincidental unrelated email from the same
address. This is thread-based matching, which is reliable for the vast
majority of real conversations, but has a few known edge cases worth
knowing about:

- **Gmail thread ids are per-mailbox.** The same physical email delivered
  to two of your mailboxes (e.g. you cc'd yourself across accounts) gets
  two unrelated thread ids. Reply matching is done separately within each
  mailbox, then counts/dates are aggregated across mailboxes — deduplicated
  on the RFC822 `Message-Id` header (identical across every copy of one
  physical message) so a cc'd-to-two-of-your-mailboxes reply isn't double
  counted.
- **A contact who replies to a mailbox they weren't addressed from won't
  match.** If you emailed a contact only from `sammy@`, and they somehow
  reply only into `s@` (e.g. they had that address from elsewhere and used
  reply-all inconsistently), that reply won't be linked, because `s@` never
  has a "sent" message in that thread to match it against. Uncommon, but
  possible.
- **Some mail clients break threading** by stripping `References`/
  `In-Reply-To` headers or changing the subject enough that Gmail starts a
  new thread instead of continuing the old one. A "reply" sent this way
  looks like a brand-new, unrelated message and won't be counted. This is
  a limitation of thread-based matching in general, not something this
  tool can detect from the metadata it has.
- Out-of-office/autoresponder messages are detected via the
  `Auto-Submitted`, `X-Autoreply`, and `Precedence` headers and excluded
  from reply counts — an OOO bounce isn't genuine engagement.

If you start seeing systematic gaps, the usual cause is one of the above,
not a bug in the counting logic.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
# or, for the `engagement-tracker` console command:
pip install -e .
```

### 2. Create a Google Cloud project and service account

1. Go to the [Google Cloud Console](https://console.cloud.google.com/),
   create (or reuse) a project, and enable the **Gmail API** under
   **APIs & Services → Library**.
2. Go to **IAM & Admin → Service Accounts → Create Service Account**. Give
   it any name (e.g. "engagement-tracker"). No roles are needed on the
   project itself.
3. Open the new service account → **Keys → Add Key → Create new key →
   JSON**. Download it and save it as `service_account.json` in this
   project directory (already excluded via `.gitignore` — never commit it).
4. Note the service account's **Client ID** (a numeric string, shown on the
   service account's Details page) — you'll need it in the next step.

### 3. Authorize domain-wide delegation in Workspace admin

You need Google Workspace super admin access for this step.

1. Go to the
   [Google Workspace Admin Console](https://admin.google.com/) →
   **Security → Access and data control → API controls → Domain-wide
   Delegation**.
2. Click **Add new**, paste the service account's **Client ID**, and under
   **OAuth scopes** add:
   ```
   https://www.googleapis.com/auth/gmail.metadata
   ```
3. Save. This authorizes the service account to impersonate **any** mailbox
   on your domain with this narrow, read-headers-only scope — it cannot
   read message bodies, send mail, or modify anything.

No further per-mailbox setup is needed — the service account can
impersonate all 5 configured mailboxes immediately once this is saved.

### 4. Configure the mailbox list

The 5 mailboxes to pull from are constants at the top of
`engagement_tracker/config.py` (`MAILBOXES`). Edit that list if your
mailboxes change, or override per-run with `--mailboxes`.

## Usage

```bash
engagement-tracker contacts.csv
```

or without installing the package:

```bash
python -m engagement_tracker.cli contacts.csv
```

First run syncs each mailbox's sent/received history going back
`SYNC_LOOKBACK_DAYS` (default 730 days / ~2 years, set in `config.py`) —
this can still take a while on large mailboxes, since Gmail's API costs one
metadata fetch per message regardless of batching. Set it to `None` for
unbounded full-lifetime history if you want it, but expect a much longer
first run on a large mailbox. Every run after the first is incremental
(only new messages since last sync) and much faster, regardless of this
setting.

### Options

| Flag | Description |
|---|---|
| `input_csv` | Path to the input contacts CSV (required) |
| `-o, --output` | Path to write the scored CSV (default: `<input>_scored.csv`) |
| `--email-column` | Name of the column holding the contact's email address (default: `email`) |
| `--service-account-key` | Path to the domain-wide-delegation service account JSON key (default: `service_account.json`) |
| `--mailboxes` | Comma-separated mailbox override (default: the 5 in `config.py`) |
| `--db` | Path to the local SQLite cache (default: `engagement_tracker_cache.sqlite`) |
| `--full-resync` | Wipe cached history for the target mailboxes and re-sync from scratch |
| `--no-sync` | Skip Gmail entirely and recompute scores from the existing cache (for re-tuning tier thresholds without re-hitting the API) |

## Output

The output CSV contains every original column plus:

| Column | Meaning |
|---|---|
| `total_sent_count` | Emails you sent to this contact (to/cc/bcc), across all 5 mailboxes, deduplicated |
| `total_reply_count` | Genuine thread replies from this contact, across all 5 mailboxes, deduplicated |
| `first_contact_date` | Date of your earliest sent message to this contact |
| `last_sent_date` | Date of your most recent sent message |
| `last_reply_date` | Date of their most recent genuine reply (blank if never) |
| `avg_reply_latency_days` | Average days between your send and their reply, across all matched replies (blank if never replied) |
| `days_since_last_reply` | Days since their last reply (blank if never replied) |
| `engagement_tier` | See below |

Rows are sorted with `NEVER_REPLIED` and `COLD` contacts first (highest
sent_count at the top within each) — these are the highest-priority rows to
review. Malformed email addresses get `engagement_tier = INVALID_EMAIL`
with blank stats. Rows whose address is one of your own 5 mailboxes get
`engagement_tier = SELF`.

### Engagement tiers

Thresholds are constants at the top of `engagement_tracker/scoring.py` —
tune them after seeing real output:

- **RESPONSIVE** — replied to more than half the threads you started with
  them, and replied within the last ~6 months (`RESPONSIVE_RECENCY_DAYS`).
- **OCCASIONAL** — replied at least once ever, but doesn't meet RESPONSIVE.
- **NEVER_REPLIED** — you've sent 3+ times (`NEVER_REPLIED_MIN_SENT`), zero
  replies ever.
- **COLD** — replied before, but nothing in the last ~12 months
  (`COLD_THRESHOLD_DAYS`).
- **INSUFFICIENT_DATA** — fewer than 3 sends and zero replies; not enough
  history to judge yet.

### Console summary

Printed after every run: total contacts processed, a breakdown by
engagement tier, and the top 20 `NEVER_REPLIED` contacts by `sent_count` —
the people you're emailing most who never respond, highest priority to
review.

## Notes

- No LLM/AI API calls at runtime — pure Gmail API metadata + local logic.
- The Gmail scope used (`gmail.metadata`) cannot read message bodies,
  attachments, send mail, or modify anything — only headers, dates, and
  thread/label structure.
- Handles Gmail API pagination (`nextPageToken`) and rate limits
  (exponential backoff with jitter on 403/429/500/503) for both single and
  batch requests.
- The SQLite cache (`engagement_tracker_cache.sqlite` by default) is
  local-only and gitignored — delete it (or use `--full-resync`) to force a
  clean re-sync.
- Duplicate rows for the same email in the input CSV are all preserved in
  the output (each gets the same computed stats) — the tool doesn't
  deduplicate your contact list.
