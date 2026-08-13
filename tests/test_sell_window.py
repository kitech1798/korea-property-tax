"""매도 시점 최적화 테스트 — 손으로 적은 상담 메모를 엔진이 통째로 재현한다.

사건(재혁 사례)
  · 취득 2017-03-01, 취득 당시 조정대상지역, **실거주 0년**
  · 직전임대차 2023-02-01~2025-01-31 · 상생임대차 2025-02-01~2027-01-31
  · 양도가액 32억 / 취득가액 12억 / 보유 10년

메모가 잡은 것과 놓친 것을 둘 다 확인한다.
  잡은 것 — 세입자 통지기한 2026-11-30
  놓친 것 — 진짜 절벽은 상생임대 기한(2028-01-31)이 아니라 **2028-01-01**이다.
           개편안이 장기보유특별공제의 보유공제를 연 4%에서 2%로 깎기 때문이다.
"""

from __future__ import annotations

from datetime import date

import pytest

from realestate_tax.domain import (
    Household,
    HouseholdId,
    LeaseOrigin,
    LeaseSpell,
    Ownership,
    Person,
    PersonId,
    PriceFact,
    Property,
    PropertyId,
    PropertyKind,
    TaxCase,
)
from realestate_tax.engine.sell_window import ConstraintKind, optimize
from realestate_tax.engine.transfer_tax import TransferEvent
from realestate_tax.rules import RuleSet, Track, default_ruleset_root

ME = PersonId("me")
HOUSE = PropertyId("본가")
SEOUL = "1168010100"
EOK = 100_000_000

TENANT = "임차인A"
PRIOR = LeaseSpell(
    property_id=HOUSE, start=date(2023, 2, 1), end=date(2025, 1, 31),
    deposit=5 * EOK, contracted_on=date(2022, 12, 10),
    down_payment_evidenced=True, tenant_ref=TENANT,
)
SANGSAENG = LeaseSpell(
    property_id=HOUSE, start=date(2025, 2, 1), end=date(2027, 1, 31),
    deposit=520_000_000, contracted_on=date(2024, 12, 15),
    down_payment_evidenced=True, tenant_ref=TENANT,
)


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


def make_case(*leases: LeaseSpell) -> TaxCase:
    hh = HouseholdId("hh")
    return TaxCase(
        year=2027,
        persons=(Person(id=ME, household_id=hh, birth_date=date(1970, 1, 1)),),
        households=(Household(id=hh, member_ids=(ME,)),),
        properties=(
            Property(
                id=HOUSE, kind=PropertyKind.APARTMENT, legal_dong_code=SEOUL,
                display_name="본가",
                published_prices=(PriceFact(2027, 20 * EOK),),
            ),
        ),
        ownerships=(Ownership(ME, HOUSE, acquired_on=date(2017, 3, 1)),),
        leases=leases,
    )


EVENT = TransferEvent(
    property_id=HOUSE, person_id=ME,
    transfer_date=date(2027, 6, 1),
    transfer_price=32 * EOK, acquisition_price=12 * EOK,
    holding_years=10, residence_years=0,
)


@pytest.fixture(scope="module")
def window(rs: RuleSet):
    return optimize(
        make_case(PRIOR, SANGSAENG), EVENT, rs,
        start=date(2026, 8, 13), end=date(2029, 12, 31), track=Track.REFORM,
    )


def at(window, on: date) -> int:
    return next(p.transfer_tax for p in window.points if p.on == on)


# --------------------------------------------------------------------------
# 제약 — 세법 밖에서 오는 기한
# --------------------------------------------------------------------------


def test_세입자_통지기한이_2026_11_30이다(window):
    """★ 메모가 잡은 것. 주임법 §6① "끝나기 6개월 전부터 2개월 전까지".

    임대차 종료 2027-01-31 → 창구는 2026-07-31 ~ **2026-11-30**.
    세액 곡선에는 흔적조차 남지 않는 기한인데, 실제로는 가장 먼저 온다.
    """
    notice = next(c for c in window.constraints if "통지" in c.label_ko)
    assert notice.kind is ConstraintKind.DEADLINE
    assert notice.window == (date(2026, 7, 31), date(2026, 11, 30))
    assert notice.on == date(2026, 11, 30)


def test_통지기한이_세법_기한보다_먼저_온다(window):
    """이 모듈이 존재하는 이유. 세법만 보면 "2028-01-31까지"라고 답하는데,
    그때는 손쓸 시점이 1년 2개월 전에 지나 있다."""
    notice = next(c for c in window.constraints if "통지" in c.label_ko)
    sang = next(c for c in window.constraints if "상생임대" in c.label_ko)
    assert notice.on < sang.on
    assert sang.on == date(2028, 1, 31)


def test_갱신요구권을_안_썼으면_위험으로_낸다(window):
    """§6의3①은 "제6조에도 불구하고"로 시작한다 — 통지만으로는 막지 못한다."""
    risk = next(c for c in window.constraints if c.kind is ConstraintKind.RISK)
    assert "갱신요구권" in risk.label_ko
    assert "매수인의 실거주는" in risk.note_ko


