"""
GmailAgent: sends HTML emails via Gmail API and checks for replies.

MIME structure:
  multipart/mixed
    ├── multipart/alternative
    │   ├── text/plain  (fallback for older clients)
    │   └── text/html   (rendered by all modern clients)
    └── application/pdf (CV attachment)
"""

import base64
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from config.settings import GOOGLE_SCOPES

log = logging.getLogger(__name__)

_OOO_SUBJECT_PATTERNS = [
    "out of office", "automatic reply", "auto-reply", "autoreply",
    "away from office", "on leave", "on annual leave", "on vacation",
    "i am away", "i'm away", "i am out", "i'm out", "ooo",
]


def _load_creds(token_file: str) -> Credentials:
    creds = Credentials.from_authorized_user_file(token_file, GOOGLE_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError(f"Invalid credentials in {token_file}. Run setup_auth.py.")
    return creds


class GmailAgent:
    def __init__(self, sender: dict):
        self.sender = sender
        creds = _load_creds(sender["token_file"])
        self.service = build("gmail", "v1", credentials=creds)

    def send_email(
        self,
        to: str,
        subject: str,
        body_html: str,
        body_plain: str,
        cv_path: str = "",
        cv_bytes: bytes = None,
        cv_filename: str = "",
        reply_to_thread_id: str = "",
    ) -> str:
        """
        Send an HTML email with plain text fallback and CV attached.
        If reply_to_thread_id is set the email is sent as a reply in that thread.
        cv_bytes (with cv_filename) takes priority over cv_path when both are given —
        used for followups reattaching the exact PDF sent in the initial email.
        Returns the Gmail threadId.
        """
        # When replying, prefix subject with Re: if not already present
        if reply_to_thread_id and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        outer = MIMEMultipart("mixed")
        outer["From"]    = f"{self.sender['name']} <{self.sender['email']}>"
        outer["To"]      = to
        outer["Subject"] = subject

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_plain, "plain", "utf-8"))
        alt.attach(MIMEText(body_html,  "html",  "utf-8"))
        outer.attach(alt)

        if cv_bytes:
            name = cv_filename or "CV.pdf"
            part = MIMEApplication(cv_bytes, Name=name)
            part["Content-Disposition"] = f'attachment; filename="{name}"'
            outer.attach(part)
        elif cv_path and os.path.exists(cv_path):
            with open(cv_path, "rb") as f:
                part = MIMEApplication(f.read(), Name=os.path.basename(cv_path))
            part["Content-Disposition"] = (
                f'attachment; filename="{os.path.basename(cv_path)}"'
            )
            outer.attach(part)
        else:
            log.warning("CV not found at %s — sending without attachment", cv_path)

        raw  = base64.urlsafe_b64encode(outer.as_bytes()).decode("utf-8")
        body = {"raw": raw}
        if reply_to_thread_id:
            body["threadId"] = reply_to_thread_id

        result = (
            self.service.users()
            .messages()
            .send(userId="me", body=body)
            .execute()
        )

        thread_id = result.get("threadId") or reply_to_thread_id or ""
        log.info("Sent to %s | subject: %s | thread: %s", to, subject, thread_id)
        return thread_id

    def find_recent_sent(self, to: str, subject: str, since_minutes: int = 20) -> str:
        """
        Check Sent for a message to `to` with this exact subject sent within the last
        since_minutes. Used after send_email() raises (e.g. a client-side read timeout)
        to tell a real failure apart from a send that reached Gmail but whose response
        was lost — so callers can record it instead of resending a duplicate.
        Returns the threadId if found, else "".
        """
        try:
            q = f'to:{to} subject:"{subject}" newer_than:1d'
            result = (
                self.service.users().messages()
                .list(userId="me", q=q, maxResults=5)
                .execute()
            )
            messages = result.get("messages", [])
            if not messages:
                return ""
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
            for m in messages:
                full = (
                    self.service.users().messages()
                    .get(userId="me", id=m["id"], format="minimal")
                    .execute()
                )
                sent_at = datetime.fromtimestamp(
                    int(full.get("internalDate", "0")) / 1000, tz=timezone.utc
                )
                if sent_at >= cutoff:
                    return full.get("threadId", "")
            return ""
        except Exception as exc:
            log.warning("Could not check recent sent messages to %s: %s", to, exc)
            return ""

    def get_first_attachment(self, thread_id: str):
        """
        Fetch the attachment from the FIRST message in a thread — i.e. the CV sent
        with the initial email. Used so followups reattach the exact same file
        rather than a freshly (and possibly differently) tailored one.
        Returns (filename, bytes) or (None, None) if no thread/attachment found.
        """
        if not thread_id:
            return None, None
        try:
            thread = (
                self.service.users().threads()
                .get(userId="me", id=thread_id, format="full")
                .execute()
            )
            messages = thread.get("messages", [])
            if not messages:
                return None, None

            def _find_attachment_part(part):
                if part.get("filename") and part.get("body", {}).get("attachmentId"):
                    return part
                for sub in part.get("parts", []) or []:
                    found = _find_attachment_part(sub)
                    if found:
                        return found
                return None

            first_msg = messages[0]
            part = _find_attachment_part(first_msg.get("payload", {}))
            if not part:
                return None, None

            attachment = (
                self.service.users().messages().attachments()
                .get(userId="me", messageId=first_msg["id"], id=part["body"]["attachmentId"])
                .execute()
            )
            data = attachment.get("data", "")
            if not data:
                return None, None
            return part.get("filename", "CV.pdf"), base64.urlsafe_b64decode(data + "==")
        except Exception as exc:
            log.warning("Could not fetch initial attachment from thread %s: %s", thread_id, exc)
            return None, None

    def has_bounced(self, recipient_email: str) -> bool:
        """Return True if any delivery failure notification exists for recipient_email."""
        if not recipient_email:
            return False
        try:
            q = (
                f"({recipient_email}) "
                f"(from:mailer-daemon OR from:postmaster "
                f'OR subject:"delivery failed" OR subject:"undeliverable" '
                f'OR subject:"delivery status notification" OR subject:"mail delivery")'
            )
            result = (
                self.service.users()
                .messages()
                .list(userId="me", q=q, maxResults=1)
                .execute()
            )
            return bool(result.get("messages"))
        except Exception as exc:
            log.warning("Could not check bounce for %s: %s", recipient_email, exc)
            return False

    def has_reply_in_thread(self, thread_id: str, recipient_email: str) -> bool:
        """Return True if recipient_email has replied in the specific outreach thread."""
        if not thread_id or not recipient_email:
            return False
        try:
            thread = (
                self.service.users()
                .threads()
                .get(userId="me", id=thread_id, format="metadata",
                     metadataHeaders=["From"])
                .execute()
            )
            for msg in thread.get("messages", []):
                headers = {
                    h["name"].lower(): h["value"]
                    for h in msg.get("payload", {}).get("headers", [])
                }
                if recipient_email.lower() in headers.get("from", "").lower():
                    return True
            return False
        except Exception as exc:
            log.warning("Could not check thread %s for reply: %s", thread_id, exc)
            return False

    def get_reply_body_in_thread(self, thread_id: str, recipient_email: str) -> str:
        """
        Return concatenated body text of every message in the thread NOT sent by us —
        i.e. the recipient's reply, or a colleague replying on their behalf. Used to
        classify reply content (e.g. detecting a "no longer with the company" notice).
        """
        if not thread_id or not recipient_email:
            return ""
        try:
            thread = (
                self.service.users()
                .threads()
                .get(userId="me", id=thread_id, format="full")
                .execute()
            )
            own_email = self.sender["email"].lower()
            bodies = []
            for msg in thread.get("messages", []):
                headers = {
                    h["name"].lower(): h["value"]
                    for h in msg.get("payload", {}).get("headers", [])
                }
                if own_email in headers.get("from", "").lower():
                    continue
                body = self._extract_body_text(msg)
                if body:
                    bodies.append(body)
            return "\n\n---\n\n".join(bodies)
        except Exception as exc:
            log.warning("Could not fetch reply body for thread %s: %s", thread_id, exc)
            return ""

    def get_ooo_reply(self, thread_id: str, recipient_email: str, after_date: str = ""):
        """
        Check if recipient sent an OOO auto-reply anywhere in the inbox.
        Searches by sender address (OOO replies from Exchange/Outlook arrive as
        separate threads, not threaded with the original sent email).
        after_date: Gmail date filter string e.g. "2026/08/01" — only OOOs after this date.
        Returns (is_ooo: bool, body_text: str).
        """
        if not recipient_email:
            return False, ""
        try:
            subject_terms = " OR ".join(
                f'subject:"{p}"' for p in _OOO_SUBJECT_PATTERNS
            )
            date_filter = f" after:{after_date}" if after_date else ""
            q = f"from:{recipient_email} ({subject_terms}){date_filter}"
            result = (
                self.service.users()
                .messages()
                .list(userId="me", q=q, maxResults=1)
                .execute()
            )
            messages = result.get("messages", [])
            # Fallback: search by username only — catches OOOs sent from a different domain
            # (e.g. tom.baynes@afd-systems.com replying to tom.baynes@airframedesigns.com)
            if not messages and "@" in recipient_email:
                username = recipient_email.split("@")[0]
                q2 = f"from:{username} ({subject_terms}){date_filter}"
                result2 = (
                    self.service.users()
                    .messages()
                    .list(userId="me", q=q2, maxResults=1)
                    .execute()
                )
                messages = result2.get("messages", [])
            if messages:
                msg_full = (
                    self.service.users()
                    .messages()
                    .get(userId="me", id=messages[0]["id"], format="full")
                    .execute()
                )
                return True, self._extract_body_text(msg_full)

            # Fallback: check the actual thread for any OOO reply regardless of sender domain.
            # Catches cases where the OOO arrives from a different address (e.g. afd-systems.com
            # vs airframedesigns.com).
            if thread_id:
                thread = (
                    self.service.users()
                    .threads()
                    .get(userId="me", id=thread_id, format="full")
                    .execute()
                )
                own_email = self.sender["email"].lower()
                for msg in thread.get("messages", [])[1:]:  # skip first (our outbound)
                    headers = {h["name"].lower(): h["value"].lower()
                               for h in msg.get("payload", {}).get("headers", [])}
                    subject = headers.get("subject", "")
                    from_addr = headers.get("from", "")
                    # Any non-us reply with an OOO subject is from the recipient side
                    if own_email not in from_addr and any(p in subject for p in _OOO_SUBJECT_PATTERNS):
                        body = self._extract_body_text(msg)
                        return True, body

        except Exception as exc:
            log.warning("OOO check failed for %s: %s", recipient_email, exc)
        return False, ""

    def _extract_body_text(self, msg: dict) -> str:
        """Extract plain text from a Gmail message payload (up to 3000 chars)."""
        def _walk(part):
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            for sub in part.get("parts", []):
                result = _walk(sub)
                if result:
                    return result
            return ""
        return _walk(msg.get("payload", {}))[:3000]
