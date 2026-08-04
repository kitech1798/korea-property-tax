"""확실성(certainty) 3축.

경쟁 계산기가 무너진 자리는 대부분 "모르는 것을 아는 척한 자리"였다.
그래서 이 엔진은 모든 값에 확실성을 붙여 다니게 하고, 계산이 진행될수록
**가장 약한 고리로 자동 강등**시킨다(componentwise min = 모노이드).

세 축은 서로 독립이다. 하나로 뭉개면 "법은 확정인데 공시가격이 추정"인 경우와
"공시가격은 고지서인데 법이 국회 미통과"인 경우를 구분할 수 없다.

  A축 legal      — 근거 규칙이 법적으로 얼마나 확정됐나
  B축 input      — 입력값을 어디서 얻었나
  C축 determination — 법적 판정(주택수·1세대1주택 등)을 확정했나

값이 클수록 확실하다. `min()`으로 합성하기 위해 IntEnum을 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class LegalStatus(IntEnum):
    """A축 — 근거 규칙의 법적 확정도."""

    ENACTED = 5
    """현행법으로 시행 중. 예: 지방세법 재산세 조항."""

    PROMULGATED = 4
    """공포되어 시행일이 확정됐으나 아직 시행 전."""

    BILL_PENDING = 3
    """정부 제출안. 국회 미통과 — 바뀔 수 있다. 2026.8.3 세제개편안 대부분이 여기."""

    DECREE_PENDING = 2
    """법률은 있으나 세부기준을 시행령에 위임했고 그 시행령이 아직 없다.
    예: 기본공제 신공식의 '거주주택가액' 산정 방법."""

    ASSUMED = 1
    """근거 문서가 없어 우리가 세운 가정. 반드시 사용자에게 노출한다."""


class InputQuality(IntEnum):
    """B축 — 입력값의 출처."""

    OFFICIAL_NOTICE = 5
    """세금 고지서·공적 서류에 인쇄된 숫자를 그대로 옮김. 가장 정확하다."""

    OFFICIAL_LOOKUP = 4
    """공적 조회 API 응답(V-World 등)."""

    USER_INPUT = 3
    """사용자가 직접 입력. 1차 버전의 기본 경로."""

    ESTIMATED = 2
    """추정값(예: 미래 연도 공시가격 상승률 시나리오). 반드시 라벨링."""

    UNKNOWN = 1
    """미상. 계산을 중단하지 않고 UnknownReason과 함께 전파시킨다."""


class DeterminationQuality(IntEnum):
    """C축 — 법적 판정의 확실도."""

    DECIDED = 3
    """규칙으로 확정. trace에 조문이 붙는다."""

    ASSUMED = 2
    """가정 하에 판정. 반대 가정 결과를 함께 산출해야 한다."""

    UNDECIDABLE = 1
    """해석 영역이라 엔진이 판정하지 않는다(예: '부득이한 사유' 해당성).
    양쪽 시나리오를 모두 제시하고 '세무서 확인 필요'를 붙인다."""


@dataclass(frozen=True, slots=True, order=False)
class Certainty:
    """3축 확실성. `&` 연산으로 합성하면 축별 최솟값이 된다."""

    legal: LegalStatus = LegalStatus.ENACTED
    input: InputQuality = InputQuality.USER_INPUT
    determination: DeterminationQuality = DeterminationQuality.DECIDED

    def __and__(self, other: "Certainty") -> "Certainty":
        """가장 약한 고리로 강등. 결합법칙·교환법칙이 성립하므로
        여러 입력을 어떤 순서로 합쳐도 같은 결과가 나온다."""
        return Certainty(
            legal=min(self.legal, other.legal),
            input=min(self.input, other.input),
            determination=min(self.determination, other.determination),
        )

    @classmethod
    def combine(cls, items: "Certainty | None", *rest: "Certainty | None") -> "Certainty":
        """None을 섞어 넘겨도 되는 가변 인자 버전."""
        result = cls.BEST
        for c in (items, *rest):
            if c is not None:
                result = result & c
        return result

    @property
    def is_fully_certain(self) -> bool:
        return (
            self.legal is LegalStatus.ENACTED
            and self.input >= InputQuality.OFFICIAL_LOOKUP
            and self.determination is DeterminationQuality.DECIDED
        )

    def labels_ko(self) -> tuple[str, ...]:
        """UI 배지용. 확정적인 축은 생략하고 '주의가 필요한 축'만 돌려준다."""
        out: list[str] = []
        if self.legal is not LegalStatus.ENACTED:
            out.append(_LEGAL_KO[self.legal])
        if self.input is not InputQuality.OFFICIAL_NOTICE:
            out.append(_INPUT_KO[self.input])
        if self.determination is not DeterminationQuality.DECIDED:
            out.append(_DETERMINATION_KO[self.determination])
        return tuple(out)


Certainty.BEST = Certainty(  # type: ignore[attr-defined]
    LegalStatus.ENACTED, InputQuality.OFFICIAL_NOTICE, DeterminationQuality.DECIDED
)
"""합성의 항등원. `Certainty.combine()`의 시작값."""


_LEGAL_KO: dict[LegalStatus, str] = {
    LegalStatus.ENACTED: "현행법",
    LegalStatus.PROMULGATED: "시행예정",
    LegalStatus.BILL_PENDING: "국회 미통과",
    LegalStatus.DECREE_PENDING: "시행령 미정",
    LegalStatus.ASSUMED: "가정",
}

_INPUT_KO: dict[InputQuality, str] = {
    InputQuality.OFFICIAL_NOTICE: "고지서 확인",
    InputQuality.OFFICIAL_LOOKUP: "공적 조회",
    InputQuality.USER_INPUT: "사용자 입력",
    InputQuality.ESTIMATED: "추정치",
    InputQuality.UNKNOWN: "미상",
}

_DETERMINATION_KO: dict[DeterminationQuality, str] = {
    DeterminationQuality.DECIDED: "확정",
    DeterminationQuality.ASSUMED: "가정 판정",
    DeterminationQuality.UNDECIDABLE: "판단 필요",
}
