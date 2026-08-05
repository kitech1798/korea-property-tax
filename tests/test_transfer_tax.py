"""양도소득세 테스트.

★ 게이트: 재정경제부 세제개편안 문답자료 p.37의 **산출세액 6개 값**을 재현한다.
정부가 직접 인쇄한 숫자이므로 역산이 아니라 원본과의 대조다.

  사례① 양도 32억 / 취득 12억 / 2년 거주 · 10년 보유
        → '27 2.36억 · '28 3.20억 · '29 4.05억
  사례② 양도 75억 / 취득 25억 / 10년 거주 · 보유
        → '27 3.16억 · '28 9.23억 · '29 13.73억
"""

from __future__ import annotations

from dataclasses import replace
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
from realestate_tax.engine.transfer_tax import TransferEvent, compute_transfer_tax
from realestate_tax.rules import RuleSet, Track, default_ruleset_root

ME = PersonId("me")
SEOUL = "1168010100"  # 강남구 — 조정대상지역
BUSAN = "2635010300"  # 해운대구 — 비규제

EOK = 100_000_000


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


def make_case(prices: list[tuple[str, int, str]], year: int) -> TaxCase:
    hh = HouseholdId("hh")
    p = Person(id=ME, household_id=hh, name="본인", birth_date=date(1970, 1, 1))
    props = tuple(
        Property(
            id=PropertyId(name),
            kind=PropertyKind.APARTMENT,
            legal_dong_code=dong,
            display_name=name,
            published_prices=(PriceFact(year, price),),
        )
        for name, price, dong in prices
    )
    return TaxCase(
        year=year,
        persons=(p,),
        households=(Household(id=hh, member_ids=(ME,)),),
        properties=props,
        ownerships=tuple(
            Ownership(ME, pr.id, acquired_on=date(year - 10, 3, 1)) for pr in props
        ),
    )


# --------------------------------------------------------------------------
# ★ 게이트 — 문답자료 p.37 산출세액 재현
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "year, expected_eok, expected_deduction_eok",
    [(2027, 2.36, 6.00), (2028, 3.20, 4.00), (2029, 4.05, 2.00)],
)
def test_문답자료_p37_사례1을_재현한다(
    rs: RuleSet, year, expected_eok, expected_deduction_eok
):
    """1주택자 양도가액 32억, 취득가액 12억, 2년 거주, 10년 보유."""
    case = make_case([("본가", 20 * EOK, SEOUL)], year)
    event = TransferEvent(
        property_id=PropertyId("본가"),
        person_id=ME,
        transfer_date=date(year, 6, 1),
        transfer_price=32 * EOK,
        acquisition_price=12 * EOK,
        holding_years=10,
        residence_years=2,
    )
    r = compute_transfer_tax(case, event, rs, track=Track.REFORM)

    detail = (
        f"\n  양도차익      {r.gain.as_int():,}"
        f"\n  과세대상      {r.taxable_gain.as_int():,}"
        f"\n  장특공제      {r.long_term_deduction.as_int():,}  (기대 {expected_deduction_eok}억)"
        f"\n  과세표준      {r.taxable_base.as_int():,}"
        f"\n  산출세액      {r.income_tax.as_int():,}  (기대 {expected_eok}억)"
        f"\n  대입식        {r.trace.find('tr.07.income_tax').substitution}"
    )
    assert r.long_term_deduction.as_int() / EOK == pytest.approx(
        expected_deduction_eok, abs=0.01
    ), detail
    assert r.income_tax.as_int() / EOK == pytest.approx(expected_eok, abs=0.005), detail


@pytest.mark.parametrize(
    "year, expected_eok, expected_deduction_eok",
    [(2027, 3.16, 33.6), (2028, 9.23, 20.0), (2029, 13.73, 10.0)],
)
def test_문답자료_p37_사례2를_재현한다(
    rs: RuleSet, year, expected_eok, expected_deduction_eok
):
    """1주택자 양도가액 75억, 취득가액 25억, 10년 거주·보유.
    '28·'29는 공제 한도(20억/10억)가 실제로 물리는 케이스다."""
    case = make_case([("본가", 50 * EOK, SEOUL)], year)
    event = TransferEvent(
        property_id=PropertyId("본가"),
        person_id=ME,
        transfer_date=date(year, 6, 1),
        transfer_price=75 * EOK,
        acquisition_price=25 * EOK,
        holding_years=10,
        residence_years=10,
    )
    r = compute_transfer_tax(case, event, rs, track=Track.REFORM)

    assert r.long_term_deduction.as_int() / EOK == pytest.approx(
        expected_deduction_eok, abs=0.01
    )
    assert r.income_tax.as_int() / EOK == pytest.approx(expected_eok, abs=0.005)


