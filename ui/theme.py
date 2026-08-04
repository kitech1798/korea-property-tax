"""화면 토큰과 공통 스타일.

색을 직접 쓰지 않고 의미 토큰(bg/surface/ink/sub/accent/warn/danger)만 쓴다.
다크·라이트를 모두 지원하고, 본문 대비는 WCAG AA(4.5:1) 이상을 목표로 한다.

한글 조판 규약
  · `word-break: keep-all` — 단어 중간에서 줄이 끊기지 않게 한다
  · 본문 16px 이상, 줄높이 1.6~1.75
  · 숫자는 `font-variant-numeric: tabular-nums` — 표에서 자릿수가 흔들리지 않게
"""

from __future__ import annotations

CSS = """
<style>
:root {
  --bg: #f7f8fa;
  --surface: #ffffff;
  --surface-2: #f1f3f6;
  --border: #e2e6ec;
  --ink: #14181f;
  --sub: #5a6472;
  --accent: #1f5eff;
  --accent-soft: #eaf0ff;
  --warn: #a86400;
  --warn-soft: #fff5e2;
  --danger: #b3261e;
  --danger-soft: #fdecea;
  --ok: #1a7f5a;
  --ok-soft: #e7f5ef;
  --radius: 12px;
  --gap: 8px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1216;
    --surface: #171b21;
    --surface-2: #1e232b;
    --border: #2a313b;
    --ink: #e9edf3;
    --sub: #9aa4b2;
    --accent: #6f9bff;
    --accent-soft: #1a2540;
    --warn: #e0a13a;
    --warn-soft: #33280f;
    --danger: #ef8078;
    --danger-soft: #3a1a17;
    --ok: #5fd3a6;
    --ok-soft: #102b22;
  }
}

html, body, [class*="css"] { word-break: keep-all; }
.block-container { padding-top: 2.2rem; max-width: 1160px; }
h1, h2, h3 { letter-spacing: -0.02em; line-height: 1.3; }
h1 { font-size: 1.9rem; }
h2 { font-size: 1.35rem; margin-top: 2rem; }
h3 { font-size: 1.08rem; }
p, li { line-height: 1.7; }

.rt-lede {
  color: var(--sub); font-size: 0.95rem; line-height: 1.7;
  margin: -0.4rem 0 1.4rem;
}

/* ── 배지 ────────────────────────────────────────────── */
.rt-badges { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0 12px; }
.rt-badge {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 10px; border-radius: 999px;
  font-size: 0.78rem; font-weight: 600; line-height: 1.5;
  border: 1px solid transparent;
}
.rt-badge--warn { background: var(--warn-soft); color: var(--warn); border-color: var(--warn); }
.rt-badge--danger { background: var(--danger-soft); color: var(--danger); border-color: var(--danger); }
.rt-badge--ok { background: var(--ok-soft); color: var(--ok); border-color: var(--ok); }
.rt-badge--muted { background: var(--surface-2); color: var(--sub); border-color: var(--border); }

/* ── 세액 카드 ───────────────────────────────────────── */
.rt-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
.rt-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 16px 18px;
}
.rt-card__label { color: var(--sub); font-size: 0.82rem; margin-bottom: 6px; }
.rt-card__value {
  font-size: 1.5rem; font-weight: 700; color: var(--ink);
  font-variant-numeric: tabular-nums; letter-spacing: -0.02em;
}
.rt-card__sub { color: var(--sub); font-size: 0.8rem; margin-top: 4px; }
.rt-card--accent { background: var(--accent-soft); border-color: var(--accent); }
.rt-card--accent .rt-card__value { color: var(--accent); }

/* ── 계산 근거 ───────────────────────────────────────── */
.rt-formula {
  font-family: ui-monospace, "SFMono-Regular", Consolas, monospace;
  font-size: 0.85rem; line-height: 1.7;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 12px; margin: 6px 0;
  overflow-x: auto; white-space: pre-wrap; word-break: break-all;
  color: var(--ink);
}
.rt-basis { color: var(--sub); font-size: 0.8rem; margin-top: 4px; }
.rt-basis a { color: var(--accent); text-decoration: none; }
.rt-basis a:hover { text-decoration: underline; }

/* ── 안내 블록 ───────────────────────────────────────── */
.rt-note {
  border-left: 3px solid var(--border); padding: 10px 14px; margin: 8px 0;
  background: var(--surface); border-radius: 0 8px 8px 0;
  font-size: 0.9rem; line-height: 1.7; color: var(--ink);
}
.rt-note--warn { border-left-color: var(--warn); background: var(--warn-soft); }
.rt-note--action { border-left-color: var(--accent); background: var(--accent-soft); }
.rt-note__title { font-weight: 700; margin-bottom: 4px; }
.rt-note__body { color: var(--sub); }

/* ── 빈 상태 ─────────────────────────────────────────── */
.rt-empty {
  text-align: center; padding: 56px 24px; color: var(--sub);
  background: var(--surface); border: 1px dashed var(--border);
  border-radius: var(--radius);
}
.rt-empty__icon { font-size: 2rem; margin-bottom: 10px; opacity: 0.7; }

/* ── 표 ─────────────────────────────────────────────── */
.rt-table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
.rt-table th, .rt-table td {
  padding: 9px 12px; border-bottom: 1px solid var(--border); text-align: right;
  font-variant-numeric: tabular-nums;
}
.rt-table th { color: var(--sub); font-weight: 600; font-size: 0.82rem; text-align: right; }
.rt-table th:first-child, .rt-table td:first-child { text-align: left; }
.rt-table tr.is-best td { background: var(--ok-soft); font-weight: 600; }
.rt-table__wrap { overflow-x: auto; }

/* 터치 타깃 44px 확보 */
.stButton > button { min-height: 44px; border-radius: 10px; font-weight: 600; }
.stSelectbox div[data-baseweb="select"] { min-height: 44px; }

/* 포커스 링 — 키보드 접근성 */
*:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
</style>
"""

DISCLAIMER = (
    "이 계산은 입력하신 값을 기준으로 한 **참고용 추정치**이며, 실제 고지세액과 다를 수 있습니다. "
    "2026년 세제개편안은 국회 통과 전이므로 확정된 제도가 아닙니다. "
    "세무 대리·신고 업무를 대신하지 않으며, 최종 판단은 세무 전문가와 관할 관청 확인을 거치시기 바랍니다."
)
