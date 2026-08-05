"""전략 엔진 — "그래서 어떻게 해야 하나"에 계산으로 답한다.

세액을 알려주는 것만으로는 상담이 아니다. 사용자가 실제로 묻는 것은
"내가 뭘 바꾸면 얼마나 달라지나"이고, 그건 조건을 바꿔 **다시 계산해봐야만** 안다.

이 모듈의 규칙 세 가지.

  ① 절감액은 추정하지 않는다. 대안 시나리오를 엔진으로 완전히 재계산한 차액만 쓴다.
  ② 요건과 부작용을 함께 말한다. "부부공동명의로 바꾸세요"만 하고 증여세를 빼놓으면
     그건 조언이 아니라 함정이다.
  ③ 개편안 기반 전략에는 "국회 미통과"가 따라붙는다. 확정된 법이 아니다.

2026 개편안의 무게중심이 '거주'로 옮겨갔으므로 절세 레버도 대부분 거기 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Sequence

from ..domain.certainty import Certainty, InputQuality, LegalStatus
from ..domain.models import (
    PersonId,
    PriceFact,
    ResidenceSpell,
    TaxCase,
    TaxYear,
    Won,
)
from ..rules.resolver import RuleSet
from ..rules.schema import Track
from .jongbuse import (
    JongbuseOptions,
    JongbuseResult,
    compare_joint_spouse_election,
    compute_jongbuse,
)
from .transfer_tax import TransferEvent, compute_transfer_tax
from .trace import format_manwon


@dataclass(frozen=True, slots=True)
class Strategy:
    """대안 하나. 절감액은 전부 재계산으로 얻은 실측값이다."""

    key: str
    label_ko: str
    what_to_do_ko: str
    basis_ko: str
    """근거 조문. 근거 없는 조언은 내지 않는다."""

    baseline: Won
    alternative: Won
    years: tuple[TaxYear, ...]
    requirements_ko: tuple[str, ...] = ()
    """이걸 하려면 갖춰야 하는 요건."""
    caveats_ko: tuple[str, ...] = ()
    """부작용·놓치기 쉬운 비용. 비워두면 안 된다."""
    certainty: Certainty = Certainty()

    @property
    def saving(self) -> Won:
        """양수면 절감, 음수면 오히려 손해."""
        return self.baseline - self.alternative

    @property
    def is_beneficial(self) -> bool:
        return self.saving > 0

    def summary_ko(self) -> str:
        span = f"{self.years[0]}~{self.years[-1]}년" if len(self.years) > 1 else f"{self.years[0]}년"
        direction = "절감" if self.saving > 0 else "증가"
        return (
            f"[{span}] {self.label_ko} — {format_manwon(abs(self.saving))} {direction}"
        )


@dataclass(frozen=True, slots=True)
class YearPoint:
    year: TaxYear
    track: Track
    property_tax: Won
    jongbuse: Won

    @property
    def total(self) -> Won:
        return self.property_tax + self.jongbuse


@dataclass(frozen=True, slots=True)
class Consultation:
    """상담 결과 한 벌."""

    person_id: PersonId
    timeline: tuple[YearPoint, ...]
    strategies: tuple[Strategy, ...]
    notes_ko: tuple[str, ...]

    @property
    def beneficial(self) -> tuple[Strategy, ...]:
        """실제로 이득인 것만, 절감액 큰 순."""
        return tuple(
            sorted(
                (s for s in self.strategies if s.is_beneficial),
                key=lambda s: s.saving,
                reverse=True,
            )
        )

    def total_over(self, track: Track) -> Won:
        return sum(p.total for p in self.timeline if p.track is track)


# --------------------------------------------------------------------------
# 연도 투영
# --------------------------------------------------------------------------


def project_case(
    case: TaxCase, year: TaxYear, *, growth: float = 0.0
) -> TaxCase:
    """같은 사건을 다른 연도로 옮긴다.

    공시가격은 알 수 없는 미래값이므로 상승률 시나리오를 명시적으로 받는다.
    기본값 0%는 정부 문답자료의 '공시가격 변동 없음' 가정과 같다.
    추정한 값에는 반드시 ESTIMATED 라벨이 붙어 화면에 '추정치'로 표시된다.
    """
    if year == case.year:
        return case

    span = year - case.year
    factor = (1.0 + growth) ** span

    properties = []
    for prop in case.properties:
        base = prop.price_for(case.year)
        if base is None or prop.price_for(year) is not None:
            properties.append(prop)
            continue
        projected = PriceFact(
            year=year,
            value=int(base.value * factor),
            # 상승률 0%를 골라도 미래값을 가정한 것이므로 추정치다.
            # 여기서 base.quality를 물려주면 사용자 입력값이 미래에도 확실한 것처럼 보인다.
            quality=InputQuality.ESTIMATED,
            note=f"{case.year}년 공시가격에서 연 {growth:.1%} 가정으로 투영",
        )
        properties.append(
            replace(prop, published_prices=prop.published_prices + (projected,))
        )

    return replace(case, year=year, properties=tuple(properties))


def build_timeline(
    case: TaxCase,
    person_id: PersonId,
    ruleset: RuleSet,
    *,
    years: Sequence[TaxYear] = (2026, 2027, 2028, 2029),
    options: JongbuseOptions | None = None,
    growth: float = 0.0,
) -> tuple[YearPoint, ...]:
    """연도별 보유세 추이.

    이 서비스의 실제 화면은 "올해 얼마"가 아니라 "4년간 어떻게 변하나"다.
    개편안이 2026~2029 단계 시행이라 한 해만 보면 판단을 그르친다.
    2026년은 개편 전이므로 현행법, 2027년 이후는 두 트랙을 모두 낸다.
    """
    options = options or JongbuseOptions()
    points: list[YearPoint] = []

    for year in years:
        projected = project_case(case, year, growth=growth)
        tracks = (Track.CURRENT,) if year <= 2026 else (Track.CURRENT, Track.REFORM)
        for track in tracks:
            r = compute_jongbuse(projected, person_id, ruleset, track=track, options=options)
            points.append(
                YearPoint(
                    year=year,
                    track=track,
                    property_tax=r.property_tax_total.as_int(),
                    jongbuse=r.total.as_int(),
                )
            )
    return tuple(points)


# --------------------------------------------------------------------------
# 전략 생성
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SellTimingPoint:
    """특정 해에 팔았을 때의 총비용."""

    year: TaxYear
    track: Track
    transfer_tax: Won
    """양도세 + 개인지방소득세."""
    holding_tax_paid: Won
    """그해까지 낸 보유세 누적(매도 연도 포함)."""

    @property
    def total_cost(self) -> Won:
        return self.transfer_tax + self.holding_tax_paid


@dataclass(frozen=True, slots=True)
class SellTiming:
    """언제 파는 것이 유리한가.

    이 질문에 답하려면 두 세목을 같이 봐야 한다. 개편안은 종부세를 올리면서
    양도세 중과를 '27~'28 한시 완화했다 — **버틸수록 보유세는 늘고, 늦게 팔수록
    양도세는 는다.** 어느 쪽이 큰지는 계산해야만 안다.
    """

    points: tuple[SellTimingPoint, ...]
    property_label: str = ""

    @property
    def best(self) -> SellTimingPoint | None:
        return min(self.points, key=lambda p: p.total_cost) if self.points else None

    @property
    def worst(self) -> SellTimingPoint | None:
        return max(self.points, key=lambda p: p.total_cost) if self.points else None

    @property
    def spread(self) -> Won:
        """가장 싼 해와 가장 비싼 해의 차이. 이 값이 크면 시점이 결정적이다."""
        if not self.points:
            return 0
        return self.worst.total_cost - self.best.total_cost


def sell_timing(
    case: TaxCase,
    person_id: PersonId,
    event: TransferEvent,
    ruleset: RuleSet,
    *,
    years: Sequence[TaxYear] = (2027, 2028, 2029),
    track: Track = Track.REFORM,
    options: JongbuseOptions | None = None,
    growth: float = 0.0,
) -> SellTiming:
    """매도 연도별 총비용(양도세 + 그때까지의 보유세)을 계산한다.

    `event`는 양도가액·취득가액 등을 담은 틀이고, 양도일만 해마다 바꿔 다시 계산한다.
    양도가액은 해마다 같다고 가정한다 — 시세 전망까지 섞으면 세제 효과가 묻힌다.
    """
    options = options or JongbuseOptions()
    points: list[SellTimingPoint] = []
    holding_cumulative = 0

    for year in sorted(years):
        projected = project_case(case, year, growth=growth)
        holding = compute_jongbuse(
            projected, person_id, ruleset, track=track, options=options
        )
        holding_cumulative += holding.holding_tax_total

        sale = replace(event, transfer_date=date(year, event.transfer_date.month, 1))
        transfer = compute_transfer_tax(projected, sale, ruleset, track=track)

        points.append(
            SellTimingPoint(
                year=year,
                track=track,
                transfer_tax=transfer.total.as_int(),
                holding_tax_paid=holding_cumulative,
            )
        )

    label = case.find_property(event.property_id).display_name or str(event.property_id)
    return SellTiming(tuple(points), label)


def consult(
    case: TaxCase,
    person_id: PersonId,
    ruleset: RuleSet,
    *,
    options: JongbuseOptions | None = None,
    years: Sequence[TaxYear] = (2026, 2027, 2028, 2029),
    growth: float = 0.0,
) -> Consultation:
    """상담 한 벌 — 타임라인 + 절세 대안."""
    options = options or JongbuseOptions()
    timeline = build_timeline(
        case, person_id, ruleset, years=years, options=options, growth=growth
    )

    strategies: list[Strategy] = []
    strategies.extend(_joint_spouse_strategy(case, person_id, ruleset, options, years))
    strategies.extend(_move_in_strategy(case, person_id, ruleset, options, years, growth))

    notes = _notes(case, person_id, ruleset, options, years, growth)
    return Consultation(person_id, timeline, tuple(strategies), notes)


def _sum_reform_years(
    case: TaxCase,
    person_id: PersonId,
    ruleset: RuleSet,
    options: JongbuseOptions,
    years: Sequence[TaxYear],
    growth: float,
) -> Won:
    """개편안이 시행되는 해(2027~)의 보유세 합계."""
    total = 0
    for year in years:
        if year <= 2026:
            continue
        projected = project_case(case, year, growth=growth)
        r = compute_jongbuse(projected, person_id, ruleset, track=Track.REFORM, options=options)
        total += r.property_tax_total.as_int() + r.total.as_int()
    return total


def _joint_spouse_strategy(
    case: TaxCase,
    person_id: PersonId,
    ruleset: RuleSet,
    options: JongbuseOptions,
    years: Sequence[TaxYear],
) -> list[Strategy]:
    """부부공동명의 1주택자 특례를 신청할지.

    신청이 늘 유리한 것은 아니다. 세액공제가 붙지 않는 연령대라면 각자 공제받는
    쪽이 이긴다. 그래서 추측하지 않고 완전 계산 2회를 돌린 차액만 쓴다.
    """
    # ★ `if years`는 리스트가 비었는지만 본다. 필터를 걸고 나서 비는 경우를 못 막는다
    #   (SIM-05, 2026-08-05). 사건 연도가 룰셋 사정 범위를 넘으면(예: 2030년) 타임라인이
    #   2026~2029라 조건을 만족하는 해가 하나도 없고, min()이 빈 제너레이터에서 터진다.
    #   "범위 밖입니다"라고 말해야 할 자리에서 상담 화면 전체가 죽었다.
    future = [y for y in years if y >= case.year]
    base_year = min(future) if future else case.year
    projected = project_case(case, base_year)
    cmp = compare_joint_spouse_election(projected, person_id, ruleset, options=options)
    if not cmp.eligible:
        return []

    return [
        Strategy(
            key="joint_spouse_election",
            label_ko="부부공동명의 1주택자 특례 신청",
            what_to_do_ko=(
                "매년 9월 16일~30일에 관할세무서장에게 공동명의 1주택자 신청서를 제출합니다. "
                "한 번 신청하면 변동이 없는 한 다음 해부터는 다시 내지 않아도 됩니다."
            ),
            basis_ko="종합부동산세법 §10의2, 시행령 §5의2",
            baseline=cmp.not_elected_total,
            alternative=cmp.elected_total,
            years=(base_year,),
            requirements_ko=(
                "세대원 중 1명과 그 배우자만이 1주택을 공동 소유할 것",
                "부부 모두 소득세법상 거주자일 것",
                "납세의무자로 지정할 1인을 부부 합의로 정할 것",
            ),
            caveats_ko=(
                "지정된 1인의 연령·보유기간으로 세액공제가 계산됩니다 — "
                "고령이거나 오래 보유한 쪽을 지정하는 것이 대개 유리합니다.",
                "신청하면 그 1인에게 세금이 몰립니다. 납부 주체가 바뀌는 점을 확인하세요.",
            ),
        )
    ]


def _move_in_strategy(
    case: TaxCase,
    person_id: PersonId,
    ruleset: RuleSet,
    options: JongbuseOptions,
    years: Sequence[TaxYear],
    growth: float,
) -> list[Strategy]:
    """실거주 전환.

    2026 개편안의 핵심이다. 기본공제가 거주 14억 / 비거주 9억으로 갈리고,
    보유공제가 거주공제로 전환되므로 '살지 않는 집'의 세부담이 크게 오른다.
    이미 거주 중이면 제안하지 않는다.

    ★ 가드가 **옵션만** 봐서 한 번도 발동하지 않았다(2026-08-05).
      거주 여부는 `ResidenceSpell`에서 도출되므로 `options.resides_in_main_house`는
      보통 None이다. 그래서 이미 살고 있는 사람에게도 "실거주 전환"이 제시됐고,
      기준선이 이미 거주를 반영한 탓에 **"손해 557만원"**이라는 헛소리가 나왔다.

      모델에 있는 사실을 엔진이 안 읽는 같은 실수의 다섯 번째다
      (거주 여부 → 거주기간 → 취득일 → Election → 여기).
      **판정 결과를 값으로 받아** 확인한다 — 계산을 한 번 더 돌리는 값보다
      틀린 조언을 띄우는 비용이 크다.
    """
    reform_years = tuple(y for y in years if y > 2026)
    if not reform_years:
        return []

    if options.resides_in_main_house is None:
        probe = compute_jongbuse(
            project_case(case, reform_years[0], growth=growth),
            person_id,
            ruleset,
            track=Track.REFORM,
            options=options,
        )
        if probe.resides:
            return []
    elif options.resides_in_main_house:
        return []

    baseline = _sum_reform_years(case, person_id, ruleset, options, years, growth)

    # 대안: 실거주로 전환하고, 전환 시점부터 거주기간이 쌓인다고 본다.
    moved = replace(
        options,
        resides_in_main_house=True,
        residence_years=options.residence_years or 0,
    )
    alternative = _sum_reform_years(case, person_id, ruleset, moved, years, growth)

    return [
        Strategy(
            key="move_in",
            label_ko="해당 주택으로 실거주 전환",
            what_to_do_ko=(
                "과세기준일(6월 1일) 이전에 전입해 실제로 거주하면, 2027년부터 "
                "기본공제가 9억원에서 14억원으로 올라갑니다. 거주기간이 쌓이면 "
                "거주공제(5년 20% / 10년 40% / 15년 50%)도 함께 붙습니다."
            ),
            basis_ko="종합부동산세법 §8① (개정안), §9⑧ (개정안)",
            baseline=baseline,
            alternative=alternative,
            years=reform_years,
            requirements_ko=(
                "과세기준일 현재 해당 주택에 실제 거주할 것",
                "거주공제는 거주기간 5년 이상부터 붙습니다",
            ),
            caveats_ko=(
                "개정안 기준입니다. 국회 통과 전이므로 확정된 제도가 아닙니다.",
                "취학·근무상 형편·질병 등 부득이한 사유로 비거주한 기간은 "
                "최장 3년까지 거주기간으로 인정받을 수 있습니다(개정안 §9⑩ 신설). "
                "해당된다면 이사하지 않고도 공제를 받을 여지가 있습니다.",
                "실거주 전환에는 기존 임대차 정리, 이사 비용 등 세금 외 비용이 따릅니다.",
            ),
            certainty=Certainty(legal=LegalStatus.BILL_PENDING),
        )
    ]


def _notes(
    case: TaxCase,
    person_id: PersonId,
    ruleset: RuleSet,
    options: JongbuseOptions,
    years: Sequence[TaxYear],
    growth: float,
) -> tuple[str, ...]:
    """계산으로는 답할 수 없지만 반드시 알려야 하는 사실.

    세액 추정을 붙이지 않는다. 양도소득세는 아직 이 엔진의 범위 밖이고,
    숫자 없이 사실만 전하는 것이 없는 숫자를 지어내는 것보다 낫다.
    """
    notes: list[str] = []

    r = compute_jongbuse(case, person_id, ruleset, track=Track.CURRENT, options=options)
    for step_id, reason in r.trace.unknowns():
        notes.append(f"입력이 더 있으면 정확해집니다 ({step_id}): {reason}")

    for key, source in r.trace.certainty_concerns():
        notes.append(f"확실성 유의 — {key} (발생 지점: {source})")

    house_count = len(
        {
            o.property_id
            for o in case.ownerships_of(person_id)
            if case.find_property(o.property_id).is_house
        }
    )
    if house_count >= 2:
        notes.append(
            "다주택자의 조정대상지역 주택 양도세 중과가 2027~2028년 한시 완화됩니다"
            "(2주택 +20%p → 2027년 +5%p / 2028년 +10%p, 3주택 이상 +30%p → +10%p / +15%p, "
            "2029년 원상복귀 · 소득세법 §104⑦ 개정안). "
            "양도가액과 취득가액을 알려주시면 `sell_timing()`으로 매도 연도별 "
            "총비용(양도세 + 그때까지의 보유세)을 계산해 드립니다."
        )

    if options.prior_year_total_tax is None:
        notes.append(
            "작년 재산세·종합부동산세 고지서 금액을 알려주시면 세부담 상한 적용 여부까지 계산합니다."
        )

    return tuple(notes)
