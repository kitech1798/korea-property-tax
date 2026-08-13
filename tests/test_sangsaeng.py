"""상생임대주택 특례 판정 엔진 테스트 (소득세법 시행령 §155의3).

가장 중요한 것은 `test_임대기간은_종료일을_포함해_센다`와 재혁 사례 골든이다.
하루를 잘못 세면 24개월 전세가 23개월로 읽혀 요건에서 탈락하고, 비과세 12억이
통째로 날아간다.
"""

from __future__ import annotations

from datetime import date

import pytest

from realestate_tax.domain import (
    LeaseOrigin,
    LeaseSpell,
    Person,
    PersonId,
    PriceFact,
    Property,
    PropertyId,
    PropertyKind,
    TaxCase,
)
from realestate_tax.engine.periods import full_months
from realestate_tax.engine.sangsaeng import (
    _lease_months,
    _plus_months,
    assess,
    within_transfer_deadline,
)
from realestate_tax.rules import RuleSet, Track, default_ruleset_root

HOUSE = PropertyId("h1")
EOK = 100_000_000


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


def make_case(*leases: LeaseSpell) -> TaxCase:
    return TaxCase(
        year=2026,
        persons=(Person(id=PersonId("me")),),
        properties=(
            Property(
                id=HOUSE,
                kind=PropertyKind.APARTMENT,
                legal_dong_code="1168010100",
                published_prices=(PriceFact(year=2026, value=15 * EOK),),
            ),
        ),
        leases=leases,
    )


def lease(start: str, end: str, deposit: int, *, contracted: str | None = None,
          rent: int = 0, origin: LeaseOrigin = LeaseOrigin.NEW,
          evidenced: bool | None = True, **kw) -> LeaseSpell:
    return LeaseSpell(
        property_id=HOUSE,
        start=date.fromisoformat(start),
        end=date.fromisoformat(end),
        deposit=deposit,
        monthly_rent=rent,
        origin=origin,
        contracted_on=date.fromisoformat(contracted) if contracted else None,
        down_payment_evidenced=evidenced,
        **kw,
    )


# 직전 24개월 + 상생 24개월, 보증금 5억 → 5억 2천(4% 인상).
PRIOR = lease("2023-02-01", "2025-01-31", 5 * EOK, contracted="2022-12-10")
SANGSAENG = lease("2025-02-01", "2027-01-31", 520_000_000, contracted="2024-12-15")


# --------------------------------------------------------------------------
# 기간 계산 — 하루가 요건을 뒤집는다
# --------------------------------------------------------------------------


def test_임대기간은_종료일을_포함해_센다():
    """★ 2025-02-01 ~ 2027-01-31 전세는 **24개월**이다. 23개월이 아니다.

    `full_months`의 until은 제외 규약이므로 종료일 다음 날을 넘겨야 한다.
    이 하루를 빠뜨리면 정상적인 2년 전세가 §155의3①3호(2년 이상)에서 탈락해
    비과세 12억이 사라진다.
    """
    assert _lease_months(SANGSAENG) == 24
    # 규약을 직접 확인 — 종료일 그대로 넣으면 23개월로 읽힌다
    assert full_months(date(2025, 2, 1), date(2027, 1, 31)) == 23
    assert full_months(date(2025, 2, 1), date(2027, 2, 1)) == 24


def test_직전계약_1년6개월은_18개월이다():
    assert _lease_months(lease("2023-08-01", "2025-01-31", 5 * EOK)) == 18
    assert _lease_months(lease("2023-08-02", "2025-01-31", 5 * EOK)) == 17


@pytest.mark.parametrize(
    "start, end, expected",
    [
        # 2025-08-31의 18개월은 민법 §160③상 2027-02-28에 만료한다(2월엔 31일이 없다).
        ("2025-08-31", "2027-02-28", 18),   # 만료일까지 임대 — 충족
        ("2025-08-31", "2027-02-27", 17),   # ★ 하루 모자람 — 예전에는 18로 읽혔다
        ("2025-08-31", "2027-03-01", 18),   # 하루 더 — 여전히 18(19가 아니다)
        # 말일로 당겨지지 않는 달은 영향이 없어야 한다
        ("2025-01-31", "2027-01-30", 24),
        ("2025-01-31", "2027-01-29", 23),
        ("2025-02-01", "2027-01-31", 24),
    ],
)
def test_말일로_당겨진_달은_하루를_더_요구한다(start, end, expected):
    """★ 2026-08-13 멀티에이전트 감사에서 잡힌 경계.

    `min(start.day, 말일)`로만 비교하면 만료일 하루 전에 끝난 계약도 만 개월로
    읽힌다. 요건을 **통과시키는** 방향이라 과소신고가 된다.
    말일에 시작하는 전세는 드물지 않다.
    """
    assert _lease_months(lease(start, end, 5 * EOK)) == expected


