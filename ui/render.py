"""화면 렌더러 — 계산은 하지 않는다.

이 모듈에 세법 지식이 들어가는 순간 설계가 무너진다. 엔진이 만든 `TraceNode`를
그리기만 한다. 그래서 룰셋을 고치면 화면이 저절로 따라온다.

렌더링 원칙
  · 금액은 만원 단위(정부 문답자료와 같은 단위)로 보이고, 원 단위는 접어 둔다
  · 모든 단계에 **대입식**을 붙인다 — 사람이 손으로 검산할 수 있어야 한다
  · 미적용 대안(`alternatives_not_taken`)을 숨기지 않는다. 이게 시중 계산기의
    '유의사항: ○○ 미반영' 정적 면책을 대체하는 자리다
"""

from __future__ import annotations

import html
from typing import Iterable, Sequence

import streamlit as st

from realestate_tax.engine.trace import (
    Alternative,
    TraceNode,
    format_manwon,
    format_won,
)

_SEVERITY = {
    "국회 미통과": "warn",
    "시행령 미정": "warn",
    "가정": "warn",
    "추정치": "warn",
    "미상": "danger",
    "판단 필요": "danger",
    "사용자 입력": "muted",
    "공적 조회": "muted",
    "시행예정": "muted",
    "가정 판정": "warn",
}


def esc(text: object) -> str:
    return html.escape(str(text))


# --------------------------------------------------------------------------
# 배지 · 카드
# --------------------------------------------------------------------------


def badges(labels: Iterable[str]) -> None:
    """확실성 우려를 전부 나열한다.

    최솟값 하나만 보여주면 '국회 미통과'가 '가정'에 가려진다. 사용자는 무엇을
    조심해야 하는지 알 수 없게 된다.
    """
    items = list(dict.fromkeys(labels))
    if not items:
        return
    chips = "".join(
        f'<span class="rt-badge rt-badge--{_SEVERITY.get(t, "muted")}">{esc(t)}</span>'
        for t in items
    )
    st.markdown(f'<div class="rt-badges">{chips}</div>', unsafe_allow_html=True)


def card(label: str, value: str, sub: str = "", accent: bool = False) -> str:
    cls = "rt-card rt-card--accent" if accent else "rt-card"
    sub_html = f'<div class="rt-card__sub">{esc(sub)}</div>' if sub else ""
    return (
        f'<div class="{cls}">'
        f'<div class="rt-card__label">{esc(label)}</div>'
        f'<div class="rt-card__value">{esc(value)}</div>'
        f"{sub_html}</div>"
    )


def cards(items: Sequence[tuple[str, str, str, bool]]) -> None:
    body = "".join(card(*it) for it in items)
    st.markdown(f'<div class="rt-cards">{body}</div>', unsafe_allow_html=True)


def note(title: str, body: str, kind: str = "") -> None:
    cls = f"rt-note rt-note--{kind}" if kind else "rt-note"
    st.markdown(
        f'<div class="{cls}"><div class="rt-note__title">{esc(title)}</div>'
        f'<div class="rt-note__body">{esc(body)}</div></div>',
        unsafe_allow_html=True,
    )


