"""V3 격자 판정표(B4)와 옮긴 판정(evals/search/verdict.py)을 칸마다 견준다(#752). 한 번 쓰는 코드라 이 브랜치에만 둔다.

    uv run python -m evals.search.repro_752 <results 폴더> <V3 labels.json> <B4 격자 판정표.md>

results 폴더에는 base/base-screen.json 과 b4/<안>.json 이 있어야 한다. 라벨은 V3 판정 때의 1,417개다.
"""

import json
import sys
from pathlib import Path

from evals.search import verdict

results = Path(sys.argv[1])
labels_path = Path(sys.argv[2])
table = Path(sys.argv[3])
labels = json.loads(labels_path.read_text(encoding="utf-8"))["labels"]
rows = {}
lines = table.read_text(encoding="utf-8").splitlines()
head = [c.strip() for c in lines[0].strip("|").split("|")]
for line in lines[2:]:
    if not line.startswith("| "):
        continue
    cells = [c.strip() for c in line.strip("|").split("|")]
    rows[cells[0]] = dict(zip(head, cells))
base = verdict.load(results / "base" / "base-screen.json", labels)
same = mismatched = 0
tally = {}
for name, want in sorted(rows.items()):
    path = results / "b4" / f"{name}.json"
    got = verdict.compare(base, verdict.load(path, labels))
    cells = {x.name: f"{verdict._value(x.new)} ({x.verdict})" for x in got.lines}
    bad = [(k, want.get(k), v) for k, v in cells.items() if want.get(k) != v]
    if got.verdict != want["판정"]:
        bad.append(("판정", want["판정"], got.verdict))
    if str(got.changed.get("descriptive", 0)) != want["바뀐 서술형"]:
        bad.append(("바뀐 서술형", want["바뀐 서술형"], got.changed.get("descriptive")))
    tally[got.verdict] = tally.get(got.verdict, 0) + 1
    if bad:
        mismatched += 1
        print("MISMATCH", name, bad[:4])
    else:
        same += 1
print(f"rows {len(rows)}, same {same}, mismatched {mismatched}, verdicts {tally}")
