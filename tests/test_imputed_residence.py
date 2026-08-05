"""비거주 기간을 거주기간으로 인정하는 사유 (2026 개편안, 개조식 p.22).

★ **위험한 방향의 규칙이다.** 거주기간을 늘려 세금을 줄이므로, 상한과 요건을
  빠뜨리면 과소신고가 된다. 실제로 엔진이 `ImputedResidenceReason`을 아예 읽지
  않아 인정 구간이 **상한 없이** 전부 거주로 세어지고 있었다 —
  모델에 있는 사실을 엔진이 안 읽는 실수의 여섯 번째이자, 방향이 가장 나쁜 것.
"""

from __future__ import annotations

from datetime import date

import pytest

from realestate_tax.domain import (
    ImputedResidenceReason,
    PersonId,
    PropertyId,
    ResidenceSpell,
)
from realestate_tax.engine.periods import imputed_spec, merged_residence_years
from realestate_tax.rules import RuleSet, Track, default_ruleset_root
from realestate_tax.rules.resolver import load_ruleset

P = PersonId("p")
H = PropertyId("h")
ON = date(2027, 6, 1)


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return load_ruleset(default_ruleset_root())


@pytest.fixture(scope="module")
def spec(rs: RuleSet):
    return imputed_spec(rs, tax="jongbuse", on=ON, track=Track.REFORM)


def _spell(a: date, b: date, reason=None) -> ResidenceSpell:
    return ResidenceSpell(person_id=P, property_id=H, start=a, end=b, imputed_reason=reason)


def test_실거주는_그대로_센다(spec):
    assert merged_residence_years([_spell(date(2015, 1, 1), date(2020, 1, 1))], ON, imputed=spec) == 5


def test_부득이한_사유_기간을_거주로_인정한다(spec):
    """개조식 p.22 ➊ — 취학·전근·질병·학교폭력·해외체류·부모봉양."""
    spells = [
        _spell(date(2015, 1, 1), date(2020, 1, 1)),
        _spell(date(2020, 1, 1), date(2022, 1, 1), ImputedResidenceReason.JOB_TRANSFER),
    ]
    assert merged_residence_years(spells, ON, imputed=spec) == 7


def test_인정_기간은_최장_3년으로_잘린다(spec):
    """★ 상한을 빠뜨리면 거주기간이 부풀어 **과소신고**가 된다.

    실거주 5년 + 전근 8년을 그냥 더하면 13년이지만, 조문은 최장 3년만 인정하므로
    8년이다. 15년 구간(공제율 50%)과 10년 구간(40%)이 여기서 갈린다."""
    spells = [
        _spell(date(2010, 1, 1), date(2015, 1, 1)),
        _spell(date(2015, 1, 1), date(2023, 1, 1), ImputedResidenceReason.JOB_TRANSFER),
    ]
    assert merged_residence_years(spells, ON, imputed=spec) == 8  # 5 + min(8, 3)


def test_재건축_공사기간은_절반만_인정한다(spec):
    """개조식 p.22 ➋ — "정비사업으로 인한 공사기간의 1/2을 거주기간으로 인정"."""
    spells = [
        _spell(date(2010, 1, 1), date(2015, 1, 1)),
        _spell(date(2015, 1, 1), date(2021, 1, 1), ImputedResidenceReason.RECONSTRUCTION),
    ]
    assert merged_residence_years(spells, ON, imputed=spec) == 8  # 5 + 6//2


def test_근거_규칙이_없으면_인정하지_않는다(spec):
    """`imputed=None`이면(현행법 트랙, 시행 전 연도) 인정 구간을 아예 세지 않는다.
    근거 없이 인정해 주는 것이 가장 위험하다."""
    spells = [
        _spell(date(2015, 1, 1), date(2020, 1, 1)),
        _spell(date(2020, 1, 1), date(2023, 1, 1), ImputedResidenceReason.JOB_TRANSFER),
    ]
    assert merged_residence_years(spells, ON, imputed=None) == 5


def test_시행일_전에는_규칙이_없다(rs: RuleSet):
    """세목마다 시행일이 다르다 — 종부세 '27.1.1., 양도세 '28.1.1.(개조식 p.22)."""
    assert imputed_spec(rs, tax="jongbuse", on=date(2026, 6, 1), track=Track.REFORM) is None
    assert imputed_spec(rs, tax="jongbuse", on=date(2027, 6, 1), track=Track.REFORM) is not None
    assert imputed_spec(rs, tax="transfer", on=date(2027, 6, 1), track=Track.REFORM) is None
    assert imputed_spec(rs, tax="transfer", on=date(2028, 6, 1), track=Track.REFORM) is not None


def test_현행법_트랙에는_인정_규칙이_없다(rs: RuleSet):
    """개편안 신설 조항이다. 현행법으로 계산하면서 인정해 주면 안 된다."""
    assert imputed_spec(rs, tax="jongbuse", on=date(2027, 6, 1), track=Track.CURRENT) is None


def test_열거되지_않은_사유는_인정하지_않는다(spec):
    """조문이 사유를 열거한다. `OTHER`를 통과시키면 아무 사유나 인정된다."""
    spells = [
        _spell(date(2015, 1, 1), date(2020, 1, 1)),
        _spell(date(2020, 1, 1), date(2022, 1, 1), ImputedResidenceReason.OTHER),
    ]
    assert merged_residence_years(spells, ON, imputed=spec) == 5
