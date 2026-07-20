"""CLI entry point: sync Gmail metadata, score a contact list by reply engagement."""

import argparse
import csv
import re
import sys
from collections import Counter
from pathlib import Path

from . import auth, cache, gmail_sync, scoring
from .config import DEFAULT_DB_PATH, DEFAULT_SERVICE_ACCOUNT_KEY, MAILBOXES

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

NEW_COLUMNS = [
    "total_sent_count",
    "total_reply_count",
    "first_contact_date",
    "last_sent_date",
    "last_reply_date",
    "avg_reply_latency_days",
    "days_since_last_reply",
    "engagement_tier",
]

INVALID_EMAIL_TIER = "INVALID_EMAIL"
SELF_TIER = "SELF"

TOP_N_NEVER_REPLIED = 20


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="engagement-tracker",
        description="Score a contact list by reply engagement across your Gmail mailboxes.",
    )
    parser.add_argument("input_csv", type=Path, help="Path to the input contacts CSV")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Path to write the scored CSV (default: <input>_scored.csv)",
    )
    parser.add_argument(
        "--email-column", default="email",
        help="Name of the column containing the contact's email address (default: email)",
    )
    parser.add_argument(
        "--service-account-key", default=DEFAULT_SERVICE_ACCOUNT_KEY,
        help=f"Path to the domain-wide-delegation service account JSON key (default: {DEFAULT_SERVICE_ACCOUNT_KEY})",
    )
    parser.add_argument(
        "--mailboxes", default=None,
        help="Comma-separated list of mailboxes to pull from (default: the 5 configured in config.py)",
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB_PATH,
        help=f"Path to the local SQLite cache (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--full-resync", action="store_true",
        help="Wipe the cached history for the target mailboxes and re-sync from scratch",
    )
    parser.add_argument(
        "--no-sync", action="store_true",
        help="Skip Gmail entirely and recompute scores from the existing local cache "
        "(useful for re-tuning tier thresholds without re-hitting the API)",
    )
    return parser.parse_args(argv)


def read_contacts(input_csv, email_column):
    with input_csv.open(newline="", encoding="utf-8-sig") as f:
        probe = csv.reader(f)
        first_row = next(probe, None)
        f.seek(0)

        # Headerless single-column file (just a list of addresses, no "email"
        # row) - detected the same way email_frequency_counter does: one
        # column, and its first cell is already a valid address rather than
        # a header label.
        if first_row and len(first_row) == 1 and EMAIL_RE.match(first_row[0].strip()):
            reader = csv.reader(f)
            rows = [{email_column: row[0].strip()} for row in reader if row and row[0].strip()]
            return rows, [email_column]

        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"{input_csv} appears to be empty (no header row)")
        if email_column not in reader.fieldnames:
            raise ValueError(
                f"Column {email_column!r} not found in {input_csv}. "
                f"Available columns: {', '.join(reader.fieldnames)}. "
                f"Pass --email-column to specify a different one."
            )
        rows = list(reader)
    return rows, list(reader.fieldnames)


def wipe_mailboxes(conn, mailboxes):
    placeholders = ",".join("?" * len(mailboxes))
    conn.execute(f"DELETE FROM messages WHERE mailbox IN ({placeholders})", mailboxes)
    conn.execute(f"DELETE FROM recipients WHERE mailbox IN ({placeholders})", mailboxes)
    conn.execute(f"DELETE FROM sync_state WHERE mailbox IN ({placeholders})", mailboxes)
    conn.commit()


def stats_to_row_fields(stats):
    return {
        "total_sent_count": stats.total_sent_count,
        "total_reply_count": stats.total_reply_count,
        "first_contact_date": stats.first_contact_date or "",
        "last_sent_date": stats.last_sent_date or "",
        "last_reply_date": stats.last_reply_date or "",
        "avg_reply_latency_days": stats.avg_reply_latency_days if stats.avg_reply_latency_days is not None else "",
        "days_since_last_reply": stats.days_since_last_reply if stats.days_since_last_reply is not None else "",
        "engagement_tier": stats.engagement_tier,
    }


BLANK_ROW_FIELDS = {col: "" for col in NEW_COLUMNS}


