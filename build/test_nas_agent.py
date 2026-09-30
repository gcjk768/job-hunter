"""Offline tests for the NAS loop, the Telegram commands and the watchdog. No network: Telegram,
`claude -p` and MyCareersFuture are faked (urlopen and subprocess.run).

    python build/test_nas_agent.py
"""
from __future__ import annotations

import datetime as dt
import email.message
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

os.environ.update(TELEGRAM_BOT_TOKEN="T", TELEGRAM_CHAT_ID="42")
os.environ.pop("TELEGRAM_THREAD_ID", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import nas_agent as a  # noqa: E402
import watchdog  # noqa: E402

TMP = Path(tempfile.mkdtemp())
a.STATE, a.OUT = TMP / "state.json", TMP / "out"
SENT: list[str] = []
UPDATES: list[dict] = []
VERDICT = {"suitable": True, "score": 85, "reason": "fits", "gaps": "none", "coding_test_risk": "low",
           "tagline": "", "summary": "", "projects": [], "letter": ["p1", "p2"], "employer": "Acme",
           "contact_titles": ["Head of Platform"], "outreach": "Hi, I run EKS platforms and saw your SRE role."}
EXTRACTED = {"jobs": [
    {"title": "Senior SRE", "company": "Globex", "location": "Singapore", "snippet": "EKS, Terraform",
     "url": "https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc&refId=x", "salary": "S$10K - S$14K / month"},
    {"title": "Marketing Manager", "company": "Globex", "location": "Singapore", "snippet": "",
     "url": "https://www.linkedin.com/comm/jobs/view/4099999999/", "salary": ""},
    {"title": "Junior DevOps Engineer", "company": "Hooli", "location": "Singapore", "snippet": "",
     "url": "https://www.linkedin.com/comm/jobs/view/4077777777/", "salary": "S$1,200 - S$1,500 / month"}]}
FACTCHECK = {"issues": ["'10 years of Kubernetes' -> removed, the resume shows 4"], "tagline": "t",
             "summary": "s", "outreach": "Checked note.", "letter": ["checked p1", "checked p2"]}


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def fake_urlopen(req, data=None, timeout=0):
    url = req if isinstance(req, str) else req.full_url
    if "sendMessage" in url:
        SENT.append(urllib.parse.unquote_plus(data.decode()))
        return Resp(b"{}")
    if "getUpdates" in url:
        out, UPDATES[:] = list(UPDATES), []
        return Resp(json.dumps({"result": out}).encode())
    if "mycareersfuture" in url and url.rstrip("/").endswith("a" * 32):
        return Resp(json.dumps({"title": "Platform Engineer", "hiringCompany": {"name": "Acme"},
                                "salary": {"minimum": 9000, "maximum": 12000}, "description": "<p>k8s</p>",
                                "metadata": {"jobDetailsUrl": "https://mcf/job"}}).encode())
    if url.startswith("https://boards-api.greenhouse.io/v1/boards/") and url.endswith("/jobs/77"):
        return Resp(json.dumps({"content": "&lt;p&gt;Own our &lt;b&gt;EKS&lt;/b&gt; fleet&lt;/p&gt;"}).encode())
    if url.startswith("https://boards-api.greenhouse.io/v1/boards/") and url.endswith("/jobs"):
        return Resp(json.dumps({"jobs": [{"id": 77, "title": "SRE"}]}).encode())
    if url.startswith("https://api.mycareersfuture.gov.sg/v2/jobs?"):
        return Resp(json.dumps({"results": [{"uuid": "u1"}, {"uuid": "u2"}]}).encode())
    if url.startswith("https://api.ashbyhq.com/posting-api/job-board/"):
        return Resp(json.dumps({"jobs": [{"title": "SRE", "descriptionPlain": "Run infra"}]}).encode())
    if url.startswith("https://api.lever.co/v0/postings/"):
        return Resp(json.dumps([]).encode())
    if url.startswith("https://www.linkedin.com/jobs/view/"):
        return Resp(b"<html><title>LinkedIn</title><body>Sign in to view this job. Join now. " + b"x " * 300 + b"</body></html>")
    if url.startswith("https://example.com"):
        return Resp(b"<html><title>SRE at Example</title><body>" + b"Run Kubernetes. " * 30 + b"</body></html>")
    raise AssertionError(f"unexpected URL {url}")


CALLS: list[list[str]] = []


def fake_run(cmd, input=None, cwd=None, **kw):
    """Stands in for the claude CLI: --version, a schema'd judge call, or a plain /ask."""
    CALLS.append(cmd)
    if cmd[1:] == ["--version"]:
        return subprocess.CompletedProcess(cmd, 0, "2.1.0 (Claude Code)", "")
    assert "--tools" in cmd and cmd[cmd.index("--tools") + 1] == "", "tools must be disabled"
    assert cwd and not os.listdir(cwd), "must run in an empty directory"
    out = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01}
    if "--json-schema" in cmd:
        schema = cmd[cmd.index("--json-schema") + 1]
        got = EXTRACTED if '"jobs"' in schema else FACTCHECK if '"issues"' in schema else VERDICT
        out.update(result=json.dumps(got), structured_output=got)
    elif "which fits best?" in input:
        out["result"] = "Role X fits best."
    elif "interview prep pack" in input:
        out["result"] = "LIKELY QUESTIONS - design a multi-region EKS platform"
    elif "single word OK" in input:
        out["result"] = "OK"
    elif "follow-up email" in input:
        out["result"] = "Subject: Following up on the SRE role"
    else:
        out["result"] = "?"
    return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")


