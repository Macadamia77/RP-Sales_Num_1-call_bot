"""Twilio 전화 연동 (선택).

프로토타입은 Media Streams(WebSocket 오디오) 대신 <Gather input="speech"> 방식을 쓴다.
- 상대 음성 인식(STT)은 Twilio가 해서 SpeechResult로 보내 준다 (ko-KR).
- 봇 음성은 TTS 파일이 있으면 <Play>, 없으면 Twilio <Say>(한국어 기본 음성)로 재생한다.
- <Gather> 안의 재생은 상대가 말을 시작하면 멈춘다 (말 끊기 기본 지원).

지연이 더 짧은 실시간 양방향 스트림(<Connect><Stream>, 8kHz μ-law)은 후속 단계다.
SDK 없이 REST API를 직접 호출한다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from xml.sax.saxutils import escape, quoteattr

import httpx

from ..config import settings

API = "https://api.twilio.com/2010-04-01"
SAY_VOICE = os.environ.get("TWILIO_SAY_VOICE", "Polly.Seoyeon")


def _auth() -> tuple[str, str]:
    return settings.twilio_account_sid, settings.twilio_auth_token


def start_call(to_number: str, call_id: str) -> str:
    """발신을 요청하고 Twilio CallSid를 돌려준다."""
    base = settings.public_base_url.rstrip("/")
    r = httpx.post(
        f"{API}/Accounts/{settings.twilio_account_sid}/Calls.json",
        auth=_auth(),
        data={
            "To": to_e164(to_number),
            "From": settings.twilio_from_number,
            "Url": f"{base}/webhooks/twilio/voice?call_id={call_id}",
            "StatusCallback": f"{base}/webhooks/twilio/status?call_id={call_id}",
            "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
            "Timeout": "30",
            "TimeLimit": "300",  # 통화 시간 상한 (초)
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["sid"]


def hangup(call_sid: str) -> None:
    httpx.post(
        f"{API}/Accounts/{settings.twilio_account_sid}/Calls/{call_sid}.json",
        auth=_auth(),
        data={"Status": "completed"},
        timeout=15,
    ).raise_for_status()


def to_e164(number: str) -> str:
    """'010-1234-5678' -> '+821012345678'"""
    digits = "".join(ch for ch in number if ch.isdigit() or ch == "+")
    if digits.startswith("+"):
        return digits
    if digits.startswith("0"):
        return "+82" + digits[1:]
    return "+" + digits


def validate_signature(url: str, params: dict, signature: str) -> bool:
    """X-Twilio-Signature 검증: HMAC-SHA1(auth_token, URL + 정렬된 key+value)."""
    payload = url + "".join(k + params[k] for k in sorted(params))
    mac = hmac.new(settings.twilio_auth_token.encode(), payload.encode(), hashlib.sha1)
    expected = base64.b64encode(mac.digest()).decode()
    return hmac.compare_digest(expected, signature or "")


def _speak(text: str, audio_url: str | None) -> str:
    if audio_url:
        return f"<Play>{escape(audio_url)}</Play>"
    return f'<Say language="ko-KR" voice={quoteattr(SAY_VOICE)}>{escape(text)}</Say>'


def twiml_turn(text: str, audio_url: str | None, action_url: str, end: bool) -> str:
    if end:
        body = _speak(text, audio_url) + "<Hangup/>"
    else:
        body = (
            f'<Gather input="speech" language="ko-KR" speechTimeout="auto" timeout="7" '
            f'actionOnEmptyResult="true" method="POST" action={quoteattr(action_url)}>'
            f"{_speak(text, audio_url)}</Gather>"
        )
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


def twiml_hangup() -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
