"""Always-on job watcher for the NAS: sweep, judge fit with Claude (`claude -p`), draft documents, ping Telegram.

    python build/nas_agent.py          # loop forever (the container's entrypoint)
    python build/nas_agent.py --once   # one cycle, then exit
    python build/nas_agent.py --selfcheck   # check every live dependency, print the report, exit

Between cycles it listens to the Telegram chat (TELEGRAM_CHAT_ID only) for commands:
    /status          health: last cycle, heartbeat, recent runs, errors (no LLM)
    /ask <question>  ask the model about the jobs it has seen, the drafts, or the system
    /judge <url> [pasted job text]
                     judge one posting on demand (MCF links are read via the API; for login-walled
                     boards like LinkedIn paste the description after the URL) and draft if it fits
    /sweep           run a cycle now
    /applied|/interview|/offer|/rejected|/ghosted <ref or free text>
                     record an outcome; /interview also writes an interview prep pack
    /pipeline        every tracked application and its status
    /selfcheck       try every live dependency (Claude, boards, mail, storage) and report ✅/❌
    /help

It also reads job-alert emails (LinkedIn, JobStreet, Glassdoor, Indeed) from your inbox when IMAP_* is
set (mail_alerts.py), sweeps more often during working hours, drafts a LinkedIn outreach note for each
fit, reminds you to follow up FOLLOWUP_DAYS after /applied, and posts a weekly digest.

Reuses the sweep (weekly_sweep.collect + job_sources) and the document builder (build_docs), so the
filters in docs/vault/Target Criteria.md and the resume content in build/content.py stay the single
source of truth. Never applies anywhere: it drafts into tailored-auto/ and tells James.

It keeps its own state (build/.nas_state.json), separate from the PC sweep's, and writes nothing to
the vault — the NAS copy of the vault would drift from the OneDrive one.

The state file also carries a heartbeat, rewritten every HEARTBEAT_MIN minutes while idle, so an
outside watchdog (or the compose healthcheck) can tell "alive but nothing new" from "down".

The model is called through the Claude Code CLI in print mode (`claude -p`), with every tool disabled,
no MCP servers, no settings files and an empty working directory: it can only read the prompt and
answer. Auth comes from CLAUDE_CODE_OAUTH_TOKEN (`claude setup-token`, uses the subscription) or
ANTHROPIC_API_KEY. The verdict is enforced with --json-schema, so there is no JSON to hand-parse.

Env: CLAUDE_BIN, JOB_MODEL, CLAUDE_TIMEOUT, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_THREAD_ID, SWEEP_INTERVAL_HOURS,
     BUSY_INTERVAL_HOURS, BUSY_HOURS, MAX_PER_CYCLE, FIT_THRESHOLD, URGENT_SCORE, HEARTBEAT_MIN,
     FOLLOWUP_DAYS, DIGEST_WEEKDAY, EXTRACT_MODEL, IMAP_* (see mail_alerts.py).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_docs
import job_sources
import mail_alerts
import weekly_sweep as sweep

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "build" / ".nas_state.json"
OUT = ROOT / "tailored-auto"

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
MODEL = os.environ.get("JOB_MODEL", "sonnet")  # any `claude --model` value: sonnet, opus, haiku or a full id
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))
INTERVAL_H = float(os.environ.get("SWEEP_INTERVAL_HOURS", "6"))
# Applying early matters, so sweep faster while recruiters are working (local time, Mon-Fri).
BUSY_INTERVAL_H = float(os.environ.get("BUSY_INTERVAL_HOURS", "2"))
BUSY_HOURS = tuple(int(h) for h in os.environ.get("BUSY_HOURS", "8-20").split("-"))
URGENT_SCORE = int(os.environ.get("URGENT_SCORE", "85"))  # alerts at or above this say "apply today"
FOLLOWUP_DAYS = int(os.environ.get("FOLLOWUP_DAYS", "7"))
DIGEST_WEEKDAY = int(os.environ.get("DIGEST_WEEKDAY", "6"))  # Monday=0 … Sunday=6, sent after 09:00
EXTRACT_MODEL = os.environ.get("EXTRACT_MODEL", "haiku")  # reading alert emails is simple; keep it cheap
FACTCHECK = os.environ.get("FACTCHECK", "1") != "0"  # second pass: every claim in a draft must be in the resume
BACKUP_DAYS = int(os.environ.get("BACKUP_DAYS", "30"))  # daily copies of the state file kept in build/backups
OUTCOMES = {"/applied": "applied", "/interview": "interview", "/offer": "offer",
            "/rejected": "rejected", "/ghosted": "ghosted"}
# ponytail: caps cloud spend per cycle; the rest stay unseen and roll into the next cycle.
MAX_PER_CYCLE = int(os.environ.get("MAX_PER_CYCLE", "8"))
FIT_THRESHOLD = int(os.environ.get("FIT_THRESHOLD", "70"))
HEARTBEAT_MIN = float(os.environ.get("HEARTBEAT_MIN", "30"))
# A posting whose verdict keeps failing to parse is given up after this many cycles instead of
# eating a MAX_PER_CYCLE slot forever. CLI, auth and network errors never count against it.
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

Answer with these fields:
{{"suitable": bool, "score": 0-100 fit to the candidate's current role, "reason": "one sentence",
  "gaps": "the honest gaps, one sentence", "coding_test_risk": "low|medium|high",
  "tagline": "resume tagline for this role, pipe-separated like the candidate's",
  "summary": "tailored professional summary, 90-130 words, first person implied, only true claims from the resume",
  "projects": [3-4 project names chosen ONLY from {projects}, most relevant first],
  "employer": "the hiring company's name as the job text states it, else empty",
  "contact_titles": [1-3 job titles of the people worth messaging directly about this role, most likely
                     hiring manager first, e.g. "Head of Platform Engineering"; a recruiter named in the
                     posting may be listed by name],
  "outreach": "a LinkedIn connection note to that person, at most 280 characters: the role, one concrete
               matching proof point from the resume, a polite ask. No flattery, no emojis",
  "letter": ["4-6 cover-letter paragraphs: why this role, matching evidence against the posting's stated requirements, the honest gaps, close. No greeting or sign-off."]}}
If suitable is false, leave tagline, summary, letter, outreach empty and projects, contact_titles [].
Never invent employers, numbers, certifications or years that are not in the resume."""


