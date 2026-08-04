"""건축HUB 주택가격 경로 실측 검증.

★ 인증키를 받으면 **다른 작업보다 먼저** 이걸 돌려라.

검증되지 않은 전제 두 가지가 설계 전체를 좌우한다.
  ① 공동주택 전유부에 `hsprc`(주택가격)가 실제로 채워져 있는가 — 공란 비율은?
  ② `hsprc`가 공동주택공시가격과 **같은 값**인가 — 부동산공시가격 알리미와 대조

②가 깨지면 자동조회는 참고값으로만 쓰고 사용자 입력을 정본으로 삼아야 한다.
①의 채움률이 낮으면 자동조회 자체가 무의미하다.

설정
    $env:DATA_GO_KR_KEY = "발급받은 인증키"
    발급: https://www.data.go.kr/data/15134735/openapi.do (자동승인, 즉시)

실행
    python tools/verify_hub.py --dong 1168010100 --bun 1               # 법정동코드 + 번
    python tools/verify_hub.py --pnu 1168010100100010000               # PNU 19자리
    python tools/verify_hub.py --dong 1168010100 --bun 1 --year 2026   # 특정 연도
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from realestate_tax.sources.building_hub import (  # noqa: E402
    BuildingHubError,
    ParcelKey,
    coverage,
    fetch_prices,
    fetch_units,
    join_units_with_prices,
    latest_price_by_pk,
)


def money(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value:,}원 ({value / 100_000_000:.2f}억)"


def verify(key: ParcelKey, year: int | None) -> int:
    print(f"■ 조회 대상: {key.as_params()}")
    print()

    try:
        units = fetch_units(key)
        prices = fetch_prices(key)
    except BuildingHubError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    print(f"① 전유부(getBrExposPubuseAreaInfo): {len(units)}호")
    if not units:
        print("   ✗ 호가 하나도 안 나온다. 필지 지정이 틀렸거나 공동주택이 아니다.")
        return 1
    for u in units[:5]:
        print(f"   · {u.label_ko:24s} {u.floor_no:>6s}  {u.area_m2 or 0:8.2f}㎡  pk={u.mgm_pk}")
    if len(units) > 5:
        print(f"   … 외 {len(units) - 5}호")
    print()

    print(f"② 주택가격(getBrHsprcInfo): {len(prices)}건")
    years = Counter(p.year for p in prices)
    if years:
        span = ", ".join(f"{y}:{n}건" for y, n in sorted(years.items()) if y)
        print(f"   연도별 이력: {span}")
        print("   → 연도별로 쌓여 있으면 세부담상한에 필요한 전년도 값도 쓸 수 있다.")
    else:
        print("   ✗ 주택가격이 하나도 안 나온다.")
    print()

    joined = join_units_with_prices(units, prices, year=year)
    rate = coverage(joined)
    label = f"{year}년 기준" if year else "최신"
    print(f"③ 조인 결과 ({label}): 채움률 {rate:.1%}  ({sum(1 for j in joined if j.is_resolved)}/{len(joined)}호)")
    for j in joined[:8]:
        print(f"   · {j.unit.label_ko:24s} {money(j.price.price if j.price else None)}")
    print()

    # ── 판정 ──────────────────────────────────────────────────────
    print("=" * 60)
    if rate >= 0.95:
        print("✅ 자동조회 경로 사용 가능. 단, 사용자 '수정' 버튼은 반드시 유지하라.")
        verdict = 0
    elif rate >= 0.5:
        print("⚠️ 채움률이 낮다. 자동조회는 '참고값'으로만 표시하고 사용자 확인을 필수로 하라.")
        verdict = 0
    else:
        print("✗ 채움률이 너무 낮다. 이 경로를 1차 조회로 쓸 수 없다.")
        print("  → 사용자 직접 입력을 정본으로 하고, 자동조회는 제거하거나 보조로만 둘 것.")
        verdict = 1

    print()
    print("④ 남은 수동 검증 (자동화 불가):")
    print("   위에 찍힌 금액 중 하나를 부동산공시가격 알리미에서 직접 확인하라.")
    print("     https://www.realtyprice.kr/notice/town/nfSiteLink.htm")
    print("     [지번 검색] 탭 → 시/도 → 시군구 → 읍면 → 지번 → 단지 → 동 → 호")
    print("   값이 다르면 건축물대장 주택가격 ≠ 공동주택공시가격이라는 뜻이고,")
    print("   그러면 자동조회는 절대 정본이 될 수 없다. 이 대조를 건너뛰지 마라.")
    return verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pnu", help="PNU 19자리")
    parser.add_argument("--dong", help="법정동코드 10자리")
    parser.add_argument("--bun", type=int, help="번")
    parser.add_argument("--ji", type=int, default=0, help="지 (기본 0)")
    parser.add_argument("--mountain", action="store_true", help="산 지번")
    parser.add_argument("--year", type=int, help="기준 연도 (미지정이면 최신)")
    args = parser.parse_args(argv)

    if not os.environ.get("DATA_GO_KR_KEY"):
        print(
            "환경변수 DATA_GO_KR_KEY가 없다.\n"
            '  PowerShell:  $env:DATA_GO_KR_KEY = "발급키"\n'
            "  발급: https://www.data.go.kr/data/15134735/openapi.do (자동승인)",
            file=sys.stderr,
        )
        return 2

    if args.pnu:
        key = ParcelKey.from_pnu(args.pnu)
    elif args.dong and args.bun is not None:
        key = ParcelKey.from_parts(args.dong, args.bun, args.ji, mountain=args.mountain)
    else:
        parser.error("--pnu 또는 (--dong과 --bun)을 지정하라")
        return 2

    return verify(key, args.year)


if __name__ == "__main__":
    raise SystemExit(main())