def test_1개월_미만은_1개월로_본다():
    """§155의3③ 후단."""
    assert _lease_months(lease("2025-02-01", "2025-02-10", 5 * EOK)) == 1


def test_중도_퇴거하면_실제_임대기간으로_센다():
    early = lease("2025-02-01", "2027-01-31", 5 * EOK, vacated_on=date(2026, 7, 31))
    assert _lease_months(early) == 18


@pytest.mark.parametrize(
    "start, months, expected",
    [
        ("2027-01-31", 12, "2028-01-31"),
        ("2027-01-31", 1, "2027-02-28"),   # 2월엔 31일이 없다 — 민법 §160③
        ("2027-12-31", 12, "2028-12-31"),
        ("2028-02-29", 12, "2029-02-28"),  # 윤년 → 평년
    ],
)
def test_월_가산은_말일_규칙을_따른다(start, months, expected):
    assert _plus_months(date.fromisoformat(start), months) == date.fromisoformat(expected)


# --------------------------------------------------------------------------
# 요건 판정
# --------------------------------------------------------------------------


def test_요건을_모두_갖추면_상생임대주택이다(rs: RuleSet):
    v = assess(make_case(PRIOR, SANGSAENG), HOUSE, rs, on=date(2027, 6, 1), track=Track.CURRENT)
    assert v.applies
    assert not v.undecidable
    assert v.prior is PRIOR and v.lease is SANGSAENG
    assert any("4.00%" in c for c in v.checks_ko)


def test_승계받은_계약은_직전임대차계약이_될_수_없다(rs: RuleSet):
    """§155의3①1호 괄호. 세입자가 살던 집을 사서 계약을 물려받은 경우다.

    상생임대 판정에서 가장 흔한 함정 — 이걸 놓치면 요건을 충족한다고 잘못 안내한다.
    """
    succeeded = lease("2023-02-01", "2025-01-31", 5 * EOK,
                      contracted="2022-12-10", origin=LeaseOrigin.SUCCEEDED)
    v = assess(make_case(succeeded, SANGSAENG), HOUSE, rs, on=date(2027, 6, 1), track=Track.CURRENT)
    assert not v.applies
    assert any("승계" in r for r in v.reasons_ko)


def test_증가율이_5퍼센트를_넘으면_탈락한다(rs: RuleSet):
    over = lease("2025-02-01", "2027-01-31", 530_000_000, contracted="2024-12-15")  # 6%
    v = assess(make_case(PRIOR, over), HOUSE, rs, on=date(2027, 6, 1), track=Track.CURRENT)
    assert not v.applies
    assert any("6.00%" in r for r in v.reasons_ko)


def test_경계값_정확히_5퍼센트는_통과한다(rs: RuleSet):
    """법문은 "100분의 5를 **초과**하지 않는"이다. 5.00%는 충족이다.

    Fraction으로 계산하는 이유 — 5억 → 5억 2500만을 float로 재면
    0.05000000000000001이 되어 경계에서 탈락시킬 수 있다.
    """
    exact = lease("2025-02-01", "2027-01-31", 525_000_000, contracted="2024-12-15")
    v = assess(make_case(PRIOR, exact), HOUSE, rs, on=date(2027, 6, 1), track=Track.CURRENT)
    assert v.applies


def test_직전임대차가_1년6개월_미만이면_탈락한다(rs: RuleSet):
    short = lease("2023-09-01", "2025-01-31", 5 * EOK, contracted="2023-07-10")  # 17개월
    v = assess(make_case(short, SANGSAENG), HOUSE, rs, on=date(2027, 6, 1), track=Track.CURRENT)
    assert not v.applies
    assert any("17개월" in r for r in v.reasons_ko)


def test_상생임대차가_2년_미만이면_탈락한다(rs: RuleSet):
    short = lease("2025-02-01", "2026-12-31", 520_000_000, contracted="2024-12-15")  # 23개월
    v = assess(make_case(PRIOR, short), HOUSE, rs, on=date(2027, 6, 1), track=Track.CURRENT)
    assert not v.applies
    assert any("23개월" in r for r in v.reasons_ko)