a.urllib.request.urlopen = fake_urlopen
a.subprocess.run = fake_run
real_build = a.build
a.build = lambda rec, v: TMP / "draft"  # document rendering is checked once, below
CHECKS: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    CHECKS.append((label, bool(ok)))


def msg(update_id: int, text: str, chat: int = 42, thread: int | None = None) -> dict:
    m = {"chat": {"id": chat}, "text": text}
    if thread:
        m["message_thread_id"] = thread
    return {"update_id": update_id, "message": m}


def rec(i: str) -> dict:
    return {"id": i, "title": f"SRE {i}", "company": "Acme", "source": "x", "lo": None}


# --- cycles ---------------------------------------------------------------------------------------
a.candidates = lambda: [rec("j1")]
a.cycle()
st = a.load_state()
check("baseline cycle records a run and heartbeat", st["runs"][-1]["note"] == "baseline" and st["heartbeat"])
a.cycle()
check("a quiet cycle still records a run", len(a.load_state()["runs"]) == 2)

a.candidates = lambda: [rec("j1"), rec("j2")]
SENT.clear()
a.cycle()
check("a fitting new posting is alerted", any("SRE j2" in s for s in SENT))
check("the cycle's claude cost is recorded (judge + fact-check)", a.load_state()["runs"][-1]["cost_usd"] == 0.02)
check("the verdict schema pins projects to the real library",
      a.verdict_schema()["properties"]["projects"]["items"]["enum"] == a.PROJECTS)

# --- claude -p error handling ---------------------------------------------------------------------
a.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "Invalid API key · Please run /login")
try:
    a.claude("x")
    check("a signed-out CLI raises ClaudeError", False)
except a.ClaudeError as exc:
    check("a signed-out CLI raises ClaudeError", "Invalid API key" in str(exc))
check("ClaudeError is transient (never counts toward MAX_ATTEMPTS)", not issubclass(a.ClaudeError, a.PERMANENT))
a.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(
    cmd, 0, json.dumps({"subtype": "success", "is_error": False, "result": "nope"}), "")
try:
    a.claude("x", {"type": "object"})
    check("missing structured output is a permanent error", False)
except a.PERMANENT:
    check("missing structured output is a permanent error", True)
a.subprocess.run = fake_run

real_judge = a.judge
a.candidates = lambda: [rec("j3")]
a.judge = lambda r: (_ for _ in ()).throw(a.ClaudeError("rate limited"))
SENT.clear()
for _ in range(a.MAX_ATTEMPTS + 1):
    a.cycle()
