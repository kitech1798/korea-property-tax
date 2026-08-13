"""부동산 보유세·양도세 상담 — Streamlit 화면.

★ 이 파일에는 세법 계산이 한 줄도 없다.
   문진으로 **사실**을 받아 `TaxCase`를 만들고, 엔진이 돌려준 `TraceNode`를 그린다.
   룰셋(rulesets/)을 고치면 화면이 저절로 따라온다.

실행
    streamlit run app.py --server.port 8555
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import date
from fractions import Fraction

import streamlit as st

from realestate_tax.domain import (
    AcquisitionCause,
    Household,
    HouseholdId,
    ImputedResidenceReason,
    InheritedMeta,
    LeaseOrigin,
    LeaseSpell,
    Ownership,
    Person,
    PersonId,
    PriceFact,
    Property,
    PropertyId,
    PropertyKind,
    RentalRegistration,
    RentalType,
    ResidenceSpell,
    TaxCase,
)
from realestate_tax.advisory import advise as A_ADVISE
from realestate_tax.engine.jongbuse import (
    JongbuseOptions,
    compare_joint_spouse_election,
    compute_jongbuse,
)
from realestate_tax.engine.regions import (
    LAND_PERMIT_NOTICE_KO,
    UNKNOWN,
    YES,
    check_regulated,
)
from realestate_tax.engine.deferral import check_deferral
from realestate_tax.engine.special_houses import assess as A_ASSESS
from realestate_tax.engine.sell_window import ConstraintKind, optimize
from realestate_tax.engine.strategy import consult, sell_timing
from realestate_tax.engine.trace import format_manwon
from realestate_tax.engine.transfer_tax import (
    BurdenGift,
    TransferEvent,
    compute_burden_gift,
    compute_transfer_tax,
)
from realestate_tax.intake import LOOKUP_GUIDE_KO, Severity, intake
from realestate_tax.rules import RuleError, RuleSet, Track, default_ruleset_root
from ui import address as A
from ui import render as R
from ui.theme import CSS, DISCLAIMER

_IMPUTED = {
    "없음": None,
    "취학(고등학교·대학교)": ImputedResidenceReason.SCHOOLING,
    "직장 변경·전근": ImputedResidenceReason.JOB_TRANSFER,
    "질병(1년 이상 치료·요양)": ImputedResidenceReason.ILLNESS,
    "학교폭력 피해로 전학": ImputedResidenceReason.SCHOOL_VIOLENCE,
    "해외체류(취학·근무)": ImputedResidenceReason.OVERSEAS,
    "60세 이상 직계존속 동거봉양": ImputedResidenceReason.ELDER_CARE,
    "재개발·재건축 공사기간": ImputedResidenceReason.RECONSTRUCTION,
}

# 임대차계약이 어떻게 성립했는가. 화면 문구 → 도메인 값.
# ★ '승계'를 따로 두는 이유 — 소득세법 시행령 §155의3①1호 괄호가 "취득으로 임대인의
#   지위가 승계된 경우의 임대차계약은 제외"한다. 세입자가 살던 집을 사서 물려받은
#   계약은 직전임대차계약이 **될 수 없다**. 상생임대 판정에서 가장 흔한 함정이다.
_LEASE_ORIGINS = {
    "새로 체결": LeaseOrigin.NEW,
    "집을 사면서 승계받음": LeaseOrigin.SUCCEEDED,
    "묵시적 갱신": LeaseOrigin.IMPLICIT_RENEWAL,
    "임차인이 갱신요구권 행사": LeaseOrigin.TENANT_RENEWAL_RIGHT,
    "합의로 재계약": LeaseOrigin.AGREED_RENEWAL,
}
_EVIDENCED = {"예": True, "아니오": False, "모름": None}

ME = PersonId("me")
SPOUSE = PersonId("spouse")
HH = HouseholdId("hh")
EOK = 100_000_000


st.set_page_config(
    page_title="부동산 보유세·양도세 상담",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.markdown(CSS, unsafe_allow_html=True)


def _bootstrap_keys() -> None:
    """배포 환경의 비밀값을 환경변수로 옮긴다.

    `realestate_tax/sources/`는 Streamlit을 모른다 — 순수 파이썬이라 테스트가
    쉽고 다른 프론트엔드에도 붙일 수 있다. 그래서 `os.environ`만 읽는다.
    Streamlit Cloud는 값을 `st.secrets`로 주므로, **경계인 여기서** 한 번만 옮긴다.

    ⚠️ 키는 저장소에 없다. 로컬은 사용자 환경변수, 배포는 Cloud Secrets다.
    """
    for name in ("JUSO_CONFM_KEY", "DATA_GO_KR_KEY"):
        if os.environ.get(name):
            continue
        try:
            value = st.secrets[name]
        except Exception:  # secrets.toml이 없으면 그냥 없는 것이다
            value = _secret_near(name)
        if value:
            os.environ[name] = str(value).strip()


def _secret_near(name: str) -> str | None:
    """이름이 **거의** 맞는 비밀값을 찾아 알려준다.

    ★ 실제로 당한 함정(2026-08-05): PowerShell 5.1의 `-Encoding utf8`이 BOM을 붙여
      `secrets.toml` 첫 줄이 `﻿JUSO_CONFM_KEY`가 됐다. TOML 파서는 BOM을 키
      이름의 일부로 읽으므로 **첫 번째 키만 조용히 사라진다.** 두 번째 키는 멀쩡해서
      "왜 하나만 안 되지"로 한참을 헤매게 된다.

      값이 없는 것과 이름이 어긋난 것은 사용자가 할 일이 완전히 다르다. 그래서
      "없다"고만 하지 않고 **어긋난 이름을 그대로 보여준다.** 한 줄이면 끝날 문제를
      30분짜리 미스터리로 만들지 않는다.
    """
    try:
        available = list(st.secrets.keys())
    except Exception:
        return None
    for key in available:
        if key != name and key.strip().lstrip("﻿") == name:
            st.warning(
                f"`{name}` 이름이 어긋나 있습니다 — Secrets에 `{key!r}`로 들어가 있습니다. "
                "앞에 보이지 않는 문자(BOM)나 공백이 붙은 것이니, 해당 줄을 지우고 "
                f"`{name} = \"...\"` 를 다시 입력해주세요.",
                icon="⚠️",
            )
            return st.secrets[key]
    return None


_bootstrap_keys()


@st.cache_resource
def load_rules() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


rs = load_rules()


# ==========================================================================
# 사이드바 — 시나리오
# ==========================================================================

with st.sidebar:
    st.markdown("### 계산 조건")
    # 위젯 key는 테스트가 위치가 아니라 이름으로 찾게 해준다.
    # 위치로 찾으면 사이드바·본문 순서가 바뀔 때 조용히 다른 위젯을 집는다.
    year = st.selectbox("과세연도", [2026, 2027, 2028, 2029], index=0, key="year")
    track_label = st.radio(
        "적용 법령",
        ["현행법", "2026 개편안"],
        key="track",
        help="개편안은 2026.8.3 정부안입니다. 국회 통과 전이라 확정된 제도가 아닙니다.",
    )
    track = Track.CURRENT if track_label == "현행법" else Track.REFORM

    # ★ 트랙을 통째로 갈아끼우던 예전 방식(2026이면 무조건 현행법)을 없앴다.
    #   개편안은 조항마다 시행일이 다르다 — 보유세는 2027년부터지만
    #   양도세 중과 완화는 '26년 양도분에도 경과조치가 있다.
    #   이제 룰셋 resolver가 **조항 단위로** 판단하므로 트랙을 그대로 넘긴다.
    effective_track = track
    if track is Track.REFORM and year <= 2026:
        st.info(
            "**개편안이 통과되어도 2026년 보유세는 현행법과 같습니다.** "
            "종부세 개정 조항은 대부분 2027.1.1. 이후 납세의무 성립분부터 적용되기 때문입니다"
            "(개조식 p.18~20).\n\n"
            "차이는 2027년부터 나타납니다 — **② 탭의 4년 타임라인**에서 현행법과 개편안을 "
            "나란히 보실 수 있습니다. "
            "양도세는 '26년 양도분에도 중과 완화 경과조치가 있어 **③ 탭**에서 차이가 납니다."
        )

    growth = st.select_slider(
        "공시가격 연 상승률 가정",
        options=[0.0, 0.03, 0.05, 0.10],
        value=0.0,
        key="growth",
        format_func=lambda v: f"{v:.0%}",
        help="미래 연도 계산에만 쓰입니다. 정부 문답자료도 0% 가정입니다.",
    )

    st.divider()
    st.caption(f"룰셋 `{rs.version}` · 해시 `{rs.content_hash}`")
    st.caption(f"규칙 {len(rs)}건")


# ==========================================================================
# 문진 — 사실만 받는다
# ==========================================================================

st.title("부동산 보유세·양도세 상담")
st.markdown(
    '<div class="rt-lede">주택 수와 1세대1주택 여부, 조정대상지역은 <b>입력하지 않습니다</b>. '
    "사실을 알려주시면 법령에 따라 판정합니다. 모든 숫자에 근거 조문과 계산식이 붙습니다.</div>",
    unsafe_allow_html=True,
)

if "houses" not in st.session_state:
    st.session_state.houses = [
        {
            "name": "우리집",
            "dong": "1168010100",
            "price": 1_500_000_000,
            "share": "단독",
            "resides": True,
            "residence_years": 10,
            "holding_years": 10,
            "acquired": date(2016, 3, 1),
            "cause": "매매",
            "inheritance_date": date(2024, 1, 1),
            "inherited_share": 100,
            "inherited_same_household": "모름",
            "imputed_reason": "없음",
            "imputed_years": 0,
            "rental": False,
            "rental_declared": False,
            "urban": True,
        }
    ]


# 주택 하나가 화면에 남기는 상태 키의 접두어. 인덱스가 뒤에 붙는다.
# 위젯 키는 Streamlit이 정리하지만 `src`는 우리가 직접 넣은 값이라 우리가 지운다.
_HOUSE_STATE_PREFIXES = (
    "src", "adr_q", "adr_sel", "adr_dong", "adr_ho", "adr_go",
)


def _forget_houses_from(start: int, upto: int = 24) -> None:
    """`start` 이상 인덱스의 주택 흔적을 지운다. 삭제 후 재추가에서 되살아나지 않게."""
    for i in range(start, upto):
        for prefix in _HOUSE_STATE_PREFIXES:
            st.session_state.pop(f"{prefix}{i}", None)


tab_input, tab_holding, tab_sell, tab_advice = st.tabs(
    ["① 상황 입력", "② 보유세 · 4년 타임라인", "③ 팔까 버틸까", "④ 상담"]
)


with tab_input:
    left, right = st.columns([1, 1], gap="large")

    with left:
        st.markdown("### 세대")
        has_spouse = st.checkbox("배우자가 있습니다", value=True)
        birth = st.date_input(
            "본인 생년월일",
            value=date(1962, 5, 20),
            min_value=date(1920, 1, 1),
            max_value=date.today(),
            help="종부세 연령별 세액공제(60·65·70세)와 고령자 특례 판정에 씁니다.",
        )
        st.caption(
            "배우자와 19세 미만 미혼 자녀는 주민등록이 달라도 같은 세대로 봅니다"
            "(지방세법 시행령 §110의2③)."
        )

    with right:
        st.markdown("### 주택")
        st.caption(
            "종부세는 **인별 과세 + 세대별 판정**입니다. "
            "누가 얼마나 가졌는지를 받아야 부부 각자 1채씩과 부부공동명의 1채를 구분할 수 있습니다."
        )
        c1, c2 = st.columns(2)
        if c1.button("＋ 주택 추가", use_container_width=True):
            # ⚠️ 첫 주택을 통째로 복사하면 **임대차 이력까지 따라온다**
            #    (2026-08-13 감사). 세를 준 적 없는 주택에 계약 2건이 생기고,
            #    그대로 두면 있지도 않은 상생임대 특례가 판정된다.
            #    임대차는 주택마다 고유한 사실이므로 복사 대상이 아니다.
            _fresh = {
                k: v for k, v in st.session_state.houses[0].items()
                if not (k == "leased" or k.startswith(("prior_", "sang_")))
            }
            _fresh["leased"] = False
            st.session_state.houses.append(dict(_fresh, name=f"주택{len(st.session_state.houses) + 1}"))
            st.rerun()
        if c2.button("－ 마지막 주택 삭제", use_container_width=True, disabled=len(st.session_state.houses) <= 1):
            st.session_state.houses.pop()
            st.rerun()

    # ★ 삭제된 주택의 흔적을 지운다.
    #   Streamlit은 **위젯 키**(pr1·dg1…)는 화면에서 사라지면 정리해 주지만,
    #   우리가 직접 넣은 `src{i}`(자동조회 출처)는 정리하지 않는다.
    #   그대로 두면 삭제 후 같은 인덱스로 주택을 다시 추가했을 때
    #   **손으로 넣은 숫자에 "압구정 미성 25동 202호 조회값"이라는 거짓 딱지**가 붙는다.
    #   값이 틀리는 것보다 출처가 거짓인 게 더 나쁘다 — 사용자가 그 숫자를 믿게 된다.
    _forget_houses_from(len(st.session_state.houses))

    st.divider()

    for i, h in enumerate(st.session_state.houses):
        with st.expander(f"{h['name']} — {format_manwon(h['price'])}", expanded=i == 0):
            # 주소로 채우는 경로를 먼저 둔다. 손으로 넣는 경로는 그대로 살아 있다 —
            # 자동조회는 편의지 관문이 아니다.
            A.picker(i, h, year)
            if st.session_state.get(f"src{i}"):
                st.caption(f"자동 입력 · {st.session_state[f'src{i}']} — 값은 아래에서 고칠 수 있습니다.")

            a, b, c = st.columns([1.2, 1, 1])

            # 자동조회가 값을 넣을 수 있는 두 칸은 **세션 상태로만** 초기화한다.
            # value= 와 session_state 를 함께 쓰면 Streamlit이 경고를 내고,
            # 조회 결과가 화면에 반영되지 않는다.
            st.session_state.setdefault(f"dg{i}", h["dong"])
            st.session_state.setdefault(f"pr{i}", f"{h['price']:,}")

            h["name"] = a.text_input("이름(메모용)", h["name"], key=f"nm{i}")
            h["dong"] = a.text_input(
                "법정동코드 10자리",
                key=f"dg{i}",
                help="주소 문자열이 아니라 코드로만 지역을 판정합니다. "
                "시중 계산기가 `주소.startswith('서울')`로 조정대상지역을 추측하는 것이 "
                "신뢰를 잃은 대표적 이유입니다.",
            )

            raw_price = b.text_input(
                "공시가격",
                key=f"pr{i}",
                help="예: 15억 / 1,500,000,000 / 15억 3,200만",
            )
            parsed = intake(raw_price, rs, on=date(year, 6, 1))
            if parsed.value is not None:
                h["price"] = parsed.value
            for n in parsed.notices:
                if n.severity is Severity.BLOCK:
                    b.error(f"{n.message_ko}\n\n{n.hint_ko}")
                else:
                    b.warning(f"{n.message_ko}\n\n{n.hint_ko}")

            h["share"] = c.selectbox(
                "명의",
                ["단독", "부부 공동 1/2"],
                index=0 if h["share"] == "단독" else 1,
                key=f"sh{i}",
                disabled=not has_spouse,
            )
            h["urban"] = c.checkbox(
                "도시지역 안",
                h["urban"],
                key=f"ur{i}",
                help="재산세 도시지역분(과세표준 0.14%) 부과 여부. "
                "시중 계산기는 묻지 않고 전국에 일률 부과합니다.",
            )

            d, e, f = st.columns(3)
            h["resides"] = d.checkbox("여기 살고 있습니다", h["resides"], key=f"rd{i}")
            h["residence_years"] = d.number_input(
                "거주기간(년)", 0, 60, h["residence_years"], key=f"ry{i}"
            )
            h["holding_years"] = e.number_input(
                "보유기간(년)", 0, 60, h["holding_years"], key=f"hy{i}"
            )
            h["acquired"] = e.date_input(
                "취득일",
                h["acquired"],
                min_value=date(1960, 1, 1),
                max_value=date.today(),
                key=f"aq{i}",
                help="일시적 2주택 특례 판정에 필요합니다.",
            )
            h["cause"] = f.selectbox(
                "취득 원인", ["매매", "상속", "증여", "신축"],
                index=["매매", "상속", "증여", "신축"].index(h["cause"]),
                key=f"cs{i}",
            )
            if h["cause"] == "상속":
                h["inheritance_date"] = f.date_input(
                    "상속개시일", h["inheritance_date"], key=f"id{i}"
                )
                h["inherited_share"] = f.number_input(
                    "상속 지분율(%)", 1, 100, h["inherited_share"], key=f"is{i}"
                )
                # 양도세 상속주택 특례(시행령 §155②)가 이 한 줄로 갈린다.
                # 3지선다인 이유: '모른다'가 실제 상태이고, 모르면 판정하지 않는다.
                # ★ `h[...]`가 아니라 `h.get(...)`이다. 세션에 이미 들어 있는 주택
                #   딕셔너리에는 **오늘 추가한 키가 없다.** 브라우저를 열어둔 채로
                #   배포되면 KeyError로 화면이 통째로 죽는다. 새 항목을 추가할 때는
                #   항상 기본값과 함께 읽는다.
                _SAME_HH = ["모름", "아니오 (따로 살았음)", "예 (같이 살았음)"]
                h["inherited_same_household"] = f.selectbox(
                    "상속 당시 고인과 같은 세대였나요?",
                    _SAME_HH,
                    index=_SAME_HH.index(h.get("inherited_same_household", "모름")),
                    key=f"ish{i}",
                    help=(
                        "양도세에서 상속주택을 주택 수에서 뺄지가 이 답으로 갈립니다"
                        "(소득세법 시행령 §155② 단서). 따로 사시던 부모님 집을 상속받은 "
                        "경우가 '아니오'입니다. 종합부동산세에는 이 요건이 없습니다."
                    ),
                )
            if h["resides"]:
                # 개편안이 보유공제를 거주공제로 바꾸면서 넣은 완충장치다 —
                # 이게 없으면 전근·유학 간 사람이 전부 벌을 받는다(개조식 p.22).
                _keys = list(_IMPUTED)
                h["imputed_reason"] = f.selectbox(
                    "못 산 기간이 있나요? (거주로 인정되는 사유)",
                    _keys,
                    index=_keys.index(h.get("imputed_reason", "없음")),
                    key=f"ir{i}",
                    help=(
                        "개편안은 부득이한 사유로 비운 기간을 **최장 3년**까지 거주기간으로 "
                        "인정합니다. 재개발·재건축 공사기간은 **1/2**을 인정합니다. "
                        "이사하지 않고도 거주공제를 받을 수 있는 길입니다."
                    ),
                )
                if h.get("imputed_reason", "없음") != "없음":
                    h["imputed_years"] = f.number_input(
                        "그 기간(년)", 0, 30, int(h.get("imputed_years", 0)), key=f"iy{i}"
                    )
            h["rental"] = f.checkbox("등록임대주택", h["rental"], key=f"rt{i}")
            if h["rental"]:
                h["rental_declared"] = f.checkbox(
                    "합산배제 신고했습니다", h["rental_declared"], key=f"rc{i}",
                    help="임대유형·의무임대기간·가액요건은 등록증으로만 확인됩니다. "
                    "확인해주시기 전에는 유리하게 가정하지 않습니다.",
                )

            # ── 임대차 이력 — 상생임대주택 특례(소득세법 시행령 §155의3) ──────
            # 거주요건을 못 채운 사람에게 비과세 12억을 열어 주는 유일한 길이라,
            # 실거주 0년인 주택에서는 이 입력 하나가 세액을 억 단위로 가른다.
            h["leased"] = f.checkbox(
                "세를 준 적이 있습니다 (상생임대 판정)", h.get("leased", False), key=f"lz{i}",
                help="임대료를 5% 이내로만 올린 집주인은 1세대1주택 비과세와 "
                "장기보유특별공제의 **2년 거주요건을 면제**받습니다(시행령 §155의3). "
                "실거주하지 않은 집도 비과세가 될 수 있습니다.",
            )
            if h["leased"]:
                st.caption(
                    "직전임대차계약과 상생임대차계약 **두 건**을 비교해 판정합니다. "
                    "상생임대차계약은 직전계약 대비 보증금·월세 인상률이 5% 이내여야 합니다."
                )
                for slot, title in (("prior", "직전임대차계약"), ("sang", "상생임대차계약")):
                    st.markdown(f"**{title}**")
                    c1, c2, c3 = st.columns(3)
                    h[f"{slot}_start"] = c1.date_input(
                        "임대 개시", h.get(f"{slot}_start", date(2023, 2, 1)),
                        key=f"{slot}s{i}", min_value=date(2000, 1, 1), max_value=date(2035, 12, 31),
                    )
                    h[f"{slot}_end"] = c2.date_input(
                        "임대 종료", h.get(f"{slot}_end", date(2025, 1, 31)),
                        key=f"{slot}e{i}", min_value=date(2000, 1, 1), max_value=date(2035, 12, 31),
                        help="계약서상 만료일입니다. **그 날까지 임대한 것**으로 셉니다.",
                    )
                    h[f"{slot}_contracted"] = c3.date_input(
                        "계약 체결일", h.get(f"{slot}_contracted", date(2022, 12, 10)),
                        key=f"{slot}c{i}", min_value=date(2000, 1, 1), max_value=date(2035, 12, 31),
                        help="상생임대차계약은 **'21.12.20.~'26.12.31. 중 체결**이 요건입니다. "
                        "임대 개시일이 아니라 체결일로 갈립니다.",
                    )
                    d1, d2, d3 = st.columns(3)
                    h[f"{slot}_deposit"] = d1.number_input(
                        "보증금(억원)", 0.0, 100.0, float(h.get(f"{slot}_deposit", 5.0)),
                        step=0.1, format="%.2f", key=f"{slot}d{i}",
                    )
                    h[f"{slot}_rent"] = d2.number_input(
                        "월세(만원)", 0, 5000, int(h.get(f"{slot}_rent", 0)),
                        step=10, key=f"{slot}r{i}",
                        help="전세면 0. ⚠️ 보증금과 월세를 **서로 전환**한 계약은 "
                        "증가율 산식(민간임대주택법 §44④)을 확보하지 못해 판정하지 않습니다.",
                    )
                    _ok = list(_LEASE_ORIGINS)
                    h[f"{slot}_origin"] = d3.selectbox(
                        "이 계약이 생긴 경위", _ok,
                        index=_ok.index(h.get(f"{slot}_origin", "새로 체결")),
                        key=f"{slot}o{i}",
                        help="**승계받은 계약은 직전임대차계약이 될 수 없습니다**"
                        "(§155의3①1호 괄호). 상생임대 판정에서 가장 흔한 함정입니다. "
                        "갱신요구권 행사 이력은 다음 만기에 세입자를 내보낼 수 있는지를 가릅니다.",
                    )
                    # ⚠️ 예전에는 임차인 별칭이 **한 칸뿐**이라 두 계약의 임차인이
                    #    다르다고 답할 방법이 없었다(2026-08-13 감사). 그래서 앞 계약에
                    #    '갱신요구권 행사'를 고르면 사람이 바뀌었어도 권리가 소진된 것으로
                    #    읽혀 **경고가 통째로 사라졌다.** 계약마다 받는다.
                    h[f"{slot}_tenant"] = st.text_input(
                        "임차인 구분(별칭)",
                        h.get(f"{slot}_tenant", h.get("tenant_ref", "임차인A")),
                        key=f"{slot}tn{i}",
                        help="갱신요구권은 **임차인마다 1회**입니다(§6의3②). 두 계약의 "
                        "임차인이 같은 사람인지 알아야 소진 여부를 판정할 수 있습니다. "
                        "개인정보를 남기지 않도록 이름 대신 별칭을 쓰세요.",
                    )
                    if slot == "sang":
                        _ek = list(_EVIDENCED)
                        h["sang_evidenced"] = st.selectbox(
                            "계약금 지급이 증빙서류로 확인되나요?", _ek,
                            index=_ek.index(h.get("sang_evidenced", "모름")),
                            key=f"sv{i}",
                            help="§155의3①1호가 요건으로 정합니다. "
                            "**모르면 유리하게 가정하지 않습니다** — 확인 전에는 특례를 적용하지 않습니다.",
                        )
                st.caption(
                    "임차인이 바뀌면 갱신요구권도 새로 생깁니다(§6의3② — 임차인마다 1회). "
                    "두 계약의 임차인이 다르면 아래 별칭을 서로 다르게 적어주세요."
                )

            zone = check_regulated(h["dong"], rs, on=date(year, 6, 1), track=effective_track)
            if zone.designation is YES:
                st.markdown(
                    f'<div class="rt-badges"><span class="rt-badge rt-badge--warn">조정대상지역</span></div>',
                    unsafe_allow_html=True,
                )
                st.caption(zone.reason_ko)
            elif zone.designation is UNKNOWN:
                R.note("조정대상지역 판정 불가", zone.reason_ko, "warn")
            else:
                st.caption(f"조정대상지역 아님 — {zone.reason_ko}")

    with st.expander("공시가격은 어디서 확인하나요?"):
        st.markdown(LOOKUP_GUIDE_KO)
    with st.expander("토지거래허가구역은 판정하지 않습니다 — 이유"):
        st.markdown(LAND_PERMIT_NOTICE_KO)


# ==========================================================================
# 사건 조립 — 여기서 세법 판정을 하지 않는다
# ==========================================================================


def build_case(target_year: int) -> TaxCase:
    persons = [Person(id=ME, household_id=HH, name="본인", birth_date=birth)]
    members = [ME]
    if has_spouse:
        persons.append(
            Person(id=SPOUSE, household_id=HH, name="배우자", spouse_id=ME, birth_date=birth)
        )
        persons[0] = replace(persons[0], spouse_id=SPOUSE)
        members.append(SPOUSE)

    props, owns, spells, leases = [], [], [], []
    for i, h in enumerate(st.session_state.houses):
        pid = PropertyId(f"h{i}")
        rental = (
            RentalRegistration(
                rental_type=RentalType.BUILT_LONG_TERM, registered_on=h["acquired"]
            )
            if h["rental"]
            else None
        )
        props.append(
            Property(
                id=pid,
                kind=PropertyKind.APARTMENT,
                legal_dong_code=h["dong"],
                display_name=h["name"],
                published_prices=(PriceFact(target_year, h["price"]),),
                in_urban_planning_area=h["urban"],
                rental=rental,
            )
        )
        inherited = None
        cause = AcquisitionCause.PURCHASE
        if h["cause"] == "상속":
            cause = AcquisitionCause.INHERITANCE
            inherited = InheritedMeta(
                inheritance_date=h["inheritance_date"],
                share=Fraction(h["inherited_share"], 100),
                inherited_value=int(h["price"] * Fraction(h["inherited_share"], 100)),
                same_household_at_death={
                    "모름": None,
                    "아니오 (따로 살았음)": False,
                    "예 (같이 살았음)": True,
                }[h.get("inherited_same_household", "모름")],
            )
        elif h["cause"] == "증여":
            cause = AcquisitionCause.GIFT
        elif h["cause"] == "신축":
            cause = AcquisitionCause.NEW_BUILD

        if h["share"] == "부부 공동 1/2" and has_spouse:
            owns.append(Ownership(ME, pid, Fraction(1, 2), h["acquired"], cause, inherited))
            owns.append(Ownership(SPOUSE, pid, Fraction(1, 2), h["acquired"], cause, inherited))
        else:
            owns.append(Ownership(ME, pid, Fraction(1), h["acquired"], cause, inherited))

        if h["resides"]:
            # ⚠️ 예전에는 `max(1, 거주기간)`이었다. 거주 0년(올해 이사)을 넣으면
            #    1년 전부터 산 것으로 **17개월을 날조**했다. 사실을 받는 도구가
            #    사실을 지어내면 안 된다. 0년이면 그해 1월 1일부터로 둔다 —
            #    과세기준일(6/1)은 포함하되 없는 과거는 만들지 않는다.
            years = max(0, h["residence_years"])
            spells.append(
                ResidenceSpell(ME, pid, start=date(target_year - years, 1, 1))
            )

        # 부득이한 사유로 못 산 기간 — 개편안이 거주기간으로 인정한다(개조식 p.22).
        # 실거주 구간과 **별개 구간**으로 넣어야 상한 3년·재건축 1/2이 따로 적용된다.
        reason_key = _IMPUTED.get(h.get("imputed_reason", "없음"))
        if reason_key is not None and h.get("imputed_years", 0) > 0:
            span = int(h["imputed_years"])
            end = date(target_year - max(0, h["residence_years"]), 1, 1)
            spells.append(
                ResidenceSpell(
                    ME, pid,
                    start=date(end.year - span, 1, 1),
                    end=end,
                    imputed_reason=reason_key,
                )
            )

        # 임대차 이력. **판정은 담지 않는다** — 상생임대 해당 여부도, 갱신요구권
        # 소진 여부도 엔진이 두 계약을 비교해서 낸다. 화면은 사실만 넘긴다.
        if h.get("leased"):
            for slot in ("prior", "sang"):
                leases.append(
                    LeaseSpell(
                        property_id=pid,
                        start=h[f"{slot}_start"],
                        end=h[f"{slot}_end"],
                        contracted_on=h[f"{slot}_contracted"],
                        deposit=int(h[f"{slot}_deposit"] * EOK),
                        monthly_rent=int(h[f"{slot}_rent"]) * 10_000,
                        origin=_LEASE_ORIGINS[h[f"{slot}_origin"]],
                        # 계약금 증빙은 **상생임대차계약**의 요건이다(§155의3①1호).
                        #
                        # ⚠️ 직전 칸에 True를 박아 두면, 사용자가 두 칸을 바꿔 넣었을 때
                        #    (날짜로는 엔진이 순서를 바로잡는다) 실제 상생계약에
                        #    'True'가 붙어 **'모름'이라고 답했는데 요건이 통과**한다
                        #    (2026-08-13 감사). None으로 두면 바꿔 넣어도 판정 불가로
                        #    흘러 유리한 쪽으로 새지 않는다.
                        down_payment_evidenced=(
                            _EVIDENCED[h.get("sang_evidenced", "모름")]
                            if slot == "sang"
                            else None
                        ),
                        tenant_ref=h.get(f"{slot}_tenant", ""),
                    )
                )

    return TaxCase(
        year=target_year,
        persons=tuple(persons),
        households=(Household(id=HH, member_ids=tuple(members)),),
        properties=tuple(props),
        ownerships=tuple(owns),
        residences=tuple(spells),
        leases=tuple(leases),
    )


def main_options() -> JongbuseOptions:
    main = st.session_state.houses[0]
    return JongbuseOptions(
        residence_years=main["residence_years"],
        holding_years=main["holding_years"],
        resides_in_main_house=main["resides"],
    )


# ==========================================================================
# ② 보유세
# ==========================================================================

with tab_holding:
    case = build_case(year)
    opts = main_options()

    # ⚠️ 규칙 하나가 없다고 **화면 전체가 죽으면 안 된다**(2026-08-13 배포본에서 발생).
    #
    #   룰셋에 조건에 맞는 블록이 없으면 resolver가 MissingRule을 던진다. 그건
    #   "조용히 기본값으로 때우지 않는다"는 이 프로젝트의 설계이고 옳다. 다만 그
    #   예외가 화면까지 올라오면 Streamlit이 빨간 상자에 **가려진 트레이스백**만
    #   띄우고 앱이 통째로 멈춘다 — 사용자는 무엇을 해야 할지 알 수 없다.
    #
    #   숨기지 않는다. 무엇이 없는지 그대로 보여주고, 다른 탭은 살려 둔다.
    result = None
    try:
        result = compute_jongbuse(case, ME, rs, track=effective_track, options=opts)
    except RuleError as exc:
        R.note(
            "이 조건에서는 계산할 규칙이 없습니다",
            f"{exc}\n\n적용 법령이나 과세연도를 바꾸면 계산될 수 있습니다. "
            "이 화면만 멈추고 다른 탭은 그대로 쓸 수 있습니다.",
            "warn",
        )
        st.stop()

    R.badges(k for k, _ in result.trace.certainty_concerns())

    R.cards(
        [
            ("재산세", format_manwon(result.property_tax_total.as_int()), "본세+도시지역분+지방교육세", False),
            ("종합부동산세", format_manwon(result.total.as_int()), "농어촌특별세 포함", False),
            ("보유세 합계", format_manwon(result.holding_tax_total), f"{year}년 · {track_label}", True),
        ]
    )

    st.markdown("## 4년 타임라인")
    st.caption(
        "개편안은 2026~2029 단계 시행이라 한 해만 보면 판단을 그르칩니다. "
        "2027년부터는 현행법과 개편안을 나란히 냅니다."
    )
    con = consult(case, ME, rs, options=opts, growth=growth)
    by_year: dict[int, dict[str, int]] = {}
    for p in con.timeline:
        by_year.setdefault(p.year, {})[str(p.track)] = p.total
    R.table(
        ["연도", "현행법", "개편안", "차이"],
        [
            [
                f"{y}년",
                format_manwon(v.get("current")) if v.get("current") is not None else "—",
                format_manwon(v.get("reform")) if v.get("reform") is not None else "—",
                format_manwon(v["reform"] - v["current"]) if len(v) == 2 else "—",
            ]
            for y, v in sorted(by_year.items())
        ],
    )

    if con.beneficial:
        st.markdown("## 절세 대안")
        st.caption("절감액은 추정이 아니라 조건을 바꿔 **다시 계산한 차액**입니다.")
        for s in con.beneficial:
            with st.container(border=True):
                head = f"**{s.label_ko}** — {format_manwon(s.saving)} 절감"
                # 일회성 비용이 드는 대안은 **회수기간**이 실제 판단 기준이다.
                # 4년 누적 절감액만 보여주면 비교 창의 길이가 결론을 만든다.
                if s.upfront_cost > 0 and s.payback_years not in (None, float("inf")):
                    head += (
                        f" · 초기비용 {format_manwon(s.upfront_cost)} · "
                        f"**약 {s.payback_years:.1f}년이면 본전**"
                    )
                st.markdown(head)
                st.markdown(s.what_to_do_ko)
                st.caption(f"근거 · {s.basis_ko}")
                if s.requirements_ko:
                    st.markdown("**요건**\n" + "\n".join(f"- {r}" for r in s.requirements_ko))
                if s.caveats_ko:
                    st.markdown("**주의**\n" + "\n".join(f"- {c}" for c in s.caveats_ko))

    # ★ "하면 손해"도 절세 정보다. 통념과 반대인 결론일수록 그렇다 —
    #   "배우자에게 증여하면 절세된다"는 널리 퍼져 있지만 1주택자에게는
    #   1세대1주택 세액공제(최대 80%)를 잃어 오히려 손해다.
    #   이걸 안 보여주면 사용자는 다른 데서 듣고 그대로 한다.
    # ── 납부유예 ──────────────────────────────────────────────────────
    # 절감이 아니라 **유예**라 절세 대안 목록에 넣지 않는다. 절감액 칸에 숫자가
    # 들어가는 순간 사용자는 그만큼 안 내도 되는 줄 안다.
    # 그래도 반드시 있어야 한다 — 집은 있는데 현금이 없는 고령 1주택자에게는
    # 이게 유일한 현실적 답이고, 개편안이 보유세를 올리면서 쓸모가 커졌다.
    main_result = compute_jongbuse(case, ME, rs, track=effective_track, options=opts)
    assessment = A_ASSESS(case, ME, rs, track=effective_track)
    defer = check_deferral(
        case, ME, rs,
        jongbuse_amount=main_result.net_tax.as_int(),
        one_house=assessment.is_one_house,
        holding_years=main_options().holding_years,
        track=effective_track,
    )
    if defer.worth_showing:
        st.markdown("## 세금을 미룰 수 있습니다 — 납부유예")
        st.caption(
            "**감면이 아니라 유예입니다.** 집을 팔거나 물려줄 때 유예된 세액을 "
            "이자상당가산액과 함께 냅니다(종합부동산세법 §20의2⑤). "
            "집은 있는데 낼 현금이 없을 때 쓰는 제도입니다."
        )
        with st.container(border=True):
            st.markdown(f"**유예 가능액 {format_manwon(defer.deferrable)}**")
            st.markdown("\n".join(f"- ✅ {m}" for m in defer.met_ko))
            if defer.asks_ko:
                st.markdown(
                    "**확인이 필요한 요건**\n"
                    + "\n".join(f"- {a}" for a in defer.asks_ko)
                )
            st.markdown(
                "**신청** — 납부기한 만료 3일 전까지 관할세무서장에게 신청하고, "
                "유예할 세액에 상당하는 담보를 제공해야 합니다(§20의2①)."
            )
            if defer.revoke_reasons_ko:
                st.markdown(
                    "**허가가 취소되는 경우**\n"
                    + "\n".join(f"- {r}" for r in defer.revoke_reasons_ko)
                )

    harmful = [s for s in con.strategies if s.saving < 0]
    if harmful:
        st.markdown("## 하지 않는 편이 나은 것")
        st.caption("흔히 절세로 알려졌지만 이 상황에서는 **오히려 손해**로 계산된 대안입니다.")
        for s in harmful:
            with st.container(border=True):
                st.markdown(f"**{s.label_ko}** — {format_manwon(-s.saving)} 손해")
                st.caption(f"근거 · {s.basis_ko}")
                if s.caveats_ko:
                    st.markdown("\n".join(f"- {c}" for c in s.caveats_ko))

    if has_spouse and len(st.session_state.houses) == 1:
        cmp = compare_joint_spouse_election(case, ME, rs, track=effective_track, options=opts)
        if cmp.eligible:
            st.markdown("## 부부공동명의 1주택자 특례")
            st.caption(
                "신청이 늘 유리하지는 않습니다. 세액공제까지 포함한 완전 계산을 두 번 돌려 비교합니다."
            )
            R.table(
                ["구분", "세액", "판정"],
                [
                    ["특례 신청", format_manwon(cmp.elected_total), "권장" if cmp.recommended == "elected" else ""],
                    ["개별 납부", format_manwon(cmp.not_elected_total), "권장" if cmp.recommended == "not_elected" else ""],
                ],
                best_row=0 if cmp.recommended == "elected" else 1,
            )
            st.caption(f"차이 {format_manwon(cmp.saving)}")

    R.alternatives(result.trace.all_alternatives())

    if con.notes_ko:
        st.markdown("## 알려드릴 것")
        for n in con.notes_ko:
            st.markdown(f"- {n}")

    st.markdown("## 계산 근거")
    st.caption("모든 단계에 산식·대입값·근거 조문이 붙습니다. 손으로 검산하실 수 있습니다.")
    R.trace_tree(result.trace)


# ==========================================================================
# ③ 매도 시점
# ==========================================================================

with tab_sell:
    st.markdown("### 팔까 버틸까")
    st.caption(
        "보유세만으로도, 양도세만으로도 답이 안 나옵니다. "
        "버틸수록 보유세는 쌓이고, 늦게 팔수록 양도세는 늘어납니다."
    )

    names = [h["name"] for h in st.session_state.houses]
    s1, s2, s3 = st.columns(3)
    target = s1.selectbox("팔 주택", names, key="sell_target")
    idx = names.index(target)
    sale_price = s2.number_input(
        "예상 양도가액(억원)", 0.1, 300.0, 30.0, step=0.5, format="%.1f", key="sell_price"
    )
    buy_price = s3.number_input(
        "취득가액(억원)", 0.0, 300.0, 12.0, step=0.5, format="%.1f", key="buy_price",
        help="세법상 정본은 **매매계약서**입니다(소득세법 §97①1). "
        "실거래가 공개시스템 값은 신고가액의 공개본이지 세법상 정본이 아닙니다.",
    )

    t1, t2 = st.columns(2)
    sell_year = t1.selectbox(
        "양도 예정 연도", [2026, 2027, 2028, 2029], index=1, key="sell_year",
        help="중과 한시완화가 '27~'28에만 있고 '26년 양도분에도 경과조치가 있어 "
        "연도마다 세액이 크게 다릅니다.",
    )
    expense = t2.number_input(
        "필요경비(만원)", 0, 500_000, 0, step=100, key="sell_expense",
        help="취득세·법무비·중개보수·자본적지출 등(소득세법 §97①2). "
        "**빼지 않으면 양도차익이 실제보다 크게 잡혀 세금이 과대계상됩니다.**",
    )

    h = st.session_state.houses[idx]
    event = TransferEvent(
        property_id=PropertyId(f"h{idx}"),
        person_id=ME,
        transfer_date=date(sell_year, 6, 1),
        transfer_price=int(sale_price * EOK),
        acquisition_price=int(buy_price * EOK),
        acquisition_date=h["acquired"],
        necessary_expense=expense * 10_000,
        holding_years=h["holding_years"],
        residence_years=h["residence_years"],
    )
    sell_case = build_case(sell_year)
    detail = compute_transfer_tax(sell_case, event, rs, track=track)

    # ======================================================================
    # 매도 시점 — 세액과 제약을 따로 본다
    # ======================================================================
    #
    # ★ "세액이 가장 낮은 날"을 고르면 틀린다. 그날 팔 수 있어야 고를 수 있다.
    #   실제 상담 사건에서 구속력이 가장 큰 기한은 세법이 아니라 주택임대차보호법
    #   §6①(갱신거절 통지)에서 나왔고, 그 기한은 세액 곡선에 흔적조차 남지 않는다.
    #   세법만 보고 "2028년 1월까지 팔면 됩니다"라고 답하면, 그때는 손쓸 시점이
    #   1년 2개월 전에 이미 지나 있다.
    if sell_case.leases:
        st.markdown("### 언제 팔아야 하나")
        st.caption(
            "세액은 날짜에 대해 **계단 함수**입니다. 값이 바뀌는 곳은 법이 그은 경계뿐이라, "
            "달력을 훑지 않고 경계를 직접 계산합니다."
        )

        _start = max(date.today(), date(2026, 1, 1))
        _end = date(2030, 12, 31)
        _base = optimize(sell_case, event, rs, start=_start, end=_end, track=track)
        _renewed = optimize(
            sell_case, event, rs, start=_start, end=_end, track=track, assume_renewal=True
        )

        if _base.best is not None:
            _loss = (
                _renewed.best.transfer_tax - _base.best.transfer_tax
                if _renewed.best is not None
                else 0
            )
            R.cards(
                [
                    ("권장 매도일", str(_base.best.on), "팔 수 있는 날 중 세금이 가장 적은 날", True),
                    ("그날의 총부담", format_manwon(_base.best.transfer_tax), "양도세 + 지방소득세", False),
                    (
                        "임대차가 갱신되면",
                        format_manwon(_renewed.best.transfer_tax) if _renewed.best else "—",
                        f"매도일 {_renewed.best.on}로 밀림" if _renewed.best else "",
                        False,
                    ),
                    # ⚠️ 예전 이름은 '통지를 놓친 비용'이었다. 그러면 "통지만 제때
                    #    하면 안전하다"는 **반대의 오해**를 만든다(2026-08-13 감사).
                    #    갱신은 통지 실패로도 오지만 임차인의 갱신요구권 행사로도 온다.
                    #    §6의3①이 "제6조에도 불구하고"로 시작하므로 통지만으로는 막지 못한다.
                    ("갱신되면 더 내는 세금", format_manwon(_loss),
                     "통지 실패 또는 임차인의 갱신요구", _loss > 0),
                ]
            )

        _KIND_KO = {
            ConstraintKind.DEADLINE: "기한",
            ConstraintKind.BLOCKS: "매도 가능",
            ConstraintKind.RISK: "확인 필요",
        }
        if _base.constraints:
            st.markdown("**기한과 제약 — 먼저 오는 것이 진짜 데드라인입니다**")
            R.table(
                ["구분", "무엇", "언제", "근거", "해야 할 일"],
                [
                    [
                        _KIND_KO[c.kind],
                        c.label_ko,
                        f"{c.window[0]} ~ {c.window[1]}" if c.window else str(c.on),
                        c.basis_ko,
                        c.action_ko,
                    ]
                    for c in sorted(
                        _base.constraints, key=lambda c: c.on or date(2099, 1, 1)
                    )
                ],
            )
            for c in _base.constraints:
                if c.kind is ConstraintKind.RISK:
                    R.note(f"확인 필요 — {c.label_ko}", f"{c.action_ko} {c.note_ko}", "warn")

        _cliffs = [c for c in _base.cliffs(threshold=1_000_000)]
        if _cliffs:
            st.markdown("**세액 절벽 — 하루 차이로 갈리는 자리**")
            R.table(
                ["이 날까지", "하루 넘기면", "늘어나는 세금"],
                [
                    [str(c.before), str(c.after), f"+{format_manwon(c.increase)}"]
                    for c in _cliffs
                ],
            )
            st.caption(
                "달력을 월 단위로만 훑으면 1월 31일과 2월 1일 사이의 절벽을 통째로 놓칩니다. "
                "그래서 각 기한일과 **그 다음 날**을 직접 계산에 넣습니다."
            )

        st.divider()

    st.markdown(f"### {sell_year}년에 판다면")

    # ★ 세액 명세를 먼저 보여준다. 예전에는 '권장 매도 시점' 카드만 있고
    #   양도차익·공제·과세표준이 감사추적 안에만 있어서, 사용자는 자기 세금이
    #   어떻게 나왔는지 보려면 트리를 펼쳐야 했다.
    R.badges(k for k, _ in detail.trace.certainty_concerns())
    R.cards(
        [
            ("양도차익", format_manwon(detail.gain.as_int()), "양도가액 − 취득가액 − 필요경비", False),
            ("과세대상 양도차익", format_manwon(detail.taxable_gain.as_int()),
             "1세대1주택 비과세·고가주택 안분 반영", False),
            ("장기보유특별공제", format_manwon(detail.long_term_deduction.as_int()),
             "보유·거주기간별", False),
            ("과세표준", format_manwon(detail.taxable_base.as_int()), "기본공제 차감 후", False),
        ]
    )
    R.cards(
        [
            ("양도소득세", format_manwon(detail.income_tax.as_int()), "산출세액", False),
            ("개인지방소득세", format_manwon(detail.local_income_tax.as_int()), "산출세액의 10%", False),
            ("총 부담세액", format_manwon(detail.total.as_int()), f"{sell_year}년 양도 기준", True),
        ]
    )

    if detail.gain.as_int() == 0:
        R.note(
            "양도차익이 없습니다",
            "양도가액이 취득가액과 필요경비의 합 이하라 과세할 소득이 없습니다. "
            "다만 양도소득세는 차익이 없어도 신고 대상입니다(소득세법 §110). "
            "같은 해에 다른 자산을 팔아 이익이 났다면 이 손실과 통산할 수 있습니다.",
            "warn",
        )

    R.alternatives(detail.trace.all_alternatives(), "적용되지 않은 항목")

    st.markdown("### 언제 파는 게 나은가")
    st.caption(
        "버틸수록 보유세는 쌓이고, 늦게 팔수록 중과가 돌아옵니다. "
        "두 세목을 합친 **총비용**으로 비교합니다."
    )
    timing = sell_timing(
        build_case(2027), ME, replace(event, transfer_date=date(2027, 6, 1)),
        rs, track=track, options=main_options(), growth=growth,
    )
    best_idx = timing.points.index(timing.best)
    R.table(
        ["매도 연도", "양도세", "보유세 누적", "총비용"],
        [
            [
                f"{p.year}년",
                format_manwon(p.transfer_tax),
                format_manwon(p.holding_tax_paid),
                format_manwon(p.total_cost),
            ]
            for p in timing.points
        ],
        best_row=best_idx,
    )
    R.cards(
        [
            ("권장 매도 시점", f"{timing.best.year}년", "총비용이 가장 낮은 해", True),
            ("최악 대비 차이", format_manwon(timing.spread), f"{timing.worst.year}년 매도 대비", False),
        ]
    )

    st.markdown("### 계산 근거")
    st.caption("모든 단계에 산식·대입값·근거 조문이 붙습니다. 손으로 검산하실 수 있습니다.")
    R.trace_tree(detail.trace)

    # ── 부담부증여 ────────────────────────────────────────────────────
    st.divider()
    st.markdown("### 파는 대신 물려준다면 — 부담부증여")
    st.caption(
        "전세보증금이나 대출을 함께 넘기는 증여입니다. **한 사건이 두 세목으로 쪼개집니다** — "
        "채무액에 해당하는 부분은 **양도**로 보아 주는 사람이 양도소득세를 내고"
        "(소득세법 §88① 후단), 나머지는 받는 사람이 **증여세**를 냅니다."
    )

    g1, g2, g3 = st.columns(3)
    appraised = g1.number_input(
        "증여재산 평가액(억원)", 0.1, 300.0, 20.0, step=0.5, format="%.1f",
        key="gift_value",
        help="상속세및증여세법 §60~66에 따라 평가한 가액입니다.",
    )
    debt = g2.number_input(
        "수증자가 인수하는 채무(억원)", 0.0, 300.0, 8.0, step=0.5, format="%.1f",
        key="gift_debt",
        help="전세보증금·근저당 등. 이 금액에 해당하는 부분만 양도로 봅니다.",
    )
    g3.metric("양도로 보는 비율", f"{(debt / appraised * 100) if appraised else 0:.1f}%")

    if debt <= 0:
        R.note(
            "채무가 없으면 부담부증여가 아닙니다",
            "인수 채무가 0원이면 순수 증여라 양도소득세가 생기지 않습니다. "
            "증여세만 검토하시면 됩니다.",
        )
    elif debt > appraised:
        R.note(
            "채무가 재산보다 큽니다",
            "인수 채무가 증여재산 평가액을 넘으면 부담부증여로 보지 않습니다. "
            "금액을 확인해주세요.",
            "warn",
        )
    else:
        bg = BurdenGift(
            property_id=PropertyId(f"h{idx}"),
            person_id=ME,
            gift_date=date(sell_year, 6, 1),
            appraised_value=int(appraised * EOK),
            gift_value=int(appraised * EOK),
            debt_assumed=int(debt * EOK),
            acquisition_price=int(buy_price * EOK),
            necessary_expense=expense * 10_000,
            holding_years=h["holding_years"],
            residence_years=h["residence_years"],
        )
        bg_result = compute_burden_gift(sell_case, bg, rs, track=track)
        R.badges(k for k, _ in bg_result.trace.certainty_concerns())
        R.cards(
            [
                ("양도로 보는 가액", format_manwon(bg_result.event.transfer_price),
                 f"평가액 × 채무 {debt:.1f}억 ÷ 증여가액 {appraised:.1f}억", False),
                ("양도소득세", format_manwon(bg_result.income_tax.as_int()), "산출세액", False),
                ("총 부담세액", format_manwon(bg_result.total.as_int()),
                 "지방소득세 포함 · 증여세 별도", True),
            ]
        )
        R.note(
            "증여세는 이 계산에 없습니다",
            f"나머지 {format_manwon(bg.gift_portion)}은 받는 사람이 증여세를 냅니다"
            "(상속세및증여세법). 이 서비스는 양도소득세만 계산하므로, "
            "**부담부증여가 유리한지는 두 세목을 합쳐야 판단할 수 있습니다.** "
            "증여세는 세무 전문가의 확인을 받으세요.",
            "warn",
        )
        R.alternatives(bg_result.trace.all_alternatives(), "부담부증여에서 확인할 항목")
        with st.expander("부담부증여 계산 근거"):
            R.trace_tree(bg_result.trace)


# ==========================================================================
# ④ 상담 — 런타임 LLM 호출 0회
# ==========================================================================

with tab_advice:
    st.markdown("### 이 상황에서 알아야 할 것")
    st.caption(
        "매번 새로 생성한 문장이 아닙니다. **개발 시점에** 여러 검토자가 조문과 대조해 "
        "만들어 둔 상담 노트를, 엔진이 판정한 결과로 골라 꺼냅니다. "
        "그래서 같은 상황에는 언제나 같은 답이 나오고, 모든 문장에 근거 조문이 붙습니다."
    )

    plan_sale = st.checkbox(
        "가까운 시일에 팔 생각이 있습니다",
        key="advice_sale",
        help="양도 관련 항목을 함께 보여드립니다.",
    )

    advice_case = build_case(year)
    advice_result = compute_jongbuse(
        advice_case, ME, rs, track=effective_track, options=main_options()
    )
    picked = A_ADVISE(
        advice_case, ME, rs,
        track=effective_track,
        result=advice_result,
        transfer_planned=plan_sale,
    )

    R.advisories(
        picked,
        empty_hint=(
            "이 상황에 해당하는 상담 노트가 아직 없습니다. "
            "계산 결과와 근거 조문은 ②·③ 탭에서 확인하실 수 있습니다."
        ),
    )

    if picked:
        st.caption(
            f"{len(picked)}건 · 조건이 구체적인 항목부터 보여드립니다. "
            "여기 없는 특례가 적용될 수 있으니, 실제 신고 전에는 세무 전문가의 확인을 받으세요."
        )


st.divider()
st.caption(DISCLAIMER)
