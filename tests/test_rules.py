"""룰셋 로딩·해석·린팅 테스트.

resolver의 계약 하나가 이 파일의 주제다.

    조건에 맞는 블록은 정확히 하나여야 한다.

시중 계산기가 조정대상지역을 `주소.startswith("서울")`로 때운 것은
"못 찾으면 대충 이걸로"가 허용된 결과다. 그 한 줄이 계산기 전체의 신뢰를 깎았다.
여기서는 못 찾으면 예외를 던지고, 두 개 찾아도 예외를 던진다.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from pathlib import Path

import pytest

from realestate_tax.domain import LegalStatus
from realestate_tax.rules import (
    AmbiguousRule,
    MissingRule,
    RuleError,
    RuleSet,
    Selector,
    Track,
    parse_rule,
)
from realestate_tax.rules.lint import lint_ruleset

FIXTURE = Path(__file__).parent / "fixtures" / "ruleset_min"


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(FIXTURE)


# --------------------------------------------------------------------------
# 로딩
# --------------------------------------------------------------------------


def test_룰셋을_읽고_버전과_내용해시를_찍는다(rs: RuleSet):
    assert rs.version == "test-fixture"
    assert len(rs.content_hash) == 16
    assert rs.rule_ids == ("test.basic_deduction", "test.rate_table")


def test_내용이_같으면_해시도_같다(rs: RuleSet):
    # 결과에 룰셋 해시를 각인해 골든 테스트를 버전에 고정하기 위한 성질.
    assert RuleSet.load(FIXTURE).content_hash == rs.content_hash


def test_없는_규칙을_찾으면_기본값이_아니라_예외다(rs: RuleSet):
    with pytest.raises(MissingRule, match="규칙이 없다"):
        rs.rule("test.does_not_exist")


def test_rule_id가_두_파일에_중복되면_로딩이_실패한다(tmp_path: Path):
    """조용히 덮어쓰면 '분명히 고쳤는데 값이 안 바뀐다'는 최악의 디버깅이 시작된다."""
    body = (
        "rule_id: dup.rule\nblocks:\n"
        "  - id: a\n    effective_from: 2020-01-01\n    value: 1\n"
    )
    (tmp_path / "one.yaml").write_text(body, encoding="utf-8")
    (tmp_path / "two.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(RuleError, match="rule_id 중복"):
        RuleSet.load(tmp_path)


# --------------------------------------------------------------------------
# 해석 — 정확히 한 블록
# --------------------------------------------------------------------------


def test_현행법_1주택_기본공제는_12억(rs: RuleSet):
    r = rs.resolve(
        "test.basic_deduction",
        on=date(2026, 6, 1),
        track=Track.CURRENT,
        taxpayer="individual",
        one_house=True,
    )
    assert r.value == 1_200_000_000
    assert r.block.id == "bd-current-1house"
    assert r.ref().certainty.legal is LegalStatus.ENACTED


def test_개편안은_같은_1주택자를_거주여부로_가른다(rs: RuleSet):
    """개편안의 핵심 전환. 같은 사람·같은 집인데 사느냐 아니냐로 5억이 갈린다."""
    common = dict(on=date(2027, 6, 1), track=Track.REFORM, taxpayer="individual", one_house=True)

    resident = rs.resolve("test.basic_deduction", **common, resides=True)
    non_resident = rs.resolve("test.basic_deduction", **common, resides=False)

    assert resident.value == 1_400_000_000
    assert non_resident.value == 900_000_000
    assert resident.ref().certainty.legal is LegalStatus.BILL_PENDING


def test_시행_전_연도의_개편안은_현행_조문으로_답한다(rs: RuleSet):
    """★ "개편안이 통과된다고 가정하면 2026년은 얼마?"의 답은
    **"현행법과 같다"**이지 "계산할 수 없다"가 아니다.

    개편안은 현행법을 통째로 갈아치우지 않고 조항별로 시행일을 갖는다.
    시행일 전까지는 현행 조문이 그대로 적용된다 — 그게 시행일의 의미다.

    예전에는 여기서 MissingRule을 던졌고, 화면이 "개편안 조항이 아직
    시행되지 않습니다"라고만 말해 **계산을 거부하는 것처럼** 보였다."""
    r = rs.resolve(
        "test.basic_deduction",
        on=date(2026, 6, 1),
        track=Track.REFORM,
        taxpayer="individual",
        one_house=True,
        resides=True,
    )
    assert r.value == 1_200_000_000, "2026년은 현행 12억"
    assert r.block.id == "bd-current-1house"
    assert r.fell_back_to_current is True

    # 실제로 적용한 것은 현행 조문이다. 개편안 배지를 붙이면 거짓이 된다.
    assert r.ref().certainty.legal is not LegalStatus.BILL_PENDING

    # 2027년에는 개편안 블록이 잡힌다
    r27 = rs.resolve(
        "test.basic_deduction", on=date(2027, 6, 1), track=Track.REFORM,
        taxpayer="individual", one_house=True, resides=True,
    )
    assert r27.value == 1_400_000_000
    assert r27.fell_back_to_current is False


