"""보유·거주기간 도출 — 사실에서 뽑고, 없으면 없다고 말한다.

★ 시뮬레이션 하네스가 잡은 첫 버그(SIM-01, 2026-08-05)의 회귀 고정.

  `JongbuseOptions`는 "None이면 취득일·거주 이력에서 도출"이라고 문서에 적어 뒀는데
  **도출 코드가 없었다.** 그래서 70세·10년 거주 1주택자가 옵션을 손으로 넘기지
  않으면 세액공제가 40%(연령분)만 잡히고 거주분 40%가 통째로 빠졌다.

  골든 테스트 499개가 이걸 못 잡은 이유는 하나다 — **테스트가 옵션을 먹여줬기**
  때문이다. 아는 값을 손으로 넣어주는 테스트는 "사실에서 도출되는가"를 검증하지
  못한다. 시뮬레이션은 사용자처럼 사실만 주고 나머지를 엔진에 맡겼기에 잡혔다.
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
    ResidenceSpell,
    TaxCase,
)
from realestate_tax.domain.certainty import DeterminationQuality
from realestate_tax.engine.jongbuse import JongbuseOptions, compute_jongbuse
from realestate_tax.rules import RuleSet, Track, default_ruleset_root
from realestate_tax.rules.resolver import load_ruleset

SEOUL = "1168010100"
P1 = PersonId("p1")
H1 = PropertyId("h1")


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return load_ruleset(default_ruleset_root())


def _case(
    *,
    acquired: date | None,
    spells: tuple[ResidenceSpell, ...] = (),
    birth: date = date(1956, 1, 1),
    price: int = 3_000_000_000,
) -> TaxCase:
    return TaxCase(
        year=2026,
        persons=(Person(id=P1, birth_date=birth, household_id=HouseholdId("hh")),),
        households=(Household(id=HouseholdId("hh"), member_ids=(P1,)),),
        properties=(
            Property(
                id=H1,
                kind=PropertyKind.APARTMENT,
                legal_dong_code=SEOUL,
                published_prices=(PriceFact(2026, price),),
            ),
        ),
        ownerships=(Ownership(person_id=P1, property_id=H1, acquired_on=acquired),),
        residences=spells,
    )


def _credit(case: TaxCase, rs: RuleSet, **opts) -> tuple[int, DeterminationQuality]:
    result = compute_jongbuse(
        case, P1, rs, track=Track.CURRENT, options=JongbuseOptions(**opts)
    )
    node = result.trace.find("jb.10.tax_credit")
    assert node is not None
    return node.output.as_int(), node.output.certainty.determination


def test_취득일이_있으면_보유기간을_옵션_없이_도출한다(rs: RuleSet):
    """사용자가 취득일을 입력했으면 보유기간을 또 묻지 않는다.

    같은 사실을 두 곳에서 받으면 둘이 어긋날 수 있고, 어긋나면 어느 쪽이
    계산에 쓰였는지 아무도 모른다.
    """
    plain, quality = _credit(_case(acquired=date(2016, 1, 1)), rs)
    assert quality is DeterminationQuality.DECIDED
    # 70세(40%) + 보유 10년(40%) = 80%
    fed, _ = _credit(_case(acquired=date(2016, 1, 1)), rs, holding_years=10)
    assert plain == fed > 0


def test_거주이력이_있으면_거주기간을_도출한다(rs: RuleSet):
    case = _case(
        acquired=date(2016, 1, 1),
        spells=(ResidenceSpell(person_id=P1, property_id=H1, start=date(2016, 1, 1)),),
    )
    result = compute_jongbuse(case, P1, rs, track=Track.CURRENT)
    node = result.trace.find("jb.10.tax_credit")
    assert "거주 10년" in node.branch.detail_ko


def test_옵션이_도출값을_이긴다(rs: RuleSet):
    """배우자 상속 시 피상속인 보유기간 통산처럼 엔진이 모르는 특칙이 있다.
    사용자가 직접 말한 값을 도출값이 덮으면 그 특칙을 영영 반영할 수 없다."""
    case = _case(acquired=date(2024, 1, 1))  # 도출하면 2년
    short, _ = _credit(case, rs)
    long, _ = _credit(case, rs, holding_years=15)
    assert long > short


def test_기간을_모르면_확정인_척하지_않는다(rs: RuleSet):
    """취득일도 거주 이력도 없으면 보유·거주 공제분이 **확정이 아니다.**

    연령공제(40%)는 생년월일만으로 확정되므로 그대로 나온다. 확정된 축까지
    판정 불가로 끌어내리면 반대 방향의 거짓말이 된다 — 아는 것도 모른다고 하는 것.
    금액은 나오되 **배지가 붙는다**는 조합이 정직한 답이다."""
    known, _ = _credit(_case(acquired=date(2016, 1, 1)), rs)
    amount, quality = _credit(_case(acquired=None), rs)
    assert quality is DeterminationQuality.UNDECIDABLE
    assert 0 < amount < known  # 연령분만 남고 보유분이 빠진다
    assert amount * 2 == known  # 40% vs 80%


def test_모르면_행동_가능한_안내를_남긴다(rs: RuleSet):
    """판정 불가로 끝내지 않고 '무엇을 입력하면 되는지'를 알려준다."""
    result = compute_jongbuse(_case(acquired=None), P1, rs, track=Track.CURRENT)
    keys = {a.key for a in result.trace.all_alternatives()}
    assert "holding_residence_period" in keys
    alt = next(a for a in result.trace.all_alternatives() if a.key == "holding_residence_period")
    assert alt.actionable


def test_겹치는_거주구간을_두_번_세지_않는다(rs: RuleSet):
    """한 기간을 두 줄로 나눠 입력해도 거주기간이 부풀지 않는다.

    구간을 병합하지 않고 더하면 살지도 않은 기간이 공제로 둔갑한다."""
    overlapping = _case(
        acquired=date(2016, 1, 1),
        spells=(
            ResidenceSpell(person_id=P1, property_id=H1, start=date(2016, 1, 1), end=date(2024, 1, 1)),
            ResidenceSpell(person_id=P1, property_id=H1, start=date(2018, 1, 1), end=date(2024, 1, 1)),
        ),
    )
    result = compute_jongbuse(overlapping, P1, rs, track=Track.CURRENT)
    node = result.trace.find("jb.10.tax_credit")
    assert "거주 8년" in node.branch.detail_ko  # 6년을 더해 14년이 되면 안 된다


def test_지분을_나눠_취득했으면_가장_이른_날이_기산일이다(rs: RuleSet):
    """추가 취득이 보유기간을 리셋하면 안 된다."""
    from fractions import Fraction

    case = TaxCase(
        year=2026,
        persons=(Person(id=P1, birth_date=date(1956, 1, 1), household_id=HouseholdId("hh")),),
        households=(Household(id=HouseholdId("hh"), member_ids=(P1,)),),
        properties=(
            Property(
                id=H1,
                kind=PropertyKind.APARTMENT,
                legal_dong_code=SEOUL,
                published_prices=(PriceFact(2026, 3_000_000_000),),
            ),
        ),
        ownerships=(
            Ownership(person_id=P1, property_id=H1, share=Fraction(1, 2), acquired_on=date(2010, 1, 1)),
            Ownership(person_id=P1, property_id=H1, share=Fraction(1, 2), acquired_on=date(2025, 1, 1)),
        ),
    )
    result = compute_jongbuse(case, P1, rs, track=Track.CURRENT)
    assert "보유 16년" in result.trace.find("jb.10.tax_credit").branch.detail_ko


def test_나눠_취득해도_1세대1주택자_지위를_잃지_않는다(rs: RuleSet):
    """SIM-02 — 소유권 **행**이 아니라 **주택**을 센다.

    한 집을 1/2 + 1/2로 나눠 취득하는 것은 흔하다(추가 매수, 배우자 지분 증여,
    공동상속인 지분 매수). 행을 세면 한 채가 두 채로 읽혀 기본공제가 12억→9억,
    세액공제가 최대 80%→0%로 떨어진다. 사용자는 아무 잘못도 하지 않았는데.
    """
    from fractions import Fraction

    def build(ownerships) -> TaxCase:
        return TaxCase(
            year=2026,
            persons=(Person(id=P1, birth_date=date(1956, 1, 1), household_id=HouseholdId("hh")),),
            households=(Household(id=HouseholdId("hh"), member_ids=(P1,)),),
            properties=(
                Property(
                    id=H1,
                    kind=PropertyKind.APARTMENT,
                    legal_dong_code=SEOUL,
                    published_prices=(PriceFact(2026, 3_000_000_000),),
                ),
            ),
            ownerships=ownerships,
        )

    one_row = build((Ownership(person_id=P1, property_id=H1, acquired_on=date(2010, 1, 1)),))
    two_rows = build(
        (
            Ownership(person_id=P1, property_id=H1, share=Fraction(1, 2), acquired_on=date(2010, 1, 1)),
            Ownership(person_id=P1, property_id=H1, share=Fraction(1, 2), acquired_on=date(2010, 1, 1)),
        )
    )
    a = compute_jongbuse(one_row, P1, rs, track=Track.CURRENT)
    b = compute_jongbuse(two_rows, P1, rs, track=Track.CURRENT)
    assert a.total.as_int() == b.total.as_int(), "같은 집·같은 지분을 어떻게 적었느냐로 세액이 달라지면 안 된다"


# --------------------------------------------------------------------------
# 양도세 — 같은 병이 여기서도 났다 (SIM-06)
# --------------------------------------------------------------------------


def test_양도세도_취득일에서_보유기간을_도출한다(rs: RuleSet):
    """SIM-06 — 취득일이 이벤트에 적혀 있는데 보유기간을 None으로 뒀다.

    결과가 참혹했다. 10년 보유·10년 거주 1주택자가 12억에 팔아도
      · 비과세 → "보유기간 미상"으로 요건 미충족
      · 장특공제 → "보유 0년 < 3년"으로 0원
      · 세율 → 보유 0년이라 **1년 미만 70% 단일세율**
    셋이 한꺼번에 걸려 차익 전액에 70%가 붙었다. 실제 세액은 0원이다.
    """
    from realestate_tax.engine.transfer_tax import TransferEvent, compute_transfer_tax

    case = TaxCase(
        year=2027,
        persons=(Person(id=P1, birth_date=date(1970, 1, 1), household_id=HouseholdId("hh")),),
        households=(Household(id=HouseholdId("hh"), member_ids=(P1,)),),
        properties=(
            Property(
                id=H1,
                kind=PropertyKind.APARTMENT,
                legal_dong_code="2647010100",  # 비조정지역 — 거주요건 무관
                published_prices=(PriceFact(2027, 900_000_000),),
            ),
        ),
        ownerships=(Ownership(person_id=P1, property_id=H1, acquired_on=date(2016, 4, 1)),),
        residences=(ResidenceSpell(person_id=P1, property_id=H1, start=date(2016, 4, 1)),),
    )
    event = TransferEvent(
        property_id=H1,
        person_id=P1,
        transfer_date=date(2027, 6, 15),
        transfer_price=1_200_000_000,
        acquisition_price=500_000_000,
    )
    result = compute_transfer_tax(case, event, rs, track=Track.CURRENT)

    node = result.trace.find("tr.03a.exemption_requirements")
    assert "보유 11년" in node.substitution
    assert "거주 11년" in node.substitution
    # 양도가액 12억 이하 1세대1주택 = 비과세 (소득세법 §89①3호)
    assert result.taxable_gain.as_int() == 0
    assert result.total.as_int() == 0


def test_이벤트에_적은_기간이_도출값을_이긴다(rs: RuleSet):
    """배우자 상속분 통산처럼 엔진이 모르는 특칙은 사용자만 안다."""
    from realestate_tax.engine.transfer_tax import TransferEvent, compute_transfer_tax

    case = TaxCase(
        year=2027,
        persons=(Person(id=P1, birth_date=date(1970, 1, 1), household_id=HouseholdId("hh")),),
        households=(Household(id=HouseholdId("hh"), member_ids=(P1,)),),
        properties=(
            Property(
                id=H1,
                kind=PropertyKind.APARTMENT,
                legal_dong_code="2647010100",
                published_prices=(PriceFact(2027, 900_000_000),),
            ),
        ),
        ownerships=(Ownership(person_id=P1, property_id=H1, acquired_on=date(2026, 4, 1)),),
    )
    event = TransferEvent(
        property_id=H1, person_id=P1, transfer_date=date(2027, 6, 15),
        transfer_price=2_000_000_000, acquisition_price=500_000_000,
        holding_years=20,
    )
    result = compute_transfer_tax(case, event, rs, track=Track.CURRENT)
    assert "보유 20년" in result.trace.find("tr.03a.exemption_requirements").substitution


def test_취득일이_없으면_보유_0년으로_단언하지_않는다(rs: RuleSet):
    """0년과 미상은 다르다. 0년으로 내리면 **확정적으로 불리한 판정**이 나간다."""
    from realestate_tax.engine.transfer_tax import TransferEvent, compute_transfer_tax

    case = TaxCase(
        year=2027,
        persons=(Person(id=P1, birth_date=date(1970, 1, 1), household_id=HouseholdId("hh")),),
        households=(Household(id=HouseholdId("hh"), member_ids=(P1,)),),
        properties=(
            Property(
                id=H1, kind=PropertyKind.APARTMENT, legal_dong_code="2647010100",
                published_prices=(PriceFact(2027, 900_000_000),),
            ),
        ),
        ownerships=(Ownership(person_id=P1, property_id=H1, acquired_on=None),),
    )
    event = TransferEvent(
        property_id=H1, person_id=P1, transfer_date=date(2027, 6, 15),
        transfer_price=2_000_000_000, acquisition_price=500_000_000,
    )
    result = compute_transfer_tax(case, event, rs, track=Track.CURRENT)
    assert "short_term_rate_assumed" in {a.key for a in result.trace.all_alternatives()}


def test_거주_이력이_없는_것과_0년은_다르다(rs: RuleSet):
    """이력이 한 줄도 없으면 '안 살았다'가 아니라 '모른다'다."""
    from realestate_tax.engine import periods

    case = _case(acquired=date(2016, 1, 1))
    assert periods.residence_years(case, P1, H1, date(2026, 6, 1)) is None

    lived = _case(
        acquired=date(2016, 1, 1),
        spells=(
            ResidenceSpell(person_id=P1, property_id=H1, start=date(2016, 1, 1), end=date(2016, 2, 1)),
        ),
    )
    assert periods.residence_years(lived, P1, H1, date(2026, 6, 1)) == 0


def test_중과_한시완화_창구가_실제로_이득을_보여준다(rs: RuleSet):
    """SIM-06의 가장 비싼 증상 — 개편안의 **간판 전략**이 무력화돼 있었다.

    '27~'28 다주택 중과 한시완화(소득세법 §104⑦ 개정안)는 종부세를 올리면서
    매도 창구를 열어주는 설계다. 그런데 완화 블록의 `min_holding_years: 2` 게이트가
    보유기간을 못 읽어 **매번 실패**하고 현행 30%p로 폴백했다. 룰셋에는 완화가
    멀쩡히 들어 있었는데 화면에는 이득이 0원으로 나왔다.

    실측 차이 2.99억원. "지금 팔까 버틸까"에 답하는 것이 이 서비스의 목적이라
    이 회귀는 서비스의 존재 이유를 무력화한다.
    """
    from fractions import Fraction

    from realestate_tax.engine.transfer_tax import TransferEvent, compute_transfer_tax

    SEOUL2 = "1171010100"  # 송파 — 조정대상지역
    people = (Person(id=P1, birth_date=date(1970, 1, 1), household_id=HouseholdId("hh")),)
    props = tuple(
        Property(
            id=PropertyId(f"h{i}"),
            kind=PropertyKind.APARTMENT,
            legal_dong_code=SEOUL2,
            published_prices=(PriceFact(2026, 2_000_000_000),),
        )
        for i in range(3)
    )
    case = TaxCase(
        year=2026,
        persons=people,
        households=(Household(id=HouseholdId("hh"), member_ids=(P1,)),),
        properties=props,
        ownerships=tuple(
            Ownership(person_id=P1, property_id=p.id, share=Fraction(1),
                      acquired_on=date(2015, 3, 1))
            for p in props
        ),
    )
    event = TransferEvent(
        property_id=PropertyId("h0"),
        person_id=P1,
        transfer_date=date(2027, 5, 20),
        transfer_price=2_500_000_000,
        acquisition_price=1_100_000_000,
    )
    current = compute_transfer_tax(case, event, rs, track=Track.CURRENT)
    reform = compute_transfer_tax(case, event, rs, track=Track.REFORM)

    assert reform.total.as_int() < current.total.as_int(), (
        "'27 한시완화가 현행보다 싸지 않다 — 완화 게이트가 다시 막혔다"
    )
    node = reform.trace.find("tr.07.income_tax")
    assert "10%p" in node.substitution, f"중과율이 완화되지 않았다: {node.substitution}"