def test_갱신요구권을_이미_썼으면_위험이_없다(rs: RuleSet):
    used = LeaseSpell(
        property_id=HOUSE, start=date(2025, 2, 1), end=date(2027, 1, 31),
        deposit=520_000_000, contracted_on=date(2024, 12, 15),
        down_payment_evidenced=True, tenant_ref=TENANT,
        origin=LeaseOrigin.TENANT_RENEWAL_RIGHT,
    )
    w = optimize(make_case(PRIOR, used), EVENT, rs,
                 start=date(2026, 8, 13), end=date(2028, 12, 31))
    assert not [c for c in w.constraints if c.kind is ConstraintKind.RISK]


def test_임차인을_구분할_수_없으면_위험을_지우지_않는다(rs: RuleSet):
    """별칭이 없으면 같은 임차인인지 알 수 없다. 모르는 것을 소진으로 처리하면
    위험이 사라진 것처럼 보인다."""
    anon = LeaseSpell(
        property_id=HOUSE, start=date(2025, 2, 1), end=date(2027, 1, 31),
        deposit=520_000_000, contracted_on=date(2024, 12, 15),
        down_payment_evidenced=True, origin=LeaseOrigin.TENANT_RENEWAL_RIGHT,
    )
    w = optimize(make_case(PRIOR, anon), EVENT, rs,
                 start=date(2026, 8, 13), end=date(2028, 12, 31))
    assert [c for c in w.constraints if c.kind is ConstraintKind.RISK]


# --------------------------------------------------------------------------
# 세액 절벽 — 경계를 표본에 넣어야 보인다
# --------------------------------------------------------------------------


def test_2028년_1월_1일에_절벽이_있다(window):
    """★★ 메모가 놓친 것.

    메모는 "2029년 1월부터 최대공제액 10억 제한"만 봤다. 그런데 실거주 0년인
    이 사건을 때리는 것은 한도가 아니라 **공제율 전환**이고, 그 절벽은 2029년이
    아니라 2028-01-01에 있다(보유 연4% → 연2%).
    """
    assert at(window, date(2028, 1, 1)) > at(window, date(2027, 12, 31))


def test_2028년_2월_1일에_두_번째_절벽이_있다(window):
    """상생임대 양도기한(2028-01-31)을 하루 넘기면 비과세 12억이 사라진다."""
    assert at(window, date(2028, 2, 1)) > at(window, date(2028, 1, 31))


def test_절벽은_하루_차이로_잡힌다(window):
    """균등 격자만 훑으면 1월 31일과 2월 1일 사이를 통째로 놓친다.
    법적 경계를 표본에 직접 넣기 때문에 하루짜리 절벽이 하루로 잡힌다."""
    overnight = [c for c in window.cliffs() if c.is_overnight]
    assert {c.after for c in overnight} >= {date(2028, 1, 1), date(2028, 2, 1)}


# --------------------------------------------------------------------------
# 최적안
# --------------------------------------------------------------------------


def test_최적안은_보유_10년이_되는_날이다(window):
    """★ 2026-08-13 회귀 — 예전에는 2027-02-01(공실 인도 첫날)이 답이었다.

    그때는 `optimize()`가 후보일마다 날짜만 바꾸고 **보유기간을 고정**해서,
    보유기간이 만드는 절벽이 곡선에서 통째로 사라져 있었다. 세무·개발·이용자
    세 관점이 독립적으로 같은 결함을 짚었다.

    취득일이 2017-03-01이므로 보유 10년은 **2027-03-01**에 찬다. 장기보유공제가
    거기서 한 단 오르므로, 한 달을 더 기다리는 것이 답이다.
    공실 인도(2027-02-01)만 보고 고르면 그 한 달을 놓친다.
    """
    best = window.best
    assert best is not None
    assert best.on == date(2027, 3, 1)
    assert best.transfer_tax < at(window, date(2027, 2, 1)), "보유 10년 절벽이 안 보인다"
    assert best.transfer_tax < at(window, date(2028, 1, 31))


def test_보유기간이_매도일을_따라_움직인다(window):
    """이 모듈의 존재 이유. 날짜만 바꾸고 기간을 고정하면 곡선이 평평해진다."""
    early = at(window, date(2026, 9, 1))    # 보유 9년
    later = at(window, date(2027, 3, 1))    # 보유 10년
    assert early != later, "보유기간이 안 움직인다 — 곡선이 평평하다"


def test_임차인이_사는_동안은_고르지_않는다(window):
    """세액이 같아도 팔 수 없는 날은 최적안이 될 수 없다."""
    blocked = [p for p in window.points if p.on < date(2027, 2, 1)]
    assert blocked and all(not p.feasible for p in blocked)
    assert all("2027-02-01" in p.blocked_by[0] for p in blocked)


