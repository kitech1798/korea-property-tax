"""상생임대주택 특례 · 일시적 2주택 단축 — 룰셋 회귀 테스트.

이 프로젝트가 배운 것: **세액이 아니라 표를 고정한다.** 세액은 우연히 맞을 수
있지만 표는 아니다. 2028년 3주택 중과세율이 +15%p여야 하는데 0.20으로 들어간 채
335개 테스트를 통과한 사건이 그 교훈의 출처다.

그래서 여기서는 계산 결과가 아니라 **룰셋이 내놓는 값 자체**를 정부 원문과
글자 그대로 대조한다.
"""

from __future__ import annotations

from datetime import date

import pytest

from realestate_tax.rules import MissingRule, RuleSet, Track, default_ruleset_root

SANGSAENG = "transfer.sangsaeng_lease"
HOUSE_COUNT = "transfer.house_count_specials"


@pytest.fixture(scope="module")
def rs() -> RuleSet:
    return RuleSet.load(default_ruleset_root())


def sangsaeng(rs: RuleSet, on: date, track: Track) -> dict:
    return dict(rs.resolve(SANGSAENG, on=on, track=track).block.payload)


def temporary_two(rs: RuleSet, on: date, track: Track) -> dict:
    payload = rs.resolve(HOUSE_COUNT, on=on, track=track).block.payload
    return dict(payload["temporary_two"])


# --------------------------------------------------------------------------
# 상생임대 — §155의3① 요건을 원문 그대로 고정한다
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "track, on",
    [
        (Track.CURRENT, date(2026, 6, 1)),
        (Track.CURRENT, date(2028, 6, 1)),
        (Track.REFORM, date(2026, 6, 1)),
        (Track.REFORM, date(2028, 6, 1)),
    ],
)
def test_상생임대_요건은_트랙과_시점에_상관없이_같다(rs: RuleSet, track, on):
    """개편안이 바꾸는 것은 **양도기한**뿐이다. 요건 자체는 손대지 않는다.

    개조식 p.21의 "적용기한 종료(~'26.12.31.)"를 '새 기한 신설'로 오독하면
    요건이 바뀐 줄 알고 룰셋을 잘못 고치게 된다. 그 오독을 여기서 막는다.
    """
    p = sangsaeng(rs, on, track)
    assert p["contract_window"] == {"from": date(2021, 12, 20), "to": date(2026, 12, 31)}
    assert p["rent_increase_cap"] == "0.05"
    assert p["prior_lease_min_months"] == 18
    assert p["sangsaeng_lease_min_months"] == 24


def test_승계받은_계약은_직전임대차계약에서_제외된다(rs: RuleSet):
    """§155의3①1호 괄호 — "해당 주택의 취득으로 임대인의 지위가 승계된 경우의
    임대차계약은 제외". 상세본 p.78도 "* 주택 매수로 승계받은 계약 제외"로 확인한다.

    세입자가 살던 집을 사서 물려받은 계약을 직전계약으로 세면 요건을 충족한다고
    잘못 안내하게 된다. 상생임대 판정에서 가장 흔한 함정이다.
    """
    assert sangsaeng(rs, date(2026, 6, 1), Track.CURRENT)["exclude_succeeded_prior_lease"] is True


def test_계약금_지급_증빙이_요건이다(rs: RuleSet):
    """§155의3①1호 "계약금을 지급받은 사실이 증빙서류에 의해 확인되는 경우로 한정한다"."""
    assert sangsaeng(rs, date(2026, 6, 1), Track.CURRENT)["require_down_payment_evidence"] is True


