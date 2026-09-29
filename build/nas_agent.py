"""Always-on job watcher for the NAS: sweep hourly, judge fit with `claude -p`, draft documents, ping Telegram.

    python build/nas_agent.py            # loop forever (the container's entrypoint)
    python build/nas_agent.py --once     # one sweep, then exit
    python build/nas_agent.py --digest   # send today's digest now, then exit

Telegram: a digest of the day's sweeps every day at DIGEST_HOUR (18:00 SGT), even when nothing fits, so
silence always means the watcher is down. A suitable posting from a TIER1 employer is sent immediately.

Reuses the sweep (weekly_sweep.collect + job_sources) and the document builder (build_docs), so the
filters in docs/vault/Target Criteria.md and the resume content in build/content.py stay the single
source of truth. Never applies anywhere: it drafts into tailored-auto/ and tells James.

It keeps its own state (build/.nas_state.json), separate from the PC sweep's. Every judged posting also
becomes an Obsidian note under JOB_VAULT (Jobs/, filenames start "YYYY-MM-DD HHMM" so they sort by date
and time; Daily/ holds each digest). A role already seen under another posting id (MCF reposts, or the
same job on a board and a careers page) is recorded with dup_of and never judged or sent again; so is
anything matching skip.txt (already applied / rejected). The first run judges only the last
BASELINE_DAYS of postings. FAIL_ALERT judge failures in a row send one Telegram warning a day. When any
build/*.py changes on disk (Syncthing), the loop re-execs itself so new code takes effect.

Env: JOB_MODEL (claude CLI must be logged in), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
     TELEGRAM_THREAD_ID, SWEEP_INTERVAL_HOURS, MAX_PER_CYCLE, FIT_THRESHOLD, DIGEST_HOUR, TIER1, JOB_VAULT.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_docs
import job_sources
import weekly_sweep as sweep

ROOT = Path(__file__).resolve().parent.parent
# Docker reads env_file only when the container is created, so a plain restart would miss a new .env.
if (ROOT / ".env").exists():
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        key, sep, val = line.partition("=")
        if sep and not key.strip().startswith("#"):
            os.environ.setdefault(key.strip(), val.strip())
# The NAS mounts build/ read-only from the Syncthing mirror of the PC, so state lives outside it there.
STATE = Path(os.environ.get("JOB_STATE") or ROOT / "build" / ".nas_state.json")
OUT = ROOT / "tailored-auto"
# Notes go in a folder of the project vault, which Syncthing already mirrors to the PC's Obsidian.
VAULT = Path(os.environ.get("JOB_VAULT") or ROOT / "vault")
# Postings already applied to or rejected: one case-insensitive substring per line (posting id, URL
# fragment, or "title @ company"); '#' starts a comment. Matches are recorded as seen, never judged.
SKIP_FILE = ROOT / "build" / "skip.txt"  # in build/ so Syncthing carries PC edits to the NAS
BASELINE_DAYS = 3  # first run still judges postings this recent; older ones count as already seen
FAIL_ALERT = 3     # consecutive judge failures before an immediate Telegram warning (once a day)

MODEL = os.environ.get("JOB_MODEL", "sonnet")
INTERVAL_H = float(os.environ.get("SWEEP_INTERVAL_HOURS", "1"))
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "18"))  # local time; the container runs TZ=Asia/Singapore
# Tier 1 = FAANG + MANGOES (big tech and the frontier AI labs): suitable postings are sent at once.
TIER1 = re.compile(os.environ.get("TIER1") or r"\b(meta|facebook|apple|amazon|aws|netflix|google|alphabet|"
                   r"microsoft|nvidia|openai|anthropic)\b", re.I)
# ponytail: caps cloud spend per cycle; the rest stay unseen and roll into the next cycle.
MAX_PER_CYCLE = int(os.environ.get("MAX_PER_CYCLE", "8"))
FIT_THRESHOLD = int(os.environ.get("FIT_THRESHOLD", "70"))
MASTER = build_docs.MASTER
PROJECTS = [p["name"] for p in MASTER["projects"]]

PROMPT = """You screen job postings for one candidate and, when one fits, write his tailored application.