def test_2028년과_2029년은_공제한도가_실제로_물린다(rs: RuleSet):
    """한도가 없으면 33.6억이 공제될 상황에서 20억·10억으로 잘린다.
    잘렸다는 사실이 대안으로 남아야 사용자가 이유를 안다."""
    for year, cap in ((2028, 20 * EOK), (2029, 10 * EOK)):
        case = make_case([("본가", 50 * EOK, SEOUL)], year)
        event = TransferEvent(
            PropertyId("본가"), ME, date(year, 6, 1), 75 * EOK, 25 * EOK,
            holding_years=10, residence_years=10,
        )
        r = compute_transfer_tax(case, event, rs, track=Track.REFORM)
        assert r.long_term_deduction.as_int() == cap
        assert any(a.key == "ltd_cap" for a in r.trace.all_alternatives())


# --------------------------------------------------------------------------
# 1세대1주택 비과세
# --------------------------------------------------------------------------


def test_양도가액_12억_이하면_전액_비과세(rs: RuleSet):
    case = make_case([("본가", 8 * EOK, SEOUL)], 2026)
    event = TransferEvent(
        PropertyId("본가"), ME, date(2026, 6, 1), 12 * EOK, 6 * EOK,
        holding_years=10, residence_years=10,
    )
    r = compute_transfer_tax(case, event, rs)
    assert r.taxable_gain.as_int() == 0
    assert r.income_tax.as_int() == 0
    assert r.trace.find("tr.03.exemption").branch.taken == "전액 비과세"


def test_고가주택은_12억_초과분만_안분해_과세한다(rs: RuleSet):
    """이 안분을 빠뜨리면 고가주택 세액이 통째로 부풀어 오른다."""
    case = make_case([("본가", 20 * EOK, SEOUL)], 2026)
    event = TransferEvent(
        PropertyId("본가"), ME, date(2026, 6, 1), 32 * EOK, 12 * EOK,
        holding_years=10, residence_years=2,
    )
    r = compute_transfer_tax(case, event, rs)
    # 양도차익 20억 × (32억 − 12억) / 32억 = 12.5억
    assert r.gain.as_int() == 20 * EOK
    assert r.taxable_gain.as_int() == int(20 * EOK * Fraction(20, 32))
    assert "÷ 3,200,000,000" in r.trace.find("tr.03.exemption").substitution


# --------------------------------------------------------------------------
# ★ 비과세 요건 (2026-08-04 감사에서 발견 — 영향 최대)
#   1주택이라는 사실만으로 비과세를 준 결과, 6개월 보유 사례에서
#   2.29억을 0원으로 안내하고 있었다.
# --------------------------------------------------------------------------


def short_hold_case(residence_years: int = 0):
    """2026-01-10 취득 → 2026-07-01 양도. 보유 6개월."""
    hh = HouseholdId("hh")
    p = Person(id=ME, household_id=hh, name="본인", birth_date=date(1970, 1, 1))
    prop = Property(
        id=PropertyId("본가"), kind=PropertyKind.APARTMENT, legal_dong_code=SEOUL,
        display_name="본가", published_prices=(PriceFact(2026, 8 * EOK),),
    )
    case = TaxCase(
        year=2026,
        persons=(p,),
        households=(Household(id=hh, member_ids=(ME,)),),
        properties=(prop,),
        ownerships=(Ownership(ME, prop.id, acquired_on=date(2026, 1, 10)),),
    )
    event = TransferEvent(
        PropertyId("본가"), ME, date(2026, 7, 1), 11 * EOK, 8 * EOK,
        holding_years=0, residence_years=residence_years,
    )
    return case, event


