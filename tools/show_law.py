"""수집한 조문 스냅샷을 사람이 읽을 수 있게 출력한다.

룰셋에 숫자를 적기 전에 반드시 이 도구로 원문을 눈으로 확인한다.
기억이나 일반지식으로 세율을 적으면 반드시 틀린다 — 한국 세법은 특례가
본칙보다 많고, 본칙만 읽으면 실제로 적용되는 값과 다르다.

실행
    python tools/show_law.py 지방세법 111
    python tools/show_law.py 지방세법_시행령 109 --version current
    python tools/show_law.py --list
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAP_DIR = ROOT / "corpus" / "law_snapshots"

TAG_RE = re.compile(r"<[^>]+>")


def clean(text: Any) -> str:
    if text is None:
        return ""
    s = TAG_RE.sub("", str(text))
    return re.sub(r"[ \t]+", " ", s).strip()


def as_list(node: Any) -> list[Any]:
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def render_article(doc: dict) -> str:
    """조/항/호/목 계층을 들여쓰기로 편다.

    조문단위는 단일 dict일 때도, 리스트일 때도 온다(가지번호 조문이 붙어 있는 경우
    제110조와 제110조의2가 함께 내려온다). 둘 다 받는다.
    """
    unit = doc.get("법령", {}).get("조문", {}).get("조문단위")
    if unit is None:
        return "(조문 구조를 찾지 못했다)"
    if isinstance(unit, list):
        return "\n\n".join(render_unit(u) for u in unit)
    return render_unit(unit)


def render_unit(unit: dict) -> str:
    out: list[str] = []
    title = clean(unit.get("조문제목"))
    out.append(f"■ 제{unit.get('조문번호')}조({title})  [시행 {unit.get('조문시행일자')}]")
    body = clean(unit.get("조문내용"))
    if body:
        out.append(f"  {body}")

    for hang in as_list(unit.get("항")):
        out.append(f"  {clean(hang.get('항번호'))} {clean(hang.get('항내용'))}")
        for ho in as_list(hang.get("호")):
            out.append(f"      {clean(ho.get('호번호'))} {clean(ho.get('호내용'))}")
            for mok in as_list(ho.get("목")):
                mok_text = mok.get("목내용")
                for line in as_list(mok_text):
                    out.append(f"          - {clean(line)}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("law", nargs="?", help="파일명(공백은 밑줄). 예: 지방세법_시행령")
    parser.add_argument("articles", nargs="*", help="조번호. 생략하면 전부.")
    parser.add_argument("--version", default="current")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    if args.list or not args.law:
        for p in sorted(SNAP_DIR.glob("*.json")):
            doc = json.loads(p.read_text(encoding="utf-8"))
            versions = ", ".join(sorted(doc["versions"]))
            arts = ", ".join(doc["versions"]["current"]["articles"]) if "current" in doc["versions"] else ""
            print(f"{p.stem}\n  판: {versions}\n  조문: {arts}\n")
        return 0

    path = SNAP_DIR / f"{args.law}.json"
    if not path.exists():
        print(f"없다: {path}", file=sys.stderr)
        return 2

    doc = json.loads(path.read_text(encoding="utf-8"))
    version = doc["versions"].get(args.version)
    if version is None:
        print(f"판이 없다: {args.version} (가능: {sorted(doc['versions'])})", file=sys.stderr)
        return 2

    print(f"# {doc['law_name']} — {args.version} (MST {version['mst']}, 시행 {version['effective_date']})\n")
    targets = args.articles or list(version["articles"])
    for art in targets:
        payload = version["articles"].get(art)
        if payload is None:
            print(f"(제{art}조 미수집)")
            continue
        if "error" in payload:
            print(f"(제{art}조 수집 실패: {payload['error']})")
            continue
        print(render_article(payload))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
