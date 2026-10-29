"""상품 카테고리 체계 로더·검증기 (D002).

사용법:
    python -m src.schema.categories            # 검증 + 요약 출력
    python -m src.schema.categories --csv out.csv   # 소분류 30개를 평탄화한 CSV 저장

데이터: data/schema/categories.yaml (대 > 중 > 소 3단계)
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO_ROOT / "data" / "schema" / "categories.yaml"

CODE_RE = {
    1: re.compile(r"^[A-Z]{2}$"),
    2: re.compile(r"^[A-Z]{2}-[A-Z]{2}$"),
    3: re.compile(r"^[A-Z]{2}-[A-Z]{2}-[A-Z]{3}$"),
}
LEAF_FIELDS = ("expiry_class", "seasonality", "size_class", "regulation", "examples")
EXPECTED_LEAF_COUNT = 30


@dataclass
class Leaf:
    code: str
    name: str
    l1_code: str
    l1_name: str
    l2_code: str
    l2_name: str
    expiry_class: str
    seasonality: list[str] = field(default_factory=list)
    size_class: str = ""
    regulation: str = ""
    examples: list[str] = field(default_factory=list)


class CategoryError(ValueError):
    pass


def load_raw(path: Path = DEFAULT_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def flatten(raw: dict) -> list[Leaf]:
    leaves: list[Leaf] = []
    for l1 in raw["categories"]:
        for l2 in l1.get("children", []):
            for l3 in l2.get("children", []):
                leaves.append(
                    Leaf(
                        code=l3["code"],
                        name=l3["name"],
                        l1_code=l1["code"],
                        l1_name=l1["name"],
                        l2_code=l2["code"],
                        l2_name=l2["name"],
                        expiry_class=l3.get("expiry_class", ""),
                        seasonality=list(l3.get("seasonality") or []),
                        size_class=l3.get("size_class", ""),
                        regulation=l3.get("regulation", ""),
                        examples=list(l3.get("examples") or []),
                    )
                )
    return leaves


def validate(raw: dict) -> list[str]:
    """규칙 위반 목록을 돌려준다. 비어 있으면 통과."""
    errors: list[str] = []
    enums = raw.get("enums", {})
    seen: Counter[str] = Counter()

    def check_code(level: int, code: str, parent: str | None) -> None:
        if not CODE_RE[level].match(code):
            errors.append(f"{code}: level{level} 코드 형식 위반")
        if parent and not code.startswith(parent + "-"):
            errors.append(f"{code}: 상위 코드 {parent} 접두 불일치")
        seen[code] += 1

    for l1 in raw["categories"]:
        check_code(1, l1["code"], None)
        if not l1.get("children"):
            errors.append(f"{l1['code']}: 중분류 없음")
        for l2 in l1.get("children", []):
            check_code(2, l2["code"], l1["code"])
            if not l2.get("children"):
                errors.append(f"{l2['code']}: 소분류 없음")
            for l3 in l2.get("children", []):
                check_code(3, l3["code"], l2["code"])
                for fld in LEAF_FIELDS:
                    if fld not in l3:
                        errors.append(f"{l3['code']}: 필수 속성 누락 {fld}")
                for fld in ("expiry_class", "size_class", "regulation"):
                    if fld in l3 and l3[fld] not in enums.get(fld, []):
                        errors.append(f"{l3['code']}: {fld}={l3[fld]} 허용값 아님")
                for s in l3.get("seasonality") or []:
                    if s not in enums.get("seasonality", []):
                        errors.append(f"{l3['code']}: seasonality={s} 허용값 아님")
                if len(l3.get("examples") or []) < 3:
                    errors.append(f"{l3['code']}: examples 3개 미만")

    for code, n in seen.items():
        if n > 1:
            errors.append(f"{code}: 코드 중복 {n}회")

    names = Counter(l.name for l in flatten(raw))
    for name, n in names.items():
        if n > 1:
            errors.append(f"소분류 이름 중복: {name} ({n}회)")

    leaf_count = len(flatten(raw))
    if leaf_count != EXPECTED_LEAF_COUNT:
        errors.append(f"소분류 개수 {leaf_count} != {EXPECTED_LEAF_COUNT}")
    return errors


def load(path: Path = DEFAULT_PATH) -> list[Leaf]:
    raw = load_raw(path)
    errors = validate(raw)
    if errors:
        raise CategoryError("\n".join(errors))
    return flatten(raw)


def summary(raw: dict, leaves: list[Leaf]) -> str:
    lines = []
    n_l1 = len(raw["categories"])
    n_l2 = sum(len(l1.get("children", [])) for l1 in raw["categories"])
    lines.append(f"version={raw.get('version')} status={raw.get('status')}")
    lines.append(f"대분류 {n_l1} / 중분류 {n_l2} / 소분류 {len(leaves)}")
    lines.append("")
    lines.append("대분류별 소분류 수:")
    for l1 in raw["categories"]:
        n = sum(1 for l in leaves if l.l1_code == l1["code"])
        lines.append(f"  {l1['code']} {l1['name']}: {n}")
    lines.append("")
    for fld in ("expiry_class", "size_class", "regulation"):
        c = Counter(getattr(l, fld) for l in leaves)
        lines.append(f"{fld}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    sc = Counter(s for l in leaves for s in l.seasonality)
    lines.append("seasonality(태그 수): " + ", ".join(f"{k}={v}" for k, v in sorted(sc.items())))
    lines.append(f"seasonality 없음: {sum(1 for l in leaves if not l.seasonality)}")
    lines.append(f"examples 총 {sum(len(l.examples) for l in leaves)}개")
    return "\n".join(lines)


def write_csv(leaves: list[Leaf], out: Path) -> None:
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "name", "l1_code", "l1_name", "l2_code", "l2_name",
                    "expiry_class", "seasonality", "size_class", "regulation", "examples"])
        for l in leaves:
            w.writerow([l.code, l.name, l.l1_code, l.l1_name, l.l2_code, l.l2_name,
                        l.expiry_class, "|".join(l.seasonality), l.size_class,
                        l.regulation, "|".join(l.examples)])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", type=Path, default=DEFAULT_PATH)
    ap.add_argument("--csv", type=Path, default=None)
    args = ap.parse_args(argv)

    raw = load_raw(args.path)
    errors = validate(raw)
    if errors:
        print("검증 실패:")
        for e in errors:
            print("  -", e)
        return 1
    leaves = flatten(raw)
    print("검증 통과 (오류 0건)")
    print(summary(raw, leaves))
    if args.csv:
        write_csv(leaves, args.csv)
        print(f"CSV 저장: {args.csv} ({len(leaves)}행)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
