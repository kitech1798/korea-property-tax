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
    InheritedMeta,
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
from realestate_tax.engine.strategy import consult, sell_timing
from realestate_tax.engine.trace import format_manwon
from realestate_tax.engine.transfer_tax import TransferEvent, compute_transfer_tax
from realestate_tax.intake import LOOKUP_GUIDE_KO, Severity, intake
from realestate_tax.rules import RuleSet, Track, default_ruleset_root
from ui import address as A
from ui import render as R
from ui.theme import CSS, DISCLAIMER

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
            continue
        if value:
            os.environ[name] = str(value)


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
            "rental": False,
            "rental_declared": False,
            "urban": True,
        }
    ]

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
            st.session_state.houses.append(dict(st.session_state.houses[0], name=f"주택{len(st.session_state.houses) + 1}"))
            st.rerun()
        if c2.button("－ 마지막 주택 삭제", use_container_width=True, disabled=len(st.session_state.houses) <= 1):
            st.session_state.houses.pop()
            st.rerun()

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
            h["rental"] = f.checkbox("등록임대주택", h["rental"], key=f"rt{i}")
            if h["rental"]:
                h["rental_declared"] = f.checkbox(
                    "합산배제 신고했습니다", h["rental_declared"], key=f"rc{i}",
                    help="임대유형·의무임대기간·가액요건은 등록증으로만 확인됩니다. "
                    "확인해주시기 전에는 유리하게 가정하지 않습니다.",
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

    props, owns, spells = [], [], []
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
            spells.append(
                ResidenceSpell(
                    ME, pid, start=date(target_year - max(1, h["residence_years"]), 1, 1)
                )
            )

    return TaxCase(
        year=target_year,
        persons=tuple(persons),
        households=(Household(id=HH, member_ids=tuple(members)),),
        properties=tuple(props),
        ownerships=tuple(owns),
        residences=tuple(spells),
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
    result = compute_jongbuse(case, ME, rs, track=effective_track, options=opts)

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
                st.markdown(f"**{s.label_ko}** — {format_manwon(s.saving)} 절감")
                st.markdown(s.what_to_do_ko)
                st.caption(f"근거 · {s.basis_ko}")
                if s.requirements_ko:
                    st.markdown("**요건**\n" + "\n".join(f"- {r}" for r in s.requirements_ko))
                if s.caveats_ko:
                    st.markdown("**주의**\n" + "\n".join(f"- {c}" for c in s.caveats_ko))

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
    target = s1.selectbox("팔 주택", names)
    idx = names.index(target)
    sale_price = s2.number_input(
        "예상 양도가액(억원)", 1.0, 300.0, 30.0, step=0.5, format="%.1f"
    )
    buy_price = s3.number_input(
        "취득가액(억원)", 0.1, 300.0, 12.0, step=0.5, format="%.1f",
        help="세법상 정본은 **매매계약서**입니다(소득세법 §97①1). "
        "실거래가 공개시스템 값은 신고가액의 공개본이지 세법상 정본이 아닙니다.",
    )

    if sale_price <= buy_price:
        R.empty("📉", "양도차익이 없습니다", "양도가액이 취득가액보다 크게 설정해주세요.")
    else:
        h = st.session_state.houses[idx]
        base_case = build_case(2027)
        event = TransferEvent(
            property_id=PropertyId(f"h{idx}"),
            person_id=ME,
            transfer_date=date(2027, 6, 1),
            transfer_price=int(sale_price * EOK),
            acquisition_price=int(buy_price * EOK),
            holding_years=h["holding_years"],
            residence_years=h["residence_years"],
        )
        timing = sell_timing(
            base_case, ME, event, rs, track=track, options=main_options(), growth=growth  # 매도는 2027년 이후만 보므로 원래 트랙
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

        detail = compute_transfer_tax(
            base_case, replace(event, transfer_date=date(timing.best.year, 6, 1)), rs, track=track
        )
        R.badges(k for k, _ in detail.trace.certainty_concerns())
        R.alternatives(detail.trace.all_alternatives(), "양도세에서 적용되지 않은 항목")

        st.markdown("#### 계산 근거")
        R.trace_tree(detail.trace)


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
