"""매도 시점 분석 테스트 — 이 서비스가 답해야 할 진짜 질문.

"팔까 버틸까"는 보유세만으로도, 양도세만으로도 답이 안 나온다.
개편안은 종부세를 올리면서 양도세 중과를 '27~'28 한시 완화했다.
**버틸수록 보유세는 쌓이고, 늦게 팔수록 양도세는 는다.** 어느 쪽이 큰지는 계산해야 안다.
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
from realestate_tax.engine.jongbuse import JongbuseOptions
from realestate_tax.engine.strategy import sell_timing
from realestate_tax.engine.transfer_tax import TransferEvent
from realestate_tax.rules import RuleSet, Track, default_ruleset_root

ME = PersonId("me")
SEOUL = "1168010100"  # 강남구 — 조정대상지역
BUSAN = "2635010300"
EOK = 100_000_000


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


def two_house_case() -> tuple[TaxCase, TransferEvent]:
    """조정대상지역 1채 + 비규제 1채. 조정지역 주택을 파는 시나리오."""
    hh = HouseholdId("hh")
    p = Person(id=ME, household_id=hh, name="본인", birth_date=date(1965, 1, 1))
    props = (
        Property(
            id=PropertyId("강남집"),
            kind=PropertyKind.APARTMENT,
            legal_dong_code=SEOUL,
            display_name="강남집",
            published_prices=(PriceFact(2026, 20 * EOK),),
        ),
        Property(
            id=PropertyId("부산집"),
            kind=PropertyKind.APARTMENT,
            legal_dong_code=BUSAN,
            display_name="부산집",
            published_prices=(PriceFact(2026, 8 * EOK),),
        ),
    )
    case = TaxCase(
        year=2026,
        persons=(p,),
        households=(Household(id=hh, member_ids=(ME,)),),
        properties=props,
        ownerships=tuple(
            Ownership(ME, pr.id, acquired_on=date(2012, 3, 1)) for pr in props
        ),
    )
    event = TransferEvent(
        property_id=PropertyId("강남집"),
        person_id=ME,
        transfer_date=date(2027, 6, 1),
        transfer_price=30 * EOK,
        acquisition_price=12 * EOK,
        holding_years=15,
        residence_years=0,
    )
    return case, event


OPTS = JongbuseOptions(holding_years=15, residence_years=0)


def test_매도_연도별_총비용을_계산한다(rs: RuleSet):
    case, event = two_house_case()
    result = sell_timing(case, ME, event, rs, options=OPTS)

    assert [p.year for p in result.points] == [2027, 2028, 2029]
    for p in result.points:
        assert p.total_cost == p.transfer_tax + p.holding_tax_paid
        assert p.transfer_tax > 0 and p.holding_tax_paid > 0
    assert result.property_label == "강남집"


def test_보유세는_해마다_누적된다(rs: RuleSet):
    """늦게 팔수록 그때까지 낸 보유세가 쌓인다. 이게 '버티는 비용'이다."""
    result = sell_timing(*two_house_case()[:1], ME, two_house_case()[1], rs, options=OPTS)
    paid = [p.holding_tax_paid for p in result.points]
    assert paid[0] < paid[1] < paid[2]


def test_중과_한시완화_때문에_늦게_팔수록_양도세가_는다(rs: RuleSet):
    """2주택 조정지역: '27 +5%p → '28 +10%p → '29 +20%p 복귀.
    이 흐름이 매도 시점 결정의 핵심이다."""
    case, event = two_house_case()
    result = sell_timing(case, ME, event, rs, options=OPTS)
    taxes = {p.year: p.transfer_tax for p in result.points}
    assert taxes[2027] < taxes[2028] < taxes[2029]


def test_권장_시점과_최악_시점의_차이를_금액으로_준다(rs: RuleSet):
    """이 차이가 크면 '언제 파느냐'가 결정적이라는 뜻이다."""
    case, event = two_house_case()
    result = sell_timing(case, ME, event, rs, options=OPTS)

    assert result.best is not None and result.worst is not None
    assert result.best.total_cost <= result.worst.total_cost
    assert result.spread == result.worst.total_cost - result.best.total_cost
    # 조정지역 다주택이면 중과 복귀 폭이 커서 시점 차이가 뚜렷하다
    assert result.spread > 100_000_000


def test_조정지역_다주택은_2027년이_가장_유리하다(rs: RuleSet):
    """중과가 가장 낮고 보유세 누적도 가장 적은 해다.
    개편안이 의도한 '매도 창구'가 계산으로 확인된다."""
    case, event = two_house_case()
    result = sell_timing(case, ME, event, rs, options=OPTS)
    assert result.best.year == 2027


def test_현행법_트랙으로도_비교할_수_있다(rs: RuleSet):
    """개편안이 부결되면 중과가 계속 +20%p다. 두 트랙 모두 볼 수 있어야 한다."""
    case, event = two_house_case()
    current = sell_timing(case, ME, event, rs, track=Track.CURRENT, options=OPTS)
    reform = sell_timing(case, ME, event, rs, track=Track.REFORM, options=OPTS)

    by_year_current = {p.year: p.transfer_tax for p in current.points}
    by_year_reform = {p.year: p.transfer_tax for p in reform.points}
    # 2027년은 개편안이 훨씬 싸다(한시 완화)
    assert by_year_reform[2027] < by_year_current[2027]


def test_1주택자는_비과세_때문에_시점_차이가_작다(rs: RuleSet):
    """중과가 없고 12억 비과세가 걸리므로 다주택과 양상이 완전히 다르다.
    같은 조언을 두 집단에 하면 안 된다."""
    hh = HouseholdId("hh")
    p = Person(id=ME, household_id=hh, name="본인", birth_date=date(1965, 1, 1))
    prop = Property(
        id=PropertyId("본가"),
        kind=PropertyKind.APARTMENT,
        legal_dong_code=SEOUL,
        display_name="본가",
        published_prices=(PriceFact(2026, 15 * EOK),),
    )
    case = TaxCase(
        year=2026,
        persons=(p,),
        households=(Household(id=hh, member_ids=(ME,)),),
        properties=(prop,),
        ownerships=(Ownership(ME, prop.id, acquired_on=date(2012, 3, 1)),),
    )
    event = TransferEvent(
        PropertyId("본가"), ME, date(2027, 6, 1), 20 * EOK, 10 * EOK,
        holding_years=15, residence_years=15,
    )
    result = sell_timing(
        case, ME, event, rs,
        options=JongbuseOptions(holding_years=15, residence_years=15, resides_in_main_house=True),
    )
    multi = sell_timing(*two_house_case()[:1], ME, two_house_case()[1], rs, options=OPTS)
    assert result.spread < multi.spread


def test_매도_시점_결과에도_개편안_불확실성이_따라붙는다(rs: RuleSet):
    """개편안 트랙 계산이므로 국회 통과 전이라는 사실이 사라지면 안 된다."""
    from realestate_tax.engine.transfer_tax import compute_transfer_tax

    case, event = two_house_case()
    r = compute_transfer_tax(case, event, rs, track=Track.REFORM)
    assert "국회 미통과" in dict(r.trace.certainty_concerns())
    assert "국회 통과 전" in r.trace.note_ko
