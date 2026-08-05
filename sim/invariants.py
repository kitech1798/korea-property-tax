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
        _property_tax_credit_bounded,
        _burden_cap_respected,
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
    gaps: set[str] = set()
    for ob in o.observations:
        gaps.update(ob.formula_gaps)
    if gaps:
        yield Violation(
            rule="formula_without_substitution",
            severity="warn",
            detail_ko=(
                f"산식만 있고 대입값이 없는 단계 {len(gaps)}개: {', '.join(sorted(gaps)[:6])}. "
                "사용자가 손으로 검산할 수 없다."
            ),
            evidence={"steps": sorted(gaps)},
        )


# -- 7b. 재산세 공제는 낸 재산세를 넘지 못한다 -------------------------------


def _property_tax_credit_bounded(s: Scenario, o: Outcome, rs: RuleSet) -> Iterable[Violation]:
    """종부세 재산세공제가 **실제로 부담한 재산세**를 넘으면 이중공제다.

    종부세법 §9③은 '주택분 재산세로 부과된 세액'을 공제한다고 정한다. 부과되지도
    않은 재산세를 빼면 국가가 없는 세금을 돌려주는 셈이 된다. 지분 안분·특례주택
    안분이 겹치는 자리라 실수가 나기 쉽다.
    """
    for ob in o.observations:
        if ob.property_tax <= 0:
            continue
        if ob.property_tax_credit > ob.property_tax:
            yield Violation(
                rule="property_tax_credit_bounded",
                severity="block",
                detail_ko=(
                    f"{ob.year}년 {ob.track}: 재산세 공제 {ob.property_tax_credit:,}원이 "
                    f"실제 재산세 {ob.property_tax:,}원을 넘는다(종부세법 §9③)."
                ),
                evidence={"year": ob.year, "track": ob.track,
                          "credit": ob.property_tax_credit, "property_tax": ob.property_tax},
            )


# -- 7c. 세부담상한이 실제로 걸리는가 ----------------------------------------


