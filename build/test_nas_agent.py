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
import re
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
HEAL_REPLY = {"diagnosis": "The board API timed out twice.", "cause": "transient",
              "actions": ["none"], "user_steps": "", "patch": ""}  # benign unless a test says otherwise
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
    if "getUpdates" in url:  # offset=-1 (skip the backlog) returns only the newest and confirms the rest
        out, UPDATES[:] = (UPDATES[-1:] if "offset=-1" in url else list(UPDATES)), []
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
    """Stands in for the claude CLI: --version, a schema'd judge call, or a plain /jobask."""
    CALLS.append(cmd)
    if cmd[1:] == ["--version"]:
        return subprocess.CompletedProcess(cmd, 0, "2.1.0 (Claude Code)", "")
    assert "--tools" in cmd and cmd[cmd.index("--tools") + 1] == "", "tools must be disabled"
    assert cwd and not os.listdir(cwd), "must run in an empty directory"
    out = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01}
    if "--json-schema" in cmd:
        schema = cmd[cmd.index("--json-schema") + 1]
        got = (EXTRACTED if '"jobs"' in schema else FACTCHECK if '"issues"' in schema
               else HEAL_REPLY if '"diagnosis"' in schema else VERDICT)
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
UPDATES[:] = [msg(1, "/jobstatus"), msg(2, "/jobsweep"), msg(3, "/jobask old question")]
check("a first start with no stored offset skips the backlog",
      "tg_offset" not in a.load_state() and a.poll_commands(a.load_state(), 1) is False and not SENT
      and a.load_state()["tg_offset"] == 4)
UPDATES[:] = [msg(5, "/jobstatus", thread=7), msg(6, "/jobstatus", chat=99), msg(7, "/jobask@JobBot which fits best?"),
              msg(8, "/jobjudge https://www.mycareersfuture.gov.sg/job/x-" + "a" * 32),
              msg(9, "/jobjudge https://example.com/job/1"), msg(10, "/jobsweep")]
check("/jobsweep asks for a cycle", a.poll_commands(a.load_state(), 1) is True)
check("update offset persisted", a.load_state()["tg_offset"] == 11)
check("/jobstatus answers in the asking topic", "message_thread_id=7" in SENT[0] and "Heartbeat" in SENT[0])
check("/jobstatus shows the claude CLI version", "2.1.0 (Claude Code)" in SENT[0])
check("strangers are ignored", sum("Heartbeat" in s for s in SENT) == 1)
check("/jobask returns the model's answer", any("Role X fits best." in s for s in SENT))
check("/jobjudge reads MCF via the API", any("Platform Engineer" in s and "$9,000" in s for s in SENT))
check("/jobjudge reads a plain page", any("SRE at Example" in s for s in SENT))
check("/jobjudge records the posting as seen", "a" * 32 in a.load_state()["seen"])

# Topic filter: the group is shared by ~10 bots, so only the job topic (TELEGRAM_THREAD_ID) is ours.
os.environ["TELEGRAM_THREAD_ID"] = "2574"
SENT.clear()
UPDATES[:] = [msg(30, "/jobask which fits best?", thread=2574), msg(31, "/ask which fits best?", thread=2763),
              msg(32, "/jobask which fits best?", thread=2763), msg(33, "/status", thread=3038),
              msg(34, "/jobstatus"), msg(35, "https://example.com/job/1", thread=2763)]
a.poll_commands(a.load_state(), 1)
check("/jobask in the job topic is answered there",
      len(SENT) == 1 and "Role X fits best." in SENT[0] and "message_thread_id=2574" in SENT[0])
check("/ask, /jobask and /status in other topics (or none) are ignored, links too", len(SENT) == 1)
SENT.clear()
UPDATES[:] = [msg(40, "/status", thread=2574), msg(41, "/ask which fits best?", thread=2574),
              msg(42, "/help", thread=2574)]
a.poll_commands(a.load_state(), 1)
check("old names still work as aliases inside the job topic",
      len(SENT) == 3 and "Heartbeat" in SENT[0] and "Role X fits best." in SENT[1] and "COMMANDS</b>" in SENT[2])
