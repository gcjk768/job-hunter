"""Obsidian vault: the job watcher's movement log and memory (NAS vault standard).

Layout under $VAULT_DIR (on the NAS: /volume1/James/Obsidian/Job Hunter, mounted at /vault):
    Home.md                    the pipeline at a glance, rebuilt on each fit, outcome and new day
    Activity/YYYY-MM-DD.md     one line per event: - HH:MM emoji **what** · detail · [[entity]] (SGT)
    Jobs/Company — Title.md    one note per posting: fit, why, gaps, link; status in frontmatter; ## History
    Companies/Company.md       one note per company: its jobs and outcomes in ## History

The agent writes here after each sweep, judgement, alert, outcome, command and self-repair, and reads
it back: memory() goes into the judge and /jobask prompts, statuses() stops a handled posting being
alerted again. Best-effort: with VAULT_DIR unset nothing happens; any error is printed and swallowed,
never raised. Never pass secrets or whole prompts in here.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SGT = timezone(timedelta(hours=8))
MEMORY_CHARS = 4000
MEMORY_DAYS = 7  # Activity notes read back as memory
ENTITY_CHARS = 1200  # per entity note inside the memory excerpt
UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]\n\r\t]+')
# A posting in one of these states was already alerted or acted on: never alert it again.
HANDLED = {"drafted", "applied", "interview", "offer", "rejected", "ghosted"}
ORDER = ["offer", "interview", "applied", "drafted", "rejected", "ghosted", "skipped"]


def root() -> Path | None:
    path = os.environ.get("VAULT_DIR", "").strip()
    return Path(path) if path else None


def _warn(error) -> None:
    print(f"  ! vault: {error}", file=sys.stderr)


def note_name(text) -> str:
    """Text as a safe file / wikilink name."""
    return " ".join(UNSAFE.sub(" ", str(text)).split()).strip(" .")[:100].strip() or "untitled"


def company_name(company: str) -> str:
    """MCF shouts company names (ACME PTE. LTD.); keep everyone else's casing (OpenAI)."""
    company = " ".join(str(company).split()) or "?"
    return company.title() if company.isupper() else company


def job_link(company: str, title: str) -> str:
    return f"Jobs/{note_name(f'{company_name(company)} — {title}')}"


def company_link(company: str) -> str:
    return f"Companies/{note_name(company_name(company))}"


def _one_line(text, limit=300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _own(path: Path, mode: int = 0o664) -> None:
    """Keep it editable by James: the container runs as root, James is uid 1000 on the NAS."""
    try:
        os.chmod(path, mode)
        if getattr(os, "geteuid", lambda: -1)() == 0:
            os.chown(path, int(os.environ.get("VAULT_UID", "1000")), -1)
    except OSError:
        pass


def _mkdir(path: Path) -> None:
    if not path.is_dir():
        path.mkdir(parents=True, exist_ok=True)
        _own(path, 0o775)


def _write(path: Path, text: str) -> None:
    """Atomic write (tmp + rename)."""
    _mkdir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    _own(path)


def _split(text: str) -> tuple[dict, str]:
    """(frontmatter fields, body). Values are JSON when quoted, else raw text."""
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}, text
    fields = {}
    for line in m.group(1).splitlines():
        key, sep, val = line.partition(": ")
        if sep:
            try:
                fields[key] = json.loads(val)
            except ValueError:
                fields[key] = val
    return fields, text[m.end():]


def _front(fields: dict) -> str:
    raw = {"tags": f"[{fields.get('tags')}]", "updated": fields.get("updated")}  # plain, like James's own notes
    return "---\n" + "".join(f"{k}: {raw[k] if k in raw else json.dumps(v, ensure_ascii=False)}\n"
                             for k, v in fields.items() if v is not None) + "---\n"


def _note(link: str, history: str, summary: str | None, fields: dict, now: datetime) -> None:
    """Create or update an entity note: summary on top (kept when None), one line appended to ## History."""
    path = root() / f"{link}.md"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    meta, body = _split(old)
    head, _, hist = body.partition("\n## History\n")
    if summary is None:  # keep what was there, minus the title and the Home link
        summary = "\n".join(l for l in head.strip().splitlines()[1:] if l.strip() != "Back to [[Home]]").strip()
    lines = hist.strip().splitlines() + [f"- {now:%Y-%m-%d %H:%M} · {_one_line(history)}"]
    meta.pop("tags", None), meta.pop("updated", None)
    meta.update({k: v for k, v in fields.items() if v is not None})
    meta = {"tags": meta.get("status") or "active", "updated": f"{now:%Y-%m-%d}", **meta}
    _write(path, f"{_front(meta)}# {link.split('/', 1)[1]}\n\n{summary.strip()}\n\nBack to [[Home]]\n\n"
                 f"## History\n" + "\n".join(lines) + "\n")


def job(company: str, title: str, history: str, summary: str | None = None, now: datetime | None = None,
        **fields) -> str | None:
    """Write the posting's Jobs note and a line in its Companies note. fields: status, fit, ref, fp, url.
    Returns the wikilink target, or None when the vault is off or failed. Never raises."""
    if not root():
        return None
    try:
        now = now or datetime.now(SGT)
        jl, cl = job_link(company, title), company_link(company)
        if summary is not None:
            summary = f"🏢 [[{cl}]]\n{summary}"
        _note(jl, history, summary, dict(fields, company=company_name(company)), now)
        _note(cl, f"[[{jl}]] {history}",
              f"Every posting from {company_name(company)} the watcher has seen, and how each went.",
              {}, now)
        if fields.get("status") != "skipped":
            _home(root(), now)
        return jl
    except Exception as error:  # best-effort: never lose an alert over the vault
        _warn(error)
        return None