def test_거주기간을_의제하지_않는다(rs: RuleSet):
    """★★ 이 프로젝트에서 가장 비싼 한 줄이다.

    §155의3①은 §159의4(장기보유특별공제)를 적용할 때 "해당 규정에 따른 **거주기간의
    제한**을 받지 않는다"고 한다. §159의4는 표2 대상을 "보유기간 중 거주기간이 2년
    이상인 것"으로 정의하므로, 면제되는 것은 **표2 진입 요건**이다.

    거주기간을 2년으로 의제하는 규정은 없다. 그러므로 표2 안의 거주기간 공제율은
    실제 거주기간으로 계산한다 — 실거주 0년이면 거주공제 0%.

    이 값이 False로 뒤집히면 2026 개편안 아래에서 장특공제가 통째로 틀린다.
    개편안이 보유공제를 거주공제로 옮기기 때문이다('27 40% → '28 20% → '29 0%).
    """
    for track in (Track.CURRENT, Track.REFORM):
        p = sangsaeng(rs, date(2028, 6, 1), track)
        assert p["waives_residence_requirement_only"] is True
        assert "장기보유특별공제" in " ".join(p["waives_ko"])


# --------------------------------------------------------------------------
# 상생임대 — 개편안이 추가하는 양도기한
# --------------------------------------------------------------------------


def test_현행에는_양도기한_요건이_없다(rs: RuleSet):
    """현행 §155의3에는 "언제까지 팔아야 한다"는 요건이 없다. 개편안이 신설한다."""
    assert "transfer_deadline" not in sangsaeng(rs, date(2028, 6, 1), Track.CURRENT)


def test_개편안이라도_시행일_전_양도에는_기한이_없다(rs: RuleSet):
    """상세본 p.78 <적용시기> "'26.10.1. 이후 양도하는 분부터 적용"."""
    assert "transfer_deadline" not in sangsaeng(rs, date(2026, 9, 30), Track.REFORM)


def test_개편안_양도기한을_상세본_표대로_고정한다(rs: RuleSet):
    """상세본 p.78 <추가> "다음 기한 이내에 양도
        ➊ '26.12.31. 이전 상생임대차계약 종료: '27.12.31.
        ➋ '27.1.1. 이후 상생임대차계약 종료: 계약종료 후 1년이 되는 날과
           '29.12.31. 중 빠른 날"

    개조식 p.21은 같은 내용을 "최대 '29.12.31.까지 인정(※ '26.12.31. 이전 종료시
    '27.12.31.까지)"로 압축한다. 두 원문이 일치하는 것을 확인하고 상세본을 담았다.
    """
    d = sangsaeng(rs, date(2026, 10, 1), Track.REFORM)["transfer_deadline"]
    assert d["lease_ended_on_or_before"] == date(2026, 12, 31)
    assert d["deadline_if_ended_early"] == date(2027, 12, 31)
    assert d["months_after_lease_end"] == 12
    assert d["absolute_cap"] == date(2029, 12, 31)


# --------------------------------------------------------------------------
# 일시적 2주택 — 조정대상지역만 3년 → 2년
# --------------------------------------------------------------------------


def test_현행_처분기한은_3년이고_단축_규정이_없다(rs: RuleSet):
    t = temporary_two(rs, date(2028, 6, 1), Track.CURRENT)
    assert t["max_years_to_sell_old"] == 3
    assert t["min_years_before_new"] == 1
    assert "regulated_shortened" not in t


def test_개편안이라도_시행일_전_양도는_3년이다(rs: RuleSet):
    """상세본 p.73 <적용시기> "'26.10.1. 이후 조정대상지역 내 종전주택 양도분부터"."""
    t = temporary_two(rs, date(2026, 9, 30), Track.REFORM)
    assert t["max_years_to_sell_old"] == 3
    assert "regulated_shortened" not in t


def test_단축은_두_주택이_모두_조정대상지역일_때만이다(rs: RuleSet):
    """상세본 p.73 단서신설의 각주 — "조정대상지역 소재 종전주택을 보유한 상태에서
    조정대상지역 소재 신규주택 취득".

    하나라도 비규제면 3년 그대로다. 기본값을 2년으로 바꿔 버리면 비규제 지역
    보유자에게 **없는 기한을 만들어** 팔라고 재촉하게 된다.
    """
    t = temporary_two(rs, date(2026, 10, 1), Track.REFORM)
    assert t["max_years_to_sell_old"] == 3  # 기본은 여전히 3년

    short = t["regulated_shortened"]
    assert short["max_years_to_sell_old"] == 2
    assert short["requires_both_regulated"] is True


