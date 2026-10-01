"""PC-side watchdog for the NAS job watcher. Run it every 15 minutes from Windows Task Scheduler:

    python build/watchdog.py

It reads the NAS state file that Syncthing copies to this PC and messages Telegram once when the
watcher goes quiet, and once more when it recovers. It judges two things separately:

- heartbeat: the NAS loop rewrites it every HEARTBEAT_MIN (30) minutes, even when there is nothing
  new. Stale means the container, the NAS or Syncthing is down.
- last_cycle: a finished sweep, due every SWEEP_INTERVAL_HOURS (6). A fresh heartbeat with an old
  cycle means the loop is alive but sweeps are stuck or failing.

Env (or a .env next to build/): JOB_STATE (default build/.nas_state.json), WATCHDOG_MAX_H (2),
SWEEP_INTERVAL_HOURS (6), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_THREAD_ID.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tg import esc, head, telegram  # noqa: E402  (the watcher's one send path, stdlib only)

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            key, sep, val = line.strip().partition("=")
            if sep and not key.startswith("#"):
                os.environ.setdefault(key.strip(), val.strip().strip('"'))


def hours_since(stamp: str | None) -> float | None:
    try:
        return (dt.datetime.now() - dt.datetime.fromisoformat(stamp)).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def problem(state_path: Path, max_h: float, interval_h: float) -> str | None:
    """None when healthy, else one line saying what is wrong."""
    if not state_path.exists():
        return f"state file {state_path} not found (is Syncthing running?)"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"state file unreadable: {exc}"
    # Before the heartbeat existed, the file's age was the only signal; keep it as the fallback.
    beat = hours_since(state.get("heartbeat"))
    if beat is None:
        beat = (dt.datetime.now().timestamp() - state_path.stat().st_mtime) / 3600
    if beat > max_h:
        return (f"the NAS last reported {beat:.1f}h ago. Check Docker → job-hunter (Log tab) on the NAS, "
                f"and that Syncthing is running.")
    cycle = hours_since(state.get("last_cycle"))
    if cycle is not None and cycle > 2 * interval_h + 1:
        return (f"alive, but the last finished sweep was {cycle:.1f}h ago (due every {interval_h:g}h). "
                f"Send /jobstatus to the bot or check the Log tab.")
    err = state.get("last_error") or {}
    if err and (hours_since(err.get("at")) or 99) < max_h:
        return f"the last sweep crashed: {err.get('error', '')[-300:]}"
    return None


def main() -> int:
    load_env()
    state_path = Path(os.environ.get("JOB_STATE", ROOT / "build" / ".nas_state.json"))
    # One alert per outage. Kept out of the Syncthing folder so it never travels to the NAS.
    marker = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "job-hunter-watchdog.alerted"
    issue = problem(state_path, float(os.environ.get("WATCHDOG_MAX_H", "2")),
                    float(os.environ.get("SWEEP_INTERVAL_HOURS", "6")))
    if issue and not marker.exists():
        telegram(head("down", "from the PC watchdog") + "\n\n🔴 " + esc(issue))
        marker.write_text(issue, encoding="utf-8")
    elif not issue and marker.exists():
        telegram(head("back", "from the PC watchdog") + "\n\n🟢 Job watcher is reporting again.")
        marker.unlink()
    print(issue or "ok")
    return 1 if issue else 0


if __name__ == "__main__":
    sys.exit(main())