def profile() -> str:
    job = MASTER["experience"][0]
    projects = "\n".join(f"- {p['name']}: {p['text']}" for p in MASTER["projects"])
    skills = "\n".join(f"- {k}: {v}" for k, v in MASTER["skills"])
    return (f"{MASTER['tagline']}\n{MASTER['summary']}\n\nCurrent: {job['title']}, {job['company']} "
            f"({job['dates']})\n" + "\n".join(f"- {b}" for b in job["bullets"])
            + f"\n\nSkills:\n{skills}\n\nProjects:\n{projects}\n\nCertifications: "
            + "; ".join(MASTER["certifications"]))


class ClaudeError(RuntimeError):
    """The CLI failed (not installed, not signed in, rate-limited, offline). Transient: retried next cycle."""


COST = {"usd": 0.0}  # running total for the current cycle; reset by cycle()


def claude(prompt: str, schema: dict | None = None, model: str | None = None):
    """One `claude -p` call. Returns the schema-validated object when `schema` is given, else the text."""
    cmd = [CLAUDE_BIN, "-p", "--output-format", "json", "--model", model or MODEL, "--tools", "",
           "--no-session-persistence", "--strict-mcp-config", "--setting-sources", ""]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    # An empty cwd keeps the project folder (and .env) out of reach even if a posting tries prompt injection.
    with tempfile.TemporaryDirectory() as cwd:
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd,
                                  timeout=CLAUDE_TIMEOUT)
        except FileNotFoundError as exc:
            raise ClaudeError(f"{CLAUDE_BIN} not found — is the Claude Code CLI installed?") from exc
        except subprocess.TimeoutExpired as exc:
            raise ClaudeError(f"claude -p timed out after {CLAUDE_TIMEOUT}s") from exc
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise ClaudeError(f"claude -p exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-300:]}")
    COST["usd"] += out.get("total_cost_usd") or 0
    if out.get("is_error") or out.get("subtype") != "success":
        raise ClaudeError(f"claude -p: {out.get('subtype')}: {str(out.get('result'))[:300]}")
    if schema:
        verdict = out.get("structured_output")
        if not isinstance(verdict, dict):  # the model's reply, not the CLI: counts toward MAX_ATTEMPTS
            raise ValueError(f"no structured output: {str(out.get('result'))[:200]}")
        return verdict
    return (out.get("result") or "").strip()


def code_version() -> str:
    """The deployed git commit when running from a clone (deploy/nas/update.sh), else 'copied files'."""
    git = ROOT / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head[:7]
        ref = head[5:]
        if (git / ref).exists():
            return f"{ref.split('/')[-1]}@{(git / ref).read_text().strip()[:7]}"
        packed = (git / "packed-refs").read_text()
        m = re.search(rf"^([0-9a-f]{{40}}) {re.escape(ref)}$", packed, re.M)
        return f"{ref.split('/')[-1]}@{m.group(1)[:7]}" if m else ref
    except OSError:
        return "copied files (not a git clone)"


def claude_ok() -> str:
    try:
        v = subprocess.run([CLAUDE_BIN, "--version"], capture_output=True, text=True, timeout=30)
        return v.stdout.strip() or f"exit {v.returncode}"
    except Exception as exc:
        return f"UNAVAILABLE: {exc}"