check("/jobhelp lists only /job* commands", all(c in a.HELP for c in a.COMMANDS if c != "/jobhelp")
      and not re.search(r"(?<![\w/])/(status|ask|help|pipeline|applied|selfcheck)\b", a.HELP))
os.environ.pop("TELEGRAM_THREAD_ID")
try:
    a.posting_from("https://example.com/job/1 short")
    check("/jobjudge with short pasted text falls back to the page", True)
except ValueError:
    check("/jobjudge with short pasted text falls back to the page", False)

# --- alerts: urgency, outreach, refs --------------------------------------------------------------
SENT.clear()
a.candidates = lambda: [rec("j10")]
a.cycle()
alert = next((s for s in SENT if "SRE j10" in s), "")
check("score >= URGENT_SCORE is flagged 'apply today'", "🔥 <b>APPLY TODAY</b>" in alert)
check("alert carries who to reach, a people-search link and the note",
      "Reach out to:</b> Head of Platform" in alert and "linkedin.com/search/results/people" in alert
      and "Checked note." in alert)
ref10 = a.load_state()["seen"]["j10"]["ref"]
check("fits get a ref number in the alert", f"/jobapplied {ref10}" in alert)
check("the fact-check's corrections reach the alert", "1 claim corrected" in alert and "Checked note." in alert)
check("pay reads as a clean range", a.pay_text({"lo": 11000, "hi": 14000}) == "$11,000–14,000/mo"
      and a.pay_text({"lo": 9500}) == "from $9,500/mo" and a.pay_text({"lo": None}) == "pay not published")
check("ages never go negative and switch to days", a._age((dt.datetime.now() + dt.timedelta(hours=3)).isoformat())
      == "just now" and a._age((dt.datetime.now() - dt.timedelta(days=5)).isoformat()) == "5d ago")
check("/jobstatus shows the interval in force now", f"every {a.interval_now():g}h now" in a.status_text({}))

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
                                         years="", desc={"desc": "Own our fleet"}.get("desc"), projects=[], memory=""))
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
UPDATES[:] = [msg(20, f"/jobapplied {ref10}"), msg(21, "/jobapplied Staff SRE at Initech"), msg(22, "/jobpipeline"),
              msg(23, f"/jobinterview #{ref10}")]
a.poll_commands(a.load_state(), 1)
st = a.load_state()
e10 = st["seen"]["j10"]
check("/jobapplied records status and date", e10["status"] == "interview" and e10.get("applied_at"))
check("/jobapplied with free text tracks a manual entry",
      any(e.get("source") == "manual" and e["c"] == "Initech" for e in st["seen"].values()))
check("/jobpipeline lists tracked applications", any("Initech" in s and f"#{ref10}" in s for s in SENT))
check("/jobinterview replies with a prep pack", any("📚 <b>PREP PACK</b>" in s and "LIKELY QUESTIONS" in s for s in SENT))
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
check("the weekly digest is sent once per week", sum("<b>WEEKLY DIGEST</b>" in s for s in SENT) == 1)
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
check("/jobstatus shows the deployed commit", "@" in a.code_version() or "copied files" in a.code_version())
check("a failing follow-up draft still nudges, once, without retrying every loop",
      sum("⏰" in s for s in SENT) == 1 and any("couldn't draft one" in s for s in SENT))

# --- sweep cadence --------------------------------------------------------------------------------
check("one hourly interval at any hour of any day (no quiet hours)",
      a.interval_now(monday10) == a.interval_now(monday10.replace(hour=3)) == a.interval_now(sunday10)
      == a.INTERVAL_H == 1.0)

# --- model fallback -------------------------------------------------------------------------------
CALLS = []
a.subprocess.run = lambda cmd, **kw: (CALLS.append(cmd[cmd.index("--model") + 1]),
                                      subprocess.CompletedProcess(cmd, 1, "", "overloaded") if CALLS[-1] == a.MODEL
                                      else subprocess.CompletedProcess(cmd, 0, json.dumps(
                                          {"subtype": "success", "result": "fine"}), ""))[1]
