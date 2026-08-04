"""주소로 공시가격 찾기 — 화면 조각.

계산은 없다. 두 공공 API를 부르고, 결과를 사용자가 **고칠 수 있는 형태로** 넘긴다.

★ 왜 단계를 쪼갰나 (2026-08-04 실측, 940호 단지)
      표제부       0.2초   ← 단지 확인, 동 목록
      전유부(동별) 2.7초   ← 그 동의 호 목록
      주택가격    41.0초   ← 단지 전건. 필터가 안 먹고 서버가 1,000행 상한

  한 번에 다 부르면 첫 화면이 70초 멈춘다. 그래서 사용자가 실제로 필요해진
  시점까지 비싼 조회를 미루고, 미룬 것은 캐시해 두 번 부르지 않는다.

★ 자동조회는 **참고값**이지 정본이 아니다
  건축물대장 주택가격이 공동주택공시가격과 언제나 같다는 보장은 검증되지 않았다.
  그래서 조회에 성공해도 입력칸은 늘 열려 있고, 값을 덮어쓸 때 출처를 남긴다.
"""

from __future__ import annotations

import os

import streamlit as st

from realestate_tax.sources import building_hub as hub
from realestate_tax.sources import juso
from realestate_tax.sources import resolve

DAY = 60 * 60 * 24

SETUP_GUIDE_KO = """
자동조회를 쓰려면 무료 인증키 2개가 필요합니다. 둘 다 **즉시 발급**입니다.

| 키 | 발급처 | 환경변수 |
|---|---|---|
| 도로명주소 검색 | [business.juso.go.kr](https://business.juso.go.kr) → 주소정보 API 연계 | `JUSO_CONFM_KEY` |
| 건축물대장 | [data.go.kr 15134735](https://www.data.go.kr/data/15134735/openapi.do) | `DATA_GO_KR_KEY` |

키가 없어도 **공시가격을 직접 입력하면 계산은 그대로 됩니다.**
"""


def keys_ready() -> tuple[bool, list[str]]:
    missing = [k for k in ("JUSO_CONFM_KEY", "DATA_GO_KR_KEY") if not os.environ.get(k)]
    return (not missing, missing)


# --------------------------------------------------------------------------
# 캐시 — 같은 단지를 두 번 묻지 않는다
# --------------------------------------------------------------------------


@st.cache_data(ttl=DAY, show_spinner=False, max_entries=200)
def _search(keyword: str) -> tuple[juso.AddressMatch, ...]:
    return juso.search_address(keyword, per_page=10)


@st.cache_data(ttl=DAY, show_spinner=False, max_entries=200)
def _probe(address: juso.AddressMatch) -> resolve.ParcelProbe:
    return resolve.probe_address(address)


@st.cache_data(ttl=DAY, show_spinner=False, max_entries=200)
def _units(parcel: hub.ParcelKey, dong_nm: str) -> tuple[hub.Unit, ...]:
    return resolve.units_in_dong(parcel, dong_nm)


@st.cache_data(ttl=DAY, show_spinner=False, max_entries=50)
def _prices(parcel: hub.ParcelKey) -> tuple[hub.HousePrice, ...]:
    """단지 전건. 41초짜리라 캐시가 핵심이다 — 같은 단지의 다음 호는 즉시 나온다."""
    return hub.fetch_prices(parcel)


def price_of(
    prices: tuple[hub.HousePrice, ...], mgm_pk: str, year: int
) -> tuple[int, int] | None:
    """(금액, 공시기준연도). 요청 연도가 없으면 **가장 최근 것**으로 내리고 연도를 함께 준다.

    조용히 다른 해 값을 쓰면 사용자는 그게 올해 값인 줄 안다. 연도를 같이 돌려주는
    이유가 그것이다 — 화면이 "2026년 공시"라고 밝힐 수 있어야 한다.
    """
    mine = [p for p in prices if p.mgm_pk == mgm_pk and p.year]
    if not mine:
        return None
    exact = [p for p in mine if p.year == year]
    best = max(exact or mine, key=lambda p: p.year or 0)
    return (best.price, best.year or 0)


# --------------------------------------------------------------------------
# 위젯
# --------------------------------------------------------------------------


