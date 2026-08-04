"""공공데이터 API 키 일괄 점검.

인증키를 발급받은 직후 이걸 먼저 돌려라. 어느 API가 실제로 살아 있는지,
활용신청이 제대로 승인됐는지를 한 번에 확인한다.

공공데이터포털은 오류를 HTTP 200 + XML로 내려주는 일이 잦아서, 코드에서
"응답이 왔으니 성공"이라고 착각하기 쉽다. 여기서 그걸 명시적으로 갈라낸다.

실행
    $env:DATA_GO_KR_KEY = "발급키(Decoding 쪽)"
    python tools/check_apis.py
    python tools/check_apis.py --find-region 화성    # 법정동코드 조회
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TIMEOUT = 20
UA = "realestate-tax-consult/0.1"

# 강남구 일원동 — 공동주택이 확실히 있는 필지로 잡아 둔다.
SAMPLE = {"sigunguCd": "11680", "bjdongCd": "10800", "platGbCd": "0", "bun": "0680", "ji": "0000"}


@dataclass(frozen=True)
class Endpoint:
    label: str
    url: str
    params: dict
    signup: str
    why: str
    optional: bool = False


ENDPOINTS = (
    Endpoint(
        "① 법정동코드",
        "https://apis.data.go.kr/1741000/StanReginCd/getStanReginCdList",
        {"type": "json", "numOfRows": 3, "pageNo": 1, "locatadd_nm": "강남구"},
        "https://www.data.go.kr/data/15077871/openapi.do",
        "주소 드롭다운 · 조정대상지역 판정 키 · 화성 동탄구 코드 확정",
    ),
    Endpoint(
        "② 건축HUB 전유공용면적",
        "https://apis.data.go.kr/1613000/BldRgstHubService/getBrExposPubuseAreaInfo",
        {"_type": "json", "numOfRows": 5, "pageNo": 1, **SAMPLE},
        "https://www.data.go.kr/data/15134735/openapi.do",
        "동명 · 호명 · 전용면적",
    ),
    Endpoint(
        "② 건축HUB 주택가격 ★",
        "https://apis.data.go.kr/1613000/BldRgstHubService/getBrHsprcInfo",
        {"_type": "json", "numOfRows": 5, "pageNo": 1, **SAMPLE},
        "https://www.data.go.kr/data/15134735/openapi.do",
        "공시가격(주택가격) · 연도별 이력 — 이 서비스의 핵심",
    ),
    Endpoint(
        "③ 공동주택 기본정보",
        "https://apis.data.go.kr/1613000/AptBasisInfoServiceV3/getAphusBassInfoV3",
        {"_type": "json", "kaptCode": "A13822001"},
        "https://www.data.go.kr/data/15058453/openapi.do",
        "단지명 → 주소 변환 (단지명 검색 탭)",
        optional=True,
    ),
    Endpoint(
        "④ 아파트 매매 실거래가 상세",
        "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev",
        {"_type": "json", "LAWD_CD": "11680", "DEAL_YMD": "202606", "numOfRows": 5, "pageNo": 1},
        "https://www.data.go.kr/data/15126468/openapi.do",
        "취득가 입력 검증 · 시세 밴드 (호수는 안 나옴)",
        optional=True,
    ),
)


def probe(ep: Endpoint, key: str) -> tuple[bool, str]:
    url = f"{ep.url}?" + urllib.parse.urlencode(
        {"serviceKey": key, **ep.params}, encoding="utf-8"
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return False, f"네트워크 오류: {exc}"

    text = raw.lstrip()

    # 포털은 오류도 HTTP 200 + XML로 준다. 문구로 원인을 짚어준다.
    if "SERVICE_KEY_IS_NOT_REGISTERED" in raw or "SERVICE ACCESS DENIED" in raw:
        return False, "키가 이 서비스에 등록되지 않았다 → 활용신청 필요 (또는 Encoding/Decoding 키 혼동)"
    if "LIMITED_NUMBER_OF_SERVICE_REQUESTS" in raw:
        return False, "일일 트래픽 초과"
    if "SERVICE_KEY_IS_NOT_REGISTERED_ERROR" in raw or "등록되지 않은" in raw:
        return False, "미등록 키"
    if "HTTP ROUTING ERROR" in raw:
        return False, "엔드포인트 경로 오류 (오퍼레이션명 확인 필요)"

    if not text.startswith(("{", "[")):
        return False, f"JSON이 아닌 응답: {text[:160]}"

    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        return False, f"JSON 파싱 실패: {exc}"

    header = doc.get("response", {}).get("header", {})
    code = str(header.get("resultCode", "")).strip()
    if code and code not in ("00", "0"):
        return False, f"resultCode={code} {header.get('resultMsg', '')}"

    body = doc.get("response", {}).get("body") or doc.get("StanReginCd")
    if body is None:
        return False, f"본문 없음: {text[:160]}"

    count = _count(doc)
    return True, f"응답 정상 (레코드 {count}건)"


def _count(doc: dict) -> int:
    body = doc.get("response", {}).get("body")
    if isinstance(body, dict):
        items = body.get("items")
        if isinstance(items, dict):
            item = items.get("item")
            if isinstance(item, list):
                return len(item)
            return 1 if item else 0
        if isinstance(items, list):
            return len(items)
        return int(body.get("totalCount") or 0)
    # 법정동코드 API는 응답 구조가 다르다
    stan = doc.get("StanReginCd")
    if isinstance(stan, list):
        for node in stan:
            if isinstance(node, dict) and "row" in node:
                return len(node["row"])
    return 0


def find_region(key: str, keyword: str) -> int:
    """법정동코드 조회. 화성시 동탄구 코드를 확정하는 데 쓴다."""
    url = "https://apis.data.go.kr/1741000/StanReginCd/getStanReginCdList?" + urllib.parse.urlencode(
        {"serviceKey": key, "type": "json", "numOfRows": 1000, "pageNo": 1, "locatadd_nm": keyword},
        encoding="utf-8",
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        doc = json.loads(resp.read().decode("utf-8", errors="replace"))

    rows = []
    for node in doc.get("StanReginCd", []):
        if isinstance(node, dict) and "row" in node:
            rows = node["row"]
            break

    if not rows:
        print(f"'{keyword}' 결과 없음")
        return 1

    # 시군구 단위(코드 뒤 5자리가 00000)만 추려 보여준다
    print(f"■ '{keyword}' 검색 결과 중 시·군·구 단위\n")
    seen = set()
    for r in rows:
        code = str(r.get("region_cd", ""))
        name = r.get("locatadd_nm", "")
        if len(code) == 10 and code[5:] == "00000" and code[:5] not in seen:
            seen.add(code[:5])
            print(f"  {code[:5]}  {name}")

    print(
        "\n→ 조정대상지역 테이블에 채울 코드는 위 5자리다.\n"
        "  rulesets/v2026.08.03/reference/regulated_areas.yaml 의\n"
        "  code_unverified 항목과 undecidable_prefixes를 수정하라."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--find-region", help="법정동코드 조회 (예: 화성)")
    args = parser.parse_args(argv)

    key = os.environ.get("DATA_GO_KR_KEY")
    if not key:
        print(
            "환경변수 DATA_GO_KR_KEY가 없다.\n"
            '  $env:DATA_GO_KR_KEY = "발급키(Decoding 쪽)"\n\n'
            "신청 안내: docs/API_설정.md",
            file=sys.stderr,
        )
        return 2

    if "%" in key:
        print(
            "⚠️ 키에 '%'가 들어 있다. Encoding 키를 넣으신 것 같다.\n"
            "   이 코드는 URL 인코딩을 직접 하므로 **Decoding 키**를 써야 한다.\n",
            file=sys.stderr,
        )

    if args.find_region:
        return find_region(key, args.find_region)

    print("공공데이터 API 점검\n" + "=" * 64)
    failed_required = 0
    for ep in ENDPOINTS:
        ok, detail = probe(ep, key)
        mark = "✅" if ok else ("⚠️" if ep.optional else "❌")
        print(f"\n{mark} {ep.label}")
        print(f"   {detail}")
        print(f"   용도: {ep.why}")
        if not ok:
            print(f"   신청: {ep.signup}")
            if not ep.optional:
                failed_required += 1

    print("\n" + "=" * 64)
    if failed_required:
        print(f"필수 항목 {failed_required}건 실패. 활용신청을 확인하라 (자동승인이라 즉시 반영된다).")
        return 1

    print("필수 항목 전부 정상.\n")
    print("다음 단계 — 이걸 먼저 돌려라:")
    print("  python tools/verify_hub.py --dong 1168010100 --bun 1 --year 2026")
    print("  hsprc 채움률을 재고, 알리미와 값을 수동 대조한다.")
    print("  결과에 따라 자동조회를 1차 경로로 쓸지 보조로 쓸지가 갈린다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
