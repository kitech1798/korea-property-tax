"""부담부증여 — 한 사건이 두 세목으로 쪼개진다.

소득세법 §88① 후단: "부담부증여 시 수증자가 부담하는 **채무액에 해당하는 부분은
양도로 보며**". 채무액 부분은 증여자가 양도소득세를, 나머지는 수증자가 증여세를 낸다.

안분 산식은 시행령 §159①이다. ⚠️ 법제처 API가 이 계산식을 **이미지로** 줘서
JSON·HTML로는 안 보인다. `type=XML`로 받아야 텍스트가 나온다(2026-08-05 확인).

    취득가액 = A × 채무액 ÷ 증여가액   A: 법 §97①1에 따른 가액
    양도가액 = A × 채무액 ÷ 증여가액   A: 상증세법 §60~66에 따라 평가한 가액
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
    PriceFact,
    Property,
    PropertyId,
    PropertyKind,
    TaxCase,
)
from realestate_tax.engine.transfer_tax import (
    BurdenGift,
    compute_burden_gift,
    compute_transfer_tax,
)
from realestate_tax.rules import RuleSet, Track, default_ruleset_root

ME = PersonId("me")
SEOUL = "1168010100"
BUSAN = "2635010300"
EOK = 100_000_000


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


def make_case(names_prices_dongs, year=2026) -> TaxCase:
    hh = HouseholdId("hh")
    p = Person(id=ME, household_id=hh, name="본인", birth_date=date(1960, 1, 1))
    props = tuple(
        Property(
            id=PropertyId(n), kind=PropertyKind.APARTMENT, legal_dong_code=d,
            display_name=n, published_prices=(PriceFact(year, v),),
        )
        for n, v, d in names_prices_dongs
    )
    return TaxCase(
        year=year, persons=(p,),
        households=(Household(id=hh, member_ids=(ME,)),),
        properties=props,
        ownerships=tuple(
            Ownership(ME, pr.id, acquired_on=date(year - 16, 1, 1)) for pr in props
        ),
    )


def gift(**over) -> BurdenGift:
    base = dict(
        property_id=PropertyId("본가"), person_id=ME, gift_date=date(2026, 6, 1),
        appraised_value=20 * EOK, gift_value=20 * EOK, debt_assumed=8 * EOK,
        acquisition_price=5 * EOK, holding_years=16, residence_years=16,
    )
    base.update(over)
    return BurdenGift(**base)


# --------------------------------------------------------------------------
# 안분 — 시행령 §159①
# --------------------------------------------------------------------------


def test_채무액_비율만큼만_양도로_본다():
    g = gift()
    assert g.transfer_ratio == Fraction(8, 20)
    ev = g.to_transfer_event()
    assert ev.transfer_price == 8 * EOK      # 20억 × 8/20
    assert ev.acquisition_price == 2 * EOK   # 5억 × 8/20


def test_필요경비도_같은_비율로_안분한다():
    """안분하지 않으면 양도로 보는 부분에 전체 경비를 떠넘기게 된다."""
    ev = gift(necessary_expense=5_000_000).to_transfer_event()
    assert ev.necessary_expense == 2_000_000  # 500만 × 2/5


def test_평가액과_증여가액이_다르면_산식대로_계산한다():
    """조문은 A(평가액)와 C(증여가액)를 별개 항목으로 쓴다. 같다고 가정하지 않는다."""
    ev = gift(appraised_value=22 * EOK, gift_value=20 * EOK).to_transfer_event()
    assert ev.transfer_price == int(22 * EOK * Fraction(8, 20))


def test_채무가_없으면_양도가_아니다():
    g = gift(debt_assumed=0)
    assert not g.is_transfer
    assert g.transfer_ratio == 0
    assert g.gift_portion == 20 * EOK


@pytest.mark.parametrize(
    "over, match",
    [
        (dict(gift_value=0), "증여가액"),
        (dict(debt_assumed=-1), "음수"),
        (dict(debt_assumed=25 * EOK), "넘는다"),
    ],
)
def test_말이_안_되는_입력은_거부한다(over, match):
    with pytest.raises(ValueError, match=match):
        gift(**over)


# --------------------------------------------------------------------------
# 세액 계산 + 증여세 경계
# --------------------------------------------------------------------------


def test_증여세는_계산하지_않는다는_사실을_반드시_밝힌다(rs: RuleSet):
    """★ 증여세를 빼놓고 '이만큼이면 유리하다'고 말하면 조언이 아니라 함정이다.
    부담부증여의 유불리는 두 세목을 합쳐야 판단할 수 있다."""
    r = compute_burden_gift(make_case([("본가", 15 * EOK, SEOUL)]), gift(), rs)
    n = r.trace.find("tr.01.burden_gift_split")
    assert n is not None
    assert "증여세" in n.note_ko
    assert "포함되어 있지 않습니다" in n.note_ko
    assert f"{12 * EOK:,}" in n.note_ko  # 증여세 대상 금액을 숫자로 알려준다


def test_안분_근거가_감사추적_맨_앞에_남는다(rs: RuleSet):
    """'왜 양도가액이 증여가액보다 작지?'에서 막히지 않으려면 쪼갠 근거가 보여야 한다."""
    r = compute_burden_gift(make_case([("본가", 15 * EOK, SEOUL)]), gift(), rs)
    assert r.trace.children[0].step_id == "tr.01.burden_gift_split"
    n = r.trace.children[0]
    assert "2,000,000,000 × (800,000,000 ÷ 2,000,000,000)" in n.substitution
    assert n.branch.taken == "40.0%"


def test_다주택_중과도_그대로_적용된다(rs: RuleSet):
    """부담부증여라고 중과를 피해가지 않는다. 양도로 보는 부분은 양도다."""
    case = make_case([("본가", 15 * EOK, SEOUL), ("부산집", 8 * EOK, BUSAN)])
    r = compute_burden_gift(case, gift(holding_years=16, residence_years=0), rs)
    assert "%p [중과]" in r.trace.find("tr.07.income_tax").substitution
    assert r.income_tax.as_int() > 0


def test_양도로_본_부분에도_비과세_요건이_그대로_걸린다(rs: RuleSet):
    """보유 1년짜리 1주택을 부담부증여해도 비과세는 안 된다(시행령 §154①)."""
    case = make_case([("본가", 15 * EOK, SEOUL)])
    r = compute_burden_gift(case, gift(holding_years=1, residence_years=1), rs)
    assert r.trace.find("tr.03a.exemption_requirements").branch.taken == "미충족"
    assert r.taxable_gain.as_int() > 0


# --------------------------------------------------------------------------
# ★ 해석이 갈리는 지점 — 답이 갈릴 때만 판정 불가를 낸다
# --------------------------------------------------------------------------


def test_고가주택_판정_기준이_갈리면_그_사실을_알린다(rs: RuleSet):
    """전체 평가액 20억은 12억을 넘는데 안분 후 8억은 넘지 않는다.
    어느 쪽을 기준으로 보느냐에 따라 비과세가 통째로 뒤집힌다."""
    r = compute_burden_gift(make_case([("본가", 15 * EOK, SEOUL)]), gift(), rs)
    alts = {a.key: a for a in r.trace.all_alternatives()}
    assert "burden_gift_high_value_basis" in alts
    assert alts["burden_gift_high_value_basis"].actionable
    assert "세무서 확인" in alts["burden_gift_high_value_basis"].reason_ko
    assert "판단 필요" in dict(r.trace.certainty_concerns())


def test_답이_갈리지_않으면_판정_불가를_내지_않는다(rs: RuleSet):
    """전체도 안분 후도 12억을 넘으면 어느 해석이든 결론이 같다.
    답이 같은 지점에서까지 불확실을 뿌리면 나머지 경고까지 무시된다."""
    big = gift(appraised_value=40 * EOK, gift_value=40 * EOK, debt_assumed=30 * EOK)
    r = compute_burden_gift(make_case([("본가", 30 * EOK, SEOUL)]), big, rs)
    assert "burden_gift_high_value_basis" not in {
        a.key for a in r.trace.all_alternatives()
    }


def test_전체가_12억_이하면_해석이_갈리지_않는다(rs: RuleSet):
    small = gift(appraised_value=10 * EOK, gift_value=10 * EOK, debt_assumed=4 * EOK)
    r = compute_burden_gift(make_case([("본가", 8 * EOK, SEOUL)]), small, rs)
    assert "burden_gift_high_value_basis" not in {
        a.key for a in r.trace.all_alternatives()
    }


def test_안분한_양도와_직접_양도가_같은_결과를_낸다(rs: RuleSet):
    """부담부증여 경로가 별도 계산을 만드는 게 아니라 **입력을 안분해 같은 엔진**을
    태우는 것임을 고정한다. 두 경로가 갈라지면 한쪽만 고쳐지는 사고가 난다."""
    case = make_case([("본가", 15 * EOK, SEOUL)])
    g = gift()
    direct = compute_transfer_tax(case, g.to_transfer_event(), rs)
    via_gift = compute_burden_gift(case, g, rs)
    assert via_gift.income_tax.as_int() == direct.income_tax.as_int()
    assert via_gift.taxable_base.as_int() == direct.taxable_base.as_int()
