"""종합부동산세 주택 수 특례와 합산배제 판정.

★ 두 가지를 구분한다. 섞으면 세액이 통째로 틀린다.

    주택 수 제외 (종부세법 §8④)   — 1세대1주택자 판정에서만 뺀다.
                                     **과세표준에는 그대로 합산된다.**
    합산배제     (종부세법 §8②)   — 과세표준 합산에서 빠진다. 세금이 안 붙는다.

시중 계산기는 이 판정을 아예 하지 않고 "예외 규정이 있으니 관할 세무서에 문의"로
회피한다. 그런데 종부세를 실제로 내는 계층 상당수가 상속·일시적2주택·임대등록 중
하나 이상에 걸린다. 즉 **면책으로 제외한 집합이 곧 타깃 사용자 집합**이다.

여기서는 판정하되, 사실이 모자라면 **판정하지 않고 그렇다고 말한다.**
유리한 쪽으로 자동 가정하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from fractions import Fraction
from typing import Any, Mapping

from ..domain.certainty import Certainty, DeterminationQuality
from ..domain.models import (
    AcquisitionCause,
    ElectionKind,
    PersonId,
    Property,
    PropertyId,
    TaxCase,
    Won,
)
from ..rules.resolver import RuleSet
from ..rules.schema import Track
from .trace import Alternative, SubjectRef, SubjectType, TraceNode, Value, node

S = "jongbuse.special"


class SpecialKind(StrEnum):
    INHERITANCE = "inheritance"
    TEMPORARY_TWO = "temporary_two"
    RURAL_LOW_PRICE = "rural_low_price"
    RENTAL_EXCLUSION = "rental_exclusion"


@dataclass(frozen=True, slots=True)
class SpecialHouse:
    """특례가 적용된 주택 한 채."""

    property_id: PropertyId
    kind: SpecialKind
    reason_ko: str
    basis_ko: str
    excluded_from_count: bool = True
    """1세대1주택자 판정에서 주택 수 제외."""
    excluded_from_aggregate: bool = False
    """과세표준 합산에서도 제외(합산배제). 임대주택 등."""


@dataclass(frozen=True, slots=True)
class MissedSpecial:
    """요건을 못 갖췄거나 사실이 모자라 적용하지 못한 특례."""

    property_id: PropertyId
    kind: SpecialKind
    reason_ko: str
    actionable: bool = False
    undecidable: bool = False


@dataclass(frozen=True, slots=True)
class SpecialAssessment:
    """세대 전체에 대한 특례 판정 결과."""

    counted: tuple[PropertyId, ...]
    """**세대** 기준으로 주택 수에 들어가는 주택. 1세대1주택자 판정용(종부세법 §8④)."""
    specials: tuple[SpecialHouse, ...]
    missed: tuple[MissedSpecial, ...]
    certainty: Certainty = Certainty()
    restored: PropertyId | None = None
    """제외하고 나니 0채가 되어 되돌린 주택.

    종부령 §2의3① "1주택만을 소유한 경우". 주택 수 제외는 다른 주택을 무시하는
    장치이지 자기 자신을 없애는 장치가 아니다. 특례주택만 가진 사람을 0채로
    만들면 1세대1주택자 혜택을 통째로 잃는다(2026-08-04 감사).
    """
    personal_counted: tuple[PropertyId, ...] = ()
    """**납세의무자 본인** 기준 주택 수에 들어가는 주택.

    ★ 세율표와 공정시장가액비율은 이쪽을 써야 한다(종부령 §4의3③).
      조문: "법 제9조제1항 및 제2항에 따라 주택분 종합부동산세액을 계산할 때
      적용해야 하는 주택 수는 … 1주택을 여러 사람이 공동으로 소유한 경우
      공동 소유자 각자가 그 주택을 소유한 것으로 본다"

      §9(세율)는 납세의무자 개인의 주택 수를 말하고, §8④(1세대1주택자)는
      세대를 말한다. **두 값이 다를 수 있다.**
      부부가 각자 2채씩(세대 4채) 가지면 세대 기준으로는 '3+'지만
      각자는 2채이므로 '1-2' 세율표를 쓴다.
    """

    @property
    def count(self) -> int:
        """세대 주택 수."""
        return len(self.counted)

    @property
    def personal_count(self) -> int:
        """본인 주택 수. 세율표·공정시장가액비율은 이 값으로 고른다."""
        return len(self.personal_counted)

    @property
    def is_one_house(self) -> bool:
        """특례 적용 후 주택 수가 1채인가 — 종부세법 §8④의 '1세대1주택자로 본다'."""
        return self.count == 1

    @property
    def main_property_id(self) -> PropertyId | None:
        """거주 여부 판정의 기준이 되는 '기존 1주택'.

        문답자료 p.40: 1주택 + 특례주택을 보유하면 거주 여부는 **기존 1주택 기준**으로
        판단한다. 특례주택에 살고 있어도 본래 주택이 비어 있으면 비거주다.
        """
        return self.counted[0] if len(self.counted) == 1 else None

    def aggregate_excluded(self) -> frozenset[PropertyId]:
        return frozenset(
            s.property_id for s in self.specials if s.excluded_from_aggregate
        )


def assess(
    case: TaxCase,
    person_id: PersonId,
    ruleset: RuleSet,
    *,
    track: Track = Track.CURRENT,
    on: date | None = None,
) -> SpecialAssessment:
    """세대가 보유한 주택에 특례를 적용해 주택 수를 다시 센다."""
    on = on or case.assessment_date
    member_ids = set(case.household_member_ids(person_id))

    owned: dict[PropertyId, Property] = {}
    for o in case.ownerships:
        if o.person_id in member_ids:
            prop = case.find_property(o.property_id)
            if prop.is_house:
                owned[prop.id] = prop

    specials: list[SpecialHouse] = []
    missed: list[MissedSpecial] = []
    undecidable = False

    for pid, prop in owned.items():
        rental = _check_rental(case, prop, ruleset, on, track)
        if rental is not None:
            if isinstance(rental, SpecialHouse):
                specials.append(rental)
            else:
                missed.append(rental)
                undecidable = undecidable or rental.undecidable
            continue

        inherited = _check_inheritance(case, prop, ruleset, on, track)
        if inherited is not None:
            if isinstance(inherited, SpecialHouse):
                specials.append(inherited)
            else:
                missed.append(inherited)
            continue

        rural = _check_rural(case, prop, ruleset, on, track)
        if rural is not None:
            if isinstance(rural, SpecialHouse):
                specials.append(rural)
            else:
                missed.append(rural)

    # 일시적 2주택은 물건 하나가 아니라 '두 채를 함께 본' 판정이라 따로 돌린다.
    temp = _check_temporary_two(case, owned, specials, ruleset, on, track)
    if isinstance(temp, SpecialHouse):
        specials.append(temp)
    elif isinstance(temp, MissedSpecial):
        missed.append(temp)

    # ── 합산배제 임대주택의 주택 수 제외는 **조건부**다 (종부령 §2의3② 단서) ──
    # "제1호는 각 호 외의 주택을 소유하는 자가 과세기준일 현재 그 주택에 주민등록이
    #  되어 있고 실제로 거주하고 있는 경우에 한정하여 적용한다"
    # 즉 다른 집에 **실제로 살고 있어야** 임대주택을 주택 수에서 뺄 수 있다.
    # 무조건 빼면 임대사업자가 조용히 1세대1주택자가 된다(2026-08-04 감사).
    specials, missed = _apply_rental_residence_proviso(
        case, person_id, owned, specials, missed, on
    )

    excluded = {s.property_id for s in specials if s.excluded_from_count}
    counted = tuple(pid for pid in owned if pid not in excluded)

    # ── 제외하고 나니 0채라면, 제외가 과했다 (종부령 §2의3①) ──────────
    # "1세대 1주택자란 … 주택분 재산세 과세대상인 **1주택만을 소유한 경우**"
    # 주택 수 제외는 '다른 주택을 무시하는 장치'이지 자기 자신을 없애는 장치가 아니다.
    # 특례주택 1채만 가진 사람을 0채로 만들면 1세대1주택자 혜택을 통째로 잃는다.
    restored: PropertyId | None = None
    if not counted and owned:
        restored = max(
            owned, key=lambda pid: _price_of(owned[pid], case.year)
        )
        counted = (restored,)

    # 본인이 지분을 가진 주택만 따로 센다(종부령 §4의3③1 — 공동 소유자 각자가
    # 그 주택을 소유한 것으로 본다). 지분 크기와 무관하게 1채다.
    mine = {o.property_id for o in case.ownerships_of(person_id)}
    personal_counted = tuple(pid for pid in counted if pid in mine)

    certainty = Certainty()
    if undecidable:
        certainty = certainty & Certainty(determination=DeterminationQuality.UNDECIDABLE)

    return SpecialAssessment(
        counted,
        tuple(specials),
        tuple(missed),
        certainty,
        restored=restored,
        personal_counted=personal_counted,
    )


def _price_of(prop: Property, year: int) -> int:
    fact = prop.price_for(year)
    if fact is not None:
        return fact.value
    return max((p.value for p in prop.published_prices), default=0)


def _resides_in(case: TaxCase, member_ids: set[PersonId], pid: PropertyId, on: date) -> bool:
    """세대원 누구든 그 집에 과세기준일 현재 살고 있는가."""
    return any(
        r.covers(on) for m in member_ids for r in case.residences_of(m, pid)
    )


def _apply_rental_residence_proviso(
    case: TaxCase,
    person_id: PersonId,
    owned: Mapping[PropertyId, Property],
    specials: list[SpecialHouse],
    missed: list[MissedSpecial],
    on: date,
) -> tuple[list[SpecialHouse], list[MissedSpecial]]:
    """종부령 §2의3② 단서 — 합산배제 임대주택의 **주택 수** 제외 조건.

    합산배제(과세표준에서 빼는 것)는 그대로 유지된다. 조건이 걸리는 것은
    **주택 수 제외**뿐이다. 이 둘을 한 필드로 뭉치면 조문을 표현할 수 없다.
    """
    rentals = [s for s in specials if s.kind is SpecialKind.RENTAL_EXCLUSION]
    if not rentals:
        return specials, missed

    rental_ids = {s.property_id for s in rentals}
    others = [pid for pid in owned if pid not in rental_ids]
    if not others:
        # 임대주택만 보유 — "각 호 외의 주택"이 없어 단서를 충족할 수 없다.
        # 아래 0채 복원 규칙이 받아준다.
        lives_elsewhere = False
        reason = "합산배제 임대주택 외에 보유한 주택이 없어 §2의3② 단서를 충족할 수 없다"
    else:
        member_ids = set(case.household_member_ids(person_id))
        lives_elsewhere = any(_resides_in(case, member_ids, pid, on) for pid in others)
        reason = (
            "다른 주택에 주민등록·실거주 사실이 확인되지 않았다"
            "(종부령 §2의3② 단서). 거주 사실이 있으면 주택 수에서 빠집니다."
        )

    if lives_elsewhere:
        return specials, missed

    out: list[SpecialHouse] = []
    for s in specials:
        if s.kind is SpecialKind.RENTAL_EXCLUSION:
            out.append(replace(s, excluded_from_count=False))
            missed.append(
                MissedSpecial(
                    s.property_id,
                    SpecialKind.RENTAL_EXCLUSION,
                    f"과세표준 합산배제는 적용되지만 **주택 수에서는 빠지지 않는다** — {reason}",
                    actionable=True,
                    undecidable=not others,
                )
            )
        else:
            out.append(s)
    return out, missed


# --------------------------------------------------------------------------
# 개별 판정
# --------------------------------------------------------------------------


def _payload(ruleset: RuleSet, rule_id: str, on: date, track: Track, **ctx) -> Mapping[str, Any] | None:
    res = ruleset.resolve_or_none(rule_id, on=on, track=track, **ctx)
    return res.block.payload if res is not None else None


def is_capital_area(legal_dong_code: str, ruleset: RuleSet, on: date, track: Track) -> bool:
    """수도권(서울·인천·경기) 여부. 상속주택 저가 기준이 6억/3억으로 갈린다."""
    payload = _payload(ruleset, f"{S}.capital_area_codes", on, track) or {}
    return any(str(legal_dong_code).startswith(p) for p in payload.get("prefixes", ()))


def _check_inheritance(
    case: TaxCase, prop: Property, ruleset: RuleSet, on: date, track: Track
) -> SpecialHouse | MissedSpecial | None:
    """상속주택 주택 수 제외(종부령 §4의2②).

    세 요건 중 **하나만** 충족하면 된다. 누적 조건으로 읽으면 대부분 탈락한다.
    """
    ownership = next(
        (
            o
            for o in case.owners_of(prop.id)
            if o.cause is AcquisitionCause.INHERITANCE and o.inherited
        ),
        None,
    )
    if ownership is None or ownership.inherited is None:
        return None

    payload = _payload(ruleset, f"{S}.inheritance", on, track)
    if payload is None:
        return None

    meta = ownership.inherited
    basis = "종합부동산세법 시행령 §4의2②"

    for cond in payload["any_of"]:
        key = cond["key"]
        if key == "within_years":
            elapsed_years = (on - meta.inheritance_date).days / 365.25
            if elapsed_years < int(cond["years"]):
                return SpecialHouse(
                    prop.id,
                    SpecialKind.INHERITANCE,
                    f"상속개시 후 {elapsed_years:.1f}년으로 {cond['years']}년 미경과",
                    basis,
                )
        elif key == "small_share":
            if meta.share <= Fraction(cond["max_share"]):
                return SpecialHouse(
                    prop.id,
                    SpecialKind.INHERITANCE,
                    f"상속 지분율 {meta.share}로 {cond['max_share']} 이하",
                    basis,
                )
        elif key == "low_value":
            capital = is_capital_area(prop.legal_dong_code, ruleset, on, track)
            limit = int(
                cond["max_value_capital"] if capital else cond["max_value_other"]
            )
            if meta.inherited_value <= limit:
                where = "수도권" if capital else "수도권 밖"
                return SpecialHouse(
                    prop.id,
                    SpecialKind.INHERITANCE,
                    f"지분 공시가격 {meta.inherited_value:,}원이 {where} 기준 {limit:,}원 이하",
                    basis,
                )

    elapsed = (on - meta.inheritance_date).days / 365.25
    return MissedSpecial(
        prop.id,
        SpecialKind.INHERITANCE,
        f"상속주택이지만 세 요건을 모두 벗어났다 "
        f"(경과 {elapsed:.1f}년 · 지분 {meta.share} · 지분가액 {meta.inherited_value:,}원)",
    )


def _check_rural(
    case: TaxCase, prop: Property, ruleset: RuleSet, on: date, track: Track
) -> SpecialHouse | MissedSpecial | None:
    """지방 저가주택(종부령 §4의2③). 가액 요건은 자동 판정하되 지역 요건은 확인을 요구한다.

    지역 요건("수도권 밖 광역시·특별자치시가 아닌 지역" 등)은 법정동코드만으로 완전히
    판정하기 어렵다(광역시 소속 군, 세종시 읍·면 등 예외가 있다). 그래서 가액만으로
    단정하지 않고 사용자 확인을 붙인다.
    """
    payload = _payload(ruleset, f"{S}.rural_low_price", on, track)
    if payload is None:
        return None

    fact = prop.price_for(case.year)
    if fact is None:
        return None
    if is_capital_area(prop.legal_dong_code, ruleset, on, track):
        return None

    limit = int(payload["max_value"])
    if fact.value > limit:
        return None

    auto_from = payload.get("auto_applied_from")
    needs_application = not (auto_from and on >= date.fromisoformat(str(auto_from)))
    if needs_application and case.election(
        _any_owner(case, prop.id), ElectionKind.RENTAL_EXCLUSION
    ) is None:
        # 현행은 신청해야 적용된다. 신청 사실이 없으면 적용하지 않되 안내한다.
        return MissedSpecial(
            prop.id,
            SpecialKind.RURAL_LOW_PRICE,
            f"공시가격 {fact.value:,}원으로 지방 저가주택 가액 요건({limit:,}원 이하)을 "
            "충족하나, 현행법은 관할 세무서장에게 신청해야 적용된다. "
            "지역 요건도 함께 확인이 필요하다.",
            actionable=True,
        )

    return SpecialHouse(
        prop.id,
        SpecialKind.RURAL_LOW_PRICE,
        f"공시가격 {fact.value:,}원으로 {limit:,}원 이하이고 수도권 밖에 소재",
        "종합부동산세법 시행령 §4의2③",
    )


def _check_rental(
    case: TaxCase, prop: Property, ruleset: RuleSet, on: date, track: Track
) -> SpecialHouse | MissedSpecial | None:
    """합산배제 임대주택(종부세법 §8②).

    ★ 다른 특례와 달리 **과세표준 합산에서 제외**된다. 세금이 아예 안 붙는다.
    그만큼 요건이 까다로워 등록증·임대차계약 없이는 확정할 수 없다.
    """
    if prop.rental is None:
        return None
    payload = _payload(ruleset, f"{S}.rental_exclusion", on, track)
    if payload is None:
        return None

    if not prop.rental.rent_increase_within_cap:
        return MissedSpecial(
            prop.id,
            SpecialKind.RENTAL_EXCLUSION,
            "임대료 증액 5% 상한을 지키지 않아 합산배제가 깨진다",
            actionable=False,
        )

    owner = _any_owner(case, prop.id)
    if case.election(owner, ElectionKind.RENTAL_EXCLUSION) is None:
        return MissedSpecial(
            prop.id,
            SpecialKind.RENTAL_EXCLUSION,
            "등록임대주택이지만 합산배제 신고 사실이 확인되지 않았다. "
            "임대유형·의무임대기간·가액요건 충족 여부는 등록증으로만 확인된다.",
            actionable=True,
            undecidable=True,
        )

    return SpecialHouse(
        prop.id,
        SpecialKind.RENTAL_EXCLUSION,
        f"등록임대주택({prop.rental.rental_type}) 합산배제 신고분",
        "종합부동산세법 §8② · 시행령 §3",
        excluded_from_count=True,
        excluded_from_aggregate=True,
    )


def _check_temporary_two(
    case: TaxCase,
    owned: Mapping[PropertyId, Property],
    specials: list[SpecialHouse],
    ruleset: RuleSet,
    on: date,
    track: Track,
) -> SpecialHouse | MissedSpecial | None:
    """일시적 2주택(종부령 §4의2①). 종전주택을 팔기 전에 신규주택을 취득한 경우.

    개편안은 조정대상지역 → 조정대상지역 이동에 한해 3년 → 2년으로 줄인다.
    """
    already = {s.property_id for s in specials}
    remaining = [pid for pid in owned if pid not in already]
    if len(remaining) != 2:
        return None

    acquired: list[tuple[PropertyId, date]] = []
    for pid in remaining:
        dates = [o.acquired_on for o in case.owners_of(pid) if o.acquired_on]
        if not dates:
            return MissedSpecial(
                pid,
                SpecialKind.TEMPORARY_TWO,
                "취득일을 모르면 일시적 2주택 여부를 판정할 수 없다. 취득일을 입력해주세요.",
                actionable=True,
            )
        acquired.append((pid, min(dates)))

    acquired.sort(key=lambda x: x[1])
    (old_id, _), (new_id, new_date) = acquired

    from .regions import YES, check_regulated

    old_zone = check_regulated(
        case.find_property(old_id).legal_dong_code, ruleset, on=new_date, track=track
    )
    new_zone = check_regulated(
        case.find_property(new_id).legal_dong_code, ruleset, on=new_date, track=track
    )
    adjusted_to_adjusted = old_zone.designation is YES and new_zone.designation is YES

    payload = _payload(
        ruleset,
        f"{S}.temporary_two",
        on,
        track,
        adjusted_to_adjusted=adjusted_to_adjusted,
    )
    if payload is None:
        payload = _payload(ruleset, f"{S}.temporary_two", on, track)
    if payload is None:
        return None

    years = int(payload["years"])
    grandfather = payload.get("grandfather_before")
    if grandfather and new_date < date.fromisoformat(str(grandfather)):
        # 경과조치: 개편안 발표 전 취득분은 종전 3년을 적용한다.
        years = 3

    elapsed = (on - new_date).days / 365.25
    label = "조정대상지역 간 이동" if adjusted_to_adjusted else "일반"

    if elapsed < years:
        return SpecialHouse(
            new_id,
            SpecialKind.TEMPORARY_TWO,
            f"신규주택 취득 후 {elapsed:.1f}년으로 특례기간 {years}년 이내 ({label})",
            "종합부동산세법 시행령 §4의2①",
        )

    return MissedSpecial(
        new_id,
        SpecialKind.TEMPORARY_TWO,
        f"신규주택 취득 후 {elapsed:.1f}년이 지나 특례기간 {years}년을 초과했다 ({label})",
    )


def _any_owner(case: TaxCase, property_id: PropertyId) -> PersonId:
    owners = case.owners_of(property_id)
    return owners[0].person_id if owners else PersonId("")


# --------------------------------------------------------------------------
# 감사 추적
# --------------------------------------------------------------------------


def special_trace(
    case: TaxCase, assessment: SpecialAssessment, subject: SubjectRef
) -> TraceNode:
    """'1세대1주택자입니다'만 보여주면 체크박스와 다를 게 없다.
    어떤 집이 왜 빠졌고, 어떤 특례가 왜 적용되지 않았는지가 드러나야 한다."""

    def label(pid: PropertyId) -> str:
        return case.find_property(pid).display_name or str(pid)

    lines = [f"{label(p)}(산입)" for p in assessment.counted]
    lines += [
        f"{label(s.property_id)}(제외: {s.kind})" for s in assessment.specials
    ]

    alternatives = tuple(
        Alternative(
            key=f"special:{m.kind}:{m.property_id}",
            label_ko=f"{_KIND_KO[m.kind]} — {label(m.property_id)}",
            reason_ko=m.reason_ko,
            actionable=m.actionable,
        )
        for m in assessment.missed
    )

    aggregate_excluded = assessment.aggregate_excluded()
    note = "종합부동산세법 §8④에 따른 주택 수 판정"
    if aggregate_excluded:
        names = ", ".join(label(p) for p in aggregate_excluded)
        note += f" · 합산배제(과세표준에서 제외): {names}"

    return node(
        "jb.04.special_houses",
        "주택 수 특례 판정",
        Value(
            assessment.count,
            unit="count",
            certainty=assessment.certainty,
            label=f"{assessment.count}주택",
        ),
        subject=subject,
        formula="세대 보유 주택 − 주택 수 제외 특례",
        substitution=" / ".join(lines) or "보유 주택 없음",
        alternatives_not_taken=alternatives,
        note_ko=note,
    )


_KIND_KO = {
    SpecialKind.INHERITANCE: "상속주택 주택 수 제외",
    SpecialKind.TEMPORARY_TWO: "일시적 2주택 특례",
    SpecialKind.RURAL_LOW_PRICE: "지방 저가주택 특례",
    SpecialKind.RENTAL_EXCLUSION: "합산배제 임대주택",
}
