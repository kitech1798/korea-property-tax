"""2026 세제개편안 PDF에서 텍스트를 뽑아 corpus/reform_2026/로 구조화한다.

왜 필요한가
    이 서비스의 1차 출처는 재정경제부 세제개편안 PDF 5종이다. 하지만 PDF는
    grep도 안 되고 인용도 안 된다. **페이지 번호를 보존한 채** 텍스트로 풀어야
    "이 숫자는 어느 문서 몇 쪽에서 왔다"를 룰셋과 화면에 붙일 수 있다.

왜 PyMuPDF인가
    `pdftotext`(poppler)는 이 PDF들에서 한글을 한 글자도 못 뽑는다(폰트 서브셋).
    PyMuPDF는 완전히 뽑는다. 도구 선택 하나로 "OCR 필요"와 "즉시 가능"이 갈렸다.

알려진 왜곡
    개조식·상세본은 조판상 어절 사이 공백이 사라져 "거주지원을위해현행보유공제를"
    처럼 붙어 나온다. 숫자와 표는 멀쩡하므로 룰셋 값 추출에는 지장이 없다.
    검색 편의를 위해 공백 제거본을 별도 인덱스로 함께 저장한다.

실행
    python tools/extract_reform.py
    python tools/extract_reform.py --grep 공정시장가액비율
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    print("PyMuPDF가 필요하다:  pip install pymupdf", file=sys.stderr)
    raise

ROOT = Path(__file__).resolve().parents[1]
PDF_DIR = ROOT / "세제개편안_8.3"
OUT_DIR = ROOT / "corpus" / "reform_2026"

ANNOUNCED_ON = date(2026, 8, 3)
"""발표일. 이 날짜 이전 취득·계약에 대한 경과조치의 기준이 되므로 상수로 둔다."""

# 부동산 보유세 상담에 실제로 쓰이는 구간. 조사에서 페이지 인덱스로 특정했다.
DOCS: dict[str, dict] = {
    "개조식": {
        "file": "1. 2026년 세제개편안 개조식.pdf",
        "role": "전체 요약. 세율표·단계 시행표가 가장 압축적으로 실려 있다.",
        "focus": (17, 25),
    },
    "상세본": {
        "file": "2. 2026년 세제개편안 상세본.pdf",
        "role": "조문별 현행/개정안/적용시기. 룰셋 basis의 1차 근거.",
        "focus": (55, 112),
    },
    "문답자료": {
        "file": "3. 2026년 세제개편안 문답자료.pdf",
        "role": "정부 공식 Q&A와 세액 계산 사례. 골든 테스트의 정답지.",
        "focus": (35, 68),
    },
    "보도자료": {
        "file": "0. 2026년 세제개편안 보도자료.pdf",
        "role": "발표 개요와 배포 정보.",
        "focus": None,
    },
    "인포그래픽": {
        "file": "2026 세제개편안 인포그래픽.pdf",
        "role": "시각 요약.",
        "focus": None,
    },
}

REAL_ESTATE_TERMS = (
    "종합부동산세", "종부세", "재산세", "양도소득", "장기보유", "장기거주",
    "1세대", "주택", "임대", "공정시장가액", "조정대상지역", "공시가격",
    "기본공제", "세부담", "거주자", "다주택", "합산배제", "취득세",
)


@dataclass
class Page:
    doc: str
    """문서 별칭(개조식·상세본…)."""
    index: int
    """0부터 세는 PDF 페이지 인덱스."""
    printed: int
    """PDF에 인쇄된 쪽번호. 인용은 이걸로 한다 — 사람이 문서를 열어 확인할 수 있어야 한다."""
    text: str
    term_hits: int = 0

    def to_dict(self) -> dict:
        return {
            "doc": self.doc,
            "page_index": self.index,
            "printed_page": self.printed,
            "term_hits": self.term_hits,
            "text": self.text,
        }


PRINTED_RE = re.compile(r"^\s*-\s*(\d{1,3})\s*-\s*$", re.MULTILINE)


def printed_page_number(text: str, fallback: int) -> int:
    """머리말/꼬리말의 "- 17 -" 패턴에서 인쇄 쪽번호를 읽는다.

    PDF 인덱스와 인쇄 쪽번호는 표지·목차 때문에 어긋난다. 인용을 인덱스로 하면
    사용자가 문서를 열었을 때 다른 쪽이 나온다 — 검증 가능성이 깨진다.
    """
    m = PRINTED_RE.search(text)
    return int(m.group(1)) if m else fallback


def count_terms(text: str) -> int:
    return sum(text.count(t) for t in REAL_ESTATE_TERMS)


def extract(pdf_path: Path, alias: str) -> list[Page]:
    doc = fitz.open(pdf_path)
    pages: list[Page] = []
    try:
        for i in range(doc.page_count):
            text = doc[i].get_text()
            pages.append(
                Page(
                    doc=alias,
                    index=i,
                    printed=printed_page_number(text, i + 1),
                    text=text,
                    term_hits=count_terms(text),
                )
            )
    finally:
        doc.close()
    return pages


def normalize_for_search(text: str) -> str:
    """검색용 정규화. 조판으로 사라진 공백 때문에 원문 검색이 자주 실패한다.

    "공정시장가액비율" 같은 용어는 붙어 있고, "공정시장가액 비율"처럼 띄어진
    경우도 있다. 모든 공백을 지운 사본을 따로 두어 둘 다 잡히게 한다.
    """
    return re.sub(r"\s+", "", text)


def build(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "built_at": date.today().isoformat(),
        "announced_on": ANNOUNCED_ON.isoformat(),
        "source": "재정경제부 2026년 세제개편안 (2026.8.3 발표)",
        "extractor": "PyMuPDF — pdftotext는 이 PDF에서 한글 추출에 실패한다",
        "documents": {},
    }

    for alias, meta in DOCS.items():
        pdf_path = PDF_DIR / meta["file"]
        if not pdf_path.exists():
            print(f"  ✗ 없음: {pdf_path.name}", file=sys.stderr)
            continue

        pages = extract(pdf_path, alias)
        payload = {
            "alias": alias,
            "file": meta["file"],
            "role": meta["role"],
            "focus_printed_pages": meta["focus"],
            "page_count": len(pages),
            "pages": [p.to_dict() for p in pages],
        }
        (out_dir / f"{alias}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )

        # 사람이 읽고 grep 하는 용도의 마크다운. 쪽번호를 헤더로 박아 인용을 강제한다.
        md = [f"# {alias} — {meta['file']}", "", f"> {meta['role']}", ""]
        for p in pages:
            md.append(f"\n## p.{p.printed}  <!-- pdf index {p.index} -->\n")
            md.append(p.text.rstrip())
        (out_dir / f"{alias}.md").write_text("\n".join(md), encoding="utf-8")

        focus = meta["focus"]
        hot = [p for p in pages if p.term_hits >= 3]
        manifest["documents"][alias] = {
            "file": meta["file"],
            "role": meta["role"],
            "page_count": len(pages),
            "focus_printed_pages": focus,
            "real_estate_dense_pages": [p.printed for p in hot],
        }
        span = f"p.{focus[0]}~{focus[1]}" if focus else "전체"
        print(f"  ✓ {alias:8s} {len(pages):3d}쪽 · 부동산 밀집 {len(hot):3d}쪽 · 핵심구간 {span}")

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def grep(term: str, out_dir: Path, context: int = 90) -> int:
    """corpus에서 용어를 찾아 쪽번호와 함께 보여준다.

    룰셋에 숫자를 적을 때 "이 값이 어느 쪽에서 왔는지"를 바로 확인하기 위한 도구다.
    출처를 손으로 기억해서 적으면 반드시 틀린다.
    """
    needle = normalize_for_search(term)
    hits = 0
    for path in sorted(out_dir.glob("*.json")):
        if path.name == "manifest.json":
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for page in payload["pages"]:
            flat = normalize_for_search(page["text"])
            start = 0
            while (pos := flat.find(needle, start)) != -1:
                lo = max(0, pos - context)
                snippet = flat[lo : pos + len(needle) + context]
                print(f"\n[{payload['alias']} p.{page['printed_page']}]")
                print(f"  …{snippet}…")
                hits += 1
                start = pos + len(needle)
    if not hits:
        print(f"'{term}' 없음")
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(OUT_DIR))
    parser.add_argument("--grep", help="corpus에서 용어 검색(추출은 생략)")
    args = parser.parse_args(argv)
    out_dir = Path(args.out)

    if args.grep:
        if not out_dir.exists():
            print("corpus가 없다. 먼저 추출을 실행하라.", file=sys.stderr)
            return 2
        grep(args.grep, out_dir)
        return 0

    print(f"세제개편안 PDF → {out_dir.relative_to(ROOT)}")
    build(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
