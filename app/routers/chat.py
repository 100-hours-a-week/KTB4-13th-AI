"""③ 대화형 도서 추천: POST /recommendations/chat

명세: docs/wiki/ai/1-model-api/spec.md ③ POST /recommendations/chat
텍스트 턴과 이미지 턴을 여기서 함께 다룬다 — 같은 핸들러가 후보검색·카드생성
파이프라인을 공유하므로 나누지 않는다(개발 워크플로 위키 §9 참고).

처리를 명세 4단계 그대로 함수로 나눴다. 1·3단계는 LLM 체인이고, 2단계는 ①의
하이브리드 검색을 쓴다(아래 각 함수 docstring 참고). 바깥 흐름(이 4단계가 이 순서로
불리는 것)은 안 바뀐다.

같은 처리를 세 가지 방식으로 받는다. 기본은 JSON 한 번이다. `Accept: text/event-stream` 이면 말풍선을 카드보다
먼저 흘려보내고(SSE, app/chat/streaming.py), `/chat/jobs` 로 맡기면 job_id 를 바로 주고 결과는 따로 묻게 한다
(폴링, app/chat/jobs.py). 응답을 만드는 갈래는 셋이 같은 함수(_respond)를 쓴다.
"""

import asyncio
import json
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from langchain_core.prompts import ChatPromptTemplate
from pydantic import ValidationError

from app.chat import (
    asking,
    invented_mood,
    jobs,
    lanes,
    picking,
    reading_graph,
    reasons,
    recall,
    shown,
    streaming,
    topic_switch,
)
from app.chat import options as option_lines
from app.chat.copying import COPIED_SHARE_MIN, copied_share
from app.chat.schemas import ChatRequest, Spec, SpecExact, Turn
from app.chat.streaming import ReplySink
from app.chat.topic_switch import RELATION_PROMPT
from app.core import body, categories, db, editions, isbn, popularity, responses, voice
from app.core.config import get_settings
from app.core.pgvector import to_vector_literal
from app.core.turns import MAX_RECENT_TURNS
from app.feed import interest_scoring, personalized, scoring
from app.gate import judge as gate_judge
from app.gate import rules as gate_rules
from app.gate import shopping
from app.gateway import decision, embedding, reranker
from app.gateway.llm import (
    LLMUnavailableError,
    get_chat_model,
    invoke_chain,
    message_text,
    parse_json_response,
    stream_json,
)
from app.routers import agent
from app.search import service
from app.search.schemas import MAX_QUERY_CHARS, SearchFilters, SearchRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recommendations", tags=["recommendations"])

CANDIDATE_LIMIT = 10
# 새 요청 판정은 1단계와 나란히 가서 보통 시간이 안 늘어난다. 판정만 느려져도 응답이 그만큼 늦어지지 않게, 1단계가
# 끝난 뒤 이만큼만 더 기다리고 못 받으면 이어가는 말로 본다(#513). 업계도 다시 쓰기 호출에 0.3~1초 한도를 둔다.
RELATION_GRACE_SECONDS = 0.5
# 넓은 첫 요청 판정(#530)도 1단계와 나란히 간다. 넓으면 검색·카드 생성을 건너뛰어 그보다 훨씬 빨리 끝나므로, 새 요청
# 판정보다 길게 기다린다. 판정은 질문 문장까지 쓰느라 1단계보다 늦게 끝나기도 한다.
SCOPE_GRACE_SECONDS = 2.0
# ①에 없는 exclude, 소개글 없는 책을 검색 결과에서 사후 필터링하므로, 걸러지고도
# CANDIDATE_LIMIT이 남을 만큼 넉넉히 받는다. SearchRequest.size 상한(50) 안쪽.
SEARCH_SIZE = 30

# 명세 에러표의 413은 (V2) 이미지 10MB 상한이라 V1(텍스트 턴)엔 숫자가 없다(#99).
# message(200자)·recent_turns(20턴)는 작지만, exclude_isbns처럼 길이 상한이
# 없는 배열도 있어 상한 없이 통째로 메모리에 올리면 거절 자체가 공격 수단이 될
# 수 있다. 정상 요청이면 절대 안 닿을 만큼 넉넉하게 잡는다.
MAX_BODY_BYTES = 64 * 1024

# 출판사 비교용 잡음 — "(주)"·"주식회사"·공백. 카탈로그 표기가 "(주)현암사",
# "현암주니어 :현암사"처럼 들쭉날쭉해, 이걸 지우고 포함 관계로 봐야 같은
# 출판사가 표기 차이로 조용히 빠지지 않는다(리뷰 지적).
_PUBLISHER_NOISE = re.compile(r"\(주\)|주식회사|\s+")


def _normalize_publisher(name: str) -> str:
    return _PUBLISHER_NOISE.sub("", name)


# ③ 전용 채점 비중(#222) — ④(app/feed/scoring.py)와 다른 식을 쓴다. 명세 각주: "이번
# 요청 조건(spec.semantic) 유사도와 취향 유사도를 함께 쓴다. ④의 계산식과 다르다."
# 지금까지는 ④와 똑같은 식(취향 0.6·카테고리 0.25·인기 0.15)을 그대로 갖다 썼는데,
# 그러면 사용자가 지금 딱 찾는 조건에 맞는 책이 나와도 "평소 취향과 다르다"는
# 이유로 점수가 낮게 뜬다. 질의 유사도를 메인으로 두고 취향은 동점 보정 정도로
# 낮춘다. 숫자는 ④의 기존 비중처럼 임시값이다 — 시드 사용자로 보고 조정한다.
QUERY_SIMILARITY_WEIGHT = 0.5
TASTE_SIMILARITY_WEIGHT = 0.2
CHAT_CATEGORY_WEIGHT = 0.2
CHAT_POPULARITY_WEIGHT = 0.1


CARD_LIMIT = 3


def parse_request(payload: Any) -> ChatRequest | tuple[int, str]:
    """계약에 맞으면 요청 객체, 아니면 (상태 코드, message) 를 돌려준다.

    spec 필드 자체가 잘못됐으면 422 spec_schema_violation — 명세가 "호출자가
    초기 spec으로 되돌려 1회 재시도"하라고 못박은 전용 에러다. 그 외
    형식 오류(예: message 길이 초과, image_ref와 동시 존재)는 400.
    """
    if not isinstance(payload, dict):
        return (400, "invalid_request")
    try:
        return ChatRequest.model_validate(payload)
    except ValidationError as e:
        # 모델 전체 검증(예: message/image_ref 동시 존재)은 loc이 빈 튜플이라
        # err["loc"][0]을 바로 쓰면 IndexError가 난다 — 실제로 재현된 버그.
        if any(err["loc"] and err["loc"][0] == "spec" for err in e.errors()):
            return (422, "spec_schema_violation")
        return (400, "invalid_request")


def _today_text() -> str:
    """1단계 프롬프트에 싣는 오늘 날짜(서비스 시간대). 날짜가 없으니 모델이 "최근 2~3년"을 2020년 이후로 짐작했다
    (11/11 dev 3번 중 3번, #693). 프롬프트를 채울 때마다 다시 읽는다."""
    today = datetime.now(ZoneInfo(get_settings().tz)).date()
    return f"{today.year}년 {today.month}월 {today.day}일"


# {spec_json}·{message}가 채워지는 자리다. JSON 예시의 중괄호는 {{ }}로 겹쳐 쓴다
# (CARD_PROMPT와 같은 이유 — 실제로 모델에 가는 글자는 겹치기 전과 같다).
#
# 지금까지의 조건 전체를 매번 그대로 옮겨 적게 시켰더니, 안 건드려야 할 값까지
# 예시를 베껴 써서 망가뜨리는 사고가 반복됐다(둘 다 실제 Ollama/qwen2.5:7b로
# 재현): 1) 예시의 false를 그대로 베껴 in_stock_only=true가 매번 false로 바뀜
# 2) intent 자리의 설명 문구("exact 또는 semantic")를 글자 그대로 복사해서
# Spec 검증 실패로 이어짐. 그래서 지금까지의 조건을 다시 옮겨 적게 하는 대신
# 이번 메시지로 "바뀌는 값만" 답하게 하고, 언급 안 된 값은 서버가 지금 조건에서
# 그대로 들고 와 겹쳐 쓴다(_merge_spec_patch) — 모델이 값을 옮겨 적다 틀릴
# 여지 자체를 없앤다. exclude도 같은 이유로 매번 전체 목록을 다시 쓰게 하면
# 모델이 옛 항목을 빠뜨렸을 때 이미 제외했던 책이 되살아난다 — 새로 빼고 싶은
# 것만 받아 서버가 기존 목록에 더한다.
#
# "너무 무겁지 않은 걸로" 같은 분위기·톤 조정 표현은 제목·저자·가격·장르·
# 제외 어디에도 안 맞아 지시문이 없으면 그냥 버려진다(실제 Ollama/qwen2.5:7b로
# 재현, 이슈 #132). exact·filters와 달리 semantic은 문장 하나짜리라 하위 키
# patch가 안 되고, 반영하려면 지금 문장을 바탕으로 전체를 다시 써야 한다 —
# 그래서 "언제 semantic을 통째로 다시 쓰는지"를 별도로 못박아둔다.
#
# "2만원 이하 아니어도돼"처럼 조건을 없애 달라는 말도 지시문이 없으면 그냥
# 버려진다(이슈 #239) — patch에 그 키를 안 넣는 것과 "없애라"를 모델이
# 구분 못 해서, _merge_spec_patch가 "patch에 없으면 기존 값 유지"로 처리해
# 이전 조건이 안 풀리고 눌러앉는다. 그래서 없애는 의도는 그 키를 JSON null로
# 명시하라고 따로 가르친다 — null이 명시되면 _merge_spec_patch가 정상적으로
# 지운다.
#
# 재고 조건(in_stock_only)은 지시문과 예시에 넣지 않는다(#319). AI가 재고를 받지
# 않게 되어(#307) 챗봇은 이 조건을 쓰지 않는다. 예시에 있으면 모델이 사용자가
# 말하지 않은 재고 조건을 지어낸다.
#
# 주제와 조건이 같이 온 메시지("2020년 이후에 나온 인공지능 책")에서 조건만 내고 주제를
# 빠뜨리는 경우가 있었다(#359). 그러면 검색어가 비어 추천을 못 한다. 다른 예시들이 조건
# 하나만 담은 꼴이라 그대로 따라 한 것으로 보고, 주제와 조건을 같이 담은 예시를 하나 둔다.
# 평가(#322)에서 이 질문에 카드가 나온 비율이 10번 중 6번에서 9번으로 올랐다. 예시의
# 낱말은 평가 질문에 없는 것으로 골랐다 — 평가 질문을 예시로 쓰면 그 질문만 맞히게 된다.
#
# 같이 넣어 본 두 가지는 뺐다(#359). "주제는 semantic에 남겨라"는 지시문과 분야 칸에 넣을
# 수 있는 값 목록이다. 넣으면 "책 추천해줘"처럼 주제가 없는 말에도 모델이 분위기를 지어내
# semantic을 채웠다(되묻기 5번 중 4번 → 0번). 목록을 주면 분야 칸을 더 자주 채워, 말하지
# 않은 분야로 후보가 걸러지기도 했다. 못 쓰는 분야 값은 서버가 걸러 낸다(_settle_category).
#
# 마지막으로 보여 준 책({shown})을 넣는다(#530). 그동안 1단계는 보여 준 책을 몰라 "2번 같은 거"를 풀 단서가 말풍선
# 속 제목뿐이었다. 가리킨 책의 제목은 semantic 에 넣지 말라고 한다. 제목이 든 문장으로 검색하면 그 책과 다른
# 판본이 맨 위로 온다(#521·#524에서 본 "기준 책이 카드에 나옴").
#
# 새 책을 찾는 말이 아닌 질문("2번 초보도 읽을 수 있어?", "너는 뭐 할 수 있어?")은 turn 을 answer 로 받는다(#530).
# 지금까지는 이런 말도 검색해 새 책 3권을 냈다(10/19 평가: 질문 4종 모두 새 카드). 호출을 늘리지 않으려고 판단을
# 이 호출에 얹는다.
#
# {ask_rule}은 이번 말이 넓은 첫 요청에 물은 질문의 답일 때 싣는 규칙(app/chat/asking.py)이다. 그 밖의 턴에는 빈 문자열이라
# 프롬프트가 그 전과 같다.
#
# {agent_rule}은 CHAT_AGENT 를 켰을 때만 싣는다(AGENT_RULE). 끄면 빈 문자열이라 프롬프트가 그 전과 같다.
SPEC_PROMPT = ChatPromptTemplate.from_template(
    "너는 책 추천 챗봇의 조건(spec) 갱신기다. 이번 메시지를 보고 조건 중 "
    "실제로 바뀌는 값만 JSON으로 답하라 — 언급되지 않은 값은 답에 아예 "
    "넣지 마라. 지금 조건을 옮겨 적을 필요 없다, 안 넣은 값은 그대로 "
    "유지된다.\n\n"
    "최근 대화(참고용 — 이번 메시지가 가리키는 대상을 파악할 때만 참고하고, "
    "여기 나온 값을 그대로 옮겨 적지 마라):\n{recent_turns}\n\n"
    "마지막으로 보여 준 책(번호는 화면의 카드 순서):\n{shown}\n\n"
    "지금 조건(spec):\n{spec_json}\n\n"
    '이번 메시지: "{message}"\n\n'
    "intent를 이번에 새로 정할 때만 아래 둘 중 하나를 그 낱말 그대로 써라"
    "(설명 문구를 베끼면 안 된다).\n"
    "- exact: 제목이나 저자를 콕 집어 말함\n"
    "- semantic: 분위기나 상황을 말함\n\n"
    "exact·filters는 바뀌는 하위 키만 넣어라 — 예를 들어 가격 상한만 새로 "
    '말했으면 {{"filters": {{"price_max": 20000}}}}처럼 그 키 하나만 담고, '
    "다른 하위 키는 넣지 마라.\n"
    '이번 메시지가 어떤 조건을 없애 달라는 뜻이면(예: "가격 상관없어", '
    '"2만원 이하 아니어도돼", "장르 상관없이 다 보여줘") 그 하위 키 값을 '
    '"JSON null"로 명시해서 넣어라 — 예를 들어 가격 상한을 없애 달라면 '
    '{{"filters": {{"price_max": null}}}}처럼 그 키를 null로 채워라. 그냥 '
    "안 넣으면 이전 값이 그대로 남으니, 없애려면 반드시 null을 명시해야 한다.\n"
    "exclude는 이번에 새로 빼고 싶은 책 id만 넣어라 — 기존 목록은 서버가 "
    "그대로 유지하니 다시 적을 필요 없다.\n"
    '오늘은 {today}이다. 출간 시기를 "최근 N년", "N년 안에", "올해", "작년 이후"처럼 '
    "기간으로 말하면 오늘 연도로 셈해 filters.pub_year_from에 연도를 넣어라 — "
    '"최근 N년"은 오늘 연도에서 N을 뺀 해이고, "2~3년"처럼 범위로 말하면 큰 수로 셈한다. '
    "연도를 짐작하지 마라.\n\n"
    "이번 메시지가 제목·저자·가격·장르·제외처럼 구체적인 조건이 아니라 "
    '"너무 무겁지 않게", "더 재밌는 걸로", "덜 슬프게"처럼 분위기나 톤을 '
    "조정하는 말이면, 지금 semantic 문장을 바탕으로 그 톤을 반영한 "
    "완전한 새 문장을 semantic에 통째로 다시 써라 — 다른 필드처럼 바뀐 "
    "조각만 넣는 게 아니라 문장 전체를 새로 써야 한다.\n"
    '한 메시지에 구체적인 조건과 분위기·톤이 같이 오면(예: "가벼운 걸로, '
    '1만원 이하로") 둘 다 반영해라 — 조건은 해당 필드에, 톤은 semantic에 '
    "각각 넣고 한쪽만 고르지 마라. 아래 마지막 예시가 이 경우다.\n\n"
    "이번 메시지가 보여 준 책을 번호나 지시어로 가리키며 그와 비슷한 책을 원하거나 "
    "그 책의 어떤 점이 안 맞는다고 하면, 그 책 소개를 보고 사용자가 원하는 방향을 "
    "semantic에 통째로 다시 써라. 가리킨 책의 제목은 semantic에 넣지 마라.\n"
    "이번 메시지가 새 책을 찾거나 조건을 바꾸는 말이 아니라 질문이면 — 보여 준 책에 "
    "대해 묻거나(내용, 난이도, 누구에게 맞는지, 실제로 있는 책인지), 보여 준 책 중에서 "
    "골라 달라거나, 챗봇에게 묻거나 인사하는 말이면 — 조건은 넣지 말고 "
    '{{"turn": "answer"}}로만 답하라.\n\n'
    "{ask_rule}"
    "이번 메시지로 바뀌는 게 없으면 빈 객체 {{}}로 답하라. 숫자는 따옴표 "
    "없이 써라. 아래는 형식 예시일 뿐 실제 값이 아니다 — 그대로 베끼지 "
    "말고 실제로 바뀌는 값만 채워라:\n"
    '{{"semantic": "비 오는 날 읽을 잔잔한 책"}}\n\n'
    "톤 조정 예시(형식일 뿐 실제 값 아님) — 지금 semantic이 "
    '"비 오는 날 읽을 책"이고 메시지가 "너무 무겁지 않은 걸로"면:\n'
    '{{"semantic": "비 오는 날 읽을 무겁지 않은 책"}}\n\n'
    "조건+톤 혼합 예시(형식일 뿐 실제 값 아님) — 지금 semantic이 "
    '"비 오는 날 읽을 책"이고 메시지가 "가벼운 걸로, 1만원 이하로"면 '
    "둘 다 넣어라:\n"
    '{{"semantic": "비 오는 날 읽을 가벼운 책", '
    '"filters": {{"price_max": 10000}}}}\n\n'
    "주제+조건 예시(형식일 뿐 실제 값 아님) — 메시지가 "
    '"만오천원 이하 요리책 추천해줘"면 둘 다 넣어라:\n'
    '{{"semantic": "요리책", "filters": {{"price_max": 15000}}}}\n\n'
    "조건 제거 예시(형식일 뿐 실제 값 아님) — 지금 filters.price_max가 "
    '20000이고 메시지가 "2만원 이하 아니어도돼"면, 키를 빼지 말고 null로 '
    "명시해라:\n"
    '{{"filters": {{"price_max": null}}}}\n\n'
    "제목 지정 예시(형식일 뿐 실제 값 아님) — 메시지가 "
    '"토지 찾아줘"면:\n'
    '{{"intent": "exact", "exact": {{"title": "토지"}}}}\n'
    "저자 지정 예시(형식일 뿐 실제 값 아님) — 메시지가 "
    '"박경리 책 보여줘"면:\n'
    '{{"intent": "exact", "exact": {{"author": "박경리"}}}}\n\n'
    "중요: 메시지가 책 제목·작품명·저자 이름뿐이거나 "
    '"~ 찾아줘/보여줘"처럼 특정 책이나 저자를 가리키면 분위기를 지어내지 '
    "말고 intent를 exact로 해라. 제목이면 exact.title에, 저자면 exact.author에 "
    "그 이름을 그대로 넣어라. 메시지에 없는 분위기·톤(가벼운, 잔잔한 등)을 "
    "semantic에 절대 덧붙이지 마라.\n"
    "제목만 말한 예시(형식일 뿐 실제 값 아님) — 메시지가 "
    '"데미안"이면:\n'
    '{{"intent": "exact", "exact": {{"title": "데미안"}}}}'
    # 에이전트 칸은 켰을 때만 채운다. 안 채우면 빈 문자열이라 이 기능 전 프롬프트와 같다
    "{agent_rule}"
).partial(agent_rule="", today=_today_text)