def test_조건이_부족하면_조용히_통과하지_않고_실패한다(rs: RuleSet):
    """resides를 안 알려줬는데 14억을 골라주면 그게 바로 시중 계산기의 실패다."""
    with pytest.raises(MissingRule) as exc:
        rs.resolve(
            "test.basic_deduction",
            on=date(2027, 6, 1),
            track=Track.REFORM,
            taxpayer="individual",
            one_house=True,
        )
    assert "입력 없음" in str(exc.value)
    # ⚠️ 시행일 폴백이 이 실패를 삼키면 안 된다. 삼키면 거주 여부를 모르는데
    #    현행 12억을 골라주게 된다 — 폴백을 처음 넣었을 때 실제로 그렇게 됐다.
    assert "resides" in str(exc.value)


def test_실패_메시지가_탈락_사유를_전부_보여준다(rs: RuleSet):
    with pytest.raises(MissingRule) as exc:
        rs.resolve(
            "test.basic_deduction",
            on=date(2027, 6, 1),
            track=Track.REFORM,
            taxpayer="individual",
            one_house=True,
        )
    msg = str(exc.value)
    assert "bd-reform-1house-resident" in msg
    assert "입력 없음" in msg and "resides" in msg
    assert "bd-corporation" in msg and "조건 불일치" in msg


def test_더_구체적인_블록이_이긴다(rs: RuleSet):
    """'28년에는 조건 1개짜리 단일세율표만 있어 그게 선택된다.
    '27년에는 조건 2개짜리가 이긴다 — 룰셋 안에 명시된 계층이지 암묵적 기본값이 아니다."""
    r2027 = rs.resolve(
        "test.rate_table",
        on=date(2027, 6, 1),
        track=Track.REFORM,
        taxpayer="individual",
        house_group="1-2",
    )
    assert r2027.block.id == "rt-reform-2027-1to2"
    assert r2027.block.selector.specificity == 2

    r2028 = rs.resolve(
        "test.rate_table",
        on=date(2028, 6, 1),
        track=Track.REFORM,
        taxpayer="individual",
        house_group="1-2",
    )
    assert r2028.block.id == "rt-reform-2028-all"


def test_같은_우선순위가_겹치면_모호로_실패한다(tmp_path: Path):
    (tmp_path / "dup.yaml").write_text(
        "rule_id: x.dup\nblocks:\n"
        "  - id: a\n    effective_from: 2020-01-01\n    selector: {k: 1}\n    value: 10\n"
        "  - id: b\n    effective_from: 2020-01-01\n    selector: {k: 1}\n    value: 20\n",
        encoding="utf-8",
    )
    rs = RuleSet.load(tmp_path)
    with pytest.raises(AmbiguousRule, match="여러 개 맞았다"):
        rs.resolve("x.dup", on=date(2026, 6, 1), track=Track.CURRENT, k=1)