def test_임대개시가_기한_밖이면_탈락한다(rs: RuleSet):
    """★ 2026-08-13 멀티에이전트 감사(원문대조)에서 잡힌 누락.

    §155의3①1호는 "…기간 중에 체결(…)하고 **임대를 개시할 것**"이고, 상세본 p.78이
    요건을 "'21.12.20.~'26.12.31. 중 계약체결 **및 임대개시**"로 풀어 적는다.
    예전에는 체결일만 봐서, '26.12.31.에 계약하고 '27년에 임대를 시작한 계약이
    통과했다.
    """
    late_start = lease("2027-01-01", "2029-01-31", 520_000_000, contracted="2026-12-20")
    v = assess(make_case(PRIOR, late_start), HOUSE, rs,
               on=date(2029, 6, 1), track=Track.CURRENT)
    assert not v.applies
    assert any("임대 개시일" in r for r in v.reasons_ko)


def test_체결일이_기한_밖이면_탈락한다(rs: RuleSet):
    """§155의3①1호 — '21.12.20.~'26.12.31. 중 체결."""
    late = lease("2027-02-01", "2029-01-31", 520_000_000, contracted="2027-01-05")
    v = assess(make_case(PRIOR, SANGSAENG, late), HOUSE, rs,
               on=date(2029, 6, 1), track=Track.CURRENT)
    # SANGSAENG 쌍이 여전히 유효하므로 전체는 통과한다
    assert v.applies
    assert v.lease is SANGSAENG


# --------------------------------------------------------------------------
# 판정 불가 — '아니다'와 '모르겠다'를 섞지 않는다
# --------------------------------------------------------------------------


def test_계약금_증빙을_모르면_판정하지_않는다(rs: RuleSet):
    """유리한 쪽으로 가정하면 과소신고가 된다."""
    unknown = lease("2025-02-01", "2027-01-31", 520_000_000,
                    contracted="2024-12-15", evidenced=None)
    v = assess(make_case(PRIOR, unknown), HOUSE, rs, on=date(2027, 6, 1), track=Track.CURRENT)
    assert not v.applies
    assert v.undecidable
    assert any("계약금" in r for r in v.reasons_ko)


def test_보증금과_월세를_전환했으면_판정하지_않는다(rs: RuleSet):
    """§155의3②는 민간임대주택법 §44④ 기준으로 계산하라고 한다.
    그 산식을 확보하지 못했으므로 지어내지 않는다."""
    converted = lease("2025-02-01", "2027-01-31", 3 * EOK,
                      contracted="2024-12-15", rent=500_000)
    v = assess(make_case(PRIOR, converted), HOUSE, rs, on=date(2027, 6, 1), track=Track.CURRENT)
    assert v.undecidable
    assert any("전환" in r for r in v.reasons_ko)


def test_보증금과_월세가_모두_0이면_판정하지_않는다(rs: RuleSet):
    """★ 회귀 — 16,200건 스트레스 스윕의 경계 사례에서 발견(2026-08-13).

    증가율을 0%로 읽어 **요건을 통과시키고 있었다.** 그런데 보증금도 월세도 없다는
    것은 임대차의 대가가 없다는 뜻이다. 무상거주는 사용대차라 상생임대 대상이
    아니고, 실제로는 금액을 입력하지 않은 경우가 대부분이다.
    빈칸에 혜택을 주는 것은 이 프로젝트가 가장 경계하는 실패 방식이다.
    """
    free_prior = lease("2023-02-01", "2025-01-31", 0)
    free_sang = lease("2025-02-01", "2027-01-31", 0, contracted="2024-12-15")
    v = assess(make_case(free_prior, free_sang), HOUSE, rs,
               on=date(2027, 6, 1), track=Track.CURRENT)
    assert not v.applies
    assert v.undecidable
    assert any("0원" in r for r in v.reasons_ko)


def test_기간이_겹치는_계약은_직전_관계가_아니다(rs: RuleSet):
    """★ 회귀 — 같은 스윕에서 발견.

    §155의3①1호의 '직전 임대차계약'은 상생임대차계약 **바로 앞**의 계약이다.
    기간이 겹치면 앞뒤 관계가 성립하지 않는데, 겹친 채로 쌍을 이뤄
    3년짜리 계약 하나가 직전계약으로 인정되고 있었다.
    """
    overlapping = lease("2023-02-01", "2026-01-31", 5 * EOK, contracted="2022-12-10")
    v = assess(make_case(overlapping, SANGSAENG), HOUSE, rs,
               on=date(2027, 6, 1), track=Track.CURRENT)
    assert not v.applies
    assert v.undecidable
    assert any("겹칩니다" in r for r in v.reasons_ko)


