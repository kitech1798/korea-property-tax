"""주소로 공시가격 찾기 — 화면 조각.

계산은 없다. 두 공공 API를 부르고, 결과를 사용자가 **고칠 수 있는 형태로** 넘긴다.

★ 왜 단계를 쪼갰나 (2026-08-04 실측, 940호 단지)
      표제부       0.2초   ← 단지 확인, 동 목록
      전유부(동별) 2.7초   ← 그 동의 호 목록
      주택가격    10.9초   ← 단지 전건(174페이지를 동시에). 원래 41초였다.

  한 번에 다 부르면 첫 화면이 멈춘다. 그래서 사용자가 실제로 필요해진
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
from realestate_tax.sources import region_code
from realestate_tax.sources import resolve

DAY = 60 * 60 * 24

ISSUERS = {
    "JUSO_CONFM_KEY": (
        "도로명주소 검색",
        "https://business.juso.go.kr",
        "business.juso.go.kr → 주소정보 API 연계 → **도로명주소 검색 API**",
    ),
    "DATA_GO_KR_KEY": (
        "건축물대장",
        "https://www.data.go.kr/data/15134735/openapi.do",
        "data.go.kr 15134735 → 활용신청(자동승인)",
    ),
}

FALLBACK_KO = "키가 없어도 **공시가격을 직접 입력하면 계산은 그대로 됩니다.**"


def is_cloud() -> bool:
    """배포 환경인가.

    ★ 안내 문구가 환경마다 달라야 한다. "인증키가 없습니다"만 띄우면,
      **키를 이미 발급받은 사람**은 "다 받았는데 왜 안 되지?"에서 막힌다.
      실제로 그렇게 막혔다 — 키는 개발자 PC의 환경변수에 있었고,
      배포 서버는 다른 컴퓨터라 그 값을 볼 수 없었을 뿐이다.
      **발급과 전달은 다른 일이다.**
    """
    return bool(
        os.environ.get("STREAMLIT_RUNTIME_ENV")
        or os.environ.get("HOSTNAME", "").startswith("streamlit")
        or os.path.isdir("/mount/src")  # Streamlit Community Cloud의 소스 경로
    )


def setup_guide_ko(missing: list[str]) -> str:
    rows = "\n".join(
        f"| {ISSUERS[k][0]} | [{ISSUERS[k][2]}]({ISSUERS[k][1]}) | `{k}` |"
        for k in missing
    )
    table = "| 키 | 발급처 | 이름 |\n|---|---|---|\n" + rows

    if is_cloud():
        where = (
            "**이 앱은 배포 서버에서 돌고 있습니다.** 개인 PC의 환경변수는 여기까지 오지 않습니다.\n\n"
            "`share.streamlit.io` → 이 앱 → **⋮ → Settings → Secrets** 에 아래 형식으로 넣고 "
            "**Reboot app** 하세요.\n\n"
            "```toml\n"
            + "\n".join(f'{k} = "발급받은_키"' for k in missing)
            + "\n```\n"
        )
    else:
        where = (
            "**이 앱은 지금 이 PC에서 돌고 있습니다.** 환경변수를 설정한 뒤 "
            "**터미널을 새로 열어** 다시 실행하세요 — 이미 떠 있던 창은 예전 환경을 그대로 씁니다.\n\n"
            "```powershell\n"
            + "\n".join(
                f'[Environment]::SetEnvironmentVariable("{k}", "발급받은_키", "User")'
                for k in missing
            )
            + "\n```\n"
        )
    return f"{where}\n{table}\n\n{FALLBACK_KO}"


def keys_ready() -> tuple[bool, list[str]]:
    missing = [k for k in ISSUERS if not os.environ.get(k)]
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
def _prices(parcel: hub.ParcelKey, _progress=None) -> tuple[hub.HousePrice, ...]:
    """단지 전건. 무거운 조회라 캐시가 핵심이다 — 같은 단지의 다음 호는 즉시 나온다.

    `_progress`는 앞에 밑줄이 있어 캐시 키에서 제외된다(Streamlit 규약).
    콜백까지 키에 들어가면 매번 다른 함수 객체라 캐시가 절대 맞지 않는다.
    """
    return hub.fetch_prices(parcel, progress=_progress)


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


@st.cache_data(ttl=DAY, show_spinner=False, max_entries=200)
def _dongs(keyword: str) -> tuple[region_code.RegionMatch, ...]:
    return tuple(region_code.search_dong(keyword))


def _by_dong_and_parcel(idx: int, house: dict) -> None:
    """juso 없이 사슬을 잇는다 — **법정동은 API가, 지번은 사용자가.**

    ★ 2026-08-13. juso가 해외 IP에 응답하지 않아 도로명 검색이 죽었다. 그런데
      자동조회에 필요한 두 값 중 **법정동코드는 도달 가능한 호스트**
      (`apis.data.go.kr`)에서 받을 수 있고, **지번은 사용자가 안다** —
      등기부·계약서에 적혀 있다.

      그래서 편의 하나(주소 한 줄)를 잃되 **사슬 전체를 잃지는 않는다.**
      법정동코드만 채워도 조정대상지역 판정이 살아나고, 지번까지 넣으면
      동·호·공시가격이 그대로 자동으로 채워진다.
    """
    kw = st.text_input(
        "법정동 이름",
        key=f"adr_dq{idx}",
        placeholder="예) 대치동   /   천안시 동남구 신부동",
        help="**도로명은 찾지 못합니다.** 동 이름으로 넣어주세요. "
        "같은 이름이 여러 시·군에 있으면 시·군을 앞에 붙이시면 좁혀집니다.",
    )
    if not kw or len(kw.strip()) < 2:
        return

    try:
        with st.spinner("법정동을 찾는 중…"):
            found = _dongs(kw.strip())
    except region_code.RegionCodeError as exc:
        st.error(f"법정동 검색에 실패했습니다. 공시가격을 직접 입력해주세요.\n\n{exc}")
        return

    if not found:
        st.info(
            "검색 결과가 없습니다. **동 이름**으로 넣어주세요 — 도로명(예: 북일로)은 "
            "이 검색에서 찾지 못합니다."
        )
        return

    labels = [m.label_ko for m in found]
    pick = st.selectbox("법정동 선택", range(len(found)), format_func=lambda i: labels[i],
                        key=f"adr_dsel{idx}")
    region = found[pick]

    # 법정동코드만으로도 얻는 게 있다 — 조정대상지역 판정이 살아난다.
    house["dong"] = region.code
    house["src"] = f"법정동 검색 · {region.name}"
    st.success(f"법정동코드 {region.code} — {region.name}")

    c1, c2, c3 = st.columns([1, 1, 1])
    bun = c1.number_input("지번 본번", 0, 9999, 0, key=f"adr_bun{idx}",
                          help="등기부·계약서의 '○○○-△△'에서 앞 숫자입니다. 0이면 건너뜁니다.")
    ji = c2.number_input("부번", 0, 9999, 0, key=f"adr_ji{idx}",
                         help="'-' 뒤 숫자. 없으면 0.")
    mountain = c3.checkbox("산", key=f"adr_mt{idx}", help="'산 12-3'처럼 산으로 시작하면 체크")

    if bun <= 0:
        st.caption(
            "지번을 넣으시면 동·호·공시가격까지 자동으로 채웁니다. "
            "지금은 법정동코드만 반영됐습니다 — 공시가격은 아래에 직접 입력해주세요."
        )
        return

    # juso가 주던 것과 같은 모양으로 만들어 **같은 사슬**에 태운다.
    # 새 경로를 따로 파면 두 경로가 서로 다르게 굴러 유지가 안 된다.
    synthetic = juso.AddressMatch(
        road_addr=region.name,
        jibun_addr=f"{region.name} {int(bun)}" + (f"-{int(ji)}" if ji else ""),
        legal_dong_code=region.code,
        lnbr_mnnm=int(bun),
        lnbr_slno=int(ji),
        mt_yn="1" if mountain else "0",
    )
    _continue_from(idx, house, synthetic)


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
            st.markdown(setup_guide_ko(missing))
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
            # ⚠️ 2026-08-13 실측 — 배포 서버(해외 IP)에서 juso가 **응답하지 않는다.**
            #   로컬(한국 회선) 0.3초 정상 / Streamlit Cloud 20초 타임아웃.
            #   주소정보 누리집이 해외 IP를 막는 것으로 보인다.
            #
            #   이걸 "실패했습니다"로만 적으면 **일시적 오류처럼 읽혀** 사용자가
            #   계속 다시 누른다. 될 일이 아니면 될 일이 아니라고 말해야 한다.
            if exc.code == "NETWORK":
                st.info(
                    "**도로명주소 검색이 이 서버에서는 되지 않습니다.** "
                    "주소정보 누리집(juso.go.kr)이 해외에 있는 이 서버의 접속에 "
                    "응답하지 않습니다 — 앱이나 인증키 문제가 아닙니다.\n\n"
                    "대신 **법정동 + 지번**으로 찾겠습니다. 등기부·계약서에 적힌 "
                    "지번을 넣으시면 그 다음(동·호·공시가격)은 그대로 자동으로 채워집니다."
                )
                _by_dong_and_parcel(idx, house)
            else:
                st.error(f"주소 검색에 실패했습니다. 직접 입력해주세요.\n\n{exc}")
            return

        if not matches:
            st.info("검색 결과가 없습니다. 도로명주소나 아파트 이름으로 다시 시도해보세요.")
            return

        labels = [m.label_ko for m in matches]
        pick = st.selectbox("주소 선택", range(len(matches)), format_func=lambda i: labels[i],
                            key=f"adr_sel{idx}")
        match = matches[pick]
        _continue_from(idx, house, match, year)


def _continue_from(idx: int, house: dict, match: juso.AddressMatch, year: int) -> None:
    """필지가 정해진 뒤의 사슬 — 표제부 → 동 → 호 → 공시가격.

    ★ 도로명 검색(juso)과 법정동+지번 입력, **두 입구가 여기서 만난다.**
      juso가 해외 IP에 막혀 두 번째 입구를 만들 때, 뒤쪽 사슬을 복사하지 않고
      함수로 뽑았다. 복사했다면 두 경로가 서로 다르게 굴러 유지가 안 된다 —
      이 저장소가 '규칙은 모든 출구에 건다'로 배운 것과 같은 이야기다.
    """
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
        "다음 단계는 단지 전체의 공시가격 이력을 한 번 읽습니다. "
        "**같은 단지의 다른 호는 그 뒤로 즉시 나옵니다.**"
    )
    if not st.button("이 호의 공시가격 가져오기", key=f"adr_go{idx}", type="primary"):
        return

    bar = st.progress(0.0, text="공시가격을 가져오는 중…")

    def tick(done: int, total: int) -> None:
        # 진행 표시가 없으면 사용자는 멈춘 줄 안다. 남은 페이지를 그대로 보여준다.
        bar.progress(min(done / max(total, 1), 1.0), text=f"공시가격 {done}/{total}")

    try:
        prices = _prices(probe.parcel, _progress=tick)
    except hub.RateLimited as exc:
        bar.empty()
        st.warning(str(exc))
        _apply_dong_only(idx, house, match)
        return
    except Exception as exc:
        bar.empty()
        st.error(f"공시가격 조회에 실패했습니다. 직접 입력해주세요.\n\n{exc}")
        _apply_dong_only(idx, house, match)
        return
    bar.empty()

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
