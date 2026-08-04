"""도로명주소 API (business.juso.go.kr).

이 서비스에서 juso가 하는 일은 **주소 한 줄 → 지번(필지)** 변환이다.
공시가격은 juso에 없다. 지번을 얻어 건축HUB로 넘겨야 금액이 나온다.

    사용자 입력 "압구정로 113"
      → juso addrLinkApi : 법정동코드 + 지번 본번/부번
      → 건축HUB          : 동·호 + 공시가격

⚠️ **API 유형이 둘이고 승인키가 따로다** (2026-08-04 실호출로 확인)
    addrLinkApi.do   도로명주소 검색  — 주소 → 지번. 이 서비스의 주 경로
    addrDetailApi.do 상세주소        — 건물키 → 동·층·호. **별도 신청**

    상세주소용 키가 없으면 addrDetailApi는 `E0001 승인되지 않은 KEY`를 돌려준다.
    키가 틀린 게 아니라 **신청하지 않은 유형**이라는 뜻이다.
    상세주소 API는 교차검증용이라 없어도 주 경로는 완성된다.

인증
    `confmKey`(승인키). business.juso.go.kr → 주소정보 자료제공 → 주소정보 API 연계.
    **개발승인키는 본인인증 없이 신청 즉시 발급된다**(가이드 3.1).
    ⚠️ 승인키는 **URL(또는 IP) 기준**으로 부여된다. 여러 도메인에서 쓰려면 각각 받아야 하고,
       그러지 않으면 서비스가 중지될 수 있다(가이드 3.1).
       (실측: 등록 IP와 다른 IP에서도 호출은 됐다. 그러나 약관상 보장이 아니므로
        배포 시 실제 서비스 URL로 키를 갱신해야 한다.)

    가이드에 공개된 테스트 키 `TESTJUSOGOKR`로 샘플 조회가 된다. 실제 서비스에는 쓰지 말 것.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

SEARCH_BASE = "https://business.juso.go.kr/addrlink/addrLinkApi.do"
"""도로명주소 검색. 주소 → 지번."""
BASE = "https://business.juso.go.kr/addrlink/addrDetailApi.do"
"""상세주소. **별도 승인 필요** — 없으면 E0001."""
TEST_KEY = "TESTJUSOGOKR"
"""가이드에 공개된 샘플 조회용 키. 운영에 쓰면 안 된다."""

USER_AGENT = "Mozilla/5.0 (compatible; realestate-tax-consult/0.1)"
TIMEOUT = 20

ERROR_MESSAGES = {
    "0": "정상",
    "-999": "시스템 에러",
    "E0001": "승인되지 않은 KEY 입니다. 승인키와 등록 URL/IP를 확인하세요.",
    "E0002": "행정구역코드(admCd)가 없습니다.",
    "E0003": "도로명코드(rnMgtSn)가 없습니다.",
    "E0004": "지하여부(udrtYn)가 없습니다.",
    "E0005": "건물본번(buldMnnm)이 없습니다.",
    "E0006": "건물부번(buldSlno)이 없습니다.",
}

# 가이드 §4.1 「API 차단 사례」.
# 이 단어·문자가 요청에 섞이면 WAF가 SQL Injection으로 보고 **차단**한다.
# 한 번 차단되면 그 뒤 정상 요청도 막히므로, 보내기 전에 걸러야 한다.
BLOCKED_KEYWORDS = (
    "or", "select", "insert", "delete", "update", "create",
    "drop", "exec", "union", "fetch", "declare", "truncate",
)
BLOCKED_CHARS = "<>=%"

_WORD = re.compile(r"[A-Za-z]+")


class JusoError(RuntimeError):
    """API가 오류 코드를 돌려줬을 때."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


class UnsafeKeyword(ValueError):
    """WAF에 차단당할 문자열. 보내기 전에 막는다."""