check("CLI failures warn on Telegram", any("judge calls failed" in s for s in SENT))
check("CLI failures never give up", "j3" not in a.load_state()["seen"])

a.judge = lambda r: (_ for _ in ()).throw(json.JSONDecodeError("bad", "", 0))
for _ in range(a.MAX_ATTEMPTS):
    a.cycle()
check("bad model output gives up after MAX_ATTEMPTS", a.load_state()["seen"].get("j3", {}).get("gave_up"))
a.judge = real_judge

st = a.load_state()
st["seen"]["old"] = {"t": "x", "c": "y", "at": "2000-01-01T00:00:00"}
st["seen"]["legacy"] = {"t": "x", "c": "y"}
a.prune(st)
check("prune drops old entries and stamps legacy ones", "old" not in st["seen"] and st["seen"]["legacy"]["at"])

# --- restart scheduling ---------------------------------------------------------------------------
now = dt.datetime.now()
check("due_in: never ran -> now", a.due_in({}) == 0)
check("due_in: just ran -> about one interval",
      abs(a.due_in({"last_cycle": now.isoformat()}) - a.INTERVAL_H * 3600) < 5)
check("due_in: overdue -> now",
      a.due_in({"last_cycle": (now - dt.timedelta(hours=a.INTERVAL_H + 1)).isoformat()}) == 0)

# --- Telegram commands ----------------------------------------------------------------------------
SENT.clear()
UPDATES[:] = [msg(5, "/status", thread=7), msg(6, "/status", chat=99), msg(7, "/ask@JobBot which fits best?"),
              msg(8, "/judge https://www.mycareersfuture.gov.sg/job/x-" + "a" * 32),
              msg(9, "/judge https://example.com/job/1"), msg(10, "/sweep")]
check("/sweep asks for a cycle", a.poll_commands(a.load_state(), 1) is True)
check("update offset persisted", a.load_state()["tg_offset"] == 11)
check("/status answers in the asking topic", "message_thread_id=7" in SENT[0] and "Heartbeat" in SENT[0])
check("/status shows the claude CLI version", "2.1.0 (Claude Code)" in SENT[0])
check("strangers are ignored", sum("Heartbeat" in s for s in SENT) == 1)
check("/ask returns the model's answer", any("Role X fits best." in s for s in SENT))
check("/judge reads MCF via the API", any("Platform Engineer" in s and "$9,000" in s for s in SENT))
check("/judge reads a plain page", any("SRE at Example" in s for s in SENT))
check("/judge records the posting as seen", "a" * 32 in a.load_state()["seen"])
try:
    a.posting_from("https://example.com/job/1 short")
    check("/judge with short pasted text falls back to the page", True)
except ValueError:
    check("/judge with short pasted text falls back to the page", False)

# --- alerts: urgency, outreach, refs --------------------------------------------------------------
SENT.clear()
a.candidates = lambda: [rec("j10")]
a.cycle()
alert = next((s for s in SENT if "SRE j10" in s), "")
check("score >= URGENT_SCORE is flagged 'apply today'", "🔥 Apply today" in alert)
check("alert carries who to reach, a people-search link and the note",
      "Reach out to: Head of Platform" in alert and "linkedin.com/search/results/people" in alert
      and "Checked note." in alert)
ref10 = a.load_state()["seen"]["j10"]["ref"]
check("fits get a ref number in the alert", f"/applied {ref10}" in alert)
check("the fact-check's corrections reach the alert", "1 claim(s) corrected" in alert and "Checked note." in alert)

v = a.factcheck(rec("x"), VERDICT)
check("fact-check replaces the drafts with the checked text", v["letter"] == ["checked p1", "checked p2"]
      and v["factcheck"] == FACTCHECK["issues"])