def test_resolve_or_none은_없을_때만_None을_준다(tmp_path: Path):
    """모호(AmbiguousRule)까지 삼키면 룰셋 오류가 조용히 묻힌다. 그건 통과시키지 않는다."""
    (tmp_path / "dup.yaml").write_text(
        "rule_id: x.dup\nblocks:\n"
        "  - id: a\n    effective_from: 2020-01-01\n    selector: {k: 1}\n    value: 10\n"
        "  - id: b\n    effective_from: 2020-01-01\n    selector: {k: 1}\n    value: 20\n",
        encoding="utf-8",
    )
    rs = RuleSet.load(tmp_path)
    assert rs.resolve_or_none("x.dup", on=date(2026, 6, 1), track=Track.CURRENT, k=9) is None
    with pytest.raises(AmbiguousRule):
        rs.resolve_or_none("x.dup", on=date(2026, 6, 1), track=Track.CURRENT, k=1)


def test_법인은_두_트랙_모두에서_기본공제가_0(rs: RuleSet):
    for track in (Track.CURRENT, Track.REFORM):
        r = rs.resolve(
            "test.basic_deduction",
            on=date(2027, 6, 1),
            track=track,
            taxpayer="corporation",
        )
        assert r.value == 0


# --------------------------------------------------------------------------
# 셀렉터
# --------------------------------------------------------------------------


def test_셀렉터는_없는_키를_통과시키지_않는다():
    """'없으면 통과'로 두면 조용한 오적용이 생긴다."""
    sel = Selector({"resides": True})
    assert sel.matches({"resides": True})
    assert not sel.matches({})


@pytest.mark.parametrize(
    "want, value, expected",
    [
        ({"min": 3}, 3, True),
        ({"min": 3}, 2, False),
        ({"min": 3, "max": 5}, 5, True),
        ({"min": 3, "max": 5}, 6, False),
        (["a", "b"], "b", True),
        (["a", "b"], "c", False),
        (True, True, True),
    ],
)
def test_셀렉터_값_형태(want, value, expected):
    assert Selector({"k": want}).matches({"k": value}) is expected


# --------------------------------------------------------------------------
# 세율표
# --------------------------------------------------------------------------


def test_비율은_문자열로만_받는다_부동소수점_금지():
    with pytest.raises(RuleError, match="문자열로 적어야"):
        parse_rule(
            {
                "rule_id": "x",
                "blocks": [{"id": "a", "table": [{"upto": None, "rate": 0.013}]}],
            }
        )


def test_비율_표기_형식():
    rule = parse_rule(
        {
            "rule_id": "x",
            "blocks": [
                {
                    "id": "a",
                    "table": [
                        {"upto": 100, "rate": "1.3%"},
                        {"upto": None, "rate": "7/10"},
                    ],
                }
            ],
        }
    )
    brackets = rule.blocks[0].table.brackets
    assert brackets[0].rate == Fraction(13, 1000)
    assert brackets[1].rate == Fraction(7, 10)


def test_누진세율은_구간별로_누적_계산되고_대입식이_남는다(rs: RuleSet):
    table = rs.resolve(
        "test.rate_table",
        on=date(2026, 6, 1),
        track=Track.CURRENT,
        taxpayer="individual",
        house_group="1-2",
    ).block.table

    # 과세표준 5억 → 3억까지 0.5%, 나머지 2억은 0.7%
    tax, bracket, sub = table.tax_for(500_000_000)
    assert tax == 300_000_000 * 5 // 1000 + 200_000_000 * 7 // 1000
    assert bracket.upto == 600_000_000
    assert sub == "300,000,000 × 0.5% + 200,000,000 × 0.7%"


def test_과세표준_0이하는_세액_0(rs: RuleSet):
    table = rs.resolve(
        "test.rate_table",
        on=date(2026, 6, 1),
        track=Track.CURRENT,
        taxpayer="individual",
        house_group="1-2",
    ).block.table
    assert table.tax_for(0)[0] == 0
    assert table.tax_for(-1)[0] == 0


def test_개편안_2027표는_6억초과12억이하_구간이_1_0에서_1_3으로_오른다(rs: RuleSet):
    """개조식 p.18의 핵심 변화 한 줄을 데이터로 고정한다."""

    def rate_at(track, on, base):
        t = rs.resolve(
            "test.rate_table",
            on=on,
            track=track,
            taxpayer="individual",
            house_group="1-2",
        ).block.table
        return t.bracket_for(base).rate

    assert rate_at(Track.CURRENT, date(2026, 6, 1), 1_000_000_000) == Fraction(10, 1000)
    assert rate_at(Track.REFORM, date(2027, 6, 1), 1_000_000_000) == Fraction(13, 1000)


