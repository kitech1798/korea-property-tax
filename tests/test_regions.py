"""조정대상지역 판정 테스트.

시중 계산기의 가장 유명한 결함을 정면으로 겨눈다 —
propertytax.co.kr은 `fullAddrName.startsWith('서울')`로 판정한다(app.js:768).

여기서 증명할 것.
  ① 주소 문자열이 아니라 법정동코드로만 판정한다
  ② 경기도 일반구 단위 부분 지정을 정확히 가른다 (수원 4개구 중 3개만)
  ③ **모르면 '아니오'가 아니라 '모름'으로 흘린다** ← 가장 중요
"""

from __future__ import annotations

from datetime import date

import pytest

from realestate_tax.domain import DeterminationQuality, LegalStatus
from realestate_tax.engine.regions import (
    LAND_PERMIT_NOTICE_KO,
    NO,
    UNKNOWN,
    YES,
    check_regulated,
    check_speculation,
    region_trace,
    sigungu_of,
)
from realestate_tax.rules import RuleSet, Track, default_ruleset_root

TODAY = date(2026, 8, 4)


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


def status(rs: RuleSet, code: str, on: date = TODAY):
    return check_regulated(code, rs, on=on, track=Track.CURRENT)


# --------------------------------------------------------------------------
# ① 코드로만 판정한다
# --------------------------------------------------------------------------


def test_법정동코드_앞_5자리를_시군구코드로_쓴다():
    assert sigungu_of("1168010100") == "11680"


@pytest.mark.parametrize("bad", ["", "1168", "서울시 강남구", "abcde12345"])
def test_법정동코드가_아니면_거부한다(bad):
    with pytest.raises(ValueError, match="법정동코드"):
        sigungu_of(bad)


def test_서울_강남구는_조정대상지역이다(rs: RuleSet):
    s = status(rs, "1168010100")
    assert s.designation is YES
    assert s.region_name == "서울 강남구"
    assert s.since == date(2016, 11, 3)


def test_서울_25개_자치구가_전부_지정돼_있다(rs: RuleSet):
    seoul = [
        "11110", "11140", "11170", "11200", "11215", "11230", "11260", "11290",
        "11305", "11320", "11350", "11380", "11410", "11440", "11470", "11500",
        "11530", "11545", "11560", "11590", "11620", "11650", "11680", "11710", "11740",
    ]
    assert len(seoul) == 25
    for code in seoul:
        assert status(rs, code + "00000").designation is YES, code


def test_인천은_비규제다(rs: RuleSet):
    """'수도권이면 규제'라는 흔한 오해. 인천은 지정돼 있지 않다."""
    assert status(rs, "2811000000").designation is NO


@pytest.mark.parametrize(
    "code, label",
    [
        ("2635010300", "부산 해운대구"),
        ("2711010100", "대구 중구"),
        ("2911010100", "광주 동구"),
        ("3017010100", "대전 서구"),
    ],
)
def test_비수도권_광역시는_비규제다(rs: RuleSet, code, label):
    assert status(rs, code).designation is NO, label


# --------------------------------------------------------------------------
# ② 경기도 일반구 부분 지정 — 시 단위로 뭉개면 틀린다
# --------------------------------------------------------------------------


def test_수원시는_4개구_중_3개만_지정돼_있다(rs: RuleSet):
    """장안·팔달·영통은 지정, 권선구는 미지정. '수원시'로 뭉개면 권선구가 오판된다."""
    assert status(rs, "4111100000").designation is YES  # 장안구
    assert status(rs, "4111500000").designation is YES  # 팔달구
    assert status(rs, "4111700000").designation is YES  # 영통구
    assert status(rs, "4111300000").designation is NO  # 권선구


def test_용인시는_수지_기흥만_지정되고_처인구는_아니다(rs: RuleSet):
    assert status(rs, "4146500000").designation is YES  # 수지구
    assert status(rs, "4146300000").designation is YES  # 기흥구
    assert status(rs, "4146100000").designation is NO  # 처인구


def test_안양시는_동안구만_지정되고_만안구는_아니다(rs: RuleSet):
    assert status(rs, "4117300000").designation is YES  # 동안구
    assert status(rs, "4117100000").designation is NO  # 만안구


# --------------------------------------------------------------------------
# 시점 판정 — 취득 당시 규제지역이었나
# --------------------------------------------------------------------------


def test_2026년_7월_1일부터_구리시가_추가됐다(rs: RuleSet):
    """지정 전후로 판정이 갈려야 한다. 취득 시점 판정에 이 정확도가 필요하다."""
    assert status(rs, "4131000000", on=date(2026, 6, 1)).designation is NO
    assert status(rs, "4131000000", on=date(2026, 8, 1)).designation is YES


def test_용인_기흥구도_2026년_7월부터다(rs: RuleSet):
    assert status(rs, "4146300000", on=date(2026, 6, 1)).designation is NO
    assert status(rs, "4146300000", on=date(2026, 8, 1)).designation is YES


def test_2025년_10월_이전은_이력이_없어_판정하지_않는다(rs: RuleSet):
    """없는 이력을 '비규제'로 단정하면 조용히 틀린 세액이 나온다."""
    s = status(rs, "1168010100", on=date(2024, 6, 1))
    assert s.designation is UNKNOWN
    assert s.certainty.determination is DeterminationQuality.UNDECIDABLE
    assert "이력을 수집하지 않았습니다" in s.reason_ko


