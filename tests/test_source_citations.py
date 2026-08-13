"""룰셋이 인용한 **원문 페이지가 실제로 그 내용을 담고 있는가.**

★ 이 프로젝트가 원문 인용으로 사고를 낸 것이 이번이 세 번째다.

  ① 시행령 §154① 원문이라며 **축약본**을 적어 뒀다(2026-08-05).
  ② 2028년 3주택 중과세율이 +15%p인데 값이 0.20으로 들어갔다(2026-08-04).
  ③ 중과 한시완화 블록이 상세본 **p.85**·개조식 **p.22**를 가리켰는데,
     그 표는 상세본 p.72·개조식 p.20에 있다(2026-08-13 멀티에이전트 감사).

셋 다 "사람이 눈으로 대조한다"에 기대다 놓쳤다. 페이지 번호는 조용히 틀린다 —
값이 맞으면 아무도 다시 안 본다. 그래서 **기계가 매번 대조한다.**

여기서 확인하는 것은 두 가지다.
  · 인용한 페이지가 추출본에 **존재하는가**
  · 그 페이지에 그 규칙의 **핵심 낱말이 있는가**

내용 전체를 대조하지는 못한다(추출본은 줄바꿈이 뭉개져 있다). 그래도 "엉뚱한
페이지를 가리키는 것"은 확실히 잡힌다 — 이번에 실제로 그것이었다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from realestate_tax.rules import RuleSet, default_ruleset_root

CORPUS = Path(__file__).resolve().parents[1] / "corpus" / "reform_2026"

# 인용에 쓰이는 PDF 파일명 → 추출본
EXTRACTS = {
    "1. 2026년 세제개편안 개조식.pdf": "개조식.md",
    "2. 2026년 세제개편안 상세본.pdf": "상세본.md",
    "3. 2026년 세제개편안 문답자료.pdf": "문답자료.md",
}


def load_pages(md_name: str) -> dict[int, str]:
    """`## p.N` 헤더로 쪼갠 페이지별 본문."""
    text = (CORPUS / md_name).read_text(encoding="utf-8")
    pages: dict[int, str] = {}
    current: int | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s*p\.(\d+)", line)
        if m:
            if current is not None:
                pages[current] = "\n".join(buf)
            current = int(m.group(1))
            buf = []
        else:
            buf.append(line)
    if current is not None:
        pages[current] = "\n".join(buf)
    return pages


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


def pdf_citations(rs: RuleSet):
    """(rule_id, block_id, 파일, 페이지) — 룰셋 전체의 PDF 인용."""
    out = []
    for rule_id in rs.rule_ids:
        for block in rs.rule(rule_id).blocks:
            src = block.source
            if src is not None and src.kind == "pdf" and src.page:
                out.append((rule_id, block.id, src.file, int(src.page)))
    return out


def test_인용한_페이지가_추출본에_있다(rs: RuleSet):
    """없는 페이지를 가리키면 대조 자체가 불가능하다."""
    missing = []
    for rule_id, block_id, file, page in pdf_citations(rs):
        md = EXTRACTS.get(file)
        if md is None:
            missing.append(f"{rule_id}#{block_id}: 추출본이 없는 파일 — {file}")
            continue
        if page not in load_pages(md):
            missing.append(f"{rule_id}#{block_id}: {md} 에 p.{page} 없음")
    assert not missing, "\n".join(missing)


# 규칙마다 **그 페이지에 반드시 있어야 하는 낱말.**
# 값이 아니라 '주제'를 잡는다 — 값은 다른 테스트가 표로 고정한다.
KEYWORDS = {
    "transfer.heavy_surcharge": ("중과", "%p"),
    "transfer.long_term_deduction": ("거주", "공제"),
    "transfer.long_term_deduction_cap": ("한도",),
    "transfer.house_count_specials": ("일시적", "2주택"),
    "transfer.sangsaeng_lease": ("상생임대",),
    "transfer.basic_deduction": ("공제",),
    "reference.imputed_residence": ("거주기간",),
}


@pytest.mark.parametrize("rule_id", sorted(KEYWORDS))
def test_인용한_페이지에_그_주제가_있다(rs: RuleSet, rule_id: str):
    """★ 2026-08-13에 실제로 걸린 검사.

    중과 블록이 상세본 p.85를 가리켰는데 그 페이지는 상생임대 절이었다.
    값(+15%p)은 맞았기 때문에 값 테스트로는 영원히 안 잡힌다.
    """
    if rule_id not in rs.rule_ids:
        pytest.skip(f"{rule_id} 없음")
    problems = []
    for r_id, block_id, file, page in pdf_citations(rs):
        if r_id != rule_id:
            continue
        md = EXTRACTS.get(file)
        if md is None:
            continue
        body = load_pages(md).get(page, "")
        for kw in KEYWORDS[rule_id]:
            if kw not in body:
                problems.append(
                    f"{r_id}#{block_id} → {md} p.{page} 에 '{kw}'가 없다"
                )
    assert not problems, "\n".join(problems)


def test_중과_한시완화는_상세본_72와_개조식_20을_가리킨다(rs: RuleSet):
    """★ 회귀 — 2026-08-13에 고친 오기를 좌표로 못박는다.

    상세본 p.72 <중과세율 한시완화> 표, 개조식 p.20 <중과세율> 표가 정본이다.
    p.85는 상생임대, p.22는 부득이한 사유 거주인정 절이다.
    """
    pages = {(f, p) for r, _, f, p in pdf_citations(rs) if r == "transfer.heavy_surcharge"}
    assert ("2. 2026년 세제개편안 상세본.pdf", 85) not in pages
    assert ("1. 2026년 세제개편안 개조식.pdf", 22) not in pages

    detail = load_pages("상세본.md")[72]
    assert "중과세율한시완화" in detail.replace(" ", "")
    assert "+15%p" in detail
    brief = load_pages("개조식.md")[20]
    assert "중과세율" in brief and "+15%p" in brief
