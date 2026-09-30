"""한국어 발화에서 전화번호를 추출·정규화·검증하고, 재확인용 낭독 문장을 만든다.

설계 원칙 (가이드 06장):
- 번호는 LLM이 아니라 코드가 추출한다. LLM은 번호의 '용도' 분류만 돕는다.
- 앞자리 0을 보존하고, 지역번호나 누락된 자리를 임의로 채우지 않는다.
- 낭독 문장은 저장한 숫자에서 코드가 생성한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SINO_DIGITS = {
    "공": "0", "영": "0", "빵": "0",
    "일": "1", "이": "2", "삼": "3", "사": "4", "오": "5",
    "육": "6", "륙": "6", "칠": "7", "팔": "8", "구": "9",
}
# 긴 단어부터 매칭해야 "일곱"이 "일"+"곱"으로 쪼개지지 않는다.
NATIVE_DIGITS = sorted(
    {
        "하나": "1", "둘": "2", "셋": "3", "넷": "4", "다섯": "5",
        "여섯": "6", "일곱": "7", "여덟": "8", "아홉": "9",
    }.items(),
    key=lambda kv: -len(kv[0]),
)
SPOKEN = {"0": "공", "1": "일", "2": "이", "3": "삼", "4": "사",
          "5": "오", "6": "육", "7": "칠", "8": "팔", "9": "구"}

# 숫자 덩어리 끝에 붙는 조사·어미. 긴 것부터 검사한다.
# 주의: 단독 "이"는 넣지 않는다 ("공이" = 02). "구요"도 넣지 않는다 ("팔구요" = 89).
SUFFIXES = sorted(
    [
        "이라고요", "이었어요", "이에요", "이예요", "입니다", "이구요", "이고요",
        "이라고", "인데요", "으로요", "번이요", "번이에요", "번입니다", "이요", "이고",
        "이야", "이죠", "이랑", "인데", "이다", "예요", "에요", "라고", "으로", "고요",
        "하고", "로요", "번", "로", "랑", "요", "고", "에", "야", "죠", "은", "는", "도", "만",
    ],
    key=len,
    reverse=True,
)
SEPARATOR_CHARS = set("-–—.·/()~")
PUNCT = ",.!?·…\"'“”‘’()[]"

# 숫자 덩어리 사이에 끼어도 같은 번호로 이어 보는 말.
FILLER_CHUNKS = {
    "국번", "국번은", "국번이", "국번이요", "다음", "다음에", "다음은", "그다음", "그다음에",
    "그리고", "하고", "에", "다시", "뒤에", "뒷자리", "뒷자리는", "뒷번호", "뒷번호는",
    "앞자리는", "지역번호는", "지역번호", "번호는", "-",
}

AREA_CODES = {
    "02", "031", "032", "033", "041", "042", "043", "044",
    "051", "052", "053", "054", "055", "061", "062", "063", "064",
}
MOBILE_PREFIXES = {"010", "011", "016", "017", "018", "019"}

ROLE_KEYWORDS = [
    ("management_office", ["관리사무소", "관리 사무소", "관리사무실", "관리소", "관리실", "관리단", "관리팀", "관리센터"]),
    ("security_office", ["경비실", "경비"]),
    ("real_estate", ["부동산", "공인중개", "중개사무소"]),
    ("building_owner", ["건물주", "집주인", "주인분", "주인"]),
]

# 띄어 쓴 서술격 조사("... 일이삼사 이에요")는 숫자 2가 아니라 번호의 끝으로 본다.
COPULA_CHUNKS = {"이에요", "이예요", "이요", "입니다", "이고요", "이구요", "이고", "이야", "이죠", "이다"}

MIN_DIGITS_FOR_CANDIDATE = 4


@dataclass
class PhoneInfo:
    digits: str
    formatted: str | None
    kind: str  # seoul/area/mobile/internet/rep/tollfree/local_no_area/invalid
    valid: bool
    needs_area_code: bool


@dataclass
class PhoneCandidate:
    raw_text: str
    digits: str
    formatted: str | None
    kind: str
    valid: bool
    needs_area_code: bool
    role: str = "unknown"
    span: tuple[int, int] = (0, 0)
    issues: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 형식 검증
# ---------------------------------------------------------------------------

def classify(digits: str) -> PhoneInfo:
    """숫자 문자열의 한국 전화번호 형식을 판정한다. 형식 검사는 오타 탐지 보조 수단일 뿐이다."""
    n = len(digits)

    def info(kind, groups, valid=True, needs_area=False):
        parts, i = [], 0
        for g in groups:
            parts.append(digits[i:i + g])
            i += g
        return PhoneInfo(digits, "-".join(parts), kind, valid, needs_area)

    if not digits.isdigit():
        return PhoneInfo(digits, None, "invalid", False, False)
    if digits.startswith("02"):
        if n == 9:
            return info("seoul", (2, 3, 4))
        if n == 10:
            return info("seoul", (2, 4, 4))
    elif digits[:3] in AREA_CODES:
        if n == 10:
            return info("area", (3, 3, 4))
        if n == 11:
            return info("area", (3, 4, 4))
    elif digits[:3] in MOBILE_PREFIXES:
        if n == 11:
            return info("mobile", (3, 4, 4))
        if n == 10 and digits[:3] != "010":
            return info("mobile", (3, 3, 4))
    elif digits.startswith("070"):
        if n == 11:
            return info("internet", (3, 4, 4))
        if n == 10:
            return info("internet", (3, 3, 4))
    elif digits.startswith("050"):
        if n == 11:
            return info("internet", (4, 3, 4))
        if n == 12:
            return info("internet", (4, 4, 4))
    elif digits.startswith("080"):
        if n == 10:
            return info("tollfree", (3, 3, 4))
    elif re.match(r"1[5-9]\d\d", digits) and n == 8:
        return info("rep", (4, 4))
    elif digits[0] != "0" and n in (7, 8):
        return info("local_no_area", (n - 4, 4), valid=False, needs_area=True)
    return PhoneInfo(digits, None, "invalid", False, False)


def is_area_code(digits: str) -> bool:
    return digits in AREA_CODES or digits in MOBILE_PREFIXES or digits == "070"


def combine_area_code(area: str, local: str) -> PhoneInfo:
    return classify(area + local)


# ---------------------------------------------------------------------------
# 낭독 문장 생성 (LLM이 숫자를 다시 만들지 않도록 코드가 담당)
# ---------------------------------------------------------------------------

def readback(digits: str, sep: str = ", ") -> str:
    """'0212345678' -> '공 이, 일 이 삼 사, 오 육 칠 팔'"""
    info = classify(digits)
    groups = info.formatted.split("-") if info.formatted else [digits]
    return sep.join(" ".join(SPOKEN[d] for d in g) for g in groups)


def readback_display(digits: str) -> str:
    return readback(digits, sep=" / ")


# ---------------------------------------------------------------------------
# 발화 -> 숫자
# ---------------------------------------------------------------------------

def _stem_digits(stem: str) -> str | None:
    """조사를 뗀 덩어리가 전부 숫자 표현이면 숫자열을, 아니면 None."""
    out: list[str] = []
    i = 0
    while i < len(stem):
        ch = stem[i]
        if "0" <= ch <= "9":
            out.append(ch)
            i += 1
            continue
        if ch in SEPARATOR_CHARS:
            i += 1
            continue
        for word, d in NATIVE_DIGITS:
            if stem.startswith(word, i):
                out.append(d)
                i += len(word)
                break
        else:
            if ch in SINO_DIGITS:
                out.append(SINO_DIGITS[ch])
                i += 1
            elif ch == "에" and out and i + 1 < len(stem):
                i += 1  # "일이삼사에오육칠팔"의 내부 구분자
            else:
                return None
    return "".join(out) if out else None


def chunk_variants(chunk: str, strip_particle: bool = False) -> list[str] | None:
    """숫자 덩어리 하나의 해석 후보 목록(선호 순). 숫자 덩어리가 아니면 None.

    "오육칠팔이요"처럼 끝의 '이'가 조사인지 숫자 2인지 모호하면 두 해석을 모두 돌려준다.
    strip_particle=True면 주격 조사 '이/가'를 뗀 해석도 추가한다 ("팔이 아니라").
    """
    core = chunk.strip(PUNCT)
    if not core:
        return None
    variants: list[str] = []
    for suffix in SUFFIXES + [""]:
        if suffix and not (core.endswith(suffix) and len(core) > len(suffix)):
            continue
        stem = core[: len(core) - len(suffix)] if suffix else core
        d = _stem_digits(stem)
        if d is None:
            continue
        variants.append(d)
        if suffix.startswith("이") and len(suffix) > 1:
            alt = _stem_digits(stem + "이")
            if alt:
                variants.append(alt)
        break
    if strip_particle and core[-1] in "이가" and len(core) > 1:
        d = _stem_digits(core[:-1])
        if d:
            variants.append(d)
    # 중복 제거, 순서 유지
    seen, uniq = set(), []
    for v in variants:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq or None


def _score(digits: str) -> int:
    info = classify(digits)
    if info.valid:
        return 2
    if info.needs_area_code:
        return 1
    return 0


def _best(run_variants: list[list[str]]) -> str:
    """덩어리별 해석 후보의 조합 중 가장 그럴듯한 번호를 고른다(동점이면 앞 순서 우선)."""
    combos = [""]
    for vs in run_variants:
        combos = [c + v for c in combos for v in vs][:16]
    best = combos[0]
    for c in combos[1:]:
        if _score(c) > _score(best):
            best = c
    return best


_CHUNK_RE = re.compile(r"[^\s,]+")


def extract_numbers(text: str) -> list[PhoneCandidate]:
    """발화에서 전화번호 후보를 모두 찾는다. 역할(관리사무소 등)은 주변 단어로 추정한다."""
    chunks = [(m.group(), m.start(), m.end()) for m in _CHUNK_RE.finditer(text)]
    runs: list[dict] = []
    cur: dict | None = None
    pending_filler = False
    for word, start, end in chunks:
        variants = None if word.strip(PUNCT) in COPULA_CHUNKS else chunk_variants(word)
        if variants is None:
            if cur and word.strip(PUNCT) in FILLER_CHUNKS:
                pending_filler = True
                continue
            if cur:
                runs.append(cur)
                cur = None
            pending_filler = False
            continue
        if cur:
            joined = _best(cur["variants"])
            extended = _best(cur["variants"] + [variants])
            # 이미 완결된 번호에 덧붙여 형식이 깨지면 새 번호로 본다.
            if classify(joined).valid and not classify(extended).valid:
                runs.append(cur)
                cur = None
        if cur is None:
            cur = {"variants": [variants], "start": start, "end": end}
        else:
            cur["variants"].append(variants)
            cur["end"] = end
        pending_filler = False
    if cur:
        runs.append(cur)

    candidates: list[PhoneCandidate] = []
    for run in runs:
        digits = _best(run["variants"])
        if len(digits) < MIN_DIGITS_FOR_CANDIDATE:
            continue
        info = classify(digits)
        issues = []
        if info.needs_area_code:
            issues.append("지역번호 없음")
        elif not info.valid:
            issues.append("형식 불일치")
        candidates.append(
            PhoneCandidate(
                raw_text=text[run["start"]: run["end"]],
                digits=digits,
                formatted=info.formatted,
                kind=info.kind,
                valid=info.valid,
                needs_area_code=info.needs_area_code,
                span=(run["start"], run["end"]),
                issues=issues,
            )
        )
    _assign_roles(text, candidates)
    return candidates


def detect_role(text: str) -> str | None:
    """문장 안에서 가장 마지막에 언급된 연락처 역할."""
    best, best_pos = None, -1
    for role, words in ROLE_KEYWORDS:
        for w in words:
            pos = text.rfind(w)
            if pos > best_pos:
                best, best_pos = role, pos
    return best


def _first_role(text: str) -> str | None:
    best, best_pos = None, len(text) + 1
    for role, words in ROLE_KEYWORDS:
        for w in words:
            pos = text.find(w)
            if 0 <= pos < best_pos:
                best, best_pos = role, pos
    return best


def _assign_roles(text: str, candidates: list[PhoneCandidate]) -> None:
    for i, c in enumerate(candidates):
        prev_end = candidates[i - 1].span[1] if i > 0 else 0
        next_start = candidates[i + 1].span[0] if i + 1 < len(candidates) else len(text)
        before = text[prev_end: c.span[0]]
        after = text[c.span[1]: next_start]
        c.role = detect_role(before) or _first_role(after) or "unknown"


def parse_area_code(text: str) -> str | None:
    """'공이요', '031이요' 같은 지역번호 단독 답변."""
    for m in _CHUNK_RE.finditer(text):
        for v in chunk_variants(m.group()) or []:
            if is_area_code(v):
                return v
    return None


# ---------------------------------------------------------------------------
# 정정 ("마지막은 팔이 아니라 구예요")
# ---------------------------------------------------------------------------

_CORRECTION_RE = re.compile(r"(아니라|아니고|말고|대신)")
_LAST_HINT = re.compile(r"(마지막|맨\s*끝|끝자리|끝에|뒷자리|뒤에)")
_FIRST_HINT = re.compile(r"(처음|맨\s*앞|앞자리|첫)")


def _segment_variants(segment: str, strip_particle: bool) -> list[str]:
    out: list[str] = []
    per_chunk = [chunk_variants(m.group(), strip_particle) for m in _CHUNK_RE.finditer(segment)]
    per_chunk = [v for v in per_chunk if v]
    if not per_chunk:
        return out
    combos = [""]
    for vs in per_chunk:
        combos = [c + v for c in combos for v in vs][:16]
    # 붙여 읽은 해석 + 마지막 덩어리 단독 해석
    out.extend(combos)
    out.extend(per_chunk[-1])
    seen, uniq = set(), []
    for v in out:
        if v and v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def apply_correction(text: str, current: str) -> str | None:
    """현재 후보 번호에 부분 정정을 적용한 새 숫자열. 해석할 수 없으면 None."""
    hint_last = bool(_LAST_HINT.search(text))
    hint_first = bool(_FIRST_HINT.search(text))
    m = _CORRECTION_RE.search(text)
    if m:
        left, right = text[: m.start()], text[m.end():]
        olds = _segment_variants(left, strip_particle=True)
        news = _segment_variants(right, strip_particle=False)
        if not news:
            return None
        for old in olds:
            if hint_last:
                pos = current.rfind(old)
            elif hint_first:
                pos = current.find(old)
            else:
                pos = current.find(old) if current.count(old) == 1 else -1
            if pos < 0:
                continue
            same_len = [n for n in news if len(n) == len(old)]
            new = same_len[0] if same_len else news[0]
            return current[:pos] + new + current[pos + len(old):]
        return None
    if hint_last or hint_first:
        news = _segment_variants(text, strip_particle=False)
        news = [n for n in news if len(n) <= 4]
        if not news:
            return None
        new = news[0]
        if hint_last:
            return current[: len(current) - len(new)] + new
        return new + current[len(new):]
    return None
