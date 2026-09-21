"""인기 점수 — v_book_popularity 를 읽는 모든 곳이 같은 식을 쓰게 한 곳에 둔다.

① 검색의 인기순, ③ 챗봇 후보 채점, ④ 피드의 인기 항, 신규 사용자용 목록이 대상이다.
각자 다른 식을 쓰면 화면마다 인기 순위가 달라진다.

    인기 점수 = ln(1 + 판매 수) + (보정 평점 − 전체 평균 평점)
    보정 평점 = (전체 평균 × PRIOR_REVIEWS + 이 책의 평점 × 리뷰 수) ÷ (PRIOR_REVIEWS + 리뷰 수)

- 보정 평점(베이즈 평균): 모든 책이 평균 별점짜리 리뷰를 PRIOR_REVIEWS 개 갖고 시작한다고 친다.
  리뷰가 적으면 평균 쪽으로 끌려가 몇 명의 별점 테러·자작 리뷰에 거의 안 움직이고,
  리뷰가 많으면 실제 평점이 그대로 반영된다.
- 판매 수는 심하게 쏠려 있어 로그를 씌운다. 이 식에서 별 1개 차이 = 판매량 약 2.7배 차이다.
- 리뷰가 없으면 평점 항이 0, 행이 없는 책은 0점이다(명세).

이 계산은 **임시다.** 2026-09-21 BE 와 합의: 판매·리뷰는 원래 BE 데이터라 점수 계산도 BE 가
맡고, AI 는 복제 테이블로 받은 점수를 쓴다. 다만 지금 v_book_popularity 에는 점수 칼럼이 없어
(판매 수·평균 평점·리뷰 수뿐) 그때까지 여기서 계산한다. 칼럼이 생기면 이 파일을 지우고 그
칼럼을 읽는다 — 그전까지 식을 바꾸면 BE 쪽 값과 어긋나므로 먼저 합의한다.
"""

# 데이터 없이 정한 값이다. BE 집계가 들어오면 실제 분포를 보고 조정한다.
PRIOR_REVIEWS = 10

# 전체 평균 평점. 리뷰가 하나도 없으면 0 으로 두는데, 그때는 모든 책의 평점 항이 0 이라 값이 무엇이든 같다.
_MEAN_SQL = (
    "(SELECT coalesce(sum(rating_avg * rating_count) / nullif(sum(rating_count), 0), 0)"
    " FROM v_book_popularity WHERE rating_avg IS NOT NULL)"
)


def score_sql(alias: str = "p") -> str:
    """인기 점수를 계산하는 SQL 식. `alias` 는 LEFT JOIN 한 v_book_popularity 의 별칭이다.

    행이 없는 책(LEFT JOIN 결과가 NULL)은 0 이 된다.

    위 식에서 (보정 평점 − 전체 평균) 부분은 아래와 같이 정리된다.

        (C·m + r·n) / (C + n) − m  =  n·(r − m) / (C + n)     (C=PRIOR_REVIEWS, m=전체 평균)

    정리한 쪽을 쓰는 이유는 전체 평균이 한 번만 나오기 때문이다. 양쪽에 두면 같은 값인데도
    PostgreSQL 이 InitPlan 을 두 개 만들어 v_book_popularity 전체 집계를 쿼리마다 두 번 돈다
    (13만 행 기준 한 번에 8ms 안팎). 검색·챗봇·피드가 자주 부르는 식이라 한 번으로 줄인다.
    """
    a = alias
    return (
        f"coalesce(ln(1 + {a}.sales)"
        f" + {a}.rating_count * (coalesce({a}.rating_avg, 0) - {_MEAN_SQL})"
        f" / ({PRIOR_REVIEWS} + {a}.rating_count), 0)"
    )
