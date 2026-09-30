import pytest
from fastapi.testclient import TestClient

from callbot.app import create_app
from callbot.db import Store

JOB = {
    "requester_name": "홍길동",
    "building_name": "예시빌딩",
    "building_address": "서울시 중구 예시로 1",
    "recipient_name": "예시부동산",
    "recipient_number": "02-000-0000",
    "inquiry_purpose": "상가 임대 문의",
}


@pytest.fixture
def client():
    return TestClient(create_app(Store(":memory:")))


def start(client):
    job = client.post("/api/call-jobs", json=JOB).json()
    res = client.post(f"/api/call-jobs/{job['id']}/start", json={"mode": "simulator"}).json()
    return job, res


def test_full_simulated_call_and_csv(client):
    job, res = start(client)
    call_id = res["call"]["id"]
    assert "AI 전화 도우미" in res["turn"]["text"]

    r = client.post(f"/api/calls/{call_id}/utterance", json={"text": "공이, 일이삼사에 오육칠팔이요"}).json()
    assert r["turn"]["text"].startswith("공 이, 일 이 삼 사, 오 육 칠 팔")
    r = client.post(f"/api/calls/{call_id}/utterance", json={"text": "네, 맞아요"}).json()
    assert r["turn"]["end_call"]

    detail = client.get(f"/api/calls/{call_id}").json()
    assert detail["outcome"] == "number_confirmed_by_source"
    assert detail["template_version"] == "management-contact-v1"
    (c,) = detail["contacts"]
    assert c["formatted"] == "02-1234-5678" and c["confirmed_in"] == "u_04"

    jobs = client.get("/api/call-jobs").json()
    assert jobs[0]["status"] == "done"

    csv = client.get("/api/results.csv").text
    assert "02-1234-5678" in csv and "confirmed_by_source" in csv


def test_start_is_idempotent_while_in_progress(client):
    job, res = start(client)
    again = client.post(f"/api/call-jobs/{job['id']}/start", json={"mode": "simulator"}).json()
    assert again["call"]["id"] == res["call"]["id"]
    assert again["turn"] is None


def test_refused_job_cannot_be_restarted(client):
    job, res = start(client)
    client.post(f"/api/calls/{res['call']['id']}/utterance", json={"text": "다시는 전화하지 마세요"})
    r = client.post(f"/api/call-jobs/{job['id']}/start", json={"mode": "simulator"})
    assert r.status_code == 409


def test_real_call_blocked_without_twilio(client):
    job = client.post("/api/call-jobs", json=JOB).json()
    r = client.post(f"/api/call-jobs/{job['id']}/start", json={"mode": "twilio"})
    assert r.status_code == 409


def test_template_versioning_and_guard(client):
    t = client.get("/api/templates/latest").json()
    content = t["content"]
    content["phrases"]["readback"] = "번호 맞나요?"
    assert client.post("/api/templates", json={"content": content}).status_code == 400
    content["phrases"]["readback"] = "{phone_spoken}, 이 번호가 {building_name} 관리사무소 번호 맞으세요?"
    saved = client.post("/api/templates", json={"content": content}).json()
    assert saved["version"] == 2
    _, res = start(client)
    r = client.post(f"/api/calls/{res['call']['id']}/utterance", json={"text": "공이 일이삼사 오육칠팔"}).json()
    assert "맞으세요?" in r["turn"]["text"]


def test_voice_registration_requires_consent(client):
    files = {"file": ("me.wav", b"RIFF0000", "audio/wav")}
    assert client.post("/api/voices", data={"name": "나"}, files=files).status_code == 400
    v = client.post("/api/voices", data={"name": "나", "consent": "true"}, files=files).json()
    assert v["provider"] == "mock"


def test_twilio_webhook_flow_with_dedupe(client, monkeypatch):
    from callbot import service
    from callbot.config import settings

    monkeypatch.setattr(settings, "twilio_validate_signature", False)
    monkeypatch.setattr(settings, "public_base_url", "https://example.test")
    monkeypatch.setattr(service.CallManager, "_check_real_call_policy", staticmethod(lambda job: None))
    monkeypatch.setattr(service.twilio, "start_call", lambda to, call_id: "CA123")

    job = client.post("/api/call-jobs", json=JOB).json()
    call = client.post(f"/api/call-jobs/{job['id']}/start", json={"mode": "twilio"}).json()["call"]
    cid = call["id"]
    x = client.post(f"/webhooks/twilio/voice?call_id={cid}", data={"CallSid": "CA123"}).text
    assert "<Gather" in x and "AI 전화 도우미" in x
    g = {"CallSid": "CA123", "SpeechResult": "공이 일이삼사 오육칠팔이요"}
    x1 = client.post(f"/webhooks/twilio/gather?call_id={cid}&turn=1", data=g).text
    x2 = client.post(f"/webhooks/twilio/gather?call_id={cid}&turn=1", data=g).text  # 재전송
    assert "공 이, 일 이 삼 사" in x1 and x1 == x2
    x = client.post(f"/webhooks/twilio/gather?call_id={cid}&turn=3", data={"CallSid": "CA123", "SpeechResult": "네 맞아요"}).text
    assert "<Hangup/>" in x
    client.post(f"/webhooks/twilio/status?call_id={cid}", data={"CallSid": "CA123", "CallStatus": "completed"})
    detail = client.get(f"/api/calls/{cid}").json()
    assert detail["outcome"] == "number_confirmed_by_source"
    assert [u["speaker"] for u in detail["utterances"]] == ["bot", "recipient", "bot", "recipient", "bot"]
