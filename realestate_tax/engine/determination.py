"""판정 엔진 — 사실에서 법적 지위를 도출한다.

이 모듈이 존재하는 이유가 이 프로젝트의 핵심 주장이다.

    주택 수와 1세대1주택 여부는 **입력이 아니라 판정 결과**다.

시중 계산기는 "주택 수 = 사용자가 목록에 담은 개수"로 가정하고, 1세대1주택
여부는 체크박스로 사용자에게 물었다. 그런데 그 판정에는 상속주택 제외,
합산배제, 세대 범위, 같은 세대 내 공동소유 취급 같은 규칙이 줄줄이 걸려 있다.
사용자는 그걸 판정할 줄 몰라서 계산기에 온 것이다 — 그래서 순환 실패가 된다.

여기서는 사실만 받아 엔진이 센다. 셀 수 없으면 셀 수 없다고 말한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..domain.certainty import Certainty, DeterminationQuality
from ..domain.models import (
    AcquisitionCause,
    PersonId,
    Property,
    PropertyId,
    TaxCase,
)
from .trace import Alternative, SubjectRef, SubjectType, TraceNode, Value, node

# 지방세법 시행령 §110의2①8 — 상속개시일부터 5년이 지나지 않은 상속주택은
# 1세대1주택 판정 시 주택 수에서 제외한다.
INHERITANCE_EXCLUSION_YEARS = 5


@dataclass(frozen=True, slots=True)
class ExcludedHouse:
    """주택 수에서 빠진 주택과 그 근거."""

    property_id: PropertyId
    reason_ko: str
    basis_ko: str


@dataclass(frozen=True, slots=True)
class HouseCount:
    """세대 기준 주택 수 판정 결과."""

    counted: tuple[PropertyId, ...]
    excluded: tuple[ExcludedHouse, ...]
    certainty: Certainty
    unresolved_ko: tuple[str, ...] = ()
    """엔진이 판정하지 못해 사용자 확인이 필요한 항목."""

    @property
    def count(self) -> int:
        return len(self.counted)

    @property
    def is_one_house(self) -> bool:
        return self.count == 1


def household_house_count(
    case: TaxCase,
    person_id: PersonId,
    *,
    on: date | None = None,
) -> HouseCount:
    """1세대가 소유한 주택 수를 센다(지방세법 시행령 §110의2).

    세 가지 규칙이 결과를 가른다.

    ① 세대 범위 — 배우자와 19세 미만 미혼 자녀는 주민등록이 달라도 같은 세대로 본다.
    ② 같은 세대 내 공동소유 1주택은 **1개**로 본다(§110의2④ 단서).
       이 한 줄 때문에 '부부공동명의 1주택'이 1세대1주택 특례를 받는다.
    ③ 세대원이 각자 다른 집을 1채씩 가지면 세대 주택수는 2가 된다.
       ②와 ③의 차이가 시중 계산기가 표현조차 못 하던 지점이다.
    """
    on = on or case.assessment_date
    member_ids = set(case.household_member_ids(person_id))

    owned: dict[PropertyId, Property] = {}
    for ownership in case.ownerships:
        if ownership.person_id not in member_ids:
            continue
        prop = case.find_property(ownership.property_id)
        if prop.is_house:
            # dict에 담으므로 같은 집을 세대원 여럿이 나눠 가져도 1개로 센다 → 규칙 ②
            owned[prop.id] = prop

    counted: list[PropertyId] = []
    excluded: list[ExcludedHouse] = []
    unresolved: list[str] = []

    for pid, prop in owned.items():
        reason = _exclusion_reason(case, prop, on)
        if reason is None:
            counted.append(pid)
        else:
            excluded.append(reason)

    # 엔진이 사실을 못 받아 판정을 못 한 항목은 조용히 넘기지 않는다.
    for pid, prop in owned.items():
        if prop.kind.name == "OFFICETEL_RESIDENTIAL":
            unresolved.append(
                f"{prop.display_name or pid}: 오피스텔의 주택 수 산입 여부는 "
                "실제 주거용 사용과 재산세 과세대장 등재 현황에 따라 갈린다"
            )

    certainty = Certainty()
    if unresolved:
        certainty = certainty & Certainty(
            determination=DeterminationQuality.UNDECIDABLE
        )

    return HouseCount(tuple(counted), tuple(excluded), certainty, tuple(unresolved))


def _exclusion_reason(
    case: TaxCase, prop: Property, on: date
) -> ExcludedHouse | None:
    """주택 수에서 제외되는 사유. 해당 없으면 None.

    지방세법 시행령 §110의2①의 12개 호 중, 도메인 모델이 사실을 받고 있는 것만
    판정한다. 나머지는 판정하지 않고 넘긴다 — 없는 사실로 있는 척하지 않는다.
    """
    if prop.is_company_housing:
        return ExcludedHouse(
            prop.id, "종업원에게 제공하는 사용자 소유 주택", "지방세법 시행령 §110의2①1"
        )
    if prop.is_unsold_new:
        return ExcludedHouse(
            prop.id, "사업자가 건축하여 소유한 미분양 주택", "지방세법 시행령 §110의2①3"
        )

    for ownership in case.owners_of(prop.id):
        if ownership.cause is AcquisitionCause.INHERITANCE and ownership.inherited:
            elapsed = (on - ownership.inherited.inheritance_date).days
            if elapsed < INHERITANCE_EXCLUSION_YEARS * 365:
                years = elapsed / 365
                return ExcludedHouse(
                    prop.id,
                    f"상속개시 후 {years:.1f}년으로 {INHERITANCE_EXCLUSION_YEARS}년 미경과",
                    "지방세법 시행령 §110의2①8",
                )
    return None


def one_house_determination_trace(
    case: TaxCase,
    person_id: PersonId,
    count: HouseCount,
) -> TraceNode:
    """판정 과정을 감사 추적으로 남긴다.

    "1세대1주택입니다"만 보여주면 시중 계산기의 체크박스와 다를 게 없다.
    누구를 세대원으로 봤고, 어떤 집을 왜 뺐는지가 드러나야 한다.
    """
    members = case.household_member_ids(person_id)
    member_names = ", ".join(
        case.find_person(m).name or str(m) for m in members
    )
    labels = [
        case.find_property(pid).display_name or str(pid) for pid in count.counted
    ]

    alternatives = tuple(
        Alternative(
            key=f"excluded:{ex.property_id}",
            label_ko=f"주택 수 제외: {case.find_property(ex.property_id).display_name or ex.property_id}",
            reason_ko=f"{ex.reason_ko} ({ex.basis_ko})",
        )
        for ex in count.excluded
    )
    if count.count != 1:
        alternatives += (
            Alternative(
                key="one_house_special",
                label_ko="1세대 1주택 세율 특례",
                reason_ko=f"세대 주택 수가 {count.count}채로 1채가 아니다",
                actionable=count.count > 1,
            ),
        )

    return node(
        "pt.03.house_count",
        "세대 주택 수 판정",
        Value(
            count.count,
            unit="count",
            certainty=count.certainty,
            label=f"{count.count}주택",
        ),
        subject=SubjectRef(SubjectType.HOUSEHOLD, str(person_id), member_names),
        formula="1세대가 소유한 주택 수 (같은 세대 내 공동소유 1주택은 1개로 계산)",
        substitution=f"세대원 [{member_names}] 소유 주택: {', '.join(labels) or '없음'}",
        rules=(),
        alternatives_not_taken=alternatives,
        note_ko=(
            "판단 필요: " + " / ".join(count.unresolved_ko)
            if count.unresolved_ko
            else "지방세법 시행령 §110의2에 따른 판정"
        ),
    )