check("a failed opus call is retried once on the fallback model",
      a.claude("hi") == "fine" and CALLS == [a.MODEL, a.FALLBACK_MODEL] and a.MODEL == "opus" and a.FALLBACK_MODEL == "sonnet")
CALLS.clear()
try:
    a.claude("hi", model=a.EXTRACT_MODEL)
    extract_ok = True
except a.ClaudeError:
    extract_ok = False
check("explicit cheap-model calls never fall back", extract_ok and CALLS == [a.EXTRACT_MODEL])
a.subprocess.run = fake_run

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
check("email salaries are parsed to monthly SGD", any("$10,000–14,000/mo" in s for s in SENT))
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
      and any("APPLY TODAY" in s for s in SENT) and "pending_judge" not in st)
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

# --- /jobselfcheck -----------------------------------------------------------------------------------
a.mail_alerts.fetch = lambda done: real_fetch(done, imap_factory=FakeIMAP)
SENT.clear()
UPDATES[:] = [msg(40, "/jobselfcheck")]
a.poll_commands(a.load_state(), 1)
report = next((s for s in SENT if "🩺 <b>SELF-CHECK</b>" in s), "")
check("/jobselfcheck reports Claude, MCF and each board",
      "✅ <b>Claude</b>" in report and "✅ <b>MyCareersFuture</b> · 2 results" in report
      and "✅ <b>Greenhouse" in report and "description 17 chars" in report and "✅ <b>Ashby" in report)
check("/jobselfcheck marks an empty board as reachable, not failed", "⚪ <b>Lever" in report and "no open roles" in report)
check("/jobselfcheck logs in to the mailbox", "✅ <b>Alert emails</b> · logged in, 1 alert email(s)" in report)
check("/jobselfcheck warns when the example resume/profile are loaded", "❌ <b>Private files</b>" in report
      and "EXAMPLE resume" in report)
check("/jobselfcheck checks storage and counts problems", "✅ <b>Storage</b> · writable" in report and "problem(s) above" in report)
real_urlopen = a.urllib.request.urlopen
a.urllib.request.urlopen = lambda req, *x, **k: fake_urlopen(req, *x, **k) if "telegram" in str(
    getattr(req, "full_url", req)) else (_ for _ in ()).throw(OSError("network down"))
report = a.selfcheck()
a.urllib.request.urlopen = real_urlopen
check("one failing dependency never hides the others",
      report.count("❌") >= 5 and "✅ <b>Claude</b>" in report and "network down" in report)
sys.argv = ["nas_agent.py", "--selfcheck"]
try:
    a.main()
    check("--selfcheck exits non-zero on problems", False)
except SystemExit as exc:
    check("--selfcheck exits non-zero on problems", exc.code == 1)
sys.argv = ["test"]
os.environ.pop("IMAP_USER")

# --- one bad reply only costs that board / page ------------------------------------------------
import http.client  # noqa: E402

real_get = job_sources._get
boards = []


def flaky_get(url, data=None):
    boards.append(url)
    if len(boards) == 1:
        raise ValueError("Expecting value: line 1 column 1 (char 0)")  # a non-JSON reply
    if len(boards) == 2:
        raise http.client.RemoteDisconnected("Remote end closed connection")
    return {"jobs": [{"id": 1, "title": "Site Reliability Engineer", "location": {"name": "Singapore"},
                      "absolute_url": f"https://x/{len(boards)}"}]}


job_sources._get = flaky_get
got = job_sources.greenhouse(a.sweep.TITLE_KEEP, log=lambda *x: None)
job_sources._get = real_get
check("a garbled or dropped Greenhouse reply skips only that board",
      len(boards) == len(job_sources.GREENHOUSE) and len(got) == len(job_sources.GREENHOUSE) - 2)
real_fetch_mcf = a.sweep.fetch
a.sweep.fetch = lambda search, page: (_ for _ in ()).throw(http.client.RemoteDisconnected("closed"))
try:
    check("a dropped MyCareersFuture connection doesn't crash the sweep", a.sweep.collect() == {})
except Exception:
    check("a dropped MyCareersFuture connection doesn't crash the sweep", False)
