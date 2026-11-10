"""정보나루 도서별 이용 분석 원본(usage.jsonl)으로 ③ 의 함께 대출·마니아 그래프 파일을 만든다.

    python -m app.jobs.build_reading_graph <usage.jsonl> <나올 파일.json.gz>

규칙은 app/chat/reading_graph.py 에 있다(순서 점수 없음, 방향 살림, 함께 대출·마니아 따로, 판본은 작품 하나, 다독자 안 씀).
DB 는 읽지 않는다. 우리 서점에 있는 책인지는 서버가 추천할 때 책 표로 본다.
"""

import gzip
import json
import statistics
import sys
from pathlib import Path

from app.chat import reading_graph


def main(source: str, target: str) -> None:
    with open(source, encoding="utf-8") as f:
        data = reading_graph.build(f)
    out = Path(target)
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    graph = reading_graph.from_data(data)
    for kind in reading_graph.KINDS:
        sizes = [len(ts) for ts in graph.lists[kind].values()]
        print(
            f"{kind}: 목록 있는 작품 {len(sizes):,}, 이웃 중앙 {statistics.median(sizes) if sizes else 0}"
            f", 최대 {max(sizes, default=0)}"
        )
    print(
        f"작품 {len(graph.keys):,}, ISBN {len(graph.by_isbn):,}, 허브 {len(graph.hubs)}, "
        f"파일 {out.stat().st_size / 1e6:.1f}MB"
    )
    top = sorted(graph.hubs, key=lambda w: -graph.listed["co"][w])[:8]
    print("허브 예:", ", ".join(graph.keys[w].split("|")[0] for w in top))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
