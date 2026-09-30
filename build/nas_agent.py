"""Always-on job watcher for the NAS: sweep, judge fit with Ollama Cloud, draft documents, ping Telegram.

    python build/nas_agent.py          # loop forever (the container's entrypoint)
    python build/nas_agent.py --once   # one cycle, then exit

Between cycles it listens to the Telegram chat (TELEGRAM_CHAT_ID only) for commands:
    /status          health: last cycle, heartbeat, recent runs, errors (no LLM)
    /ask <question>  ask the model about the jobs it has seen, the drafts, or the system
    /judge <url> [pasted job text]
                     judge one posting on demand (MCF links are read via the API; for login-walled
                     boards like LinkedIn paste the description after the URL) and draft if it fits
    /sweep           run a cycle now
    /help

Reuses the sweep (weekly_sweep.collect + job_sources) and the document builder (build_docs), so the
filters in docs/vault/Target Criteria.md and the resume content in build/content.py stay the single
source of truth. Never applies anywhere: it drafts into tailored-auto/ and tells James.

It keeps its own state (build/.nas_state.json), separate from the PC sweep's, and writes nothing to
the vault — the NAS copy of the vault would drift from the OneDrive one.

The state file also carries a heartbeat, rewritten every HEARTBEAT_MIN minutes while idle, so an
outside watchdog (or the compose healthcheck) can tell "alive but nothing new" from "down".

Env: OLLAMA_BASE_URL, JOB_MODEL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_THREAD_ID, SWEEP_INTERVAL_HOURS,
     MAX_PER_CYCLE, FIT_THRESHOLD, HEARTBEAT_MIN.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
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
STATE = ROOT / "build" / ".nas_state.json"
OUT = ROOT / "tailored-auto"

OLLAMA = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL = os.environ.get("JOB_MODEL", "deepseek-v4.1-flash:cloud")
INTERVAL_H = float(os.environ.get("SWEEP_INTERVAL_HOURS", "6"))
# ponytail: caps cloud spend per cycle; the rest stay unseen and roll into the next cycle.
MAX_PER_CYCLE = int(os.environ.get("MAX_PER_CYCLE", "8"))
FIT_THRESHOLD = int(os.environ.get("FIT_THRESHOLD", "70"))
HEARTBEAT_MIN = float(os.environ.get("HEARTBEAT_MIN", "30"))
# A posting whose verdict keeps failing to parse is given up after this many cycles instead of
# eating a MAX_PER_CYCLE slot forever. Network errors (Ollama down) never count against it.
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
PERMANENT = (ValueError, KeyError, TypeError)  # json.JSONDecodeError is a ValueError
SEEN_DAYS = int(os.environ.get("SEEN_DAYS", "180"))  # forget postings older than this
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
  "employer": "the hiring company's name as the job text states it, else empty",
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


def _post(url: str, payload: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def description(rec: dict) -> str:
    """MCF job text; career-page sources have none, so the model judges on title + company."""
    if rec.get("source") != "MyCareersFuture":
        return "(not available — judge on title and company)"
    try:
        req = urllib.request.Request(f"{sweep.API}/{rec['uuid']}", headers=sweep.UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            html = json.load(r).get("description") or ""
    except Exception as exc:  # a missing JD must not stop the cycle
        return f"(description unavailable: {exc})"
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()[:6000]


def judge(rec: dict) -> dict:
    pay = "not published" if rec.get("lo") is None else f"${rec['lo']:,}-{rec.get('hi') or ''}"
    prompt = PROMPT.format(today=dt.date.today(), floor=sweep.SALARY_FLOOR,
                           excluded=sweep.profile.EXCLUDED_EMPLOYERS_TEXT, profile=profile(), title=rec["title"], company=rec["company"], pay=pay,
                           years=rec.get("years") or "n/s", desc=rec.get("desc") or description(rec),
                           projects=PROJECTS)
    # think=False: on reasoning models the thinking otherwise eats the reply and content comes back empty.
    resp = _post(f"{OLLAMA}/api/chat", {"model": MODEL, "stream": False, "format": "json", "think": False,
                                        "messages": [{"role": "user", "content": prompt}]})
    verdict = json.loads(resp["message"]["content"])
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


def is_fit(v: dict) -> bool:
    return bool(v.get("suitable")) and int(v.get("score") or 0) >= FIT_THRESHOLD and bool(v.get("letter"))


def alert_text(r: dict, v: dict, folder: Path) -> str:
    pay = "pay not published" if r.get("lo") is None else f"${r['lo']:,}–{r.get('hi') or '?'}/mo"
    return (f"🆕 {r['title']} — {r['company'].title()}\n{pay} · fit {v['score']}/100 · "
            f"coding-test risk {v.get('coding_test_risk', '?')}\n\nWhy: {v.get('reason')}\n"
            f"Gaps: {v.get('gaps')}\n\n{r.get('url', '')}\n\n"
            f"Resume + cover letter: NAS docker/job-hunter/tailored-auto/{folder.name}/\n"
            f"Nothing was submitted — your call.")


def telegram(text: str, thread: str | int | None = None) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("[TG] not configured:\n" + text)
        return
    fields = {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"}
    thread = thread or os.environ.get("TELEGRAM_THREAD_ID")
    if thread:  # a forum topic, e.g. t.me/c/<chat>/<topic>
        fields["message_thread_id"] = str(thread)
    data = urllib.parse.urlencode(fields).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30)


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def load_state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"seen": {}}


def save_state(state: dict) -> None:
    state["heartbeat"] = _now()
    tmp = STATE.with_suffix(".tmp")  # atomic replace: Syncthing and the healthcheck never see half a file
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def candidates() -> list[dict]:
    found = sweep.collect()
    for rec in job_sources.fetch_all(sweep.TITLE_KEEP):
        if not sweep.TITLE_DROP.search(rec["title"]) and not sweep.COMPANY_DROP.search(rec["company"]):
            found.setdefault(rec["id"], rec)
    # Below-floor bands are out; unknown pay and career pages go to the model.
    return [r for r in found.values() if r.get("lo") is None or r["lo"] >= sweep.SALARY_FLOOR]


def cycle() -> None:
    state = load_state()
    seen = state["seen"]
    prune(state)
    state["last_cycle_start"] = _now()
    save_state(state)
    jobs = candidates()
    ident = lambda r: r.get("uuid") or r["id"]  # noqa: E731
    if not seen:
        # First run: everything on the boards today is old news (the PC sweep already covered it).
        for r in jobs:
            seen[ident(r)] = {"t": r["title"], "c": r["company"], "fit": None, "at": _now()}
        _finish(state, {"candidates": len(jobs), "new": len(jobs), "judged": 0, "fits": 0, "failed": 0,
                        "note": "baseline"})
        telegram(f"Job watcher online on the NAS. Baseline: {len(jobs)} current postings; "
                 f"I'll message you when something new fits (checking every {INTERVAL_H:g}h).")
        return

    new = [r for r in jobs if ident(r) not in seen]
    print(f"[{dt.datetime.now():%F %T}] {len(jobs)} candidates, {len(new)} new")
    judged = fits = failed = 0
    last_fail = ""
    attempts = state.setdefault("attempts", {})
    for r in new[:MAX_PER_CYCLE]:
        try:
            v = judge(r)
        except Exception as exc:  # leave it unseen so the next cycle retries it
            failed += 1
            last_fail = f"{type(exc).__name__}: {exc}"
            print(f"  ! judge failed for {r['title']} @ {r['company']}: {exc}")
            if isinstance(exc, PERMANENT):  # the model's reply, not the network: count it
                key = ident(r)
                attempts[key] = attempts.get(key, 0) + 1
                if attempts[key] >= MAX_ATTEMPTS:
                    seen[key] = {"t": r["title"], "c": r["company"], "fit": None, "at": _now(),
                                 "gave_up": last_fail[:200]}
                    del attempts[key]
                    print(f"  ! gave up on {r['title']} @ {r['company']} after {MAX_ATTEMPTS} tries")
                save_state(state)
            continue
        judged += 1
        attempts.pop(ident(r), None)
        fit = is_fit(v)
        seen[ident(r)] = {"t": r["title"], "c": r["company"], "fit": v.get("score"), "suitable": bool(fit),
                          "why": v.get("reason"), "url": r.get("url", ""), "at": _now()}
        print(f"  {v.get('score'):>3} {'FIT ' if fit else 'skip'} {r['title']} @ {r['company']}")
        if fit:
            fits += 1
            telegram(alert_text(r, v, build(r, v)))
        save_state(state)
    _finish(state, {"candidates": len(jobs), "new": len(new), "judged": judged, "fits": fits,
                    "failed": failed, "note": last_fail[:300]})
    if failed and not judged:
        # Every judge call failed: usually trading-ollama is down or off the network. Say so instead of
        # silently retrying forever.
        telegram(f"⚠️ Job watcher: all {failed} judge calls failed ({last_fail[:200]}). "
                 f"Is trading-ollama running? Postings stay unseen and retry next cycle.")


def prune(state: dict) -> None:
    """Forget postings older than SEEN_DAYS so the state file (synced every heartbeat) stays small.
    Entries from before timestamps existed are stamped now and age out from here. A posting still
    live after SEEN_DAYS just gets judged once more."""
    cutoff = (dt.datetime.now() - dt.timedelta(days=SEEN_DAYS)).isoformat(timespec="seconds")
    seen = state["seen"]
    for key, v in list(seen.items()):
        v.setdefault("at", _now())
        if v["at"] < cutoff:
            del seen[key]


def _finish(state: dict, run: dict) -> None:
    """Every cycle leaves a record, even one with nothing new, so 'quiet' is visible as 'alive'."""
    run["at"] = _now()
    state["last_cycle"] = run["at"]
    state["runs"] = (state.get("runs") or [])[-29:] + [run]
    state.pop("last_error", None)
    save_state(state)


def record_error(text: str) -> None:
    try:
        state = load_state()
        state["last_error"] = {"at": _now(), "error": text[-1500:]}
        save_state(state)
    except Exception:
        traceback.print_exc()


# ---- Telegram commands -------------------------------------------------------------------------

HELP = ("Job watcher commands:\n"
        "/status — health, last cycle, recent runs\n"
        "/ask <question> — ask the model about seen jobs, drafts, or the system\n"
        "/judge <url> [pasted job text] — judge one posting now, draft if it fits\n"
        "/sweep — run a cycle now\n"
        "Nothing here ever applies anywhere.")
ASK_PROMPT = """You are the assistant inside James's self-hosted job watcher (a Docker container on his NAS).
Answer his question briefly and concretely, in plain text (no markdown tables), from the context below.
If the context does not contain the answer, say so rather than guessing. Never claim anything was applied to.

