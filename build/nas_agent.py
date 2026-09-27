"""Always-on job watcher for the NAS: sweep, judge fit with Ollama Cloud, draft documents, ping Telegram.

    python build/nas_agent.py          # loop forever (the container's entrypoint)
    python build/nas_agent.py --once   # one cycle, then exit

Reuses the sweep (weekly_sweep.collect + job_sources) and the document builder (build_docs), so the
filters in docs/vault/Target Criteria.md and the resume content in build/content.py stay the single
source of truth. Never applies anywhere: it drafts into tailored-auto/ and tells James.

It keeps its own state (build/.nas_state.json), separate from the PC sweep's, and writes nothing to
the vault — the NAS copy of the vault would drift from the OneDrive one.

Env: OLLAMA_BASE_URL, JOB_MODEL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_THREAD_ID, SWEEP_INTERVAL_HOURS,
     MAX_PER_CYCLE, FIT_THRESHOLD.
"""
from __future__ import annotations

import datetime as dt
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
                           years=rec.get("years") or "n/s", desc=description(rec), projects=PROJECTS)
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


def telegram(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("[TG] not configured:\n" + text)
        return
    fields = {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"}
    if os.environ.get("TELEGRAM_THREAD_ID"):  # a forum topic, e.g. t.me/c/<chat>/<topic>
        fields["message_thread_id"] = os.environ["TELEGRAM_THREAD_ID"]
    data = urllib.parse.urlencode(fields).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30)


def candidates() -> list[dict]:
    found = sweep.collect()
    for rec in job_sources.fetch_all(sweep.TITLE_KEEP):
        if not sweep.TITLE_DROP.search(rec["title"]) and not sweep.COMPANY_DROP.search(rec["company"]):
            found.setdefault(rec["id"], rec)
    # Below-floor bands are out; unknown pay and career pages go to the model.
    return [r for r in found.values() if r.get("lo") is None or r["lo"] >= sweep.SALARY_FLOOR]


def cycle() -> None:
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"seen": {}}
    seen = state["seen"]
    jobs = candidates()
    ident = lambda r: r.get("uuid") or r["id"]  # noqa: E731
    if not seen:
        # First run: everything on the boards today is old news (the PC sweep already covered it).
        for r in jobs:
            seen[ident(r)] = {"t": r["title"], "c": r["company"], "fit": None}
        STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
        telegram(f"Job watcher online on the NAS. Baseline: {len(jobs)} current postings; "
                 f"I'll message you when something new fits (checking every {INTERVAL_H:g}h).")
        return

    new = [r for r in jobs if ident(r) not in seen]
    print(f"[{dt.datetime.now():%F %T}] {len(jobs)} candidates, {len(new)} new")
    for r in new[:MAX_PER_CYCLE]:
        try:
            v = judge(r)
        except Exception as exc:  # leave it unseen so the next cycle retries it
            print(f"  ! judge failed for {r['title']} @ {r['company']}: {exc}")
            continue
        fit = bool(v.get("suitable")) and int(v.get("score") or 0) >= FIT_THRESHOLD and v.get("letter")
        seen[ident(r)] = {"t": r["title"], "c": r["company"], "fit": v.get("score"), "suitable": bool(fit)}
        print(f"  {v.get('score'):>3} {'FIT ' if fit else 'skip'} {r['title']} @ {r['company']}")
        if fit:
            folder = build(r, v)
            pay = "pay not published" if r.get("lo") is None else f"${r['lo']:,}–{r.get('hi') or '?'}/mo"
            telegram(f"🆕 {r['title']} — {r['company'].title()}\n{pay} · fit {v['score']}/100 · "
                     f"coding-test risk {v.get('coding_test_risk', '?')}\n\nWhy: {v.get('reason')}\n"
                     f"Gaps: {v.get('gaps')}\n\n{r.get('url', '')}\n\n"
                     f"Resume + cover letter: NAS docker/job-hunter/tailored-auto/{folder.name}/\n"
                     f"Nothing was submitted — your call.")
        STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def main() -> None:
    once = "--once" in sys.argv
    while True:
        try:
            cycle()
        except Exception:
            traceback.print_exc()
            try:
                telegram("⚠️ Job watcher cycle failed — see `docker logs job-hunter`.")
            except Exception:
                pass
        if once:
            return
        time.sleep(INTERVAL_H * 3600)


if __name__ == "__main__":
    main()