def test_보유_2년_미만이면_1주택이어도_비과세가_아니다(rs: RuleSet):
    """소득세법 시행령 §154① — "해당 주택의 보유기간이 2년 이상인 것".

    엔진은 양도가액 11억 ≤ 12억이라는 이유만으로 전액 비과세를 줬다.
    법대로면 양도차익 3억이 통째로 과세된다."""
    case, event = short_hold_case()
    r = compute_transfer_tax(case, event, rs)
    assert r.taxable_gain.as_int() == 3 * EOK, "보유 6개월인데 비과세가 나갔다"
    assert r.trace.find("tr.03a.exemption_requirements").branch.taken == "미충족"
    alts = {a.key: a for a in r.trace.all_alternatives()}
    assert "one_house_exemption_requirements" in alts
    assert "보유 0년" in alts["one_house_exemption_requirements"].reason_ko


def test_요건을_못_채우면_단서_예외를_안내한다(rs: RuleSet):
    """수용·해외이주는 사실관계 확인이 필요해 자동 판정하지 않는다.
    그래도 '이런 경우엔 될 수 있다'는 길은 알려줘야 한다."""
    case, event = short_hold_case()
    r = compute_transfer_tax(case, event, rs)
    note = r.trace.find("tr.03a.exemption_requirements").note_ko or ""
    assert "수용" in note and "해외이주" in note


def test_취득_당시_조정대상지역이면_거주요건이_붙는다(rs: RuleSet):
    """★ 거주요건은 **양도 당시**가 아니라 **취득 당시** 지역으로 갈린다."""
    case = make_case([("본가", 20 * EOK, SEOUL)], 2026)  # 취득 2016년
    base = TransferEvent(
        PropertyId("본가"), ME, date(2026, 6, 1), 32 * EOK, 12 * EOK,
        holding_years=10, residence_years=0,
    )
    no_live = compute_transfer_tax(case, base, rs)
    lived = compute_transfer_tax(case, replace(base, residence_years=2), rs)
    # 거주 2년을 채운 쪽만 비과세 안분을 받는다
    assert lived.taxable_gain.as_int() < no_live.taxable_gain.as_int()


def test_거주요건을_이미_채웠으면_취득_당시_지역을_따지지_않는다(rs: RuleSet):
    """답이 갈리지 않는 지점에서 '판정 불가'를 내는 것은 정직이 아니라 무능이다.
    지역 이력이 없는 옛 취득분(2016년)이 전부 막히면 도구가 쓸모없어진다."""
    case = make_case([("본가", 20 * EOK, SEOUL)], 2026)
    event = TransferEvent(
        PropertyId("본가"), ME, date(2026, 6, 1), 32 * EOK, 12 * EOK,
        holding_years=10, residence_years=2,
    )
    r = compute_transfer_tax(case, event, rs)
    n = r.trace.find("tr.03a.exemption_requirements")
    assert n.branch.taken == "충족"
    assert "따지지 않습니다" in (n.branch.detail_ko or "")


def test_다주택자는_비과세가_없고_사유가_남는다(rs: RuleSet):
    case = make_case([("A", 10 * EOK, BUSAN), ("B", 9 * EOK, BUSAN)], 2026)
    event = TransferEvent(
        PropertyId("A"), ME, date(2026, 6, 1), 15 * EOK, 8 * EOK,
        holding_years=10, residence_years=0,
    )
    r = compute_transfer_tax(case, event, rs)
    assert r.taxable_gain.as_int() == r.gain.as_int()
    alts = {a.key for a in r.trace.all_alternatives()}
    assert "one_house_exemption" in alts


# --------------------------------------------------------------------------
# ★ 다주택 중과 — 2027~2028 한시 완화 창구
# --------------------------------------------------------------------------


def multi_case(year: int) -> tuple[TaxCase, TransferEvent]:
    case = make_case([("서울집", 15 * EOK, SEOUL), ("부산집", 8 * EOK, BUSAN)], year)
    event = TransferEvent(
        PropertyId("서울집"), ME, date(year, 6, 1), 20 * EOK, 15 * EOK,
        holding_years=10, residence_years=0,
    )
    return case, event