# 책 추천 에이전트(CHAT_AGENT)가 1단계 호출에 더 받는 칸(app/chat/lanes.py). 호출을 늘리지 않으려고 같은 호출에 얹는다.
# - anchor_card·anchor_title·relation: 사용자가 짚은 기준 책과, 그 책에 대해 물은 관계. 함께 읽은 책을 묻는 말을 보여
#   준 책에 대한 질문(turn: answer)으로 읽으면 새 책을 못 찾아, 새 책 찾기라고 따로 적는다. 칸 이름을 "anchor"로 했더니
#   mini 가 spec 의 anchor_book 에 번호를 넣었다
# - 떠올린 제목(titles)은 여기서 받지 않고 따로 부른다(app/chat/recall.py). 같은 호출에서는 4o 가 22권 중 18권을 지어냈다
#   (11/10 실험 1)
# - anchor_title 은 모양과 예시를 적는다. 모양 없이 "그 책"이라고만 적었더니 4o 가 제목으로 짚은 기준 책 6문장 중 2문장은
#   글자로("데미안") 냈고, 4문장은 아예 안 냈다("아몬드 같은 소설"을 semantic 에만 담음, 11/10 실험 1)
# 프롬프트 맨 끝에 둔다. 조건 규칙 사이에 두었더니 "바뀌는 값만 답하라"에 눌려 4o·mini 모두 칸을 내지 않았다(11/10 점검
# 3문장). 맨 끝에 두자 이번엔 mini 가 조건 칸을 빼고 빈 {} 를 냈다("파이썬 처음인데 예제로 따라 하고 싶어" 4번 중 0~1번만
# semantic, 끄면 3/3). 그래서 조건 칸을 먼저 정하고 빼지 말라고 적는다.
# 이 글은 프롬프트 틀이 아니라 채우는 값이라 중괄호를 겹쳐 쓰지 않는다.
AGENT_RULE = (
    "\n\n추가 칸 — 먼저 위 규칙대로 조건 칸(intent, semantic, filters 등)을 정하고, 그 칸들과 함께 아래 칸도 "
    "판단해 같은 JSON 에 더 넣어라. 이 칸들은 조건 칸을 대신하지 않는다. 위에서 바뀌는 값만 넣으라고 했어도 이 칸들은 "
    "해당하면 넣는다. 해당하지 않는 칸만 뺀다. 이 칸들 때문에 위 규칙으로 넣을 semantic·filters 같은 조건 칸을 빼지 "
    "마라.\n"
    "- anchor_card: 이번 메시지가 보여 준 책 하나를 번호·순서·지시어로 짚거나, 그 책을 읽고 이어서 궁금한 것을 말하면 "
    "그 책의 번호(정수). 보여 준 책이 여럿인데 어느 책인지 말하지 않았으면 넣지 않는다. anchor_book 칸에는 번호를 넣지 "
    "마라.\n"
    '- anchor_title: 보여 준 책이 아닌 책의 제목을 말하며 그 책을 기준으로 삼으면 {"title": "그 책 제목", "author": '
    '"저자"} 모양으로 넣는다. "X 읽은 사람들", "X 같은 책", "X처럼", "X 재밌게 읽었는데", "X 끝냈는데"의 X 가 그 책이다. '
    "author 는 확실히 알 때만 넣는다.\n"
    '- relation: 기준 책을 읽은 사람들이 함께(또, 같이) 읽은 책을 물으면 "co", 기준 책과 비슷한 책을 더 원하면'
    '("X 같은", "X처럼", "결이 비슷한") "similar".\n'
    "기준 책을 읽은 사람들이 함께 읽은 책이나 기준 책과 비슷한 책을 찾는 말은 질문이 아니라 새 책을 찾는 말이다. "
    'turn 을 "answer" 로 하지 마라.\n'
    "형식 예시(값은 설명일 뿐 베끼지 마라) — 보여 준 2번 책을 읽은 사람들이 함께 읽은 책을 물으면: "
    '{"anchor_card": 2, "relation": "co"} / 메시지가 "『X』 읽은 사람들은 또 뭐 읽어?"면: '
    '{"anchor_title": {"title": "X"}, "relation": "co"} / 메시지가 "X처럼 따뜻한 소설"이면: '
    '{"semantic": "X처럼 따뜻한 소설", "anchor_title": {"title": "X"}, "relation": "similar"}'
)


def _agent_on() -> bool:
    return get_settings().chat_agent


def _format_recent_turns(turns: list[Turn]) -> str:
    """최근 대화를 "user: …" 줄로 바꾼다. 없으면 빈 대화임을 알리는 문구.

    명세: 최대 20턴, 초과분은 최근 20턴만 사용 — 뒤에서 MAX_RECENT_TURNS개만 자른다.
    """
    if not turns:
        return "(최근 대화 없음)"
    return "\n".join(f"{t.role}: {t.text}" for t in turns[-MAX_RECENT_TURNS:])


def _clear_stale_exact(merged_exact: dict, sub_patch: dict) -> None:
    """새 제목을 찾을 때 이전 턴의 저자·출판사가 남으면 엉뚱한 책이 되거나 0건이 된다(#203).

    title이 이번 patch에 새로 오면, 같이 안 온 author·publisher는 이전 턴 값을
    지운다 — 다른 책을 찾는 것이니 이전 저자·출판사는 이제 안 맞다. 같은 patch에
    author·publisher도 같이 왔으면(예: "이 저자의 이 책") 그 값은 그대로 쓴다.
    """
    if "title" not in sub_patch:
        return
    if "author" not in sub_patch:
        merged_exact["author"] = None
    if "publisher" not in sub_patch:
        merged_exact["publisher"] = None


def _clear_inverted_range(
    merged_filters: dict, sub_patch: dict, low: str, high: str
) -> None:
    """가격·연도 구간의 한쪽만 새로 오면, 반대쪽에 남아 있던 이전 값과 뒤집힐 수 있다(#203).

    뒤집힌 채로 두면 Spec 검증(SearchFilters._ranges_are_ordered)이 실패해
    update_spec이 이걸 LLM 장애와 똑같이 spec_degraded로 처리한다 — LLM은
    멀쩡히 답했는데 병합 로직 때문에 대화가 막힌다. 이번에 안 건드린 반대쪽이
    새 값과 뒤집히면, 그 반대쪽은 더 이상 유효하지 않은 이전 조건이므로 지운다.
    """
    low_given, high_given = low in sub_patch, high in sub_patch
    if low_given and not high_given:
        old_high = merged_filters.get(high)
        new_low = merged_filters.get(low)
        if old_high is not None and new_low is not None and new_low > old_high:
            merged_filters[high] = None
    elif high_given and not low_given:
        old_low = merged_filters.get(low)
        new_high = merged_filters.get(high)
        if old_low is not None and new_high is not None and old_low > new_high:
            merged_filters[low] = None


def _merge_spec_patch(current: Spec, patch: dict) -> dict:
    """이번 턴에 LLM이 바꾼 값만 담긴 patch를 지금 spec 위에 겹쳐 완전한 spec dict를 만든다.

    patch에 없는 키(하위 키 포함)는 지금 값을 그대로 두고, 있는 키만
    덮어쓴다. exclude는 겹쳐 쓰지 않고 더한다 — 모델이 옛 항목을 안 실어도
    사라지지 않는다. 단순 겹쳐쓰기만으로는 안 맞는 두 경우(#203)는 겹쳐쓴
    뒤 따로 손본다 — 새 제목에 이전 저자·출판사가 남는 경우, 구간 한쪽만
    새로 와 반대쪽과 뒤집히는 경우.
    """
    merged = current.model_dump()
    for key in ("exact", "filters"):
        sub_patch = patch.get(key)
        if isinstance(sub_patch, dict):
            merged[key] = {**merged[key], **sub_patch}

    exact_patch = patch.get("exact")
    if isinstance(exact_patch, dict):
        _clear_stale_exact(merged["exact"], exact_patch)

    filters_patch = patch.get("filters")
    if isinstance(filters_patch, dict):
        _clear_inverted_range(
            merged["filters"], filters_patch, "price_min", "price_max"
        )
        _clear_inverted_range(
            merged["filters"], filters_patch, "pub_year_from", "pub_year_to"
        )

    # 모델은 책 id 를 모른다. "첫 번째는 별로야"에 제목이나 순번 같은 글자를 넣으면 Spec 검증(정수 목록)이 깨져
    # LLM 은 멀쩡한데도 degraded 가 된다(#513, 평가 92번). 정수만 받고 나머지는 버린다.
    new_exclude = patch.get("exclude")
    if isinstance(new_exclude, list):
        merged["exclude"] = merged["exclude"] + [
            book_id
            for book_id in new_exclude
            if isinstance(book_id, int)
            and not isinstance(book_id, bool)
            and book_id not in merged["exclude"]
        ]

    for key in ("intent", "semantic", "anchor_book"):
        if key in patch:
            merged[key] = patch[key]

    # LLM이 제목·저자만 채우고 intent를 빠뜨리면 semantic으로 남아 소개글 없는 책이 버려진다(#281).
    # intent를 직접 준 경우는 LLM 판단을 따른다.
    if (
        "intent" not in patch
        and isinstance(exact_patch, dict)
        and (exact_patch.get("title") or exact_patch.get("author"))
    ):
        merged["intent"] = "exact"

    # 분위기 요청(semantic)에서는 제목·저자가 검색에 쓰이지 않는다. 앞 턴의 제목이 응답 spec 에 남아 다음 턴으로
    # 이어지지 않게, 이번 patch 가 고르지 않은 제목·저자는 비운다(#513, 평가 104번 7턴). 출판사는 분위기 요청에도
    # 걸 수 있어 남긴다. 이번 patch 가 직접 고른 제목은 그대로 둔다(intent 를 직접 준 경우는 LLM 판단을 따른다).
    if merged["intent"] == "semantic":
        given = exact_patch if isinstance(exact_patch, dict) else {}
        for key in ("title", "author"):
            if key not in given:
                merged["exact"][key] = None

    return merged


async def _ask_spec_patch(
    message: str,
    spec: Spec,
    recent_turns: list[Turn],
    shown_listing: str = "(없음)",
    ask_stage: asking.Stage = "none",
) -> dict:
    chain = SPEC_PROMPT | get_chat_model() | message_text | parse_json_response
    return await invoke_chain(
        chain,
        {
            "recent_turns": _format_recent_turns(recent_turns),
            "shown": shown_listing,
            "spec_json": json.dumps(spec.model_dump(), ensure_ascii=False),
            "message": message,
            "ask_rule": asking.rule(ask_stage),
            "agent_rule": AGENT_RULE if _agent_on() else "",
        },
    )


async def _asks_new_request(message: str, previous: str) -> bool:
    """모델이 이전 요청과 이번 메시지만 보고 새 책 찾기라고 판정했는지. 호출이 실패하면 아니라고 본다.

    spec 도 앞 대화도 보여 주지 않는다. 판정만 시킨다. 실패하면 앞 조건을 남기는 쪽, 곧 고치기 전과 같은 동작이다.
    CHAT_JUDGE=jev 면 Jev 에 같은 글로 묻는다(app/gateway/decision.py). 4o 는 1단계가 끝난 뒤 0.5초 안에 못 와 버려지는
    일이 많았다.
    """
    if decision.enabled():
        try:
            picked = await decision.choose(
                state=topic_switch.RELATION_STATE.format(
                    previous=previous, message=message
                ),
                instructions=topic_switch.RELATION_INSTRUCTIONS,
                criteria=topic_switch.RELATION_CRITERIA,
                timeout=get_settings().llm_timeout_seconds,
            )
        except LLMUnavailableError:
            logger.warning("새 요청 판정(Jev) 실패 — 이어가는 말로 본다", exc_info=True)
            return False
        logger.info("새 요청 판정(Jev): %s, 확신 %s", picked.choice, picked.confidence)
        return picked.choice == "new"
    model_id = get_settings().relation_llm_model_id
    model = get_chat_model(model_id=model_id) if model_id else get_chat_model()
    chain = RELATION_PROMPT | model | message_text | parse_json_response
    try:
        answer = await invoke_chain(chain, {"previous": previous, "message": message})
    except LLMUnavailableError:
        logger.warning("새 요청 판정 실패 — 이어가는 말로 본다", exc_info=True)
        return False
    return answer.get("relation") == "new"


async def _relation_within_grace(relation: "asyncio.Task[bool]") -> bool:
    """판정 결과를 받는다. 1단계가 끝난 뒤 RELATION_GRACE_SECONDS 안에 못 받으면 포기하고 아니라고 본다."""
    try:
        return await asyncio.wait_for(relation, timeout=RELATION_GRACE_SECONDS)
    except TimeoutError:
        logger.warning(
            "새 요청 판정이 1단계보다 %.1f초 넘게 늦어 이어가는 말로 본다",
            RELATION_GRACE_SECONDS,
        )
        return False


async def _judge_scope(message: str) -> asking.Ask | None:
    """넓은 첫 요청인지 판정한다(#530). 넓으면 물을 말과 답 보기, 아니면 None. 호출이 실패하면 묻지 않는다."""
    chain = asking.SCOPE_PROMPT | get_chat_model() | message_text | parse_json_response
    try:
        answer = await invoke_chain(chain, {"message": message})
    except LLMUnavailableError:
        logger.warning("넓은 요청 판정 실패 — 묻지 않고 찾는다", exc_info=True)
        return None
    return asking.parse(answer)


async def _scope_within_grace(
    scope: "asyncio.Task[asking.Ask | None]",
) -> asking.Ask | None:
    """판정 결과를 받는다. 1단계가 끝난 뒤 SCOPE_GRACE_SECONDS 안에 못 받으면 포기하고 묻지 않는다."""
    try:
        return await asyncio.wait_for(scope, timeout=SCOPE_GRACE_SECONDS)
    except TimeoutError:
        logger.warning(
            "넓은 요청 판정이 1단계보다 %.1f초 넘게 늦어 묻지 않고 찾는다",
            SCOPE_GRACE_SECONDS,
        )
        return None


async def _restart_patch(message: str, fresh: Spec) -> dict | None:
    """새 요청이라는 판정을 확인하려고, 조건도 앞 대화도 없이 같은 메시지로 다시 뽑은 patch.

    메시지가 스스로 주제를 말하지 않으면(has_topic 아님) None 이다. 판정이 새 요청이라 했어도 "2만원 이하 아니어도
    돼", "또 다른 거", 인사처럼 주제 없는 말은 앞 요청을 이어가는 말이다. 최근 대화까지 빼는 것은, 빈 spec 이어도
    최근 대화에 있는 앞 가격과 앞 주제를 모델이 다시 옮겨 적었기 때문이다(평가 104번 6턴의 재현). 다시 뽑다
    LLM 이 실패해도 None 이다. 앞 조건을 남기는 쪽이 고치기 전과 같은 동작이다.
    """
    try:
        patch = await _ask_spec_patch(message, fresh, [])
    except LLMUnavailableError:
        logger.warning("빈 조건에서 다시 뽑기 실패 — 앞 조건을 남긴다", exc_info=True)
        return None
    return patch if topic_switch.has_topic(patch) else None


@dataclass(frozen=True)
class SpecUpdate:
    """1단계 결과. answer 는 새 책을 찾는 말이 아니라 대답할 질문이라는 뜻이다(#530). 그때 spec 은 바꾸지 않는다.

    ask 는 넓은 첫 요청이라 찾기 전에 물을 말과 답 보기다(#530, #505). 그때 spec 은 메시지에서 알게 된 조건까지
    갱신한다. 다음 턴이 그 위에서 답을 더한다.

    plan 은 CHAT_AGENT 를 켰을 때 1단계가 더 낸 기준 책·관계다(app/chat/lanes.py). 끄면 늘 비었다.

    recall 은 CHAT_AGENT 를 켰을 때 1단계와 나란히 보낸 떠올린 제목 호출이다(app/chat/recall.py). 쓸 턴이면 갈래를
    모을 때 받고(_recalled_within_grace), 안 쓸 턴이면 호출부가 끊는다. 끄면 늘 None 이다.
    """

    spec: Spec
    degraded: bool
    answer: bool = False
    ask: asking.Ask | None = None
    plan: lanes.Plan = lanes.EMPTY_PLAN
    recall: "asyncio.Task[tuple[lanes.Title, ...]] | None" = None


def _plan(patch: dict, first: dict) -> lanes.Plan:
    """1단계 patch 에서 에이전트 칸을 꺼낸다. 새 요청이라 빈 조건에서 다시 뽑은 patch 는 보여 준 책을 모르므로
    기준 책과 관계는 처음 patch 의 것을 쓴다."""
    if not _agent_on():
        return lanes.EMPTY_PLAN
    plan = lanes.parse_plan(patch)
    if patch is first:
        return plan
    first_plan = lanes.parse_plan(first)
    return lanes.Plan(
        anchor=plan.anchor or first_plan.anchor,
        relation=plan.relation or first_plan.relation,
        named=plan.named or first_plan.named,
    )


# 떠올린 제목 호출(app/chat/recall.py)은 1단계와 나란히 가고, 갈래를 모을 때 검색과 나란히 받는다. 그때까지 못 끝났으면
# 이만큼만 더 기다리고 버린다. 11/10 실험 1(gpt-4o, 12번)에서 이 호출은 턴 시작부터 1.6~2.5초, 1단계가 끝난 뒤 0.3~1.6초
# 더 걸렸다. 검색이 도는 동안 대부분 끝난다. 넓은 첫 요청 판정(SCOPE_GRACE_SECONDS)과 같은 한도를 둔다.
RECALL_GRACE_SECONDS = 2.0


def _may_recall(message: str) -> bool:
    """떠올린 제목을 부를 턴인가. 보여 준 책이나 특정 책을 기준으로 삼는 말이면 부르지 않는다(기준 책 목록을 쓴다).

    주제 말이 하나도 없는 말("책 추천해줘", "아무거나 추천해 줄래?")에도 부르지 않는다. 떠올릴 거리가 없어 되묻는 턴인데
    호출이 1단계와 나란히 나가 LLM 을 두 번 불렀다(11/10 실험 2). 주제 말은 1단계 주제 되찾기(_recover_topic)와 같은 낱말
    규칙으로 본다. 낱말 규칙이 모르는 말("ㅋㅋㅋ", 인사)은 주제로 보고 부른다 — 놓치면 떠올린 제목 갈래가 통째로 빠진다.
    """
    if not _agent_on() or lanes.points_at_book(message):
        return False
    words = [w for w in (t.strip(_PUNCTUATION) for t in message.split()) if w]
    return any(_is_topic_word(w) for w in words)


async def _recall_titles(
    message: str, recent_turns: list[Turn]
) -> tuple[lanes.Title, ...]:
    """떠올린 제목 호출. 실패하면 빈 목록이다 — 떠올린 제목은 덤이라 없어도 검색으로 답한다.

    어떤 예외도 밖으로 내지 않는다. 이 호출은 턴과 나란히 돌다 쓰지 않으면 끊기는데, 끝난 작업의 예외를 아무도 받지
    않으면 이벤트 루프가 경고만 남기고 원인을 잃는다.
    """
    chain = recall.RECALL_PROMPT | get_chat_model() | message_text | parse_json_response
    try:
        answer = await invoke_chain(
            chain,
            {"message": message, "recent_turns": _format_recent_turns(recent_turns)},
        )
    except Exception:
        logger.warning("떠올린 제목 호출 실패 — 검색으로만 찾는다", exc_info=True)
        return ()
    return recall.parse(answer)


async def _recalled_within_grace(
    task: "asyncio.Task[tuple[lanes.Title, ...]]",
) -> tuple[lanes.Title, ...]:
    try:
        return await asyncio.wait_for(task, timeout=RECALL_GRACE_SECONDS)
    except TimeoutError:
        logger.warning(
            "떠올린 제목 호출이 갈래 모으기보다 %.1f초 넘게 늦어 버린다",
            RECALL_GRACE_SECONDS,
        )
        return ()


def _drop(task: "asyncio.Task | None") -> None:
    if task is not None and not task.done():
        task.cancel()