a.sweep.fetch = real_fetch_mcf

# --- staying alive -------------------------------------------------------------------------------
EXITS: list[int] = []
SLEEPS: list[float] = []
a.EXIT = EXITS.append
a.SLEEP = SLEEPS.append
real_state = a.STATE
iso = Path(tempfile.mkdtemp())
a.STATE = iso / "build" / ".nas_state.json"
a.STATE.parent.mkdir()
(a.STATE.parent / "backups").mkdir()
(a.STATE.parent / "backups" / "nas_state-2026-09-01.json").write_text('{"seen": {"old": {"t": "x"}}}')
(a.STATE.parent / "backups" / "nas_state-2026-09-02.json").write_text('{"seen": {"good": {"t": "y"}}, "next_ref": 4}')
(a.STATE.parent / "backups" / "nas_state-2026-09-03.json").write_text('{"seen": {"trunc')
a.STATE.write_text('{"seen": {"half-writ')
SENT.clear()
st = a.load_state()
check("an unreadable state file is restored from the newest backup that parses",
      "good" in st["seen"] and st["restored"]["from"] == "backup nas_state-2026-09-02.json"
      and any("🩹 The state file couldn't be read" in s for s in SENT)
      and list(a.STATE.parent.glob(".nas_state.broken-*.json")))
check("the restored state is saved, so the next load is clean", "restored" in json.loads(a.STATE.read_text()))
for b in (a.STATE.parent / "backups").glob("*.json"):
    b.unlink()
a.STATE.write_text("garbage")
st = a.load_state()
check("with no usable backup it starts fresh instead of crashing", st["seen"] == {} and "fresh" in st["restored"]["from"])
a.STATE.write_text('["not", "a", "state"]')
check("valid JSON of the wrong shape is treated as broken too", a.load_state()["seen"] == {})

saved_state_path = a.STATE
a.STATE = Path("/nonexistent-dir/state.json")
try:
    a.save_state({"seen": {}})
    check("a failed save (disk full, read-only) is logged, not raised", True)
except OSError:
    check("a failed save (disk full, read-only) is logged, not raised", False)
a.STATE = saved_state_path

a.PROGRESS["t"] = a.time.time()
check("no restart while the loop is making progress", not a.stall_check() and not EXITS)
a.PROGRESS["t"] = a.time.time() - (a.STALL_MIN + 5) * 60
SENT.clear()
check("a stalled loop restarts the process", a.stall_check() and EXITS == [3]
      and any("restarting itself" in s for s in SENT) and a.load_state()["stalled"]["minutes"] >= a.STALL_MIN)
EXITS.clear()
a.PROGRESS["t"] = a.time.time()

HEALS: list[tuple] = []
real_heal = a.heal
a.heal = lambda trigger, error, **k: HEALS.append((trigger, error)) or True
a.startup_checks()
check("the first start after a stall asks for a diagnosis", HEALS and HEALS[-1][0] == "restarted after a stall")
st = a.load_state()
st["starts"] = [a._now()] * 5
a.save_state(st)
HEALS.clear()
SLEEPS.clear()
a.startup_checks()
check("a crash loop backs off 10 minutes and is diagnosed", SLEEPS == [600] and HEALS[0][0].startswith("crash loop"))

SLEEPS.clear()
HEALS.clear()
check("guarded() turns an exception into a recorded error and a heal",
      a.guarded(lambda: 1 / 0) is False and "ZeroDivisionError" in a.load_state()["last_error"]["error"]
      and HEALS and SLEEPS == [60])


class Stop(BaseException):
    pass


calls = {"cycle": 0, "idle": 0}
real = (a.cycle, a.idle, a.start_watchdog, a.startup_checks)


def bad_cycle():
    calls["cycle"] += 1
    raise RuntimeError("boom")


def bad_idle(seconds):
    calls["idle"] += 1
    if calls["idle"] >= 3:
        raise Stop()
    raise OSError("idle broke")


a.cycle, a.idle, a.start_watchdog, a.startup_checks = bad_cycle, bad_idle, lambda: None, lambda: None
sys.argv = ["nas_agent.py"]
st = a.load_state()
st.pop("last_cycle", None)
a.save_state(st)
try:
    a.main()
