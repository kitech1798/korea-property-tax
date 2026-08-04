"""주소 → 공시가격 사슬 테스트.

이 사슬은 두 API를 잇는데, **이어지지 않는 경우가 실제로 있다**.
2026-08-04 실호출로 확인한 사실을 여기에 못 박는다.
"""

from __future__ import annotations

import os

import pytest

from realestate_tax.sources import building_hub as hub
from realestate_tax.sources import juso
from realestate_tax.sources.resolve import (
    LookupOutcome,
    ParcelLookup,
    ParcelProbe,
    dong_sort_key,
    lookup_by_address,
    probe_address,
    unit_price_of,
)

live = pytest.mark.live

# 실제 응답에서 그대로 옮긴 것. 표시지번(458)과 관리번호지번(456)이 다르다.
HYUNDAI = {
    "roadAddr": "서울특별시 강남구 압구정로 201 (압구정동, 현대아파트)",
    "jibunAddr": "서울특별시 강남구 압구정동 458 현대아파트",
    "admCd": "1168011000",
    "bdNm": "현대아파트",
    "zipNo": "06004",
    "lnbrMnnm": "458",
    "lnbrSlno": "0",
    "mtYn": "0",
    "bdMgtSn": "1168011000104560000004824",
    "detBdNmList": "74, 65, 유치원, 84, 83",
}


def parse_one(raw: dict) -> juso.AddressMatch:
    return juso.parse_search({"results": {"common": {"errorCode": "0"}, "juso": [raw]}})[0]


# --------------------------------------------------------------------------
# ★ 지번 후보 — 하나만 쓰면 대단지에서 조용히 0건이 된다
# --------------------------------------------------------------------------


def test_표시지번과_관리번호지번이_다르면_둘_다_후보가_된다():
    """압구정 현대: 표시 458은 건축HUB에서 0건, 관리번호 456이 1,370건.
    하나만 골라 쓰면 '지원 안 하는 아파트'가 되어 버린다."""
    hints = parse_one(HYUNDAI).parcel_hints()
    assert [(h.bun, h.source) for h in hints] == [
        (458, "표시지번(lnbrMnnm)"),
        (456, "건물관리번호(bdMgtSn)"),
    ]


def test_두_지번이_같으면_후보는_하나다():
    """중복 호출로 API 쿼터를 낭비하지 않는다."""
    raw = dict(HYUNDAI, lnbrMnnm="397", bdMgtSn="1168011000103970000004824")
    assert len(parse_one(raw).parcel_hints()) == 1


def test_건물관리번호_형식이_아니면_무시한다():
    """25자리가 아니거나 법정동코드가 안 맞으면 지번을 뽑지 않는다."""
    for bad in ("", "1234", "9999911000104560000004824"):
        hints = parse_one(dict(HYUNDAI, bdMgtSn=bad)).parcel_hints()
        assert [h.bun for h in hints] == [458], bad


def test_산여부는_두_필드의_규약이_반대다():
    """mtYn은 0=대지·1=산, 건물관리번호는 PNU 관례라 1=대지·2=산.
    그대로 복사하면 산지가 대지로 둔갑한다."""
    raw = dict(HYUNDAI, mtYn="1", bdMgtSn="1168011000204560000004824")
    hints = parse_one(raw).parcel_hints()
    assert hints[0].mountain is True  # mtYn="1" → 산
    assert hints[1].mountain is True  # bdMgtSn[10]="2" → 산

    raw2 = dict(HYUNDAI, mtYn="0", bdMgtSn="1168011000104560000004824")
    assert all(h.mountain is False for h in parse_one(raw2).parcel_hints())


def test_지번이_없으면_후보도_없다():
    assert parse_one(dict(HYUNDAI, lnbrMnnm="0", bdMgtSn="")).parcel_hints() == ()


# --------------------------------------------------------------------------
# 파싱
# --------------------------------------------------------------------------


def test_법정동코드를_그대로_지역판정에_쓸_수_있다():
    m = parse_one(HYUNDAI)
    assert m.legal_dong_code == "1168011000"
    assert (m.sigungu_cd, m.bjdong_cd) == ("11680", "11000")


def test_오류코드는_예외로_올린다():
    with pytest.raises(juso.JusoError, match="E0001"):
        juso.parse_search(
            {"results": {"common": {"errorCode": "E0001", "errorMessage": "승인되지 않은 KEY"}}}
        )


def test_검색어_최소길이와_차단패턴을_보내기_전에_막는다():
    """한 번 차단당하면 그 뒤 정상 요청도 막힌다. 그래서 사전 차단이다."""
    with pytest.raises(juso.UnsafeKeyword, match="2자 이상"):
        juso.search_address("가")
    with pytest.raises(juso.UnsafeKeyword):
        juso.search_address("select * from")


# --------------------------------------------------------------------------
# 실패를 조용히 넘기지 않는다
# --------------------------------------------------------------------------


