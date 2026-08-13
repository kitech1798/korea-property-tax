"""상생임대주택 특례 판정 — 소득세법 시행령 §155의3.

임대료를 5% 이내로만 올린 집주인에게 **거주요건을 면제**해 주는 제도다.
실거주하지 않은 집도 1세대1주택 비과세(12억)와 장기보유특별공제 우대(표2)를
받게 되므로, 이 판정 하나가 세액을 통째로 가른다.

★★ 면제되는 것은 '거주기간의 제한'이지 거주기간이 아니다.

  §155의3①은 §159의4(장기보유특별공제)를 적용할 때 "해당 규정에 따른 **거주기간의
  제한**을 받지 않는다"고 한다. §159의4가 표2 대상을 "거주기간이 2년 이상인 것"으로
  정의하므로, 면제되는 것은 **표2에 들어갈 자격**이다.

  거주기간을 2년으로 의제하는 규정은 없다. 표2 안의 거주기간 공제율은 실제 거주
  기간으로 계산한다 — 실거주 0년이면 거주공제 0%, 보유공제만 남는다.

  2026 개편안이 보유공제를 거주공제로 옮기므로(연 4%→2%→폐지) 이 구분이 치명적이
  된다. 상생임대 특례가 살아 있어도 실거주 0년인 주택의 장특공제는
  '27년 40% → '28년 20% → '29년 0%로 무너진다.
  **비과세 12억은 지켜지지만 장기보유특별공제는 지켜지지 않는다.**

이 모듈이 하지 않는 것
  - 보증금↔월세 전환이 있는 경우의 증가율(§155의3②, 민간임대주택법 §44④ 기준).
    전환식을 확보하지 못했으므로 **판정하지 않고 드러낸다.**
  - §155의3④의 임대기간 합산(재정경제부령 요건 미확보).
  둘 다 유리한 쪽으로 가정하면 과소신고가 되므로 지어내지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction
from typing import Any, Mapping

from ..domain import LeaseOrigin, LeaseSpell, PropertyId, TaxCase
from ..domain.certainty import Certainty, DeterminationQuality
from ..rules import RuleSet, Track
from .periods import full_months, plus_months

RULE_ID = "transfer.sangsaeng_lease"


# 월 가산은 매도시점 최적화기와 공유한다(주임법 §6①의 '2개월 전'도 같은 계산이다).
_plus_months = plus_months


def _lease_months(spell: LeaseSpell, as_of: date | None = None) -> int | None:
    """임대한 기간의 만 개월수(§155의3③).

    ⚠️ 종료일은 **그 날까지 임대했다**는 뜻이므로 다음 날을 넘긴다.
       2025-02-01~2027-01-31 전세는 24개월이지 23개월이 아니다.
       이 하루가 2년 요건을 뒤집는다.

    ★ `as_of`(양도일)를 넘기면 **그때까지 실제로 임대한 기간**만 센다.
      §155의3①은 "다음 각 호의 요건을 **모두 갖춘** 주택을 양도하는 경우"이므로
      요건은 양도 시점에 이미 충족돼 있어야 한다. 이걸 빠뜨리면 계약서에 적힌
      미래의 임대기간까지 앞당겨 세어, 아직 2년을 못 채운 사람에게 비과세를
      내주게 된다(매도 시점 최적화기를 만들다 발견, 2026-08-13).

    "1개월 미만인 경우에는 1개월로 본다"(§155의3③ 후단)도 여기서 처리한다.
    """
    end = spell.actual_end
    if end is None:
        return None
    if as_of is not None:
        end = min(end, as_of)
        if end < spell.start:
            return 0
    months = full_months(spell.start, end + timedelta(days=1))
    if months == 0 and end >= spell.start:
        return 1
    return months


def _increase_rate(before: int, after: int) -> Fraction | None:
    """증가율. 기준값이 0이면 비율이 정의되지 않으므로 None(판정 불가)."""
    if before == 0:
        return Fraction(0) if after == 0 else None
    return Fraction(after - before, before)


@dataclass(frozen=True, slots=True)
class SangsaengVerdict:
    """상생임대주택 판정 결과.

    `applies`와 `undecidable`을 따로 두는 이유 — "아니다"와 "모르겠다"는 다른
    사건이다. 전자는 계산을 계속하고, 후자는 확실성을 떨어뜨려 화면이 확인을
    요구하게 만든다. 하나로 합치면 모르는 것이 '아니다'로 굳는다.
    """

    applies: bool = False
    undecidable: bool = False
    prior: LeaseSpell | None = None
    """직전임대차계약."""
    lease: LeaseSpell | None = None
    """상생임대차계약."""
    transfer_deadline: date | None = None
    """개편안이 신설한 양도기한. 현행 트랙에서는 None(기한 요건이 없다)."""
    reasons_ko: tuple[str, ...] = ()
    checks_ko: tuple[str, ...] = ()
    """통과한 요건. 왜 되는지도 보여야 사용자가 검증할 수 있다."""

    @property
    def certainty(self) -> Certainty:
        if self.undecidable:
            return Certainty(determination=DeterminationQuality.UNDECIDABLE)
        return Certainty()


def _deadline(
    lease: LeaseSpell, spec: Mapping[str, Any] | None
) -> tuple[date | None, bool]:
    """개편안 양도기한. (기한, 판정불가) 를 돌려준다.

    상세본 p.78 <추가> "다음 기한 이내에 양도
      ➊ '26.12.31. 이전 상생임대차계약 종료: '27.12.31.
      ➋ '27.1.1. 이후 상생임대차계약 종료: 계약종료 후 1년이 되는 날과
         '29.12.31. 중 **빠른 날**"
    """
    if not spec:
        return None, False
    end = lease.actual_end
    if end is None:
        return None, True  # 언제 끝나는지 모르면 기한도 모른다

    early_cut = spec["lease_ended_on_or_before"]
    if end <= early_cut:
        return spec["deadline_if_ended_early"], False

    after = _plus_months(end, int(spec["months_after_lease_end"]))
    return min(after, spec["absolute_cap"]), False


def assess(
    case: TaxCase,
    property_id: PropertyId,
    ruleset: RuleSet,
    on: date,
    track: Track,
) -> SangsaengVerdict:
    """그 주택이 상생임대주택인가.

    `on`은 **양도일**이다. 개편안의 양도기한 요건이 양도일 기준으로 시행되므로
    (상세본 p.78 "'26.10.1. 이후 양도하는 분부터"), 규칙 해석도 양도일로 한다.
    """
    resolution = ruleset.resolve_or_none(RULE_ID, on=on, track=track)
    if resolution is None:
        return SangsaengVerdict(reasons_ko=("상생임대 특례 규칙을 찾지 못했습니다",))
    p = resolution.block.payload

    leases = case.leases_of(property_id)
    if len(leases) < 2:
        return SangsaengVerdict(
            reasons_ko=(
                "직전임대차계약과 상생임대차계약이 모두 있어야 합니다"
                f" (입력된 임대차 {len(leases)}건)",
            )
        )

    window = p["contract_window"]
    cap = Fraction(p["rent_increase_cap"])
    need_prior = int(p["prior_lease_min_months"])
    need_lease = int(p["sangsaeng_lease_min_months"])
    deadline_spec = p.get("transfer_deadline")

    best: SangsaengVerdict | None = None
    undecided: SangsaengVerdict | None = None
    misses: list[str] = []

    # 연속한 두 계약을 (직전, 상생) 쌍으로 훑는다. 요건을 만족하는 쌍이 여럿이면
    # **상생임대차계약이 가장 늦게 끝난 쌍**을 택한다 — 법은 어느 쌍을 쓰라고
    # 정하지 않고, 양도기한이 그 계약의 종료일에 붙으므로 납세자는 늦은 쪽을 든다.
    for prior, lease in zip(leases, leases[1:]):
        fail: list[str] = []
        unsure: list[str] = []
        ok: list[str] = []

        # ── 승계 계약은 직전임대차계약이 될 수 없다 ────────────────────
        if p.get("exclude_succeeded_prior_lease") and prior.origin is LeaseOrigin.SUCCEEDED:
            misses.append(
                f"{prior.start}~ 계약은 주택 취득으로 승계받은 것이라 "
                "직전임대차계약이 될 수 없습니다(§155의3①1호 괄호)"
            )
            continue

        # ── 체결 시기 ────────────────────────────────────────────────
        if lease.contracted_on is None:
            unsure.append(f"{lease.start}~ 계약의 체결일을 모릅니다")
        elif not (window["from"] <= lease.contracted_on <= window["to"]):
            fail.append(
                f"체결일 {lease.contracted_on}이 "
                f"{window['from']}~{window['to']} 밖입니다"
            )
        else:
            ok.append(f"체결일 {lease.contracted_on} — 기한 내")

        # ── 임대 개시 시기 ───────────────────────────────────────────
        # ⚠️ 2026-08-13 감사에서 잡힌 누락 — 체결일만 보고 있었다.
        #    §155의3①1호는 "…기간 중에 체결(…)하고 **임대를 개시할 것**"이고,
        #    상세본 p.78이 요건을 "'21.12.20.~'26.12.31. 중 계약체결 **및 임대개시**"로
        #    풀어 적는다. 둘 다 기한 안이어야 한다.
        #    체결은 '26.12.31.에 하고 임대는 '27년에 시작한 계약이 통과하고 있었다.
        if not (window["from"] <= lease.start <= window["to"]):
            fail.append(
                f"임대 개시일 {lease.start}이 {window['from']}~{window['to']} 밖입니다 "
                "— 체결과 임대개시가 모두 기한 안이어야 합니다(상세본 p.78)"
            )
        else:
            ok.append(f"임대 개시 {lease.start} — 기한 내")

        # ── 계약금 지급 증빙 ─────────────────────────────────────────
        if p.get("require_down_payment_evidence"):
            if lease.down_payment_evidenced is None:
                unsure.append("계약금 지급 증빙서류 확인 여부를 모릅니다")
            elif not lease.down_payment_evidenced:
                fail.append("계약금 지급 사실이 증빙서류로 확인되지 않습니다")
            else:
                ok.append("계약금 지급 증빙 확인")

        # ── 직전계약이 정말 '직전'인가 ────────────────────────────────
        # §155의3①1호의 '직전 임대차계약'은 상생임대차계약 **바로 앞**의 계약이다.
        # 기간이 겹치면 앞뒤 관계가 성립하지 않는다. 한 물건에 겹치는 계약이 있는
        # 상황(다가구 호별 임대 등)은 이 엔진이 호를 구분하지 못해 판정할 수 없다.
        # 겹치는데도 쌍으로 세면, 3년짜리 계약 하나를 직전계약으로 오인해
        # 1년 6개월 요건을 통과시킨다(2026-08-13 스트레스 스윕에서 발견).
        if prior.actual_end is not None and prior.actual_end >= lease.start:
            unsure.append(
                f"{prior.start}~{prior.actual_end} 계약이 {lease.start} 시작 계약과 "
                "기간이 겹칩니다 — 어느 것이 직전임대차계약인지 판정할 수 없습니다"
            )

        # ── 임대료 증가율 5% 이내 ────────────────────────────────────
        # ⚠️ 보증금도 월세도 없으면 임대차의 대가가 없다는 뜻이다. 무상거주는
        #    임대차가 아니라 사용대차라 상생임대 대상이 아니고, 실제로는 금액을
        #    입력하지 않은 경우가 대부분이다. 증가율 0%로 읽어 통과시키면
        #    **빈칸에 혜택을 주는 셈**이다.
        for who, spell in (("직전임대차", prior), ("상생임대차", lease)):
            if spell.deposit == 0 and spell.monthly_rent == 0:
                unsure.append(
                    f"{who}계약의 임대보증금과 월 임대료가 모두 0원입니다 — "
                    "금액을 입력했는지 확인하십시오(무상거주는 상생임대 대상이 아닙니다)"
                )

        converted = (prior.monthly_rent == 0) != (lease.monthly_rent == 0)
        if converted:
            unsure.append(
                "보증금과 월세를 서로 전환한 계약입니다 — 증가율은 민간임대주택법 "
                "§44④ 기준으로 계산해야 하는데(§155의3②) 그 산식을 확보하지 못했습니다"
            )
        else:
            for label, before, after in (
                ("임대보증금", prior.deposit, lease.deposit),
                ("월 임대료", prior.monthly_rent, lease.monthly_rent),
            ):
                rate = _increase_rate(before, after)
                if rate is None:
                    unsure.append(f"{label} 증가율을 계산할 수 없습니다({before:,}원 → {after:,}원)")
                elif rate > cap:
                    fail.append(
                        f"{label} 증가율 {float(rate) * 100:.2f}%가 "
                        f"{float(cap) * 100:g}%를 넘습니다"
                    )
                else:
                    ok.append(f"{label} 증가율 {float(rate) * 100:.2f}%")

        # ── 임대기간 ────────────────────────────────────────────────
        for label, spell, need in (
            ("직전임대차", prior, need_prior),
            ("상생임대차", lease, need_lease),
        ):
            months = _lease_months(spell, as_of=on)
            if months is None:
                unsure.append(f"{label}계약의 종료일을 몰라 임대기간을 계산할 수 없습니다")
            elif months < need:
                fail.append(f"{label} 임대기간 {months}개월 < {need}개월")
            else:
                ok.append(f"{label} 임대기간 {months}개월")
            if spell.ended_by_tenant_circumstance:
                unsure.append(
                    f"{label}계약이 임차인 사정으로 종료됐습니다 — 새 계약과의 기간 합산"
                    "(§155의3④)은 재정경제부령 요건이 필요해 판정하지 않습니다"
                )

        if fail:
            misses.extend(fail)
            continue

        deadline, deadline_unsure = _deadline(lease, deadline_spec)
        if deadline_unsure:
            unsure.append("상생임대차계약 종료일을 몰라 양도기한을 계산할 수 없습니다")

        if unsure:
            candidate = SangsaengVerdict(
                undecidable=True, prior=prior, lease=lease,
                transfer_deadline=deadline,
                reasons_ko=tuple(unsure), checks_ko=tuple(ok),
            )
            if undecided is None or _later(lease, undecided.lease):
                undecided = candidate
            continue

        candidate = SangsaengVerdict(
            applies=True, prior=prior, lease=lease,
            transfer_deadline=deadline, checks_ko=tuple(ok),
        )
        if best is None or _later(lease, best.lease):
            best = candidate

    if best is not None:
        return best
    if undecided is not None:
        return undecided
    return SangsaengVerdict(reasons_ko=tuple(misses) or ("요건을 충족하는 계약 쌍이 없습니다",))


def _later(lease: LeaseSpell, other: LeaseSpell | None) -> bool:
    """상생임대차계약이 더 늦게 끝나는가. 종료일을 모르는 쪽은 뒤로 보낸다."""
    if other is None:
        return True
    a, b = lease.actual_end, other.actual_end
    if a is None:
        return False
    if b is None:
        return True
    return a > b


def within_transfer_deadline(verdict: SangsaengVerdict, transfer_on: date) -> bool | None:
    """양도일이 개편안 기한 안인가. 기한 요건이 없으면(현행) None."""
    if verdict.transfer_deadline is None:
        return None
    return transfer_on <= verdict.transfer_deadline


def waives_residence(verdict: SangsaengVerdict, transfer_on: date) -> bool:
    """이 양도일 기준으로 **실제로** 거주요건이 면제되는가.

    요건을 갖췄어도 개편안의 양도기한을 넘기면 특례가 사라진다. 그래서
    `verdict.applies`만 보면 안 되고 양도일을 함께 봐야 한다 — 이 함수가
    두 조건을 한 자리에서 합쳐, 호출하는 쪽마다 빠뜨리는 일을 막는다.

    ⚠️ 판정 불가(`undecidable`)는 면제하지 않는다. 모르는 것을 유리하게 적용하면
       과소신고가 된다. 화면은 확실성이 떨어진 것을 보고 확인을 요구한다.
    """
    if not verdict.applies:
        return False
    return within_transfer_deadline(verdict, transfer_on) is not False
