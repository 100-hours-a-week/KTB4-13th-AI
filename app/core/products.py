"""책의 가격·재고 — 상품 표(v_products)에서 읽는 모든 곳이 같은 식을 쓰게 한 곳에 둔다(#208).

BE 는 가격·재고를 책이 아니라 상품에 둔다. 책 하나에 상품이 여럿일 수 있어(지금 데이터는 1:1)
재고 있는 상품 중 가장 싼 것 하나를 고른다. 책마다 값 하나만 나오므로 JOIN 과 달리 같은 책이
두 번 나오지 않는다. 상품이 없는 책은 가격 null, 품절이다.

- 가격: 할인 적용 후 판매가(discounted_price)를 원 단위 정수로 바꾼다(명세의 price 는 int).
- 재고: 재고 수량이 1 이상이면 있음. BE 상품 화면과 같은 기준이다.
"""

# 책마다 상품 하나를 고르는 하위 쿼리. 가격과 재고가 같은 상품에서 나오도록 순서를 같게 둔다.
_PICK = (
    "(SELECT {expr} FROM v_products pr WHERE pr.book_id = {alias}.book_id"
    " ORDER BY pr.stock_quantity > 0 DESC, pr.discounted_price LIMIT 1)"
)


def price_sql(alias: str = "b") -> str:
    """책의 판매가(원, int) SQL 식. `alias` 는 v_books 의 별칭이다. 상품이 없으면 NULL."""
    return _PICK.format(expr="pr.discounted_price::int", alias=alias)


def in_stock_sql(alias: str = "b") -> str:
    """책의 재고 여부(bool) SQL 식. 상품이 없으면 false."""
    return f"coalesce({_PICK.format(expr='pr.stock_quantity > 0', alias=alias)}, false)"