def verdict_schema() -> dict:
    s, arr = {"type": "string"}, lambda item: {"type": "array", "items": item}
    props = {"suitable": {"type": "boolean"}, "score": {"type": "integer", "minimum": 0, "maximum": 100},
             "reason": s, "gaps": s, "coding_test_risk": {"type": "string", "enum": ["low", "medium", "high"]},
             "tagline": s, "summary": s, "projects": arr({"type": "string", "enum": PROJECTS}),
             "employer": s, "contact_titles": arr(s), "outreach": s, "letter": arr(s)}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def description(rec: dict) -> str:
    """The job text: MCF via its API, Greenhouse via the per-job endpoint (Ashby and Lever arrive with
    rec["desc"] already filled by job_sources). Anything else is judged on title + company."""
    if rec.get("jd_url"):
        try:
            req = urllib.request.Request(rec["jd_url"], headers=sweep.UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return job_sources._plain(json.load(r).get("content")) or "(empty description)"
        except Exception as exc:
            return f"(description unavailable: {exc})"
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
    verdict = claude(prompt, verdict_schema())
    # The schema already restricts projects to the real library; this is the belt to its braces.
    picks = [p for p in verdict.get("projects") or [] if p in PROJECTS]
    verdict["projects"] = picks or PROJECTS[:4]
    return verdict


FACTCHECK_PROMPT = """You are fact-checking a job application drafted for the candidate below. The ONLY
source of truth about him is the RESUME. Check every factual claim about the candidate in the DRAFT
(employers, titles, dates, years of experience, numbers, tools, certifications, achievements).

Return the draft corrected: remove or soften any claim the resume does not support, and keep everything
else exactly as written (same wording, same paragraph count). In "issues", list each change as
"<claim> -> <what you did and why>"; an empty list means the draft was already accurate.

RESUME:
{profile}

DRAFT (for {title} at {company}):
Tagline: {tagline}
Summary: {summary}
Outreach note: {outreach}
Cover letter paragraphs:
{letter}"""
FACTCHECK_SCHEMA = {"type": "object", "additionalProperties": False,
                    "required": ["issues", "tagline", "summary", "outreach", "letter"],
                    "properties": {"issues": {"type": "array", "items": {"type": "string"}},
                                   "tagline": {"type": "string"}, "summary": {"type": "string"},
                                   "outreach": {"type": "string"},
                                   "letter": {"type": "array", "items": {"type": "string"}}}}


def factcheck(rec: dict, v: dict) -> dict:
    """Second pass on a fit's drafts. On failure the drafts go out unchanged but marked unchecked."""
    if not FACTCHECK:
        return v
    try:
        got = claude(FACTCHECK_PROMPT.format(
            profile=profile(), title=rec["title"], company=rec["company"], tagline=v.get("tagline", ""),
            summary=v.get("summary", ""), outreach=v.get("outreach", ""),
            letter="\n\n".join(f"[{i + 1}] {p}" for i, p in enumerate(v.get("letter") or []))), FACTCHECK_SCHEMA)
    except Exception as exc:
        return dict(v, factcheck=None, factcheck_error=str(exc)[:200])
    fixed = dict(v, factcheck=got.get("issues") or [])
    for k in ("tagline", "summary", "outreach"):
        if got.get(k):
            fixed[k] = got[k]
    if got.get("letter"):
        fixed["letter"] = got["letter"]
    return fixed


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
        f"Why: {v.get('reason')}\n\nGaps: {v.get('gaps')}\n"
        + ("" if v.get("factcheck") is None else
           "\nFact-check against the resume: "
           + ("no changes needed.\n" if not v["factcheck"] else "\n" + "\n".join(f"- {i}" for i in v["factcheck"]) + "\n")),
        encoding="utf-8")
    if v.get("outreach"):
        links = "\n".join(f"- {t}: {people_search(rec['company'], t)}" for t in v.get("contact_titles") or [])
        (outdir / "outreach.md").write_text(
            f"# Outreach — {rec['title']} at {rec['company']}\n\nWho to reach (check for 1st/2nd-degree "
            f"connections first; a referral beats a cold note):\n{links}\n\nConnection note:\n\n"
            f"{v['outreach']}\n", encoding="utf-8")
    return outdir


def people_search(company: str, title: str) -> str:
    q = urllib.parse.quote(f"{company.title()} {title}")
    return f"https://www.linkedin.com/search/results/people/?keywords={q}"


def is_fit(v: dict) -> bool:
    return bool(v.get("suitable")) and int(v.get("score") or 0) >= FIT_THRESHOLD and bool(v.get("letter"))


def alert_text(r: dict, v: dict, folder: Path, ref: int | None = None) -> str:
    pay = "pay not published" if r.get("lo") is None else f"${r['lo']:,}–{r.get('hi') or '?'}/mo"
    head = "🔥 Apply today" if int(v.get("score") or 0) >= URGENT_SCORE else "🆕"
    src = f" · via {r['source']}" if str(r.get("source", "")).startswith("email") else ""
    text = (f"{head} {r['title']} — {r['company'].title()}\n{pay} · fit {v['score']}/100 · "
            f"coding-test risk {v.get('coding_test_risk', '?')}{src}\n\nWhy: {v.get('reason')}\n"
            f"Gaps: {v.get('gaps')}\n\n{r.get('url', '')}\n")
    if v.get("outreach"):
        who = v.get("contact_titles") or ["the hiring manager"]
        text += (f"\n👤 Reach out to: {', '.join(who)}\n{people_search(r['company'], who[0])}\n"
                 f"Note: {v['outreach']}\n")
    if v.get("factcheck") is not None:
        n = len(v["factcheck"])
        text += f"\n✔ Fact-checked against your resume: {'no changes' if not n else f'{n} claim(s) corrected, see fit.md'}\n"
    elif v.get("factcheck_error"):
        text += "\n⚠️ Not fact-checked (the check failed) — read the letter carefully.\n"
    text += f"\nResume + cover letter: NAS docker/job-hunter/tailored-auto/{folder.name}/\n"
    if ref:
        text += f"Ref #{ref} — send /applied {ref} once you've applied.\n"
    return text + "Nothing was submitted — your call."