except Stop:
    pass
a.cycle, a.idle, a.start_watchdog, a.startup_checks = real
check("the main loop survives failing sweeps and a failing idle loop", calls == {"cycle": 3, "idle": 3})
a.heal = real_heal

# --- self-repair ----------------------------------------------------------------------------------
st = a.load_state()
st.update(attempts={"p1": 2, "p2": 1}, heals=[])
a.save_state(st)
(a.STATE.parent / "backups").mkdir(exist_ok=True)
SENT.clear()
CALLS.clear()
HEAL_REPLY.update(actions=["clear_retry_counters", "wait_and_retry", "pause_mail_24h"],
                  patch="--- a/build/x.py\n+++ b/build/x.py\n@@ -1 +1 @@\n-a\n+b")
err = 'Traceback (most recent call last):\n  File "/app/build/nas_agent.py", line 30, in cycle\nTimeoutError: timed out'
check("heal() reports a diagnosis", a.heal("sweep raised", err) and any("🩹 <b>SELF-HEAL</b> · sweep raised" in s
                                                                     and "board API timed out" in s for s in SENT))
st = a.load_state()
check("heal() applies the chosen safe remedies", st["attempts"] == {} and a.backoff_left(st) > 3000
      and a._hours_since(st["mail_paused_until"]) < -23)
patches = list((a.STATE.parent / "patches").glob("*.diff"))
check("a proposed code fix is saved, not applied", len(patches) == 1 and any("NOT applied" in s for s in SENT))
check("the same failure isn't diagnosed twice in 6h", a.heal("sweep raised", err) is False)
check("…unless forced (/jobheal)", a.heal("sweep raised", err, force=True) is True)
os.environ["IMAP_USER"] = "me@gmail.com"
check("a paused mail reader reads nothing", a.mail_jobs(a.load_state()) == [])
os.environ.pop("IMAP_USER")

SENT.clear()
CALLS.clear()
check("a login failure gets fixed guidance without asking Claude",
      a.heal("every judge call in a sweep failed", "ClaudeError: Invalid API key · Please run /login")
      and any("claude setup-token" in s for s in SENT) and not CALLS)

HEAL_REPLY.update(actions=["restart_process"], patch="")
check("restart_process exits so Docker restarts it", a.heal("idle raised", "x = broken state 1", force=True)
      and EXITS == [3])
EXITS.clear()
st = a.load_state()
st["heals"] = [{"sig": str(i), "at": a._now()} for i in range(a.HEAL_MAX_PER_DAY)]
a.save_state(st)
check("self-heal is capped per day", a.heal("new failure", "something else", force=True) is False)
st["heals"] = []
a.save_state(st)

(a.STATE.parent / "backups" / "nas_state-2026-09-05.json").write_text('{"seen": {"from-backup": {"t": "z"}}}')
check("restore_state_from_backup swaps in the backup and keeps the old file",
      a.apply_remedy("restore_state_from_backup") and "from-backup" in a.load_state()["seen"]
      and list(a.STATE.parent.glob(".nas_state.replaced-*.json")))
UPDATES[:] = [msg(700, "old queued message")]
check("reset_telegram_offset skips the queue", a.apply_remedy("reset_telegram_offset")
      and a.load_state()["tg_offset"] == 701)
UPDATES[:] = []
st = a.load_state()
st["attempts"] = {"poison": 2}
a.save_state(st)
a.apply_remedy("skip_failing_postings")
check("skip_failing_postings marks them seen", a.load_state()["seen"]["poison"]["gave_up"] == "skipped by self-heal")
check("the code around the failure is sent to the diagnosis",
      "def " in a._code_context(f'File "{Path(a.__file__).resolve()}", line 150, in claude') or
      "line 150" in a._code_context(f'File "{Path(a.__file__).resolve()}", line 150, in claude'))

