"""상생임대주택 특례가 **실제 세액을 바꾸는지** 관통 테스트.

판정 엔진(test_sangsaeng.py)과 룰셋(test_sangsaeng_rules.py)이 각각 맞아도,
그 판정이 양도세 계산에 연결되지 않으면 세액은 그대로다. 이 프로젝트는 정확히
그 사고를 겪었다 — 도메인이 모델링한 사실을 엔진이 읽지 않아 여섯 번 같은 병이 났다.
그래서 여기서는 **원 단위 세액 차이**로 연결을 확인한다.

시나리오는 손으로 적은 상담 메모의 사건이다:
  · 취득 당시 조정대상지역 아파트 1채 → 비과세에 2년 거주요건이 붙는다
  · **실거주 0년** — 그래서 상생임대 특례로 거주요건을 면제받는다
  · 상생임대차계약 2025-02-01 ~ 2027-01-31
"""

from __future__ import annotations

from datetime import date

import pytest

from realestate_tax.domain import (
    Household,
    HouseholdId,
    LeaseSpell,
    Ownership,
    Person,
    PersonId,
    PriceFact,
    Property,
    PropertyKind,
    PropertyId,
    TaxCase,
)
from realestate_tax.engine.transfer_tax import TransferEvent, compute_transfer_tax
from realestate_tax.rules import RuleSet, Track, default_ruleset_root

ME = PersonId("me")
HOUSE = PropertyId("본가")
SEOUL = "1168010100"  # 강남구 — 취득 당시 조정대상지역
EOK = 100_000_000

PRIOR = LeaseSpell(
    property_id=HOUSE,
    start=date(2023, 2, 1),
    end=date(2025, 1, 31),
    deposit=5 * EOK,
    contracted_on=date(2022, 12, 10),
    down_payment_evidenced=True,
)
SANGSAENG = LeaseSpell(
    property_id=HOUSE,
    start=date(2025, 2, 1),
    end=date(2027, 1, 31),
    deposit=520_000_000,  # 4% 인상
    contracted_on=date(2024, 12, 15),
    down_payment_evidenced=True,
)


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


def make_case(*leases: LeaseSpell, year: int = 2027) -> TaxCase:
    hh = HouseholdId("hh")
    return TaxCase(
        year=year,
        persons=(Person(id=ME, household_id=hh, birth_date=date(1970, 1, 1)),),
        households=(Household(id=hh, member_ids=(ME,)),),
        properties=(
            Property(
                id=HOUSE,
                kind=PropertyKind.APARTMENT,
                legal_dong_code=SEOUL,
                display_name="본가",
                published_prices=(PriceFact(year, 20 * EOK),),
            ),
        ),
        ownerships=(Ownership(ME, HOUSE, acquired_on=date(2017, 3, 1)),),
        leases=leases,
    )


def event(on: date) -> TransferEvent:
    """양도가액 32억 / 취득가액 12억 / 10년 보유 / **거주 0년**."""
    return TransferEvent(
        property_id=HOUSE,
        person_id=ME,
        transfer_date=on,
        transfer_price=32 * EOK,
        acquisition_price=12 * EOK,
        holding_years=10,
        residence_years=0,
    )


def run(rs: RuleSet, case: TaxCase, on: date, track: Track = Track.REFORM):
    return compute_transfer_tax(case, event(on), rs, track=track)


# --------------------------------------------------------------------------
# 연결 확인 — 임대차 이력이 세액을 바꾼다
# --------------------------------------------------------------------------


def test_상생임대가_없으면_거주요건_미달로_비과세를_못_받는다(rs: RuleSet):
    """취득 당시 조정대상지역인데 거주 0년이므로 §154①의 거주요건에서 탈락한다.
    양도차익 20억이 **전액 과세**된다."""
    r = run(rs, make_case(), date(2027, 6, 1))
    assert r.taxable_gain.as_int() == r.gain.as_int()  # 안분 없음 = 비과세 없음


def test_상생임대가_있으면_거주_0년이어도_비과세를_받는다(rs: RuleSet):
    """§155의3① — 상생임대주택은 §154①의 거주기간 제한을 받지 않는다.

    양도가액 32억이므로 12억 초과분만 과세된다:
      과세대상 = 양도차익 × (32억 − 12억) ÷ 32억 = 20억 × 0.625 = 12.5억
    """
    r = run(rs, make_case(PRIOR, SANGSAENG), date(2027, 6, 1))
    assert r.taxable_gain.as_int() == 1_250_000_000
    assert r.trace.find("tr.03a.sangsaeng").output.amount is True


def test_비과세_차이가_세액으로_나타난다(rs: RuleSet):
    """판정이 계산에 실제로 닿았는지는 **세액 차이**로만 증명된다."""
    without = run(rs, make_case(), date(2027, 6, 1))
    with_ = run(rs, make_case(PRIOR, SANGSAENG), date(2027, 6, 1))
    assert with_.income_tax.as_int() < without.income_tax.as_int()
    # 억 단위로 갈린다 — 이 격차가 이 기능의 존재 이유다
    assert without.income_tax.as_int() - with_.income_tax.as_int() > 2 * EOK