real_run = a.subprocess.run
a.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "rate limited")
v = a.factcheck(rec("x"), VERDICT)
a.subprocess.run = real_run
check("a failed fact-check keeps the drafts but says so",
      v["letter"] == VERDICT["letter"] and "Not fact-checked" in a.alert_text(rec("x"), v, TMP / "d"))

# --- full job descriptions and cross-source dedup -------------------------------------------------
import job_sources  # noqa: E402

check("Greenhouse's double-escaped HTML becomes plain text",
      job_sources._plain("&lt;p&gt;Run &amp;amp; own&lt;/p&gt;") == "Run & own")
check("Greenhouse descriptions are fetched for judging",
      a.description({"jd_url": "https://boards-api.greenhouse.io/v1/boards/acme/jobs/77"}) == "Own our EKS fleet")
check("records that carry a description (Ashby/Lever) use it",
      "Own our fleet" in a.PROMPT.format(today="", floor=1, excluded="", profile="", title="", company="", pay="",
                                         years="", desc={"desc": "Own our fleet"}.get("desc"), projects=[]))
check("fingerprints ignore legal suffixes, location and case",
      a.fingerprint("Globex Asia Pte. Ltd.", "Senior SRE (Singapore)") == a.fingerprint("GLOBEX", "senior sre"))
check("too-vague companies are never deduped", a.fingerprint("?", "SRE") == "")
SENT.clear()
a.candidates = lambda: [dict(rec("mcf-1"), company="Wayne Enterprises Pte Ltd", title="Platform Engineer"),
                        dict(rec("li:1"), company="Wayne Enterprises", title="Platform Engineer (Singapore)")]
a.cycle()
st = a.load_state()
check("the same role from two sources is judged and alerted once",
      st["seen"]["li:1"].get("dup_of") == "mcf-1" and sum("Platform Engineer" in s for s in SENT) == 1
      and st["runs"][-1]["dups"] == 1)

folder = real_build(rec("j10") | {"url": "https://x/job"},
                    dict(VERDICT, projects=a.PROJECTS[:3], tagline="t", summary="s"))
check("the draft folder gets outreach.md", "Head of Platform" in (folder / "outreach.md").read_text())

# --- outcome tracking -----------------------------------------------------------------------------
SENT.clear()
UPDATES[:] = [msg(20, f"/applied {ref10}"), msg(21, "/applied Staff SRE at Initech"), msg(22, "/pipeline"),
              msg(23, f"/interview #{ref10}")]
a.poll_commands(a.load_state(), 1)
st = a.load_state()
e10 = st["seen"]["j10"]
check("/applied records status and date", e10["status"] == "interview" and e10.get("applied_at"))
check("/applied with free text tracks a manual entry",
      any(e.get("source") == "manual" and e["c"] == "Initech" for e in st["seen"].values()))
check("/pipeline lists tracked applications", any("Initech" in s and f"#{ref10}" in s for s in SENT))
check("/interview replies with a prep pack", any("📚 LIKELY QUESTIONS" in s for s in SENT))
check("history keeps every status change", [h[0] for h in e10["history"]] == ["drafted", "applied", "interview"])

st["seen"]["old-tracked"] = {"t": "x", "c": "y", "at": "2000-01-01T00:00:00", "ref": 999}
a.prune(st)
check("prune keeps tracked applications", "old-tracked" in st["seen"])

# --- follow-ups and the weekly digest -------------------------------------------------------------
monday10 = dt.datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
monday10 -= dt.timedelta(days=monday10.weekday())
st = a.load_state()
man = next(k for k, e in st["seen"].items() if e.get("source") == "manual")
st["seen"][man]["applied_at"] = (monday10 - dt.timedelta(days=a.FOLLOWUP_DAYS + 1)).isoformat()
a.save_state(st)
SENT.clear()
a.chores(monday10)
a.chores(monday10)
check("a stale application gets exactly one follow-up nudge with a draft",
      sum("⏰" in s for s in SENT) == 1 and any("Following up on the SRE role" in s for s in SENT))
