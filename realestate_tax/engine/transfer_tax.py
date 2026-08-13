"""양도소득세 (주택).

왜 보유세와 함께 있어야 하는가
    "팔까 버틸까"는 보유세만으로 답이 안 나온다. 2026 개편안이 종부세를 올리면서
    동시에 양도세 중과를 '27~'28 한시 완화한 것은 **매도 창구를 열어주는 설계**다.
    두 세목을 같은 사건 위에서 계산해야 판단이 선다.

계산 구조
    양도가액 − 취득가액 − 필요경비                  = 양도차익
    × (양도가액 − 12억) ÷ 양도가액                   = 과세대상 양도차익  (1세대1주택 고가주택)
    − 장기보유특별공제 (개정안: 장기거주 소득공제)     [한도 '28 20억 / '29 10억]
    − 양도소득 기본공제                              = 과세표준
    × 기본세율 (+ 다주택 조정지역 중과)               = 산출세액
    + 개인지방소득세 (산출세액 × 10%)                 = 총부담

정부 문답자료 p.37의 산출세액 6개 값을 재현하는 것이 이 모듈의 게이트다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from fractions import Fraction
from typing import Any, Mapping

from ..domain.certainty import Certainty, DeterminationQuality
from ..domain.models import (
    AcquisitionCause,
    PersonId,
    PropertyId,
    TaxCase,
    Won,
)
from ..rules.resolver import RuleSet
from ..rules.schema import Track
from . import periods
from .regions import UNKNOWN, YES, check_regulated
from .sangsaeng import SangsaengVerdict
from .sangsaeng import assess as sangsaeng_assess
from .sangsaeng import waives_residence
from .special_houses import assess
from .trace import (
    Alternative,
    BranchRecord,
    SubjectRef,
    SubjectType,
    TraceNode,
    UnknownReason,
    Value,
    derive_value,
    node,
)

T = "transfer"


@dataclass(frozen=True, slots=True)
class TransferEvent:
    """양도 한 건. 세법상 취득가액의 정본은 **매매계약서**이므로 사용자 입력을 받는다.

    실거래가 공개시스템 값은 부동산거래신고법상 신고가액의 공개본이지 세법상 정본이
    아니다(소득세법 §97①1은 '실지거래가액'을 요구한다). 자동으로 채우면 안 된다.
    """

    property_id: PropertyId
    person_id: PersonId
    transfer_date: date
    transfer_price: Won
    """양도가액."""
    acquisition_price: Won
    """취득가액 — 매매계약서상 실지거래가액."""
    acquisition_date: date | None = None
    necessary_expense: Won = 0
    """필요경비(취득세·중개보수·자본적지출 등)."""
    share: Fraction = Fraction(1)
    holding_years: int | None = None
    residence_years: int | None = None

    def __post_init__(self) -> None:
        if self.transfer_price < 0 or self.acquisition_price < 0:
            raise ValueError("양도가액·취득가액은 음수일 수 없다")
        if self.acquisition_date and self.acquisition_date > self.transfer_date:
            raise ValueError("취득일이 양도일보다 늦다")

    @property
    def year(self) -> int:
        return self.transfer_date.year


@dataclass(frozen=True, slots=True)
class TransferTaxResult:
    event: TransferEvent
    gain: Value
    """양도차익."""
    taxable_gain: Value
    """과세대상 양도차익(고가주택 안분 후)."""
    long_term_deduction: Value
    taxable_base: Value
    income_tax: Value
    """산출세액 — 정부 문답자료의 '산출세액'과 같은 정의."""
    local_income_tax: Value
    total: Value
    trace: TraceNode


@dataclass(frozen=True, slots=True)
class BurdenGift:
    """부담부증여 — 채무를 함께 넘기는 증여.

    ★ 한 사건이 두 세목으로 쪼개진다(소득세법 §88① 후단).
        "부담부증여 시 수증자가 부담하는 **채무액에 해당하는 부분은 양도로 보며**"

      채무액 부분 → **증여자**가 양도소득세
      나머지 부분 → **수증자**가 증여세

    ⚠️ 이 엔진은 **양도소득세만** 계산한다. 증여세는 상속세및증여세법 소관이라
       범위 밖이고, 증여세를 빼놓고 "이만큼이면 유리하다"고 말하면 함정이 된다.
       화면은 반드시 그 사실을 밝혀야 한다.

    안분 산식 (소득세법 시행령 §159①, 2026-08-05 조문 원문 확인):
        취득가액 = A × 채무액 ÷ 증여가액   A: 법 §97①1에 따른 가액
        양도가액 = A × 채무액 ÷ 증여가액   A: 상증세법 §60~66에 따라 평가한 가액

      ⚠️ 법제처 API는 이 계산식을 **이미지로** 준다(JSON·HTML 모두).
         XML(`type=XML`)로 받아야 텍스트가 나온다.
    """

    property_id: PropertyId
    person_id: PersonId
    gift_date: date
    appraised_value: Won
    """상증세법 §60~66에 따라 평가한 가액. 산식의 A."""
    gift_value: Won
    """증여가액. 산식의 C. 보통 평가액과 같지만 조문이 별개 항목으로 쓴다."""
    debt_assumed: Won
    """수증자가 인수하는 채무액. 산식의 B — 전세보증금·근저당 등."""
    acquisition_price: Won
    """증여자가 실제로 취득할 때 낸 금액(안분 전)."""
    necessary_expense: Won = 0
    holding_years: int | None = None
    residence_years: int | None = None

    def __post_init__(self) -> None:
        if self.gift_value <= 0:
            raise ValueError("증여가액은 0보다 커야 한다")
        if self.debt_assumed < 0:
            raise ValueError("채무액은 음수일 수 없다")
        if self.debt_assumed > self.gift_value:
            raise ValueError(
                f"채무액({self.debt_assumed:,})이 증여가액({self.gift_value:,})을 넘는다. "
                "채무가 재산보다 크면 부담부증여가 아니다."
            )

    @property
    def transfer_ratio(self) -> Fraction:
        """양도로 보는 비율 = 채무액 ÷ 증여가액."""
        return Fraction(self.debt_assumed, self.gift_value)

    @property
    def is_transfer(self) -> bool:
        """채무가 없으면 순수 증여라 양도소득세가 없다."""
        return self.debt_assumed > 0

    @property
    def gift_portion(self) -> Won:
        """증여세 대상 금액. **이 엔진은 계산하지 않는다** — 화면 안내용."""
        return max(0, self.gift_value - self.debt_assumed)

    def to_transfer_event(self) -> TransferEvent:
        """양도로 보는 부분만 떼어낸 양도 사건."""
        r = self.transfer_ratio
        return TransferEvent(
            property_id=self.property_id,
            person_id=self.person_id,
            transfer_date=self.gift_date,
            transfer_price=int(self.appraised_value * r),
            acquisition_price=int(self.acquisition_price * r),
            # 필요경비도 같은 비율로 안분한다. §159①은 취득가액·양도가액만 적지만,
            # 안분하지 않으면 양도로 보는 부분에 전체 경비를 떠넘기게 된다.
            necessary_expense=int(self.necessary_expense * r),
            holding_years=self.holding_years,
            residence_years=self.residence_years,
        )


def compute_burden_gift(
    case: TaxCase,
    gift: BurdenGift,
    ruleset: RuleSet,
    *,
    track: Track = Track.CURRENT,
) -> TransferTaxResult:
    """부담부증여의 **양도소득세**를 계산한다. 증여세는 계산하지 않는다.

    안분 사실을 감사추적 맨 앞에 남긴다 — 사용자가 "왜 양도가액이 증여가액보다
    작지?"에서 막히지 않으려면 쪼갠 근거가 보여야 한다.
    """
    prop = case.find_property(gift.property_id)
    subject = SubjectRef(
        SubjectType.PROPERTY, str(prop.id), prop.display_name or str(prop.id)
    )
    ratio = gift.transfer_ratio
    event = gift.to_transfer_event()

    split = node(
        "tr.01.burden_gift_split",
        "부담부증여 안분",
        Value.money(event.transfer_price, label="양도로 보는 가액"),
        subject=subject,
        formula="양도가액 = 평가액 × (채무액 ÷ 증여가액)",
        substitution=(
            f"{gift.appraised_value:,} × ({gift.debt_assumed:,} ÷ {gift.gift_value:,})"
            f" = {event.transfer_price:,}"
            f"  ·  취득가액 {gift.acquisition_price:,} × {ratio} = {event.acquisition_price:,}"
        ),
        branch=BranchRecord(
            condition_ko="양도로 보는 부분",
            taken=f"{float(ratio) * 100:.1f}%",
            detail_ko=f"채무 {gift.debt_assumed:,}원 / 증여가액 {gift.gift_value:,}원",
        ),
        note_ko=(
            "채무액에 해당하는 부분만 양도로 봅니다(소득세법 §88① 후단, 시행령 §159①). "
            f"나머지 {gift.gift_portion:,}원은 **수증자가 증여세**를 냅니다 — "
            "이 계산에는 증여세가 포함되어 있지 않습니다. "
            "부담부증여의 유불리는 두 세목을 합쳐야 판단할 수 있습니다."
        ),
    )

    result = compute_transfer_tax(case, event, ruleset, track=track)

    # ★ 해석이 갈리는 유일한 지점을 드러낸다.
    #   고가주택(12억) 판정을 **안분 후 양도가액**으로 하느냐 **주택 전체 평가액**으로
    #   하느냐에 따라 비과세가 통째로 뒤집힌다. 조문 문언은 안분 후를 가리킨다 —
    #   시행령 §160의 고가주택 안분식이 "양도가액"을 쓰고, §159①이 부담부증여의
    #   양도가액을 안분액으로 정의하기 때문이다.
    #   그러나 예규로 달리 볼 여지가 있고, 우리는 그 예규를 확인하지 못했다.
    #   **두 해석의 답이 갈리는 경우에만** 판정 불가로 표시한다 —
    #   답이 같은 지점에서까지 불확실을 뿌리면 나머지 경고까지 무시된다.
    limit = ruleset.resolve(
        f"{T}.one_house_exempt_limit", on=gift.gift_date, track=track
    ).block.as_int()
    diverges = gift.appraised_value > limit >= event.transfer_price

    trace = replace(
        result.trace,
        label_ko=f"부담부증여 양도소득세 — {prop.display_name or prop.id} ({gift.gift_date})",
        children=(split,) + result.trace.children,
    )
    if diverges:
        trace = replace(
            trace,
            output=replace(
                trace.output,
                certainty=trace.output.certainty
                & Certainty(determination=DeterminationQuality.UNDECIDABLE),
            ),
            alternatives_not_taken=trace.alternatives_not_taken
            + (
                Alternative(
                    key="burden_gift_high_value_basis",
                    label_ko=f"고가주택({limit:,}원) 판정 기준",
                    reason_ko=(
                        f"안분 후 양도가액 {event.transfer_price:,}원은 기준 이하지만 "
                        f"주택 전체 평가액은 {gift.appraised_value:,}원입니다. "
                        "조문 문언대로 안분 후 금액으로 판정했으나, 전체 가액을 기준으로 "
                        "보는 해석이면 비과세가 배제되어 세액이 크게 달라집니다. "
                        "**세무서 확인이 필요한 구간입니다.**"
                    ),
                    actionable=True,
                ),
            ),
        )
    return replace(result, trace=trace)


def compute_transfer_tax(
    case: TaxCase,
    event: TransferEvent,
    ruleset: RuleSet,
    *,
    track: Track = Track.CURRENT,
) -> TransferTaxResult:
    on = event.transfer_date
    person = case.find_person(event.person_id)
    prop = case.find_property(event.property_id)
    subject = SubjectRef(SubjectType.PROPERTY, str(prop.id), prop.display_name or str(prop.id))
    children: list[TraceNode] = []

    # ── 00. 기간 확정 — **여기 한 곳에서** 사실로부터 뽑는다 ────────
    #
    # ★ 취득일이 이벤트에도 소유 이력에도 적혀 있는데 보유기간을 None으로 두고 있었다
    #   (SIM-06, 2026-08-05 시뮬레이션). 결과가 참혹했다:
    #     · tr.03a 비과세 → "보유기간 미상"으로 요건 미충족
    #     · tr.05 장특공제 → "보유 0년 < 3년"으로 0원
    #     · tr.09 세율 → 보유 0년이라 **1년 미만 70% 단일세율**
    #   즉 10년 보유·10년 거주 1주택자가 12억에 팔아도 **차익 전액에 70%**가 붙었다.
    #   골든 테스트가 못 잡은 이유는 SIM-01과 같다 — 테스트가 기간을 손으로 먹여줬다.
    #
    #   아래 20여 곳이 event.holding_years를 직접 읽으므로, 입구에서 한 번 채워
    #   전 구간이 같은 값을 보게 한다. 호출부마다 도출하게 하면 한 곳을 빠뜨린다.
    #
    # 명시값은 도출값을 이긴다(배우자 상속 통산 등 엔진이 모르는 특칙이 있다).
    # 다만 **모순을 조용히 통과시키지는 않는다** — 취득일과 양도일이 사실로 주어졌는데
    # 보유기간을 다르게 적으면, 숫자만 바꿔 적어 비과세를 받아내는 길이 열린다.
    # 실측: 보유 1일 양도에 holding_years: 12를 적자 경고 없이 세액 0원이 나왔다.
    derived_hold = periods.holding_years(case, event.person_id, event.property_id, on)
    derived_live = periods.residence_years(
        case, event.person_id, event.property_id, on,
        imputed=periods.imputed_spec(ruleset, tax="transfer", on=on, track=track),
    )
    period_conflicts: list[str] = []
    if event.holding_years is not None and derived_hold is not None and event.holding_years != derived_hold:
        period_conflicts.append(
            f"보유기간: 입력 {event.holding_years}년 vs 취득일로 계산한 {derived_hold}년"
        )
    if event.residence_years is not None and derived_live is not None and event.residence_years != derived_live:
        period_conflicts.append(
            f"거주기간: 입력 {event.residence_years}년 vs 거주 이력으로 계산한 {derived_live}년"
        )

    event = replace(
        event,
        holding_years=event.holding_years if event.holding_years is not None else derived_hold,
        residence_years=event.residence_years if event.residence_years is not None else derived_live,
    )
    if period_conflicts:
        children.append(
            node(
                "tr.00.period_conflict",
                "기간 입력과 사실이 어긋납니다",
                Value.flag(
                    False,
                    certainty=Certainty(determination=DeterminationQuality.UNDECIDABLE),
                    label="기간 정합성",
                ),
                subject=subject,
                formula="입력한 보유·거주기간 vs 취득일·거주 이력에서 계산한 값",
                substitution=" / ".join(period_conflicts),
                note_ko=(
                    "입력하신 값으로 계산했습니다. 배우자 상속분 통산이나 재건축 기간 통산처럼 "
                    "정당한 사유가 있으면 맞습니다. 그렇지 않다면 취득일·거주 이력을 "
                    "확인해주세요 — 기간이 세율 구간과 비과세 요건을 직접 가릅니다."
                ),
                alternatives_not_taken=(
                    Alternative(
                        key="period_from_facts",
                        label_ko="취득일·거주 이력으로 계산한 기간",
                        reason_ko=" / ".join(period_conflicts),
                        actionable=True,
                    ),
                ),
            )
        )

    # ── 01. 주택 수 판정 (특례 반영) ────────────────────────────────
    # ── 01. 주택 수 판정 ────────────────────────────────────────────
    #
    # ★ **세목이 다르면 주택 수 규정도 다르다**(SIM-07, 2026-08-05 시뮬레이션).
    #   예전에는 종부세용 `assess()`의 결과를 그대로 썼다. 그래서 종합부동산세법
    #   시행령의 주택 수 제외 특례(지방 저가주택 §4의2③, 합산배제 임대주택 §3 등)가
    #   **양도소득세 비과세 판정에 그대로 흘러들었다.**
    #
    #   소득세법에는 지방 저가주택 제외가 없다. 1세대1주택 비과세의 주택 수 특례는
    #   소득세법 시행령 §155(일시적 2주택·상속·동거봉양·혼인 합가 등)가 따로 정하고,
    #   요건도 종부세와 다르다. 남의 법으로 센 주택 수로 비과세를 내주면
    #   **낼 세금보다 적은 금액을 알려주는 것**이고, 그대로 신고하면 과소신고 가산세다.
    #
    #   실측: 부산 1채 + 대구 1채(공시 3억)를 가진 사람이 지방 저가주택 제외로
    #   1세대1주택자가 되어 양도차익 5.4억이 전액 비과세로 나왔다.
    #
    #   그래서 여기서는 **제외 없이** 세대 주택 수를 센다. 소득세법 고유 특례는
    #   아직 구현하지 않았으므로, 해당 가능성이 있으면 조용히 넘기지 않고 드러낸다.
    assessment = assess(case, event.person_id, ruleset, track=track, on=on)
    counted, applied_special, count_note = _transfer_house_count(
        case, event.person_id, event.property_id, on, ruleset, track
    )
    house_count = len(counted)
    one_house = house_count == 1
    children.append(
        node(
            "tr.01.house_count",
            "1세대 주택 수 (양도소득세 기준)",
            Value.flag(one_house, label="1세대1주택"),
            subject=subject,
            formula="세대 전원이 소유한 주택 수 (소득세법 §89①3호)",
            substitution=(
                f"세대 주택 {house_count}채: {', '.join(str(p) for p in counted)}"
                + (f" → {applied_special}" if applied_special else "")
            ),
            branch=BranchRecord(
                condition_ko="1세대1주택 해당 여부",
                # ★ 어떤 특례가 걸렸는지는 `applied_special`이 말한다. 여기에 조항을
                #   박아 두면 다른 특례가 붙는 날 **틀린 근거**를 표시한다 —
                #   오늘 룰셋에서 고친 조항 오기와 같은 실수다(2026-08-05).
                taken=(
                    f"1세대1주택 ({applied_special.split(' —')[0]})"
                    if applied_special
                    else ("1세대1주택" if one_house else f"{house_count}주택")
                ),
            ),
            note_ko=(
                "종합부동산세의 주택 수 특례(지방 저가주택·합산배제 임대주택 등)는 "
                "양도소득세에 적용되지 않습니다. 세목마다 규정이 다릅니다."
            ),
            alternatives_not_taken=(
                (
                    Alternative(
                        key="income_tax_house_count_special",
                        label_ko="소득세법 시행령 §155 주택 수 특례",
                        reason_ko=count_note + " — 이 엔진은 아직 판정하지 않으므로 세무서 확인이 필요합니다",
                        actionable=True,
                    ),
                )
                if count_note
                else ()
            ),
        )
    )

    # ── 02. 양도차익 ────────────────────────────────────────────────
    gain_amount = max(
        0, event.transfer_price - event.acquisition_price - event.necessary_expense
    )
    gain = Value.money(gain_amount, label="양도차익")
    children.append(
        node(
            "tr.02.gain",
            "양도차익",
            gain,
            subject=subject,
            formula="양도가액 − 취득가액 − 필요경비",
            substitution=(
                f"{event.transfer_price:,} − {event.acquisition_price:,} "
                f"− {event.necessary_expense:,} = {gain_amount:,}"
            ),
        )
    )

    # ── 03. 1세대1주택 비과세 (요건 판정 → 고가주택 안분) ───────────
    # 1주택이라는 사실만으로 비과세가 되지 않는다. 시행령 §154①의 보유·거주
    # 요건을 통과해야 한다. 이 게이트가 없어서 6개월 보유 1주택자에게
    # 2.29억을 0원으로 안내하던 결함이 있었다(2026-08-04 감사).
    # 상생임대주택이면 거주요건의 '제한'을 받지 않는다(시행령 §155의3①).
    # 비과세와 장특공제 두 곳에서 쓰이므로 여기서 한 번만 판정한다.
    sang = sangsaeng_assess(case, event.property_id, ruleset, on, track)
    sang_waives = waives_residence(sang, on)
    if sang.applies or sang.undecidable:
        children.append(_sangsaeng_node(sang, sang_waives, on))

    eligible, req_node = _exemption_eligible(
        case, ruleset, on, track, event, prop, one_house, subject,
        sangsaeng_waives=sang_waives,
    )
    if req_node is not None:
        children.append(req_node)
    taxable_gain, exempt_node = _apply_exemption(
        ruleset, on, track, event, gain, one_house and eligible, subject
    )
    children.append(exempt_node)

    # ── 04. 조정대상지역 중과 판정 ──────────────────────────────────
    zone = check_regulated(prop.legal_dong_code, ruleset, on=on, track=track)
    heavy_applies = (not one_house) and house_count >= 2 and zone.designation is YES
    zone_unknown = (not one_house) and zone.designation is UNKNOWN

    # ── 05. 장기보유특별공제 (개정안: 장기거주 소득공제) ─────────────
    deduction, ded_node = _long_term_deduction(
        ruleset, on, track, event, taxable_gain, one_house, heavy_applies, subject,
        sangsaeng_waives=sang_waives,
    )
    children.append(ded_node)

    # ── 06. 기본공제 → 과세표준 ─────────────────────────────────────
    basic, basic_node = _basic_deduction(ruleset, on, track, event, one_house, subject)
    children.append(basic_node)

    base_amount = max(0, taxable_gain.as_int() - deduction.as_int() - basic.as_int())
    taxable_base = derive_value(
        base_amount, taxable_gain, deduction, basic, label="과세표준"
    )
    children.append(
        node(
            "tr.06.taxable_base",
            "양도소득 과세표준",
            taxable_base,
            subject=subject,
            inputs=(("과세대상 양도차익", taxable_gain), ("장기보유특별공제", deduction)),
            formula="과세대상 양도차익 − 장기보유특별공제 − 기본공제",
            substitution=(
                f"{taxable_gain.as_int():,} − {deduction.as_int():,} "
                f"− {basic.as_int():,} = {base_amount:,}"
            ),
        )
    )

    # ── 07. 세율 → 산출세액 ─────────────────────────────────────────
    income_tax, rate_node = _apply_rates(
        ruleset, on, track, base_amount, taxable_base, house_count,
        heavy_applies, zone, zone_unknown, event, subject,
    )
    children.append(rate_node)

    # ── 08. 개인지방소득세 ──────────────────────────────────────────
    local_res = ruleset.resolve(f"{T}.local_income_tax_rate", on=on, track=track)
    local_rate = local_res.block.as_fraction()
    local_amount = int(income_tax.as_int() * local_rate)
    local_tax = derive_value(
        local_amount, income_tax, local_res.ref(), label="개인지방소득세"
    )
    children.append(
        node(
            "tr.08.local_income_tax",
            "개인지방소득세",
            local_tax,
            subject=subject,
            rules=(local_res.ref(),),
            formula="양도소득 산출세액 × 10%",
            substitution=f"{income_tax.as_int():,} × {float(local_rate) * 100:g}%",
            note_ko="정부 문답자료의 '산출세액'에는 포함되지 않는 별도 세목입니다.",
        )
    )

    total_amount = income_tax.as_int() + local_amount
    total = derive_value(total_amount, income_tax, local_tax, label="총 부담세액")

    # 지분 소유면 안분한다. Fraction이라 공동명의 합이 원본과 정확히 같다.
    if event.share != 1:
        children.append(
            node(
                "tr.09.share",
                "지분 안분",
                Value.money(int(total_amount * event.share), label="본인 부담"),
                subject=subject,
                formula="총 부담세액 × 본인 지분",
                substitution=f"{total_amount:,} × {event.share}",
            )
        )

    trace = node(
        "tr.00.transfer_tax",
        f"양도소득세 — {prop.display_name or prop.id} ({on} 양도, {track})",
        total,
        subject=subject,
        formula="산출세액 + 개인지방소득세",
        substitution=f"{income_tax.as_int():,} + {local_amount:,} = {total_amount:,}",
        children=tuple(children),
        note_ko=(
            "개정안 기준입니다. 국회 통과 전이므로 확정된 세액이 아닙니다."
            if track is Track.REFORM
            else ""
        ),
    )

    return TransferTaxResult(
        event=event,
        gain=gain,
        taxable_gain=taxable_gain,
        long_term_deduction=deduction,
        taxable_base=taxable_base,
        income_tax=income_tax,
        local_income_tax=local_tax,
        total=total,
        trace=trace,
    )


# --------------------------------------------------------------------------
# 단계별 구현
# --------------------------------------------------------------------------


def _transfer_house_count(
    case: TaxCase,
    person_id: PersonId,
    property_id: PropertyId,
    on: date,
    ruleset: RuleSet,
    track: Track,
) -> tuple[tuple[PropertyId, ...], str, str]:
    """양도소득세용 1세대 주택 수. **종부세 특례를 쓰지 않는다.**

    소득세법 §89①3호의 '1세대 1주택'은 세대 전원이 소유한 주택을 센다.
    제외 특례는 시행령 §155가 따로 정하며 요건이 종합부동산세법과 다르다
    (SIM-07, 2026-08-05: 종부령 §4의2③ 지방 저가주택 제외를 그대로 써서
     2주택자의 양도차익 5.4억이 전액 비과세로 나갔다 — 실측 0원 → 1.7억원).

    적용 원칙 — **방향이 다르면 기준도 다르다.**
      주택 수를 줄이는 특례는 세액을 **낮춘다.** 확인하지 못한 채 적용하면
      과소신고가 되고 가산세가 붙는다. 그래서 **사실만으로 전부 판정되는 특례만
      적용하고**, 하나라도 확인이 필요하면 적용하지 않고 안내한다.

    돌려주는 것: (센 주택, 적용한 특례 설명, 확인이 필요한 특례 안내)
    """
    members = set(case.household_member_ids(person_id))
    owned: dict[PropertyId, list] = {}
    for o in case.ownerships:
        if o.person_id in members and case.find_property(o.property_id).is_house:
            owned.setdefault(o.property_id, []).append(o)

    counted = tuple(owned)
    if len(counted) <= 1:
        return counted, "", ""

    payload = _payload_of(ruleset, f"{T}.house_count_specials", on, track)

    # ── §155① 일시적 2주택 — 취득일만으로 전부 판정된다 ──────────────
    applied = ""
    if payload and len(counted) == 2 and property_id in owned:
        spec = payload.get("temporary_two") or {}
        firsts = {
            pid: min((o.acquired_on for o in rows if o.acquired_on), default=None)
            for pid, rows in owned.items()
        }
        if all(firsts.values()):
            old_id, new_id = sorted(firsts, key=lambda p: firsts[p])
            gap_years = periods.full_years(firsts[old_id], firsts[new_id])
            sell_years = periods.full_years(firsts[new_id], on)
            min_gap = int(spec.get("min_years_before_new", 1))
            max_sell = int(spec.get("max_years_to_sell_old", 3))
            # 양도하는 것이 **종전주택**이어야 한다. 신규주택을 팔면 특례가 아니다.
            if property_id == old_id and gap_years >= min_gap and sell_years < max_sell:
                applied = (
                    f"일시적 2주택(§155①) — 종전주택 취득 {firsts[old_id]} → "
                    f"{gap_years}년 후 신규 취득 {firsts[new_id]} → "
                    f"{sell_years}년 만에 종전주택 양도 (요건 {min_gap}년 이상 · {max_sell}년 이내)"
                )
                return (property_id,), applied, ""

    # ── §155② 상속주택 — 일반주택을 양도할 때만, 동일세대가 아닐 때만 ─
    inherited_ids = {
        pid for pid, rows in owned.items()
        if any(o.cause is AcquisitionCause.INHERITANCE and o.inherited for o in rows)
    }
    if payload and len(counted) == 2 and len(inherited_ids) == 1:
        (inherited_id,) = inherited_ids
        metas = [
            o.inherited for rows in owned.values() for o in rows
            if o.property_id == inherited_id and o.inherited
        ]
        same_household = metas[0].same_household_at_death if metas else None
        # 조문이 "**일반주택**을 양도하는 경우"라고 못 박는다.
        # 상속주택을 팔면 특례가 아니다 — 방향을 뒤집으면 비과세를 잘못 내준다.
        if property_id != inherited_id:
            if same_household is False:
                applied = (
                    f"상속주택 특례(§155②) — 상속주택({inherited_id})을 주택 수에서 제외하고 "
                    "일반주택을 양도. 상속개시 당시 피상속인과 별도 세대"
                )
                return (property_id,), applied, ""
            if same_household is None:
                return counted, "", (
                    "상속받은 주택이 있습니다. **일반주택**을 양도하면 상속주택은 주택 수에서 "
                    "빠져 1세대1주택이 될 수 있습니다(§155②). 다만 **상속개시 당시 피상속인과 "
                    "같은 세대였는지**에 따라 결론이 정반대로 갈리는데(§155② 단서) 그 사실이 "
                    "입력되지 않아 판정하지 않았습니다"
                )
            # same_household is True → 단서: 동거봉양 합가로 2주택이 된 경우
            #   '합치기 이전부터 보유하던 주택'만 상속주택으로 본다. 합가 시점을
            #   입력받지 않으므로 판정하지 않는다.
            return counted, "", (
                "상속개시 당시 피상속인과 같은 세대였습니다. 이 경우 §155② 단서에 따라 "
                "**동거봉양 합가**로 2주택이 된 경우의 '합치기 이전부터 보유하던 주택'만 "
                "상속주택으로 봅니다. 합가 시점을 입력받지 않아 판정하지 않았습니다"
            )

    # ── 확인이 필요한 나머지는 적용하지 않고 알린다 ──────────────────
    hints: list[str] = []
    if inherited_ids and property_id in inherited_ids:
        hints.append(
            "양도하려는 주택이 **상속받은 주택**입니다. §155②는 상속주택이 아니라 "
            "**일반주택**을 양도할 때 적용되므로 이 양도에는 특례가 없습니다"
        )
    elif inherited_ids:
        hints.append(
            "상속받은 주택이 있습니다. 일반주택을 양도하면 §155② 특례를 볼 수 있으나 "
            "주택이 3채 이상이라 조문 요건(각 1개씩)에 맞지 않습니다"
        )
    if len(counted) == 2 and not applied:
        hints.append(
            "2주택입니다. 일시적 2주택 요건(§155① 종전주택 취득 1년 후 신규 취득 · "
            "신규 취득 3년 이내 종전주택 양도)을 다시 확인해보세요"
        )
    if len(members) > 1:
        years = (payload or {}).get("cohabitation_years", 10)
        hints.append(
            f"동거봉양 합가(§155④)나 혼인(§155⑤)으로 2주택이 됐다면 합친 날부터 "
            f"{years}년 이내 먼저 양도하는 주택은 1세대1주택으로 봅니다. "
            "합가일·혼인일을 입력받지 않아 판정하지 않았습니다"
        )

    return counted, applied, " / ".join(hints)


def _payload_of(
    ruleset: RuleSet, rule_id: str, on: date, track: Track
) -> Mapping[str, Any] | None:
    res = ruleset.resolve_or_none(rule_id, on=on, track=track)
    return res.block.payload if res is not None else None


def _sangsaeng_node(v: SangsaengVerdict, waives: bool, on: date) -> TraceNode:
    """상생임대주택 판정을 화면에 드러낸다.

    이 판정은 비과세 12억을 좌우하는데 계산식에는 숫자로 나타나지 않는다.
    노드로 남기지 않으면 사용자가 "왜 비과세가 됐는지" 검증할 수 없다.
    """
    overdue = v.applies and not waives
    if waives:
        taken = "적용 — 거주요건 면제"
    elif overdue:
        taken = f"상실 — 양도기한 {v.transfer_deadline} 초과"
    elif v.undecidable:
        taken = "판정 불가"
    else:
        taken = "미적용"

    lines: list[str] = []
    if v.lease is not None:
        lines.append(
            f"상생임대차계약 {v.lease.start}~{v.lease.actual_end or '미정'}"
            + (f" · 직전계약 {v.prior.start}~{v.prior.actual_end or '미정'}" if v.prior else "")
        )
    if v.transfer_deadline is not None:
        lines.append(
            f"개편안 양도기한 {v.transfer_deadline}까지 — 넘기면 거주요건 면제가 사라집니다"
        )
    lines.extend(v.checks_ko)
    lines.extend(v.reasons_ko)
    if waives:
        # ★ 이 한 줄이 이 노드의 존재 이유다. 면제를 '거주한 것으로 쳐준다'로
        #   오해하면 장기보유특별공제를 실제보다 크게 잡는다.
        lines.append(
            "면제되는 것은 거주기간의 '제한'입니다. 거주기간을 2년으로 쳐주는 것이 "
            "아니므로, 장기보유특별공제의 거주기간 공제율은 실제 거주기간으로 "
            "계산합니다(실거주 0년이면 거주공제 0%)."
        )

    return node(
        "tr.03a.sangsaeng",
        "상생임대주택 특례",
        Value.flag(waives, certainty=v.certainty, label="거주요건 면제"),
        formula="직전임대차 1년6개월 + 상생임대차 2년 + 증가율 5% 이내(시행령 §155의3①)",
        substitution=" / ".join(lines) if lines else "해당 없음",
        branch=BranchRecord(condition_ko="상생임대주택 특례", taken=taken),
        note_ko=(
            "상생임대주택은 1세대1주택 비과세와 장기보유특별공제(표2)의 2년 "
            "거주요건을 면제받습니다(소득세법 시행령 §155의3①)."
        ),
    )


def _exemption_eligible(
    case: TaxCase,
    ruleset: RuleSet,
    on: date,
    track: Track,
    event: TransferEvent,
    prop,
    one_house: bool,
    subject: SubjectRef,
    sangsaeng_waives: bool = False,
) -> tuple[bool, TraceNode | None]:
    """1세대1주택 비과세의 보유·거주 요건(소득세법 시행령 §154①).

    ★ 거주요건은 **취득 당시** 조정대상지역이었는지로 갈린다. 양도 시점이 아니다.
      2026년에 규제가 풀린 지역이라도, 2021년 취득 당시 조정대상지역이었으면
      거주 2년을 채워야 한다.

    요건 미달이면 비과세를 주지 않는다. 단서 예외(수용·해외이주 등)는 사실관계
    확인이 필요해 자동 판정하지 않고, 해당할 수 있다는 안내만 남긴다 —
    **유리한 쪽으로 자동 가정하지 않는다**는 원칙 그대로다.
    """
    if not one_house:
        return False, None

    res = ruleset.resolve(f"{T}.one_house_exempt_requirements", on=on, track=track)
    payload = res.block.payload
    min_hold = int(payload.get("min_holding_years", 2))
    min_live = int(payload.get("min_residence_years_if_regulated_at_acquisition", 2))

    acquired = next(
        (o.acquired_on for o in case.ownerships_of(event.person_id)
         if o.property_id == event.property_id),
        None,
    )
    zone_then = check_regulated(
        prop.legal_dong_code, ruleset, on=acquired or on, track=track
    )

    held = event.holding_years
    lived = event.residence_years
    failures: list[str] = []
    certainty = Certainty()

    # ── 보유요건 (모든 경우 공통) ──────────────────────────────────
    if held is None:
        certainty = certainty & Certainty(determination=DeterminationQuality.UNDECIDABLE)
        failures.append("보유기간 미상")
    elif held < min_hold:
        failures.append(f"보유 {held}년 < {min_hold}년")

    # ── 거주요건 (취득 당시 조정대상지역인 경우만) ──────────────────
    # ★ 순서가 중요하다. 거주요건을 **이미 충족했으면** 취득 당시 지역이 무엇이든
    #   결론이 같으므로 지역을 따질 필요가 없다. 이 순서를 뒤집으면 2017년에 산
    #   집처럼 지역 이력이 없는 사례가 전부 '판정 불가'로 막힌다 —
    #   답이 갈리지 않는 지점에서까지 판정 불가를 내는 것은 정직이 아니라 무능이다.
    residence_met = lived is not None and lived >= min_live
    needs_residence = zone_then.designation is YES

    # ★ 상생임대주택은 이 요건의 '제한'을 받지 않는다(시행령 §155의3①이 §154①을 지목).
    #   거주기간을 채운 것으로 **의제하는 것이 아니라**, 요건 자체가 걸리지 않는다.
    #   그래서 여기서만 통과시키고, 장특공제의 거주기간 공제율에는 손대지 않는다.
    #   거주요건 면제 자체는 현행법(§155의3)이다. 개편안이 손대는 것은 '언제까지
    #   팔아야 하는가'뿐이고, 그 미확정성은 상생임대 판정 노드가 따로 드러낸다.
    if not residence_met and sangsaeng_waives:
        residence_met = True

    if not residence_met:
        if needs_residence:
            if lived is None:
                certainty = certainty & Certainty(
                    determination=DeterminationQuality.UNDECIDABLE
                )
                failures.append("거주기간 미상(취득 당시 조정대상지역)")
            else:
                failures.append(f"거주 {lived}년 < {min_live}년 (취득 당시 조정대상지역)")
        elif zone_then.designation is UNKNOWN:
            # 거주요건을 못 채웠는데 지역도 모른다 — 여기서만 답이 갈린다.
            # 보수적으로(세액 높은 쪽) 비과세를 주지 않고, 갈린다는 사실을 남긴다.
            certainty = certainty & Certainty(
                determination=DeterminationQuality.UNDECIDABLE
            )
            failures.append(
                f"취득({acquired or '일자 미상'}) 당시 조정대상지역 여부 미상 "
                f"— 조정대상지역이었다면 거주 {min_live}년이 필요합니다"
            )

    # ★ 기간 요건을 못 채웠다고 **끝난 게 아니다**(SIM-10, 2026-08-05 법령 대조).
    #   시행령 §154⑧은 보유·거주기간을 통산하도록 정한다.
    #     1호 소실·무너짐·노후로 멸실되어 재건축한 주택 → 멸실 주택의 기간을 통산
    #     3호 상속인과 피상속인이 상속개시 당시 동일세대 → 상속 전 기간을 통산
    #
    #   재건축 조합원이 준공 2년 안에 팔면 20년 보유자도 "보유 1년"으로 읽히고,
    #   부모와 살던 집을 상속받아 팔면 상속개시일부터만 세어 2년 미달이 된다.
    #   둘 다 매우 흔하다.
    #
    #   멸실 주택의 취득일·피상속인의 보유기간은 지금 입력받지 않으므로 **통산을
    #   지어내지 않는다.** 다만 통산 여지가 있는데 "미충족"이라고 단정하지도 않는다 —
    #   답이 갈리는 자리에서는 갈린다고 말하는 것이 이 엔진의 규칙이다.
    aggregation_hints: list[str] = []
    if failures:
        causes = {
            o.cause
            for o in case.ownerships_of(event.person_id)
            if o.property_id == event.property_id
        }
        if AcquisitionCause.RECONSTRUCTION in causes or AcquisitionCause.NEW_BUILD in causes:
            aggregation_hints.append(
                "멸실 후 재건축한 주택이면 멸실 주택의 보유·거주기간을 통산합니다(시행령 §154⑧1호)"
            )
        if AcquisitionCause.INHERITANCE in causes:
            aggregation_hints.append(
                "상속개시 당시 피상속인과 동일세대였다면 상속 전 기간을 통산합니다(시행령 §154⑧3호)"
            )

    ok = not failures
    if aggregation_hints:
        certainty = certainty & Certainty(determination=DeterminationQuality.UNDECIDABLE)

    # 지역 판정의 확실성도 함께 물고 온다 — 취득 당시 지역이 미상이면
    # 요건 판정 자체가 미상이다.
    certainty = certainty & zone_then.certainty
    value = Value.money(1 if ok else 0, certainty=certainty, label="비과세 요건 충족")
    alternatives: tuple[Alternative, ...] = ()
    if not ok:
        alternatives = (
            Alternative(
                key="one_house_exemption_requirements",
                label_ko="1세대1주택 비과세",
                reason_ko=" · ".join(failures),
                actionable=True,
            ),
        )
        if aggregation_hints:
            alternatives += (
                Alternative(
                    key="holding_period_aggregation",
                    label_ko="보유·거주기간 통산(시행령 §154⑧)",
                    reason_ko=(
                        " / ".join(aggregation_hints)
                        + " — 통산하면 요건을 충족할 수 있습니다. 이 엔진은 멸실 주택의 "
                        "취득일이나 피상속인의 보유기간을 입력받지 않아 판정하지 않았습니다."
                    ),
                    actionable=True,
                ),
            )

    return ok, node(
        "tr.03a.exemption_requirements",
        "1세대1주택 비과세 요건",
        value,
        subject=subject,
        rules=(res.ref(),),
        formula=(
            f"보유 {min_hold}년 이상"
            + (
                f" + 거주 {min_live}년 이상(취득 당시 조정대상지역)"
                if needs_residence or not residence_met
                else ""
            )
        ),
        substitution=(
            f"취득 {acquired or '미상'} · 보유 {held}년 · 거주 {lived}년 → "
            + ("충족" if ok else " / ".join(failures))
        ),
        branch=BranchRecord(
            condition_ko="비과세 요건", taken="충족" if ok else "미충족",
            detail_ko=(
                "취득 당시 조정대상지역이라 거주요건이 추가됩니다"
                if needs_residence
                else (
                    f"거주 {lived}년으로 요건을 이미 충족해 취득 당시 지역을 따지지 않습니다"
                    if residence_met
                    else "취득 당시 비규제지역 — 보유요건만"
                )
            ),
        ),
        alternatives_not_taken=alternatives,
        note_ko=(
            None if ok else
            "다음에 해당하면 보유·거주 요건 없이 비과세될 수 있습니다(세무서 확인 필요): "
            + " / ".join(payload.get("exceptions_ko", []))
        ),
    )


def _apply_exemption(
    ruleset: RuleSet,
    on: date,
    track: Track,
    event: TransferEvent,
    gain: Value,
    one_house: bool,
    subject: SubjectRef,
) -> tuple[Value, TraceNode]:
    """1세대1주택 비과세(소득세법 §89①3).

    12억원 이하면 전액 비과세, 초과하면 **초과분 비율만큼만** 과세한다.
    이 안분을 빠뜨리면 고가주택 세액이 통째로 부풀어 오른다.
    """
    res = ruleset.resolve(f"{T}.one_house_exempt_limit", on=on, track=track)
    limit = res.block.as_int()

    if not one_house:
        return gain, node(
            "tr.03.exemption",
            "1세대1주택 비과세",
            gain,
            subject=subject,
            rules=(res.ref(),),
            substitution="1세대1주택자가 아니므로 양도차익 전액 과세",
            branch=BranchRecord(condition_ko="1세대1주택 비과세", taken="미적용"),
            alternatives_not_taken=(
                Alternative(
                    key="one_house_exemption",
                    label_ko=f"1세대1주택 비과세(양도가액 {limit:,}원 이하)",
                    reason_ko="1세대1주택자가 아니다",
                ),
            ),
        )

    if event.transfer_price <= limit:
        zero = derive_value(0, gain, res.ref(), label="과세대상 양도차익")
        return zero, node(
            "tr.03.exemption",
            "1세대1주택 비과세",
            zero,
            subject=subject,
            rules=(res.ref(),),
            formula="양도가액이 비과세 한도 이하면 전액 비과세",
            substitution=f"{event.transfer_price:,} ≤ {limit:,} → 과세대상 0원",
            branch=BranchRecord(condition_ko="1세대1주택 비과세", taken="전액 비과세"),
        )

    ratio = Fraction(event.transfer_price - limit, event.transfer_price)
    amount = int(gain.as_int() * ratio)
    value = derive_value(amount, gain, res.ref(), label="과세대상 양도차익")
    return value, node(
        "tr.03.exemption",
        "1세대1주택 비과세 (고가주택 안분)",
        value,
        subject=subject,
        inputs=(("양도차익", gain),),
        rules=(res.ref(),),
        formula="양도차익 × (양도가액 − 12억원) ÷ 양도가액",
        substitution=(
            f"{gain.as_int():,} × ({event.transfer_price:,} − {limit:,}) "
            f"÷ {event.transfer_price:,} = {amount:,}"
        ),
        branch=BranchRecord(
            condition_ko="1세대1주택 비과세", taken="고가주택 초과분만 과세"
        ),
    )


def _rate_for(spec: Mapping[str, Any] | None, years: int | None) -> Fraction:
    """공제율 한 축. 요건 미달이면 0."""
    if not spec or years is None:
        return Fraction(0)
    if years < int(spec["min_years"]):
        return Fraction(0)
    capped = min(years, int(spec["cap_years"]))
    rate = Fraction(str(spec["rate_per_year"])) * capped
    return min(rate, Fraction(str(spec["max_rate"])))


def _long_term_deduction(
    ruleset: RuleSet,
    on: date,
    track: Track,
    event: TransferEvent,
    taxable_gain: Value,
    one_house: bool,
    heavy_applies: bool,
    subject: SubjectRef,
    sangsaeng_waives: bool = False,
) -> tuple[Value, TraceNode]:
    """장기보유특별공제 → 개정안 「장기거주 소득공제」.

    ★ 다주택자가 조정대상지역 주택을 양도하면 이 공제가 **배제**된다(소득세법 §95② 단서).
      중과와 공제 배제가 함께 오므로 세액이 두 배로 뛴다.
    """
    if heavy_applies:
        zero = Value.money(0, label="장기보유특별공제")
        return zero, node(
            "tr.05.long_term_deduction",
            "장기보유특별공제",
            zero,
            subject=subject,
            substitution="다주택자의 조정대상지역 주택 양도 → 공제 배제",
            branch=BranchRecord(condition_ko="장특공제 적용", taken="배제"),
            note_ko=(
                "중과세율과 공제 배제가 함께 적용되어 세부담이 크게 늘어납니다. "
                "소득세법 §95② 단서."
            ),
            alternatives_not_taken=(
                Alternative(
                    key="long_term_deduction",
                    label_ko="장기보유특별공제",
                    reason_ko="다주택자가 조정대상지역 소재 주택을 양도하면 배제된다",
                ),
            ),
        )

    res = ruleset.resolve(
        f"{T}.long_term_deduction", on=on, track=track, one_house=one_house
    )
    payload = res.block.payload

    # ── 게이트 ① 소득세법 §95② 본문: "보유기간이 3년 이상인 것" ──────────
    # 축별 min_years만으로는 못 막는다. 거주 축이 min_years=2라서 보유 2년짜리가
    # 거주공제만 챙겨 빠져나갔다(2026-08-04 감사에서 발견).
    # 2029년 블록은 holding: null이라 아예 게이트가 없었다 — 개편안이 폐지한 것은
    # 보유 '공제율'이지 보유 3년 '기본요건'이 아니다.
    min_holding = int(payload.get("min_holding_years", 3))
    if (event.holding_years or 0) < min_holding:
        zero = Value.money(0, label="장기보유특별공제")
        return zero, node(
            "tr.05.long_term_deduction",
            "장기보유특별공제",
            zero,
            subject=subject,
            rules=(res.ref(),),
            formula=f"보유기간 {min_holding}년 이상인 자산만 공제",
            substitution=f"보유 {event.holding_years or 0}년 < {min_holding}년 → 공제 없음",
            branch=BranchRecord(
                condition_ko="장특공제 적용", taken=f"배제(보유 {min_holding}년 미만)"
            ),
            note_ko=(
                f"장기보유특별공제는 보유기간 {min_holding}년 이상인 자산에만 적용됩니다"
                "(소득세법 §95② 본문)."
            ),
        )

    # ── 게이트 ② 시행령 §159의4: 표2는 "거주기간이 2년 이상인" 1세대1주택만 ──
    # 1세대1주택이어도 거주 2년 미만이면 다주택자와 같은 표1(보유 연2%·최대30%)이다.
    # 거주기간 미입력은 요건 충족으로 보지 않는다 — 유리한 쪽 자동 가정 금지.
    #
    # ★★ 상생임대주택은 이 **게이트만** 면제받는다(§155의3①이 §159의4를 지목).
    #    §159의4는 표2 대상을 "거주기간이 2년 이상인 것"으로 정의하므로, 면제되는
    #    것은 표2에 **들어갈 자격**이다. 거주기간을 2년으로 의제하는 규정은 없다.
    #
    #    그래서 아래 `live_rate`는 손대지 않는다 — 실거주 0년이면 거주공제는 0%다.
    #    2026 개편안이 보유공제를 거주공제로 옮기므로(연 4%→2%→폐지) 이 구분이
    #    치명적이 된다. 상생임대 특례가 살아 있어도 실거주 0년 주택의 공제율은
    #    '27년 40% → '28년 20% → '29년 0%로 무너진다.
    #    **비과세 12억은 지켜지지만 장기보유특별공제는 지켜지지 않는다.**
    fell_back = False
    need_residence = payload.get("table2_min_residence_years")
    if one_house and need_residence is not None and not sangsaeng_waives:
        if (event.residence_years or 0) < int(need_residence):
            fell_back = True
            res = ruleset.resolve(
                f"{T}.long_term_deduction", on=on, track=track, one_house=False
            )
            payload = res.block.payload

    hold_rate = _rate_for(payload.get("holding"), event.holding_years)
    live_rate = _rate_for(payload.get("residence"), event.residence_years)

    if payload.get("mode") == "max":
        rate = max(hold_rate, live_rate)
        detail = f"보유 {hold_rate} vs 거주 {live_rate} 중 높은 쪽"
    else:
        rate = hold_rate + live_rate
        detail = f"보유 {hold_rate} + 거주 {live_rate}"

    raw = int(taxable_gain.as_int() * rate)

    cap_res = ruleset.resolve(f"{T}.long_term_deduction_cap", on=on, track=track)
    cap = (
        None
        if cap_res.block.payload.get("applicable") is False
        else int(cap_res.block.value)
    )
    amount = raw if cap is None else min(raw, cap)

    alternatives: list[Alternative] = []
    if cap is not None and raw > cap:
        alternatives.append(
            Alternative(
                key="ltd_cap",
                label_ko="장기거주 소득공제 한도(개정안 신설)",
                reason_ko=f"공제 산출액 {raw:,}원이 한도 {cap:,}원을 초과",
                delta=Value.money(cap - raw),
            )
        )
    if fell_back:
        # 왜 1주택인데 표1을 썼는지 화면에 남긴다. 이게 없으면 사용자는
        # "1주택인데 공제가 왜 이렇게 적지?"에서 막힌다.
        alternatives.append(
            Alternative(
                key="ltd_table2",
                label_ko="1세대1주택 우대 공제율(표2, 보유+거주 최대 80%)",
                reason_ko=(
                    f"거주기간 {event.residence_years or 0}년으로 "
                    f"{need_residence}년 요건에 미달 — 소득세법 시행령 §159의4"
                ),
                actionable=True,
            )
        )

    certainty = Certainty()
    if event.holding_years is None and event.residence_years is None:
        certainty = certainty & Certainty(determination=DeterminationQuality.UNDECIDABLE)
    if fell_back and event.residence_years is None:
        # 거주기간을 안 받았으면 표1을 쓴 것이 '판정'이 아니라 '보수적 가정'이다.
        certainty = certainty & Certainty(determination=DeterminationQuality.UNDECIDABLE)

    value = derive_value(
        amount, taxable_gain, certainty, res.ref(), cap_res.ref(), label="장기보유특별공제"
    )
    return value, node(
        "tr.05.long_term_deduction",
        "장기보유특별공제" if track is Track.CURRENT else "장기거주 소득공제",
        value,
        subject=subject,
        inputs=(("과세대상 양도차익", taxable_gain),),
        rules=(res.ref(), cap_res.ref()),
        formula="과세대상 양도차익 × 공제율",
        substitution=(
            f"{taxable_gain.as_int():,} × {rate} = {raw:,}"
            + (f" → 한도 {cap:,} 적용 = {amount:,}" if amount != raw else "")
        ),
        branch=BranchRecord(
            condition_ko="공제율 구성",
            taken=detail + (" · 표1(거주요건 미충족)" if fell_back else ""),
            detail_ko=f"보유 {event.holding_years}년 · 거주 {event.residence_years}년",
        ),
        alternatives_not_taken=tuple(alternatives),
    )


def _basic_deduction(
    ruleset: RuleSet,
    on: date,
    track: Track,
    event: TransferEvent,
    one_house: bool,
    subject: SubjectRef,
) -> tuple[Value, TraceNode]:
    """양도소득 기본공제(소득세법 §103).

    개편안은 10년 이상 거주한 30억원 이하 1세대1주택에 한해 250만원 → 2,500만원으로 올린다.
    """
    # 현행 트랙에는 확대 규정이 없다. 셀렉터 없는 일반 블록이 잡히므로,
    # 조회 결과가 실제로 확대 규정인지(payload에 요건이 있는지)를 확인해야 한다.
    candidate = ruleset.resolve_or_none(
        f"{T}.basic_deduction", on=on, track=track, long_residence_one_house=True
    )
    special = (
        candidate
        if candidate is not None and "min_residence_years" in candidate.block.payload
        else None
    )

    long_residence = False
    if special is not None and one_house:
        p = special.block.payload
        long_residence = (
            (event.residence_years or 0) >= int(p["min_residence_years"])
            and event.transfer_price <= int(p["max_transfer_price"])
        )

    res = (
        special
        if long_residence
        else ruleset.resolve(f"{T}.basic_deduction", on=on, track=track)
    )
    amount = int(res.block.value)
    value = derive_value(amount, res.ref(), label="양도소득 기본공제")

    alternatives: tuple[Alternative, ...] = ()
    if special is not None and one_house and not long_residence:
        p = special.block.payload
        alternatives = (
            Alternative(
                key="basic_deduction_long_residence",
                label_ko="장기 거주 1주택 기본공제 확대(개정안, 2,500만원)",
                reason_ko=(
                    f"{p['min_residence_years']}년 이상 거주 + 양도가액 "
                    f"{int(p['max_transfer_price']):,}원 이하 요건 미충족 "
                    f"(거주 {event.residence_years}년 · 양도가액 {event.transfer_price:,}원)"
                ),
                actionable=True,
            ),
        )

    return value, node(
        "tr.06a.basic_deduction",
        "양도소득 기본공제",
        value,
        subject=subject,
        rules=(res.ref(),),
        substitution=f"{amount:,}",
        branch=BranchRecord(
            condition_ko="기본공제 유형",
            taken="장기 거주 1주택 확대" if long_residence else "일반",
        ),
        alternatives_not_taken=alternatives,
    )


def _apply_rates(
    ruleset: RuleSet,
    on: date,
    track: Track,
    base_amount: int,
    taxable_base: Value,
    house_count: int,
    heavy_applies: bool,
    zone,
    zone_unknown: bool,
    event: TransferEvent,
    subject: SubjectRef,
) -> tuple[Value, TraceNode]:
    """기본세율 + 다주택 조정지역 중과."""
    rate_res = ruleset.resolve(f"{T}.basic_rate_table", on=on, track=track)
    table = rate_res.block.table
    assert table is not None
    amount, _, substitution = table.tax_for(base_amount)

    rules = [rate_res.ref()]
    surcharge = Fraction(0)
    surcharge_ref = None
    alternatives: list[Alternative] = []

    held = event.holding_years or 0

    if heavy_applies:
        group = "2" if house_count == 2 else "3+"
        heavy = ruleset.resolve(
            f"{T}.heavy_surcharge", on=on, track=track, house_group=group
        )
        # ★ 한시완화는 **2년 이상 보유분에 한정**된다(개조식 p.22 각주 "* 2년 이상 보유").
        #   요건을 안 보면 보유 1.5년 2주택자에게 +5%p를 줘서 15%p를 덜 매긴다.
        need = heavy.block.payload.get("min_holding_years")
        if need is not None and held < int(need):
            heavy = ruleset.resolve(
                f"{T}.heavy_surcharge", on=on, track=Track.CURRENT, house_group=group
            )
            alternatives.append(
                Alternative(
                    key="heavy_relief",
                    label_ko="다주택 중과 한시완화(개편안)",
                    reason_ko=f"보유 {held}년으로 {need}년 요건에 미달 — 본칙 중과세율이 적용됩니다",
                    actionable=True,
                )
            )
        surcharge = heavy.block.as_fraction()
        surcharge_ref = heavy.ref()
        rules.append(surcharge_ref)
        extra = int(base_amount * surcharge)
        amount += extra
        substitution += f" + {base_amount:,} × {float(surcharge) * 100:g}%p [중과]"

    # ── 단기보유 단일세율과 비교 (소득세법 §104① 후단 · §104⑦ 후단) ──────
    # "하나의 자산이 둘 이상의 세율에 해당할 때에는 … 산출세액 중 **큰 것**"
    #
    # ★ 기본세율은 누진이라 실효세율이 45%를 넘지 못한다. 그래서 중과가 없으면
    #   단기세율(70%/60%)이 **항상 이긴다.** 비교가 실제로 갈리는 곳은 중과 구간뿐이다
    #   — 3주택 중과 +30%p면 45+30=75%가 되어 70%를 넘는다.
    #   §104⑦에 후단이 따로 붙어 있는 이유가 그것이다.
    #
    # 이 비교가 없어서 보유 6개월 사례가 45%로 계산되고 있었다(2026-08-04 감사).
    #
    # ⚠️ 보유기간을 **모를 때**도 이 분기를 탄다. `held`가 0으로 떨어져 '1년 미만'이
    #   되므로 세액은 보수적(높은) 쪽으로 가지만, 그건 판정이 아니라 **가정**이다.
    #   가정을 판정인 척하면 사용자는 70%가 확정인 줄 안다 — 아래에서 확실성을 낮추고
    #   무엇을 알려주면 달라지는지 남긴다.
    unknown_holding = event.holding_years is None
    band = "under_1y" if held < 1 else ("1y_to_2y" if held < 2 else None)
    if band is not None and base_amount > 0:
        short = ruleset.resolve(f"{T}.short_term_rate", on=on, track=track, holding=band)
        short_rate = short.block.as_fraction()
        short_amount = int(base_amount * short_rate)
        rules.append(short.ref())
        label = "1년 미만" if band == "under_1y" else "1년 이상 2년 미만"
        if unknown_holding:
            alternatives.append(
                Alternative(
                    key="short_term_rate_assumed",
                    label_ko="보유기간에 따른 세율 확정",
                    reason_ko=(
                        "보유기간을 알려주지 않아 **1년 미만으로 가정**했습니다"
                        f"(단일세율 {float(short_rate) * 100:g}%). "
                        "취득일을 알려주시면 실제 세율로 다시 계산합니다 — "
                        "2년 이상이면 이 세율은 적용되지 않습니다."
                    ),
                    actionable=True,
                )
            )
        if short_amount > amount:
            substitution = (
                f"max(기본{'+중과' if heavy_applies else ''} {amount:,}, "
                f"단기 {base_amount:,} × {float(short_rate) * 100:g}% = {short_amount:,})"
                f" = {short_amount:,}"
            )
            amount = short_amount
        else:
            alternatives.append(
                Alternative(
                    key="short_term_rate",
                    label_ko=f"단기보유 세율({label} {float(short_rate) * 100:g}%)",
                    reason_ko=(
                        f"단기세율 산출세액 {short_amount:,}원이 "
                        f"기본세율 산출세액 {amount:,}원보다 작아 적용되지 않습니다"
                    ),
                )
            )

    certainty = Certainty()

    if zone_unknown:
        # 조정대상지역을 모르면 중과 여부를 확정할 수 없다.
        # 유리한 쪽(비중과)으로 계산하되 확실성을 낮추고 사실을 드러낸다.
        certainty = certainty & Certainty(determination=DeterminationQuality.UNDECIDABLE)
        alternatives.append(
            Alternative(
                key="heavy_surcharge_unknown",
                label_ko="다주택 조정대상지역 중과",
                reason_ko=(
                    f"조정대상지역 여부를 판정하지 못했습니다. {zone.reason_ko} "
                    "중과 대상이면 세액이 크게 올라갑니다."
                ),
                actionable=True,
            )
        )

    if unknown_holding and band is not None:
        # 보유기간을 몰라 '1년 미만'으로 가정했다. 가정을 판정인 척하지 않는다.
        certainty = certainty & Certainty(determination=DeterminationQuality.UNDECIDABLE)

    value = derive_value(amount, taxable_base, certainty, *rules, label="산출세액")
    return value, node(
        "tr.07.income_tax",
        "양도소득 산출세액",
        value,
        subject=subject,
        inputs=(("과세표준", taxable_base),),
        rules=tuple(rules),
        formula="과세표준 구간별 기본세율" + (" + 중과세율" if heavy_applies else ""),
        substitution=substitution,
        branch=BranchRecord(
            condition_ko="다주택 조정대상지역 중과",
            taken="적용" if heavy_applies else "미적용",
            detail_ko=f"주택 수 {house_count}채 · {zone.reason_ko}",
        ),
        alternatives_not_taken=tuple(alternatives),
    )
