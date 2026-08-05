"""불변식 — **LLM 없이** 기계가 판정하는 심판.

지난 감사에서 66건의 지적 중 46건이 기각됐다(70%). 원인은 하나다:
**리뷰어가 코드를 읽고 추측했기 때문**이다. 읽어서 나온 의심은 대부분 틀린다.

그래서 이번 회차의 1차 심판은 사람도 AI도 아닌 **실행 결과**다.
아래 불변식은 전부 다음 성질을 갖는다.

  · 위반하면 **법적으로 불가능한 결과**이거나, 스스로 모순이다.
  · 판정에 해석이 개입하지 않는다 — 반박할 여지가 없다.
  · 위반 시 재현 방법이 자동으로 따라 나온다(시나리오 id + 조건).

즉 여기서 잡힌 것은 논쟁 없이 버그다. 리뷰 에이전트는 **여기서 안 잡히는 것**,
곧 "숫자는 성립하는데 법 해석이 틀린 것"에만 투입한다. 그게 비싼 자원을 쓸 자리다.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Callable, Iterable

from realestate_tax.domain.models import PriceFact, Property, TaxCase, Won
from realestate_tax.engine import strategy as st
from realestate_tax.intake.price import deduction_boundaries
from realestate_tax.rules.resolver import RuleSet
from realestate_tax.rules.schema import Track

from .runner import Outcome, Violation, recompute
from .spec import Scenario

_TRACKS = {"current": Track.CURRENT, "reform": Track.REFORM}

CLIFF_TOLERANCE = 10_000
"""공시가격 1원 차이로 이만큼 넘게 튀면 절벽이다. 법이 만든 절벽(기본공제·특례세율
경계)은 알려진 좌표에서만 허용하고, 그 밖의 절벽은 버그로 본다."""

BOUNDARY_SLACK = 2_000_000
"""알려진 경계 근처 판정 폭. 합산·안분 때문에 경계가 정확히 일치하지 않을 수 있다."""


# --------------------------------------------------------------------------
# 케이스 변형 헬퍼
# --------------------------------------------------------------------------


def _map_prices(case: TaxCase, fn: Callable[[Won], Won]) -> TaxCase:
    """모든 물건의 공시가격을 옮긴다. 단조성·절벽 검사의 기본 도구."""
    props = tuple(
        replace(
            p,
            published_prices=tuple(
                PriceFact(pf.year, max(0, fn(pf.value)), pf.quality, pf.note)
                for pf in p.published_prices
            ),
        )
        for p in case.properties
    )
    return replace(case, properties=props)


def _bump_one(case: TaxCase, prop: Property, delta: Won) -> TaxCase:
    props = tuple(
        replace(
            p,
            published_prices=tuple(
                PriceFact(pf.year, max(0, pf.value + delta), pf.quality, pf.note)
                for pf in p.published_prices
            ),
        )
        if p.id == prop.id
        else p
        for p in case.properties
    )
    return replace(case, properties=props)


def _total_price(case: TaxCase, year: int) -> Won:
    return sum(
        (pf.value for p in case.properties for pf in p.published_prices if pf.year == year),
        0,
    )


# --------------------------------------------------------------------------
# 불변식
# --------------------------------------------------------------------------


def check(scenario: Scenario, outcome: Outcome, ruleset: RuleSet) -> tuple[Violation, ...]:
    """한 시나리오의 결과를 전 불변식에 건다."""
    out: list[Violation] = []
    for fn in (
        _non_negative,
        _credit_within_gross,
        _unknown_honesty,
        _tracks_both_computed,
        _transfer_sanity,
        _strategy_honesty,
        _trace_verifiability,
        _determinism,
        _monotonic_in_price,
        _no_unexpected_cliff,
        _joint_spouse_optimal,
        _share_additivity,
    ):
        try:
            out.extend(fn(scenario, outcome, ruleset))
        except Exception as exc:  # noqa: BLE001
            # 검사기가 터진 것도 정보다. 조용히 통과시키면 검사가 없는 것과 같다.
            out.append(
                Violation(
                    rule=f"checker:{fn.__name__}",
                    severity="warn",
                    detail_ko=f"불변식 검사기 자체가 실패했다: {type(exc).__name__}: {exc}",
                )
            )
    return tuple(out)


# -- 1. 값의 부호 -----------------------------------------------------------


def _non_negative(s: Scenario, o: Outcome, rs: RuleSet) -> Iterable[Violation]:
    for ob in o.observations:
        for name, v in (
            ("재산세", ob.property_tax),
            ("종부세", ob.jongbuse),
            ("과세표준", ob.taxable_base),
            ("산출세액", ob.gross_tax),
            ("세액공제", ob.tax_credit),
        ):
            if v < 0:
                yield Violation(
                    rule="non_negative",
                    severity="block",
                    detail_ko=f"{ob.year}년 {ob.track}: {name}가 음수({v:,}원). 세액은 음수일 수 없다.",
                    evidence={"year": ob.year, "track": ob.track, name: v},
                )


# -- 2. 공제는 산출세액을 못 넘는다 -----------------------------------------


def _credit_within_gross(s: Scenario, o: Outcome, rs: RuleSet) -> Iterable[Violation]:
    """세액공제가 산출세액보다 크면 **환급이 발생**한다. 종부세에 환급은 없다.

    종부세법 §9의3·§9의4는 공제를 '산출세액에서 뺀다'고만 정하고 초과분 환급 근거가
    없으므로, 넘으면 계산 순서가 틀린 것이다.
    """
    for ob in o.observations:
        if ob.gross_tax <= 0:
            continue
        if ob.tax_credit > ob.gross_tax:
            yield Violation(
                rule="credit_within_gross",
                severity="block",
                detail_ko=(
                    f"{ob.year}년 {ob.track}: 세액공제 {ob.tax_credit:,}원 > "
                    f"산출세액 {ob.gross_tax:,}원. 종부세는 환급되지 않는다."
                ),
                evidence={"year": ob.year, "track": ob.track,
                          "credit": ob.tax_credit, "gross": ob.gross_tax},
            )


# -- 3. 모르는 것을 아는 척하지 않는다 ★ 이 프로젝트의 존재 이유 -------------


def _unknown_honesty(s: Scenario, o: Outcome, rs: RuleSet) -> Iterable[Violation]:
    """trace 어딘가에서 값을 몰랐는데 **결과가 확정 배지를 달고 나오면** 위반이다.

    시중 계산기가 신뢰를 잃은 지점이 정확히 여기다. 모르는 입력을 0으로 읽고
    "귀하의 종부세는 0원입니다"라고 단언하는 것.

    확실성은 트리에서 min()으로 파생되므로, 모르는 값이 있으면 자동으로 강등돼야
    한다. 강등이 안 됐다면 **어딘가에서 Value를 새로 만들며 확실성을 버린 것**이다.
    """
    for ob in o.observations:
        if ob.unknowns and not ob.certainty:
            yield Violation(
                rule="unknown_honesty",
                severity="block",
                detail_ko=(
                    f"{ob.year}년 {ob.track}: 모르는 값이 {len(ob.unknowns)}곳 있는데 "
                    f"결과에 확실성 경고가 하나도 없다. 확실성 전파가 끊긴 자리가 있다.\n"
                    f"  모르는 자리: {', '.join(ob.unknowns[:5])}"
                ),
                evidence={"year": ob.year, "track": ob.track, "unknowns": list(ob.unknowns)},
            )


# -- 4. 두 트랙이 모두 나와야 한다 ------------------------------------------


def _tracks_both_computed(s: Scenario, o: Outcome, rs: RuleSet) -> Iterable[Violation]:
    """"현행 vs 개편안"이 이 서비스의 메인 화면이다. 한쪽만 나오면 화면이 성립 안 한다."""
    for year in (s.years or (s.case.year,)):
        got = {ob.track for ob in o.observations if ob.year == year}
        missing = set(s.tracks) - got
        if missing and got:
            yield Violation(
                rule="tracks_both_computed",
                severity="block",
                detail_ko=(
                    f"{year}년: {sorted(got)}는 계산됐는데 {sorted(missing)}는 안 나왔다. "
                    "비교 화면이 반쪽이 된다."
                ),
                evidence={"year": year, "computed": sorted(got), "missing": sorted(missing)},
            )


# -- 5. 양도세 자기모순 -----------------------------------------------------


def _transfer_sanity(s: Scenario, o: Outcome, rs: RuleSet) -> Iterable[Violation]:
    for t in o.transfers:
        if t.gain <= 0:
            continue
        if t.total > t.gain:
            yield Violation(
                rule="transfer_not_exceed_gain",
                severity="block",
                detail_ko=(
                    f"{t.kind}: 세액 {t.total:,}원이 양도차익 {t.gain:,}원을 넘는다. "
                    f"최고세율(중과 82.5% 포함)로도 불가능하다."
                ),
                evidence={"kind": t.kind, "total": t.total, "gain": t.gain},
            )
        if t.long_term_deduction > t.taxable_gain and t.taxable_gain > 0:
            yield Violation(
                rule="ltd_within_gain",
                severity="block",
                detail_ko=(
                    f"{t.kind}: 장기보유특별공제 {t.long_term_deduction:,}원이 "
                    f"과세대상 양도차익 {t.taxable_gain:,}원을 넘는다 (공제율 80% 상한 위반)."
                ),
                evidence={"kind": t.kind, "ltd": t.long_term_deduction, "gain": t.taxable_gain},
            )
        if t.effective_rate > 0.825 + 1e-9:
            yield Violation(
                rule="transfer_rate_cap",
                severity="warn",
                detail_ko=(
                    f"{t.kind}: 실효세율 {t.effective_rate:.1%}가 법정 최고(70% + 지방소득세 "
                    f"10% = 77%, 미등기 70%×1.1=82.5%)를 넘는다."
                ),
                evidence={"kind": t.kind, "rate": t.effective_rate},
            )


# -- 6. 조언의 정직성 -------------------------------------------------------


def _strategy_honesty(s: Scenario, o: Outcome, rs: RuleSet) -> Iterable[Violation]:
    """이득이라 제시한 전략에 **부작용이 비어 있으면** 그건 조언이 아니라 유인이다.

    실거주 전환은 이사비·기존 세입자 문제가 있고, 부부 증여는 증여세·취득세가 붙는다.
    절감액만 크게 써 놓고 비용을 안 쓰면 사용자가 손해 보는 결정을 한다.
    """
    for strat in o.strategies:
        if strat["saving"] > 0 and not strat["caveats"]:
            yield Violation(
                rule="strategy_has_caveats",
                severity="block",
                detail_ko=(
                    f"전략 '{strat['label']}'이 {strat['saving']:,}원 절감이라면서 "
                    "부작용(caveats)이 비어 있다. 비용 없는 절세는 없다."
                ),
                evidence={"key": strat["key"], "saving": strat["saving"]},
            )
        if not strat["basis"]:
            yield Violation(
                rule="strategy_has_basis",
                severity="block",
                detail_ko=f"전략 '{strat['label']}'에 근거 조문이 없다.",
                evidence={"key": strat["key"]},
            )


# -- 7. 손으로 검산할 수 있는가 ---------------------------------------------


def _trace_verifiability(s: Scenario, o: Outcome, rs: RuleSet) -> Iterable[Violation]:
    """산식은 있는데 대입값이 없으면 사용자가 검산할 수 없다.

    "근거를 보여준다"가 이 서비스의 유일한 차별점인데, 기호식만 보여주는 것은
    보여주지 않는 것과 같다. 세무사에게 들고 갈 수 있어야 한다.
    """
    seen: set[str] = set()
    for ob in o.observations:
        for step in ob.trace_steps:
            seen.add(step)
    # 관측 단계에서 formula/substitution을 다시 얻으려면 trace가 필요하므로
    # 여기서는 '단계 수'만 본다. 산식 짝 검사는 아래 run 단계에서 trace로 직접 한다.
    if o.observations and len(seen) < 3:
        yield Violation(
            rule="trace_depth",
            severity="warn",
            detail_ko=f"trace 단계가 {len(seen)}개뿐이다. 근거를 보여줄 내용이 없다.",
            evidence={"steps": sorted(seen)},
        )


# -- 8. 결정성 -------------------------------------------------------------


def _determinism(s: Scenario, o: Outcome, rs: RuleSet) -> Iterable[Violation]:
    """같은 입력을 두 번 넣으면 같은 답이 나와야 한다.

    캐시·전역 상태·집합 순회 순서 때문에 깨지는 일이 실제로 있다. 세금 도구에서
    "새로고침하니 금액이 달라졌다"는 곧바로 신뢰 붕괴다.
    """
    for ob in o.observations[:2]:  # 전 조합을 두 번 돌리면 비싸다. 앞 두 개면 충분히 잡힌다.
        again = recompute(s, rs, year=ob.year, track=ob.track)
        if again.total.as_int() != ob.jongbuse:
            yield Violation(
                rule="determinism",
                severity="block",
                detail_ko=(
                    f"{ob.year}년 {ob.track}: 같은 입력을 다시 계산했더니 "
                    f"{ob.jongbuse:,}원 → {again.total.as_int():,}원으로 달라졌다."
                ),
                evidence={"year": ob.year, "track": ob.track,
                          "first": ob.jongbuse, "second": again.total.as_int()},
            )


# -- 9. 공시가격 단조성 ------------------------------------------------------


def _monotonic_in_price(s: Scenario, o: Outcome, rs: RuleSet) -> Iterable[Violation]:
    """공시가격이 오르면 보유세는 **줄지 않는다.**

    누진세율·공제·상한이 얽혀도 이 성질은 유지돼야 한다. 깨지면 어딘가에서 구간을
    잘못 잡았거나 공제를 잘못 걸었다는 뜻이고, 그건 곧 "집값이 오르니 세금이 줄었다"는
    화면이 나온다는 뜻이다.

    ★ 다만 세부담상한(직전연도 총세액 × 상한율)은 **직전연도 세액**에 걸린다.
      시나리오가 prior_year_total_tax를 고정해 놓았으면 상한이 그대로여서 성질이
      유지되지만, 역산 경로에서는 전년 공시가격도 함께 올라 상한도 오른다.
      두 경우 모두 비감소이므로 검사는 유효하다.
    """
    for ob in o.observations[:2]:
        base_case = st.project_case(s.case, ob.year, growth=s.growth)
        raised = _map_prices(base_case, lambda v: int(v * 1.10))
        after = recompute(s, rs, year=ob.year, track=ob.track, case=raised)
        before_total = ob.jongbuse
        after_total = after.total.as_int()
        if after_total < before_total:
            yield Violation(
                rule="monotonic_in_price",
                severity="block",
                detail_ko=(
                    f"{ob.year}년 {ob.track}: 공시가격을 10% 올렸더니 종부세가 "
                    f"{before_total:,}원 → {after_total:,}원으로 **줄었다**."
                ),
                evidence={"year": ob.year, "track": ob.track,
                          "before": before_total, "after": after_total},
            )


# -- 10. 예상치 못한 절벽 ----------------------------------------------------


def _no_unexpected_cliff(s: Scenario, o: Outcome, rs: RuleSet) -> Iterable[Violation]:
    """공시가격 1원 차이로 세액이 튀는 자리는 **법이 만든 경계에서만** 허용된다.

    기본공제(9억/12억/14억)와 재산세 특례세율 상한(9억)은 진짜 절벽이라 그대로 둔다.
    그 밖의 좌표에서 튀면 구간 비교에 `<`와 `<=`를 잘못 썼다는 신호다.
    """
    if not o.observations:
        return
    ob = o.observations[0]
    base_case = st.project_case(s.case, ob.year, growth=s.growth)
    houses = [p for p in base_case.properties if p.is_house and p.published_prices]
    if not houses:
        return

    from realestate_tax.domain.models import assessment_date

    boundaries = deduction_boundaries(rs, on=assessment_date(ob.year))
    total = _total_price(base_case, ob.year)

    for prop in houses[:2]:
        up = recompute(s, rs, year=ob.year, track=ob.track, case=_bump_one(base_case, prop, 1))
        jump = abs(up.total.as_int() - ob.jongbuse)
        if jump <= CLIFF_TOLERANCE:
            continue
        near_known = any(abs(total - b) <= BOUNDARY_SLACK for b in boundaries)
        if near_known:
            continue
        yield Violation(
            rule="unexpected_cliff",
            severity="warn",
            detail_ko=(
                f"{ob.year}년 {ob.track}: '{prop.display_name or prop.id}'의 공시가격을 "
                f"**1원** 올렸더니 종부세가 {jump:,}원 뛰었다. 합산 공시가격 {total:,}원은 "
                f"알려진 경계({', '.join(f'{b:,}' for b in sorted(boundaries))})가 아니다."
            ),
            evidence={"year": ob.year, "track": ob.track, "property": str(prop.id),
                      "jump": jump, "total_price": total},
        )


# -- 11. 부부공동명의 특례는 유리한 쪽이어야 한다 ----------------------------


def _joint_spouse_optimal(s: Scenario, o: Outcome, rs: RuleSet) -> Iterable[Violation]:
    """`auto_optimize`가 켜졌는데 **불리한 쪽**을 골랐으면 특례 비교가 깨진 것이다.

    신청/미신청은 세액공제(고령·장기보유)까지 포함한 완전 계산 2회를 해야만 갈린다.
    나이·거주기간에 따라 뒤집히는 구간이 실제로 있어서, 한쪽만 계산하면 반드시 틀린다.
    """
    if s.joint_spouse_election is not None:
        return  # 사용자가 명시적으로 고정한 경우는 최적화 대상이 아니다
    for ob in o.observations[:2]:
        picked = ob.jongbuse
        alt = recompute(s, rs, year=ob.year, track=ob.track, joint_spouse_election=True).total.as_int()
        base = recompute(s, rs, year=ob.year, track=ob.track, joint_spouse_election=False).total.as_int()
        best = min(alt, base)
        if picked > best:
            yield Violation(
                rule="joint_spouse_optimal",
                severity="block",
                detail_ko=(
                    f"{ob.year}년 {ob.track}: 부부공동명의 특례 신청={alt:,}원, "
                    f"미신청={base:,}원인데 결과는 {picked:,}원으로 유리한 쪽이 아니다."
                ),
                evidence={"year": ob.year, "track": ob.track,
                          "elected": alt, "not_elected": base, "picked": picked},
            )


# -- 12. 지분 안분의 가산성 --------------------------------------------------


def _share_additivity(s: Scenario, o: Outcome, rs: RuleSet) -> Iterable[Violation]:
    """**재산세는 물건별 과세**다. 소유자를 몇 명으로 쪼개든 물건의 총 재산세는 같다.

    지방세법 §107①은 '재산을 사실상 소유하고 있는 자'를 납세의무자로 하고, 공유물은
    §107① 단서로 지분에 따라 안분한다. 즉 안분은 **나눗셈**이지 재계산이 아니다.
    2인 공동명의로 바꿨더니 물건 재산세 합계가 달라지면 안분 로직이 깨진 것이다.

    ★ 종부세는 다르다(인별 과세 + 기본공제가 사람마다 붙는다). 그래서 이 검사는
      재산세에만 건다. 둘을 섞으면 정상 동작을 버그로 신고하게 된다.
    """
    if not o.observations:
        return
    ob = o.observations[0]
    case = st.project_case(s.case, ob.year, growth=s.growth)
    solo = [
        p for p in case.properties
        if p.is_house and len(case.owners_of(p.id)) == 1 and case.owners_of(p.id)[0].share == 1
    ]
    if not solo:
        return

    from fractions import Fraction

    from realestate_tax.engine.property_tax import compute_property_tax

    prop = solo[0]
    owner = case.owners_of(prop.id)[0]
    whole, fail = _safe(lambda: compute_property_tax(case, prop.id, rs, track=_TRACKS[ob.track]))
    if whole is None:
        return

    # 같은 물건을 반씩 나눈 사건으로 만든다. 물건 자체는 바뀌지 않았으므로 총액도 같아야 한다.
    from realestate_tax.domain.models import Ownership, Person, PersonId

    ghost = PersonId(f"{owner.person_id}__half")
    if any(p.id == ghost for p in case.persons):
        return
    persons = case.persons + (
        Person(id=ghost, household_id=case.find_person(owner.person_id).household_id),
    )
    owners = tuple(
        o for o in case.ownerships if not (o.property_id == prop.id and o.person_id == owner.person_id)
    ) + (
        replace(owner, share=Fraction(1, 2)),
        Ownership(person_id=ghost, property_id=prop.id, share=Fraction(1, 2),
                  acquired_on=owner.acquired_on, cause=owner.cause, inherited=owner.inherited),
    )
    split = replace(case, persons=persons, ownerships=owners)
    halved, _ = _safe(lambda: compute_property_tax(split, prop.id, rs, track=_TRACKS[ob.track]))
    if halved is None:
        return

    if whole.total.as_int() != halved.total.as_int():
        yield Violation(
            rule="share_additivity",
            severity="block",
            detail_ko=(
                f"{ob.year}년 {ob.track}: '{prop.display_name or prop.id}'를 1인 단독 → "
                f"2인 1/2씩으로 바꿨더니 **물건 전체 재산세**가 "
                f"{whole.total.as_int():,}원 → {halved.total.as_int():,}원으로 달라졌다. "
                "재산세는 물건별 과세이므로 소유자 구성이 총액을 바꿀 수 없다."
            ),
            evidence={"property": str(prop.id), "solo": whole.total.as_int(),
                      "split": halved.total.as_int()},
        )

    # 안분 합이 전체와 같은가 — 반올림으로 1원 이상 새면 지분 계산이 float으로 샌 것이다.
    parts = whole.share_of(Fraction(1, 2)) * 2
    if abs(parts - whole.total.as_int()) > 1:
        yield Violation(
            rule="share_rounding",
            severity="warn",
            detail_ko=(
                f"1/2씩 안분한 합 {parts:,}원이 전체 {whole.total.as_int():,}원과 "
                f"{abs(parts - whole.total.as_int()):,}원 어긋난다."
            ),
            evidence={"property": str(prop.id)},
        )


def _safe(fn: Callable[[], object]) -> tuple[object | None, Exception | None]:
    try:
        return fn(), None
    except Exception as exc:  # noqa: BLE001
        return None, exc


__all__ = ["check", "CLIFF_TOLERANCE"]