Today is {today}.

CANDIDATE (current role and resume):
{profile}

HARD FILTERS (any failure means suitable=false):
- Base pay SGD {floor:,}/month or more (judge the bottom of a posted band; unknown pay is allowed).
- No government, public-sector or defence work, including SI roles staffed onto such accounts.
- Not {excluded}.
- Role shape: Platform / DevOps / SRE / DevSecOps / Cloud infrastructure / AI infrastructure / solutions architect.
  Not a generic "Software Engineer". A LeetCode-style coding screen is a flag, not a failure.

JOB:
Title: {title}
Company: {company}
Pay: {pay}
Min years: {years}
Description:
{desc}

Return ONLY a JSON object:
{{"suitable": bool, "score": 0-100 fit to the candidate's current role, "reason": "one sentence",
  "gaps": "the honest gaps, one sentence", "coding_test_risk": "low|medium|high",
  "tagline": "resume tagline for this role, pipe-separated like the candidate's",
  "summary": "tailored professional summary, 90-130 words, first person implied, only true claims from the resume",
  "projects": [3-4 project names chosen ONLY from {projects}, most relevant first],
  "letter": ["4-6 cover-letter paragraphs: why this role, matching evidence against the posting's stated requirements, the honest gaps, close. No greeting or sign-off."]}}