def test_조회_실패는_시도한_지번을_밝힌다():
    """빈 목록을 그냥 그리면 사용자는 '지원 안 하나 보다' 하고 떠난다."""
    r = ParcelLookup(
        address=parse_one(HYUNDAI),
        outcome=LookupOutcome.NO_UNITS,
        tried=((juso.ParcelHint(458, 0, False, "표시지번(lnbrMnnm)"), 0),),
    )
    msg = r.message_ko()
    assert "458" in msg and "표시지번" in msg
    assert "직접 입력" in msg
    assert not r.ok


def test_호는_있는데_공시가격이_없으면_직접입력으로_흘린다():
    unit = hub.Unit(mgm_pk="pk1", dong_nm="101", ho_nm="1201호")
    r = ParcelLookup(
        address=parse_one(HYUNDAI),
        outcome=LookupOutcome.UNITS_ONLY,
        units=(hub.UnitPrice(unit=unit, price=None),),
    )
    assert not r.ok
    assert "직접 입력" in r.message_ko()


def test_동_목록은_숫자로_정렬한다():
    """문자열 정렬이면 '10동'이 '2동'보다 앞에 온다 — 사용자가 자기 동을 못 찾는다."""
    units = tuple(
        hub.UnitPrice(unit=hub.Unit(mgm_pk=f"pk{i}", dong_nm=d, ho_nm="101호"), price=None)
        for i, d in enumerate(("10", "2", "101", "7"))
    )
    r = ParcelLookup(address=parse_one(HYUNDAI), outcome=LookupOutcome.UNITS_ONLY, units=units)
    assert r.dong_names == ("2", "7", "10", "101")


def test_없는_호는_0이_아니라_None이다():
    """0원으로 뭉개면 '공시가격 0원'이 그대로 세액 계산에 들어간다."""
    unit = hub.Unit(mgm_pk="pk1", dong_nm="101", ho_nm="1201호")
    units = (hub.UnitPrice(unit=unit, price=None),)
    assert unit_price_of(units, "101", "1201호") is None
    assert unit_price_of(units, "999", "1201호") is None


# --------------------------------------------------------------------------
# ★ 싼 조회로 먼저 거른다 — 표제부 0.2초 vs 전유부 27초 vs 주택가격 41초
# --------------------------------------------------------------------------

TITLE_ROWS = [
    {"dongNm": "25", "bldNm": "미성아파트 제25동", "mainPurpsCdNm": "공동주택",
     "hhldCnt": 67, "mgmBldrgstPk": "102411467"},
    {"dongNm": "2", "bldNm": "미성아파트 제2동", "mainPurpsCdNm": "공동주택",
     "hhldCnt": 60, "mgmBldrgstPk": "102411468"},
    {"dongNm": "10", "bldNm": "미성아파트 제10동", "mainPurpsCdNm": "공동주택",
     "hhldCnt": 80, "mgmBldrgstPk": "102411469"},
]


def test_표제부에서_동목록과_세대수를_얻는다():
    bs = hub.parse_buildings(TITLE_ROWS)
    p = ParcelProbe(address=parse_one(HYUNDAI), hint=juso.ParcelHint(397),
                    parcel=hub.ParcelKey("11680", "11000", "0397"), buildings=bs)
    assert p.ok
    assert p.dong_names == ("2", "10", "25")  # 문자열 정렬이면 10이 2보다 앞
    assert p.household_count == 207
    assert p.complex_name == "미성아파트"  # '제25동' 꼬리표를 뗀다
    assert "3개 동" in p.message_ko() and "207세대" in p.message_ko()


def test_표제부_중복_PK는_한_번만_센다():
    rows = TITLE_ROWS + [dict(TITLE_ROWS[0])]
    assert len(hub.parse_buildings(rows)) == 3


def test_주택이_아니면_계산_대상이_아니라고_말한다():
    """강남파이낸스센터 같은 업무시설에 보유세 계산을 태우면 안 된다."""
    bs = hub.parse_buildings(
        [{"dongNm": "", "bldNm": "강남파이낸스센터", "mainPurpsCdNm": "업무시설",
          "hhldCnt": 0, "mgmBldrgstPk": "1"}]
    )
    p = ParcelProbe(address=parse_one(HYUNDAI), hint=juso.ParcelHint(737),
                    parcel=hub.ParcelKey("11680", "10100", "0737"), buildings=bs)
    assert not p.has_house
    assert "주택이 아닙니다" in p.message_ko()
    assert "업무시설" in p.message_ko()


def test_표제부가_비면_다음_지번_후보로_넘어간다(monkeypatch):
    """압구정 현대: 458은 대장이 없고 456에 있다.
    이 판정을 27초짜리 전유부로 하면 후보 2개에 1분을 버린다."""
    calls: list[str] = []

    def fake(key, *, service_key=None):
        calls.append(key.bun)
        return hub.parse_buildings(TITLE_ROWS) if key.bun == "0456" else ()

    monkeypatch.setattr(hub, "fetch_buildings", fake)
    p = probe_address(parse_one(HYUNDAI))
    assert calls == ["0458", "0456"]  # 싼 것부터, 순서대로
    assert p.ok and p.hint is not None
    assert p.hint.source == "건물관리번호(bdMgtSn)"


