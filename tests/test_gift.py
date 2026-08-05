"""배우자 증여 — 비용 계산과 절세 전략.

이 묶음이 지키는 것은 하나다. **절감만 세지 않는다.**
증여세·취득세는 즉시 나가고 절감은 해마다 돌아온다. 둘을 같은 화면에 놓지 않으면
조언이 아니라 유인이 된다.
"""

from __future__ import annotations

from datetime import date

import pytest

from realestate_tax.domain import (
    Household,
    HouseholdId,
    Ownership,
    Person,
    PersonId,
    PriceFact,
    Property,
    PropertyId,
    PropertyKind,
    TaxCase,
)
from realestate_tax.engine.gift import carryover_years, compute_spouse_gift_cost
from realestate_tax.engine.strategy import consult
from realestate_tax.rules import RuleSet, default_ruleset_root
from realestate_tax.rules.resolver import load_ruleset

ON = date(2026, 6, 1)
ME = PersonId("me")
SPOUSE = PersonId("sp")


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return load_ruleset(default_ruleset_root())


# --------------------------------------------------------------------------
# 조문 재현 — 상증세법 §53 §56(→§26) §69, 지방세법 §11①2
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gift, expected_base, expected_gift_tax",
    [
        # 6억 이하는 배우자 공제로 과세표준 0 → 증여세 0
        (500_000_000, 0, 0),
        (600_000_000, 0, 0),
        # 9억 → 과표 3억 → 1천만 + (3억−1억)×20% = 5천만 → 신고세액공제 3%
        (900_000_000, 300_000_000, 48_500_000),
        # 12.5억 → 과표 6.5억 → 9천만 + (6.5억−5억)×30% = 1.35억 → ×0.97
        (1_250_000_000, 650_000_000, 130_950_000),
        # 40억 → 과표 34억 → 10.4억 + (34억−30억)×50% = 12.4억 → ×0.97
        (4_000_000_000, 3_400_000_000, 1_202_800_000),
    ],
)
def test_증여세가_조문_그대로_계산된다(rs: RuleSet, gift, expected_base, expected_gift_tax):
    """세율표는 상증세법 §26(§56이 준용)이고 공제는 §53①1호 6억,
    신고세액공제는 §69② 3%다. 전부 2026-08-05 법제처 XML 원문 확인."""
    cost = compute_spouse_gift_cost(gift, rs, on=ON)
    assert cost.taxable_base == expected_base
    assert cost.gift_tax == expected_gift_tax


def test_무상취득_취득세는_3_5퍼센트다(rs: RuleSet):
    """지방세법 §11①2 "제1호 외의 무상취득: 1천분의 35"."""
    cost = compute_spouse_gift_cost(1_000_000_000, rs, on=ON)
    assert cost.acquisition_tax == 35_000_000


def test_조정지역_중과는_판정하지_않고_금액을_보여준다(rs: RuleSet):
    """'일정가액'이 시행령 소관이라 엔진이 판정하지 않는다.
    확인 못 한 것을 확인한 척하지 않되, 해당되면 얼마인지는 알려준다."""
    cost = compute_spouse_gift_cost(1_000_000_000, rs, on=ON)
    alts = {a.key: a for a in cost.trace.all_alternatives()}
    assert "gift_acquisition_heavy" in alts
    assert alts["gift_acquisition_heavy"].actionable
    # 12% = 표준 4%(§11①7나) + 중과기준세율 2% × 400%
    assert alts["gift_acquisition_heavy"].delta.as_int() == 120_000_000 - 35_000_000


def test_이월과세는_5년이_아니라_10년이다(rs: RuleSet):
    """2023.12.31 개정으로 5년 → 10년이 됐다(소득세법 §97의2①).
    기억으로 5년이라 쓰면 틀린다."""
    assert carryover_years(rs, on=ON) == 10


def test_기공제액이_있으면_공제가_줄어든다(rs: RuleSet):
    """§53① 후단 — 10년간 합산 한도다."""
    fresh = compute_spouse_gift_cost(900_000_000, rs, on=ON)
    used = compute_spouse_gift_cost(900_000_000, rs, on=ON, prior_gifts_10y=400_000_000)
    assert used.deduction == 200_000_000
    assert used.gift_tax > fresh.gift_tax


