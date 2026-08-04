"""룰셋 로딩과 규칙 해석.

`resolve()`의 계약이 이 파일의 전부다.

    조건에 맞는 블록은 **정확히 하나**여야 한다.
    0개면 MissingRule, 최고 우선순위에서 2개 이상이면 AmbiguousRule.

조용한 기본값을 두지 않는 것이 핵심이다. 시중 계산기가 조정대상지역을
`주소.startswith("서울")`로 때운 것이 바로 이 계약이 없어서 생긴 일이다.
"모르면 대충 이걸로"가 한 번 허용되면 그 뒤로는 어디서 틀렸는지 추적할 수 없다.

블록이 여러 개 맞을 때는 셀렉터 조건 수(specificity)가 많은 쪽이 이긴다.
이건 암묵적 기본값이 아니라 룰셋 안에 명시된 계층이고, 린터가 동률 중복을 잡는다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterator, Mapping

import yaml

from .schema import (
    AmbiguousRule,
    MissingRule,
    Rule,
    RuleBlock,
    RuleError,
    Track,
    parse_rule,
)


@dataclass(frozen=True, slots=True)
class Resolution:
    """해석 결과. 값과 함께 '어느 블록이 왜 골라졌는지'를 들고 온다."""

    block: RuleBlock
    track: Track
    on: date
    context: Mapping[str, Any]
    rejected: tuple[tuple[RuleBlock, str], ...] = ()
    """탈락한 블록과 사유. 룰셋 디버깅과 화면의 '왜 이 규칙인가' 설명에 쓴다."""

    @property
    def value(self) -> Any:
        return self.block.value

    def ref(self):
        return self.block.to_ref(self.track)


class RuleSet:
    """한 버전의 룰셋 전체. 불변으로 다룬다."""

    def __init__(self, version: str, rules: Mapping[str, Rule], content_hash: str, root: Path | None = None):
        self.version = version
        self._rules = dict(rules)
        self.content_hash = content_hash
        self.root = root

    # -- 로딩 -----------------------------------------------------------

    @classmethod
    def load(cls, root: str | Path) -> "RuleSet":
        """디렉터리 전체를 읽는다. 파일 배치는 자유이고 rule_id가 유일 키다.

        같은 rule_id가 두 파일에 나오면 즉시 실패시킨다. 조용히 덮어쓰면
        "분명히 고쳤는데 값이 안 바뀐다"는 최악의 디버깅이 시작된다.
        """
        root = Path(root)
        if not root.is_dir():
            raise RuleError(f"룰셋 디렉터리가 없다: {root}")

        rules: dict[str, Rule] = {}
        origin: dict[str, Path] = {}
        hasher = hashlib.sha256()

        for path in sorted(root.rglob("*.yaml")):
            if path.name == "manifest.yaml":
                continue
            raw_text = path.read_text(encoding="utf-8")
            hasher.update(path.relative_to(root).as_posix().encode("utf-8"))
            hasher.update(raw_text.encode("utf-8"))

            # 한 파일에 규칙 여러 개를 `---`로 나눠 담는다. 관련 규칙을 한눈에
            # 보려면 파일을 쪼개는 것보다 이쪽이 낫다.
            try:
                documents = list(yaml.safe_load_all(raw_text))
            except yaml.YAMLError as exc:
                raise RuleError(f"YAML 파싱 실패: {path}\n  {exc}") from exc

            for doc in documents:
                if doc is None:
                    continue
                for entry in doc if isinstance(doc, list) else [doc]:
                    if not isinstance(entry, Mapping) or "rule_id" not in entry:
                        continue
                    rule = parse_rule(entry)
                    if rule.rule_id in rules:
                        raise RuleError(
                            f"rule_id 중복: {rule.rule_id}\n"
                            f"  - {origin[rule.rule_id]}\n  - {path}"
                        )
                    rules[rule.rule_id] = rule
                    origin[rule.rule_id] = path

        version = cls._read_version(root)
        return cls(version, rules, hasher.hexdigest()[:16], root)

    @staticmethod
    def _read_version(root: Path) -> str:
        manifest = root / "manifest.yaml"
        if manifest.exists():
            doc = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            return str(doc.get("version") or root.name)
        return root.name

    # -- 조회 -----------------------------------------------------------

    def __contains__(self, rule_id: str) -> bool:
        return rule_id in self._rules

    def __iter__(self) -> Iterator[Rule]:
        return iter(self._rules.values())

    def __len__(self) -> int:
        return len(self._rules)

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._rules))

    def rule(self, rule_id: str) -> Rule:
        try:
            return self._rules[rule_id]
        except KeyError:
            raise MissingRule(
                f"룰셋 '{self.version}'에 규칙이 없다: {rule_id}"
            ) from None

    # -- 해석 -----------------------------------------------------------

    def resolve(
        self,
        rule_id: str,
        *,
        on: date,
        track: Track,
        **context: Any,
    ) -> Resolution:
        """정확히 한 블록을 고른다. 못 고르면 예외를 던진다."""
        rule = self.rule(rule_id)
        rejected: list[tuple[RuleBlock, str]] = []
        matched: list[RuleBlock] = []

        for block in rule.blocks:
            if track not in block.tracks:
                rejected.append((block, f"트랙 불일치(요청 {track})"))
                continue
            if not block.applies_on(on):
                rejected.append(
                    (block, f"시행기간 밖({block.effective_from}~{block.effective_to})")
                )
                continue
            if not block.selector.matches(context):
                rejected.append((block, f"조건 불일치({block.selector.describe_ko()})"))
                continue
            matched.append(block)

        if not matched:
            raise MissingRule(self._explain_missing(rule_id, on, track, context, rejected))

        top = max(b.selector.specificity for b in matched)
        winners = [b for b in matched if b.selector.specificity == top]
        if len(winners) > 1:
            ids = ", ".join(b.id for b in winners)
            raise AmbiguousRule(
                f"{rule_id}: 같은 우선순위(조건 {top}개)의 블록이 여러 개 맞았다 → {ids}\n"
                f"  트랙={track} 기준일={on} 조건={dict(context)}\n"
                f"  룰셋의 셀렉터가 겹친다. 겹치지 않게 조건을 명시하라."
            )

        for b in matched:
            if b is not winners[0]:
                rejected.append((b, f"더 구체적인 블록에 밀림(조건 {b.selector.specificity}개)"))

        return Resolution(winners[0], track, on, dict(context), tuple(rejected))

    def resolve_or_none(
        self, rule_id: str, *, on: date, track: Track, **context: Any
    ) -> Resolution | None:
        """규칙이 없을 수도 있는 선택적 항목용. 예외 대신 None.

        `try/except MissingRule`을 호출부마다 쓰면 AmbiguousRule까지 삼켜버릴 위험이
        있어서 별도 진입점으로 둔다."""
        try:
            return self.resolve(rule_id, on=on, track=track, **context)
        except MissingRule:
            return None

    def _explain_missing(
        self,
        rule_id: str,
        on: date,
        track: Track,
        context: Mapping[str, Any],
        rejected: list[tuple[RuleBlock, str]],
    ) -> str:
        lines = [
            f"{rule_id}: 조건에 맞는 블록이 없다.",
            f"  트랙={track} 기준일={on} 조건={dict(context)}",
            "  탈락 사유:",
        ]
        for block, why in rejected:
            lines.append(f"    - {block.id}: {why}")
        if not rejected:
            lines.append("    (블록이 하나도 정의돼 있지 않다)")
        return "\n".join(lines)


_CACHE: dict[Path, RuleSet] = {}


def default_ruleset_root() -> Path:
    """저장소 표준 위치의 최신 룰셋 디렉터리."""
    base = Path(__file__).resolve().parents[2] / "rulesets"
    if not base.is_dir():
        raise RuleError(f"룰셋 루트가 없다: {base}")
    versions = sorted(p for p in base.iterdir() if p.is_dir())
    if not versions:
        raise RuleError(f"룰셋 버전 디렉터리가 없다: {base}")
    return versions[-1]


def load_ruleset(root: str | Path | None = None) -> RuleSet:
    """룰셋을 읽는다. 같은 경로는 캐시한다(파일 수십 개를 매 계산마다 파싱하지 않도록)."""
    path = Path(root) if root is not None else default_ruleset_root()
    path = path.resolve()
    if path not in _CACHE:
        _CACHE[path] = RuleSet.load(path)
    return _CACHE[path]


def clear_cache() -> None:
    """룰셋 YAML을 고친 뒤 테스트에서 다시 읽게 할 때 쓴다."""
    _CACHE.clear()
