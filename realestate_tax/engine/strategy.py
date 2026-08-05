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
from fractions import Fraction
from typing import Sequence

from ..domain.certainty import Certainty, InputQuality, LegalStatus
from ..domain.models import (
    Election,
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

    upfront_cost: Won = 0
    """실행 즉시 나가는 돈(증여세·취득세 등). 절감은 해마다 돌아오는데 이건 한 번에 나간다."""
    annual_saving: Won = 0
    """연 평균 절감액. 회수기간 계산의 분모."""

    @property
    def payback_years(self) -> float | None:
        """몇 년이면 본전인가. 일회성 비용이 없으면 None(즉시 이득).

        ★ 이게 없으면 조언이 거꾸로 나간다(2026-08-05). 증여는 비용이 **한 번에**
          나가고 절감은 **해마다** 돌아온다. 비교 창이 3년뿐이면 거의 모든 증여가
          '손해'로 찍히는데, 실제로는 5~6년이면 본전을 넘는 경우가 흔하다.
          창의 길이가 결론을 만들면 그건 계산이 아니라 착시다.
        """
        if self.upfront_cost <= 0:
            return None
        if self.annual_saving <= 0:
            return float("inf")
        return self.upfront_cost / self.annual_saving

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
    strategies.extend(_spouse_gift_strategy(case, person_id, ruleset, options, years, growth))
    strategies.extend(_rental_exclusion_strategy(case, person_id, ruleset, options, years, growth))

    # 효과가 정확히 0인 대안은 말할 것이 없다. 남겨두면 "실거주 전환 — 0원 손해"처럼
    # 뜻 없는 줄이 화면을 채우고, 진짜 경고(음수)까지 같이 무시당한다.
    # 음수는 남긴다 — "하면 손해"는 알아야 할 정보다.
    strategies = [s for s in strategies if s.saving != 0]

    notes = _notes(case, person_id, ruleset, options, years, growth)
    return Consultation(person_id, timeline, tuple(strategies), notes)


def _rental_exclusion_strategy(
    case: TaxCase,
    person_id: PersonId,
    ruleset: RuleSet,
    options: JongbuseOptions,
    years: Sequence[TaxYear],
    growth: float,
) -> list[Strategy]:
    """합산배제 임대주택 신고(종부세법 §8②, 시행령 §3).

    ★ 다른 특례와 성격이 다르다. 주택 수만 빼주는 게 아니라 **과세표준 합산에서
      제외**된다 — 그 주택에는 종부세가 아예 붙지 않는다. 그래서 절감폭이 크다.

    엔진은 이미 판정하고 있었다. 없던 것은 **"신고하세요"라는 말**이다.
    실측에서 신고 하나로 1,050만원이 80만원이 됐는데 화면은 아무 말도 안 했다.
    계산이 맞는 것과 사용자가 행동할 수 있는 것은 다르다.
    """
    from ..domain.models import ElectionKind

    reform_years = tuple(y for y in years if y > 2026)
    if not reform_years:
        return []

    # 등록임대주택인데 **신고하지 않은** 것이 있는가.
    candidates = [
        o.property_id
        for o in case.ownerships_of(person_id)
        if case.find_property(o.property_id).rental is not None
    ]
    if not candidates:
        return []
    if case.election(person_id, ElectionKind.RENTAL_EXCLUSION) is not None:
        return []  # 이미 신고했다

    baseline = _sum_reform_years(case, person_id, ruleset, options, years, growth)
    declared = replace(
        case,
        elections=case.elections
        + (Election(person_id=person_id, kind=ElectionKind.RENTAL_EXCLUSION),),
    )
    alternative = _sum_reform_years(declared, person_id, ruleset, options, years, growth)

    names = ", ".join(
        case.find_property(pid).display_name or str(pid) for pid in candidates
    )
    return [
        Strategy(
            key="rental_exclusion",
            label_ko=f"합산배제 임대주택 신고 ({names})",
            what_to_do_ko=(
                "매년 **9월 16일~30일**에 관할세무서장에게 합산배제 신고서를 냅니다. "
                "합산배제되면 그 주택은 과세표준에서 아예 빠져 종합부동산세가 붙지 않고, "
                "1세대1주택자 판정의 주택 수에서도 제외됩니다. "
                "한 번 신고하면 변동이 없는 한 다음 해부터는 다시 내지 않아도 됩니다."
            ),
            basis_ko="종합부동산세법 §8②, 시행령 §3",
            baseline=baseline,
            alternative=alternative,
            years=reform_years,
            requirements_ko=(
                "「민간임대주택특별법」에 따라 임대사업자로 등록하고 사업자등록도 할 것",
                "임대개시일 당시 공시가격이 요건 이하일 것(유형·지역별로 다릅니다)",
                "임대료 증액을 **5% 이내**로 지킬 것",
                "의무임대기간을 채울 것(장기일반민간임대 10년)",
            ),
            caveats_ko=(
                "⚠️ **의무임대기간을 못 채우면 그동안 면제받은 세액이 추징**됩니다"
                "(종부세법 §17③). 임대료 5% 제한을 어겨도 같습니다.",
                "조정대상지역의 **매입임대 아파트**는 개편안에서 단계적으로 배제됩니다 — "
                "등록 시기와 유형에 따라 달라지므로 등록증으로 확인하세요.",
                "이 계산은 임대유형·가액·면적 요건을 **충족한다고 보고** 낸 것입니다. "
                "요건 충족 여부는 임대사업자 등록증으로만 확인됩니다.",
            ),
            certainty=Certainty(legal=LegalStatus.BILL_PENDING),
        )
    ]


def _household_reform_total(
    case: TaxCase,
    people: Sequence[PersonId],
    ruleset: RuleSet,
    options: JongbuseOptions,
    years: Sequence[TaxYear],
    growth: float,
) -> Won:
    """세대 구성원 전원의 보유세 합계.

    ★ **이 함수가 없으면 조언이 거짓말이 된다.**
      지분을 배우자에게 넘기면 내 세금은 줄지만 배우자에게 세금이 생긴다.
      본인 것만 비교하면 넘길수록 이득으로 보이고, 그 화면을 믿고 증여하면
      세대 전체로는 손해를 볼 수 있다. 종부세는 인별 과세지만 **가계는 하나다.**
    """
    total = 0
    for year in years:
        if year <= 2026:
            continue  # 개편안 효과는 2027년부터다
        projected = project_case(case, year, growth=growth)
        for pid in people:
            total += compute_jongbuse(
                projected, pid, ruleset, track=Track.REFORM, options=options
            ).holding_tax_total
    return total


def _spouse_gift_strategy(
    case: TaxCase,
    person_id: PersonId,
    ruleset: RuleSet,
    options: JongbuseOptions,
    years: Sequence[TaxYear],
    growth: float,
) -> list[Strategy]:
    """배우자에게 지분 1/2을 증여해 인별 합산 공시가격을 나눈다.

    종합부동산세는 **인별 과세**다. 한 사람에게 몰린 공시가격을 둘로 나누면
    기본공제를 각자 받고 누진 구간도 낮아진다. 다주택자에게 특히 크다.

    다만 공짜가 아니다 — 증여세·취득세가 **즉시** 나가고, 절감은 해마다 조금씩
    돌아온다. 그래서 4년 누적 절감액에서 증여 비용을 빼고 나서야 이득인지 알 수 있다.
    `Strategy.alternative`에 비용을 더해 넣으므로 `saving`이 곧 **순이익**이다.
    """
    from .gift import carryover_years, compute_spouse_gift_cost

    person = case.find_person(person_id)
    if person.spouse_id is None or person.is_corporation:
        return []
    try:
        spouse = case.find_person(person.spouse_id)
    except KeyError:
        return []

    reform_years = tuple(y for y in years if y > 2026)
    if not reform_years:
        return []

    # 본인이 **단독으로** 가진 주택 중 가장 비싼 것을 나눈다.
    # 이미 공동명의인 것을 더 쪼개는 것은 효과가 작고 §10의2 특례를 깨뜨릴 수 있다.
    solo = [
        o for o in case.ownerships_of(person_id)
        if o.share == 1 and case.find_property(o.property_id).is_house
    ]
    if not solo:
        return []
    target = max(
        solo,
        key=lambda o: (case.find_property(o.property_id).price_for(case.year) or PriceFact(case.year, 0)).value,
    )
    prop = case.find_property(target.property_id)
    fact = prop.price_for(case.year)
    if fact is None or fact.value <= 0:
        return []

    # ★ **증여 규모를 공제 한도에 맞춘다**(2026-08-05).
    #   지분 절반을 통째로 넘기면 증여세가 터져 거의 모든 사례가 '손해'로 찍혔다.
    #   실무가 6억에 맞춰 나누는 이유가 그것이다 — 공제 안에서는 증여세가 0원이고
    #   종부세 분산 효과는 그대로 얻는다. 한도를 넘겨서까지 나눌 이유가 없다.
    ded_cap = int(ruleset.resolve(
        "gift.spouse.deduction", on=case.assessment_date, track=Track.REFORM
    ).block.value)
    gift_value = min(fact.value // 2, ded_cap)
    if gift_value <= 0:
        return []
    share = Fraction(gift_value, fact.value)
    people = (person_id, spouse.id)

    baseline = _household_reform_total(case, people, ruleset, options, reform_years, growth)

    moved = replace(
        case,
        ownerships=tuple(o for o in case.ownerships if o is not target)
        + (
            replace(target, share=1 - share),
            replace(target, person_id=spouse.id, share=share),
        ),
    )
    after_tax = _household_reform_total(moved, people, ruleset, options, reform_years, growth)

    cost = compute_spouse_gift_cost(
        gift_value, ruleset, on=case.assessment_date, track=Track.REFORM
    )
    carry = carryover_years(ruleset, on=case.assessment_date)
    annual = (baseline - after_tax) // max(1, len(reform_years))

    return [
        Strategy(
            key="spouse_gift",
            label_ko=(
                f"배우자에게 '{prop.display_name or prop.id}' 지분 {share} 증여"
                f" (공시 {gift_value:,}원어치)"
            ),
            what_to_do_ko=(
                f"공시가격 기준 {gift_value:,}원어치 지분({share})을 배우자에게 증여하면 "
                "종합부동산세가 두 사람에게 나뉘어 각자 기본공제를 받고 누진 구간도 낮아집니다. "
                "증여일이 속하는 달의 말일부터 3개월 이내에 신고해야 신고세액공제 3%를 받습니다."
            ),
            basis_ko="종합부동산세법 §7①(인별 과세), 상속세 및 증여세법 §53①1호, 지방세법 §11①2",
            baseline=baseline,
            # 절감만 세면 거짓이 된다. 즉시 나가는 비용을 대안 쪽에 더해
            # `saving`이 곧 순이익이 되게 한다.
            alternative=after_tax + cost.total,
            years=reform_years,
            upfront_cost=cost.total,
            annual_saving=annual,
            requirements_ko=(
                "법률상 배우자일 것 — 사실혼은 증여재산공제 대상이 아닙니다",
                "증여 후 소유권이전등기를 마칠 것",
                f"과세기준일(6월 1일) 이전에 등기를 마쳐야 그해 {reform_years[0]}년분부터 반영됩니다",
            ),
            caveats_ko=(
                f"증여세 {cost.gift_tax:,}원 + 취득세 {cost.acquisition_tax:,}원 = "
                f"**{cost.total:,}원이 즉시 나갑니다.** 절감은 해마다 나눠 돌아옵니다"
                + (
                    f" — 연 {annual:,}원씩이라 **약 {cost.total / annual:.1f}년이면 본전**입니다."
                    if annual > 0
                    else ". 이 사건에서는 절감이 없거나 오히려 늘어 **회수되지 않습니다.**"
                ),
                f"⚠️ **이월과세** — 증여 후 {carry}년 이내에 그 주택을 팔면 양도세 취득가액이 "
                "증여 당시 가격이 아니라 **원래 취득가**로 돌아갑니다(소득세법 §97의2①). "
                f"{carry}년 안에 매도 계획이 있으면 손익이 뒤집힐 수 있습니다.",
                "배우자 증여재산공제 6억원은 **10년간 합산**입니다. 이미 증여한 이력이 "
                "있으면 공제가 줄어 증여세가 늘어납니다(상증세법 §53① 후단).",
                "조정대상지역의 일정가액 이상 주택은 증여 취득세가 12%로 중과됩니다"
                "(지방세법 §13의2②). 해당 여부는 확인이 필요합니다.",
                "⚠️ **1주택자는 대개 손해입니다.** 단독명의 1세대1주택자가 누리던 "
                "연령·거주 세액공제(최대 80%)를 잃기 때문입니다. 부부공동명의가 되면 "
                "§10의2 특례를 따로 신청해야 하고, 그 유불리는 또 달라집니다 — "
                "이 계산은 특례 미신청 기준입니다.",
            ),
            certainty=Certainty(legal=LegalStatus.BILL_PENDING),
        )
    ]


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

    # 지방 세컨드홈 — 엔진이 지역을 판정하지 못하므로 **존재를 알리는 것**까지 한다.
    # 인구감소지역 목록은 행안부 고시라 기계가 읽을 데이터가 없고, 개편안이 넓히는
    # 지역은 '27.2월 시행령 개정에서 정해진다(아직 없는 목록이다).
    # 판정은 못 해도 "이런 특례가 있다"를 모르면 사용자는 영영 못 받는다.
    second = ruleset.resolve_or_none(
        "jongbuse.special.second_home", on=case.assessment_date, track=Track.REFORM
    )
    if second is not None:
        caps = second.block.payload.get("caps") or {}
        cheap = [
            case.find_property(o.property_id)
            for o in case.ownerships_of(person_id)
            if case.find_property(o.property_id).is_house
            and (case.find_property(o.property_id).price_for(case.year) or None) is not None
            and case.find_property(o.property_id).price_for(case.year).value <= max(caps.values())
        ]
        if cheap and len(case.ownerships_of(person_id)) >= 2:
            names = ", ".join(p.display_name or str(p.id) for p in cheap)
            notes.append(
                f"지방 세컨드홈 특례를 확인해보세요 — {names}이(가) **비수도권(광역시 제외)**의 "
                "인구감소지역·인구감소관심지역에 있다면 주택 수에서 빠져 1세대1주택자가 될 수 있습니다"
                "(조세특례제한법 §71의2). 개편안은 대상 지역을 비수도권 전 지역으로 넓히고 "
                "가액 기준을 올리며 적용기한을 2029년까지 연장합니다. "
                "다만 **지역 목록은 행정안전부 고시**라 이 엔진이 판정하지 못하고, "
                "확대되는 지역은 2027년 2월 시행령 개정에서 정해집니다 — 세무서 확인이 필요합니다."
            )

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