def sort_key(row):
    tier = row["engagement_tier"]
    priority = 0 if tier in scoring.HIGH_PRIORITY_TIERS else 1
    sent_count = row["total_sent_count"] if isinstance(row["total_sent_count"], int) else 0
    return (priority, -sent_count)


def main(argv=None):
    args = parse_args(argv)

    if not args.input_csv.exists():
        sys.exit(f"Error: input file not found: {args.input_csv}")

    output_path = args.output or args.input_csv.with_name(f"{args.input_csv.stem}_scored.csv")
    mailboxes = (
        [m.strip() for m in args.mailboxes.split(",") if m.strip()] if args.mailboxes else MAILBOXES
    )
    mailboxes_lower = {m.lower() for m in mailboxes}

    try:
        rows, fieldnames = read_contacts(args.input_csv, args.email_column)
    except ValueError as e:
        sys.exit(f"Error: {e}")

    conn = cache.connect(args.db)

    if not args.no_sync:
        if args.full_resync:
            print(f"Wiping cached history for {len(mailboxes)} mailbox(es)...")
            wipe_mailboxes(conn, mailboxes)
        services = auth.build_services(args.service_account_key, mailboxes)
        gmail_sync.sync_all(conn, services)
    else:
        print("--no-sync: recomputing from existing cache only, no Gmail calls made.")

    unique_emails = set()
    for row in rows:
        raw = (row.get(args.email_column) or "").strip()
        if raw and EMAIL_RE.match(raw):
            unique_emails.add(raw.lower())

    print(f"Scoring {len(unique_emails)} unique contact(s)...")
    stats_by_email = {}
    for email in unique_emails:
        if email in mailboxes_lower:
            continue
        stats_by_email[email] = scoring.compute_contact_stats(conn, mailboxes, email)

    out_fieldnames = list(fieldnames) + NEW_COLUMNS
    output_rows = []
    malformed_count = 0
    self_count = 0

    for row in rows:
        raw = (row.get(args.email_column) or "").strip()
        out_row = dict(row)

        if not raw or not EMAIL_RE.match(raw):
            out_row.update(BLANK_ROW_FIELDS)
            out_row["engagement_tier"] = INVALID_EMAIL_TIER
            malformed_count += 1
        elif raw.lower() in mailboxes_lower:
            out_row.update(BLANK_ROW_FIELDS)
            out_row["engagement_tier"] = SELF_TIER
            self_count += 1
        else:
            stats = stats_by_email[raw.lower()]
            out_row.update(stats_to_row_fields(stats))

        output_rows.append(out_row)

    output_rows.sort(key=sort_key)

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)

    tier_counts = Counter(row["engagement_tier"] for row in output_rows)

    never_replied_rows = [
        row for row in output_rows
        if row["engagement_tier"] == scoring.NEVER_REPLIED
    ]
    never_replied_rows.sort(key=lambda r: -r["total_sent_count"])

    print()
    print("=== Summary ===")
    print(f"Total contacts processed: {len(output_rows)}")
    if malformed_count:
        print(f"  Malformed/skipped emails: {malformed_count}")
    if self_count:
        print(f"  Own mailbox addresses skipped: {self_count}")
    print()
    print("By engagement tier:")
    for tier in [scoring.RESPONSIVE, scoring.OCCASIONAL, scoring.NEVER_REPLIED,
                 scoring.COLD, scoring.INSUFFICIENT_DATA]:
        print(f"  {tier:<20} {tier_counts.get(tier, 0)}")
    if tier_counts.get(INVALID_EMAIL_TIER):
        print(f"  {INVALID_EMAIL_TIER:<20} {tier_counts[INVALID_EMAIL_TIER]}")
    if tier_counts.get(SELF_TIER):
        print(f"  {SELF_TIER:<20} {tier_counts[SELF_TIER]}")

    print()
    print(f"Top {TOP_N_NEVER_REPLIED} NEVER_REPLIED contacts by sent_count (highest priority to review):")
    email_col = args.email_column
    for row in never_replied_rows[:TOP_N_NEVER_REPLIED]:
        print(f"  {row['total_sent_count']:>4}  {row.get(email_col, '')}")
    if not never_replied_rows:
        print("  (none)")

    print()
    print(f"Output written to: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
