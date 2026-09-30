"""음성 합성(TTS)과 목소리 등록.

- mock       : 서버는 음성을 만들지 않는다. 브라우저는 speechSynthesis(무료)로, Twilio는 <Say>로 읽는다.
- edge       : edge-tts (무료, 비공식). 한국어 기본 음성만 가능하고 본인 목소리 복제는 안 된다.
- elevenlabs : 유료. Instant Voice Cloning으로 본인 목소리를 등록하고 voice_id로 합성한다.

같은 문장은 파일로 캐시한다 (고정 문구가 많아 비용·지연을 줄인다).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

import httpx

from ..config import settings

log = logging.getLogger(__name__)


class TTS:
    name = "mock"
    mime = "audio/mpeg"
    can_clone = False

    def synthesize(self, text: str, voice_id: str | None = None) -> bytes | None:
        return None

    def register_voice(self, name: str, sample_path: Path) -> str | None:
        """본인 목소리 등록. 공급사 voice_id를 돌려준다. 지원하지 않으면 None."""
        return None

    def synthesize_to_file(self, text: str, voice_id: str | None = None) -> Path | None:
        key = hashlib.sha1(f"{self.name}|{voice_id}|{text}".encode()).hexdigest()[:20]
        path = settings.audio_dir / f"{key}.mp3"
        if path.exists():
            return path
        try:
            audio = self.synthesize(text, voice_id)
        except Exception as e:
            log.warning("TTS 실패 (%s): %s", self.name, e)
            return None
        if not audio:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio)
        return path


class EdgeTTS(TTS):
    name = "edge"

    def synthesize(self, text: str, voice_id: str | None = None) -> bytes | None:
        import edge_tts  # 선택 의존성: pip install edge-tts

        async def run() -> bytes:
            buf = bytearray()
            async for chunk in edge_tts.Communicate(text, settings.edge_voice).stream():
                if chunk["type"] == "audio":
                    buf.extend(chunk["data"])
            return bytes(buf)

        return asyncio.run(run())


class ElevenLabsTTS(TTS):
    name = "elevenlabs"
    can_clone = True
    base = "https://api.elevenlabs.io/v1"

    def _headers(self) -> dict:
        if not settings.elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY 없음")
        return {"xi-api-key": settings.elevenlabs_api_key}

    def synthesize(self, text: str, voice_id: str | None = None) -> bytes | None:
        if not voice_id:
            raise RuntimeError("등록된 voice_id가 없습니다")
        r = httpx.post(
            f"{self.base}/text-to-speech/{voice_id}",
            params={"output_format": "mp3_44100_128"},
            headers=self._headers(),
            json={"text": text, "model_id": settings.elevenlabs_model},
            timeout=20,
        )
        r.raise_for_status()
        return r.content

    def register_voice(self, name: str, sample_path: Path) -> str | None:
        with open(sample_path, "rb") as f:
            r = httpx.post(
                f"{self.base}/voices/add",
                headers=self._headers(),
                data={"name": name},
                files={"files": (sample_path.name, f)},
                timeout=60,
            )
        r.raise_for_status()
        return r.json()["voice_id"]


def make_tts(provider: str | None = None) -> TTS:
    provider = (provider or settings.tts_provider).lower()
    if provider == "edge":
        return EdgeTTS()
    if provider == "elevenlabs":
        return ElevenLabsTTS()
    return TTS()
