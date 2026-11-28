"""
Golden traces: today's behaviour must not change when the drives and the
vendor equipment are refactored (docs/DRIVE_INTERFACE_DESIGN.md, section 4;
docs/EQUIPMENT_INTERFACE_DESIGN.md, section 8).  Scenarios live in
tests/golden/scenarios.py; the recorded traces in tests/golden/*.json.gz.
"""

import math

import pytest

from tests.golden.scenarios import SCENARIOS, load


def _diff(a, b, path=""):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        if not math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6):
            return f"{path}: recorded {a!r}, now {b!r}"
        return None
    if a != b:
        return f"{path}: recorded {a!r}, now {b!r}"
    return None


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_golden_trace(name):
    recorded = load(name)
    now = SCENARIOS[name]()
    assert len(now) == len(recorded), f"{name}: {len(recorded)} snapshots recorded, {len(now)} now"
    problems = []
    for i, (r, n) in enumerate(zip(recorded, now)):
        missing = set(r) - set(n)
        if missing:
            problems.append(f"snapshot {i} ({r['tag']}): signals gone {sorted(missing)[:5]}")
        for k in r:
            if k in n:
                d = _diff(r[k], n[k], f"snapshot {i} t={r['t']} ({r['tag']}) {k}")
                if d:
                    problems.append(d)
        if len(problems) > 20:
            break
    assert not problems, "\n".join(problems[:20])
