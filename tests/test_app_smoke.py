"""Streamlit 화면 스모크 테스트.

서버가 뜬 것과 화면이 그려지는 것은 다르다. `streamlit run`은 스크립트 예외가 나도
프로세스가 살아 있고 health 엔드포인트는 ok를 돌려준다. 그래서 실제로 스크립트를
실행해 예외 유무를 확인해야 한다.

AppTest는 브라우저 없이 앱 스크립트를 돌리고 예외를 수집한다.
"""

from __future__ import annotations

import pytest

pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

TIMEOUT = 60


def run(**session_state) -> AppTest:
    at = AppTest.from_file("app.py", default_timeout=TIMEOUT)
    for k, v in session_state.items():
        at.session_state[k] = v
    return at.run()


def assert_clean(at: AppTest) -> None:
    if at.exception:
        raise AssertionError(
            "화면 렌더링 중 예외:\n"
            + "\n".join(f"  {e.message}\n{e.stack_trace}" for e in at.exception)
        )


def test_기본_화면이_예외_없이_그려진다():
    assert_clean(run())


def test_핵심_문구가_화면에_있다():
    at = run()
    assert_clean(at)
    text = " ".join(m.value for m in at.markdown)
    # 이 서비스의 주장이 화면에 실제로 있는지
    assert "입력하지 않습니다" in text
    assert "근거 조문" in text or "계산 근거" in text


def test_세_탭이_모두_있다():
    at = run()
    assert_clean(at)
    assert len(at.tabs) == 3


def test_개편안_트랙으로_바꿔도_예외가_없다():
    """위젯을 위치가 아니라 key로 찾는다. 위치로 찾으면 사이드바·본문 순서가
    바뀔 때 조용히 다른 위젯을 집어 테스트가 엉뚱한 걸 검증하게 된다."""
    at = AppTest.from_file("app.py", default_timeout=TIMEOUT).run()
    assert_clean(at)
    at.radio(key="track").set_value("2026 개편안").run()
    assert_clean(at)
    assert at.session_state["track"] == "2026 개편안"


@pytest.mark.parametrize("year", [2026, 2027, 2028, 2029])
def test_모든_과세연도에서_예외가_없다(year):
    at = AppTest.from_file("app.py", default_timeout=TIMEOUT).run()
    assert_clean(at)
    at.selectbox(key="year").set_value(year).run()
    assert_clean(at)
    assert at.session_state["year"] == year


@pytest.mark.parametrize("year", [2027, 2028, 2029])
def test_개편안_트랙_전_연도에서_예외가_없다(year):
    """개편안은 연도마다 세율·공제가 달라 조합마다 다른 룰셋 블록을 탄다."""
    at = AppTest.from_file("app.py", default_timeout=TIMEOUT).run()
    at.radio(key="track").set_value("2026 개편안").run()
    at.selectbox(key="year").set_value(year).run()
    assert_clean(at)


def _house(**over) -> dict:
    base = {
        "name": "우리집",
        "dong": "1168010100",
        "price": 1_500_000_000,
        "share": "단독",
        "resides": True,
        "residence_years": 10,
        "holding_years": 10,
        "acquired": __import__("datetime").date(2016, 3, 1),
        "cause": "매매",
        "inheritance_date": __import__("datetime").date(2024, 1, 1),
        "inherited_share": 100,
        "rental": False,
        "rental_declared": False,
        "urban": True,
    }
    base.update(over)
    return base


def test_다주택_상황도_예외_없이_계산된다():
    at = run(
        houses=[
            _house(name="강남집", dong="1168010100", price=2_000_000_000),
            _house(name="부산집", dong="2635010300", price=800_000_000, resides=False),
        ]
    )
    assert_clean(at)


def test_부부공동명의_1주택도_예외_없이_계산된다():
    at = run(houses=[_house(share="부부 공동 1/2")])
    assert_clean(at)


def test_상속주택이_섞여도_예외가_없다():
    at = run(
        houses=[
            _house(name="본가"),
            _house(
                name="상속집",
                cause="상속",
                price=600_000_000,
                resides=False,
                inherited_share=40,
            ),
        ]
    )
    assert_clean(at)


def test_합산배제_임대주택도_예외가_없다():
    at = run(
        houses=[
            _house(name="본가"),
            _house(name="임대집", rental=True, rental_declared=True, resides=False),
        ]
    )
    assert_clean(at)


def test_판정_불가_지역_코드를_넣어도_화면이_버틴다():
    """화성시처럼 코드가 미확정인 지역. 예외로 죽지 않고 안내가 떠야 한다."""
    at = run(houses=[_house(dong="4159000000")])
    assert_clean(at)
    text = " ".join(m.value for m in at.markdown) + " ".join(
        c.value for c in at.caption
    )
    assert "판정" in text


def test_공시가격을_이상하게_입력해도_죽지_않는다():
    """0원·미상 입력에서 예외가 나면 사용자는 아무것도 못 본다."""
    at = run(houses=[_house(price=1)])
    assert_clean(at)


def test_면책_문구가_항상_노출된다():
    at = run()
    assert_clean(at)
    captions = " ".join(c.value for c in at.caption)
    assert "참고용 추정치" in captions
    assert "국회 통과 전" in captions


# --------------------------------------------------------------------------
# 주소 자동조회 — 있어도 없어도 화면이 산다
# --------------------------------------------------------------------------


def test_인증키가_없어도_화면이_그려지고_직접입력이_살아_있다(monkeypatch):
    """자동조회는 편의지 관문이 아니다. 키가 없다고 계산을 막으면 안 된다."""
    monkeypatch.delenv("JUSO_CONFM_KEY", raising=False)
    monkeypatch.delenv("DATA_GO_KR_KEY", raising=False)
    at = run()
    assert_clean(at)
    assert any("공시가격" in i.label for i in at.text_input)
    text = " ".join(c.value for c in at.caption) + " ".join(m.value for m in at.markdown)
    assert "직접 입력하면 계산은 그대로" in text


def test_인증키가_있으면_주소_검색칸이_뜬다(monkeypatch):
    """빈 검색어에서는 API를 부르지 않는다 — 화면 진입만으로 쿼터를 쓰면 안 된다."""
    monkeypatch.setenv("JUSO_CONFM_KEY", "DUMMY")
    monkeypatch.setenv("DATA_GO_KR_KEY", "DUMMY")
    at = run()
    assert_clean(at)
    assert any("도로명주소" in i.label for i in at.text_input)


def test_주소_검색이_실패해도_화면이_죽지_않는다(monkeypatch):
    """외부 API는 언젠가 죽는다. 그때 사용자가 보는 것은 빈 화면이 아니라 안내여야 한다."""
    monkeypatch.setenv("JUSO_CONFM_KEY", "DUMMY")
    monkeypatch.setenv("DATA_GO_KR_KEY", "DUMMY")
    at = AppTest.from_file("app.py", default_timeout=TIMEOUT).run()
    assert_clean(at)
    at.text_input(key="adr_q0").set_value("압구정로 113").run()
    assert_clean(at)  # 가짜 키라 호출이 실패하지만 예외로 죽으면 안 된다
    text = " ".join(e.value for e in at.error) + " ".join(w.value for w in at.warning)
    assert "직접 입력" in text
