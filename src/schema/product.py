"""상품 속성 스키마 로더·검증기 (D003).

사용법:
    python -m src.schema.product                       # 스키마 자체 점검 + 예시 레코드 검증 + 요약
    python -m src.schema.product --records some.jsonl  # JSONL 레코드 파일 검증

검증 2단계:
  1) JSON Schema(data/schema/product.json, draft 2020-12) — 타입·enum·범위·필수 필드·format(date)
  2) 교차 규칙(이 모듈) — 카테고리 코드 존재, expiry_class 에 따른 expiry_date 필수/금지,
     날짜 순서(제조일 ≤ 입고일 ≤ 기준일 < 유통기한), 등급 C 의 기한 임박 규칙,
     sealed 포장이면서 등급 B/C 인 모순.
  3) 경고(검증 실패 아님) — size_class 가 카테고리 기본값과 두 단계 차이(small↔large).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from src.schema.categories import Leaf, load as load_categories

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "data" / "schema" / "product.json"
EXAMPLES_PATH = REPO_ROOT / "data" / "schema" / "examples" / "product_examples.jsonl"

SIZE_ORDER = {"small": 0, "medium": 1, "large": 2}
NEAR_EXPIRY_DAYS = 90  # 등급 C 정의의 "유통기한 임박" 기준


def load_schema(path: Path = SCHEMA_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        schema = json.load(f)
    Draft202012Validator.check_schema(schema)
    return schema


def _validator(schema: dict) -> Draft202012Validator:
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def schema_errors(record: dict, validator: Draft202012Validator) -> list[str]:
    errs = []
    for e in sorted(validator.iter_errors(record), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in e.path) or "<root>"
        errs.append(f"[schema] {loc}: {e.message}")
    return errs


def cross_errors(record: dict, leaves: dict[str, Leaf], today: date) -> list[str]:
    """스키마만으로 표현 못 하는 규칙. 스키마 오류가 있는 레코드에는 호출하지 않는다."""
    return cross_checks(record, leaves, today)[0]


def cross_warnings(record: dict, leaves: dict[str, Leaf], today: date) -> list[str]:
    return cross_checks(record, leaves, today)[1]


def cross_checks(record: dict, leaves: dict[str, Leaf], today: date) -> tuple[list[str], list[str]]:
    """(오류, 경고). 경고는 검증 실패로 치지 않고 보고만 한다."""
    errs: list[str] = []
    warns: list[str] = []
    pid = record.get("product_id", "?")
    leaf = leaves.get(record["category_code"])
    if leaf is None:
        errs.append(f"[cross] {pid}: category_code {record['category_code']} 가 categories.yaml 에 없음")
        return errs, warns

    acquired = _d(record["acquired_date"])
    manufactured = _d(record.get("manufacture_date"))
    expiry = _d(record.get("expiry_date"))

    # 유통기한 필수/금지
    if leaf.expiry_class == "none" and expiry is not None:
        errs.append(f"[cross] {pid}: {leaf.code} 는 expiry_class=none 인데 expiry_date 가 있음")
    if leaf.expiry_class != "none" and expiry is None:
        errs.append(f"[cross] {pid}: {leaf.code} 는 expiry_class={leaf.expiry_class} 이므로 expiry_date 필수")

    # 날짜 순서
    if acquired > today:
        errs.append(f"[cross] {pid}: acquired_date {acquired} 가 기준일 {today} 이후")
    if manufactured and manufactured > acquired:
        errs.append(f"[cross] {pid}: manufacture_date {manufactured} 가 acquired_date {acquired} 이후")
    if expiry is not None:
        if expiry <= today:
            errs.append(f"[cross] {pid}: expiry_date {expiry} 가 기준일 {today} 이전·당일 → 재유통 불가, 입력 거부")
        if manufactured and expiry <= manufactured:
            errs.append(f"[cross] {pid}: expiry_date 가 manufacture_date 이전")
        # 기한 임박(90일 이내)인데 등급이 S/A 면 모순
        if (expiry - today).days <= NEAR_EXPIRY_DAYS and record["condition_grade"] in ("S", "A"):
            errs.append(
                f"[cross] {pid}: 유통기한 {(expiry - today).days}일 남음(임박)인데 condition_grade={record['condition_grade']} → C 로 조정 필요"
            )

    # 포장·등급 모순
    if record["packaging_state"] == "sealed" and record["condition_grade"] in ("B", "C"):
        if not (expiry is not None and (expiry - today).days <= NEAR_EXPIRY_DAYS):
            errs.append(f"[cross] {pid}: packaging_state=sealed 인데 condition_grade={record['condition_grade']} (기한 임박 아님) → 모순")
    if record["packaging_state"] in ("box_damaged", "no_box", "repackaged") and record["condition_grade"] == "S":
        errs.append(f"[cross] {pid}: packaging_state={record['packaging_state']} 인데 condition_grade=S → S 는 밀봉·원박스만 허용")

    # size_class 가 카테고리 기본값과 두 단계 차이(small↔large)면 경고 (입력 오류 가능성 점검용)
    if abs(SIZE_ORDER[record["spec"]["size_class"]] - SIZE_ORDER[leaf.size_class]) >= 2:
        warns.append(
            f"[warn] {pid}: spec.size_class={record['spec']['size_class']} 가 카테고리 {leaf.code} 기본 {leaf.size_class} 와 두 단계 차이"
        )
    return errs, warns


def validate_records(
    records: list[dict],
    schema: dict | None = None,
    leaves: dict[str, Leaf] | None = None,
    today: date | None = None,
    warnings: dict[int, list[str]] | None = None,
) -> dict[int, list[str]]:
    """{레코드 인덱스: 오류 목록}. 비어 있으면 전부 통과. warnings 딕셔너리를 넘기면 경고를 채워 준다."""
    schema = schema or load_schema()
    leaves = leaves or {l.code: l for l in load_categories()}
    today = today or date.today()
    v = _validator(schema)
    out: dict[int, list[str]] = {}
    for i, rec in enumerate(records):
        errs = schema_errors(rec, v)
        if not errs:
            errs, warns = cross_checks(rec, leaves, today)
            if warns and warnings is not None:
                warnings[i] = warns
        if errs:
            out[i] = errs
    return out


def read_jsonl(path: Path) -> list[dict]:
    recs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                recs.append(json.loads(line))
    return recs


def summary(records: list[dict], leaves: dict[str, Leaf]) -> str:
    lines = [f"레코드 {len(records)}건"]
    for fld in ("condition_grade", "packaging_state"):
        c = Counter(r[fld] for r in records)
        lines.append(f"{fld}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    c = Counter(r["brand"]["tier"] for r in records)
    lines.append("brand.tier: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    c = Counter(r["category_code"][:2] for r in records)
    lines.append("대분류: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    prices = [r["list_price_krw"] for r in records]
    lines.append(f"list_price_krw: min={min(prices):,} / median={sorted(prices)[len(prices)//2]:,} / max={max(prices):,}")
    n_exp = sum(1 for r in records if r.get("expiry_date"))
    lines.append(f"expiry_date 있음: {n_exp} / 카테고리 expiry_class≠none: "
                 f"{sum(1 for r in records if leaves[r['category_code']].expiry_class != 'none')}")
    nullable = ("manufacture_date", "expiry_date", "source_text", "spec.weight_g", "spec.dimensions_cm", "spec.net_content", "spec.variant", "brand.name")
    def _get(r, k):
        cur = r
        for part in k.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        return cur
    filled = {k: sum(1 for r in records if _get(r, k) is not None) for k in nullable}
    lines.append("null 허용 필드 채움률: " + ", ".join(f"{k}={v}/{len(records)}" for k, v in filled.items()))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=Path, default=EXAMPLES_PATH)
    ap.add_argument("--today", type=lambda s: date.fromisoformat(s), default=None,
                    help="기준일(YYYY-MM-DD). 기본 오늘")
    args = ap.parse_args(argv)

    schema = load_schema()
    n_props = len(schema["properties"])
    print(f"스키마 점검 통과: {SCHEMA_PATH.name} (draft 2020-12, 최상위 필드 {n_props}개, 필수 {len(schema['required'])}개)")

    leaves = {l.code: l for l in load_categories()}
    records = read_jsonl(args.records)
    today = args.today or date.today()
    warnings: dict[int, list[str]] = {}
    errors = validate_records(records, schema, leaves, today, warnings)
    print(f"레코드 검증: {args.records.relative_to(REPO_ROOT) if args.records.is_relative_to(REPO_ROOT) else args.records}"
          f" — {len(records)}건 중 오류 {len(errors)}건, 경고 {len(warnings)}건 (기준일 {today})")
    for i, errs in errors.items():
        for e in errs:
            print(f"  #{i} {e}")
    for i, ws in warnings.items():
        for w in ws:
            print(f"  #{i} {w}")
    if not errors:
        print(summary(records, leaves))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