# --------------------------------------------------------------------------
# 전략 — 비용을 빼고도 이득인가
# --------------------------------------------------------------------------


def _case(prices: tuple[int, ...]) -> TaxCase:
    props = tuple(
        Property(
            id=PropertyId(f"h{i}"), display_name=f"주택{i}",
            kind=PropertyKind.APARTMENT, legal_dong_code="2647010100",
            published_prices=(PriceFact(2026, v),),
        )
        for i, v in enumerate(prices)
    )
    return TaxCase(
        year=2026,
        persons=(
            Person(id=ME, birth_date=date(1970, 1, 1), household_id=HouseholdId("hh"), spouse_id=SPOUSE),
            Person(id=SPOUSE, birth_date=date(1972, 1, 1), household_id=HouseholdId("hh"), spouse_id=ME),
        ),
        households=(Household(id=HouseholdId("hh"), member_ids=(ME, SPOUSE)),),
        properties=props,
        ownerships=tuple(
            Ownership(person_id=ME, property_id=p.id, acquired_on=date(2012, 5, 1)) for p in props
        ),
    )


def _gift_strategy(prices: tuple[int, ...], rs: RuleSet):
    found = [s for s in consult(_case(prices), ME, rs).strategies if s.key == "spouse_gift"]
    return found[0] if found else None


def test_증여는_공제_한도에_맞춰_제안된다(rs: RuleSet):
    """★ 처음엔 지분 절반을 통째로 넘기게 짜서 증여세가 터졌고, 모든 사례가
    '손해'로 찍혔다. 실무가 6억에 맞춰 나누는 이유가 그것이다 —
    공제 안에서는 증여세 0원이고 종부세 분산 효과는 그대로다."""
    s = _gift_strategy((1_500_000_000, 1_200_000_000, 1_000_000_000), rs)
    assert s is not None
    # 6억까지만 증여 → 증여세 0, 취득세만 3.5%
    assert s.upfront_cost == int(600_000_000 * 0.035)


def test_다주택자는_증여가_몇_년이면_회수된다(rs: RuleSet):
    """종부세는 인별 과세다. 한 사람에게 몰린 공시가격을 나누면 각자 기본공제를
    받고 누진 구간도 낮아진다 — 다주택자에게 특히 크다."""
    s = _gift_strategy((1_500_000_000, 1_200_000_000, 1_000_000_000), rs)
    assert s.annual_saving > 0
    assert s.payback_years is not None and s.payback_years < 5


def test_1주택자는_증여가_오히려_손해다(rs: RuleSet):
    """단독명의 1세대1주택자가 누리던 연령·거주 세액공제(최대 80%)를 잃는다.
    "증여하면 무조건 절세"라는 통념과 반대이므로 반드시 잡아야 한다."""
    s = _gift_strategy((5_000_000_000,), rs)
    assert s is not None
    assert s.annual_saving <= 0, "1주택자에게 증여가 이득으로 나왔다"
    assert any("1주택자는 대개 손해" in c for c in s.caveats_ko)


def test_비용과_이월과세를_반드시_말한다(rs: RuleSet):
    """절감만 크게 써 놓고 비용을 빼면 조언이 아니라 유인이다."""
    s = _gift_strategy((1_500_000_000, 1_200_000_000, 1_000_000_000), rs)
    joined = " ".join(s.caveats_ko)
    assert "즉시 나갑니다" in joined
    assert "이월과세" in joined and "10년" in joined
    assert "10년간 합산" in joined