# --------------------------------------------------------------------------
# ★★ 면제되는 것은 '제한'이지 거주기간이 아니다
# --------------------------------------------------------------------------


# 늦게 끝나는 상생임대차 — 양도기한이 '29.12.31.(절대 상한)까지 열린다.
# 재혁 사건(기한 2028-01-31)으로는 '29년 공제율을 관찰할 수 없어서 따로 만든다.
PRIOR_LATE = LeaseSpell(
    property_id=HOUSE,
    start=date(2024, 6, 1),
    end=date(2026, 11, 30),
    deposit=5 * EOK,
    contracted_on=date(2024, 4, 10),
    down_payment_evidenced=True,
)
SANGSAENG_LATE = LeaseSpell(
    property_id=HOUSE,
    # ⚠️ 상생임대차계약은 **체결과 임대개시가 모두** '26.12.31. 안이어야 한다
    #    (상세본 p.78 ➋). 개시는 '26년에 하고 종료만 늦은 계약을 쓴다.
    #    예전 픽스처는 '27.1.1. 개시라 법적으로 성립할 수 없는 사건이었다.
    start=date(2026, 12, 1),
    end=date(2028, 11, 30),
    deposit=520_000_000,
    contracted_on=date(2026, 10, 15),
    down_payment_evidenced=True,
)


@pytest.mark.parametrize(
    "on, leases, expected_rate",
    [
        # 재혁 사건 — 기한 2028-01-31. 그 안에서만 관찰할 수 있다.
        (date(2027, 6, 1), "재혁", "0.40"),   # 보유 연4% × 10년 (거주공제 0%)
        (date(2028, 1, 31), "재혁", "0.20"),  # 보유 연2% × 10년 — 개편안이 보유공제를 깎는다
        # 기한이 '29.12.31.까지 열린 사건이라야 '29년을 볼 수 있다.
        (date(2029, 6, 1), "늦은", "0.00"),   # 보유공제 폐지 · 거주 0년 → 공제가 사라진다
    ],
)
def test_장특공제는_실거주_0년으로_계산된다(rs: RuleSet, on, leases, expected_rate):
    """★★ 이 프로젝트에서 가장 비싼 구분.

    §155의3①이 §159의4(장기보유특별공제)를 지목하므로 상생임대주택은 **표2에
    들어간다**. 그러나 §159의4가 면제해 주는 것은 "거주기간이 2년 이상" 이라는
    **진입 요건**이고, 표2 안의 거주기간 공제율은 실제 거주기간으로 계산한다.
    거주기간을 2년으로 의제하는 규정은 어디에도 없다.

    그 결과 2026 개편안 아래에서 실거주 0년 상생임대주택의 공제율은
    **'27년 40% → '28년 20% → '29년 0%**로 무너진다. 특례가 살아 있어도 그렇다.
    비과세 12억은 지켜지지만 장기보유특별공제는 지켜지지 않는다.

    만약 거주 2년을 의제한다면 '27년 48%('40+8')가 나와야 한다. 그렇게 나오면
    이 테스트가 깨진다 — 그게 이 테스트의 목적이다.

    ⚠️ 날짜 선택에 함정이 있다. 개편안의 양도기한을 넘긴 날짜를 쓰면 특례 자체가
       사라져 비과세부터 무너지므로, 공제율이 아니라 **다른 것**을 재게 된다.
       실제로 이 테스트를 처음 쓸 때 2028-06-01을 넣어 그 함정에 걸렸다.
    """
    pair = (PRIOR, SANGSAENG) if leases == "재혁" else (PRIOR_LATE, SANGSAENG_LATE)
    case = make_case(*pair, year=on.year)
    r = compute_transfer_tax(case, event(on), rs, track=Track.REFORM)

    # 전제 확인 — 특례가 살아 있어야 공제율을 관찰하는 의미가 있다
    assert r.trace.find("tr.03a.sangsaeng").output.amount is True, "양도기한을 넘긴 날짜다"

    rate = r.long_term_deduction.as_int() / r.taxable_gain.as_int()
    assert f"{rate:.2f}" == expected_rate, (
        f"{on} 공제율 {rate:.4f} — 장특공제 {r.long_term_deduction.as_int():,}원 / "
        f"과세대상 {r.taxable_gain.as_int():,}원"
    )


def test_표2_진입은_열리고_거주공제만_0이다(rs: RuleSet):
    """표1(다주택자용 연2%·최대30%)로 떨어진 것이 아니라 표2에 들어갔는지 확인한다.

    둘은 '27년에 우연히 값이 비슷해 보일 수 있으나(표2 보유 40% vs 표1 30%)
    다른 규칙이다. 표1로 떨어졌다면 공제율이 0.30이어야 한다.
    """
    r = run(rs, make_case(PRIOR, SANGSAENG), date(2027, 6, 1))
    assert "표1" not in r.trace.find("tr.05.long_term_deduction").branch.taken


