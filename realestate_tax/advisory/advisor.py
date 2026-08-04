"""상담 지식 로딩과 매칭.

엔진의 계산 결과에서 **매칭 컨텍스트**를 뽑아내고, 그 조건에 맞는 상담 지식을 고른다.
LLM 호출이 없으므로 같은 상황에는 항상 같은 조언이 나온다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from ..domain.models import PersonId, TaxCase
from ..engine.jongbuse import JongbuseResult, _joint_spouse_status
from ..engine.regions import UNKNOWN, YES, check_regulated
from ..engine.special_houses import SpecialKind, assess
from ..engine.trace import format_manwon
from ..rules.resolver import RuleSet
from ..rules.schema import Track
from .schema import Advisory, AdvisoryError, RenderedAdvisory, parse_advisory, select

_CACHE: dict[Path, tuple[Advisory, ...]] = {}


def default_root() -> Path:
    return Path(__file__).resolve().parents[2] / "corpus" / "advisory"


def load(root: str | Path | None = None) -> tuple[Advisory, ...]:
    """상담 지식 YAML을 읽는다. id 중복은 즉시 실패시킨다."""
    path = Path(root) if root is not None else default_root()
    path = path.resolve()
    if path in _CACHE:
        return _CACHE[path]

    if not path.is_dir():
        _CACHE[path] = ()
        return ()

    items: list[Advisory] = []
    seen: dict[str, Path] = {}
    for file in sorted(path.rglob("*.yaml")):
        docs = yaml.safe_load(file.read_text(encoding="utf-8"))
        if docs is None:
            continue
        entries = docs.get("entries", docs) if isinstance(docs, Mapping) else docs
        for raw in entries or []:
            adv = parse_advisory(raw)
            if adv.id in seen:
                raise AdvisoryError(
                    f"id 중복: {adv.id}\n  - {seen[adv.id]}\n  - {file}"
                )
            seen[adv.id] = file
            items.append(adv)

    _CACHE[path] = tuple(items)
    return _CACHE[path]


def clear_cache() -> None:
    _CACHE.clear()


# --------------------------------------------------------------------------
# 매칭 컨텍스트 — 엔진 판정 결과에서만 뽑는다
# --------------------------------------------------------------------------


def build_context(
    case: TaxCase,
    person_id: PersonId,
    ruleset: RuleSet,
    *,
    track: Track = Track.CURRENT,
    result: JongbuseResult | None = None,
    transfer_planned: bool = False,
) -> dict[str, Any]:
    """상담 조건 매칭에 쓸 컨텍스트.

    ★ 사용자가 입력한 값이 아니라 **엔진이 판정한 결과**만 담는다.
      '1세대1주택입니다'를 사용자에게 물어서 조언을 고르면, 판정을 떠넘긴
      시중 계산기와 같은 실수를 반복하는 것이다.
    """
    on = case.assessment_date
    person = case.find_person(person_id)
    assessment = assess(case, person_id, ruleset, track=track, on=on)

    kinds = {s.kind for s in assessment.specials}
    zones = [
        check_regulated(case.find_property(o.property_id).legal_dong_code, ruleset, on=on, track=track)
        for o in case.ownerships_of(person_id)
        if case.find_property(o.property_id).is_house
    ]

    total_price = 0
    for o in case.ownerships_of(person_id):
        prop = case.find_property(o.property_id)
        fact = prop.price_for(case.year) if prop.is_house else None
        if fact is not None:
            total_price += int(fact.value * o.share)

    ctx: dict[str, Any] = {
        "house_count": assessment.count,
        "is_one_house": assessment.is_one_house,
        "joint_spouse_eligible": _joint_spouse_status(case, person_id).eligible,
        "has_inherited": SpecialKind.INHERITANCE in kinds,
        "has_rental": SpecialKind.RENTAL_EXCLUSION in kinds,
        "has_temporary_two": SpecialKind.TEMPORARY_TWO in kinds,
        "in_regulated_zone": any(z.designation is YES for z in zones),
        "zone_unknown": any(z.designation is UNKNOWN for z in zones),
        "track": str(track),
        "year": case.year,
        "price_total": total_price,
        "transfer_planned": transfer_planned,
    }

    age = person.age_at(on)
    if age is not None:
        ctx["age"] = age

    if result is not None:
        credit = result.trace.find("jb.10.tax_credit")
        ctx["resides"] = _resides_flag(result)
        if credit is not None and credit.branch is not None:
            ctx.setdefault("residence_years", None)
    return ctx


def _resides_flag(result: JongbuseResult) -> bool:
    node = result.trace.find("jb.06.basic_deduction")
    if node is None or node.branch is None:
        return False
    return "비거주" not in node.branch.taken and "거주" in node.branch.taken


def build_values(result: JongbuseResult | None, extra: Mapping[str, Any] | None = None) -> dict[str, str]:
    """자리표시자에 넣을 값. **엔진이 계산한 숫자만** 들어간다."""
    values: dict[str, str] = {}
    if result is not None:
        values["보유세"] = format_manwon(result.holding_tax_total)
        values["종부세"] = format_manwon(result.total.as_int())
        values["재산세"] = format_manwon(result.property_tax_total.as_int())
    for k, v in (extra or {}).items():
        values[str(k)] = str(v)
    return values


def advise(
    case: TaxCase,
    person_id: PersonId,
    ruleset: RuleSet,
    *,
    track: Track = Track.CURRENT,
    result: JongbuseResult | None = None,
    transfer_planned: bool = False,
    values: Mapping[str, Any] | None = None,
    root: str | Path | None = None,
    limit: int = 8,
) -> tuple[RenderedAdvisory, ...]:
    """상황에 맞는 상담 지식을 골라 자리표시자를 채운다.

    채우지 못한 자리표시자가 남은 항목은 **버린다** — "{{절감액}} 절감됩니다"가
    그대로 화면에 나가는 것보다 아예 안 보여주는 게 낫다.
    """
    ctx = build_context(
        case, person_id, ruleset, track=track, result=result, transfer_planned=transfer_planned
    )
    filled = build_values(result, values)
    rendered = [a.render(filled) for a in select(load(root), ctx, limit=limit * 2)]
    return tuple(r for r in rendered if r.displayable)[:limit]
