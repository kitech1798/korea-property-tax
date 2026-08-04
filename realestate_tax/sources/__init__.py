"""외부 데이터 소스 클라이언트.

라이선스 판정 (2026-08-04 원문 확인)
    ✅ 건축HUB 건축물대장 (data.go.kr 15134735) — 이용허락범위 제한 없음, 자동승인
    ✅ 행정표준코드 법정동코드 (15077871) — 제한 없음, 자동승인
    ⛔ 공동주택가격정보 (15052271) — 공공누리 제4유형 / CC BY-NC-ND.
       '변경금지'가 "형식의 변경"까지 포함해 DB 적재 자체가 걸린다. 쓰지 않는다.
    ⛔ realtyprice.kr — Open API 없음, 저작권 전면 유보. 사용자 안내 링크로만.
"""

from .building_hub import (
    Building,
    BuildingHubError,
    HousePrice,
    ParcelKey,
    Unit,
    UnitPrice,
    coverage,
    join_units_with_prices,
    latest_price_by_pk,
    parse_prices,
    parse_units,
)
from .juso import AddressMatch, JusoError, ParcelHint, UnsafeKeyword
from .resolve import (
    LookupOutcome,
    ParcelLookup,
    ParcelProbe,
    dong_sort_key,
    lookup_by_address,
    probe_address,
    search_addresses,
    unit_price_of,
    units_in_dong,
)

__all__ = [
    "AddressMatch",
    "Building",
    "BuildingHubError",
    "HousePrice",
    "JusoError",
    "LookupOutcome",
    "ParcelHint",
    "ParcelKey",
    "ParcelLookup",
    "ParcelProbe",
    "Unit",
    "UnitPrice",
    "UnsafeKeyword",
    "coverage",
    "dong_sort_key",
    "join_units_with_prices",
    "latest_price_by_pk",
    "lookup_by_address",
    "parse_prices",
    "parse_units",
    "probe_address",
    "search_addresses",
    "unit_price_of",
    "units_in_dong",
]