If suitable is false, leave tagline, summary, letter empty and projects [].
Never invent employers, numbers, certifications or years that are not in the resume."""


def profile() -> str:
    job = MASTER["experience"][0]
    projects = "\n".join(f"- {p['name']}: {p['text']}" for p in MASTER["projects"])
    skills = "\n".join(f"- {k}: {v}" for k, v in MASTER["skills"])
    return (f"{MASTER['tagline']}\n{MASTER['summary']}\n\nCurrent: {job['title']}, {job['company']} "
            f"({job['dates']})\n" + "\n".join(f"- {b}" for b in job["bullets"])
            + f"\n\nSkills:\n{skills}\n\nProjects:\n{projects}\n\nCertifications: "
            + "; ".join(MASTER["certifications"]))


def ask_claude(prompt: str) -> dict:
    """One-shot `claude -p` with no tools; returns the JSON object in the reply."""
    run = subprocess.run([shutil.which("claude") or "claude", "-p", "--output-format", "json", "--model", MODEL, "--tools", "",
                          "--no-session-persistence"], input=prompt, capture_output=True, text=True,
                         encoding="utf-8", timeout=600)
    try:
        out = json.loads(run.stdout)
    except ValueError:
        raise RuntimeError(f"claude -p exit {run.returncode}: {(run.stderr or run.stdout).strip()[:300]}")
    if out.get("is_error"):
        raise RuntimeError(f"claude -p failed: {out.get('result')}")
    text = out["result"]
    return json.loads(text[text.find("{"):text.rfind("}") + 1])  # tolerate ```json fences


def description(rec: dict) -> str:
    """Job text for the judge: inline from the career-page list call, the Workday detail endpoint,
    or the MCF job API. Only Apple (and a failed fetch) leaves the model judging on title + company."""
    html = rec.get("desc") or ""
    try:
        if not html and rec.get("jd_api"):
            with urllib.request.urlopen(urllib.request.Request(rec["jd_api"], headers=job_sources.UA),
                                        timeout=30) as r:
                html = (json.load(r).get("jobPostingInfo") or {}).get("jobDescription") or ""
        elif not html and rec.get("source") == "MyCareersFuture":
            req = urllib.request.Request(f"{sweep.API}/{rec['uuid']}", headers=sweep.UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                html = json.load(r).get("description") or ""
    except Exception as exc:  # a missing JD must not stop the cycle
        return f"(description unavailable: {exc})"
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()[:6000]
    return text or "(not available — judge on title and company)"


def judge(rec: dict) -> dict:
    pay = "not published" if rec.get("lo") is None else f"${rec['lo']:,}-{rec.get('hi') or ''}"
    prompt = PROMPT.format(today=dt.date.today(), floor=sweep.SALARY_FLOOR,
                           excluded=sweep.profile.EXCLUDED_EMPLOYERS_TEXT, profile=profile(), title=rec["title"], company=rec["company"], pay=pay,
                           years=rec.get("years") or "n/s", desc=description(rec), projects=PROJECTS)
    verdict = ask_claude(prompt)
    # The model only gets to pick from the real project library; anything else is dropped.
    picks = [p for p in verdict.get("projects") or [] if p in PROJECTS]
    verdict["projects"] = picks or PROJECTS[:4]
    return verdict


def build(rec: dict, v: dict) -> Path:
    key = build_docs._slug(f"{dt.date.today():%Y%m%d}_{rec['company']}_{rec['title']}")[:90]
    outdir = OUT / key
    outdir.mkdir(parents=True, exist_ok=True)
    spec = dict(MASTER, tagline=v["tagline"] or MASTER["tagline"], summary=v["summary"] or MASTER["summary"])
    by_name = {p["name"]: p for p in MASTER["projects"]}
    spec["projects"] = [by_name[n] for n in v["projects"]]
    co = build_docs._slug(rec["company"].title())
    build_docs.build_resume(spec, str(outdir / f"James_Koh_Resume_{co}.docx"))
    build_docs.build_cover_letter({"company": rec["company"].title(), "role": rec["title"],
                                   "letter": v["letter"]},
                                  str(outdir / f"James_Koh_Cover_Letter_{co}.docx"))
    (outdir / "fit.md").write_text(
        f"# {rec['title']} — {rec['company']}\n\n{rec.get('url', '')}\n\n"
        f"Score {v.get('score')} · coding-test risk {v.get('coding_test_risk')}\n\n"
        f"Why: {v.get('reason')}\n\nGaps: {v.get('gaps')}\n", encoding="utf-8")
    return outdir


def telegram(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("[TG] not configured:\n" + text)
        return
    for part in chunks(text):  # Telegram caps a message at 4096 chars; long digests go out in parts
        fields = {"chat_id": chat, "text": part, "disable_web_page_preview": "true"}
        if os.environ.get("TELEGRAM_THREAD_ID"):  # a forum topic, e.g. t.me/c/<chat>/<topic>
            fields["message_thread_id"] = os.environ["TELEGRAM_THREAD_ID"]
        data = urllib.parse.urlencode(fields).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30)


def chunks(text: str, limit: int = 3900) -> list[str]:
    """Split on line boundaries so no entry is cut mid-way; a single over-long line is hard-cut."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            out, cur, line = out + ([cur] if cur else []) + [line[:limit]], "", line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out, cur = out + [cur], ""
        cur = f"{cur}\n{line}" if cur else line
    return out + ([cur] if cur else [])


def candidates() -> list[dict]:
    found = sweep.collect()
    for rec in job_sources.fetch_all(sweep.TITLE_KEEP):
        if not sweep.TITLE_DROP.search(rec["title"]) and not sweep.COMPANY_DROP.search(rec["company"]):
            found.setdefault(rec["id"], rec)
    # Below-floor bands are out; unknown pay and career pages go to the model.
    return [r for r in found.values() if r.get("lo") is None or r["lo"] >= sweep.SALARY_FLOOR]


def dupe_key(title: str, company: str) -> str:
    """Same role under a new posting id: compare title + employer, ignoring case and punctuation. The
    employer is its first distinctive word, because sources name it differently ("Amazon / AWS" on
    amazon.jobs, "AMAZON WEB SERVICES SINGAPORE PRIVATE LIMITED" on MyCareersFuture)."""
    words = [w for w in re.split(r"[^a-z0-9]+", company.lower()) if w and w not in GENERIC_CO]
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip() + " | " + (words[0] if words else "")


GENERIC_CO = {"the", "singapore", "sg", "asia", "pacific", "apac", "global", "international", "group"}


