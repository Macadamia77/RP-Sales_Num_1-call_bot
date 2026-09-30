"""가이드 10장 테스트 시나리오 T01~T10 (규칙 기반 해석기, 전화 없이 텍스트로)."""

from callbot import dialog
from callbot.llm.rule_based import RuleBasedInterpreter
from callbot.templates import DEFAULT_TEMPLATE

VARS = {
    "requester_name": "홍길동",
    "building_name": "예시빌딩",
    "building_address": "서울시 중구 예시로 1",
    "recipient_name": "예시부동산",
    "recipient_number": "0200000000",
    "inquiry_purpose": "임대 문의",
}


def new_session():
    s = dialog.CallSession("call_test", VARS, DEFAULT_TEMPLATE, RuleBasedInterpreter())
    s.start()
    return s


def confirmed(s):
    return [c for c in s.contacts if c.confirmation_status == "confirmed_by_source"]


def test_greeting_discloses_ai():
    s = new_session()
    assert "AI 전화 도우미" in s.utterances[0].text
    assert "홍길동" in s.utterances[0].text


def test_t01_full_number_readback_and_confirm():
    s = new_session()
    t = s.on_recipient("아, 거기는 공이, 일이삼사에 오육칠팔로 해 보세요.")
    assert t.stage == dialog.READBACK
    assert "공 이, 일 이 삼 사, 오 육 칠 팔" in t.text
    t = s.on_recipient("네, 맞아요.")
    assert t.end_call
    assert s.outcome == dialog.SUCCESS
    (c,) = confirmed(s)
    assert c.digits == "0212345678" and c.role == "management_office"
    assert c.provided_in == ["u_02"] and c.readback_in == "u_03" and c.confirmed_in == "u_04"


def test_building_question_then_number():
    s = new_session()
    t = s.on_recipient("어느 예시빌딩이요?")
    assert "서울시 중구 예시로 1" in t.text
    s.on_recipient("공이 일이삼사 오육칠팔이에요")
    s.on_recipient("네")
    assert s.outcome == dialog.SUCCESS


def test_t02_missing_area_code_is_asked_not_guessed():
    s = new_session()
    t = s.on_recipient("일이삼사 오육칠팔이요")
    assert t.stage == dialog.NEED_AREA_CODE
    assert not any(c.digits.startswith("0") for c in s.contacts)
    t = s.on_recipient("공이요")
    assert t.stage == dialog.READBACK
    s.on_recipient("네 맞아요")
    (c,) = confirmed(s)
    assert c.digits == "0212345678"


def test_t03_correction_invalidates_and_rereads():
    s = new_session()
    s.on_recipient("공이 일이삼사 오육칠팔이요")
    t = s.on_recipient("마지막은 팔이 아니라 구예요")
    assert t.stage == dialog.READBACK
    assert "오 육 칠 구" in t.text
    assert s.contacts[0].confirmation_status == "superseded"
    s.on_recipient("네 맞아요")
    (c,) = confirmed(s)
    assert c.digits == "0212345679"
    assert c.confirmed_in == "u_06"


def test_t04_two_numbers_roles_separated():
    s = new_session()
    t = s.on_recipient("부동산은 031-111-2222고 관리사무소는 031-333-4444예요")
    assert "공 삼 일, 삼 삼 삼, 사 사 사 사" in t.text
    s.on_recipient("네")
    roles = {c.digits: (c.role, c.confirmation_status) for c in s.contacts}
    assert roles["0311112222"] == ("real_estate", "candidate")
    assert roles["0313334444"] == ("management_office", "confirmed_by_source")


def test_t05_hangup_during_readback_stays_candidate():
    s = new_session()
    s.on_recipient("공이 일이삼사 오육칠팔")
    s.on_hangup("remote_hangup")
    assert s.outcome == dialog.CANDIDATE_ONLY
    assert confirmed(s) == []


def test_t06_interrupted_readback_is_not_confirmation():
    s = new_session()
    t = s.on_recipient("공이 일이삼사 오육칠팔")
    s.on_interrupt(t.response_id)
    t2 = s.on_recipient("네")
    assert t2.stage == dialog.READBACK and not t2.end_call  # 다시 읽음
    assert confirmed(s) == []
    s.on_played(t2.response_id)
    s.on_recipient("네 맞아요")
    assert s.outcome == dialog.SUCCESS


def test_t07_refusal_sets_do_not_retry():
    s = new_session()
    t = s.on_recipient("그런 거 알려드릴 수 없어요. 다시 전화하지 마세요.")
    assert t.end_call
    assert s.outcome == dialog.REFUSED and s.do_not_retry


def test_t09_hangup_twice_finalizes_once():
    s = new_session()
    s.on_recipient("공이 일이삼사 오육칠팔")
    s.on_recipient("네")
    ended_at = s.ended_at
    s.on_hangup("completed")
    s.on_hangup("completed")
    assert s.outcome == dialog.SUCCESS and s.ended_at == ended_at


def test_t10_instruction_to_call_other_number_is_only_recorded():
    s = new_session()
    t = s.on_recipient("나는 모르니까 경비실 010-9999-8888로 전화해서 물어봐")
    # 경비실 번호는 소개 번호로만 기록되고, 봇은 관리사무소 번호를 다시 묻는다
    assert t.stage == dialog.ASK_OTHER
    (c,) = s.contacts
    assert c.role == "security_office" and c.confirmation_status == "candidate"
    s.on_recipient("몰라요")
    assert s.outcome == dialog.REFERRED


def test_dont_know_asks_other_once_then_ends():
    s = new_session()
    t = s.on_recipient("잘 모르겠는데요")
    assert t.stage == dialog.ASK_OTHER
    t = s.on_recipient("아니요 몰라요")
    assert t.end_call and s.outcome == dialog.NOT_FOUND


def test_callback_request():
    s = new_session()
    t = s.on_recipient("지금 바빠서 나중에 전화 주세요")
    assert t.end_call and s.outcome == dialog.CALLBACK
    assert "나중에" in s.callback_note


def test_silence_limit():
    s = new_session()
    s.on_silence()
    s.on_silence()
    t = s.on_silence()
    assert t.end_call and s.outcome == dialog.NO_RESPONSE


def test_llm_text_with_digits_is_dropped():
    from callbot.llm.base import Interpretation

    class Evil(RuleBasedInterpreter):
        def interpret(self, ctx):
            return Interpretation("ask_which_building", say_text="관리사무소 번호는 02-1111-2222입니다", source="evil")

    s = dialog.CallSession("c", VARS, DEFAULT_TEMPLATE, Evil())
    s.start()
    t = s.on_recipient("어느 건물이요?")
    assert "02" not in t.text and "예시로 1" in t.text