def _burden_cap_respected(s: Scenario, o: Outcome, rs: RuleSet) -> Iterable[Violation]:
    """직전연도 총세액을 알려줬으면 결과가 **상한을 넘을 수 없다.**

    종부세법 §10 — 직전연도 총세액상당액의 150%(개인)를 초과하는 부분은 없는 것으로
    본다. 시중 계산기가 '미반영'이라고 면책 문구로 적어 둔 바로 그 항목이라,
    여기가 무너지면 우리도 같은 자리에서 무너진 것이다.

    상한율은 룰셋에서 읽는다 — 코드에 박으면 개편안이 바꿀 때 검사가 거짓이 된다.
    """
    if s.prior_year_total_tax is None or s.prior_year_total_tax <= 0:
        return
    from realestate_tax.domain.models import PersonType, assessment_date

    # ★ 단일세율 법인은 세부담상한을 **적용받지 않는다**(종부세법 §10 괄호, §9②3호 법인).
    #   납세자 유형을 안 보고 검사하면 정상 동작을 위반으로 신고한다 —
    #   실제로 multi-house-05가 그렇게 잡혔다. 검사기가 법을 모르면 소음만 만든다.
    #   공익법인등(corporation_progressive)은 §9②3호가 아니므로 상한을 받는다.
    subject = s.case.find_person(s.subject)
    if subject.type is PersonType.CORPORATION:
        return
    taxpayer = "individual"

    for ob in o.observations:
        res = rs.resolve_or_none(
            "jongbuse.house.burden_cap",
            on=assessment_date(ob.year),
            track=_TRACKS[ob.track],
            taxpayer=taxpayer,
        )
        if res is None or res.block.value is None:
            continue
        if res.block.payload.get("applicable") is False:
            continue
        cap_rate = float(res.block.as_fraction()) if hasattr(res.block, "as_fraction") else None
        if not cap_rate:
            continue
        ceiling = int(s.prior_year_total_tax * cap_rate)
        # ★ 상한의 **기준**은 재산세+종부세 합계지만, 깎이는 것은 **종부세뿐**이다.
        #   §10은 "주택분 종합부동산세액 … 초과하는 세액에 대해서는 없는 것으로 본다"고
        #   정한다. 재산세는 지방세법 §122의 별도 상한을 따르므로 종부세법으로 깎을 수 없다.
        #
        #   그래서 합계가 상한을 넘는 것 자체는 위반이 아니다(재산세만으로 넘길 수 있다).
        #   검사할 것은 **종부세가 남은 여유분을 넘지 않는가**이다. 이 검사가 잡는 실패:
        #   상한율을 잘못 뽑음, 세액공제 전 금액에 상한을 걺, 재산세를 빼지 않음, 음수 허용.
        allowed = max(0, ceiling - ob.property_tax)
        if ob.net_tax > allowed + 1:
            yield Violation(
                rule="burden_cap_respected",
                severity="block",
                detail_ko=(
                    f"{ob.year}년 {ob.track}: 결정세액 {ob.net_tax:,}원이 세부담상한 여유분 "
                    f"{allowed:,}원(직전연도 {s.prior_year_total_tax:,} × {cap_rate} "
                    f"− 재산세 {ob.property_tax:,})을 넘는다."
                ),
                evidence={"year": ob.year, "track": ob.track, "property_tax": ob.property_tax,
                          "net": ob.net_tax, "allowed": allowed,
                          "ceiling": ceiling, "cap_rate": cap_rate},
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

    ★ 무엇에 단조성을 걸 것인가가 이 검사의 전부다 — 처음엔 **종부세 단독**에 걸었다가
      멀쩡한 엔진을 두 번 위반으로 신고했다(2026-08-05 실측에서 정정).

      종부세법 §10은 `재산세 + 종부세 ≤ 직전연도 총세액상당액 × 상한율`을 걸고,
      초과분에서 깎이는 것은 **종부세뿐**이다(재산세는 지방세법 §122 별도 상한 소관).
      그래서 공시가격이 오르면 재산세가 오르고, 상한 여유분이 줄어 **종부세는 정상적으로
      내려간다.** 조문이 그렇게 시킨 것이지 버그가 아니다.

      농특세도 빼야 한다. 농특세는 종부세액 × 20%(농특세법 §5①8호)이므로 상한이
      걸리는 순간 함께 줄어, 상한 경계에서 총액이 살짝 꺼질 수 있다. 이 역시 정상이다.

      법이 실제로 보장하는 단조량은 **재산세 + 종부세 본세**다. 그것만 검사한다.
    """
    for ob in o.observations[:2]:
        base_case = st.project_case(s.case, ob.year, growth=s.growth)
        raised = _map_prices(base_case, lambda v: int(v * 1.10))
        after = recompute(s, rs, year=ob.year, track=ob.track, case=raised)
        before = ob.property_tax + ob.net_tax
        after_total = after.property_tax_total.as_int() + after.net_tax.as_int()
        if after_total < before:
            yield Violation(
                rule="monotonic_in_price",
                severity="block",
                detail_ko=(
                    f"{ob.year}년 {ob.track}: 공시가격을 10% 올렸더니 총세액상당액"
                    f"(재산세+종부세 본세)이 {before:,}원 → {after_total:,}원으로 **줄었다**. "
                    f"재산세 {ob.property_tax:,}→{after.property_tax_total.as_int():,}, "
                    f"본세 {ob.net_tax:,}→{after.net_tax.as_int():,}"
                ),
                evidence={"year": ob.year, "track": ob.track,
                          "before": before, "after": after_total},
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
    """부부공동명의 1주택자 특례가 **진술대로 반영**되고, 유리하면 **알려지는가**.

    ★ 처음엔 "엔진이 알아서 유리한 쪽을 고른다"로 검사했는데 그건 법을 잘못 읽은 것이다
      (2026-08-05 실측에서 정정). 종부세법 §10의2는 "신청한 경우"에만 특례를 준다 —
      9월 16~30일 관할세무서장에게 신청서를 내야 한다. 신청하지도 않은 사람에게
      특례 세액을 보여주면 **실제로 낼 금액보다 적은 숫자**를 알려주는 것이다.

      그래서 검사는 둘로 갈린다.
        ① 신청을 진술했으면 그대로 반영돼야 한다.
        ② 신청하지 않았고 신청이 유리하면 **행동 가능한 안내**가 떠야 한다.
      ②가 없으면 사용자는 매년 수백만원을 모르고 흘려보낸다.
    """
    from realestate_tax.domain.models import ElectionKind

    declared = s.case.election(s.subject, ElectionKind.JOINT_SPOUSE_SPECIAL)
    person = s.case.find_person(s.subject)
    spouse_declared = (
        s.case.election(person.spouse_id, ElectionKind.JOINT_SPOUSE_SPECIAL)
        if person.spouse_id
        else None
    )

    for ob in o.observations[:2]:
        elected = recompute(s, rs, year=ob.year, track=ob.track, joint_spouse_election=True).total.as_int()
        plain = recompute(s, rs, year=ob.year, track=ob.track, joint_spouse_election=False).total.as_int()
        if elected == plain:
            continue  # 요건 미충족 등으로 갈리지 않으면 볼 것이 없다

        # ① 진술한 신청이 반영됐는가
        mine = declared is not None and declared.designated_taxpayer in (None, s.subject)
        theirs = spouse_declared is not None and spouse_declared.designated_taxpayer == s.subject
        if (mine or theirs) and ob.jongbuse != elected:
            yield Violation(
                rule="election_honored",
                severity="block",
                detail_ko=(
                    f"{ob.year}년 {ob.track}: 부부공동명의 특례 신청을 사건에 진술했는데 "
                    f"결과가 미신청({ob.jongbuse:,}원)으로 계산됐다. 신청 시 {elected:,}원."
                ),
                evidence={"year": ob.year, "track": ob.track,
                          "elected": elected, "picked": ob.jongbuse},
            )
            continue

        # ② 신청이 유리한데 안내가 없는가
        #    이미 신청한 사람(옵션·진술)에게 "신청하세요"를 띄우라고 요구하면 안 된다.
        if declared is None and not s.joint_spouse_election and elected < plain:
            told = any(a.startswith("joint_spouse_special:") for a in ob.alternatives)
            if not told:
                yield Violation(
                    rule="joint_spouse_advertised",
                    severity="block",
                    detail_ko=(
                        f"{ob.year}년 {ob.track}: 특례를 신청하면 {plain - elected:,}원 "
                        f"싼데({elected:,} vs {plain:,}) 화면에 안내가 없다. "
                        "사용자는 신청할 수 있다는 사실 자체를 모른다."
                    ),
                    evidence={"year": ob.year, "track": ob.track,
                              "elected": elected, "not_elected": plain},
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
