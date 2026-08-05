"""도메인 모델 — 사실(fact)만 담는다.

이 파일의 단 하나의 설계 원칙:

    **주택 수, 1세대1주택 여부, 조정대상지역은 여기에 없다.**

셋 다 입력이 아니라 판정의 *결과*다. 시중 계산기가 무너진 지점이 정확히 여기다.
`properties[]` 배열에 소유자 필드를 두지 않고 "주택 수 = 배열 길이"로 가정하면,
'부부가 각자 단독명의로 1채씩' 같은 흔한 상황을 **표현조차 할 수 없다**.
그건 필드 하나 추가로 못 고치는 데이터 모델의 결함이다.

종합부동산세는 **인별 과세 + 세대별 판정**이라는 이중 구조를 갖는다.
세액은 사람마다 따로 매기지만, 1세대1주택인지는 세대 전체를 봐야 안다.
그래서 Person과 Household를 각각 1급 엔티티로 두고, 소유는 Ownership이라는
별도의 관계로 표현한다.

수치 규약
  - 금액은 원 단위 `int`. float 금지(누적 오차가 공제 경계에서 세액을 뒤집는다).
  - 지분은 `Fraction`. 1/3을 0.33으로 쓰면 3인 공동명의 합이 1이 안 된다.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from fractions import Fraction
from typing import NewType

from .certainty import Certainty, InputQuality

# --------------------------------------------------------------------------
# 기본 타입
# --------------------------------------------------------------------------

PersonId = NewType("PersonId", str)
HouseholdId = NewType("HouseholdId", str)
PropertyId = NewType("PropertyId", str)

Won = int
"""금액. 원 단위 정수."""

TaxYear = int
"""과세연도. 보유세 과세기준일은 매년 6월 1일이다."""


def assessment_date(year: TaxYear) -> date:
    """과세기준일 — 재산세·종합부동산세 공통으로 매년 6월 1일.

    지방세법 §114, 종합부동산세법 §3. 이 날짜 하나가 소유·거주·연령 판정의
    기준시점이 되므로 상수로 흩뿌리지 않고 여기서만 만든다.
    """
    return date(year, 6, 1)


# --------------------------------------------------------------------------
# 열거형
# --------------------------------------------------------------------------


class PersonType(StrEnum):
    INDIVIDUAL = "individual"
    """개인. 기본공제·누진세율·세액공제·세부담상한 모두 적용."""

    CORPORATION = "corporation"
    """일반 법인. 기본공제 없음, 단일세율, 세부담상한 미적용."""

    CORPORATION_PROGRESSIVE = "corporation_progressive"
    """공익법인 등 — 법인이지만 개인과 같은 누진세율을 적용받는 부류."""


class PropertyKind(StrEnum):
    APARTMENT = "apartment"
    """공동주택(아파트·연립·다세대). 공동주택가격이 공시된다."""

    DETACHED = "detached"
    """단독주택. 개별주택가격이 공시된다."""

    MULTI_UNIT = "multi_unit"
    """다가구주택. 구분등기 여부에 따라 주택 수 산정이 달라진다."""

    OFFICETEL_RESIDENTIAL = "officetel_residential"
    """주거용 오피스텔. 재산세는 신고 용도에 따르고, 주택 수 산입이 쟁점."""

    LAND_COMPREHENSIVE = "land_comprehensive"
    """종합합산 과세대상 토지."""

    LAND_SEPARATE = "land_separate"
    """별도합산 과세대상 토지."""

    BUILDING_OTHER = "building_other"
    """주택 외 건축물."""

    @property
    def is_house(self) -> bool:
        return self in _HOUSE_KINDS


_HOUSE_KINDS = frozenset(
    {
        PropertyKind.APARTMENT,
        PropertyKind.DETACHED,
        PropertyKind.MULTI_UNIT,
        PropertyKind.OFFICETEL_RESIDENTIAL,
    }
)


class AcquisitionCause(StrEnum):
    PURCHASE = "purchase"
    INHERITANCE = "inheritance"
    GIFT = "gift"
    NEW_BUILD = "new_build"
    RECONSTRUCTION = "reconstruction"
    """재개발·재건축으로 취득한 신축주택. 공사기간의 1/2이 거주기간으로 인정된다."""
    OTHER = "other"


class ImputedResidenceReason(StrEnum):
    """비거주 기간을 거주 기간으로 인정받는 사유.

    2026 세제개편안이 보유공제를 거주공제로 바꾸면서 신설한 완충장치다
    (종합부동산세법 §9⑩ 신설안). 이게 없으면 전근·유학 간 사람이 전부 벌을 받는다.
    """

    SCHOOLING = "schooling"
    """취학(고등학교·대학교)."""
    JOB_TRANSFER = "job_transfer"
    """직장 변경·전근 등 근무상 형편."""
    ILLNESS = "illness"
    """1년 이상 치료·요양이 필요한 질병."""
    SCHOOL_VIOLENCE = "school_violence"
    """학교폭력 피해로 인한 전학."""
    OVERSEAS = "overseas"
    """취학·근무상 출국."""
    ELDER_CARE = "elder_care"
    """60세 이상 직계존속 동거봉양 합가."""
    RECONSTRUCTION = "reconstruction"
    """재개발·재건축 공사기간(인가일~입주가능일)의 1/2."""
    OTHER = "other"


class ElectionKind(StrEnum):
    """납세자의 '선택'. 사실이 아니라 의사표시이므로 사실과 분리해 담는다."""

    JOINT_SPOUSE_SPECIAL = "joint_spouse_special"
    """부부공동명의 1주택자의 1세대1주택 특례 신청(종부세법 §10의2).
    신청하면 1인이 14억/9억 공제, 미신청이면 각자 9억/4억. 어느 쪽이 유리한지는
    세액공제까지 포함한 완전 계산 2회를 해봐야만 안다."""

    RENTAL_EXCLUSION = "rental_exclusion"
    """합산배제 임대주택 신고."""

    DEFERRAL = "deferral"
    """납부유예 신청."""


class RentalType(StrEnum):
    BUILT_PUBLIC_SUPPORT = "built_public_support"
    """공공지원민간임대(건설)."""
    BUILT_LONG_TERM = "built_long_term"
    """장기일반민간임대(건설)."""
    PURCHASED_PUBLIC_SUPPORT = "purchased_public_support"
    """공공지원민간임대(매입)."""
    PURCHASED_LONG_TERM = "purchased_long_term"
    """장기일반민간임대(매입). 조정대상지역 아파트는 개편안에서 단계적으로 배제된다."""
    SHORT_TERM = "short_term"
    """단기임대(4년). 자동 등록말소 대상."""


# --------------------------------------------------------------------------
# 값 객체
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PriceFact:
    """특정 연도의 공시가격 한 건.

    공시가격을 단일 값이 아니라 **연도별 시계열**로 두는 이유:
    세부담상한(직전연도 총세액 × 상한율)을 계산하려면 전년도 세액이 필요하고,
    사용자는 그걸 모르므로 전년도 룰셋으로 역산해야 한다. 역산하려면 전년도
    공시가격이 있어야 한다. 단일 값으로 시작하면 이 지점에서 구조가 붕괴한다.
    """

    year: TaxYear
    value: Won
    quality: InputQuality = InputQuality.USER_INPUT
    note: str = ""

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError(f"공시가격은 음수일 수 없다: {self.value}")

    @property
    def certainty(self) -> Certainty:
        return Certainty(input=self.quality)


@dataclass(frozen=True, slots=True)
class InheritedMeta:
    """상속주택 특례 판정에 필요한 사실.

    상속주택은 일정 요건 아래 주택 수에서 제외된다. 요건(지분율·가액·경과기간)은
    시행령 소관이라 룰셋 데이터로 두고, 여기서는 판정에 필요한 사실만 받는다.
    """

    inheritance_date: date
    share: Fraction
    """상속받은 지분율."""
    inherited_value: Won
    """상속받은 지분에 해당하는 공시가격."""


@dataclass(frozen=True, slots=True)
class RentalRegistration:
    """등록임대주택 정보. 합산배제 판정용."""

    rental_type: RentalType
    registered_on: date
    obligation_end: date | None = None
    """임대의무기간 종료일. 자동 등록말소 시점 판정에 쓰인다."""
    rent_increase_within_cap: bool = True
    """임대료 증액 5% 이내 준수 여부. 위반하면 합산배제가 깨진다."""


# --------------------------------------------------------------------------
# 엔티티
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Person:
    """납세의무자. 개인 또는 법인."""

    id: PersonId
    type: PersonType = PersonType.INDIVIDUAL
    name: str = ""
    birth_date: date | None = None
    """연령별 세액공제(60/65/70세) 판정용. 법인은 None."""
    household_id: HouseholdId | None = None
    """법인은 세대에 속하지 않으므로 None."""
    spouse_id: PersonId | None = None
    is_resident: bool = True
    """거주자 여부(소득세법상). 비거주자는 일부 특례에서 배제된다."""

    def age_at(self, on: date) -> int | None:
        """만 나이. 생년월일이 없으면 None을 돌려 판정 불가로 흘려보낸다.

        2월 29일생은 평년에 해당일이 없다. 민법 §160③에 따라 **그 월의 말일**(2/28)에
        나이를 먹는다. 이 처리를 빼면 윤년생만 하루씩 밀려 60/65/70세 세액공제
        구간이 어긋난다 — 보유기간에서 같은 버그를 실측했다(SIM-08).
        """
        if self.birth_date is None:
            return None
        years = on.year - self.birth_date.year
        birthday = min(self.birth_date.day, monthrange(on.year, self.birth_date.month)[1])
        if (on.month, on.day) < (self.birth_date.month, birthday):
            years -= 1
        return years

    @property
    def is_corporation(self) -> bool:
        return self.type in (PersonType.CORPORATION, PersonType.CORPORATION_PROGRESSIVE)


@dataclass(frozen=True, slots=True)
class Household:
    """1세대. 세법상 '세대'는 진술이자 판정 대상이다.

    사용자가 "우리는 한 세대다"라고 말해도, 배우자 별도 등록·30세 미만 자녀의
    소득 유무 등에 따라 법적 판정이 달라진다. 그래서 진술을 그대로 믿지 않고
    판정 엔진의 입력으로 넘긴다.
    """

    id: HouseholdId
    member_ids: tuple[PersonId, ...] = ()
    spouse_separately_registered: bool = False
    """배우자가 주민등록상 별도 세대인 경우. 그래도 세법상 같은 세대로 보는 것이 원칙."""
    independent_members: tuple[PersonId, ...] = ()
    """30세 미만이지만 독립 생계가 인정되어 별도 세대로 볼 여지가 있는 구성원."""


@dataclass(frozen=True, slots=True)
class Property:
    """과세 물건.

    주소를 문자열로 담지 않는다. 조정대상지역 판정을 `주소.startswith("서울")`로
    하는 것이 시중 계산기가 신뢰를 잃은 대표적 이유다. 법정동코드만 받고,
    지역 판정은 고시 이력 테이블 조회로만 한다.
    """

    id: PropertyId
    kind: PropertyKind
    legal_dong_code: str
    """행정표준 법정동코드 10자리. 조정대상지역·수도권·인구감소지역 판정의 유일한 키."""
    published_prices: tuple[PriceFact, ...] = ()
    """연도별 공시가격. 연도 중복은 __post_init__에서 막는다."""
    display_name: str = ""
    """화면 표시용 별칭. 계산에 쓰지 않는다."""
    area_m2: float | None = None
    rental: RentalRegistration | None = None
    is_company_housing: bool = False
    """사원용 주택. 합산배제 대상."""
    is_unsold_new: bool = False
    """미분양주택."""
    in_urban_planning_area: bool = True
    """도시지역 안 여부. 재산세 도시지역분(과세표준 × 0.14%) 부과 여부를 가른다.
    시중 계산기는 이걸 묻지 않고 전국에 일률 부과한다."""

    def __post_init__(self) -> None:
        years = [p.year for p in self.published_prices]
        if len(years) != len(set(years)):
            raise ValueError(f"공시가격 연도가 중복됐다: {years}")

    def price_for(self, year: TaxYear) -> PriceFact | None:
        for p in self.published_prices:
            if p.year == year:
                return p
        return None

    @property
    def is_house(self) -> bool:
        return self.kind.is_house


@dataclass(frozen=True, slots=True)
class Ownership:
    """사람과 물건을 잇는 소유 관계. 이 엔티티가 있어서
    '부부 각자 단독명의 1채씩'과 '부부공동명의 1채'가 구분된다.
    """

    person_id: PersonId
    property_id: PropertyId
    share: Fraction = Fraction(1)
    acquired_on: date | None = None
    cause: AcquisitionCause = AcquisitionCause.PURCHASE
    inherited: InheritedMeta | None = None

    def __post_init__(self) -> None:
        if not (0 < self.share <= 1):
            raise ValueError(f"지분은 0 초과 1 이하여야 한다: {self.share}")
        if self.cause is AcquisitionCause.INHERITANCE and self.inherited is None:
            raise ValueError("상속 취득이면 InheritedMeta가 있어야 특례 판정이 가능하다")

    def holding_days(self, on: date) -> int | None:
        if self.acquired_on is None:
            return None
        return (on - self.acquired_on).days


@dataclass(frozen=True, slots=True)
class ResidenceSpell:
    """거주 구간 한 토막.

    2026 개편안의 핵심이 보유공제 → 거주공제 전환이므로, 이 엔티티의 정확도가
    곧 세액의 정확도다. 한 집에 살다 나갔다 다시 들어온 경우를 표현해야 해서
    단일 기간이 아니라 구간의 나열로 둔다.
    """

    person_id: PersonId
    property_id: PropertyId
    start: date
    end: date | None = None
    """None이면 현재까지 계속 거주 중."""
    imputed_reason: ImputedResidenceReason | None = None
    """실제로는 살지 않았지만 거주로 인정받는 구간이면 그 사유."""

    def __post_init__(self) -> None:
        if self.end is not None and self.end < self.start:
            raise ValueError(f"거주 종료일이 시작일보다 빠르다: {self.start} ~ {self.end}")

    def days_until(self, on: date) -> int:
        """기준일까지의 거주 일수."""
        finish = min(self.end, on) if self.end is not None else on
        return max(0, (finish - self.start).days)

    def covers(self, on: date) -> bool:
        return self.start <= on and (self.end is None or on <= self.end)


@dataclass(frozen=True, slots=True)
class Election:
    """납세자의 선택. 사실과 섞지 않는다."""

    person_id: PersonId
    kind: ElectionKind
    year: TaxYear | None = None
    """None이면 모든 연도에 적용."""
    designated_taxpayer: PersonId | None = None
    """부부공동명의 특례에서 납세의무자로 지정된 사람."""
    auto_optimize: bool = True
    """True면 엔진이 신청/미신청 양쪽을 완전 계산해 유리한 쪽을 고른다."""


# --------------------------------------------------------------------------
# 계산 단위
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaxCase:
    """계산 한 건. 이것만 있으면 결과가 재현된다.

    frozen + 해시 가능하게 유지하는 이유는 취향이 아니다.
    부부공동명의 특례의 '유리한 쪽'을 고르려면 같은 사건을 조건만 바꿔
    두 번 계산해야 하고, 연도별 타임라인은 4번, 현행/개편안 비교는 8번 돌린다.
    파이프라인이 순수해야 이게 공짜가 된다.
    """

    year: TaxYear
    persons: tuple[Person, ...] = ()
    households: tuple[Household, ...] = ()
    properties: tuple[Property, ...] = ()
    ownerships: tuple[Ownership, ...] = ()
    residences: tuple[ResidenceSpell, ...] = ()
    elections: tuple[Election, ...] = ()
    prior_year_total_tax: Won | None = None
    """직전연도 총 보유세(재산세+종부세) 실납부액. 세부담상한 계산의 입력.
    모르면 None으로 두고 전년도 룰셋으로 역산한다."""

    def __post_init__(self) -> None:
        self._require_unique("persons", [p.id for p in self.persons])
        self._require_unique("properties", [p.id for p in self.properties])
        self._require_unique("households", [h.id for h in self.households])

        person_ids = {p.id for p in self.persons}
        property_ids = {p.id for p in self.properties}
        shares: dict[PropertyId, Fraction] = {}
        for o in self.ownerships:
            if o.person_id not in person_ids:
                raise ValueError(f"소유자가 persons에 없다: {o.person_id}")
            if o.property_id not in property_ids:
                raise ValueError(f"물건이 properties에 없다: {o.property_id}")
            shares[o.property_id] = shares.get(o.property_id, Fraction(0)) + o.share

        # ★ 지분 합이 1을 넘으면 **존재할 수 없는 소유 관계**다(2026-08-05 시뮬레이션).
        #   행마다 0<지분≤1만 검사하고 합을 안 봐서, 세 사람이 각 1/2씩 가진 사건이
        #   조용히 통과해 세액까지 산출됐다. 데이터 오류가 숫자로 흘러들면
        #   사용자는 그 숫자를 믿는다.
        #
        #   합이 1보다 **작은 것은 막지 않는다.** 세대 밖 공동소유자(친척·타인)를
        #   사건에 적지 않는 것은 정상이고, 그 경우 합은 당연히 1에 못 미친다.
        for pid, total in shares.items():
            if total > 1:
                raise ValueError(
                    f"'{pid}'의 지분 합이 1을 넘는다: {total} "
                    f"(소유 행 {sum(1 for o in self.ownerships if o.property_id == pid)}개). "
                    "한 물건의 지분 총합은 1을 초과할 수 없다."
                )

    @staticmethod
    def _require_unique(label: str, ids: list[str]) -> None:
        if len(ids) != len(set(ids)):
            raise ValueError(f"{label}의 id가 중복됐다: {ids}")

    # -- 조회 헬퍼. 엔진 곳곳에서 반복될 탐색을 한곳에 모은다 --------------

    @property
    def assessment_date(self) -> date:
        return assessment_date(self.year)

    # `property`라는 이름은 내장 데코레이터를 가려 클래스 본문을 망가뜨리므로 쓰지 않는다.
    def find_person(self, pid: PersonId) -> Person:
        for p in self.persons:
            if p.id == pid:
                return p
        raise KeyError(pid)

    def find_property(self, pid: PropertyId) -> Property:
        for p in self.properties:
            if p.id == pid:
                return p
        raise KeyError(pid)

    def ownerships_of(self, pid: PersonId) -> tuple[Ownership, ...]:
        return tuple(o for o in self.ownerships if o.person_id == pid)

    def owners_of(self, pid: PropertyId) -> tuple[Ownership, ...]:
        return tuple(o for o in self.ownerships if o.property_id == pid)

    def household_member_ids(self, pid: PersonId) -> tuple[PersonId, ...]:
        """같은 세대에 속한 사람들(본인 포함). 세대 정보가 없으면 본인만."""
        person = self.find_person(pid)
        if person.household_id is None:
            return (pid,)
        return tuple(
            p.id for p in self.persons if p.household_id == person.household_id
        )

    def residences_of(self, pid: PersonId, prop_id: PropertyId) -> tuple[ResidenceSpell, ...]:
        return tuple(
            r for r in self.residences if r.person_id == pid and r.property_id == prop_id
        )

    def election(self, pid: PersonId, kind: ElectionKind) -> Election | None:
        for e in self.elections:
            if e.person_id == pid and e.kind is kind and e.year in (None, self.year):
                return e
        return None