def log(emoji: str, what: str, detail="", link: str | None = None, now: datetime | None = None) -> None:
    """Append one event line to today's Activity note. Never raises."""
    base = root()
    if not base:
        return
    try:
        now = now or datetime.now(SGT)
        day = base / "Activity" / f"{now:%Y-%m-%d}.md"
        parts = [f"- {now:%H:%M} {emoji} **{_one_line(what, 80)}**"]
        if detail:
            parts.append(_one_line(detail))
        if link:
            parts.append(f"[[{link}]]")
        new = not day.exists()
        _mkdir(day.parent)
        with day.open("a", encoding="utf-8") as f:
            if new:
                f.write(f"---\ntags: [active]\nupdated: {now:%Y-%m-%d}\n---\n"
                        f"# Activity {now:%Y-%m-%d}\n\nBack to [[Home]]\n\n")
            f.write(" · ".join(parts) + "\n")
        if new:
            _own(day)
            _home(base, now)
    except Exception as error:
        _warn(error)


def _jobs(base: Path) -> list[tuple[Path, dict]]:
    out = []
    for p in (base / "Jobs").glob("*.md") if (base / "Jobs").is_dir() else []:
        try:
            out.append((p, _split(p.read_text(encoding="utf-8", errors="replace"))[0]))
        except OSError:
            continue
    return out


def statuses() -> dict[str, str]:
    """{fingerprint: status} of every Jobs note. Never raises."""
    base = root()
    if not base:
        return {}
    try:
        return {m["fp"]: m.get("status", "") for _, m in _jobs(base) if m.get("fp")}
    except Exception as error:
        _warn(error)
        return {}


def _home(base: Path, now: datetime) -> None:
    # ponytail: rereads every Jobs note; rebuilt only on a fit, an outcome or a new day. Keep an index
    # file if Jobs/ ever reaches tens of thousands of notes.
    jobs = _jobs(base)
    count = {s: sum(m.get("status") == s for _, m in jobs) for s in ORDER}
    row = lambda p, m: f"- [[Jobs/{p.stem}]] · {m.get('status', '?')} · fit {m.get('fit', '?')}"  # noqa: E731
    active = sorted(((p, m) for p, m in jobs if m.get("status") in ("offer", "interview", "applied")),
                    key=lambda pm: ORDER.index(pm[1]["status"]))
    fresh = sorted(((p, m) for p, m in jobs if m.get("status") == "drafted"),
                   key=lambda pm: pm[1].get("updated", ""), reverse=True)[:10]
    days = sorted((base / "Activity").glob("*.md"), reverse=True)[:7]
    companies = sorted((base / "Companies").glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:10] \
        if (base / "Companies").is_dir() else []
    links = lambda paths, folder: "\n".join(f"- [[{folder}/{p.stem}]]" for p in paths) or "- (none yet)"  # noqa: E731
    _write(base / "Home.md", f"""---
tags: [active]
updated: {now:%Y-%m-%d}
---
# Job Hunter

The NAS job watcher (@jameskoh_jobhunter_bot, James Channel → Job topic 2574). It writes what it did here
and reads it back before each Claude call, so it knows what you applied to and never re-alerts a posting.

- `Activity/` one note per day, one line per event (SGT)
- `Jobs/` one note per judged posting: fit, why, link, status history
- `Companies/` one note per company: its postings and outcomes

## Pipeline
{" · ".join(f"{s} **{n}**" for s, n in count.items())}

## Active applications
{chr(10).join(row(p, m) for p, m in active) or "- (none yet)"}

## Latest fits, not applied yet
{chr(10).join(row(p, m) for p, m in fresh) or "- (none yet)"}

## Latest activity
{links(days, "Activity")}

## Recent companies
{links(companies, "Companies")}
""")


def _entity_text(base: Path, link: str) -> str:
    """One entity note as one line: summary, then its History newest first, capped."""
    path = base / f"{link}.md"
    if not path.exists():
        return ""
    meta, body = _split(path.read_text(encoding="utf-8", errors="replace"))
    head, _, hist = body.partition("\n## History\n")
    keep = [l.strip() for l in head.splitlines()[1:] if l.strip() and l.strip() != "Back to [[Home]]"]
    status = f"status {meta['status']}: " if meta.get("status") else ""
    text = f"[{link}] {status}" + " | ".join(keep + list(reversed(hist.strip().splitlines())))
    return _one_line(text, ENTITY_CHARS)


def memory(links=(), max_chars: int = MEMORY_CHARS, days: int = MEMORY_DAYS) -> str:
    """The notes in `links` (Jobs/…, Companies/…), then recent Activity lines newest first; capped at
    max_chars on a line boundary. Never raises."""
    base = root()
    if not base:
        return ""
    try:
        out = [t for t in (_entity_text(base, l) for l in dict.fromkeys(links)) if t]
        for day in sorted((base / "Activity").glob("*.md"), reverse=True)[:days]:
            events = [l for l in day.read_text(encoding="utf-8", errors="replace").splitlines() if l.startswith("- ")]
            out += [f"{day.stem} {l[2:]}" for l in reversed(events)]
        text = "\n".join(out)
        return text if len(text) <= max_chars else text[:max_chars].rsplit("\n", 1)[0]
    except Exception as error:
        _warn(error)
        return ""


def mentioned(text: str) -> list[str]:
    """Companies notes whose name appears in `text` (for /jobask). Never raises."""
    base = root()
    try:
        folder = base / "Companies" if base else None
        low = text.lower()
        return [f"Companies/{p.stem}" for p in (folder.glob("*.md") if folder and folder.is_dir() else [])
                if len(p.stem) >= 3 and p.stem.lower() in low]
    except Exception as error:
        _warn(error)
        return []
