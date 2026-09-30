"""웹 API + 관리 화면 (가이드 08장 내부 API 설계).

실행: uvicorn callbot.app:app --reload  →  http://localhost:8000
"""

from __future__ import annotations

import csv
import io
import logging
import shutil
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import dialog
from .config import settings
from .db import Store, new_id
from .service import CallManager, PolicyError
from .telephony import twilio
from .templates import DEFAULT_TEMPLATE_NAME, TEMPLATE_VARIABLES, merged_with_default, validate_template

log = logging.getLogger("callbot")
STATIC = Path(__file__).parent / "static"


class JobIn(BaseModel):
    building_name: str = Field(min_length=1)
    building_address: str = Field(min_length=1)
    recipient_number: str = Field(min_length=4)
    requester_name: str = Field(min_length=1)
    inquiry_purpose: str = Field(min_length=1)
    recipient_name: str | None = None
    template_id: str | None = None
    voice_id: str | None = None


class StartIn(BaseModel):
    mode: str = "simulator"  # simulator | twilio


class UtteranceIn(BaseModel):
    text: str


class ResponseRef(BaseModel):
    response_id: str


class TemplateIn(BaseModel):
    content: dict


class ReviewIn(BaseModel):
    review_status: str  # approved | rejected | pending_review


def create_app(store: Store | None = None) -> FastAPI:
    store = store or Store(settings.db_path)
    manager = CallManager(store)
    app = FastAPI(title="관리사무소 연락처 수집 콜봇 (프로토타입)")
    app.state.store, app.state.manager = store, manager
    settings.audio_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/audio", StaticFiles(directory=settings.audio_dir), name="audio")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ------------------------------------------------------------- helpers
    def get_session(call_id: str) -> dialog.CallSession:
        s = manager.session(call_id)
        if not s:
            raise HTTPException(404, "진행 중인 통화 세션이 없습니다 (서버 재시작 시 메모리 세션은 사라집니다).")
        return s

    def turn_payload(s: dialog.CallSession, turn: dialog.BotTurn | None) -> dict:
        out = {"turn": turn.to_dict() if turn else None, "state": s.result()}
        if turn and manager.tts.name != "mock":
            out["turn"]["audio_url"] = f"/api/calls/{s.call_id}/audio/{turn.response_id}"
        return out

    # ---------------------------------------------------------------- pages
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    def config():
        return {
            "llm_provider": manager.interpreter.name,
            "tts_provider": manager.tts.name,
            "tts_can_clone": manager.tts.can_clone,
            "twilio_enabled": settings.twilio_enabled,
            "public_base_url": settings.public_base_url or None,
            "call_allowlist": settings.call_allowlist,
            "call_hours": settings.call_hours,
            "template_variables": TEMPLATE_VARIABLES,
        }

    # --------------------------------------------------------------- voices
    @app.post("/api/voices")
    def register_voice(name: str = Form(...), consent: bool = Form(False), file: UploadFile = File(...)):
        if not consent:
            raise HTTPException(400, "본인 목소리 사용 동의가 필요합니다.")
        settings.voice_dir.mkdir(parents=True, exist_ok=True)
        path = settings.voice_dir / f"{new_id('sample')}{Path(file.filename or '').suffix or '.wav'}"
        with path.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        provider_voice_id = None
        if manager.tts.can_clone:
            try:
                provider_voice_id = manager.tts.register_voice(name, path)
            except Exception as e:
                raise HTTPException(502, f"목소리 등록 실패: {e}")
        return store.add_voice(name, manager.tts.name, provider_voice_id, str(path), consent)

    @app.get("/api/voices")
    def list_voices():
        return store.list_voices()

    @app.delete("/api/voices/{voice_id}")
    def delete_voice(voice_id: str):
        store.delete_voice(voice_id)
        return {"ok": True}

    @app.post("/api/voices/{voice_id}/preview")
    def preview_voice(voice_id: str, body: UtteranceIn):
        voice = store.get_voice(voice_id)
        if not voice:
            raise HTTPException(404)
        path = manager.tts.synthesize_to_file(body.text[:200], voice["provider_voice_id"])
        if not path:
            return Response(status_code=204)  # 브라우저 기본 음성으로 재생
        return FileResponse(path, media_type=manager.tts.mime)

    # ------------------------------------------------------------ templates
    @app.get("/api/templates")
    def list_templates():
        return store.list_templates()

    @app.get("/api/templates/latest")
    def latest_template():
        t = store.latest_template(DEFAULT_TEMPLATE_NAME)
        t["content"] = merged_with_default(t["content"])
        return t

    @app.get("/api/templates/{template_id}")
    def get_template(template_id: str):
        t = store.get_template(template_id)
        if not t:
            raise HTTPException(404)
        return t

    @app.post("/api/templates")
    def save_template(body: TemplateIn):
        errors = validate_template(body.content)
        if errors:
            raise HTTPException(400, " ".join(errors))
        return store.save_template(DEFAULT_TEMPLATE_NAME, merged_with_default(body.content))

    # ----------------------------------------------------------------- jobs
    @app.post("/api/call-jobs")
    def create_job(body: JobIn):
        return store.create_job(body.model_dump())

    @app.get("/api/call-jobs")
    def list_jobs():
        return store.list_jobs()

    @app.post("/api/call-jobs/{job_id}/start")
    def start_job(job_id: str, body: StartIn):
        try:
            call, turn = manager.start_job(job_id, body.mode)
        except KeyError:
            raise HTTPException(404, "작업이 없습니다.")
        except PolicyError as e:
            raise HTTPException(409, str(e))
        s = manager.session(call["id"])
        return {"call": call, **(turn_payload(s, turn) if s else {})}

    @app.post("/api/call-jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        manager.cancel_job(job_id)
        return {"ok": True}

    # ------------------------------------------ simulator / browser voice
    @app.post("/api/calls/{call_id}/utterance")
    def utterance(call_id: str, body: UtteranceIn):
        s = get_session(call_id)
        with manager.lock_for(call_id):
            turn = s.on_recipient(body.text)
        return turn_payload(s, turn)

    @app.post("/api/calls/{call_id}/silence")
    def silence(call_id: str):
        s = get_session(call_id)
        with manager.lock_for(call_id):
            turn = s.on_silence()
        return turn_payload(s, turn)

    @app.post("/api/calls/{call_id}/interrupt")
    def interrupt(call_id: str, body: ResponseRef):
        s = get_session(call_id)
        with manager.lock_for(call_id):
            s.on_interrupt(body.response_id)
        return {"ok": True}

    @app.post("/api/calls/{call_id}/played")
    def played(call_id: str, body: ResponseRef):
        s = get_session(call_id)
        with manager.lock_for(call_id):
            s.on_played(body.response_id)
        return {"ok": True}

    @app.post("/api/calls/{call_id}/hangup")
    def hangup(call_id: str):
        s = get_session(call_id)
        with manager.lock_for(call_id):
            s.on_hangup("hangup_by_tester")
        return turn_payload(s, None)

    @app.get("/api/calls/{call_id}/audio/{response_id}")
    def call_audio(call_id: str, response_id: str):
        s = get_session(call_id)
        u = next((u for u in s.utterances if u.response_id == response_id), None)
        if not u:
            raise HTTPException(404)
        path = manager.audio_for(s, u.text)
        if not path:
            return Response(status_code=204)
        return FileResponse(path, media_type=manager.tts.mime)

    # -------------------------------------------------------------- results
    @app.get("/api/calls")
    def list_calls():
        calls = store.list_calls()
        for c in calls:
            c["contacts"] = store.contacts(c["id"])
        return calls

    @app.get("/api/calls/{call_id}")
    def get_call(call_id: str):
        call = store.get_call(call_id)
        if not call:
            raise HTTPException(404)
        job = store.get_job(call["job_id"])
        return {
            "call_id": call_id,
            "building": {"name": job["building_name"], "address": job["building_address"]},
            "recipient": {"name": job["recipient_name"], "number": job["recipient_number"]},
            "template_version": f"{DEFAULT_TEMPLATE_NAME}-v{call['template_version']}",
            "interpreter": call["interpreter"],
            "mode": call["mode"],
            "status": call["status"],
            "outcome": call["outcome"],
            "end_reason": call["end_reason"],
            "contacts": store.contacts(call_id),
            "utterances": store.utterances(call_id),
            "recording_ref": None,
            "callback_request": call["callback_note"],
            "do_not_retry": bool(job["do_not_retry"]),
            "review_status": call["review_status"],
        }

    @app.post("/api/calls/{call_id}/review")
    def review(call_id: str, body: ReviewIn):
        if body.review_status not in ("approved", "rejected", "pending_review"):
            raise HTTPException(400)
        store.update_call(call_id, review_status=body.review_status)
        return {"ok": True}

    @app.get("/api/results.csv")
    def results_csv(only_confirmed: bool = False):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["building_name", "building_address", "source_name", "source_number", "phone", "role",
                    "confirmation_status", "reachability_status", "outcome", "review_status", "call_id",
                    "evidence_utterances"])
        for c in store.list_calls():
            contacts = store.contacts(c["id"])
            if only_confirmed:
                contacts = [x for x in contacts if x["confirmation_status"] == "confirmed_by_source"]
            rows = [x for x in contacts if x["confirmation_status"] != "superseded"] or ([None] if not only_confirmed else [])
            for x in rows:
                evidence = ""
                if x:
                    evidence = ",".join(x["provided_in"] + [e for e in (x["readback_in"], x["confirmed_in"]) if e])
                w.writerow([
                    c["building_name"], c["building_address"], c["recipient_name"], c["recipient_number"],
                    (x["formatted"] or x["digits"]) if x else "", x["role"] if x else "",
                    x["confirmation_status"] if x else "", x["reachability_status"] if x else "",
                    c["outcome"], c["review_status"], c["id"], evidence,
                ])
        data = "﻿" + buf.getvalue()  # 엑셀 한글 깨짐 방지 BOM
        return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": "attachment; filename=results.csv"})

    # ------------------------------------------------------ Twilio webhooks
    async def twilio_params(request: Request) -> dict:
        form = await request.form()
        params = {k: str(v) for k, v in form.items()}
        if settings.twilio_validate_signature:
            url = settings.public_base_url.rstrip("/") + request.url.path
            if request.url.query:
                url += "?" + request.url.query
            if not twilio.validate_signature(url, params, request.headers.get("X-Twilio-Signature", "")):
                raise HTTPException(403, "invalid signature")
        return params

    def twiml_for(s: dialog.CallSession, turn: dialog.BotTurn | None) -> PlainTextResponse:
        base = settings.public_base_url.rstrip("/")
        if turn is None:
            last = next((u for u in reversed(s.utterances) if u.speaker == "bot"), None)
            if not last or s.ended:
                return PlainTextResponse(twilio.twiml_hangup(), media_type="application/xml")
            turn = dialog.BotTurn(last.response_id, last.id, last.text, False, s.stage)
        path = manager.audio_for(s, turn.text)
        audio_url = f"{base}/audio/{path.name}" if path else None
        action = f"{base}/webhooks/twilio/gather?call_id={s.call_id}&turn={len(s.utterances)}"
        s.on_played(turn.response_id)
        return PlainTextResponse(twilio.twiml_turn(turn.text, audio_url, action, turn.end_call),
                                 media_type="application/xml")

    @app.post("/webhooks/twilio/voice")
    async def twilio_voice(request: Request, call_id: str):
        await twilio_params(request)
        s = get_session(call_id)
        with manager.lock_for(call_id):
            turn = s.start() if not s.utterances else None  # 중복 웹훅이면 마지막 문장 재전송
        return twiml_for(s, turn)

    @app.post("/webhooks/twilio/gather")
    async def twilio_gather(request: Request, call_id: str, turn: int = 0):
        params = await twilio_params(request)
        s = get_session(call_id)
        if not store.record_event_once(f"{params.get('CallSid')}:gather:{turn}", call_id, params):
            return twiml_for(s, None)
        speech = params.get("SpeechResult", "").strip()
        with manager.lock_for(call_id):
            bot_turn = s.on_recipient(speech) if speech else s.on_silence()
        return twiml_for(s, bot_turn)

    @app.post("/webhooks/twilio/status")
    async def twilio_status(request: Request, call_id: str):
        params = await twilio_params(request)
        status = params.get("CallStatus", "")
        store.record_event_once(f"{params.get('CallSid')}:status:{status}", call_id, params)
        if status in ("completed", "busy", "no-answer", "failed", "canceled"):
            s = manager.session(call_id)
            if s:
                with manager.lock_for(call_id):
                    s.on_hangup(status)
        return Response(status_code=204)

    return app


app = create_app()
