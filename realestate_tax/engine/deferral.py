"""종합부동산세 납부유예(종부세법 §20의2) 자격 판정.

★ **이건 절세가 아니다.** 세금을 줄이는 게 아니라 미루는 것이고, 허가가 취소되면
  이자상당가산액이 붙어 함께 징수된다(§20의2⑤). 그래서 `Strategy`로 만들지 않았다 —
  절감액 칸에 숫자를 넣는 순간 사용자는 그만큼 안 내도 되는 줄 안다.

  그럼에도 반드시 있어야 한다. **집은 있는데 현금이 없는 고령 1주택자**에게는
  이게 유일한 현실적 답이고, 개편안이 보유세를 올리면서 쓸모가 커졌다.
  "세금을 줄일 방법이 없습니다"로 끝내면 그 사람은 집을 팔아야 한다.

판정 원칙
  요건 4개 중 **셋은 사건에서 판정**되고 소득 기준 하나는 모델에 없다.
  모르는 것을 충족한 것으로 가정하지 않는다 — 판정된 셋을 보여주고 소득은 묻는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..domain.models import PersonId, TaxCase, Won
from ..rules.resolver import RuleSet
from ..rules.schema import Track

J = "jongbuse.house"


@dataclass(frozen=True, slots=True)
class DeferralCheck:
    """납부유예 자격 판정 결과."""

    eligible_so_far: bool
    """엔진이 판정할 수 있는 요건을 **전부** 통과했는가. 소득 기준은 포함되지 않는다."""
    met_ko: tuple[str, ...]
    failed_ko: tuple[str, ...]
    asks_ko: tuple[str, ...]
    """엔진이 판정하지 못해 사용자에게 물어야 하는 것."""
    deferrable: Won = 0
    """유예할 수 있는 금액 — 해당 연도 주택분 종합부동산세액."""
    revoke_reasons_ko: tuple[str, ...] = ()

    @property
    def worth_showing(self) -> bool:
        """요건을 하나라도 갖췄고 세액이 문턱을 넘을 때만 화면에 낸다.

        해당 없는 사람에게까지 띄우면 진짜 해당되는 사람의 안내가 묻힌다.
        """
        return self.deferrable > 0 and not self.failed_ko


def check_deferral(
    case: TaxCase,
    person_id: PersonId,
    ruleset: RuleSet,
    *,
    jongbuse_amount: Won,
    one_house: bool,
    holding_years: int | None,
    track: Track = Track.CURRENT,
    on: date | None = None,
) -> DeferralCheck:
    """납부유예 요건을 사건의 사실로 판정한다.

    `jongbuse_amount`는 **주택분 종합부동산세액**이다(농특세 제외).
    조문이 "해당 연도의 주택분 종합부동산세액이 100만원을 초과할 것"이라 못 박는다.
    """
    on = on or case.assessment_date
    res = ruleset.resolve(f"{J}.deferral", on=on, track=track)
    p = res.block.payload

    person = case.find_person(person_id)
    age = person.age_at(on)
    min_age = int(p["min_age"])
    min_hold = int(p["min_holding_years"])
    min_tax = int(p["min_tax"])

    met: list[str] = []
    failed: list[str] = []
    asks: list[str] = []

    # ── 1호 1세대1주택자 ────────────────────────────────────────────
    if one_house:
        met.append("과세기준일 현재 1세대1주택자입니다(§20의2①1호)")
    else:
        failed.append("1세대1주택자가 아닙니다 — 납부유예는 1세대1주택자만 신청할 수 있습니다")

    # ── 2호 연령 **또는** 보유기간 ─────────────────────────────────
    #   조문이 "이거나"로 잇는다. 하나만 충족해도 된다 — 둘 다 요구하면 잘못 막는다.
    age_ok = age is not None and age >= min_age
    hold_ok = holding_years is not None and holding_years >= min_hold
    if age_ok:
        met.append(f"만 {age}세로 {min_age}세 이상입니다(§20의2①2호)")
    elif hold_ok:
        met.append(f"해당 주택을 {holding_years}년 보유해 {min_hold}년 이상입니다(§20의2①2호)")
    elif age is None and holding_years is None:
        asks.append(
            f"만 {min_age}세 이상이거나 해당 주택을 {min_hold}년 이상 보유하셨나요? "
            "생년월일이나 취득일을 입력하시면 판정해드립니다"
        )
    else:
        failed.append(
            f"만 {min_age}세 미만이고 보유기간도 {min_hold}년에 못 미칩니다"
            + (f" (만 {age}세" if age is not None else "")
            + (f", 보유 {holding_years}년)" if holding_years is not None else ")")
        )

    # ── 4호 세액 문턱 ──────────────────────────────────────────────
    if jongbuse_amount > min_tax:
        met.append(f"주택분 종합부동산세액이 {jongbuse_amount:,}원으로 {min_tax:,}원을 넘습니다(§20의2①4호)")
    else:
        failed.append(
            f"주택분 종합부동산세액이 {jongbuse_amount:,}원으로 {min_tax:,}원 이하입니다 — "
            "납부유예 대상이 아닙니다"
        )

    # ── 3호 소득 기준 — 사건에 없다. 가정하지 않고 묻는다 ──────────
    salary = int(p["income_salary_cap"])
    total = int(p["income_total_cap"])
    asks.append(
        f"직전 과세기간 총급여가 {salary:,}원 이하(근로소득만 있는 경우)이거나, "
        f"종합소득금액이 {total:,}원 이하인가요?(§20의2①3호) "
        "이 조건은 사건 정보로 판정할 수 없어 확인이 필요합니다"
    )

    return DeferralCheck(
        eligible_so_far=not failed,
        met_ko=tuple(met),
        failed_ko=tuple(failed),
        asks_ko=tuple(asks),
        deferrable=jongbuse_amount if not failed else 0,
        revoke_reasons_ko=tuple(p.get("revoke_reasons_ko") or ()),
    )


__all__ = ["DeferralCheck", "check_deferral"]
