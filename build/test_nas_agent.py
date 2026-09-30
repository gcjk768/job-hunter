"""Offline tests for the NAS loop, the Telegram commands and the watchdog. No network: Telegram,
Ollama and MyCareersFuture are replaced by a fake urlopen.

    python build/test_nas_agent.py
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
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
           "tagline": "", "summary": "", "projects": [], "letter": ["p1", "p2"], "employer": "Acme"}


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
    if "/api/version" in url:
        return Resp(b'{"version":"0.9"}')
    if "/api/chat" in url:
        body = json.loads(req.data)
        if "format" in body:  # judge
            return Resp(json.dumps({"message": {"content": json.dumps(VERDICT)}}).encode())
        return Resp(b'{"message":{"content":"Role X fits best."}}')
    if "mycareersfuture" in url and url.rstrip("/").endswith("a" * 32):
        return Resp(json.dumps({"title": "Platform Engineer", "hiringCompany": {"name": "Acme"},
                                "salary": {"minimum": 9000, "maximum": 12000}, "description": "<p>k8s</p>",
                                "metadata": {"jobDetailsUrl": "https://mcf/job"}}).encode())
    if url.startswith("https://example.com"):
        return Resp(b"<html><title>SRE at Example</title><body>" + b"Run Kubernetes. " * 30 + b"</body></html>")
    raise AssertionError(f"unexpected URL {url}")


a.urllib.request.urlopen = fake_urlopen
a.build = lambda rec, v: TMP / "draft"  # document rendering has its own checks
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

real_judge = a.judge
a.candidates = lambda: [rec("j3")]
a.judge = lambda r: (_ for _ in ()).throw(ConnectionError("refused"))
SENT.clear()
for _ in range(a.MAX_ATTEMPTS + 1):
    a.cycle()
check("network failures warn on Telegram", any("judge calls failed" in s for s in SENT))
check("network failures never give up", "j3" not in a.load_state()["seen"])

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