st = a.load_state()
st["last_error"] = {"at": a._now(), "error": "TimeoutError: board timed out"}
st["heals"] = []
a.save_state(st)
HEAL_REPLY.update(actions=["none"])
SENT.clear()
UPDATES[:] = [msg(800, "/jobheal")]
a.poll_commands(a.load_state(), 1)
check("/jobheal diagnoses the last recorded error", any("🩹 <b>SELF-HEAL</b> · manual /jobheal" in s for s in SENT))
real_tg = a.telegram
a.telegram = lambda *x, **k: (_ for _ in ()).throw(OSError("telegram down"))
try:
    check("a Telegram outage during self-heal is logged, not raised", a.heal("x", "y failed", force=True) is True)
except OSError:
    check("a Telegram outage during self-heal is logged, not raised", False)
a.telegram = real_tg
a.STATE = real_state
sys.argv = ["test"]

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

# --- Telegram HTML: escaping, block-safe chunks, plain-text fallback --------------------------------

import urllib.error  # noqa: E402

import tg  # noqa: E402

check("every message goes out as HTML with link previews off",
      SENT and all("parse_mode=HTML" in s and "disable_web_page_preview=true" in s for s in SENT))
check("the watchdog uses the same send path", watchdog.telegram is tg.telegram is a.telegram)
hostile = {"id": "h1", "title": "R&D <SRE>", "company": "A&B <Asia>", "source": "email:linkedin",
           "url": "https://x.io/job?a=1&b=<2>", "lo": 9000, "hi": 12000}
bad_v = dict(VERDICT, score=90, reason="<script>alert(1)</script> & co", gaps="none <yet>",
             outreach="Hi <b>there</b> & thanks", contact_titles=["Head of <Infra>"], factcheck=[])
card = a.alert_text(hostile, bad_v, TMP / "R&D_<x>", 7)
check("dynamic text in an alert is escaped (title, employer, LLM text, URL, folder)",
      "<SRE>" not in card and "<script>" not in card and "<Asia>" not in card and "<b>there</b>" not in card
      and "R&amp;D &lt;SRE&gt;" in card and "&lt;script&gt;" in card and "a=1&amp;b=&lt;2&gt;" in card
      and "R&amp;D_&lt;x&gt;" in card)
check("plain() unescapes and keeps link targets", "Open posting (https://x.io/job?a=1&b=<2>)" in tg.plain(card)
      and "R&D <SRE>" in tg.plain(card))
TAGS = ("b", "i", "a", "code", "blockquote")
parts = tg.chunks("\n\n".join([card] * 40))
check("long text splits into parts under the limit",
      len(parts) > 1 and max(map(len, parts)) <= tg.LIMIT and sum(p.count("APPLY TODAY") for p in parts) == 40)
check("chunks never split inside a tag",
      all(len(re.findall(f"<{t}[ >]", p)) == p.count(f"</{t}>") for p in parts for t in TAGS))
check("a quote never contains a blank line, so it stays one block",
      "\n\n" not in tg.quote("x", "a\n\n\nb") and tg.chunks(tg.quote("x", "a\n\nb")) == [tg.quote("x", "a\nb")])
TRIED: list[dict] = []


def rejecting_urlopen(url, data=None, timeout=0):
    f = dict(urllib.parse.parse_qsl(data.decode()))
    TRIED.append(f)
    if "parse_mode" in f:
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(
            b'{"ok":false,"error_code":400,"description":"Bad Request: can\'t parse entities: unclosed tag"}'))
    return Resp(b"{}")


a.urllib.request.urlopen = rejecting_urlopen
tg.telegram(card, 2574)
check("rejected HTML is resent as plain text in the same topic",
      len(TRIED) == 2 and TRIED[0]["parse_mode"] == "HTML" and "parse_mode" not in TRIED[1]
      and "<b>APPLY" not in TRIED[1]["text"] and "R&D <SRE>" in TRIED[1]["text"] and TRIED[1]["message_thread_id"] == "2574")
a.urllib.request.urlopen = lambda url, data=None, timeout=0: (_ for _ in ()).throw(
    urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b"{}")))
try:
    tg.telegram("x")
    check("other Telegram errors still raise", False)
except urllib.error.HTTPError:
    check("other Telegram errors still raise", True)