def test_임대차가_한_건뿐이면_직전계약이_없다(rs: RuleSet):
    v = assess(make_case(SANGSAENG), HOUSE, rs, on=date(2027, 6, 1), track=Track.CURRENT)
    assert not v.applies
    assert not v.undecidable


# --------------------------------------------------------------------------
# 개편안 양도기한 — 상세본 p.78
# --------------------------------------------------------------------------


def test_현행에는_양도기한이_없다(rs: RuleSet):
    v = assess(make_case(PRIOR, SANGSAENG), HOUSE, rs, on=date(2028, 6, 1), track=Track.CURRENT)
    assert v.applies
    assert v.transfer_deadline is None
    assert within_transfer_deadline(v, date(2030, 1, 1)) is None


def test_재혁_사례_양도기한은_2028_01_31이다(rs: RuleSet):
    """★ 골든. 손으로 적은 상담 메모의 사건을 엔진이 재현한다.

    상생임대차계약 종료 2027-01-31 → '26.12.31. 이후 종료이므로 상세본 p.78 ➋ 적용
      = 계약종료 후 1년이 되는 날(2028-01-31)과 '29.12.31. 중 빠른 날
      = **2028-01-31**

    메모에 적힌 "2027.12.31.까지 팔아야"는 ➊(‘26.12.31. 이전 종료)의 단서라
    이 사건에는 적용되지 않는다. 메모의 "2028년 2월 1일"은 하루 넘어간다.
    """
    v = assess(make_case(PRIOR, SANGSAENG), HOUSE, rs, on=date(2027, 6, 1), track=Track.REFORM)
    assert v.applies
    assert v.transfer_deadline == date(2028, 1, 31)
    assert within_transfer_deadline(v, date(2028, 1, 31)) is True
    assert within_transfer_deadline(v, date(2028, 2, 1)) is False


def test_2026년_안에_끝난_계약은_2027_12_31까지다(rs: RuleSet):
    """상세본 p.78 ➊ — '26.12.31. 이전 상생임대차계약 종료: '27.12.31."""
    prior = lease("2022-07-01", "2024-06-30", 5 * EOK, contracted="2022-05-10")
    early = lease("2024-07-01", "2026-06-30", 520_000_000, contracted="2024-05-15")
    v = assess(make_case(prior, early), HOUSE, rs, on=date(2027, 6, 1), track=Track.REFORM)
    assert v.applies
    assert v.transfer_deadline == date(2027, 12, 31)


def test_2029년을_넘는_기한은_2029_12_31로_잘린다(rs: RuleSet):
    """상세본 p.78 ➋ — "계약종료 후 1년이 되는 날과 '29.12.31. 중 **빠른 날**".

    ⚠️ 상생임대차계약은 **체결과 임대개시가 모두** '26.12.31. 안이어야 한다
       (상세본 p.78 ➋). 그래서 '27년에 개시하는 계약으로는 이 분기를 시험할 수 없다.
       개시는 '26년, 종료는 '29년인 긴 계약을 쓴다.
    """
    prior = lease("2024-06-01", "2026-11-30", 5 * EOK, contracted="2024-04-10")
    late = lease("2026-12-01", "2029-01-31", 520_000_000, contracted="2026-10-15")
    v = assess(make_case(prior, late), HOUSE, rs, on=date(2029, 6, 1), track=Track.REFORM)
    assert v.applies
    # 2029-01-31 + 1년 = 2030-01-31이지만 '29.12.31. 상한에서 잘린다
    assert v.transfer_deadline == date(2029, 12, 31)


def test_요건을_만족하는_쌍이_여럿이면_늦게_끝난_쪽을_든다(rs: RuleSet):
    """법은 어느 쌍을 쓰라고 정하지 않는다. 양도기한이 상생임대차계약 종료일에
    붙으므로 납세자는 늦게 끝난 계약을 든다.

    세 계약이 연달아 있으면 쌍이 둘 생긴다 — (A,B)와 (B,C). 둘 다 요건을 갖추면
    C가 걸린 쪽을 든다. ⚠️ B·C 모두 임대개시가 '26.12.31. 안이어야 한다.
    """
    a = lease("2022-01-01", "2023-12-31", 5 * EOK, contracted="2021-12-25")
    b = lease("2024-01-01", "2025-12-31", 520_000_000, contracted="2023-11-15")
    c = lease("2026-01-01", "2027-12-31", 540_000_000, contracted="2025-11-15")
    v = assess(make_case(a, b, c), HOUSE, rs, on=date(2029, 6, 1), track=Track.REFORM)
    assert v.applies
    assert v.lease is c
    assert v.prior is b
    assert v.transfer_deadline == date(2028, 12, 31)  # 종료 후 1년
