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
from realestate_tax.sources import building_hub as hub
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


# --------------------------------------------------------------------------
# 오류 분류 — "무엇이 잘못됐나"를 틀리게 말하면 사용자가 엉뚱한 일을 한다
# --------------------------------------------------------------------------


def test_빈_응답은_키_문제가_아니라_일시적_장애다():
    """★ 2026-08-13 실측.

    같은 시각에 한 오퍼레이션은 HTTP 200에 **본문 0자**, 다른 하나는 503,
    또 다른 하나는 정상 JSON을 돌려줬다. 포털 과부하의 전형이다.

    예전 코드는 이걸 "인증키 미승인·한도초과 의심"이라고 말했다. 키는 멀쩡한데
    사용자를 발급 페이지로 보내는 문구였다. 실제로 화면에 그렇게 떴다.
    """
    err = hub._classify_non_json("", "getBrExposPubuseAreaInfo")
    assert isinstance(err, hub.TransientUnavailable)
    assert "인증키와는 무관" in str(err)
    assert "미승인" not in str(err)


def test_포털이_보낸_오류_사유를_그대로_보여준다():
    """XML 오류문서에는 **진짜 사유**가 적혀 있다. 요약하지 말고 옮긴다."""
    xml = (
        '<?xml version="1.0"?><OpenAPI_ServiceResponse><cmmMsgHeader>'
        "<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>"
        "<returnReasonCode>30</returnReasonCode>"
        "</cmmMsgHeader></OpenAPI_ServiceResponse>"
    )
    err = hub._classify_non_json(xml, "getBrTitleInfo")
    assert not isinstance(err, hub.TransientUnavailable)
    assert "SERVICE_KEY_IS_NOT_REGISTERED_ERROR" in str(err)
    assert "returnReasonCode=30" in str(err)


def test_한도초과도_사유_그대로_전달한다():
    xml = (
        "<response><resultCode>22</resultCode>"
        "<resultMsg>LIMITED NUMBER OF SERVICE REQUESTS EXCEEDS ERROR</resultMsg></response>"
    )
    err = hub._classify_non_json(xml, "getBrTitleInfo")
    assert "LIMITED NUMBER OF SERVICE REQUESTS EXCEEDS ERROR" in str(err)


def test_모르는_형식이면_앞부분을_보여준다():
    """점검 안내 HTML 등. 지어내지 말고 받은 것을 보여준다."""
    err = hub._classify_non_json("<html><body>점검 중입니다</body></html>", "getBrTitleInfo")
    assert "앞부분" in str(err)
    assert "점검 중입니다" in str(err)


