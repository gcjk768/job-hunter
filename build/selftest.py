"""Quick integrity checks for the sweep. Run before trusting a result.

    python build/selftest.py

Exists because the same bug bit twice: regexes written through a shell heredoc had their `\\b`
word-boundaries collapse into literal backspace characters (0x08), so PUBLIC_SECTOR silently matched
nothing and REMOTE_NO silently let US-only roles through. Both looked like working filters. A filter
that silently matches nothing is worse than no filter, so these assertions check behaviour, not
just that the file parses.
"""

from __future__ import annotations

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
