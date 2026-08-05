"""배우자 증여의 **비용**을 계산한다.

이 모듈은 증여세 서비스가 아니다. 존재 이유는 하나다.

    "부부간에 지분을 나누면 종부세가 줄어듭니다"라고 말하려면
    **그 대가**를 같은 화면에서 계산해야 한다.

절감액만 크게 써 놓고 증여세·취득세를 빼면 그건 조언이 아니라 유인이다.
전략 엔진의 규칙 ②("요건과 부작용을 함께 말한다")가 여기서 실행된다.

범위를 **배우자 증여 한 가지로** 못 박는다. 직계존비속 증여·상속·저가양수는
담지 않는다. 담을 수 있는 척하면 쓰는 사람이 그걸 믿는다.

계산 구조 (상증세법)
    증여재산가액                                  = 증여세 과세가액
    − 배우자 증여재산공제 6억 (§53①1호, 10년 합산)  = 과세표준
    × 세율 (§56 → §26 준용)                       = 산출세액
    − 신고세액공제 3% (§69②)                      = 증여세

    + 취득세 (지방세법 §11①2 무상취득 3.5%,
              조정지역 일정가액 이상은 §13의2② 12%)  = 총 비용
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from ..domain.models import Won
from ..rules.resolver import RuleSet
from ..rules.schema import Track
from .trace import (
    Alternative,
    BranchRecord,
    SubjectRef,
    TraceNode,
    Value,
    derive_value,
    node,
)

G = "gift"


@dataclass(frozen=True, slots=True)
class GiftCost:
    """배우자에게 증여할 때 드는 돈. 전부 세금이고, 전부 즉시 나간다."""

    gift_value: Won
    """증여하는 지분의 가액."""
    deduction: Won
    taxable_base: Won
    gift_tax: Won
    acquisition_tax: Won
    trace: TraceNode

    @property
    def total(self) -> Won:
        return self.gift_tax + self.acquisition_tax

    @property
    def carryover_warning_needed(self) -> bool:
        """증여세가 0원이어도 이월과세는 걸린다 — 취득세와 별개로 항상 경고한다."""
        return True


def compute_spouse_gift_cost(
    gift_value: Won,
    ruleset: RuleSet,
    *,
    on: date,
    track: Track = Track.CURRENT,
    regulated_heavy: bool | None = None,
    prior_gifts_10y: Won = 0,
    subject: SubjectRef | None = None,
) -> GiftCost:
    """배우자에게 `gift_value`만큼 증여할 때의 증여세 + 취득세.

    `regulated_heavy`
        조정대상지역 + 일정가액 이상 주택의 무상취득 중과(12%) 해당 여부.
        '일정가액'이 시행령 소관이라 **엔진이 판정하지 않는다.** None이면
        표준세율로 계산하되 중과 시 금액을 대안으로 함께 낸다 —
        확인 못 한 것을 확인한 척하지 않는다.

    `prior_gifts_10y`
        10년 이내에 이미 공제받은 금액(§53① 후단). 입력받지 못하면 0으로 두되
        화면이 그 가정을 밝힌다.
    """
    subject = subject or SubjectRef.case()
    children: list[TraceNode] = []

    # ── 증여재산공제 ────────────────────────────────────────────────
    ded_res = ruleset.resolve(f"{G}.spouse.deduction", on=on, track=track)
    cap = int(ded_res.block.value)
    deduction = max(0, min(gift_value, cap - max(0, prior_gifts_10y)))
    children.append(
        node(
            "gf.01.deduction",
            "배우자 증여재산 공제",
            derive_value(deduction, ded_res.ref(), label="증여재산공제"),
            subject=subject,
            rules=(ded_res.ref(),),
            formula="min(증여가액, 6억원 − 10년 내 기공제액)",
            substitution=(
                f"min({gift_value:,}, {cap:,} − {max(0, prior_gifts_10y):,}) = {deduction:,}"
            ),
            note_ko=(
                "10년간 합산 한도입니다(상증세법 §53① 후단). 배우자에게 이미 증여한 "
                "이력이 있으면 그만큼 줄어듭니다 — 이 계산은 기공제액을 "
                f"{max(0, prior_gifts_10y):,}원으로 보았습니다."
            ),
        )
    )

    base = max(0, gift_value - deduction)

    # ── 산출세액 → 신고세액공제 ─────────────────────────────────────
    rate_res = ruleset.resolve(f"{G}.rate_table", on=on, track=track)
    # ★ 누진세율 적용은 `RateTable.tax_for`가 이미 한다. 여기서 다시 짜면
    #   정의가 둘이 되고, 한쪽만 고치는 날이 온다 — 오늘 하루 고친 실수 그대로다.
    #   대입식 형식까지 프로젝트 표준을 따르므로 화면도 일관된다.
    gross, _bracket, gross_sub = rate_res.block.table.tax_for(base)

    credit_res = ruleset.resolve(f"{G}.filing_credit", on=on, track=track)
    credit_rate = credit_res.block.as_fraction()
    credit = int(gross * credit_rate)
    gift_tax = gross - credit

    children.append(
        node(
            "gf.02.gift_tax",
            "증여세",
            derive_value(gift_tax, rate_res.ref(), credit_res.ref(), label="증여세"),
            subject=subject,
            rules=(rate_res.ref(), credit_res.ref()),
            formula="(증여가액 − 공제) × 누진세율 − 신고세액공제 3%",
            substitution=(
                f"과세표준 {base:,} → {gross_sub}"
                + (f" → 신고세액공제 {credit:,} 차감 = {gift_tax:,}" if credit else "")
            ),
            branch=BranchRecord(
                condition_ko="증여세 발생 여부",
                taken="공제 범위 내 — 증여세 없음" if base == 0 else f"과세표준 {base:,}원",
            ),
            note_ko=(
                "신고세액공제는 **기한 내 신고**가 전제입니다 — 증여일이 속하는 달의 "
                "말일부터 3개월 이내(상증세법 §68①)."
            ),
        )
    )

    # ── 취득세 ─────────────────────────────────────────────────────
    heavy = bool(regulated_heavy)
    acq_res = ruleset.resolve(
        f"{G}.acquisition_tax", on=on, track=track, regulated_heavy=heavy
    )
    acq_rate = acq_res.block.as_fraction()
    acquisition_tax = int(gift_value * acq_rate)

    alternatives: tuple[Alternative, ...] = ()
    if regulated_heavy is None:
        other = ruleset.resolve(
            f"{G}.acquisition_tax", on=on, track=track, regulated_heavy=True
        )
        heavy_amount = int(gift_value * other.block.as_fraction())
        alternatives = (
            Alternative(
                key="gift_acquisition_heavy",
                label_ko="조정대상지역 증여 취득세 중과(지방세법 §13의2②)",
                reason_ko=(
                    "조정대상지역의 **일정가액 이상** 주택을 증여받으면 취득세가 12%로 "
                    f"중과됩니다. 해당되면 {heavy_amount:,}원입니다"
                    f"({acquisition_tax:,}원이 아니라). '일정가액'은 시행령 소관이라 "
                    "이 엔진이 판정하지 않았습니다 — 확인이 필요합니다"
                ),
                delta=Value.money(heavy_amount - acquisition_tax),
                actionable=True,
            ),
        )

    children.append(
        node(
            "gf.03.acquisition_tax",
            "취득세 (증여받는 배우자가 부담)",
            derive_value(acquisition_tax, acq_res.ref(), label="취득세"),
            subject=subject,
            rules=(acq_res.ref(),),
            formula="증여가액 × 무상취득 세율",
            substitution=f"{gift_value:,} × {float(acq_rate):.1%} = {acquisition_tax:,}",
            note_ko="지방교육세·농어촌특별세가 별도로 붙습니다. 여기에는 취득세 본세만 담았습니다.",
            alternatives_not_taken=alternatives,
        )
    )

    total = gift_tax + acquisition_tax
    return GiftCost(
        gift_value=gift_value,
        deduction=deduction,
        taxable_base=base,
        gift_tax=gift_tax,
        acquisition_tax=acquisition_tax,
        trace=node(
            "gf.00.spouse_gift",
            "배우자 증여 비용",
            Value.money(total, label="증여 비용 합계"),
            subject=subject,
            formula="증여세 + 취득세",
            substitution=f"{gift_tax:,} + {acquisition_tax:,} = {total:,}",
            children=tuple(children),
        ),
    )


def carryover_years(ruleset: RuleSet, *, on: date, track: Track = Track.CURRENT) -> int:
    """이월과세 기간(소득세법 §97의2①). 2023 개정으로 5년 → 10년이 됐다."""
    return int(ruleset.resolve(f"{G}.spouse_carryover_years", on=on, track=track).block.value)


__all__ = ["GiftCost", "carryover_years", "compute_spouse_gift_cost"]
