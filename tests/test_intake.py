"""공시가격 입력 계층 테스트.

이 계층의 존재 이유는 하나다 — **공시가격이 조금만 틀려도 세액이 통째로 뒤집히는
구간이 있다.** 그 구간에 들어오면 사용자를 멈춰 세운다.
"""

from __future__ import annotations

from datetime import date

import pytest

from realestate_tax.intake import (
    PriceParseError,
    Severity,
    check,
    deduction_boundaries,
    guidance,
    intake,
    parse_won,
)
from realestate_tax.rules import RuleSet, Track, default_ruleset_root


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


# --------------------------------------------------------------------------
# 정규화 — 알리미 화면을 그대로 붙여넣을 수 있어야 한다
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("324000000", 324_000_000),
        ("324,000,000", 324_000_000),
        ("324,000,000원", 324_000_000),
        ("3억2400만", 324_000_000),
        ("3억 2,400만원", 324_000_000),
        ("3억", 300_000_000),
        ("12억", 1_200_000_000),
        (" 9억 ", 900_000_000),
    ],
)
def test_사람이_쓰는_금액_표기를_전부_받는다(text, expected):
    """입력 마찰이 줄어야 오타가 준다. 오타 하나가 세액을 뒤집는다."""
    assert parse_won(text) == expected


@pytest.mark.parametrize("bad", ["", "   ", "모름", "원"])
def test_금액으로_읽을_수_없으면_거부한다(bad):
    with pytest.raises(PriceParseError):
        parse_won(bad)


def test_읽을_수_없는_입력은_계산을_막는다(rs: RuleSet):
    parsed = intake("잘 모르겠어요", rs)
    assert parsed.value is None
    assert not parsed.ok
    assert parsed.notices[0].severity is Severity.BLOCK


# --------------------------------------------------------------------------
# 자릿수 방어
# --------------------------------------------------------------------------


def test_만원_단위로_잘못_넣으면_막는다(rs: RuleSet):
    """'32400'을 3억 2400만원이라 생각하고 넣는 사고를 막는다."""
    parsed = intake("32400", rs)
    assert not parsed.ok
    assert "만원 단위" in parsed.notices[0].hint_ko


def test_자릿수가_과하면_막는다(rs: RuleSet):
    parsed = intake("324000000000", rs)
    assert not parsed.ok
    assert parsed.notices[0].severity is Severity.BLOCK


# --------------------------------------------------------------------------
# ★ 경계 경고 — 이 계층의 핵심
# --------------------------------------------------------------------------


def test_경계값을_코드가_아니라_룰셋에서_뽑는다(rs: RuleSet):
    """개편안이 12억 → 14억/9억으로 바꿨고 국회에서 또 바뀔 수 있다.
    경계를 코드에 박으면 룰셋을 고쳐도 경고가 따라오지 않는다."""
    boundaries = deduction_boundaries(rs, on=date(2027, 6, 1))
    assert 1_400_000_000 in boundaries  # 개편안 거주용 1주택
    assert 900_000_000 in boundaries  # 비거주 1주택 / 재산세 특례 상한
    assert all(label for label in boundaries.values())


def test_현행_12억_경계도_잡힌다(rs: RuleSet):
    boundaries = deduction_boundaries(rs, on=date(2026, 6, 1), tracks=(Track.CURRENT,))
    assert 1_200_000_000 in boundaries


@pytest.mark.parametrize(
    "value",
    [
        850_000_000,  # 9억 -5.6%
        900_000_000,  # 정확히 경계
        980_000_000,  # 9억 +8.9%
        1_300_000_000,  # 14억 -7.1%
        1_450_000_000,  # 14억 +3.6%
    ],
)
def test_경계_10퍼센트_안에_들어오면_경고한다(rs: RuleSet, value):
    notices = check(value, rs, on=date(2027, 6, 1))
    warns = [n for n in notices if n.severity is Severity.WARN]
    assert warns, f"{value:,}원에서 경계 경고가 없다"
    assert "경계" in warns[0].message_ko
    assert "알리미" in warns[0].hint_ko


@pytest.mark.parametrize("value", [500_000_000, 2_500_000_000, 4_000_000_000])
def test_경계에서_충분히_떨어지면_경고하지_않는다(rs: RuleSet, value):
    """아무 때나 경고하면 사용자가 경고를 무시하게 된다."""
    assert not check(value, rs, on=date(2027, 6, 1))


