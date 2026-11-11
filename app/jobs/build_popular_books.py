"""정보나루 도서별 이용 분석 원본(usage.jsonl)으로 ③ 의 인기 책 파일을 만든다(#514).

    python -m app.jobs.build_popular_books <usage.jsonl> [<usage.jsonl> ...] <나올 파일.json.gz>

함께 대출 그래프(app/jobs/build_reading_graph.py)와 같은 원본이다. 여러 번 나눠 받았으면 파일을 다 넣는다(같은 ISBN 은 뒤의 것).
규칙은 app/chat/popular.py 에 있다. 파일에는 ISBN 과 꼬리표만 대출 많은 순서로 담고, 대출 건수·정보나루 낱말은 담지 않는다.
DB 는 읽지 않는다. 우리 서점에 있는 책인지는 서버가 추천할 때 책 표로 본다.
"""

import contextlib
import gzip
import itertools
import json
import sys
from collections import Counter
from pathlib import Path

from app.chat import popular


def main(sources: list[str], target: str) -> None:
    with contextlib.ExitStack() as stack:
        files = [stack.enter_context(open(s, encoding="utf-8")) for s in sources]
        data = popular.build(itertools.chain.from_iterable(files))
    out = Path(target)
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    index = popular.from_data(data)
    kinds = Counter(e.kind for e in index.entries)
    subgenres = Counter(s for e in index.entries for s in e.subgenres)
    print(f"책 {len(index.entries):,}권, 파일 {out.stat().st_size / 1e6:.1f}MB")
    print("분야:", ", ".join(f"{k} {n:,}" for k, n in kinds.most_common()))
    print(
        "소설 세부 장르:", ", ".join(f"{k} {n:,}" for k, n in subgenres.most_common())
    )


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(sys.argv[1:-1], sys.argv[-1])