def track(state: dict, key: str, rec: dict, v: dict | None, folder: Path | None, status: str = "drafted") -> int:
    """Give a posting a short ref number so outcomes can be recorded from Telegram (/applied 12)."""
    entry = state["seen"].setdefault(key, {"t": rec["title"], "c": rec["company"], "at": _now()})
    if not entry.get("ref"):
        state["next_ref"] = n = int(state.get("next_ref") or 0) + 1
        entry["ref"] = n
        state.setdefault("refs", {})[str(n)] = key
    entry.update(status=status, history=(entry.get("history") or []) + [[status, _now()]],
                 url=rec.get("url", entry.get("url", "")), source=rec.get("source", entry.get("source", "")))
    if v is not None:
        entry["fit"] = v.get("score")
    if folder is not None:
        entry["folder"] = folder.name
    return entry["ref"]


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
    COST["usd"] = 0.0
    save_state(state)
    jobs = candidates()
    mail_note = ""
    try:
        jobs += mail_jobs(state)
    except MailPartial as exc:
        jobs += exc.jobs
        mail_note = f"mail: {exc}"[:200]
        print("  ! " + mail_note)
    except Exception as exc:  # a mail problem must not cost the board sweep
        mail_note = f"mail: {type(exc).__name__}: {exc}"[:200]
        print("  ! " + mail_note)
    ident = lambda r: r.get("uuid") or r["id"]  # noqa: E731
    if not seen:
        # First run: everything on the boards today is old news (the PC sweep already covered it).
        for r in jobs:
            seen[ident(r)] = {"t": r["title"], "c": r["company"], "fit": None, "at": _now()}
            if fingerprint(r["company"], r["title"]):
                state.setdefault("fps", {})[fingerprint(r["company"], r["title"])] = ident(r)
        _finish(state, {"candidates": len(jobs), "new": len(jobs), "judged": 0, "fits": 0, "failed": 0,
                        "note": "baseline"})
        telegram(f"Job watcher online on the NAS. Baseline: {len(jobs)} current postings; "
                 f"I'll message you when something new fits (checking every {BUSY_INTERVAL_H:g}h in "
                 f"working hours, {INTERVAL_H:g}h otherwise).")
        return

    new = [r for r in jobs if ident(r) not in seen]
    fps = state.setdefault("fps", {})
    fresh, dups, batch = [], 0, {}
    for r in new:
        # The same role often shows up on MCF, a LinkedIn alert and the company's own board, sometimes in
        # the same sweep: judge it once.
        fp = fingerprint(r["company"], r["title"])
        first = batch.get(fp) or (fps.get(fp) if fps.get(fp) in seen else None)
        if fp and first and first != ident(r):
            seen[ident(r)] = {"t": r["title"], "c": r["company"], "fit": None, "at": _now(), "dup_of": first}
            dups += 1
        else:
            fresh.append(r)
            if fp:
                batch[fp] = ident(r)
    new = fresh
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
        if fingerprint(r["company"], r["title"]):
            fps[fingerprint(r["company"], r["title"])] = ident(r)
        fit = is_fit(v)
        seen[ident(r)] = {"t": r["title"], "c": r["company"], "fit": v.get("score"), "suitable": bool(fit),
                          "why": v.get("reason"), "url": r.get("url", ""), "at": _now()}
        print(f"  {v.get('score'):>3} {'FIT ' if fit else 'skip'} {r['title']} @ {r['company']}")
        if fit:
            fits += 1
            v = factcheck(r, v)
            folder = build(r, v)
            telegram(alert_text(r, v, folder, track(state, ident(r), r, v, folder)))
        save_state(state)
    _finish(state, {"candidates": len(jobs), "new": len(new), "judged": judged, "fits": fits,
                    "failed": failed, "dups": dups,
                    "note": "; ".join(x for x in (last_fail[:300], mail_note) if x)})
    if failed and not judged:
        # Every judge call failed: usually the CLI is signed out, rate-limited or offline. Say so instead of
        # silently retrying forever.
        telegram(f"⚠️ Job watcher: all {failed} judge calls failed ({last_fail[:200]}). "
                 f"Check the Claude login (CLAUDE_CODE_OAUTH_TOKEN) or limits. Postings stay unseen and retry "
                 f"next cycle.")


COMPANY_NOISE = re.compile(r"\b(pte|ltd|limited|inc|llc|plc|corp|corporation|co|company|group|holdings|"
                           r"technologies|technology|singapore|sg|asia|pacific|apac|the)\b")


def fingerprint(company: str, title: str) -> str:
    """company|title with legal suffixes, locations and punctuation removed; '' when too vague to trust."""
    norm = lambda t: " ".join(re.sub(r"[^a-z0-9]+", " ", t.lower()).split())  # noqa: E731
    co = " ".join(COMPANY_NOISE.sub(" ", norm(company)).split())
    ti = " ".join(re.sub(r"\b(singapore|sg|remote|hybrid)\b", " ", norm(title)).split())
    return f"{co}|{ti}" if len(co) >= 2 and ti else ""


def prune(state: dict) -> None:
    """Forget postings older than SEEN_DAYS so the state file (synced every heartbeat) stays small.
    Entries from before timestamps existed are stamped now and age out from here. A posting still
    live after SEEN_DAYS just gets judged once more."""
    cutoff = (dt.datetime.now() - dt.timedelta(days=SEEN_DAYS)).isoformat(timespec="seconds")
    seen = state["seen"]
    for key, v in list(seen.items()):
        v.setdefault("at", _now())
        if v["at"] < cutoff and not v.get("ref"):  # tracked applications are kept for the record
            del seen[key]
    state["fps"] = {fp: k for fp, k in (state.get("fps") or {}).items() if k in seen}


