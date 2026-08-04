"""종합부동산세 주택분 계산.

재산세와 결정적으로 다른 점: **인별 과세 + 세대별 판정**의 이중 구조다.
세액은 사람마다 따로 매기지만(인별 합산), 1세대1주택인지는 세대 전체를 봐야 안다.
이 이중성이 물건 배열만 있는 데이터 모델로는 표현되지 않는다.

계산 구조 (종합부동산세법)
    인별 합산 공시가격 (지분 반영)
      − 기본공제                     §8①
      × 공정시장가액비율              §8①, 시행령 §2의4
      = 과세표준
      × 세율                         §9①②
      = 주택분 종합부동산세액
      − 재산세 공제                   §9③, 시행령 §4의3   ← 재현의 관건
      − 세액공제 (연령 + 보유/거주)     §9⑥⑦⑧
      → 세부담 상한 적용               §10
      + 농어촌특별세 20%              농특세법 §5①
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from fractions import Fraction
from typing import Mapping, Sequence

from ..domain.certainty import Certainty, DeterminationQuality
from ..domain.models import (
    PersonId,
    PersonType,
    PropertyId,
    TaxCase,
    TaxYear,
    Won,
)
from ..rules.resolver import MissingRule, RuleSet
from ..rules.schema import RateTable, Track
from .special_houses import SpecialAssessment, SpecialKind, assess, special_trace
from .property_tax import (
    PropertyTaxOptions,
    PropertyTaxResult,
    compute_property_tax,
    price_band,
)
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

J = "jongbuse.house"
PT = "property_tax.house"


@dataclass(frozen=True, slots=True)
class JongbuseOptions:
    """엔진이 사실에서 못 뽑는 값. 전부 기본값이 '유리하지 않은 쪽'이다."""

    residence_years: int | None = None
    """거주기간(년). 개편안의 거주공제 계산에 쓴다. None이면 거주 이력에서 도출."""

    holding_years: int | None = None
    """보유기간(년). None이면 취득일에서 도출."""

    resides_in_main_house: bool | None = None
    """거주용 1주택 여부. 개편안 기본공제 14억/9억을 가른다."""

    prior_year_total_tax: Won | None = None
    """직전연도 보유세 총액. 세부담상한 계산에 필요하다."""

    property_tax_options: PropertyTaxOptions | None = None

    joint_spouse_election: bool = False
    """부부공동명의 1주택자 특례(종부세법 §10의2) 신청 여부.
    신청하면 배우자 지분을 합산해 1인이 1세대1주택자로 과세된다."""


@dataclass(frozen=True, slots=True)
class JointSpouseStatus:
    """부부공동명의 1주택자 특례 적용 가능 여부(종부세법 §10의2, 시행령 §5의2②).

    요건: 세대원 중 1명과 그 배우자**만이** 주택분 재산세 과세대상 1주택만 소유하고,
    둘 다 소득세법상 거주자일 것.
    """

    eligible: bool
    spouse_id: PersonId | None = None
    property_id: PropertyId | None = None
    reason_ko: str = ""


@dataclass(frozen=True, slots=True)
class JongbuseResult:
    person_id: PersonId
    year: TaxYear
    taxable_base: Value
    gross_tax: Value
    property_tax_credit: Value
    tax_credit: Value
    net_tax: Value
    surtax: Value
    total: Value
    property_tax_total: Value
    trace: TraceNode

    @property
    def holding_tax_total(self) -> Won:
        """보유세 = 재산세 + 종부세(농특세 포함). 정부 문답자료의 '보유세' 행과 같은 정의."""
        return self.property_tax_total.as_int() + self.total.as_int()


def compute_jongbuse(
    case: TaxCase,
    person_id: PersonId,
    ruleset: RuleSet,
    *,
    track: Track = Track.CURRENT,
    options: JongbuseOptions | None = None,
) -> JongbuseResult:
    options = options or JongbuseOptions()
    on = case.assessment_date
    person = case.find_person(person_id)
    # ★ '법인'을 한 덩어리로 보면 공익법인등이 잘못 계산된다(2026-08-04 감사).
    #
    #   종부세법 §8①  2호  "제9조제2항제3호 각 목의 세율이 적용되는 법인": 기본공제 0원
    #                 3호  "제1호 및 제2호에 해당하지 아니하는 자": 9억원
    #
    #   즉 기본공제 0원과 단일세율은 **§9②3호 세율이 적용되는 법인에 한정**된다.
    #   공익법인등(사회복지·학교법인이 사택·기숙사를 보유하는 흔한 구성)은 그 호에
    #   해당하지 않으므로 §8①3호로 9억원을 공제받고, §9① 누진세율을 쓰며,
    #   §10 세부담상한도 적용받는다 — 이 세 가지 모두 **개인과 같다.**
    #   다른 점은 세대가 없어 1세대1주택자 판정이 없다는 것뿐이다.
    #
    #   그래서 룰셋 블록을 복제하지 않고 selector를 individual로 보낸다.
    #   '법인이니까 corporation'이라는 직관이 조문과 어긋나는 자리다.
    progressive_corp = person.type is PersonType.CORPORATION_PROGRESSIVE
    taxpayer = (
        "corporation" if person.is_corporation and not progressive_corp else "individual"
    )
    children: list[TraceNode] = []
    subject = SubjectRef(SubjectType.PERSON, str(person_id), person.name or str(person_id))

    # ── 03~04. 특례를 반영한 주택 수 판정 → 1세대1주택 여부 ──────────
    # 상속·일시적2주택·지방저가·합산배제를 반영해 다시 센다.
    # 이걸 안 하면 상속받은 집 하나 때문에 1세대1주택 특례를 통째로 잃는다.
    house_owned = [
        o for o in case.ownerships_of(person_id)
        if case.find_property(o.property_id).is_house
    ]
    assessment = assess(case, person_id, ruleset, track=track, on=on)
    children.append(special_trace(case, assessment, subject))

    # 종부세법 §8④의 1세대1주택자는 '세대원 중 1명만이 1주택을 단독 소유'하는 경우다.
    # 부부공동명의는 §10의2 특례를 신청해야 1세대1주택자로 본다.
    counted_owned = [o for o in house_owned if o.property_id in assessment.counted]
    solely_owned = all(o.share == 1 for o in counted_owned)
    joint = _joint_spouse_status(case, person_id)
    elected = options.joint_spouse_election and joint.eligible

    one_house = (
        False
        if person.is_corporation
        else elected or (assessment.is_one_house and len(counted_owned) == 1 and solely_owned)
    )

    children.append(
        _house_count_node(case, person_id, assessment, one_house, joint, elected, subject)
    )

    # ── 재산세 먼저 계산한다. 종부세가 재산세를 입력으로 쓰기 때문이다. ──
    # 지분 소유면 재산세도 지분만큼만 부과되므로 안분한다.
    # 다만 부부공동명의 특례를 신청하면 시행령 §5의2⑦에 따라 **1주택 지분 전체**를 쓴다.
    # 합산배제 주택(등록임대 등)은 과세표준에서 빠지므로 종부세 계산에서 제외한다.
    # 재산세는 그대로 부과되지만 종부세의 재산세 공제 대상은 아니다.
    aggregate_excluded = assessment.aggregate_excluded()
    pt_results = _compute_property_taxes(
        case, person_id, ruleset, track, options,
        elected=elected, skip=aggregate_excluded,
    )
    children.append(_property_tax_summary_node(pt_results, subject))

    # ── 05. 인별 합산 공시가격 (지분 반영) ──────────────────────────
    assessed, price_node = _sum_published_price(
        case, person_id, subject, elected=elected, skip=aggregate_excluded
    )
    children.append(price_node)

    # ── 05b. 과세대상 판정 (개편안 §7① 신설) ───────────────────────
    # ★ 개편안은 과세대상 문턱과 기본공제를 **분리**한다. 비거주 1주택은
    #   문턱 14억 / 기본공제 9억이라 9억~14억 구간에서 둘이 어긋난다.
    #   기본공제만 보고 계산하면 과세대상이 아닌 사람에게 세금을 매긴다.
    resides = _resides(options, one_house)
    threshold_node = _taxable_threshold(
        ruleset, on, track, taxpayer, one_house, assessed, subject
    )
    if threshold_node is not None:
        children.append(threshold_node)
        if threshold_node.output.as_int() == 0:  # 문턱 이하 → 종부세 없음
            return _not_taxable_result(
                case, person_id, person, track, subject, children, pt_results
            )

    # ── 06. 기본공제 ───────────────────────────────────────────────
    deduction, ded_node = _basic_deduction(
        ruleset, on, track, taxpayer, one_house, resides, case, person_id, subject
    )
    children.append(ded_node)

    # ── 07. 공정시장가액비율 → 과세표준 ─────────────────────────────
    # 종부령 §4의3③ — 세율표·공정시장가액비율은 **납세의무자 본인** 주택 수로 고른다.
    # 세대 주택 수(assessment.count)를 쓰면 부부가 각자 2채씩 가진 경우
    # 각자에게 3주택 중과세율표가 붙는다(2026-08-04 감사).
    rate_count = assessment.personal_count
    heavy = _is_heavy_group(rate_count, one_house)
    fmv_res = ruleset.resolve(
        f"{J}.fair_market_ratio", on=on, track=track, heavy_group=heavy
    )
    fmv = fmv_res.block.as_fraction()
    raw_base = max(0, assessed.as_int() - deduction.as_int())
    base_amount = int(raw_base * fmv)
    taxable_base = derive_value(
        base_amount, assessed, deduction, fmv_res.ref(), label="과세표준"
    )
    children.append(
        node(
            "jb.07.taxable_base",
            "과세표준",
            taxable_base,
            subject=subject,
            inputs=(("합산 공시가격", assessed), ("기본공제", deduction)),
            rules=(fmv_res.ref(),),
            formula="max(0, 합산 공시가격 − 기본공제) × 공정시장가액비율",
            substitution=(
                f"max(0, {assessed.as_int():,} − {deduction.as_int():,}) "
                f"× {float(fmv) * 100:g}% = {base_amount:,}"
            ),
        )
    )

    # ── 08. 세율 → 종부세액 ────────────────────────────────────────
    house_group = "1-2" if rate_count <= 2 else "3+"
    rate_res = ruleset.resolve(
        f"{J}.rate_table", on=on, track=track, taxpayer=taxpayer, house_group=house_group
    )
    table = rate_res.block.table
    assert table is not None
    gross_amount, _, gross_sub = table.tax_for(base_amount)
    gross_tax = derive_value(gross_amount, taxable_base, rate_res.ref(), label="종부세액")
    children.append(
        node(
            "jb.08.gross_tax",
            "주택분 종합부동산세액",
            gross_tax,
            subject=subject,
            inputs=(("과세표준", taxable_base),),
            rules=(rate_res.ref(),),
            formula="과세표준 구간별 세율 적용",
            substitution=gross_sub,
            branch=BranchRecord(
                condition_ko="세율표 선택",
                taken=f"{house_group} 주택",
                detail_ko=(
                    f"본인 주택 수 {rate_count}채"
                    + (f" (세대 {assessment.count}채)" if assessment.count != rate_count else "")
                ),
            ),
        )
    )

    # ── 09. 재산세 공제 ────────────────────────────────────────────
    ptc, ptc_node = _property_tax_credit(
        ruleset, on, track, case, person_id, pt_results,
        base_amount, one_house, gross_tax, subject,
    )
    children.append(ptc_node)

    after_ptc = max(0, gross_tax.as_int() - ptc.as_int())

    # ── 10. 세액공제 (연령 + 보유/거주) ─────────────────────────────
    # ★ 종부세법 §9⑦⑨ — §8④ 특례주택(상속·일시적2주택·지방저가·부속토지)이 있으면
    #   "산출된 세액에서 그 주택분 산출세액(**공시가격합계액으로 안분**)을 제외한
    #   금액"에만 공제율을 곱한다. 전액에 곱하면 특례주택분까지 공제받는다.
    credit_base, credit_ratio = _credit_base(
        case, person_id, assessment, after_ptc,
        elected=elected, skip=aggregate_excluded,
    )
    credit, credit_node = _tax_credit(
        ruleset, on, track, case, person_id, one_house, credit_base, options, subject,
        full_tax=after_ptc, ratio=credit_ratio,
    )
    children.append(credit_node)

    net_amount = max(0, after_ptc - credit.as_int())
    net_tax = derive_value(net_amount, gross_tax, ptc, credit, label="결정세액")
    children.append(
        node(
            "jb.10.net_tax",
            "결정세액",
            net_tax,
            subject=subject,
            formula="종부세액 − 재산세 공제 − 세액공제",
            substitution=(
                f"{gross_tax.as_int():,} − {ptc.as_int():,} "
                f"− {credit.as_int():,} = {net_amount:,}"
            ),
        )
    )

    # ── 11. 세부담 상한 ────────────────────────────────────────────
    net_tax, cap_node = _burden_cap(
        ruleset, on, track, taxpayer, net_tax, pt_results, options, subject
    )
    children.append(cap_node)

    # ── 12. 농어촌특별세 ───────────────────────────────────────────
    surtax_res = ruleset.resolve(f"{J.split('.')[0]}.surtax_rate", on=on, track=track)
    surtax_rate = surtax_res.block.as_fraction()
    surtax_amount = int(net_tax.as_int() * surtax_rate)
    surtax = derive_value(surtax_amount, net_tax, surtax_res.ref(), label="농어촌특별세")
    children.append(
        node(
            "jb.12.surtax",
            "농어촌특별세",
            surtax,
            subject=subject,
            rules=(surtax_res.ref(),),
            formula="종합부동산세 결정세액 × 20%",
            substitution=f"{net_tax.as_int():,} × {float(surtax_rate) * 100:g}%",
        )
    )

    total_amount = net_tax.as_int() + surtax_amount
    total = derive_value(total_amount, net_tax, surtax, label="종부세 합계")

    pt_total = (
        derive_value(
            _pt_total(pt_results),
            *(r.total for r, _ in pt_results.values()),
            label="재산세 합계",
        )
        if pt_results
        else Value.money(0, label="재산세 합계")
    )

    trace = node(  # noqa: F841 — 아래 JongbuseResult로 넘어간다
        "jb.00.jongbuse",
        f"종합부동산세 — {person.name or person_id} ({case.year}년, {track})",
        total,
        subject=subject,
        formula="결정세액 + 농어촌특별세",
        substitution=f"{net_tax.as_int():,} + {surtax_amount:,} = {total_amount:,}",
        children=tuple(children),
        note_ko=(
            "개정안 기준입니다. 국회 통과 전이므로 확정된 세액이 아닙니다."
            if track is Track.REFORM
            else ""
        ),
    )

    return JongbuseResult(
        person_id=person_id,
        year=case.year,
        taxable_base=taxable_base,
        gross_tax=gross_tax,
        property_tax_credit=ptc,
        tax_credit=credit,
        net_tax=net_tax,
        surtax=surtax,
        total=total,
        property_tax_total=pt_total,
        trace=trace,
    )


@dataclass(frozen=True, slots=True)
class JointSpouseComparison:
    """부부공동명의 1주택 특례 신청 / 미신청 비교.

    "신청하는 게 유리한가"는 세액공제까지 포함한 **완전 계산 2회**를 해봐야만 안다.
    연령·보유기간 공제가 붙는 쪽(신청)이 대개 유리하지만, 지분이 나뉘어 각자
    기본공제를 받는 쪽(미신청)이 이기는 구간도 있다. 그래서 추측하지 않고 둘 다 계산한다.
    """

    eligible: bool
    reason_ko: str
    elected: JongbuseResult | None
    """특례 신청 — 지정된 1인이 배우자 지분까지 합산해 납부."""
    not_elected: tuple[JongbuseResult, ...]
    """특례 미신청 — 부부가 각자 지분만큼 납부."""

    @property
    def elected_total(self) -> Won:
        return self.elected.total.as_int() if self.elected else 0

    @property
    def not_elected_total(self) -> Won:
        return sum(r.total.as_int() for r in self.not_elected)

    @property
    def recommended(self) -> str:
        if not self.eligible:
            return "not_elected"
        return "elected" if self.elected_total <= self.not_elected_total else "not_elected"

    @property
    def saving(self) -> Won:
        """권장안을 택했을 때 아끼는 금액."""
        if not self.eligible:
            return 0
        return abs(self.elected_total - self.not_elected_total)


def compare_joint_spouse_election(
    case: TaxCase,
    person_id: PersonId,
    ruleset: RuleSet,
    *,
    track: Track = Track.CURRENT,
    options: JongbuseOptions | None = None,
) -> JointSpouseComparison:
    """부부공동명의 1주택자 특례를 신청할지 말지 계산으로 답한다.

    시중 계산기는 이 특례를 '미반영'으로 두고 사용자에게 판단을 떠넘긴다.
    그런데 사용자는 그걸 판단할 줄 몰라서 계산기에 온 것이다.
    """
    options = options or JongbuseOptions()
    status = _joint_spouse_status(case, person_id)

    def run_for(pid: PersonId, elect: bool) -> JongbuseResult:
        return compute_jongbuse(
            case,
            pid,
            ruleset,
            track=track,
            options=replace(options, joint_spouse_election=elect),
        )

    separate = [run_for(person_id, False)]
    if status.eligible and status.spouse_id is not None:
        separate.append(run_for(status.spouse_id, False))

    elected = run_for(person_id, True) if status.eligible else None

    return JointSpouseComparison(
        eligible=status.eligible,
        reason_ko=status.reason_ko,
        elected=elected,
        not_elected=tuple(separate),
    )


# --------------------------------------------------------------------------
# 단계별 구현
# --------------------------------------------------------------------------


def _resides(options: JongbuseOptions, one_house: bool) -> bool:
    """거주 여부. 모르면 **거주하지 않는 것으로 본다**.

    개편안에서 거주 여부는 기본공제 14억 vs 9억을 가른다. 모를 때 유리한 쪽으로
    가정하면 세액을 과소평가해 사용자를 오도한다. 대표값은 항상 보수적으로 둔다.
    """
    if options.resides_in_main_house is not None:
        return options.resides_in_main_house
    return False


def _taxable_threshold(
    ruleset: RuleSet,
    on: date,
    track: Track,
    taxpayer: str,
    one_house: bool,
    assessed: Value,
    subject: SubjectRef,
) -> TraceNode | None:
    """과세대상 문턱 판정(종부세법 §7① 개정안).

    값이 1이면 과세대상, 0이면 아니다. 규칙이 없으면 `None` —
    **현행법은 기본공제가 곧 문턱이라 별도 문턱이 없다.** 규칙 부재를
    '문턱 0원'으로 오해하면 전원이 과세대상에서 빠진다.
    """
    try:
        res = ruleset.resolve(
            f"{J}.taxable_threshold", on=on, track=track, taxpayer=taxpayer,
            one_house=one_house,
        )
    except MissingRule:
        return None  # 현행법 — 문턱 개념이 없다

    if res.block.payload.get("applicable") is False:
        return None  # 법인 — 문턱 없음

    threshold = res.block.as_int()
    total = assessed.as_int()
    taxable = total > threshold

    return node(
        "jb.05b.taxable_threshold",
        "과세대상 판정",
        derive_value(
            1 if taxable else 0, assessed, res.ref(), label="과세대상 여부"
        ),
        subject=subject,
        inputs=(("합산 공시가격", assessed),),
        rules=(res.ref(),),
        formula="합산 공시가격이 과세대상 기준금액을 초과하는가",
        substitution=(
            f"{total:,} {'>' if taxable else '≤'} {threshold:,} → "
            + ("과세대상" if taxable else "과세대상 아님 (종부세 0원)")
        ),
        branch=BranchRecord(
            condition_ko="과세대상",
            taken="해당" if taxable else "비해당",
            detail_ko=(
                "1세대1주택자 기준" if one_house else "1세대1주택자 외 기준"
            ),
        ),
        note_ko=(
            None if taxable else
            "개편안은 과세대상 기준금액과 기본공제금액을 따로 둡니다. "
            f"공시가격 합계가 {threshold:,}원 이하이므로 기본공제와 무관하게 "
            "종합부동산세가 부과되지 않습니다(재산세는 그대로 냅니다)."
        ),
    )


def _not_taxable_result(
    case: TaxCase,
    person_id: PersonId,
    person,
    track: Track,
    subject: SubjectRef,
    children: list[TraceNode],
    pt_results,
) -> "JongbuseResult":
    """과세대상이 아닐 때의 결과. 재산세는 그대로 살아 있다."""
    zero = Value.money(0, label="종합부동산세")
    pt_total = (
        derive_value(
            _pt_total(pt_results),
            *(r.total for r, _ in pt_results.values()),
            label="재산세 합계",
        )
        if pt_results
        else Value.money(0, label="재산세 합계")
    )
    trace = node(
        "jb.00.jongbuse",
        f"종합부동산세 — {person.name or person_id} ({case.year}년, {track})",
        zero,
        subject=subject,
        formula="과세대상 아님",
        substitution="공시가격 합계가 과세대상 기준금액 이하 → 종부세 0원",
        children=tuple(children),
        note_ko=(
            "개정안 기준입니다. 국회 통과 전이므로 확정된 결과가 아닙니다."
            if track is Track.REFORM
            else ""
        ),
    )
    return JongbuseResult(
        person_id=person_id,
        year=case.year,
        taxable_base=Value.money(0, label="과세표준"),
        gross_tax=zero,
        property_tax_credit=Value.money(0, label="재산세 공제"),
        tax_credit=Value.money(0, label="세액공제"),
        net_tax=zero,
        surtax=Value.money(0, label="농어촌특별세"),
        total=zero,
        property_tax_total=pt_total,
        trace=trace,
    )


def _is_heavy_group(house_count: int, one_house: bool) -> bool:
    """개편안 2028년 공정시장가액비율 80% 대상 여부.

    조건은 '3주택 이상 보유자 및 조정대상지역 주택 보유자(1세대1주택자 제외)'다.
    조정대상지역은 공공데이터가 존재하지 않아 별도 고시 테이블이 필요하다(Phase 3).
    여기서는 주택 수만으로 판정하고, 조정대상지역분은 아직 반영하지 않는다.
    """
    return house_count >= 3 and not one_house


def _joint_spouse_status(case: TaxCase, person_id: PersonId) -> JointSpouseStatus:
    """부부공동명의 1주택자 특례 요건 판정(종부세법 §10의2, 시행령 §5의2②).

    '세대원 중 1명과 그 배우자만이 주택분 재산세 과세대상 1주택만 소유'가 요건이다.
    부부가 각자 다른 집을 1채씩 가진 경우는 여기 해당하지 않는다 — 그건 1세대 2주택이다.
    두 상황을 구분하지 못한 것이 시중 계산기가 "지원하지 않는다"고 자백한 지점이다.
    """
    person = case.find_person(person_id)
    if person.spouse_id is None:
        return JointSpouseStatus(False, reason_ko="배우자 정보가 없다")

    try:
        spouse = case.find_person(person.spouse_id)
    except KeyError:
        return JointSpouseStatus(False, reason_ko="배우자가 사건에 등록돼 있지 않다")

    house_ids = {
        o.property_id
        for o in case.ownerships
        if case.find_property(o.property_id).is_house
        and o.person_id in {person_id, spouse.id}
    }
    if len(house_ids) != 1:
        return JointSpouseStatus(
            False,
            reason_ko=f"부부가 소유한 주택이 {len(house_ids)}채로 1채가 아니다",
        )

    prop_id = next(iter(house_ids))
    owners = {o.person_id for o in case.owners_of(prop_id)}
    if owners != {person_id, spouse.id}:
        return JointSpouseStatus(
            False, property_id=prop_id, reason_ko="해당 주택을 부부가 공동으로 소유하고 있지 않다"
        )

    # 세대 내 다른 구성원이 주택을 소유하면 '1명과 그 배우자만' 요건이 깨진다
    member_ids = set(case.household_member_ids(person_id)) - {person_id, spouse.id}
    if any(
        o.person_id in member_ids and case.find_property(o.property_id).is_house
        for o in case.ownerships
    ):
        return JointSpouseStatus(
            False, property_id=prop_id, reason_ko="세대 내 다른 구성원도 주택을 소유하고 있다"
        )

    if not (person.is_resident and spouse.is_resident):
        return JointSpouseStatus(
            False, property_id=prop_id, reason_ko="부부 모두 소득세법상 거주자여야 한다"
        )

    return JointSpouseStatus(True, spouse_id=spouse.id, property_id=prop_id)


def _house_count_node(
    case: TaxCase,
    person_id: PersonId,
    assessment: SpecialAssessment,
    one_house: bool,
    joint: JointSpouseStatus,
    elected: bool,
    subject: SubjectRef,
) -> TraceNode:
    alternatives: tuple[Alternative, ...] = ()
    if joint.eligible and not elected:
        alternatives = (
            Alternative(
                key="joint_spouse_special",
                label_ko="부부공동명의 1주택자 특례(종부세법 §10의2)",
                reason_ko=(
                    "요건은 충족하나 신청하지 않은 것으로 계산했다. 신청하면 배우자 지분을 "
                    "합산해 1세대1주택자로 과세되며 기본공제와 세액공제가 달라진다"
                ),
                actionable=True,
            ),
        )
    elif not one_house and assessment.is_one_house and not joint.eligible:
        alternatives = (
            Alternative(
                key="joint_spouse_special",
                label_ko="부부공동명의 1주택자 특례(종부세법 §10의2)",
                reason_ko=f"특례 요건 미충족: {joint.reason_ko}",
            ),
        )

    taken = "1세대1주택자"
    if elected:
        taken = "1세대1주택자 (부부공동명의 특례 신청)"
    elif not one_house:
        taken = "그 외"

    return node(
        "jb.03.house_count",
        "1세대1주택자 판정",
        Value.flag(one_house, certainty=assessment.certainty, label="1세대1주택자"),
        subject=subject,
        formula="세대 주택 수 1채 + 단독 소유 (또는 부부공동명의 특례 신청)",
        substitution=f"세대 주택 {assessment.count}채, 본인 단독소유 여부 판정",
        branch=BranchRecord(condition_ko="1세대1주택자 해당 여부", taken=taken),
        alternatives_not_taken=alternatives,
    )


def _effective_ownerships(
    case: TaxCase,
    person_id: PersonId,
    *,
    elected: bool,
    skip: frozenset[PropertyId] = frozenset(),
) -> list[tuple[PropertyId, Fraction]]:
    """계산에 쓸 (물건, 실효지분) 목록.

    부부공동명의 특례를 신청하면 배우자 지분까지 합산한다(시행령 §5의2⑥).
    신청하지 않으면 본인 지분만 — 재산세도 종부세도 지분만큼만 부담한다.
    `skip`은 합산배제 주택 — 과세표준에 들어가지 않는다.
    """
    own = [
        (o.property_id, o.share)
        for o in case.ownerships_of(person_id)
        if case.find_property(o.property_id).is_house and o.property_id not in skip
    ]
    if not elected:
        return own

    person = case.find_person(person_id)
    merged: dict[PropertyId, Fraction] = {pid: share for pid, share in own}
    if person.spouse_id is not None:
        for o in case.ownerships_of(person.spouse_id):
            if case.find_property(o.property_id).is_house and o.property_id not in skip:
                merged[o.property_id] = merged.get(o.property_id, Fraction(0)) + o.share
    return list(merged.items())


def _compute_property_taxes(
    case: TaxCase,
    person_id: PersonId,
    ruleset: RuleSet,
    track: Track,
    options: JongbuseOptions,
    *,
    elected: bool = False,
    skip: frozenset[PropertyId] = frozenset(),
) -> dict[PropertyId, tuple[PropertyTaxResult, Fraction]]:
    """본인이 부담하는 주택 재산세를 물건별로 계산하고 지분을 함께 돌려준다.

    재산세는 물건별로 산출한 뒤 지분에 따라 안분된다. 지분을 적용하지 않으면
    1/2 소유자에게 물건 전체 세액이 잡혀 종부세 재산세공제가 과다해진다.

    종부세가 재산세를 입력으로 쓰므로(§9③ 공제, §10 세부담상한) 순서를 뒤집을 수 없다.
    """
    results: dict[PropertyId, tuple[PropertyTaxResult, Fraction]] = {}
    for prop_id, share in _effective_ownerships(case, person_id, elected=elected, skip=skip):
        results[prop_id] = (
            compute_property_tax(
                case,
                prop_id,
                ruleset,
                track=track,
                owner_id=person_id,
                options=options.property_tax_options,
            ),
            share,
        )
    return results


def _pt_total(results: Mapping[PropertyId, tuple[PropertyTaxResult, Fraction]]) -> int:
    return sum(int(r.total.as_int() * share) for r, share in results.values())


def _property_tax_summary_node(
    results: Mapping[PropertyId, tuple[PropertyTaxResult, Fraction]],
    subject: SubjectRef,
) -> TraceNode:
    total = _pt_total(results)
    parts = [
        f"{r.total.as_int():,}" + ("" if share == 1 else f" × {share}")
        for r, share in results.values()
    ]
    return node(
        "jb.02.property_tax",
        "재산세 (종부세 계산의 입력)",
        Value.money(total, label="재산세 합계"),
        subject=subject,
        formula="물건별 재산세 × 본인 지분의 합",
        substitution=" + ".join(parts) or "0",
        children=tuple(r.trace for r, _ in results.values()),
    )


def _sum_published_price(
    case: TaxCase,
    person_id: PersonId,
    subject: SubjectRef,
    *,
    elected: bool = False,
    skip: frozenset[PropertyId] = frozenset(),
) -> tuple[Value, TraceNode]:
    """인별 합산 공시가격. 지분만큼만 합산한다(Fraction이라 반올림 손실 없음)."""
    total = 0
    parts: list[str] = []
    missing = False
    for prop_id, share in _effective_ownerships(case, person_id, elected=elected, skip=skip):
        prop = case.find_property(prop_id)
        fact = prop.price_for(case.year)
        if fact is None:
            missing = True
            continue
        amount = int(fact.value * share)
        total += amount
        label = prop.display_name or str(prop.id)
        share_txt = "" if share == 1 else f" × {share}"
        parts.append(f"{label} {fact.value:,}{share_txt}")

    value = (
        Value.missing(UnknownReason.MISSING_INPUT, label="합산 공시가격")
        if missing
        else Value.money(total, label="합산 공시가격")
    )
    return value, node(
        "jb.05.assessed_value",
        "인별 합산 공시가격",
        value,
        subject=subject,
        formula="본인 지분에 해당하는 주택 공시가격의 합",
        substitution=" + ".join(parts) + (f" = {total:,}" if parts else "0"),
    )


def _basic_deduction(
    ruleset: RuleSet,
    on: date,
    track: Track,
    taxpayer: str,
    one_house: bool,
    resides: bool,
    case: TaxCase,
    person_id: PersonId,
    subject: SubjectRef,
) -> tuple[Value, TraceNode]:
    ctx: dict[str, object] = {"taxpayer": taxpayer}
    if taxpayer == "individual":
        ctx["one_house"] = one_house
        if track is Track.REFORM and one_house:
            ctx["resides"] = resides

    res = ruleset.resolve(f"{J}.basic_deduction", on=on, track=track, **ctx)
    block = res.block

    if block.value is not None:
        value = derive_value(int(block.value), res.ref(), label="기본공제")
        return value, node(
            "jb.06.basic_deduction",
            "기본공제",
            value,
            subject=subject,
            rules=(res.ref(),),
            formula=block.note or "기본공제금액",
            substitution=f"{int(block.value):,}",
            branch=BranchRecord(
                condition_ko="1세대1주택자 / 거주 여부",
                taken=f"{'1세대1주택' if one_house else '그 외'}"
                + (f" · {'거주' if resides else '비거주'}" if track is Track.REFORM and one_house else ""),
            ),
        )

    # 개편안 다주택 신공식: 4억 + 5억 × (거주용주택 공시가격 ÷ 주택 공시가격 합계액)
    return _reform_multi_deduction(case, person_id, res, subject)


def _reform_multi_deduction(case, person_id, res, subject) -> tuple[Value, TraceNode]:
    payload = res.block.payload
    base = int(payload["base"])
    bonus = int(payload["residence_bonus"])

    member_ids = set(case.household_member_ids(person_id))
    total_price = 0
    residence_price = 0
    for ownership in case.ownerships:
        if ownership.person_id not in member_ids:
            continue
        prop = case.find_property(ownership.property_id)
        if not prop.is_house:
            continue
        fact = prop.price_for(case.year)
        if fact is None:
            continue
        total_price += fact.value
        if case.residences_of(ownership.person_id, prop.id):
            residence_price += fact.value

    if total_price == 0:
        value = Value.missing(UnknownReason.MISSING_INPUT, label="기본공제")
        share = Fraction(0)
    else:
        share = Fraction(residence_price, total_price)
        value = derive_value(
            base + int(bonus * share), res.ref(), label="기본공제"
        )

    return value, node(
        "jb.06.basic_deduction",
        "기본공제 (개정안 · 거주주택 비중 반영)",
        value,
        subject=subject,
        rules=(res.ref(),),
        formula="4억원 + (5억원 × 거주용주택 공시가격 ÷ 주택 공시가격 합계액)",
        substitution=(
            f"{base:,} + ({bonus:,} × {residence_price:,} ÷ {total_price:,})"
            if total_price
            else "공시가격 미입력"
        ),
        note_ko=(
            "산정 세부기준이 시행령에 위임돼 있어 확정되지 않았습니다. "
            "거주주택이 2채 이상인 경우, 지분 반영 시점 등 해석이 갈릴 수 있습니다."
        ),
    )


def _property_tax_credit(
    ruleset: RuleSet,
    on: date,
    track: Track,
    case: TaxCase,
    person_id: PersonId,
    pt_results: Mapping[PropertyId, tuple[PropertyTaxResult, Fraction]],
    taxable_base: int,
    one_house: bool,
    gross_tax: Value,
    subject: SubjectRef,
) -> tuple[Value, TraceNode]:
    """주택분 종부세에서 공제되는 재산세액(종부세법 §9③, 시행령 §4의3①).

                                    (종부세 과세표준 × 재산세 FMV) × 재산세 표준세율
      공제액 = 부과된 재산세 합계 × ────────────────────────────────────────
                                    주택 합산액을 재산세 표준세율로 계산한 상당액

    분자의 '표준세율'은 누진 전체가 아니라 **해당 구간의 한계세율**로 적용해야
    정부 문답자료 p.44의 세 값이 동시에 재현된다. 근거는 재현 결과이므로 assumed다.
    """
    method = ruleset.resolve(f"{J}.property_tax_credit_method", on=on, track=track)
    std = ruleset.resolve(f"{PT}.rate_table_standard", on=on, track=track)
    std_table = std.block.table
    assert std_table is not None

    # 인별 합산 공시가격(지분 반영)과 재산세 공정시장가액비율.
    # 분자·분모·부과세액을 모두 같은 지분 기준으로 맞춰야 비율이 왜곡되지 않는다.
    total_price = 0
    for pid, (_, share) in pt_results.items():
        fact = case.find_property(pid).price_for(case.year)
        if fact is not None:
            total_price += int(fact.value * share)

    pt_fmv_res = ruleset.resolve(
        f"{PT}.fair_market_ratio",
        on=on,
        track=track,
        one_house_special=one_house,
        price_band=price_band(total_price),
    )
    pt_fmv = pt_fmv_res.block.as_fraction()

    imposed = sum(int(r.base_tax.as_int() * share) for r, share in pt_results.values())
    denominator, _, _ = std_table.tax_for(int(total_price * pt_fmv))

    numerator_base = int(taxable_base * pt_fmv)
    marginal = std_table.bracket_for(numerator_base).rate
    numerator = int(numerator_base * marginal)

    if denominator <= 0:
        amount = 0
        substitution = "재산세 상당액 0 → 공제 없음"
    else:
        amount = numerator * imposed // denominator
        substitution = (
            f"{imposed:,} × ({taxable_base:,} × {float(pt_fmv) * 100:g}% "
            f"× {float(marginal) * 100:g}%) ÷ {denominator:,} = {amount:,}"
        )

    value = derive_value(
        amount, gross_tax, method.ref(), std.ref(), pt_fmv_res.ref(), label="재산세 공제"
    )
    return value, node(
        "jb.09.property_tax_credit",
        "재산세 공제",
        value,
        subject=subject,
        rules=(method.ref(), std.ref(), pt_fmv_res.ref()),
        formula=(
            "부과된 재산세 × (종부세 과세표준 × 재산세 공정시장가액비율 × 재산세 표준세율) "
            "÷ 주택 합산 재산세 상당액"
        ),
        substitution=substitution,
        note_ko=(
            "분자의 재산세 표준세율은 해당 구간의 한계세율로 적용했습니다. "
            "정부 문답자료의 계산 결과와 일치하는 해석입니다."
        ),
    )


def _tier_rate(tiers: Sequence[Mapping[str, object]], key: str, years: int | None) -> Fraction:
    """구간표에서 해당 값의 공제율. 미달이면 0."""
    if years is None:
        return Fraction(0)
    rate = Fraction(0)
    for tier in tiers:
        if years >= int(tier[key]):  # type: ignore[arg-type]
            rate = Fraction(str(tier["rate"]))
    return rate


# 종부세법 §8④ 각 호 — 주택 수에서는 빼되 **과세표준에는 합산**되는 특례주택.
# §9⑦⑨가 세액공제 기초에서 이 주택분 산출세액을 제외하라고 명시한다.
_ARTICLE_8_4_KINDS = frozenset(
    {SpecialKind.INHERITANCE, SpecialKind.TEMPORARY_TWO, SpecialKind.RURAL_LOW_PRICE}
)


def _credit_base(
    case: TaxCase,
    person_id: PersonId,
    assessment: SpecialAssessment,
    after_ptc: int,
    *,
    elected: bool,
    skip: frozenset[PropertyId],
) -> tuple[int, Fraction]:
    """세액공제의 기초가 되는 산출세액과, 그것이 전체에서 차지하는 비율.

    종부세법 §9⑦: "…에 해당하는 산출세액(**공시가격합계액으로 안분하여 계산한
    금액**을 말한다)을 제외한 금액에 … 공제율을 곱한 금액"

    합산배제 주택(§8②)은 이미 과세표준에 없으므로 분모·분자 양쪽에서 빠진다 —
    `skip`이 그 역할을 한다.
    """
    special_ids = {
        s.property_id for s in assessment.specials if s.kind in _ARTICLE_8_4_KINDS
    }
    if not special_ids:
        return after_ptc, Fraction(1)

    total = 0
    excluded = 0
    for prop_id, share in _effective_ownerships(
        case, person_id, elected=elected, skip=skip
    ):
        fact = case.find_property(prop_id).price_for(case.year)
        if fact is None:
            continue
        amount = int(fact.value * share)
        total += amount
        if prop_id in special_ids:
            excluded += amount

    if total <= 0:
        return after_ptc, Fraction(1)
    ratio = Fraction(total - excluded, total)
    return int(after_ptc * ratio), ratio


def _tax_credit(
    ruleset: RuleSet,
    on: date,
    track: Track,
    case: TaxCase,
    person_id: PersonId,
    one_house: bool,
    after_ptc: int,
    options: JongbuseOptions,
    subject: SubjectRef,
    *,
    full_tax: int | None = None,
    ratio: Fraction = Fraction(1),
) -> tuple[Value, TraceNode]:
    """1세대1주택자 세액공제(연령 + 보유/거주). 1세대1주택자가 아니면 0이다."""
    person = case.find_person(person_id)
    alternatives: list[Alternative] = []

    if not one_house:
        return Value.money(0, label="세액공제"), node(
            "jb.10.tax_credit",
            "세액공제",
            Value.money(0),
            subject=subject,
            substitution="1세대1주택자가 아니므로 적용 없음",
            alternatives_not_taken=(
                Alternative(
                    key="one_house_credit",
                    label_ko="1세대1주택자 세액공제(연령·보유/거주)",
                    reason_ko="1세대1주택자가 아니다",
                ),
            ),
        )

    age = person.age_at(on)
    age_res = ruleset.resolve(f"{J}.credit_age", on=on, track=track)
    age_rate = _tier_rate(age_res.block.payload["tiers"], "min_age", age)
    if age is not None and age < 60:
        alternatives.append(
            Alternative(
                key="age_credit",
                label_ko="연령별 세액공제",
                reason_ko=f"과세기준일 현재 만 {age}세로 60세 미만",
            )
        )

    hold_res = ruleset.resolve(f"{J}.credit_holding", on=on, track=track)
    payload = hold_res.block.payload
    holding_years = options.holding_years
    residence_years = options.residence_years

    mode = payload.get("mode", "holding_only")
    if "tiers" in payload:  # 현행 — 보유공제만
        second_rate = _tier_rate(payload["tiers"], "min_years", holding_years)
        second_label = f"보유 {holding_years}년"
    elif mode == "residence_only":
        second_rate = _tier_rate(payload["residence_tiers"], "min_years", residence_years)
        second_label = f"거주 {residence_years}년"
    else:  # 2027 과도기 — 보유공제의 1/2과 거주공제 중 높은 쪽
        h = _tier_rate(payload["holding_tiers"], "min_years", holding_years)
        r = _tier_rate(payload["residence_tiers"], "min_years", residence_years)
        second_rate = max(h, r)
        second_label = (
            f"보유 {holding_years}년({h}) vs 거주 {residence_years}년({r}) 중 높은 쪽"
        )

    cap_res = ruleset.resolve(f"{J}.credit_rate_cap", on=on, track=track)
    rate_cap = cap_res.block.as_fraction()
    combined = min(age_rate + second_rate, rate_cap)
    raw_credit = int(after_ptc * combined)

    amount_cap_res = ruleset.resolve(f"{J}.credit_amount_cap", on=on, track=track)
    amount_cap = (
        None
        if amount_cap_res.block.payload.get("applicable") is False
        else int(amount_cap_res.block.value)
    )
    credit = raw_credit if amount_cap is None else min(raw_credit, amount_cap)

    if amount_cap is not None and raw_credit > amount_cap:
        alternatives.append(
            Alternative(
                key="credit_amount_cap",
                label_ko="세액공제 금액 한도(개정안 신설)",
                reason_ko=f"공제 산출액 {raw_credit:,}원이 한도 {amount_cap:,}원을 초과",
                delta=Value.money(amount_cap - raw_credit),
            )
        )

    certainty = Certainty()
    if holding_years is None and residence_years is None:
        certainty = certainty & Certainty(determination=DeterminationQuality.UNDECIDABLE)

    value = derive_value(
        credit,
        certainty,
        age_res.ref(),
        hold_res.ref(),
        cap_res.ref(),
        amount_cap_res.ref(),
        label="세액공제",
    )
    return value, node(
        "jb.10.tax_credit",
        "세액공제 (연령 + 보유/거주)",
        value,
        subject=subject,
        rules=(age_res.ref(), hold_res.ref(), cap_res.ref(), amount_cap_res.ref()),
        formula=(
            "(종부세액 − 재산세공제"
            + (" − 특례주택분 산출세액" if ratio != 1 else "")
            + ") × min(연령공제율 + 보유·거주공제율, 80%)"
        ),
        substitution=(
            (
                f"{full_tax:,} × {ratio} = {after_ptc:,} (특례주택분 제외) → "
                if ratio != 1 and full_tax is not None
                else ""
            )
            + f"{after_ptc:,} × min({age_rate} + {second_rate}, {rate_cap}) "
            f"= {raw_credit:,}"
            + (f" → 한도 {amount_cap:,} 적용 = {credit:,}" if credit != raw_credit else "")
        ),
        branch=BranchRecord(
            condition_ko="공제율 구성",
            taken=f"연령 {age}세({age_rate}) + {second_label}",
            detail_ko=f"합계 {combined} (한도 {rate_cap})",
        ),
        note_ko=(
            "상속주택·일시적2주택·지방저가주택이 있으면 그 주택분 산출세액을 "
            "공시가격 비율로 안분해 공제 기초에서 제외합니다(종부세법 §9⑦⑨)."
            if ratio != 1
            else ""
        ),
        alternatives_not_taken=tuple(alternatives),
    )


def _burden_cap(
    ruleset: RuleSet,
    on: date,
    track: Track,
    taxpayer: str,
    net_tax: Value,
    pt_results: Mapping[PropertyId, tuple[PropertyTaxResult, Fraction]],
    options: JongbuseOptions,
    subject: SubjectRef,
) -> tuple[Value, TraceNode]:
    """세부담 상한(§10). 직전연도 보유세를 모르면 적용할 수 없다."""
    res = ruleset.resolve(f"{J}.burden_cap", on=on, track=track, taxpayer=taxpayer)

    if res.block.payload.get("applicable") is False:
        return net_tax, node(
            "jb.11.burden_cap",
            "세부담 상한",
            net_tax,
            subject=subject,
            rules=(res.ref(),),
            substitution="단일세율 법인은 세부담 상한 미적용",
        )

    # ── 0원 붕괴 차단 (2026-08-04 감사) ────────────────────────────
    # 직전연도 과세기준일(6/1) 이후에 취득했으면 작년 고지서가 0원이다.
    # 사용자가 사실대로 0을 넣으면 상한이 0 × 150% = 0이 되어 **종부세가
    # 통째로 사라진다**(실측 2,344만원 소실). 그런데 상한은 급증을 막는
    # 장치이지 신규 취득자를 면세하는 장치가 아니다.
    #
    # 종부령 §5는 신규 취득분의 '총세액상당액'을 별도로 정한다(전년에 보유한
    # 것으로 보고 산정). 그 산정을 구현하기 전까지는 **상한을 적용하지 않고
    # 판정 불가로 남긴다** — 조용히 0을 내는 것보다 낫다.
    if options.prior_year_total_tax is not None and options.prior_year_total_tax <= 0:
        undecided = replace(
            net_tax,
            certainty=net_tax.certainty
            & Certainty(determination=DeterminationQuality.UNDECIDABLE),
        )
        return undecided, node(
            "jb.11.burden_cap",
            "세부담 상한",
            undecided,
            subject=subject,
            rules=(res.ref(),),
            formula="직전연도 보유세 × 상한율을 넘지 않도록 종부세를 제한",
            substitution="직전연도 보유세가 0원 → 상한 적용 보류(0원으로 계산하지 않음)",
            branch=BranchRecord(
                condition_ko="세부담 상한", taken="판정 불가",
            ),
            note_ko=(
                "직전연도 보유세가 0원이면(작년 과세기준일 6월 1일 이후 취득 등) "
                "상한을 그대로 적용할 경우 종부세가 0원이 됩니다. 세부담 상한은 "
                "급증을 막는 장치이지 신규 취득자를 면세하는 장치가 아니므로 "
                "적용을 보류했습니다. 종부세법 시행령 §5의 '총세액상당액' 산정이 "
                "필요한 사안이라 세무서 확인을 권합니다."
            ),
            alternatives_not_taken=(
                Alternative(
                    key="burden_cap",
                    label_ko=f"세부담 상한({float(res.block.as_fraction()) * 100:g}%)",
                    reason_ko="직전연도 보유세가 0원이라 비교 기준이 없다",
                    actionable=True,
                ),
            ),
        )

    if options.prior_year_total_tax is None:
        return net_tax, node(
            "jb.11.burden_cap",
            "세부담 상한",
            net_tax,
            subject=subject,
            rules=(res.ref(),),
            formula="직전연도 보유세 × 상한율을 넘지 않도록 종부세를 제한",
            substitution="직전연도 보유세 미입력 → 상한 미적용",
            note_ko="작년 재산세·종부세 고지서 금액을 알려주면 상한 적용 여부까지 계산합니다.",
            alternatives_not_taken=(
                Alternative(
                    key="burden_cap",
                    label_ko=f"세부담 상한({float(res.block.as_fraction()) * 100:g}%)",
                    reason_ko="직전연도 보유세 총액이 없어 판정하지 못했다",
                    actionable=True,
                ),
            ),
        )

    ratio = res.block.as_fraction()
    pt_total = _pt_total(pt_results)
    ceiling = int(options.prior_year_total_tax * ratio)
    allowed = max(0, ceiling - pt_total)
    capped = min(net_tax.as_int(), allowed)
    applied = capped < net_tax.as_int()

    value = derive_value(capped, net_tax, res.ref(), label="결정세액(상한 적용)")
    return value, node(
        "jb.11.burden_cap",
        "세부담 상한",
        value,
        subject=subject,
        rules=(res.ref(),),
        formula="해당연도 보유세가 직전연도 보유세 × 상한율을 넘지 않도록 종부세를 제한",
        substitution=(
            f"min({net_tax.as_int():,}, "
            f"{options.prior_year_total_tax:,} × {float(ratio) * 100:g}% − {pt_total:,}) "
            f"= {capped:,}"
        ),
        branch=BranchRecord(
            condition_ko="보유세 합계 > 직전연도 × 상한율",
            taken="상한 적용" if applied else "상한 미적용",
        ),
    )