def empty(icon: str, title: str, body: str) -> None:
    """빈 상태도 디자인한다. 빈 화면은 안내로 채운다."""
    st.markdown(
        f'<div class="rt-empty"><div class="rt-empty__icon">{esc(icon)}</div>'
        f"<div><strong>{esc(title)}</strong></div>"
        f'<div style="margin-top:6px">{esc(body)}</div></div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# 계산 근거 트리
# --------------------------------------------------------------------------


def _basis_html(node: TraceNode) -> str:
    parts: list[str] = []
    for rule in node.rules:
        if rule.basis is None:
            continue
        cite = esc(rule.basis.cite_ko())
        url = rule.basis.url()
        parts.append(f'<a href="{url}" target="_blank">{cite}</a>' if url else cite)
        if rule.source:
            parts[-1] += f" · {esc(rule.source.cite_ko())}"
    return " / ".join(parts)


def trace_tree(node: TraceNode, depth: int = 0) -> None:
    """계산 트리를 접이식으로 그린다.

    시중 계산기는 서버가 준 세액만 렌더링해서 "왜 이 숫자냐"에 답할 수 없다.
    여기서는 각 단계의 산식·대입값·근거조문이 전부 열린다.
    """
    known = node.output.is_known
    amount = (
        format_manwon(node.output.amount)
        if known and node.output.unit == "KRW"
        else (esc(node.output.label) or "—")
    )
    label = f"{'　' * depth}{node.label_ko}"

    with st.expander(f"{label} · {amount}", expanded=depth == 0):
        if node.formula:
            st.markdown(f"**{esc(node.formula)}**")
        if node.substitution:
            st.markdown(
                f'<div class="rt-formula">{esc(node.substitution)}</div>',
                unsafe_allow_html=True,
            )
        if known and node.output.unit == "KRW":
            st.caption(f"= {format_won(node.output.amount)}")

        if node.branch:
            st.caption(
                f"분기 — {node.branch.condition_ko}: **{node.branch.taken}**"
                + (f" ({node.branch.detail_ko})" if node.branch.detail_ko else "")
            )

        basis = _basis_html(node)
        if basis:
            st.markdown(f'<div class="rt-basis">근거 · {basis}</div>', unsafe_allow_html=True)

        if node.note_ko:
            st.caption(node.note_ko)

        if node.output.unknown is not None:
            note(
                "이 값은 확정되지 않았습니다",
                f"사유: {node.output.unknown}. 필요한 정보를 입력하시면 정확해집니다.",
                "warn",
            )

        for child in node.children:
            trace_tree(child, depth + 1)


def alternatives(items: Sequence[Alternative], title: str = "적용되지 않은 항목") -> None:
    """미적용 특례를 사유와 함께 보여준다.

    시중 계산기의 '유의사항: 세부담상한·고령자공제·특례 미반영' 같은 정적 면책 문구를
    대체하는 자리다. 그 면책 목록이 곧 실제 납세자 집합이라는 게 문제의 본질이었다.
    """
    if not items:
        return
    st.markdown(f"### {title}")
    st.caption("엔진이 실제로 판정한 결과입니다. 해당되지 않는 항목은 사유가 함께 표시됩니다.")

    actionable = [a for a in items if a.actionable]
    others = [a for a in items if not a.actionable]

    for alt in actionable:
        body = alt.reason_ko
        if alt.delta is not None and alt.delta.is_known:
            body += f"  (적용 시 {format_manwon(abs(alt.delta.amount))} 차이)"
        note(f"✓ {alt.label_ko}", body, "action")

    if others:
        with st.expander(f"해당 없음 {len(others)}건", expanded=False):
            for alt in others:
                body = alt.reason_ko
                if alt.delta is not None and alt.delta.is_known:
                    body += f"  (적용 시 {format_manwon(abs(alt.delta.amount))} 차이)"
                st.markdown(f"- **{esc(alt.label_ko)}** — {esc(body)}")


_ADVISORY_ICON = {"opportunity": "💡", "caution": "⚠️", "fact": "📌"}
_ADVISORY_LABEL = {"opportunity": "기회", "caution": "주의", "fact": "알아둘 것"}


def advisories(items: Sequence, empty_hint: str = "") -> None:
    """상담 지식. **근거 조문과 부작용을 접지 않고 함께 편다.**

    조언만 크게 보여주고 부작용을 접어두면 사용자는 부작용을 안 읽는다.
    "부부공동명의로 바꾸세요"만 보고 증여세를 모르면 조언이 아니라 함정이다.
    """
    if not items:
        if empty_hint:
            st.caption(empty_hint)
        return

    order = {"caution": 0, "opportunity": 1, "fact": 2}
    for r in sorted(items, key=lambda a: order.get(str(a.advisory.severity), 9)):
        sev = str(r.advisory.severity)
        with st.container(border=True):
            st.markdown(
                f"**{_ADVISORY_ICON.get(sev, '·')} {esc(r.title_ko)}**"
                f"　<span class='rt-badge rt-badge--"
                f"{'warn' if sev == 'caution' else 'muted'}'>"
                f"{_ADVISORY_LABEL.get(sev, sev)}</span>",
                unsafe_allow_html=True,
            )
            st.markdown(r.fact_ko)
            st.markdown(f"**어떻게 하면 되나**\n\n{r.advice_ko}")
            if r.advisory.caveats_ko:
                st.markdown(
                    "**놓치면 손해 보는 것**\n"
                    + "\n".join(f"- {c}" for c in r.advisory.caveats_ko)
                )
            if r.advisory.uncertainty_ko:
                note("확실하지 않은 부분", r.advisory.uncertainty_ko, "warn")
            st.caption("근거 · " + " / ".join(r.advisory.basis))


def table(headers: Sequence[str], rows: Sequence[Sequence[str]], best_row: int | None = None) -> None:
    """표. 넓은 표는 자체 스크롤 — 페이지 본문이 가로로 밀리면 안 된다."""
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = ""
    for i, row in enumerate(rows):
        cls = ' class="is-best"' if best_row == i else ""
        cells = "".join(f"<td>{esc(c)}</td>" for c in row)
        body += f"<tr{cls}>{cells}</tr>"
    st.markdown(
        f'<div class="rt-table__wrap"><table class="rt-table">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>",
        unsafe_allow_html=True,
    )