SENT.clear()
sunday10 = monday10 + dt.timedelta(days=6)
a.chores(sunday10)
a.chores(sunday10)
check("the weekly digest is sent once per week", sum("Weekly job-hunt digest" in s for s in SENT) == 1)
check("the digest reports the reply rate", any("reply rate" in s for s in SENT))
SENT.clear()
a.chores(sunday10 + dt.timedelta(days=1, hours=-3))
check("no chores before 09:00", not SENT)
st = a.load_state()
st["seen"][man].pop("followup_sent")
st.pop("followups_checked")
a.save_state(st)
real_run = a.subprocess.run
a.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "Invalid API key")
SENT.clear()
CALLS.clear()
for _ in range(3):
    a.chores(monday10)
a.subprocess.run = real_run
check("a daily state backup is written", (a.STATE.parent / "backups" / f"nas_state-{monday10:%Y-%m-%d}.json").exists())
for i in range(a.BACKUP_DAYS + 5):
    a.backup_state(monday10 - dt.timedelta(days=i))
check("only the last BACKUP_DAYS backups are kept",
      len(list((a.STATE.parent / "backups").glob("nas_state-*.json"))) == a.BACKUP_DAYS)
check("/status shows the deployed commit", "@" in a.code_version() or "copied files" in a.code_version())
check("a failing follow-up draft still nudges, once, without retrying every loop",
      sum("⏰" in s for s in SENT) == 1 and any("couldn't draft one" in s for s in SENT))

# --- sweep cadence --------------------------------------------------------------------------------
check("weekday working hours use the busy interval", a.interval_now(monday10) == a.BUSY_INTERVAL_H)
check("evenings use the normal interval", a.interval_now(monday10.replace(hour=22)) == a.INTERVAL_H)
check("weekends use the normal interval", a.interval_now(sunday10) == a.INTERVAL_H)

# --- job-alert emails -----------------------------------------------------------------------------
import mail_alerts  # noqa: E402

check("html_to_text keeps each link's URL",
      "Senior SRE [https://x/1]" in mail_alerts.html_to_text('<p><a href="https://x/1"><b>Senior SRE</b></a></p>'))
check("LinkedIn tracking links are canonicalised",
      mail_alerts.canonical_url("https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc")
      == "https://www.linkedin.com/jobs/view/4012345678/")
check("board job ids become stable keys",
      mail_alerts.job_key("x", "y", "https://www.linkedin.com/jobs/view/4012345678/") == "li:4012345678")
check("without an id the key is title+company, case-insensitive",
      mail_alerts.job_key("Senior SRE", "Globex", "https://g/1") == mail_alerts.job_key("senior sre", "GLOBEX", "https://g/2"))


def raw_mail(mid: str, sender: str, subject: str, body: str) -> bytes:
    m = email.message.EmailMessage()
    m["Message-ID"], m["From"], m["Subject"] = mid, sender, subject
    m.set_content(body, subtype="html")
    return m.as_bytes()


class FakeIMAP:
    boxes = {b"1": raw_mail("<a1@li>", "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
                            "10 new jobs for platform engineer", '<a href="https://li/1">Senior SRE</a>'),
             b"2": raw_mail("<n1@li>", "LinkedIn <jobalerts-noreply@linkedin.com>",
                            "Your weekly profile views", "<p>not a job</p>")}
    readonly = None

    def login(self, user, pw):
        pass

    def select(self, folder, readonly=False):
        FakeIMAP.readonly = readonly

    def search(self, charset, *crit):
        return "OK", [b"1 2" if "linkedin" in crit[-1] else b""]

    def fetch(self, num, what):
        assert "PEEK" in what, "must not mark mail as read"
        return "OK", [(b"", self.boxes[num])]

    def logout(self):
        pass


os.environ.update(IMAP_USER="me@gmail.com", IMAP_PASSWORD="app-pw")
mails = mail_alerts.fetch(set(), imap_factory=FakeIMAP)
check("only alert-looking emails are read, read-only", [m["id"] for m in mails] == ["<a1@li>"] and FakeIMAP.readonly)
check("already-processed emails are skipped", mail_alerts.fetch({"<a1@li>"}, imap_factory=FakeIMAP) == [])