# --------------------------------------------------------------------------
# 린터
# --------------------------------------------------------------------------


def test_실제_룰셋_픽스처는_린트를_통과한다(rs: RuleSet):
    findings = lint_ruleset(rs)
    errors = [f for f in findings if f.severity == "error"]
    assert errors == [], "\n".join(str(f) for f in errors)


def _lint_one(tmp_path: Path, body: str):
    (tmp_path / "t.yaml").write_text(body, encoding="utf-8")
    return lint_ruleset(RuleSet.load(tmp_path))


def test_린터는_근거조문_누락을_잡는다(tmp_path: Path):
    findings = _lint_one(
        tmp_path,
        "rule_id: x\nblocks:\n  - id: a\n    effective_from: 2020-01-01\n    value: 1\n",
    )
    assert {f.code for f in findings} >= {"L1", "L2"}


def test_린터는_세율_구간_역전을_잡는다(tmp_path: Path):
    findings = _lint_one(
        tmp_path,
        "rule_id: x\nblocks:\n  - id: a\n    effective_from: 2020-01-01\n"
        "    table:\n      - {upto: 600000000, rate: '0.005'}\n"
        "      - {upto: 300000000, rate: '0.007'}\n"
        "      - {upto: null, rate: '0.010'}\n",
    )
    assert any(f.code == "L5" for f in findings)


def test_린터는_최고구간이_닫혀있으면_잡는다(tmp_path: Path):
    """마지막 구간의 상한이 닫혀 있으면 초고가 주택이 과세표준 구간을 벗어나
    런타임에 터진다. 그건 CI에서 잡아야 한다."""
    findings = _lint_one(
        tmp_path,
        "rule_id: x\nblocks:\n  - id: a\n    effective_from: 2020-01-01\n"
        "    table:\n      - {upto: 300000000, rate: '0.005'}\n"
        "      - {upto: 9400000000, rate: '0.027'}\n",
    )
    assert any(f.code == "L6" for f in findings)


def test_린터는_셀렉터_충돌을_런타임_전에_잡는다(tmp_path: Path):
    findings = _lint_one(
        tmp_path,
        "rule_id: x\nblocks:\n"
        "  - id: a\n    effective_from: 2020-01-01\n    selector: {k: 1}\n    value: 1\n"
        "  - id: b\n    effective_from: 2020-01-01\n    selector: {k: 1}\n    value: 2\n",
    )
    assert any(f.code == "L4" for f in findings)


def test_린터는_조건이_배타적이면_충돌로_보지_않는다(tmp_path: Path):
    findings = _lint_one(
        tmp_path,
        "rule_id: x\nblocks:\n"
        "  - id: a\n    effective_from: 2020-01-01\n    selector: {k: 1}\n    value: 1\n"
        "  - id: b\n    effective_from: 2020-01-01\n    selector: {k: 2}\n    value: 2\n",
    )
    assert not any(f.code == "L4" for f in findings)


def test_린터는_기간이_안겹치면_충돌로_보지_않는다(tmp_path: Path):
    findings = _lint_one(
        tmp_path,
        "rule_id: x\nblocks:\n"
        "  - id: a\n    effective_from: 2027-01-01\n    effective_to: 2027-12-31\n"
        "    selector: {k: 1}\n    value: 1\n"
        "  - id: b\n    effective_from: 2028-01-01\n    selector: {k: 1}\n    value: 2\n",
    )
    assert not any(f.code == "L4" for f in findings)


def test_린터는_2027년_이후_시행인데_현행법_표기면_경고한다(tmp_path: Path):
    """개편안 값을 enacted로 적어두면 화면에 '국회 미통과' 배지가 안 뜬다.
    그건 사용자를 오도한다."""
    findings = _lint_one(
        tmp_path,
        "rule_id: x\nblocks:\n  - id: a\n    effective_from: 2027-01-01\n"
        "    certainty: enacted\n    value: 1\n",
    )
    warn = [f for f in findings if f.code == "L9"]
    assert warn and warn[0].severity == "warning"
