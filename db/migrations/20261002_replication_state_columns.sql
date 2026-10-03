-- BE MySQL 증분 복제 상태와 소프트 삭제·주문 취소 정보를 보존한다.
-- 기존 v_* 행과 애플리케이션의 짧은 INSERT 문을 깨지 않도록 새 컬럼에는 기본값을 둔다.

ALTER TABLE v_books
    ADD COLUMN IF NOT EXISTS isbn text,
    ADD COLUMN IF NOT EXISTS published_at date,
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT to_timestamp(0),
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

ALTER TABLE v_products
    ADD COLUMN IF NOT EXISTS sale_price numeric(19, 2),
    ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'ACTIVE',
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT to_timestamp(0),
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

ALTER TABLE v_user_purchases
    ADD COLUMN IF NOT EXISTS order_item_id bigint,
    ADD COLUMN IF NOT EXISTS order_id bigint,
    ADD COLUMN IF NOT EXISTS product_id bigint,
    ADD COLUMN IF NOT EXISTS quantity integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS canceled_quantity integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS order_status text NOT NULL DEFAULT 'PAID',
    ADD COLUMN IF NOT EXISTS canceled_at timestamptz,
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

ALTER TABLE v_user_reviews
    ADD COLUMN IF NOT EXISTS review_id bigint,
    ADD COLUMN IF NOT EXISTS order_item_id bigint,
    ADD COLUMN IF NOT EXISTS active_flag boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz,
    ADD COLUMN IF NOT EXISTS updated_at timestamptz;

UPDATE v_user_reviews
SET updated_at = created_at
WHERE updated_at IS NULL;

ALTER TABLE v_user_reviews
    ALTER COLUMN updated_at SET NOT NULL,
    ALTER COLUMN updated_at SET DEFAULT to_timestamp(0);

-- PostgreSQL UNIQUE는 NULL을 서로 다른 값으로 취급하므로 조건부 색인이 필요 없다.
-- 비조건부여야 복제기의 ON CONFLICT (column)이 이 색인을 추론할 수 있다.
CREATE UNIQUE INDEX IF NOT EXISTS v_user_purchases_order_item_uniq
    ON v_user_purchases (order_item_id);

CREATE UNIQUE INDEX IF NOT EXISTS v_user_reviews_review_uniq
    ON v_user_reviews (review_id);

CREATE INDEX IF NOT EXISTS v_user_purchases_active_user_idx
    ON v_user_purchases (user_id, purchased_at DESC, book_id)
    WHERE deleted_at IS NULL
      AND order_status IN ('PAID', 'PARTIAL_CANCELED')
      AND quantity > canceled_quantity;

CREATE INDEX IF NOT EXISTS v_user_reviews_active_user_idx
    ON v_user_reviews (user_id, created_at DESC, book_id)
    WHERE active_flag AND deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS v_books_active_newest_idx
    ON v_books (pub_year DESC NULLS LAST, book_id)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS v_products_active_book_price_idx
    ON v_products (book_id, discounted_price)
    WHERE deleted_at IS NULL;

-- 임베딩 원문이 바뀐 책만 벡터를 무효화한다. 가격·재고·updated_at 변경은
-- 임베딩 내용에 영향을 주지 않으므로 기존 벡터를 보존한다.
CREATE OR REPLACE FUNCTION invalidate_changed_book_embedding()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.title IS DISTINCT FROM NEW.title
       OR OLD.author IS DISTINCT FROM NEW.author
       OR OLD.description IS DISTINCT FROM NEW.description
       OR OLD.publisher IS DISTINCT FROM NEW.publisher
       OR OLD.category IS DISTINCT FROM NEW.category
       OR OLD.deleted_at IS DISTINCT FROM NEW.deleted_at THEN
        DELETE FROM book_embeddings WHERE book_id = NEW.book_id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS v_books_invalidate_embedding ON v_books;
CREATE TRIGGER v_books_invalidate_embedding
AFTER UPDATE OF title, author, description, publisher, category, deleted_at
ON v_books
FOR EACH ROW
EXECUTE FUNCTION invalidate_changed_book_embedding();

CREATE TABLE IF NOT EXISTS replication_state (
    name text PRIMARY KEY,
    cursor_value timestamptz NOT NULL,
    cursor_id bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE replication_state
    ADD COLUMN IF NOT EXISTS cursor_id bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

CREATE TABLE IF NOT EXISTS replication_status (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    last_started_at timestamptz,
    last_succeeded_at timestamptz,
    last_error text
);
