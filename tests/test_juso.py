"""도로명주소 상세주소 검색 API 테스트.

응답 표본은 테스트 승인키 `TESTJUSOGOKR`로 실제 호출해 받은 것을 그대로 썼다.
가이드 문서가 아니라 **실응답**이 기준이다 — 가이드는 `bdMgtSn`을 필수(Y)로 적었지만
실제 응답에는 없었다.
"""

from __future__ import annotations

import json

import pytest

from realestate_tax.sources.juso import (
    BLOCKED_KEYWORDS,
    TEST_KEY,
    BuildingKey,
    DetailAddress,
    JusoError,
    UnsafeKeyword,
    assert_safe,
    cross_check,
    parse,
    search,
)

# 실제 응답 (TESTJUSOGOKR, admCd=1144012700 rnMgtSn=311301212700 buldMnnm=301)
LIVE_OK = json.loads(
    '{"results":{"common":{"errorMessage":"정상","errorCode":"0","totalCount":"3"},'
    '"juso":[{"rnMgtSn":"114403113012","buldSlno":"0","buldMnnm":"301","hoNm":"101호",'
    '"dongNm":"A동","udrtYn":"0","floorNm":"1층","admCd":"1144012700"},'
    '{"rnMgtSn":"114403113012","buldSlno":"0","buldMnnm":"301","hoNm":"102호",'
    '"dongNm":"A동","udrtYn":"0","floorNm":"1층","admCd":"1144012700"},'
    '{"rnMgtSn":"114403113012","buldSlno":"0","buldMnnm":"301","hoNm":"103호",'
    '"dongNm":"A동","udrtYn":"0","floorNm":"1층","admCd":"1144012700"}]}}'
)

LIVE_BAD_KEY = json.loads(
    '{"results":{"common":{"errorMessage":"승인되지 않은 KEY 입니다.",'
    '"errorCode":"E0001","totalCount":"0"},"juso":null}}'
)

LIVE_MISSING = json.loads(
    '{"results":{"common":{"errorMessage":"행정구역코드(admCd)의 요청항목이 없습니다.",'
    '"errorCode":"E0002","totalCount":"0"},"juso":null}}'
)


# --------------------------------------------------------------------------
# 파싱
# --------------------------------------------------------------------------


def test_동_층_호를_함께_돌려준다():
    rows = parse(LIVE_OK)
    assert len(rows) == 3
    assert rows[0].dong_nm == "A동"
    assert rows[0].floor_nm == "1층"
    assert rows[0].ho_nm == "101호"
    assert rows[0].adm_cd == "1144012700"


def test_행정구역코드가_법정동코드라_조정대상지역_판정에_바로_쓸_수_있다():
    from realestate_tax.engine.regions import sigungu_of

    assert sigungu_of(parse(LIVE_OK)[0].adm_cd) == "11440"


def test_가이드에_필수로_적힌_bdMgtSn이_실응답에는_없다():
    """가이드 문서를 그대로 믿고 필수 필드로 다루면 KeyError로 터진다.
    실응답이 기준이다."""
    assert parse(LIVE_OK)[0].bd_mgt_sn == ""


def test_표시명은_동과_호를_붙여_만든다():
    assert parse(LIVE_OK)[0].label_ko == "A동 101호"


@pytest.mark.parametrize(
    "payload, code, keyword",
    [
        (LIVE_BAD_KEY, "E0001", "승인되지 않은"),
        (LIVE_MISSING, "E0002", "행정구역코드"),
    ],
)
def test_오류코드는_예외로_올린다(payload, code, keyword):
    """errorCode가 0이 아닌데 빈 목록으로 흘리면 '해당 호 없음'으로 오인된다."""
    with pytest.raises(JusoError) as exc:
        parse(payload)
    assert exc.value.code == code
    assert keyword in str(exc.value)


def test_결과가_비어도_오류가_아니면_빈_튜플():
    payload = {"results": {"common": {"errorCode": "0"}, "juso": None}}
    assert parse(payload) == ()


# --------------------------------------------------------------------------
# ★ WAF 차단 방지 — 가이드 §4.1
# --------------------------------------------------------------------------


def test_정상_주소는_통과한다():
    for text in ("A동", "101동", "가동", "래미안아파트", "e편한세상"):
        assert assert_safe(text) == text