def assert_safe(text: str, field: str = "검색어") -> str:
    """차단 패턴 검사.

    가이드는 "제거 및 안내메세지 출력"을 권한다. 조용히 지우면 사용자는
    자기가 입력한 것과 다른 결과를 보게 되므로, 여기서는 **거부하고 알린다**.

    주의: SQL 예약어를 부분문자열로 잡으면 정상 주소가 막힌다
    ("서울"의 로마자 'Seoul'에 'or'가 없지만, 'Doran'·'Union' 같은 건물명은 있다).
    그래서 **단어 경계**로만 검사한다.
    """
    lowered = str(text).lower()

    bad_chars = [c for c in BLOCKED_CHARS if c in lowered]
    if bad_chars:
        raise UnsafeKeyword(
            f"{field}에 사용할 수 없는 문자가 있습니다: {' '.join(bad_chars)}\n"
            "  주소 검색에는 <, >, =, % 를 쓸 수 없습니다."
        )

    words = set(_WORD.findall(lowered))
    hits = sorted(words & set(BLOCKED_KEYWORDS))
    if hits:
        raise UnsafeKeyword(
            f"{field}에 사용할 수 없는 단어가 있습니다: {', '.join(hits)}\n"
            "  주소검색 서버가 보안 규칙으로 차단하므로 다른 표기로 입력해주세요."
        )
    return str(text)


@dataclass(frozen=True, slots=True)
class BuildingKey:
    """상세주소 조회에 필요한 건물 식별자.

    도로명주소 검색 API(addrLinkApi.do)나 주소DB의 출력값에서 그대로 가져온다.
    사용자가 직접 입력할 값이 아니다.
    """

    adm_cd: str
    """행정구역코드 10자리(= 법정동코드)."""
    rn_mgt_sn: str
    """도로명코드 12자리."""
    buld_mnnm: int
    """건물본번."""
    buld_slno: int = 0
    """건물부번."""
    udrt_yn: str = "0"
    """지하여부. 0=지상, 1=지하."""

    def as_params(self) -> dict[str, str]:
        return {
            "admCd": self.adm_cd,
            "rnMgtSn": self.rn_mgt_sn,
            "udrtYn": self.udrt_yn,
            "buldMnnm": str(self.buld_mnnm),
            "buldSlno": str(self.buld_slno),
        }

    @property
    def legal_dong_code(self) -> str:
        """조정대상지역 판정에 그대로 쓸 수 있다."""
        return self.adm_cd


@dataclass(frozen=True, slots=True)
class DetailAddress:
    """동·층·호 한 건."""

    dong_nm: str
    floor_nm: str
    ho_nm: str
    adm_cd: str = ""
    rn_mgt_sn: str = ""
    bd_mgt_sn: str = ""
    """건물관리번호. 가이드에는 필수로 적혀 있으나 실제 응답에서 누락되는 경우가 있다."""

    @property
    def label_ko(self) -> str:
        return " ".join(p for p in (self.dong_nm, self.ho_nm) if p) or self.floor_nm


@dataclass(frozen=True, slots=True)
class ParcelHint:
    """건축HUB에 넣어볼 필지 후보 하나.

    후보가 여럿인 이유는 §AddressMatch.parcel_hints 참조.
    """

    bun: int
    """지번 본번."""
    ji: int = 0
    """지번 부번."""
    mountain: bool = False
    source: str = ""
    """어느 필드에서 나왔는지. 조회 실패를 사용자에게 설명할 때 쓴다."""

    @property
    def label_ko(self) -> str:
        base = f"{'산 ' if self.mountain else ''}{self.bun}"
        return f"{base}-{self.ji}" if self.ji else base


