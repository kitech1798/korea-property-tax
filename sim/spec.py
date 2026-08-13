"""시나리오 명세 — 상황 한 건을 **데이터로** 진술한다.

왜 Python이 아니라 YAML인가.

  1. 시나리오를 만드는 주체가 AI 에이전트다. 에이전트에게 코드를 쓰게 하면
     실패가 두 종류로 섞인다 — **엔진의 결함**과 **시나리오 작성자의 문법 오류**.
     둘을 구분하지 못하면 감사 자체가 신뢰를 잃는다. 데이터는 스키마로 검증되므로
     잘못 쓴 시나리오는 "엔진 버그"가 아니라 "명세 오류"로 즉시 분리된다.
  2. 실패한 시나리오가 **그대로 회귀 픽스처**가 된다. 고친 뒤 파일을 지우지 않고
     남겨두면 다음 회차에 다시 돌아간다. 코드였다면 테스트로 옮겨 적어야 한다.
  3. diff가 읽힌다. 시나리오 60건이 늘어날 때 무엇이 새로 들어왔는지 보인다.

이 파일은 **사실만** 받는다. 주택 수·1세대1주택·조정대상지역은 여기 없다 —
도메인 모델과 같은 원칙이다. 시나리오 작성자가 "이 사람은 2주택자"라고 쓰면
판정을 검증할 수 없게 된다. 사실을 쓰게 하고 판정은 엔진이 하게 둔다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from realestate_tax.domain.models import (
    AcquisitionCause,
    Election,
    ElectionKind,
    Household,
    HouseholdId,
    ImputedResidenceReason,
    InheritedMeta,
    LeaseOrigin,
    LeaseSpell,
    Ownership,
    Person,
    PersonId,
    PersonType,
    PriceFact,
    Property,
    PropertyId,
    PropertyKind,
    RentalRegistration,
    RentalType,
    ResidenceSpell,
    TaxCase,
    Won,
)
from realestate_tax.domain.certainty import InputQuality
from realestate_tax.engine.transfer_tax import BurdenGift, TransferEvent


class SpecError(ValueError):
    """시나리오 명세가 잘못됐다. **엔진 버그가 아니다** — 이 구분이 감사의 전부다."""


# --------------------------------------------------------------------------
# 파싱 헬퍼 — 관대하게 받되, 모호하면 거부한다
# --------------------------------------------------------------------------


def _as_date(raw: Any, where: str) -> date:
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw.strip())
        except ValueError as exc:  # noqa: PERF203
            raise SpecError(f"{where}: 날짜를 읽을 수 없다 {raw!r}") from exc
    raise SpecError(f"{where}: 날짜여야 한다 {raw!r}")


def _as_opt_date(raw: Any, where: str) -> date | None:
    return None if raw is None else _as_date(raw, where)


def _as_won(raw: Any, where: str) -> Won:
    """금액. YAML의 `1_500_000_000`, `"15억"` 둘 다 받는다.

    문자열 표기는 사용자 입력 파서를 **그대로 재사용**한다. 시나리오 전용 파서를
    따로 두면 둘이 갈라지고, 그러면 시나리오가 통과해도 실제 앱은 못 읽는 값이 생긴다.
    """
    if isinstance(raw, bool):
        raise SpecError(f"{where}: 금액 자리에 불리언 {raw!r}")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        from realestate_tax.intake.price import PriceParseError, parse_won

        try:
            return parse_won(raw)
        except PriceParseError as exc:
            raise SpecError(f"{where}: {exc}") from exc
    raise SpecError(f"{where}: 금액을 읽을 수 없다 {raw!r}")


def _as_fraction(raw: Any, where: str) -> Fraction:
    """지분. `"1/3"`, `0.5`, `1` 모두 받되 float은 **정확히 떨어질 때만**.

    0.33을 조용히 받으면 3인 공동명의 합이 0.99가 되고, 그 1%가 어디서 증발했는지
    아무도 못 찾는다. 도메인 모델이 Fraction을 쓰는 이유를 입구에서 지킨다.
    """
    if isinstance(raw, Fraction):
        return raw
    if isinstance(raw, int):
        return Fraction(raw)
    if isinstance(raw, str):
        try:
            return Fraction(raw.strip())
        except (ValueError, ZeroDivisionError) as exc:
            raise SpecError(f"{where}: 지분을 읽을 수 없다 {raw!r}") from exc
    if isinstance(raw, float):
        exact = Fraction(raw).limit_denominator(1000)
        if abs(float(exact) - raw) > 1e-9:
            raise SpecError(
                f"{where}: 소수 지분 {raw!r}는 정확히 표현되지 않는다. "
                f'"1/3" 처럼 분수로 써주세요.'
            )
        return exact
    raise SpecError(f"{where}: 지분을 읽을 수 없다 {raw!r}")


def _enum(cls: Any, raw: Any, where: str, default: Any = None) -> Any:
    if raw is None:
        if default is None:
            raise SpecError(f"{where}: 값이 필요하다")
        return default
    try:
        return cls(str(raw).strip())
    except ValueError as exc:
        allowed = ", ".join(m.value for m in cls)
        raise SpecError(f"{where}: {raw!r}는 없는 값이다. 가능: {allowed}") from exc


def _require(mapping: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in mapping or mapping[key] is None:
        raise SpecError(f"{where}: '{key}'가 없다")
    return mapping[key]


def _reject_unknown(mapping: Mapping[str, Any], allowed: set[str], where: str) -> None:
    """모르는 키를 **조용히 무시하지 않는다.**

    ★ 이 검사가 없으면 최악의 실패가 난다. 에이전트가 `acquired_on:`이라 써야 할 것을
      `acquired:`라 쓰면, 취득일 없이 계산이 돌아 **결과가 그럴듯하게 나온다.**
      그러면 "취득일을 무시하는 엔진 버그"를 보고하게 되는데 엔진은 멀쩡하다.
      무시된 오타 하나가 감사 리포트 전체를 오염시킨다.
    """
    unknown = set(mapping) - allowed
    if unknown:
        raise SpecError(
            f"{where}: 모르는 항목 {sorted(unknown)} — 오타이거나 지원하지 않는 필드다. "
            f"가능: {sorted(allowed)}"
        )


# --------------------------------------------------------------------------
# 명세
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TransferSpec:
    event: TransferEvent | None = None
    burden_gift: BurdenGift | None = None


@dataclass(frozen=True, slots=True)
class Scenario:
    """상황 한 건.

    `subject`(누구의 세금인가)를 명시적으로 받는 이유: 종부세는 **인별 과세**다.
    부부 사건에서 누구를 기준으로 보는지 정하지 않으면 결과를 해석할 수 없다.
    """

    id: str
    label_ko: str
    case: TaxCase
    subject: PersonId
    source_path: Path | None = None

    origin: str = ""
    """이 시나리오를 만든 주체(에이전트 이름 등). 어느 앵글이 버그를 잘 잡는지 본다."""
    intent_ko: str = ""
    """무엇을 노린 시나리오인가. 실패 리포트에서 '의도 대비 결과'를 읽게 해준다."""
    expectation_ko: str = ""
    """작성자가 예상한 결과. **숫자를 쓰지 않는다** — LLM이 만든 숫자를 정답으로
    삼으면 환각을 정답표로 승격시키는 꼴이다. 방향과 성질만 서술한다."""

    tracks: tuple[str, ...] = ("current", "reform")
    years: tuple[int, ...] = ()
    """타임라인으로 돌려볼 연도. 비면 case.year 한 해만."""
    growth: float = 0.0
    """미래 연도 공시가격 상승률 시나리오."""

    prior_year_total_tax: Won | None = None
    resides_in_main_house: bool | None = None
    joint_spouse_election: bool | None = None
    """None이면 신청/미신청을 **둘 다** 계산해 비교한다."""

    transfer: TransferSpec = field(default_factory=TransferSpec)

    tags: tuple[str, ...] = ()


_SCENARIO_KEYS = {
    "id", "label", "origin", "intent", "expectation", "year", "years", "growth",
    "tracks", "subject", "persons", "households", "properties", "ownerships",
    "residences", "leases", "elections", "prior_year_total_tax", "resides_in_main_house",
    "joint_spouse_election", "transfer", "burden_gift", "tags",
}
_PERSON_KEYS = {"id", "type", "name", "birth", "household", "spouse", "is_resident"}
_HOUSEHOLD_KEYS = {"id", "members", "spouse_separately_registered", "independent_members"}
_PROPERTY_KEYS = {
    "id", "kind", "dong", "prices", "name", "area_m2", "rental", "company_housing",
    "unsold_new", "urban",
}
_OWNERSHIP_KEYS = {"person", "property", "share", "acquired", "cause", "inherited"}
_RESIDENCE_KEYS = {"person", "property", "start", "end", "imputed_reason"}
_LEASE_KEYS = {
    "property", "start", "end", "origin", "contracted", "deposit", "rent",
    "down_payment_evidenced", "vacated", "ended_by_tenant_circumstance", "tenant",
}
_ELECTION_KEYS = {"person", "kind", "year", "designated_taxpayer", "auto_optimize"}
_RENTAL_KEYS = {"type", "registered_on", "obligation_end", "rent_increase_within_cap"}
_INHERITED_KEYS = {"date", "share", "value"}
_TRANSFER_KEYS = {
    "property", "person", "date", "price", "acquisition_price", "acquisition_date",
    "necessary_expense", "share", "holding_years", "residence_years",
}
_BURDEN_KEYS = {
    "property", "person", "date", "appraised_value", "gift_value", "debt_assumed",
    "acquisition_price", "necessary_expense", "holding_years", "residence_years",
}


def parse(raw: Mapping[str, Any], *, source: Path | None = None) -> Scenario:
    """YAML 한 덩이 → Scenario. 잘못된 명세는 여기서 전부 죽는다."""
    if not isinstance(raw, Mapping):
        raise SpecError(f"{source}: 최상위가 매핑이 아니다")
    _reject_unknown(raw, _SCENARIO_KEYS, "시나리오")

    sid = str(_require(raw, "id", "시나리오"))
    where = f"[{sid}]"
    year = int(_require(raw, "year", where))

    persons = tuple(_person(p, f"{where}.persons[{i}]") for i, p in enumerate(raw.get("persons") or ()))
    if not persons:
        raise SpecError(f"{where}: persons가 비었다")

    households = tuple(
        _household(h, f"{where}.households[{i}]") for i, h in enumerate(raw.get("households") or ())
    )
    properties = tuple(
        _property(p, year, f"{where}.properties[{i}]")
        for i, p in enumerate(raw.get("properties") or ())
    )
    ownerships = tuple(
        _ownership(o, f"{where}.ownerships[{i}]") for i, o in enumerate(raw.get("ownerships") or ())
    )
    residences = tuple(
        _residence(r, f"{where}.residences[{i}]") for i, r in enumerate(raw.get("residences") or ())
    )
    leases = tuple(
        _lease(l, f"{where}.leases[{i}]") for i, l in enumerate(raw.get("leases") or ())
    )
    elections = tuple(
        _election(e, f"{where}.elections[{i}]") for i, e in enumerate(raw.get("elections") or ())
    )

    prior = raw.get("prior_year_total_tax")
    prior_won = None if prior is None else _as_won(prior, f"{where}.prior_year_total_tax")

    try:
        case = TaxCase(
            year=year,
            persons=persons,
            households=households,
            properties=properties,
            ownerships=ownerships,
            residences=residences,
            leases=leases,
            elections=elections,
            prior_year_total_tax=prior_won,
        )
    except ValueError as exc:
        # 도메인 모델의 무결성 검사가 잡은 것 = 명세 오류다. 엔진 버그와 섞지 않는다.
        raise SpecError(f"{where}: {exc}") from exc

    subject = PersonId(str(raw.get("subject") or persons[0].id))
    if subject not in {p.id for p in persons}:
        raise SpecError(f"{where}: subject '{subject}'가 persons에 없다")

    years = tuple(int(y) for y in (raw.get("years") or ()))
    tracks = tuple(str(t) for t in (raw.get("tracks") or ("current", "reform")))
    for t in tracks:
        if t not in ("current", "reform"):
            raise SpecError(f"{where}: 모르는 트랙 {t!r}")

    return Scenario(
        id=sid,
        label_ko=str(raw.get("label") or sid),
        case=case,
        subject=subject,
        source_path=source,
        origin=str(raw.get("origin") or ""),
        intent_ko=str(raw.get("intent") or ""),
        expectation_ko=str(raw.get("expectation") or ""),
        tracks=tracks,
        years=years,
        growth=float(raw.get("growth") or 0.0),
        prior_year_total_tax=prior_won,
        resides_in_main_house=raw.get("resides_in_main_house"),
        joint_spouse_election=raw.get("joint_spouse_election"),
        transfer=_transfer(raw, where),
        tags=tuple(str(t) for t in (raw.get("tags") or ())),
    )


def _person(raw: Mapping[str, Any], where: str) -> Person:
    _reject_unknown(raw, _PERSON_KEYS, where)
    return Person(
        id=PersonId(str(_require(raw, "id", where))),
        type=_enum(PersonType, raw.get("type"), f"{where}.type", PersonType.INDIVIDUAL),
        name=str(raw.get("name") or ""),
        birth_date=_as_opt_date(raw.get("birth"), f"{where}.birth"),
        household_id=HouseholdId(str(raw["household"])) if raw.get("household") else None,
        spouse_id=PersonId(str(raw["spouse"])) if raw.get("spouse") else None,
        is_resident=bool(raw.get("is_resident", True)),
    )


def _household(raw: Mapping[str, Any], where: str) -> Household:
    _reject_unknown(raw, _HOUSEHOLD_KEYS, where)
    return Household(
        id=HouseholdId(str(_require(raw, "id", where))),
        member_ids=tuple(PersonId(str(m)) for m in (raw.get("members") or ())),
        spouse_separately_registered=bool(raw.get("spouse_separately_registered", False)),
        independent_members=tuple(
            PersonId(str(m)) for m in (raw.get("independent_members") or ())
        ),
    )


def _property(raw: Mapping[str, Any], year: int, where: str) -> Property:
    _reject_unknown(raw, _PROPERTY_KEYS, where)
    prices_raw = raw.get("prices")
    if isinstance(prices_raw, Mapping):
        prices = tuple(
            PriceFact(int(y), _as_won(v, f"{where}.prices[{y}]"))
            for y, v in sorted(prices_raw.items())
        )
    elif prices_raw is None:
        prices = ()
    else:
        # 단일 값이면 시나리오의 기준연도 것으로 본다. 흔한 축약을 막지 않는다.
        prices = (PriceFact(year, _as_won(prices_raw, f"{where}.prices")),)

    dong = str(_require(raw, "dong", where))
    if not dong.isdigit() or len(dong) != 10:
        raise SpecError(
            f"{where}.dong: 법정동코드는 숫자 10자리여야 한다 (받은 값 {dong!r}). "
            "예) 서울 강남구 역삼동 = 1168010100"
        )

    rental_raw = raw.get("rental")
    rental = None
    if rental_raw:
        _reject_unknown(rental_raw, _RENTAL_KEYS, f"{where}.rental")
        rental = RentalRegistration(
            rental_type=_enum(RentalType, _require(rental_raw, "type", f"{where}.rental"), f"{where}.rental.type"),
            registered_on=_as_date(_require(rental_raw, "registered_on", f"{where}.rental"), f"{where}.rental.registered_on"),
            obligation_end=_as_opt_date(rental_raw.get("obligation_end"), f"{where}.rental.obligation_end"),
            rent_increase_within_cap=bool(rental_raw.get("rent_increase_within_cap", True)),
        )

    return Property(
        id=PropertyId(str(_require(raw, "id", where))),
        kind=_enum(PropertyKind, raw.get("kind"), f"{where}.kind", PropertyKind.APARTMENT),
        legal_dong_code=dong,
        published_prices=prices,
        display_name=str(raw.get("name") or ""),
        area_m2=float(raw["area_m2"]) if raw.get("area_m2") is not None else None,
        rental=rental,
        is_company_housing=bool(raw.get("company_housing", False)),
        is_unsold_new=bool(raw.get("unsold_new", False)),
        in_urban_planning_area=bool(raw.get("urban", True)),
    )


def _ownership(raw: Mapping[str, Any], where: str) -> Ownership:
    _reject_unknown(raw, _OWNERSHIP_KEYS, where)
    inherited_raw = raw.get("inherited")
    inherited = None
    if inherited_raw:
        _reject_unknown(inherited_raw, _INHERITED_KEYS, f"{where}.inherited")
        inherited = InheritedMeta(
            inheritance_date=_as_date(_require(inherited_raw, "date", f"{where}.inherited"), f"{where}.inherited.date"),
            share=_as_fraction(_require(inherited_raw, "share", f"{where}.inherited"), f"{where}.inherited.share"),
            inherited_value=_as_won(_require(inherited_raw, "value", f"{where}.inherited"), f"{where}.inherited.value"),
        )
    try:
        return Ownership(
            person_id=PersonId(str(_require(raw, "person", where))),
            property_id=PropertyId(str(_require(raw, "property", where))),
            share=_as_fraction(raw.get("share", 1), f"{where}.share"),
            acquired_on=_as_opt_date(raw.get("acquired"), f"{where}.acquired"),
            cause=_enum(AcquisitionCause, raw.get("cause"), f"{where}.cause", AcquisitionCause.PURCHASE),
            inherited=inherited,
        )
    except ValueError as exc:
        raise SpecError(f"{where}: {exc}") from exc


def _residence(raw: Mapping[str, Any], where: str) -> ResidenceSpell:
    _reject_unknown(raw, _RESIDENCE_KEYS, where)
    try:
        return ResidenceSpell(
            person_id=PersonId(str(_require(raw, "person", where))),
            property_id=PropertyId(str(_require(raw, "property", where))),
            start=_as_date(_require(raw, "start", where), f"{where}.start"),
            end=_as_opt_date(raw.get("end"), f"{where}.end"),
            imputed_reason=(
                _enum(ImputedResidenceReason, raw["imputed_reason"], f"{where}.imputed_reason")
                if raw.get("imputed_reason")
                else None
            ),
        )
    except ValueError as exc:
        raise SpecError(f"{where}: {exc}") from exc


def _lease(raw: Mapping[str, Any], where: str) -> LeaseSpell:
    """임대차 구간. 판정(상생임대 여부·갱신요구권 소진)은 절대 적지 않는다.

    ⚠️ `down_payment_evidenced`를 안 적으면 **모름(None)**이다. 시나리오 작성자가
       빼먹은 것을 '확인됨'으로 읽으면, 요건을 못 갖춘 사건이 조용히 통과해
       불변식 검사가 무의미해진다.
    """
    _reject_unknown(raw, _LEASE_KEYS, where)
    evidenced = raw.get("down_payment_evidenced")
    try:
        return LeaseSpell(
            property_id=PropertyId(str(_require(raw, "property", where))),
            start=_as_date(_require(raw, "start", where), f"{where}.start"),
            end=_as_opt_date(raw.get("end"), f"{where}.end"),
            origin=(
                _enum(LeaseOrigin, raw["origin"], f"{where}.origin")
                if raw.get("origin")
                else LeaseOrigin.NEW
            ),
            contracted_on=_as_opt_date(raw.get("contracted"), f"{where}.contracted"),
            deposit=_as_won(raw.get("deposit", 0), f"{where}.deposit"),
            monthly_rent=_as_won(raw.get("rent", 0), f"{where}.rent"),
            down_payment_evidenced=None if evidenced is None else bool(evidenced),
            vacated_on=_as_opt_date(raw.get("vacated"), f"{where}.vacated"),
            ended_by_tenant_circumstance=bool(raw.get("ended_by_tenant_circumstance", False)),
            tenant_ref=str(raw.get("tenant", "")),
        )
    except ValueError as exc:
        raise SpecError(f"{where}: {exc}") from exc


def _election(raw: Mapping[str, Any], where: str) -> Election:
    _reject_unknown(raw, _ELECTION_KEYS, where)
    return Election(
        person_id=PersonId(str(_require(raw, "person", where))),
        kind=_enum(ElectionKind, _require(raw, "kind", where), f"{where}.kind"),
        year=int(raw["year"]) if raw.get("year") else None,
        designated_taxpayer=(
            PersonId(str(raw["designated_taxpayer"])) if raw.get("designated_taxpayer") else None
        ),
        auto_optimize=bool(raw.get("auto_optimize", True)),
    )


def _transfer(raw: Mapping[str, Any], where: str) -> TransferSpec:
    event = None
    if raw.get("transfer"):
        t = raw["transfer"]
        _reject_unknown(t, _TRANSFER_KEYS, f"{where}.transfer")
        try:
            event = TransferEvent(
                property_id=PropertyId(str(_require(t, "property", f"{where}.transfer"))),
                person_id=PersonId(str(_require(t, "person", f"{where}.transfer"))),
                transfer_date=_as_date(_require(t, "date", f"{where}.transfer"), f"{where}.transfer.date"),
                transfer_price=_as_won(_require(t, "price", f"{where}.transfer"), f"{where}.transfer.price"),
                acquisition_price=_as_won(
                    _require(t, "acquisition_price", f"{where}.transfer"), f"{where}.transfer.acquisition_price"
                ),
                acquisition_date=_as_opt_date(t.get("acquisition_date"), f"{where}.transfer.acquisition_date"),
                necessary_expense=_as_won(t.get("necessary_expense", 0), f"{where}.transfer.necessary_expense"),
                share=_as_fraction(t.get("share", 1), f"{where}.transfer.share"),
                holding_years=int(t["holding_years"]) if t.get("holding_years") is not None else None,
                residence_years=int(t["residence_years"]) if t.get("residence_years") is not None else None,
            )
        except ValueError as exc:
            raise SpecError(f"{where}.transfer: {exc}") from exc

    gift = None
    if raw.get("burden_gift"):
        g = raw["burden_gift"]
        _reject_unknown(g, _BURDEN_KEYS, f"{where}.burden_gift")
        try:
            gift = BurdenGift(
                property_id=PropertyId(str(_require(g, "property", f"{where}.burden_gift"))),
                person_id=PersonId(str(_require(g, "person", f"{where}.burden_gift"))),
                gift_date=_as_date(_require(g, "date", f"{where}.burden_gift"), f"{where}.burden_gift.date"),
                appraised_value=_as_won(_require(g, "appraised_value", f"{where}.burden_gift"), f"{where}.burden_gift.appraised_value"),
                gift_value=_as_won(_require(g, "gift_value", f"{where}.burden_gift"), f"{where}.burden_gift.gift_value"),
                debt_assumed=_as_won(_require(g, "debt_assumed", f"{where}.burden_gift"), f"{where}.burden_gift.debt_assumed"),
                acquisition_price=_as_won(_require(g, "acquisition_price", f"{where}.burden_gift"), f"{where}.burden_gift.acquisition_price"),
                necessary_expense=_as_won(g.get("necessary_expense", 0), f"{where}.burden_gift.necessary_expense"),
                holding_years=int(g["holding_years"]) if g.get("holding_years") is not None else None,
                residence_years=int(g["residence_years"]) if g.get("residence_years") is not None else None,
            )
        except ValueError as exc:
            raise SpecError(f"{where}.burden_gift: {exc}") from exc

    return TransferSpec(event=event, burden_gift=gift)


# --------------------------------------------------------------------------
# 로딩
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoadResult:
    scenarios: tuple[Scenario, ...]
    errors: tuple[tuple[Path, str], ...]
    """명세 오류. 엔진 결함이 아니므로 **따로 담아** 리포트에서 분리한다."""


def load_dir(root: str | Path) -> LoadResult:
    """디렉터리 아래 모든 `*.yaml`을 읽는다. 파일 하나에 문서 여럿(`---`) 허용."""
    root = Path(root)
    scenarios: list[Scenario] = []
    errors: list[tuple[Path, str]] = []
    seen: dict[str, Path] = {}

    for path in sorted(root.rglob("*.yaml")):
        try:
            docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except yaml.YAMLError as exc:
            errors.append((path, f"YAML 파싱 실패: {exc}"))
            continue
        for doc in docs:
            if not doc:
                continue
            # 파일 하나에 `scenarios:` 목록으로 담는 형태도 받는다.
            items: Sequence[Any] = doc["scenarios"] if isinstance(doc, Mapping) and "scenarios" in doc else [doc]
            for item in items:
                try:
                    scenario = parse(item, source=path)
                except SpecError as exc:
                    errors.append((path, str(exc)))
                    continue
                if scenario.id in seen:
                    errors.append((path, f"id 중복: {scenario.id} (앞서 {seen[scenario.id].name})"))
                    continue
                seen[scenario.id] = path
                scenarios.append(scenario)

    return LoadResult(tuple(scenarios), tuple(errors))