a.urllib.request.urlopen = fake_urlopen
SENT.clear()
a.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, json.dumps(
    {"subtype": "success", "is_error": False, "result": "Use <kubectl> & helm"}), "")
UPDATES[:] = [msg(900, "/jobask what <tools>?")]
a.poll_commands(a.load_state(), 1)
a.subprocess.run = fake_run
check("/jobask escapes the model's answer and the question",
      any("Use &lt;kubectl&gt; &amp; helm" in s and "what &lt;tools&gt;?" in s for s in SENT))
a.urllib.request.urlopen = lambda req, *x, **k: Resp(json.dumps(
    {"jobPostingInfo": {"jobDescription": "<p>Run &amp; scale K8s</p>"}}).encode())
check("Workday postings fetch their description", a.description({"jd_workday": "https://w/job"}) == "Run & scale K8s")
a.urllib.request.urlopen = fake_urlopen

# --- vault: movement log + memory -------------------------------------------------------------------
import vault  # noqa: E402

VAULT = TMP / "vault"
os.environ["VAULT_DIR"] = str(VAULT)
at = dt.datetime(2026, 10, 2, 14, 3, tzinfo=vault.SGT)
vault.log("🔔", "fit alert", "85/100 · ref #3", vault.job_link("ACME PTE. LTD.", "SRE"), now=at)
day = (VAULT / "Activity" / "2026" / "10" / "2026-10-02.md").read_text(encoding="utf-8")
check("Activity line is '- HH:MM emoji **what** · detail · [[entity]]'",
      day.splitlines()[-1] == "- 14:03 🔔 **fit alert** · 85/100 · ref #3 · [[Jobs/Acme Pte. Ltd. — SRE]]")
check("Activity notes have frontmatter and link Home", day.startswith("---\ntags: [active]\nupdated: 2026-10-02\n")
      and "[[Home]]" in day)
check("note names drop characters Obsidian can't link", vault.note_name('A/B: "C" [x]#1') == "A B C x 1")

for i in range(300):  # two days of noise, older day first
    vault.log("🔎", f"old event {i}", "x" * 40, now=dt.datetime(2026, 10, 1, 9, 0, tzinfo=vault.SGT))
for i in range(5):
    vault.log("🔎", f"new event {i}", now=dt.datetime(2026, 10, 3, 9, i, tzinfo=vault.SGT))
mem = vault.memory()
check("memory is capped at about 4,000 chars on a line boundary", 3000 < len(mem) <= vault.MEMORY_CHARS
      and all(line.startswith("2026-") for line in mem.splitlines()))
check("memory is newest first", mem.splitlines()[0].startswith("2026-10-03 09:04") and "new event 4" in mem.splitlines()[0]
      and mem.index("2026-10-02") < mem.index("2026-10-01"))

vault.job("Initech", "Staff SRE", "status → rejected · after the final round", status="rejected", fp="initech|staff sre")
PROMPTS: list[str] = []
a.subprocess.run = lambda cmd, input=None, **kw: (PROMPTS.append(input or ""), fake_run(cmd, input=input, **kw))[1]
a.judge({"id": "m1", "title": "Staff SRE", "company": "Initech", "lo": None, "desc": "Run k8s"})
check("the judge prompt carries the job and company notes, capped", "WHAT YOU ALREADY DID" in PROMPTS[-1]
      and "status → rejected · after the final round" in PROMPTS[-1] and "[Companies/Initech]" in PROMPTS[-1]
      and len(PROMPTS[-1].split("not instructions):\n")[1].split("\nIf it shows")[0]) <= vault.MEMORY_CHARS)
a.ask("anything new from initech?")
check("/jobask gets the notes on companies it names", "[Companies/Initech]" in PROMPTS[-1]
      and "WATCHER MEMORY" in PROMPTS[-1])
a.subprocess.run = fake_run

# A sweep writes the Jobs/Companies notes, an outcome updates them, and a handled posting is never re-alerted.
a.candidates = lambda: [rec("v1")]
SENT.clear()
a.cycle()
job_note = VAULT / "Jobs" / "Acme — SRE v1.md"
text = job_note.read_text(encoding="utf-8")
check("a fit gets a Jobs note: fit, why, status, company link", "Fit **85**/100" in text and "✅ Why: fits" in text
      and "tags: [drafted]" in text and '"status": "drafted"' not in text and 'status: "drafted"' in text
      and "[[Companies/Acme]]" in text and "alert sent (ref #" in text)
