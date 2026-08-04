"""감사 추적(audit trail).

시중 계산기가 신뢰를 잃은 가장 깊은 이유는 틀린 숫자가 아니라
**틀렸는지 확인할 수 없다**는 것이다. 서버가 던져준 세액을 화면에 찍기만 하면
사용자는 "왜 이 숫자냐"에 답할 수 없고, 고지서와 다를 때 어디가 어긋났는지도 모른다.

그래서 이 엔진은 숫자를 반환하지 않는다. `Value`(값 + 출처 + 확실성)와
`TraceNode`(산식 + 대입값 + 근거조문)를 반환한다. 화면은 계산하지 않고 이걸 그린다.

세 가지가 이 모듈의 존재 이유다.

1. `substitution` — "max(0, 3,000,000,000 − 1,400,000,000) × 0.7" 처럼
   실제 숫자가 대입된 문자열. 사람이 손으로 검산할 수 있어야 한다.
2. `alternatives_not_taken` — 평가했지만 적용되지 않은 특례를 남긴다.
   "고령자공제 미적용: 과세기준일 현재 만 58세로 60세 미만" 처럼
   *왜 안 됐는지*가 나온다. 시중 계산기의 '미반영 항목' 면책 목록이
   곧 실제 납세자 집합이었던 문제의 직접적 해독제다.
3. `diff()` — 이 서비스의 실제 화면은 "'26 대비 '27에 얼마 오르나",
   "현행법 대비 개편안은 얼마 차이나"다. 트리 비교가 곧 메인 UI다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from fractions import Fraction
from typing import Iterator, Literal

from ..domain.certainty import Certainty, DeterminationQuality, InputQuality, LegalStatus

Number = int | Fraction


class UnknownReason(StrEnum):
    """값을 모를 때 그 이유. 계산을 중단시키지 않고 결과까지 전파시킨다.

    중단하면 "아무것도 못 보여줌"이 되고, 0으로 채우면 "틀린 숫자를 보여줌"이 된다.
    둘 다 나쁘다. 모른 채로 흘려보내되 결과에 이유를 붙이는 것이 셋 중 유일하게 정직하다.
    """

    MISSING_INPUT = "missing_input"
    """사용자가 아직 입력하지 않았다."""
    MISSING_PRIOR_YEAR = "missing_prior_year"
    """직전연도 세액·공시가격이 없어 세부담상한을 계산할 수 없다."""
    UNDECIDABLE_FACT = "undecidable_fact"
    """법적 해석이 필요해 엔진이 판정하지 않는다."""
    RULE_NOT_FOUND = "rule_not_found"
    """해당 연도·트랙의 규칙이 룰셋에 없다."""


class SubjectType(StrEnum):
    PERSON = "person"
    HOUSEHOLD = "household"
    PROPERTY = "property"
    CASE = "case"


@dataclass(frozen=True, slots=True)
class SubjectRef:
    """이 계산 단계가 '누구/무엇에 대한' 것인지. 화면에서 인별·물건별 접기의 근거."""

    type: SubjectType
    id: str = ""
    label: str = ""

    @classmethod
    def case(cls) -> "SubjectRef":
        return cls(SubjectType.CASE)


@dataclass(frozen=True, slots=True)
class Value:
    """값 + 단위 + 확실성. 숫자가 프로버넌스를 들고 다닌다."""

    amount: Number | bool | None
    unit: str = "KRW"
    certainty: Certainty = field(default_factory=lambda: Certainty())
    unknown: UnknownReason | None = None
    label: str = ""

    def __post_init__(self) -> None:
        if self.amount is None and self.unknown is None:
            raise ValueError("값이 없으면 UnknownReason을 반드시 붙여야 한다")

    @classmethod
    def money(
        cls,
        amount: Number,
        *,
        certainty: Certainty | None = None,
        label: str = "",
    ) -> "Value":
        return cls(amount, "KRW", certainty or Certainty(), None, label)

    @classmethod
    def rate(
        cls,
        amount: Number,
        *,
        certainty: Certainty | None = None,
        label: str = "",
    ) -> "Value":
        return cls(amount, "rate", certainty or Certainty(), None, label)

    @classmethod
    def flag(
        cls,
        amount: bool,
        *,
        certainty: Certainty | None = None,
        label: str = "",
    ) -> "Value":
        return cls(amount, "bool", certainty or Certainty(), None, label)

    @classmethod
    def missing(
        cls,
        reason: UnknownReason,
        *,
        unit: str = "KRW",
        label: str = "",
    ) -> "Value":
        """미상값. input 축을 UNKNOWN으로 깔고, 판정 불가 사유면 determination도 낮춘다."""
        certainty = Certainty(input=InputQuality.UNKNOWN)
        if reason is UnknownReason.UNDECIDABLE_FACT:
            certainty = certainty & Certainty(
                determination=DeterminationQuality.UNDECIDABLE
            )
        if reason is UnknownReason.RULE_NOT_FOUND:
            certainty = certainty & Certainty(legal=LegalStatus.ASSUMED)
        return cls(None, unit, certainty, reason, label)

    @property
    def is_known(self) -> bool:
        return self.amount is not None

    def as_int(self) -> int:
        """원 단위 정수로. 미상이면 0으로 흘리되 확실성이 이미 강등돼 있다."""
        if self.amount is None:
            return 0
        if isinstance(self.amount, bool):
            return int(self.amount)
        return int(self.amount)

    def derive(
        self,
        amount: Number | bool | None,
        *sources: "Value | Certainty | RuleRef | None",
        unit: str | None = None,
        label: str = "",
        unknown: UnknownReason | None = None,
    ) -> "Value":
        """이 값에서 파생된 새 값. 확실성은 자신 + 모든 출처의 축별 최솟값."""
        return derive_value(
            amount,
            self,
            *sources,
            unit=unit or self.unit,
            label=label,
            unknown=unknown,
        )


def certainty_of(item: "Value | Certainty | RuleRef | None") -> Certainty | None:
    if item is None:
        return None
    if isinstance(item, Certainty):
        return item
    return item.certainty


def derive_value(
    amount: Number | bool | None,
    *sources: "Value | Certainty | RuleRef | None",
    unit: str = "KRW",
    label: str = "",
    unknown: UnknownReason | None = None,
) -> Value:
    """여러 출처에서 파생된 값을 만든다. 확실성 합성이 여기 한 곳에만 있으므로
    "어디선가 확실성을 떨어뜨리는 걸 깜빡했다"가 구조적으로 불가능해진다."""
    combined = Certainty.combine(*(certainty_of(s) for s in sources))

    # 출처 중 하나라도 미상이면 결과도 미상이다. 0으로 계산해 버리면
    # "모르는데 아는 척한 숫자"가 만들어진다.
    if unknown is None:
        for s in sources:
            if isinstance(s, Value) and s.unknown is not None:
                unknown = s.unknown
                amount = None
                break

    return Value(amount, unit, combined, unknown, label)


# --------------------------------------------------------------------------
# 근거
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LawBasis:
    """근거 조문. 화면에 "종합부동산세법 제8조 제1항"으로 렌더링된다."""

    law: str
    article: str = ""
    clause: str = ""
    item: str = ""
    mst: str = ""
    """법제처 법령일련번호. 이게 있으면 원문 링크를 만들 수 있다."""

    def cite_ko(self) -> str:
        parts = [self.law]
        if self.article:
            parts.append(f"제{self.article}조")
        if self.clause:
            parts.append(f"제{self.clause}항")
        if self.item:
            parts.append(f"제{self.item}호")
        return " ".join(parts)

    def url(self) -> str | None:
        if not self.mst:
            return None
        return f"https://www.law.go.kr/DRF/lawService.do?target=law&MST={self.mst}&type=HTML"


@dataclass(frozen=True, slots=True)
class SourceRef:
    """1차 출처. 2차 출처(뉴스·블로그)는 여기 들어오지 못한다.

    조사 과정에서 확인된 사실: 8.3 개편안에 대한 전문지 요약조차 원문과 어긋났다.
    한 매체는 장기보유특별공제를 '거주 연8% 최대80%' 단일 서술했지만 원문은
    '27 현행유지 → '28 거주6%+보유2% → '29~ 거주8%의 3단계다.
    """

    kind: Literal["pdf", "law_api", "nts", "manual"]
    file: str = ""
    page: int | None = None
    url: str = ""
    fetched_at: date | None = None
    note: str = ""

    def cite_ko(self) -> str:
        if self.kind == "pdf":
            return f"{self.file} p.{self.page}" if self.page else self.file
        if self.kind == "law_api":
            return f"국가법령정보 {self.url}"
        return self.note or self.url or self.file


@dataclass(frozen=True, slots=True)
class RuleRef:
    """적용된 규칙 블록 하나에 대한 참조. 룰셋 데이터에서 그대로 옮겨온다."""

    rule_id: str
    block_id: str
    track: str
    effective_from: date | None
    effective_to: date | None
    basis: LawBasis | None
    source: SourceRef | None
    certainty: Certainty
    note: str = ""

    def cite_ko(self) -> str:
        bits = []
        if self.basis:
            bits.append(self.basis.cite_ko())
        if self.source:
            bits.append(self.source.cite_ko())
        return " · ".join(bits) or self.rule_id


# --------------------------------------------------------------------------
# 분기와 미적용 대안
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Alternative:
    """평가했지만 적용되지 않은 선택지.

    시중 계산기는 이걸 '유의사항: 고령자 세액공제는 미반영입니다' 같은
    정적 면책 문구로 처리했다. 그러면 사용자는 자기에게 해당되는지조차 모른다.
    여기서는 엔진이 실제로 판정한 뒤 사유를 남긴다.
    """

    key: str
    label_ko: str
    reason_ko: str
    delta: Value | None = None
    """적용됐다면 세액이 얼마나 달라졌을지. 계산 가능한 경우에만."""
    actionable: bool = False
    """사용자가 행동을 바꿔 충족시킬 수 있는 요건인가. 전략 엔진의 입력이 된다."""


@dataclass(frozen=True, slots=True)
class BranchRecord:
    """조건 분기 기록. 어느 가지를 왜 탔는지."""

    condition_ko: str
    taken: str
    detail_ko: str = ""


# --------------------------------------------------------------------------
# 추적 노드
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TraceNode:
    """계산 한 단계. 트리를 이룬다."""

    step_id: str
    """버전 간 안정적인 식별자. 예: "jb.06.basic_deduction".
    diff가 이 id로 노드를 짝지으므로 함부로 바꾸면 비교가 깨진다."""

    label_ko: str
    output: Value
    subject: SubjectRef = field(default_factory=SubjectRef.case)
    inputs: tuple[tuple[str, Value], ...] = ()
    rules: tuple[RuleRef, ...] = ()
    formula: str = ""
    """기호 산식. 예: "max(0, 합산공시가격 − 기본공제) × 공정시장가액비율" """
    substitution: str = ""
    """숫자가 대입된 산식. 사람이 손으로 검산할 수 있어야 한다."""
    branch: BranchRecord | None = None
    alternatives_not_taken: tuple[Alternative, ...] = ()
    children: tuple["TraceNode", ...] = ()
    note_ko: str = ""

    @property
    def certainty(self) -> Certainty:
        """자신의 출력, 입력, 규칙, 자식 전부의 축별 최솟값.

        확실성을 손으로 전파시키지 않고 트리에서 파생시키므로
        "중간 단계에서 강등을 깜빡했다"가 생기지 않는다.
        """
        return Certainty.combine(
            self.output.certainty,
            *(v.certainty for _, v in self.inputs),
            *(r.certainty for r in self.rules),
            *(c.certainty for c in self.children),
        )

    def walk(self) -> Iterator["TraceNode"]:
        yield self
        for c in self.children:
            yield from c.walk()

    def find(self, step_id: str) -> "TraceNode | None":
        for n in self.walk():
            if n.step_id == step_id:
                return n
        return None

    def all_alternatives(self) -> tuple[Alternative, ...]:
        """트리 전체의 미적용 대안. 결과 화면의 '이런 것도 있습니다' 섹션."""
        return tuple(a for n in self.walk() for a in n.alternatives_not_taken)

    def all_rules(self) -> tuple[RuleRef, ...]:
        """트리 전체가 인용한 규칙. 중복 제거 후 '근거 조문' 목록이 된다."""
        seen: dict[tuple[str, str], RuleRef] = {}
        for n in self.walk():
            for r in n.rules:
                seen.setdefault((r.rule_id, r.block_id), r)
        return tuple(seen.values())

    def certainty_concerns(self) -> tuple[tuple[str, str], ...]:
        """트리 전체의 확실성 우려 사항을 (사유, 발생 단계)로 모은다.

        `certainty`는 축별 최솟값이라 가장 나쁜 것 하나만 보여준다. 그러면
        "국회 미통과"가 "가정"에 가려져 사용자가 개정안 기준임을 놓친다.
        화면에는 우려가 있는 항목을 **전부** 나열해야 한다.
        """
        found: dict[str, str] = {}
        for n in self.walk():
            sources = [n.output.certainty, *(r.certainty for r in n.rules)]
            for c in sources:
                for label in c.labels_ko():
                    found.setdefault(label, n.label_ko)
        return tuple(found.items())

    def unknowns(self) -> tuple[tuple[str, UnknownReason], ...]:
        """미상으로 흘러간 지점들. 사용자에게 "이걸 알려주시면 정확해집니다"로 쓴다."""
        return tuple(
            (n.step_id, n.output.unknown)
            for n in self.walk()
            if n.output.unknown is not None
        )


def node(
    step_id: str,
    label_ko: str,
    output: Value,
    **kwargs,
) -> TraceNode:
    """TraceNode 생성 헬퍼. 엔진 코드의 시각적 잡음을 줄인다."""
    for key in ("inputs", "rules", "alternatives_not_taken", "children"):
        if key in kwargs and not isinstance(kwargs[key], tuple):
            kwargs[key] = tuple(kwargs[key])
    return TraceNode(step_id=step_id, label_ko=label_ko, output=output, **kwargs)


# --------------------------------------------------------------------------
# 비교
# --------------------------------------------------------------------------


class DiffKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"
    SAME = "same"


@dataclass(frozen=True, slots=True)
class TraceDiffEntry:
    step_id: str
    label_ko: str
    kind: DiffKind
    left: Value | None
    right: Value | None
    rule_changed: bool = False
    """금액이 같아도 근거 규칙이 바뀌었으면 True. 검토 대상이다."""

    @property
    def delta(self) -> Number | None:
        if self.left is None or self.right is None:
            return None
        if not (self.left.is_known and self.right.is_known):
            return None
        if isinstance(self.left.amount, bool) or isinstance(self.right.amount, bool):
            return None
        return self.right.amount - self.left.amount  # type: ignore[operator]


def diff(left: TraceNode, right: TraceNode) -> tuple[TraceDiffEntry, ...]:
    """두 계산 트리를 step_id로 짝지어 비교한다.

    "'26 vs '27", "현행법 vs 개편안", "부부특례 신청 vs 미신청"이 전부 이 함수 하나다.
    """
    lmap = {n.step_id: n for n in left.walk()}
    rmap = {n.step_id: n for n in right.walk()}
    order = list(lmap) + [k for k in rmap if k not in lmap]

    entries: list[TraceDiffEntry] = []
    for step_id in order:
        ln, rn = lmap.get(step_id), rmap.get(step_id)
        if ln is None and rn is not None:
            entries.append(
                TraceDiffEntry(step_id, rn.label_ko, DiffKind.ADDED, None, rn.output)
            )
            continue
        if rn is None and ln is not None:
            entries.append(
                TraceDiffEntry(step_id, ln.label_ko, DiffKind.REMOVED, ln.output, None)
            )
            continue
        assert ln is not None and rn is not None
        rule_changed = _rule_keys(ln) != _rule_keys(rn)
        kind = (
            DiffKind.SAME
            if ln.output.amount == rn.output.amount and not rule_changed
            else DiffKind.CHANGED
        )
        entries.append(
            TraceDiffEntry(
                step_id, rn.label_ko, kind, ln.output, rn.output, rule_changed
            )
        )
    return tuple(entries)


def _rule_keys(n: TraceNode) -> frozenset[tuple[str, str]]:
    return frozenset((r.rule_id, r.block_id) for r in n.rules)


# --------------------------------------------------------------------------
# 표기
# --------------------------------------------------------------------------


def format_won(amount: Number | None) -> str:
    if amount is None:
        return "—"
    return f"{int(amount):,}원"


def to_manwon(amount: Number, digits: int = 1) -> Decimal:
    """만원 단위로 환산. 사사오입(ROUND_HALF_UP)을 명시적으로 쓴다.

    파이썬 기본 포매팅은 은행가 반올림이라 281.25 → 281.2가 된다.
    정부 문답자료는 사사오입이므로 그대로 두면 골든 케이스 대조가 어긋난다.
    표기 규칙조차 추측하지 않고 못 박아 두는 이유다.
    """
    quantum = Decimal(1).scaleb(-digits)
    value = Decimal(int(amount)) / Decimal(10_000)
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def format_manwon(amount: Number | None, digits: int = 1) -> str:
    """만원 단위 표기.

    정부 문답자료의 사례가 만원 단위(예: 154.9)로 실려 있어 골든 테스트와
    화면 표기의 단위를 여기에 맞춘다. 대조가 눈으로 되어야 검증이 굴러간다.
    """
    if amount is None:
        return "—"
    return f"{to_manwon(amount, digits):,.{digits}f}만원"


def format_rate(rate: Number | None) -> str:
    if rate is None:
        return "—"
    return f"{float(rate) * 100:g}%"