def test_경계_경고에는_확인_경로_안내가_붙는다(rs: RuleSet):
    parsed = intake("9억", rs, on=date(2027, 6, 1))
    assert parsed.value == 900_000_000
    text = guidance(parsed)
    assert "realtyprice.kr" in text
    assert "지번 검색" in text
    # 검색 결과 화면은 URL이 안 바뀌므로 딥링크를 만들 수 없다 — 그 사실을 명시한다
    assert "바로가기 링크를 만들 수 없습니다" in text


def test_문제가_없으면_안내문을_붙이지_않는다(rs: RuleSet):
    assert guidance(intake("25억", rs, on=date(2027, 6, 1))) == ""


def test_경계_경고는_계산을_막지는_않는다(rs: RuleSet):
    """확인은 권하되 진행은 막지 않는다. 사용자가 이미 확인했을 수도 있다."""
    parsed = intake("14억", rs, on=date(2027, 6, 1))
    assert parsed.ok
    assert any(n.severity is Severity.WARN for n in parsed.notices)


def test_경계_경고가_실제_세액_절벽과_같은_지점을_가리킨다(rs: RuleSet):
    """경고 지점과 실제 세액이 튀는 지점이 어긋나면 경고가 무의미하다.
    재산세 1세대1주택 세율 특례 상한(9억)에서 실제로 세액이 점프하는지 확인한다."""
    from fractions import Fraction

    from realestate_tax.domain import (
        Household,
        HouseholdId,
        Ownership,
        Person,
        PersonId,
        PriceFact,
        Property,
        PropertyId,
        PropertyKind,
        TaxCase,
    )
    from realestate_tax.engine.property_tax import compute_property_tax

    def tax_at(price: int) -> int:
        hh = HouseholdId("hh")
        p = Person(id=PersonId("p0"), household_id=hh)
        prop = Property(
            id=PropertyId("h0"),
            kind=PropertyKind.APARTMENT,
            legal_dong_code="1168010100",
            published_prices=(PriceFact(2026, price),),
        )
        case = TaxCase(
            year=2026,
            persons=(p,),
            households=(Household(id=hh, member_ids=(p.id,)),),
            properties=(prop,),
            ownerships=(Ownership(p.id, prop.id, share=Fraction(1)),),
        )
        return compute_property_tax(
            case, PropertyId("h0"), rs, track=Track.CURRENT, owner_id=PersonId("p0")
        ).total.as_int()

    jump = tax_at(900_000_001) - tax_at(900_000_000)
    assert jump > 200_000, "9억 경계에서 세액이 튀지 않으면 경고 설계가 틀린 것이다"
    assert check(900_000_000, rs, on=date(2026, 6, 1))


# --------------------------------------------------------------------------
# ★ 모호하면 추측하지 않고 거부한다 (2026-08-05 경계 훑기)
#   세금 도구에서 숫자를 **조용히** 잘못 읽는 것이 가장 나쁜 실패다.
#   사용자는 화면의 숫자를 자기가 넣은 값이라고 믿는다.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, was",
    [
        ("15억5000", 1_500_005_000),   # 15억 5,000만을 뜻했는데 5천원으로 읽혔다
        ("-5억", 500_000_000),         # 마이너스를 먹고 양수가 됐다
        ("1.5e9", 10),                 # 'e'를 무시하고 1 + 9로 읽었다
    ],
)
def test_조용히_잘못_읽던_입력을_이제_거부한다(raw, was):
    """세 가지 모두 예전에는 **알림 없이** 통과했다."""
    with pytest.raises(PriceParseError):
        parse_won(raw)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("15억", 1_500_000_000),
        ("1,500,000,000", 1_500_000_000),
        ("1500000000원", 1_500_000_000),
        ("15억 3,200만", 1_532_000_000),
        ("3억2,400만", 324_000_000),   # 단위가 둘 다 있으면 붙여 써도 명확하다
        ("15 억", 1_500_000_000),
    ],
)
def test_정상_표기는_그대로_읽는다(raw, expected):
    """거부 규칙이 멀쩡한 입력을 막으면 도구가 못 쓰게 된다."""
    assert parse_won(raw) == expected


def test_단위_없는_숫자는_혼자일_때만_허용한다():
    """'15억5000'의 5000이 만인지 천인지 원인지 알 수 없다.
    모르는 것을 골라잡으면 5,000만원이 5천원이 된다."""
    assert parse_won("1500000000") == 1_500_000_000  # 혼자면 원
    with pytest.raises(PriceParseError, match="단위를 붙여"):
        parse_won("15억 5000")


def test_읽지_못한_글자가_남으면_거부한다():
    """남은 글자를 무시하면 뜻이 통째로 바뀌는 입력이 통과한다."""
    for raw in ("1.5e9", "15억 x", "12만$"):
        with pytest.raises(PriceParseError):
            parse_won(raw)