def _safe(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|#^\[\]]+', "", text).strip()[:70]


def write_note(r: dict, v: dict, fit: bool, tier1: bool, folder: str | None) -> str:
    """One Obsidian note per judged posting; the timestamp prefix keeps vault/Jobs sorted by date and time."""
    now = dt.datetime.now()
    name = f"{now:%Y-%m-%d %H%M} {_safe(r['company'].title())} — {_safe(r['title'])}"
    path = VAULT / "Jobs" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    status = "fit" if fit else "skip"
    path.write_text(
        f"---\ntags: [job, {status}{', tier1' if tier1 else ''}]\ndate: {now:%Y-%m-%d}\ntime: \"{now:%H:%M}\"\n"
        f"company: \"{r['company'].title()}\"\ntitle: \"{r['title']}\"\nscore: {v.get('score')}\n"
        f"pay: \"{pay_of(r)}\"\ncoding_test_risk: {v.get('coding_test_risk', '?')}\nurl: {r.get('url', '')}\n---\n\n"
        f"# {r['title']} — {r['company'].title()}\n\n**{status.upper()}** · fit {v.get('score')}/100\n\n"
        f"**Why:** {v.get('reason')}\n\n**Gaps:** {v.get('gaps')}\n\n"
        + (f"**Docs:** `tailored-auto/{folder}/`\n\n" if folder else "")
        + f"[Posting]({r.get('url', '')}) · [[{now:%Y-%m-%d}]]\n", encoding="utf-8")
    moc = VAULT / "Job Watcher.md"  # not Home.md: the project vault already has one
    if not moc.exists():
        moc.write_text("---\ntags: [active]\n---\n\n# Job Watcher\n\nWritten by the NAS watcher "
                       "([[NAS Job Watcher]]).\n\n- `Jobs/` — one note per judged posting, sorted by date "
                       "and time\n- `Daily/` — the 18:00 digest for each day\n", encoding="utf-8")
    return name


def skip_patterns() -> list[str]:
    if not SKIP_FILE.exists():
        return []
    lines = (ln.split("#", 1)[0].strip().lower() for ln in SKIP_FILE.read_text(encoding="utf-8").splitlines())
    return [ln for ln in lines if ln]


def skipped(r: dict, patterns: list[str]) -> bool:
    hay = f"{r.get('uuid') or ''} {r['id']} {r.get('url') or ''} {r['title']} @ {r['company']}".lower()
    return any(p in hay for p in patterns)


def baseline(state: dict, jobs: list[dict]) -> int:
    """Mark postings older than BASELINE_DAYS as seen; returns how many recent ones stay unseen.
    Undated postings (NVIDIA's "Posted 23 Days Ago", Lever, Apple) count as old."""
    cutoff = str(dt.date.today() - dt.timedelta(days=BASELINE_DAYS))
    recent = 0
    for r in jobs:
        posted = str(r.get("posted") or "")[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", posted) and posted >= cutoff:
            recent += 1
        else:
            state["seen"][r.get("uuid") or r["id"]] = {"t": r["title"], "c": r["company"], "fit": None}
    save(state)
    return recent


CLOSE_AFTER_H = 24  # a judged posting missing this long is closed; a flaky source sweep is not enough


def close_gone(state: dict, live: set[str]) -> None:
    """Postings that left the boards drop out of digests, and their notes move to Archive/ (kept, not
    deleted). The grace period stops one failed source fetch from closing everything it carries."""
    now = time.time()
    for pid, e in state["seen"].items():
        if e.get("closed") or e.get("fit") is None:
            continue
        if pid in live:
            e.pop("missing_since", None)
            continue
        if now - e.setdefault("missing_since", now) < CLOSE_AFTER_H * 3600:
            continue
        e["closed"] = str(dt.date.today())
        note = VAULT / "Jobs" / f"{e.get('note')}.md"
        if e.get("note") and note.exists():
            (VAULT / "Archive").mkdir(parents=True, exist_ok=True)
            note.replace(VAULT / "Archive" / note.name)


def pay_of(r: dict) -> str:
    return "pay not published" if r.get("lo") is None else f"${r['lo']:,}–{r.get('hi') or '?'}/mo"


def load() -> dict:
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    state.setdefault("seen", {})
    return state


def save(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def today_log(state: dict) -> dict:
    """Per-day counters for the digest; reset when the date rolls over."""
    day = str(dt.date.today())
    if state.get("day", {}).get("date") != day:
        state["day"] = {"date": day, "sweeps": 0, "candidates": 0, "failed": 0, "error": ""}
    return state["day"]


def cycle(state: dict) -> None:
    seen, log = state["seen"], today_log(state)
    jobs = candidates()
    log["sweeps"] += 1
    log["candidates"] = len(jobs)
    ident = lambda r: r.get("uuid") or r["id"]  # noqa: E731
    close_gone(state, {ident(r) for r in jobs})
    if not seen and not state.get("baselined"):
        state["baselined"] = True
        # First run: older postings count as already seen so it does not flood; the last few days are
        # still judged (a blanket baseline once hid everything since the previous PC sweep).
        recent = baseline(state, jobs)
        telegram(f"Job watcher online on the NAS. Baseline: {len(jobs) - recent} older postings skipped, "
                 f"{recent} from the last {BASELINE_DAYS} days queued for judging; sweeping every "
                 f"{INTERVAL_H:g}h, digest daily at {DIGEST_HOUR}:00, tier-1 employers straight away.")

    skips = skip_patterns()
    known = {dupe_key(e["t"], e["c"]): i for i, e in seen.items()}
    new = []
    for r in (r for r in jobs if ident(r) not in seen):
        k = dupe_key(r["title"], r["company"])
        if skipped(r, skips):  # already applied / rejected
            seen[ident(r)] = {"t": r["title"], "c": r["company"], "fit": None, "skip": True}
        elif k in known:  # a repost of a role already judged: remember the id, never judge or send it again
            seen[ident(r)] = {"t": r["title"], "c": r["company"], "fit": None, "dup_of": known[k],
                              "url": r.get("url", "")}
        else:
            known[k] = ident(r)
            new.append(r)
    print(f"[{dt.datetime.now():%F %T}] {len(jobs)} candidates, {len(new)} new")
    # Tier-1 first, so the cap never pushes a big-name posting to a later sweep.
    new.sort(key=lambda r: not TIER1.search(r["company"]))
    for r in new[:MAX_PER_CYCLE]:
        try:
            v = judge(r)
        except Exception as exc:  # leave it unseen so the next cycle retries it
            print(f"  ! judge failed for {r['title']} @ {r['company']}: {exc}")
            log["failed"] += 1
            log["error"] = str(exc)[:300]
            state["fail_streak"] = state.get("fail_streak", 0) + 1
            if state["fail_streak"] >= FAIL_ALERT and state.get("fail_alert_on") != str(dt.date.today()):
                state["fail_alert_on"] = str(dt.date.today())
                telegram(f"⚠️ Job watcher: {state['fail_streak']} judge calls failed in a row, so new postings "
                         f"(tier-1 included) are not being scored. Last error: {log['error']}\n"
                         f"If it says login/auth: Docker → job-hunter → Terminal → `claude` → /login.")
            save(state)
            continue
        state["fail_streak"] = 0
        suitable = bool(v.get("suitable"))
        fit = suitable and int(v.get("score") or 0) >= FIT_THRESHOLD and bool(v.get("letter"))
        tier1 = bool(TIER1.search(r["company"]))
        folder = build(r, v).name if fit else None
        note = write_note(r, v, fit, tier1, folder)
        seen[ident(r)] = {"t": r["title"], "c": r["company"], "fit": v.get("score"), "suitable": fit,
                          "tier1": tier1, "d": str(dt.date.today()), "url": r.get("url", ""),
                          "pay": pay_of(r), "why": v.get("reason"), "gaps": v.get("gaps"), "dir": folder,
                          "note": note}
        print(f"  {v.get('score'):>3} {'FIT ' if fit else 'skip'}{' T1' if tier1 else ''} "
              f"{r['title']} @ {r['company']}")
        if tier1 and suitable:
            docs = (f"Resume + cover letter: NAS docker/job-hunter/tailored-auto/{folder}/" if folder
                    else f"Below the {FIT_THRESHOLD} fit bar, so no documents drafted.")
            telegram(f"⭐ Tier-1: {r['title']} — {r['company'].title()}\n{pay_of(r)} · fit {v.get('score')}/100 · "
                     f"coding-test risk {v.get('coding_test_risk', '?')}\n\nWhy: {v.get('reason')}\n"
                     f"Gaps: {v.get('gaps')}\n\n{r.get('url', '')}\n\n{docs}\nNothing was submitted — your call.")
        save(state)


def digest(state: dict) -> str:
    log, day, skips = today_log(state), str(dt.date.today()), skip_patterns()
    # Only postings still on the boards, and never ones already applied to / rejected.
    judged = sorted((e for e in state["seen"].values() if e.get("d") == day and not e.get("closed")
                     and not any(p in f"{e.get('url', '')} {e['t']} @ {e['c']}".lower() for p in skips)),
                    key=lambda e: -(e.get("fit") or 0))
    shown: set[str] = set()  # a role judged twice before dedupe existed is listed once, best score
    judged = [e for e in judged if not (dupe_key(e["t"], e["c"]) in shown or shown.add(dupe_key(e["t"], e["c"])))]
    fits =[e for e in judged if e.get("suitable")]
    lines = [f"📋 Job digest {day} — {log['sweeps']} sweeps, {log['candidates']} postings on the boards, "
             f"{len(judged)} new judged, {len(fits)} fit."]
    if log["error"]:
        lines.append(f"⚠️ {log['failed']} judge failures (retried next sweep). Last error: {log['error']}")
    for e in fits:
        lines.append(f"\n✅ {'⭐ ' if e.get('tier1') else ''}{e['t']} — {e['c'].title()}\n{e['pay']} · "
                     f"fit {e['fit']}/100\nWhy: {e['why']}\nGaps: {e['gaps']}\n{e['url']}\n"
                     f"Docs: tailored-auto/{e['dir']}/")
    rest = [e for e in judged if not e.get("suitable")]
    if rest:
        lines.append("\nNot a fit:")
        lines += [f"· {e.get('fit')} {e['t']} — {e['c'].title()}" for e in rest]
    if not judged:
        lines.append("Nothing new today.")
    text = "\n".join(lines)
    daily = VAULT / "Daily" / f"{day}.md"
    daily.parent.mkdir(parents=True, exist_ok=True)
    daily.write_text(f"---\ntags: [digest]\ndate: {day}\n---\n\n{text}\n\n## Notes\n"
                     + "".join(f"- [[{e['note']}]]\n" for e in judged if e.get("note")), encoding="utf-8")
    return text


def main() -> None:
    if "--digest" in sys.argv:
        telegram(digest(load()))
        return
    once = "--once" in sys.argv
    code = lambda: {f: f.stat().st_mtime for f in Path(__file__).parent.glob("*.py")}  # noqa: E731
    started = code()
    while True:
        if code() != started:  # Syncthing delivered new code: restart in place so it takes effect
            print("code changed on disk, reloading", flush=True)
            os.execv(sys.executable, [sys.executable, "-u", *sys.argv])
        state = load()
        if time.time() - state.get("last_sweep", 0) >= INTERVAL_H * 3600:
            state["last_sweep"] = time.time()
            try:
                cycle(state)
            except Exception as exc:  # reported in the digest instead of a separate alert
                traceback.print_exc()
                today_log(state)["error"] = f"sweep crashed: {exc}"[:300]
            save(state)
            if once:
                return
        today = str(dt.date.today())
        if dt.datetime.now().hour >= DIGEST_HOUR and state.get("digest_on") != today:
            try:
                telegram(digest(state))
                state["digest_on"] = today
                save(state)
            except Exception:
                traceback.print_exc()  # retried on the next tick
        time.sleep(600)


if __name__ == "__main__":
    main()
