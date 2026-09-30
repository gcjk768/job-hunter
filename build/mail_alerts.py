"""Job-alert emails as a source: LinkedIn, JobStreet, Glassdoor and Indeed all email you their matches,
so the boards that stay login-walled to a scraper still reach the watcher, through your own inbox.

Reads over IMAP in read-only mode (BODY.PEEK: nothing is marked read, moved or deleted). Only messages
from MAIL_FROM senders whose subject looks like a job alert are read. Pulling the individual jobs out of
an alert is left to the model (nas_agent.alert_jobs), because every board lays its email out differently
and changes it often; this module only fetches and flattens the text, keeping each link's URL.

Env: IMAP_HOST (imap.gmail.com), IMAP_USER, IMAP_PASSWORD (a Gmail *app password*), IMAP_FOLDER (INBOX),
     MAIL_FROM (comma-separated sender substrings), MAIL_DAYS (3).
"""
from __future__ import annotations

import datetime as dt
import email
import email.policy
import hashlib
import html
import imaplib
import os
import re

MAIL_FROM = [s.strip() for s in os.environ.get(
    "MAIL_FROM", "jobalerts-noreply@linkedin.com,jobs-noreply@linkedin.com,jobstreet,glassdoor,indeed"
).split(",") if s.strip()]
ALERT_SUBJECT = re.compile(r"job|role|position|hiring|opening|vacanc|engineer|devops|sre|platform|cloud", re.I)


def configured() -> bool:
    return bool(os.environ.get("IMAP_USER") and os.environ.get("IMAP_PASSWORD"))


def html_to_text(body: str) -> str:
    """Flatten an HTML email, keeping each link as `text [url]` so the model can pair jobs with links."""
    body = re.sub(r"<(script|style|head)[^>]*>.*?</\1>", " ", body, flags=re.I | re.S)
    body = re.sub(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                  lambda m: f" {re.sub(r'<[^>]+>', ' ', m.group(2))} [{m.group(1)}] ", body, flags=re.I | re.S)
    body = re.sub(r"<(br|/p|/div|/tr|/li|/h\d)[^>]*>", "\n", body, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def message_text(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("html", "plain"))
    if part is None:
        return ""
    body = part.get_content()
    return html_to_text(body) if part.get_content_type() == "text/html" else body


def canonical_url(url: str) -> str:
    """Strip tracking so the same job from two alerts dedups. LinkedIn wraps /jobs/view/<id> in /comm/ links."""
    url = html.unescape(url.strip())
    m = re.search(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?]*-)?(\d{6,})", url)
    if m:
        return f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
    m = re.search(r"jobstreet\.com/(?:[a-z-]+/)*job/(\d+)", url)
    if m:
        return f"https://sg.jobstreet.com/job/{m.group(1)}"
    return url.split("?")[0] if "linkedin.com" in url or "jobstreet" in url else url


PAY = re.compile(r"(?:S\$|SGD|US\$|USD|\$)\s*([\d][\d.,]*)\s*(k)?"
                 r"(?:\s*(?:-|–|—|to)\s*(?:S\$|SGD|US\$|USD|\$)?\s*([\d][\d.,]*)\s*(k)?)?", re.I)


def parse_pay(text: str) -> tuple[int | None, int | None]:
    """(min, max) per month from an alert's salary text, e.g. "S$10K - S$14K / month" or
    "$120,000 - $150,000 a year". (None, None) when there is none or it is hourly/unclear."""
    m = PAY.search(text or "")
    if not m or re.search(r"hour|/hr\b|per hr|daily|/day", text, re.I):
        return None, None

    def num(v: str | None, k: str | None) -> float | None:
        if not v:
            return None
        n = float(v.replace(",", ""))
        return n * 1000 if k else n

    lo, hi = num(m.group(1), m.group(2)), num(m.group(3), m.group(4) or m.group(2))
    yearly = re.search(r"year|annum|annual|/yr|\bp\.?a\.?\b|yearly", text, re.I) or (lo or 0) >= 40000
    if yearly:
        lo, hi = lo / 12 if lo else lo, hi / 12 if hi else hi
    if not lo or lo < 500:  # not a monthly salary we can trust
        return None, None
    return round(lo), round(hi) if hi else None


def job_key(title: str, company: str, url: str) -> str:
    """Stable id: the board's own job id when the URL carries one, else title+company (catches the same
    role arriving from two boards)."""
    m = re.search(r"linkedin\.com/jobs/view/(\d+)", url) or re.search(r"jobstreet\.com/job/(\d+)", url)
    if m:
        return ("li:" if "linkedin" in url else "js:") + m.group(1)
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()  # noqa: E731
    return "mail:" + hashlib.sha1(f"{norm(company)}|{norm(title)}".encode()).hexdigest()[:12]


def fetch(done: set[str], days: int | None = None, imap_factory=None) -> list[dict]:
    """New alert emails as [{"id", "from", "subject", "text"}], skipping Message-IDs in `done`."""
    days = days or int(os.environ.get("MAIL_DAYS", "3"))
    since = (dt.date.today() - dt.timedelta(days=days)).strftime("%d-%b-%Y")
    make = imap_factory or (lambda: imaplib.IMAP4_SSL(os.environ.get("IMAP_HOST", "imap.gmail.com")))
    box = make()
    try:
        box.login(os.environ["IMAP_USER"], os.environ["IMAP_PASSWORD"])
        box.select(os.environ.get("IMAP_FOLDER", "INBOX"), readonly=True)
        nums: list[bytes] = []
        for sender in MAIL_FROM:
            _, data = box.search(None, "SINCE", since, "FROM", f'"{sender}"')
            nums += [n for n in (data[0] or b"").split() if n not in nums]
        out = []
        for num in nums:
            _, data = box.fetch(num, "(BODY.PEEK[])")
            raw = next((d[1] for d in data if isinstance(d, tuple)), None)
            if raw is None:
                continue
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            mid = (msg.get("Message-ID") or f"num:{num.decode()}").strip()
            subject = str(msg.get("Subject") or "")
            if mid in done or not ALERT_SUBJECT.search(subject):
                continue
            out.append({"id": mid, "from": str(msg.get("From") or ""), "subject": subject,
                        "text": message_text(msg)[:15000]})
        return out
    finally:
        try:
            box.logout()
        except Exception:
            pass