Today is {today}.

CANDIDATE:
{profile}

SYSTEM STATUS:
{status}

RECENTLY JUDGED POSTINGS (newest last; score, verdict, title @ company, reason, url):
{judged}

RECENT DRAFTS (fit.md of each):
{drafts}

QUESTION: {question}"""


def _age(stamp: str | None) -> str:
    if not stamp:
        return "never"
    try:
        h = (dt.datetime.now() - dt.datetime.fromisoformat(stamp)).total_seconds() / 3600
    except ValueError:
        return stamp
    return f"{h * 60:.0f}m ago" if h < 1 else f"{h:.1f}h ago"


def status_text(state: dict) -> str:
    runs = state.get("runs") or []
    lines = [f"Heartbeat: {_age(state.get('heartbeat'))}",
             f"Last cycle finished: {_age(state.get('last_cycle'))} (every {INTERVAL_H:g}h)",
             f"Model: {MODEL} via {OLLAMA} ({ollama_ok()})",
             f"Postings tracked: {len(state.get('seen', {}))}"]
    if state.get("last_error"):
        e = state["last_error"]
        lines.append(f"Last error {_age(e.get('at'))}: {e.get('error', '')[-400:]}")
    for r in runs[-5:]:
        lines.append(f"- {r.get('at', '?')[5:16]}: {r.get('candidates')} cand, {r.get('new')} new, "
                     f"{r.get('judged')} judged, {r.get('fits')} fit, {r.get('failed')} failed"
                     + (f" ({r['note']})" if r.get("note") else ""))
    return "\n".join(lines)


def ollama_ok() -> str:
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/version", timeout=10) as r:
            return "reachable, v" + json.load(r).get("version", "?")
    except Exception as exc:
        return f"UNREACHABLE: {exc}"


def ask(question: str) -> str:
    state = load_state()
    judged = [v for v in state.get("seen", {}).values() if v.get("fit") is not None][-60:]
    judged_txt = "\n".join(f"- {v.get('fit')} {'FIT' if v.get('suitable') else 'skip'} {v['t']} @ {v['c']}"
                            + (f" — {v['why']}" if v.get("why") else "") + (f" {v['url']}" if v.get("url") else "")
                            for v in judged) or "(none yet)"
    drafts = sorted(OUT.glob("*/fit.md"), key=lambda f: f.stat().st_mtime)[-8:] if OUT.exists() else []
    drafts_txt = "\n\n".join(f.read_text(encoding="utf-8")[:800] for f in drafts) or "(none yet)"
    prompt = ASK_PROMPT.format(today=dt.date.today(), profile=profile(), status=status_text(state),
                               judged=judged_txt, drafts=drafts_txt, question=question)
    resp = _post(f"{OLLAMA}/api/chat", {"model": MODEL, "stream": False, "think": False,
                                        "messages": [{"role": "user", "content": prompt}]})
    return (resp.get("message", {}).get("content") or "").strip() or "(the model returned nothing)"


MCF_UUID = re.compile(r"([0-9a-f]{32})")


def _page_text(url: str) -> tuple[str, str]:
    """(title, visible text) of a plain web page. Login-walled boards return little; paste instead."""
    req = urllib.request.Request(url, headers=sweep.UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read(2_000_000).decode("utf-8", "replace")
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    body = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
    return (re.sub(r"\s+", " ", m.group(1)).strip() if m else ""), text


def posting_from(arg: str) -> dict:
    """Turn '/judge <url> [pasted text]' into a record judge() understands."""
    url, _, pasted = arg.strip().partition(" ")
    if not url.startswith("http"):
        url, pasted = "", arg.strip()
    pasted = pasted.strip()
    m = MCF_UUID.search(url) if "mycareersfuture" in url else None
    if m:
        req = urllib.request.Request(f"{sweep.API}/{m.group(1)}", headers=sweep.UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            job = json.load(r)
        lo, hi = sweep.monthly_salary(job)
        return {"uuid": m.group(1), "id": m.group(1), "title": (job.get("title") or "").strip(),
                "company": sweep.company_of(job), "lo": lo, "hi": hi,
                "years": job.get("minimumYearsExperience"), "source": "MyCareersFuture",
                "url": (job.get("metadata") or {}).get("jobDetailsUrl") or url}
    title = ""
    if len(pasted) < 200 and url:
        title, pasted = _page_text(url)
    if len(pasted) < 200:
        raise ValueError("couldn't read enough of the posting — paste the job description after the URL")
    return {"id": url or "pasted:" + hashlib.sha1(pasted.encode()).hexdigest()[:12], "title": title or pasted.split(".")[0][:80],
            "company": "(see description)", "lo": None, "source": "link", "url": url, "desc": pasted[:6000]}


def judge_command(arg: str) -> str:
    rec = posting_from(arg)
    v = judge(rec)
    if rec["company"] == "(see description)" and v.get("employer"):
        rec["company"] = v["employer"]
    fit = is_fit(v)
    state = load_state()
    state["seen"][rec.get("uuid") or rec["id"]] = {"t": rec["title"], "c": rec["company"], "fit": v.get("score"),
                                                   "suitable": fit, "why": v.get("reason"),
                                                   "url": rec.get("url", ""), "at": _now(), "via": "judge"}
    save_state(state)
    if fit:
        return alert_text(rec, v, build(rec, v))
    return (f"❌ {rec['title']} — {rec['company']}\nfit {v.get('score')}/100 (threshold {FIT_THRESHOLD}) · "
            f"coding-test risk {v.get('coding_test_risk', '?')}\n\nWhy: {v.get('reason')}\nGaps: {v.get('gaps')}")


def poll_commands(state: dict, wait: int) -> bool:
    """Long-poll Telegram for up to `wait` seconds and handle commands. Returns True when /sweep asked for
    a cycle now. Only messages from TELEGRAM_CHAT_ID are answered; everyone else is ignored."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        time.sleep(wait)
        return False
    q = urllib.parse.urlencode({"timeout": wait, "offset": state.get("tg_offset", 0),
                                "allowed_updates": json.dumps(["message"])})
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates?{q}",
                                    timeout=wait + 15) as r:
            updates = json.load(r).get("result", [])
    except Exception as exc:  # 409 = another process is polling this bot token
        print(f"  ! getUpdates failed: {exc}")
        time.sleep(min(wait, 60))
        return False
    sweep_now = False
    for u in updates:
        # Persist first, so a crash on one message never replays it forever. Re-read rather than reuse
        # `state`: a previous command (/judge) may have written the file since.
        fresh = load_state()
        fresh["tg_offset"] = u["update_id"] + 1
        save_state(fresh)
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        if str(msg.get("chat", {}).get("id")) != str(chat) or not text.startswith("/"):
            continue
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()  # /ask@JobHunterBot in groups
        thread = msg.get("message_thread_id")
        try:
            if cmd in ("/help", "/start"):
                telegram(HELP, thread)
            elif cmd == "/status":
                telegram("🟢 " + status_text(load_state()), thread)
            elif cmd == "/ask":
                if not arg.strip():
                    telegram("Usage: /ask <question>, e.g. /ask which of this week's roles fit best?", thread)
                else:
                    telegram("🤔 " + ask(arg.strip()), thread)
            elif cmd == "/judge":
                if not arg.strip():
                    telegram("Usage: /judge <url> [pasted job description]", thread)
                else:
                    telegram("⏳ Reading and judging…", thread)
                    telegram(judge_command(arg), thread)
            elif cmd == "/sweep":
                telegram("Running a sweep now…", thread)
                sweep_now = True
        except Exception as exc:
            telegram(f"⚠️ {cmd} failed: {type(exc).__name__}: {exc}", thread)
    return sweep_now