def test_배우자가_없으면_제안하지_않는다(rs: RuleSet):
    case = TaxCase(
        year=2026,
        persons=(Person(id=ME, birth_date=date(1970, 1, 1), household_id=HouseholdId("hh")),),
        households=(Household(id=HouseholdId("hh"), member_ids=(ME,)),),
        properties=(
            Property(id=PropertyId("h"), kind=PropertyKind.APARTMENT, legal_dong_code="2647010100",
                     published_prices=(PriceFact(2026, 2_000_000_000),)),
        ),
        ownerships=(Ownership(person_id=ME, property_id=PropertyId("h"), acquired_on=date(2012, 5, 1)),),
    )
    assert not [s for s in consult(case, ME, rs).strategies if s.key == "spouse_gift"]


# --------------------------------------------------------------------------
# 납부유예 — 감면이 아니라 유예다 (종부세법 §20의2)
# --------------------------------------------------------------------------


def _one_house_case(birth: date, acquired: date, price: int) -> TaxCase:
    from realestate_tax.domain import ResidenceSpell

    return TaxCase(
        year=2027,
        persons=(Person(id=ME, birth_date=birth, household_id=HouseholdId("hh")),),
        households=(Household(id=HouseholdId("hh"), member_ids=(ME,)),),
        properties=(
            Property(id=PropertyId("h"), kind=PropertyKind.APARTMENT,
                     legal_dong_code="1168010100",
                     published_prices=(PriceFact(2027, price),)),
        ),
        ownerships=(Ownership(person_id=ME, property_id=PropertyId("h"), acquired_on=acquired),),
        residences=(ResidenceSpell(person_id=ME, property_id=PropertyId("h"), start=acquired),),
    )


def _check(case: TaxCase, rs: RuleSet):
    from realestate_tax.engine.deferral import check_deferral
    from realestate_tax.engine.jongbuse import compute_jongbuse
    from realestate_tax.engine.periods import holding_years
    from realestate_tax.engine.special_houses import assess
    from realestate_tax.rules import Track

    result = compute_jongbuse(case, ME, rs, track=Track.REFORM)
    a = assess(case, ME, rs, track=Track.REFORM)
    return check_deferral(
        case, ME, rs,
        jongbuse_amount=result.net_tax.as_int(),
        one_house=a.is_one_house,
        holding_years=holding_years(case, ME, PropertyId("h"), case.assessment_date),
        track=Track.REFORM,
    )


def test_고령_1주택자는_납부유예_요건을_갖춘다(rs: RuleSet):
    """집은 있는데 현금이 없는 사람의 실제 답이다. 개편안이 보유세를 올리면서
    쓸모가 커졌다 — "줄일 방법이 없습니다"로 끝내면 그 사람은 집을 팔아야 한다."""
    c = _check(_one_house_case(date(1958, 4, 2), date(2010, 3, 1), 3_000_000_000), rs)
    assert c.eligible_so_far
    assert c.worth_showing
    assert c.deferrable > 0
    assert any("60세 이상" in m for m in c.met_ko)


def test_연령과_보유기간은_둘_중_하나만_충족해도_된다(rs: RuleSet):
    """조문이 "만 60세 이상이거나 해당 주택을 5년 이상 보유"로 **잇는다**.
    둘 다 요구하면 자격 있는 사람을 잘못 막는다."""
    young_but_long = _check(_one_house_case(date(1990, 1, 1), date(2010, 3, 1), 3_000_000_000), rs)
    assert young_but_long.eligible_so_far
    assert any("보유" in m and "5년 이상" in m for m in young_but_long.met_ko)


def test_요건에_미달하면_이유를_대고_막는다(rs: RuleSet):
    """36세·보유 3년 — 연령도 보유도 못 채운다."""
    c = _check(_one_house_case(date(1990, 1, 1), date(2024, 3, 1), 3_000_000_000), rs)
    assert not c.eligible_so_far
    assert not c.worth_showing
    assert any("60세 미만" in f for f in c.failed_ko)


def test_세액이_100만원_이하면_대상이_아니다(rs: RuleSet):
    """§20의2①4호 "해당 연도의 주택분 종합부동산세액이 100만원을 초과할 것"."""
    c = _check(_one_house_case(date(1958, 4, 2), date(2010, 3, 1), 1_300_000_000), rs)
    assert not c.eligible_so_far
    assert any("100만" in f or "1,000,000" in f for f in c.failed_ko)