@dataclass(frozen=True, slots=True)
class AddressMatch:
    """도로명주소 검색 결과 한 건."""

    road_addr: str
    jibun_addr: str
    legal_dong_code: str
    """admCd 10자리. 조정대상지역 판정에 그대로 쓴다."""
    building_nm: str = ""
    zip_no: str = ""
    lnbr_mnnm: int = 0
    lnbr_slno: int = 0
    mt_yn: str = "0"
    """0=대지, 1=산. **PNU 관례(1=대지)와 반대다.**"""
    bd_mgt_sn: str = ""
    """건물관리번호 25자리. 지번이 한 번 더 들어 있다."""
    detail_names: str = ""
    """detBdNmList — 동 이름 목록. 단지 규모를 가늠하는 힌트."""

    @property
    def sigungu_cd(self) -> str:
        return self.legal_dong_code[:5]

    @property
    def bjdong_cd(self) -> str:
        return self.legal_dong_code[5:]

    @property
    def label_ko(self) -> str:
        return f"{self.road_addr}" + (f" · {self.building_nm}" if self.building_nm else "")

    def parcel_hints(self) -> tuple[ParcelHint, ...]:
        """건축HUB에 시도할 필지 후보를 **우선순위 순으로**.

        ★ 왜 후보가 여럿인가 (2026-08-04 실호출로 확인한 사실)
          juso가 주는 지번은 두 곳에 있고, 그 둘이 **다를 수 있다**.

            lnbrMnnm/lnbrSlno  주소 표시용 대표지번
            bdMgtSn[11:19]     건물관리번호에 박힌 지번

          압구정로 201(현대아파트): 표시 458 → 건축HUB 0건 / 관리번호 456 → 1,370건
          왕십리로 410:            표시 1070 → 3,329건    / 관리번호 842 → 0건

          **어느 쪽도 항상 옳지 않다.** 하나만 골라 쓰면 대단지에서 조용히 0건이 되고,
          사용자는 "이 아파트는 지원 안 하나 보다"라고 생각하고 떠난다.
          그래서 후보를 다 시도하고, 전부 실패하면 실패를 분명히 알린다.
        """
        hints: list[ParcelHint] = []
        seen: set[tuple[int, int, bool]] = set()

        def add(bun: int, ji: int, mountain: bool, source: str) -> None:
            if bun <= 0:
                return
            key = (bun, ji, mountain)
            if key in seen:
                return
            seen.add(key)
            hints.append(ParcelHint(bun=bun, ji=ji, mountain=mountain, source=source))

        add(self.lnbr_mnnm, self.lnbr_slno, self.mt_yn == "1", "표시지번(lnbrMnnm)")

        sn = self.bd_mgt_sn
        if len(sn) == 25 and sn[:10] == self.legal_dong_code and sn[10:19].isdigit():
            # 건물관리번호 = 법정동코드(10) + 대지구분(1) + 본번(4) + 부번(4) + 일련번호(6)
            # 대지구분은 **PNU 관례**(1=대지, 2=산)라 mtYn과 반대다.
            add(int(sn[11:15]), int(sn[15:19]), sn[10] == "2", "건물관리번호(bdMgtSn)")

        return tuple(hints)


def parse_search(payload: Mapping[str, Any]) -> tuple[AddressMatch, ...]:
    """검색 응답 파싱. 호출과 분리해 승인키 없이도 테스트할 수 있게 한다."""
    results = payload.get("results", payload)
    common = results.get("common", {})
    code = str(common.get("errorCode", "")).strip()
    if code and code != "0":
        raise JusoError(code, str(common.get("errorMessage") or ERROR_MESSAGES.get(code, "")))

    rows = results.get("juso") or []
    if isinstance(rows, Mapping):
        rows = [rows]

    def num(raw: Any) -> int:
        text = str(raw or "").strip()
        return int(text) if text.isdigit() else 0

    return tuple(
        AddressMatch(
            road_addr=str(r.get("roadAddr") or "").strip(),
            jibun_addr=str(r.get("jibunAddr") or "").strip(),
            legal_dong_code=str(r.get("admCd") or "").strip(),
            building_nm=str(r.get("bdNm") or "").strip(),
            zip_no=str(r.get("zipNo") or "").strip(),
            lnbr_mnnm=num(r.get("lnbrMnnm")),
            lnbr_slno=num(r.get("lnbrSlno")),
            mt_yn=str(r.get("mtYn") or "0").strip() or "0",
            bd_mgt_sn=str(r.get("bdMgtSn") or "").strip(),
            detail_names=str(r.get("detBdNmList") or "").strip(),
        )
        for r in rows
    )


def search_address(
    keyword: str,
    *,
    page: int = 1,
    per_page: int = 10,
    confm_key: str | None = None,
) -> tuple[AddressMatch, ...]:
    """도로명주소·지번주소·건물명으로 검색한다.

    가이드가 요구하는 최소 길이(2자)와 WAF 차단 패턴을 **보내기 전에** 거른다.
    한 번 차단당하면 그 뒤 정상 요청도 막히기 때문이다.
    """
    text = str(keyword).strip()
    if len(text) < 2:
        raise UnsafeKeyword("검색어는 2자 이상 입력해주세요.")
    assert_safe(text, "검색어")

    payload = _get(
        SEARCH_BASE,
        {
            "confmKey": _confm_key(confm_key),
            "currentPage": str(max(1, page)),
            "countPerPage": str(max(1, min(per_page, 100))),
            "keyword": text,
            "resultType": "json",
        },
    )
    return parse_search(payload)