@pytest.mark.parametrize("bad", ["<script>", "a=b", "50%", "a>b"])
def test_특수문자는_보내기_전에_막는다(bad):
    """가이드 §4.1: <, >, =, % 는 SQL Injection 패턴으로 차단된다.
    한 번 차단되면 이후 정상 요청도 막히므로 보내기 전에 걸러야 한다."""
    with pytest.raises(UnsafeKeyword, match="문자"):
        assert_safe(bad)


@pytest.mark.parametrize("bad", ["union select", "DROP table", "1 or 1"])
def test_SQL_예약어는_보내기_전에_막는다(bad):
    with pytest.raises(UnsafeKeyword, match="단어"):
        assert_safe(bad)


def test_예약어가_단어_일부로_들어간_정상_주소는_막지_않는다():
    """부분문자열로 잡으면 정상 주소가 막힌다.
    'Orchard'에 'or', 'Selection'에 'select'가 들어 있다."""
    for text in ("Orchard빌라", "Selection타워", "Creative동", "Uniontown"):
        assert assert_safe(text) == text


def test_차단_단어_목록이_가이드와_일치한다():
    """가이드 §4.1이 열거한 SQL 예약어 전량."""
    assert set(BLOCKED_KEYWORDS) == {
        "or", "select", "insert", "delete", "update", "create",
        "drop", "exec", "union", "fetch", "declare", "truncate",
    }


def test_동_이름에도_차단_검사를_건다():
    key = BuildingKey("1144012700", "311301212700", 301)
    with pytest.raises(UnsafeKeyword):
        search(key, search_type="floorho", dong_nm="A=B동", confm_key=TEST_KEY)


# --------------------------------------------------------------------------
# 요청 조립
# --------------------------------------------------------------------------


def test_요청_파라미터가_가이드_사양과_같다():
    key = BuildingKey("1135010200", "113503109006", 111, 0, "0")
    assert key.as_params() == {
        "admCd": "1135010200",
        "rnMgtSn": "113503109006",
        "udrtYn": "0",
        "buldMnnm": "111",
        "buldSlno": "0",
    }


def test_검색유형은_dong_또는_floorho만_받는다():
    key = BuildingKey("1144012700", "311301212700", 301)
    with pytest.raises(ValueError, match="dong 또는 floorho"):
        search(key, search_type="ho", confm_key=TEST_KEY)


def test_승인키가_없으면_신청_경로를_알려준다(monkeypatch):
    monkeypatch.delenv("JUSO_CONFM_KEY", raising=False)
    key = BuildingKey("1144012700", "311301212700", 301)
    with pytest.raises(JusoError) as exc:
        search(key)
    assert "business.juso.go.kr" in str(exc.value)
    assert "즉시 발급" in str(exc.value)


# --------------------------------------------------------------------------
# 교차검증 — 주소 API vs 건축물대장
# --------------------------------------------------------------------------


def test_두_소스의_호_목록을_대조한다():
    """주소 API와 건축물대장은 서로 독립이다. 어긋나면 한쪽이 낡았다는 신호이므로
    조용히 한쪽을 택하지 않고 차이를 드러낸다."""
    from realestate_tax.sources import Unit

    juso_rows = parse(LIVE_OK)
    hub_rows = [
        Unit(mgm_pk="pk1", dong_nm="A동", ho_nm="101호"),
        Unit(mgm_pk="pk2", dong_nm="A동", ho_nm="102호"),
        Unit(mgm_pk="pk3", dong_nm="A동", ho_nm="104호"),
    ]
    only_juso, only_hub = cross_check(juso_rows, hub_rows)
    assert only_juso == ("103",)
    assert only_hub == ("104",)


def test_두_소스가_일치하면_차이가_없다():
    from realestate_tax.sources import Unit

    hub_rows = [
        Unit(mgm_pk=f"pk{i}", dong_nm="A동", ho_nm=f"10{i}호") for i in (1, 2, 3)
    ]
    assert cross_check(parse(LIVE_OK), hub_rows) == ((), ())


# --------------------------------------------------------------------------
# 라이브 (승인키 있을 때만)
# --------------------------------------------------------------------------


@pytest.mark.live
def test_테스트키로_실제_호출이_된다():
    """가이드에 공개된 샘플 키. 네트워크가 필요하므로 기본 실행에서는 제외된다.
    실행: pytest -m live"""
    key = BuildingKey("1144012700", "311301212700", 301)
    rows = search(key, search_type="dong", confm_key=TEST_KEY)
    assert rows and rows[0].ho_nm
