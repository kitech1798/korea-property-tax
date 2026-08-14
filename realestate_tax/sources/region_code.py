"""법정동명 → 법정동코드 (행정표준코드관리시스템, `apis.data.go.kr/1741000`).

★ 이 모듈이 생긴 이유 — **juso가 해외 IP에 응답하지 않는다.**

  2026-08-13 실측
      business.juso.go.kr   로컬(한국 회선) 0.3초 정상 / 배포 서버(미국 AWS) 20초 타임아웃
      apis.data.go.kr       같은 배포 서버에서 정상 응답

  주소 자동조회 사슬은 juso가 주는 **법정동코드 + 지번**에서 시작한다. juso가 죽으면
  사슬 전체가 죽는다. 그런데 필요한 두 값 중 **법정동코드는 도달 가능한 호스트에서
  받을 수 있다.** 지번은 사용자가 안다 — 등기부·계약서에 적혀 있다.

  그래서 이 모듈은 juso를 대체하지 않는다. **juso가 못 갈 때 사슬을 잇는다.**

      juso 있음   "북일로 70"        → 법정동코드 + 지번 (한 줄)
      juso 없음   "신부동" + "978"   → 법정동코드(여기) + 지번(사용자)

⚠️ 도로명은 못 찾는다. 이 API는 **법정동명**만 안다("북일로" → 0건).
   화면은 그 사실을 먼저 말해야 한다 — 도로명을 넣고 0건을 보면
   사용자는 "이 주소는 지원 안 하나 보다"라고 생각하고 떠난다.

⚠️ data.go.kr 활용신청의 **허용 IP를 `*.*.*.*`로 두지 않으면 배포 서버에서 403**이다.
   1741000 계열에서 실제로 겪은 함정이다. 403은 타임아웃과 원인이 다르므로 구분해 알린다.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

BASE = "https://apis.data.go.kr/1741000/StanReginCd/getStanReginCdList"
TIMEOUT = 15
USER_AGENT = "realestate-tax-consult/0.1"

__all__ = ["RegionMatch", "RegionCodeError", "search_dong"]


class RegionCodeError(RuntimeError):
    """조회 실패. `code`로 원인을 구분한다 — 사용자가 할 일이 다르다."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


@dataclass(frozen=True, slots=True)
class RegionMatch:
    """법정동 하나."""

    code: str
    """법정동코드 10자리. 이 엔진에서 지역 판정의 유일한 키다."""
    name: str
    """전체 표기 — "서울특별시 강남구 대치동"."""

    @property
    def label_ko(self) -> str:
        return f"{self.name}  ({self.code})"


def _key() -> str:
    key = os.environ.get("DATA_GO_KR_KEY", "")
    if not key:
        try:  # 배포 환경은 secrets로 온다
            import streamlit as st

            key = st.secrets.get("DATA_GO_KR_KEY", "")
        except Exception:
            key = ""
    if not key:
        raise RegionCodeError("NO_KEY", "DATA_GO_KR_KEY가 없습니다")
    return key


def search_dong(keyword: str, *, limit: int = 20) -> list[RegionMatch]:
    """법정동명으로 찾는다. **도로명은 찾지 못한다.**

    "대치동" · "천안시 동남구 신부동"처럼 동 이름이 들어가야 한다.
    """
    kw = (keyword or "").strip()
    if len(kw) < 2:
        return []

    query = {
        "ServiceKey": _key(),
        "type": "json",
        "numOfRows": limit,
        "pageNo": 1,
        "locatadd_nm": kw,
    }
    url = f"{BASE}?" + urllib.parse.urlencode(query, encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # 403은 허용 IP 설정 문제다. "네트워크 오류"로 뭉개면 엉뚱한 데를 고치게 된다.
        if exc.code == 403:
            raise RegionCodeError(
                "FORBIDDEN",
                "공공데이터포털이 접근을 거부했습니다(403). 활용신청의 허용 IP를 "
                "`*.*.*.*`로 바꿔주세요 — 배포 서버는 IP가 고정이 아닙니다.",
            ) from exc
        raise RegionCodeError("HTTP", f"호출 실패({exc.code})") from exc
    except Exception as exc:
        raise RegionCodeError("NETWORK", f"호출 실패: {exc}") from exc

    if not raw.strip():
        raise RegionCodeError("EMPTY", "포털이 빈 응답을 돌려줬습니다. 잠시 뒤 다시 시도해주세요")

    try:
        doc: Any = json.loads(raw)
    except json.JSONDecodeError:
        head = raw.lstrip()[:200].replace("\n", " ")
        raise RegionCodeError("NOT_JSON", f"JSON이 아닌 응답: {head}") from None

    return _parse(doc)


def _parse(doc: Any) -> list[RegionMatch]:
    """응답 모양이 두 가지다 — 결과가 있으면 `StanReginCd`, 없으면 `RESULT`만 온다."""
    rows: list[dict[str, Any]] = []
    node = doc.get("StanReginCd") if isinstance(doc, dict) else None
    if isinstance(node, list):
        for part in node:
            if isinstance(part, dict) and isinstance(part.get("row"), list):
                rows.extend(part["row"])

    out: list[RegionMatch] = []
    seen: set[str] = set()
    for row in rows:
        code = str(row.get("region_cd") or "").strip()
        name = str(row.get("locatadd_nm") or "").strip()
        # ⚠️ 폐지된 동도 함께 온다. `region_cd`가 10자리가 아니면 쓰지 않는다.
        if len(code) != 10 or not name or code in seen:
            continue
        seen.add(code)
        out.append(RegionMatch(code=code, name=name))
    return out
