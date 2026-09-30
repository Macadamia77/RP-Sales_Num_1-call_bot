"""환경 변수 설정. 프로젝트 루트의 .env 파일도 읽는다 (.env.example 참고)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _list(key: str) -> list[str]:
    return [x.strip() for x in _env(key).split(",") if x.strip()]


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(ROOT / "data"))))

    # LLM: rule(기본, 무료) | ollama(무료, 로컬) | gemini(무료 등급) | claude(유료)
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "rule"))
    llm_timeout: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT", "6")))
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: _env("OLLAMA_MODEL", "qwen2.5:7b"))
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-flash-latest"))
    claude_model: str = field(default_factory=lambda: _env("CLAUDE_MODEL", "claude-opus-5-5"))

    # TTS: mock(브라우저/전화사 기본 음성) | edge(무료, 목소리 복제 없음) | elevenlabs(유료, 본인 목소리 복제)
    tts_provider: str = field(default_factory=lambda: _env("TTS_PROVIDER", "mock"))
    edge_voice: str = field(default_factory=lambda: _env("EDGE_TTS_VOICE", "ko-KR-SunHiNeural"))
    elevenlabs_api_key: str = field(default_factory=lambda: _env("ELEVENLABS_API_KEY"))
    elevenlabs_model: str = field(default_factory=lambda: _env("ELEVENLABS_MODEL", "eleven_multilingual_v2"))

    # 전화: Twilio (선택). 없으면 시뮬레이터만 사용
    twilio_account_sid: str = field(default_factory=lambda: _env("TWILIO_ACCOUNT_SID"))
    twilio_auth_token: str = field(default_factory=lambda: _env("TWILIO_AUTH_TOKEN"))
    twilio_from_number: str = field(default_factory=lambda: _env("TWILIO_FROM_NUMBER"))
    twilio_validate_signature: bool = field(default_factory=lambda: _env("TWILIO_VALIDATE_SIGNATURE", "1") == "1")
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL"))

    # 발신 정책 (가이드 11장): 허용 목록에 있는 번호에만 실제 발신
    call_allowlist: list[str] = field(default_factory=lambda: _list("CALL_ALLOWLIST"))
    call_hours: str = field(default_factory=lambda: _env("CALL_HOURS", "09-18"))
    max_attempts_per_job: int = field(default_factory=lambda: int(_env("MAX_ATTEMPTS_PER_JOB", "2")))

    @property
    def twilio_enabled(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from_number)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "callbot.sqlite3"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def voice_dir(self) -> Path:
        return self.data_dir / "voices"


settings = Settings()