def _confm_key(explicit: str | None) -> str:
    key = explicit or os.environ.get("JUSO_CONFM_KEY")
    if not key:
        raise JusoError(
            "NO_KEY",
            "승인키가 없다. 환경변수 JUSO_CONFM_KEY를 설정하거나 confm_key 인자로 넘겨라.\n"
            "  신청: https://business.juso.go.kr → 주소정보 자료제공 → 주소정보 API 연계\n"
            "  (개발승인키는 본인인증 없이 즉시 발급)",
        )
    return key


def parse(payload: Mapping[str, Any]) -> tuple[DetailAddress, ...]:
    """응답 파싱. 호출과 분리해 승인키 없이도 테스트할 수 있게 한다."""
    results = payload.get("results", payload)
    common = results.get("common", {})
    code = str(common.get("errorCode", "")).strip()
    if code and code != "0":
        raise JusoError(code, str(common.get("errorMessage") or ERROR_MESSAGES.get(code, "")))

    rows = results.get("juso") or []
    if isinstance(rows, Mapping):
        rows = [rows]
    return tuple(
        DetailAddress(
            dong_nm=str(r.get("dongNm") or "").strip(),
            floor_nm=str(r.get("floorNm") or "").strip(),
            ho_nm=str(r.get("hoNm") or "").strip(),
            adm_cd=str(r.get("admCd") or "").strip(),
            rn_mgt_sn=str(r.get("rnMgtSn") or "").strip(),
            bd_mgt_sn=str(r.get("bdMgtSn") or "").strip(),
        )
        for r in rows
    )


def search(
    key: BuildingKey,
    *,
    search_type: str = "dong",
    dong_nm: str = "",
    confm_key: str | None = None,
) -> tuple[DetailAddress, ...]:
    """상세주소 조회. search_type은 'dong' 또는 'floorho'."""
    if search_type not in ("dong", "floorho"):
        raise ValueError(f"search_type은 dong 또는 floorho여야 한다: {search_type!r}")
    if dong_nm:
        assert_safe(dong_nm, "동 이름")

    params = {
        **key.as_params(),
        "confmKey": _confm_key(confm_key),
        "resultType": "json",
        "searchType": search_type,
    }
    if dong_nm:
        params["dongNm"] = dong_nm

    return parse(_get(BASE, params))


def _get(base: str, params: Mapping[str, str]) -> dict[str, Any]:
    """GET 후 JSON 파싱. 차단 응답을 오해하지 않도록 원인을 짚어준다."""
    url = f"{base}?" + urllib.parse.urlencode(dict(params), encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise JusoError("NETWORK", f"호출 실패: {exc}") from exc

    stripped = raw.lstrip()
    if not stripped.startswith("{"):
        # 차단당하면 JSON이 아니라 HTML 안내가 온다. 원인을 짚어준다.
        raise JusoError(
            "BLOCKED",
            "JSON이 아닌 응답이 왔다. 보안 규칙에 차단됐거나 승인키의 등록 URL/IP가 "
            f"현재 호출 환경과 다를 수 있다.\n  앞부분: {stripped[:200]}",
        )
    return json.loads(raw)


def dong_names(
    key: BuildingKey, *, confm_key: str | None = None
) -> tuple[str, ...]:
    """동 목록. 단독건물이면 빈 튜플이 나올 수 있다."""
    rows = search(key, search_type="dong", confm_key=confm_key)
    seen: list[str] = []
    for r in rows:
        if r.dong_nm and r.dong_nm not in seen:
            seen.append(r.dong_nm)
    return tuple(seen)


def units_of(
    key: BuildingKey, dong_nm: str = "", *, confm_key: str | None = None
) -> tuple[DetailAddress, ...]:
    """특정 동의 층·호 목록."""
    return search(
        key,
        search_type="floorho" if dong_nm else "dong",
        dong_nm=dong_nm,
        confm_key=confm_key,
    )


def cross_check(
    juso_units: Sequence[DetailAddress], hub_units: Iterable
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """주소 API와 건축물대장의 호 목록을 대조한다.

    두 소스가 독립적이라 어긋나면 둘 중 하나가 낡았다는 신호다.
    (juso에만 있는 호, 건축물대장에만 있는 호)를 돌려준다. 둘 다 비면 일치다.
    """
    def norm(text: str) -> str:
        return re.sub(r"\s+", "", str(text)).replace("호", "")

    a = {norm(u.ho_nm) for u in juso_units if u.ho_nm}
    b = {norm(getattr(u, "ho_nm", "")) for u in hub_units}
    b.discard("")
    return tuple(sorted(a - b)), tuple(sorted(b - a))