async def update_spec(
    message: str,
    spec: Spec,
    recent_turns: list[Turn],
    shown_listing: str = "(없음)",
    ask_stage: asking.Stage = "none",
) -> SpecUpdate:
    """1단계 — 메시지로 spec을 갱신한다. 명세대로 LLM이 한다.

    LLM은 지금 조건 전체가 아니라 바뀌는 값만 담은 patch를 돌려주고,
    _merge_spec_patch가 지금 spec 위에 겹쳐 완전한 spec을 만든다(SPEC_PROMPT
    주석 참고).

    recent_turns는 spec에 안 담기는 애매한 참조("아까 그 책 말고")를 이번
    메시지가 뭘 가리키는지 판단하는 데만 쓴다. 서버는 대화를 저장하지 않으므로
    (명세) 이번 호출이 끝나면 버려지고, 다음 턴에도 호출자가 다시 실어 보낸다.

    새 요청이면 patch 를 앞 spec 위에 겹쳐 쓰지 않는다(#513). 앞 요청에 주제가 있으면 새 요청인지 판정하는 호출을
    1단계 호출과 나란히 보내고(지연이 늘지 않는다), 새 요청으로 판정되고 빈 조건으로 다시 뽑은 patch 가 스스로 주제를
    담고 있으면 그 patch 를 앞 조건을 비운 spec 위에 쓴다. 옛 spec 이 프롬프트에 있어 흔들린 첫 patch 는 버린다
    (app/chat/topic_switch.py).

    실패(LLM 장애, 또는 병합한 값이 Spec 모양이 아님)하면 원래 spec을 그대로
    돌려주고 degraded 를 True로 준다. 명세: "LLM 장애면 1과 3을 건너뛰고
    요청의 spec으로 2만 돌려 점수 상위 3권을 낸다" — 호출부(chat())가 이
    신호를 보고 3단계(카드 생성)를 건너뛴다.

    모델이 대답할 질문이라고 하면(turn: answer) spec 을 바꾸지 않고 answer 를 True 로 준다(#530). 새 요청 판정은
    기다리지 않는다. 질문은 새 주제가 아니다.

    ask_stage 는 넓은 첫 요청에 한 번 묻기(app/chat/asking.py)의 단계다. may_ask 면 넓은지 판정하는 호출을 1단계와
    나란히 보내고, 넓으면 ask 에 물을 말을 준다(1단계가 제목·저자를 콕 집었으면 묻지 않는다). answered 면 이번 말은
    직전 질문의 답이라 1단계에 답을 더하라는 규칙을 싣고 새 요청 판정은 하지 않는다.
    """
    scope = (
        asyncio.create_task(_judge_scope(message)) if ask_stage == "may_ask" else None
    )
    recalling = (
        asyncio.create_task(_recall_titles(message, recent_turns))
        if _may_recall(message)
        else None
    )
    handed_over = False
    try:
        previous = topic_switch.previous_request(spec)
        if ask_stage == "answered":
            previous = None
        base, new_request = spec, False
        if previous is None:
            patch = await _ask_spec_patch(
                message, spec, recent_turns, shown_listing, ask_stage
            )
        else:
            relation = asyncio.create_task(_asks_new_request(message, previous))
            try:
                patch = await _ask_spec_patch(
                    message, spec, recent_turns, shown_listing, ask_stage
                )
            except BaseException:
                relation.cancel()
                raise
            if patch.get("turn") == "answer":
                relation.cancel()
            else:
                new_request = await _relation_within_grace(relation)
        if patch.get("turn") == "answer":
            return SpecUpdate(spec, degraded=False, answer=True)
        first = patch
        if new_request:
            fresh = topic_switch.fresh_spec(spec)
            restarted = await _restart_patch(message, fresh)
            if restarted is not None:
                logger.info("주제가 바뀌어 앞 조건을 비웠다")
                base, patch = fresh, restarted
        if _agent_on():
            # 기준 책은 에이전트 칸(anchor_card)으로 받는다. 모델이 spec 의 anchor_book(book_id 칸)에 카드 번호를 넣는
            # 일이 있었다(11/10 점검, gpt-4o-mini). 그대로 겹쳐 쓰면 번호가 엉뚱한 book_id 로 남는다
            patch = {k: v for k, v in patch.items() if k != "anchor_book"}
        updated = Spec.model_validate(_merge_spec_patch(base, patch))
        ask = await _scope_within_grace(scope) if scope is not None else None
        if ask is not None and _is_pinned(updated):
            ask = None
        plan = _plan(patch, first)
        # 제목·저자를 콕 집은 요청과 기준 책을 짚은 요청에는 떠올린 제목을 쓰지 않는다. 묻는 턴에도 안 쓴다
        handed_over = (
            ask is None and updated.intent == "semantic" and plan.anchor is None
        )
        return SpecUpdate(
            updated,
            degraded=False,
            ask=ask,
            plan=plan,
            recall=recalling if handed_over else None,
        )
    except LLMUnavailableError:
        logger.exception("spec 갱신 실패")
        return SpecUpdate(spec, degraded=True)
    except ValidationError as exc:
        # pydantic ValidationError의 문자열 표현은 각 오류에 input_value=...로 실제
        # 값을 싣는다. LLM이 patch에 사용자 발화를 옮겨 적어 여기 걸리면(SPEC_PROMPT
        # 주석의 실제 재현 사례들처럼), logger.exception()을 그대로 쓰면 그 값이
        # ERROR 로그에 남는다(리뷰 지적, #202). 어느 필드가 왜 틀렸는지(loc·type)만
        # 남긴다.
        logger.error(
            "spec 갱신 실패: 병합 결과가 Spec 모양이 아님 %s",
            [(err["loc"], err["type"]) for err in exc.errors()],
        )
        return SpecUpdate(spec, degraded=True)
    finally:
        # 대답 턴으로 일찍 돌아가거나 1단계가 실패하면 판정은 쓰지 않는다
        if scope is not None and not scope.done():
            scope.cancel()
        if not handed_over:
            _drop(recalling)


def _query_text(spec: Spec) -> str:
    """spec에서 ①에 넘길 검색어 한 줄을 만든다.

    intent가 exact면 지정된 제목·저자를 합친다. 출판사는 일부러 뺀다 — ①의
    키워드 검색은 제목·저자·소개글에서만 낱말을 찾고 평균 커버리지가 기준
    (0.6)을 못 채우면 후보가 통째로 빠지는데, 출판사는 이 셋 어디에도 없는
    낱말이라 평균만 깎아 정확한 책을 탈락시킨다(예: "마음 현암사", 리뷰 지적).
    출판사는 검색 결과를 받은 뒤 get_candidates()가 publisher 필드로 거른다
    (이슈 #137). 제목·저자·semantic이 전부 비어 출판사만 남으면, 출판사를
    최후 수단으로 검색어에 써서 최소한 service.search()까지는 가게 한다 —
    안 그러면 빈 검색어로 바로 return [] 돼 그 뒤의 publisher 필터가 걸릴
    기회조차 없다.
    어느 쪽도 없으면 빈 문자열(호출부가 후보 없음으로 처리).
    """
    exact_query = " ".join(p for p in (spec.exact.title, spec.exact.author) if p)
    text = (
        exact_query
        if spec.intent == "exact" and exact_query
        else spec.semantic or exact_query or spec.exact.publisher
    )
    return (text or "").strip()[:MAX_QUERY_CHARS]


async def _fetch_descriptions(book_ids: list[int]) -> dict[int, str]:
    """①의 book_id 목록에 description을 붙인다.

    app.search.books.fetch()는 description을 안 돌려준다 — ①의 응답 계약에
    없는 필드라, 거기 추가하면 /search 응답에도 새 나간다. ①의 파일은 건드리지
    않고 ③에서 직접 조회한다(엔드포인트 소유권 교차 금지).
    """
    if not book_ids:
        return {}
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT book_id, description FROM v_books WHERE book_id = ANY($1::int[])",
            book_ids,
        )
    return {r["book_id"]: r["description"] for r in rows}


# 카탈로그는 판본·권마다 book_id가 달라 같은 책이 후보와 카드에 여러 번 나온다(#281).
# 저자 표기도 "지은이: 한강", "J.K. 롤링 지음 ;강동혁 옮김"처럼 들쭉날쭉해서, 역할 말을 빼고
# 첫 저자만 본다. 역할 접두어는 콜론이 있을 때만, 접미어는 앞에 공백이 있을 때만 뗀다 —
# 안 그러면 이름 속 글자("글", "저")가 같이 잘린다.
_AUTHOR_ROLE_PREFIX = re.compile(
    r"^(지은이|저자|글쓴이|글|그림|그린이|원작|옮긴이|역자|엮은이|편저)\s*[:：]\s*"
)
_AUTHOR_ROLE_SUFFIX = re.compile(r"\s+(지음|옮김|엮음|그림|글|저|역|편)\s*$")
_NON_WORD = re.compile(r"[\W_]+")


_AUTHOR_SEPARATOR = re.compile(r"[;,]")


def _first_author(author: str | None) -> str:
    """저자 칸에서 첫 저자의 이름만 꺼낸다. "지은이:" 같은 역할 말은 뗀다."""
    first = _AUTHOR_SEPARATOR.split(author or "")[0].strip()
    first = _AUTHOR_ROLE_PREFIX.sub("", first)
    return _AUTHOR_ROLE_SUFFIX.sub("", first)


def _edition_key(title: str | None, author: str | None) -> tuple[str, str] | None:
    """같은 책의 다른 판본을 한 키로 묶는다. 제목이나 저자가 비면 None — 다른 책일 수 있어 안 묶는다."""
    first_author = _first_author(author)
    title_key = _NON_WORD.sub("", (title or "").casefold())
    author_key = _NON_WORD.sub("", first_author.casefold())
    if not title_key or not author_key:
        return None
    return title_key, author_key


def _dedupe_editions(pool: list[dict]) -> list[dict]:
    """같은 책의 판본은 앞에 나온 하나만 남긴다. 앞 판본에 소개글이 없고 뒤에 있으면 뒤 것으로 바꾼다."""
    kept: list[dict] = []
    position: dict[tuple[str, str], int] = {}
    for candidate in pool:
        key = _edition_key(candidate.get("title"), candidate.get("author"))
        if key is None:
            kept.append(candidate)
        elif key not in position:
            position[key] = len(kept)
            kept.append(candidate)
        elif not kept[position[key]]["description"] and candidate["description"]:
            kept[position[key]] = candidate
    return kept


async def _fetch_edition_keys(book_ids: list[int]) -> set[tuple[str, str]]:
    """이미 보여 준(제외한) 책들의 판본 키. 그 책의 다른 판본이 다시 추천되지 않게 한다."""
    if not book_ids:
        return set()
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT title, author FROM v_books WHERE book_id = ANY($1::int[])",
            book_ids,
        )
    keys = {_edition_key(r["title"], r["author"]) for r in rows}
    keys.discard(None)
    return keys


async def _fetch_categories(book_ids: list[int]) -> dict[int, str | None]:
    """①의 book_id 목록에 카탈로그 분류(v_books.category)를 붙인다.

    _fetch_descriptions와 같은 이유로 ①의 파일은 안 건드리고 여기서 직접 조회한다
    (엔드포인트 소유권 교차 금지). 장르 필터 정규화(_catalog_categories_for)에서 쓴다.
    """
    if not book_ids:
        return {}
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT book_id, category FROM v_books WHERE book_id = ANY($1::int[])",
            book_ids,
        )
    return {r["book_id"]: r["category"] for r in rows}


def _catalog_categories_for(category: str) -> tuple[str, ...] | None:
    """온보딩 장르 값(예: "소설")이면 대응하는 카탈로그 분류들(핵심+일부, #110).

    대응표에 없는 값(예: 이미 카탈로그 분류명 그대로거나, 대응표가 모르는 값)이면
    None — 호출부가 예전처럼 ①의 정확 일치 필터를 그대로 쓴다(categories.py
    docstring: "여기 없는 값은 쓰는 쪽에서 건너뛴다"). 카탈로그 분류명도 아닌 값은
    chat()이 후보 검색 전에 _settle_category로 걸러 낸다(#359).
    """
    match = categories.ONBOARDING_TO_CATALOG.get(category)
    if match is None:
        return None
    return match.core + match.partial


async def _category_has_books(name: str) -> bool:
    """카탈로그에 이 분류명의 책이 한 권이라도 있는지(#359). 분류 색인을 타므로 가볍다."""
    pool = db.get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM v_books WHERE category = $1)", name
        )


async def _is_filterable_category(name: str) -> bool:
    """이 분야 값으로 후보를 거를 수 있는지(#359).

    온보딩 값(대응표)이면 거를 수 있다. 어린이처럼 분류로 가를 수 없는 온보딩 값은 못 거른다(#228).
    그 밖의 값은 카탈로그 분류명 그대로일 때만 거를 수 있어 책 표에 물어본다.
    """
    if name in categories.ONBOARDING_TO_CATALOG:
        return True
    if name in categories.UNFILTERED_ONBOARDING:
        return False
    return await _category_has_books(name)


def _move_category_to_semantic(spec: Spec) -> Spec:
    """분야 필터를 풀고 그 말을 검색 문장에 붙인다. 이미 문장에 든 말이면 다시 붙이지 않는다.

    제목·저자를 콕 집은 요청은 검색어가 제목·저자라(_query_text) 붙여도 쓰이지 않고,
    카드 프롬프트 첫 줄만 분위기 문장으로 바뀐다(_card_prompt_intro). 그래서 필터만 푼다.
    """
    word = (spec.filters.category or "").strip()
    filters = spec.filters.model_copy(update={"category": None})
    pinned = spec.intent == "exact" and (spec.exact.title or spec.exact.author)
    if not word or pinned:
        return spec.model_copy(update={"filters": filters})
    semantic = spec.semantic or ""
    if word.casefold() not in semantic.casefold():
        semantic = f"{semantic} {word}".strip()
    return spec.model_copy(update={"filters": filters, "semantic": semantic})


async def _settle_category(spec: Spec) -> Spec:
    """1단계가 낸 분야 값을 검사해, 필터로 못 쓰는 값이면 검색 문장으로 옮긴다(#359).

    1단계 LLM은 분야 칸에 "SF", "알고리즘", "입문자용" 같은 말도 넣는다. 대응표에도 카탈로그
    분류명에도 없는 값은 ①의 정확 일치 필터에 걸려 후보가 0건이 된다(평가 #322: 이런 값이
    걸린 14턴이 전부 카드 0장). 사용자가 한 말이라 버리지 않고 검색 문장(semantic)으로 옮긴다.

    분야는 쓸 수 있는 값인데 찾을 말(semantic·제목·저자)이 비었으면 분야 이름을 검색 문장으로
    쓴다. 되묻기(#283)가 안내한 "경제경영 책 추천해줘"에 1단계가 분야만 채우면, 검색어가
    비어 같은 질문을 되풀이했다.

    고친 spec을 응답에 그대로 싣는다. 못 쓰는 분야 값이 "걸린 조건"으로 안내되거나 다음 턴까지
    남지 않게 한다.
    """
    category = spec.filters.category
    if category is None:
        return spec
    if not await _is_filterable_category(category):
        return _move_category_to_semantic(spec)
    if not (spec.semantic or spec.exact.title or spec.exact.author):
        return spec.model_copy(update={"semantic": category})
    return spec


# 1단계가 조건만 뽑고 주제를 빠뜨렸을 때 메시지에서 주제를 되찾는 규칙(_recover_topic)이 쓰는 말이다.
# 숫자 뒤에 단위가 붙은 낱말은 조건이다: 2020년, 2만원, 300쪽, 1만원대로. 단위 없는 숫자("1984", "3D프린터")는 주제일 수 있다.
_CONDITION_WORD = re.compile(
    r"^\d[\d,]*(만|천|백)?(원|년|쪽|페이지|p|권|살|세|개월|달|일|월|%|대)\S*$",
    re.IGNORECASE,
)
# 요청하는 말의 끝: 보여줘, 알려주세요, 있어?, 있을까요 …("추천…"은 따로 거른다)
_REQUEST_WORD = re.compile(
    r"(줘|주세요|줄래|부탁해?요?|있어요?|있나요|있을까요?|싶어요?)[?!.~]*$"
)
_TRAILING_PARTICLE = re.compile(r"(에|로|으로|의|은|는|도|이|가|를|을|만)$")
_PUNCTUATION = ".,!?~\"'()[]“”‘’"
# 주제는 책을 가리키는 낱말 바로 앞에 붙은 말로만 본다(#408 리뷰). 목록에 없는 말을 전부 주제로 보면 "중에 뭐",
# "만 원 거" 같은 잔말이 검색 문장이 되어, 원래 되묻던 문장이 엉뚱한 책을 찾는다. 이 낱말이 없는 문장은 되찾지
# 않고 되묻는다 — 되찾지 못하는 쪽은 고치기 전과 같은 동작이라 안전하다.
# - 책 말: 주제가 아니라 앞말이 가리키는 대상이다("인공지능 책"에서 "인공지능")
# - 책 종류 말: 이 말 자체도 주제다("자바 입문서", "디스토피아 소설"). 앞말이 없어도 이 말만으로 주제가 된다
_BOOK_WORDS = frozenset({"책", "책들", "도서"})
_BOOK_KIND_WORDS = frozenset(
    {"소설", "에세이", "시집", "만화", "동화", "입문서", "교재", "참고서", "자기계발서"}
)
# 빼 달라는 말에 나온 책 종류는 사용자가 원하는 주제가 아니다("소설 말고 다른 거")
_EXCLUSION = re.compile(r"말고|빼고|제외|아닌")
# 조건이나 요청을 말하는 낱말. 주제가 아니다.
_NOT_TOPIC = frozenset(
    {
        "책", "책들", "이후", "이전", "이하", "이상", "이내", "나온", "나오는", "출간", "출간된", "출판된",
        "신간", "최근", "요즘", "넘는", "넘지", "않는", "안", "좀", "정도", "가격", "분량", "쪽", "페이지",
        "저렴한", "싼", "비싼", "얇은", "두꺼운", "살", "수", "있는", "좋은", "괜찮은", "그냥", "제일",
        "읽을", "볼", "만한", "아무거나",
    }
)  # fmt: skip


def _has_condition(filters: SearchFilters) -> bool:
    """가격·출간연도 조건이 걸려 있는지. price_min 0은 조건이 아니다(LLM이 "저렴한"에 0을 넣는다)."""
    return bool(filters.price_min) or any(
        value is not None
        for value in (filters.price_max, filters.pub_year_from, filters.pub_year_to)
    )


def _is_topic_word(word: str) -> bool:
    if _CONDITION_WORD.match(word) or _REQUEST_WORD.search(word):
        return False
    if word.startswith("추천"):
        return False
    return word not in _NOT_TOPIC and _TRAILING_PARTICLE.sub("", word) not in _NOT_TOPIC


def _recover_topic(spec: Spec, message: str) -> Spec:
    """1단계가 조건만 뽑고 주제를 빠뜨렸으면 메시지에서 주제를 되찾는다(#359 원인 2).

    "2020년 이후에 나온 인공지능 책 추천해줘"에서 1단계가 pub_year_from만 내고 semantic을 비우는 일이 있다
    (평가 38번: 같은 프롬프트로 5회 중 1~2회). 검색 문장이 비면 후보를 찾지 않고 "어떤 분야가 좋으세요?"로
    되물어 카드가 0장이 된다. 책을 가리키는 낱말("책", "소설", "입문서" …) 바로 앞에
    주제 말이 붙어 있으면 그것을 검색 문장으로 쓴다. 그런 말이 없으면("2만원 이하 책 추천해줘", "만 원 이하로 볼
    만한 거 있나요") 주제를 모르는 것이니 그대로 되묻는다. 빼 달라는 말("소설 말고")이 있으면 건드리지 않는다.

    조건이 걸려 있을 때만 쓴다. 조건 없이 주제를 빠뜨린 경우("책 추천해줘")는 되묻는 것이 맞고, 이름이
    아닌 말을 주제로 잘못 잡을 위험이 가장 큰 곳이기도 하다. 이미 찾을 말(semantic·제목·저자·출판사)이
    있거나 exact 의도면 건드리지 않는다. 고친 spec은 응답에 실려 다음 턴으로 이어진다.
    """
    if (
        spec.intent != "semantic"
        or _query_text(spec)
        or not _has_condition(spec.filters)
    ):
        return spec
    if _EXCLUSION.search(message):
        return spec
    words = [w for w in (t.strip(_PUNCTUATION) for t in message.split()) if w]
    topic = _topic_before_book_word(words)
    if not topic:
        return spec
    return spec.model_copy(update={"semantic": topic[:MAX_QUERY_CHARS]})


def _without_invented_mood(
    spec: Spec, req: ChatRequest, shown_books: list[shown.ShownBook]
) -> Spec:
    """검색 문장에서 사용자가 말하지 않은 분위기 낱말을 뺀다(#543, app/chat/invented_mood.py).

    말풍선은 검색 문장을 보고 쓰므로, 여기서 빼야 "가벼운 에세이를 찾으신다면"처럼 사용자가 안 한 말을 되읊지 않는다.
    고친 spec 은 응답에 실려 다음 턴으로 이어진다.
    """
    grounds = invented_mood.evidence(
        req.message or "", req.recent_turns, req.spec.semantic, shown_books
    )
    semantic, removed = invented_mood.strip(spec.semantic, grounds)
    if not removed:
        return spec
    logger.info("검색 문장에서 사용자가 말하지 않은 분위기 말 %d개를 뺐다", removed)
    return spec.model_copy(update={"semantic": semantic})


def _topic_before_book_word(words: list[str]) -> str:
    """책을 가리키는 낱말 바로 앞에 붙은 주제 말을 돌려준다. 없으면 빈 문자열.

    앞에서부터 처음 만나는 책 낱말부터 거꾸로 걸으며 주제 말을 모으고, 조건·요청 말을 만나면 멈춘다.
    "300쪽 안 넘는 자바 입문서"는 "안 넘는"에서 멈춰 "자바 입문서"가 된다. 책 종류 말은 그 자체를 주제에 넣는다.
    """
    for index, word in enumerate(words):
        bare = _TRAILING_PARTICLE.sub("", word)
        is_kind = word in _BOOK_KIND_WORDS or bare in _BOOK_KIND_WORDS
        if not (is_kind or word in _BOOK_WORDS or bare in _BOOK_WORDS):
            continue
        run: list[str] = []
        for before in reversed(words[:index]):
            if not _is_topic_word(before):
                break
            run.append(before)
        run.reverse()
        if is_kind:
            run.append(word if word in _BOOK_KIND_WORDS else bare)
        if run:
            return " ".join(run)
    return ""