# --------------------------------------------------------------------------
# 개편안 양도기한 — 하루 넘기면 특례가 사라진다
# --------------------------------------------------------------------------


def test_양도기한_안에_팔면_특례가_유지된다(rs: RuleSet):
    """상생임대차계약 종료 2027-01-31 → 기한 2028-01-31(상세본 p.78 ➋)."""
    r = run(rs, make_case(PRIOR, SANGSAENG, year=2028), date(2028, 1, 31))
    assert r.taxable_gain.as_int() == 1_250_000_000
    assert r.trace.find("tr.03a.sangsaeng").output.amount is True


def test_양도기한을_하루_넘기면_비과세가_사라진다(rs: RuleSet):
    """2028-02-01 양도 — 기한을 하루 넘겼다.

    거주요건 면제가 사라지므로 거주 0년인 이 사건은 §154① 거주요건에서 탈락하고,
    양도차익 20억이 전액 과세된다. **하루로 억이 갈린다.**
    """
    ok = run(rs, make_case(PRIOR, SANGSAENG, year=2028), date(2028, 1, 31))
    late = run(rs, make_case(PRIOR, SANGSAENG, year=2028), date(2028, 2, 1))

    assert late.taxable_gain.as_int() == late.gain.as_int()  # 비과세 안분이 사라졌다
    assert late.income_tax.as_int() > ok.income_tax.as_int()
    assert "초과" in late.trace.find("tr.03a.sangsaeng").branch.taken


def test_현행_트랙에는_양도기한이_없어_2030년에_팔아도_유지된다(rs: RuleSet):
    """양도기한은 개편안이 신설하는 것이다. 개편안이 부결되면 기한이 없다.

    두 트랙을 나란히 보여주지 않는 것 자체가 오류라는 이 프로젝트의 전제가
    여기서 실제 금액 차이로 나타난다.
    """
    r = compute_transfer_tax(
        make_case(PRIOR, SANGSAENG, year=2030),
        event(date(2030, 6, 1)),
        rs,
        track=Track.CURRENT,
    )
    assert r.taxable_gain.as_int() == 1_250_000_000


# --------------------------------------------------------------------------
# 판정 불가는 유리하게 처리하지 않는다
# --------------------------------------------------------------------------


def test_계약금_증빙을_모르면_비과세를_주지_않는다(rs: RuleSet):
    """모르는 것을 유리하게 적용하면 과소신고가 된다."""
    unknown = LeaseSpell(
        property_id=HOUSE,
        start=date(2025, 2, 1),
        end=date(2027, 1, 31),
        deposit=520_000_000,
        contracted_on=date(2024, 12, 15),
        down_payment_evidenced=None,
    )
    r = run(rs, make_case(PRIOR, unknown), date(2027, 6, 1))
    assert r.taxable_gain.as_int() == r.gain.as_int()
    assert r.trace.find("tr.03a.sangsaeng").branch.taken == "판정 불가"


def test_요건에_걸려_탈락해도_이유와_고칠_곳을_말한다(rs: RuleSet):
    """★ 2026-08-13 감사(이용자 관점) — 탈락하면 화면이 아무 말도 하지 않았다.

    적용·판정불가일 때만 노드를 남겨서, 임대차를 열심히 입력한 사용자가
    왜 안 되는지도 무엇을 고쳐야 하는지도 알 수 없었다.
    """
    short = LeaseSpell(
        property_id=HOUSE, start=date(2025, 2, 1), end=date(2026, 12, 31),  # 23개월
        deposit=520_000_000, contracted_on=date(2024, 12, 15),
        down_payment_evidenced=True,
    )
    r = run(rs, make_case(PRIOR, short), date(2027, 6, 1))
    node = r.trace.find("tr.03a.sangsaeng")
    assert node is not None, "탈락했다고 노드를 통째로 지웠다"
    assert node.output.amount is False
    assert "23개월" in node.substitution, "무엇이 모자란지 말하지 않는다"
    assert "다섯 가지를 모두" in node.substitution, "어디를 고쳐야 하는지 말하지 않는다"


def test_승계받은_계약뿐이면_비과세를_주지_않는다(rs: RuleSet):
    """§155의3①1호 괄호 — 세입자가 살던 집을 사서 물려받은 계약은
    직전임대차계약이 될 수 없다."""
    from realestate_tax.domain import LeaseOrigin

    succeeded = LeaseSpell(
        property_id=HOUSE,
        start=date(2023, 2, 1),
        end=date(2025, 1, 31),
        deposit=5 * EOK,
        contracted_on=date(2022, 12, 10),
        origin=LeaseOrigin.SUCCEEDED,
        down_payment_evidenced=True,
    )
    r = run(rs, make_case(succeeded, SANGSAENG), date(2027, 6, 1))
    assert r.taxable_gain.as_int() == r.gain.as_int()
