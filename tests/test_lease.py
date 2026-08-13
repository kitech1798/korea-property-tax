"""임대차 구간(`LeaseSpell`) 테스트.

이 엔티티가 없어서 엔진이 못 하던 세 가지를 표현할 수 있는지 확인한다:
  ① 상생임대주택 특례(소득세법 시행령 §155의3)
  ② 갱신거절 통지기한(주택임대차보호법 §6①)
  ③ 임차인 계약갱신요구권(주임법 §6의3)

가장 중요한 것은 마지막 절의 '구조 불변식'이다. 임대차는 **사실**만 담고 판정은
담지 않는다 — 여기가 무너지면 화면이 넣은 결론을 엔진이 그대로 믿게 된다.
"""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from realestate_tax.domain import (
    LeaseOrigin,
    LeaseSpell,
    Person,
    PersonId,
    PriceFact,
    Property,
    PropertyId,
    PropertyKind,
    TaxCase,
)

SEOUL_GANGNAM = "1168010100"
HOUSE = PropertyId("h1")


def make_house(pid: str = "h1") -> Property:
    return Property(
        id=PropertyId(pid),
        kind=PropertyKind.APARTMENT,
        legal_dong_code=SEOUL_GANGNAM,
        published_prices=(PriceFact(year=2026, value=1_000_000_000),),
    )


def lease(**kw) -> LeaseSpell:
    base = dict(property_id=HOUSE, start=date(2025, 2, 1), end=date(2027, 1, 31))
    return LeaseSpell(**{**base, **kw})


# --------------------------------------------------------------------------
# 입력 검증 — 존재할 수 없는 사실을 막는다
# --------------------------------------------------------------------------


def test_종료일이_개시일보다_빠르면_거부():
    with pytest.raises(ValueError, match="임대 종료일"):
        lease(start=date(2027, 1, 31), end=date(2025, 2, 1))


def test_퇴거일이_개시일보다_빠르면_거부():
    with pytest.raises(ValueError, match="퇴거일"):
        lease(vacated_on=date(2024, 12, 1))


@pytest.mark.parametrize("field", ["deposit", "monthly_rent"])
def test_임대료가_음수면_거부(field: str):
    with pytest.raises(ValueError):
        lease(**{field: -1})


def test_없는_물건의_임대차는_사건_생성에서_거부된다():
    """오타 난 물건 id는 조용히 사라지는 것이 가장 나쁘다.

    상생임대 특례가 적용되지 않는 것으로 계산되는데, 사용자는 입력했다고 믿는다.
    누락은 화면에 보이지 않으므로 여기서 끊는다.
    """
    with pytest.raises(ValueError, match="임대차의 물건이 properties에 없다"):
        TaxCase(
            year=2026,
            persons=(Person(id=PersonId("p1")),),
            properties=(make_house(),),
            leases=(lease(property_id=PropertyId("오타")),),
        )


# --------------------------------------------------------------------------
# 실제 임대기간 — 계약서가 아니라 실제를 본다
# --------------------------------------------------------------------------


def test_중도_퇴거가_있으면_실제_종료일은_퇴거일():
    """§155의3①2호·3호는 "임대한 기간"이라고 적는다. 계약 기간이 아니다.

    직전계약 1년 6개월 · 상생계약 2년 요건이 이 구분에서 갈린다.
    """
    spell = lease(vacated_on=date(2026, 6, 30))
    assert spell.actual_end == date(2026, 6, 30)
    assert spell.end == date(2027, 1, 31)  # 계약서상 기간은 그대로 남는다


def test_중도_퇴거가_없으면_실제_종료일은_계약_종료일():
    assert lease().actual_end == date(2027, 1, 31)


def test_기간을_정하지_않으면_실제_종료일은_None():
    """주임법 §4①의 '2년 의제'는 판정이므로 도메인이 하지 않는다."""
    assert lease(end=None).actual_end is None


def test_실제_임대일수는_퇴거일에서_멈춘다():
    spell = lease(vacated_on=date(2026, 2, 1))
    assert spell.rented_days_until(date(2027, 1, 31)) == 365
    assert spell.rented_days_until(date(2025, 8, 1)) == 181  # 기준일이 더 이르면 거기까지


def test_임대_개시_전_기준일이면_0일():
    assert lease().rented_days_until(date(2024, 1, 1)) == 0


def test_covers는_실제_종료일_기준():
    spell = lease(vacated_on=date(2026, 6, 30))
    assert spell.covers(date(2026, 6, 30))
    assert not spell.covers(date(2026, 7, 1))  # 계약은 살아 있어도 임대는 끝났다


# --------------------------------------------------------------------------
# 이력 조회 — 직전계약을 잘못 집으면 상생임대 판정이 통째로 틀린다
# --------------------------------------------------------------------------


