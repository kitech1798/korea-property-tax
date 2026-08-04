"""룰셋 린터.

세법 데이터의 오류는 조용하다. 세율 구간 하나가 겹치거나 비면 계산은 멀쩡히
돌아가고 숫자만 틀린다. 그래서 값이 아니라 **구조**를 기계가 검사한다.

검사 항목
  L1 근거 조문 누락        — 출처 없는 숫자는 룰셋에 들어올 수 없다
  L2 1차 출처 누락
  L3 시행기간 역전         — effective_from > effective_to
  L4 셀렉터 충돌           — 같은 트랙·같은 기간·같은 우선순위에서 겹침
  L5 세율 구간 비단조       — upto가 오름차순이 아니거나 중복
  L6 세율 구간 미종결       — 마지막 구간의 upto가 None이 아님(상한 초과 구간 실종)
  L7 세율 음수
  L8 값 부재               — value/table/payload가 모두 비어 있음
  L9 확정도 미표기 개편안   — 2027년 이후 시행인데 certainty가 enacted

실행:  python -m realestate_tax.rules.lint [룰셋경로]
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from itertools import combinations
from pathlib import Path

from ..domain.certainty import LegalStatus
from .resolver import RuleSet, default_ruleset_root
from .schema import Rule, RuleBlock, Track

REFORM_ERA_START = date(2027, 1, 1)
"""이 날짜 이후 시행되는 규칙이 '현행법(enacted)'으로 표기돼 있으면 의심한다.
2026.8.3 개편안은 국회 미통과이므로 bill_pending이어야 한다."""


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    rule_id: str
    block_id: str
    message: str
    severity: str = "error"

    def __str__(self) -> str:
        mark = "✗" if self.severity == "error" else "!"
        where = f"{self.rule_id}#{self.block_id}" if self.block_id else self.rule_id
        return f"  {mark} [{self.code}] {where}\n      {self.message}"


def lint_block(rule: Rule, block: RuleBlock) -> list[Finding]:
    out: list[Finding] = []
    rid, bid = rule.rule_id, block.id

    if block.basis is None:
        out.append(Finding("L1", rid, bid, "근거 조문(basis)이 없다. 출처 없는 숫자는 쓰지 않는다."))
    if block.source is None:
        out.append(Finding("L2", rid, bid, "1차 출처(source)가 없다. PDF 페이지 또는 법제처 API를 적어라."))

    if (
        block.effective_from is not None
        and block.effective_to is not None
        and block.effective_from > block.effective_to
    ):
        out.append(
            Finding("L3", rid, bid, f"시행기간 역전: {block.effective_from} > {block.effective_to}")
        )

    if block.table is not None:
        out.extend(_lint_table(rid, bid, block))

    if block.value is None and block.table is None and not block.payload:
        out.append(Finding("L8", rid, bid, "value·table·payload가 모두 비어 있다."))

    if (
        block.effective_from is not None
        and block.effective_from >= REFORM_ERA_START
        and block.certainty.legal is LegalStatus.ENACTED
    ):
        out.append(
            Finding(
                "L9",
                rid,
                bid,
                f"{block.effective_from} 시행인데 certainty가 enacted다. "
                "2026.8.3 개편안이면 bill_pending이어야 한다.",
                severity="warning",
            )
        )
    return out


def _lint_table(rid: str, bid: str, block: RuleBlock) -> list[Finding]:
    out: list[Finding] = []
    assert block.table is not None
    brackets = block.table.brackets

    if not brackets:
        out.append(Finding("L5", rid, bid, "세율표가 비어 있다."))
        return out

    prev: int | None = None
    for i, b in enumerate(brackets):
        if b.rate < 0:
            out.append(Finding("L7", rid, bid, f"{i}번째 구간의 세율이 음수다: {b.rate}"))
        if b.upto is None:
            if i != len(brackets) - 1:
                out.append(
                    Finding("L5", rid, bid, f"{i}번째 구간의 upto가 None인데 마지막이 아니다.")
                )
            continue
        if prev is not None and b.upto <= prev:
            out.append(
                Finding(
                    "L5",
                    rid,
                    bid,
                    f"{i}번째 구간 upto={b.upto:,}가 직전 {prev:,} 이하다. 오름차순이어야 한다.",
                )
            )
        prev = b.upto

    if brackets[-1].upto is not None:
        out.append(
            Finding(
                "L6",
                rid,
                bid,
                f"마지막 구간의 upto가 {brackets[-1].upto:,}로 닫혀 있다. "
                "최고 구간은 upto를 비워 상한을 없애야 한다.",
            )
        )
    return out


def lint_rule(rule: Rule) -> list[Finding]:
    out: list[Finding] = []
    for block in rule.blocks:
        out.extend(lint_block(rule, block))

    seen_ids = [b.id for b in rule.blocks]
    if len(seen_ids) != len(set(seen_ids)):
        out.append(Finding("L4", rule.rule_id, "", f"블록 id 중복: {seen_ids}"))

    out.extend(_lint_overlaps(rule))
    return out


def _lint_overlaps(rule: Rule) -> list[Finding]:
    """같은 트랙·같은 우선순위에서 기간과 조건이 동시에 겹치는 블록을 잡는다.

    이게 resolver의 AmbiguousRule을 런타임이 아니라 CI에서 미리 터뜨려 준다.
    """
    out: list[Finding] = []
    for a, b in combinations(rule.blocks, 2):
        if not (a.tracks & b.tracks):
            continue
        if a.selector.specificity != b.selector.specificity:
            continue
        if not _periods_overlap(a, b):
            continue
        if not _selectors_can_collide(a, b):
            continue
        shared = ", ".join(sorted(str(t) for t in (a.tracks & b.tracks)))
        out.append(
            Finding(
                "L4",
                rule.rule_id,
                f"{a.id}↔{b.id}",
                f"트랙({shared})·기간·조건이 동시에 겹친다. "
                f"조건 A={a.selector.describe_ko()} / B={b.selector.describe_ko()}",
            )
        )
    return out


def _periods_overlap(a: RuleBlock, b: RuleBlock) -> bool:
    lo = max(a.effective_from or date.min, b.effective_from or date.min)
    hi = min(a.effective_to or date.max, b.effective_to or date.max)
    return lo <= hi


def _selectors_can_collide(a: RuleBlock, b: RuleBlock) -> bool:
    """두 셀렉터를 동시에 만족시키는 컨텍스트가 존재할 수 있는가.

    보수적으로 판정한다 — 같은 키에 대해 서로 배타적인 값이 하나라도 있으면
    충돌하지 않는다고 본다. 판정이 애매하면 충돌로 보고 사람이 확인하게 한다.
    """
    for key, want_a in a.selector.constraints.items():
        if key not in b.selector.constraints:
            continue
        want_b = b.selector.constraints[key]
        if _values_disjoint(want_a, want_b):
            return False
    return True


def _values_disjoint(x, y) -> bool:
    if isinstance(x, dict) or isinstance(y, dict):
        xr = x if isinstance(x, dict) else {"min": x, "max": x}
        yr = y if isinstance(y, dict) else {"min": y, "max": y}
        xlo, xhi = xr.get("min"), xr.get("max")
        ylo, yhi = yr.get("min"), yr.get("max")
        if xhi is not None and ylo is not None and xhi < ylo:
            return True
        if yhi is not None and xlo is not None and yhi < xlo:
            return True
        return False
    xs = set(x) if isinstance(x, (list, tuple, set)) else {x}
    ys = set(y) if isinstance(y, (list, tuple, set)) else {y}
    return not (xs & ys)


def lint_ruleset(ruleset: RuleSet) -> list[Finding]:
    out: list[Finding] = []
    for rule in ruleset:
        out.extend(lint_rule(rule))
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(argv[0]) if argv else default_ruleset_root()
    ruleset = RuleSet.load(root)

    findings = lint_ruleset(ruleset)
    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity != "error"]

    print(f"룰셋 {ruleset.version} (해시 {ruleset.content_hash}) — 규칙 {len(ruleset)}개")
    if not findings:
        print("  ✓ 이상 없음")
        return 0

    for f in findings:
        print(f)
    print(f"\n오류 {len(errors)}건 · 경고 {len(warnings)}건")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
