"""시나리오 실행 — 한 상황을 엔진 전체에 통과시키고 **구조화된 결과**를 낸다.

설계 원칙 하나: **단계마다 예외를 격리한다.**

  종부세가 터졌다고 재산세·양도세·상담까지 못 보면, 한 회차에 버그를 하나씩밖에
  못 잡는다. 60건을 돌리는 이유는 한 번에 많이 보기 위해서다. 그래서 각 단계를
  try로 감싸고 실패를 값으로 담는다 — 실패도 관측 대상이지 실행 중단 사유가 아니다.

원칙 둘: **여기서 판정하지 않는다.**

  runner는 "무슨 일이 일어났는가"만 기록한다. 그게 잘못됐는지는 invariants가
  기계적으로, 그리고 리뷰 에이전트가 법령으로 따진다. 관측과 판단을 섞으면
  관측이 판단에 오염된다.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from realestate_tax.domain.certainty import Certainty
from realestate_tax.domain.models import PersonId, TaxCase, Won
from realestate_tax.engine import jongbuse as jb
from realestate_tax.engine import strategy as st
from realestate_tax.engine import transfer_tax as tt
from realestate_tax.engine.determination import household_house_count
from realestate_tax.engine.property_tax import PropertyTaxOptions, compute_property_tax
from realestate_tax.engine.special_houses import assess
from realestate_tax.engine.trace import TraceNode, Value
from realestate_tax.rules.resolver import RuleSet
from realestate_tax.rules.schema import Track

from .spec import Scenario

_TRACKS = {"current": Track.CURRENT, "reform": Track.REFORM}


# --------------------------------------------------------------------------
# 결과 구조
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Failure:
    """실행 중 터진 것. 예외는 **가장 값싼 버그**다 — 논쟁의 여지가 없다."""

    stage: str
    kind: str
    """예외 클래스명. MissingRule/AmbiguousRule이면 룰셋 구멍이다."""
    message: str
    traceback_tail: str
    """마지막 프레임들만. 리포트가 스택으로 뒤덮이면 아무도 안 읽는다."""

    def key(self) -> tuple[str, str, str]:
        """같은 버그를 묶는 키. 60건이 같은 원인으로 터지면 1건으로 보고해야 한다."""
        return (self.stage, self.kind, self.message[:120])


@dataclass(frozen=True, slots=True)
class Observation:
    """계산이 성공한 한 조합의 관측치."""

    year: int
    track: str
    property_tax: Won
    jongbuse: Won
    holding_total: Won
    taxable_base: Won
    gross_tax: Won
    tax_credit: Won
    house_count_household: int
    house_count_personal: int
    one_house: bool
    resides: bool
    certainty: tuple[str, ...]
    unknowns: tuple[str, ...]
    """trace 전체에서 값을 모른 채 지나간 자리 — `step_id:사유`."""
    undecidable_steps: tuple[str, ...]
    alternatives: tuple[str, ...]
    trace_steps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TransferObservation:
    kind: str
    """transfer | burden_gift"""
    gain: Won
    taxable_gain: Won
    long_term_deduction: Won
    income_tax: Won
    total: Won
    effective_rate: float
    """total ÷ gain. 100%를 넘으면 그 자체로 사고다."""
    certainty: tuple[str, ...]
    undecidable_steps: tuple[str, ...]
    trace_steps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Outcome:
    scenario_id: str
    label_ko: str
    origin: str
    intent_ko: str
    expectation_ko: str
    tags: tuple[str, ...]
    observations: tuple[Observation, ...] = ()
    transfers: tuple[TransferObservation, ...] = ()
    strategies: tuple[Mapping[str, Any], ...] = ()
    failures: tuple[Failure, ...] = ()
    violations: tuple["Violation", ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures and not self.violations

    def observation(self, year: int, track: str) -> Observation | None:
        for o in self.observations:
            if o.year == year and o.track == track:
                return o
        return None


@dataclass(frozen=True, slots=True)
class Violation:
    """불변식 위반. invariants가 채우고 runner는 자리만 마련한다."""

    rule: str
    severity: str
    """block | warn — block은 '법적으로 불가능한 결과'다."""
    detail_ko: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def key(self) -> tuple[str, str]:
        return (self.rule, self.detail_ko[:120])


# --------------------------------------------------------------------------
# 실행
# --------------------------------------------------------------------------


def _capture(stage: str, fn: Callable[[], Any]) -> tuple[Any, Failure | None]:
    try:
        return fn(), None
    except Exception as exc:  # noqa: BLE001 — 관측이 목적이므로 전부 잡는다
        tb = traceback.format_exc().strip().splitlines()
        return None, Failure(
            stage=stage,
            kind=type(exc).__name__,
            message=str(exc)[:400],
            traceback_tail="\n".join(tb[-6:]),
        )


def _scan_trace(node: TraceNode) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """trace 한 그루에서 관측 항목을 뽑는다.

    `unknowns`를 **step_id와 함께** 담는 이유: "어딘가 모르는 값이 있다"는
    고칠 수 없는 정보다. 어느 단계인지 알아야 룰셋 구멍인지 입력 부족인지 갈린다.
    """
    unknowns: list[str] = []
    undecidable: list[str] = []
    steps: list[str] = []
    for n in node.walk():
        steps.append(n.step_id)
        if n.output.unknown is not None:
            unknowns.append(f"{n.step_id}:{n.output.unknown.value}")
        c = n.output.certainty
        if c.determination.name == "UNDECIDABLE":
            undecidable.append(n.step_id)
    alts = tuple(f"{a.key}:{a.reason_ko}"[:160] for a in node.all_alternatives())
    return tuple(unknowns), tuple(undecidable), alts, tuple(steps)


def _won(value: Value) -> Won:
    """Value → 원. 모르는 값은 0이 아니라 **0으로 읽되 unknowns에 기록된 것**이다.

    여기서 예외를 던지면 관측이 끊긴다. 대신 unknowns가 같은 자리를 잡아주므로
    불변식 검사가 '모르는데 숫자가 나왔다'를 따로 잡을 수 있다.
    """
    try:
        return value.as_int()
    except Exception:  # noqa: BLE001
        return 0


def _certainty_labels(c: Certainty) -> tuple[str, ...]:
    return tuple(c.labels_ko())


def run(scenario: Scenario, ruleset: RuleSet) -> Outcome:
    """시나리오 한 건을 전 구간에 통과시킨다."""
    obs: list[Observation] = []
    failures: list[Failure] = []
    notes: list[str] = []

    years = scenario.years or (scenario.case.year,)
    subject = scenario.subject

    for year in years:
        case, fail = _capture(
            f"project({year})",
            lambda y=year: st.project_case(scenario.case, y, growth=scenario.growth),
        )
        if fail:
            failures.append(fail)
            continue

        for track_name in scenario.tracks:
            track = _TRACKS[track_name]
            options = jb.JongbuseOptions(
                resides_in_main_house=scenario.resides_in_main_house,
                prior_year_total_tax=scenario.prior_year_total_tax,
                joint_spouse_election=bool(scenario.joint_spouse_election),
            )
            result, fail = _capture(
                f"jongbuse({year},{track_name})",
                lambda c=case, t=track, o=options: jb.compute_jongbuse(
                    c, subject, ruleset, track=t, options=o
                ),
            )
            if fail:
                failures.append(fail)
                continue

            # 주택 수는 **두 개**다. 세대 기준(1세대1주택 판정)과 본인 기준(세율표).
            # 하나만 관측하면 둘이 갈리는 버그를 영영 못 본다(종부령 §4의3③).
            household, hh_fail = _capture(
                f"house_count({year},{track_name})",
                lambda c=case: household_house_count(c, subject),
            )
            if hh_fail:
                failures.append(hh_fail)
            special, sp_fail = _capture(
                f"special_assess({year},{track_name})",
                lambda c=case, t=track: assess(c, subject, ruleset, track=t),
            )
            if sp_fail:
                failures.append(sp_fail)

            unknowns, undecidable, alts, steps = _scan_trace(result.trace)
            obs.append(
                Observation(
                    year=year,
                    track=track_name,
                    property_tax=result.property_tax_total.as_int(),
                    jongbuse=result.total.as_int(),
                    holding_total=result.holding_tax_total,
                    taxable_base=_won(result.taxable_base),
                    gross_tax=_won(result.gross_tax),
                    tax_credit=_won(result.tax_credit),
                    house_count_household=special.count if special else (household.count if household else -1),
                    house_count_personal=special.personal_count if special else -1,
                    one_house=bool(special.is_one_house) if special else False,
                    resides=result.resides,
                    certainty=_certainty_labels(result.trace.certainty),
                    unknowns=unknowns,
                    undecidable_steps=undecidable,
                    alternatives=alts,
                    trace_steps=steps,
                )
            )

    # -- 양도세 / 부담부증여 ------------------------------------------------
    transfers: list[TransferObservation] = []
    for kind, payload in (
        ("transfer", scenario.transfer.event),
        ("burden_gift", scenario.transfer.burden_gift),
    ):
        if payload is None:
            continue
        for track_name in scenario.tracks:
            track = _TRACKS[track_name]
            if kind == "transfer":
                fn = lambda c=scenario.case, p=payload, t=track: tt.compute_transfer_tax(  # noqa: E731
                    c, p, ruleset, track=t
                )
            else:
                fn = lambda c=scenario.case, p=payload, t=track: tt.compute_burden_gift(  # noqa: E731
                    c, p, ruleset, track=t
                )
            res, fail = _capture(f"{kind}({track_name})", fn)
            if fail:
                failures.append(fail)
                continue
            _, undecidable, _, steps = _scan_trace(res.trace)
            gain = _won(res.gain)
            transfers.append(
                TransferObservation(
                    kind=f"{kind}:{track_name}",
                    gain=gain,
                    taxable_gain=_won(res.taxable_gain),
                    long_term_deduction=_won(res.long_term_deduction),
                    income_tax=_won(res.income_tax),
                    total=_won(res.total),
                    effective_rate=(_won(res.total) / gain) if gain > 0 else 0.0,
                    certainty=_certainty_labels(res.trace.certainty),
                    undecidable_steps=undecidable,
                    trace_steps=steps,
                )
            )

    # -- 상담(전략) ---------------------------------------------------------
    strategies: list[Mapping[str, Any]] = []
    consultation, fail = _capture(
        "consult",
        lambda: st.consult(scenario.case, subject, ruleset, growth=scenario.growth),
    )
    if fail:
        failures.append(fail)
    elif consultation is not None:
        for s in consultation.strategies:
            strategies.append(
                {
                    "key": s.key,
                    "label": s.label_ko,
                    "saving": s.saving,
                    "baseline": s.baseline,
                    "alternative": s.alternative,
                    "basis": s.basis_ko,
                    "caveats": list(s.caveats_ko),
                    "requirements": list(s.requirements_ko),
                    "certainty": list(_certainty_labels(s.certainty)),
                }
            )
        notes.extend(consultation.notes_ko)

    return Outcome(
        scenario_id=scenario.id,
        label_ko=scenario.label_ko,
        origin=scenario.origin,
        intent_ko=scenario.intent_ko,
        expectation_ko=scenario.expectation_ko,
        tags=scenario.tags,
        observations=tuple(obs),
        transfers=tuple(transfers),
        strategies=tuple(strategies),
        failures=tuple(failures),
        notes=tuple(notes),
    )


def recompute(
    scenario: Scenario,
    ruleset: RuleSet,
    *,
    year: int,
    track: str,
    case: TaxCase | None = None,
    joint_spouse_election: bool | None = None,
) -> jb.JongbuseResult:
    """불변식 검사가 '조건 하나만 바꿔' 다시 돌릴 때 쓰는 창구.

    ★ 불변식 검사가 자기만의 계산 경로를 만들면 안 된다. 그러면 두 경로가 갈라져
      "검사는 통과했는데 앱은 틀린" 상태가 생긴다. 재계산도 **같은 문**으로 들어간다.
    """
    target = case if case is not None else st.project_case(scenario.case, year, growth=scenario.growth)
    options = jb.JongbuseOptions(
        resides_in_main_house=scenario.resides_in_main_house,
        prior_year_total_tax=scenario.prior_year_total_tax,
        joint_spouse_election=(
            bool(scenario.joint_spouse_election)
            if joint_spouse_election is None
            else joint_spouse_election
        ),
    )
    return jb.compute_jongbuse(
        target, scenario.subject, ruleset, track=_TRACKS[track], options=options
    )


__all__ = [
    "Failure",
    "Observation",
    "Outcome",
    "TransferObservation",
    "Violation",
    "recompute",
    "run",
]
