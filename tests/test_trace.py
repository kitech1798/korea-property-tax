"""감사 추적 테스트.

핵심은 두 가지다.
  - 미상(unknown)이 0으로 둔갑하지 않고 결과까지 전파되는가
  - 확실성이 트리에서 자동으로 강등되는가 (손으로 전파시키면 반드시 빠뜨린다)
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction

import pytest

from realestate_tax.domain import (
    Certainty,
    DeterminationQuality,
    InputQuality,
    LegalStatus,
)
from realestate_tax.engine import (
    Alternative,
    DiffKind,
    LawBasis,
    RuleRef,
    SourceRef,
    SubjectRef,
    SubjectType,
    TraceNode,
    UnknownReason,
    Value,
    derive_value,
    diff,
    format_manwon,
    format_won,
    node,
)

BILL_PENDING = Certainty(legal=LegalStatus.BILL_PENDING)


def rule_ref(rule_id: str = "r1", block_id: str = "b1", certainty=None) -> RuleRef:
    return RuleRef(
        rule_id=rule_id,
        block_id=block_id,
        track="reform",
        effective_from=date(2027, 1, 1),
        effective_to=None,
        basis=LawBasis("종합부동산세법", "8", "1", mst="280417"),
        source=SourceRef("pdf", file="1. 개조식.pdf", page=18),
        certainty=certainty or BILL_PENDING,
    )


# --------------------------------------------------------------------------
# Value
# --------------------------------------------------------------------------


def test_값이_없으면_사유를_반드시_붙여야_한다():
    with pytest.raises(ValueError, match="UnknownReason"):
        Value(None)


def test_미상은_0으로_둔갑하지_않고_전파된다():
    unknown = Value.missing(UnknownReason.MISSING_PRIOR_YEAR, label="전년도 세액")
    known = Value.money(1_000_000)

    result = derive_value(999, known, unknown, label="세부담상한 적용액")

    # 계산을 밀어붙여 999를 내놓지 않는다. 값은 사라지고 사유가 남는다.
    assert result.amount is None
    assert result.unknown is UnknownReason.MISSING_PRIOR_YEAR
    assert result.certainty.input is InputQuality.UNKNOWN


def test_판정불가_미상은_determination축까지_강등한다():
    v = Value.missing(UnknownReason.UNDECIDABLE_FACT)
    assert v.certainty.determination is DeterminationQuality.UNDECIDABLE


def test_규칙없음_미상은_legal축까지_강등한다():
    v = Value.missing(UnknownReason.RULE_NOT_FOUND)
    assert v.certainty.legal is LegalStatus.ASSUMED


def test_파생값은_모든_출처의_확실성을_흡수한다():
    price = Value.money(3_000_000_000, certainty=Certainty(input=InputQuality.ESTIMATED))
    ref = rule_ref()  # bill_pending

    taxable = derive_value(2_100_000_000, price, ref, label="과세표준")

    assert taxable.certainty.legal is LegalStatus.BILL_PENDING  # 규칙에서
    assert taxable.certainty.input is InputQuality.ESTIMATED  # 공시가격에서
    assert set(taxable.certainty.labels_ko()) == {"국회 미통과", "추정치"}


def test_as_int는_미상을_0으로_흘리되_확실성이_이미_강등돼_있다():
    v = Value.missing(UnknownReason.MISSING_INPUT)
    assert v.as_int() == 0
    assert not v.is_known
    assert v.certainty.input is InputQuality.UNKNOWN


# --------------------------------------------------------------------------
# TraceNode
# --------------------------------------------------------------------------


def build_tree() -> TraceNode:
    price = node(
        "jb.01.price",
        "공시가격 합계",
        Value.money(3_000_000_000, certainty=Certainty(input=InputQuality.USER_INPUT)),
        subject=SubjectRef(SubjectType.PERSON, "p1", "본인"),
    )
    deduction = node(
        "jb.06.basic_deduction",
        "기본공제",
        Value.money(1_400_000_000),
        rules=(rule_ref("jongbuse.basic_deduction", "bd-2027-resident"),),
        formula="거주용 1주택 기본공제",
        substitution="1,400,000,000",
        alternatives_not_taken=(
            Alternative(
                key="non_resident",
                label_ko="비거주 1주택 공제(9억원)",
                reason_ko="과세기준일 현재 해당 주택에 거주 중이므로 미적용",
            ),
        ),
    )
    return node(
        "jb.07.taxable_base",
        "과세표준",
        Value.money(1_120_000_000),
        formula="max(0, 합산공시가격 − 기본공제) × 공정시장가액비율",
        substitution="max(0, 3,000,000,000 − 1,400,000,000) × 0.7",
        children=(price, deduction),
    )


def test_확실성은_자식과_규칙에서_자동으로_올라온다():
    tree = build_tree()
    # 루트 노드 자체는 아무 규칙도 안 달았지만, 자식의 bill_pending이 전파된다.
    assert tree.output.certainty.legal is LegalStatus.ENACTED
    assert tree.certainty.legal is LegalStatus.BILL_PENDING


def test_대입식이_남아_사람이_손으로_검산할_수_있다():
    tree = build_tree()
    assert tree.substitution == "max(0, 3,000,000,000 − 1,400,000,000) × 0.7"
    # 실제로 검산해 보면 맞아야 한다
    assert int(max(0, 3_000_000_000 - 1_400_000_000) * Fraction(7, 10)) == 1_120_000_000


def test_미적용_대안이_왜_안됐는지와_함께_수집된다():
    """시중 계산기의 '유의사항: 고령자 공제 미반영' 같은 정적 면책 문구를
    엔진 판정 결과로 대체하는 것이 이 기능의 목적이다."""
    alts = build_tree().all_alternatives()
    assert len(alts) == 1
    assert alts[0].key == "non_resident"
    assert "거주 중이므로" in alts[0].reason_ko


def test_근거_조문이_중복없이_모인다():
    tree = build_tree()
    refs = tree.all_rules()
    assert len(refs) == 1
    assert refs[0].basis.cite_ko() == "종합부동산세법 제8조 제1항"
    assert "law.go.kr" in refs[0].basis.url()
    assert refs[0].source.cite_ko() == "1. 개조식.pdf p.18"


def test_find와_walk로_특정_단계를_찾을_수_있다():
    tree = build_tree()
    assert tree.find("jb.06.basic_deduction").output.as_int() == 1_400_000_000
    assert tree.find("없는단계") is None
    assert len(list(tree.walk())) == 3


def test_미상_지점이_수집돼_추가_입력_요청으로_이어진다():
    tree = node(
        "root",
        "합계",
        Value.money(0),
        children=(
            node(
                "jb.11.burden_cap",
                "세부담상한",
                Value.missing(UnknownReason.MISSING_PRIOR_YEAR),
            ),
        ),
    )
    assert tree.unknowns() == (("jb.11.burden_cap", UnknownReason.MISSING_PRIOR_YEAR),)


# --------------------------------------------------------------------------
# diff — 연도 비교·트랙 비교의 엔진
# --------------------------------------------------------------------------


def test_금액이_바뀌면_변경으로_잡히고_증감이_계산된다():
    left = node("s1", "종부세", Value.money(1_549_000))
    right = node("s1", "종부세", Value.money(2_003_000))

    (entry,) = diff(left, right)
    assert entry.kind is DiffKind.CHANGED
    assert entry.delta == 454_000


def test_금액이_같아도_근거_규칙이_바뀌면_검토_대상이다():
    """'26과 '27의 세액이 우연히 같아도 적용 조문이 다르면 사람이 봐야 한다."""
    left = node("s1", "기본공제", Value.money(1_200_000_000), rules=(rule_ref("r", "old"),))
    right = node("s1", "기본공제", Value.money(1_200_000_000), rules=(rule_ref("r", "new"),))

    (entry,) = diff(left, right)
    assert entry.kind is DiffKind.CHANGED
    assert entry.rule_changed is True
    assert entry.delta == 0


def test_한쪽에만_있는_단계는_추가_삭제로_잡힌다():
    left = node("root", "합계", Value.money(0))
    right = node(
        "root",
        "합계",
        Value.money(0),
        children=(node("new_step", "거주공제", Value.money(500_000)),),
    )

    entries = {e.step_id: e for e in diff(left, right)}
    assert entries["root"].kind is DiffKind.SAME
    assert entries["new_step"].kind is DiffKind.ADDED
    assert entries["new_step"].left is None

    reverse = {e.step_id: e for e in diff(right, left)}
    assert reverse["new_step"].kind is DiffKind.REMOVED


def test_미상이_섞이면_증감을_계산하지_않는다():
    left = node("s1", "x", Value.money(100))
    right = node("s1", "x", Value.missing(UnknownReason.MISSING_INPUT))
    (entry,) = diff(left, right)
    assert entry.delta is None


# --------------------------------------------------------------------------
# 표기
# --------------------------------------------------------------------------


def test_만원_단위_표기는_정부_문답자료와_같은_형식():
    # 문답자료 p.44의 "154.9"와 눈으로 대조할 수 있어야 검증이 굴러간다.
    assert format_manwon(1_549_000) == "154.9만원"
    assert format_manwon(None) == "—"


def test_만원_환산은_사사오입이지_은행가반올림이_아니다():
    """파이썬 기본 포매팅은 281.25를 281.2로 만든다(round-half-to-even).
    정부 문서는 사사오입이므로 그대로 두면 골든 케이스 대조가 통째로 어긋난다."""
    assert format_manwon(2_812_500) == "281.3만원"
    assert format_manwon(2_807_500) == "280.8만원"
    assert f"{2_812_500 / 10_000:.1f}" == "281.2"  # 파이썬 기본값의 함정


def test_원_단위_표기():
    assert format_won(1_549_000) == "1,549,000원"
    assert format_won(None) == "—"
