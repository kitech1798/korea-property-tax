"""상담 지식 — 개발 시점에 에이전트가 검토해 만들고, 런타임에는 규칙으로 매칭한다.

런타임 LLM 호출 0회. 그래서 비용이 없고, 같은 상황에 항상 같은 답이 나오며,
모든 문장의 근거 조문을 사전에 검증할 수 있다.
"""

from .advisor import advise, build_context, build_values, clear_cache, default_root, load
from .schema import (
    ALLOWED_KEYS,
    Advisory,
    AdvisoryError,
    AdvisorySeverity,
    Condition,
    RenderedAdvisory,
    parse_advisory,
    select,
)

__all__ = [
    "ALLOWED_KEYS",
    "Advisory",
    "AdvisoryError",
    "AdvisorySeverity",
    "Condition",
    "RenderedAdvisory",
    "advise",
    "build_context",
    "build_values",
    "clear_cache",
    "default_root",
    "load",
    "parse_advisory",
    "select",
]
