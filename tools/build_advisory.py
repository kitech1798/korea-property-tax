"""감사 산출 상담 지식(JSON) → `corpus/advisory/*.yaml`.

왜 손으로 안 옮기는가
    16건이 조문 인용으로 촘촘하다. 손으로 옮기면 **전사 오류**가 난다 —
    이번 감사에서 잡은 F01(2028년 중과세율 0.20 vs note의 +15%p)이 바로
    그 종류의 오류였다. 사람이 옮기지 않으면 그 오류는 생기지 않는다.

    대신 사람이 하는 일은 **조문을 원문과 대조하는 것**이다. 그건 자동화가 안 된다.

실행
    python tools/build_advisory.py            # 생성 + 스키마 검증
    python tools/build_advisory.py --check    # 생성하지 않고 검증만
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import yaml

# Windows 콘솔이 cp949로 뜨면 한글·기호 출력에서 죽는다. 도구가 인코딩 때문에
# 실패하면 안 되므로 표준출력을 UTF-8로 고정한다.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "audit" / "2026-08-04_advisory_draft.json"
OUT = ROOT / "corpus" / "advisory"

# 감사 워크플로에서 이 항목들을 만든 렌즈. 어떤 관점을 거쳤는지 데이터에 남긴다.
REVIEWED_BY = ("부부공동명의·세대", "거주요건·장특공제", "매도시점·중과", "편집장")

GROUPS = {
    "joint_spouse": "부부공동명의 1주택자 특례 (종부세법 §10의2)",
    "household": "세대·주택 수 판정",
    "residence": "거주요건 — 개편안의 무게중심",
    "sell_timing": "매도 시점 — 중과 한시완화 창구",
}


class Str(str):
    """여러 줄 문자열을 YAML 블록(|)으로 내보내기 위한 표시."""


def _block(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


yaml.add_representer(Str, _block)


def wrap(text: str, width: int = 78) -> str:
    """긴 문장을 읽을 수 있는 폭으로 접는다. 원문은 한 글자도 바꾸지 않는다."""
    out: list[str] = []
    for para in str(text).split("\n"):
        line = ""
        for word in para.split(" "):
            if line and len(line) + 1 + len(word) > width:
                out.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        out.append(line)
    return "\n".join(out)


def to_entry(raw: dict) -> dict:
    entry = {
        "id": raw["id"],
        "title_ko": raw["title_ko"],
        "severity": raw.get("severity", "fact"),
        "when": dict(raw["when"]),
        "basis": list(raw["basis"]),
        "fact_ko": Str(wrap(raw["fact_ko"])),
        "advice_ko": Str(wrap(raw["advice_ko"])),
        "caveats_ko": [Str(wrap(c)) for c in raw["caveats_ko"]],
        "reviewed_by": list(REVIEWED_BY),
    }
    if raw.get("uncertainty_ko"):
        entry["uncertainty_ko"] = Str(wrap(raw["uncertainty_ko"]))
    return entry


def build(write: bool) -> int:
    # ⚠️ PowerShell의 Out-File -Encoding utf8이 BOM을 붙인다. utf-8-sig로 읽는다.
    entries = json.loads(SRC.read_text(encoding="utf-8-sig"))
    grouped: dict[str, list[dict]] = defaultdict(list)
    for raw in entries:
        # id는 "adv.<그룹>.<이름>" 규약이다.
        parts = raw["id"].split(".")
        if len(parts) < 3 or parts[0] != "adv":
            raise SystemExit(f"id 규약 위반(adv.<그룹>.<이름>): {raw['id']}")
        grouped[parts[1]].append(to_entry(raw))

    unknown = set(grouped) - set(GROUPS)
    if unknown:
        raise SystemExit(f"GROUPS에 없는 그룹: {sorted(unknown)}")

    if write:
        OUT.mkdir(parents=True, exist_ok=True)
        for group, items in sorted(grouped.items()):
            header = (
                f"# {GROUPS[group]}\n"
                "#\n"
                "# ⚠️ 이 파일은 tools/build_advisory.py가 생성한다. 직접 고치지 말 것.\n"
                "#    원본: docs/audit/2026-08-04_advisory_draft.json\n"
                "#\n"
                "# 인용된 조문은 법제처 조문 API 원문으로 대조했다. 대조하지 못한 항목은\n"
                "# 넣지 않는다 — 그럴듯한 것보다 없는 게 낫다.\n\n"
            )
            body = yaml.dump(
                {"entries": items},
                allow_unicode=True,
                sort_keys=False,
                width=100,
                default_flow_style=False,
            )
            (OUT / f"{group}.yaml").write_text(header + body, encoding="utf-8")

    # ── 검증: 실제 로더로 읽어 스키마를 통과하는지 ────────────────────
    sys.path.insert(0, str(ROOT))
    from realestate_tax.advisory import ALLOWED_KEYS, clear_cache, load

    clear_cache()
    loaded = load()
    if len(loaded) != len(entries):
        raise SystemExit(f"로딩 개수 불일치: {len(loaded)} ≠ {len(entries)}")

    for a in loaded:
        assert a.basis, f"{a.id}: 근거 조문 없음"
        assert a.caveats_ko, f"{a.id}: 부작용 없음"
        assert set(a.when.raw) <= ALLOWED_KEYS, f"{a.id}: 허용되지 않은 조건 키"

    print(f"✓ {len(loaded)}건 · 그룹 {len(grouped)}개 · 스키마 통과")
    for group, items in sorted(grouped.items()):
        print(f"  {group:<14} {len(items):>2}건  {GROUPS[group]}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--check", action="store_true", help="생성하지 않고 검증만")
    args = p.parse_args()
    return build(write=not args.check)


if __name__ == "__main__":
    raise SystemExit(main())
