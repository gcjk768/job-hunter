"""Quick integrity checks for the sweep. Run before trusting a result.

    python build/selftest.py

Exists because the same bug bit twice: regexes written through a shell heredoc had their `\\b`
word-boundaries collapse into literal backspace characters (0x08), so PUBLIC_SECTOR silently matched
nothing and REMOTE_NO silently let US-only roles through. Both looked like working filters. A filter
that silently matches nothing is worse than no filter, so these assertions check behaviour, not
just that the file parses.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import job_sources
import weekly_sweep as sweep

BUILD = Path(__file__).resolve().parent
# Every C0 control character except tab/newline/carriage-return. Four separate escaping faults
# have reached shipped output in this project: a regex \\b collapsing to a literal BACKSPACE
# (twice), and `\\2192` being read as a Python *octal* escape — 0x11 followed by "92" — which put
# a tofu box in the label of every Apply button. Scanning the whole range costs nothing and
# catches the next variant. The generated HTML is scanned too: the source can be clean while the
# artefact is not.
CONTROL_CHARS = {chr(i): f"0x{i:02x}" for i in range(32) if i not in (9, 10, 13)}
SCANNED = ["build/*.py", "docs/reports/*.html"]

CASES = [
    # (label, callable -> bool, expected)
    ("COMPANY_DROP catches GovTech", lambda: bool(sweep.COMPANY_DROP.search("GovTech Singapore")), True),
    ("COMPANY_DROP catches defence", lambda: bool(sweep.COMPANY_DROP.search("ST Engineering")), True),
    # The personal list (my_profile.EXCLUDED_EMPLOYERS) must actually be wired in, not silently empty.
    ("COMPANY_DROP carries my_profile", lambda: bool(sweep.COMPANY_DROP.search(sweep.profile.EXCLUDED_SAMPLE)), True),
    ("COMPANY_DROP spares lookalikes", lambda: bool(sweep.COMPANY_DROP.search("Ellipsys Pte Ltd")), False),
    ("PUBLIC_SECTOR catches government", lambda: bool(sweep.PUBLIC_SECTOR.search("works with government agencies")), True),
    ("PUBLIC_SECTOR catches defence", lambda: bool(sweep.PUBLIC_SECTOR.search("a defence programme")), True),
    ("PUBLIC_SECTOR catches clearance", lambda: bool(sweep.PUBLIC_SECTOR.search("requires security clearance")), True),
    ("PUBLIC_SECTOR spares 'governmental studies'", lambda: bool(sweep.PUBLIC_SECTOR.search("governmental studies")), False),
    ("TITLE_KEEP catches SRE", lambda: bool(sweep.TITLE_KEEP.search("Site Reliability Engineer")), True),
    ("TITLE_KEEP catches MLOps", lambda: bool(sweep.TITLE_KEEP.search("MLOps Engineer")), True),
    ("TITLE_DROP catches fresh grad", lambda: bool(sweep.TITLE_DROP.search("DevOps Engineer (Fresh Graduates)")), True),
    ("remote: APAC accepted", lambda: job_sources._reachable("APAC (Remote)") == (True, True), True),
    ("remote: US rejected", lambda: job_sources._reachable("Remote - USA") == (False, False), True),
    ("remote: Singapore accepted", lambda: job_sources._reachable("Singapore") == (True, False), True),
]


def nas_watcher() -> str:
    """One stubbed NAS cycle: the model, Telegram, the sweep and the doc builder are fakes, so this
    runs offline in a temp dir. Returns "" when every behaviour holds, else what broke."""
    import datetime as dt
    import tempfile

    import nas_agent as a

    real_judge = a.judge
    tmp = Path(tempfile.mkdtemp())
    a.VAULT, a.STATE, a.SKIP_FILE = tmp / "vault", tmp / "state.json", tmp / "skip.txt"
    a.SKIP_FILE.write_text("10525204  # AWS SGP, rejected\n", encoding="utf-8")
    sent, judged = [], []
    a.telegram, a.build = (lambda text, keys=None, rich=False: sent.append(text)), (lambda r, v: tmp / "docs")
    a.TRACKER = tmp / "no-tracker.md"
    today, old = str(dt.date.today()), "2020-01-01"
    job = lambda i, t, c, posted=today: {"id": i, "title": t, "company": c, "url": f"u/{i}",  # noqa: E731
                                         "lo": None, "posted": posted, "desc": "x"}
    first = [job("1", "Senior SRE", "NVIDIA"), job("2", "Old Role", "Acme", old)]
    later = first + [job("3", "Senior SRE ", "nvidia"),                       # repost of 1
                     job("amazon.jobs/10525204", "Solutions Architect", "Amazon"),  # on skip list
                     job("5", "DevOps Engineer", "Beta Pte"),
                     # MyCareersFuture shape: "uuid", no "id" (this crashed the first NAS sweep in testing)
                     {"uuid": "mcf-1", "title": "Cloud Engineer", "company": "Delta Pte", "url": "u/mcf",
                      "lo": 9000, "hi": 12000, "posted": old, "source": "MyCareersFuture"}]
    a.judge = lambda r: judged.append(r["id"]) or {"suitable": True, "score": 80, "letter": ["x"],
                                                   "reason": "r", "gaps": "g"}
    state = {"seen": {}}
    a.candidates = lambda: first
    a.cycle(state)
    if judged != ["1"]:
        return f"first run should judge only the recent posting, judged {judged}"
    a.candidates = lambda: later
    a.cycle(state)
    if judged != ["1", "5"]:
        return f"repost/skip-list leaked into judging: {judged}"
    if state["seen"]["3"].get("dup_of") != "1" or not state["seen"]["amazon.jobs/10525204"].get("skip"):
        return "repost or skip not recorded"
    if sum("Tier-1" in m for m in sent) != 1:
        return f"expected exactly one tier-1 alert, got {len(sent)} messages"
    notes = sorted(p.name for p in (a.VAULT / "Jobs").iterdir())
    if len(notes) != 2 or not all(n.startswith(today) for n in notes):
        return f"vault notes wrong: {notes}"
    a.judge = lambda r: (_ for _ in ()).throw(RuntimeError("401 login expired"))
    a.candidates = lambda: later + [job(str(i), f"Role {i}", "Gamma") for i in range(10, 14)]
    a.cycle(state)
    if sum("failed in a row" in m for m in sent) != 1:
        return "no single failure alert after repeated judge errors"
    a.digest(state)
    if not (a.VAULT / "Daily" / f"{today}.md").exists():
        return "daily digest note missing"
    if a.dupe_key("Solutions Architect, AWS SGP", "Amazon / AWS") != \
            a.dupe_key("Solutions Architect, AWS SGP", "AMAZON WEB SERVICES SINGAPORE PRIVATE LIMITED"):
        return "cross-source repost (Amazon naming) not recognised"
    parts = a.chunks("\n".join(f"line {i} " + "x" * 50 for i in range(400)))
    if len(parts) < 2 or max(map(len, parts)) > 3900 or sum(p.count("line ") for p in parts) != 400:
        return "long digest not split cleanly for Telegram"
    # A judged posting gone from the boards for over a day: closed, note archived, out of the digest.
    state["seen"]["1"]["missing_since"] = 0
    a.candidates = lambda: [j for j in later if j.get("id") != "1"]
    a.judge = lambda r: {"suitable": False, "score": 10, "reason": "r", "gaps": "g"}
    a.cycle(state)
    if not state["seen"]["1"].get("closed") or not list((a.VAULT / "Archive").glob("*.md")):
        return "closed posting not archived"
    if "Senior SRE" in a.digest(state):
        return "closed posting still in the digest"
    # Tracker rows marked applied feed the skip list with no manual step.
    a.TRACKER = tmp / "tracker.md"
    a.TRACKER.write_text("| 2026-09-13 | AWS | Solutions Architect I (10535776) | amazon.jobs | **Applied 2026-09-13** |\n"
                         "| 2026-09-14 | Acme | SRE (99999999) | mcf | unsent |\n", encoding="utf-8")
    pats = a.skip_patterns()
    if "10535776" not in pats or "99999999" in pats:
        return f"tracker applied-ids not parsed right: {pats}"
    # A tapped "Applied" button closes the posting and archives its note.
    live_pid = "5"
    a.tg = lambda method, fields: {"result": [{"update_id": 7, "callback_query": {
        "id": "q", "data": f"a:{a.job_hash(live_pid)}", "message": {"chat": {"id": 1}, "message_id": 2}}}]} \
        if method == "getUpdates" else {}
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")  # poll_buttons is a no-op without a token
    a.poll_buttons(state)
    if state["seen"][live_pid].get("status") != "applied" or state.get("tg_offset") != 7:
        return "button tap not applied"
    # Two-stage judge: a non-fit never triggers the (expensive) documents call.
    calls = []
    a.ask_claude = lambda prompt: calls.append(prompt) or {"suitable": False, "score": 40}
    a.description = lambda r: "x"
    real_judge({"title": "T", "company": "C", "lo": None})
    if len(calls) != 1:
        return f"non-fit made {len(calls)} model calls"
    calls.clear()
    a.ask_claude = lambda prompt: calls.append(prompt) or ({"suitable": True, "score": 90} if len(calls) == 1
                                                          else {"letter": ["p"], "projects": []})
    if not real_judge({"title": "T", "company": "C", "lo": None}).get("letter") or len(calls) != 2:
        return "fit did not get its documents"
    # Pay estimate: median posted band of similar roles, only with enough samples, never for a paid posting.
    a.learn_bands([{"title": f"Senior SRE {i}", "lo": lo, "hi": lo + 3000} for i, lo in
                   enumerate((8000, 9000, 10000))] + [{"title": "Senior DevOps", "lo": 7000, "hi": 9000}])
    if "$9,000–12,000/mo" not in a.pay_of({"title": "Senior Site Reliability Engineer", "lo": None}):
        return f"pay estimate wrong: {a.pay_of({'title': 'Senior Site Reliability Engineer', 'lo': None})}"
    if "est." in a.pay_of({"title": "Senior DevOps Engineer", "lo": None}):
        return "estimated from a single band"
    # Interview prep lands in the note; the weekly roll-up is written and counts the week's fits.
    name = a.write_note({"company": "Acme", "title": "SRE", "url": "u", "lo": None},
                        {"score": 90, "interview": ["Q1 — A1"]}, True, False, None)
    if "Interview prep" not in (a.VAULT / "Jobs" / f"{name}.md").read_text(encoding="utf-8"):
        return "interview prep missing from note"
    if "fit" not in a.weekly(state) or not list((a.VAULT / "Weekly").glob("*.md")):
        return "weekly summary missing"
    return ""


CASES.append(("NAS watcher: baseline, reposts, skip list, alerts, vault", lambda: nas_watcher() == "", True))


def main() -> int:
    failures = []

    targets = []
    for pattern in SCANNED:
        targets += sorted(BUILD.parent.glob(pattern))
    for path in targets:
        text = path.read_text(encoding="utf-8", errors="replace")
        for char, name in CONTROL_CHARS.items():
            if char in text:
                failures.append(f"{path.name}: literal control char {name} x{text.count(char)}"
                                f" - an escape collapsed into a raw byte")

    for label, fn, expected in CASES:
        try:
            got = fn()
        except Exception as exc:  # a broken import should read as a failure, not a crash
            failures.append(f"{label}: raised {type(exc).__name__}")
            continue
        if got != expected:
            failures.append(f"{label}: expected {expected}, got {got}")

    if failures:
        print("FAILED:")
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print(f"ok - {len(CASES)} behaviour checks pass, {len(targets)} files clean of control chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
