"""상담 지식 — 사전 생성, 런타임 규칙 매칭.

왜 런타임에 LLM을 부르지 않는가
    ① 비용 — 공개 서비스에서 상담 1건마다 과금되면 지속이 안 된다.
    ② 재현성 — 같은 상황에 같은 답이 나와야 한다. 세금 조언에서 이건 타협 불가다.
    ③ 감사 가능성 — LLM이 실시간으로 지어낸 문장은 근거를 검증할 수 없다.

대신 **개발 시점에** 여러 에이전트가 다각도로 검토하고 반박을 거쳐 만든 지식을
데이터로 굳혀 두고, 런타임에는 엔진 판정 결과로 매칭만 한다.
결과적으로 "전문가 패널이 미리 써둔 상담 노트를 조건에 맞춰 꺼내는" 구조다.

절대 규칙
    · `fact_ko`는 법률 팩트. `basis`(조문)가 비면 로딩 자체가 실패한다.
    · **숫자를 지식에 박지 않는다.** 세액은 엔진이 계산해 자리표시자에 넣는다.
      법정 고정 수치(기본공제 14억, 공제율 80%)는 예외로 허용한다.
    · `caveats_ko`가 비면 로딩이 실패한다. 부작용 없는 조언은 조언이 아니라 함정이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from ..domain.certainty import Certainty, LegalStatus


class AdvisorySeverity(StrEnum):
    OPPORTUNITY = "opportunity"
    """행동하면 세액이 줄어드는 것."""
    CAUTION = "caution"
    """놓치면 손해를 보거나 요건이 깨지는 것."""
    FACT = "fact"
    """알아야 할 사실. 행동을 요구하지 않는다."""


class AdvisoryError(ValueError):
    pass


# 런타임 매칭에 쓸 수 있는 조건 키. 여기 없는 키가 오면 로딩이 실패한다.
# 오타 하나로 조건이 조용히 무시되어 아무에게나 조언이 뜨는 것을 막는다.
ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "house_count",
        "is_one_house",
        "joint_spouse_eligible",
        "resides",
        "age_min",
        "holding_years_min",
        "residence_years_min",
        "has_inherited",
        "has_rental",
        "has_temporary_two",
        "in_regulated_zone",
        "zone_unknown",
        "track",
        "year_min",
        "year_max",
        "price_total_min",
        "transfer_planned",
    }
)

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


@dataclass(frozen=True, slots=True)
class Condition:
    """언제 이 조언을 보여줄 것인가.

    `_min`/`_max` 접미사는 범위 비교, 그 외는 동등 비교로 푼다.
    조건이 비어 있으면(=아무에게나 뜨는 조언) 로딩이 실패한다.
    """

    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.raw:
            raise AdvisoryError("when 조건이 비었다. 아무에게나 뜨는 조언은 신뢰를 깎는다.")
        unknown = set(self.raw) - ALLOWED_KEYS
        if unknown:
            raise AdvisoryError(
                f"허용되지 않은 조건 키: {sorted(unknown)}\n"
                f"  가능한 키: {sorted(ALLOWED_KEYS)}"
            )

    @property
    def specificity(self) -> int:
        return len(self.raw)

    def matches(self, ctx: Mapping[str, Any]) -> bool:
        for key, want in self.raw.items():
            base = key.rsplit("_", 1)[0] if key.endswith(("_min", "_max")) else key
            actual = ctx.get(base)
            if actual is None:
                return False
            if key.endswith("_min"):
                if actual < want:
                    return False
            elif key.endswith("_max"):
                if actual > want:
                    return False
            elif isinstance(want, (list, tuple)):
                if actual not in want:
                    return False
            elif actual != want:
                return False
        return True

    def describe_ko(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in sorted(self.raw.items()))


@dataclass(frozen=True, slots=True)
class Advisory:
    """상담 지식 한 항목."""

    id: str
    title_ko: str
    when: Condition
    fact_ko: str
    basis: tuple[str, ...]
    advice_ko: str
    caveats_ko: tuple[str, ...]
    severity: AdvisorySeverity = AdvisorySeverity.FACT
    uncertainty_ko: str = ""
    reviewed_by: tuple[str, ...] = ()
    """검토한 에이전트 렌즈. 어떤 관점을 거쳤는지 남긴다."""

    def __post_init__(self) -> None:
        if not self.basis:
            raise AdvisoryError(f"{self.id}: 근거 조문(basis)이 없다. 법률 팩트가 아니다.")
        if not self.caveats_ko:
            raise AdvisoryError(
                f"{self.id}: caveats_ko가 비었다. 부작용 없는 조언은 함정이다."
            )
        if not self.advice_ko.strip():
            raise AdvisoryError(f"{self.id}: advice_ko가 비었다.")

    @property
    def placeholders(self) -> frozenset[str]:
        """런타임에 엔진 값으로 채워야 하는 자리표시자."""
        return frozenset(
            _PLACEHOLDER.findall(self.fact_ko) + _PLACEHOLDER.findall(self.advice_ko)
        )

    @property
    def certainty(self) -> Certainty:
        """개편안 기반이면 '국회 미통과'가 따라붙는다."""
        text = f"{self.uncertainty_ko} {self.fact_ko}"
        if "국회" in text or "개정안" in text or "개편안" in text:
            return Certainty(legal=LegalStatus.BILL_PENDING)
        return Certainty()

    def render(self, values: Mapping[str, str]) -> "RenderedAdvisory":
        """자리표시자를 엔진이 계산한 값으로 채운다.

        채우지 못한 자리표시자가 남으면 그 항목은 **표시하지 않는다**.
        "{{절감액}} 절감됩니다" 같은 문장이 그대로 화면에 나가는 것보다 낫다.
        """
        missing = self.placeholders - set(values)
        return RenderedAdvisory(
            advisory=self,
            fact_ko=_fill(self.fact_ko, values),
            advice_ko=_fill(self.advice_ko, values),
            missing=tuple(sorted(missing)),
        )


def _fill(text: str, values: Mapping[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)


@dataclass(frozen=True, slots=True)
class RenderedAdvisory:
    advisory: Advisory
    fact_ko: str
    advice_ko: str
    missing: tuple[str, ...] = ()

    @property
    def displayable(self) -> bool:
        return not self.missing

    @property
    def id(self) -> str:
        return self.advisory.id

    @property
    def title_ko(self) -> str:
        return self.advisory.title_ko


def parse_advisory(raw: Mapping[str, Any]) -> Advisory:
    try:
        return Advisory(
            id=str(raw["id"]),
            title_ko=str(raw["title_ko"]),
            when=Condition(dict(raw.get("when") or {})),
            fact_ko=str(raw["fact_ko"]).strip(),
            basis=tuple(str(b) for b in (raw.get("basis") or [])),
            advice_ko=str(raw["advice_ko"]).strip(),
            caveats_ko=tuple(str(c) for c in (raw.get("caveats_ko") or [])),
            severity=AdvisorySeverity(raw.get("severity", "fact")),
            uncertainty_ko=str(raw.get("uncertainty_ko", "")).strip(),
            reviewed_by=tuple(str(r) for r in (raw.get("reviewed_by") or [])),
        )
    except KeyError as exc:
        raise AdvisoryError(f"필수 항목 누락: {exc} — {raw.get('id', '(id 없음)')}") from None


def select(
    advisories: Sequence[Advisory], ctx: Mapping[str, Any], *, limit: int = 8
) -> tuple[Advisory, ...]:
    """조건에 맞는 항목을 구체적인 순으로 고른다.

    조건이 많이 걸린(=상황에 딱 맞는) 항목을 먼저 보여준다.
    limit을 두는 이유: 스무 개를 쏟아내면 사용자는 하나도 안 읽는다.
    """
    matched = [a for a in advisories if a.when.matches(ctx)]
    matched.sort(key=lambda a: (-a.when.specificity, a.id))
    return tuple(matched[:limit])
