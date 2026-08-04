"""종합부동산세 엔진 테스트.

★ 게이트: 재정경제부 세제개편안 문답자료 p.44의 값을 재현한다.

문답자료는 재산세와 달리 종부세를 **직접 인쇄**한다. 역산이 아니라 정부가 찍은
숫자 그 자체와 대조하는 것이므로, 여기가 이 엔진의 가장 강한 검증이다.

  사례① 시가45억(공시30억), 70세 10년 거주 → 세액공제 516.5 / 종부세 154.9 / 보유세 916.3
  사례③ 시가70억(공시50억), 70세 10년 거주 → 세액공제 1,562.9 / 종부세 468.9 / 보유세 1,788.3
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction

import pytest

from realestate_tax.domain import (
    Household,
    HouseholdId,
    Ownership,
    Person,
    PersonId,
    PersonType,
    PriceFact,
    Property,
    PropertyId,
    PropertyKind,
    ResidenceSpell,
    TaxCase,
)
from realestate_tax.engine.jongbuse import JongbuseOptions, compute_jongbuse
from realestate_tax.engine.trace import to_manwon
from realestate_tax.rules import RuleSet, Track, default_ruleset_root

SEOUL = "1168010100"


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


def one_house_case(
    *,
    year: int = 2026,
    price: int,
    birth: date | None = None,
    resides_since: date | None = None,
) -> TaxCase:
    hh = HouseholdId("hh")
    person = Person(
        id=PersonId("p0"), household_id=hh, name="본인", birth_date=birth
    )
    prop = Property(
        id=PropertyId("h0"),
        kind=PropertyKind.APARTMENT,
        legal_dong_code=SEOUL,
        display_name="본가",
        published_prices=(PriceFact(year, price),),
    )
    residences = (
        (ResidenceSpell(person.id, prop.id, start=resides_since),)
        if resides_since
        else ()
    )
    return TaxCase(
        year=year,
        persons=(person,),
        households=(Household(id=hh, member_ids=(person.id,)),),
        properties=(prop,),
        ownerships=(Ownership(person.id, prop.id, share=Fraction(1)),),
        residences=residences,
    )


def run(rs: RuleSet, case: TaxCase, **kw):
    return compute_jongbuse(
        case, PersonId("p0"), rs, track=kw.pop("track", Track.CURRENT), **kw
    )


# --------------------------------------------------------------------------
# ★ 게이트 — 정부 문답자료 p.44 재현
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "price, expected_credit, expected_jongbuse, expected_holding, label",
    [
        (
            3_000_000_000,
            516.5,
            154.9,
            916.3,
            "문답자료 p.44 사례① 시가45억(공시30억) 70세 10년 거주",
        ),
        (
            5_000_000_000,
            1562.9,
            468.9,
            1788.3,
            "문답자료 p.44 사례③ 시가70억(공시50억) 70세 10년 거주",
        ),
    ],
)
def test_문답자료_p44의_종부세를_재현한다(
    rs: RuleSet, price, expected_credit, expected_jongbuse, expected_holding, label
):
    case = one_house_case(
        year=2026, price=price, birth=date(1956, 1, 1), resides_since=date(2016, 1, 1)
    )
    result = run(
        rs,
        case,
        options=JongbuseOptions(
            holding_years=10, residence_years=10, resides_in_main_house=True
        ),
    )

    credit = float(to_manwon(result.tax_credit.as_int()))
    jongbuse = float(to_manwon(result.total.as_int()))
    holding = float(to_manwon(result.holding_tax_total))

    detail = (
        f"\n{label}"
        f"\n  과세표준   {result.taxable_base.as_int():,}"
        f"\n  종부세액   {result.gross_tax.as_int():,}"
        f"\n  재산세공제 {result.property_tax_credit.as_int():,}"
        f"\n  세액공제   {result.tax_credit.as_int():,}  (기대 {expected_credit}만원)"
        f"\n  결정세액   {result.net_tax.as_int():,}"
        f"\n  종부세계   {result.total.as_int():,}  (기대 {expected_jongbuse}만원)"
        f"\n  재산세     {result.property_tax_total.as_int():,}"
        f"\n  보유세     {result.holding_tax_total:,}  (기대 {expected_holding}만원)"
    )
    assert credit == pytest.approx(expected_credit, abs=0.1), detail
    assert jongbuse == pytest.approx(expected_jongbuse, abs=0.1), detail
    assert holding == pytest.approx(expected_holding, abs=0.1), detail


def test_재산세_공제가_감사추적에_산식과_함께_남는다(rs: RuleSet):
    """공제 산식은 정부가 계산 과정을 공개하지 않은 부분이다.
    재현으로 확인한 해석을 화면에 드러내야 사용자가 검증할 수 있다."""
    case = one_house_case(price=3_000_000_000, birth=date(1956, 1, 1))
    trace = run(rs, case, options=JongbuseOptions(holding_years=10)).trace
    ptc = trace.find("jb.09.property_tax_credit")

    assert "1,944,000" in ptc.substitution
    assert "한계세율" in ptc.note_ko
    # 근거가 assumed로 표시돼 화면에 "가정" 배지가 뜬다
    assert "가정" in ptc.certainty.labels_ko()


# --------------------------------------------------------------------------
# 개편안 트랙 — 4년 타임라인
# --------------------------------------------------------------------------


def test_거주_여부만으로_기본공제가_5억_갈린다(rs: RuleSet):
    """개편안의 핵심 전환. 같은 사람·같은 집인데 거기 사느냐로 14억/9억이 갈린다."""
    case = one_house_case(year=2027, price=3_000_000_000, birth=date(1956, 1, 1))

    lives = run(
        rs, case, track=Track.REFORM,
        options=JongbuseOptions(resides_in_main_house=True, residence_years=10),
    )
    away = run(
        rs, case, track=Track.REFORM,
        options=JongbuseOptions(resides_in_main_house=False, holding_years=10),
    )

    assert lives.trace.find("jb.06.basic_deduction").output.as_int() == 1_400_000_000
    assert away.trace.find("jb.06.basic_deduction").output.as_int() == 900_000_000
    assert away.total.as_int() > lives.total.as_int()


def test_거주_여부를_모르면_유리한_쪽으로_가정하지_않는다(rs: RuleSet):
    """모를 때 14억을 주면 세액을 과소평가해 사용자를 오도한다."""
    case = one_house_case(year=2027, price=3_000_000_000)
    unknown = run(rs, case, track=Track.REFORM, options=JongbuseOptions())
    assert unknown.trace.find("jb.06.basic_deduction").output.as_int() == 900_000_000


def test_개편안_결과에는_국회_미통과_배지가_붙는다(rs: RuleSet):
    """확실성 최솟값 하나만 보면 '국회 미통과'가 '가정'에 가려진다.
    우려 사항은 전부 나열되어야 사용자가 무엇을 조심할지 안다."""
    case = one_house_case(year=2027, price=3_000_000_000)
    reform = run(rs, case, track=Track.REFORM, options=JongbuseOptions(holding_years=10))

    concerns = dict(reform.trace.certainty_concerns())
    assert "국회 미통과" in concerns
    # 개편안 근거로 계산한 첫 단계. 과세대상 판정(§7① 신설)이 기본공제보다 앞선다.
    assert concerns["국회 미통과"] in (
        "과세대상 판정", "기본공제", "과세표준", "주택분 종합부동산세액"
    )
    # 재산세 공제 산식은 정부가 공개하지 않아 재현으로 확인한 해석이다 — 따로 표시된다
    assert "가정" in concerns
    assert "국회 통과 전" in reform.trace.note_ko

    current = run(
        rs, one_house_case(price=3_000_000_000), options=JongbuseOptions(holding_years=10)
    )
    assert "국회 미통과" not in dict(current.trace.certainty_concerns())


def test_2027년_세액공제_금액한도_800만원이_적용된다(rs: RuleSet):
    """개편안 신설. 공제율 80%를 채워도 금액 상한에 잘린다."""
    case = one_house_case(
        year=2027, price=5_000_000_000, birth=date(1956, 1, 1), resides_since=date(2016, 1, 1)
    )
    r = run(
        rs, case, track=Track.REFORM,
        options=JongbuseOptions(
            residence_years=10, holding_years=10, resides_in_main_house=True
        ),
    )
    assert r.tax_credit.as_int() == 8_000_000
    alts = {a.key for a in r.trace.all_alternatives()}
    assert "credit_amount_cap" in alts


def test_2027년은_보유공제_절반과_거주공제_중_높은_쪽을_쓴다(rs: RuleSet):
    """개조식 p.19의 과도기 규칙. 10년 보유(20%) vs 10년 거주(40%) → 40%."""
    case = one_house_case(year=2027, price=2_000_000_000, birth=date(1990, 1, 1))
    lived = run(
        rs, case, track=Track.REFORM,
        options=JongbuseOptions(holding_years=10, residence_years=10, resides_in_main_house=True),
    )
    only_held = run(
        rs, case, track=Track.REFORM,
        options=JongbuseOptions(holding_years=10, residence_years=0, resides_in_main_house=True),
    )
    assert "높은 쪽" in lived.trace.find("jb.10.tax_credit").branch.taken
    assert lived.tax_credit.as_int() > only_held.tax_credit.as_int()


def test_2028년부터는_보유공제가_사라지고_거주공제만_남는다(rs: RuleSet):
    case = one_house_case(year=2028, price=2_000_000_000, birth=date(1990, 1, 1))
    held_only = run(
        rs, case, track=Track.REFORM,
        options=JongbuseOptions(holding_years=15, residence_years=0, resides_in_main_house=True),
    )
    assert held_only.tax_credit.as_int() == 0


def test_연도별_타임라인이_같은_사건에서_뽑힌다(rs: RuleSet):
    """이 서비스의 실제 화면은 '올해 얼마'가 아니라 '4년간 어떻게 변하나'다."""
    timeline = {}
    for year in (2026, 2027, 2028):
        case = one_house_case(
            year=year, price=3_000_000_000, birth=date(1956, 1, 1), resides_since=date(2016, 1, 1)
        )
        track = Track.CURRENT if year == 2026 else Track.REFORM
        timeline[year] = run(
            rs, case, track=track,
            options=JongbuseOptions(
                holding_years=10, residence_years=10, resides_in_main_house=True
            ),
        ).total.as_int()

    # 거주 1주택은 개편안에서 보호받는 쪽이지만 공정시장가액비율 인상으로 오른다
    assert timeline[2026] < timeline[2027] < timeline[2028]


# --------------------------------------------------------------------------
# 구조
# --------------------------------------------------------------------------


def test_다주택자는_세액공제를_받지_못하고_사유가_남는다(rs: RuleSet):
    hh = HouseholdId("hh")
    p = Person(id=PersonId("p0"), household_id=hh, name="본인", birth_date=date(1950, 1, 1))
    props = tuple(
        Property(
            id=PropertyId(f"h{i}"),
            kind=PropertyKind.APARTMENT,
            legal_dong_code=SEOUL,
            published_prices=(PriceFact(2026, 800_000_000),),
        )
        for i in range(2)
    )
    case = TaxCase(
        year=2026,
        persons=(p,),
        households=(Household(id=hh, member_ids=(p.id,)),),
        properties=props,
        ownerships=tuple(Ownership(p.id, pr.id) for pr in props),
    )
    r = run(rs, case, options=JongbuseOptions(holding_years=20))
    assert r.tax_credit.as_int() == 0
    alts = {a.key: a for a in r.trace.all_alternatives()}
    assert "one_house_credit" in alts
    assert "1세대1주택자가 아니다" in alts["one_house_credit"].reason_ko


def test_부부공동명의_1주택은_특례_안내가_대안으로_뜬다(rs: RuleSet):
    """세대 주택은 1채인데 단독 소유가 아니라 §10의2 신청이 필요하다.
    시중 계산기는 이 특례를 '미반영'으로 두고 사용자에게 알리지 않는다."""
    hh = HouseholdId("hh")
    a = Person(id=PersonId("p0"), household_id=hh, name="본인", spouse_id=PersonId("p1"))
    b = Person(id=PersonId("p1"), household_id=hh, name="배우자", spouse_id=PersonId("p0"))
    prop = Property(
        id=PropertyId("h0"),
        kind=PropertyKind.APARTMENT,
        legal_dong_code=SEOUL,
        published_prices=(PriceFact(2026, 2_000_000_000),),
    )
    case = TaxCase(
        year=2026,
        persons=(a, b),
        households=(Household(id=hh, member_ids=(a.id, b.id)),),
        properties=(prop,),
        ownerships=(
            Ownership(a.id, prop.id, share=Fraction(1, 2)),
            Ownership(b.id, prop.id, share=Fraction(1, 2)),
        ),
    )
    r = run(rs, case)
    alts = {a_.key: a_ for a_ in r.trace.all_alternatives()}
    assert "joint_spouse_special" in alts
    assert alts["joint_spouse_special"].actionable


def test_세부담상한은_직전연도_보유세가_있어야_적용된다(rs: RuleSet):
    case = one_house_case(price=5_000_000_000, birth=date(1956, 1, 1))

    without = run(rs, case, options=JongbuseOptions(holding_years=10))
    assert "미입력" in without.trace.find("jb.11.burden_cap").substitution
    assert any(a.key == "burden_cap" for a in without.trace.all_alternatives())

    with_prior = run(
        rs, case,
        options=JongbuseOptions(holding_years=10, prior_year_total_tax=10_000_000),
    )
    assert with_prior.trace.find("jb.11.burden_cap").branch.taken == "상한 적용"
    assert with_prior.total.as_int() < without.total.as_int()


def test_법인은_기본공제가_없고_세부담상한도_받지_못한다(rs: RuleSet):
    corp = Person(id=PersonId("p0"), type=PersonType.CORPORATION, name="법인")
    prop = Property(
        id=PropertyId("h0"),
        kind=PropertyKind.APARTMENT,
        legal_dong_code=SEOUL,
        published_prices=(PriceFact(2026, 1_000_000_000),),
    )
    case = TaxCase(
        year=2026,
        persons=(corp,),
        properties=(prop,),
        ownerships=(Ownership(corp.id, prop.id),),
    )
    r = run(rs, case, options=JongbuseOptions(prior_year_total_tax=1_000))
    assert r.trace.find("jb.06.basic_deduction").output.as_int() == 0
    assert "법인" in r.trace.find("jb.11.burden_cap").substitution


def test_모든_숫자에_근거_조문이_붙는다(rs: RuleSet):
    case = one_house_case(price=3_000_000_000, birth=date(1956, 1, 1))
    cites = {
        r.basis.cite_ko()
        for r in run(rs, case, options=JongbuseOptions(holding_years=10)).trace.all_rules()
        if r.basis
    }
    assert "종합부동산세법 제8조 제1항" in cites
    assert "종합부동산세법 제9조 제1항" in cites
    assert "종합부동산세법 시행령 제4-3조 제1항" in cites
    assert "농어촌특별세법 제5조 제1항" in cites


def test_세율표의_기초금액이_누적계산과_일치한다(rs: RuleSet):
    """룰셋의 base_amount는 법문에서 옮긴 값이다. 세율·구간만으로 누적 계산한
    결과와 어긋나면 옮겨 적다 틀린 것이다. 데이터 자체를 교차검증한다."""
    from realestate_tax.rules.schema import Bracket, RateTable

    for rule_id in ("jongbuse.house.rate_table", "property_tax.house.rate_table_standard"):
        for block in rs.rule(rule_id).blocks:
            table = block.table
            assert table is not None
            stripped = RateTable(
                tuple(Bracket(upto=b.upto, rate=b.rate) for b in table.brackets)
            )
            for base in (1, 250_000_000, 500_000_000, 1_000_000_000,
                         2_000_000_000, 4_000_000_000, 8_000_000_000, 20_000_000_000):
                assert table.tax_for(base)[0] == stripped.tax_for(base)[0], (
                    f"{rule_id}#{block.id} 과세표준 {base:,}에서 "
                    f"기초금액 방식과 누적 방식이 어긋난다"
                )


# --------------------------------------------------------------------------
# ★ 과세대상 문턱 — 개편안 §7① 신설 (2026-08-04 감사에서 발견)
#   개조식 p.20이 '과세대상 조정'과 '기본공제금액 조정'을 별개 항목으로 적는다.
#   룰셋에 기본공제만 있어서, 과세대상이 아닌 사람에게 세금을 매기고 있었다.
# --------------------------------------------------------------------------


def test_비거주_1주택_12억은_개편안에서_과세대상이_아니다(rs: RuleSet):
    """문턱 14억 / 기본공제 9억이라 **9~14억 구간에서 둘이 어긋난다.**
    기본공제만 보면 3억이 남아 세금이 나오지만, 법대로는 과세대상이 아니다."""
    case = one_house_case(year=2027, price=1_200_000_000)  # 거주 이력 없음
    r = run(rs, case, track=Track.REFORM, options=JongbuseOptions(holding_years=10))

    assert r.total.as_int() == 0, "과세대상이 아닌데 종부세가 나왔다"
    n = r.trace.find("jb.05b.taxable_threshold")
    assert n is not None and n.branch.taken == "비해당"
    assert "1,400,000,000" in n.substitution
    assert "재산세는 그대로" in (n.note_ko or "")
    assert r.property_tax_total.as_int() > 0, "재산세까지 사라지면 안 된다"


def test_문턱을_넘으면_그대로_계산된다(rs: RuleSet):
    case = one_house_case(year=2027, price=2_000_000_000)
    r = run(rs, case, track=Track.REFORM, options=JongbuseOptions(holding_years=10))
    assert r.trace.find("jb.05b.taxable_threshold").branch.taken == "해당"
    assert r.total.as_int() > 0


def test_거주_1주택은_문턱과_기본공제가_같아_경계가_생기지_않는다(rs: RuleSet):
    """둘 다 14억이라 우연히 일치한다. 이 사실을 테스트로 고정해 둔다 —
    나중에 한쪽만 바뀌면 여기서 깨진다."""
    case = one_house_case(
        year=2027, price=1_390_000_000, resides_since=date(2015, 1, 1)
    )
    r = run(
        rs, case, track=Track.REFORM,
        options=JongbuseOptions(holding_years=10, resides_in_main_house=True),
    )
    assert r.total.as_int() == 0
    assert r.trace.find("jb.06.basic_deduction") is None  # 문턱에서 끝났다


def test_현행법에는_문턱_단계가_없다(rs: RuleSet):
    """현행은 기본공제가 곧 문턱이다. 규칙 부재를 '문턱 0원'으로 오해하면
    전원이 과세대상에서 빠진다 — 그래서 규칙이 없으면 단계를 건너뛴다."""
    case = one_house_case(year=2026, price=1_200_000_000)
    r = run(rs, case, options=JongbuseOptions(holding_years=10))
    assert r.trace.find("jb.05b.taxable_threshold") is None
    assert r.trace.find("jb.06.basic_deduction") is not None


def test_직전연도_보유세가_0원이면_상한이_세액을_0으로_만들지_않는다(rs: RuleSet):
    """작년 과세기준일(6/1) 이후 취득이면 작년 고지서가 0원이다.
    사실대로 0을 입력했더니 상한이 0 × 150% = 0이 되어 종부세가 통째로 사라졌다.

    세부담 상한은 **급증을 막는 장치**이지 신규 취득자를 면세하는 장치가 아니다."""
    case = one_house_case(year=2026, price=5_000_000_000, birth=date(1960, 1, 1))
    zero_prior = run(
        rs, case,
        options=JongbuseOptions(holding_years=1, prior_year_total_tax=0),
    )
    no_input = run(rs, case, options=JongbuseOptions(holding_years=1))

    assert zero_prior.total.as_int() > 0, "상한이 세액을 0으로 붕괴시켰다"
    assert zero_prior.total.as_int() == no_input.total.as_int()

    n = zero_prior.trace.find("jb.11.burden_cap")
    assert n.branch.taken == "판정 불가"
    assert "면세하는 장치가 아니" in n.note_ko
    # 판정 불가라는 사실이 결과 배지에 올라온다
    assert "판단 필요" in dict(zero_prior.trace.certainty_concerns())


def test_직전연도_보유세가_있으면_상한이_정상_작동한다(rs: RuleSet):
    """0원 방어가 상한 기능 자체를 죽이면 안 된다."""
    case = one_house_case(year=2026, price=5_000_000_000, birth=date(1960, 1, 1))
    capped = run(
        rs, case,
        options=JongbuseOptions(holding_years=10, prior_year_total_tax=3_000_000),
    )
    uncapped = run(rs, case, options=JongbuseOptions(holding_years=10))
    assert capped.total.as_int() < uncapped.total.as_int()
    assert capped.trace.find("jb.11.burden_cap").branch.taken == "상한 적용"


# --------------------------------------------------------------------------
# ★ 세액공제 안분 — 종부세법 §9⑦⑨ (2026-08-04 감사)
#   "…에 해당하는 산출세액(공시가격합계액으로 안분하여 계산한 금액)을
#    제외한 금액에 … 공제율을 곱한 금액"
# --------------------------------------------------------------------------


def inherited_pair_case(year: int = 2026) -> TaxCase:
    """본인 주택 20억 + 상속 5년 미경과 주택 20억. 76세."""
    from realestate_tax.domain import AcquisitionCause, InheritedMeta

    hh = HouseholdId("hh")
    p = Person(id=PersonId("p0"), household_id=hh, name="본인", birth_date=date(1950, 1, 1))
    props = tuple(
        Property(
            id=PropertyId(name), kind=PropertyKind.APARTMENT, legal_dong_code=SEOUL,
            display_name=name, published_prices=(PriceFact(year, 2_000_000_000),),
        )
        for name in ("본가", "상속집")
    )
    return TaxCase(
        year=year,
        persons=(p,),
        households=(Household(id=hh, member_ids=(p.id,)),),
        properties=props,
        ownerships=(
            Ownership(p.id, PropertyId("본가"), acquired_on=date(year - 16, 1, 1)),
            Ownership(
                p.id, PropertyId("상속집"),
                acquired_on=date(2024, 1, 1),
                cause=AcquisitionCause.INHERITANCE,
                inherited=InheritedMeta(
                    inheritance_date=date(2024, 1, 1),
                    share=Fraction(1),
                    inherited_value=2_000_000_000,
                ),
            ),
        ),
        residences=(ResidenceSpell(p.id, PropertyId("본가"), start=date(year - 16, 1, 1)),),
    )


def test_상속주택분_산출세액은_세액공제_기초에서_빠진다(rs: RuleSet):
    """공시가격이 5:5면 공제 기초도 절반이 된다.
    전액에 80%를 곱하면 상속주택분까지 공제받아 세액이 3배 어긋난다."""
    case = inherited_pair_case()
    r = run(rs, case, options=JongbuseOptions(holding_years=16, residence_years=16))

    n = r.trace.find("jb.10.tax_credit")
    assert "특례주택분 제외" in n.substitution
    assert "1/2" in n.substitution
    assert "§9⑦⑨" in (n.note_ko or "")

    # 안분 없이 계산하면 공제가 정확히 2배가 된다
    after_ptc = r.gross_tax.as_int() - r.property_tax_credit.as_int()
    assert r.tax_credit.as_int() < int(after_ptc * Fraction(8, 10))


def test_특례주택이_없으면_안분하지_않는다(rs: RuleSet):
    """안분 로직이 일반 사례를 건드리면 골든이 깨진다."""
    case = one_house_case(price=3_000_000_000, birth=date(1956, 1, 1))
    n = run(rs, case, options=JongbuseOptions(holding_years=10)).trace.find(
        "jb.10.tax_credit"
    )
    assert "특례주택분 제외" not in n.substitution
    assert not n.note_ko


# --------------------------------------------------------------------------
# ★ 공익법인등 — 종부세법 §8①2·3호 (2026-08-04 감사)
#   기본공제 0원과 단일세율은 "§9②3호 세율이 적용되는 법인"에 한정된다.
# --------------------------------------------------------------------------


def corp_case(kind: PersonType, price: int = 2_000_000_000) -> TaxCase:
    hh = HouseholdId("hh")
    p = Person(id=PersonId("p0"), household_id=hh, name="법인", type=kind)
    prop = Property(
        id=PropertyId("h0"), kind=PropertyKind.APARTMENT, legal_dong_code=SEOUL,
        display_name="사택", published_prices=(PriceFact(2026, price),),
    )
    return TaxCase(
        year=2026, persons=(p,),
        households=(Household(id=hh, member_ids=(p.id,)),),
        properties=(prop,),
        ownerships=(Ownership(p.id, prop.id, share=Fraction(1)),),
    )


def test_공익법인등은_기본공제_9억을_받는다(rs: RuleSet):
    """§8①3호 "제1호 및 제2호에 해당하지 아니하는 자: 9억원".
    '법인이니까 0원'으로 뭉개면 과세표준이 5.4억(FMV 60%) 더 잡힌다."""
    r = run(rs, corp_case(PersonType.CORPORATION_PROGRESSIVE))
    assert r.trace.find("jb.06.basic_deduction").output.as_int() == 900_000_000


def test_일반법인은_기본공제가_0원이다(rs: RuleSet):
    """방어 로직이 일반 법인까지 바꾸면 안 된다."""
    r = run(rs, corp_case(PersonType.CORPORATION))
    assert r.trace.find("jb.06.basic_deduction").output.as_int() == 0


def test_공익법인등은_누진세율을_쓰고_세부담상한도_받는다(rs: RuleSet):
    """§9①(누진) + §10(상한). 단일세율·상한 미적용은 §9②3호 법인만이다."""
    prog = run(
        rs, corp_case(PersonType.CORPORATION_PROGRESSIVE),
        options=JongbuseOptions(prior_year_total_tax=2_000_000),
    )
    assert prog.trace.find("jb.11.burden_cap").branch is not None  # 상한 판정이 돌았다

    general = run(rs, corp_case(PersonType.CORPORATION))
    assert "미적용" in general.trace.find("jb.11.burden_cap").substitution
    # 기본공제 9억 + 누진세율이라 공익법인등이 훨씬 적게 낸다
    assert prog.total.as_int() < general.total.as_int()


def test_법인은_1세대1주택자가_될_수_없다(rs: RuleSet):
    """세대가 없다. 1채만 가졌다고 12억 공제가 나가면 안 된다."""
    for kind in (PersonType.CORPORATION, PersonType.CORPORATION_PROGRESSIVE):
        r = run(rs, corp_case(kind))
        assert r.trace.find("jb.06.basic_deduction").output.as_int() != 1_200_000_000
