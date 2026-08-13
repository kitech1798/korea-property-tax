"""매도 시점 최적화 — "언제 팔아야 하나"에 날짜로 답한다.

기존 `strategy.sell_timing`과 다른 점 두 가지. 둘 다 실제 상담 사건에서 필요했다.

  ① **연이 아니라 날짜다.**
     상생임대주택 특례의 양도기한이 2028-01-31이면 "2028년"이라는 답은 틀렸다.
     2028년 2월에 팔면 비과세 12억이 통째로 사라진다.

  ② **세액과 제약을 분리한다.**
     세액이 가장 낮은 달을 고르면 안 된다. 그 달에 팔 수 있어야 고를 수 있다.
     실제 사건에서 구속력이 가장 큰 기한은 세법이 아니라 **주택임대차보호법
     §6①**(갱신거절 통지)에서 나왔다 — 세액 곡선에는 흔적조차 남지 않는다.
     둘을 안 나누면 "2028년 1월까지 팔면 됩니다"라고 답하게 되는데, 그때는 이미
     손쓸 시점이 1년 2개월 전에 지나 있다.

후보 날짜를 어떻게 고르는가
    세액은 날짜에 대해 **계단 함수**다. 값이 바뀌는 곳은 법이 그은 경계뿐이다.
    그래서 균등 격자만 훑지 않고 **경계를 직접 표본에 넣는다** — 각 연도의 1월 1일,
    각 제약의 기한일, 그리고 기한 다음 날. 격자만 쓰면 1월 31일과 2월 1일 사이의
    절벽을 통째로 놓친다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from enum import StrEnum

from ..domain import LeaseOrigin, LeaseSpell, PropertyId, TaxCase, Won
from ..rules import RuleSet, Track
from . import periods
from .periods import plus_months
from .sangsaeng import assess as sangsaeng_assess
from .transfer_tax import TransferEvent, compute_transfer_tax

NOTICE_RULE = "reference.lease_renewal_notice"


class ConstraintKind(StrEnum):
    """제약의 종류. 성격이 다르면 사용자가 할 일도 다르다."""

    BLOCKS = "blocks"
    """이 날짜 전에는 공실로 넘길 수 없다. 매도 자체의 전제."""

    DEADLINE = "deadline"
    """이 날짜까지 무언가 **해야 한다**. 놓치면 되돌릴 수 없다."""

    RISK = "risk"
    """확인하지 않으면 답이 갈린다. 엔진이 단정하지 않는 자리."""


@dataclass(frozen=True, slots=True)
class Constraint:
    kind: ConstraintKind
    label_ko: str
    on: date | None = None
    window: tuple[date, date] | None = None
    """기간이 있는 제약(통지 창구 등). `on`은 그중 마지막 날."""
    basis_ko: str = ""
    action_ko: str = ""
    note_ko: str = ""


@dataclass(frozen=True, slots=True)
class WindowPoint:
    """어느 날 팔면 얼마인가."""

    on: date
    transfer_tax: Won
    """양도세 + 개인지방소득세."""
    blocked_by: tuple[str, ...] = ()

    @property
    def feasible(self) -> bool:
        return not self.blocked_by


@dataclass(frozen=True, slots=True)
class Cliff:
    """세액이 뛰는 자리. 하루 차이로 갈리는 지점을 사용자에게 이름으로 보여준다."""

    before: date
    after: date
    increase: Won

    @property
    def is_overnight(self) -> bool:
        return (self.after - self.before).days == 1


@dataclass(frozen=True, slots=True)
class SellWindow:
    points: tuple[WindowPoint, ...]
    constraints: tuple[Constraint, ...] = ()
    property_label: str = ""
    assumes_vacant: bool = True
    """공실로 넘긴다는 전제. 세입자를 승계시키는 매도는 이 제약을 받지 않는다."""

    @property
    def best(self) -> WindowPoint | None:
        """**팔 수 있는 날 중** 가장 싼 날. 동점이면 이른 날 — 늦출 이유가 없다."""
        ok = [p for p in self.points if p.feasible]
        return min(ok, key=lambda p: (p.transfer_tax, p.on)) if ok else None

    @property
    def naive_best(self) -> WindowPoint | None:
        """제약을 무시했을 때 가장 싼 날.

        `best`와 다르면 그 차이가 이 모듈의 존재 이유다 — 세액만 보고 고르면
        팔 수 없는 날을 고르게 된다.
        """
        return min(self.points, key=lambda p: (p.transfer_tax, p.on)) if self.points else None

    @property
    def constraint_cost(self) -> Won:
        """제약 때문에 더 내야 하는 세금. 0이면 제약이 최적안을 바꾸지 않았다."""
        b, n = self.best, self.naive_best
        if b is None or n is None:
            return 0
        return b.transfer_tax - n.transfer_tax

    @property
    def deadlines(self) -> tuple[Constraint, ...]:
        return tuple(c for c in self.constraints if c.kind is ConstraintKind.DEADLINE)

    def cliffs(self, threshold: Won = 1) -> tuple[Cliff, ...]:
        """이웃한 두 표본 사이에서 세액이 오르는 지점.

        표본에 법적 경계를 넣어 두었으므로, 하루 간격 절벽은 실제로 하루 차이다.
        """
        out: list[Cliff] = []
        ordered = sorted(self.points, key=lambda p: p.on)
        for a, b in zip(ordered, ordered[1:]):
            gap = b.transfer_tax - a.transfer_tax
            if gap >= threshold:
                out.append(Cliff(a.on, b.on, gap))
        return tuple(out)


# --------------------------------------------------------------------------
# 제약 도출 — 임대차 사실에서 기한을 뽑는다
# --------------------------------------------------------------------------


def _current_lease(case: TaxCase, property_id: PropertyId) -> LeaseSpell | None:
    """가장 늦게 끝나는 임대차. 매도 가능 시점을 정하는 것은 이 계약이다."""
    leases = [l for l in case.leases_of(property_id) if l.actual_end is not None]
    return max(leases, key=lambda l: l.actual_end) if leases else None


def _renewal_right_spent(case: TaxCase, property_id: PropertyId, lease: LeaseSpell) -> bool | None:
    """현 임차인이 계약갱신요구권을 이미 썼는가. 모르면 None.

    §6의3②가 요구권을 **1회 한정**으로 정하므로, 같은 임차인이 이미 행사했다면
    다음 만기에는 통지만으로 끝낼 수 있다. 아직 안 썼다면 임대인이 통지해도
    임차인이 요구하면 2년이 붙는다(§6의3①이 §6에 우선한다).

    임차인 별칭(`tenant_ref`)이 없으면 계약들이 같은 임차인인지 알 수 없다.
    그때는 **단정하지 않는다** — 모르는 것을 소진으로 처리하면 위험을 지운다.
    """
    if not lease.tenant_ref:
        return None
    same = [l for l in case.leases_of(property_id) if l.tenant_ref == lease.tenant_ref]
    return any(l.origin is LeaseOrigin.TENANT_RENEWAL_RIGHT for l in same)


def lease_constraints(
    case: TaxCase,
    property_id: PropertyId,
    ruleset: RuleSet,
    *,
    on: date,
    track: Track = Track.REFORM,
    assume_renewal: bool = False,
) -> tuple[tuple[Constraint, ...], date | None]:
    """임대차에서 나오는 제약과 **공실 인도 가능일**을 함께 돌려준다.

    `assume_renewal=True`면 임대차가 한 번 더 갱신된 세계를 계산한다. 갱신은 두
    경로로 온다 — 통지기한을 놓쳐 묵시적으로 갱신되거나(§6①②), 임차인이 갱신
    요구권을 행사하거나(§6의3①②). 어느 쪽이든 존속기간은 2년이라 결과가 같다.
    """
    leases = case.leases_of(property_id)
    lease = _current_lease(case, property_id)

    # ⚠️ 임대차가 있는데 종료일이 하나도 없으면 예전에는 **조용히 제약 0건**이었다.
    #    세입자가 사는데 아무 기한도 안 뜨는 화면이 나온다(2026-08-13 감사).
    if lease is None or lease.actual_end is None:
        if not leases:
            return (), None
        return (
            Constraint(
                kind=ConstraintKind.RISK,
                label_ko="임대차 종료일 미상",
                basis_ko="주택임대차보호법 §4①",
                action_ko="임대차 종료일을 입력해주세요.",
                note_ko=(
                    "종료일을 모르면 갱신거절 통지기한도 공실 인도 가능일도 계산할 수 "
                    "없습니다. 기간을 정하지 않았거나 2년 미만으로 정한 임대차는 "
                    "**2년으로 봅니다**(§4①. 다만 임차인은 2년 미만이 유효함을 주장할 수 "
                    "있습니다)."
                ),
            ),
        ), None

    res = ruleset.resolve_or_none(NOTICE_RULE, on=on, track=track)
    if res is None:
        return (), None
    p = res.block.payload

    end = lease.actual_end
    win = p["notice_window_months_before"]
    notice_from = plus_months(end, -int(win["earliest"]))
    notice_to = plus_months(end, -int(win["latest"]))
    renew_years = int(p["tenant_renewal_right_years"])

    # ⚠️ 이미 끝난 계약을 '현 임대차'로 삼아 지나간 통지기한을 시키고 있었다
    #    (2026-08-13 감사 — 법률·이용자 두 관점). 20개월 전 날짜를 두고
    #    "통지해야 합니다"라고 하면 사용자는 무엇을 해야 할지 알 수 없다.
    #
    #    만기가 지났으면 두 갈래다 — 임차인이 나갔거나, 묵시적으로 갱신됐거나(§6①②).
    #    엔진은 어느 쪽인지 모른다. **단정하지 않고 묻는다.**
    if end < on:
        return (
            Constraint(
                kind=ConstraintKind.RISK,
                label_ko="이미 끝난 임대차",
                on=end,
                basis_ko="주택임대차보호법 §6①②",
                action_ko=(
                    f"입력된 임대차는 {end}에 이미 끝났습니다. "
                    "임차인이 나갔는지, 묵시적으로 갱신됐는지 확인해주세요."
                ),
                note_ko=(
                    f"만기 6개월 전~2개월 전({notice_from}~{notice_to})에 갱신거절을 "
                    "통지하지 않았다면 전 임대차와 같은 조건으로 갱신되고 존속기간은 "
                    f"{int(p['implied_renewal_years'])}년입니다(§6①②). 갱신됐다면 "
                    f"공실 인도는 {plus_months(end, 12 * int(p['implied_renewal_years']))} "
                    "이후가 됩니다. 현재 임대차를 입력해주시면 기한을 다시 계산합니다."
                ),
            ),
        ), None

    out: list[Constraint] = [
        Constraint(
            kind=ConstraintKind.DEADLINE,
            label_ko="세입자에게 갱신거절 통지",
            on=notice_to,
            window=(notice_from, notice_to),
            basis_ko="주택임대차보호법 §6①",
            action_ko=(
                f"{notice_from}~{notice_to} 사이에 갱신거절을 통지해야 합니다. "
                "이 기간에 통지하지 않으면 묵시적으로 갱신되어 "
                f"임대차가 {int(p['implied_renewal_years'])}년 더 붙습니다(§6②)."
            ),
            note_ko="증거가 남는 방법(내용증명 등)으로 하는 편이 안전합니다.",
        )
    ]

    spent = _renewal_right_spent(case, property_id, lease)
    if spent is not True:
        # ⚠️ 통지만으로는 막지 못한다. §6의3①이 "제6조에도 불구하고"로 시작한다.
        vacant_at_risk = plus_months(end, 12 * renew_years) + timedelta(days=1)
        out.append(
            Constraint(
                kind=ConstraintKind.RISK,
                label_ko="임차인의 계약갱신요구권",
                on=notice_to,
                window=(notice_from, notice_to),
                basis_ko="주택임대차보호법 §6의3①②",
                action_ko=(
                    "현 임차인이 갱신요구권을 이미 행사했는지 확인하십시오."
                    if spent is None
                    else "현 임차인에게 갱신요구권이 남아 있습니다."
                ),
                note_ko=(
                    "요구권이 남아 있으면 갱신거절을 통지해도 임차인이 요구할 경우 "
                    f"임대차가 {renew_years}년 연장되어 공실 인도는 {vacant_at_risk} "
                    "이후가 됩니다. 거절 사유는 임대인·직계존비속의 실제 거주 등 "
                    "법정 사유뿐이고(§6의3① 단서), 매수인의 실거주는 소유권 이전 "
                    "전에는 사유가 되지 않습니다."
                ),
            )
        )

    if assume_renewal:
        vacant_from = plus_months(end, 12 * renew_years) + timedelta(days=1)
        out.append(
            Constraint(
                kind=ConstraintKind.BLOCKS,
                label_ko="임대차 갱신됨(가정)",
                on=vacant_from,
                basis_ko="주택임대차보호법 §6②·§6의3②(존속기간 2년)",
                action_ko=f"갱신되면 {vacant_from}부터 공실로 넘길 수 있습니다.",
                note_ko=(
                    "통지기한을 놓쳐 묵시적으로 갱신되거나 임차인이 갱신요구권을 "
                    "행사한 경우입니다. 어느 쪽이든 2년이라 결과가 같습니다."
                ),
            )
        )
    else:
        vacant_from = end + timedelta(days=1)
        out.append(
            Constraint(
                kind=ConstraintKind.BLOCKS,
                label_ko="임차인 거주 중",
                on=vacant_from,
                basis_ko="현 임대차 종료일",
                action_ko=f"{vacant_from}부터 공실로 넘길 수 있습니다.",
                note_ko="세입자를 승계시키는 조건이면 이 제약은 걸리지 않습니다.",
            )
        )
    return tuple(out), vacant_from


def sangsaeng_constraints(
    case: TaxCase,
    property_id: PropertyId,
    ruleset: RuleSet,
    *,
    on: date,
    track: Track = Track.REFORM,
) -> tuple[Constraint, ...]:
    """상생임대 특례의 양도기한을 제약으로 낸다.

    ★ 이 기한은 **세액 곡선에 이미 반영돼 있다** — 넘기면 비과세가 사라져 세액이
      뛴다. 그래도 별도로 내는 이유는, 곡선의 절벽에 **이름**이 붙어야 사용자가
      "왜 여기서 뛰는지"를 알 수 있기 때문이다. 숫자만 보여주는 것은 설명이 아니다.

    ⚠️ `on`은 검토 구간의 **끝**을 넘겨야 한다. 양도기한 요건은 '26.10.1. 이후
       양도분부터 시행되므로, 구간 시작일로 규칙을 해석하면 오늘이 시행일 전일 때
       기한이 통째로 사라진다(2026-08-13 실측). 기한은 '언제 파느냐'에 붙는
       요건이지 '언제 물어보느냐'에 붙는 요건이 아니다.
    """
    v = sangsaeng_assess(case, property_id, ruleset, on, track)
    if v.transfer_deadline is None:
        return ()
    return (
        Constraint(
            kind=ConstraintKind.DEADLINE,
            label_ko="상생임대 특례 양도기한",
            on=v.transfer_deadline,
            basis_ko="소득세법 시행령 §155의3(2026 개편안)",
            action_ko=f"{v.transfer_deadline}까지 잔금을 치러야 거주요건 면제가 유지됩니다.",
            note_ko=(
                "넘기면 1세대1주택 비과세(12억)와 장기보유특별공제의 2년 거주요건이 "
                "되살아납니다. 개편안 사항이라 국회·시행령 개정 전까지는 확정이 아닙니다."
            ),
        ),
    )


# --------------------------------------------------------------------------
# 최적화
# --------------------------------------------------------------------------


def _candidates(start: date, end: date, marks: list[date]) -> tuple[date, ...]:
    """표본 날짜. 균등 격자 + **법적 경계와 그 다음 날**.

    세액은 계단 함수이므로 경계를 직접 넣지 않으면 절벽을 못 본다.
    경계 '다음 날'까지 넣는 이유는, 절벽의 높이를 재려면 양쪽이 다 필요하기 때문이다.
    """
    # 격자는 **월초**로 잡는다. 시작일 기준으로 한 달씩 더하면 8월 13일·9월 13일…
    # 처럼 사람이 생각하지 않는 날짜가 나오고, 화면에 그대로 노출된다.
    pool: set[date] = {start, end}
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        if cursor >= start:
            pool.add(cursor)
        cursor = plus_months(cursor, 1)

    for year in range(start.year, end.year + 2):
        pool.add(date(year, 1, 1))
        pool.add(date(year, 12, 31))
    for m in marks:
        pool.update((m, m + timedelta(days=1), m - timedelta(days=1)))

    return tuple(sorted(d for d in pool if start <= d <= end))


def _event_on(event: TransferEvent, case: TaxCase, on: date) -> TransferEvent:
    """양도일을 옮기면 **보유·거주기간도 함께 움직인다.**

    ⚠️ 2026-08-13 멀티에이전트 감사에서 세무·개발·이용자 세 관점이 독립적으로 잡았다.
       예전에는 `replace(event, transfer_date=on)`만 했다. 그런데 `compute_transfer_tax`는
       명시된 `holding_years`가 있으면 취득일에서 도출한 값 대신 그 명시값을 쓰고,
       화면은 항상 명시값을 넣는다. 그래서 2026년에 팔든 2030년에 팔든 보유기간이
       고정됐고, **이 모듈의 존재 이유인 절벽이 곡선에서 통째로 사라졌다** —
       보유 2년 도달로 단기세율(60·70%)을 벗어나는 자리, 장기보유공제가 해마다
       오르는 자리가 전부 평평해졌다.

    처리
      · 취득일을 알면 **명시값을 지운다.** 엔진이 날짜별로 다시 도출한다(가장 정확).
      · 취득일을 모르면 지울 수 없으므로(도출이 None이 되어 판정 불가로 흐른다)
        경과한 햇수만큼 **더해서** 옮긴다.
      · 거주기간은 **옮기지 않는다.** 스칼라만으로는 아직 살고 있는지 알 수 없고,
        늘리는 쪽이 세금을 낮추므로 모르는 채 늘리면 과소신고가 된다.
        거주 이력(ResidenceSpell)이 있으면 첫 경로에서 정확히 도출된다.
    """
    acquired = periods.acquisition_date(case, event.person_id, event.property_id)
    if acquired is not None:
        return replace(event, transfer_date=on, holding_years=None)

    hold = event.holding_years
    if hold is not None:
        moved = periods.full_years(event.transfer_date, on)
        hold = max(0, hold + moved)
    return replace(event, transfer_date=on, holding_years=hold)


def optimize(
    case: TaxCase,
    event: TransferEvent,
    ruleset: RuleSet,
    *,
    start: date,
    end: date,
    track: Track = Track.REFORM,
    require_vacant: bool = True,
    assume_renewal: bool = False,
) -> SellWindow:
    """`start`~`end` 사이에서 팔 날을 고른다.

    `event`는 양도가액·취득가액을 담은 틀이고 **양도일만 바꿔 가며** 다시 계산한다.
    양도가액은 날짜와 무관하게 같다고 본다 — 시세 전망을 섞으면 세제 효과가 묻힌다.

    `require_vacant=False`면 세입자 승계 매도를 뜻하고, 임대차로 인한 BLOCKS 제약을
    적용하지 않는다. 기한(DEADLINE)과 위험(RISK)은 그대로 낸다 — 승계로 팔더라도
    묵시적 갱신은 매수인이 떠안는 사실이므로 숨기지 않는다.
    """
    lease_cs, vacant_from = lease_constraints(
        case, event.property_id, ruleset, on=start, track=track,
        assume_renewal=assume_renewal,
    )
    sang_cs = sangsaeng_constraints(
        case, event.property_id, ruleset, on=end, track=track
    )
    constraints = lease_cs + sang_cs

    marks = [c.on for c in constraints if c.on is not None]
    marks.extend(w for c in constraints if c.window for w in c.window)

    points: list[WindowPoint] = []
    for on in _candidates(start, end, marks):
        sale = _event_on(event, case, on)
        result = compute_transfer_tax(case, sale, ruleset, track=track)

        blocked: tuple[str, ...] = ()
        if require_vacant and vacant_from is not None and on < vacant_from:
            blocked = (f"{vacant_from}까지 임차인이 거주합니다",)

        points.append(
            WindowPoint(on=on, transfer_tax=result.total.as_int(), blocked_by=blocked)
        )

    label = case.find_property(event.property_id).display_name or str(event.property_id)
    return SellWindow(
        points=tuple(points),
        constraints=constraints,
        property_label=label,
        assumes_vacant=require_vacant,
    )
