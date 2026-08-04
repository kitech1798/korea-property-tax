"""국토교통부 건축HUB 건축물대장 API 클라이언트.

왜 이 경로인가 (다른 길이 전부 막혀서다)
    · 공동주택가격정보(data.go.kr 15052271)는 **공공누리 제4유형 / CC BY-NC-ND**다.
      제4유형 조문이 "영리행위와 직접 또는 **간접**으로 관련된 행위"를 금지하므로
      무료 서비스라도 블로그·상담 연계가 있으면 걸린다. 게다가 "내용상의 변경
      뿐만 아니라 **형식의 변경**"까지 금지해서, CSV를 DB에 적재하는 것 자체가 위반이다.
    · 한국부동산원 R-ONE Open API는 목록이 비어 있고, 공공데이터포털판은 지수·동향 통계뿐이다.
      공시가격의 법적 공시 주체는 **국토교통부장관**이고 부동산원은 조사·산정 수탁기관이라,
      부동산원 쪽을 파도 원본은 나오지 않는다.
    · realtyprice.kr(부동산공시가격 알리미)은 Open API가 없고 저작권을 전면 유보하며,
      검색 결과가 JS 동적이라 딥링크 URL조차 없다.

    남은 유일한 합법 경로가 건축HUB다 — **이용허락범위 제한 없음**, 개발·운영 모두 자동승인.

호 단위 도달 경로
    주소 → sigunguCd + bjdongCd + bun + ji
      ├─ getBrExposPubuseAreaInfo → dongNm(동), hoNm(호), area, mgmBldrgstPk
      └─ getBrHsprcInfo           → mgmBldrgstPk, crtnDay(기준일), hsprc(주택가격)
                     ↓ mgmBldrgstPk 조인
               동 + 호 + 전용면적 + 공시가격

⚠️ 검증되지 않은 전제
    `hsprc`의 실제 채움률과, 그 값이 공동주택공시가격과 동일한지는 인증키 없이 확인할 수 없었다.
    `tools/verify_hub.py`로 먼저 검증한 뒤에 이 경로를 신뢰하라. 검증 전에는
    사용자 직접 입력이 1차 경로다.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

BASE = "https://apis.data.go.kr/1613000/BldRgstHubService"
USER_AGENT = "realestate-tax-consult/0.1"
TIMEOUT = 20

# 대지구분코드. PNU 관례(1=대지, 2=산)와 이 API의 코드계가 다르다.
# PNU를 잘라 그대로 넣으면 전건 조회에 실패한다 — 실제로 걸려 넘어지는 함정이다.
PLAT_LAND = "0"
"""대지"""
PLAT_MOUNTAIN = "1"
"""산"""
PLAT_BLOCK = "2"
"""블록"""

PNU_TO_PLAT_GB = {"1": PLAT_LAND, "2": PLAT_MOUNTAIN}
"""PNU 11번째 자리 → 건축HUB platGbCd 매핑."""


class BuildingHubError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ParcelKey:
    """필지 하나를 가리키는 키. 건축HUB는 PNU 통짜가 아니라 분해된 형태를 받는다."""

    sigungu_cd: str
    """시군구코드 5자리."""
    bjdong_cd: str
    """법정동코드 5자리(법정동코드 10자리의 뒤 5자리)."""
    bun: str
    """번 4자리. 앞을 0으로 채운다."""
    ji: str = "0000"
    """지 4자리."""
    plat_gb_cd: str = PLAT_LAND

    @classmethod
    def from_pnu(cls, pnu: str) -> "ParcelKey":
        """PNU 19자리(법정동코드10 + 대지구분1 + 본번4 + 부번4)에서 만든다."""
        if len(pnu) != 19 or not pnu.isdigit():
            raise ValueError(f"PNU는 숫자 19자리여야 한다: {pnu!r}")
        legal_dong, gb, bun, ji = pnu[:10], pnu[10], pnu[11:15], pnu[15:19]
        return cls(
            sigungu_cd=legal_dong[:5],
            bjdong_cd=legal_dong[5:],
            bun=bun,
            ji=ji,
            plat_gb_cd=PNU_TO_PLAT_GB.get(gb, PLAT_LAND),
        )

    @classmethod
    def from_parts(
        cls, legal_dong_code: str, bun: int | str, ji: int | str = 0, *, mountain: bool = False
    ) -> "ParcelKey":
        if len(legal_dong_code) != 10:
            raise ValueError(f"법정동코드는 10자리여야 한다: {legal_dong_code!r}")
        return cls(
            sigungu_cd=legal_dong_code[:5],
            bjdong_cd=legal_dong_code[5:],
            bun=f"{int(bun):04d}",
            ji=f"{int(ji):04d}",
            plat_gb_cd=PLAT_MOUNTAIN if mountain else PLAT_LAND,
        )

    def as_params(self) -> dict[str, str]:
        return {
            "sigunguCd": self.sigungu_cd,
            "bjdongCd": self.bjdong_cd,
            "platGbCd": self.plat_gb_cd,
            "bun": self.bun,
            "ji": self.ji,
        }


@dataclass(frozen=True, slots=True)
class Unit:
    """전유부 한 호."""

    mgm_pk: str
    """관리건축물대장PK. 주택가격과의 조인 키."""
    dong_nm: str
    ho_nm: str
    floor_no: str = ""
    area_m2: float | None = None
    building_nm: str = ""

    @property
    def label_ko(self) -> str:
        parts = [p for p in (self.building_nm, self.dong_nm, self.ho_nm) if p]
        return " ".join(parts) or self.mgm_pk


@dataclass(frozen=True, slots=True)
class HousePrice:
    """건축물대장 주택가격 한 건. 연도별로 여러 건이 쌓인다.

    ⚠️ 기준일 필드는 `stdDay`다. `crtnDay`는 **대장 생성일**이라 한 단지의 모든 행이
       같은 값을 갖는다(예: 20220813). 그걸 기준일로 쓰면 연도 필터가 통째로 무너져
       2026년 조회가 0건이 된다. 실호출로 확인한 사실이다.
    """

    mgm_pk: str
    base_date: date | None
    """stdDay(공시기준일). 매년 1월 1일이다."""
    price: int
    created_on: date | None = None
    """crtnDay(대장 생성일). 기준일이 아니다 — 혼동을 막으려 별도 필드로 둔다."""

    @property
    def year(self) -> int | None:
        return self.base_date.year if self.base_date else None


@dataclass(frozen=True, slots=True)
class Building:
    """표제부 한 동.

    ★ 이 단계가 **비싼 조회를 피하는 관문**이다 (2026-08-04 실측).
        표제부      940호 단지에서   10행  0.2초
        전유공용면적 같은 단지에서  940호  27.1초
        주택가격    같은 단지에서 17,309행 41.0초

      "이 지번이 맞는가"와 "동이 몇 개인가"는 0.2초에 답할 수 있다.
      그걸 27초짜리 조회로 확인하면 후보 지번이 둘일 때 1분을 버린다.
    """

    dong_nm: str
    building_nm: str = ""
    main_purpose: str = ""
    household_count: int = 0
    mgm_pk: str = ""

    @property
    def is_house(self) -> bool:
        """공동주택·단독주택만 주택이다. 근린생활시설·업무시설은 아니다."""
        return "주택" in self.main_purpose


@dataclass(frozen=True, slots=True)
class UnitPrice:
    """동 + 호 + 공시가격. 이 서비스가 최종적으로 원하는 형태."""

    unit: Unit
    price: HousePrice | None

    @property
    def is_resolved(self) -> bool:
        return self.price is not None and self.price.price > 0


# --------------------------------------------------------------------------
# 호출
# --------------------------------------------------------------------------


def _service_key(explicit: str | None = None) -> str:
    key = explicit or os.environ.get("DATA_GO_KR_KEY")
    if not key:
        raise BuildingHubError(
            "공공데이터포털 인증키가 없다. 환경변수 DATA_GO_KR_KEY를 설정하거나 "
            "service_key 인자로 넘겨라.\n"
            "  발급: https://www.data.go.kr/data/15134735/openapi.do (자동승인)"
        )
    return key


def call(
    operation: str,
    params: Mapping[str, Any],
    *,
    service_key: str | None = None,
    rows: int = 100,
    page: int = 1,
) -> list[dict[str, Any]]:
    """오퍼레이션 하나를 **한 페이지만** 호출한다. 보통은 `call_all`을 써라.

    공공데이터포털은 오류도 HTTP 200 + XML로 내려주는 일이 잦다.
    JSON 파싱 실패를 그대로 흘리면 원인을 못 찾으므로 여기서 명시적으로 잡는다.
    """
    return _call_page(operation, params, service_key=service_key, rows=rows, page=page)[0]


def call_all(
    operation: str,
    params: Mapping[str, Any],
    *,
    service_key: str | None = None,
    page_size: int = 1000,
    max_records: int = 50_000,
) -> list[dict[str, Any]]:
    """전 페이지를 훑는다.

    ★ 이걸 안 하면 조인이 조용히 실패한다.
      전유공용면적은 호당 약 6행(전유+공용+부속), 주택가격은 호당 19행(2008~2026 이력)이
      쌓인다. 그래서 각 오퍼레이션의 1페이지는 **서로 다른 호 집합**을 덮고,
      `mgmBldrgstPk` 교집합이 비어 채움률 0%가 나온다.
      실제로 압구정 한양1차(936호)에서 1페이지만 받으면 0%, 전수를 받으면 100%였다.
    """
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        items, total = _call_page(
            operation, params, service_key=service_key, rows=page_size, page=page
        )
        out.extend(items)
        if not items or len(out) >= min(total, max_records):
            break
        page += 1
    return out


def _call_page(
    operation: str,
    params: Mapping[str, Any],
    *,
    service_key: str | None,
    rows: int,
    page: int,
) -> tuple[list[dict[str, Any]], int]:
    query = {
        "serviceKey": _service_key(service_key),
        "_type": "json",
        "numOfRows": rows,
        "pageNo": page,
        **{k: v for k, v in params.items() if v not in (None, "")},
    }
    url = f"{BASE}/{operation}?" + urllib.parse.urlencode(query, encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise BuildingHubError(f"호출 실패: {operation}\n  {exc}") from exc

    stripped = raw.lstrip()
    if not stripped.startswith(("{", "[")):
        raise BuildingHubError(
            f"JSON이 아닌 응답(인증키 미승인·한도초과 의심): {operation}\n"
            f"  앞부분: {stripped[:300]}"
        )

    doc = json.loads(raw)
    body = doc.get("response", {}).get("body")
    header = doc.get("response", {}).get("header", {})
    if body is None:
        raise BuildingHubError(
            f"응답 본문이 없다: {operation}\n"
            f"  resultCode={header.get('resultCode')} {header.get('resultMsg')}"
        )

    total = _to_int(body.get("totalCount")) or 0
    items = body.get("items")
    if not items:
        return [], total
    item = items.get("item") if isinstance(items, Mapping) else items
    if item is None:
        return [], total
    return (item if isinstance(item, list) else [item]), total


# --------------------------------------------------------------------------
# 파싱 — 호출과 분리해 인증키 없이도 테스트할 수 있게 한다
# --------------------------------------------------------------------------


def _to_int(raw: Any) -> int | None:
    if raw in (None, "", " "):
        return None
    try:
        return int(float(str(raw).replace(",", "")))
    except ValueError:
        return None


def _to_float(raw: Any) -> float | None:
    if raw in (None, "", " "):
        return None
    try:
        return float(str(raw).replace(",", ""))
    except ValueError:
        return None


def _to_date(raw: Any) -> date | None:
    text = str(raw or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def parse_units(items: Iterable[Mapping[str, Any]]) -> tuple[Unit, ...]:
    """전유공용면적 응답 → 전유부 호 목록.

    공용부(exposPubuseGbCdNm='공용')는 사람이 사는 호가 아니므로 걸러낸다.
    걸러내지 않으면 호 선택 드롭다운에 계단실·기계실이 섞여 나온다.
    """
    units: dict[str, Unit] = {}
    for it in items:
        if str(it.get("exposPubuseGbCdNm", "")).strip() == "공용":
            continue
        pk = str(it.get("mgmBldrgstPk", "")).strip()
        ho = str(it.get("hoNm", "")).strip()
        if not pk or not ho:
            continue
        # 같은 호에 주용도·부속용도 행이 여러 개 오므로 면적을 합산한다
        prev = units.get(pk)
        area = _to_float(it.get("area"))
        if prev is not None:
            merged = (prev.area_m2 or 0) + (area or 0)
            units[pk] = Unit(
                mgm_pk=pk,
                dong_nm=prev.dong_nm,
                ho_nm=prev.ho_nm,
                floor_no=prev.floor_no,
                area_m2=merged or None,
                building_nm=prev.building_nm,
            )
            continue
        units[pk] = Unit(
            mgm_pk=pk,
            dong_nm=str(it.get("dongNm", "")).strip(),
            ho_nm=ho,
            floor_no=str(it.get("flrNoNm") or it.get("flrNo") or "").strip(),
            area_m2=area,
            building_nm=str(it.get("bldNm", "")).strip(),
        )
    return tuple(units.values())


def parse_prices(items: Iterable[Mapping[str, Any]]) -> tuple[HousePrice, ...]:
    """주택가격 응답 파싱.

    응답에는 `dongNm`·`hoNm`이 없다. 호 단위 식별은 `mgmBldrgstPk`로만 가능하고,
    그 PK를 전유공용면적 응답과 조인해야 "몇 동 몇 호의 공시가격"이 된다.
    실호출 검증(압구정 한양1차, 936호): 전유 PK와 가격 PK가 **완전히 일치**했다.
    """
    out: list[HousePrice] = []
    for it in items:
        pk = str(it.get("mgmBldrgstPk", "")).strip()
        price = _to_int(it.get("hsprc"))
        if not pk or price is None:
            continue
        out.append(
            HousePrice(
                mgm_pk=pk,
                base_date=_to_date(it.get("stdDay")),
                price=price,
                created_on=_to_date(it.get("crtnDay")),
            )
        )
    return tuple(out)


def latest_price_by_pk(
    prices: Sequence[HousePrice], *, year: int | None = None
) -> dict[str, HousePrice]:
    """PK별 최신(또는 지정 연도) 주택가격.

    주택가격은 연도별 이력으로 쌓이므로 그냥 첫 건을 쓰면 몇 년 전 값을 집을 수 있다.
    과세기준일이 매년 6월 1일이고 공시기준일이 1월 1일이므로, 해당 연도 값을 써야 한다.
    """
    best: dict[str, HousePrice] = {}
    for p in prices:
        if year is not None and p.year != year:
            continue
        current = best.get(p.mgm_pk)
        if current is None:
            best[p.mgm_pk] = p
            continue
        if p.base_date and current.base_date and p.base_date > current.base_date:
            best[p.mgm_pk] = p
    return best


def join_units_with_prices(
    units: Sequence[Unit], prices: Sequence[HousePrice], *, year: int | None = None
) -> tuple[UnitPrice, ...]:
    """동·호와 공시가격을 mgmBldrgstPk로 잇는다. 값이 없으면 None으로 남긴다.

    없는 값을 0이나 단지 평균으로 채우지 않는다. 9억/14억 경계에서 그런 대체값은
    세액을 0원 ↔ 수백만원으로 뒤집는다.
    """
    by_pk = latest_price_by_pk(prices, year=year)
    return tuple(UnitPrice(unit=u, price=by_pk.get(u.mgm_pk)) for u in units)


# --------------------------------------------------------------------------
# 고수준 조회
# --------------------------------------------------------------------------


def parse_buildings(items: Iterable[Mapping[str, Any]]) -> tuple[Building, ...]:
    """표제부 응답 → 동 목록. 동명이 빈 행(단독건물)도 살린다."""
    out: list[Building] = []
    seen: set[str] = set()
    for it in items:
        dong = str(it.get("dongNm") or "").strip()
        pk = str(it.get("mgmBldrgstPk") or "").strip()
        if pk and pk in seen:
            continue
        seen.add(pk)
        out.append(
            Building(
                dong_nm=dong,
                building_nm=str(it.get("bldNm") or "").strip(),
                main_purpose=str(it.get("mainPurpsCdNm") or "").strip(),
                household_count=_to_int(it.get("hhldCnt")) or 0,
                mgm_pk=pk,
            )
        )
    return tuple(out)


def fetch_buildings(key: ParcelKey, *, service_key: str | None = None) -> tuple[Building, ...]:
    """표제부(동 목록). **0.2초.** 지번이 맞는지 먼저 여기로 물어라."""
    return parse_buildings(
        call_all("getBrTitleInfo", key.as_params(), service_key=service_key)
    )


def fetch_units(
    key: ParcelKey, *, dong_nm: str = "", service_key: str | None = None
) -> tuple[Unit, ...]:
    """전유부(호 목록).

    `dong_nm`을 주면 그 동만 받는다 — 940호 단지에서 27.1초 → 2.7초.
    (주택가격 API는 같은 필터를 **무시하고** 전건을 돌려준다. 실측으로 확인했다.)
    """
    params = dict(key.as_params())
    if dong_nm:
        params["dongNm"] = dong_nm
    return parse_units(
        call_all("getBrExposPubuseAreaInfo", params, service_key=service_key)
    )


def fetch_prices(key: ParcelKey, *, service_key: str | None = None) -> tuple[HousePrice, ...]:
    """주택가격. 단지 전건을 받아야 한다 — 동·호 필터가 먹지 않고 서버가 1,000행에서
    페이지 상한을 건다(page_size를 20,000으로 올려도 동일). 940호 단지에서 41초.
    그래서 **사용자가 호를 고른 뒤에** 부르고, 결과는 캐시한다."""
    return parse_prices(
        call_all("getBrHsprcInfo", key.as_params(), service_key=service_key)
    )


def lookup_units_with_price(
    key: ParcelKey, *, year: int | None = None, service_key: str | None = None
) -> tuple[UnitPrice, ...]:
    """필지 하나의 동·호·공시가격을 한 번에.

    조회에 성공해도 사용자에게 '수정' 경로를 항상 열어둬야 한다.
    건축물대장 주택가격이 공동주택공시가격과 언제나 같다는 보장이 검증되지 않았다.
    """
    units = fetch_units(key, service_key=service_key)
    prices = fetch_prices(key, service_key=service_key)
    return join_units_with_prices(units, prices, year=year)


def coverage(results: Sequence[UnitPrice]) -> float:
    """공시가격이 실제로 채워진 비율. 이 값이 낮으면 자동조회를 신뢰할 수 없다."""
    if not results:
        return 0.0
    return sum(1 for r in results if r.is_resolved) / len(results)
