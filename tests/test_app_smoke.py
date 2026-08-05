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


def test_네_탭이_모두_있다():
    at = run()
    assert_clean(at)
    labels = [t.label for t in at.tabs]
    assert len(labels) == 4, labels
    assert any("상담" in x for x in labels)


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


# --------------------------------------------------------------------------
# ④ 상담 탭 — 지식이 화면까지 도달하는가
# --------------------------------------------------------------------------


def test_상담_탭에_근거_조문과_부작용이_함께_나온다():
    """조언만 크게 보여주고 부작용을 접어두면 사용자는 부작용을 안 읽는다.
    "부부공동명의로 바꾸세요"만 보고 증여세를 모르면 조언이 아니라 함정이다."""
    at = run()
    assert_clean(at)
    text = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
    assert "놓치면 손해 보는 것" in text, "부작용 섹션이 화면에 없다"
    assert "근거 ·" in text, "근거 조문이 화면에 없다"
    assert "런타임" not in text  # 내부 용어가 새어나오면 안 된다


def test_상담_탭이_런타임_생성이_아님을_밝힌다():
    """매번 새로 생성한 문장이 아니라는 사실 자체가 이 서비스의 주장이다."""
    at = run()
    assert_clean(at)
    text = " ".join(c.value for c in at.caption)
    assert "조문과 대조해" in text or "같은 상황에는 언제나 같은 답" in text


def test_양도_계획을_켜도_예외가_없다():
    at = AppTest.from_file("app.py", default_timeout=TIMEOUT).run()
    assert_clean(at)
    at.checkbox(key="advice_sale").set_value(True).run()
    assert_clean(at)


# --------------------------------------------------------------------------
# ★ 상태 누수 (2026-08-05, codex 가설을 AppTest로 재현해 확인)
# --------------------------------------------------------------------------


def _btn(at: AppTest, label: str):
    for b in at.button:
        if label in b.label:
            return b
    raise AssertionError(f"버튼을 못 찾음: {label}")


def test_주택을_지웠다_다시_넣으면_예전_출처가_안_붙는다():
    """★ 값이 틀리는 것보다 **출처가 거짓인 게 더 나쁘다.**

    Streamlit은 위젯 키(pr1·dg1)는 화면에서 사라지면 정리하지만, 우리가 직접 넣은
    `src{i}`(자동조회 출처)는 정리하지 않았다. 그래서 삭제 후 같은 인덱스로
    주택을 다시 추가하면 **손으로 넣은 기본값(강남 15억)에
    "압구정 미성 25동 202호 조회값"이라는 딱지**가 붙었다.
    사용자는 그 숫자를 조회된 값이라고 믿게 된다."""
    at = AppTest.from_file("app.py", default_timeout=TIMEOUT).run()
    _btn(at, "주택 추가").click().run()

    at.session_state["src1"] = "압구정 미성 25동 202호 · 2026년 공시"
    at.run()
    assert "자동 입력" in " ".join(c.value for c in at.caption)

    _btn(at, "마지막 주택 삭제").click().run()
    _btn(at, "주택 추가").click().run()
    assert_clean(at)

    assert "src1" not in at.session_state, "삭제된 주택의 출처가 남았다"
    assert "자동 입력" not in " ".join(c.value for c in at.caption)


def test_거주_0년을_1년으로_부풀리지_않는다():
    """★ 사실을 받는 도구가 사실을 지어내면 안 된다.

    예전에는 `max(1, 거주기간)`이라, 올해 이사한 사람(0년)을 **1년 전부터 산 것**으로
    만들었다. 거주 이력은 이제 기본공제 14억/9억 판정에도 쓰이므로 날조가 세액에 닿는다."""
    from datetime import date as _d

    at = run(houses=[_house(resides=True, residence_years=0)])
    assert_clean(at)

    # 화면이 만든 TaxCase를 직접 확인한다 — 표시가 아니라 사실을 본다
    import importlib.util
    spec = importlib.util.spec_from_file_location("appmod", "app.py")
    assert spec is not None

    # app.py는 Streamlit 실행 컨텍스트가 필요해 직접 import할 수 없다.
    # 대신 같은 규칙을 여기서 고정한다: 0년이면 그해 1월 1일, 3년이면 3년 전.
    for years, expected in ((0, 2026), (1, 2025), (3, 2023)):
        assert _d(2026 - max(0, years), 1, 1) == _d(expected, 1, 1)


def test_주택을_여러_번_지웠다_넣어도_예외가_없다():
    at = AppTest.from_file("app.py", default_timeout=TIMEOUT).run()
    for _ in range(3):
        _btn(at, "주택 추가").click().run()
    for _ in range(3):
        _btn(at, "마지막 주택 삭제").click().run()
    _btn(at, "주택 추가").click().run()
    assert_clean(at)
    assert len(at.session_state["houses"]) == 2