def _finish(state: dict, run: dict) -> None:
    """Every cycle leaves a record, even one with nothing new, so 'quiet' is visible as 'alive'."""
    run["at"] = _now()
    run["cost_usd"] = round(COST["usd"], 4)
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


# ---- job-alert emails ----------------------------------------------------------------------------

EXTRACT_PROMPT = """Below is a job-alert email from a job board. List every individual job posting in it.
For each: the job title, the hiring company, the location as written, the link to that job (copy the URL
exactly from the [...] after it), the salary exactly as shown (empty if none), and any short snippet. Skip ads, courses, "people also
viewed" profiles and settings links. If the email contains no job postings, return an empty list.

FROM: {sender}
SUBJECT: {subject}

{text}"""
EXTRACT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["jobs"], "properties": {
    "jobs": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                        "required": ["title", "company", "location", "url", "salary", "snippet"],
                                        "properties": {k: {"type": "string"} for k in
                                                       ("title", "company", "location", "url", "salary",
                                                        "snippet")}}}}}


def alert_jobs(mail: dict) -> list[dict]:
    """The postings in one alert email, as records judge() understands, after the same regex filters."""
    got = claude(EXTRACT_PROMPT.format(sender=mail["from"], subject=mail["subject"], text=mail["text"]),
                 EXTRACT_SCHEMA, model=EXTRACT_MODEL)
    board = next((b for b in ("linkedin", "jobstreet", "glassdoor", "indeed") if b in mail["from"].lower()),
                 "alert")
    out = []
    for j in got.get("jobs") or []:
        title, company = j["title"].strip(), j["company"].strip()
        if not title or not sweep.TITLE_KEEP.search(title) or sweep.TITLE_DROP.search(title):
            continue
        if sweep.COMPANY_DROP.search(company):
            continue
        lo, hi = mail_alerts.parse_pay(j.get("salary") or "")
        if lo is not None and lo < sweep.SALARY_FLOOR:  # the same pay floor as the board sweep, before any judging
            continue
        url = mail_alerts.canonical_url(j["url"])
        out.append({"id": mail_alerts.job_key(title, company, url), "title": title, "company": company,
                    "url": url, "lo": lo, "hi": hi, "source": f"email:{board}",
                    "desc": f"(from a {board} job-alert email; the full description is not available — judge on "
                            f"title, company and this) Location: {j['location']}. Salary: {j.get('salary') or 'n/s'}. "
                            f"{j['snippet']}"})
    return out


def mail_jobs(state: dict) -> list[dict]:
    if not mail_alerts.configured():
        return []
    done = state.setdefault("mail_done", [])
    jobs: dict[str, dict] = {}
    failed = []
    for mail in mail_alerts.fetch(set(done)):
        try:
            recs = alert_jobs(mail)
        except Exception as exc:  # not marked done, so this email is retried next cycle
            failed.append(f"{type(exc).__name__}: {exc}")
            continue
        for rec in recs:
            jobs.setdefault(rec["id"], rec)
        done.append(mail["id"])
    state["mail_done"] = done[-500:]
    if failed:  # the jobs already read are still returned; cycle() reports the rest
        raise MailPartial(list(jobs.values()), f"{len(failed)} alert email(s) not read: {failed[-1]}")
    return list(jobs.values())


class MailPartial(Exception):
    def __init__(self, jobs: list[dict], msg: str):
        super().__init__(msg)
        self.jobs = jobs


# ---- application tracking ------------------------------------------------------------------------

def find_ref(state: dict, arg: str) -> tuple[str | None, str]:
    """(seen key, rest of the text) for '12 ...' or '#12 ...'; (None, arg) when it isn't a known ref."""
    m = re.match(r"#?(\d+)\b\s*(.*)", arg, re.S)
    if m and m.group(1) in state.get("refs", {}):
        return state["refs"][m.group(1)], m.group(2)
    return None, arg


def record_outcome(arg: str, status: str) -> str:
    state = load_state()
    key, note = find_ref(state, arg)
    if key is None:  # something applied to outside the watcher: track it by its description
        title, _, company = arg.partition(" at ")
        key = "manual:" + hashlib.sha1(arg.lower().encode()).hexdigest()[:10]
        rec = {"title": title.strip()[:120], "company": company.strip()[:80] or "?", "source": "manual"}
        note = ""
    else:
        e = state["seen"][key]
        rec = {"title": e["t"], "company": e["c"]}
    ref = track(state, key, rec, None, None, status)
    entry = state["seen"][key]
    if status == "applied":
        entry["applied_at"] = _now()
        entry.pop("followup_sent", None)
    if note.strip():
        entry["note"] = note.strip()[:300]
    save_state(state)
    tail = {"applied": f"I'll remind you to follow up in {FOLLOWUP_DAYS} days if nothing moves.",
            "interview": "Good luck! Prep pack coming.", "offer": "🎉 Congratulations!",
            "rejected": "Noted. It still helps calibrate the threshold.",
            "ghosted": "Noted."}[status]
    return f"✅ #{ref} {entry['t']} — {entry['c']}: {status}. {tail}"


STATUS_ORDER = ["offer", "interview", "applied", "drafted", "rejected", "ghosted"]