def picker(idx: int, house: dict, year: int) -> None:
    """주소 → 동 → 호 → 공시가격. `house`를 직접 고친다.

    성공하든 실패하든 **수동 입력 경로를 막지 않는다.** 자동조회는 편의지 관문이 아니다.
    """
    ready, missing = keys_ready()
    if not ready:
        with st.expander("주소로 공시가격 찾기 — 인증키가 없습니다"):
            st.caption(f"환경변수 미설정: {', '.join(missing)}")
            st.markdown(SETUP_GUIDE_KO)
        return

    with st.expander("주소로 공시가격 찾기", expanded=False):
        st.caption(
            "국토교통부 건축물대장의 **주택가격**입니다. 공동주택공시가격과 대개 같지만 "
            "언제나 같다는 보장은 확인되지 않았습니다 — 고지서와 다르면 고지서를 쓰세요."
        )

        q = st.text_input(
            "도로명주소 또는 아파트 이름",
            key=f"adr_q{idx}",
            placeholder="예) 압구정로 113   /   압구정동 397   /   미성아파트",
            help="지번주소·건물명으로도 찾습니다.",
        )
        if not q or len(q.strip()) < 2:
            return

        try:
            with st.spinner("주소를 찾는 중…"):
                matches = _search(q.strip())
        except juso.UnsafeKeyword as exc:
            st.warning(str(exc))
            return
        except juso.JusoError as exc:
            st.error(f"주소 검색에 실패했습니다. 직접 입력해주세요.\n\n{exc}")
            return

        if not matches:
            st.info("검색 결과가 없습니다. 도로명주소나 아파트 이름으로 다시 시도해보세요.")
            return

        labels = [m.label_ko for m in matches]
        pick = st.selectbox("주소 선택", range(len(matches)), format_func=lambda i: labels[i],
                            key=f"adr_sel{idx}")
        match = matches[pick]

        with st.spinner("건축물대장을 확인하는 중…"):
            probe = _probe(match)

        if not probe.ok or probe.parcel is None:
            st.warning(probe.message_ko())
            _apply_dong_only(idx, house, match)
            return
        if not probe.has_house:
            st.warning(probe.message_ko())
            return

        st.success(f"{probe.message_ko()}  ·  지번 {probe.hint.label_ko if probe.hint else ''}")

        dongs = probe.dong_names
        dong = ""
        if dongs:
            dong = st.selectbox("동", dongs, key=f"adr_dong{idx}")

        with st.spinner(f"{dong or '건물'}의 호 목록을 가져오는 중…"):
            try:
                units = _units(probe.parcel, dong)
            except Exception as exc:
                st.error(f"호 목록 조회에 실패했습니다. 직접 입력해주세요.\n\n{exc}")
                return

        if not units:
            st.warning("이 동의 호 정보가 없습니다. 공시가격을 직접 입력해주세요.")
            _apply_dong_only(idx, house, match)
            return

        ho_labels = [
            f"{u.ho_nm}" + (f"  ({u.area_m2:.2f}㎡)" if u.area_m2 else "") for u in units
        ]
        hi = st.selectbox("호", range(len(units)), format_func=lambda i: ho_labels[i],
                          key=f"adr_ho{idx}")
        unit = units[hi]

        st.caption(
            "다음 단계는 단지 전체의 공시가격 이력을 한 번 읽습니다(약 40초). "
            "같은 단지의 다른 호는 그 뒤로 즉시 나옵니다."
        )
        if not st.button("이 호의 공시가격 가져오기", key=f"adr_go{idx}", type="primary"):
            return

        with st.spinner("공시가격을 가져오는 중… (단지 전체 이력, 약 40초)"):
            try:
                prices = _prices(probe.parcel)
            except Exception as exc:
                st.error(f"공시가격 조회에 실패했습니다. 직접 입력해주세요.\n\n{exc}")
                return

        found = price_of(prices, unit.mgm_pk, year)
        if found is None:
            st.warning(
                f"{unit.ho_nm}의 공시가격이 대장에 없습니다. 신축이거나 공시 전일 수 있습니다 — "
                "직접 입력해주세요."
            )
            _apply_dong_only(idx, house, match)
            return

        amount, base_year = found
        _apply(idx, house, match, amount)
        note = f"{probe.complex_name} {dong}동 {unit.ho_nm} · {base_year}년 공시"
        if base_year != year:
            note += f" (요청하신 {year}년 값이 아직 없어 최신 공시를 넣었습니다)"
        st.session_state[f"src{idx}"] = note
        st.rerun()


def _apply(idx: int, house: dict, match: juso.AddressMatch, price: int) -> None:
    """공시가격과 법정동코드를 함께 채운다.

    법정동코드까지 넣는 이유: 조정대상지역 판정이 여기 걸린다. 주소는 맞는데
    코드가 기본값으로 남아 있으면 엉뚱한 지역으로 판정된다.
    """
    house["price"] = price
    house["dong"] = match.legal_dong_code
    st.session_state[f"pr{idx}"] = f"{price:,}"
    st.session_state[f"dg{idx}"] = match.legal_dong_code


def _apply_dong_only(idx: int, house: dict, match: juso.AddressMatch) -> None:
    """가격은 못 얻었어도 법정동코드는 쓸 수 있다. 조정대상지역 판정만이라도 정확해진다."""
    house["dong"] = match.legal_dong_code
    st.session_state[f"dg{idx}"] = match.legal_dong_code
