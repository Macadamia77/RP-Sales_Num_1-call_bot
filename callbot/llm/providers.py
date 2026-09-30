"""외부 LLM 어댑터. 모두 선택 사항이며, 실패·지연 시 규칙 기반 해석기로 대체한다.

- ollama : 로컬 오픈소스 모델 (무료). 예: qwen2.5:7b, gemma3 등 한국어가 되는 모델
- gemini : Google AI Studio 무료 등급 API 키로 사용 가능 (무료 등급은 데이터가 학습에 쓰일 수 있음)
- claude : Anthropic API (유료). 공식 SDK 사용

OpenAI는 사용하지 않는다.
"""

from __future__ import annotations

import logging

import httpx

from ..config import settings
from .base import DialogContext, Interpretation, Interpreter, build_prompts, parse_llm_json
from .rule_based import RuleBasedInterpreter

log = logging.getLogger(__name__)


class _FallbackMixin(Interpreter):
    """외부 호출이 실패하면 규칙 기반 결과를 돌려준다."""

    def __init__(self):
        self.fallback = RuleBasedInterpreter()

    def _call(self, system: str, user: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def interpret(self, ctx: DialogContext) -> Interpretation:
        system, user = build_prompts(ctx)
        try:
            data = parse_llm_json(self._call(system, user))
        except Exception as e:  # 네트워크·인증·타임아웃 등
            log.warning("%s 호출 실패, 규칙 기반으로 대체: %s", self.name, e)
            data = None
        if not data:
            result = self.fallback.interpret(ctx)
            result.source = f"{self.name}->rule"
            return result
        return Interpretation(
            intent=data["intent"],
            say_text=data.get("say_text") or None,
            note=data.get("note") or None,
            source=self.name,
        )


class OllamaInterpreter(_FallbackMixin):
    name = "ollama"

    def _call(self, system: str, user: str) -> str:
        r = httpx.post(
            f"{settings.ollama_url.rstrip('/')}/api/chat",
            json={
                "model": settings.ollama_model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.2},
            },
            timeout=settings.llm_timeout,
        )
        r.raise_for_status()
        return r.json()["message"]["content"]


class GeminiInterpreter(_FallbackMixin):
    name = "gemini"

    def _call(self, system: str, user: str) -> str:
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY 없음")
        r = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent",
            headers={"x-goog-api-key": settings.gemini_api_key},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
            },
            timeout=settings.llm_timeout,
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]


class ClaudeInterpreter(_FallbackMixin):
    name = "claude"

    def __init__(self):
        super().__init__()
        self._client = None

    def _call(self, system: str, user: str) -> str:
        if self._client is None:
            import anthropic  # 선택 의존성: pip install anthropic

            self._client = anthropic.Anthropic(timeout=settings.llm_timeout, max_retries=0)
        response = self._client.beta.messages.create(
            model=settings.claude_model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("refusal")
        return "".join(b.text for b in response.content if b.type == "text")


def make_interpreter(provider: str | None = None) -> Interpreter:
    provider = (provider or settings.llm_provider).lower()
    if provider == "ollama":
        return OllamaInterpreter()
    if provider == "gemini":
        return GeminiInterpreter()
    if provider == "claude":
        return ClaudeInterpreter()
    return RuleBasedInterpreter()
