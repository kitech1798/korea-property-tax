"""도메인 모델 테스트.

가장 중요한 것은 마지막의 '구조 불변식' 두 개다. 시중 계산기가 무너진 자리를
테스트로 못 박아 회귀를 막는다.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction

import pytest

from realestate_tax.domain import (
    AcquisitionCause,
    Certainty,
    DeterminationQuality,
    Household,
    HouseholdId,
    InputQuality,
    LegalStatus,
    Ownership,
    Person,
    PersonId,
    PersonType,
    PriceFact,
    Property,
    PropertyId,
    PropertyKind,
    ResidenceSpell,
    TaxCase,
    assessment_date,
)

SEOUL_GANGNAM = "1168010100"
BUSAN_HAEUNDAE = "2635010300"


def make_house(pid: str, price: int, year: int = 2026, dong: str = SEOUL_GANGNAM) -> Property:
    return Property(
        id=PropertyId(pid),
        kind=PropertyKind.APARTMENT,
        legal_dong_code=dong,
        published_prices=(PriceFact(year=year, value=price),),
    )


# --------------------------------------------------------------------------
# 기본 규약
# --------------------------------------------------------------------------


def test_과세기준일은_매년_6월_1일():
    assert assessment_date(2026) == date(2026, 6, 1)
    assert assessment_date(2029) == date(2029, 6, 1)


def test_지분은_Fraction이라_3인_공동명의_합이_정확히_1():
    shares = [Fraction(1, 3)] * 3
    assert sum(shares) == 1

    # 시중 계산기는 지분을 1~99 정수 퍼센트로 받는다(propertytax.co.kr app.js:819).
    # 3인 공동명의를 33%씩 넣으면 1%가 증발해 공시가격 30억 물건에서 3천만원이 사라진다.
    price = 3_000_000_000
    percent_based = sum(price * 33 // 100 for _ in range(3))
    assert percent_based == price - 30_000_000

    fraction_based = sum(price * Fraction(1, 3) for _ in range(3))
    assert fraction_based == price


def test_지분_안분에_반올림_손실이_없다():
    # 7분의 1처럼 나누어떨어지지 않는 지분도 합이 원본과 정확히 같아야 한다.
    price = 1_234_567_890
    assert sum(price * Fraction(1, 7) for _ in range(7)) == price


@pytest.mark.parametrize("bad", [Fraction(0), Fraction(-1, 2), Fraction(3, 2)])
def test_지분_범위_위반은_생성_시점에_막힌다(bad):
    with pytest.raises(ValueError, match="지분"):
        Ownership(PersonId("p1"), PropertyId("h1"), share=bad)


def test_상속취득인데_상속정보가_없으면_거부한다():
    # 상속주택 특례 판정을 아예 못 하게 되므로 조용히 통과시키지 않는다.
    with pytest.raises(ValueError, match="상속"):
        Ownership(
            PersonId("p1"),
            PropertyId("h1"),
            cause=AcquisitionCause.INHERITANCE,
        )


def test_공시가격_연도_중복은_거부한다():
    with pytest.raises(ValueError, match="중복"):
        Property(
            id=PropertyId("h1"),
            kind=PropertyKind.APARTMENT,
            legal_dong_code=SEOUL_GANGNAM,
            published_prices=(
                PriceFact(2026, 1_000_000_000),
                PriceFact(2026, 1_200_000_000),
            ),
        )


def test_공시가격은_연도별_시계열로_조회된다():
    # 세부담상한이 직전연도 세액을 요구하므로 단일 값으로는 부족하다.
    prop = Property(
        id=PropertyId("h1"),
        kind=PropertyKind.APARTMENT,
        legal_dong_code=SEOUL_GANGNAM,
        published_prices=(
            PriceFact(2026, 1_000_000_000),
            PriceFact(2027, 1_100_000_000),
        ),
    )
    assert prop.price_for(2026).value == 1_000_000_000
    assert prop.price_for(2027).value == 1_100_000_000
    assert prop.price_for(2028) is None


# --------------------------------------------------------------------------
# 나이 계산 — 연령별 세액공제(60/65/70세)의 경계
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "birth, expected",
    [
        (date(1966, 6, 1), 60),  # 과세기준일에 생일이 도래 → 만 60세
        (date(1966, 6, 2), 59),  # 하루 차이로 공제 대상에서 빠진다
        (date(1961, 5, 31), 65),
    ],
)
def test_만나이는_과세기준일_기준이고_하루_차이로_갈린다(birth, expected):
    person = Person(id=PersonId("p1"), birth_date=birth)
    assert person.age_at(assessment_date(2026)) == expected


def test_생년월일이_없으면_나이는_None으로_흘려보낸다():
    # 0살로 때우면 "공제 없음"이 사실처럼 보인다. 미상은 미상으로 전파시킨다.
    assert Person(id=PersonId("p1")).age_at(date(2026, 6, 1)) is None


# --------------------------------------------------------------------------
# 거주 구간
# --------------------------------------------------------------------------


def test_거주구간은_종료일_역전을_막는다():
    with pytest.raises(ValueError, match="거주 종료일"):
        ResidenceSpell(
            PersonId("p1"), PropertyId("h1"), start=date(2020, 1, 1), end=date(2019, 1, 1)
        )


def test_거주일수는_기준일에서_잘린다():
    spell = ResidenceSpell(PersonId("p1"), PropertyId("h1"), start=date(2020, 6, 1))
    assert spell.days_until(date(2026, 6, 1)) == (date(2026, 6, 1) - date(2020, 6, 1)).days
    # 아직 시작하지 않은 구간은 음수가 아니라 0
    future = ResidenceSpell(PersonId("p1"), PropertyId("h1"), start=date(2030, 1, 1))
    assert future.days_until(date(2026, 6, 1)) == 0


# --------------------------------------------------------------------------
# 무결성
# --------------------------------------------------------------------------


def test_소유자가_persons에_없으면_사건_생성이_실패한다():
    with pytest.raises(ValueError, match="소유자"):
        TaxCase(
            year=2026,
            persons=(Person(id=PersonId("p1")),),
            properties=(make_house("h1", 1_000_000_000),),
            ownerships=(Ownership(PersonId("p_ghost"), PropertyId("h1")),),
        )


def test_id_중복은_사건_생성이_실패한다():
    with pytest.raises(ValueError, match="중복"):
        TaxCase(
            year=2026,
            persons=(Person(id=PersonId("p1")), Person(id=PersonId("p1"))),
        )


# --------------------------------------------------------------------------
# ★ 구조 불변식 — 경쟁 계산기가 표현조차 못 하던 케이스
# --------------------------------------------------------------------------


def _couple_each_own_one() -> TaxCase:
    """부부가 각자 단독명의로 1채씩 보유. 세대 주택수 2, 인별 각 1."""
    hh = HouseholdId("hh1")
    husband = Person(
        id=PersonId("husband"), household_id=hh, spouse_id=PersonId("wife")
    )
    wife = Person(id=PersonId("wife"), household_id=hh, spouse_id=PersonId("husband"))
    return TaxCase(
        year=2026,
        persons=(husband, wife),
        households=(Household(id=hh, member_ids=(husband.id, wife.id)),),
        properties=(make_house("h1", 1_000_000_000), make_house("h2", 900_000_000)),
        ownerships=(
            Ownership(husband.id, PropertyId("h1"), share=Fraction(1)),
            Ownership(wife.id, PropertyId("h2"), share=Fraction(1)),
        ),
    )


def _couple_jointly_own_one() -> TaxCase:
    """부부공동명의 1주택. 세대 주택수 1, 인별 각 1/2 지분."""
    hh = HouseholdId("hh1")
    husband = Person(
        id=PersonId("husband"), household_id=hh, spouse_id=PersonId("wife")
    )
    wife = Person(id=PersonId("wife"), household_id=hh, spouse_id=PersonId("husband"))
    return TaxCase(
        year=2026,
        persons=(husband, wife),
        households=(Household(id=hh, member_ids=(husband.id, wife.id)),),
        properties=(make_house("h1", 1_900_000_000),),
        ownerships=(
            Ownership(husband.id, PropertyId("h1"), share=Fraction(1, 2)),
            Ownership(wife.id, PropertyId("h1"), share=Fraction(1, 2)),
        ),
    )


def test_부부_각자_단독1채와_부부공동1채는_구조적으로_다른_사건이다():
    """propertytax.co.kr이 '지원하지 않는다'고 자백한 바로 그 케이스.

    그쪽은 properties[] 배열에 소유자 필드가 없어서 두 상황을 같은 입력으로
    표현할 수밖에 없었다. 여기서는 물건 수·소유 관계·세대 주택수가 전부 다르다.
    """
    separate = _couple_each_own_one()
    joint = _couple_jointly_own_one()

    assert len(separate.properties) == 2
    assert len(joint.properties) == 1

    # 세대 단위로 센 주택 수가 다르다 — 1세대1주택 판정이 갈리는 지점
    def household_house_count(case: TaxCase) -> int:
        return len(
            {
                o.property_id
                for o in case.ownerships
                if case.find_person(o.person_id).household_id == HouseholdId("hh1")
                and case.find_property(o.property_id).is_house
            }
        )

    assert household_house_count(separate) == 2
    assert household_house_count(joint) == 1

    # 그런데 인별로 세면 둘 다 각자 1건씩이다. 이 이중성이 종부세의 본질이고,
    # 물건 배열만 있는 모델로는 절대 표현할 수 없다.
    assert len(separate.ownerships_of(PersonId("husband"))) == 1
    assert len(joint.ownerships_of(PersonId("husband"))) == 1


def test_세대원_조회는_배우자를_포함한다():
    case = _couple_each_own_one()
    members = case.household_member_ids(PersonId("husband"))
    assert set(members) == {PersonId("husband"), PersonId("wife")}


def test_법인은_세대에_속하지_않는다():
    corp = Person(id=PersonId("corp"), type=PersonType.CORPORATION)
    case = TaxCase(year=2026, persons=(corp,))
    assert corp.is_corporation
    assert case.household_member_ids(PersonId("corp")) == (PersonId("corp"),)


# --------------------------------------------------------------------------
# 확실성 3축
# --------------------------------------------------------------------------


def test_확실성은_축별_최솟값으로_합성된다():
    a = Certainty(legal=LegalStatus.ENACTED, input=InputQuality.ESTIMATED)
    b = Certainty(
        legal=LegalStatus.BILL_PENDING, input=InputQuality.OFFICIAL_NOTICE
    )
    merged = a & b
    assert merged.legal is LegalStatus.BILL_PENDING
    assert merged.input is InputQuality.ESTIMATED
    assert merged.determination is DeterminationQuality.DECIDED


def test_확실성_합성은_순서에_무관하다():
    a = Certainty(legal=LegalStatus.DECREE_PENDING)
    b = Certainty(input=InputQuality.UNKNOWN)
    c = Certainty(determination=DeterminationQuality.UNDECIDABLE)
    assert (a & b) & c == a & (b & c)
    assert Certainty.combine(a, b, c) == Certainty.combine(c, b, a)


def test_BEST는_합성의_항등원():
    x = Certainty(legal=LegalStatus.ASSUMED, input=InputQuality.ESTIMATED)
    assert Certainty.BEST & x == x
    assert Certainty.combine(None, x, None) == x


def test_라벨은_주의가_필요한_축만_돌려준다():
    clean = Certainty(
        legal=LegalStatus.ENACTED,
        input=InputQuality.OFFICIAL_NOTICE,
        determination=DeterminationQuality.DECIDED,
    )
    assert clean.labels_ko() == ()

    risky = Certainty(
        legal=LegalStatus.BILL_PENDING,
        input=InputQuality.ESTIMATED,
        determination=DeterminationQuality.UNDECIDABLE,
    )
    assert risky.labels_ko() == ("국회 미통과", "추정치", "판단 필요")
