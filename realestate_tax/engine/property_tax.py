"""재산세 주택분 계산.

왜 종부세보다 먼저인가
    ① 의존 방향이 그렇다. 종부세액 = 산출세액 − **재산세 공제** − 세액공제이고,
       종부세 세부담상한도 재산세를 포함한 총 보유세 기준이다. 재산세는 종부세의
       *입력*이다. 나중에 만들면 종부세가 틀렸을 때 원인이 종부세 버그인지
       재산세 부재인지 분리할 수 없다.
    ② 재산세는 이번 개편안에 개정이 없어 전부 확정된 현행법이다. 개편안의
       불확실성과 섞이지 않은 상태에서 룰셋·감사추적 인프라를 검증할 수 있다.
    ③ 종부세 납세자는 소수지만 재산세는 전원이 낸다.

계산 구조 (지방세법)
    시가표준액(공시가격)
      × 공정시장가액비율            §110①, 시행령 §109
      = 과세표준  (과세표준상한 적용) §110③
      × 세율                        §111①3 표준 / §111의2① 1세대1주택 특례
      = 재산세 본세
      + 도시지역분 (과세표준 × 0.14%) §112①2
      + 지방교육세 (본세 × 20%)       §151①2
      = 납부할 세액

두 가지를 특별히 지킨다.
    · 재산세는 **물건별** 과세다. 여러 채를 합산해 누진세율을 매기지 않는다.
      (정부 세제개편안 문답자료 p.47의 3주택 예시는 합산 방식으로 계산돼 있어
       실제보다 세액이 크게 나온다 — 골든 테스트에 가정으로 명시해 둔다.)
    · 지방교육세 과세표준에 **도시지역분은 포함되지 않는다**(§151①2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from ..domain.certainty import Certainty, InputQuality
from ..domain.models import (
    PersonId,
    Property,
    PropertyId,
    TaxCase,
    TaxYear,
    Won,
)
from ..rules.resolver import RuleSet
from ..rules.schema import Track
from .determination import HouseCount, household_house_count, one_house_determination_trace
from .trace import (
    Alternative,
    BranchRecord,
    SubjectRef,
    SubjectType,
    TraceNode,
    UnknownReason,
    Value,
    derive_value,
    node,
)

R = "property_tax.house"


@dataclass(frozen=True, slots=True)
class PropertyTaxOptions:
    """엔진이 사실에서 도출할 수 없는 지역별 변수.

    전부 조례 소관이라 전국 단일값이 없다. 기본값은 법정 표준이고, 사용자가
    고지서를 보고 조정할 수 있게 열어 둔다. "전국이 다 같다"고 가정하는 것이
    시중 계산기가 틀리는 자리 중 하나다.
    """

    urban_area_rate: Fraction | None = None
    """도시지역분 세율. 조례로 0.23%까지 다르게 정할 수 있다(§112②)."""

    rate_adjustment: Fraction | None = None
    """탄력세율. 지자체가 표준세율의 ±50% 범위에서 가감할 수 있다(§111③)."""

    prior_year_taxable_base: int | None = None
    """직전연도 과세표준 상당액. 과세표준상한(§110③) 계산에 필요하다."""

    force_one_house_special: bool | None = None
    """1세대1주택 판정을 강제로 덮어쓴다. 테스트와 '만약에' 시뮬레이션 전용."""


@dataclass(frozen=True, slots=True)
class PropertyTaxResult:
    """물건 하나에 대한 재산세 계산 결과(지분 안분 전, 물건 전체 기준)."""

    property_id: PropertyId
    year: TaxYear
    taxable_base: Value
    base_tax: Value
    urban_tax: Value
    education_tax: Value
    total: Value
    trace: TraceNode

    def share_of(self, share: Fraction) -> Won:
        """지분만큼 안분한 세액.

        Fraction으로 곱하므로 3인 1/3 공동명의의 합이 원본과 정확히 같다.
        정수 퍼센트로 받으면 1%가 증발한다.
        """
        return int(self.total.as_int() * share)


def price_band(published_price: Won) -> str:
    """공정시장가액비율 룰셋의 셀렉터 키. 시행령 §109①2 단서의 3구간."""
    if published_price <= 300_000_000:
        return "~3억"
    if published_price <= 600_000_000:
        return "3~6억"
    return "6억~"


def compute_property_tax(
    case: TaxCase,
    property_id: PropertyId,
    ruleset: RuleSet,
    *,
    track: Track = Track.CURRENT,
    owner_id: PersonId | None = None,
    options: PropertyTaxOptions | None = None,
) -> PropertyTaxResult:
    """물건 하나의 재산세를 계산한다(물건 전체 기준).

    owner_id는 1세대1주택 판정을 위해 필요하다 — 같은 집이라도 소유자의
    세대가 몇 채를 갖고 있느냐에 따라 세율과 공정시장가액비율이 달라진다.
    """
    options = options or PropertyTaxOptions()
    prop = case.find_property(property_id)
    on = case.assessment_date
    children: list[TraceNode] = []

    # ── 01. 시가표준액 ────────────────────────────────────────────────
    price_fact = prop.price_for(case.year)
    if price_fact is None:
        price = Value.missing(
            UnknownReason.MISSING_INPUT, label=f"{case.year}년 공시가격"
        )
    else:
        price = Value.money(
            price_fact.value,
            certainty=Certainty(input=price_fact.quality),
            label=f"{case.year}년 공시가격",
        )
    children.append(
        node(
            "pt.01.published_price",
            "시가표준액(공시가격)",
            price,
            subject=_subject(prop),
            formula="과세기준일(6/1) 현재 공시가격",
            # 산식만 있고 대입값이 없으면 화면에서 이 줄이 비어 보인다. 재산세 계산의
            # **출발점**이라 여기가 비면 아래 모든 숫자의 출처를 따라갈 수 없다.
            substitution=(
                f"{case.year}년 {prop.display_name or prop.id}: {price_fact.value:,}원"
                if price_fact is not None
                else f"{case.year}년 공시가격 미입력"
            ),
        )
    )

    # ── 03. 1세대1주택 판정 ──────────────────────────────────────────
    count, one_house = _determine_one_house(case, owner_id, options)
    if owner_id is not None and options.force_one_house_special is None:
        children.append(one_house_determination_trace(case, owner_id, count))

    # ── 04. 공정시장가액비율 → 과세표준 ──────────────────────────────
    price_amount = price.as_int()
    fmv_res = ruleset.resolve(
        f"{R}.fair_market_ratio",
        on=on,
        track=track,
        one_house_special=one_house,
        price_band=price_band(price_amount),
    )
    fmv = fmv_res.block.as_fraction()
    fmv_ref = fmv_res.ref()

    raw_base = int(price_amount * fmv)
    base_value = derive_value(
        raw_base,
        price,
        fmv_ref,
        label="과세표준(상한 적용 전)",
    )
    children.append(
        node(
            "pt.04.taxable_base_raw",
            "과세표준(공정시장가액비율 적용)",
            base_value,
            subject=_subject(prop),
            inputs=(("시가표준액", price),),
            rules=(fmv_ref,),
            formula="시가표준액 × 공정시장가액비율",
            substitution=f"{price_amount:,} × {float(fmv) * 100:g}%",
        )
    )

    # ── 05. 과세표준상한 (§110③) ────────────────────────────────────
    base_value, cap_node = _apply_taxable_base_cap(
        ruleset, on, track, base_value, raw_base, options, prop
    )
    children.append(cap_node)
    taxable_base = base_value.as_int()

    # ── 06~07. 세율 → 재산세 본세 ───────────────────────────────────
    base_tax, tax_node = _compute_base_tax(
        ruleset, on, track, prop, price_amount, taxable_base, one_house, base_value, options
    )
    children.append(tax_node)

    # ── 08. 도시지역분 (§112) ───────────────────────────────────────
    urban_tax, urban_node = _compute_urban_tax(
        ruleset, on, track, prop, taxable_base, base_value, options
    )
    children.append(urban_node)

    # ── 09. 지방교육세 (§151①2) ─────────────────────────────────────
    edu_res = ruleset.resolve(f"{R}.local_education_rate", on=on, track=track)
    edu_rate = edu_res.block.as_fraction()
    edu_amount = int(base_tax.as_int() * edu_rate)
    education_tax = derive_value(
        edu_amount, base_tax, edu_res.ref(), label="지방교육세"
    )
    children.append(
        node(
            "pt.09.local_education_tax",
            "지방교육세",
            education_tax,
            subject=_subject(prop),
            inputs=(("재산세 본세", base_tax),),
            rules=(edu_res.ref(),),
            formula="재산세 본세 × 20%  (도시지역분은 과세표준에 포함하지 않는다)",
            substitution=f"{base_tax.as_int():,} × {float(edu_rate) * 100:g}%",
        )
    )

    # ── 10. 합계 ────────────────────────────────────────────────────
    total_amount = base_tax.as_int() + urban_tax.as_int() + education_tax.as_int()
    total = derive_value(
        total_amount, base_tax, urban_tax, education_tax, label="재산세 합계"
    )

    trace = node(
        "pt.00.property_tax",
        f"재산세 — {prop.display_name or prop.id} ({case.year}년)",
        total,
        subject=_subject(prop),
        formula="재산세 본세 + 도시지역분 + 지방교육세",
        substitution=(
            f"{base_tax.as_int():,} + {urban_tax.as_int():,} "
            f"+ {education_tax.as_int():,} = {total_amount:,}"
        ),
        children=tuple(children),
    )

    return PropertyTaxResult(
        property_id=prop.id,
        year=case.year,
        taxable_base=base_value,
        base_tax=base_tax,
        urban_tax=urban_tax,
        education_tax=education_tax,
        total=total,
        trace=trace,
    )


# --------------------------------------------------------------------------
# 단계별 구현
# --------------------------------------------------------------------------


def _subject(prop: Property) -> SubjectRef:
    return SubjectRef(SubjectType.PROPERTY, str(prop.id), prop.display_name or str(prop.id))


def _determine_one_house(
    case: TaxCase, owner_id: PersonId | None, options: PropertyTaxOptions
) -> tuple[HouseCount, bool]:
    if options.force_one_house_special is not None:
        return (
            HouseCount((), (), Certainty(), ()),
            options.force_one_house_special,
        )
    if owner_id is None:
        # 소유자를 모르면 세대를 알 수 없다. 유리한 쪽(1주택 특례)으로 기울지 않는다.
        return HouseCount((), (), Certainty(), ()), False
    count = household_house_count(case, owner_id)
    return count, count.is_one_house


def _apply_taxable_base_cap(
    ruleset: RuleSet,
    on: date,
    track: Track,
    base_value: Value,
    raw_base: int,
    options: PropertyTaxOptions,
    prop: Property,
) -> tuple[Value, TraceNode]:
    """과세표준상한제(§110③).

    2023년 개편으로 주택 재산세의 세부담상한(§122)이 폐지되고 이것으로 대체됐다.
    §122 단서: "다만, 주택의 경우에는 적용하지 아니한다."
    시중 계산기와 해설이 아직도 105/110/130% 세부담상한을 말하는 것은 폐지된 제도다.
    """
    cap_res = ruleset.resolve(f"{R}.taxbase_cap_rate", on=on, track=track)
    cap_rate = cap_res.block.as_fraction()

    if options.prior_year_taxable_base is None:
        # 직전연도 과세표준을 모르면 상한을 계산할 수 없다. 상한 미적용으로
        # 밀어붙이되 미상 사유를 남긴다 — 상한이 걸렸다면 세액이 더 낮았을 수 있다.
        return base_value, node(
            "pt.05.taxable_base_cap",
            "과세표준상한 적용",
            base_value,
            subject=_subject(prop),
            rules=(cap_res.ref(),),
            formula="과세표준상한액 = 직전연도 과세표준 상당액 + (당해 과세표준 × 상한율)",
            substitution="직전연도 과세표준 미입력 → 상한 미적용",
            note_ko="직전연도 과세표준을 알려주면 상한 적용 여부까지 계산합니다.",
            alternatives_not_taken=(
                Alternative(
                    key="taxbase_cap",
                    label_ko="과세표준상한(지방세법 §110③)",
                    reason_ko="직전연도 과세표준 상당액이 없어 판정하지 못했다",
                    actionable=True,
                ),
            ),
        )

    cap_amount = options.prior_year_taxable_base + int(raw_base * cap_rate)
    capped = min(raw_base, cap_amount)
    applied = capped < raw_base
    result = derive_value(capped, base_value, cap_res.ref(), label="과세표준")

    return result, node(
        "pt.05.taxable_base_cap",
        "과세표준상한 적용",
        result,
        subject=_subject(prop),
        rules=(cap_res.ref(),),
        formula="min(산정 과세표준, 직전연도 과세표준 + 당해 과세표준 × 상한율)",
        substitution=(
            f"min({raw_base:,}, {options.prior_year_taxable_base:,} "
            f"+ {raw_base:,} × {float(cap_rate) * 100:g}%) = {capped:,}"
        ),
        branch=BranchRecord(
            condition_ko="산정 과세표준 > 과세표준상한액",
            taken="상한 적용" if applied else "상한 미적용",
            detail_ko=f"상한액 {cap_amount:,}원",
        ),
    )


def _compute_base_tax(
    ruleset: RuleSet,
    on: date,
    track: Track,
    prop: Property,
    published_price: int,
    taxable_base: int,
    one_house: bool,
    base_value: Value,
    options: PropertyTaxOptions,
) -> tuple[Value, TraceNode]:
    """세율 적용. 1세대1주택 특례세율은 **시가표준액 9억원 이하**에만 적용된다.

    이 경계를 놓치면 고가 1주택의 세액이 통째로 틀린다. 실제로 정부 문답자료의
    공시가 30억 1주택 사례도 표준세율로 계산돼 있다.
    """
    price_cap = ruleset.resolve(
        f"{R}.one_house_rate_price_cap", on=on, track=track
    ).block.as_int()
    special_applies = one_house and published_price <= price_cap

    rule_id = (
        f"{R}.rate_table_one_house" if special_applies else f"{R}.rate_table_standard"
    )
    res = ruleset.resolve(rule_id, on=on, track=track)
    table = res.block.table
    assert table is not None

    amount, bracket, substitution = table.tax_for(taxable_base)

    alternatives: tuple[Alternative, ...] = ()
    if one_house and not special_applies:
        special = ruleset.resolve(f"{R}.rate_table_one_house", on=on, track=track)
        assert special.block.table is not None
        would_be, _, _ = special.block.table.tax_for(taxable_base)
        alternatives = (
            Alternative(
                key="one_house_rate",
                label_ko="1세대1주택 세율 특례(지방세법 §111의2)",
                reason_ko=(
                    f"시가표준액 {published_price:,}원이 적용 상한 "
                    f"{price_cap:,}원을 초과한다"
                ),
                delta=Value.money(would_be - amount),
            ),
        )

    if options.rate_adjustment is not None:
        # 탄력세율(§111③)은 지자체 조례라 전국 단일값이 없다. 사용자가 알려준 경우만 반영.
        amount = int(amount * (1 + options.rate_adjustment))
        substitution += f"  × (1 {options.rate_adjustment:+}) [조례 탄력세율]"

    value = derive_value(amount, base_value, res.ref(), label="재산세 본세")
    return value, node(
        "pt.07.base_tax",
        "재산세 본세",
        value,
        subject=_subject(prop),
        inputs=(("과세표준", base_value),),
        rules=(res.ref(),),
        formula="과세표준 구간별 세율 적용",
        substitution=substitution,
        branch=BranchRecord(
            condition_ko="1세대1주택 특례세율 적용 여부",
            taken="특례세율" if special_applies else "표준세율",
            detail_ko=(
                f"세대 1주택={one_house}, 시가표준액 {published_price:,}원 "
                f"(특례 상한 {price_cap:,}원)"
            ),
        ),
        alternatives_not_taken=alternatives,
    )


def _compute_urban_tax(
    ruleset: RuleSet,
    on: date,
    track: Track,
    prop: Property,
    taxable_base: int,
    base_value: Value,
    options: PropertyTaxOptions,
) -> tuple[Value, TraceNode]:
    """도시지역분(§112). 도시지역 안이고 지방의회가 고시한 지역에만 부과된다.

    시중 계산기는 이걸 묻지 않고 전국에 일률 부과한다. 도시지역 밖 주택은
    그만큼 세액이 과대계상된다.
    """
    res = ruleset.resolve(f"{R}.urban_area_rate", on=on, track=track)
    rate = options.urban_area_rate or res.block.as_fraction()

    if not prop.in_urban_planning_area:
        zero = derive_value(0, base_value, res.ref(), label="도시지역분")
        return zero, node(
            "pt.08.urban_area_tax",
            "재산세 도시지역분",
            zero,
            subject=_subject(prop),
            rules=(res.ref(),),
            formula="도시지역 안 주택에만 부과",
            substitution="도시지역 밖 → 0원",
            branch=BranchRecord(
                condition_ko="도시지역 안 여부", taken="도시지역 밖", detail_ko=""
            ),
        )

    amount = int(taxable_base * rate)
    value = derive_value(amount, base_value, res.ref(), label="도시지역분")
    note = ""
    if options.urban_area_rate is not None:
        note = "조례로 정한 세율을 사용했습니다(법정 표준 0.14%)."
    return value, node(
        "pt.08.urban_area_tax",
        "재산세 도시지역분",
        value,
        subject=_subject(prop),
        inputs=(("과세표준", base_value),),
        rules=(res.ref(),),
        formula="과세표준 × 도시지역분 세율",
        substitution=f"{taxable_base:,} × {float(rate) * 100:g}%",
        note_ko=note,
    )
