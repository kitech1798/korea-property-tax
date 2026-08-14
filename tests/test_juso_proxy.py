"""서울 리전 중계(`api/juso.js`)를 타는 경로.

★ 왜 중계인가 — **juso가 해외 IP에 응답하지 않는다**(2026-08-13 실측:
  한국 회선 0.3초 정상 / 미국 AWS 20초 타임아웃). 앱은 해외 PaaS에 있다.
  앱을 국내로 옮기는 대신 중계만 서울(`icn1`)에 둔다.

여기서 못박는 것은 셋이다.
  ① 중계를 쓰면 **승인키를 보내지 않는다** — 키는 중계에만 있다.
  ② 중계에 **경로 이름만** 넘긴다 — 전체 URL을 넘기면 SSRF 도구가 된다.
  ③ URL과 토큰이 **둘 다** 있어야 중계를 쓴다 — 토큰 없이 부르면 401이고,
     그걸 네트워크 오류로 오해하면 원인을 못 찾는다.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from realestate_tax.sources import juso

SEARCH_OK = {
    "results": {
        "common": {"errorCode": "0", "errorMessage": "정상", "totalCount": "1"},
        "juso": [{
            "roadAddr": "충청남도 천안시 동남구 북일로 70",
            "jibunAddr": "충청남도 천안시 동남구 신부동 978",
            "admCd": "4413111800",
            "lnbrMnnm": "978",
            "lnbrSlno": "0",
            "mtYn": "0",
            "bdMgtSn": "4413111800109780000000001",
            "bdNm": "",
            "zipNo": "31119",
            "detBdNmList": "",
        }],
    }
}


class _Resp:
    def __init__(self, body: str):
        self._b = body.encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _no_local_secrets(monkeypatch):
    """★ 로컬 `.streamlit/secrets.toml`이 테스트에 새어 들어오지 않게 막는다.

    `_setting()`은 환경변수가 비면 **secrets로 폴백한다.** 그래서 환경변수만
    지우고 "설정 없음"을 시험하면, 개발자 PC에 실제 값이 있는 순간 테스트가
    조용히 다른 것을 재게 된다 — 실제로 이 파일이 그렇게 깨졌다(2026-08-13,
    중계 토큰을 secrets.toml에 넣은 직후).

    이 저장소는 같은 착각을 이미 두 번 박아 뒀다
    (`test_환경변수만_지우면_키_없음이_되지_않는다`). **두 통로를 다 막아야 한다.**
    """
    import streamlit as st

    monkeypatch.setattr(st, "secrets", {}, raising=False)


@pytest.fixture
def capture(monkeypatch):
    """호출된 URL과 헤더를 잡아 둔다."""
    seen: dict = {}

    def fake(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.headers)
        return _Resp(json.dumps(SEARCH_OK, ensure_ascii=False))

    monkeypatch.setattr(juso.urllib.request, "urlopen", fake)
    return seen


def _proxy_on(monkeypatch, url="https://p.example/api/juso", token="t0ken"):
    monkeypatch.setenv("JUSO_PROXY_URL", url)
    monkeypatch.setenv("JUSO_PROXY_TOKEN", token)


def _proxy_off(monkeypatch):
    monkeypatch.delenv("JUSO_PROXY_URL", raising=False)
    monkeypatch.delenv("JUSO_PROXY_TOKEN", raising=False)


# --------------------------------------------------------------------------
# ① 승인키를 중계로 보내지 않는다
# --------------------------------------------------------------------------


def test_중계를_쓰면_승인키를_보내지_않는다(monkeypatch, capture):
    """키가 두 곳에 있으면 두 곳에서 샌다. 중계에만 둔다."""
    _proxy_on(monkeypatch)
    monkeypatch.setenv("JUSO_CONFM_KEY", "SHOULD_NOT_BE_SENT")
    juso.search_address("북일로 70")
    assert "confmKey" not in capture["url"]
    assert "SHOULD_NOT_BE_SENT" not in capture["url"]


def test_중계가_없으면_승인키를_직접_붙인다(monkeypatch, capture):
    _proxy_off(monkeypatch)
    monkeypatch.setenv("JUSO_CONFM_KEY", "DIRECT_KEY")
    juso.search_address("북일로 70")
    assert "business.juso.go.kr" in capture["url"]
    assert "confmKey=DIRECT_KEY" in capture["url"]


def test_중계를_쓰면_승인키가_없어도_된다(monkeypatch, capture):
    """키는 중계에만 있다. 여기서 키를 요구하면 설정을 두 번 하게 된다."""
    _proxy_on(monkeypatch)
    monkeypatch.delenv("JUSO_CONFM_KEY", raising=False)
    juso.search_address("북일로 70")  # NO_KEY로 죽으면 안 된다
    assert capture["url"].startswith("https://p.example/api/juso?")


# --------------------------------------------------------------------------
# ② 경로 이름만 넘긴다 (SSRF 차단)
# --------------------------------------------------------------------------


def test_중계에는_전체_URL이_아니라_경로_이름만_넘긴다(monkeypatch, capture):
    """전체 URL을 넘기면 중계가 **아무 데나 요청을 보내주는 도구**가 된다."""
    _proxy_on(monkeypatch)
    juso.search_address("북일로 70")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(capture["url"]).query)
    assert q["path"] == ["addrLinkApi.do"]
    assert "business.juso.go.kr" not in capture["url"]
    assert "http" not in q["path"][0]


def test_토큰을_헤더로_보낸다(monkeypatch, capture):
    """공개 URL이라 막지 않으면 남의 할당량을 쓴다. 쿼리가 아니라 헤더로 보낸다 —
    쿼리는 중계 로그·리퍼러에 남는다."""
    _proxy_on(monkeypatch, token="s3cret")
    juso.search_address("북일로 70")
    headers = {k.lower(): v for k, v in capture["headers"].items()}
    assert headers.get("x-proxy-token") == "s3cret"
    assert "s3cret" not in capture["url"]


# --------------------------------------------------------------------------
# ③ 둘 다 있어야 쓴다
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, token",
    [("https://p.example/api/juso", ""), ("", "t0ken"), ("", "")],
)
def test_URL과_토큰_중_하나만_있으면_중계를_쓰지_않는다(monkeypatch, capture, url, token):
    """토큰 없이 부르면 중계가 401을 준다. 그걸 '네트워크 오류'로 보면
    사용자가 juso를 의심하게 되고, 원인은 영원히 안 나온다."""
    monkeypatch.setenv("JUSO_PROXY_URL", url)
    monkeypatch.setenv("JUSO_PROXY_TOKEN", token)
    monkeypatch.setenv("JUSO_CONFM_KEY", "DIRECT_KEY")
    assert juso.proxy_config() is None
    juso.search_address("북일로 70")
    assert "business.juso.go.kr" in capture["url"]


def test_중계_응답도_같은_파서를_탄다(monkeypatch, capture):
    """중계는 원문을 그대로 넘긴다. 중계 쪽에서 손대면 파싱이 어긋난다."""
    _proxy_on(monkeypatch)
    got = juso.search_address("북일로 70")
    assert len(got) == 1
    assert got[0].legal_dong_code == "4413111800"
    assert got[0].lnbr_mnnm == 978
