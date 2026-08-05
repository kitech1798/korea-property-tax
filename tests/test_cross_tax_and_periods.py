"""세목 경계와 기간 계산 — 2회차 시뮬레이션이 잡은 것들.

여기 묶인 것들은 성격이 같다. **조용히 세금을 적게 알려주는** 결함이다.
과다 안내는 사용자가 놀라서 확인하지만, 과소 안내는 그대로 신고돼 가산세가 된다.
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
from realestate_tax.engine.periods import full_years
from realestate_tax.engine.transfer_tax import TransferEvent, compute_transfer_tax
from realestate_tax.rules import RuleSet, Track, default_ruleset_root
from realestate_tax.rules.resolver import load_ruleset

BUSAN = "2647010100"   # 부산 해운대 — 광역시
DAEGU = "2711010100"   # 대구 중구 — 광역시 본청
MOKPO = "4611010100"   # 목포 — 광역시 아님
P1 = PersonId("p1")
P2 = PersonId("p2")
MAIN = PropertyId("main")
SECOND = PropertyId("second")
SOLO = PropertyId("h")


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return load_ruleset(default_ruleset_root())


def _two_house_case(second_dong: str, second_price: int, *, second_owner: PersonId = P1) -> TaxCase:
    people = [Person(id=P1, birth_date=date(1970, 1, 1), household_id=HouseholdId("hh"))]
    if second_owner != P1:
        people.append(
            Person(id=second_owner, birth_date=date(1998, 1, 1), household_id=HouseholdId("hh"))
        )
    return TaxCase(
        year=2027,
        persons=tuple(people),
        households=(Household(id=HouseholdId("hh"), member_ids=tuple(p.id for p in people)),),
        properties=(
            Property(id=MAIN, kind=PropertyKind.APARTMENT, legal_dong_code=BUSAN,
                     published_prices=(PriceFact(2027, 1_500_000_000),)),
            Property(id=SECOND, kind=PropertyKind.APARTMENT, legal_dong_code=second_dong,
                     published_prices=(PriceFact(2027, second_price),)),
        ),
        ownerships=(
            Ownership(person_id=P1, property_id=MAIN, acquired_on=date(2015, 3, 1)),
            Ownership(person_id=second_owner, property_id=SECOND, acquired_on=date(2018, 5, 1)),
        ),
        residences=(ResidenceSpell(person_id=P1, property_id=MAIN, start=date(2015, 3, 1)),),
    )


def _sell_main() -> TransferEvent:
    return TransferEvent(
        property_id=MAIN, person_id=P1,
        transfer_date=date(2027, 6, 15),
        transfer_price=2_000_000_000, acquisition_price=1_000_000_000,
    )


def _solo_case(year: int, *, acquired: date, lived_from: date | None = None,
               dong: str = MOKPO) -> TaxCase:
    return TaxCase(
        year=year,
        persons=(Person(id=P1, birth_date=date(1970, 1, 1), household_id=HouseholdId("hh")),),
        households=(Household(id=HouseholdId("hh"), member_ids=(P1,)),),
        properties=(
            Property(id=SOLO, kind=PropertyKind.APARTMENT, legal_dong_code=dong,
                     published_prices=(PriceFact(year, 900_000_000),)),
        ),
        ownerships=(Ownership(person_id=P1, property_id=SOLO, acquired_on=acquired),),
        residences=(
            (ResidenceSpell(person_id=P1, property_id=SOLO, start=lived_from),)
            if lived_from is not None else ()
        ),
    )


# --------------------------------------------------------------------------
# SIM-07 세목 오염
# --------------------------------------------------------------------------


def test_양도세는_종부세_주택수_특례를_쓰지_않는다(rs: RuleSet):
    """세목이 다르면 주택 수 규정도 다르다.

    지방 저가주택 제외는 **종합부동산세법** 시행령 §4의2③이다. 소득세법에는 없다.
    남의 법으로 센 주택 수로 비과세를 내주면 낼 세금보다 적은 금액을 알려주는 것이고,
    그대로 신고하면 과소신고 가산세가 붙는다.

    실측: 부산 1채 + 목포 1채(공시 3억)를 가진 사람이 지방 저가주택 제외로
    1세대1주택자가 되어 양도차익 전액이 비과세로 나왔다.
    """
    case = _two_house_case(MOKPO, 300_000_000)
    result = compute_transfer_tax(case, _sell_main(), rs, track=Track.CURRENT)

    node = result.trace.find("tr.01.house_count")
    assert node is not None, "양도세 전용 주택 수 판정 노드가 없다"
    assert "세대 주택 2채" in node.substitution
    assert result.taxable_gain.as_int() > 0, "2주택자에게 1세대1주택 비과세가 나갔다"


def test_세대원이_따로_가진_집도_센다(rs: RuleSet):
    """1세대1주택은 **세대** 기준이다. 동거 성년 자녀의 집을 빼면 비과세가 샌다."""
    case = _two_house_case(MOKPO, 300_000_000, second_owner=P2)
    result = compute_transfer_tax(case, _sell_main(), rs, track=Track.CURRENT)
    assert "세대 주택 2채" in result.trace.find("tr.01.house_count").substitution
    assert result.taxable_gain.as_int() > 0


def test_주택수_특례_가능성을_알려준다(rs: RuleSet):
    """제외하지 않는 대신 **말한다.** 소득세법 §155 특례에 해당할 수 있는데
    그 사실조차 모르면 정당한 비과세를 놓친다."""
    case = _two_house_case(MOKPO, 300_000_000, second_owner=P2)
    result = compute_transfer_tax(case, _sell_main(), rs, track=Track.CURRENT)
    alts = {a.key: a for a in result.trace.all_alternatives()}
    assert "income_tax_house_count_special" in alts
    assert alts["income_tax_house_count_special"].actionable
    assert "§155" in alts["income_tax_house_count_special"].reason_ko


# --------------------------------------------------------------------------
# SIM-09 지방 저가주택 지역 요건
# --------------------------------------------------------------------------


def test_광역시는_지방저가주택_대상이_아니다(rs: RuleSet):
    """종부령 §4의2③의 지역 요건은 '수도권 밖'만이 아니다.

    "수도권 밖의 지역 **중** 광역시·특별자치시가 아닌 지역, 광역시에 소속된 군 …"
    이므로 대구 중구 같은 광역시 본청 지역은 대상이 아니다. 예전에는 수도권만
    걸러 놓고 "수도권 밖에 소재"라고 단정해 광역시 주택이 주택 수에서 빠졌다.
    """
    from realestate_tax.engine.special_houses import assess

    case = _two_house_case(DAEGU, 300_000_000)
    result = assess(case, P1, rs, track=Track.CURRENT, on=case.assessment_date)
    assert result.count == 2, "광역시 주택이 지방 저가주택으로 빠졌다"
    assert any("광역시" in m.reason_ko for m in result.missed), "왜 안 빠졌는지 설명이 없다"


def test_광역시가_아니면_지방저가주택_판정은_그대로다(rs: RuleSet):
    """고치면서 정상 경로를 막지 않았는지 함께 고정한다."""
    from realestate_tax.engine.special_houses import assess

    case = _two_house_case(MOKPO, 300_000_000)
    result = assess(case, P1, rs, track=Track.CURRENT, on=case.assessment_date)
    assert result.count == 1, "목포(광역시 아님)는 지방 저가주택 판정이 살아 있어야 한다"


# --------------------------------------------------------------------------
# SIM-08 윤년 — 민법 §160③
# --------------------------------------------------------------------------


def test_윤년_2월29일_취득분은_평년_2월28일에_만기다():
    """민법 §160③ "최종의 월에 해당일이 없는 때에는 그 월의 말일로 만료".

    국세기본법 §4가 기간 계산에 민법을 준용한다. 이 처리를 빼면 2/29 취득분만
    하루가 밀려 보유 1년으로 읽히고, 2년 미만 단기세율 60%가 붙는다.
    실측 차이 840,000원 vs 8,400,000원 — 10배다.
    """
    assert full_years(date(2024, 2, 29), date(2026, 2, 28)) == 2
    assert full_years(date(2024, 2, 29), date(2026, 2, 27)) == 1
    assert full_years(date(2024, 2, 29), date(2028, 2, 29)) == 4
    # 평범한 날짜는 그대로여야 한다
    assert full_years(date(2016, 1, 1), date(2026, 6, 1)) == 10
    assert full_years(date(2016, 4, 1), date(2027, 3, 31)) == 10


def test_윤년생의_나이도_평년_2월28일에_오른다():
    """60/65/70세 세액공제 구간이 윤년생만 하루 밀리면 안 된다."""
    p = Person(id=P1, birth_date=date(1956, 2, 29))
    assert p.age_at(date(2026, 2, 28)) == 70
    assert p.age_at(date(2026, 2, 27)) == 69


# --------------------------------------------------------------------------
# 명시값과 사실의 모순
# --------------------------------------------------------------------------


def test_명시한_기간이_사실과_어긋나면_드러낸다(rs: RuleSet):
    """숫자만 바꿔 적어 비과세를 받아내는 길을 막는다.

    명시값을 무시하지는 않는다 — 배우자 상속 통산처럼 정당한 사유가 있다.
    다만 **조용히 통과시키지 않는다.** 실측: 보유 1일 양도에 12년을 적자
    아무 경고 없이 세액 0원이 나왔다.
    """
    case = _solo_case(2027, acquired=date(2027, 6, 1))
    event = TransferEvent(
        property_id=SOLO, person_id=P1, transfer_date=date(2027, 6, 2),
        transfer_price=1_000_000_000, acquisition_price=500_000_000,
        holding_years=12,
    )
    result = compute_transfer_tax(case, event, rs, track=Track.CURRENT)
    node = result.trace.find("tr.00.period_conflict")
    assert node is not None, "모순이 경고 없이 통과했다"
    assert "입력 12년" in node.substitution and "계산한 0년" in node.substitution
    assert "판단 필요" in dict(result.trace.certainty_concerns())


def test_명시값이_사실과_맞으면_경고하지_않는다(rs: RuleSet):
    """맞는 입력에까지 경고를 달면 사용자가 모든 경고를 무시하게 된다."""
    case = _solo_case(2027, acquired=date(2017, 6, 1))
    event = TransferEvent(
        property_id=SOLO, person_id=P1, transfer_date=date(2027, 6, 15),
        transfer_price=1_000_000_000, acquisition_price=500_000_000,
        holding_years=10,
    )
    result = compute_transfer_tax(case, event, rs, track=Track.CURRENT)
    assert result.trace.find("tr.00.period_conflict") is None


# --------------------------------------------------------------------------
# 기각한 지적의 회귀 방지
# --------------------------------------------------------------------------


def test_개편안_장특공제_3단계가_실제로_갈린다(rs: RuleSet):
    """2회차 에이전트가 "3단계가 표2에 반영 안 됐다"고 보고했으나 **틀렸다.**

    보유 5년·거주 5년에서 세 해가 모두 40%인 것은 우연한 고정점이다 —
    2027 `4a+4b`, 2028 `2a+6b`, 2029 `8b`가 a=b=5에서 전부 40%가 된다.
    보유 10년·거주 3년으로 옮기면 52% → 38% → 24%로 정확히 갈린다.
    이 테스트는 그 고정점에 다시 속지 않도록 **갈리는 조합**으로 고정한다.
    """
    def ltd_rate(year: int, hold: int, live: int) -> float:
        transfer_date = date(year, 6, 15)
        case = TaxCase(
            year=year,
            persons=(Person(id=P1, birth_date=date(1970, 1, 1), household_id=HouseholdId("hh")),),
            households=(Household(id=HouseholdId("hh"), member_ids=(P1,)),),
            properties=(
                Property(id=SOLO, kind=PropertyKind.APARTMENT, legal_dong_code=MOKPO,
                         published_prices=(PriceFact(year, 900_000_000),)),
            ),
            ownerships=(Ownership(person_id=P1, property_id=SOLO,
                                  acquired_on=date(year - hold, 6, 1)),),
            residences=(ResidenceSpell(person_id=P1, property_id=SOLO,
                                       start=date(year - live, 6, 1), end=transfer_date),),
        )
        event = TransferEvent(
            property_id=SOLO, person_id=P1, transfer_date=transfer_date,
            transfer_price=3_000_000_000, acquisition_price=1_000_000_000,
        )
        r = compute_transfer_tax(case, event, rs, track=Track.REFORM)
        return r.long_term_deduction.as_int() / r.taxable_gain.as_int()

    assert round(ltd_rate(2027, 10, 3), 2) == 0.52   # 보유 4%×10 + 거주 4%×3
    assert round(ltd_rate(2028, 10, 3), 2) == 0.38   # 보유 2%×10 + 거주 6%×3
    assert round(ltd_rate(2029, 10, 3), 2) == 0.24   # 보유 폐지 + 거주 8%×3


# --------------------------------------------------------------------------
# 소득세법 시행령 §155 — 양도세 고유 주택수 특례
# --------------------------------------------------------------------------

OLD = PropertyId("old")
NEW = PropertyId("new")


def _two_acquisitions(old_acq: date, new_acq: date) -> TaxCase:
    return TaxCase(
        year=2027,
        persons=(Person(id=P1, birth_date=date(1975, 1, 1), household_id=HouseholdId("hh")),),
        households=(Household(id=HouseholdId("hh"), member_ids=(P1,)),),
        properties=(
            Property(id=OLD, kind=PropertyKind.APARTMENT, legal_dong_code=BUSAN,
                     published_prices=(PriceFact(2027, 900_000_000),)),
            Property(id=NEW, kind=PropertyKind.APARTMENT, legal_dong_code=BUSAN,
                     published_prices=(PriceFact(2027, 900_000_000),)),
        ),
        ownerships=(
            Ownership(person_id=P1, property_id=OLD, acquired_on=old_acq),
            Ownership(person_id=P1, property_id=NEW, acquired_on=new_acq),
        ),
        residences=(ResidenceSpell(person_id=P1, property_id=OLD, start=old_acq),),
    )


def _sell(pid: PropertyId) -> TransferEvent:
    return TransferEvent(
        property_id=pid, person_id=P1, transfer_date=date(2027, 6, 15),
        transfer_price=1_100_000_000, acquisition_price=600_000_000,
    )


def test_일시적_2주택은_종전주택을_팔_때만_1세대1주택이다(rs: RuleSet):
    """소득세법 시행령 §155① — 조문 원문(2026-08-05 법제처 XML).

    "종전의 주택을 취득한 날부터 **1년 이상**이 지난 후 신규 주택을 취득하고
     신규 주택을 취득한 날부터 **3년 이내**에 종전의 주택을 양도하는 경우"

    ★ 종부세의 일시적 2주택(종부령 §4의2①)과 요건이 다르다. 세목을 섞으면 안 된다.
    """
    case = _two_acquisitions(date(2018, 3, 1), date(2025, 9, 1))

    ok = compute_transfer_tax(case, _sell(OLD), rs, track=Track.CURRENT)
    assert ok.total.as_int() == 0, "요건을 갖춘 종전주택 양도인데 비과세가 아니다"
    assert "§155①" in ok.trace.find("tr.01.house_count").branch.taken

    # 신규주택을 팔면 특례가 아니다 — 조문이 '종전의 주택을 양도하는 경우'라 못 박는다
    wrong = compute_transfer_tax(case, _sell(NEW), rs, track=Track.CURRENT)
    assert wrong.total.as_int() > 0
    assert "2주택" in wrong.trace.find("tr.01.house_count").branch.taken


def test_일시적_2주택_기간_요건을_못_채우면_적용하지_않는다(rs: RuleSet):
    """두 기간 요건이 각각 독립적으로 특례를 죽인다."""
    # 종전주택 취득 1년 안에 신규 취득 → 요건 미충족
    early = _two_acquisitions(date(2025, 3, 1), date(2025, 9, 1))
    assert compute_transfer_tax(early, _sell(OLD), rs, track=Track.CURRENT).total.as_int() > 0

    # 신규 취득 후 3년이 지나 양도 → 요건 미충족
    late = _two_acquisitions(date(2015, 3, 1), date(2023, 1, 1))
    assert compute_transfer_tax(late, _sell(OLD), rs, track=Track.CURRENT).total.as_int() > 0


def test_확인이_필요한_특례는_적용하지_않고_알린다(rs: RuleSet):
    """주택 수를 줄이는 특례는 세액을 **낮춘다.**

    확인하지 못한 채 적용하면 과소신고가 되고 가산세가 붙는다. 그래서 사실만으로
    전부 판정되는 것만 적용하고, 나머지는 적용하지 않되 무엇을 확인해야 하는지 말한다.
    """
    case = _two_acquisitions(date(2015, 3, 1), date(2023, 1, 1))
    result = compute_transfer_tax(case, _sell(OLD), rs, track=Track.CURRENT)
    alts = {a.key: a for a in result.trace.all_alternatives()}
    assert "income_tax_house_count_special" in alts
    assert alts["income_tax_house_count_special"].actionable
    assert "§155①" in alts["income_tax_house_count_special"].reason_ko


# --------------------------------------------------------------------------
# §155② 상속주택 — 한 줄의 사실이 결론을 뒤집는다
# --------------------------------------------------------------------------

INH = PropertyId("inh")


def _inherited_case(same_household: bool | None) -> TaxCase:
    """내 집 1채 + 상속받은 집 1채. 가장 흔한 2주택 구성이다."""
    from fractions import Fraction

    from realestate_tax.domain import AcquisitionCause, InheritedMeta

    return TaxCase(
        year=2027,
        persons=(Person(id=P1, birth_date=date(1975, 1, 1), household_id=HouseholdId("hh")),),
        households=(Household(id=HouseholdId("hh"), member_ids=(P1,)),),
        properties=(
            Property(id=MAIN, kind=PropertyKind.APARTMENT, legal_dong_code=BUSAN,
                     published_prices=(PriceFact(2027, 900_000_000),)),
            Property(id=INH, kind=PropertyKind.APARTMENT, legal_dong_code=MOKPO,
                     published_prices=(PriceFact(2027, 500_000_000),)),
        ),
        ownerships=(
            Ownership(person_id=P1, property_id=MAIN, acquired_on=date(2015, 3, 1)),
            Ownership(
                person_id=P1, property_id=INH, acquired_on=date(2024, 5, 10),
                cause=AcquisitionCause.INHERITANCE,
                inherited=InheritedMeta(
                    inheritance_date=date(2024, 5, 10), share=Fraction(1),
                    inherited_value=500_000_000, same_household_at_death=same_household,
                ),
            ),
        ),
        residences=(ResidenceSpell(person_id=P1, property_id=MAIN, start=date(2015, 3, 1)),),
    )


def _sell(pid: PropertyId) -> TransferEvent:
    return TransferEvent(
        property_id=pid, person_id=P1, transfer_date=date(2027, 6, 15),
        transfer_price=1_100_000_000, acquisition_price=600_000_000,
    )


def test_별도세대_상속이면_일반주택_양도가_1세대1주택이다(rs: RuleSet):
    """소득세법 시행령 §155② 본문 — 상속주택과 일반주택을 각 1개씩 가진 1세대가
    **일반주택**을 양도하면 1주택으로 본다. 따로 살던 부모 집을 상속받은,
    가장 흔한 경우다."""
    result = compute_transfer_tax(_inherited_case(False), _sell(MAIN), rs, track=Track.CURRENT)
    assert result.total.as_int() == 0
    assert "§155②" in result.trace.find("tr.01.house_count").branch.taken


def test_상속주택을_양도하면_특례가_아니다(rs: RuleSet):
    """조문이 "**일반주택**을 양도하는 경우"라고 못 박는다. 방향을 뒤집으면
    비과세를 잘못 내주게 된다."""
    result = compute_transfer_tax(_inherited_case(False), _sell(INH), rs, track=Track.CURRENT)
    assert result.total.as_int() > 0
    assert "2주택" in result.trace.find("tr.01.house_count").branch.taken


def test_동일세대_상속은_판정하지_않는다(rs: RuleSet):
    """§155② 단서 — 상속개시 당시 1세대였으면 동거봉양 합가 여부까지 봐야 한다.

    합가 시점을 입력받지 않으므로 판정하지 않는다. 세액을 낮추는 방향이라
    확인 못 한 채 적용하면 과소신고가 된다."""
    result = compute_transfer_tax(_inherited_case(True), _sell(MAIN), rs, track=Track.CURRENT)
    assert result.total.as_int() > 0
    reasons = " ".join(a.reason_ko for a in result.trace.all_alternatives())
    assert "동거봉양" in reasons


def test_동일세대_여부를_모르면_적용하지_않고_묻는다(rs: RuleSet):
    """결론이 정반대로 갈리는 사실이 비었다. 유리한 쪽으로 가정하지 않는다."""
    result = compute_transfer_tax(_inherited_case(None), _sell(MAIN), rs, track=Track.CURRENT)
    assert result.total.as_int() > 0
    alts = {a.key: a for a in result.trace.all_alternatives()}
    assert alts["income_tax_house_count_special"].actionable
    assert "같은 세대" in alts["income_tax_house_count_special"].reason_ko


def test_적용된_특례가_자기_조항을_표시한다(rs: RuleSet):
    """배지에 조항을 박아 두면 다른 특례가 붙는 날 틀린 근거를 표시한다."""
    inherited = compute_transfer_tax(_inherited_case(False), _sell(MAIN), rs, track=Track.CURRENT)
    temporary = compute_transfer_tax(
        _two_acquisitions(date(2018, 3, 1), date(2025, 9, 1)), _sell_old(), rs, track=Track.CURRENT
    )
    assert "§155②" in inherited.trace.find("tr.01.house_count").branch.taken
    assert "§155①" in temporary.trace.find("tr.01.house_count").branch.taken


def _sell_old() -> TransferEvent:
    return TransferEvent(
        property_id=OLD, person_id=P1, transfer_date=date(2027, 6, 15),
        transfer_price=1_100_000_000, acquisition_price=600_000_000,
    )