def test_경과조치_기준일은_2026_08_04다(rs: RuleSet):
    """'26.8.3. 이전 취득·계약금 지급은 종전규정(3년). 하루 차이로 1년이 갈린다.

    개조식 p.21은 "'26.8.4. 이후 신규취득"으로, 상세본 p.73은 "'26.8.3. 이전 …
    종전규정 적용"으로 적는다. 같은 경계를 양쪽에서 말한 것이라 값은 하나다.
    """
    short = temporary_two(rs, date(2026, 10, 1), Track.REFORM)["regulated_shortened"]
    assert short["new_house_acquired_from"] == date(2026, 8, 4)


def test_분양권_경과조치는_판정하지_않고_드러낸다(rs: RuleSet):
    """상세본만 경과조치 대상에 "주택을 취득할 수 있는 권리"(분양권·입주권)를 적는다.
    개조식은 "주택"만 적는다. 넓은 쪽이 납세자에게 유리하지만 이 엔진은 분양권
    취득일을 입력받지 않으므로, 유리한 쪽으로 가정하지 않고 확인을 요구한다.
    """
    short = temporary_two(rs, date(2026, 10, 1), Track.REFORM)["regulated_shortened"]
    assert "분양권" in short["undecidable_note_ko"]


# --------------------------------------------------------------------------
# 해석 계약 — 어느 시점·트랙에서도 블록이 정확히 하나여야 한다
# --------------------------------------------------------------------------


RULE_START = {
    SANGSAENG: date(2021, 12, 20),   # §155의3①1호의 상생임대차계약 체결 가능 시작일
    HOUSE_COUNT: date(2023, 2, 28),  # 일시적 2주택 처분기한이 3년이 된 개정의 시행일
}


@pytest.mark.parametrize("rule_id", [SANGSAENG, HOUSE_COUNT])
@pytest.mark.parametrize("track", [Track.CURRENT, Track.REFORM])
@pytest.mark.parametrize(
    "on",
    [
        None,  # 그 규칙의 시행 첫날 — 경계는 규칙마다 다르다
        date(2026, 9, 30),
        date(2026, 10, 1),
        date(2027, 12, 31),
        date(2028, 1, 1),
        date(2030, 6, 1),
    ],
)
def test_모든_시점_트랙에서_블록이_정확히_하나_해석된다(rs: RuleSet, rule_id, track, on):
    """트랙을 쪼개면서 생기는 사고 두 가지를 한 번에 막는다.

      ① 구멍 — reform 블록만 만들고 시행일 전 구간을 안 채우면 MissingRule로 죽는다.
      ② 겹침 — current·reform을 둘 다 가진 블록 옆에 reform 블록을 더하면
              같은 시점에 두 개가 맞아 AmbiguousRule이 된다.

    린터도 겹침을 잡지만, 린터는 '기간이 겹치는가'만 본다. 실제로 해석되는지는
    해석해 봐야 안다.
    """
    resolution = rs.resolve(rule_id, on=on or RULE_START[rule_id], track=track)
    assert resolution.block.payload, f"{rule_id} / {track} / {on} 의 payload가 비었다"

    # ★ 개편안 트랙인데 현행으로 내려앉았다면, 그건 reform 블록이 없다는 뜻이다.
    #   resolver가 조용히 메워 주므로 테스트가 통과해 버린다. 여기서 못박는다.
    if track is Track.REFORM:
        assert not resolution.fell_back_to_current, (
            f"{rule_id} / {on} 에 개편안 블록이 없어 현행으로 폴백했다"
        )


def test_시행일_전_양도는_규칙이_없다고_말한다(rs: RuleSet):
    """조용히 기본값으로 때우지 않는다 — 이 프로젝트가 시중 계산기와 갈라지는 지점.

    일시적 2주택 처분기한 3년은 2023.2.28. 개정분이다. 그 전 양도는 다른 값이었고,
    이 룰셋은 담지 않는다. 담지 않은 것을 담은 척하는 대신 MissingRule로 드러낸다.
    (2026년 기준 상담 엔진이므로 과거 양도는 계산 대상이 아니다.)
    """
    with pytest.raises(MissingRule):
        rs.resolve(HOUSE_COUNT, on=date(2022, 1, 1), track=Track.REFORM)