async def _owned_book_ids(req: ChatRequest) -> list[int] | None:
    """요청에 실려 온 산 책·나의 도서관 책을 book_id로 바꾼다(#319). 안 왔으면 None.

    get_candidates가 이 목록으로 후보에서 뺀다. 두 칸이 다 없으면(null 포함) 빼는 책이 없다.
    복제한 사용자 표로 빼던 길은 없앴다(#307).

    책 표에 없는 ISBN은 건너뛴다(app.core.isbn). 가진 책이 없으면 책 표를 조회하지 않는다.
    """
    if req.purchased_isbns is None and req.library_isbns is None:
        return None
    isbns = [*(req.purchased_isbns or []), *(req.library_isbns or [])]
    if not isbns:
        return []
    return await _book_ids_for_isbns(isbns)


async def _fetch_similarities(
    conn: asyncpg.Connection, book_ids: list[int], centroid: list[float]
) -> dict[int, float]:
    """후보 book_id들과 취향 centroid의 코사인 유사도(#180).

    ④(app.feed.personalized)는 centroid와 가까운 책을 벡터 색인으로 "찾는다"(수십만
    권 중 500권). ③은 후보가 이미 정해져 있어(①의 키워드 검색 결과, 최대
    CANDIDATE_LIMIT권) 그 책들의 거리만 "재는" 거라 색인 튜닝 없이 WHERE IN 하나로
    충분하다 — book_embeddings는 book_id가 PK라 어차피 색인을 탄다.

    pgvector `<=>`는 코사인 거리라 1을 빼야 유사도다(④와 같은 식, personalized.py 참고).
    """
    if not book_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT book_id, 1 - (embedding <=> $1::vector) AS similarity
        FROM book_embeddings
        WHERE book_id = ANY($2::int[])
        """,
        to_vector_literal(centroid),
        book_ids,
    )
    return {r["book_id"]: r["similarity"] for r in rows}


async def _embed_query_semantic(text: str) -> list[float] | None:
    """spec.semantic을 벡터로 바꾼다. 실패하면 None — match_score는 취향 기반 옛 식으로 폴백한다(#222).

    ①(app.search.service._embed_query)과 같은 방어 패턴이다 — 임베딩 장애로
    ③ 전체가 500이 되면 안 된다. ①이 이미 검색어(_query_text(spec))를
    임베딩하지만, exact와 semantic이 함께 오면 그 검색어가 spec.semantic과
    달라질 수 있어(예: 제목은 지정하고 분위기도 말한 경우) 여기서 따로 부른다.

    intent가 semantic이면(제일 흔한 경우) ①과 같은 문장을 중복으로 임베딩하는
    셈이지만, 실측(로컬 CPU, 모델 로딩 후) p95 11ms로 턴 목표(6~8초)에 비해
    미미해 지금은 그대로 둔다. ①이 벡터를 밖으로 안 돌려줘 재사용하려면 그
    파일(팀원 소유)을 고쳐야 한다.
    """
    try:
        vectors, _, _ = await embedding.embed([text], "query")
    except Exception:
        logger.exception("질의 임베딩 실패. 취향 기반 점수로 대신한다")
        return None
    return vectors[0]


# 함께 대출·마니아 이웃 책과 기준 책의 유사도 가운데 값(평가 DB, 기준 책 300권 — 아래 docstring). 목록에 든 책의 바닥이다
LISTED_SIMILARITY = 0.854


def _book_similarity_part(similarity: float | None) -> float:
    """기준 책과 후보 책의 코사인 유사도를 0–1 로. 요청 문장 ↔ 책의 구간(0.85–0.95)을 쓰면 관계 카드가 0~19% 였다(11/10
    점검). 평가 DB 에서 잰 책 ↔ 책 유사도(기준 책 300권): 함께 읽은 책 가운데 0.854·위 10% 0.891, 마니아 추천 가운데
    0.854·위 10% 0.899, 아무 책 둘 가운데 0.814·위 10% 0.840. ④ 관심사 목록이 책 ↔ 책에 쓰는 구간(0.82–0.90, #483)이 이
    분포에 맞아 같은 구간을 쓴다 — 아무 책은 거의 0, 이웃 책 가운데는 약 0.4 다.

    이 식으로 매기는 책은 기준 책의 함께 대출·마니아 목록에서 온 책뿐이다(관계를 물은 턴에는 떠올린 제목을 부르지 않고,
    검색 책은 요청 문장으로 이미 매겼다). 목록에 든 것이 기준 책과 이어진 근거라, 유사도가 낮거나 벡터가 없어도(소개글 없음)
    이웃 책 가운데 값(LISTED_SIMILARITY)보다 낮게 매기지 않는다. 그 전에는 『데미안』의 마니아 추천 『죄와 벌』·『이방인』,
    소개글 없는 함께 읽은 책이 0% 로 나왔다(11/10 실험 2).
    """
    span = (
        interest_scoring.BOOKS_SIMILARITY_CEILING
        - interest_scoring.BOOKS_SIMILARITY_FLOOR
    )
    measured = (
        LISTED_SIMILARITY if similarity is None else max(similarity, LISTED_SIMILARITY)
    )
    return min(
        1.0, max(0.0, (measured - interest_scoring.BOOKS_SIMILARITY_FLOOR) / span)
    )


def _chat_match_score(
    query_similarity: float | None,
    taste_similarity: float | None,
    category_raw: float | None,
    popularity_value: float | None,
    catalog_max: float | None,
    *,
    has_taste: bool,
    book_to_book: bool = False,
) -> int:
    """③ 전용 매칭 점수(#222) — 이번 요청 조건과의 유사도를 메인으로 쓴다.

    ④(app.feed.scoring.match_score)와 계산식이 다르다(명세 ③ 각주). 취향
    유사도는 ④와 같은 원칙으로 없으면 0으로 두고 남은 비중을 재분배하지
    않는다 — 축소 응답도 같은 잣대를 쓰기 위해서다.

    book_to_book 이면 query_similarity 가 요청 문장이 아니라 기준 책과의 유사도다(책 추천 에이전트가 관계를 물은 턴).
    """
    query_part = (
        _book_similarity_part(query_similarity)
        if book_to_book
        else scoring.similarity_part(query_similarity)
    )
    total = QUERY_SIMILARITY_WEIGHT * query_part
    total += CHAT_CATEGORY_WEIGHT * scoring.category_part(category_raw)
    total += CHAT_POPULARITY_WEIGHT * scoring.popularity_part(
        popularity_value, catalog_max
    )
    if has_taste:
        total += TASTE_SIMILARITY_WEIGHT * scoring.similarity_part(taste_similarity)
    return round(total * 100)


async def _attach_match_scores(
    conn: asyncpg.Connection,
    candidates: list[dict],
    user_id: int,
    spec: Spec,
    *,
    query_vector: list[float] | None = None,
    book_to_book: bool = False,
) -> None:
    """후보마다 match_score를 채운다(명세 2단계 축소판, #129).

    ①의 검색 결과에는 카테고리·인기 점수가 없다(응답 계약 밖이라 ①의 파일은
    안 건드리고 여기서 직접 조회한다 — _fetch_descriptions와 같은 이유,
    엔드포인트 소유권 교차 금지). 취향 벡터 유사도(책 임베딩 대 취향
    centroid)는 프로필이 있는 사용자만 붙는다(#180) — cold_start(프로필이
    없거나 아직 벡터를 못 만든 사용자)는 ④의 cold_start 목록과 같은 이유로
    유사도가 없다.

    spec.semantic이 있으면(#222) 이번 요청 조건과 후보 책의 유사도를 메인으로
    쓰는 ③ 전용 식(_chat_match_score)으로 계산한다. semantic이 없거나(exact
    의도라 비교할 조건이 없음) 그 임베딩이 실패하면, ④와 같은 식
    (app.feed.scoring.match_score, with_similarity=has_centroid)으로 그대로
    폴백한다 — 지금까지의 동작을 그대로 유지한다.

    candidates를 그 자리에서 고친다(각 항목에 match_score·popularity 칸을
    더한다) — _fetch_descriptions가 description을 더하는 것과 같은 방식이다.
    conn을 받는다(app.feed의 함수들과 같은 방식) — 테스트가 트랜잭션 하나로
    묶어 흔적 없이 돌릴 수 있게 한다.
    """
    if not candidates:
        return
    book_ids = [c["book_id"] for c in candidates]
    rows = await conn.fetch(
        f"""
        SELECT b.book_id, b.category,
               coalesce({popularity.score_sql("p")}, 0) AS popularity
        FROM v_books b
        LEFT JOIN v_book_popularity p USING (book_id)
        WHERE b.book_id = ANY($1::int[])
        """,
        book_ids,
    )
    profile_row = await conn.fetchrow(
        "SELECT centroid::text AS centroid, tag_weights::text AS tag_weights, "
        "cold_start FROM taste_profile WHERE user_id = $1",
        user_id,
    )
    catalog_max = await personalized.catalog_max_popularity(conn)

    # ④의 _profile()과 같은 조건(app/feed/service.py) — cold_start거나 centroid가
    # 없으면 벡터를 못 쓰는 사용자다. 저장된 벡터가 없으면 구매·리뷰 이력이 있어도 취향을
    # 만들지 않는다(#266) — BE는 개인화 추천에 동의한 사용자만 ⑥을 부르므로, 프로필이 없는
    # 사용자는 동의하지 않은 사용자다.
    has_centroid = bool(
        profile_row and not profile_row["cold_start"] and profile_row["centroid"]
    )
    tag_weights = json.loads(profile_row["tag_weights"]) if profile_row else {}
    centroid = json.loads(profile_row["centroid"]) if has_centroid else None
    similarities: dict[int, float] = {}
    if has_centroid:
        similarities = await _fetch_similarities(conn, book_ids, centroid)

    # #222 — semantic이 있으면(이번 요청의 조건) 그 임베딩과 후보 책들의 유사도를
    # 구해 ③ 전용 식으로 점수를 낸다. 없거나 임베딩이 실패하면 has_query가 False로
    # 남아 아래에서 옛 식(④와 동일)으로 그대로 폴백한다.
    # query_vector 를 주면(책 추천 에이전트가 관계를 물은 턴의 기준 책 벡터) 이번 요청 문장 대신 그것과 견준다.
    # book_to_book(관계를 물은 턴)인데 기준 책 벡터가 없으면(기준 책 소개글 없음) 요청 문장으로 돌아가지 않고 유사도를 모르는
    # 것으로 둔다 — 목록 책의 바닥(_book_similarity_part)을 받는다. 돌아가면 소개글 없는 『혼자 공부하는 파이썬』의 함께 읽은
    # 책이 0% 로 나왔다(11/10 재측정).
    has_query = book_to_book
    query_similarities: dict[int, float] = {}
    if query_vector is None and spec.semantic and not book_to_book:
        query_vector = await _embed_query_semantic(spec.semantic)
    if query_vector is not None:
        has_query = True
        query_similarities = await _fetch_similarities(conn, book_ids, query_vector)

    fields = {r["book_id"]: r for r in rows}
    for c in candidates:
        f = fields.get(c["book_id"])
        category = f["category"] if f else None
        c["popularity"] = f["popularity"] if f else 0
        if has_query:
            c["match_score"] = _chat_match_score(
                query_similarities.get(c["book_id"]),
                similarities.get(c["book_id"]),
                tag_weights.get(category),
                c["popularity"],
                catalog_max,
                has_taste=has_centroid,
                book_to_book=book_to_book,
            )
        else:
            c["match_score"] = scoring.match_score(
                similarities.get(c["book_id"]),
                tag_weights.get(category),
                c["popularity"],
                catalog_max,
                with_similarity=has_centroid,
            )


def _rule_based_cards(candidates: list[dict]) -> list[dict]:
    """LLM 장애 시 match_score 상위 CARD_LIMIT권으로 카드를 채운다(명세 3단계 축소판).

    명세: 이때 reason_short는 규칙으로 만들고 reason_long은 null이다. 동점이면
    ④(#172)와 같은 규칙으로 인기 → 번호 순으로 가른다(candidates에는 신간
    여부(pub_year)가 없어 그 항만 뺀다).
    """
    ranked = sorted(
        candidates, key=lambda c: (-c["match_score"], -c["popularity"], c["book_id"])
    )
    return [
        {
            "isbn": c.get("isbn"),
            "rank": rank,
            "match_score": c["match_score"],
            "title": c["title"],
            "author": c["author"],
            "price": c.get("price"),
            "cover_url": c.get("cover_url"),
            "reason_short": "지금 조건에 잘 맞는 책이에요.",
            "reason_long": None,
            "match_basis": [],
        }
        for rank, c in enumerate(ranked[:CARD_LIMIT], start=1)
    ]


async def _book_ids_for_isbns(isbns: list[str]) -> list[int]:
    """ISBN 으로 온 책을 book_id 로 바꾼다(#308). 책 표에 없는 ISBN 은 빠진다."""
    async with db.get_pool().acquire() as conn:
        return await isbn.to_book_ids(conn, isbns)


async def _shown_book_ids(req: ChatRequest) -> list[int]:
    """이 대화에서 이미 보여 준 책. ISBN 으로 온 것을 book_id 로 바꾼다. 안 왔으면 책 표를 조회하지 않는다."""
    if not req.exclude_isbns:
        return []
    return await _book_ids_for_isbns(req.exclude_isbns)


async def get_candidates(
    spec: Spec,
    exclude_book_ids: list[int],
    user_id: int,
    owned_book_ids: list[int] | None = None,
) -> list[dict]:
    """2단계 — spec으로 후보를 뽑는다. ①의 하이브리드 검색(제목 완전 일치 먼저, 나머지는 순위 합치기)을 쓴다.

    match_score는 _attach_match_scores가 채운다(#129). 가진 책 제외는 점수와 무관하게
    바로 되므로 여기서 한다(#144). owned_book_ids(요청에 실려 온 산 책·나의 도서관 책,
    #319)를 뺀다. None이면 빼는 책이 없다(#307).
    exclude·publisher는 ①에 없는 개념이라(①은 이 필요가 없음) 결과를 받은
    뒤 여기서 직접 거른다. ①이 keyword-only로 축소됐는지는 지금은 안 본다
    (후속 판단).

    semantic이면 소개글 없는 책도 여기서 뺀다 — 3단계는 소개글만 근거로
    카드를 쓰는데, 빈 소개글을 그대로 넘기면 모델이 제목·저자만 보고 이유를
    지어낸다. exact는 분위기를 안 보므로 소개글 유무와 무관하게 그대로 둔다
    (리뷰 — ①의 하이브리드 검색이 소개글 없는 책도 키워드로 찾아주는 걸
    exact에서는 그대로 살린다). exclude와 마찬가지로 CANDIDATE_LIMIT으로
    자르기 전에 걸러야, 소개글 없는 책이 그 자리를 먹고 뒤쪽의 쓸 수 있는
    후보가 밀려나지 않는다.
    """
    query = _query_text(spec)
    if not query:
        return []

    # 장르는 온보딩 값(예: "소설")으로 오는데 ①의 category 필터는 카탈로그 분류명과
    # 정확히 같아야 해서 그대로 넘기면 0건이 된다(#203). 대응표에 있는 값이면 ①에는
    # category를 안 보내고(정확 일치를 걸면 애초에 0건이라 넓게 받아야 한다) 아래에서
    # 카탈로그 분류로 직접 거른다 — publisher와 같은 패턴(엔드포인트 소유권 교차 금지).
    search_filters = spec.filters
    catalog_categories = None
    if spec.filters.category is not None:
        catalog_categories = _catalog_categories_for(spec.filters.category)
        if catalog_categories is not None:
            search_filters = spec.filters.model_copy(update={"category": None})

    outcome = await service.search(
        SearchRequest(query=query, filters=search_filters, size=SEARCH_SIZE)
    )
    exclude = set(exclude_book_ids) | set(spec.exclude)
    pool = [r for r in outcome.results if r["book_id"] not in exclude]
    if catalog_categories is not None:
        book_categories = await _fetch_categories([c["book_id"] for c in pool])
        pool = [
            r for r in pool if book_categories.get(r["book_id"]) in catalog_categories
        ]
    if spec.exact.publisher:
        # _query_text()는 출판사를 검색어에 안 섞는다(위 docstring) — 그래서 여기서
        # 결과를 따로 거른다. exclude와 같은 이유로 CANDIDATE_LIMIT 전에 거른다.
        # 완전 일치가 아니라 포함 관계로 본다 — "(주)현암사", "현암주니어 :현암사"처럼
        # 표기가 섞여 있어 완전 일치면 같은 출판사가 조용히 빠진다(리뷰 지적).
        publisher = _normalize_publisher(spec.exact.publisher)
        pool = [
            r
            for r in pool
            if publisher in _normalize_publisher(r.get("publisher") or "")
        ]
    if not pool:
        return []

    if owned_book_ids:
        # 저평점을 준 책은 따로 받지 않는다. 리뷰는 산 책에만 달 수 있어 산 책에 들어 있다.
        owned = set(owned_book_ids)
        pool = [c for c in pool if c["book_id"] not in owned]
    if not pool:
        return []

    descriptions = await _fetch_descriptions([c["book_id"] for c in pool])
    for c in pool:
        c["description"] = descriptions.get(c["book_id"]) or ""

    if spec.intent == "semantic":
        pool = [c for c in pool if c["description"]]

    # 이미 보여 준 책의 다른 판본, 그리고 후보끼리 겹치는 판본을 CANDIDATE_LIMIT 전에 걷어 낸다(#281).
    shown_keys = await _fetch_edition_keys(list(exclude))
    if shown_keys:
        pool = [
            c
            for c in pool
            if _edition_key(c.get("title"), c.get("author")) not in shown_keys
        ]
    pool = _dedupe_editions(pool)
    # 분위기로 찾는 요청이면 남은 후보(보통 25권 안팎)를 질문과 책 글을 같이 읽는 리랭커로 다시 줄 세운 뒤 자른다(#638).
    # 제목·저자 찾기(exact)는 제목이 같은 책을 맨 앞에 둔 ①의 순서가 맞는 순서라 그대로 둔다. 리랭커를 못 쓰면 검색 순서다.
    # DB 연결을 잡기 전에 부른다 — 외부 호출을 기다리는 동안 연결을 쥐고 있지 않게.
    if spec.intent == "semantic" and spec.semantic:
        pool = await reranker.rerank(spec.semantic, pool)
    pool = pool[:CANDIDATE_LIMIT]
    async with db.get_pool().acquire() as conn:
        await _attach_match_scores(conn, pool, user_id, spec)
    return pool


def _card_prompt_intro(spec: Spec) -> str:
    """카드 생성 프롬프트 첫 줄 — spec 상태에 따라 다른 문장을 쓴다(#153).

    semantic이 없는데도 예전처럼 "분위기: None"을 그대로 박아 넣으면 모델이
    카드를 거의 못 만든다(실측: semantic만 채워 넣으면 카드 3장, null이면
    0장). exact 의도는 애초에 semantic이 없는 게 정상이라 대부분의 exact
    대화가 여기 걸렸다 — semantic 유무뿐 아니라 exact.title/author 유무까지
    봐서, 상황에 맞는 문장을 고른다.
    """
    if spec.semantic:
        return f'사용자가 원하는 책 분위기: "{spec.semantic}"'
    if spec.exact.title and spec.exact.author:
        return (
            f'사용자가 "{spec.exact.title}"(저자: {spec.exact.author})를 '
            "콕 집어 찾았다. 아래는 그 후보 목록이다."
        )
    if spec.exact.title:
        return (
            f'사용자가 "{spec.exact.title}"를 콕 집어 찾았다. 아래는 그 후보 목록이다.'
        )
    if spec.exact.author:
        return f'사용자가 저자 "{spec.exact.author}"의 책을 찾았다. 아래는 그 후보 목록이다.'
    return "아래는 사용자 조건에 맞는 책 후보 목록이다."


# 소개글이 없는 책(#86). 카드 생성은 소개글만 근거로 이유를 쓰는데, 소개글 칸을 비운 채 넘기면 모델이
# 제목·저자만 보고 책 내용을 지어낸다(평가 32번: 소개글 없는 카드 14장 전부에 긴 이유가 채워짐).
# 프롬프트로 막지 않는다. 목록에 "소개글 없음"을 적고 지어내지 말라고 하면 모델이 그런 책을 아예 안 골라
# 카드가 0장이 된다(평가 33번 5회 중 4회, 2026-10-14). 그래서 그 책의 이유는 모델이 쓴 것을 쓰지 않고 규칙으로 쓴다.
#
# 한 줄 이유는 명세 ③ 의 80자를 넘으면 안 된다. 저자 칸은 여럿이 쉼표로 이어지고 역할 말이 붙은 채로 오기도 해서
# (#407 리뷰: 소개글 없는 책 146만 권 중 24%가 저자 여럿, 1.7%가 80자 초과) 첫 저자만 쓰고, 그래도 넘으면
# 저자를 빼고, 제목만으로도 넘으면 제목을 줄인다.
_NO_DESCRIPTION_REASON = "찾으신 {author}의 『{title}』{copula}."
_NO_DESCRIPTION_REASON_NO_AUTHOR = "찾으신 『{title}』{copula}."
MAX_REASON_SHORT_CHARS = 80


# JSON 형식 예시의 값은 낱말이 아니라 설명으로 적는다. 예시에 `"detail": "잔잔함"`을 두었더니 모델이 사용자가 말하지
# 않은 "잔잔한 분위기"를 답과 이유에 베꼈다(평가: LLM을 부른 턴의 32%, 설명으로 바꾸면 4%. #410).
# {intro}·{limit}·{listing}이 채워지는 자리다. JSON 예시의 중괄호는 자리 표시로
# 오해받지 않게 {{ }}로 겹쳐 쓴다 — 실제로 모델에 가는 글자는 겹치기 전과 같다.
#
# 긴 이유(reason_long)는 도서 상세 페이지에 책 소개와 같이 보인다(#503). "긴 이유(2-4문장)"라고만 적었더니 모델이
# 책 소개를 줄여 쓰거나 그대로 옮겼다 — 평가에서 긴 이유의 절반이 "이 책은 …"·"이 소설은 …"으로 시작했고,
# 사용자의 상황을 짚은 것은 33%였다. 그래서 요청과 책 소개의 맞는 점을 이으라고 적는다. 그래도 옮긴 글은
# generate_cards()가 규칙으로 버린다. 책 소개가 없는 책을 따로 짚지는 않는다(#86 — 짚으면 그 책을 안 골라 카드가
# 0장이 된다).
#
# 말풍선(reply)이 사용자 말을 보게 한다(#530). 전에는 이 호출이 정리된 검색 문장과 책 목록만 받아서, 말풍선이
# 사용자 말에 반응하지 못하고 검색 문장을 되읊었다. 10/19 평가 417턴에서 말풍선의 88%가 카드에 보이는 제목을
# 다시 늘어놓았고 70%가 "추천합니다"로 끝났다. 그래서 이번 메시지와 최근 대화를 넣고, 제목 나열과 그 맺음말을 막는다.
#
# JSON 은 고른 책(picks) → 말풍선(reply) → 카드 이유(cards) 순서로 쓰게 한다. 말풍선이 고른 책을 말하니 책을 먼저
# 고르게 한다 — 답 칸을 먼저 쓰면 모델이 말한 책과 실제로 고른 카드가 어긋날 수 있다(Tam 외, EMNLP 2024: 답 칸이 이유
# 칸보다 먼저 나오면 추론이 나빠짐. #540 이 그래서 cards 를 reply 앞에 뒀다). 대신 reply 를 카드 이유 앞에 두어 SSE 가
# 말풍선을 먼저 보낸다. cards 가 앞이면 말풍선이 카드를 다 쓴 뒤에야 나와 SSE 로 당겨지는 폭이 ~0.3초였다. 11/02
# 실측(4o, 5번씩 중앙값)에서 말풍선은 6.25초 → 2.74초로 당겨지고 전체는 6.28초 → 8.76초로 늦어졌다.
# 이유를 쓰기 전에 말풍선을 쓰니 책 설명을 말풍선에 몰아 썼다(평균 97 → 142자). 그래서 책마다의 설명은 카드 이유에
# 쓰라고 못박는다.
#
# 말풍선의 마지막 문장이 사용자에게 차례를 넘긴다({turn_end}, #545). 10/21 평가 원본에서 카드 턴 말풍선이 다음 말을
# 건넨 적이 0/39였다. 명세상 카드가 있으면 followup 이 null 이라 매 턴 사용자가 다시 말을 꺼내야 했고, 대화가 아니라
# 검색창처럼 느껴졌다. 고른 책들이 갈리는 점을 하나 짚어 묻거나(Bing 되묻기 연구: 구체적인 질문이 "무엇이 궁금하세요?"
# 같은 질문보다 최대 5배 눌림), 한 권을 짚어 의견을 말한다(INSPIRED, EMNLP 2020: 사람 추천자가 의견을 말한 추천이 더
# 받아들여짐). 연달아 물으면 설문지가 되니 직전 챗봇 말이 질문이었으면 의견만 말한다. 예시는 낱말이 아니라 설명으로
# 적는다(#410 — 낱말을 적으면 베낀다).
#
# 말투는 ⑦ 과 같은 규칙(app/core/voice.py)을 붙인다. "조건을 바꿨으면 받았다고 짧게 말하고"는 지웠다. 모델이 글자
# 그대로 따라 해 "조건을 바꿨군요!"를 "다른 거 보여줘"에도 붙였다(#545). 바뀐 조건은 고른 책을 말하는 문장에 녹인다.
#
# 한 권은 번호가 아니라 제목으로 짚는다. 번호로 짚게 했더니 카드를 한 장 고르고 "2번 책은…"이라고 말해, 사용자가
# "2번 담아줘"라고 하면 없는 카드가 됐다(점검 대화 1/37). 묻는 말은 두 갈래를 직접 말하는 꼴로 한다. "어떤 쪽이 더
# 끌리세요?"만 물었더니 무엇과 무엇 중인지 말풍선 앞 문장을 다시 읽어야 했고, 말풍선이 길어졌다(중앙 35자 → 92자).
# 책을 고르라는 질문 뒤에는 사용자의 다음 말("습관 만드는 책으로 보여줘")을 문지기 판정이 책 고르기로 읽어 ⑦로
# 보내기도 했다. 방향을 묻는 질문이면 답이 곧 추천 조건이다.
_HAND_OVER = (
    "마지막 문장으로 사용자에게 차례를 넘긴다. 둘 중 하나만 한다. 고른 책들이 서로 갈리는 점 하나(수준, 형식, "
    "다루는 범위처럼 책 소개에서 드러나는 차이)를 골라 그 두 갈래를 직접 말하며 어느 쪽이 맞는지 묻거나, 사용자가 "
    "말한 것에 가장 맞는 한 권을 『제목』으로 짚고 그 까닭을 책 소개에 있는 것으로 한마디 하라. "
    '"어떤 스타일이 끌리세요?", "어느 책이 마음에 드세요?"처럼 갈래를 말하지 않고 고르라고만 묻지 마라. '
)
_POINT_ONE = (
    "이번에는 묻지 말고, 사용자가 말한 것에 가장 맞는 한 권을 『제목』으로 짚고 그 까닭을 책 소개에 있는 것으로 "
    "한마디 하라. "
)
CARD_PROMPT = ChatPromptTemplate.from_template(
    "너는 책 추천 챗봇이다. 사용자와 대화하며 원하는 책을 함께 좁혀 간다.\n\n"
    "최근 대화:\n{recent_turns}\n\n"
    '이번 메시지: "{message}"\n\n'
    "{intro}\n\n"
    "아래 책 목록 중 위 조건에 맞는 책을 최대 {limit}권 골라라.\n"
    "반드시 한국어로만 답하라. 다른 언어를 섞지 마라.\n"
    "book_id는 반드시 아래 목록에 적힌 값을 그대로 써라. 순서 번호가 아니다. "
    "picks에 적은 차례가 화면의 1번, 2번, 3번이다. cards에는 picks의 책을 같은 차례로 적어라.\n"
    + reasons.SHORT_RULE
    + "match_basis는 reason_short·reason_long과 같은 근거를 label(예: 분위기, "
    "장르, 가격)과 detail(그 근거의 구체적 내용) 짝으로 1~3개 적어라 — 새로운 "
    "근거를 지어내지 말고 두 이유 문장에 이미 쓴 근거만 옮겨 적어라.\n"
    "reason_long은 도서 상세 페이지에서 책 소개와 함께 보인다. 책 소개 문장을 옮기거나 "
    "줄여 쓰지 말고, 사용자가 찾은 것(위 조건)과 이 책 소개의 어떤 점이 맞는지 이어서 "
    "2-4문장으로 써라(최대 300자). 근거는 그 책의 소개에 있는 것만 쓴다.\n"
    "reply는 말풍선에 보일 1-2문장이다(최대 100자). 먼저 이번 메시지에 반응하라 — "
    "사정을 털어놓았으면 그 사정에 한마디 답하고, 바뀐 조건은 이번에 고른 책을 말하는 문장에 녹여라. 요청을 "
    '"~을 찾으셨군요"처럼 되풀이하며 시작하지 마라. 책 제목은 '
    "카드에 보이니 말풍선에 늘어놓지 말고, 이번에 고른 책들이 어떤 결인지 한마디로 말하라. "
    "책마다의 내용 설명은 뒤의 cards 이유에 쓰니 말풍선에 쓰지 마라. "
    "{turn_end}\n"
    + voice.RULES
    + '다음 JSON 형식으로만, 이 칸 순서대로 답하라: {{"picks": [고른 책의 book_id 정수], '
    '"reply": "말풍선에 보일 1-2문장", '
    '"cards": [{{"book_id": 정수, '
    '"reason_short": "다른 책과 갈리는 점(30자 이내)", '
    '"reason_long": "사용자가 찾은 것과 이 책이 맞는 점을 잇는 긴 이유(2-4문장)", '
    '"match_basis": [{{"label": "근거 이름", "detail": "그 근거의 구체적 내용"}}]}}]}}\n\n'
    "책 목록:\n{listing}"
)


def _format_listing(candidates: list[dict]) -> str:
    # 목록 번호("1. 2. 3...")만 주면 모델이 book_id 대신 그 번호를 돌려준다
    # (실제로 재현됨). 각 줄에 book_id 값을 명시하고, 반드시 그 값을
    # 그대로 쓰라고 못박는다.
    return "\n".join(
        f"- book_id {c['book_id']}: {c['title']} - {c['author']} - {c['description']}"
        for c in candidates
    )


def _has_description(book: dict) -> bool:
    """소개글이 있는 책인가. 공백뿐인 소개글은 없는 것으로 본다."""
    return bool((book.get("description") or "").strip())


def _copula(word: str) -> str:
    """끝 글자에 받침이 없으면 "예요", 있거나 한글이 아니면 "이에요"."""
    last = word.rstrip()[-1:]
    if "가" <= last <= "힣" and (ord(last) - ord("가")) % 28 == 0:
        return "예요"
    return "이에요"


def _display_author(author: str | None) -> str:
    """한 줄 이유에 쓸 저자. 첫 저자만 쓰고 여럿이면 "외"를 붙인다. 없으면 빈 문자열."""
    first = _first_author(author)
    if (
        first
        and sum(1 for part in _AUTHOR_SEPARATOR.split(author or "") if part.strip()) > 1
    ):
        return f"{first} 외"
    return first


def _no_description_reason(book: dict) -> str:
    """소개글이 없는 책의 한 줄 이유. 사용자가 찾은 책의 제목·저자만 쓴다 — 책 내용은 알 수 없다.

    80자 안에 들도록 저자 → 저자 없이 제목만 → 줄인 제목 순으로 물러난다.
    """
    title = book["title"]
    author = _display_author(book.get("author"))
    if author:
        reason = _NO_DESCRIPTION_REASON.format(
            author=author, title=title, copula=_copula(title)
        )
        if len(reason) <= MAX_REASON_SHORT_CHARS:
            return reason
    reason = _NO_DESCRIPTION_REASON_NO_AUTHOR.format(title=title, copula=_copula(title))
    if len(reason) <= MAX_REASON_SHORT_CHARS:
        return reason
    # 줄인 제목은 끝이 "…"라 받침을 알 수 없어 "이에요"를 쓴다. 틀의 길이(…와 어미 포함)를 빼고 남는 자리만큼 자른다
    frame = _NO_DESCRIPTION_REASON_NO_AUTHOR.format(title="…", copula="이에요")
    room = MAX_REASON_SHORT_CHARS - len(frame)
    return _NO_DESCRIPTION_REASON_NO_AUTHOR.format(
        title=title[:room].rstrip() + "…", copula="이에요"
    )


def _unless_copied(reason_long: Any, book: dict) -> Any:
    """긴 이유가 책 소개를 옮긴 글이면 None(#503). 상세 페이지에 같은 글이 두 번 보이느니 이유 칸을 숨긴다.

    명세 ③: 긴 이유만 비면 카드는 남기고 reason_long 은 null 이다(BE 는 상세의 이유 영역을 숨긴다). 한 줄 이유로
    채우지 않는다 — 한 줄 이유는 카드에 이미 보이고, 명세의 2-4문장도 아니다.
    """
    if not isinstance(reason_long, str):
        return reason_long
    share = copied_share(reason_long, book.get("description"))
    if share < COPIED_SHARE_MIN:
        return reason_long
    # 글은 남기지 않는다(#202처럼 응답 내용을 로그에 남기지 않는다). 몇 번 버렸는지만 센다.
    logger.info(
        "긴 이유가 책 소개를 옮겨 비움: book_id=%s 옮긴 몫=%.2f",
        book.get("book_id"),
        share,
    )
    return None


def _parse_match_basis(raw: Any) -> list[dict]:
    """모델이 준 match_basis를 정리한다. 형식이 틀리면 빈 배열로 폴백한다.

    label·detail이 둘 다 문자열인 항목만 남긴다 — 모델이 리스트가 아닌 걸
    주거나 항목에 다른 키를 섞어 보내도 카드 조립이 깨지지 않게 한다.
    """
    if not isinstance(raw, list):
        return []
    return [
        {"label": item["label"], "detail": item["detail"]}
        for item in raw
        if isinstance(item, dict)
        and isinstance(item.get("label"), str)
        and isinstance(item.get("detail"), str)
    ]


def _order_by_picks(raw_cards: list, picks: object) -> list:
    """카드를 picks 차례로 세운다. 화면 차례는 말풍선을 쓰기 전에 고른 picks 다.

    picks 가 정수 목록이 아니면 cards 차례를 그대로 쓴다. picks 에 없는 카드는 picks 의 카드 뒤에 둔다 —
    모델이 picks 를 빠뜨렸다고 카드를 버리면 카드가 0장이 될 수 있다. 카드가 없는 pick 은 건너뛴다.
    """
    if not isinstance(picks, list):
        return raw_cards
    order = {}
    for book_id in picks:
        if isinstance(book_id, int) and not isinstance(book_id, bool):
            order.setdefault(book_id, len(order))
    if not order:
        return raw_cards

    def rank(item: object) -> int:
        book_id = item.get("book_id") if isinstance(item, dict) else None
        return (
            order.get(book_id, len(order)) if isinstance(book_id, int) else len(order)
        )

    return sorted(raw_cards, key=rank)


@dataclass(frozen=True)
class CardResult:
    cards: list[dict]
    reply: str | None
    degraded: bool
    # 카드 아래에 보일 답 보기(#652). 지금은 책 추천 에이전트의 함께 읽은 책·비슷한 책 제안만 쓴다
    options: tuple[str, ...] = ()


def _card_prompt_conditions(spec: Spec) -> str:
    """카드 프롬프트에 붙일 걸린 조건 한 줄. 말풍선이 바뀐 조건을 받았다고 말하려면 조건이 보여야 한다(#530)."""
    names = ", ".join(name for name, _ in _active_conditions(spec))
    return f"\n걸린 조건: {names}" if names else ""


async def generate_cards(
    candidates: list[dict],
    spec: Spec,
    *,
    message: str = "",
    recent_turns: list[Turn] | None = None,
    may_ask: bool = True,
    on_reply: ReplySink | None = None,
) -> CardResult:
    """3단계 — 후보 중에서 골라 카드를 만든다.

    명세대로 reason_short·reason_long·match_basis·말풍선 reply를 한 번의
    LLM 호출로 만든다(이슈 #139, #158 — reply를 위해 새 호출을 늘리지 않고
    카드 생성 호출에 얹는다). match_basis는 모델이 형식을 안 지키면 빈
    배열로 폴백하듯(_parse_match_basis), reply도 문자열이 아니면 None으로
    폴백한다 — candidates가 비어 호출 자체가 없었을 때와 같은 신호라,
    호출부(chat())가 이 경우들을 규칙 기반 문구로 채운다. LLM 장애 시
    degraded로 match_score 상위 카드를 대신 돌려준다(명세: reason_short는
    규칙, reason_long은 null. #129).

    on_reply 가 있으면(SSE·폴링) 모델이 쓰는 글을 흘려 받다가 reply 를 다 쓰면 _preview_reply 로 다듬어 거기로
    먼저 넘긴다. 결과는 같다.
    """
    if not candidates:
        return CardResult([], None, False)

    id_by_book = {c["book_id"]: c for c in candidates}
    inputs = {
        "recent_turns": _format_recent_turns(recent_turns or []),
        "message": message,
        "intro": _card_prompt_intro(spec) + _card_prompt_conditions(spec),
        "limit": CARD_LIMIT,
        "listing": _format_listing(candidates),
        "turn_end": _HAND_OVER if may_ask else _POINT_ONE,
    }

    try:
        if on_reply is None:
            # 프롬프트 채우기 → 모델 호출 → 답에서 글자 꺼내기 → JSON 읽기, 네 칸을 잇는다.
            chain = CARD_PROMPT | get_chat_model() | message_text | parse_json_response
            parsed = await invoke_chain(chain, inputs)
        else:
            parsed = await stream_json(
                CARD_PROMPT | get_chat_model(),
                inputs,
                streaming.ReplyPreview(on_reply, _preview_reply).feed,
            )
    except LLMUnavailableError:
        logger.exception("카드 생성 실패")
        return CardResult(_rule_based_cards(candidates), None, True)

    raw_cards = parsed.get("cards")
    if not isinstance(raw_cards, list):
        # 명세를 어긴 모양(cards가 null·문자열 등)을 그냥 슬라이스하면 TypeError가
        # 나 라우터의 어떤 try/except도 안 거치고 그대로 샌다(#201에서 실제로 확인).
        # LLM 장애와 똑같이 degraded로 규칙 기반 카드를 대신 낸다(#203) — 원문은
        # 안 남기고 타입만 로그에 남긴다(#202처럼 대화 내용을 로그에 남기지 않는다).
        logger.error(
            "카드 생성 응답의 cards가 배열이 아님: %s", type(raw_cards).__name__
        )
        return CardResult(_rule_based_cards(candidates), None, True)

    reply = parsed.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        reply = None
    else:
        reply = voice.without_echo(reply.strip())

    raw_cards = _order_by_picks(raw_cards, parsed.get("picks"))
    cards = []
    for rank, item in enumerate(raw_cards[:CARD_LIMIT], start=1):
        if not isinstance(item, dict):
            # 개별 항목이 문자열 등 dict가 아니면 그 카드만 건너뛴다(#203) — 다른
            # 항목이 멀쩡하면 그것까지 통째로 degraded 처리할 이유는 없다.
            continue
        book_id = item.get("book_id")
        book = id_by_book.get(book_id)
        if book is None:
            continue
        if _has_description(book):
            reason_short = item.get("reason_short")
            reason_long = _unless_copied(item.get("reason_long"), book)
            match_basis = _parse_match_basis(item.get("match_basis"))
        else:
            # 소개글이 없으면 근거로 쓸 글이 없다(#86). 모델이 뭘 썼든 쓰지 않고 규칙으로 쓴다:
            # 한 줄 이유는 찾은 책의 제목·저자만, 긴 이유와 근거는 비운다(명세 ③).
            reason_short = _no_description_reason(book)
            reason_long = None
            match_basis = []
        if not reason_short:
            # 명세: 한 줄 이유를 못 만든 책은 카드에서 뺀다.
            continue
        cards.append(
            {
                "isbn": book.get("isbn"),
                "rank": rank,
                "match_score": book["match_score"],
                "title": book["title"],
                "author": book["author"],
                "price": book.get("price"),
                "cover_url": book.get("cover_url"),
                "reason_short": reason_short,
                "reason_long": reason_long,
                "match_basis": match_basis,
            }
        )
    if reply is not None:
        reply = _without_unshown_titles(reply, [card["title"] for card in cards])
    if reply is not None:
        reply = voice.without_vague_question(reply)
    return CardResult(cards, reply, False)


def _preview_reply(reply: str) -> str:
    """카드보다 먼저 보낼 말풍선(SSE·폴링). 아래 generate_cards 의 후처리 중 카드 없이 할 수 있는 것만 한다.

    카드에 없는 책을 짚은 문장은 카드를 다 읽어야 알 수 있어 여기서 못 뺀다. 최종 reply 는 응답 본문의 것이다.
    """
    cleaned = _fit_reply(
        voice.without_vague_question(voice.without_echo(reply.strip()))
    )
    # 카드 턴이면 끝 질문은 카드 아래로 간다(#652). 먼저 보낸 말풍선에도 넣지 않아야 done 에서 글이 줄지 않는다
    return _split_turn_question(cleaned)[0]


# 말풍선이 한 권을 『제목』으로 짚게 한 뒤(#545) 카드에 없는 책을 짚은 일이 있었다. 대화 평가에서 고른 카드에 없는
# 『작은 행복』을 "좋습니다"라고 해, 사용자가 그 책을 말하자 ⑦이 "추천드린 책 목록에 없네요"라고 답했다(카드 턴 21개 중 1개).
# 화면에 없는 책을 권하는 문장은 뺀다. 제목은 괄호 머리말과 띄어쓰기를 지우고, 어느 한쪽이 다른 쪽을 담으면 같은 책으로 본다.
_NAMED_TITLE = re.compile(r"[『《]([^』》]+)[』》]")
_TITLE_PREFIX = re.compile(r"\([^)]*\)")


def _title_key(title: str | None) -> str:
    return _NON_WORD.sub("", _TITLE_PREFIX.sub("", title or "")).lower()


def _without_unshown_titles(
    reply: str,
    titles: list[str],
    split: Callable[[str], list[str]] = voice.sentences_of,
) -> str | None:
    """말풍선에서 카드에 없는 책 제목을 짚은 문장을 뺀다. 다 빠지면 None — 호출부가 기본 문구로 채운다."""
    keys = [key for key in map(_title_key, titles) if key]

    def shown(name: str) -> bool:
        key = _title_key(name)
        return not key or any(key in k or k in key for k in keys)

    kept = [
        sentence
        for sentence in split(reply)
        if all(shown(name) for name in _NAMED_TITLE.findall(sentence))
    ]
    return " ".join(kept) or None


# --- 책 추천 에이전트(CHAT_AGENT, 11/10 설계): 세 갈래 후보 → 판단 호출 → 카드·질문·한 번 더 찾기 ---
# 갈래 모으기는 app/chat/lanes.py, 판단 호출의 글과 이유 규칙은 app/chat/picking.py 에 있다. 여기서는 순서만 잇는다.
# 끄면(기본) 이 부분을 지나지 않고 지금의 검색 → 카드 생성 그대로다.
_LANE_ASK_REPLY = "찾으시는 것에 따라 두 갈래로 나뉘어요."
# 보여 준 카드가 여럿인데 "이거 읽은 사람들은 또 뭐 봐?"처럼 어느 책인지 말하지 않았을 때(lanes.fill_plan 의 ask_card)
_WHICH_CARD_REPLY = "보여 드린 책 중 어느 책을 말씀하시는 건가요?"
_WHICH_CARD_QUESTION = "골라 주시면 그 책을 읽은 분들이 함께 읽은 책을 찾아 드릴게요."


def _which_card_followup(shown_books: Sequence[Any]) -> str:
    """어느 카드인지 묻는 followup. 보여 준 책마다 "『제목』 함께 읽은 책 보여줘" 보기를 단다(#652) — 제목과 "함께 읽"
    낱말이 있어야 다음 턴이 그 카드를 짚어 함께 읽은 책 갈래로 간다(lanes.fill_plan)."""
    return (
        option_lines.with_options(
            _WHICH_CARD_QUESTION,
            [
                f"『{lanes.display_title(book.title)}』 함께 읽은 책 보여줘"
                for book in shown_books
            ],
            asking.FOLLOWUP_MAX_CHARS,
        )
        or _WHICH_CARD_QUESTION
    )


@dataclass
class _LaneTurn:
    """한 턴의 세 갈래 후보와, 한 번 더 찾을 때 다시 쓸 것들."""

    req: ChatRequest
    spec: Spec
    plan: lanes.Plan
    anchor: lanes.Anchor | None
    shown_ids: list[int]
    owned: list[int] | None
    graph: reading_graph.ReadingGraph | None
    skip: lanes.Skip
    pool: list[dict]


def _uses_lanes(spec: Spec, plan: lanes.Plan) -> bool:
    """세 갈래로 찾을 턴인가. 제목·저자를 콕 집은 요청은 그 책 자체를 찾는 말이라 지금처럼 검색만 한다."""
    return spec.intent == "semantic" or plan.anchor is not None


async def _no_books() -> list[dict]:
    return []


async def _near_lane(
    req: ChatRequest,
    spec: Spec,
    anchor: lanes.Anchor,
    shown_ids: list[int],
    owned: list[int] | None,
) -> list[dict]:
    """마니아 목록 없는 기준 책의 "비슷한 책 더"(#654). 기준 책과 소개가 가까운 책을 모은다.

    기준 책 벡터가 없으면(소개글 없는 책) 기준 책 제목으로 찾는다 — spec 에 남은 앞 턴 주제로는 찾지 않는다. 사용자 조건
    (가격·연도·분야)은 그대로 둔다. 가까운 책 조회가 실패해도 제목으로 찾는다(턴을 500 으로 끝내지 않는다).
    """
    try:
        books = await lanes.near_books(anchor)
    except Exception:
        logger.exception("기준 책과 가까운 책 찾기 실패 — 기준 책 제목으로 찾는다")
        books = None
    if books is not None:
        return books
    by_title = spec.model_copy(
        update={
            "intent": "semantic",
            "semantic": lanes.display_title(anchor.title)[:MAX_QUERY_CHARS],
            "exact": SpecExact(),
        }
    )
    return await get_candidates(by_title, shown_ids, req.user_id, owned_book_ids=owned)


async def _recalled_books(
    recalling: "asyncio.Task[tuple[lanes.Title, ...]]",
) -> list[dict]:
    """B — 떠올린 제목 호출을 받아 책 표에서 찾는다."""
    return await lanes.titled_books(await _recalled_within_grace(recalling))


async def _neighbor_books(
    graph: reading_graph.ReadingGraph,
    anchor: lanes.Anchor,
    plan: lanes.Plan,
    skip: lanes.Skip,
) -> list[dict]:
    """C — 기준 책 이웃. DB 조회가 실패하면 이 갈래만 빼고 검색·떠올린 제목으로 고른다(턴을 500 으로 끝내지 않는다).

    관계를 물은 턴(함께 읽은 책·비슷한 책)은 이 갈래가 답이라 카드 장수만큼은 허브로라도 채운다. 다른 턴은 검색이 있다.
    """
    try:
        return await lanes.neighbor_books(
            graph,
            anchor,
            lanes.relation_kinds(plan.relation),
            skip=skip,
            fill_to=CARD_LIMIT if plan.relation is not None else 0,
        )
    except Exception:
        logger.exception("기준 책 이웃 찾기 실패 — 이 갈래 없이 찾는다")
        return []


async def _settle_pool(
    turn: _LaneTurn,
    searched: list[dict],
    *others: list[dict],
    others_first: bool = False,
) -> list[dict]:
    """갈래들을 합치고 뺄 책을 뺀 뒤, 새로 들어온 책에 점수와 이웃 줄을 붙인다.

    검색(A)은 get_candidates 가 사용자 조건·뺄 책·점수를 이미 다뤘다. 다른 갈래 책에는 여기서 같은 조건을 건다.
    목록 순서는 검색 먼저다. others_first 면 다른 갈래를 앞에 둔다(비슷한 책을 더 원할 때 — 마니아 목록 중심).
    """
    spec, graph = turn.spec, turn.graph
    passed = [
        [b for b in lane if lanes.passes(b, spec.filters, spec.exact.publisher)]
        for lane in others
    ]
    pool = [
        b
        for b in lanes.merge(
            *((*passed, searched) if others_first else (searched, *passed)),
            graph=graph,
        )
        if not turn.skip.blocks(b, graph)
    ]
    unscored = [b for b in pool if "match_score" not in b]
    if unscored:
        async with db.get_pool().acquire() as conn:
            await _attach_match_scores(
                conn,
                unscored,
                turn.req.user_id,
                spec,
                query_vector=await _anchor_vector(conn, turn),
                # 관계를 물은 턴이면 기준 책 벡터가 없어도 책끼리 식으로 매긴다(목록 책의 바닥을 받는다)
                book_to_book=turn.anchor is not None and turn.plan.relation is not None,
            )
    if graph is not None:
        try:
            await lanes.attach_neighbor_lines(
                graph, [b for b in pool if "neighbors" not in b], turn.skip
            )
        except Exception:
            # 이웃 줄은 말풍선 제안에만 쓴다. 못 붙여도 카드는 낼 수 있으니 줄 없이 간다(판단 프롬프트·제안은 빈 줄을 받는다)
            logger.exception("이웃 줄 붙이기 실패 — 이웃 줄 없이 고른다")
    return pool


async def _anchor_vector(
    conn: asyncpg.Connection, turn: _LaneTurn
) -> list[float] | None:
    """관계를 물은 턴이면 기준 책의 벡터. 다른 갈래 책의 매칭 % 를 이번 요청 문장 대신 이것과 견줘 매긴다.

    "이 책 읽은 사람들은 또 뭐 읽었어?"의 요청 문장은 기준 책 제목뿐이라, 그와 견주면 함께 읽은 책이 모두 0~5% 로
    나왔다(11/10 점검). 사용자가 물은 것은 기준 책과의 관계라 기준 책과 견준다. 벡터가 없는 책(소개글 없음)이면 None.
    """
    anchor = turn.anchor
    if anchor is None or anchor.book_id is None or turn.plan.relation is None:
        return None
    raw = await conn.fetchval(
        "SELECT embedding::text FROM book_embeddings WHERE book_id = $1", anchor.book_id
    )
    return json.loads(raw) if raw else None


async def _gather_lanes(
    req: ChatRequest,
    spec: Spec,
    plan: lanes.Plan,
    shown_cards: list[tuple[int, str]],
    shown_books: list[shown.ShownBook],
    shown_ids: list[int],
    owned: list[int] | None,
    *,
    recalling: "asyncio.Task[tuple[lanes.Title, ...]] | None" = None,
) -> _LaneTurn:
    """검색(A)·떠올린 제목(B)·기준 책 이웃(C)을 나란히 모아 한 목록으로 만든다.

    사용자가 기준 책을 읽은 사람들이 함께 읽은 책을 물으면(relation co) 그 목록에서만 고른다. 검색어로 찾은 책이
    섞이면 "함께 읽은 책"이라는 답이 틀린다. 기준 책의 목록이 없으면 지금처럼 찾는다.
    비슷한 책을 더 원하는데(relation similar) 기준 책에 마니아 목록이 없으면 검색 대신 기준 책과 소개가 가까운 책을
    모은다(#654).

    recalling 은 1단계와 나란히 보낸 떠올린 제목 호출이다. 기준 책이 있는 턴에는 쓰지 않는다(기준 책 목록을 쓴다).
    """
    graph = reading_graph.get()
    anchor = await lanes.resolve_anchor(plan, shown_cards, shown_books, graph)
    excluded = set(shown_ids) | set(spec.exclude)
    skip = lanes.Skip(
        book_ids=excluded | set(owned or []),
        edition_keys=await _fetch_edition_keys(list(excluded)),
    )
    if anchor is not None and anchor.book_id is not None:
        skip.book_ids.add(anchor.book_id)
    if anchor is not None and anchor.work is not None:
        skip.works.add(anchor.work)
    if anchor is not None and (title := lanes.title_key(anchor.title)):
        skip.titles.add(title)
    # "이 작가 말고": 기준 책 저자의 책은 한 권도 내지 않는다(한 장까지 허용하는 저자 제한보다 세다)
    if (
        plan.other_author
        and anchor is not None
        and (author := editions.author_key(anchor.author))
    ):
        skip.authors.add(author)
    has_lists = (
        graph is not None
        and anchor is not None
        and anchor.work is not None
        and graph.has_lists(anchor.work)
    )
    relation_only = plan.relation == "co" and has_lists
    # "비슷한 책 더"인데 기준 책에 마니아 목록이 없다(#654). spec 에 남은 앞 턴 주제로 찾으면 다른 주제의 책이 나와서
    # 검색(A) 대신 기준 책과 소개가 가까운 책을 찾는다(_near_lane)
    near_anchor = (
        plan.relation == "similar"
        and anchor is not None
        and not lanes.has_mania(graph, anchor)
    )
    search_spec = spec
    if not _query_text(spec) and plan.anchor is not None and not has_lists:
        # 기준 책을 짚었는데 못 찾았거나 그 책의 목록이 없다. 검색어도 없으면 카드가 0장이라 사용자 말로 찾는다.
        # 제목으로 기준 책을 짚는 말("데미안 읽은 사람들은 또 뭐 읽어?")에는 1단계가 검색어를 비우는 일이 많다(11/10 실험 1)
        search_spec = spec.model_copy(
            update={"semantic": (req.message or "")[:MAX_QUERY_CHARS] or None}
        )
    if anchor is not None:
        _drop(recalling)
        recalling = None
    started = asyncio.get_running_loop().time()
    searched, titled, related, near = await asyncio.gather(
        get_candidates(search_spec, shown_ids, req.user_id, owned_book_ids=owned)
        if _query_text(search_spec) and not relation_only and not near_anchor
        else _no_books(),
        _recalled_books(recalling) if recalling is not None else _no_books(),
        _neighbor_books(graph, anchor, plan, skip)
        if graph is not None and anchor is not None
        else _no_books(),
        _near_lane(req, spec, anchor, shown_ids, owned)
        if near_anchor and anchor is not None
        else _no_books(),
    )
    turn = _LaneTurn(req, spec, plan, anchor, shown_ids, owned, graph, skip, [])
    # 비슷한 책을 더 원하면 기준 책의 마니아 목록을 앞에 둔다. 11/10 점검에서 검색 결과가 앞에 있자 판단 호출이 『데미안』
    # 마니아 목록 대신 검색에 걸린 자기계발 "성장" 책 3권을 골랐다
    if plan.relation == "similar" and related:
        turn.pool = await _settle_pool(
            turn, searched, related, titled, others_first=True
        )
    else:
        turn.pool = await _settle_pool(turn, searched, near, titled, related)
    note = ""
    if relation_only:
        note = " — 함께 읽은 책만"
    elif near_anchor:
        note = f" — 검색 대신 기준 책과 가까운 책 {len(near)}권"
    logger.info(
        "세 갈래: 검색 %d · 떠올린 제목 %d · 기준 책 이웃 %d → 후보 %d권(%.2f초)%s",
        len(searched),
        len(titled),
        len(related),
        len(turn.pool),
        asyncio.get_running_loop().time() - started,
        note,
    )
    return turn


async def _judge(
    turn: _LaneTurn, *, may_ask: bool, may_search: bool, on_reply: ReplySink | None
) -> picking.Decision | None:
    """판단 호출. 실패하면 None(호출부가 규칙 카드로 대신한다)."""
    inputs = picking.prompt_inputs(
        recent_turns=_format_recent_turns(turn.req.recent_turns),
        message=turn.req.message or "",
        intro=_card_prompt_intro(turn.spec) + _card_prompt_conditions(turn.spec),
        listing=picking.format_listing(turn.pool),
        limit=CARD_LIMIT,
        may_ask=may_ask,
        may_search=may_search,
        relation=turn.plan.relation if turn.anchor is not None else None,
    )
    try:
        if on_reply is None:
            chain = (
                picking.JUDGE_PROMPT
                | get_chat_model()
                | message_text
                | parse_json_response
            )
            answer = await invoke_chain(chain, inputs)
        else:
            preview = streaming.ReplyPreview(on_reply, _preview_reply)

            async def feed(text: str) -> None:
                # 한 번 더 찾을 때는 이 말풍선을 쓰지 않으니 먼저 보내지 않는다
                if picking.streams_reply(text):
                    await preview.feed(text)

            answer = await stream_json(
                picking.JUDGE_PROMPT | get_chat_model(), inputs, feed
            )
    except LLMUnavailableError:
        logger.exception("판단 호출 실패")
        return None
    return picking.parse(answer, may_ask=may_ask, may_search=may_search)


async def _search_again(turn: _LaneTurn, query: str) -> None:
    """판단 호출이 낸 새 검색어로 검색(A)만 다시 하고, 떠올린 제목·기준 책 이웃은 그대로 둔다."""
    retry = turn.spec.model_copy(
        update={
            "intent": "semantic",
            "semantic": query,
            "exact": turn.spec.exact.model_copy(update={"title": None, "author": None}),
        }
    )
    searched = await get_candidates(
        retry, turn.shown_ids, turn.req.user_id, owned_book_ids=turn.owned
    )
    kept = [b for b in turn.pool if any(lane != "search" for lane, _ in b["sources"])]
    turn.pool = await _settle_pool(turn, searched, kept)
    logger.info("다시 찾기: 검색 %d권 → 후보 %d권", len(searched), len(turn.pool))


def _graph_source(book: dict) -> tuple[str, str] | None:
    return next(
        (
            (lane, anchor)
            for lane, anchor in book.get("sources") or []
            if lane in ("co", "mania")
        ),
        None,
    )


def _lane_reasons(
    item: dict, book: dict, spec: Spec, *, relation_asked: bool = False
) -> tuple[str, Any, list[dict]] | None:
    """카드 한 장의 (한 줄 이유, 긴 이유, 근거). 한 줄 이유를 못 만들면 None(카드에서 뺀다).

    소개글이 있으면 모델이 소개로 쓴 이유를 쓴다. 그래프에서 온 책이면 관계 사실을 근거 맨 앞에 두고, 모델 글이
    목록으로 알 수 없는 것("많이 읽은")을 말하면 관계 사실 문장으로 바꾼다. 소개글이 없으면 모델 글을 쓰지 않는다
    (#86): 그래프 책은 관계 사실, 떠올린 책은 확인된 칸(저자·연도·분류), 제목·저자로 찾은 책은 "찾으신 …"이다.

    사용자가 기준 책과의 관계("이 책 읽은 사람들은 또 뭐 읽었어?")를 물었으면(relation_asked) 그래프 책의 한 줄 이유는
    소개글이 있어도 관계 사실이다. 그 물음의 답이 관계다.
    """
    source = _graph_source(book)
    if _has_description(book):
        reason_short = item.get("reason_short")
        reason_long = _unless_copied(item.get("reason_long"), book)
        basis = picking.without_placeholder_basis(
            _parse_match_basis(item.get("match_basis"))
        )
        if source is not None:
            if (
                relation_asked
                or not isinstance(reason_short, str)
                or picking.overclaims(reason_short)
            ):
                reason_short = picking.relation_reason(*source)
            if isinstance(reason_long, str) and picking.overclaims(reason_long):
                reason_long = None
            basis = [picking.relation_basis(*source), *basis][:3]
    elif source is not None:
        reason_short = picking.relation_reason(*source)
        reason_long, basis = None, [picking.relation_basis(*source)]
    elif spec.intent == "exact":
        reason_short, reason_long, basis = _no_description_reason(book), None, []
    else:
        reason_short, reason_long, basis = picking.confirmed_reason(book), None, []
    if not isinstance(reason_short, str) or not reason_short.strip():
        return None
    return reason_short.strip(), reason_long, basis


def _lane_reply(
    reply: str | None, picked: list[dict], proposed: Sequence[str]
) -> str | None:
    """말풍선 후처리. 카드에 없는 책을 짚은 문장, 목록으로 알 수 없는 것을 말한 문장을 뺀다. 두 갈래 제안을 답
    보기(proposed, picking.proposal_options)로 내면 모델이 이웃 줄의 책을 짚은 문장도 뺀다 — 같은 제안이 말풍선과 보기에
    두 번 나가지 않게."""
    if reply is None:
        return None
    neighbors = [b.get("neighbors") or {} for b in picked]
    allowed = [b["title"] for b in picked]
    if not proposed:
        allowed += [
            t
            for n in neighbors
            for kind in reading_graph.KINDS
            for t in n.get(kind, [])
        ]
    co_titles = {t for n in neighbors for t in n.get("co", [])}
    co_titles |= {
        b["title"] for b in picked if any(lane == "co" for lane, _ in b["sources"])
    }
    cleaned = _without_unshown_titles(
        voice.without_echo(reply), allowed, picking.sentences
    )
    if cleaned is not None:
        cleaned = picking.without_overclaims(cleaned, co_titles)
    if cleaned is not None:
        cleaned = voice.without_vague_question(cleaned)
    return cleaned


def _lane_cards(
    decision: picking.Decision, turn: _LaneTurn
) -> tuple[list[dict], str | None, list[str]]:
    """판단 호출이 고른 책으로 카드를 만든다. "비슷한 책" 턴에서는 같은 저자 책을 한 장까지 낸다.

    같은 저자 쏠림(25%)은 기준 책의 마니아 목록에서 잰 값이라, 그 목록을 데려오는 "비슷한 책" 턴에만 건다(#674·#675).
    판단 호출에도 그 턴에만 같은 저자 책은 한 권까지라고 시킨다(picking._RELATION_RULE). 이 제한은 말풍선을 쓴 뒤에 카드를
    빼는 것이라 다른 턴에도 걸면 말풍선과 카드 장수가 어긋난다. 일반 추천 턴에서는 "같은 작가 책 두 권을 골랐어요" 아래에
    카드가 한 장만 남았고(11/11 dev 멀티턴 시험), 함께 읽은 책 턴에서는 줄리언 반스 책의 함께 읽은 책이 전부 반스 책이라
    카드가 1장만 남았다(11/11 실측). 저자를 콕 집은 요청은 그 저자 책만 찾는 말이라 걸지 않는다.
    """
    by_id = {b["book_id"]: b for b in turn.pool}
    one_per_author = (
        turn.anchor is not None
        and turn.plan.relation == "similar"
        and not turn.spec.exact.author
    )
    cards: list[dict] = []
    picked: list[dict] = []
    authors: set[str] = set()
    for item in decision.cards:
        if len(cards) == CARD_LIMIT:
            break
        if not isinstance(item, dict):
            continue
        book = by_id.get(item.get("book_id"))
        if book is None or any(p is book for p in picked):
            continue
        author = editions.author_key(book.get("author"))
        if one_per_author and author and author in authors:
            continue
        reasons = _lane_reasons(
            item,
            book,
            turn.spec,
            relation_asked=turn.anchor is not None and turn.plan.relation is not None,
        )
        if reasons is None:
            continue
        reason_short, reason_long, basis = reasons
        authors.add(author)
        picked.append(book)
        cards.append(
            {
                "isbn": book.get("isbn"),
                "rank": len(cards) + 1,
                "match_score": book["match_score"],
                "title": book["title"],
                "author": book["author"],
                "price": book.get("price"),
                "cover_url": book.get("cover_url"),
                "reason_short": reason_short,
                "reason_long": reason_long,
                "match_basis": basis,
            }
        )
    proposed = picking.proposal_options(picked)
    return cards, _lane_reply(decision.reply, picked, proposed), proposed


def _fallback_pool(pool: list[dict]) -> list[dict]:
    """판단 호출이 실패했을 때 규칙 카드를 고를 후보. 사용자 말로 찾은 책(검색·떠올린 제목)이 있으면 그것만 본다."""
    asked = [
        b for b in pool if any(lane in ("search", "title") for lane, _ in b["sources"])
    ]
    return asked or pool


async def _pick_from_lanes(
    turn: _LaneTurn, *, may_ask: bool, on_reply: ReplySink | None
) -> tuple[CardResult, tuple[str, str] | None]:
    """판단 호출로 고른다. (카드 결과, 카드 대신 물을 (reply, followup)). 한 번 더 찾기는 턴마다 한 번이다."""
    if not turn.pool:
        return CardResult([], None, False), None
    decision = await _judge(turn, may_ask=may_ask, may_search=True, on_reply=on_reply)
    if decision is not None and decision.action == "search_again":
        logger.info("판단 호출이 놓친 조건이 있다고 해 한 번 더 찾는다")
        try:
            await _search_again(turn, decision.query or "")
        except Exception:
            logger.exception("다시 찾기 실패 — 처음 후보로 고른다")
        decision = await _judge(
            turn, may_ask=may_ask, may_search=False, on_reply=on_reply
        )
    if decision is None:
        return CardResult(
            _rule_based_cards(_fallback_pool(turn.pool)), None, True
        ), None
    logger.info("판단 호출: %s", decision.action)
    if decision.action == "ask":
        return CardResult([], None, False), (
            _fit_reply(decision.reply or _LANE_ASK_REPLY),
            option_lines.with_options(
                _fit_followup(decision.question or ""),
                decision.options,
                asking.FOLLOWUP_MAX_CHARS,
            )
            or "",
        )
    cards, reply, proposed = _lane_cards(decision, turn)
    return CardResult(cards, reply, False, tuple(proposed)), None


# 되묻기 1단계(#283) — 카드가 없을 때 followup을 상황에 맞는 질문으로 만든다. LLM은 더 안 부른다.
_CLARIFY_CATEGORIES = ("소설", "에세이", "인문", "경제경영", "자기계발", "과학")
# 분야 답 보기(#652). 누르면 이 말이 그대로 다음 메시지가 된다 — 한 낱말보다 문장으로 답하게 한다(아래 _no_card_response)
_CLARIFY_OPTIONS = tuple(f"{category} 책 추천해줘" for category in _CLARIFY_CATEGORIES)
_CLARIFY_ECHO_MAX_CHARS = 20


def _won(amount: int) -> str:
    return f"{amount // 10000}만원" if amount % 10000 == 0 else f"{amount:,}원"


def _active_conditions(spec: Spec) -> list[tuple[str, str]]:
    """걸려 있는 조건을 (사용자에게 보일 말, 풀어 달라고 할 때 따라 말할 예시)로 돌려준다."""
    f = spec.filters
    found: list[tuple[str, str]] = []
    if f.price_max is not None:
        found.append((f"{_won(f.price_max)} 이하", "가격은 상관없어요"))
    if f.price_min:  # 0은 조건이 아니다. LLM이 "저렴한"에 0을 넣는 경우가 있다.
        found.append((f"{_won(f.price_min)} 이상", "가격은 상관없어요"))
    if f.pub_year_from is not None:
        found.append((f"{f.pub_year_from}년 이후", "출간연도는 상관없어요"))
    if f.pub_year_to is not None:
        found.append((f"{f.pub_year_to}년 이전", "출간연도는 상관없어요"))
    if f.category:
        found.append((f.category, "분야는 상관없어요"))
    # 재고 조건은 짚지 않는다. 챗봇은 이 조건을 쓰지 않는다(#319).
    if spec.exact.publisher:
        found.append((f"{spec.exact.publisher} 책", "출판사는 상관없어요"))
    return found


def _no_card_response(
    spec: Spec,
    message: str,
    *,
    had_candidates: bool,
    slot_question: str | None = None,
) -> tuple[str, str]:
    """카드가 없을 때의 (reply, followup). reply는 짧은 안내, followup은 사용자가 답할 질문이다.

    검색어가 비었으면(조건이 걸려 있어도) 주제부터 묻는다 — 조건을 풀어도 찾을 게 없다.
    검색어가 있고 걸린 조건이 있으면 그 조건을 풀지 묻고(따라 말할 예시를 준다), 검색어는
    있는데 못 찾았으면 제목·저자나 분야를 묻는다. 다음 턴의 답은 1단계(spec 갱신)가 그대로
    받는다. 답 예시는 한 단어가 아니라 문장으로 줘서 사용자가 문장으로 답하게 한다.
    """
    if had_candidates:
        # 후보는 있었는데 카드를 못 만들었다(카드 생성이 비어 돌아옴). 조건 탓이 아니다.
        return (
            "추천을 만들지 못했어요.",
            "같은 요청을 한 번만 다시 말씀해 주시겠어요?",
        )
    if slot_question:
        # "아이한테 읽어줄 책"에서 LLM이 지어낸 조건 대신, 정말 빠진 정보(나이)를 묻는다.
        return "조건에 맞는 책을 아직 못 찾았어요.", slot_question
    conditions = _active_conditions(spec)
    names = ", ".join(name for name, _ in conditions)
    if not _query_text(spec):
        held = f" (지금 걸린 조건: {names})" if conditions else ""
        return (
            "추천해 드리려면 조금 더 알아야 해요.",
            _with_clarify_options(
                f"어떤 분야가 좋으세요? 골라 주시거나 알고 있는 제목·저자를 알려 주세요.{held}"
            ),
        )
    if conditions:
        return (
            "조건에 맞는 책을 아직 못 찾았어요.",
            option_lines.with_options(
                f"{names} 조건이 걸려 있어요. 조건을 넓혀서 다시 찾아볼까요?",
                [example for _, example in conditions],
                asking.FOLLOWUP_MAX_CHARS,
            )
            or f"{names} 조건이 걸려 있어요. 조건을 넓혀서 다시 찾아볼까요?",
        )
    said = message.strip()
    query = _query_text(spec)
    if said and not _query_from_message(query, said):
        # 검색어가 앞 턴에서 이어졌다("추리소설 입문" 뒤의 "다른 거 더 없어?", "일본 작가 말고"). 이번 말만 짚으면
        # 앞의 조건을 버린 것처럼 들리고, 분야 보기(소설·에세이…)는 처음부터 다시 고르라는 말이 된다(#680)
        shown = (
            query
            if len(query) <= _CARRIED_QUERY_MAX_CHARS
            else query[:_CARRIED_QUERY_MAX_CHARS] + "…"
        )
        # reply 가 이미 "못 찾았어요"라고 하므로 followup 은 다시 말하지 않고 검색어를 짚어 바로 묻는다(#685 리뷰)
        return (
            "조건에 맞는 책을 아직 못 찾았어요.",
            (
                f'"{shown}"에서 조건을 하나 줄이거나 다른 말로 바꿔 말씀해 주시겠어요? '
                "아는 제목이나 저자를 알려 주셔도 돼요."
            ),
        )
    head = (
        f'"{said}"만으로는 찾기 어려워요.'
        if said and len(said) <= _CLARIFY_ECHO_MAX_CHARS
        else "말씀하신 내용만으로는 찾기 어려워요."
    )
    return (
        "조건에 맞는 책을 아직 못 찾았어요.",
        _with_clarify_options(
            f"{head} 제목이나 저자를 알려 주시거나, 분야를 골라 주세요."
        ),
    )


# 앞 턴에서 이어진 검색어를 답에 따라 쓸 때의 최대 길이. 넘으면 자르고 "…"를 붙인다
_CARRIED_QUERY_MAX_CHARS = 30
_QUERY_WORD = re.compile(r"[0-9A-Za-z가-힣]{2,}")


def _query_from_message(query: str, message: str) -> bool:
    """검색어를 이번 말이 정했는가. 검색어 낱말의 절반 이상이 이번 말에 있으면 그렇다고 본다.

    낱말은 앞 두 글자로 견준다. 조사가 붙은 꼴("추리소설을", "작가를")도 같은 낱말로 센다.
    """
    words = _QUERY_WORD.findall(query)
    if not words:
        return True
    flat = re.sub(r"\s+", "", message)
    said = sum(1 for word in words if word[:2] in flat)
    return said * 2 >= len(words)


def _with_clarify_options(question: str) -> str:
    """분야를 묻는 followup 에 분야 답 보기를 단다(#652)."""
    return (
        option_lines.with_options(question, _CLARIFY_OPTIONS, asking.FOLLOWUP_MAX_CHARS)
        or question
    )


# 되묻기 2단계(#283) — 모호한 요청은 규칙으로 가려 낸다. LLM은 더 안 부른다.
# 1) 답할 수 없는 요청(가리킬 책이 없는 지시어, 알아들을 수 없는 입력)은 검색·추천을 건너뛰고
#    cards: [] + followup으로 되묻는다(명세: 카드가 없을 때 followup).
# 2) 답은 할 수 있지만 정보가 빠진 요청(선물, 아이, 저렴한, 최근)은 첫 턴에만 추천을 주고
#    reply 끝에 질문 한 줄을 붙인다. 명세의 reply 최대 200자는 지킨다.
_REPLY_MAX_CHARS = 200
# 지시어는 낱말 앞머리에서만 잡는다. "학생이거든요", "어린이 책", "아까운"처럼 다른 낱말 속에 든 글자는 지시어가 아니다.
# 가리키는 곳이 다른 두 갈래로 나눈다(#359). "이거", "그 책"은 같은 메시지의 앞말을 가리킬 수 있고
# ("토비의 스프링은 어려웠어. 그것보다 쉬운 책"), "아까", "첫 번째"는 앞선 턴만 가리킨다.
_PRONOUN = re.compile(
    r"(?<![가-힣])(이거|그거|저거|이것(?!저것)|그것|저것|[이그저] 책)"
)
_EARLIER_TURN = re.compile(
    r"(아까(?![운워웠우])|방금|조금 전|위에서|앞에서|[첫두세네]\s?번째|\d+번째|\d+번 책)"
)
# 지시어 앞에 이만큼 글자가 있으면 가리킬 말이 같은 메시지에 있다고 본다. "아", "음", "그럼" 같은
# 짧은 말머리는 가리킬 말이 아니다. 책 제목은 "데미안"처럼 석 자부터 흔하다.
_ANTECEDENT_MIN_CHARS = 3
_ONLY_JAMO = re.compile(r"^[\sㄱ-ㅎㅏ-ㅣ!?.~,;]+$")
_DIGIT = re.compile(r"\d")
# 출간 시기를 이미 정한 말. 연도("2024년")뿐 아니라 기간("2~3년 안", "3년 이내", "6개월")도 정한 것이다 — "최근 2~3년
# 안에 나온 걸로"에 '"최근"은 어느 정도까지 보시나요?'라고 다시 물었다(11/11 dev, #693)
_YEAR_WORD = re.compile(r"(\d+\s*(년|개월)|올해|작년|재작년|금년)")
_RECIPIENT = re.compile(
    r"(엄마|아빠|어머니|아버지|부모|친구|연인|남자친구|여자친구|남편|아내|동료|상사"
    r"|선생|할머니|할아버지|형|누나|동생|언니|오빠|딸|아들)"
)
_AGE_GIVEN = re.compile(r"(\d+\s*(세|살|학년)|유치원|초등|중학|고등)")
# (이름, 모호하다고 보는 말, 이미 구체적이라 묻지 않는 말, 질문)
_SLOT_RULES = (
    (
        "age",
        # "아이슬란드", "아이유", "아이스크림"처럼 이어지는 낱말은 거르려고, 뒤에 한글이 없거나 조사·책이 올 때만 잡는다.
        re.compile(
            r"(?<![가-힣])(아이|아기|애기|어린이|유아|꼬마)"
            r"(?=$|[^가-힣]|가|는|은|를|을|도|한테|에게|에|랑|와|과|용|들|의|께|만|로|책)"
        ),
        _AGE_GIVEN,
        '아이가 몇 살쯤인가요? 나이를 알려 주시면 더 맞게 골라 드려요. 예) "5세", "초등 저학년"',
    ),
    (
        "gift",
        re.compile(r"선물"),
        _RECIPIENT,
        "받는 분이 어떤 분인가요? 나이, 취향, 예산을 알려 주시면 더 맞게 골라 드려요.",
    ),
    (
        "budget",
        re.compile(r"(저렴|가성비|값싼|(?<![가-힣])(싼|싸게))"),
        _DIGIT,
        '예산은 어느 정도로 생각하세요? 예) "1만원 이하"',
    ),
    (
        "recent",
        # "요즘"은 "요새"라는 뜻으로 더 많이 쓰여서("요즘 우울한데") 신간 의도로 보지 않는다.
        re.compile(r"(최근|신간|새로\s?나온)"),
        _YEAR_WORD,
        '"최근"은 어느 정도까지 보시나요? 예) "작년 이후", "2024년 이후"',
    ),
)


def _points_nowhere(text: str) -> bool:
    """첫 턴 메시지에 가리킬 말이 없는 지시어가 있는지.

    앞선 턴을 가리키는 말은 첫 턴에 가리킬 것이 없다. "이거", "그 책"은 그 앞에 다른 말이
    있으면 같은 메시지 안에서 가리킬 수 있다고 보고 1단계 LLM에게 넘긴다 — 책 이름을 말한
    사용자에게 "어떤 책을 말씀하시는 건가요?"로 되묻는 쪽이 더 나쁘다(평가 #322: 5번 중 5번).
    글자 수로만 보므로 "나 요즘 우울한데 그 책 추천해줘"처럼 가리킬 말 없이 긴 문장도 넘어간다.
    """
    if _EARLIER_TURN.search(text):
        return True
    pronoun = _PRONOUN.search(text)
    if pronoun is None:
        return False
    said_before = _NON_WORD.sub("", text[: pronoun.start()])
    return len(said_before) < _ANTECEDENT_MIN_CHARS


def _unanswerable_request(
    message: str, spec: Spec, recent_turns: list[Turn]
) -> tuple[str, str] | None:
    """검색·추천을 해 봐야 소용없는 요청의 (reply, followup). 해당 없으면 None."""
    text = message.strip()
    if _ONLY_JAMO.match(text):
        return (
            "무슨 말씀인지 잘 모르겠어요.",
            '찾고 싶은 책이나 읽고 싶은 분위기를 다시 말씀해 주시겠어요? 예) "잔잔한 소설 추천해줘"',
        )
    if not recent_turns and spec.anchor_book is None and _points_nowhere(text):
        # 첫 턴이라 "이거", "그 책"이 가리킬 책이 없다. 서버는 대화를 저장하지 않아 요청에 실린 것만 안다.
        return (
            "어떤 책을 말씀하시는 건가요?",
            "책 제목을 알려 주시면 그 책을 기준으로 찾아 드릴게요.",
        )
    return None


def _slot_question(message: str) -> str | None:
    """추천은 할 수 있지만 빠진 정보가 있는 요청이면 물을 질문 한 줄. 여럿이어도 하나만 묻는다."""
    for _, trigger, specific, question in _SLOT_RULES:
        if trigger.search(message) and not specific.search(message):
            return question
    return None


def _split_turn_question(reply: str) -> tuple[str, str | None]:
    """카드 턴 말풍선 끝의 묻는 문장을 떼어 낸다(#652). 그 질문은 카드 아래(followup)에 보인다 — 카드를 보기 전에
    "어느 쪽이 좋으세요?"를 먼저 읽지 않게. 말풍선이 그 한 문장뿐이면 떼지 않는다(말풍선은 비면 안 된다)."""
    parts = picking.sentences(reply)
    if len(parts) < 2 or not parts[-1].endswith("?"):
        return reply, None
    return " ".join(parts[:-1]), parts[-1]


def _fit_reply(reply: str) -> str:
    """명세의 reply 최대 200자를 지킨다. 넘으면 뒷 문장부터 덜어 내고, 한 문장만으로도 넘으면 자른다."""
    if len(reply) <= _REPLY_MAX_CHARS:
        return reply
    sentences = re.split(r"(?<=[.!?])\s+", reply.strip())
    while len(sentences) > 1 and len(" ".join(sentences)) > _REPLY_MAX_CHARS:
        sentences = sentences[:-1]
    fitted = " ".join(sentences)
    if len(fitted) <= _REPLY_MAX_CHARS:
        return fitted
    return fitted[: _REPLY_MAX_CHARS - 1] + "…"


# 넓은 첫 요청에 한 번 묻기(#530, app/chat/asking.py). 대화의 첫 요청에서만 묻는다. 그 뒤로도 물으면 설문지가 된다.
# 서버는 대화를 저장하지 않아 첫 요청인지 따로 모른다. 그래서 최근 대화에 사용자 말이 있는지로 본다.
def _ask_stage(req: ChatRequest) -> asking.Stage:
    """이번 턴이 묻기의 어느 단계인가. 직전 질문의 답이면 스위치와 상관없이 answered 다(그 사이 스위치를 꺼도 답은 받는다)."""
    if asking.asked(req.recent_turns):
        return "answered"
    if not get_settings().chat_narrowing:
        return "none"
    if any(turn.role == "user" for turn in req.recent_turns):
        return "none"
    return "may_ask"


def _bot_asked_last(turns: list[Turn]) -> bool:
    """직전 챗봇 말이 질문이었나(#545). BE 는 챗봇 줄에 reply 와 followup 을 같이 싣는다."""
    last = turns[-1] if turns else None
    return last is not None and last.role == "assistant" and "?" in last.text


def _is_pinned(spec: Spec) -> bool:
    """제목·저자를 콕 집은 요청인가. 이런 요청은 물을 게 없다."""
    return spec.intent == "exact" or bool(spec.exact.title or spec.exact.author)


# 대답 턴(#530) — 새 책을 찾는 말이 아닌 질문에는 검색하지 않고 답한다. 1단계가 turn: answer 로 가려 준다.
# 보여 준 책에 대한 답은 그 책 소개로만 한다. 카드는 0장이고, 명세상 카드가 없으면 followup 이 있어야 해서 대화를
# 이어 갈 질문을 같이 받는다. 호출은 1단계와 이것, 두 번이다.
ANSWER_DESCRIPTION_CHARS = 300
SPEC_SHOWN_DESCRIPTION_CHARS = 80
_ANSWER_FALLBACK_REPLY = "지금은 답을 드리기 어려워요."
_ANSWER_FALLBACK_FOLLOWUP = "잠시 뒤에 다시 물어봐 주시겠어요?"
_ANSWER_DEFAULT_FOLLOWUP = "다른 책도 더 찾아볼까요?"
_FOLLOWUP_MAX_CHARS = 100

ANSWER_PROMPT = ChatPromptTemplate.from_template(
    "너는 책 추천 챗봇이다. 사용자와 대화하며 원하는 책을 함께 좁혀 찾아 준다. "
    "장바구니 담기와 주문은 다른 기능이 맡는다.\n\n"
    "최근 대화:\n{recent_turns}\n\n"
    "마지막으로 보여 준 책(번호는 화면의 카드 순서, 모두 이 서점에서 파는 책이다):\n{shown}\n\n"
    '이번 메시지: "{message}"\n\n'
    "이번 메시지는 새 책을 찾는 말이 아니라 질문이거나 보여 준 책에 대한 말이다. 책을 새로 "
    "고르지 말고 답하라.\n"
    "- 보여 준 책에 대해 물으면 그 책 소개에 있는 내용으로만 답하라. 소개에 없는 것은 "
    "모른다고 말하라.\n"
    "- 보여 준 책을 두고 한 말(좋다, 별로다, 표지가 마음에 든다 등)에는 먼저 그 말에 짧게 반응하고, "
    "필요하면 그 책 소개에서 사용자가 말한 것과 맞는 점이나 다른 점을 한마디 덧붙여라.\n"
    "- 보여 준 책 중에서 골라 달라면, 최근 대화에서 사용자가 말한 것에 가장 맞는 한 권을 "
    "번호로 고르고 이유를 짧게 말하라.\n"
    "- 챗봇이 무엇을 하는지 물으면, 원하는 책을 대화로 함께 좁혀 찾아 준다고 답하라. "
    "추천에 무엇을 쓰는지 물으면 대화에서 말한 내용을 바탕으로 찾는다고만 답하고, 그 밖의 "
    "것은 지어내지 마라.\n"
    "- 인사에는 짧게 인사하라.\n"
    "reply는 1-3문장(최대 200자)이다. followup은 지금 이야기에서 이어지는 구체적인 질문 한 문장"
    '(최대 80자)이다. "더 궁금한 점이 있으신가요?"처럼 어느 대화에나 붙는 질문은 쓰지 마라.\n'
    + voice.RULES
    + '다음 JSON 형식으로만 답하라: {{"reply": "질문에 대한 답", "followup": "이어 갈 질문"}}'
)


# 문지기 판정(#530)을 이만큼만 기다린다. 넘으면 규칙의 기본값(⑦에 보내 쇼핑이 아니면 되돌리기)으로 간다.
GATE_JUDGE_TIMEOUT_SECONDS = 3.0


async def _judge_lane(
    req: ChatRequest,
    cards: list[tuple[int, str]],
    books: list[shown.ShownBook],
) -> gate_judge.Judged | None:
    """규칙이 애매하다고 본 말을 판정 호출로 가른다. 실패·시간 초과·모르는 답이면 None 이다(규칙의 기본값으로 간다).

    직전 챗봇 말은 최근 대화의 마지막 줄이 챗봇 말일 때만 쓴다. 사용자 말만 싣는 호출자도 있다.
    CHAT_JUDGE=jev 면 Jev 에 같은 글로 묻는다(app/gateway/decision.py). 시간 제한과 실패 때 가는 곳은 같다.
    """
    last = req.recent_turns[-1] if req.recent_turns else None
    last_bot = last.text if last is not None and last.role == "assistant" else ""
    inputs = {
        "last_bot": last_bot[-gate_judge.LAST_BOT_CHARS :] or "(없음)",
        "cards": gate_judge.card_lines(books, [rank for rank, _ in cards]),
        "message": req.message or "",
    }
    try:
        if decision.enabled():
            picked = await decision.choose(
                state=gate_judge.STATE.format(**inputs),
                instructions=gate_judge.INSTRUCTIONS,
                criteria=gate_judge.CRITERIA,
                timeout=GATE_JUDGE_TIMEOUT_SECONDS,
            )
            logger.info(
                "문지기 판정(Jev): %s, 확신 %s", picked.choice, picked.confidence
            )
            answer = {"lane": picked.choice}
        else:
            chain = (
                gate_judge.LANE_PROMPT
                | get_chat_model()
                | message_text
                | parse_json_response
            )
            answer = await asyncio.wait_for(
                invoke_chain(chain, inputs), timeout=GATE_JUDGE_TIMEOUT_SECONDS
            )
    except (LLMUnavailableError, TimeoutError):
        logger.warning("문지기 판정 실패 — 규칙의 기본값으로 간다", exc_info=True)
        return None
    return gate_judge.parse(answer)


async def _shown_books(cards: list[tuple[int, str]]) -> list[shown.ShownBook]:
    """보여 준 카드를 책 정보로 바꾼다. 실패해도 추천은 이어 간다 — 이 정보 없이도 고치기 전처럼 동작한다."""
    try:
        return await shown.fetch(cards)
    except Exception:
        logger.exception("보여 준 카드 조회 실패 — 없이 이어 간다")
        return []


async def _answer(
    req: ChatRequest, shown_books: list[shown.ShownBook]
) -> tuple[str, str, bool]:
    """대답 턴의 (reply, followup, degraded). LLM 이 실패하면 고정 안내와 degraded 를 준다."""
    chain = ANSWER_PROMPT | get_chat_model() | message_text | parse_json_response
    try:
        parsed = await invoke_chain(
            chain,
            {
                "recent_turns": _format_recent_turns(req.recent_turns),
                "shown": shown.listing(shown_books, ANSWER_DESCRIPTION_CHARS),
                "message": req.message,
            },
        )
    except LLMUnavailableError:
        logger.exception("대답 턴 실패")
        return _ANSWER_FALLBACK_REPLY, _ANSWER_FALLBACK_FOLLOWUP, True
    reply = parsed.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        logger.error("대답 턴 응답에 reply 가 없음")
        return _ANSWER_FALLBACK_REPLY, _ANSWER_FALLBACK_FOLLOWUP, True
    followup = parsed.get("followup")
    if not isinstance(followup, str) or not followup.strip():
        followup = _ANSWER_DEFAULT_FOLLOWUP
    return _fit_reply(reply.strip()), _fit_followup(followup.strip()), False


def _fit_followup(followup: str) -> str:
    if len(followup) <= _FOLLOWUP_MAX_CHARS:
        return followup
    return followup[: _FOLLOWUP_MAX_CHARS - 1] + "…"


@router.post("/chat")
async def chat(request: Request) -> Response:
    read = await _read_request(request)
    if isinstance(read, JSONResponse):
        return read
    req, payload = read
    if streaming.wants_stream(request):
        # 요청 검사 오류는 위에서 상태 코드로 끝났다. 여기부터의 결과는 이벤트로 보낸다(AI #236)
        return streaming.stream_response(
            lambda on_reply: _respond(req, payload, on_reply)
        )
    return await _respond(req, payload, None)


@router.post("/chat/jobs")
async def submit_chat_job(request: Request) -> JSONResponse:
    """폴링: /chat 과 같은 요청을 작업으로 맡기고 job_id 만 바로 돌려준다(202). 결과는 GET 으로 묻는다.

    요청 검사 오류는 맡기지 않고 /chat 과 같은 상태 코드로 바로 답한다.
    """
    read = await _read_request(request)
    if isinstance(read, JSONResponse):
        return read
    req, payload = read
    job_id = jobs.submit(lambda on_reply: _respond(req, payload, on_reply))
    return responses.success(
        "job_accepted", {"job_id": job_id, "status": "pending"}, status=202
    )


@router.get("/chat/jobs/{job_id}")
async def get_chat_job(job_id: str) -> JSONResponse:
    """폴링: 아직이면 202 와 먼저 보여 줄 말풍선, 끝났으면 /chat 과 같은 응답(상태 코드·envelope)이다.

    결과는 맡긴 뒤 jobs.KEEP_SECONDS 동안 몇 번이든 같은 것을 돌려준다. 모르거나 지난 작업이면 404 다.
    """
    job = jobs.get(job_id)
    if job is None:
        return responses.error(404, "job_not_found")
    if not job.task.done():
        return responses.success(
            "job_pending",
            {"job_id": job_id, "status": "pending", "reply": job.reply},
            status=202,
        )
    if job.task.cancelled() or job.task.exception() is not None:
        # 실패 로그는 작업이 끝날 때 jobs 가 남겼다
        return responses.error(500, "internal_server_error")
    return job.task.result()


async def _read_request(request: Request) -> tuple[ChatRequest, dict] | JSONResponse:
    """본문을 읽고 검사한다. 실패면 그대로 돌려줄 오류 응답이다. 세 전달 방식이 같은 검사를 쓴다."""
    raw = await body.read_limited(request, MAX_BODY_BYTES)
    if raw is None:
        return responses.error(413, "payload_too_large")

    try:
        payload = json.loads(raw)
    except ValueError:
        return responses.error(400, "invalid_request")

    req = parse_request(payload)
    if isinstance(req, tuple):
        status, message = req
        return responses.error(status, message)
    return req, payload


async def _respond(
    req: ChatRequest, payload: dict, on_reply: ReplySink | None
) -> JSONResponse:
    """요청 하나에 답한다. on_reply 가 있으면 카드 생성 LLM 이 쓰는 reply 를 거기로 먼저 흘려보낸다."""
    # 쇼핑 칸(idempotency_key)을 보낸 요청이면 문지기가 ③과 ⑦ 중 어디로 보낼지 정한다(#413).
    # 칸이 없으면 이 변경 전과 똑같이 ③만 하고 응답에 route 도 없다.
    lane = None
    cards = shown.parse(payload)
    books: list[shown.ShownBook] | None = None
    target: gate_judge.Judged = "recommend"
    if shopping.enabled(payload):
        lane = "recommend"
        decision = gate_rules.decide(
            req.message or "",
            shopping.cards_shown(payload),
            asking.asked(req.recent_turns),
            gate_rules.asked_action(req.recent_turns),
            gate_rules.asked_fork(req.recent_turns),
            # "사 줘"를 장바구니 말로 보는 것(v4)은 카드 밖 책을 찾는 ⑦ 이 켜진 서버에서만이다
            buy_requests=get_settings().agent_checkout,
            # 리뷰·평점·쿠폰·남은 수량을 묻는 말(v6)은 책 정보를 답하는 ⑦ 이 켜진 서버에서만 ⑦ 말로 본다(#650)
            info_requests=get_settings().agent_book_info,
        )
        target = decision.lane
        if not decision.sure:
            # 규칙이 낱말 근거 없이 기본값으로 정했다. 한쪽에 쏠리지 않은 판정이 가른다(#530, app/gate/judge.py)
            books = await _shown_books(cards)
            judged = await _judge_lane(req, cards, books)
            if judged is not None:
                target = judged
        logger.info(
            "문지기: %s(%s) → %s",
            "확실" if decision.sure else "애매",
            decision.basis,
            target,
        )
        if target == "shop":
            shop_req = shopping.to_agent_request(payload)
            if shop_req is None:
                return responses.error(400, "invalid_request")
            turn = await agent.shop_turn(shop_req, shopping.agent_payload(payload))
            if turn.error is not None:
                # ⑦ 이 실패한 것을 ③ 의 추천으로 덮지 않는다. 장바구니 요청에 추천 답을 하는 쪽이 더 나쁘다
                return turn.error
            if not turn.not_shopping:
                logger.info("문지기: 쇼핑으로 처리했다")
                return responses.success(turn.message, {**turn.data, "route": "shop"})
            logger.info("문지기: ⑦이 쇼핑이 아니라고 해서 ③으로 되돌린다")
            target = "recommend"
    return await _recommend(
        req,
        lane,
        cards,
        answer_only=target == "card",
        shown_books=books,
        on_reply=on_reply,
    )


async def _answer_response(
    req: ChatRequest, lane: str | None, shown_books: list[shown.ShownBook]
) -> JSONResponse:
    """대답 턴의 응답. 새 책을 찾는 말이 아니라 검색하지 않는다. spec 은 요청에 실린 그대로 돌려준다."""
    reply, followup, degraded = await _answer(req, shown_books)
    return responses.success(
        "recommend_success",
        _with_lane(
            {
                "reply": reply,
                "spec": req.spec.model_dump(),
                "recognition": None,
                "cards": [],
                "followup": followup,
                "buttons": [],
                "degraded": degraded,
            },
            lane,
        ),
    )


def _with_lane(data: dict, lane: str | None) -> dict:
    """문지기를 켠 요청에만 갈래(route)를 담는다. 안 켠 요청의 응답 모양은 그대로다."""
    return data if lane is None else {**data, "route": lane}


async def _recommend(
    req: ChatRequest,
    lane: str | None,
    shown_cards: list[tuple[int, str]] | None = None,
    *,
    answer_only: bool = False,
    shown_books: list[shown.ShownBook] | None = None,
    on_reply: ReplySink | None = None,
) -> JSONResponse:
    """③ 본문: 조건 갱신 → 후보 검색 → 카드 생성. 질문이면 조건 갱신 → 대답, 넓은 첫 요청이면 조건 갱신 → 묻기(#530).

    answer_only 는 문지기 판정이 화면 카드에 대한 말이라고 본 턴이다. 조건을 갱신하지 않고 바로 대답한다.
    shown_books 는 문지기가 이미 읽은 보여 준 카드다. 없으면 여기서 읽는다.
    """
    unanswerable = _unanswerable_request(req.message, req.spec, req.recent_turns)
    if unanswerable:
        # LLM도 검색도 부르지 않는다. spec은 요청에 실린 그대로 돌려준다(6키 계약 유지).
        reply, followup = unanswerable
        return responses.success(
            "recommend_success",
            _with_lane(
                {
                    "reply": reply,
                    "spec": req.spec.model_dump(),
                    "recognition": None,
                    "cards": [],
                    "followup": followup,
                    "buttons": [],
                    "degraded": False,
                },
                lane,
            ),
        )

    if shown_books is None:
        shown_books = await _shown_books(shown_cards or [])
    if answer_only:
        # 새로 찾으면 사용자가 가리킨 책이 화면에서 사라진다. 그 책 소개로 답하고 카드는 그대로 둔다
        logger.info("대답 턴: 문지기가 카드에 대한 말이라고 해서 검색하지 않고 답했다")
        return await _answer_response(req, lane, shown_books)
    update = await update_spec(
        req.message,
        req.spec,
        req.recent_turns,
        shown.listing(shown_books, SPEC_SHOWN_DESCRIPTION_CHARS),
        ask_stage=_ask_stage(req),
    )
    if update.ask is not None:
        # 넓은 첫 요청이다. 찾기 전에 한 번 묻는다. spec 은 메시지에서 알게 된 조건까지 갱신해 다음 턴에 넘긴다.
        # 답 보기는 질문 뒤에 문장으로 붙는다(#505). 사용자가 그중 하나를 따라 말하면 다음 턴이 답으로 받는다.
        ask = update.ask
        logger.info("넓은 첫 요청이라 찾기 전에 물었다(보기 %d개)", len(ask.options))
        return responses.success(
            "recommend_success",
            _with_lane(
                {
                    "reply": ask.reply,
                    "spec": update.spec.model_dump(),
                    "recognition": None,
                    "cards": [],
                    "followup": asking.followup(ask.question, ask.options),
                    "buttons": [],
                    "degraded": False,
                },
                lane,
            ),
        )
    if update.answer:
        logger.info("대답 턴: 검색하지 않고 답했다")
        return await _answer_response(req, lane, shown_books)
    spec, spec_degraded = update.spec, update.degraded
    turn: _LaneTurn | None = None
    try:
        # 분야 값 검사가 책 표를 읽다 실패해도, ISBN을 book_id로 바꾸다 실패해도
        # 후보 검색 실패와 같은 500이다.
        spec = _without_invented_mood(spec, req, shown_books)
        spec = await _settle_category(spec)
        spec = _recover_topic(spec, req.message)
        shown_ids = await _shown_book_ids(req)
        owned = await _owned_book_ids(req)
        plan = lanes.fill_plan(
            update.plan,
            req.message or "",
            [book.rank for book in shown_books],
            {book.rank: book.title for book in shown_books},
        )
        if plan.ask_card and _agent_on():
            # 카드가 여럿인데 어느 책을 읽은 사람들인지 말하지 않았다. 아무 카드나 찍지 않고 번호를 묻는다(LLM·검색 없이)
            _drop(update.recall)
            logger.info("함께 읽은 책을 물었는데 어느 카드인지 몰라 번호를 묻는다")
            return responses.success(
                "recommend_success",
                _with_lane(
                    {
                        "reply": _WHICH_CARD_REPLY,
                        "spec": spec.model_dump(),
                        "recognition": None,
                        "cards": [],
                        "followup": _which_card_followup(shown_books),
                        "buttons": [],
                        "degraded": False,
                    },
                    lane,
                ),
            )
        if not spec_degraded and _agent_on() and _uses_lanes(spec, plan):
            turn = await _gather_lanes(
                req,
                spec,
                plan,
                shown_cards or [],
                shown_books,
                shown_ids,
                owned,
                recalling=update.recall,
            )
            candidates = turn.pool
        else:
            _drop(update.recall)
            candidates = await get_candidates(
                spec, shown_ids, req.user_id, owned_book_ids=owned
            )
    except Exception:
        _drop(update.recall)
        # 그대로 두면 FastAPI 기본 500(text/plain)이 나가 공통 응답 형식이 깨진다.
        logger.exception("후보 검색 실패")
        return responses.error(500, "internal_server_error")

    # 첫 턴에만 묻는다. 이어지는 턴은 사용자가 이미 답하는 중이라 같은 걸 또 묻지 않는다.
    slot_question = None if req.recent_turns else _slot_question(req.message)
    # 첫 턴에 코드가 질문을 붙이거나 직전에 물었으면 말풍선은 묻지 않는다. 한 번에 두 개를 묻거나 연달아 묻지 않게
    may_ask = slot_question is None and not _bot_asked_last(req.recent_turns)
    lane_ask: tuple[str, str] | None = None
    if spec_degraded:
        # 명세: 1단계가 실패하면 3단계(카드 생성)도 건너뛴다. 점수 상위
        # CARD_LIMIT권을 규칙으로 낸다(#129).
        result = CardResult(_rule_based_cards(candidates), None, True)
    elif turn is not None:
        result, lane_ask = await _pick_from_lanes(
            turn, may_ask=may_ask, on_reply=on_reply
        )
    else:
        result = await generate_cards(
            candidates,
            spec,
            message=req.message,
            recent_turns=req.recent_turns,
            may_ask=may_ask,
            on_reply=on_reply,
        )
    cards, llm_reply, degraded = result.cards, result.reply, result.degraded

    # reply 우선순위(#158): LLM 장애 시엔 고정 안내, 정상 호출이면 그 턴의
    # 추천을 반영한 LLM reply, 카드가 있는데 reply만 비면 최후 폴백, 카드
    # 자체가 없으면(검색 결과 없음 등 — 이때는 generate_cards가 LLM을 아예
    # 안 부르므로 llm_reply도 없다) "골라봤어요"가 어색해 못 찾음 안내로 바꾼다.
    no_card_reply, followup = None, None
    if lane_ask is not None:
        # 판단 호출이 후보가 두 갈래로 갈린다며 물었다. 카드 없이 그 질문을 followup 으로 준다(명세: 카드가 없으면 followup)
        no_card_reply, followup = lane_ask
    elif not cards:
        no_card_reply, followup = _no_card_response(
            spec,
            req.message,
            # 판단 호출이 맞는 책이 없다고 본 것은 카드 생성 실패가 아니다. 조건이나 주제를 묻는 쪽으로 간다
            had_candidates=bool(candidates) and turn is None,
            slot_question=slot_question,
        )

    if degraded:
        reply = "지금은 추천이 어려워요. 조건에 맞는 책을 찾아볼게요."
    elif llm_reply:
        reply = llm_reply
    elif cards:
        reply = "골라봤어요."
    else:
        reply = no_card_reply

    reply = _fit_reply(reply)
    if cards and not degraded:
        # 카드 턴의 질문과 답 보기는 카드 아래(followup)에 둔다(#652). 빠진 정보를 묻는 질문(slot_question)이 있으면
        # 그것을 묻는다 — 카드 프롬프트는 이때 묻지 말라고 한다
        reply, turn_question = _split_turn_question(reply)
        followup = option_lines.with_options(
            slot_question or turn_question, result.options, asking.FOLLOWUP_MAX_CHARS
        )

    return responses.success(
        "recommend_success",
        _with_lane(
            {
                "reply": reply,
                "spec": spec.model_dump(),
                "recognition": None,
                "cards": cards,
                "followup": followup,
                "buttons": [],
                "degraded": degraded,
            },
            lane,
        ),
    )
