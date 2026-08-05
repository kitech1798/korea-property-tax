"""국가법령정보 Open API에서 조문 스냅샷을 받아 corpus/law_snapshots/에 저장한다.

왜 PDF가 아니라 조문인가
    2026.8.3 세제개편안 PDF는 "무엇이 어떻게 바뀌는가"만 담는다.
    실제 계산에 필요한 현행 세율·공제·요건은 법령 본문에 있고, 그것도
    법률이 아니라 시행령에 있는 경우가 많다(1세대1주택 특례, 거주요건 등).

왜 시행일자별(eflaw)인가
    세법은 같은 조문이 연도마다 다른 값을 갖는다. `현행연혁코드`가
    현행 / 연혁 / 시행예정 3값으로 내려오므로, 개정 전·후를 법적으로 구분해
    저장할 수 있다. 이게 "누더기 세법"에 대응하는 정공법이다.

⚠️ 이 API가 **안 주는 것 두 가지** (2026-08-05 실측)
    ① 계산식·표 — JSON과 HTML에서는 `<img>`로 온다. **`type=XML`로 받으면
       텍스트가 나온다.** 부담부증여 안분식(시행령 §159①)을 이걸로 확보했다.
       JSON만 쓰다가 "계산식이 없는 조문"으로 오해할 뻔했다.
    ② **부칙** — XML로도 안 온다. `eflawjosub`도 `eflaw` 전문(424KB)도 조문만 담는다.
       한시 규정의 유효기간은 조문이 아니라 **부칙에만** 적히므로,
       지금 파이프라인으로는 일몰 누락을 **원리적으로 검출할 수 없다.**
       (F03: 1세대1주택 재산세 특례세율의 일몰을 이것 때문에 확인하지 못했다.)
       개별 버그가 아니라 구조적 구멍이다.

인증
    OC 파라미터에 open.law.go.kr에서 발급받은 값(이메일 ID 앞부분)을 넣는다.
    환경변수 LAW_OC로 주거나 --oc 로 넘긴다. 미지정 시 'test'로 시도한다.

실행
    python tools/fetch_law.py                 # 기본 법령 세트 전체
    python tools/fetch_law.py --law 지방세법   # 하나만
    python tools/fetch_law.py --list          # 받지 않고 목록만 확인
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

BASE = "https://www.law.go.kr/DRF"
USER_AGENT = "realestate-tax-consult/0.1 (+local research tool)"
THROTTLE_SEC = 0.4
"""연속 호출 간격. 공개 API에 부담을 주지 않기 위한 최소한의 예의."""

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "corpus" / "law_snapshots"


@dataclass(frozen=True)
class LawSpec:
    """받아올 법령 하나. articles가 비어 있으면 전 조문을 받는다."""

    name: str
    why: str
    articles: tuple[str, ...] = ()
    """조번호. "9" 또는 가지번호가 있으면 "10-2" 형식."""


# 보유세(재산세+종부세) 계산에 실제로 필요한 것만 추린다.
# 양도세·취득세는 1차 범위 밖이라 뺐다 — 넣으면 corpus만 무거워지고 검증이 흐려진다.
LAW_SET: tuple[LawSpec, ...] = (
    LawSpec(
        "종합부동산세법",
        "주택분 종부세의 과세대상·기본공제·세율·세액공제·세부담상한·납부유예",
        ("7", "8", "9", "10", "10-2", "11", "12", "13", "14", "15", "20-2"),
    ),
    LawSpec(
        "종합부동산세법 시행령",
        "합산배제·1세대1주택 특례(상속·지방저가·일시적2주택)·공정시장가액비율",
        ("2-3", "2-4", "3", "4", "4-2", "4-3", "4-5", "5-2"),
    ),
    LawSpec(
        "지방세법",
        "재산세 과세대상·과세표준·과표상한제·세율·세부담상한·도시지역분",
        ("104", "105", "106", "107", "110", "111", "111-2", "112", "113", "114", "122"),
    ),
    LawSpec(
        "지방세법 시행령",
        "재산세 공정시장가액비율·주택 수 산정·1세대1주택 판정",
        ("109", "110", "110-2", "112", "118"),
    ),
    LawSpec("지방세기본법", "세대의 정의, 특수관계인", ("2",)),
    LawSpec("조세특례제한법", "지방 세컨드홈·미분양주택 등 주택 수 특례", ("71-2", "99-9")),
)


class LawApiError(RuntimeError):
    pass


def _get(path: str, params: dict[str, Any]) -> Any:
    url = f"{BASE}/{path}?" + urllib.parse.urlencode(params, encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # 네트워크는 항상 실패할 수 있다. 조용히 넘기지 않는다.
        raise LawApiError(f"호출 실패: {url}\n  {exc}") from exc

    stripped = raw.lstrip()
    if not stripped.startswith(("{", "[")):
        # 인증 실패·차단 시 HTML 안내문이 온다. JSON 파싱 오류로 위장되면 원인을 못 찾는다.
        raise LawApiError(
            f"JSON이 아닌 응답이 왔다(인증·차단 의심): {url}\n  앞부분: {stripped[:200]}"
        )
    return json.loads(raw)


def search_law(name: str, oc: str) -> list[dict[str, Any]]:
    """시행일 법령 목록. 현행 / 연혁 / 시행예정이 함께 내려온다."""
    doc = _get(
        "lawSearch.do",
        {"OC": oc, "target": "eflaw", "type": "JSON", "query": name, "display": 50},
    )
    body = doc.get("LawSearch") or {}
    rows = body.get("law") or []
    if isinstance(rows, dict):
        rows = [rows]
    # 부분일치가 많이 섞여 온다(예: "지방세법" 검색에 "지방세법 시행령"). 정확히 일치만.
    return [r for r in rows if str(r.get("법령명한글", "")).strip() == name]


def pick_versions(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """현행 1건 + 시행예정 전건을 고른다. 연혁(과거)은 지금 필요 없다.

    시행예정을 같이 받는 것이 이 도구의 존재 이유다. "내년에 뭐가 바뀌나"를
    2차 출처(뉴스)가 아니라 법령 원문으로 답할 수 있게 된다.
    """
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        status = str(r.get("현행연혁코드", "")).strip()
        eff = str(r.get("시행일자", "")).strip()
        mst = str(r.get("법령일련번호", "")).strip()
        if status == "현행":
            out.setdefault("current", r)
        elif status == "시행예정":
            # 같은 시행일에 서로 다른 개정법이 여러 건 걸려 있는 경우가 흔하다
            # (지방세법 2027-01-01 시행예정이 3건). MST를 키에 넣지 않으면 조용히 덮인다.
            out[f"upcoming_{eff}_{mst}"] = r
    return out


def fetch_article(mst: str, ef_ymd: str, jo: str, oc: str) -> dict[str, Any]:
    """조문 하나. JO는 6자리(조번호 4 + 가지번호 2)."""
    return _get(
        "lawService.do",
        {
            "OC": oc,
            "target": "eflawjosub",
            "type": "JSON",
            "MST": mst,
            "efYd": ef_ymd,
            "JO": jo,
        },
    )


def fetch_whole(mst: str, ef_ymd: str, oc: str) -> dict[str, Any]:
    return _get(
        "lawService.do",
        {"OC": oc, "target": "eflaw", "type": "JSON", "MST": mst, "efYd": ef_ymd},
    )


def jo_code(article: str) -> str:
    """"10-2" → "001002"  /  "9" → "000900". 법제처 JO 파라미터 규격."""
    if "-" in article:
        main, sub = article.split("-", 1)
    else:
        main, sub = article, "0"
    return f"{int(main):04d}{int(sub):02d}"


def slugify(name: str) -> str:
    return name.replace(" ", "_")


def fetch_law(spec: LawSpec, oc: str, verbose: bool = True) -> dict[str, Any]:
    rows = search_law(spec.name, oc)
    if not rows:
        raise LawApiError(f"법령을 찾지 못했다: {spec.name}")

    versions = pick_versions(rows)
    if not versions:
        raise LawApiError(f"현행·시행예정 판이 없다: {spec.name}")

    snapshot: dict[str, Any] = {
        "law_name": spec.name,
        "why": spec.why,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": "국가법령정보 공동활용 Open API (open.law.go.kr)",
        "versions": {},
    }

    for key, row in sorted(versions.items()):
        mst = str(row.get("법령일련번호") or row.get("법령ID"))
        eff = str(row.get("시행일자", "")).strip()
        entry: dict[str, Any] = {
            "mst": mst,
            "law_id": str(row.get("법령ID", "")),
            "effective_date": eff,
            "promulgation_date": str(row.get("공포일자", "")),
            "status": str(row.get("현행연혁코드", "")),
            "articles": {},
        }
        if verbose:
            print(f"  · {key:24s} MST={mst} 시행={eff} ({entry['status']})")

        for article in spec.articles:
            time.sleep(THROTTLE_SEC)
            try:
                doc = fetch_article(mst, eff, jo_code(article), oc)
            except LawApiError as exc:
                entry["articles"][article] = {"error": str(exc)}
                if verbose:
                    print(f"      제{article}조 ✗ {exc}")
                continue
            entry["articles"][article] = doc
            if verbose:
                print(f"      제{article}조 ✓ {_article_title(doc)}")

        snapshot["versions"][key] = entry
    return snapshot


def _article_title(doc: Any) -> str:
    """응답 구조가 판마다 조금씩 달라서 방어적으로 훑는다."""
    for key in ("법령", "Law", "조문"):
        node = doc.get(key) if isinstance(doc, dict) else None
        if isinstance(node, dict):
            for k in ("조문제목", "조문내용"):
                if node.get(k):
                    return str(node[k])[:40]
    text = json.dumps(doc, ensure_ascii=False)
    return text[:60]


def save(snapshot: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{slugify(snapshot['law_name'])}.json"
    path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oc", default=os.environ.get("LAW_OC", "test"))
    parser.add_argument("--law", action="append", help="특정 법령만. 반복 지정 가능.")
    parser.add_argument("--out", default=str(OUT_DIR))
    parser.add_argument("--list", action="store_true", help="받지 않고 판 목록만 확인")
    args = parser.parse_args(argv)

    specs: Iterable[LawSpec] = LAW_SET
    if args.law:
        wanted = set(args.law)
        specs = [s for s in LAW_SET if s.name in wanted]
        missing = wanted - {s.name for s in LAW_SET}
        if missing:
            print(f"알 수 없는 법령: {', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    out_dir = Path(args.out)
    failures: list[str] = []

    for spec in specs:
        print(f"\n■ {spec.name} — {spec.why}")
        try:
            if args.list:
                for row in search_law(spec.name, args.oc):
                    print(
                        f"  · {row.get('현행연혁코드'):6s} 시행={row.get('시행일자')} "
                        f"MST={row.get('법령일련번호')} 공포={row.get('공포일자')}"
                    )
                continue
            snapshot = fetch_law(spec, args.oc)
            path = save(snapshot, out_dir)
            print(f"  → 저장: {path.relative_to(ROOT)}")
        except LawApiError as exc:
            print(f"  ✗ {exc}", file=sys.stderr)
            failures.append(spec.name)

    if failures:
        print(f"\n실패 {len(failures)}건: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