def test_이력은_개시일_순으로_정렬되어_나온다():
    """상생임대 판정은 '직전임대차계약 → 상생임대차계약'의 연속 관계를 읽는다.

    입력 순서에 의존하면 사용자가 나중 계약을 먼저 적은 사건에서 조용히 틀린다.
    """
    later = lease(start=date(2025, 2, 1), end=date(2027, 1, 31))
    earlier = lease(start=date(2023, 2, 1), end=date(2025, 1, 31))

    case = TaxCase(
        year=2026,
        persons=(Person(id=PersonId("p1")),),
        properties=(make_house(),),
        leases=(later, earlier),  # 뒤집어 넣는다
    )
    assert case.leases_of(HOUSE) == (earlier, later)


def test_다른_물건의_임대차는_섞이지_않는다():
    mine = lease()
    other = lease(property_id=PropertyId("h2"))
    case = TaxCase(
        year=2026,
        persons=(Person(id=PersonId("p1")),),
        properties=(make_house("h1"), make_house("h2")),
        leases=(mine, other),
    )
    assert case.leases_of(HOUSE) == (mine,)
    assert case.leases_of(PropertyId("h2")) == (other,)


# --------------------------------------------------------------------------
# 법이 요구하는 사실을 표현할 수 있는가
# --------------------------------------------------------------------------


def test_승계받은_계약을_구분해_적을_수_있다():
    """§155의3①1호 괄호 — "해당 주택의 취득으로 임대인의 지위가 승계된 경우의
    임대차계약은 제외"한다.

    세입자가 살던 집을 사서 물려받은 계약은 직전임대차계약이 **될 수 없다.**
    이 구분을 못 적으면 상생임대 요건을 충족한다고 잘못 안내하게 된다.
    """
    succeeded = lease(start=date(2023, 2, 1), end=date(2025, 1, 31),
                      origin=LeaseOrigin.SUCCEEDED)
    own = lease(origin=LeaseOrigin.NEW)
    assert succeeded.origin is LeaseOrigin.SUCCEEDED
    assert own.origin is LeaseOrigin.NEW


def test_갱신의_종류를_구분해_적을_수_있다():
    """주임법 §6의3②는 갱신요구권을 1회로 제한한다.

    묵시적 갱신(§6①)은 요구권 행사가 아니므로 권리가 남고, 요구권 행사에 의한
    갱신이면 소진된다. 둘을 못 나누면 "다음 만기에 내보낼 수 있는가"의 답이 뒤집힌다.
    """
    assert LeaseOrigin.IMPLICIT_RENEWAL is not LeaseOrigin.TENANT_RENEWAL_RIGHT
    assert lease(origin=LeaseOrigin.TENANT_RENEWAL_RIGHT).origin is (
        LeaseOrigin.TENANT_RENEWAL_RIGHT
    )


def test_계약_체결일을_임대_개시일과_따로_받는다():
    """§155의3①1호는 '21.12.20.~'26.12.31. 중 **체결**을 요건으로 삼는다.

    임대 개시일로 판정하면 체결은 기한 내인데 개시가 넘어간 사건에서 틀린다.
    """
    spell = lease(contracted_on=date(2024, 12, 15), start=date(2025, 2, 1))
    assert spell.contracted_on == date(2024, 12, 15)
    assert spell.contracted_on != spell.start


# --------------------------------------------------------------------------
# 구조 불변식 — 여기가 무너지면 나머지 테스트가 다 무의미해진다
# --------------------------------------------------------------------------


def test_계약금_증빙은_기본값이_모름이다():
    """요건을 충족한 것으로 기본 가정하면 **과소신고**가 된다.

    §155의3①1호는 "계약금을 지급받은 사실이 증빙서류에 의해 확인되는 경우로
    한정한다". True를 기본값으로 두면 확인되지 않은 사건이 조용히 요건을 통과한다.
    이 프로젝트가 '모르면 보수적으로'를 지키는 자리다.
    """
    assert lease().down_payment_evidenced is None


def test_임대차에_판정_결과_필드가_없다():
    """도메인은 사실만 담는다.

    `is_sangsaeng` 같은 필드를 두는 순간 화면이 넣은 결론을 엔진이 그대로 믿는다.
    상생임대 여부는 **연속한 두 계약을 비교해야** 나오는 판정이고, 갱신요구권
    소진 여부는 **이력 전체를 봐야** 나오는 판정이다. 둘 다 엔진의 몫이다.
    """
    banned = {
        "is_sangsaeng",
        "sangsaeng",
        "is_sangsaeng_lease",
        "qualifies",
        "renewal_right_exhausted",
        "renewal_right_available",
        "is_exempt",
    }
    assert banned.isdisjoint(LeaseSpell.__dataclass_fields__)


def test_임대차는_불변이다():
    """사건을 조건만 바꿔 여러 번 계산하는 파이프라인이 순수해야 공짜가 된다."""
    spell = lease()
    with pytest.raises(dataclasses.FrozenInstanceError):
        spell.deposit = 1  # type: ignore[misc]
