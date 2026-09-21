"""pgvector 에 벡터를 넘길 때 쓰는 글자 표기. 전용 라이브러리 없이 `$1::vector` 로 넣는다."""


def to_vector_literal(vector: list[float]) -> str:
    """[0.1, 0.2] → '[0.1,0.2]'"""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"