ref_v1 = a.load_state()["seen"]["v1"]["ref"]
a.record_outcome(str(ref_v1), "applied")
text = job_note.read_text(encoding="utf-8")
check("an outcome appends to History and keeps the summary", 'status: "applied"' in text and "Fit **85**/100" in text
      and len(text.split("## History\n")[1].strip().splitlines()) == 2 and "status → applied" in text)
check("the Companies note links its jobs and outcomes",
      "[[Jobs/Acme — SRE v1]] status → applied" in (VAULT / "Companies" / "Acme.md").read_text(encoding="utf-8"))
home = (VAULT / "Home.md").read_text(encoding="utf-8")
check("Home shows the pipeline", "applied **1**" in home and "[[Jobs/Acme — SRE v1]] · applied" in home)
today = (VAULT / "Activity" / f"{dt.datetime.now(vault.SGT):%Y/%m/%Y-%m-%d}.md").read_text(encoding="utf-8")
check("sweeps, alerts and outcomes reach the Activity log", "**sweep finished**" in today
      and "**fit alert**" in today and "**/jobapplied**" in today)
check("Home links the month folder and dated notes", f"## Latest activity — {dt.datetime.now(vault.SGT):%Y-%m}" in home
      and "[[Activity/2026/10/2026-10-02]]" in home)
(VAULT / "Activity" / "2026-09-30.md").write_text("---\ntags: [active]\n---\n- 09:00 🔎 **old**\n", encoding="utf-8")
(VAULT / "Activity" / "2026-10-02.md").write_text("flat duplicate\n", encoding="utf-8")
check("migrate moves flat notes into YYYY/MM and never overwrites", vault.migrate() == 1
      and (VAULT / "Activity" / "2026" / "09" / "2026-09-30.md").exists()
      and not (VAULT / "Activity" / "2026-09-30.md").exists()
      and (VAULT / "Activity" / "2026-10-02.md").exists()
      and vault._days(VAULT)[-1].name == "2026-09-30.md"  # memory() reads it last (oldest), still in reach
      and "2026-09-30 09:00 🔎 **old**" in vault.memory(max_chars=10 ** 6))

st = a.load_state()  # the state forgot the posting (pruned, restored, new id on a repost): the vault remembers
st["seen"].pop("v1"), st["fps"].clear()
a.save_state(st)
a.candidates = lambda: [dict(rec("v1-repost"), title="SRE v1")]
SENT.clear()
judged_before = sum("--json-schema" in c for c in CALLS)
a.cycle()
check("a posting the vault shows as handled is not judged or alerted again",
      not SENT and sum("--json-schema" in c for c in CALLS) == judged_before
      and a.load_state()["seen"]["v1-repost"].get("handled") == "applied")

os.environ["VAULT_DIR"] = str(job_note)  # a file, not a folder: every vault call fails
try:
    vault.log("x", "y")
    vault.job("A", "B", "c", "d", status="drafted")
    quiet = (vault.memory(["Jobs/x"]) == "" and vault.statuses() == {} and vault.mentioned("acme") == [])
    a.candidates = lambda: [rec("v2")]
    SENT.clear()
    a.cycle()
    check("vault errors never raise and never cost an alert", quiet and any("SRE v2" in s for s in SENT))
except Exception as exc:
    check(f"vault errors never raise ({exc!r})", False)
os.environ.pop("VAULT_DIR")
check("with VAULT_DIR unset the vault is off", vault.memory() == "" and vault.job("A", "B", "c") is None)

failed = [label for label, ok in CHECKS if not ok]
for label, ok in CHECKS:
    print(f"{'ok  ' if ok else 'FAIL'} {label}")
if failed:
    sys.exit(f"{len(failed)} of {len(CHECKS)} checks failed")
print(f"ok - {len(CHECKS)} checks pass")
