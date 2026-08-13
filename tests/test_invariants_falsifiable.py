"""새 불변식이 **진짜로 울리는지** 일부러 망가뜨려 확인한다.

이 프로젝트는 "정의상 항상 0이라 절대 안 울리는 검수"를 만든 적이 있다.
통과 137건은 엔진이 옳다는 증거일 수도 있고, 검사기가 죽어 있다는 증거일 수도 있다.
둘을 가르는 방법은 하나뿐이다 — **울려야 할 때 우는지 본다.**

각 테스트는 엔진을 한 곳만 뒤집고, 해당 불변식이 그것을 잡아내는지 확인한다.
잡지 못하면 그 불변식은 장식이다.
"""

from __future__ import annotations

from datetime import date

import pytest

from realestate_tax.domain import (
    Household, HouseholdId, LeaseSpell, Ownership, Person, PersonId,
    PriceFact, Property, PropertyId, PropertyKind, TaxCase,
)
from realestate_tax.engine import sell_window as sw
from realestate_tax.engine import transfer_tax as tt
from realestate_tax.engine.sangsaeng import within_transfer_deadline
from realestate_tax.engine.sell_window import SellWindow
from realestate_tax.engine.transfer_tax import TransferEvent
from realestate_tax.rules import RuleSet, default_ruleset_root
from sim.invariants import (
    _sangsaeng_deadline_direction,
    _sangsaeng_never_raises_tax,
    _sell_window_contract,
)
from sim.runner import Outcome
from sim.spec import Scenario, TransferSpec

ME, HOUSE, EOK = PersonId("me"), PropertyId("본가"), 100_000_000


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    """재혁 사건 — 상생임대 요건을 갖춘 표준 사례."""
    hh = HouseholdId("hh")
    case = TaxCase(
        year=2027,
        persons=(Person(id=ME, household_id=hh, birth_date=date(1970, 1, 1)),),
        households=(Household(id=hh, member_ids=(ME,)),),
        properties=(
            Property(id=HOUSE, kind=PropertyKind.APARTMENT, legal_dong_code="1168010100",
                     display_name="본가", published_prices=(PriceFact(2027, 20 * EOK),)),
        ),
        ownerships=(Ownership(ME, HOUSE, acquired_on=date(2017, 3, 1)),),
        leases=(
            LeaseSpell(property_id=HOUSE, start=date(2023, 2, 1), end=date(2025, 1, 31),
                       deposit=5 * EOK, contracted_on=date(2022, 12, 10),
                       down_payment_evidenced=True, tenant_ref="A"),
            LeaseSpell(property_id=HOUSE, start=date(2025, 2, 1), end=date(2027, 1, 31),
                       deposit=520_000_000, contracted_on=date(2024, 12, 15),
                       down_payment_evidenced=True, tenant_ref="A"),
        ),
    )
    event = TransferEvent(
        property_id=HOUSE, person_id=ME, transfer_date=date(2027, 2, 1),
        transfer_price=32 * EOK, acquisition_price=12 * EOK,
        holding_years=10, residence_years=0,
    )
    return Scenario(
        id="falsify", label_ko="반증용", case=case, subject=ME,
        transfer=TransferSpec(event=event),
    )


@pytest.fixture(scope="module")
def scenario_early(scenario) -> Scenario:
    """같은 사건인데 **임차인이 아직 사는 동안**을 검토 시작점으로 잡은 것.

    `_sell_window_contract`는 양도일부터 표본을 뜨므로, 양도일이 임대차 종료일
    이후면 '막힌 날짜'가 검사 구간에 아예 없다. 그러면 제약 관련 불변식은
    울릴 데이터를 못 받는다 — 검사기가 멀쩡해도 조용해진다.
    반증 테스트를 짜다 이 사각을 발견했다.
    """
    event = scenario.transfer.event
    return Scenario(
        id="falsify-early", label_ko="반증용(임차 중)", case=scenario.case,
        subject=ME,
        transfer=TransferSpec(event=__import__("dataclasses").replace(
            event, transfer_date=date(2026, 9, 1)
        )),
    )


@pytest.fixture(scope="module")
def outcome() -> Outcome:
    """불변식이 쓰지 않는 자리는 비운다 — 이 검사들은 사건에서 직접 다시 계산한다."""
    return Outcome(
        scenario_id="falsify", label_ko="반증용", origin="", intent_ko="",
        expectation_ko="", tags=(),
    )