# --------------------------------------------------------------------------
# ★ 장기보유특별공제의 법정 요건 (2026-08-04 감사에서 발견)
#   축별 min_years만 걸어두면 조문의 **본문 요건**이 빠져나간다.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("track", [Track.CURRENT, Track.REFORM])
@pytest.mark.parametrize("year", [2026, 2027, 2028, 2029])
def test_보유_3년_미만이면_장특공제가_0이다(rs: RuleSet, year, track):
    """소득세법 §95② 본문 "보유기간이 3년 이상인 것".

    거주 축의 min_years가 2라서, 보유 2년짜리가 **거주공제만 챙겨 빠져나갔다.**
    2029년 블록은 holding: null이라 게이트가 아예 없었다 —
    개편안이 폐지한 것은 보유 '공제율'이지 보유 3년 '기본요건'이 아니다."""
    if track is Track.REFORM and year < 2027:
        pytest.skip("개편안은 2027년부터")
    case = make_case([("우리집", 15 * EOK, SEOUL)], year)
    event = TransferEvent(
        PropertyId("우리집"), ME, date(year, 6, 1), 30 * EOK, 10 * EOK,
        holding_years=2, residence_years=2,
    )
    r = compute_transfer_tax(case, event, rs, track=track)
    assert r.long_term_deduction.as_int() == 0
    assert "보유 3년 미만" in r.trace.find("tr.05.long_term_deduction").branch.taken


@pytest.mark.parametrize("track", [Track.CURRENT, Track.REFORM])
@pytest.mark.parametrize("year", [2026, 2027, 2028, 2029])
def test_거주_2년_미만_1주택은_표2가_아니라_표1을_쓴다(rs: RuleSet, year, track):
    """소득세법 시행령 §159의4 — 표2 대상은 "보유기간 중 거주기간이 2년 이상인" 1세대1주택.

    1주택이라는 이유만으로 우대 공제율(최대 80%)을 주면
    12년 보유·거주 0년 사례에서 8천만원 넘게 세액이 어긋난다."""
    if track is Track.REFORM and year < 2027:
        pytest.skip("개편안은 2027년부터")
    case = make_case([("우리집", 15 * EOK, SEOUL)], year)
    base = TransferEvent(
        PropertyId("우리집"), ME, date(year, 6, 1), 30 * EOK, 10 * EOK,
        holding_years=12, residence_years=0,
    )
    lived = replace(base, residence_years=12)

    no_live = compute_transfer_tax(case, base, rs, track=track)
    with_live = compute_transfer_tax(case, lived, rs, track=track)

    assert no_live.long_term_deduction.as_int() < with_live.long_term_deduction.as_int()
    alts = {a.key: a for a in no_live.trace.all_alternatives()}
    assert "ltd_table2" in alts
    assert "159의4" in alts["ltd_table2"].reason_ko


def test_거주기간을_안_받았으면_유리하게_가정하지_않는다(rs: RuleSet):
    """모르면 유리한 쪽으로 기울지 않는다 — 이 프로젝트의 기본 원칙."""
    case = make_case([("우리집", 15 * EOK, SEOUL)], 2026)
    event = TransferEvent(
        PropertyId("우리집"), ME, date(2026, 6, 1), 30 * EOK, 10 * EOK,
        holding_years=12, residence_years=None,
    )
    r = compute_transfer_tax(case, event, rs)
    labels = dict(r.trace.certainty_concerns())
    assert labels, "거주기간 미상인데 확실성 우려가 하나도 안 붙었다"


@pytest.mark.parametrize(
    "year, group, expected",
    [
        # 개조식 p.22 <중과세율> 표 전체 + 상세본 p.85. 정부 원문 두 곳이 일치한다.
        #        현행    ’27     ’28     ’29~
        # 2주택  +20%p  +5%p   +10%p   +20%p
        # 3주택  +30%p  +10%p  +15%p   +30%p
        (2027, "2", "0.05"), (2027, "3+", "0.10"),
        (2028, "2", "0.10"), (2028, "3+", "0.15"),
        (2029, "2", "0.20"), (2029, "3+", "0.30"),
        # 경과조치 — 상세본 p.85 <특례규정> "’26년 양도분도 ’27.1.1. 이후 신고시 완화"
        (2026, "2", "0.05"), (2026, "3+", "0.10"),
    ],
)
def test_중과세율_8칸을_정부_표대로_고정한다(rs: RuleSet, year, group, expected):
    """★ 이 테스트가 없어서 2028년 3주택 +15%p가 0.20으로 들어간 채 335개 테스트를
    통과했다. 세액이 아니라 **표 자체**를 고정해야 전사 오류가 잡힌다.

    기존 테스트는 2채짜리 사례뿐이라 '3+' 분기를 한 번도 타지 않았다."""
    block = rs.resolve(
        "transfer.heavy_surcharge", on=date(year, 6, 1), track=Track.REFORM, house_group=group
    )
    assert str(block.value) == expected, f"{year}년 {group}주택"


