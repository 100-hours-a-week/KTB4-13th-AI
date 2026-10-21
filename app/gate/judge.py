"""문지기의 애매한 말 판정(#530) — 규칙이 낱말 근거 없이 기본값으로 정한 말만 작은 LLM 호출로 가른다.

규칙(app/gate/rules.py)은 장바구니·추천 낱말이 있으면 바로 가르고, 그 밖은 ⑦에 보내 ⑦이 "쇼핑 아님"이라고 하면
③으로 되돌렸다. 그런데 ⑦은 쇼핑하라고 만든 에이전트라 애매한 말을 쇼핑으로 읽었다. 머지 전 점검에서 "1만5천원
이하로"를 예산 골라 담기로, "다른 거 보여줘"를 지금 카드 되읊기로, "2번은 어떤 내용이야?"를 제목·가격 안내로
받았다. 그래서 애매한 말은 한쪽에 쏠리지 않은 판정이 가른다.

판정은 어느 쪽으로 보낼지만 정한다. 보는 것은 직전 챗봇 말, 화면의 카드, 이번 말뿐이다. "아까 그 책"이 무엇인지는
보내진 쪽이 최근 대화 전체를 보고 푼다. 앞 대화를 길게 주면 판단이 흔들렸다(#513: 앞 조건이 프롬프트에 있으면
같은 말을 제목 찾기로 봄).

③ 말풍선이 끝에서 묻거나 한 권을 권하게 된 뒤(#545) "1만5천원 이하로", "2만원 이하로"가 둘 다 shop 으로 갔다(점검 대화,
main 은 recommend). 직전 챗봇 말의 "어느 책이 끌리세요?"를 보고 가격으로 책을 고르는 말로 읽은 것이다. 그래서 직전 말이
책을 고르라고 했어도 조건만 말하면 recommend 라고 적는다.

갈래는 셋이다.
- recommend: ③ 추천. 새 책 찾기, 지금 추천의 조건·분위기 바꾸기, 다른 책 더 보기, 인사, 챗봇에 대한 질문
- shop: ⑦ 쇼핑. 담기·빼기·수량, 장바구니, 가격·재고·배송·주문
- card: ③ 대답 턴. 화면의 카드 한 권을 가리켜 묻거나 평가하는 말. 새로 찾지 않고 그 책 소개로 답해 카드를 지킨다
"""

from typing import Literal, get_args

from langchain_core.prompts import ChatPromptTemplate

from app.chat.shown import ShownBook

Judged = Literal["recommend", "shop", "card"]
LANES: tuple[str, ...] = get_args(Judged)
# 프롬프트에 넣을 직전 챗봇 말의 길이. 최근 대화 한 줄과 같다(명세 ③ 입력 ³)
LAST_BOT_CHARS = 200

# {last_bot}·{cards}·{message}가 채워지는 자리다. JSON 예시의 중괄호는 {{ }}로 겹쳐 쓴다.
# 예시는 머지 전 점검과 대화 평가(evals/chat_dialog)에 쓴 문장과 겹치지 않게 고른다. 점검 문장을 예로 쓰면 점검이 낙관적이 된다.
LANE_PROMPT = ChatPromptTemplate.from_template(
    "너는 온라인 서점 챗봇의 안내원이다. 사용자의 이번 말을 어느 담당에게 보낼지 정하라.\n\n"
    '직전 챗봇 말: "{last_bot}"\n'
    "화면에 보이는 책 카드: {cards}\n"
    '이번 말: "{message}"\n\n'
    "- recommend: 새 책을 찾거나, 지금 추천의 조건이나 분위기를 바꾸거나(가격대, 출간 연도, 쉬운 정도, "
    '분위기), 다른 책을 더 보여 달라는 말. 인사나 챗봇에 대해 묻는 말도 여기다. 예: "3만원 넘지 않는 걸로", '
    '"좀 더 차분한 걸로", "또 없어?", "작가 무라카미 하루키로". 직전 챗봇 말이 어느 책이 끌리는지 묻거나 한 권을 '
    "권했어도, 이번 말이 담기·사기 없이 가격·출간 연도·분위기 같은 조건만 말하면 recommend 다.\n"
    "- shop: 장바구니에 담기·빼기·수량 바꾸기, 장바구니 보기, 가격·재고·배송·주문·결제를 묻거나, 고른 책을 "
    '사겠다는 말. 예: "이걸로 할게요", "그거 하나 주세요", "몇 원이야?"\n'
    "- card: 화면의 카드 한 권을 가리켜 그 책의 내용·난이도·대상·작가를 묻거나, 그 책이 좋다·별로다고 "
    '평가하는 말. 새 책을 찾지 않는다. 예: "3번은 초등학생도 읽어?", "첫 번째 책 작가가 누구야?", '
    '"마지막 책 괜찮네"\n\n'
    '답은 JSON 한 줄로만 하라: {{"lane": "recommend"}} 또는 {{"lane": "shop"}} 또는 {{"lane": "card"}}'
)


def card_lines(books: list[ShownBook], ranks: list[int]) -> str:
    """화면 카드를 "1번 『제목』, 2번 『제목』"으로. 책 표에서 못 찾은 카드는 번호만 남긴다. 없으면 "(없음)"."""
    if not ranks:
        return "(없음)"
    titles = {book.rank: book.title for book in books}
    return ", ".join(
        f"{rank}번 『{titles[rank]}』" if rank in titles else f"{rank}번"
        for rank in ranks
    )


def parse(answer: dict) -> Judged | None:
    """판정 답의 갈래. 모르는 값이면 None — 호출부는 규칙의 기본값으로 간다."""
    lane = answer.get("lane")
    if lane == "recommend" or lane == "shop" or lane == "card":
        return lane
    return None
