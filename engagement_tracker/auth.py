"""Domain-wide delegation auth: one impersonated Gmail service per mailbox."""

import os
import sys

from google.oauth2 import service_account
from googleapiclient.discovery import build

from .config import SCOPES


def load_service_account_key(key_path):
    if not os.path.exists(key_path):
        sys.exit(
            f"Error: service account key not found at '{key_path}'.\n"
            "See the README for how to create one and authorize it for "
            "domain-wide delegation in the Workspace admin console."
        )
    return key_path


def build_service(key_path, mailbox_email):
    """Return a Gmail API service impersonating `mailbox_email` via domain-wide delegation."""
    base_creds = service_account.Credentials.from_service_account_file(
        key_path, scopes=SCOPES
    )
    delegated_creds = base_creds.with_subject(mailbox_email)
    return build("gmail", "v1", credentials=delegated_creds, cache_discovery=False)


def build_services(key_path, mailboxes):
    """Return {mailbox_email: service} for every mailbox, failing fast on auth errors."""
    load_service_account_key(key_path)
    services = {}
    for mailbox in mailboxes:
        try:
            services[mailbox] = build_service(key_path, mailbox)
        except Exception as e:
            sys.exit(
                f"Error: failed to authenticate as '{mailbox}' via domain-wide "
                f"delegation: {e}\n"
                "Confirm the service account's client ID is authorized for this "
                "mailbox's scope in the Workspace admin console (Security > API "
                "Controls > Domain-wide Delegation), and that the account has "
                "gmail.metadata authorized."
            )
    return services
