"""건축HUB 클라이언트 테스트.

인증키가 없어도 검증할 수 있도록 파싱과 호출을 분리해 두었다.
응답 표본은 공공데이터포털에 공개된 필드 명세를 그대로 따른다.
"""

from __future__ import annotations

import pytest

from realestate_tax.sources import (
    HousePrice,
    ParcelKey,
    Unit,
    coverage,
    join_units_with_prices,
    latest_price_by_pk,
    parse_prices,
    parse_units,
)
from realestate_tax.sources.building_hub import PLAT_LAND, PLAT_MOUNTAIN


# --------------------------------------------------------------------------
# ★ 필지 키 — 실제로 걸려 넘어지는 함정
# --------------------------------------------------------------------------


def test_PNU의_대지구분코드는_API와_코드계가_다르다():
    """PNU 관례는 1=대지·2=산인데 건축HUB는 0:대지·1:산·2:블록이다.
    PNU를 잘라 그대로 넣으면 전건 조회에 실패한다."""
    land = ParcelKey.from_pnu("1168010100" + "1" + "0001" + "0000")
    mountain = ParcelKey.from_pnu("1168010100" + "2" + "0001" + "0000")

    assert land.plat_gb_cd == PLAT_LAND == "0"
    assert mountain.plat_gb_cd == PLAT_MOUNTAIN == "1"
    # PNU 원본 값(1, 2)을 그대로 쓰지 않았음을 못 박는다
    assert land.plat_gb_cd != "1"


def test_PNU를_시군구코드와_법정동코드로_쪼갠다():
    key = ParcelKey.from_pnu("1168010100100120003")
    assert key.sigungu_cd == "11680"
    assert key.bjdong_cd == "10100"
    assert key.bun == "0012"
    assert key.ji == "0003"


@pytest.mark.parametrize("bad", ["11680101001", "abc", "1168010100100120003X"])
def test_잘못된_PNU는_거부한다(bad):
    with pytest.raises(ValueError, match="PNU"):
        ParcelKey.from_pnu(bad)


def test_번지를_4자리로_채운다():
    key = ParcelKey.from_parts("1168010100", 12, 3)
    assert (key.bun, key.ji) == ("0012", "0003")
    assert key.as_params()["sigunguCd"] == "11680"


def test_법정동코드가_10자리가_아니면_거부한다():
    with pytest.raises(ValueError, match="10자리"):
        ParcelKey.from_parts("11680", 1)


# --------------------------------------------------------------------------
# 전유부 파싱
# --------------------------------------------------------------------------


EXPOS_SAMPLE = [
    {
        "mgmBldrgstPk": "PK-101",
        "dongNm": "101동",
        "hoNm": "1502호",
        "flrNoNm": "15",
        "area": "84.99",
        "bldNm": "행복아파트",
        "exposPubuseGbCdNm": "전유",
    },
    {
        "mgmBldrgstPk": "PK-101",
        "dongNm": "101동",
        "hoNm": "1502호",
        "flrNoNm": "15",
        "area": "5.01",
        "bldNm": "행복아파트",
        "exposPubuseGbCdNm": "전유",
    },
    {
        "mgmBldrgstPk": "PK-102",
        "dongNm": "101동",
        "hoNm": "1503호",
        "flrNoNm": "15",
        "area": "59.94",
        "bldNm": "행복아파트",
        "exposPubuseGbCdNm": "전유",
    },
    {
        "mgmBldrgstPk": "PK-COMMON",
        "dongNm": "101동",
        "hoNm": "계단실",
        "area": "300.0",
        "exposPubuseGbCdNm": "공용",
    },
]


def test_공용부는_호_목록에서_제외한다():
    """걸러내지 않으면 호 선택 드롭다운에 계단실·기계실이 섞여 나온다."""
    units = parse_units(EXPOS_SAMPLE)
    assert {u.ho_nm for u in units} == {"1502호", "1503호"}


def test_같은_호의_여러_행은_면적을_합산한다():
    """주용도·부속용도가 별도 행으로 오므로 합치지 않으면 면적이 과소 표시된다."""
    unit = next(u for u in parse_units(EXPOS_SAMPLE) if u.ho_nm == "1502호")
    assert unit.area_m2 == pytest.approx(90.0)