@pytest.mark.parametrize("group, expected", [("2", "0.20"), ("3+", "0.30")])
def test_현행_중과세율도_고정한다(rs: RuleSet, group, expected):
    block = rs.resolve(
        "transfer.heavy_surcharge", on=date(2026, 6, 1), track=Track.CURRENT, house_group=group
    )
    assert str(block.value) == expected


def test_조정대상지역_다주택_양도는_중과되고_장특공제도_배제된다(rs: RuleSet):
    """중과와 공제 배제가 함께 온다. 소득세법 §104⑦ + §95② 단서."""
    case, event = multi_case(2026)
    r = compute_transfer_tax(case, event, rs)
    assert r.long_term_deduction.as_int() == 0
    assert r.trace.find("tr.05.long_term_deduction").branch.taken == "배제"
    assert r.trace.find("tr.07.income_tax").branch.taken == "적용"
    assert "중과" in r.trace.find("tr.07.income_tax").substitution


def test_비조정지역_주택_양도는_중과되지_않는다(rs: RuleSet):
    case = make_case([("서울집", 15 * EOK, SEOUL), ("부산집", 8 * EOK, BUSAN)], 2026)
    event = TransferEvent(
        PropertyId("부산집"), ME, date(2026, 6, 1), 12 * EOK, 8 * EOK,
        holding_years=10, residence_years=0,
    )
    r = compute_transfer_tax(case, event, rs)
    assert r.trace.find("tr.07.income_tax").branch.taken == "미적용"
    assert r.long_term_deduction.as_int() > 0


def test_2027_2028년에_중과가_한시_완화되고_2029년에_복귀한다(rs: RuleSet):
    """종부세를 올리면서 매도 창구를 열어주는 설계다.
    **파는 시점이 세액을 크게 가른다** — 이게 상담의 핵심 결론이 된다."""
    taxes = {}
    for year in (2027, 2028, 2029):
        case, event = multi_case(year)
        taxes[year] = compute_transfer_tax(
            case, event, rs, track=Track.REFORM
        ).income_tax.as_int()

    assert taxes[2027] < taxes[2028] < taxes[2029]
    # 2029년 복귀 시 2027년 대비 얼마나 오르는지 — 상담에서 쓸 숫자
    assert taxes[2029] - taxes[2027] > 50_000_000


def test_현행과_2027년_개편안의_중과세율_차이(rs: RuleSet):
    """2주택자: 현행 +20%p → 2027년 +5%p."""
    case, event = multi_case(2027)
    current = compute_transfer_tax(case, event, rs, track=Track.CURRENT)
    reform = compute_transfer_tax(case, event, rs, track=Track.REFORM)
    assert reform.income_tax.as_int() < current.income_tax.as_int()


def test_지정_전_시점에는_화성시도_정상적으로_비규제다(rs: RuleSet):
    """화성 동탄구 지정은 2026-07-01이다. 그 이전 시점에는 '모름'이 아니라
    '지정 안 됨'이 정답이다 — 미확정 코드 문제는 지정 이후에만 발생한다."""
    case = make_case([("화성집", 10 * EOK, "4159000000"), ("부산집", 8 * EOK, BUSAN)], 2026)
    event = TransferEvent(
        PropertyId("화성집"), ME, date(2026, 6, 1), 15 * EOK, 10 * EOK,
        holding_years=10, residence_years=0,
    )
    r = compute_transfer_tax(case, event, rs)
    assert r.trace.find("tr.07.income_tax").branch.taken == "미적용"
    assert "판단 필요" not in r.trace.certainty.labels_ko()


def test_조정대상지역을_모르면_중과를_확정하지_않고_드러낸다(rs: RuleSet):
    """화성시는 2026-07-01 지정분에 동탄구가 들어갔는데 그 법정동코드를 확정하지 못했다.
    유리한 쪽(비중과)으로 계산하되 확실성을 낮추고 '중과 대상이면 크게 오른다'를 알린다."""
    case = make_case([("화성집", 10 * EOK, "4159000000"), ("부산집", 8 * EOK, BUSAN)], 2026)
    event = TransferEvent(
        PropertyId("화성집"), ME, date(2026, 9, 1), 15 * EOK, 10 * EOK,
        holding_years=10, residence_years=0,
    )
    r = compute_transfer_tax(case, event, rs)
    alts = {a.key: a for a in r.trace.all_alternatives()}
    assert "heavy_surcharge_unknown" in alts
    assert alts["heavy_surcharge_unknown"].actionable
    assert "판단 필요" in r.trace.certainty.labels_ko()


