"""법정동명 → 법정동코드 (`apis.data.go.kr/1741000`).

★ 이 모듈이 왜 있나 — **juso가 해외 IP에 응답하지 않는다**(2026-08-13 실측).

  business.juso.go.kr   로컬 0.3초 정상 / 배포 서버(미국 AWS) 20초 타임아웃
  apis.data.go.kr       같은 배포 서버에서 정상

  주소 자동조회는 juso가 주는 **법정동코드 + 지번**에서 시작한다. juso가 죽으면
  사슬 전체가 죽는데, 두 값 중 법정동코드는 **도달 가능한 호스트**에서 받을 수 있다.
  지번은 사용자가 안다 — 등기부·계약서에 적혀 있다.

  편의 하나(주소 한 줄)를 잃되 사슬 전체를 잃지는 않는다.
"""

from __future__ import annotations

import json

import pytest

from realestate_tax.sources import region_code as rc

SAMPLE = {
    "StanReginCd": [
        {"head": [{"totalCount": "2"}]},
        {
            "row": [
                {"region_cd": "1168010600", "locatadd_nm": "서울특별시 강남구 대치동"},
                {"region_cd": "4413111800", "locatadd_nm": "충청남도 천안시 동남구 신부동"},
            ]
        },
    ]
}


def test_응답에서_코드와_이름을_뽑는다():
    got = rc._parse(SAMPLE)
    assert [m.code for m in got] == ["1168010600", "4413111800"]
    assert got[0].name == "서울특별시 강남구 대치동"


def test_10자리가_아닌_코드는_버린다():
    """폐지된 동이나 상위 행정구역이 섞여 온다. 법정동코드는 **10자리**여야
    이 엔진의 지역 판정 키로 쓸 수 있다."""
    doc = {"StanReginCd": [{}, {"row": [
        {"region_cd": "1168000000", "locatadd_nm": "서울특별시 강남구"},   # 10자리 — 살린다
        {"region_cd": "11680", "locatadd_nm": "강남구"},                  # 5자리 — 버린다
        {"region_cd": "", "locatadd_nm": "이름만"},
    ]}]}
    got = rc._parse(doc)
    assert [m.code for m in got] == ["1168000000"]


def test_같은_코드가_여러_번_와도_한_번만():
    doc = {"StanReginCd": [{}, {"row": [
        {"region_cd": "1168010600", "locatadd_nm": "서울특별시 강남구 대치동"},
        {"region_cd": "1168010600", "locatadd_nm": "서울특별시 강남구 대치동"},
    ]}]}
    assert len(rc._parse(doc)) == 1


def test_결과가_없으면_빈_목록():
    """결과가 없을 때는 `StanReginCd`가 아예 안 온다. 그걸 오류로 다루면
    '검색 결과 없음'과 '조회 실패'가 섞인다."""
    assert rc._parse({"RESULT": {"resultCode": "INFO-200"}}) == []


@pytest.mark.parametrize("kw", ["", " ", "대"])
def test_두_글자_미만은_호출하지_않는다(kw, monkeypatch):
    """한 글자로 부르면 전국이 걸린다. 네트워크를 아끼는 것보다,
    **의미 없는 결과를 사용자에게 보여주지 않는 것**이 목적이다."""
    def boom(*a, **k):
        raise AssertionError("호출하면 안 된다")
    monkeypatch.setattr(rc.urllib.request, "urlopen", boom)
    assert rc.search_dong(kw) == []


# --------------------------------------------------------------------------
# 오류 구분 — 사용자가 할 일이 다르다
# --------------------------------------------------------------------------


class _Resp:
    def __init__(self, body: str):
        self._b = body.encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_403은_허용IP_문제라고_말한다(monkeypatch):
    """★ 이 함정을 실제로 겪었다 — 1741000 계열은 활용신청의 허용 IP가
    `*.*.*.*`가 아니면 배포 서버에서 403이다.

    "네트워크 오류"로 뭉개면 사용자가 엉뚱한 데를 고친다.
    """
    import urllib.error

    def raise403(*a, **k):
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

    monkeypatch.setattr(rc, "_key", lambda: "k")
    monkeypatch.setattr(rc.urllib.request, "urlopen", raise403)
    with pytest.raises(rc.RegionCodeError) as e:
        rc.search_dong("대치동")
    assert e.value.code == "FORBIDDEN"
    assert "*.*.*.*" in str(e.value)


def test_빈_응답은_일시적_문제로_말한다(monkeypatch):
    monkeypatch.setattr(rc, "_key", lambda: "k")
    monkeypatch.setattr(rc.urllib.request, "urlopen", lambda *a, **k: _Resp(""))
    with pytest.raises(rc.RegionCodeError) as e:
        rc.search_dong("대치동")
    assert e.value.code == "EMPTY"


def test_키가_없으면_그렇게_말한다(monkeypatch):
    """⚠️ 환경변수만 지우면 '키 없음'이 되지 않는다 — `_key()`가 `st.secrets`로
    폴백한다. 이 저장소는 같은 착각을 이미 한 번 테스트로 박아 뒀다
    (`test_환경변수만_지우면_키_없음이_되지_않는다`). 두 통로를 다 막아야 한다."""
    import streamlit as st

    monkeypatch.setattr(rc.os, "environ", {})
    monkeypatch.setattr(st, "secrets", {}, raising=False)
    with pytest.raises(rc.RegionCodeError) as e:
        rc.search_dong("대치동")
    assert e.value.code == "NO_KEY"


def test_정상_응답은_그대로_파싱된다(monkeypatch):
    monkeypatch.setattr(rc, "_key", lambda: "k")
    monkeypatch.setattr(
        rc.urllib.request, "urlopen",
        lambda *a, **k: _Resp(json.dumps(SAMPLE, ensure_ascii=False)),
    )
    got = rc.search_dong("대치동")
    assert len(got) == 2
    assert got[0].label_ko.startswith("서울특별시 강남구 대치동")
