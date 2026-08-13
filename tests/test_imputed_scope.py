"""부득이한 사유 거주 인정의 **범위와 요건** (개편안, 소득법 §95⑦·소득령 §159의4②③).

2026-08-13 멀티에이전트 감사(세무 관점)가 두 가지를 짚었고, 정부 원문으로 확인됐다.

  ① **범위** — 상세본 p.84 (18) 제목이 "주택 **장기거주 소득공제 적용 시**"이고
     신설 문언이 "주택장기거주소득공제 **공제율 산정 시**"다. 근거 조문도
     소득법 §95⑦·소득령 §159의4②③으로 **장기보유특별공제 조항**이다.
     비과세(§154①)나 기본공제(§103)를 고치지 않는다.
     예전 엔진은 인정분을 포함한 값 하나를 만들어 그 셋에 다 썼다.

  ② **요건** — "ㅇ (요건) ➊~➌ 모두 충족 / ➊ 주거이전하는 날 현재 1년 이상
     **계속하여** 거주 중인 주택일 것". 룰셋에 값을 담아 두고 코드가 안 읽었다.

둘 다 요건을 **늘리는** 방향이라 과소신고다.
"""

from __future__ import annotations

from datetime import date

import pytest

from realestate_tax.domain import (
    Household, HouseholdId, ImputedResidenceReason, Ownership, Person, PersonId,
    PriceFact, Property, PropertyId, PropertyKind, ResidenceSpell, TaxCase,
)
from realestate_tax.engine.transfer_tax import TransferEvent, compute_transfer_tax
from realestate_tax.rules import RuleSet, Track, default_ruleset_root

ME = PersonId("me")
HH = HouseholdId("hh")
HOUSE = PropertyId("본가")
SEOUL = "1168010100"   # 취득 당시 조정대상지역 → 비과세에 2년 거주요건이 붙는다
EOK = 100_000_000
ON = date(2028, 6, 1)  # 인정 규정은 '28.1.1. 이후 양도분부터


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


def make_case(*spells: ResidenceSpell) -> TaxCase:
    return TaxCase(
        year=2028,
        persons=(Person(id=ME, household_id=HH, birth_date=date(1970, 1, 1)),),
        households=(Household(id=HH, member_ids=(ME,)),),
        properties=(
            Property(id=HOUSE, kind=PropertyKind.APARTMENT, legal_dong_code=SEOUL,
                     display_name="본가",
                     published_prices=(PriceFact(2028, 20 * EOK),)),
        ),
        ownerships=(Ownership(ME, HOUSE, acquired_on=date(2018, 3, 1)),),
        residences=spells,
    )


def run(rs: RuleSet, case: TaxCase):
    """거주기간을 **명시하지 않는다** — 거주 이력에서 도출되게 둔다."""
    event = TransferEvent(
        property_id=HOUSE, person_id=ME, transfer_date=ON,
        transfer_price=32 * EOK, acquisition_price=12 * EOK,
        holding_years=10, residence_years=None,
    )
    return compute_transfer_tax(case, event, rs, track=Track.REFORM)


def taxable_ratio(r) -> float:
    return r.taxable_gain.as_int() / r.gain.as_int()


# --------------------------------------------------------------------------
# ① 범위 — 공제율 산정 밖으로 새지 않는다
# --------------------------------------------------------------------------


def test_산_적_없는_집은_인정_구간만으로_비과세를_못_받는다(rs: RuleSet):
    """★ 그 집에 하루도 산 적 없는 사람이 근무상 형편만 적으면
    비과세 12억이 열리고 있었다.

    취득 당시 조정대상지역이라 §154①의 2년 거주요건이 붙는데, 인정분이 그 요건까지
    채워 줬다. 원문은 인정 범위를 '장기거주 소득공제 공제율 산정'으로 못 박는다.
    """
    only_imputed = ResidenceSpell(
        ME, HOUSE, start=date(2025, 1, 1), end=date(2028, 1, 1),
        imputed_reason=ImputedResidenceReason.JOB_TRANSFER,
    )
    r = run(rs, make_case(only_imputed))
    # 비과세 안분이 없어야 한다 = 양도차익 전액이 과세대상
    assert taxable_ratio(r) == pytest.approx(1.0), "인정분만으로 비과세가 열렸다"


def test_실제로_2년_살았으면_비과세가_열린다(rs: RuleSet):
    """대조군 — 요건을 실제로 채운 경우까지 막으면 그건 다른 버그다."""
    lived = ResidenceSpell(ME, HOUSE, start=date(2019, 1, 1), end=date(2023, 1, 1))
    r = run(rs, make_case(lived))
    assert taxable_ratio(r) < 1.0


# --------------------------------------------------------------------------
# ② 요건 ➊ — 이전 직전에 1년 이상 '계속' 거주
# --------------------------------------------------------------------------


def test_이전_직전_1년_미만_거주면_인정하지_않는다(rs: RuleSet):
    """상세본 p.84 ➊. 6개월만 살고 전근 간 경우는 인정 대상이 아니다."""
    short = ResidenceSpell(ME, HOUSE, start=date(2024, 7, 1), end=date(2024, 12, 31))
    excused = ResidenceSpell(
        ME, HOUSE, start=date(2025, 1, 1), end=date(2028, 1, 1),
        imputed_reason=ImputedResidenceReason.JOB_TRANSFER,
    )
    with_short = run(rs, make_case(short, excused))
    only_short = run(rs, make_case(short))
    assert with_short.long_term_deduction.as_int() == only_short.long_term_deduction.as_int(), (
        "이전 직전 거주가 1년에 못 미치는데 인정 구간이 공제율에 반영됐다"
    )


def test_이전_직전_1년_이상_거주면_공제율에_반영된다(rs: RuleSet):
    """대조군 — 요건을 갖추면 공제율은 실제로 올라야 한다."""
    lived = ResidenceSpell(ME, HOUSE, start=date(2023, 1, 1), end=date(2025, 1, 1))
    excused = ResidenceSpell(
        ME, HOUSE, start=date(2025, 1, 1), end=date(2028, 1, 1),
        imputed_reason=ImputedResidenceReason.JOB_TRANSFER,
    )
    with_imputed = run(rs, make_case(lived, excused))
    without = run(rs, make_case(lived))
    assert with_imputed.long_term_deduction.as_int() > without.long_term_deduction.as_int()


def test_흩어진_거주의_합으로는_요건을_채울_수_없다(rs: RuleSet):
    """조문이 "1년 이상 **계속하여** 거주 중인"이라고 한다.
    10년 전에 1년 살고 떠난 집이 통과하면 안 된다."""
    long_ago = ResidenceSpell(ME, HOUSE, start=date(2018, 3, 1), end=date(2019, 6, 1))
    excused = ResidenceSpell(
        ME, HOUSE, start=date(2025, 1, 1), end=date(2028, 1, 1),
        imputed_reason=ImputedResidenceReason.JOB_TRANSFER,
    )
    both = run(rs, make_case(long_ago, excused))
    alone = run(rs, make_case(long_ago))
    assert both.long_term_deduction.as_int() == alone.long_term_deduction.as_int()