# --------------------------------------------------------------------------
# ③ ★ 모르면 '아니오'가 아니라 '모름'
# --------------------------------------------------------------------------


def test_화성시는_동탄구만_조정대상지역이다(rs: RuleSet):
    """2026-02-01 신설된 일반구 4개 중 동탄구만 지정됐다.
    시 단위로 뭉갰으면 나머지 세 구가 통째로 오판됐을 자리다.
    코드 출처: 행정표준코드관리시스템(code.go.kr) — python tools/find_region_code.py 화성시"""
    assert status(rs, "4159700000").designation is YES  # 동탄구
    assert status(rs, "4159700000").region_name == "화성시 동탄구"
    for code, name in (("41591", "만세구"), ("41593", "효행구"), ("41595", "병점구")):
        assert status(rs, code + "00000").designation is NO, name


def test_동탄구도_2026년_7월_이전에는_비규제였다(rs: RuleSet):
    assert status(rs, "4159700000", on=date(2026, 6, 1)).designation is NO
    assert status(rs, "4159700000", on=date(2026, 8, 1)).designation is YES


def test_구_신설_이전_옛_화성시_코드는_판정_불가로_흐른다(rs: RuleSet):
    """41590으로 시작하는 옛 코드는 어느 구인지 알 수 없다.
    동탄구면 조정대상지역, 나머지 세 구면 아니다 — 가를 수 없으므로 '모름'이 정답이다.
    '아님'으로 단정하면 동탄구 소유자에게 조용히 틀린 세액이 나간다."""
    s = status(rs, "4159000000")
    assert s.designation is UNKNOWN
    assert "일반구 신설 전" in s.reason_ko
    assert "41597" in s.reason_ko
    assert s.certainty.determination is DeterminationQuality.UNDECIDABLE


def test_판정_불가는_규제_아님과_다르다(rs: RuleSet):
    """`is_regulated`가 둘 다 False라서 세액 분기에 그걸 쓰면 안 된다.
    호출부는 반드시 designation을 봐야 한다."""
    unknown = status(rs, "4159000000")
    clean = status(rs, "2811000000")

    assert unknown.is_regulated is False and clean.is_regulated is False
    assert unknown.needs_confirmation and not clean.needs_confirmation
    assert unknown.designation is not clean.designation


def test_고시_확인일보다_뒤의_기준일은_확실성이_낮아진다(rs: RuleSet):
    """조정대상지역은 반기마다 재검토되고 예고 없이 바뀐다.
    2026년 8월에 확인한 표로 2028년을 판정하면서 확정인 척하면 안 된다."""
    now = status(rs, "1168010100", on=TODAY)
    future = status(rs, "1168010100", on=date(2028, 6, 1))

    assert now.certainty.legal is LegalStatus.ENACTED
    assert future.certainty.legal is LegalStatus.ASSUMED
    assert future.designation is YES  # 값은 같지만 확실성이 다르다


# --------------------------------------------------------------------------
# 투기과열지구
# --------------------------------------------------------------------------


def test_투기과열지구는_현재_조정대상지역과_동일하다(rs: RuleSet):
    for code in ("1168010100", "4111100000", "4131000000", "2811000000"):
        assert (
            check_speculation(code, rs, on=TODAY).designation
            == status(rs, code).designation
        ), code


def test_투기과열지구는_별도_규칙으로_관리된다(rs: RuleSet):
    """지금은 값이 같아도 장차 갈릴 수 있다. 하나로 합치면 그때 조용히 틀린다."""
    assert "reference.speculation_zones" in rs
    assert "reference.regulated_areas" in rs


# --------------------------------------------------------------------------
# 감사 추적
# --------------------------------------------------------------------------


def test_판정_근거가_감사추적에_남는다(rs: RuleSet):
    trace = region_trace(status(rs, "1168010100"), "우리집")
    assert "11680" in trace.substitution
    assert "고시 확인일" in trace.substitution
    assert trace.output.amount is True


def test_오래된_표로_판정하면_추적에_경고가_붙는다(rs: RuleSet):
    trace = region_trace(status(rs, "1168010100", on=date(2028, 6, 1)))
    assert "⚠️" in trace.note_ko
    assert "국토교통부 공고" in trace.note_ko


def test_판정_불가도_추적에_사유와_해결책이_남는다(rs: RuleSet):
    """'모릅니다'로 끝내면 사용자는 막힌다. 무엇을 하면 되는지까지 알려야 한다."""
    trace = region_trace(status(rs, "4159000000"))
    assert trace.output.label == "판정 불가"
    assert "일반구 신설 전" in trace.note_ko
    assert "41597" in trace.note_ko  # 다시 입력할 코드를 제시한다


# --------------------------------------------------------------------------
# 토지거래허가구역 — 판정하지 않는다
# --------------------------------------------------------------------------


def test_토지거래허가구역은_판정하지_않고_이유를_밝힌다():
    """세율·과표를 정하는 조문에서 참조되지 않고, 필지 단위라 주소로는 오판한다.
    '지원 안 함'이 아니라 '왜 안 하는지'를 말한다."""
    assert "세율이나 과세표준을 정하는 조문에서 참조되지 않습니다" in LAND_PERMIT_NOTICE_KO
    assert "필지·구역 단위" in LAND_PERMIT_NOTICE_KO
    assert "eum.go.kr" in LAND_PERMIT_NOTICE_KO
    # 매도 시점 계획에는 영향이 있을 수 있다는 점까지 알린다
    assert "리드타임" in LAND_PERMIT_NOTICE_KO