def pipeline_text(state: dict, limit: int = 30) -> str:
    rows = [e for e in state.get("seen", {}).values() if e.get("ref")]
    rows.sort(key=lambda e: (STATUS_ORDER.index(e.get("status", "drafted")), -e["ref"]))
    lines = []
    for e in rows[:limit]:
        since = _age((e.get("history") or [[None, None]])[-1][1])
        lines.append(f"#{e['ref']} {e.get('status', '?'):<9} {e['t']} — {e['c']}"
                     + (f" (fit {e['fit']})" if e.get("fit") is not None else "") + f", {since}")
    return "\n".join(lines)


PREP_PROMPT = """Write a one-page interview prep pack for the candidate below, for this role.
Plain text for a Telegram message: short headings in CAPS, dash bullets, no markdown tables, under 3,500
characters. You have no web access: say "from general knowledge, verify" wherever you describe the company.

Sections:
COMPANY & LIKELY STACK — what the company does and the infrastructure it most likely runs.
LIKELY QUESTIONS — 6-8 technical / system-design / behavioural questions this role would ask, each with a
  one-line pointer to the resume evidence to use.
YOUR GAPS — the gaps below, each with an honest 1-2 sentence way to answer it.
QUESTIONS TO ASK THEM — 4 sharp ones about the platform, on-call and team.

Today is {today}.

CANDIDATE:
{profile}

ROLE: {title} at {company}
{url}
Fit notes: {fit}"""


def prep_pack(arg: str) -> str:
    state = load_state()
    key, _ = find_ref(state, arg)
    e = state["seen"].get(key) if key else None
    if e is None:
        title, _, company = arg.partition(" at ")
        e = {"t": title.strip(), "c": company.strip() or "?", "url": ""}
    folder = OUT / e["folder"] if e.get("folder") else None
    fit = (folder / "fit.md").read_text(encoding="utf-8") if folder and (folder / "fit.md").exists() else \
        (e.get("why") or "(no earlier verdict)")
    pack = claude(PREP_PROMPT.format(today=dt.date.today(), profile=profile(), title=e["t"], company=e["c"],
                                     url=e.get("url", ""), fit=fit[:1500]))
    if folder and folder.is_dir():  # the draft folder may have been cleaned up by hand
        (folder / "prep.md").write_text(pack + "\n", encoding="utf-8")
    return "📚 " + pack


FOLLOWUP_PROMPT = """Draft a short, polite follow-up email (subject line + at most 90 words) from the
candidate below to the recruiter or hiring manager for a role he applied to {days} days ago with no reply.
Restate interest, add one concrete proof point from the resume that matches the role, ask about next
steps. Plain text, no placeholders except [Name].

CANDIDATE:
{profile}

ROLE: {title} at {company}
{url}"""


def chores(now: dt.datetime | None = None) -> None:
    """Time-based jobs run from the idle loop: follow-up reminders (daily) and the weekly digest."""
    now = now or dt.datetime.now()
    backup_state(now)
    if now.hour < 9:
        return
    state = load_state()
    today = now.date().isoformat()
    if state.get("followups_checked") != today:
        # Marked first: a failing Claude call must not turn this into a retry every idle loop (~50s).
        # One that fails is simply tried again tomorrow.
        state["followups_checked"] = today
        save_state(state)
        cutoff = (now - dt.timedelta(days=FOLLOWUP_DAYS)).isoformat(timespec="seconds")
        for e in state.get("seen", {}).values():
            if e.get("status") == "applied" and e.get("applied_at", "9") < cutoff and not e.get("followup_sent"):
                try:
                    draft = claude(FOLLOWUP_PROMPT.format(days=FOLLOWUP_DAYS, profile=profile(), title=e["t"],
                                                          company=e["c"], url=e.get("url", "")))
                except Exception as exc:
                    draft = f"(couldn't draft one: {exc})"
                telegram(f"⏰ #{e['ref']} {e['t']} — {e['c']}: applied {FOLLOWUP_DAYS}+ days ago, no update. "
                         f"Worth a follow-up (or /ghosted {e['ref']}).\n\n{draft}")
                e["followup_sent"] = _now()
                save_state(state)
    week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    if now.weekday() == DIGEST_WEEKDAY and state.get("last_digest") != week:
        telegram(digest_text(state, now))
        state["last_digest"] = week
        save_state(state)


def backup_state(now: dt.datetime | None = None) -> None:
    """One copy of the state file per day in build/backups, the last BACKUP_DAYS kept. The tracked
    applications live only here, so a bad write or an accidental delete must be recoverable."""
    if not STATE.exists():
        return
    folder = STATE.parent / "backups"
    target = folder / f"nas_state-{(now or dt.datetime.now()):%Y-%m-%d}.json"
    if target.exists():
        return
    folder.mkdir(exist_ok=True)
    target.write_bytes(STATE.read_bytes())
    for old in sorted(folder.glob("nas_state-*.json"))[:-BACKUP_DAYS]:
        old.unlink()