def test_모든_후보가_비면_시도한_지번을_밝힌다(monkeypatch):
    monkeypatch.setattr(hub, "fetch_buildings", lambda key, *, service_key=None: ())
    p = probe_address(parse_one(HYUNDAI))
    assert not p.ok
    assert "458" in p.message_ko() and "456" in p.message_ko()
    assert "직접 입력" in p.message_ko()


def test_한_후보가_터져도_다음_후보를_시도한다(monkeypatch):
    def flaky(key, *, service_key=None):
        if key.bun == "0458":
            raise hub.BuildingHubError("호출 실패")
        return hub.parse_buildings(TITLE_ROWS)

    monkeypatch.setattr(hub, "fetch_buildings", flaky)
    assert probe_address(parse_one(HYUNDAI)).ok


def test_비싼_조회는_확정된_지번으로_한_번만_나간다(monkeypatch):
    """후보마다 전유부+가격을 돌리면 70초 × 후보수가 된다."""
    seen: list[str] = []

    def fake_lookup(key, *, year=None, service_key=None):
        seen.append(key.bun)
        unit = hub.Unit(mgm_pk="pk1", dong_nm="25", ho_nm="202호")
        price = hub.HousePrice(mgm_pk="pk1", base_date=None, price=4_281_000_000)
        return (hub.UnitPrice(unit=unit, price=price),)

    monkeypatch.setattr(
        hub, "fetch_buildings",
        lambda key, *, service_key=None: hub.parse_buildings(TITLE_ROWS) if key.bun == "0456" else (),
    )
    monkeypatch.setattr(hub, "lookup_units_with_price", fake_lookup)

    r = lookup_by_address(parse_one(HYUNDAI), year=2026)
    assert r.ok
    assert seen == ["0456"]  # 458로는 비싼 조회를 아예 안 나갔다


def test_동_정렬은_숫자로_한다():
    assert sorted(["10", "2", "101", "7"], key=dong_sort_key) == ["2", "7", "10", "101"]
    assert sorted(["가동", "나동"], key=dong_sort_key) == ["가동", "나동"]


# --------------------------------------------------------------------------
# 실호출 (pytest -m live)
# --------------------------------------------------------------------------


needs_keys = pytest.mark.skipif(
    not (os.environ.get("JUSO_CONFM_KEY") and os.environ.get("DATA_GO_KR_KEY")),
    reason="JUSO_CONFM_KEY / DATA_GO_KR_KEY 필요",
)


@live
@needs_keys
def test_live_주소검색이_지번을_준다():
    rows = juso.search_address("압구정로 113", per_page=3)
    assert rows
    m = rows[0]
    assert m.legal_dong_code == "1168011000"
    assert m.parcel_hints()[0].bun == 397


@live
@needs_keys
def test_live_표제부_판정은_1초_안에_끝난다():
    """화면이 이 속도에 기대고 있다. 느려지면 UX 설계가 무너진다."""
    import time

    m = juso.search_address("압구정로 113", per_page=1)[0]
    t = time.perf_counter()
    p = probe_address(m)
    assert p.ok, p.message_ko()
    assert time.perf_counter() - t < 3.0
    assert p.has_house
    assert p.complex_name == "미성아파트"
    assert len(p.dong_names) >= 5


@live
@needs_keys
def test_live_표시지번이_0건이면_관리번호_지번으로_넘어간다():
    """압구정 현대 — 이 폴백이 없으면 1,340세대 단지가 통째로 조회 실패한다."""
    m = juso.search_address("압구정로 201", per_page=1)[0]
    p = probe_address(m)
    assert p.ok, p.message_ko()
    assert p.hint is not None and p.hint.source == "건물관리번호(bdMgtSn)"
    assert p.household_count > 1000


@live
@needs_keys
def test_live_동을_지정하면_호_목록이_빨리_온다():
    from realestate_tax.sources.resolve import units_in_dong

    p = probe_address(juso.search_address("압구정로 113", per_page=1)[0])
    assert p.parcel is not None
    units = units_in_dong(p.parcel, p.dong_names[0])
    assert units
    assert {u.dong_nm for u in units} == {p.dong_names[0]}


@live
@needs_keys
def test_live_주소_한_줄에서_공시가격까지_이어진다():
    m = juso.search_address("압구정로 113", per_page=1)[0]
    r = lookup_by_address(m, year=2026)
    assert r.ok, r.message_ko()
    assert len(r.units) > 900
    assert r.coverage > 0.9
    assert r.dong_names


@live
@needs_keys
def test_live_주택이_아닌_건물은_계산_대상이_아니라고_말한다():
    """강남파이낸스센터 — 업무시설. 보유세 계산을 태우면 안 된다."""
    m = juso.search_address("테헤란로 152", per_page=1)[0]
    p = probe_address(m)
    assert not (p.ok and p.has_house)
    assert "직접 입력" in p.message_ko() or "주택이 아닙니다" in p.message_ko()
