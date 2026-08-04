"""공시가격 입력 — 정규화와 경계 경고.

자동조회가 되든 안 되든 이 계층은 필요하다. 건축물대장 주택가격이 공동주택공시가격과
언제나 같다는 보장이 아직 검증되지 않았고, 사용자가 값을 고칠 길은 항상 열려 있어야 한다.

이 모듈의 존재 이유는 하나다.

    **공시가격이 조금만 틀려도 세액이 통째로 뒤집히는 구간이 있다.**

기본공제 경계(현행 9억/12억, 개편안 9억/14억)에서는 공시가격 1원 차이로
과세 대상 자체가 갈린다. 1세대1주택 재산세 세율 특례의 9억 경계도 마찬가지로
세액이 절벽처럼 튄다(실측: 공시 9억 → 9억+1원에서 재산세 본세 202,500원 점프).

그래서 값을 받기만 하는 게 아니라, **위험 구간에 들어오면 사용자를 멈춰 세운다.**
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Iterable

from ..domain.models import Won
from ..rules.resolver import RuleSet
from ..rules.schema import Track

# 사람이 쓰는 표기를 전부 받는다: "3억2400만", "3억 2,400만원", "324,000,000", "324000000"
_UNIT_PATTERN = re.compile(r"(\d[\d,\.]*)\s*(억|만|천)?")
_UNIT_VALUE = {"억": 100_000_000, "만": 10_000, "천": 1_000, None: 1}

BOUNDARY_BAND = 0.10
"""경계에서 ±10% 안에 들어오면 경고한다. 공시가격을 잘못 본 정도의 오차 범위다."""

SANE_MIN = 10_000_000
"""1천만원. 이보다 낮으면 단위를 잘못 넣었을 가능성이 크다."""

SANE_MAX = 50_000_000_000
"""500억. 이보다 높으면 자릿수 착오를 의심한다."""


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    BLOCK = "block"
    """계산을 진행하기 전에 사용자 확인이 필요하다."""


@dataclass(frozen=True, slots=True)
class Notice:
    severity: Severity
    message_ko: str
    hint_ko: str = ""


@dataclass(frozen=True, slots=True)
class ParsedPrice:
    value: Won | None
    raw: str
    notices: tuple[Notice, ...] = ()

    @property
    def ok(self) -> bool:
        return self.value is not None and not any(
            n.severity is Severity.BLOCK for n in self.notices
        )


class PriceParseError(ValueError):
    pass


def parse_won(text: str) -> Won:
    """한국어 금액 표기를 원 단위 정수로.

    "3억 2,400만" 같은 복합 표기를 받는다. 사용자가 알리미 화면에서 본 그대로
    붙여넣을 수 있어야 입력 마찰이 줄고, 그래야 오타가 준다.
    """
    cleaned = str(text).replace("원", "").strip()
    if not cleaned:
        raise PriceParseError("금액이 비어 있다")

    total = 0
    matched_any = False
    for number, unit in _UNIT_PATTERN.findall(cleaned):
        digits = number.replace(",", "")
        if not digits or digits == ".":
            continue
        matched_any = True
        total += int(float(digits) * _UNIT_VALUE[unit or None])

    if not matched_any:
        raise PriceParseError(f"금액으로 읽을 수 없다: {text!r}")
    return total


def deduction_boundaries(
    ruleset: RuleSet, *, on: date, tracks: Iterable[Track] = (Track.CURRENT, Track.REFORM)
) -> dict[int, str]:
    """세액이 급변하는 공시가격 경계를 룰셋에서 뽑는다.

    경계값을 코드에 박지 않는 이유: 개편안이 12억 → 14억/9억으로 바꿨고, 국회에서
    또 바뀔 수 있다. 룰셋을 고치면 경고도 따라 움직여야 한다.
    """
    out: dict[int, str] = {}

    for track in tracks:
        for one_house in (True, False):
            for resides in (True, False, None):
                ctx: dict[str, object] = {"taxpayer": "individual", "one_house": one_house}
                if resides is not None:
                    ctx["resides"] = resides
                res = ruleset.resolve_or_none(
                    "jongbuse.house.basic_deduction", on=on, track=track, **ctx
                )
                if res is None or res.block.value is None:
                    continue
                amount = int(res.block.value)
                if amount <= 0:
                    continue
                label = "1세대1주택 기본공제" if one_house else "기본공제"
                if resides is True:
                    label = "거주용 1주택 기본공제"
                elif resides is False and one_house:
                    label = "비거주 1주택 기본공제"
                out.setdefault(amount, f"종합부동산세 {label}")

    cap = ruleset.resolve_or_none(
        "property_tax.house.one_house_rate_price_cap", on=on, track=Track.CURRENT
    )
    if cap is not None and cap.block.value:
        out.setdefault(
            int(cap.block.value), "재산세 1세대1주택 세율 특례 적용 상한"
        )
    return out


def check(
    value: Won,
    ruleset: RuleSet,
    *,
    on: date | None = None,
) -> tuple[Notice, ...]:
    """입력된 공시가격에 대한 경고 목록."""
    on = on or date(2026, 6, 1)
    notices: list[Notice] = []

    if value < SANE_MIN:
        notices.append(
            Notice(
                Severity.BLOCK,
                f"{value:,}원은 공시가격으로 보기에 너무 작습니다.",
                "만원 단위로 입력하지 않으셨는지 확인해주세요. 예: 3억 2,400만",
            )
        )
    elif value > SANE_MAX:
        notices.append(
            Notice(
                Severity.BLOCK,
                f"{value:,}원은 공시가격으로 보기에 너무 큽니다.",
                "자릿수를 확인해주세요.",
            )
        )

    for boundary, label in sorted(deduction_boundaries(ruleset, on=on).items()):
        low, high = boundary * (1 - BOUNDARY_BAND), boundary * (1 + BOUNDARY_BAND)
        if low <= value <= high:
            notices.append(
                Notice(
                    Severity.WARN,
                    f"입력하신 금액이 {label}({boundary / 100_000_000:.0f}억원) 경계 근처입니다.",
                    "이 구간에서는 공시가격이 조금만 달라도 세액이 크게 바뀝니다. "
                    "부동산공시가격 알리미에서 값을 한 번 더 확인해주세요.",
                )
            )

    return tuple(notices)


def intake(
    text: str,
    ruleset: RuleSet,
    *,
    on: date | None = None,
) -> ParsedPrice:
    """사용자 입력 한 건을 받아 파싱 + 검증까지."""
    try:
        value = parse_won(text)
    except PriceParseError as exc:
        return ParsedPrice(
            None,
            text,
            (
                Notice(
                    Severity.BLOCK,
                    str(exc),
                    "숫자로 입력해주세요. 예: 324000000 또는 3억 2,400만",
                ),
            ),
        )
    return ParsedPrice(value, text, check(value, ruleset, on=on))


# --------------------------------------------------------------------------
# 안내
# --------------------------------------------------------------------------

LOOKUP_GUIDE_KO = """공시가격은 부동산공시가격 알리미에서 호 단위로 확인할 수 있습니다(로그인 불필요).

  1. https://www.realtyprice.kr/notice/town/nfSiteLink.htm 접속
  2. [공동주택가격 열람] → [지번 검색] 탭
  3. 시/도 → 시군구 → 읍면 → 지번 입력
  4. 단지명 → 동 → 호 선택
  5. 화면의 '공동주택가격'을 그대로 입력해주세요

※ 검색 결과 화면은 주소창 URL이 바뀌지 않아 바로가기 링크를 만들 수 없습니다.
   위 경로를 따라가셔야 합니다."""


def guidance(parsed: ParsedPrice) -> str:
    """경고가 있으면 안내문을 붙인다. 없으면 빈 문자열."""
    if parsed.ok and not parsed.notices:
        return ""
    return LOOKUP_GUIDE_KO
