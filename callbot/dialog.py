"""통화 한 건의 대화 상태 관리 (가이드 04·05·06장).

흐름: 인사 → (건물 확인) → 번호 요청 → 후보 수집 → 번호 재확인 → 저장·종료

코드가 결정하는 것: 단계 전이, 고정 문구, 번호 추출·낭독, 성공 판정, 종료.
LLM(Interpreter)이 하는 것: 상대 발화 의도 분류, 자유 질문에 대한 짧은 답변.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import phone
from .llm.base import FREEFORM_INTENTS, DialogContext, Interpretation, Interpreter, safe_say_text
from .templates import merged_with_default, render

GREETING = "greeting"
ASK_NUMBER = "ask_number"
NEED_AREA_CODE = "need_area_code"
READBACK = "readback"
ASK_OTHER = "ask_other"
ENDED = "ended"

# 통화 결과 (outcome)
SUCCESS = "number_confirmed_by_source"
CANDIDATE_ONLY = "candidate_unconfirmed"
REFERRED = "referred_other_contact"
NOT_FOUND = "not_found"
REFUSED = "refused"
CALLBACK = "callback_requested"
WRONG = "wrong_contact"
NO_RESPONSE = "no_response"
INCOMPLETE = "incomplete"
HANGUP = "hangup_before_result"
NOT_CONNECTED = "not_connected"

_YES_WORDS = re.compile(r"(네|예|맞아|맞습니다|맞죠|그래요|그렇)")


@dataclass
class Utterance:
    id: str
    seq: int
    speaker: str  # bot | recipient
    text: str
    ts: float  # 통화 시작 기준 초
    response_id: str | None = None
    status: str = "final"  # 봇: generated | played | interrupted, 상대: final
    kind: str | None = None  # 봇 문구 종류 (greeting, readback ...)
    intent: str | None = None
    source: str | None = None  # 해석기 (rule, ollama ...)


@dataclass
class Contact:
    id: str
    raw_text: str
    digits: str
    formatted: str | None
    role: str
    complete: bool
    confirmation_status: str = "candidate"  # candidate | confirmed_by_source | rejected | superseded
    reachability_status: str = "not_tested"
    provided_in: list[str] = field(default_factory=list)
    readback_in: str | None = None
    confirmed_in: str | None = None
    note: str | None = None


@dataclass
class BotTurn:
    response_id: str
    utterance_id: str
    text: str
    end_call: bool
    stage: str

    def to_dict(self) -> dict:
        return asdict(self)


class CallSession:
    def __init__(
        self,
        call_id: str,
        variables: dict,
        template: dict,
        interpreter: Interpreter,
        on_change: Callable[["CallSession"], None] | None = None,
    ):
        self.call_id = call_id
        self.variables = dict(variables)
        self.template = merged_with_default(template)
        self.interpreter = interpreter
        self.on_change = on_change
        self.voice_id: str | None = None  # TTS 공급사의 목소리 ID

        self.t0 = time.time()
        self.stage = GREETING
        self.utterances: list[Utterance] = []
        self.contacts: list[Contact] = []
        self.pending_id: str | None = None

        self.outcome: str | None = None
        self.end_reason: str | None = None
        self.ended_at: float | None = None
        self.finalized = False
        self.do_not_retry = False
        self.callback_note: str | None = None
        self.referrals: list[str] = []

        self._resp_seq = 0
        self.readback_interrupted = False
        self.counters = {
            "bot_turns": 0, "silence": 0, "unclear": 0, "wrong_building": 0,
            "repeat_number": 0, "readback_unclear": 0,
        }
        self.flags = {"asked_other": False, "asked_referral": False, "asked_mgmt_after_other": False}

    # ------------------------------------------------------------------ utils
    @property
    def ended(self) -> bool:
        return self.stage == ENDED

    @property
    def pending(self) -> Contact | None:
        return next((c for c in self.contacts if c.id == self.pending_id), None)

    @property
    def limits(self) -> dict:
        return self.template["limits"]

    def phrase(self, key: str, **extra) -> str:
        return render(self.template["phrases"][key], {**self.variables, **extra})

    def _add_utt(self, speaker: str, text: str, **kw) -> Utterance:
        seq = len(self.utterances) + 1
        u = Utterance(id=f"u_{seq:02d}", seq=seq, speaker=speaker, text=text,
                      ts=round(time.time() - self.t0, 2), **kw)
        self.utterances.append(u)
        return u

    def _say(self, text: str, kind: str, end: bool = False) -> BotTurn:
        self._resp_seq += 1
        response_id = f"{self.call_id}-r{self._resp_seq}"
        u = self._add_utt("bot", text, response_id=response_id, status="generated", kind=kind)
        self.counters["bot_turns"] += 1
        if end:
            self.stage = ENDED
            self.end_reason = self.end_reason or "bot_closing"
            self._finalize()
        return BotTurn(response_id, u.id, text, end, self.stage)

    def _end(self, outcome: str, phrase_key: str) -> BotTurn:
        self.outcome = outcome
        return self._say(self.phrase(phrase_key), kind=phrase_key, end=True)

    def _notify(self) -> None:
        if self.on_change:
            self.on_change(self)

    def _fallback_outcome(self) -> str:
        """확인 완료 없이 끝날 때의 결과."""
        if self.pending and self.pending.confirmation_status == "candidate":
            return CANDIDATE_ONLY
        if any(c.confirmation_status == "candidate" for c in self.contacts) or self.referrals:
            return REFERRED
        return INCOMPLETE

    # ------------------------------------------------------------- public API
    def start(self) -> BotTurn:
        turn = self._say(self.phrase("greeting"), kind="greeting")
        self._notify()
        return turn

    def on_recipient(self, text: str) -> BotTurn | None:
        if self.ended:
            return None
        text = (text or "").strip()
        if not text:
            return self.on_silence()
        u = self._add_utt("recipient", text)
        self.counters["silence"] = 0

        nums = phone.extract_numbers(text)
        interp = self.interpreter.interpret(self._context(text, nums))
        u.intent, u.source = interp.intent, interp.source

        turn = self._decide(u, nums, interp)
        if not self.ended and self.counters["bot_turns"] >= self.limits["max_bot_turns"]:
            turn = self._end(self._fallback_outcome(), "closing_generic")
        self._notify()
        return turn

    def on_silence(self) -> BotTurn | None:
        if self.ended:
            return None
        self.counters["silence"] += 1
        if self.counters["silence"] > self.limits["max_silence"]:
            outcome = self._fallback_outcome()
            turn = self._end(NO_RESPONSE if outcome == INCOMPLETE else outcome, "closing_generic")
        elif self.stage == READBACK and self.pending:
            turn = self._readback(self.pending)
        else:
            turn = self._say(self.phrase("silence_check"), kind="silence_check")
        self._notify()
        return turn

    def on_interrupt(self, response_id: str) -> None:
        """말 끊기(barge-in): 해당 응답을 '중단됨'으로 기록한다. 끝까지 들었다고 가정하지 않는다."""
        for u in self.utterances:
            if u.response_id == response_id and u.status in ("generated", "played"):
                u.status = "interrupted"
                if u.kind == "readback":
                    self.readback_interrupted = True
        self._notify()

    def on_played(self, response_id: str) -> None:
        for u in self.utterances:
            if u.response_id == response_id and u.status == "generated":
                u.status = "played"
        self._notify()

    def on_hangup(self, reason: str = "remote_hangup") -> None:
        if not self.ended:
            self.outcome = self._fallback_outcome()
            if not self.utterances:
                self.outcome = NOT_CONNECTED  # 부재·통화 중·연결 실패
            elif self.outcome == INCOMPLETE:
                self.outcome = HANGUP
            self.stage = ENDED
            self.end_reason = reason
        self._finalize()
        self._notify()

    # --------------------------------------------------------------- decision
    def _context(self, text: str, nums: list[phone.PhoneCandidate]) -> DialogContext:
        return DialogContext(
            stage=self.stage,
            utterance=text,
            variables=self.variables,
            persona_prompt=render(self.template["persona_prompt"], self.variables),
            history=[(u.speaker, u.text) for u in self.utterances[:-1]],
            numbers_found=[{"formatted": n.formatted or n.digits, "role": n.role, "valid": n.valid} for n in nums],
            pending_number=(self.pending.formatted or self.pending.digits) if self.pending else None,
        )

    def _decide(self, u: Utterance, nums: list[phone.PhoneCandidate], interp: Interpretation) -> BotTurn:
        intent = interp.intent
        if intent == "refuse":
            self.do_not_retry = True
            return self._end(REFUSED, "closing_refused")

        if self.stage == READBACK and self.pending:
            turn = self._handle_readback(u, nums, interp)
            if turn:
                return turn
        if self.stage == NEED_AREA_CODE and self.pending:
            turn = self._handle_area_code(u, nums)
            if turn:
                return turn
        if nums:
            return self._handle_numbers(u, nums)

        if intent == "callback_later":
            self.callback_note = interp.note or u.text
            return self._end(CALLBACK, "closing_callback")

        if intent == "ask_which_building":
            self.stage = ASK_NUMBER
            return self._freeform(interp, "explain_building")
        if intent == "ask_purpose":
            self.stage = ASK_NUMBER
            return self._freeform(interp, "explain_purpose")
        if intent == "ask_identity":
            return self._freeform(interp, "explain_identity")

        if intent == "wrong_building":
            self.counters["wrong_building"] += 1
            if self.counters["wrong_building"] >= 2:
                self.do_not_retry = True
                return self._end(WRONG, "closing_wrong")
            return self._say(self.phrase("explain_building"), kind="explain_building")

        if intent in ("dont_know", "confirm_no"):
            if self.stage == ASK_OTHER or self.flags["asked_other"]:
                outcome = self._fallback_outcome()
                return self._end(NOT_FOUND if outcome == INCOMPLETE else outcome, "closing_generic")
            self.flags["asked_other"] = True
            self.stage = ASK_OTHER
            return self._say(self.phrase("ask_other_contact"), kind="ask_other_contact")

        if intent == "refer_other":
            self.referrals.append(interp.note or u.text)
            if self.flags["asked_referral"]:
                return self._end(REFERRED, "closing_generic")
            self.flags["asked_referral"] = True
            self.stage = ASK_OTHER
            return self._say(self.phrase("ask_referral_number"), kind="ask_referral_number")

        if intent == "confirm_yes":
            if self.stage == GREETING:
                self.stage = ASK_NUMBER
                return self._say(self.phrase("ask_number"), kind="ask_number")
            return self._say("네, 번호를 불러주시면 받아 적겠습니다.", kind="ask_number_go")

        # other_question / unclear / 해석 실패
        self.counters["unclear"] += 1
        if self.counters["unclear"] > self.limits["max_unclear"]:
            return self._end(self._fallback_outcome(), "closing_generic")
        return self._freeform(interp, "reask_unclear")

    def _freeform(self, interp: Interpretation, fallback_key: str) -> BotTurn:
        text = safe_say_text(interp.say_text) if interp.intent in FREEFORM_INTENTS else None
        if text:
            return self._say(text, kind=f"llm:{interp.intent}")
        return self._say(self.phrase(fallback_key), kind=fallback_key)

    # --------------------------------------------------------------- numbers
    def _new_contact(self, cand: phone.PhoneCandidate | None, u: Utterance, *, digits: str | None = None,
                     role: str | None = None, prev: Contact | None = None) -> Contact:
        digits = digits if digits is not None else cand.digits
        info = phone.classify(digits)
        c = Contact(
            id=f"c_{len(self.contacts) + 1}",
            raw_text=((prev.raw_text + " / ") if prev else "") + (cand.raw_text if cand else u.text),
            digits=digits,
            formatted=info.formatted,
            role=role or (cand.role if cand else "unknown"),
            complete=info.valid,
            provided_in=(list(prev.provided_in) if prev else []) + [u.id],
        )
        if prev:
            prev.confirmation_status = "superseded"
            if c.role == "unknown":
                c.role = prev.role
        self.contacts.append(c)
        return c

    def _handle_numbers(self, u: Utterance, nums: list[phone.PhoneCandidate]) -> BotTurn:
        wanted = [n for n in nums if n.role in ("management_office", "unknown")]
        wanted.sort(key=lambda n: n.role != "management_office")
        for n in nums:
            if n not in wanted[:1]:
                self._new_contact(n, u)  # 다른 역할의 번호는 별도 후보로 저장

        if not wanted:
            self.pending_id = None
            if self.flags["asked_mgmt_after_other"]:
                return self._end(REFERRED, "closing_generic")
            self.flags["asked_mgmt_after_other"] = True
            self.stage = ASK_OTHER
            return self._say(self.phrase("ask_management_after_other"), kind="ask_management_after_other")

        prev = self.pending if self.pending and self.pending.confirmation_status == "candidate" else None
        # 이전 후보가 지역번호 없는 부분 번호였고 이번에 다른 번호가 오면 이전 것은 대체된 것으로 본다
        c = self._new_contact(wanted[0], u, prev=prev)
        self.pending_id = c.id
        return self._next_for_contact(c)

    def _next_for_contact(self, c: Contact) -> BotTurn:
        info = phone.classify(c.digits)
        if info.needs_area_code:
            self.stage = NEED_AREA_CODE
            return self._say(self.phrase("ask_area_code"), kind="ask_area_code")
        if not info.valid:
            self.counters["repeat_number"] += 1
            if self.counters["repeat_number"] > 2:
                return self._end(CANDIDATE_ONLY, "closing_generic")
            self.stage = ASK_NUMBER
            return self._say(self.phrase("ask_repeat_number"), kind="ask_repeat_number")
        return self._readback(c)

    def _readback(self, c: Contact) -> BotTurn:
        self.stage = READBACK
        self.readback_interrupted = False
        turn = self._say(self.phrase("readback", phone_spoken=phone.readback(c.digits)), kind="readback")
        c.readback_in = turn.utterance_id
        return turn

    def _handle_area_code(self, u: Utterance, nums: list[phone.PhoneCandidate]) -> BotTurn | None:
        c = self.pending
        if any(n.valid for n in nums):
            return None  # 전체 번호를 다시 불러준 경우 일반 처리
        area = phone.parse_area_code(u.text)
        if not area:
            return None
        new = self._new_contact(None, u, digits=area + c.digits, prev=c)
        new.raw_text = f"{c.raw_text} / {u.text}"
        self.pending_id = new.id
        return self._next_for_contact(new)

    def _handle_readback(self, u: Utterance, nums: list[phone.PhoneCandidate], interp: Interpretation) -> BotTurn | None:
        c = self.pending
        intent = interp.intent
        text = u.text

        # 1) 부분 정정: "마지막은 팔이 아니라 구예요"
        if intent == "correction" or phone._CORRECTION_RE.search(text):
            corrected = phone.apply_correction(text, c.digits)
            if corrected and corrected != c.digits:
                new = self._new_contact(None, u, digits=corrected, prev=c)
                new.raw_text = f"{c.raw_text} / 정정: {text}"
                self.pending_id = new.id
                return self._next_for_contact(new)

        # 2) 번호를 다시 불러줌
        full = [n for n in nums if n.valid]
        if full:
            if any(n.digits == c.digits for n in full):
                if _YES_WORDS.search(text[:10]):
                    intent = "confirm_yes"
                else:
                    return self._readback(c)
            else:
                return self._handle_numbers(u, nums)

        # 3) 확인
        if intent == "confirm_yes":
            if self.readback_interrupted:
                # 낭독이 중간에 끊겼으면 전체 번호를 들었다고 가정하지 않는다
                return self._readback(c)
            c.confirmation_status = "confirmed_by_source"
            c.confirmed_in = u.id
            if c.role == "unknown":
                c.role = "management_office"  # 낭독 문구가 '관리사무소 번호'인지 함께 물음
            if self._has_evidence(c):
                return self._end(SUCCESS, "closing_success")
            c.confirmation_status = "candidate"
            return self._readback(c)

        # 4) 부정
        if intent == "confirm_no":
            role = phone.detect_role(text)
            if role and role != "management_office":
                c.role = role  # "아니요, 그건 부동산 번호예요"
                self.pending_id = None
                if self.flags["asked_mgmt_after_other"]:
                    return self._end(REFERRED, "closing_generic")
                self.flags["asked_mgmt_after_other"] = True
                self.stage = ASK_OTHER
                return self._say(self.phrase("ask_management_after_other"), kind="ask_management_after_other")
            c.confirmation_status = "rejected"
            self.pending_id = None
            self.stage = ASK_NUMBER
            return self._say(self.phrase("ask_repeat_number"), kind="ask_repeat_number")

        # 5) 알아듣기 어려운 답: 한 번 더 읽어 준다
        if intent in ("unclear", "other_question") and self.counters["readback_unclear"] < 1:
            self.counters["readback_unclear"] += 1
            return self._readback(c)
        return None

    @staticmethod
    def _has_evidence(c: Contact) -> bool:
        """성공 처리 조건: 번호를 알려준 발화 + 봇의 낭독 + 상대의 확인 발화가 모두 연결됨."""
        return bool(c.provided_in and c.readback_in and c.confirmed_in and c.complete)

    # --------------------------------------------------------------- result
    def _finalize(self) -> None:
        if self.finalized:
            return
        if self.outcome == SUCCESS and not any(
            c.confirmation_status == "confirmed_by_source" and self._has_evidence(c) for c in self.contacts
        ):
            self.outcome = CANDIDATE_ONLY  # 근거 없는 성공 차단
        self.finalized = True
        self.ended_at = time.time()

    def result(self) -> dict:
        return {
            "call_id": self.call_id,
            "building": {"name": self.variables.get("building_name"), "address": self.variables.get("building_address")},
            "recipient": {"name": self.variables.get("recipient_name"), "number": self.variables.get("recipient_number")},
            "stage": self.stage,
            "outcome": self.outcome,
            "end_reason": self.end_reason,
            "contacts": [asdict(c) for c in self.contacts],
            "pending_contact_id": self.pending_id,
            "referrals": self.referrals,
            "callback_request": self.callback_note,
            "do_not_retry": self.do_not_retry,
            "utterances": [asdict(u) for u in self.utterances],
        }
