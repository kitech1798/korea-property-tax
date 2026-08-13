"""일시적 2주택 처분기한 3년 → 2년 단축 (개편안, 소득령 §155①).

★ 이 파일이 존재하는 이유

룰셋에 `regulated_shortened`를 넣고 payload를 대조하는 테스트를 5개나 썼는데,
**엔진은 그 값을 한 번도 읽지 않았다.** 테스트는 전부 통과했다.
2026-08-13 멀티에이전트 감사에서 세무·원문대조 두 관점이 독립적으로 잡았다.

이 프로젝트가 여섯 번 앓은 병의 일곱 번째다 — *모델에 있는 사실을 엔진이 안 읽는다.*
룰셋 값을 확인하는 테스트는 **연결을 검증하지 못한다.** 세액으로 재야 한다.
"""

from __future__ import annotations

from datetime import date

import pytest

from realestate_tax.domain import (
    Household, HouseholdId, Ownership, Person, PersonId,
    PriceFact, Property, PropertyId, PropertyKind, TaxCase,
)
from realestate_tax.engine.transfer_tax import TransferEvent, compute_transfer_tax
from realestate_tax.rules import RuleSet, Track, default_ruleset_root

ME = PersonId("me")
HH = HouseholdId("hh")
OLD, NEW = PropertyId("종전"), PropertyId("신규")
SEOUL = "1168010100"   # 강남구 — 조정대상지역
BUSAN = "2635010300"   # 해운대구 — 비규제
EOK = 100_000_000


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


def make_case(new_acquired: date, *, old_dong=SEOUL, new_dong=SEOUL,
              year=2028, old_acquired=date(2020, 3, 1)) -> TaxCase:
    props = tuple(
        Property(id=pid, kind=PropertyKind.APARTMENT, legal_dong_code=dong,
                 display_name=str(pid), published_prices=(PriceFact(year, 15 * EOK),))
        for pid, dong in ((OLD, old_dong), (NEW, new_dong))
    )
    return TaxCase(
        year=year,
        persons=(Person(id=ME, household_id=HH, birth_date=date(1970, 1, 1)),),
        households=(Household(id=HH, member_ids=(ME,)),),
        properties=props,
        ownerships=(
            Ownership(ME, OLD, acquired_on=old_acquired),
            Ownership(ME, NEW, acquired_on=new_acquired),
        ),
    )


def sell(rs: RuleSet, case: TaxCase, on: date, track=Track.REFORM):
    event = TransferEvent(
        property_id=OLD, person_id=ME, transfer_date=on,
        transfer_price=20 * EOK, acquisition_price=10 * EOK,
        holding_years=8, residence_years=8,
    )
    return compute_transfer_tax(case, event, rs, track=track)


def is_one_house(result) -> bool:
    return bool(result.trace.find("tr.01.house_count").output.amount)


# --------------------------------------------------------------------------
# 단축이 실제로 세액을 바꾸는가
# --------------------------------------------------------------------------


def test_조정지역_두_채면_2년을_넘길_때_특례가_사라진다(rs: RuleSet):
    """★ 이 테스트가 없어서 4.9억 오차가 통과했다.

    상세본 p.73 단서신설 — "조정대상지역 소재 종전주택을 보유한 상태에서
    조정대상지역 소재 신규주택 취득 시 신규주택 취득일부터 2년 이내".

    신규 취득 2026-09-01 → 2028-11-01 양도는 2년 2개월이다. 개편안대로면 탈락이다.
    """
    case = make_case(date(2026, 9, 1))
    r = sell(rs, case, date(2028, 11, 1))
    assert not is_one_house(r), "2년을 넘겼는데 1세대1주택으로 셌다"


def test_2년_안에_팔면_특례가_그대로다(rs: RuleSet):
    case = make_case(date(2026, 9, 1))
    r = sell(rs, case, date(2028, 8, 1))  # 1년 11개월
    assert is_one_house(r)


def test_단축_전후로_세액이_실제로_갈린다(rs: RuleSet):
    """세액으로 재지 않으면 연결을 검증한 것이 아니다."""
    inside = sell(rs, make_case(date(2026, 9, 1)), date(2028, 8, 1))
    outside = sell(rs, make_case(date(2026, 9, 1)), date(2028, 11, 1))
    assert outside.total.as_int() > inside.total.as_int()
    assert outside.total.as_int() - inside.total.as_int() > 1 * EOK


# --------------------------------------------------------------------------
# 단축이 걸리지 않아야 하는 경우 — 없는 기한을 만들면 안 된다
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "old_dong, new_dong, label",
    [
        (SEOUL, BUSAN, "신규가 비규제"),
        (BUSAN, SEOUL, "종전이 비규제"),
        (BUSAN, BUSAN, "둘 다 비규제"),
    ],
)
def test_하나라도_비규제면_3년_그대로다(rs: RuleSet, old_dong, new_dong, label):
    """단축 요건은 **두 주택 모두** 조정대상지역이다.

    기본값을 2년으로 바꿔 버리면 비규제 지역 보유자에게 **없는 기한**을 만들어
    팔라고 재촉하게 된다.
    """
    case = make_case(date(2026, 9, 1), old_dong=old_dong, new_dong=new_dong)
    r = sell(rs, case, date(2028, 11, 1))  # 2년 2개월 — 3년이면 통과
    assert is_one_house(r), f"{label}인데 단축이 걸렸다"


def test_26년_8월_3일_취득은_종전규정_3년이다(rs: RuleSet):
    """상세본 p.73 <경과조치> — '26.8.3. 이전 취득은 종전규정.
    하루 차이로 1년이 갈린다."""
    case = make_case(date(2026, 8, 3))
    r = sell(rs, case, date(2028, 11, 1))
    assert is_one_house(r)


def test_26년_8월_4일_취득부터_단축이_걸린다(rs: RuleSet):
    case = make_case(date(2026, 8, 4))
    r = sell(rs, case, date(2028, 11, 1))
    assert not is_one_house(r)


def test_현행_트랙에는_단축이_없다(rs: RuleSet):
    """개편안은 시행령 개정 사항이라 아직 확정이 아니다.
    두 트랙을 나란히 보여주지 않는 것 자체가 오류라는 전제가 여기서도 지켜져야 한다."""
    case = make_case(date(2026, 9, 1))
    r = sell(rs, case, date(2028, 11, 1), track=Track.CURRENT)
    assert is_one_house(r)


def test_시행일_전_양도는_3년이다(rs: RuleSet):
    """<적용시기> '26.10.1. 이후 양도분부터."""
    case = make_case(date(2026, 8, 10), year=2026)
    r = sell(rs, case, date(2026, 9, 30))
    assert is_one_house(r)


# --------------------------------------------------------------------------
# 탈락했을 때 이유를 말하는가
# --------------------------------------------------------------------------


def test_기한을_넘겼으면_왜_넘겼는지_말한다(rs: RuleSet):
    """3년인 줄 알고 계획을 세운 사람에게 '아니다'만 말하면 안 된다.
    경과조치로 구제될 수 있다는 것까지 알려야 한다."""
    r = sell(rs, make_case(date(2026, 9, 1)), date(2028, 11, 1))
    alts = r.trace.find("tr.01.house_count").alternatives_not_taken
    assert alts, "탈락 사유를 아무 데도 남기지 않았다"
    note = " ".join(a.reason_ko for a in alts)
    assert "처분기한" in note
    assert "계약금" in note, "경과조치 안내가 없다 — 3년으로 구제될 수 있는 사람이 모른다"
    assert "아직 판정하지 않" not in note, "판정을 마쳤는데 안 했다고 말한다"
