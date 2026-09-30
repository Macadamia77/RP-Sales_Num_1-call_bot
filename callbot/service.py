"""발신 작업 실행과 통화 세션 관리 (통화 제어 서버, 가이드 02·08장).

- 작업당 진행 중 통화는 1개 (중복 발신 방지)
- 거절·재발신 제외 작업은 다시 걸지 않음
- 실제 발신은 허용 목록 + 발신 시간대 안에서만
- 결과는 세션이 끝날 때 한 번만 확정
"""

from __future__ import annotations

import datetime as dt
import threading
from zoneinfo import ZoneInfo

from . import dialog
from .config import settings
from .db import Store
from .llm.providers import make_interpreter
from .telephony import twilio
from .templates import DEFAULT_TEMPLATE, DEFAULT_TEMPLATE_NAME
from .tts.providers import make_tts


class PolicyError(Exception):
    pass


JOB_STATUS_BY_OUTCOME = {
    dialog.SUCCESS: "done",
    dialog.REFUSED: "do_not_retry",
    dialog.WRONG: "do_not_retry",
    dialog.CALLBACK: "callback_requested",
    dialog.NOT_CONNECTED: "queued",  # 재시도 가능
    dialog.NO_RESPONSE: "queued",
}


class CallManager:
    def __init__(self, store: Store):
        self.store = store
        self.interpreter = make_interpreter()
        self.tts = make_tts()
        self.sessions: dict[str, dialog.CallSession] = {}
        self.session_jobs: dict[str, str] = {}
        self.locks: dict[str, threading.Lock] = {}
        self._start_lock = threading.Lock()
        if not store.latest_template(DEFAULT_TEMPLATE_NAME):
            store.save_template(DEFAULT_TEMPLATE_NAME, DEFAULT_TEMPLATE)

    # ------------------------------------------------------------ sessions
    def lock_for(self, call_id: str) -> threading.Lock:
        return self.locks.setdefault(call_id, threading.Lock())

    def session(self, call_id: str) -> dialog.CallSession | None:
        return self.sessions.get(call_id)

    def _on_change(self, s: dialog.CallSession) -> None:
        job_id = self.session_jobs[s.call_id]
        self.store.sync_session(s, job_id)
        if s.finalized:
            call = self.store.get_call(s.call_id)
            if call and call["status"] == "in_progress":  # 결과 확정은 한 번만
                self.store.update_call(
                    s.call_id, status="ended", outcome=s.outcome, end_reason=s.end_reason,
                    callback_note=s.callback_note, ended_at=s.ended_at,
                )
                status = JOB_STATUS_BY_OUTCOME.get(s.outcome, "needs_review")
                fields = {"status": status}
                if s.do_not_retry:
                    fields.update(status="do_not_retry", do_not_retry=1)
                self.store.update_job(job_id, **fields)

    # ---------------------------------------------------------------- jobs
    def start_job(self, job_id: str, mode: str) -> tuple[dict, dialog.BotTurn | None]:
        with self._start_lock:
            job = self.store.get_job(job_id)
            if not job:
                raise KeyError(job_id)
            active = self.store.active_call_for_job(job_id)
            if active:
                return active, None  # 이미 진행 중: 새로 걸지 않는다 (멱등)
            if job["do_not_retry"]:
                raise PolicyError("재발신 제외된 작업입니다 (거절 또는 잘못된 문의처).")
            if job["status"] in ("done", "cancelled"):
                raise PolicyError(f"이미 {job['status']} 상태인 작업입니다.")
            if mode == "twilio":
                self._check_real_call_policy(job)
                if job["attempts"] >= settings.max_attempts_per_job:
                    raise PolicyError("시도 횟수 상한에 도달했습니다.")

            template = self.store.get_template(job["template_id"]) if job["template_id"] else None
            template = template or self.store.latest_template(DEFAULT_TEMPLATE_NAME)
            call = self.store.create_call(job_id, mode, template, self.interpreter.name)
            self.store.update_job(job_id, status="in_progress", attempts=job["attempts"] + 1)

            variables = {k: job[k] for k in (
                "requester_name", "building_name", "building_address", "recipient_name",
                "recipient_number", "inquiry_purpose")}
            variables["recipient_name"] = variables["recipient_name"] or ""
            s = dialog.CallSession(call["id"], variables, template["content"], self.interpreter, self._on_change)
            s.voice_id = self._provider_voice_id(job)
            self.sessions[call["id"]] = s
            self.session_jobs[call["id"]] = job_id

        if mode == "twilio":
            try:
                sid = twilio.start_call(job["recipient_number"], call["id"])
            except Exception as e:
                s.on_hangup(f"dial_failed: {e}")
                raise PolicyError(f"Twilio 발신 실패: {e}") from e
            self.store.update_call(call["id"], provider_call_id=sid)
            return self.store.get_call(call["id"]), None
        with self.lock_for(call["id"]):
            turn = s.start()
        return self.store.get_call(call["id"]), turn

    def cancel_job(self, job_id: str) -> None:
        active = self.store.active_call_for_job(job_id)
        if active:
            if active["mode"] == "twilio" and active["provider_call_id"]:
                try:
                    twilio.hangup(active["provider_call_id"])
                except Exception:
                    pass
            s = self.session(active["id"])
            if s:
                with self.lock_for(active["id"]):
                    s.on_hangup("cancelled_by_user")
            else:
                self.store.update_call(active["id"], status="ended", end_reason="cancelled_by_user")
        self.store.update_job(job_id, status="cancelled")

    def _provider_voice_id(self, job: dict) -> str | None:
        if not job.get("voice_id"):
            return None
        voice = self.store.get_voice(job["voice_id"])
        return voice["provider_voice_id"] if voice else None

    @staticmethod
    def _check_real_call_policy(job: dict) -> None:
        if not settings.twilio_enabled:
            raise PolicyError("Twilio 설정(TWILIO_ACCOUNT_SID/AUTH_TOKEN/FROM_NUMBER)이 없습니다.")
        if not settings.public_base_url:
            raise PolicyError("PUBLIC_BASE_URL이 없습니다 (ngrok 등 외부에서 접근 가능한 주소 필요).")
        number = "".join(ch for ch in job["recipient_number"] if ch.isdigit())
        allow = {"".join(ch for ch in n if ch.isdigit()) for n in settings.call_allowlist}
        if number not in allow:
            raise PolicyError("CALL_ALLOWLIST에 없는 번호입니다. 프로토타입은 허용 목록 번호로만 실제 발신합니다.")
        start, end = (int(x) for x in settings.call_hours.split("-"))
        hour = dt.datetime.now(ZoneInfo("Asia/Seoul")).hour
        if not (start <= hour < end):
            raise PolicyError(f"발신 가능 시간({settings.call_hours}시, 한국 시간)이 아닙니다.")

    # ----------------------------------------------------------------- audio
    def audio_for(self, s: dialog.CallSession, text: str):
        return self.tts.synthesize_to_file(text, s.voice_id)