# --------------------------------------------------------------------------
# 공제·세율 세부
# --------------------------------------------------------------------------


def test_2028년_공제율표가_문답자료_p38과_일치한다(rs: RuleSet):
    """10년 보유·거주 → 보유 20% + 거주 60% = 80%
    15년 보유·5년 거주 → 보유 20% + 거주 30% = 50%"""
    case = make_case([("본가", 20 * EOK, SEOUL)], 2028)

    def rate_of(holding: int, residence: int) -> float:
        event = TransferEvent(
            PropertyId("본가"), ME, date(2028, 6, 1), 32 * EOK, 12 * EOK,
            holding_years=holding, residence_years=residence,
        )
        r = compute_transfer_tax(case, event, rs, track=Track.REFORM)
        return r.long_term_deduction.as_int() / r.taxable_gain.as_int()

    assert rate_of(10, 10) == pytest.approx(0.80, abs=0.001)
    assert rate_of(15, 5) == pytest.approx(0.50, abs=0.001)


def test_2029년은_보유공제가_사라지고_거주공제만_남는다(rs: RuleSet):
    case = make_case([("본가", 20 * EOK, SEOUL)], 2029)
    event = TransferEvent(
        PropertyId("본가"), ME, date(2029, 6, 1), 32 * EOK, 12 * EOK,
        holding_years=15, residence_years=0,
    )
    r = compute_transfer_tax(case, event, rs, track=Track.REFORM)
    assert r.long_term_deduction.as_int() == 0


def test_장기거주_1주택_기본공제_확대가_적용된다(rs: RuleSet):
    """개편안: 10년 이상 거주 + 양도가액 30억 이하 1주택 → 250만원 → 2,500만원."""
    case = make_case([("본가", 15 * EOK, SEOUL)], 2027)
    event = TransferEvent(
        PropertyId("본가"), ME, date(2027, 6, 1), 20 * EOK, 10 * EOK,
        holding_years=10, residence_years=10,
    )
    r = compute_transfer_tax(case, event, rs, track=Track.REFORM)
    assert r.trace.find("tr.06a.basic_deduction").output.as_int() == 25_000_000
    assert r.trace.find("tr.06a.basic_deduction").branch.taken == "장기 거주 1주택 확대"


def test_양도가액_30억을_넘으면_기본공제_확대를_못_받고_사유가_남는다(rs: RuleSet):
    case = make_case([("본가", 20 * EOK, SEOUL)], 2027)
    event = TransferEvent(
        PropertyId("본가"), ME, date(2027, 6, 1), 32 * EOK, 12 * EOK,
        holding_years=10, residence_years=10,
    )
    r = compute_transfer_tax(case, event, rs, track=Track.REFORM)
    assert r.trace.find("tr.06a.basic_deduction").output.as_int() == 2_500_000
    alts = {a.key for a in r.trace.all_alternatives()}
    assert "basic_deduction_long_residence" in alts


def test_기본세율표가_소득세법_제55조와_일치한다(rs: RuleSet):
    """법문의 기초금액을 그대로 옮겼는지 누적 계산과 교차검증한다."""
    from realestate_tax.rules.schema import Bracket, RateTable

    table = rs.resolve(
        "transfer.basic_rate_table", on=date(2026, 6, 1), track=Track.CURRENT
    ).block.table
    stripped = RateTable(tuple(Bracket(upto=b.upto, rate=b.rate) for b in table.brackets))
    for base in (10_000_000, 50_000_000, 200_000_000, 647_500_000, 2_197_500_000):
        assert table.tax_for(base)[0] == stripped.tax_for(base)[0], f"{base:,}"

    # 문답자료 사례①의 '27년 과세표준으로 직접 검산
    assert table.tax_for(647_500_000)[0] == 174_060_000 + int(147_500_000 * 0.42)


# --------------------------------------------------------------------------
# 지방소득세 · 감사 추적
# --------------------------------------------------------------------------


