"""보유기간·거주기간 — **사실에서 기간을 뽑는 단 하나의 정의.**

이 모듈이 따로 있는 이유는 정리벽이 아니다. 같은 실수가 세 번 났기 때문이다.

  · 종부세 엔진이 `ResidenceSpell`을 안 읽어 거주 **여부**를 틀렸다 (2026-08-04)
  · 고쳐 놓고도 거주·보유 **기간**은 여전히 옵션만 봤다 (SIM-01, 2026-08-05)
  · 양도세 엔진은 취득일이 이벤트에 적혀 있는데도 보유기간을 None으로 뒀다
    (SIM-06, 2026-08-05) — 비과세 배제 + 장특공제 0 + 단기 70% 세율이 한꺼번에 걸렸다

세 번 다 원인은 하나다. **모델에 있는 사실을 엔진이 안 읽었다.**
정의가 두 곳에 흩어지면 한 곳만 고치는 날이 반드시 온다. 그래서 여기 한 벌만 둔다.

근거
  보유기간 — 소득세법 §95④ "자산의 취득일부터 양도일까지",
              종합부동산세법 §9⑧ (과세기준일 현재 보유기간)
  거주기간 — 소득세법 시행령 §154⑥ (주민등록표상 전입일~전출일)
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from fractions import Fraction
from typing import Any, Mapping, Sequence

from ..domain.models import PersonId, PropertyId, ResidenceSpell, TaxCase


def full_years(start: date, until: date) -> int:
    """만 연수. 생일 계산과 같은 규약을 쓴다.

    하루 차이로 공제 구간이 갈리므로(보유 3년, 거주 2년, 15년 구간 등)
    나이 계산과 다른 규약을 쓰면 경계에서 조용히 어긋난다.

    ★ 2월 29일 취득분(SIM-08, 2026-08-05 시뮬레이션).
      민법 §160③ — "최종의 월에 해당일이 없는 때에는 **그 월의 말일**로 기간이
      만료한다." 국세기본법 §4가 기간 계산에 민법을 준용하므로 세법에도 그대로 온다.

      즉 2024-02-29 취득분의 보유 2년은 평년인 2026년에는 **2월 28일**에 만료한다.
      단순히 (월, 일)을 비교하면 (2,28) < (2,29)라 하루가 밀려 보유 1년이 되고,
      2년 미만 단기세율 60%가 붙는다. 실측 차이 840,000원 vs 8,400,000원 — 10배다.
    """
    years = until.year - start.year
    anniversary_day = min(start.day, monthrange(until.year, start.month)[1])
    if (until.month, until.day) < (start.month, anniversary_day):
        years -= 1
    return max(0, years)


def acquisition_date(
    case: TaxCase,
    person_id: PersonId,
    property_id: PropertyId,
    *,
    declared: date | None = None,
) -> date | None:
    """보유기간의 기산일.

    명시된 값이 있으면 그것이 이긴다 — 배우자 상속분 통산처럼 엔진이 모르는 특칙을
    사용자가 알고 있을 수 있다.

    없으면 소유 이력에서 **가장 이른 취득일**을 쓴다. 지분을 여러 번에 나눠
    취득하면(추가 매수·배우자 증여·공동상속 지분 매수) 행이 여러 개가 되는데,
    나중 취득일을 쓰면 보유기간이 리셋돼 장기보유 혜택이 통째로 날아간다.
    """
    if declared is not None:
        return declared
    dates = [
        o.acquired_on
        for o in case.ownerships_of(person_id)
        if o.property_id == property_id and o.acquired_on is not None
    ]
    return min(dates) if dates else None


def holding_years(
    case: TaxCase,
    person_id: PersonId,
    property_id: PropertyId,
    until: date,
    *,
    declared_years: int | None = None,
    declared_date: date | None = None,
) -> int | None:
    """보유기간(년). 알 수 없으면 **0이 아니라 None**을 돌려준다.

    0으로 내려보내면 "보유 0년 < 3년 → 공제 없음"처럼 **확정적으로 불리한 판정**이
    되어 나간다. 모르는 것과 0년은 다르다.
    """
    if declared_years is not None:
        return declared_years
    start = acquisition_date(case, person_id, property_id, declared=declared_date)
    return None if start is None else full_years(start, until)


def _merged_days(windows: list[tuple[date, date]]) -> int:
    """겹치는 구간을 병합한 뒤 일수를 더한다.

    한 기간을 두 줄로 나눠 입력한 경우 그냥 더하면 살지도 않은 기간이 공제로 둔갑한다.
    """
    if not windows:
        return 0
    windows.sort()
    merged: list[list[date]] = [list(windows[0])]
    for start, finish in windows[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], finish)
        else:
            merged.append([start, finish])
    return sum((f - s).days for s, f in merged)


def merged_residence_years(
    spells: Sequence[ResidenceSpell],
    until: date,
    *,
    imputed: Mapping[str, Any] | None = None,
) -> int:
    """거주 구간의 합을 만 연수로.

    ★ **인정 구간(`imputed_reason`)에는 상한이 있다**(2026-08-05 발견).
      개편안은 부득이한 사유로 인한 비거주기간을 거주기간으로 인정하되
      **최장 3년**으로 자른다(개조식 p.22 ➊). 재개발·재건축 공사기간은
      **1/2만** 인정한다(➋).

      엔진은 `ImputedResidenceReason`을 아예 읽지 않아 인정 구간을 상한 없이
      전부 거주로 세고 있었다. 거주기간을 늘리는 규칙이라 **과소신고 방향**이다 —
      모델에 있는 사실을 엔진이 안 읽는 실수의 여섯 번째이자, 방향이 가장 나쁜 것.

    `imputed`가 없으면(현행법 트랙 등) **인정 구간을 아예 세지 않는다.**
    근거 규칙이 없는데 인정해 주는 것이 가장 위험하다.
    """
    real: list[tuple[date, date]] = []
    excused: list[tuple[date, date]] = []
    rebuilt: list[tuple[date, date]] = []

    allowed = set((imputed or {}).get("reasons") or ())
    for s in spells:
        finish = min(s.end, until) if s.end is not None else until
        if finish <= s.start:
            continue
        window = (s.start, finish)
        if s.imputed_reason is None:
            real.append(window)
        elif imputed is None:
            continue  # 근거 규칙 없음 → 인정하지 않는다
        elif str(s.imputed_reason) == "reconstruction":
            rebuilt.append(window)
        elif str(s.imputed_reason) in allowed:
            excused.append(window)
        # 목록에 없는 사유는 인정하지 않는다 — 조문이 열거한 것만이다

    days = _merged_days(real)

    if excused:
        cap = int((imputed or {}).get("max_years", 0)) * 365
        days += min(_merged_days(excused), cap)

    if rebuilt:
        ratio = Fraction(str((imputed or {}).get("reconstruction_ratio", "1/2")))
        days += int(_merged_days(rebuilt) * ratio)

    return days // 365


def imputed_spec(
    ruleset: object, *, tax: str, on: date, track: object
) -> Mapping[str, Any] | None:
    """해당 세목·시점에 적용되는 인정 규칙. 없으면 None(= 인정하지 않음).

    세목마다 시행일이 다르다 — 종부세 '27.1.1., 양도세 '28.1.1.(개조식 p.22).
    """
    res = ruleset.resolve_or_none(
        "reference.imputed_residence", on=on, track=track, tax=tax
    )
    return res.block.payload if res is not None else None


def residence_years(
    case: TaxCase,
    person_id: PersonId,
    property_id: PropertyId,
    until: date,
    *,
    declared: int | None = None,
    imputed: Mapping[str, Any] | None = None,
) -> int | None:
    """거주기간(년). 거주 이력이 아예 없으면 None — 0년과 구분한다.

    "이 집에 산 적 없다"와 "산 적 있는지 모른다"는 다른 사실이고, 전자만
    0년으로 단언할 수 있다. 이력이 한 줄도 없으면 후자다.
    """
    if declared is not None:
        return declared
    spells = case.residences_of(person_id, property_id)
    if not spells:
        return None
    return merged_residence_years(spells, until, imputed=imputed)


__all__ = [
    "acquisition_date",
    "imputed_spec",
    "full_years",
    "holding_years",
    "merged_residence_years",
    "residence_years",
]