def digest_text(state: dict, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now()
    since = (now - dt.timedelta(days=7)).isoformat(timespec="seconds")
    runs = [r for r in state.get("runs") or [] if r.get("at", "") >= since]
    tracked = [e for e in state.get("seen", {}).values() if e.get("ref")]
    moved = lambda st: sum(1 for e in tracked for s, at in e.get("history") or [] if s == st and at >= since)  # noqa: E731
    lines = [f"📊 Weekly job-hunt digest (week to {now:%d %b})",
             f"Sweeps {len(runs)} · judged {sum(r.get('judged') or 0 for r in runs)} · "
             f"fits {sum(r.get('fits') or 0 for r in runs)} · Claude ${sum(r.get('cost_usd') or 0 for r in runs):.2f}",
             f"This week: applied {moved('applied')}, interviews {moved('interview')}, offers {moved('offer')}, "
             f"rejections {moved('rejected')}"]
    applied = [e for e in tracked if any(s == "applied" for s, _ in e.get("history") or [])]
    replied = [e for e in applied if e.get("status") in ("interview", "offer")]
    if applied:
        lines.append(f"All time: {len(applied)} applied → {len(replied)} interviews "
                     f"({100 * len(replied) // len(applied)}% reply rate)")
        by_src: dict[str, list[int]] = {}
        for e in applied:
            src = (e.get("source") or "board").split(":")[-1]
            by_src.setdefault(src, [0, 0])[0] += 1
            by_src[src][1] += e in replied
        lines.append("By source: " + ", ".join(f"{k} {v[1]}/{v[0]}" for k, v in sorted(by_src.items())))
    unapplied = [e for e in tracked if e.get("status") == "drafted"]
    if unapplied:
        lines.append(f"{len(unapplied)} drafted but not applied — /pipeline to review.")
    scored = [e for e in applied if e.get("fit") is not None]
    good = [e["fit"] for e in scored if e in replied]
    bad = [e["fit"] for e in scored if e.get("status") in ("rejected", "ghosted")]
    if len(good) >= 3 and len(bad) >= 3:
        lines.append(f"Calibration: interviews came at avg fit {sum(good) / len(good):.0f}, "
                     f"no-reply/rejections at {sum(bad) / len(bad):.0f} (threshold {FIT_THRESHOLD}).")
        if min(good) > FIT_THRESHOLD + 5:
            lines.append(f"Every interview scored ≥{min(good)}; consider FIT_THRESHOLD={min(good) - 5} to cut noise.")
    active = pipeline_text({"seen": {k: e for k, e in state.get("seen", {}).items()
                                     if e.get("status") in ("applied", "interview", "offer")}}, 10)
    if active:
        lines += ["", "Active:", active]
    return "\n".join(lines)


# ---- Telegram commands -------------------------------------------------------------------------

HELP = ("Job watcher commands:\n"
        "/status — health, last cycle, recent runs\n"
        "/ask <question> — ask the model about seen jobs, drafts, or the system\n"
        "/judge <url> [pasted job text] — judge one posting now, draft if it fits\n"
        "/sweep — run a cycle now\n"
        "/applied, /interview, /offer, /rejected, /ghosted <ref or text> — record an outcome "
        "(/interview also builds a prep pack)\n"
        "/pipeline — tracked applications\n"
        "/selfcheck — test Claude, the job boards, mail and storage for real\n"
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

APPLICATION PIPELINE (ref, status, title @ company, fit):
{pipeline}

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
             f"Model: {MODEL} via claude -p ({claude_ok()})",
             f"Code: {code_version()}",
             f"Postings tracked: {len(state.get('seen', {}))}"]
    if state.get("last_error"):
        e = state["last_error"]
        lines.append(f"Last error {_age(e.get('at'))}: {e.get('error', '')[-400:]}")
    for r in runs[-5:]:
        lines.append(f"- {r.get('at', '?')[5:16]}: {r.get('candidates')} cand, {r.get('new')} new, "
                     f"{r.get('judged')} judged, {r.get('fits')} fit, {r.get('dups', 0)} dup, {r.get('failed')} failed, "
                     f"${r.get('cost_usd', 0):.2f}"
                     + (f" ({r['note']})" if r.get("note") else ""))
    return "\n".join(lines)


def ask(question: str) -> str:
    state = load_state()
    judged = [v for v in state.get("seen", {}).values() if v.get("fit") is not None][-60:]
    judged_txt = "\n".join(f"- {v.get('fit')} {'FIT' if v.get('suitable') else 'skip'} {v['t']} @ {v['c']}"
                            + (f" — {v['why']}" if v.get("why") else "") + (f" {v['url']}" if v.get("url") else "")
                            for v in judged) or "(none yet)"
    drafts = sorted(OUT.glob("*/fit.md"), key=lambda f: f.stat().st_mtime)[-8:] if OUT.exists() else []
    drafts_txt = "\n\n".join(f.read_text(encoding="utf-8")[:800] for f in drafts) or "(none yet)"
    prompt = ASK_PROMPT.format(today=dt.date.today(), profile=profile(), status=status_text(state),
                               judged=judged_txt, drafts=drafts_txt, pipeline=pipeline_text(state, 40),
                               question=question)
    return claude(prompt) or "(the model returned nothing)"


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
        v = factcheck(rec, v)
        folder = build(rec, v)
        state = load_state()
        ref = track(state, rec.get("uuid") or rec["id"], rec, v, folder)
        save_state(state)
        return alert_text(rec, v, folder, ref)
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
            elif cmd in OUTCOMES:
                if not arg.strip():
                    telegram(f"Usage: {cmd} <ref number from the alert, or the role and company>", thread)
                else:
                    telegram(record_outcome(arg.strip(), OUTCOMES[cmd]), thread)
                    if OUTCOMES[cmd] == "interview":
                        telegram("⏳ Building the interview prep pack…", thread)
                        telegram(prep_pack(arg.strip()), thread)
            elif cmd == "/selfcheck":
                telegram("⏳ Running the self-check (about 30s)…", thread)
                telegram(selfcheck(), thread)
            elif cmd == "/pipeline":
                telegram(pipeline_text(load_state()) or "Nothing tracked yet.", thread)
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
        try:
            chores()
        except Exception as exc:  # reminders are a nicety; never let them stop the loop
            print(f"  ! chores failed: {exc}")
        if poll_commands(load_state(), int(min(50, max(1, left)))):
            return


def due_in(state: dict) -> float:
    """Seconds until the next cycle is due, from the last finished one (0 if overdue or never ran)."""
    try:
        last = dt.datetime.fromisoformat(state["last_cycle"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    return max(0.0, interval_now() * 3600 - (dt.datetime.now() - last).total_seconds())


def interval_now(now: dt.datetime | None = None) -> float:
    now = now or dt.datetime.now()
    busy = now.weekday() < 5 and BUSY_HOURS[0] <= now.hour < BUSY_HOURS[1]
    return BUSY_INTERVAL_H if busy else INTERVAL_H


def selfcheck() -> str:
    """Exercise every live dependency once and report. Each check is independent: one failure never
    hides the others. Costs one tiny haiku call."""
    lines = ["🩺 Self-check"]

    def run(label: str, fn) -> None:
        start = time.time()
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {str(exc)[:160]}"
        mark = {True: "✅", False: "❌", None: "⚪"}[ok]
        lines.append(f"{mark} {label}: {detail} ({time.time() - start:.1f}s)")

    def claude_check():
        reply = claude("Reply with the single word OK and nothing else.", model=EXTRACT_MODEL)
        return "OK" in reply.upper(), f"{claude_ok()}, {EXTRACT_MODEL} replied {reply[:20]!r}"

    def mcf_check():
        n = len(sweep.fetch("devops", 0).get("results") or [])
        return n > 0, f"{n} results for 'devops'"

    def board(name: str, url: str, count, with_desc=None):
        def check():
            data = job_sources._get(url)
            jobs = count(data)
            extra = ""
            if with_desc and jobs:
                extra = f", description {len(with_desc(data))} chars"
            return len(jobs) > 0 or None, f"{len(jobs)} open roles{extra}" if jobs else "reachable, no open roles"
        run(name, check)

    def gh_desc(data):
        first = data["jobs"][0]
        slug = job_sources.GREENHOUSE[0]
        full = job_sources._get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{first['id']}")
        return job_sources._plain(full.get("content"))

    def mail_check():
        if not mail_alerts.configured():
            return None, "not set up (IMAP_USER / IMAP_PASSWORD unset)"
        mails = mail_alerts.fetch(set())
        return True, f"logged in, {len(mails)} alert email(s) in the last {os.environ.get('MAIL_DAYS', '3')} days"

    def storage_check():
        probe = STATE.parent / ".selfcheck"
        probe.write_text("ok")
        probe.unlink()
        backups = sorted((STATE.parent / "backups").glob("nas_state-*.json"))
        size = STATE.stat().st_size // 1024 if STATE.exists() else 0
        last = backups[-1].stem.split("-", 1)[1] if backups else "none yet"
        return True, f"writable, state {size} KB, {len(backups)} backup(s), latest {last}"

    def files_check():
        missing = []
        if "content" not in sys.modules:
            missing.append("build/content.py (drafts use the EXAMPLE resume)")
        if sweep.profile.__name__ != "my_profile":
            missing.append("build/my_profile.py (EXAMPLE pay floor and exclusions)")
        return (not missing), ("your resume and filters are loaded" if not missing else "missing " + "; ".join(missing))

    run("Private files", files_check)
    run("Claude", claude_check)
    run("MyCareersFuture", mcf_check)
    board(f"Greenhouse ({job_sources.GREENHOUSE[0]})",
          f"https://boards-api.greenhouse.io/v1/boards/{job_sources.GREENHOUSE[0]}/jobs",
          lambda d: d.get("jobs") or [], gh_desc)
    board(f"Ashby ({job_sources.ASHBY[0]})", f"https://api.ashbyhq.com/posting-api/job-board/{job_sources.ASHBY[0]}",
          lambda d: d.get("jobs") or [], lambda d: d["jobs"][0].get("descriptionPlain") or "")
    board(f"Lever ({job_sources.LEVER[0]})", f"https://api.lever.co/v0/postings/{job_sources.LEVER[0]}?mode=json",
          lambda d: d if isinstance(d, list) else [], lambda d: d[0].get("descriptionPlain") or "")
    run("Alert emails", mail_check)
    run("Storage", storage_check)
    state = load_state()
    lines.append(f"ℹ️ Heartbeat {_age(state.get('heartbeat'))}, last sweep {_age(state.get('last_cycle'))}, "
                 f"code {code_version()}")
    bad = sum(line.startswith("❌") for line in lines)
    lines.append("All good." if not bad else f"{bad} problem(s) above.")
    return "\n".join(lines)


def main() -> None:
    if "--selfcheck" in sys.argv:
        report = selfcheck()
        print(report)
        sys.exit(1 if "❌" in report else 0)
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
        idle(interval_now() * 3600)


if __name__ == "__main__":
    main()