def test_개인지방소득세는_산출세액의_10퍼센트로_별도_계산된다(rs: RuleSet):
    """정부 문답자료의 '산출세액'에는 안 들어간다. 합쳐 버리면 골든 대조가 깨진다."""
    case = make_case([("본가", 20 * EOK, SEOUL)], 2026)
    event = TransferEvent(
        PropertyId("본가"), ME, date(2026, 6, 1), 32 * EOK, 12 * EOK,
        holding_years=10, residence_years=2,
    )
    r = compute_transfer_tax(case, event, rs)
    assert r.local_income_tax.as_int() == r.income_tax.as_int() // 10
    assert r.total.as_int() == r.income_tax.as_int() + r.local_income_tax.as_int()


def test_모든_숫자에_근거_조문이_붙는다(rs: RuleSet):
    case = make_case([("본가", 20 * EOK, SEOUL)], 2026)
    event = TransferEvent(
        PropertyId("본가"), ME, date(2026, 6, 1), 32 * EOK, 12 * EOK,
        holding_years=10, residence_years=2,
    )
    cites = {
        r_.basis.cite_ko()
        for r_ in compute_transfer_tax(case, event, rs).trace.all_rules()
        if r_.basis
    }
    assert "소득세법 제55조 제1항" in cites
    assert "소득세법 제89조 제1항 제3호" in cites
    assert "소득세법 제95조 제2항" in cites
    assert "소득세법 제103조 제1항" in cites


def test_취득일이_양도일보다_늦으면_거부한다():
    with pytest.raises(ValueError, match="취득일"):
        TransferEvent(
            PropertyId("h"), ME, date(2026, 1, 1), 1000, 900,
            acquisition_date=date(2027, 1, 1),
        )


def test_지분_소유면_안분_단계가_추적에_남는다(rs: RuleSet):
    case = make_case([("본가", 20 * EOK, SEOUL)], 2026)
    event = TransferEvent(
        PropertyId("본가"), ME, date(2026, 6, 1), 32 * EOK, 12 * EOK,
        holding_years=10, residence_years=2, share=Fraction(1, 2),
    )
    r = compute_transfer_tax(case, event, rs)
    share_node = r.trace.find("tr.09.share")
    assert share_node is not None
    assert share_node.output.as_int() == r.total.as_int() // 2


# --------------------------------------------------------------------------
# ★ 단기보유 단일세율 — 소득세법 §104①2·3 + 후단 "큰 것"
# --------------------------------------------------------------------------


def test_보유_1년_미만_주택은_70퍼센트_단일세율과_비교한다(rs: RuleSet):
    """감사가 보고한 그 사례. 비과세 요건 게이트로 과세 대상이 된 뒤,
    세율까지 맞아야 2.29억이 나온다 — 기본세율 45%로 계산하면 여전히 틀리다."""
    case, event = short_hold_case()
    r = compute_transfer_tax(case, event, rs)

    base = r.taxable_base.as_int()
    assert r.income_tax.as_int() == int(base * Fraction(70, 100))
    assert "max(" in r.trace.find("tr.07.income_tax").substitution
    assert "70%" in r.trace.find("tr.07.income_tax").substitution


def test_보유_1년_이상_2년_미만은_60퍼센트다(rs: RuleSet):
    case, event = short_hold_case()
    event = replace(event, holding_years=1, transfer_date=date(2027, 3, 1))
    r = compute_transfer_tax(case, event, rs)
    assert r.income_tax.as_int() == int(r.taxable_base.as_int() * Fraction(60, 100))


def test_중과가_붙으면_단기세율이_질_수_있고_사유가_남는다(rs: RuleSet):
    """★ 기본세율은 누진이라 실효세율이 45%를 못 넘는다. 그래서 중과가 없으면
    단기세율(70%/60%)이 **항상 이긴다.** 비교가 갈리는 곳은 중과 구간뿐이다 —
    3주택 중과 +30%p면 45+30=75%가 되어 70%를 넘는다.
    §104⑦에 후단이 따로 붙어 있는 이유가 그것이다."""
    case = make_case(
        [("A", 15 * EOK, SEOUL), ("B", 8 * EOK, BUSAN), ("C", 8 * EOK, BUSAN)], 2026
    )
    event = TransferEvent(
        PropertyId("A"), ME, date(2026, 6, 1), 60 * EOK, 10 * EOK,
        holding_years=1, residence_years=0,
    )
    r = compute_transfer_tax(case, event, rs)
    alts = {a.key for a in r.trace.all_alternatives()}
    assert "short_term_rate" in alts, "중과 75%가 단기 60%를 이겨야 한다"
    assert "%p [중과]" in r.trace.find("tr.07.income_tax").substitution


