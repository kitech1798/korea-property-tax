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
from typing import Iterable, Sequence

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


def merged_residence_years(spells: Sequence[ResidenceSpell], until: date) -> int:
    """거주 구간의 합을 만 연수로. **구간을 병합한 뒤** 더한다.

    겹치는 구간(한 기간을 두 줄로 나눠 입력한 경우 등)을 그냥 더하면
    살지도 않은 기간이 공제로 둔갑한다.
    """
    windows = []
    for s in spells:
        finish = min(s.end, until) if s.end is not None else until
        if finish > s.start:
            windows.append((s.start, finish))
    if not windows:
        return 0
    windows.sort()
    merged: list[list[date]] = [list(windows[0])]
    for start, finish in windows[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], finish)
        else:
            merged.append([start, finish])
    return sum((f - s).days for s, f in merged) // 365


def residence_years(
    case: TaxCase,
    person_id: PersonId,
    property_id: PropertyId,
    until: date,
    *,
    declared: int | None = None,
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
    return merged_residence_years(spells, until)


__all__ = [
    "acquisition_date",
    "full_years",
    "holding_years",
    "merged_residence_years",
    "residence_years",
]