real_fetch = mail_alerts.fetch
a.mail_alerts.fetch = lambda done: real_fetch(done, imap_factory=FakeIMAP)
CALLS.clear()
SENT.clear()
a.candidates = lambda: []
a.cycle()
st = a.load_state()
check("alert emails are read with the cheap extract model",
      any("--model" in c and c[c.index("--model") + 1] == a.EXTRACT_MODEL for c in CALLS))
check("jobs from alert emails are judged and alerted",
      "li:4012345678" in st["seen"] and any("via email:linkedin" in s for s in SENT))
check("the regex filters apply to email jobs too", "li:4099999999" not in st["seen"])
check("the pay floor applies to email jobs that show a salary", "li:4077777777" not in st["seen"])
check("email salaries are parsed to monthly SGD", any("$10,000–14000/mo" in s for s in SENT))
for text, want in [("S$10K - S$14K / month", (10000, 14000)), ("$120,000 - $150,000 a year", (10000, 12500)),
                   ("SGD 8,000 - 10,000 per month", (8000, 10000)), ("$50 - $70 per hour", (None, None)),
                   ("Competitive", (None, None)), ("$144K/yr", (12000, None))]:
    check(f"parse_pay({text!r})", mail_alerts.parse_pay(text) == want)
check("processed emails are remembered", "<a1@li>" in st["mail_done"])
SENT.clear()
a.cycle()
check("the same alert is not judged twice", not any("Senior SRE" in s for s in SENT))
two = [{"id": "<ok@li>", "from": "jobalerts-noreply@linkedin.com", "subject": "jobs", "text": "a"},
       {"id": "<bad@li>", "from": "jobalerts-noreply@linkedin.com", "subject": "jobs", "text": "b"}]
real_alert_jobs = a.alert_jobs
a.alert_jobs = lambda m: (_ for _ in ()).throw(a.ClaudeError("limit")) if m["id"] == "<bad@li>" else \
    [dict(rec("mail-ok"), source="email:linkedin")]
a.mail_alerts.fetch = lambda done: [m for m in two if m["id"] not in done]
a.cycle()
st = a.load_state()
check("one bad alert email doesn't lose the jobs from the others",
      "mail-ok" in st["seen"] and "<ok@li>" in st["mail_done"] and "<bad@li>" not in st["mail_done"])
check("the unread email is reported in the run note", "1 alert email(s) not read" in st["runs"][-1]["note"])
a.alert_jobs = real_alert_jobs
a.mail_alerts.fetch = lambda done: (_ for _ in ()).throw(OSError("imap down"))
a.cycle()
check("a mail failure doesn't stop the sweep", "imap down" in a.load_state()["runs"][-1]["note"])

# --- sharing links in Telegram --------------------------------------------------------------------
SENT.clear()
UPDATES[:] = [msg(50, "Check out this job at Stark: https://www.linkedin.com/jobs/view/4011111111/?trackingId=Zx&refId=1"),
              msg(51, "thanks!"), msg(52, "https://www.linkedin.com/jobs/view/4011111111/", chat=99)]
a.poll_commands(a.load_state(), 1)
st = a.load_state()
check("a shared LinkedIn link behind a login asks for the description",
      any("🔒 That page needs a login" in s for s in SENT)
      and st.get("pending_judge", {}).get("url") == "https://www.linkedin.com/jobs/view/4011111111/")
check("plain chat and other chats are ignored", len([s for s in SENT if "🔒" in s]) == 1 and not any("thanks" in s for s in SENT))
SENT.clear()
jd = "Senior SRE at Stark Industries, Singapore. " + "You will run our Kubernetes platform on AWS with Terraform. " * 6
UPDATES[:] = [msg(53, jd)]
a.poll_commands(a.load_state(), 1)
st = a.load_state()
check("the pasted description is judged against the shared link",
      "li:4011111111" in st["seen"] and st["seen"]["li:4011111111"]["url"] == "https://www.linkedin.com/jobs/view/4011111111/"
      and any("Apply today" in s for s in SENT) and "pending_judge" not in st)