def test_호_표시명은_사람이_읽는_형태로_조립된다():
    unit = next(u for u in parse_units(EXPOS_SAMPLE) if u.ho_nm == "1502호")
    assert unit.label_ko == "행복아파트 101동 1502호"


# --------------------------------------------------------------------------
# 주택가격 파싱
# --------------------------------------------------------------------------


# 실응답 구조를 그대로 따른다(압구정 한양1차 실호출 확인).
#   · 기준일은 stdDay. crtnDay는 대장 생성일이라 한 단지의 모든 행이 같은 값이다.
#   · dongNm·hoNm이 없다 — 호 식별은 mgmBldrgstPk 조인으로만 된다.
#   · mgmBldrgstPk는 정수로 온다.
PRICE_SAMPLE = [
    {"mgmBldrgstPk": "PK-101", "stdDay": "20240101", "crtnDay": "20220813", "hsprc": "780000000"},
    {"mgmBldrgstPk": "PK-101", "stdDay": "20250101", "crtnDay": "20220813", "hsprc": "820000000"},
    {"mgmBldrgstPk": "PK-101", "stdDay": "20260101", "crtnDay": "20220813", "hsprc": "900000000"},
    {"mgmBldrgstPk": "PK-102", "stdDay": "20260101", "crtnDay": "20220813", "hsprc": "610,000,000"},
    {"mgmBldrgstPk": "PK-103", "stdDay": "20260101", "crtnDay": "20220813", "hsprc": ""},
]


def test_기준일은_stdDay이고_crtnDay는_대장생성일이다():
    """crtnDay를 기준일로 쓰면 한 단지의 모든 행이 같은 날짜가 되어
    연도 필터가 통째로 무너진다 — 2026년 조회가 0건이 된다.
    실호출로 확인한 사실이라 테스트로 못 박는다."""
    p = {x.mgm_pk: x for x in parse_prices(PRICE_SAMPLE)}["PK-102"]
    assert p.base_date == __import__("datetime").date(2026, 1, 1)
    assert p.created_on == __import__("datetime").date(2022, 8, 13)
    assert p.year == 2026


def test_mgmBldrgstPk가_정수로_와도_문자열로_다룬다():
    """실응답에서 PK는 int(1024149861)로 온다. 조인 키가 타입 때문에 어긋나면 안 된다."""
    units = parse_units([{"mgmBldrgstPk": 1024149861, "dongNm": "2", "hoNm": "706호",
                          "exposPubuseGbCdNm": "전유", "area": "63.87"}])
    prices = parse_prices([{"mgmBldrgstPk": 1024149861, "stdDay": "20260101", "hsprc": 900000000}])
    (joined,) = join_units_with_prices(units, prices, year=2026)
    assert joined.is_resolved
    assert joined.price.price == 900_000_000


def test_금액에_콤마가_섞여도_읽는다():
    prices = {p.mgm_pk: p for p in parse_prices(PRICE_SAMPLE)}
    assert prices["PK-102"].price == 610_000_000


def test_값이_비어_있으면_건너뛴다():
    """빈 값을 0으로 채우면 '세금 없음'이라는 틀린 결론이 나온다."""
    assert "PK-103" not in {p.mgm_pk for p in parse_prices(PRICE_SAMPLE)}


def test_연도별_이력에서_해당_연도_값을_고른다():
    """주택가격은 연도별로 쌓인다. 첫 건을 쓰면 몇 년 전 값을 집는다."""
    prices = parse_prices(PRICE_SAMPLE)
    assert latest_price_by_pk(prices, year=2026)["PK-101"].price == 900_000_000
    assert latest_price_by_pk(prices, year=2025)["PK-101"].price == 820_000_000
    assert latest_price_by_pk(prices, year=2024)["PK-101"].price == 780_000_000


def test_연도를_지정하지_않으면_최신값():
    assert latest_price_by_pk(parse_prices(PRICE_SAMPLE))["PK-101"].price == 900_000_000