# --------------------------------------------------------------------------
# 빈 200 응답 재시도 — 포털이 4번에 1번꼴로 빈손으로 온다
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")
        self.status = 200
        self.headers = {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(bodies):
    """호출될 때마다 `bodies`를 차례로 돌려준다. 다 쓰면 마지막 것을 반복한다."""
    seq = list(bodies)
    calls = {"n": 0}

    def opener(req, timeout=None):
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return _FakeResponse(seq[i])

    opener.calls = calls
    return opener


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(hub.time, "sleep", lambda *_: None)


def test_빈_응답이_와도_다시_쳐서_성공한다(monkeypatch, no_sleep):
    """★ 2026-08-13 실측 — 같은 호출이 4번에 1번꼴로 빈 본문을 돌려준다.

    한 번 실패했다고 포기하면 호 목록 조회가 통째로 무너진다. 한 단지가 수십
    페이지라 페이지 하나만 실패해도 전체가 실패하기 때문이다.
    """
    opener = _fake_urlopen(["", "", "", '{"response":{"ok":1}}'])
    monkeypatch.setattr(hub.urllib.request, "urlopen", opener)
    got = hub._get_with_retry("https://example.test/x", "getBrTitleInfo")
    assert got == '{"response":{"ok":1}}'
    assert opener.calls["n"] == 4


def test_계속_비어_있으면_일시적_장애로_보고한다(monkeypatch, no_sleep):
    """키 문제로 오진하지 않는다. 사용자가 할 일은 '기다리기'다."""
    monkeypatch.setattr(hub.urllib.request, "urlopen", _fake_urlopen([""]))
    with pytest.raises(hub.TransientUnavailable) as e:
        hub._get_with_retry("https://example.test/x", "getBrTitleInfo")
    assert "인증키와는 무관" in str(e.value)


def test_일시적_장애가_일반_오류로_둔갑하지_않는다(monkeypatch, no_sleep):
    """`try` 안에서 던진 예외를 아래 `except Exception`이 도로 잡으면
    원인 구분이 무너진다. 실제로 그렇게 짰다가 잡았다."""
    monkeypatch.setattr(hub.urllib.request, "urlopen", _fake_urlopen([""]))
    with pytest.raises(hub.BuildingHubError) as e:
        hub._get_with_retry("https://example.test/x", "getBrTitleInfo")
    assert type(e.value) is hub.TransientUnavailable


def test_빈_응답은_5xx_재시도_예산을_쓰지_않는다(monkeypatch, no_sleep):
    """둘은 성격이 다른 실패다. 한 예산으로 묶으면 빈 응답 몇 번에 5xx 여력이 사라진다."""
    bodies = [""] * (hub._MAX_ATTEMPTS + 1) + ['{"ok":1}']
    opener = _fake_urlopen(bodies)
    monkeypatch.setattr(hub.urllib.request, "urlopen", opener)
    assert hub._get_with_retry("https://example.test/x", "getBrTitleInfo") == '{"ok":1}'
    assert opener.calls["n"] == len(bodies)


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


# --------------------------------------------------------------------------
# ★ 속도와 회복력 (2026-08-05)
#   실사용에서 40초가 걸렸고, 속도를 재려다 429로 API가 막혀 앱이 죽었다.
# --------------------------------------------------------------------------


def test_페이지_수를_요청값이_아니라_실제_응답으로_센다(monkeypatch):
    """★ 서버는 numOfRows를 1000으로 줘도 **100행만** 준다.
    요청값으로 페이지를 세면 '18페이지'인 줄 알고 174번 호출한다 —
    '왜 40초나 걸리지'의 답을 못 찾은 이유가 이것이었다."""
    calls: list[int] = []

    def fake(op, params, *, service_key, rows, page):
        calls.append(page)
        return [{"i": page * 100 + n} for n in range(100)], 350  # 총 350행, 100행씩

    monkeypatch.setattr(hub, "_call_page", fake)
    out = hub.call_all("op", {}, page_size=1000)
    assert len(out) == 350
    assert sorted(calls) == [1, 2, 3, 4], f"페이지를 잘못 셌다: {sorted(calls)}"


def test_429는_물러섰다_다시_시도한다(monkeypatch):
    """한 번 막히면 사용자는 아예 못 쓴다. 재시도가 없으면 그대로 화면에 뜬다."""
    import urllib.error

    tries = {"n": 0}

    def flaky(req, timeout=None):
        tries["n"] += 1
        if tries["n"] < 3:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, None)

        class R:
            def read(self):
                return b'{"response":{"body":{"totalCount":0,"items":""}}}'
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        return R()

    monkeypatch.setattr(hub.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(hub.time, "sleep", lambda s: None)  # 테스트는 기다리지 않는다
    raw = hub._get_with_retry("https://example.invalid/x", "op")
    assert tries["n"] == 3
    assert "totalCount" in raw


def test_계속_429면_직접입력을_안내하며_멈춘다(monkeypatch):
    import urllib.error

    def always429(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, None)

    monkeypatch.setattr(hub.urllib.request, "urlopen", always429)
    monkeypatch.setattr(hub.time, "sleep", lambda s: None)
    with pytest.raises(hub.RateLimited) as exc:
        hub._get_with_retry("https://example.invalid/x", "getBrHsprcInfo")
    # 사용자가 다음에 뭘 해야 하는지가 메시지에 있어야 한다
    assert "직접 입력" in str(exc.value)


def test_진행률_콜백이_페이지마다_불린다(monkeypatch):
    """40초짜리 조회에 진행 표시가 없으면 사용자는 멈춘 줄 안다."""
    def fake(op, params, *, service_key, rows, page):
        return [{"i": n} for n in range(100)], 500

    monkeypatch.setattr(hub, "_call_page", fake)
    seen: list[tuple[int, int]] = []
    hub.call_all("op", {}, progress=lambda d, t: seen.append((d, t)))
    assert len(seen) == 5
    assert seen[-1] == (5, 5)
