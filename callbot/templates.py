"""대화 템플릿 (가이드 05장).

- persona_prompt: LLM에게 주는 역할·말투·규칙. 사용자가 편집할 수 있다.
- phrases: 코드가 그대로 말하는 고정 문구. 번호 낭독 문구는 반드시 {phone_spoken}을 사용한다.
- limits: 통화 시간·반복 제한.

템플릿을 저장할 때마다 버전이 새로 생기고, 통화마다 사용한 버전을 기록한다.
"""

from __future__ import annotations

import copy
import string

DEFAULT_TEMPLATE_NAME = "management-contact"

DEFAULT_TEMPLATE: dict = {
    "persona_prompt": (
        "[역할]\n"
        "당신은 {requester_name}의 요청으로 전화하는 AI 전화 도우미다.\n"
        "목표는 {building_address}에 있는 {building_name}의 관리사무소 업무용 전화번호를 확인하는 것이다.\n"
        "[말투]\n"
        "한국어 존댓말로 말한다. 한 번에 한 가지 질문만 한다. 답변은 한두 문장으로 짧게 한다.\n"
        "[문의를 받은 경우]\n"
        "어느 건물인지 물으면 입력된 주소로 설명한다.\n"
        "문의 이유를 물으면 '{inquiry_purpose}'를 사실대로 짧게 설명한다.\n"
        "알 수 없는 정보는 모른다고 답한다. 허위 신분이나 관계를 만들지 않는다.\n"
        "[금지]\n"
        "전화번호 숫자를 직접 말하거나 추측하지 않는다. 번호 낭독은 서버가 한다.\n"
    ),
    "phrases": {
        "greeting": (
            "안녕하세요. {requester_name}님의 요청으로 연락드린 AI 전화 도우미입니다. "
            "{building_name} 관리사무소 전화번호를 여쭤봐도 될까요?"
        ),
        "ask_number": "{building_name} 관리사무소 전화번호를 알려주실 수 있을까요?",
        "explain_building": "{building_address}에 있는 {building_name} 말씀드립니다.",
        "explain_purpose": "{inquiry_purpose} 관련해서 관리사무소에 문의드리려고 합니다.",
        "explain_identity": "{requester_name}님의 요청으로 전화드린 AI 전화 도우미입니다.",
        "readback": "{phone_spoken}, {building_name} 관리사무소 번호가 맞을까요?",
        "ask_area_code": "지역번호도 함께 알려주시겠어요?",
        "ask_repeat_number": "죄송하지만 번호를 한 번만 더 천천히 불러주시겠어요?",
        "ask_confirm_again": "방금 읽어드린 번호가 맞는지 한 번만 확인 부탁드려요.",
        "ask_other_contact": "혹시 확인해 볼 수 있는 다른 곳을 알고 계실까요?",
        "ask_referral_number": "혹시 그쪽 연락처를 알 수 있을까요?",
        "ask_management_after_other": "감사합니다. 혹시 {building_name} 관리사무소 번호도 알고 계실까요?",
        "reask_unclear": "죄송합니다, 잘 못 들었어요. 한 번만 다시 말씀해 주시겠어요?",
        "silence_check": "여보세요, 제 목소리 들리시나요?",
        "closing_success": "안내해 주셔서 감사합니다. 좋은 하루 보내세요.",
        "closing_generic": "시간 내주셔서 감사합니다. 좋은 하루 보내세요.",
        "closing_refused": "네, 알겠습니다. 시간 내주셔서 감사합니다.",
        "closing_callback": "네, 알겠습니다. 나중에 다시 연락드리겠습니다. 감사합니다.",
        "closing_wrong": "제가 잘못 연락드린 것 같습니다. 죄송하고 감사합니다.",
    },
    "limits": {
        "max_bot_turns": 14,
        "max_silence": 2,
        "max_unclear": 2,
    },
}

# 템플릿에서 사용할 수 있는 변수 (가이드 05장 표)
TEMPLATE_VARIABLES = [
    "requester_name", "building_name", "building_address", "recipient_number",
    "inquiry_purpose", "recipient_name", "phone_spoken",
]
REQUIRED_PHRASES = list(DEFAULT_TEMPLATE["phrases"].keys())


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(text: str, variables: dict) -> str:
    return string.Formatter().vformat(text, (), _SafeDict(variables))


def merged_with_default(content: dict) -> dict:
    """사용자 템플릿에 빠진 항목은 기본값으로 채운다."""
    out = copy.deepcopy(DEFAULT_TEMPLATE)
    if content.get("persona_prompt"):
        out["persona_prompt"] = content["persona_prompt"]
    out["phrases"].update({k: v for k, v in (content.get("phrases") or {}).items() if v})
    out["limits"].update(content.get("limits") or {})
    return out


def validate_template(content: dict) -> list[str]:
    """시스템이 고정하는 영역을 사용자가 깨뜨리지 않았는지 검사한다."""
    errors = []
    phrases = content.get("phrases") or {}
    rb = phrases.get("readback")
    if rb is not None and "{phone_spoken}" not in rb:
        errors.append("readback 문구에는 {phone_spoken} 이 있어야 합니다 (번호 낭독은 서버가 생성).")
    greeting = phrases.get("greeting")
    if greeting is not None and "AI" not in greeting:
        errors.append("greeting 문구에 AI 대리 통화라는 안내가 있어야 합니다.")
    return errors