def test_소득요건은_가정하지_않고_묻는다(rs: RuleSet):
    """사건 모델에 소득이 없다. 모르는 것을 충족한 것으로 가정하면
    자격 없는 사람에게 신청하라고 말하게 된다."""
    c = _check(_one_house_case(date(1958, 4, 2), date(2010, 3, 1), 3_000_000_000), rs)
    joined = " ".join(c.asks_ko)
    assert "총급여" in joined and "70,000,000" in joined
    assert "종합소득금액" in joined and "60,000,000" in joined


def test_취소_사유를_함께_말한다(rs: RuleSet):
    """유예는 공짜가 아니다 — 팔거나 물려주면 이자상당가산액과 함께 징수된다."""
    c = _check(_one_house_case(date(1958, 4, 2), date(2010, 3, 1), 3_000_000_000), rs)
    joined = " ".join(c.revoke_reasons_ko)
    assert "양도" in joined and "상속" in joined


# --------------------------------------------------------------------------
# 합산배제 임대주택 — 판정은 되는데 "신고하세요"를 안 했다
# --------------------------------------------------------------------------


def _rental_case(declared: bool) -> TaxCase:
    from realestate_tax.domain import (
        Election, ElectionKind, RentalRegistration, RentalType, ResidenceSpell,
    )

    props = (
        Property(id=PropertyId("main"), display_name="본가", kind=PropertyKind.APARTMENT,
                 legal_dong_code="1168010100",
                 published_prices=(PriceFact(2026, 2_000_000_000),)),
        Property(id=PropertyId("rent"), display_name="임대주택", kind=PropertyKind.APARTMENT,
                 legal_dong_code="4611010100",
                 published_prices=(PriceFact(2026, 500_000_000),),
                 rental=RentalRegistration(
                     rental_type=RentalType.BUILT_LONG_TERM,
                     registered_on=date(2020, 1, 15), obligation_end=date(2030, 1, 15))),
    )
    return TaxCase(
        year=2026,
        persons=(Person(id=ME, birth_date=date(1965, 1, 1), household_id=HouseholdId("hh")),),
        households=(Household(id=HouseholdId("hh"), member_ids=(ME,)),),
        properties=props,
        ownerships=tuple(
            Ownership(person_id=ME, property_id=p.id, acquired_on=date(2015, 1, 1)) for p in props
        ),
        residences=(ResidenceSpell(person_id=ME, property_id=PropertyId("main"), start=date(2015, 1, 1)),),
        elections=((Election(person_id=ME, kind=ElectionKind.RENTAL_EXCLUSION),) if declared else ()),
    )


def test_합산배제_신고를_안_했으면_하라고_말한다(rs: RuleSet):
    """엔진은 이미 판정하고 있었다. 없던 것은 **"신고하세요"라는 말**이다.

    합산배제는 주택 수만 빼주는 게 아니라 **과세표준 합산에서 제외**된다 —
    그 주택에는 종부세가 아예 붙지 않아 절감폭이 크다.
    실측에서 신고 하나로 종부세 1,050만원이 80만원이 됐는데 화면은 아무 말도 안 했다.
    """
    found = [s for s in consult(_rental_case(False), ME, rs).strategies if s.key == "rental_exclusion"]
    assert found, "합산배제 신고 안내가 없다"
    assert found[0].saving > 0


def test_이미_신고했으면_다시_권하지_않는다(rs: RuleSet):
    assert not [
        s for s in consult(_rental_case(True), ME, rs).strategies if s.key == "rental_exclusion"
    ]


def test_추징_위험을_반드시_말한다(rs: RuleSet):
    """의무임대기간을 못 채우면 면제받은 세액이 추징된다(종부세법 §17③).
    절감액만 보여주고 이걸 빼면 조언이 아니라 함정이다."""
    s = [x for x in consult(_rental_case(False), ME, rs).strategies if x.key == "rental_exclusion"][0]
    joined = " ".join(s.caveats_ko)
    assert "추징" in joined
    assert "5%" in joined or "5퍼센트" in joined
