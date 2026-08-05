"""전략 엔진 테스트 — "그래서 어떻게 해야 하나"에 계산으로 답하는지.

지켜야 할 세 가지를 테스트로 고정한다.
  ① 절감액은 추정이 아니라 **재계산한 차액**이다
  ② 요건과 부작용이 함께 나온다 (한쪽만 말하면 조언이 아니라 함정이다)
  ③ 개편안 기반 전략에는 "국회 미통과"가 따라붙는다
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from fractions import Fraction

import pytest

from realestate_tax.domain import (
    Household,
    HouseholdId,
    InputQuality,
    LegalStatus,
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
from realestate_tax.engine.strategy import build_timeline, consult, project_case
from realestate_tax.rules import RuleSet, Track, default_ruleset_root

SEOUL = "1168010100"
ME, SPOUSE = PersonId("me"), PersonId("spouse")


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


def solo_case(price: int = 3_000_000_000, birth: date = date(1950, 1, 1)) -> TaxCase:
    hh = HouseholdId("hh")
    p = Person(id=ME, household_id=hh, name="본인", birth_date=birth)
    prop = Property(
        id=PropertyId("본가"),
        kind=PropertyKind.APARTMENT,
        legal_dong_code=SEOUL,
        display_name="본가",
        published_prices=(PriceFact(2026, price),),
    )
    return TaxCase(
        year=2026,
        persons=(p,),
        households=(Household(id=hh, member_ids=(ME,)),),
        properties=(prop,),
        ownerships=(Ownership(ME, prop.id),),
    )


def couple_case(price: int = 3_000_000_000) -> TaxCase:
    hh = HouseholdId("hh")
    me = Person(id=ME, household_id=hh, name="본인", spouse_id=SPOUSE, birth_date=date(1950, 1, 1))
    sp = Person(id=SPOUSE, household_id=hh, name="배우자", spouse_id=ME, birth_date=date(1950, 1, 1))
    prop = Property(
        id=PropertyId("공동주택"),
        kind=PropertyKind.APARTMENT,
        legal_dong_code=SEOUL,
        published_prices=(PriceFact(2026, price),),
    )
    return TaxCase(
        year=2026,
        persons=(me, sp),
        households=(Household(id=hh, member_ids=(ME, SPOUSE)),),
        properties=(prop,),
        ownerships=(
            Ownership(ME, prop.id, share=Fraction(1, 2)),
            Ownership(SPOUSE, prop.id, share=Fraction(1, 2)),
        ),
    )


OPTS = JongbuseOptions(holding_years=10, residence_years=10)


# --------------------------------------------------------------------------
# 연도 투영
# --------------------------------------------------------------------------


def test_미래_연도로_투영하면_추정치_라벨이_붙는다():
    """공시가격은 알 수 없는 미래값이다. 아는 척하면 안 된다."""
    future = project_case(solo_case(), 2028, growth=0.05)
    fact = future.find_property(PropertyId("본가")).price_for(2028)
    assert fact.quality is InputQuality.ESTIMATED
    assert fact.value == int(3_000_000_000 * 1.05**2)
    assert "투영" in fact.note


def test_상승률_0퍼센트는_정부_문답자료의_가정과_같다():
    future = project_case(solo_case(), 2028, growth=0.0)
    assert future.find_property(PropertyId("본가")).price_for(2028).value == 3_000_000_000


def test_같은_연도로_투영하면_원본_그대로():
    case = solo_case()
    assert project_case(case, 2026) is case


# --------------------------------------------------------------------------
# 타임라인 — 이 서비스의 메인 화면
# --------------------------------------------------------------------------


def test_2026년은_현행법만_2027년부터는_두_트랙을_모두_낸다(rs: RuleSet):
    """개편안은 국회 미통과이므로 정답이 하나가 아니다. 둘을 나란히 보여준다."""
    points = build_timeline(solo_case(), ME, rs, options=OPTS)
    by_year: dict[int, set[Track]] = {}
    for p in points:
        by_year.setdefault(p.year, set()).add(p.track)

    assert by_year[2026] == {Track.CURRENT}
    for year in (2027, 2028, 2029):
        assert by_year[year] == {Track.CURRENT, Track.REFORM}


def test_비거주_1주택은_개편안에서_세부담이_크게_오른다(rs: RuleSet):
    """개편안의 무게중심이 거주로 옮겨간 결과. 한 해만 보면 이 흐름이 안 보인다."""
    points = {
        (p.year, p.track): p
        for p in build_timeline(solo_case(), ME, rs, options=OPTS)
    }
    assert points[(2028, Track.REFORM)].total > points[(2028, Track.CURRENT)].total
    assert points[(2029, Track.REFORM)].total > points[(2026, Track.CURRENT)].total


def test_타임라인_각_지점은_재산세와_종부세로_쪼개진다(rs: RuleSet):
    for p in build_timeline(solo_case(), ME, rs, options=OPTS):
        assert p.total == p.property_tax + p.jongbuse
        assert p.property_tax > 0


# --------------------------------------------------------------------------
# ① 절감액은 재계산한 차액이다
# --------------------------------------------------------------------------


def test_부부공동명의_전략의_절감액이_실제_재계산_차액과_같다(rs: RuleSet):
    from realestate_tax.engine.jongbuse import compare_joint_spouse_election

    result = consult(couple_case(), ME, rs, options=OPTS)
    strat = next(s for s in result.strategies if s.key == "joint_spouse_election")

    cmp = compare_joint_spouse_election(couple_case(), ME, rs, options=OPTS)
    assert strat.baseline == cmp.not_elected_total
    assert strat.alternative == cmp.elected_total
    assert strat.saving == cmp.not_elected_total - cmp.elected_total


def test_고령_부부에게는_특례_신청이_이득으로_나온다(rs: RuleSet):
    result = consult(couple_case(price=3_000_000_000), ME, rs, options=OPTS)
    keys = {s.key for s in result.beneficial}
    assert "joint_spouse_election" in keys


def test_이득인_전략만_절감액_큰_순으로_추려진다(rs: RuleSet):
    result = consult(couple_case(), ME, rs, options=OPTS)
    savings = [s.saving for s in result.beneficial]
    assert all(v > 0 for v in savings)
    assert savings == sorted(savings, reverse=True)


def test_실거주_전환_전략은_개편안_시행연도만_대상으로_한다(rs: RuleSet):
    result = consult(solo_case(), ME, rs, options=OPTS)
    strat = next(s for s in result.strategies if s.key == "move_in")
    assert strat.years == (2027, 2028, 2029)
    assert 2026 not in strat.years


def test_이미_거주_중이면_실거주_전환을_권하지_않는다(rs: RuleSet):
    """당연한 것을 제안하면 나머지 조언의 신뢰도까지 떨어진다."""
    result = consult(
        solo_case(), ME, rs, options=replace(OPTS, resides_in_main_house=True)
    )
    assert not any(s.key == "move_in" for s in result.strategies)


def test_실거주_전환은_비거주자에게_실제로_이득이다(rs: RuleSet):
    """기본공제 9억 → 14억 + 거주공제 전환. 개편안의 핵심 레버다."""
    result = consult(solo_case(), ME, rs, options=OPTS)
    strat = next(s for s in result.strategies if s.key == "move_in")
    assert strat.is_beneficial
    assert strat.alternative < strat.baseline


# --------------------------------------------------------------------------
# ② 요건과 부작용을 함께 말한다
# --------------------------------------------------------------------------


def test_모든_전략에_근거_조문과_요건과_부작용이_붙는다(rs: RuleSet):
    """'부부공동명의로 바꾸세요'만 하고 증여세를 빼놓으면 조언이 아니라 함정이다."""
    result = consult(couple_case(), ME, rs, options=OPTS)
    assert result.strategies
    for s in result.strategies:
        assert s.basis_ko, f"{s.key}에 근거 조문이 없다"
        assert s.requirements_ko, f"{s.key}에 요건이 없다"
        assert s.caveats_ko, f"{s.key}에 부작용 안내가 없다"
        assert s.what_to_do_ko, f"{s.key}에 실행 방법이 없다"


def test_부부공동명의_전략은_누구를_지정할지까지_알려준다(rs: RuleSet):
    result = consult(couple_case(), ME, rs, options=OPTS)
    strat = next(s for s in result.strategies if s.key == "joint_spouse_election")
    joined = " ".join(strat.caveats_ko)
    assert "연령" in joined and "보유기간" in joined


def test_실거주_전환에는_부득이한_사유_대안도_안내된다(rs: RuleSet):
    """이사하지 않고도 거주기간을 인정받을 길이 있으면 그것부터 알려야 한다."""
    result = consult(solo_case(), ME, rs, options=OPTS)
    strat = next(s for s in result.strategies if s.key == "move_in")
    joined = " ".join(strat.caveats_ko)
    assert "부득이한 사유" in joined and "3년" in joined


# --------------------------------------------------------------------------
# ③ 불확실성을 숨기지 않는다
# --------------------------------------------------------------------------


def test_개편안_기반_전략에는_국회_미통과가_붙는다(rs: RuleSet):
    result = consult(solo_case(), ME, rs, options=OPTS)
    strat = next(s for s in result.strategies if s.key == "move_in")
    assert strat.certainty.legal is LegalStatus.BILL_PENDING
    assert any("국회 통과 전" in c for c in strat.caveats_ko)


def test_다주택자에게는_양도세_중과_완화_창구를_알린다(rs: RuleSet):
    """보유세만으로는 '팔까 버틸까'의 답이 안 나온다.
    이제 양도세가 범위에 들어왔으므로, 사실 안내에 그치지 않고
    양도가액·취득가액을 받아 매도 시점을 계산해 주겠다고 안내한다."""
    hh = HouseholdId("hh")
    p = Person(id=ME, household_id=hh, name="본인", birth_date=date(1970, 1, 1))
    props = tuple(
        Property(
            id=PropertyId(f"h{i}"),
            kind=PropertyKind.APARTMENT,
            legal_dong_code=SEOUL,
            published_prices=(PriceFact(2026, 900_000_000),),
        )
        for i in range(2)
    )
    case = TaxCase(
        year=2026,
        persons=(p,),
        households=(Household(id=hh, member_ids=(ME,)),),
        properties=props,
        ownerships=tuple(Ownership(ME, pr.id) for pr in props),
    )
    notes = " ".join(consult(case, ME, rs, options=OPTS).notes_ko)
    assert "중과" in notes and "2027" in notes
    assert "sell_timing" in notes
    assert "취득가액" in notes


def test_입력이_부족하면_무엇을_주면_정확해지는지_알려준다(rs: RuleSet):
    notes = " ".join(consult(solo_case(), ME, rs, options=OPTS).notes_ko)
    assert "고지서" in notes


def test_확실성_우려가_상담_노트에_전부_올라온다(rs: RuleSet):
    notes = " ".join(consult(solo_case(), ME, rs, options=OPTS).notes_ko)
    assert "확실성 유의" in notes


def test_이미_거주_중이면_실거주_전환을_제안하지_않는다(rs: RuleSet):
    """★ 가드가 **옵션만** 봐서 한 번도 발동하지 않았다(2026-08-05).

    거주 여부는 `ResidenceSpell`에서 도출되므로 `options.resides_in_main_house`는
    보통 None이다. 그래서 이미 살고 있는 사람에게도 "실거주 전환"이 제시됐고,
    기준선이 이미 거주를 반영한 탓에 **"손해 557만원"**이라는 헛소리가 나왔다.

    모델에 있는 사실을 엔진이 안 읽는 같은 실수의 다섯 번째다
    (거주 여부 → 거주기간 → 취득일 → Election → 여기).
    """
    from datetime import date

    from realestate_tax.domain import (
        Household, HouseholdId, Ownership, Person, PersonId,
        PriceFact, Property, PropertyId, PropertyKind, ResidenceSpell, TaxCase,
    )
    from realestate_tax.engine.strategy import consult

    me = PersonId("me")
    h = PropertyId("h")

    def build(resides: bool) -> TaxCase:
        return TaxCase(
            year=2026,
            persons=(Person(id=me, birth_date=date(1958, 4, 2), household_id=HouseholdId("hh")),),
            households=(Household(id=HouseholdId("hh"), member_ids=(me,)),),
            properties=(
                Property(id=h, kind=PropertyKind.APARTMENT, legal_dong_code="1168010100",
                         published_prices=(PriceFact(2026, 2_500_000_000),)),
            ),
            ownerships=(Ownership(person_id=me, property_id=h, acquired_on=date(2012, 5, 1)),),
            residences=(
                (ResidenceSpell(person_id=me, property_id=h, start=date(2012, 5, 1)),)
                if resides else ()
            ),
        )

    living = consult(build(True), me, rs)
    assert not [s for s in living.strategies if s.key == "move_in"], (
        "이미 거주 중인데 실거주 전환을 제안했다"
    )

    # 반대편도 함께 고정한다 — 가드를 세게 걸다 정상 경로까지 막으면 안 된다.
    away = consult(build(False), me, rs)
    move_in = [s for s in away.strategies if s.key == "move_in"]
    assert move_in and move_in[0].saving > 0, "비거주자에게 실거주 전환이 안 나온다"


def test_조언에_틀린_조항을_적지_않는다(rs: RuleSet):
    """거주공제의 근거는 §9⑧이다. §9⑦은 상속·일시적2주택이 있을 때의
    **연령** 공제 특칙으로 전혀 다른 규범이다 — 룰셋에서 고친 것과 같은 오기가
    조언 문구에도 박혀 있었다."""
    from datetime import date

    from realestate_tax.domain import (
        Household, HouseholdId, Ownership, Person, PersonId,
        PriceFact, Property, PropertyId, PropertyKind, TaxCase,
    )
    from realestate_tax.engine.strategy import consult

    me = PersonId("me")
    h = PropertyId("h")
    case = TaxCase(
        year=2026,
        persons=(Person(id=me, birth_date=date(1958, 4, 2), household_id=HouseholdId("hh")),),
        households=(Household(id=HouseholdId("hh"), member_ids=(me,)),),
        properties=(
            Property(id=h, kind=PropertyKind.APARTMENT, legal_dong_code="1168010100",
                     published_prices=(PriceFact(2026, 2_500_000_000),)),
        ),
        ownerships=(Ownership(person_id=me, property_id=h, acquired_on=date(2012, 5, 1)),),
    )
    for s in consult(case, me, rs).strategies:
        assert "§9⑦" not in s.basis_ko, f"{s.key}: §9⑦은 연령공제 특칙이다"