# --------------------------------------------------------------------------
# 망가뜨리지 않았을 때는 조용해야 한다 (거짓 양성 확인)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "checker",
    [_sangsaeng_never_raises_tax, _sangsaeng_deadline_direction, _sell_window_contract],
)
@pytest.mark.parametrize("which", ["scenario", "scenario_early"])
def test_정상_엔진에서는_울리지_않는다(request, outcome, rs, checker, which):
    """거짓 양성이 없어야 한다. 아무 때나 우는 경보는 끄게 되고, 끄면 없는 것과 같다."""
    assert list(checker(request.getfixturevalue(which), outcome, rs)) == []


# --------------------------------------------------------------------------
# 망가뜨리면 울려야 한다
# --------------------------------------------------------------------------


def test_면제_조건을_뒤집으면_세금이_늘어_잡힌다(scenario, outcome, rs, monkeypatch):
    """요건을 갖춘 쪽에 면제를 안 주고 못 갖춘 쪽에 주도록 뒤집는다.

    그러면 임대차 이력이 있는 사건이 이력을 지운 사건보다 비싸진다 —
    거주요건 '면제'가 세금을 늘리는 일은 법적으로 불가능하다.
    """
    monkeypatch.setattr(tt, "waives_residence", lambda v, on: not v.applies)
    violations = list(_sangsaeng_never_raises_tax(scenario, outcome, rs))
    assert violations, "면제를 반대로 걸었는데 불변식이 조용하다 — 검사기가 죽어 있다"
    assert violations[0].rule == "sangsaeng_never_raises_tax"
    assert violations[0].severity == "block"


def test_양도기한_부등호를_뒤집으면_잡힌다(scenario, outcome, rs, monkeypatch):
    """기한 '안'과 '밖'을 바꾼다. 기한 다음 날이 기한일보다 싸지므로 잡혀야 한다."""
    monkeypatch.setattr(
        tt, "waives_residence",
        lambda v, on: v.applies and within_transfer_deadline(v, on) is False,
    )
    violations = list(_sangsaeng_deadline_direction(scenario, outcome, rs))
    assert violations, "기한 판정을 뒤집었는데 조용하다"
    assert violations[0].rule == "sangsaeng_deadline_direction"


def test_경계를_표본에서_빼면_잡힌다(scenario, outcome, rs, monkeypatch):
    """★ 이 검사가 가장 중요하다.

    후보 날짜를 균등 격자로만 뽑아도 결과는 멀쩡해 보인다 — 최적안도 나오고
    세액도 그럴듯하다. 다만 1월 31일과 2월 1일 사이의 절벽을 통째로 못 볼 뿐이다.
    결과를 보는 검사로는 절대 안 잡히고, **표본 자체**를 봐야 잡힌다.
    """
    original = sw._candidates
    monkeypatch.setattr(sw, "_candidates", lambda start, end, marks: original(start, end, []))
    violations = list(_sell_window_contract(scenario, outcome, rs))
    assert violations, "경계를 표본에서 뺐는데 조용하다"
    assert any(v.rule == "sell_window_samples_deadlines" for v in violations)


def test_최적안이_제약을_무시하면_잡힌다(scenario_early, outcome, rs, monkeypatch):
    """`best`가 팔 수 없는 날을 고르도록 만든다.

    임차인이 사는 동안이 검토 구간에 들어 있어야 잡을 수 있으므로 `scenario_early`를 쓴다.
    """
    monkeypatch.setattr(
        SellWindow, "best",
        property(lambda self: min(self.points, key=lambda p: (p.transfer_tax, p.on))),
    )
    violations = list(_sell_window_contract(scenario_early, outcome, rs))
    assert any(v.rule == "sell_window_best_feasible" for v in violations), (
        "제약을 무시한 최적안이 나왔는데 조용하다"
    )


def test_갱신이_더_싸지면_잡힌다(scenario, outcome, rs, monkeypatch):
    """갱신 시나리오는 매도 가능일을 뒤로 밀기만 하므로 지금 구현에서는 이 위반이
    **나올 수 없다.** 그래서 검사기 자체가 동작하는지를 확인한다.

    이 불변식은 현재 버그를 잡는 것이 아니라, 나중에 `assume_renewal`이 세액 계산까지
    건드리게 됐을 때의 회귀를 막는다. 그 사실을 숨기지 않고 여기 적는다.
    """
    real = sw.optimize

    def fake(*args, **kwargs):
        w = real(*args, **kwargs)
        if kwargs.get("assume_renewal"):
            cheap = min(w.points, key=lambda p: p.transfer_tax)
            return SellWindow(
                points=(sw.WindowPoint(on=cheap.on, transfer_tax=1),),
                constraints=w.constraints, property_label=w.property_label,
            )
        return w

    monkeypatch.setattr(sw, "optimize", fake)
    monkeypatch.setattr("sim.invariants.optimize", fake)
    violations = list(_sell_window_contract(scenario, outcome, rs))
    assert any(v.rule == "sell_window_renewal_not_cheaper" for v in violations)