def test_보유_2년_이상이면_단기세율을_보지_않는다(rs: RuleSet):
    case = make_case([("본가", 20 * EOK, SEOUL)], 2026)
    event = TransferEvent(
        PropertyId("본가"), ME, date(2026, 6, 1), 32 * EOK, 12 * EOK,
        holding_years=10, residence_years=2,
    )
    r = compute_transfer_tax(case, event, rs)
    assert "max(" not in r.trace.find("tr.07.income_tax").substitution


# --------------------------------------------------------------------------
# ★ 중과 한시완화의 2년 보유 요건 — 개조식 p.22 각주 "* 2년 이상 보유"
# --------------------------------------------------------------------------


@pytest.mark.parametrize("year, relieved, full", [(2027, "0.05", "0.20"), (2028, "0.10", "0.20")])
def test_보유_2년_미만이면_중과_한시완화를_못_받는다(rs: RuleSet, year, relieved, full):
    """보유 1.5년 2주택자에게 +5%p를 주면 15%p를 덜 매긴다."""
    case = make_case([("서울집", 15 * EOK, SEOUL), ("부산집", 8 * EOK, BUSAN)], year)
    short = TransferEvent(
        PropertyId("서울집"), ME, date(year, 6, 1), 20 * EOK, 15 * EOK,
        holding_years=1, residence_years=0,
    )
    long = replace(short, holding_years=10)

    r_short = compute_transfer_tax(case, short, rs, track=Track.REFORM)
    r_long = compute_transfer_tax(case, long, rs, track=Track.REFORM)

    # 완화를 받은 쪽만 완화세율이 대입식에 찍힌다.
    # (보유 1년은 단기세율까지 겹쳐 대입식이 max(...)로 바뀌므로 대안으로 확인한다)
    assert f"{float(relieved) * 100:g}%p" in r_long.trace.find("tr.07.income_tax").substitution

    alts = {a.key: a for a in r_short.trace.all_alternatives()}
    assert "heavy_relief" in alts, "완화를 못 받았다는 사실이 화면에 안 남는다"
    assert "2년 요건에 미달" in alts["heavy_relief"].reason_ko
    assert "heavy_relief" not in {a.key for a in r_long.trace.all_alternatives()}


def test_보유기간을_모르면_단기세율을_확정으로_말하지_않는다(rs: RuleSet):
    """★ 세액은 보수적(높은) 쪽으로 가되, **가정을 판정인 척하면 안 된다.**

    보유기간이 None이면 `held = holding_years or 0`으로 0이 되어 '1년 미만'
    단일세율 70%를 적용한다. 방향은 맞지만 그건 판정이 아니라 가정이다.
    사용자는 70%가 확정인 줄 알고 매도를 포기할 수 있다."""
    case = make_case([("본가", 15 * EOK, SEOUL)], 2026)
    unknown = TransferEvent(
        PropertyId("본가"), ME, date(2026, 6, 1), 30 * EOK, 10 * EOK,
        holding_years=None, residence_years=10,
    )
    r = compute_transfer_tax(case, unknown, rs)

    alts = {a.key: a for a in r.trace.all_alternatives()}
    assert "short_term_rate_assumed" in alts, "가정했다는 사실이 화면에 없다"
    assert alts["short_term_rate_assumed"].actionable
    assert "취득일" in alts["short_term_rate_assumed"].reason_ko
    assert "판단 필요" in dict(r.trace.certainty_concerns())


def test_보유기간을_알면_가정_안내가_뜨지_않는다(rs: RuleSet):
    case = make_case([("본가", 15 * EOK, SEOUL)], 2026)
    known = TransferEvent(
        PropertyId("본가"), ME, date(2026, 6, 1), 30 * EOK, 10 * EOK,
        holding_years=0, residence_years=10,
    )
    r = compute_transfer_tax(case, known, rs)
    assert "short_term_rate_assumed" not in {a.key for a in r.trace.all_alternatives()}