SENT.clear()
UPDATES[:] = [msg(54, jd)]
a.poll_commands(a.load_state(), 1)
check("a long message with nothing pending is left alone", not SENT)
UPDATES[:] = [msg(55, "have a look https://example.com/job/9 please")]
a.poll_commands(a.load_state(), 1)
check("a readable shared link is judged straight away", any("SRE at Example" in s for s in SENT))
saved_user = os.environ.pop("IMAP_USER")
check("the email check is hidden when email isn't set up", "Alert emails" not in a.selfcheck())
os.environ["IMAP_USER"] = saved_user

# --- /selfcheck -----------------------------------------------------------------------------------
a.mail_alerts.fetch = lambda done: real_fetch(done, imap_factory=FakeIMAP)
SENT.clear()
UPDATES[:] = [msg(40, "/selfcheck")]
a.poll_commands(a.load_state(), 1)
report = next((s for s in SENT if "🩺 Self-check" in s), "")
check("/selfcheck reports Claude, MCF and each board",
      "✅ Claude" in report and "✅ MyCareersFuture: 2 results" in report
      and "✅ Greenhouse" in report and "description 17 chars" in report and "✅ Ashby" in report)
check("/selfcheck marks an empty board as reachable, not failed", "⚪ Lever" in report and "no open roles" in report)
check("/selfcheck logs in to the mailbox", "✅ Alert emails: logged in, 1 alert email(s)" in report)
check("/selfcheck warns when the example resume/profile are loaded", "❌ Private files" in report
      and "EXAMPLE resume" in report)
check("/selfcheck checks storage and counts problems", "✅ Storage: writable" in report and "problem(s) above" in report)
real_urlopen = a.urllib.request.urlopen
a.urllib.request.urlopen = lambda req, *x, **k: fake_urlopen(req, *x, **k) if "telegram" in str(
    getattr(req, "full_url", req)) else (_ for _ in ()).throw(OSError("network down"))
report = a.selfcheck()
a.urllib.request.urlopen = real_urlopen
check("one failing dependency never hides the others",
      report.count("❌") >= 5 and "✅ Claude" in report and "network down" in report)
sys.argv = ["nas_agent.py", "--selfcheck"]
try:
    a.main()
    check("--selfcheck exits non-zero on problems", False)
except SystemExit as exc:
    check("--selfcheck exits non-zero on problems", exc.code == 1)
sys.argv = ["test"]
os.environ.pop("IMAP_USER")

# --- watchdog -------------------------------------------------------------------------------------
wd_state = TMP / "wd.json"
fresh = dt.datetime.now().isoformat(timespec="seconds")
stale = (dt.datetime.now() - dt.timedelta(hours=3)).isoformat(timespec="seconds")
wd_state.write_text(json.dumps({"heartbeat": fresh, "last_cycle": fresh}))
check("watchdog: healthy", watchdog.problem(wd_state, 2, 6) is None)
wd_state.write_text(json.dumps({"heartbeat": stale, "last_cycle": stale}))
check("watchdog: stale heartbeat", "3.0h ago" in (watchdog.problem(wd_state, 2, 6) or ""))
wd_state.write_text(json.dumps({"heartbeat": fresh, "last_cycle": "2000-01-01T00:00:00"}))
check("watchdog: alive but sweeps stuck", "last finished sweep" in (watchdog.problem(wd_state, 2, 6) or ""))
check("watchdog: missing file", "not found" in (watchdog.problem(TMP / "nope.json", 2, 6) or ""))

failed = [label for label, ok in CHECKS if not ok]
for label, ok in CHECKS:
    print(f"{'ok  ' if ok else 'FAIL'} {label}")
if failed:
    sys.exit(f"{len(failed)} of {len(CHECKS)} checks failed")
print(f"ok - {len(CHECKS)} checks pass")
