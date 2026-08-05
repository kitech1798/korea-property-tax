"""시뮬레이션 실행 CLI.

    python -m sim.run                      # sim/scenarios/ 전량
    python -m sim.run --dir sim/scenarios/gen/round1
    python -m sim.run --json out.json      # 에이전트가 읽을 구조화 결과
    python -m sim.run --only s-042 --verbose

출력이 두 벌인 이유: 사람이 읽는 요약과 에이전트가 읽는 JSON은 요구가 다르다.
사람은 **묶인 것**(같은 원인 60건 → 1줄)을 원하고, 에이전트는 **전부**를 원한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")  # 한글 콘솔(cp949)에서 깨지지 않게

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from realestate_tax.rules.resolver import load_ruleset  # noqa: E402

from sim import invariants  # noqa: E402
from sim.runner import Outcome, run  # noqa: E402
from sim.spec import load_dir  # noqa: E402

DEFAULT_DIR = ROOT / "sim" / "scenarios"


def _jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    return obj


def execute(directory: Path, *, only: str | None = None) -> tuple[list[Outcome], list[tuple[Path, str]]]:
    loaded = load_dir(directory)
    ruleset = load_ruleset()
    outcomes: list[Outcome] = []
    for scenario in loaded.scenarios:
        if only and only not in scenario.id:
            continue
        outcome = run(scenario, ruleset)
        violations = invariants.check(scenario, outcome, ruleset)
        outcomes.append(replace(outcome, violations=violations))
    return outcomes, list(loaded.errors)


def summarize(outcomes: list[Outcome], spec_errors: list[tuple[Path, str]]) -> str:
    """사람이 읽는 요약. **같은 원인은 묶는다** — 안 묶으면 아무도 안 읽는다."""
    lines: list[str] = []
    total = len(outcomes)
    clean = sum(1 for o in outcomes if o.ok)
    lines.append(f"시나리오 {total}건 · 통과 {clean} · 문제 {total - clean}")
    if spec_errors:
        lines.append("")
        lines.append(f"⚠ 명세 오류 {len(spec_errors)}건 (엔진 버그 아님 — 시나리오를 고쳐야 함)")
        for path, msg in spec_errors[:12]:
            lines.append(f"   · {path.name}: {msg}")
        if len(spec_errors) > 12:
            lines.append(f"   … 외 {len(spec_errors) - 12}건")

    # -- 예외: 같은 (stage, kind, message)로 묶는다 --
    crashes: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for o in outcomes:
        for f in o.failures:
            crashes[f.key()].append(o.scenario_id)
    if crashes:
        lines.append("")
        lines.append(f"■ 예외 {sum(len(v) for v in crashes.values())}건 / 원인 {len(crashes)}종")
        for (stage, kind, msg), ids in sorted(crashes.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"   [{len(ids):>3}건] {kind} @ {stage}")
            lines.append(f"          {msg}")
            lines.append(f"          예: {', '.join(ids[:4])}")

    # -- 불변식 위반 --
    viols: dict[tuple[str, str], list[str]] = defaultdict(list)
    severity: dict[str, str] = {}
    for o in outcomes:
        for v in o.violations:
            viols[v.key()].append(o.scenario_id)
            severity[v.rule] = v.severity
    if viols:
        blocks = sum(len(ids) for (rule, _), ids in viols.items() if severity.get(rule) == "block")
        lines.append("")
        lines.append(f"■ 불변식 위반 {sum(len(v) for v in viols.values())}건 (block {blocks}) / 종류 {len(viols)}")
        for (rule, detail), ids in sorted(
            viols.items(), key=lambda kv: (severity.get(kv[0][0]) != "block", -len(kv[1]))
        ):
            mark = "🔴" if severity.get(rule) == "block" else "🟡"
            lines.append(f"   {mark} [{len(ids):>3}건] {rule}")
            lines.append(f"          {detail}")
            lines.append(f"          예: {', '.join(ids[:4])}")

    # -- 관측 요약: 판정 불가·미상이 몰린 자리 --
    undecidable = Counter()
    unknown = Counter()
    for o in outcomes:
        for ob in o.observations:
            undecidable.update(ob.undecidable_steps)
            unknown.update(u.split(":")[0] for u in ob.unknowns)
    if undecidable or unknown:
        lines.append("")
        lines.append("■ 판정 불가·미상이 자주 나온 단계 (버그는 아니지만 사용자가 답을 못 받는 자리)")
        for step, n in undecidable.most_common(8):
            lines.append(f"   · UNDECIDABLE {n:>4}회  {step}")
        for step, n in unknown.most_common(8):
            lines.append(f"   · UNKNOWN     {n:>4}회  {step}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="부동산 세금 엔진 상황 시뮬레이션")
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--json", default="")
    ap.add_argument("--only", default="")
    ap.add_argument("--fail-on-violation", action="store_true",
                    help="위반이 있으면 종료코드 1. CI 게이트용.")
    args = ap.parse_args(argv)

    directory = Path(args.dir)
    if not directory.exists():
        print(f"디렉터리가 없다: {directory}")
        return 2

    outcomes, spec_errors = execute(directory, only=args.only or None)
    print(summarize(outcomes, spec_errors))

    if args.json:
        payload = {
            "scenarios": len(outcomes),
            "spec_errors": [{"file": str(p), "message": m} for p, m in spec_errors],
            "outcomes": [_jsonable(o) for o in outcomes],
        }
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"\nJSON → {args.json}")

    if args.fail_on_violation and any(not o.ok for o in outcomes):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
