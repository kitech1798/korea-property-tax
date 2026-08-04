"""상담 지식 계층 테스트.

이 계층의 존재 이유는 "AI가 실시간으로 지어낸 조언"을 피하는 것이다.
그래서 스키마가 **강제하는 것**을 테스트로 못 박는다.

  · 근거 조문 없는 문장은 로딩 자체가 실패한다
  · 부작용(caveats) 없는 조언은 로딩 자체가 실패한다
  · 조건이 비면(아무에게나 뜨는 조언) 로딩 자체가 실패한다
  · 채우지 못한 자리표시자가 남으면 화면에 안 나간다
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction

import pytest
import yaml

from realestate_tax.advisory import (
    ALLOWED_KEYS,
    Advisory,
    AdvisoryError,
    Condition,
    advise,
    build_context,
    clear_cache,
    default_root,
    load,
    parse_advisory,
    select,
)
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
from realestate_tax.engine.jongbuse import JongbuseOptions, compute_jongbuse
from realestate_tax.rules import RuleSet, Track, default_ruleset_root

ME = PersonId("me")
SPOUSE = PersonId("spouse")
SEOUL = "1168010100"
BUSAN = "2635010300"


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


def valid_raw(**over) -> dict:
    base = {
        "id": "adv.test.sample",
        "title_ko": "표본 항목",
        "when": {"is_one_house": True, "age_min": 60},
        "fact_ko": "1세대1주택자는 기본공제 12억원을 적용받습니다.",
        "basis": ["종합부동산세법 §8①"],
        "advice_ko": "매년 9월 신고기간에 보유현황을 확인하세요.",
        "caveats_ko": ["부부공동명의는 별도 특례 신청이 필요합니다."],
        "severity": "fact",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# ★ 스키마가 강제하는 것 — 이게 이 계층의 핵심
# --------------------------------------------------------------------------


def test_근거_조문이_없으면_로딩이_실패한다():
    """'법률 기반 팩트'라는 주장을 스키마가 강제한다."""
    with pytest.raises(AdvisoryError, match="근거 조문"):
        parse_advisory(valid_raw(basis=[]))


def test_부작용이_없으면_로딩이_실패한다():
    """'부부공동명의로 바꾸세요'만 하고 증여세를 빼놓으면 조언이 아니라 함정이다."""
    with pytest.raises(AdvisoryError, match="caveats"):
        parse_advisory(valid_raw(caveats_ko=[]))


def test_조건이_비면_로딩이_실패한다():
    """아무에게나 뜨는 조언은 나머지 조언의 신뢰까지 깎는다."""
    with pytest.raises(AdvisoryError, match="when 조건"):
        parse_advisory(valid_raw(when={}))


def test_허용되지_않은_조건_키는_로딩이_실패한다():
    """오타 하나로 조건이 조용히 무시되면 아무에게나 조언이 뜬다."""
    with pytest.raises(AdvisoryError, match="허용되지 않은"):
        parse_advisory(valid_raw(when={"is_one_hose": True}))


def test_실행방법이_비면_로딩이_실패한다():
    with pytest.raises(AdvisoryError, match="advice_ko"):
        parse_advisory(valid_raw(advice_ko="   "))


def test_필수_항목이_없으면_어느_항목인지_알려준다():
    with pytest.raises(AdvisoryError, match="fact_ko"):
        parse_advisory({k: v for k, v in valid_raw().items() if k != "fact_ko"})


# --------------------------------------------------------------------------
# 조건 매칭
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "when, ctx, expected",
    [
        ({"is_one_house": True}, {"is_one_house": True}, True),
        ({"is_one_house": True}, {"is_one_house": False}, False),
        ({"age_min": 60}, {"age": 65}, True),
        ({"age_min": 60}, {"age": 59}, False),
        ({"year_max": 2027}, {"year": 2026}, True),
        ({"year_max": 2027}, {"year": 2028}, False),
        ({"track": "reform"}, {"track": "reform"}, True),
        ({"house_count": [2, 3]}, {"house_count": 3}, True),
        ({"house_count": [2, 3]}, {"house_count": 1}, False),
    ],
)
def test_조건_비교_규칙(when, ctx, expected):
    assert Condition(when).matches(ctx) is expected


def test_컨텍스트에_키가_없으면_매칭하지_않는다():
    """모르는 상태에서 조언을 띄우면 틀린 조언이 나간다."""
    assert not Condition({"has_inherited": True}).matches({})


def test_조건이_구체적인_항목이_먼저_나온다():
    """스무 개를 쏟아내면 하나도 안 읽는다. 상황에 딱 맞는 것부터 보여준다."""
    broad = parse_advisory(valid_raw(id="a.broad", when={"is_one_house": True}))
    narrow = parse_advisory(
        valid_raw(id="a.narrow", when={"is_one_house": True, "age_min": 60, "track": "reform"})
    )
    picked = select([broad, narrow], {"is_one_house": True, "age": 70, "track": "reform"})
    assert [a.id for a in picked] == ["a.narrow", "a.broad"]


def test_표시_개수를_제한한다():
    items = [
        parse_advisory(valid_raw(id=f"a.{i}", when={"is_one_house": True})) for i in range(20)
    ]
    assert len(select(items, {"is_one_house": True}, limit=5)) == 5


# --------------------------------------------------------------------------
# 자리표시자 — 숫자는 엔진이 넣는다
# --------------------------------------------------------------------------


def test_자리표시자를_엔진_값으로_채운다():
    adv = parse_advisory(
        valid_raw(advice_ko="올해 보유세는 {{보유세}}입니다. 신청하면 {{절감액}} 줄어듭니다.")
    )
    assert adv.placeholders == {"보유세", "절감액"}
    r = adv.render({"보유세": "916.3만원", "절감액": "45.4만원"})
    assert r.displayable
    assert "916.3만원" in r.advice_ko and "45.4만원" in r.advice_ko


def test_못_채운_자리표시자가_남으면_표시하지_않는다():
    """'{{절감액}} 절감됩니다'가 그대로 화면에 나가는 것보다 아예 안 보여주는 게 낫다."""
    adv = parse_advisory(valid_raw(advice_ko="{{절감액}} 줄어듭니다."))
    r = adv.render({})
    assert not r.displayable
    assert r.missing == ("절감액",)


def test_개편안_기반이면_국회_미통과가_따라붙는다():
    from realestate_tax.domain import LegalStatus

    adv = parse_advisory(
        valid_raw(uncertainty_ko="2026 개편안 기준입니다. 국회 통과 전입니다.")
    )
    assert adv.certainty.legal is LegalStatus.BILL_PENDING
    assert parse_advisory(valid_raw()).certainty.legal is LegalStatus.ENACTED


# --------------------------------------------------------------------------
# 컨텍스트는 엔진 판정에서만 나온다
# --------------------------------------------------------------------------


def one_house_case(year: int = 2026, price: int = 1_500_000_000) -> TaxCase:
    hh = HouseholdId("hh")
    p = Person(id=ME, household_id=hh, name="본인", birth_date=date(1960, 1, 1))
    prop = Property(
        id=PropertyId("h0"),
        kind=PropertyKind.APARTMENT,
        legal_dong_code=SEOUL,
        published_prices=(PriceFact(year, price),),
    )
    return TaxCase(
        year=year,
        persons=(p,),
        households=(Household(id=hh, member_ids=(ME,)),),
        properties=(prop,),
        ownerships=(Ownership(ME, prop.id, share=Fraction(1)),),
    )


def test_컨텍스트는_사용자_입력이_아니라_엔진_판정에서_나온다(rs: RuleSet):
    """'1세대1주택입니까?'를 물어서 조언을 고르면 판정을 떠넘긴 시중 계산기와 같아진다."""
    ctx = build_context(one_house_case(), ME, rs)
    assert ctx["is_one_house"] is True
    assert ctx["house_count"] == 1
    assert ctx["in_regulated_zone"] is True  # 강남구 — 코드로 판정된 결과
    assert ctx["age"] == 66
    assert ctx["price_total"] == 1_500_000_000


def test_다주택이면_컨텍스트가_달라진다(rs: RuleSet):
    hh = HouseholdId("hh")
    p = Person(id=ME, household_id=hh, birth_date=date(1990, 1, 1))
    props = tuple(
        Property(
            id=PropertyId(f"h{i}"),
            kind=PropertyKind.APARTMENT,
            legal_dong_code=dong,
            published_prices=(PriceFact(2026, 900_000_000),),
        )
        for i, dong in enumerate((SEOUL, BUSAN))
    )
    case = TaxCase(
        year=2026,
        persons=(p,),
        households=(Household(id=hh, member_ids=(ME,)),),
        properties=props,
        ownerships=tuple(Ownership(ME, pr.id) for pr in props),
    )
    ctx = build_context(case, ME, rs)
    assert ctx["house_count"] == 2
    assert ctx["is_one_house"] is False
    assert ctx["price_total"] == 1_800_000_000


def test_부부공동명의_요건_충족이_컨텍스트에_반영된다(rs: RuleSet):
    hh = HouseholdId("hh")
    a = Person(id=ME, household_id=hh, spouse_id=SPOUSE, birth_date=date(1960, 1, 1))
    b = Person(id=SPOUSE, household_id=hh, spouse_id=ME, birth_date=date(1960, 1, 1))
    prop = Property(
        id=PropertyId("h0"),
        kind=PropertyKind.APARTMENT,
        legal_dong_code=SEOUL,
        published_prices=(PriceFact(2026, 2_000_000_000),),
    )
    case = TaxCase(
        year=2026,
        persons=(a, b),
        households=(Household(id=hh, member_ids=(ME, SPOUSE)),),
        properties=(prop,),
        ownerships=(
            Ownership(ME, prop.id, share=Fraction(1, 2)),
            Ownership(SPOUSE, prop.id, share=Fraction(1, 2)),
        ),
    )
    assert build_context(case, ME, rs)["joint_spouse_eligible"] is True


# --------------------------------------------------------------------------
# 실제 지식베이스 — 있으면 전부 유효해야 한다
# --------------------------------------------------------------------------


def test_지식베이스가_있으면_전부_스키마를_통과한다():
    """에이전트가 생성한 항목이라도 스키마를 못 지키면 로딩에서 죽는다.
    이게 'AI가 지어낸 조언'을 막는 마지막 방어선이다."""
    clear_cache()
    items = load()  # 파일이 없으면 빈 튜플
    for a in items:
        assert a.basis, a.id
        assert a.caveats_ko, a.id
        assert a.when.raw, a.id
        assert set(a.when.raw) <= ALLOWED_KEYS, a.id


def test_지식베이스_id가_중복되지_않는다():
    clear_cache()
    ids = [a.id for a in load()]
    assert len(ids) == len(set(ids))


def test_지식베이스가_없어도_상담이_동작한다(rs: RuleSet):
    """지식이 아직 없어도 엔진은 그대로 돈다. 지식은 덧붙이는 층이지 필수 의존이 아니다."""
    clear_cache()
    case = one_house_case()
    result = compute_jongbuse(case, ME, rs, options=JongbuseOptions(holding_years=10))
    out = advise(case, ME, rs, result=result, root="없는경로_xyz")
    assert out == ()
