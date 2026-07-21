"""Retry helpers for transient/rate-limit errors, shared by list and batch calls."""

import random
import sys
import time

from googleapiclient.errors import HttpError

from .config import MAX_RETRIES

RETRYABLE_STATUSES = (403, 429, 500, 503)


def call_with_backoff(func, max_retries=MAX_RETRIES):
    """Call func() with exponential backoff on transient/rate-limit errors."""
    for attempt in range(max_retries):
        try:
            return func()
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if status in RETRYABLE_STATUSES and attempt < max_retries - 1:
                delay = (2 ** attempt) + random.uniform(0, 1)
                print(
                    f"  Rate limited/transient error (status {status}); "
                    f"retrying in {delay:.1f}s...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise


def execute_batch_with_retry(service, build_request, request_ids, max_retries=MAX_RETRIES):
    """Execute a Gmail batch request for `request_ids`, retrying failed sub-requests.

    `build_request` takes a single request_id and returns the HttpRequest to
    include in the batch. Returns {request_id: response}; ids that fail with
    a non-retryable error, or exhaust retries, are omitted and logged.
    """
    results = {}
    pending = list(request_ids)

    for attempt in range(max_retries):
        if not pending:
            break

        retry_next = []
        errors = {}

        def make_callback():
            def callback(request_id, response, exception):
                if exception is not None:
                    status = getattr(getattr(exception, "resp", None), "status", None)
                    if status in RETRYABLE_STATUSES:
                        retry_next.append(request_id)
                    else:
                        errors[request_id] = exception
                else:
                    results[request_id] = response
            return callback

        batch = service.new_batch_http_request(callback=make_callback())
        for request_id in pending:
            batch.add(build_request(request_id), request_id=request_id)

        try:
            batch.execute()
        except Exception as e:
            # The whole batch HTTP call failed (network blip, DNS hiccup, a
            # 5xx from the /batch endpoint itself) rather than a per-item
            # failure inside it - none of `pending` reached the callback,
            # so retry the entire chunk rather than losing it.
            status = getattr(getattr(e, "resp", None), "status", None)
            print(
                f"  Batch request failed ({status or type(e).__name__}: {e}); "
                f"will retry the whole batch.",
                file=sys.stderr,
            )
            retry_next = list(pending)
            errors = {}

        for request_id, exception in errors.items():
            print(f"  Error fetching message {request_id}: {exception}", file=sys.stderr)

        if retry_next and attempt < max_retries - 1:
            delay = (2 ** attempt) + random.uniform(0, 1)
            print(
                f"  Rate limited/transient error on {len(retry_next)} message(s); "
                f"retrying in {delay:.1f}s...",
                file=sys.stderr,
            )
            time.sleep(delay)
        pending = retry_next

    if pending:
        print(
            f"  Giving up on {len(pending)} message(s) after {max_retries} attempts.",
            file=sys.stderr,
        )

    return results
