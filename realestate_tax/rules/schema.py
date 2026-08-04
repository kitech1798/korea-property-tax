"""룰셋 데이터 스키마.

세율·공제·비율·한도를 코드가 아니라 **버전 찍힌 데이터**로 두는 이유는
한국 세법이 자주 바뀌기 때문만이 아니다. 2026 개편안은 '26/'27/'28/'29에
서로 다른 값을 적용하는 4단계 시행이고, 그 위에 '현행법'과 '개편안'이라는
두 트랙이 겹친다. 조합이 8벌이다. 이걸 if문으로 짜면 6개월 뒤에 아무도 못 고친다.

모든 블록은 네 가지를 의무적으로 달고 다닌다.
  basis      — 근거 조문 (없으면 린터가 잡는다)
  source     — 1차 출처 (PDF 페이지 또는 법제처 API)
  certainty  — 법적 확정도
  effective  — 시행 기간

이 넷이 없는 규칙은 룰셋에 들어올 수 없다. "어디서 온 숫자인지 모르는 상수"가
코드에 박히는 것을 구조적으로 막기 위해서다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from fractions import Fraction
from typing import Any, Mapping, Sequence

from ..domain.certainty import (
    Certainty,
    DeterminationQuality,
    InputQuality,
    LegalStatus,
)
from ..engine.trace import LawBasis, RuleRef, SourceRef


class Track(StrEnum):
    """계산 트랙.

    개편안은 아직 국회를 통과하지 않았다. 그러므로 "얼마 나오나요"에 대한
    정답은 하나가 아니라 둘이다. 둘을 나란히 보여주지 않는 것 자체가 오류다.
    """

    CURRENT = "current"
    """현행법 그대로. 개편안이 부결되면 이쪽이 답이다."""

    REFORM = "reform"
    """2026.8.3 정부 세제개편안이 원안대로 통과된 경우."""


class RuleError(Exception):
    """룰셋 관련 오류의 뿌리."""


class MissingRule(RuleError):
    """조건에 맞는 블록이 하나도 없다. 조용히 기본값으로 때우지 않는다."""


class AmbiguousRule(RuleError):
    """같은 우선순위의 블록이 여러 개 맞았다. 룰셋 작성 오류다."""


# --------------------------------------------------------------------------
# 셀렉터
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Selector:
    """블록이 적용될 조건.

    표현 형태
      exact  : {taxpayer: individual}
      any-of : {taxpayer: [individual, corporation_progressive]}
      range  : {house_count: {min: 3}}  /  {taxable_base: {min: 600000000, max: 1200000000}}

    질의 컨텍스트에 없는 키를 셀렉터가 요구하면 **매칭 실패**다.
    "없으면 통과"로 두면 조용한 오적용이 생긴다.
    """

    constraints: Mapping[str, Any] = field(default_factory=dict)

    @property
    def specificity(self) -> int:
        return len(self.constraints)

    def matches(self, ctx: Mapping[str, Any]) -> bool:
        for key, want in self.constraints.items():
            if key not in ctx:
                return False
            if not _satisfies(ctx[key], want):
                return False
        return True

    def describe_ko(self) -> str:
        if not self.constraints:
            return "조건 없음"
        return ", ".join(f"{k}={v}" for k, v in self.constraints.items())


def _satisfies(actual: Any, want: Any) -> bool:
    if isinstance(want, Mapping):
        lo, hi = want.get("min"), want.get("max")
        if actual is None:
            return False
        if lo is not None and actual < lo:
            return False
        if hi is not None and actual > hi:
            return False
        return True
    if isinstance(want, (list, tuple, set)):
        return actual in want
    return actual == want


# --------------------------------------------------------------------------
# 세율표
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bracket:
    """누진세율 한 구간.

    한국 세법은 같은 누진세율을 세 가지 형식으로 쓴다. 셋 다 지원하되
    **해당 법조문이 쓴 형식 그대로** YAML에 적는 것을 규칙으로 한다.
    화면에 뜨는 대입식이 법조문과 글자 그대로 같아야 사용자가 검증할 수 있다.

      ① 누적형    구간마다 세율만. 아래에서부터 쌓아 올린다.
      ② 기초금액형 "570,000원 + 3억원 초과금액의 1,000분의 4" (지방세법 §111 주택)
      ③ 누진공제형 "과세표준 × 세율 − 누진공제액"
    """

    upto: int | None
    """구간 상한(이하). None이면 무한대."""
    rate: Fraction
    base_amount: int | None = None
    """② 기초금액형. 이 구간 하한까지의 누적세액. 법문에 적힌 숫자를 그대로 넣는다."""
    progressive_deduction: int = 0
    """③ 누진공제형."""

    def label_ko(self) -> str:
        return "초과" if self.upto is None else f"{self.upto:,}원 이하"


@dataclass(frozen=True, slots=True)
class RateTable:
    """누진세율표. 구간은 오름차순으로 정렬돼 있어야 한다(린터가 검사)."""

    brackets: tuple[Bracket, ...]

    def lower_bound(self, index: int) -> int:
        return 0 if index == 0 else (self.brackets[index - 1].upto or 0)

    def bracket_for(self, base: int) -> Bracket:
        return self.brackets[self.bracket_index_for(base)]

    def bracket_index_for(self, base: int) -> int:
        for i, b in enumerate(self.brackets):
            if b.upto is None or base <= b.upto:
                return i
        raise MissingRule(f"과세표준 {base:,}원에 해당하는 구간이 없다")

    def tax_for(self, base: int) -> tuple[int, Bracket, str]:
        """세액, 적용 구간, 대입식을 함께 돌려준다.

        어느 형식이든 결과는 같지만 대입식 문자열이 달라진다.
        사람이 법조문과 눈으로 대조할 수 있게 하는 것이 목적이다.
        """
        if base <= 0:
            return 0, self.brackets[0], "과세표준 0원 이하 → 0원"

        index = self.bracket_index_for(base)
        bracket = self.brackets[index]

        if bracket.base_amount is not None:
            lower = self.lower_bound(index)
            excess = base - lower
            tax = bracket.base_amount + int(excess * bracket.rate)
            sub = (
                f"{bracket.base_amount:,} + ({base:,} − {lower:,}) "
                f"× {_rate_str(bracket.rate)}"
            )
            return max(0, tax), bracket, sub

        if bracket.progressive_deduction:
            tax = int(base * bracket.rate) - bracket.progressive_deduction
            sub = (
                f"{base:,} × {_rate_str(bracket.rate)} "
                f"− {bracket.progressive_deduction:,}"
            )
            return max(0, tax), bracket, sub

        total = 0
        lower = 0
        parts: list[str] = []
        for b in self.brackets:
            if base <= lower:
                break
            top = base if b.upto is None else min(base, b.upto)
            span = top - lower
            if span > 0:
                total += int(span * b.rate)
                parts.append(f"{span:,} × {_rate_str(b.rate)}")
            if b.upto is None or base <= b.upto:
                break
            lower = b.upto
        return total, bracket, " + ".join(parts)


def _rate_str(rate: Fraction) -> str:
    """세율 표기. 0.14%처럼 소수 자릿수가 필요한 값이 있어 %g로 눌러 쓴다."""
    return f"{float(rate) * 100:g}%"


# --------------------------------------------------------------------------
# 블록과 규칙
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleBlock:
    """규칙 한 덩어리. 특정 기간·트랙·조건에서만 유효한 값."""

    id: str
    rule_id: str
    tracks: frozenset[Track]
    effective_from: date | None
    effective_to: date | None
    selector: Selector
    basis: LawBasis | None
    source: SourceRef | None
    certainty: Certainty
    value: Any = None
    """스칼라 값(공제금액·비율·한도 등)."""
    table: RateTable | None = None
    """누진세율표."""
    payload: Mapping[str, Any] = field(default_factory=dict)
    """구조화된 값(예: 세액공제 구간표). 규칙 종류마다 해석이 다르다."""
    note: str = ""

    def applies_on(self, on: date) -> bool:
        if self.effective_from is not None and on < self.effective_from:
            return False
        if self.effective_to is not None and on > self.effective_to:
            return False
        return True

    def to_ref(self, track: Track) -> RuleRef:
        return RuleRef(
            rule_id=self.rule_id,
            block_id=self.id,
            track=str(track),
            effective_from=self.effective_from,
            effective_to=self.effective_to,
            basis=self.basis,
            source=self.source,
            certainty=self.certainty,
            note=self.note,
        )

    def as_fraction(self) -> Fraction:
        return _to_fraction(self.value)

    def as_int(self) -> int:
        return int(self.value)


@dataclass(frozen=True, slots=True)
class Rule:
    """같은 rule_id를 가진 블록들의 묶음."""

    rule_id: str
    unit: str
    description: str
    blocks: tuple[RuleBlock, ...]

    def candidates(
        self, on: date, track: Track, ctx: Mapping[str, Any]
    ) -> tuple[RuleBlock, ...]:
        return tuple(
            b
            for b in self.blocks
            if track in b.tracks and b.applies_on(on) and b.selector.matches(ctx)
        )


# --------------------------------------------------------------------------
# 파싱
# --------------------------------------------------------------------------

_LEGAL_BY_NAME = {
    "enacted": LegalStatus.ENACTED,
    "promulgated": LegalStatus.PROMULGATED,
    "bill_pending": LegalStatus.BILL_PENDING,
    "decree_pending": LegalStatus.DECREE_PENDING,
    "assumed": LegalStatus.ASSUMED,
}


def _to_fraction(raw: Any) -> Fraction:
    """비율 파싱. YAML에서 '0.013'처럼 문자열로 적게 강제한다.

    float로 읽으면 0.7이 0.6999999...가 되어 과세표준 경계에서 세액이 흔들린다.
    문자열 → Fraction 경로만 허용해 이 부류의 오차를 원천 차단한다.
    """
    if isinstance(raw, Fraction):
        return raw
    if isinstance(raw, int):
        return Fraction(raw)
    if isinstance(raw, str):
        s = raw.strip()
        if s.endswith("%"):
            return Fraction(s[:-1].strip()) / 100
        if "/" in s:
            num, den = s.split("/", 1)
            return Fraction(int(num.strip()), int(den.strip()))
        return Fraction(s)
    raise RuleError(
        f"비율은 문자열로 적어야 한다(부동소수점 오차 방지). 받은 값: {raw!r}"
    )


def _parse_date(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    return date.fromisoformat(str(raw))


def _parse_basis(raw: Mapping[str, Any] | None) -> LawBasis | None:
    if not raw:
        return None
    return LawBasis(
        law=str(raw["law"]),
        article=str(raw.get("article", "")),
        clause=str(raw.get("clause", "")),
        item=str(raw.get("item", "")),
        mst=str(raw.get("mst", "")),
    )


def _parse_source(raw: Mapping[str, Any] | None) -> SourceRef | None:
    if not raw:
        return None
    return SourceRef(
        kind=raw.get("kind", "manual"),
        file=str(raw.get("file", "")),
        page=raw.get("page"),
        url=str(raw.get("url", "")),
        fetched_at=_parse_date(raw.get("fetched_at")),
        note=str(raw.get("note", "")),
    )


def _parse_certainty(raw: Any) -> Certainty:
    """룰셋은 A축(법적 확정도)만 정한다.
    B축(입력)·C축(판정)은 계산 시점에 합성되므로 여기서는 최선값으로 둔다."""
    if raw is None:
        legal = LegalStatus.ENACTED
    elif isinstance(raw, str):
        legal = _LEGAL_BY_NAME[raw]
    else:
        legal = _LEGAL_BY_NAME[raw.get("legal", "enacted")]
    return Certainty(
        legal=legal,
        input=InputQuality.OFFICIAL_NOTICE,
        determination=DeterminationQuality.DECIDED,
    )


def _parse_table(raw: Sequence[Mapping[str, Any]] | None) -> RateTable | None:
    if not raw:
        return None
    brackets = tuple(
        Bracket(
            upto=b.get("upto"),
            rate=_to_fraction(b["rate"]),
            base_amount=(
                None if b.get("base_amount") is None else int(b["base_amount"])
            ),
            progressive_deduction=int(b.get("progressive_deduction", 0)),
        )
        for b in raw
    )
    return RateTable(brackets)


def parse_rule(raw: Mapping[str, Any]) -> Rule:
    rule_id = str(raw["rule_id"])
    blocks: list[RuleBlock] = []
    for i, b in enumerate(raw.get("blocks", [])):
        tracks_raw = b.get("tracks") or ["current", "reform"]
        blocks.append(
            RuleBlock(
                id=str(b.get("id") or f"{rule_id}#{i}"),
                rule_id=rule_id,
                tracks=frozenset(Track(t) for t in tracks_raw),
                effective_from=_parse_date(b.get("effective_from")),
                effective_to=_parse_date(b.get("effective_to")),
                selector=Selector(dict(b.get("selector") or {})),
                basis=_parse_basis(b.get("basis")),
                source=_parse_source(b.get("source")),
                certainty=_parse_certainty(b.get("certainty")),
                value=b.get("value"),
                table=_parse_table(b.get("table")),
                payload=dict(b.get("payload") or {}),
                note=str(b.get("note", "")),
            )
        )
    return Rule(
        rule_id=rule_id,
        unit=str(raw.get("unit", "KRW")),
        description=str(raw.get("description", "")),
        blocks=tuple(blocks),
    )