def idle(seconds: float) -> None:
    """Wait for the next cycle while answering commands and refreshing the heartbeat."""
    end = time.time() + seconds
    beat = 0.0
    while (left := end - time.time()) > 0:
        state = load_state()
        if time.time() - beat >= HEARTBEAT_MIN * 60:
            save_state(state)
            beat = time.time()
        if poll_commands(state, int(min(50, max(1, left)))):
            return


def due_in(state: dict) -> float:
    """Seconds until the next cycle is due, from the last finished one (0 if overdue or never ran)."""
    try:
        last = dt.datetime.fromisoformat(state["last_cycle"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    return max(0.0, INTERVAL_H * 3600 - (dt.datetime.now() - last).total_seconds())


def main() -> None:
    once = "--once" in sys.argv
    # A container restart (NAS reboot, image update, crash) must not trigger a full paid sweep each
    # time; resume the schedule from the last finished cycle instead. /sweep still forces one.
    wait = 0.0 if once else due_in(load_state())
    if wait:
        print(f"[{dt.datetime.now():%F %T}] last cycle is recent; next one in {wait / 3600:.1f}h")
        idle(wait)
    while True:
        try:
            cycle()
        except Exception:
            traceback.print_exc()
            record_error(traceback.format_exc())
            try:
                telegram("⚠️ Job watcher cycle failed — see `docker logs job-hunter` or send /status.")
            except Exception:
                pass
        if once:
            return
        idle(INTERVAL_H * 3600)


if __name__ == "__main__":
    main()
