"""주소 한 줄 → 동·호·공시가격.

두 API를 잇는 유일한 층이다. juso와 building_hub는 서로를 모르고, 여기서만 만난다.

    "압구정로 113"
      → juso.search_address   : 법정동코드 + 지번 후보들
      → building_hub          : 동·호 + 공시가격

★ 이 모듈의 존재 이유는 **조회 실패를 조용히 넘기지 않는 것**이다.
  자동조회가 0건이면 화면은 반드시 수동입력으로 흘러야 한다. 빈 목록을 그냥 그리면
  사용자는 "이 아파트는 지원 안 하나 보다"라고 생각하고 떠난다 —
  실제로는 지번 후보 하나만 더 넣으면 됐는데도.

★ 비용 순서 (2026-08-04 실측, 940호 단지 기준)
      표제부       0.2초   ← 지번이 맞는지, 동이 몇 개인지
      전유부(동별) 2.7초   ← 그 동의 호 목록
      주택가격    41.0초   ← 단지 전건. 필터가 안 먹고 서버가 1,000행 상한

  그래서 **싼 것부터 묻는다**. 지번 후보가 둘일 때 27초짜리 조회를 두 번 돌리면
  1분을 버리는데, 표제부로 물으면 0.4초에 판가름난다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Sequence

from . import building_hub as hub
from . import juso

__all__ = [
    "LookupOutcome",
    "ParcelLookup",
    "ParcelProbe",
    "dong_sort_key",
    "lookup_by_address",
    "probe_address",
    "search_addresses",
    "unit_price_of",
    "units_in_dong",
]


class LookupOutcome(StrEnum):
    OK = "ok"
    """공시가격이 채워진 호가 있다."""
    UNITS_ONLY = "units_only"
    """호는 찾았지만 그 해 공시가격이 없다. 신축·미공시일 수 있다."""
    NO_UNITS = "no_units"
    """전유부가 없다. 단독주택·오피스거나 대장 지번이 다르다."""
    ERROR = "error"
    """API 호출 자체가 실패했다."""


@dataclass(frozen=True, slots=True)
class ParcelLookup:
    """필지 하나의 조회 결과와 **그 결과에 이르는 과정**."""

    address: juso.AddressMatch
    outcome: LookupOutcome
    units: tuple[hub.UnitPrice, ...] = ()
    hint_used: juso.ParcelHint | None = None
    tried: tuple[tuple[juso.ParcelHint, int], ...] = ()
    """(시도한 후보, 나온 전유부 건수). 실패 원인을 화면에 설명하기 위해 남긴다."""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is LookupOutcome.OK

    @property
    def coverage(self) -> float:
        return hub.coverage(self.units)

    @property
    def dong_names(self) -> tuple[str, ...]:
        """동 목록. 숫자 동은 숫자로 정렬한다('10동'이 '2동'보다 뒤)."""
        names = {u.unit.dong_nm for u in self.units if u.unit.dong_nm}
        return tuple(sorted(names, key=_dong_sort_key))

    def units_in(self, dong_nm: str = "") -> tuple[hub.UnitPrice, ...]:
        rows = [u for u in self.units if not dong_nm or u.unit.dong_nm == dong_nm]
        return tuple(sorted(rows, key=lambda u: _ho_sort_key(u.unit.ho_nm)))

    def message_ko(self) -> str:
        """사용자에게 보여줄 한 줄. 실패도 **다음 행동**으로 이어지게 쓴다."""
        if self.outcome is LookupOutcome.OK:
            return f"{len(self.units)}개 호를 찾았습니다 (공시가격 {self.coverage:.0%} 확인)."
        if self.outcome is LookupOutcome.UNITS_ONLY:
            return (
                f"{len(self.units)}개 호를 찾았지만 해당 연도 공시가격이 없습니다. "
                "공시 전이거나 신축일 수 있습니다 — 금액은 직접 입력해주세요."
            )
        if self.outcome is LookupOutcome.ERROR:
            return f"조회 중 오류가 발생했습니다. 직접 입력해주세요.\n{self.error}"
        tried = ", ".join(f"{h.label_ko}({h.source})" for h, _ in self.tried) or "없음"
        return (
            "건축물대장에서 이 주소의 호별 정보를 찾지 못했습니다. "
            "단독주택·오피스이거나 대장 지번이 다를 수 있습니다.\n"
            f"  시도한 지번: {tried}\n"
            "  공시가격을 직접 입력하시면 계산은 그대로 진행됩니다."
        )


def dong_sort_key(name: str) -> tuple[int, str]:
    """'10동'이 '2동'보다 뒤에 오게. 문자열 정렬이면 사용자가 자기 동을 못 찾는다."""
    digits = "".join(c for c in name if c.isdigit())
    return (int(digits) if digits else 10**9, name)


_dong_sort_key = dong_sort_key
_ho_sort_key = dong_sort_key


@dataclass(frozen=True, slots=True)
class ParcelProbe:
    """싼 조회(표제부)만으로 알아낸 것.

    "이 주소가 자동조회 되는가"를 0.2초에 답한다. 화면은 이걸 먼저 그리고,
    비싼 조회는 사용자가 동을 고른 뒤로 미룬다.
    """

    address: juso.AddressMatch
    hint: juso.ParcelHint | None = None
    parcel: hub.ParcelKey | None = None
    buildings: tuple[hub.Building, ...] = ()
    tried: tuple[tuple[juso.ParcelHint, int], ...] = ()
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.parcel is not None and bool(self.buildings)

    @property
    def complex_name(self) -> str:
        for b in self.buildings:
            if b.building_nm:
                return b.building_nm.split(" 제")[0].strip()
        return self.address.building_nm

    @property
    def household_count(self) -> int:
        return sum(b.household_count for b in self.buildings)

    @property
    def dong_names(self) -> tuple[str, ...]:
        names = {b.dong_nm for b in self.buildings if b.dong_nm}
        return tuple(sorted(names, key=dong_sort_key))

    @property
    def has_house(self) -> bool:
        return any(b.is_house for b in self.buildings)

    def message_ko(self) -> str:
        if not self.ok:
            tried = ", ".join(f"{h.label_ko}({h.source})" for h, _ in self.tried) or "없음"
            return (
                "건축물대장에 이 지번의 건물이 없습니다. "
                "공시가격은 직접 입력해주세요.\n"
                f"  시도한 지번: {tried}"
                + (f"\n  {self.error}" if self.error else "")
            )
        if not self.has_house:
            purposes = ", ".join(sorted({b.main_purpose for b in self.buildings if b.main_purpose}))
            return f"주택이 아닙니다({purposes}). 이 서비스는 주택만 계산합니다."
        head = self.complex_name or "건물"
        dongs = len(self.dong_names)
        got = f"{head} · {dongs}개 동" if dongs else head
        return f"{got} · {self.household_count:,}세대"


def probe_address(
    address: juso.AddressMatch, *, service_key: str | None = None
) -> ParcelProbe:
    """지번 후보를 **표제부로만** 검증한다. 후보 하나당 0.2초.

    `lookup_by_address`가 후보마다 전유부+주택가격을 돌리면 후보 2개에 2분이 넘는다.
    여기서 먼저 걸러 두면 비싼 조회는 정확히 한 번만 나간다.
    """
    hints = address.parcel_hints()
    if not hints:
        return ParcelProbe(address=address, error="주소에서 지번을 얻지 못했습니다.")

    tried: list[tuple[juso.ParcelHint, int]] = []
    last_error = ""
    for hint in hints:
        key = hub.ParcelKey.from_parts(
            address.legal_dong_code, hint.bun, hint.ji, mountain=hint.mountain
        )
        try:
            buildings = hub.fetch_buildings(key, service_key=service_key)
        except Exception as exc:
            tried.append((hint, 0))
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        tried.append((hint, len(buildings)))
        if buildings:
            return ParcelProbe(
                address=address,
                hint=hint,
                parcel=key,
                buildings=buildings,
                tried=tuple(tried),
            )
    return ParcelProbe(address=address, tried=tuple(tried), error=last_error)


def units_in_dong(
    parcel: hub.ParcelKey, dong_nm: str = "", *, service_key: str | None = None
) -> tuple[hub.Unit, ...]:
    """한 동의 호 목록. 동을 지정하면 2.7초, 안 하면 27초다."""
    units = hub.fetch_units(parcel, dong_nm=dong_nm, service_key=service_key)
    return tuple(sorted(units, key=lambda u: dong_sort_key(u.ho_nm)))


def search_addresses(
    keyword: str, *, per_page: int = 10, confm_key: str | None = None
) -> tuple[juso.AddressMatch, ...]:
    """주소 검색. 화면의 1단계."""
    return juso.search_address(keyword, per_page=per_page, confm_key=confm_key)


def lookup_by_address(
    address: juso.AddressMatch,
    *,
    year: int | None = None,
    service_key: str | None = None,
    probe: "Callable[..., ParcelProbe] | None" = None,
) -> ParcelLookup:
    """주소 하나를 필지로 풀어 동·호·공시가격을 가져온다. **느리다(≈70초).**

    지번 후보 판정은 `probe_address`(표제부, 0.2초)에 맡기고, 비싼 조회는
    확정된 지번으로 딱 한 번만 나간다. 화면에서는 이걸 통째로 부르지 말고
    `probe_address` → `units_in_dong` → (호 선택 후) 가격 순으로 나눠 불러라.

    `probe`는 화면 캐시를 끼우기 위한 구멍이다. Streamlit이 표제부 결과를
    캐시해 두고 넘겨주면 같은 단지를 다시 물어보지 않는다.
    """
    found = probe(address, service_key=service_key) if probe is not None else probe_address(
        address, service_key=service_key
    )
    if not found.ok or found.parcel is None:
        return ParcelLookup(
            address=address,
            outcome=LookupOutcome.ERROR if found.error else LookupOutcome.NO_UNITS,
            tried=found.tried,
            error=found.error,
        )

    try:
        units = hub.lookup_units_with_price(
            found.parcel, year=year, service_key=service_key
        )
    except Exception as exc:
        return ParcelLookup(
            address=address,
            outcome=LookupOutcome.ERROR,
            hint_used=found.hint,
            tried=found.tried,
            error=f"{type(exc).__name__}: {exc}",
        )

    if not units:
        return ParcelLookup(
            address=address,
            outcome=LookupOutcome.NO_UNITS,
            hint_used=found.hint,
            tried=found.tried,
        )
    outcome = LookupOutcome.OK if hub.coverage(units) > 0 else LookupOutcome.UNITS_ONLY
    return ParcelLookup(
        address=address,
        outcome=outcome,
        units=units,
        hint_used=found.hint,
        tried=found.tried,
    )


def unit_price_of(units: Sequence[hub.UnitPrice], dong_nm: str, ho_nm: str) -> int | None:
    """동·호로 공시가격 한 건을 집는다. 없으면 None — 0으로 뭉개지 않는다."""
    for u in units:
        if u.unit.dong_nm == dong_nm and u.unit.ho_nm == ho_nm:
            return u.price.price if u.is_resolved else None
    return None
