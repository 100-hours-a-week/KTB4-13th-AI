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
    """
    a = alias
    return (
        f"coalesce(ln(1 + {a}.sales)"
        f" + ({PRIOR_REVIEWS} * {_MEAN_SQL} + coalesce({a}.rating_avg, 0) * {a}.rating_count)"
        f" / ({PRIOR_REVIEWS} + {a}.rating_count) - {_MEAN_SQL}, 0)"
    )