def test_2026년_양도는_상생임대_2년을_아직_못_채웠다(rs: RuleSet):
    """★ 회귀 — 최적화기를 만들다 잡은 결함.

    판정이 양도일을 보지 않아 계약서에 적힌 **미래의 임대기간**까지 앞당겨 셌다.
    2026-08 양도 시점에 상생임대차는 18개월뿐인데 24개월로 읽혀, 아직 요건을
    못 채운 사람에게 비과세를 내주고 있었다.
    §155의3①은 "요건을 **모두 갖춘** 주택을 양도하는 경우"다.
    """
    w = optimize(make_case(PRIOR, SANGSAENG), EVENT, rs,
                 start=date(2026, 8, 13), end=date(2027, 12, 31),
                 require_vacant=False)
    # 요건 미달이라 비과세가 없으므로 2027-02-01보다 세금이 많아야 한다
    assert at(w, date(2026, 9, 1)) > at(w, date(2027, 2, 1))


# --------------------------------------------------------------------------
# 상황별 시뮬레이션 — 갱신되면 어떻게 되나
# --------------------------------------------------------------------------


def test_갱신되면_최적안이_2029년으로_밀리고_세금이_뛴다(rs: RuleSet, window):
    """★ 최악 시나리오를 숫자로 낸다.

    통지기한을 놓치거나 임차인이 갱신요구권을 행사하면 임대차가 2년 붙어
    공실 인도가 2029-02-01 이후가 된다. 그때는 상생임대 양도기한(2028-01-31)이
    이미 지나 거주요건 면제가 사라진다 — **비과세 12억과 장기보유특별공제가
    함께 무너진다.**

    "통지를 놓치면 얼마 손해인가"에 금액으로 답하는 것이 이 계산의 목적이다.
    """
    renewed = optimize(
        make_case(PRIOR, SANGSAENG), EVENT, rs,
        start=date(2026, 8, 13), end=date(2029, 12, 31), track=Track.REFORM,
        assume_renewal=True,
    )
    assert renewed.best is not None
    assert renewed.best.on >= date(2029, 2, 1)

    loss = renewed.best.transfer_tax - window.best.transfer_tax
    assert loss > 3 * EOK, f"통지 실패 시 추가 부담 {loss:,}원"

    # 제약이 답을 바꿨다는 사실 자체가 값으로 남는다
    assert renewed.constraint_cost > 0


def test_승계_조건_매도는_공실_제약을_받지_않는다(rs: RuleSet):
    """세입자를 넘기는 조건이면 임대차가 매도를 막지 않는다.
    다만 기한과 위험은 그대로 낸다 — 매수인이 떠안는 사실을 숨기지 않는다."""
    w = optimize(make_case(PRIOR, SANGSAENG), EVENT, rs,
                 start=date(2026, 8, 13), end=date(2028, 12, 31),
                 require_vacant=False)
    assert all(p.feasible for p in w.points)
    assert w.deadlines
    assert not w.assumes_vacant


def test_임대차가_없으면_제약도_없다(rs: RuleSet):
    w = optimize(make_case(), EVENT, rs,
                 start=date(2027, 1, 1), end=date(2027, 12, 31))
    assert w.constraints == ()
    assert w.best is not None


def test_이미_끝난_임대차에_지나간_통지기한을_시키지_않는다(rs: RuleSet):
    """★ 2026-08-13 감사(법률·이용자 두 관점).

    만기가 지난 계약을 '현 임대차'로 삼아 20개월 전 날짜를 두고 "통지해야 합니다"라고
    시키고 있었다. 사용자가 무엇을 해야 할지 알 수 없는 안내다.

    만기가 지났으면 두 갈래다 — 임차인이 나갔거나 묵시적으로 갱신됐거나(§6①②).
    엔진은 모른다. **단정하지 않고 묻는다.**
    """
    w = optimize(make_case(PRIOR, SANGSAENG), EVENT, rs,
                 start=date(2028, 6, 1), end=date(2029, 12, 31))
    assert not [c for c in w.constraints if c.kind is ConstraintKind.DEADLINE
                and "통지" in c.label_ko], "이미 지난 통지기한을 시켰다"
    risk = next(c for c in w.constraints if "끝난 임대차" in c.label_ko)
    assert risk.kind is ConstraintKind.RISK
    assert "갱신" in risk.note_ko and "§6①②" in risk.note_ko
    assert "확인해주세요" in risk.action_ko
    # 이미 끝났으므로 매도를 막지 않는다
    assert all(p.feasible for p in w.points)


def test_종료일이_없으면_조용히_넘어가지_않는다(rs: RuleSet):
    """★ 예전에는 임대차가 있어도 제약 0건으로 조용히 지나갔다.
    세입자가 사는데 아무 기한도 안 뜨는 화면이 된다."""
    open_ended = LeaseSpell(
        property_id=HOUSE, start=date(2025, 2, 1), end=None,
        deposit=5 * EOK, contracted_on=date(2024, 12, 15),
        down_payment_evidenced=True, tenant_ref=TENANT,
    )
    w = optimize(make_case(open_ended), EVENT, rs,
                 start=date(2026, 8, 13), end=date(2028, 12, 31))
    assert w.constraints, "임대차가 있는데 제약이 0건이다"
    risk = w.constraints[0]
    assert risk.kind is ConstraintKind.RISK
    assert "§4①" in risk.basis_ko  # 기간 미정은 2년으로 본다