def test_전년도_값이_있으면_세부담상한_계산이_가능해진다():
    """세부담상한은 직전연도 세액을 요구하고, 그러려면 직전연도 공시가격이 필요하다.
    건축HUB가 연도별 이력을 주는 것이 이 경로를 고른 이유 중 하나다."""
    by_year = {
        year: latest_price_by_pk(parse_prices(PRICE_SAMPLE), year=year)["PK-101"].price
        for year in (2025, 2026)
    }
    assert by_year[2025] < by_year[2026]


# --------------------------------------------------------------------------
# 조인
# --------------------------------------------------------------------------


def test_동_호와_공시가격을_PK로_잇는다():
    joined = {
        j.unit.ho_nm: j
        for j in join_units_with_prices(
            parse_units(EXPOS_SAMPLE), parse_prices(PRICE_SAMPLE), year=2026
        )
    }
    assert joined["1502호"].price.price == 900_000_000
    assert joined["1503호"].price.price == 610_000_000
    assert all(j.is_resolved for j in joined.values())


def test_가격을_못_찾으면_None으로_남기고_추정하지_않는다():
    """없는 값을 단지 평균이나 0으로 채우면 9억 경계에서 세액이 뒤집힌다."""
    orphan = Unit(mgm_pk="PK-999", dong_nm="102동", ho_nm="101호")
    (joined,) = join_units_with_prices([orphan], parse_prices(PRICE_SAMPLE), year=2026)
    assert joined.price is None
    assert not joined.is_resolved


def test_채움률로_자동조회_신뢰도를_판정한다():
    """이 값이 낮으면 자동조회를 1차 경로로 쓸 수 없다."""
    units = parse_units(EXPOS_SAMPLE) + (Unit(mgm_pk="PK-999", dong_nm="", ho_nm="9호"),)
    joined = join_units_with_prices(units, parse_prices(PRICE_SAMPLE), year=2026)
    assert coverage(joined) == pytest.approx(2 / 3)
    assert coverage([]) == 0.0


def test_인증키가_없으면_명확한_오류를_낸다(monkeypatch):
    from realestate_tax.sources.building_hub import BuildingHubError, call

    monkeypatch.delenv("DATA_GO_KR_KEY", raising=False)
    with pytest.raises(BuildingHubError, match="인증키"):
        call("getBrHsprcInfo", {})


# --------------------------------------------------------------------------
# 라이브 — 인증키가 있을 때만 (pytest -m live)
# --------------------------------------------------------------------------


@pytest.mark.live
def test_실제_아파트에서_동_호_공시가격이_100퍼센트_조인된다():
    """압구정 한양1차(강남구 압구정동 490번지, 936호) 실호출.

    ★ 이 테스트가 지키는 회귀: **페이징을 빠뜨리면 채움률이 0%가 된다.**
      전유공용면적은 호당 약 6행, 주택가격은 호당 19행(2008~2026)이 쌓여서,
      각 오퍼레이션의 1페이지가 서로 다른 호 집합을 덮는다. 그러면 mgmBldrgstPk
      교집합이 비어 조인이 조용히 실패한다. 실제로 그 버그가 있었다.
    """
    import os

    if not os.environ.get("DATA_GO_KR_KEY"):
        pytest.skip("DATA_GO_KR_KEY 없음")

    from realestate_tax.sources.building_hub import (
        ParcelKey,
        fetch_prices,
        fetch_units,
    )

    key = ParcelKey.from_parts("1168011000", 490, 0)
    units = fetch_units(key)
    prices = fetch_prices(key)

    assert len(units) > 500, f"전유부가 너무 적다({len(units)}호) — 페이징이 빠졌을 수 있다"

    joined = join_units_with_prices(units, prices, year=2026)
    assert coverage(joined) == 1.0, (
        f"채움률 {coverage(joined):.1%} — 페이징이나 조인 키가 깨졌다"
    )

    # 2026년 공시가격(2026-04-30 최종 공시)이 실제로 내려와야 한다
    years = {p.year for p in prices}
    assert 2026 in years and 2025 in years, f"연도 이력 부족: {sorted(years)}"

    sample = next(j for j in joined if j.is_resolved)
    assert sample.unit.dong_nm and sample.unit.ho_nm
    assert sample.price.base_date.month == 1 and sample.price.base_date.day == 1
