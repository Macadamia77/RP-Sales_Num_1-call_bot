"""무료·오프라인 기본 해석기. 키워드 규칙으로 의도를 분류한다.

외부 LLM이 없거나 실패·지연될 때의 안전망으로도 쓰인다.
"""

from __future__ import annotations

import re

from ..phone import _CORRECTION_RE, _FIRST_HINT, _LAST_HINT
from .base import DialogContext, Interpretation, Interpreter

_RULES: list[tuple[str, list[str]]] = [
    ("refuse", [
        "전화하지 마", "연락하지 마", "전화 하지 마", "알려드릴 수 없", "알려 드릴 수 없", "못 알려",
        "안 알려", "곤란", "필요 없", "관심 없", "끊을게", "끊겠습니다", "끊어요", "그만",
    ]),
    ("callback_later", [
        "나중에", "이따", "바빠", "바쁘", "지금은 좀", "다시 전화", "다시 연락", "좀 있다", "운전 중", "회의 중",
    ]),
    ("wrong_building", [
        "그런 건물", "잘못 거", "잘못 걸", "여기 아니", "여긴 아니", "다른 데", "모르는 건물",
    ]),
    ("dont_know", ["모르", "몰라", "잘 몰", "글쎄", "없어요", "없는데"]),
    ("refer_other", [
        "물어보세요", "물어 보세요", "여쭤보세요", "알아보세요", "가 보세요", "찾아가", "쪽에 물",
    ]),
    ("ask_which_building", ["어느 ", "어디 있는", "어느 건물", "어디 건물", "무슨 건물", "어느 빌딩", "어디요", "어디 말씀", "주소가"]),
    ("ask_purpose", ["왜요", "왜 그러", "무슨 일", "무슨 용", "용건", "무슨 문제", "왜 필요", "뭐 때문"]),
    ("ask_identity", ["누구세요", "누구시", "어디세요", "어디신", "어디에서", "누가", "사람이에요", "AI"]),
    ("confirm_no", ["아니요", "아뇨", "아니에요", "틀려", "틀렸", "다른데", "아닌데"]),
    ("confirm_yes", ["맞아", "맞습니다", "맞죠", "그래요", "그렇", "말씀하세요", "좋아요", "그럼요"]),
]

# "네", "예"는 "예시빌딩", "네 자리"처럼 다른 말의 일부일 수 있어 문장 첫 단어로만 인정한다
_YES_HEAD = re.compile(r"^\s*(네|예|응|어|넵|넹|yes)(?=$|[\s,.!~요])")

_QUESTION_TAIL = re.compile(r"(\?|요\?|나요|까요|세요\?)$")


class RuleBasedInterpreter(Interpreter):
    name = "rule"

    def interpret(self, ctx: DialogContext) -> Interpretation:
        text = ctx.utterance.strip()
        if not text:
            return Interpretation("unclear")

        # 번호 관련 신호는 코드가 찾은 번호를 우선 사용한다
        if ctx.pending_number and (_CORRECTION_RE.search(text) or _LAST_HINT.search(text) or _FIRST_HINT.search(text)):
            if ctx.numbers_found or re.search(r"[공영일이삼사오육칠팔구\d]", text):
                return Interpretation("correction")
        if ctx.numbers_found and not _matches(text, "refuse"):
            return Interpretation("provided_contact")

        for intent, words in _RULES:
            if _matches(text, intent, words):
                note = text if intent in ("callback_later", "refer_other") else None
                return Interpretation(intent, note=note)
        if _QUESTION_TAIL.search(text):
            return Interpretation("other_question")
        return Interpretation("unclear")


def _matches(text: str, intent: str, words: list[str] | None = None) -> bool:
    if words is None:
        words = dict(_RULES)[intent]
    if intent == "confirm_yes":
        return bool(_YES_HEAD.search(text)) or any(w in text for w in words)
    return any(w in text for w in words)
