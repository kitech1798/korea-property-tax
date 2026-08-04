"""부부공동명의 1주택자 특례(종부세법 §10의2) 테스트.

시중 계산기가 가장 크게 무너진 자리다. propertytax.co.kr은 FAQ에서
"부부공동 1주택 12억 공제 특례신청은 미반영", "부부가 각자 단독명의로 1채씩
보유한 경우는 지원하지 않으니 참고용으로만 활용하세요"라고 자백한다.

여기서 증명해야 하는 것은 세 가지다.
  ① 부부공동명의 1주택과 부부 각자 1채씩을 **구분**한다
  ② 특례 신청/미신청 중 **어느 쪽이 유리한지 계산으로 답한다**
  ③ 요건을 못 갖추면 왜 못 갖췄는지 말한다
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
    ResidenceSpell,
    TaxCase,
)
from realestate_tax.engine.jongbuse import (
    JongbuseOptions,
    compare_joint_spouse_election,
    compute_jongbuse,
)
from realestate_tax.rules import RuleSet, Track, default_ruleset_root

SEOUL = "1168010100"
ME, SPOUSE = PersonId("me"), PersonId("spouse")


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


def house(pid: str, price: int, year: int = 2026) -> Property:
    return Property(
        id=PropertyId(pid),
        kind=PropertyKind.APARTMENT,
        legal_dong_code=SEOUL,
        display_name=pid,
        published_prices=(PriceFact(year, price),),
    )


def couple(
    *,
    shares: dict[str, tuple[PersonId, Fraction]] | None = None,
    joint_price: int = 2_000_000_000,
    separate: bool = False,
    my_birth: date | None = date(1956, 1, 1),
    year: int = 2026,
    lives_there: bool = False,
) -> TaxCase:
    hh = HouseholdId("hh")
    me = Person(id=ME, household_id=hh, name="본인", spouse_id=SPOUSE, birth_date=my_birth)
    sp = Person(id=SPOUSE, household_id=hh, name="배우자", spouse_id=ME, birth_date=my_birth)

    residences: tuple[ResidenceSpell, ...] = ()
    if separate:
        props = (house("내집", 1_100_000_000, year), house("배우자집", 900_000_000, year))
        owns = (
            Ownership(ME, PropertyId("내집")),
            Ownership(SPOUSE, PropertyId("배우자집")),
        )
    else:
        props = (house("공동주택", joint_price, year),)
        owns = (
            Ownership(ME, PropertyId("공동주택"), share=Fraction(1, 2)),
            Ownership(SPOUSE, PropertyId("공동주택"), share=Fraction(1, 2)),
        )
        if lives_there:
            residences = tuple(
                ResidenceSpell(pid, PropertyId("공동주택"), start=date(2016, 1, 1))
                for pid in (ME, SPOUSE)
            )

    return TaxCase(
        year=year,
        persons=(me, sp),
        households=(Household(id=hh, member_ids=(ME, SPOUSE)),),
        properties=props,
        ownerships=owns,
        residences=residences,
    )


OPTS = JongbuseOptions(holding_years=10, residence_years=10, resides_in_main_house=True)


# --------------------------------------------------------------------------
# ① 두 상황을 구분한다
# --------------------------------------------------------------------------


def test_부부공동명의_1주택은_특례_대상이다(rs: RuleSet):
    cmp = compare_joint_spouse_election(couple(), ME, rs, options=OPTS)
    assert cmp.eligible
    assert cmp.elected is not None


def test_부부가_각자_1채씩이면_특례_대상이_아니다(rs: RuleSet):
    """1세대 2주택이지 부부공동명의 1주택이 아니다. 이 구분을 못 해서
    시중 계산기가 '지원하지 않는다'고 물러섰다."""
    cmp = compare_joint_spouse_election(couple(separate=True), ME, rs, options=OPTS)
    assert not cmp.eligible
    assert "2채" in cmp.reason_ko
    assert cmp.elected is None
    # 그래도 계산은 된다 — 각자 자기 몫을 낸다
    assert len(cmp.not_elected) == 1


def test_각자_1채씩인_부부는_둘_다_세액공제를_못_받는다(rs: RuleSet):
    case = couple(separate=True)
    for pid in (ME, SPOUSE):
        r = compute_jongbuse(case, pid, rs, options=OPTS)
        assert r.tax_credit.as_int() == 0
        assert r.trace.find("jb.06.basic_deduction").output.as_int() == 900_000_000


# --------------------------------------------------------------------------
# ② 어느 쪽이 유리한지 계산으로 답한다
# --------------------------------------------------------------------------


def test_신청과_미신청을_모두_계산해_유리한_쪽을_고른다(rs: RuleSet):
    cmp = compare_joint_spouse_election(couple(joint_price=2_000_000_000), ME, rs, options=OPTS)

    assert cmp.elected is not None
    assert len(cmp.not_elected) == 2  # 부부 각자
    assert cmp.recommended in ("elected", "not_elected")
    assert cmp.saving == abs(cmp.elected_total - cmp.not_elected_total)

    # 권장안이 실제로 더 싸다
    chosen = cmp.elected_total if cmp.recommended == "elected" else cmp.not_elected_total
    other = cmp.not_elected_total if cmp.recommended == "elected" else cmp.elected_total
    assert chosen <= other


def test_고령_장기보유자는_특례_신청이_유리하다(rs: RuleSet):
    """미신청하면 각자 9억씩 총 18억을 공제받지만 세액공제가 0이다.
    신청하면 12억 공제뿐이나 연령 40% + 보유 40% = 80% 세액공제가 붙는다."""
    cmp = compare_joint_spouse_election(
        couple(joint_price=3_000_000_000, my_birth=date(1950, 1, 1)), ME, rs, options=OPTS
    )
    assert cmp.recommended == "elected"
    assert cmp.saving > 0

    assert cmp.elected.tax_credit.as_int() > 0
    assert all(r.tax_credit.as_int() == 0 for r in cmp.not_elected)


def test_젊은_부부는_각자_공제받는_미신청이_유리할_수_있다(rs: RuleSet):
    """세액공제가 붙지 않는 나이라면, 각자 9억씩 공제받는 쪽(합계 18억)이
    12억 한 번 공제받는 쪽보다 낫다. 그래서 추측하지 않고 둘 다 계산한다."""
    cmp = compare_joint_spouse_election(
        couple(joint_price=2_400_000_000, my_birth=date(1990, 1, 1)),
        ME,
        rs,
        options=JongbuseOptions(holding_years=3, residence_years=3),
    )
    assert cmp.recommended == "not_elected"
    assert cmp.not_elected_total < cmp.elected_total


def test_특례_신청하면_배우자_지분까지_합산된다(rs: RuleSet):
    """시행령 §5의2⑥ — 과세표준·세액 산정 시 배우자 소유 주택지분을 합산한다."""
    case = couple(joint_price=2_000_000_000)

    alone = compute_jongbuse(case, ME, rs, options=OPTS)
    elected = compute_jongbuse(
        case, ME, rs, options=replace(OPTS, joint_spouse_election=True)
    )

    assert alone.trace.find("jb.05.assessed_value").output.as_int() == 1_000_000_000
    assert elected.trace.find("jb.05.assessed_value").output.as_int() == 2_000_000_000
    assert elected.trace.find("jb.06.basic_deduction").output.as_int() == 1_200_000_000


def test_특례_신청하면_재산세도_1주택_지분_전체로_계산된다(rs: RuleSet):
    """시행령 §5의2⑦ — 재산세 부과액은 해당 과세대상 1주택 지분 전체 기준."""
    case = couple(joint_price=2_000_000_000)

    alone = compute_jongbuse(case, ME, rs, options=OPTS)
    elected = compute_jongbuse(
        case, ME, rs, options=replace(OPTS, joint_spouse_election=True)
    )
    assert elected.property_tax_total.as_int() == alone.property_tax_total.as_int() * 2


# --------------------------------------------------------------------------
# ③ 요건 미충족이면 이유를 말한다
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate, expect",
    [
        ("no_spouse", "배우자 정보가 없다"),
        ("third_house", "2채"),
        ("non_resident", "거주자"),
    ],
)
def test_요건_미충족_사유를_구체적으로_알려준다(rs: RuleSet, mutate, expect):
    hh = HouseholdId("hh")
    me = Person(
        id=ME,
        household_id=hh,
        name="본인",
        spouse_id=None if mutate == "no_spouse" else SPOUSE,
        birth_date=date(1956, 1, 1),
    )
    sp = Person(
        id=SPOUSE,
        household_id=hh,
        name="배우자",
        spouse_id=ME,
        is_resident=mutate != "non_resident",
    )
    props = [house("공동주택", 2_000_000_000)]
    owns = [
        Ownership(ME, PropertyId("공동주택"), share=Fraction(1, 2)),
        Ownership(SPOUSE, PropertyId("공동주택"), share=Fraction(1, 2)),
    ]
    if mutate == "third_house":
        props.append(house("별장", 400_000_000))
        owns.append(Ownership(ME, PropertyId("별장")))

    case = TaxCase(
        year=2026,
        persons=(me, sp),
        households=(Household(id=hh, member_ids=(ME, SPOUSE)),),
        properties=tuple(props),
        ownerships=tuple(owns),
    )
    cmp = compare_joint_spouse_election(case, ME, rs, options=OPTS)
    assert not cmp.eligible
    assert expect in cmp.reason_ko


def test_요건은_되는데_미신청이면_대안으로_안내된다(rs: RuleSet):
    """'유의사항: 특례 미반영'이 아니라, 판정한 뒤 행동 가능한 안내로 남긴다."""
    r = compute_jongbuse(couple(), ME, rs, options=OPTS)
    alts = {a.key: a for a in r.trace.all_alternatives()}
    assert "joint_spouse_special" in alts
    assert alts["joint_spouse_special"].actionable
    assert "신청하면" in alts["joint_spouse_special"].reason_ko


def test_특례_신청_결과는_감사추적에_표시된다(rs: RuleSet):
    r = compute_jongbuse(
        couple(), ME, rs,
        options=replace(OPTS, joint_spouse_election=True),
    )
    assert "부부공동명의 특례 신청" in r.trace.find("jb.03.house_count").branch.taken


# --------------------------------------------------------------------------
# 지분 처리 정확성
# --------------------------------------------------------------------------


def test_지분_소유자는_재산세도_지분만큼만_부담한다(rs: RuleSet):
    """물건 전체 재산세를 잡으면 종부세 재산세공제가 과다해져 세액이 과소 계상된다."""
    case = couple(joint_price=2_000_000_000)
    mine = compute_jongbuse(case, ME, rs, options=OPTS)
    theirs = compute_jongbuse(case, SPOUSE, rs, options=OPTS)

    from realestate_tax.engine.property_tax import compute_property_tax

    whole = compute_property_tax(
        case, PropertyId("공동주택"), rs, track=Track.CURRENT, owner_id=ME
    ).total.as_int()

    assert mine.property_tax_total.as_int() + theirs.property_tax_total.as_int() == whole
    assert "× 1/2" in mine.trace.find("jb.02.property_tax").substitution


def test_개편안_부부공동명의_기본공제가_문답자료_p40과_일치한다(rs: RuleSet):
    """문답자료 p.40 원문:
      · 부부 개별납부 시 → 거주 시 각 9억원, 비거주 시 각 4억원
      · 1세대1주택자 특례 신청 시 → 거주 시 14억원, 비거주 시 9억원

    개별납부(미신청) 시 각자는 1세대1주택자가 아니므로 다주택 신공식
    `4억 + 5억 × 거주주택 비중`이 적용된다. 거주하면 비중이 1이라 9억이 되고,
    거주하지 않으면 4억이 된다 — 문답자료 수치와 정확히 맞아떨어진다.
    """

    def deductions(lives: bool) -> tuple[int, int]:
        cmp = compare_joint_spouse_election(
            couple(
                joint_price=3_000_000_000,
                year=2027,
                my_birth=date(1950, 1, 1),
                lives_there=lives,
            ),
            ME,
            rs,
            track=Track.REFORM,
            options=replace(OPTS, resides_in_main_house=lives),
        )
        assert cmp.eligible
        return (
            cmp.elected.trace.find("jb.06.basic_deduction").output.as_int(),
            cmp.not_elected[0].trace.find("jb.06.basic_deduction").output.as_int(),
        )

    elected_live, separate_live = deductions(True)
    assert (elected_live, separate_live) == (1_400_000_000, 900_000_000)

    elected_away, separate_away = deductions(False)
    assert (elected_away, separate_away) == (900_000_000, 400_000_000)
