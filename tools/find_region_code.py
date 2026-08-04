"""법정동코드 조회 — **인증키가 필요 없다.**

행정안전부 행정표준코드관리시스템(code.go.kr)의 법정동코드 조회 화면을 그대로 쓴다.
공공데이터포털 API(15077871)와 같은 원천이지만 키 발급 없이 즉시 쓸 수 있어,
조정대상지역 테이블을 갱신할 때 이쪽이 빠르다.

실제로 이 도구로 화성시 일반구 코드를 확정했다.
2026-02-01 신설된 만세·효행·병점·동탄 중 **동탄구(41597)만** 조정대상지역이라,
코드를 모르면 화성시 전체를 판정 불가로 흘릴 수밖에 없었다.

실행
    python tools/find_region_code.py 화성시          # 구 단위만
    python tools/find_region_code.py 강남구 --all     # 하위 동까지
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
import urllib.request

URL = "https://www.code.go.kr/stdcode/regCodeL.do"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Content-Type": "application/x-www-form-urlencoded",
}
ROW = re.compile(r"(\d{10})[^0-9<]{0,40}</td>\s*<td[^>]*>\s*([^<]+)</td>")


def search(keyword: str, page_size: int = 200, page: int = 1) -> list[tuple[str, str]]:
    body = urllib.parse.urlencode(
        {
            "locataddNm": keyword,
            "searchOk": "0",
            "pageSize": str(page_size),
            "cPage": str(page),
        },
        encoding="utf-8",
    ).encode()
    req = urllib.request.Request(URL, data=body, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    out: dict[str, str] = {}
    for code, name in ROW.findall(html):
        name = re.sub(r"\s+", " ", name).strip()
        if name and keyword.replace(" ", "")[:2] in name.replace(" ", ""):
            out.setdefault(code, name)
    return sorted(out.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keyword", help="지역명 (예: 화성시, 강남구)")
    parser.add_argument("--all", action="store_true", help="하위 읍면동까지 전부")
    parser.add_argument("--pages", type=int, default=1, help="조회할 페이지 수")
    args = parser.parse_args(argv)

    rows: list[tuple[str, str]] = []
    for p in range(1, args.pages + 1):
        rows.extend(search(args.keyword, page=p))
    if not rows:
        print(f"'{args.keyword}' 결과 없음", file=sys.stderr)
        return 1

    if args.all:
        target = rows
        title = "전체"
    else:
        # 시·군·구 단위 = 코드 뒤 5자리가 00000
        target = [(c, n) for c, n in rows if c.endswith("00000")]
        title = "시·군·구 단위"

    print(f"■ '{args.keyword}' — {title} {len(target)}건\n")
    for code, name in target:
        marker = "  ← 시군구코드" if code.endswith("00000") else ""
        print(f"  {code}   {code[:5]}   {name}{marker}")

    print(
        "\n조정대상지역 테이블에 넣을 값은 가운데 5자리입니다.\n"
        "  rulesets/v2026.08.03/reference/regulated_areas.yaml"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
