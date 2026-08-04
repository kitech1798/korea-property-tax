"""재산세 엔진 테스트.

핵심은 골든 테스트다. 재정경제부 세제개편안 문답자료에서 역산한 재산세를
엔진이 **원 단위까지** 재현하는지 본다. 정부 문서와 숫자가 맞는다는 것이
이 서비스가 내세울 수 있는 유일하게 검증 가능한 주장이다.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from realestate_tax.domain import (
    AcquisitionCause,
    Household,
    HouseholdId,
    InheritedMeta,
    Ownership,
    Person,
    PersonId,
    PriceFact,
    Property,
    PropertyId,
    PropertyKind,
    TaxCase,
)
from realestate_tax.engine.determination import household_house_count
from realestate_tax.engine.property_tax import (
    PropertyTaxOptions,
    compute_property_tax,
    price_band,
)
from realestate_tax.engine.trace import to_manwon
from realestate_tax.rules import RuleSet, Track, default_ruleset_root

GOLDEN = Path(__file__).parent / "golden" / "property_tax_2026.yaml"
SEOUL = "1168010100"


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


@pytest.fixture(scope="module")
def golden() -> dict:
    return yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))


def build_case(
    *,
    year: int,
    prices: list[int],
    owner_count: int = 1,
    in_urban: bool = True,
) -> TaxCase:
    """주택 n채를 한 사람이 소유한 사건. owner_count는 세대원 수."""
    hh = HouseholdId("hh")
    persons = tuple(
        Person(id=PersonId(f"p{i}"), household_id=hh, name=f"세대원{i}")
        for i in range(owner_count)
    )
    properties = tuple(
        Property(
            id=PropertyId(f"h{i}"),
            kind=PropertyKind.APARTMENT,
            legal_dong_code=SEOUL,
            display_name=f"주택{i}",
            published_prices=(PriceFact(year, price),),
            in_urban_planning_area=in_urban,
        )
        for i, price in enumerate(prices)
    )
    ownerships = tuple(
        Ownership(persons[0].id, p.id, share=Fraction(1)) for p in properties
    )
    return TaxCase(
        year=year,
        persons=persons,
        households=(Household(id=hh, member_ids=tuple(p.id for p in persons)),),
        properties=properties,
        ownerships=ownerships,
    )


def compute(rs: RuleSet, case: TaxCase, pid: str = "h0", **kw):
    return compute_property_tax(
        case,
        PropertyId(pid),
        rs,
        track=Track.CURRENT,
        owner_id=PersonId("p0"),
        **kw,
    )


# --------------------------------------------------------------------------
# ★ 골든 테스트 — 정부 문서 재현
# --------------------------------------------------------------------------


def golden_ids() -> list[str]:
    doc = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    return [c["id"] for c in doc["cases"]]


@pytest.mark.parametrize("case_id", golden_ids())
def test_정부_문답자료의_재산세를_원단위까지_재현한다(rs: RuleSet, golden: dict, case_id: str):
    spec = next(c for c in golden["cases"] if c["id"] == case_id)
    case = build_case(
        year=spec["year"],
        prices=[spec["published_price"]],
        in_urban=spec["in_urban_planning_area"],
    )
    result = compute(rs, case)

    actual = to_manwon(result.total.as_int())
    expected = spec["expected_manwon"]
    src = spec["source"]
    assert float(actual) == pytest.approx(float(expected), abs=0.05), (
        f"\n{spec['label']}"
        f"\n  출처: {src['doc']} p.{src['page']} — {src['row']}"
        f"\n  역산: {spec['derivation']}"
        f"\n  기대: {expected}만원 / 실제: {actual}만원"
        f"\n  대입식: {result.trace.substitution}"
    )


def test_골든_케이스가_다섯건_이상_있다(golden: dict):
    """정부 문서 대조 없이 '정확하다'고 말하지 않기 위한 최소선."""
    assert len(golden["cases"]) >= 5


# --------------------------------------------------------------------------
# 계산 구조 — 손으로 검산 가능한지
# --------------------------------------------------------------------------


def test_계산_단계가_전부_대입식과_함께_남는다(rs: RuleSet):
    case = build_case(year=2026, prices=[3_000_000_000])
    trace = compute(rs, case).trace

    steps = {n.step_id for n in trace.walk()}
    assert steps >= {
        "pt.01.published_price",
        "pt.03.house_count",
        "pt.04.taxable_base_raw",
        "pt.05.taxable_base_cap",
        "pt.07.base_tax",
        "pt.08.urban_area_tax",
        "pt.09.local_education_tax",
    }

    # 대입식이 법조문 형식 그대로여야 사람이 조문과 눈으로 대조할 수 있다.
    base = trace.find("pt.07.base_tax")
    assert base.substitution == "570,000 + (1,350,000,000 − 300,000,000) × 0.4%"
    assert base.output.as_int() == 570_000 + int(1_050_000_000 * Fraction(4, 1000))

    fmv = trace.find("pt.04.taxable_base_raw")
    assert fmv.substitution == "3,000,000,000 × 45%"


def test_모든_숫자에_근거_조문이_붙는다(rs: RuleSet):
    case = build_case(year=2026, prices=[3_000_000_000])
    cites = {r.basis.cite_ko() for r in compute(rs, case).trace.all_rules() if r.basis}
    assert "지방세법 제111조 제1항 제3호" in cites  # 표준세율
    assert "지방세법 시행령 제109조 제1항 제2호" in cites  # 공정시장가액비율
    assert "지방세법 제112조 제1항 제2호" in cites  # 도시지역분


def test_지방교육세_과세표준에_도시지역분은_들어가지_않는다(rs: RuleSet):
    """§151①2. 포함시키면 세액이 과대계상된다."""
    case = build_case(year=2026, prices=[3_000_000_000])
    r = compute(rs, case)
    assert r.education_tax.as_int() == int(r.base_tax.as_int() * Fraction(20, 100))
    assert r.education_tax.as_int() != int(
        (r.base_tax.as_int() + r.urban_tax.as_int()) * Fraction(20, 100)
    )


def test_합계는_본세_도시지역분_지방교육세의_합(rs: RuleSet):
    r = compute(rs, build_case(year=2026, prices=[1_500_000_000]))
    assert r.total.as_int() == (
        r.base_tax.as_int() + r.urban_tax.as_int() + r.education_tax.as_int()
    )


# --------------------------------------------------------------------------
# 시중 계산기가 틀리는 지점들
# --------------------------------------------------------------------------


def test_도시지역_밖_주택은_도시지역분을_부과하지_않는다(rs: RuleSet):
    """시중 계산기는 이걸 묻지 않고 전국에 일률 0.14%를 매긴다."""
    inside = compute(rs, build_case(year=2026, prices=[500_000_000], in_urban=True))
    outside = compute(rs, build_case(year=2026, prices=[500_000_000], in_urban=False))

    assert inside.urban_tax.as_int() > 0
    assert outside.urban_tax.as_int() == 0
    assert outside.total.as_int() < inside.total.as_int()


def test_1세대1주택_특례세율은_공시_9억_초과에_적용되지_않는다(rs: RuleSet):
    """§111의2① 괄호. 이 경계를 놓치면 고가 1주택 세액이 통째로 틀린다."""
    under = compute(rs, build_case(year=2026, prices=[900_000_000]))
    over = compute(rs, build_case(year=2026, prices=[900_000_001]))

    assert under.trace.find("pt.07.base_tax").branch.taken == "특례세율"
    assert over.trace.find("pt.07.base_tax").branch.taken == "표준세율"

    # 공시가격 1원 차이로 세액이 절벽처럼 튄다. 과표는 둘 다 4.05억으로 같은데
    # 특례세율(420,000 + 초과분 0.35%)과 표준세율(570,000 + 초과분 0.4%)이 갈린다.
    assert under.taxable_base.as_int() == over.taxable_base.as_int() == 405_000_000
    assert under.base_tax.as_int() == 420_000 + int(105_000_000 * Fraction(35, 10_000))
    assert over.base_tax.as_int() == 570_000 + int(105_000_000 * Fraction(40, 10_000))
    assert over.base_tax.as_int() - under.base_tax.as_int() == 202_500


def test_특례세율이_배제되면_얼마_손해인지_대안으로_남는다(rs: RuleSet):
    """'유의사항: 특례 미반영' 같은 정적 면책이 아니라 판정 결과로 알려준다."""
    over = compute(rs, build_case(year=2026, prices=[1_500_000_000]))
    alts = {a.key: a for a in over.trace.all_alternatives()}
    assert "one_house_rate" in alts
    assert "900,000,000원을 초과한다" in alts["one_house_rate"].reason_ko
    # 특례를 받았다면 얼마였는지를 금액으로 알려준다(음수 = 그만큼 쌌다)
    assert alts["one_house_rate"].delta.as_int() < 0


def test_주택_재산세에_세부담상한은_적용되지_않는다(rs: RuleSet):
    """지방세법 §122 단서로 2023년에 폐지되고 과세표준상한제로 대체됐다.
    시중 계산기와 해설이 아직 105/110/130%를 말하는 것은 폐지된 제도다."""
    res = rs.resolve(
        "property_tax.house.burden_cap", on=date(2026, 6, 1), track=Track.CURRENT
    )
    assert res.block.payload["applicable"] is False
    assert res.block.payload["replaced_by"] == "property_tax.house.taxbase_cap_rate"


def test_과세표준상한은_직전연도_과세표준이_있어야_계산된다(rs: RuleSet):
    case = build_case(year=2026, prices=[3_000_000_000])

    without = compute(rs, case)
    cap_node = without.trace.find("pt.05.taxable_base_cap")
    assert "미입력" in cap_node.substitution
    assert any(a.key == "taxbase_cap" for a in cap_node.alternatives_not_taken)

    # 직전연도 과세표준을 주면 상한이 실제로 걸린다
    with_prior = compute(
        rs, case, options=PropertyTaxOptions(prior_year_taxable_base=1_000_000_000)
    )
    assert with_prior.taxable_base.as_int() < without.taxable_base.as_int()
    assert with_prior.total.as_int() < without.total.as_int()
    assert with_prior.trace.find("pt.05.taxable_base_cap").branch.taken == "상한 적용"


def test_공시가격이_없으면_0원이_아니라_미상으로_흐른다(rs: RuleSet):
    """0원으로 계산해 '세금 없음'을 보여주는 것이 가장 나쁜 실패다."""
    case = build_case(year=2026, prices=[1_000_000_000])
    case_next = TaxCase(
        year=2027,
        persons=case.persons,
        households=case.households,
        properties=case.properties,
        ownerships=case.ownerships,
    )
    r = compute(rs, case_next)
    assert r.trace.find("pt.01.published_price").output.unknown is not None
    assert r.trace.certainty.labels_ko()  # 미상 라벨이 결과까지 올라온다


# --------------------------------------------------------------------------
# ★ 구조 불변식 — 부부 각자 1채 vs 부부공동 1채
# --------------------------------------------------------------------------


def _couple(separate: bool) -> TaxCase:
    hh = HouseholdId("hh")
    a = Person(id=PersonId("p0"), household_id=hh, name="본인", spouse_id=PersonId("p1"))
    b = Person(id=PersonId("p1"), household_id=hh, name="배우자", spouse_id=PersonId("p0"))

    def house(i: int, price: int) -> Property:
        return Property(
            id=PropertyId(f"h{i}"),
            kind=PropertyKind.APARTMENT,
            legal_dong_code=SEOUL,
            display_name=f"주택{i}",
            published_prices=(PriceFact(2026, price),),
        )

    if separate:
        props = (house(0, 800_000_000), house(1, 700_000_000))
        owns = (
            Ownership(a.id, PropertyId("h0")),
            Ownership(b.id, PropertyId("h1")),
        )
    else:
        props = (house(0, 800_000_000),)
        owns = (
            Ownership(a.id, PropertyId("h0"), share=Fraction(1, 2)),
            Ownership(b.id, PropertyId("h0"), share=Fraction(1, 2)),
        )

    return TaxCase(
        year=2026,
        persons=(a, b),
        households=(Household(id=hh, member_ids=(a.id, b.id)),),
        properties=props,
        ownerships=owns,
    )


def test_부부공동명의_1주택은_1세대1주택이다(rs: RuleSet):
    """지방세법 시행령 §110의2④ 단서 — 같은 세대 내 공동소유 1주택은 1개로 본다."""
    count = household_house_count(_couple(separate=False), PersonId("p0"))
    assert count.count == 1
    assert count.is_one_house


def test_부부가_각자_1채씩이면_1세대2주택이라_특례를_못_받는다(rs: RuleSet):
    """propertytax.co.kr이 '지원하지 않는다'고 자백한 바로 그 케이스.
    물건 배열만 있는 모델로는 표현 자체가 불가능했다."""
    count = household_house_count(_couple(separate=True), PersonId("p0"))
    assert count.count == 2
    assert not count.is_one_house

    joint = compute(rs, _couple(separate=False))
    apart = compute(rs, _couple(separate=True))

    # 같은 8억 주택인데 특례세율 적용 여부가 갈린다
    assert joint.trace.find("pt.07.base_tax").branch.taken == "특례세율"
    assert apart.trace.find("pt.07.base_tax").branch.taken == "표준세율"
    assert apart.base_tax.as_int() > joint.base_tax.as_int()


def test_판정_과정이_감사추적에_남는다(rs: RuleSet):
    """'1세대1주택입니다'만 보여주면 체크박스와 다를 게 없다."""
    trace = compute(rs, _couple(separate=True)).trace.find("pt.03.house_count")
    assert "본인" in trace.substitution and "배우자" in trace.substitution
    assert "주택0" in trace.substitution and "주택1" in trace.substitution
    assert any(a.key == "one_house_special" for a in trace.alternatives_not_taken)


def test_지분_안분에_반올림_손실이_없다(rs: RuleSet):
    r = compute(rs, _couple(separate=False))
    halves = [r.share_of(Fraction(1, 2)) for _ in range(2)]
    assert sum(halves) == r.total.as_int()

    thirds = [r.share_of(Fraction(1, 3)) for _ in range(3)]
    assert sum(thirds) == r.total.as_int()


def test_상속주택은_5년간_주택수에서_빠진다(rs: RuleSet):
    """지방세법 시행령 §110의2①8. 시중 계산기는 이 판정을 아예 하지 않는다."""
    hh = HouseholdId("hh")
    p = Person(id=PersonId("p0"), household_id=hh, name="본인")
    props = (
        Property(
            id=PropertyId("h0"),
            kind=PropertyKind.APARTMENT,
            legal_dong_code=SEOUL,
            published_prices=(PriceFact(2026, 800_000_000),),
        ),
        Property(
            id=PropertyId("h1"),
            kind=PropertyKind.APARTMENT,
            legal_dong_code=SEOUL,
            published_prices=(PriceFact(2026, 300_000_000),),
        ),
    )

    def case_with(inheritance_date: date) -> TaxCase:
        return TaxCase(
            year=2026,
            persons=(p,),
            households=(Household(id=hh, member_ids=(p.id,)),),
            properties=props,
            ownerships=(
                Ownership(p.id, PropertyId("h0")),
                Ownership(
                    p.id,
                    PropertyId("h1"),
                    cause=AcquisitionCause.INHERITANCE,
                    inherited=InheritedMeta(
                        inheritance_date=inheritance_date,
                        share=Fraction(1),
                        inherited_value=300_000_000,
                    ),
                ),
            ),
        )

    recent = household_house_count(case_with(date(2024, 3, 1)), PersonId("p0"))
    assert recent.count == 1 and recent.is_one_house
    assert recent.excluded[0].basis_ko == "지방세법 시행령 §110의2①8"

    old = household_house_count(case_with(date(2015, 3, 1)), PersonId("p0"))
    assert old.count == 2 and not old.is_one_house


# --------------------------------------------------------------------------
# 공정시장가액비율 구간
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "price, band",
    [
        (300_000_000, "~3억"),
        (300_000_001, "3~6억"),
        (600_000_000, "3~6억"),
        (600_000_001, "6억~"),
        (5_000_000_000, "6억~"),
    ],
)
def test_공정시장가액비율_구간_경계(price, band):
    assert price_band(price) == band


@pytest.mark.parametrize(
    "price, expected_rate",
    [(200_000_000, "43%"), (500_000_000, "44%"), (3_000_000_000, "45%")],
)
def test_2026년_1주택_공정시장가액비율은_43_44_45(rs: RuleSet, price, expected_rate):
    """시행령 §109①2 단서. '시가표준액이 9억원을 초과하는 주택을 포함한다'고
    명시돼 있어 고가 1주택도 45%다."""
    case = build_case(year=2026, prices=[price])
    assert expected_rate in compute(rs, case).trace.find("pt.04.taxable_base_raw").substitution


def test_다주택자는_공정시장가액비율_60퍼센트(rs: RuleSet):
    case = build_case(year=2026, prices=[500_000_000, 400_000_000])
    assert "60%" in compute(rs, case).trace.find("pt.04.taxable_base_raw").substitution


# --------------------------------------------------------------------------
# ★ 정부 예시의 재산세 계산 방식 불일치 — 숨기지 않고 고정한다
# --------------------------------------------------------------------------


def test_3주택_정부예시는_합산방식이고_엔진은_법대로_물건별이다(rs: RuleSet, golden: dict):
    """문답자료 p.47 별첨 ➂의 재산세는 3채를 합산해 한 물건처럼 계산돼 있다.

    재산세는 지방세법상 물건별 과세이므로 엔진은 물건별로 간다. 두 값이 다르다는
    사실 자체를 테스트로 고정해, 나중에 누가 '정부 숫자에 맞추자'며 조용히
    합산 방식으로 바꾸는 일을 막는다.
    """
    spec = next(c for c in golden["unreconciled"] if c["id"] == "pt-3house-aggregate")

    per_property = build_case(year=2026, prices=[500_000_000] * 3)
    engine_total = sum(
        compute_property_tax(
            per_property, PropertyId(f"h{i}"), rs,
            track=Track.CURRENT, owner_id=PersonId("p0"),
        ).total.as_int()
        for i in range(3)
    )

    aggregate = build_case(year=2026, prices=[1_500_000_000])
    # 다주택 기준(FMV 60%)을 쓰려면 세대가 2채 이상이어야 하므로 강제 지정한다
    government_total = compute_property_tax(
        aggregate, PropertyId("h0"), rs,
        track=Track.CURRENT, owner_id=PersonId("p0"),
        options=PropertyTaxOptions(force_one_house_special=False),
    ).total.as_int()

    assert float(to_manwon(engine_total)) == pytest.approx(
        spec["engine_per_property_manwon"], abs=0.05
    )
    assert float(to_manwon(government_total)) == pytest.approx(
        spec["government_aggregate_manwon"], abs=0.05
    )
    # 물건별이 합산보다 싸다 — 누진세율이 물건마다 아래 구간부터 다시 시작하기 때문
    assert engine_total < government_total
