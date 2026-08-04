"""종합부동산세 주택 수 특례 · 합산배제 테스트.

★ 이 파일이 지키는 핵심 구분

    주택 수 제외 (§8④)  — 1세대1주택자 판정에서만 뺀다. **과세표준에는 합산된다.**
    합산배제     (§8②)  — 과세표준에서 빠진다. 세금이 안 붙는다.

섞으면 세액이 통째로 틀린다. 시중 계산기는 이 판정을 아예 하지 않고
"예외 규정이 있으니 관할 세무서 문의"로 회피하는데, 종부세를 실제로 내는 계층
상당수가 이 특례에 걸린다. 즉 면책으로 제외한 집합이 곧 타깃 사용자 집합이다.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction

import pytest

from realestate_tax.domain import (
    AcquisitionCause,
    DeterminationQuality,
    Election,
    ElectionKind,
    Household,
    HouseholdId,
    InheritedMeta,
    Ownership,
    Person,
    PersonId,
    PriceFact,
    Property,
    PropertyId,
    PropertyKind,
    ResidenceSpell,
    RentalRegistration,
    RentalType,
    TaxCase,
)
from realestate_tax.engine.jongbuse import JongbuseOptions, compute_jongbuse
from realestate_tax.engine.special_houses import SpecialKind, assess
from realestate_tax.rules import RuleSet, Track, default_ruleset_root

ME = PersonId("me")
SPOUSE = PersonId("spouse")
SEOUL = "1168010100"  # 서울 강남구 (수도권 · 조정대상지역)
BUSAN = "2635010300"  # 부산 해운대구 (수도권 밖 · 비규제)
CHUNGBUK = "4311010100"  # 충북 청주 상당구 (수도권 밖 · 비규제)


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


def house(pid: str, price: int, dong: str = SEOUL, year: int = 2026, **kw) -> Property:
    return Property(
        id=PropertyId(pid),
        kind=PropertyKind.APARTMENT,
        legal_dong_code=dong,
        display_name=pid,
        published_prices=(PriceFact(year, price),),
        **kw,
    )


def build(
    props: list[Property],
    owns: list[Ownership],
    *,
    year: int = 2026,
    elections: tuple[Election, ...] = (),
    resides_in: str | None = None,
) -> TaxCase:
    hh = HouseholdId("hh")
    p = Person(id=ME, household_id=hh, name="본인", birth_date=date(1960, 1, 1))
    residences = (
        (ResidenceSpell(ME, PropertyId(resides_in), start=date(year - 5, 1, 1)),)
        if resides_in
        else ()
    )
    return TaxCase(
        year=year,
        persons=(p,),
        households=(Household(id=hh, member_ids=(ME,)),),
        properties=tuple(props),
        ownerships=tuple(owns),
        elections=elections,
        residences=residences,
    )


# --------------------------------------------------------------------------
# 상속주택 — 세 요건 중 '하나만' 충족하면 된다
# --------------------------------------------------------------------------


def inherited_case(
    *,
    inheritance_date: date,
    share: Fraction = Fraction(1),
    inherited_value: int = 2_000_000_000,
    dong: str = SEOUL,
) -> TaxCase:
    return build(
        [house("본가", 1_500_000_000), house("상속집", 2_000_000_000, dong)],
        [
            Ownership(ME, PropertyId("본가")),
            Ownership(
                ME,
                PropertyId("상속집"),
                share=share,
                cause=AcquisitionCause.INHERITANCE,
                inherited=InheritedMeta(inheritance_date, share, inherited_value),
            ),
        ],
    )


def test_상속_5년_이내면_주택수에서_빠진다(rs: RuleSet):
    a = assess(inherited_case(inheritance_date=date(2024, 1, 1)), ME, rs)
    assert a.count == 1 and a.is_one_house
    assert a.specials[0].kind is SpecialKind.INHERITANCE
    assert "5년 미경과" in a.specials[0].reason_ko


def test_지분_40퍼센트_이하면_5년이_지나도_빠진다(rs: RuleSet):
    """세 요건은 OR다. 누적 조건으로 읽으면 대부분 탈락한다."""
    a = assess(
        inherited_case(inheritance_date=date(2010, 1, 1), share=Fraction(2, 5)), ME, rs
    )
    assert a.is_one_house
    assert "지분율" in a.specials[0].reason_ko


def test_수도권은_지분공시가_6억_이하면_빠진다(rs: RuleSet):
    a = assess(
        inherited_case(
            inheritance_date=date(2010, 1, 1), inherited_value=600_000_000, dong=SEOUL
        ),
        ME,
        rs,
    )
    assert a.is_one_house
    assert "수도권 기준" in a.specials[0].reason_ko


def test_수도권_밖은_기준이_3억으로_낮다(rs: RuleSet):
    """같은 4억이라도 수도권이면 빠지고 지방이면 안 빠진다."""
    capital = assess(
        inherited_case(
            inheritance_date=date(2010, 1, 1), inherited_value=400_000_000, dong=SEOUL
        ),
        ME,
        rs,
    )
    rural = assess(
        inherited_case(
            inheritance_date=date(2010, 1, 1), inherited_value=400_000_000, dong=BUSAN
        ),
        ME,
        rs,
    )
    assert capital.is_one_house
    assert not rural.is_one_house


def test_세_요건을_모두_벗어나면_주택수에_들어가고_사유가_남는다(rs: RuleSet):
    a = assess(
        inherited_case(
            inheritance_date=date(2010, 1, 1),
            share=Fraction(1),
            inherited_value=2_000_000_000,
        ),
        ME,
        rs,
    )
    assert a.count == 2 and not a.is_one_house
    missed = a.missed[0]
    assert missed.kind is SpecialKind.INHERITANCE
    assert "세 요건을 모두 벗어났다" in missed.reason_ko


def test_상속주택이_있어도_1세대1주택_공제를_받는다(rs: RuleSet):
    """이 특례가 없으면 상속받은 집 하나 때문에 기본공제가 12억 → 9억으로 떨어진다."""
    case = inherited_case(inheritance_date=date(2024, 1, 1))
    r = compute_jongbuse(case, ME, rs, options=JongbuseOptions(holding_years=10))
    assert r.trace.find("jb.06.basic_deduction").output.as_int() == 1_200_000_000
    assert r.tax_credit.as_int() > 0


def test_주택수에서_빠져도_과세표준에는_합산된다(rs: RuleSet):
    """§8④는 주택 수만 빼준다. 과세표준까지 빠지는 §8② 합산배제와 다르다.
    이걸 혼동하면 세액이 통째로 틀린다."""
    case = inherited_case(inheritance_date=date(2024, 1, 1))
    r = compute_jongbuse(case, ME, rs, options=JongbuseOptions(holding_years=10))
    assessed = r.trace.find("jb.05.assessed_value").output.as_int()
    assert assessed == 1_500_000_000 + 2_000_000_000


# --------------------------------------------------------------------------
# 일시적 2주택
# --------------------------------------------------------------------------


def temporary_case(
    *, new_acquired: date, year: int = 2026, new_dong: str = SEOUL
) -> TaxCase:
    return build(
        [house("종전집", 1_500_000_000, SEOUL, year), house("신규집", 1_200_000_000, new_dong, year)],
        [
            Ownership(ME, PropertyId("종전집"), acquired_on=date(2015, 3, 1)),
            Ownership(ME, PropertyId("신규집"), acquired_on=new_acquired),
        ],
        year=year,
    )


def test_신규주택_취득_후_특례기간_이내면_1세대1주택자다(rs: RuleSet):
    a = assess(temporary_case(new_acquired=date(2025, 6, 1)), ME, rs)
    assert a.is_one_house
    assert a.specials[0].kind is SpecialKind.TEMPORARY_TWO
    assert a.specials[0].property_id == PropertyId("신규집")


def test_특례기간이_지나면_2주택이_되고_사유가_남는다(rs: RuleSet):
    a = assess(temporary_case(new_acquired=date(2020, 1, 1)), ME, rs)
    assert a.count == 2
    assert any(
        m.kind is SpecialKind.TEMPORARY_TWO and "초과했다" in m.reason_ko
        for m in a.missed
    )


def test_취득일을_모르면_판정하지_않고_입력을_요청한다(rs: RuleSet):
    """유리한 쪽으로 가정하면 세액을 과소평가해 사용자를 오도한다."""
    case = build(
        [house("A", 1_000_000_000), house("B", 900_000_000)],
        [Ownership(ME, PropertyId("A")), Ownership(ME, PropertyId("B"))],
    )
    a = assess(case, ME, rs)
    assert a.count == 2
    assert any("취득일을 입력해주세요" in m.reason_ko and m.actionable for m in a.missed)


def test_개편안은_조정지역_간_이동만_3년에서_2년으로_줄인다(rs: RuleSet):
    """조정→조정만 2년이고 나머지 조합은 3년 그대로다.

    경과조치를 피하려면 2026.8.4. 이후 취득이어야 하므로, 2년이 지난 상태를 만들려면
    기준일을 2029년으로 잡아야 한다. 이 제약 자체가 경과조치가 실제로 작동한다는 뜻이다.
    """
    acquired = date(2026, 9, 1)  # 개편안 발표 후 → 경과조치 대상 아님
    on = date(2029, 6, 1)  # 2.75년 경과

    both_adjusted = assess(
        temporary_case(new_acquired=acquired, year=2029, new_dong=SEOUL),
        ME, rs, track=Track.REFORM, on=on,
    )
    to_non_adjusted = assess(
        temporary_case(new_acquired=acquired, year=2029, new_dong=BUSAN),
        ME, rs, track=Track.REFORM, on=on,
    )

    assert not both_adjusted.is_one_house  # 2년 초과 → 특례 상실
    assert to_non_adjusted.is_one_house  # 3년 이내 → 유지
    assert "조정대상지역 간 이동" in " ".join(m.reason_ko for m in both_adjusted.missed)


def test_개편안_발표_전_취득분은_경과조치로_3년을_유지한다(rs: RuleSet):
    """2026.8.3. 이전 취득 또는 매매계약·계약금 지급분은 종전 규정(3년)을 적용한다.
    이 경과조치를 빠뜨리면 이미 집을 산 사람들이 소급해서 특례를 잃는다."""
    a = assess(
        temporary_case(new_acquired=date(2026, 7, 1), year=2029, new_dong=SEOUL),
        ME, rs, track=Track.REFORM, on=date(2029, 6, 1),
    )
    assert a.is_one_house
    assert "3년" in a.specials[0].reason_ko


# --------------------------------------------------------------------------
# 지방 저가주택
# --------------------------------------------------------------------------


def rural_case(price: int, *, year: int = 2026) -> TaxCase:
    return build(
        [house("본가", 1_500_000_000, SEOUL, year), house("시골집", price, CHUNGBUK, year)],
        [Ownership(ME, PropertyId("본가")), Ownership(ME, PropertyId("시골집"))],
        year=year,
    )


def test_현행법은_신청해야_적용되므로_안내만_한다(rs: RuleSet):
    """가액 요건을 충족해도 신청 사실이 없으면 적용하지 않는다.
    다만 '신청하면 된다'는 것을 행동 가능한 안내로 남긴다."""
    a = assess(rural_case(300_000_000), ME, rs)
    assert a.count == 2
    missed = next(m for m in a.missed if m.kind is SpecialKind.RURAL_LOW_PRICE)
    assert missed.actionable
    assert "신청해야 적용된다" in missed.reason_ko


def test_개편안은_2027년부터_신청_없이_자동_적용된다(rs: RuleSet):
    """상세본 p.206 — 지방 저가주택은 납세자 신청 없이도 특례를 적용한다."""
    a = assess(rural_case(300_000_000, year=2027), ME, rs, on=date(2027, 6, 1))
    assert a.is_one_house
    assert a.specials[0].kind is SpecialKind.RURAL_LOW_PRICE


def test_가액_4억을_넘으면_지방저가주택이_아니다(rs: RuleSet):
    a = assess(rural_case(400_000_001, year=2027), ME, rs, on=date(2027, 6, 1))
    assert a.count == 2


def test_수도권_주택은_아무리_싸도_지방저가주택이_아니다(rs: RuleSet):
    case = build(
        [house("본가", 1_500_000_000, SEOUL), house("작은집", 200_000_000, SEOUL)],
        [Ownership(ME, PropertyId("본가")), Ownership(ME, PropertyId("작은집"))],
        year=2027,
    )
    a = assess(case, ME, rs, on=date(2027, 6, 1))
    assert not any(s.kind is SpecialKind.RURAL_LOW_PRICE for s in a.specials)


# --------------------------------------------------------------------------
# ★ 합산배제 임대주택 — 과세표준에서 빠진다
# --------------------------------------------------------------------------


def rental_case(
    *, declared: bool, within_cap: bool = True, resides_in_main: bool = True
) -> TaxCase:
    """★ 기본값이 "본가에 실거주"인 이유: 종부령 §2의3② 단서가
    합산배제 임대주택의 **주택 수 제외**를 '다른 주택에 실거주'로 한정한다.
    거주 사실이 없으면 임대주택은 주택 수에 그대로 남는다."""
    rental = RentalRegistration(
        rental_type=RentalType.BUILT_LONG_TERM,
        registered_on=date(2020, 1, 1),
        rent_increase_within_cap=within_cap,
    )
    elections = (
        (Election(ME, ElectionKind.RENTAL_EXCLUSION),) if declared else ()
    )
    return build(
        [house("본가", 1_500_000_000), house("임대집", 800_000_000, rental=rental)],
        [Ownership(ME, PropertyId("본가")), Ownership(ME, PropertyId("임대집"))],
        elections=elections,
        resides_in="본가" if resides_in_main else None,
    )


def test_합산배제는_과세표준에서_아예_빠진다(rs: RuleSet):
    """주택 수만 빼주는 다른 특례와 결정적으로 다르다."""
    case = rental_case(declared=True)
    a = assess(case, ME, rs)
    assert a.is_one_house
    assert a.specials[0].excluded_from_aggregate
    assert a.aggregate_excluded() == frozenset({PropertyId("임대집")})

    r = compute_jongbuse(case, ME, rs, options=JongbuseOptions(holding_years=10))
    # 임대집 8억이 합산에서 빠져 본가 15억만 잡힌다
    assert r.trace.find("jb.05.assessed_value").output.as_int() == 1_500_000_000


def test_신고_사실이_없으면_판정하지_않는다(rs: RuleSet):
    """임대유형·의무임대기간·가액요건은 등록증으로만 확인된다.
    사용자가 확인해주기 전에는 유리하게 가정하지 않는다."""
    a = assess(rental_case(declared=False), ME, rs)
    assert a.count == 2
    missed = next(m for m in a.missed if m.kind is SpecialKind.RENTAL_EXCLUSION)
    assert missed.undecidable and missed.actionable
    assert a.certainty.determination is DeterminationQuality.UNDECIDABLE


def test_임대료_5퍼센트_상한을_어기면_합산배제가_깨진다(rs: RuleSet):
    a = assess(rental_case(declared=True, within_cap=False), ME, rs)
    assert a.count == 2
    assert any("5%" in m.reason_ko for m in a.missed)


def test_합산배제_주택의_재산세는_종부세_계산에_들어가지_않는다(rs: RuleSet):
    """종부세 과세표준에 없는 주택이므로 그 재산세도 §9③ 공제·§10 상한의 대상이 아니다.
    재산세 자체는 지자체가 그대로 부과한다 — 안 내는 게 아니라 종부세 계산에서 빠질 뿐이다."""
    from realestate_tax.engine.property_tax import compute_property_tax

    case = rental_case(declared=True)
    rental_pt = compute_property_tax(
        case, PropertyId("임대집"), rs, track=Track.CURRENT, owner_id=ME
    )
    assert rental_pt.total.as_int() > 0  # 재산세는 부과된다

    r = compute_jongbuse(case, ME, rs, options=JongbuseOptions(holding_years=10))
    home_pt = compute_property_tax(
        case, PropertyId("본가"), rs, track=Track.CURRENT, owner_id=ME
    )
    assert r.property_tax_total.as_int() == home_pt.total.as_int()


def test_재산세와_종부세는_주택수_판정_기준이_다르다(rs: RuleSet):
    """지방세법 시행령 §110의2의 제외 목록에는 **등록임대주택이 없다.**
    그래서 종부세는 합산배제로 1주택이 되지만, 재산세는 여전히 2주택이다.

    두 세목의 판정을 한 함수로 뭉뚱그리면 여기서 조용히 틀린다."""
    case = rental_case(declared=True)

    jongbuse_view = assess(case, ME, rs)
    assert jongbuse_view.is_one_house  # 종부세: 합산배제로 1주택

    from realestate_tax.engine.determination import household_house_count

    property_tax_view = household_house_count(case, ME)
    assert property_tax_view.count == 2  # 재산세: 그대로 2주택
    assert not property_tax_view.is_one_house

    # 결과적으로 재산세 공정시장가액비율이 1주택 특례(45%)가 아니라 일반(60%)이 된다
    from realestate_tax.engine.property_tax import compute_property_tax

    trace = compute_property_tax(
        case, PropertyId("본가"), rs, track=Track.CURRENT, owner_id=ME
    ).trace
    assert "60%" in trace.find("pt.04.taxable_base_raw").substitution


# --------------------------------------------------------------------------
# 감사 추적
# --------------------------------------------------------------------------


def test_어떤_집이_왜_빠졌는지_추적에_남는다(rs: RuleSet):
    case = inherited_case(inheritance_date=date(2024, 1, 1))
    trace = compute_jongbuse(
        case, ME, rs, options=JongbuseOptions(holding_years=10)
    ).trace.find("jb.04.special_houses")

    assert "본가(산입)" in trace.substitution
    assert "상속집(제외" in trace.substitution
    assert "§8④" in trace.note_ko


def test_합산배제는_추적에_별도로_표시된다(rs: RuleSet):
    trace = compute_jongbuse(
        rental_case(declared=True), ME, rs, options=JongbuseOptions(holding_years=10)
    ).trace.find("jb.04.special_houses")
    assert "합산배제(과세표준에서 제외)" in trace.note_ko
    assert "임대집" in trace.note_ko


def test_적용되지_않은_특례가_행동_가능한_안내로_올라온다(rs: RuleSet):
    """'유의사항: 특례 미반영'이라는 정적 면책과 달리, 엔진이 판정한 결과다."""
    alts = {
        a.key: a
        for a in compute_jongbuse(
            rural_case(300_000_000), ME, rs, options=JongbuseOptions(holding_years=10)
        ).trace.all_alternatives()
    }
    key = next(k for k in alts if "rural_low_price" in k)
    assert alts[key].actionable
    assert "지방 저가주택 특례" in alts[key].label_ko


# --------------------------------------------------------------------------
# ★ 세율표 주택 수는 세대가 아니라 본인 기준 (2026-08-04 감사)
#   종부령 §4의3③ — "법 제9조제1항·제2항에 따라 … 적용해야 하는 주택 수"
# --------------------------------------------------------------------------


def test_부부가_각자_2채씩이면_세대는_4채여도_각자는_2주택_세율이다(rs: RuleSet):
    """§8④(1세대1주택자)는 세대를 말하고 §9(세율)는 납세의무자를 말한다.
    이 둘을 같은 값으로 쓰면 부부 각자에게 3주택 중과세율표가 붙는다."""
    from realestate_tax.engine.jongbuse import JongbuseOptions, compute_jongbuse

    hh = HouseholdId("hh")
    a = Person(id=ME, household_id=hh, spouse_id=SPOUSE, birth_date=date(1970, 1, 1))
    b = Person(id=SPOUSE, household_id=hh, spouse_id=ME, birth_date=date(1970, 1, 1))
    props = tuple(
        Property(
            id=PropertyId(f"h{i}"),
            kind=PropertyKind.APARTMENT,
            legal_dong_code=SEOUL,
            published_prices=(PriceFact(2026, 1_500_000_000),),
        )
        for i in range(4)
    )
    case = TaxCase(
        year=2026,
        persons=(a, b),
        households=(Household(id=hh, member_ids=(ME, SPOUSE)),),
        properties=props,
        ownerships=(
            Ownership(ME, props[0].id), Ownership(ME, props[1].id),
            Ownership(SPOUSE, props[2].id), Ownership(SPOUSE, props[3].id),
        ),
    )
    a_ = assess(case, ME, rs)
    assert a_.count == 4, "세대 주택 수"
    assert a_.personal_count == 2, "본인 주택 수"

    r = compute_jongbuse(case, ME, rs, options=JongbuseOptions())
    detail = r.trace.find("jb.08.gross_tax").branch
    assert "본인 주택 수 2채" in (detail.detail_ko or "")
    assert "세대 4채" in (detail.detail_ko or "")


def test_공동소유는_지분이_얼마든_각자_1주택으로_센다(rs: RuleSet):
    """종부령 §4의3③1 — "공동 소유자 각자가 그 주택을 소유한 것으로 본다"."""
    from fractions import Fraction as F

    hh = HouseholdId("hh")
    a = Person(id=ME, household_id=hh, spouse_id=SPOUSE, birth_date=date(1970, 1, 1))
    b = Person(id=SPOUSE, household_id=hh, spouse_id=ME, birth_date=date(1970, 1, 1))
    prop = Property(
        id=PropertyId("h0"), kind=PropertyKind.APARTMENT, legal_dong_code=SEOUL,
        published_prices=(PriceFact(2026, 2_000_000_000),),
    )
    case = TaxCase(
        year=2026, persons=(a, b),
        households=(Household(id=hh, member_ids=(ME, SPOUSE)),),
        properties=(prop,),
        ownerships=(
            Ownership(ME, prop.id, share=F(1, 10)),
            Ownership(SPOUSE, prop.id, share=F(9, 10)),
        ),
    )
    assert assess(case, ME, rs).personal_count == 1  # 지분 10%여도 1주택


def test_배우자_명의_주택은_본인_세율표에_들어가지_않는다(rs: RuleSet):
    hh = HouseholdId("hh")
    a = Person(id=ME, household_id=hh, spouse_id=SPOUSE, birth_date=date(1970, 1, 1))
    b = Person(id=SPOUSE, household_id=hh, spouse_id=ME, birth_date=date(1970, 1, 1))
    props = tuple(
        Property(id=PropertyId(f"h{i}"), kind=PropertyKind.APARTMENT,
                 legal_dong_code=SEOUL,
                 published_prices=(PriceFact(2026, 1_000_000_000),))
        for i in range(3)
    )
    case = TaxCase(
        year=2026, persons=(a, b),
        households=(Household(id=hh, member_ids=(ME, SPOUSE)),),
        properties=props,
        ownerships=(
            Ownership(ME, props[0].id),
            Ownership(SPOUSE, props[1].id), Ownership(SPOUSE, props[2].id),
        ),
    )
    a_ = assess(case, ME, rs)
    assert (a_.count, a_.personal_count) == (3, 1)


# --------------------------------------------------------------------------
# ★ 종부령 §2의3② 단서 — 합산배제 임대주택의 주택 수 제외는 조건부
#   "제1호는 각 호 외의 주택을 소유하는 자가 과세기준일 현재 그 주택에
#    주민등록이 되어 있고 실제로 거주하고 있는 경우에 한정하여 적용한다"
# --------------------------------------------------------------------------


def test_다른_집에_안_살면_임대주택이_주택수에서_안_빠진다(rs: RuleSet):
    """임대사업자는 이 엔진의 핵심 타깃인데, 바로 그 집단에서 조용히 틀렸다.
    거주 사실 없이 1세대1주택자로 판정하면 기본공제 12억 + 세액공제 80%가
    잘못 나가 세액이 6.5배 어긋난다."""
    away = assess(rental_case(declared=True, resides_in_main=False), ME, rs)
    assert not away.is_one_house, "거주 사실이 없는데 1세대1주택자가 됐다"
    assert away.count == 2

    lives = assess(rental_case(declared=True, resides_in_main=True), ME, rs)
    assert lives.is_one_house


def test_주택수에서_안_빠져도_합산배제는_유지된다(rs: RuleSet):
    """단서가 걸리는 것은 **주택 수 제외**뿐이다.
    과세표준 합산배제(§8②)는 그대로다 — 이 둘을 한 필드로 뭉치면 조문을 못 담는다."""
    a = assess(rental_case(declared=True, resides_in_main=False), ME, rs)
    rental = next(s for s in a.specials if s.kind is SpecialKind.RENTAL_EXCLUSION)
    assert rental.excluded_from_aggregate is True
    assert rental.excluded_from_count is False
    assert a.aggregate_excluded() == frozenset({PropertyId("임대집")})


def test_왜_안_빠졌는지_사유가_남는다(rs: RuleSet):
    a = assess(rental_case(declared=True, resides_in_main=False), ME, rs)
    reasons = " ".join(m.reason_ko for m in a.missed)
    assert "주택 수에서는 빠지지 않는다" in reasons
    assert "§2의3② 단서" in reasons


# --------------------------------------------------------------------------
# ★ 종부령 §2의3① — 제외하고 나니 0채면 제외가 과했다
# --------------------------------------------------------------------------


def test_상속주택만_보유하면_1세대1주택자다(rs: RuleSet):
    """제외 후 0채 → '1세대1주택자 아님'으로 흘러 기본공제 12억과 세액공제를
    통째로 잃었다. 조문은 "1주택만을 소유한 경우"라고 쓴다 —
    주택 수 제외는 다른 주택을 무시하는 장치이지 자기 자신을 없애는 장치가 아니다."""
    case = build(
        [house("상속집", 2_000_000_000)],
        [Ownership(
            ME, PropertyId("상속집"),
            acquired_on=date(2024, 1, 1),
            cause=AcquisitionCause.INHERITANCE,
            inherited=InheritedMeta(
                inheritance_date=date(2024, 1, 1),
                share=Fraction(1),
                inherited_value=2_000_000_000,
            ),
        )],
    )
    a = assess(case, ME, rs)
    assert a.count == 1
    assert a.is_one_house
    assert a.restored == PropertyId("상속집")


def test_임대주택만_보유해도_0채가_되지_않는다(rs: RuleSet):
    case = build(
        [house("임대집", 800_000_000, rental=RentalRegistration(
            rental_type=RentalType.BUILT_LONG_TERM,
            registered_on=date(2020, 1, 1),
            rent_increase_within_cap=True,
        ))],
        [Ownership(ME, PropertyId("임대집"))],
        elections=(Election(ME, ElectionKind.RENTAL_EXCLUSION),),
    )
    a = assess(case, ME, rs)
    assert a.count == 1


def test_여러_특례주택_중_공시가격이_가장_높은_집이_되돌아온다(rs: RuleSet):
    """되돌릴 집을 아무거나 고르면 세액이 달라진다. 기준을 명시해 고정한다."""
    def inherited(name: str, price: int) -> Ownership:
        return Ownership(
            ME, PropertyId(name),
            acquired_on=date(2024, 1, 1),
            cause=AcquisitionCause.INHERITANCE,
            inherited=InheritedMeta(
                inheritance_date=date(2024, 1, 1),
                share=Fraction(1),
                inherited_value=price,
            ),
        )

    case = build(
        [house("작은집", 500_000_000), house("큰집", 2_000_000_000)],
        [inherited("작은집", 500_000_000), inherited("큰집", 2_000_000_000)],
    )
    a = assess(case, ME, rs)
    assert a.restored == PropertyId("큰집")
