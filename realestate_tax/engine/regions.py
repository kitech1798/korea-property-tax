"""지역 규제 판정 — 조정대상지역·투기과열지구.

시중 계산기의 가장 유명한 결함이 여기 있다. propertytax.co.kr은
`fullAddrName.startsWith('서울')`로 조정대상지역을 판정한다(app.js:768).
그런데 조정대상지역은 국토부 고시 사항이고, 2025년 10월까지 서울에도
비규제 자치구가 있었으며, 경기도는 **일반구 단위로 쪼개서** 지정된다
(수원 4개구 중 3개만, 용인 5개구 중 2개만).

주소 문자열로는 절대 알 수 없다. 그래서 여기서는 세 가지만 한다.

  ① 법정동코드로만 조회한다 — 주소 문자열은 입력으로 받지도 않는다
  ② 고시 이력 테이블(rulesets/.../reference/regulated_areas.yaml)에만 의존한다
  ③ **모르면 모른다고 한다** — 이력이 없는 기간, 코드가 확정되지 않은 지역은
     '비규제'가 아니라 '판정 불가'로 흘린다

③이 핵심이다. 모를 때 '아니오'로 처리하면 조용히 틀린 세액이 나온다.
'판정 불가'는 화면에 뜨고, 사용자가 확인할 기회를 얻는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from ..domain.certainty import Certainty, DeterminationQuality, LegalStatus
from ..rules.resolver import RuleSet
from ..rules.schema import MissingRule, Track
from .trace import SubjectRef, SubjectType, TraceNode, Value, node

RULE_REGULATED = "reference.regulated_areas"
RULE_SPECULATION = "reference.speculation_zones"


class Designation(str):
    """판정 결과 3상태. bool이 아닌 이유는 '모름'이 있기 때문이다."""


YES = Designation("yes")
NO = Designation("no")
UNKNOWN = Designation("unknown")


@dataclass(frozen=True, slots=True)
class RegionStatus:
    """법정동코드 하나에 대한 규제지역 판정."""

    sigungu_code: str
    on: date
    designation: Designation
    region_name: str = ""
    since: date | None = None
    as_of: date | None = None
    """테이블을 마지막으로 확인한 날. 기준일이 이보다 뒤면 확실성을 낮춘다."""
    reason_ko: str = ""
    certainty: Certainty = Certainty()

    @property
    def is_regulated(self) -> bool:
        """⚠️ 판정 불가를 False로 뭉갠다. 세액 분기에 쓰지 말고 `designation`을 보라."""
        return self.designation is YES

    @property
    def needs_confirmation(self) -> bool:
        return self.designation is UNKNOWN


def sigungu_of(legal_dong_code: str) -> str:
    """법정동코드 10자리 → 시군구코드 5자리.

    현행 지정은 전부 시·군·구 단위라 5자리로 충분하다. 주택법 §63의2①은
    읍·면·동 단위 지정도 허용하므로, 그런 고시가 나오면 10자리 판정으로 넓혀야 한다.
    """
    code = str(legal_dong_code).strip()
    if len(code) < 5 or not code[:5].isdigit():
        raise ValueError(f"법정동코드가 아니다: {legal_dong_code!r}")
    return code[:5]


def _lookup(
    ruleset: RuleSet, rule_id: str, on: date, track: Track
) -> Mapping[str, Any] | None:
    res = ruleset.resolve_or_none(rule_id, on=on, track=track)
    return res.block.payload if res is not None else None


def check_regulated(
    legal_dong_code: str,
    ruleset: RuleSet,
    *,
    on: date,
    track: Track = Track.CURRENT,
    rule_id: str = RULE_REGULATED,
) -> RegionStatus:
    """조정대상지역 여부를 판정한다."""
    code = sigungu_of(legal_dong_code)
    payload = _lookup(ruleset, rule_id, on, track)

    if payload is None:
        # 해당 기간의 고시 이력이 없다. '비규제'로 단정하면 조용히 틀린다.
        return RegionStatus(
            code,
            on,
            UNKNOWN,
            reason_ko=(
                f"{on} 시점의 지정 현황 이력이 없습니다. "
                "2025년 10월 16일 이전 기간은 이력을 수집하지 않았습니다."
            ),
            certainty=Certainty(determination=DeterminationQuality.UNDECIDABLE),
        )

    as_of = _as_date(payload.get("as_of"))
    stale = bool(as_of and on > as_of)

    # 코드가 확정되지 않은 시군구는 판정하지 않는다.
    for prefix in payload.get("undecidable_prefixes", ()):
        if code == str(prefix):
            note = str(payload.get("undecidable_note") or "").strip()
            return RegionStatus(
                code,
                on,
                UNKNOWN,
                as_of=as_of,
                reason_ko=note
                or "이 코드로는 조정대상지역 여부를 자동 판정할 수 없습니다.",
                certainty=Certainty(determination=DeterminationQuality.UNDECIDABLE),
            )

    for region in payload.get("regions", ()):
        if str(region.get("code") or "") != code:
            continue
        since = _as_date(region.get("since"))
        return RegionStatus(
            code,
            on,
            YES,
            region_name=str(region.get("name", "")),
            since=since,
            as_of=as_of,
            reason_ko=f"{region.get('name')} — {since}부터 조정대상지역",
            certainty=_staleness(stale, as_of),
        )

    return RegionStatus(
        code,
        on,
        NO,
        as_of=as_of,
        reason_ko="조정대상지역 지정 목록에 없습니다.",
        certainty=_staleness(stale, as_of),
    )


def check_speculation(
    legal_dong_code: str,
    ruleset: RuleSet,
    *,
    on: date,
    track: Track = Track.CURRENT,
) -> RegionStatus:
    """투기과열지구 여부. 2026-08-04 현재 조정대상지역과 동일 목록이다.

    룰셋이 `same_as`로 참조를 걸어두면 그 목록을 따른다 — 두 표를 따로 적으면
    한쪽만 고쳤을 때 조용히 어긋난다.
    """
    payload = _lookup(ruleset, RULE_SPECULATION, on, track)
    if payload is None:
        return RegionStatus(
            sigungu_of(legal_dong_code),
            on,
            UNKNOWN,
            reason_ko=f"{on} 시점의 투기과열지구 지정 이력이 없습니다.",
            certainty=Certainty(determination=DeterminationQuality.UNDECIDABLE),
        )
    target = payload.get("same_as")
    if target:
        return check_regulated(
            legal_dong_code, ruleset, on=on, track=track, rule_id=str(target)
        )
    return check_regulated(
        legal_dong_code, ruleset, on=on, track=track, rule_id=RULE_SPECULATION
    )


def _as_date(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def _staleness(stale: bool, as_of: date | None) -> Certainty:
    """테이블 확인 시점보다 뒤의 기준일이면 확실성을 낮춘다.

    조정대상지역은 예고 없이 바뀌고 반기마다 재검토된다. '2026년 8월에 확인한 표'로
    2028년을 판정하면서 확정인 척하면 안 된다.
    """
    if not stale:
        return Certainty()
    return Certainty(legal=LegalStatus.ASSUMED)


def region_trace(status: RegionStatus, property_label: str = "") -> TraceNode:
    """판정 과정을 감사 추적에 남긴다.

    '조정대상지역입니다'만 보여주면 사용자는 확인할 방법이 없다.
    어느 고시를, 언제 확인한 표로, 어떤 코드로 판정했는지가 드러나야 한다.
    """
    label = {
        YES: "조정대상지역",
        NO: "조정대상지역 아님",
        UNKNOWN: "판정 불가",
    }[status.designation]

    substitution = f"시군구코드 {status.sigungu_code} · 기준일 {status.on}"
    if status.as_of:
        substitution += f" · 고시 확인일 {status.as_of}"

    note = status.reason_ko
    if status.certainty.legal is LegalStatus.ASSUMED:
        note += (
            f" ⚠️ 기준일({status.on})이 고시 확인일({status.as_of})보다 뒤입니다. "
            "그 사이 지정이 바뀌었을 수 있으니 국토교통부 공고를 확인해주세요."
        )

    return node(
        "rg.01.regulated_area",
        f"조정대상지역 판정{f' — {property_label}' if property_label else ''}",
        Value(
            status.designation is YES,
            unit="bool",
            certainty=status.certainty,
            label=label,
        ),
        subject=SubjectRef(SubjectType.PROPERTY, status.sigungu_code, property_label),
        formula="법정동코드 앞 5자리(시군구)로 국토교통부 고시 목록 조회",
        substitution=substitution,
        note_ko=note,
    )


# --------------------------------------------------------------------------
# 토지거래허가구역 — 판정하지 않고 안내만 한다
# --------------------------------------------------------------------------

LAND_PERMIT_NOTICE_KO = """토지거래허가구역 여부는 이 서비스가 판정하지 않습니다.

이유는 세 가지입니다.
  · 보유세·양도세의 **세율이나 과세표준을 정하는 조문에서 참조되지 않습니다.**
    취득 단계 규제(허가 의무·실거주 의무)이지 세액 산정 요소가 아닙니다.
  · 지정 단위가 시·군·구가 아니라 **필지·구역 단위**입니다. 공고마다 토지조서(지번 목록)가
    따로 붙어서, 주소만으로 판정하면 오판 위험이 큽니다.
  · 지정권자가 국토교통부장관과 시·도지사로 나뉘어 있고 유효기간이 1~5년으로 짧아
    전국 단일 소스가 구조적으로 존재하지 않습니다.

지번 단위 확인은 토지이음에서 하실 수 있습니다 — 토지이용계획확인서에
'토지거래계약에관한허가구역'으로 표시됩니다.
  https://www.eum.go.kr

다만 매도 시점을 계획하실 때는 영향이 있을 수 있습니다. 허가 신청 → 허가 → 계약으로
이어지는 리드타임이 양도 시기를 밀어, 연도별로 달라지는 세율 구간을 넘길 수 있습니다."""
