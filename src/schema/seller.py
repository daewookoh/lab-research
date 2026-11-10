"""판매자 이력 스키마 로더·검증기 (D005).

사용법:
    python -m src.schema.seller                                        # 스키마 점검 + 예시 레코드 검증 + 요약
    python -m src.schema.seller --records sellers.jsonl --products p.jsonl --today 2026-11-10

검증 3단계 (D003·D004 와 같은 구조):
  1) JSON Schema(data/schema/seller.json, draft 2020-12) — 타입·enum·범위·필수·format(date)
  2) 교차 규칙(이 모듈) — main_category_codes 가 categories.yaml 중분류에 존재, registered_date ≤ 기준일,
     배치: batch_id 유일, supplied_date 가 registered_date 이후·기준일 이하·시간순 정렬,
     product_ids 길이 = product_count 및 상품 존재(상품 파일을 줬을 때), 매입 배치의 settled ≤ declared,
     status 와 이력의 일관성(blocked 면 major_gap·dispute 중 하나 이상, churned/dormant 는 이력 필요).
  3) 경고(검증 실패 아님) — 등록 후 공급 없음, 최근 1년 공급 없는 active, major_gap 비율 50% 이상인 active.

집계(요약에만 출력, 저장 안 함): 판매자별 배치 수·신고가 합·기재 정확도 분포·이슈율.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from src.schema.categories import Leaf, load as load_categories
from src.schema.product import EXAMPLES_PATH as PRODUCT_EXAMPLES_PATH, read_jsonl

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "data" / "schema" / "seller.json"
EXAMPLES_PATH = REPO_ROOT / "data" / "schema" / "examples" / "seller_examples.jsonl"

DORMANT_DAYS = 365          # status 정의의 "최근 1년"
MAJOR_GAP_WARN_RATIO = 0.5  # 가정: 절반 이상 크게 어긋나면 신뢰도 점검 대상


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


def level2_codes(leaves: dict[str, Leaf]) -> set[str]:
    return {leaf.l2_code for leaf in leaves.values()}


def history_stats(record: dict) -> dict:
    """판매자 1명의 공급 이력 집계 (저장하지 않는 파생값)."""
    hist = record["supply_history"]
    n = len(hist)
    acc = Counter(b["description_accuracy"] for b in hist)
    issues = sum(1 for b in hist if b["issue"] != "none")
    return {
        "n_batches": n,
        "declared_total_krw": sum(b["declared_value_krw"] for b in hist),
        "n_products": sum(b["product_count"] for b in hist),
        "accuracy": dict(acc),
        "major_gap_ratio": (acc["major_gap"] / n) if n else None,
        "issue_ratio": (issues / n) if n else None,
        "last_supplied": max((b["supplied_date"] for b in hist), default=None),
    }


def cross_checks(
    record: dict,
    products: dict[str, dict] | None,
    leaves: dict[str, Leaf],
    today: date,
) -> tuple[list[str], list[str]]:
    """(오류, 경고). products 가 None 이면 product_ids 존재 규칙은 건너뛴다."""
    errs: list[str] = []
    warns: list[str] = []
    sid = record.get("seller_id", "?")
    biz = record["business"]
    registered = _d(record["registered_date"])
    status = record.get("status", "active")

    l2 = level2_codes(leaves)
    for code in biz["main_category_codes"]:
        if code not in l2:
            errs.append(f"[cross] {sid}: main_category_codes 의 {code} 가 categories.yaml 중분류에 없음")

    if registered > today:
        errs.append(f"[cross] {sid}: registered_date {registered} 가 기준일 {today} 이후")

    hist = record["supply_history"]
    seen: set[str] = set()
    prev: date | None = None
    for b in hist:
        bid = b["batch_id"]
        if bid in seen:
            errs.append(f"[cross] {sid}: batch_id {bid} 중복")
        seen.add(bid)
        supplied = _d(b["supplied_date"])
        if supplied < registered:
            errs.append(f"[cross] {sid}/{bid}: supplied_date {supplied} 가 registered_date {registered} 이전")
        if supplied > today:
            errs.append(f"[cross] {sid}/{bid}: supplied_date {supplied} 가 기준일 {today} 이후")
        if prev is not None and supplied < prev:
            errs.append(f"[cross] {sid}/{bid}: supply_history 가 시간순이 아님 ({supplied} < {prev})")
        prev = supplied
        pids = b.get("product_ids")
        if pids is not None:
            if len(pids) != b["product_count"]:
                errs.append(f"[cross] {sid}/{bid}: product_ids 길이 {len(pids)} ≠ product_count {b['product_count']}")
            if products is not None:
                for pid in pids:
                    if pid not in products:
                        errs.append(f"[cross] {sid}/{bid}: product_id {pid} 가 상품 레코드에 없음")
                    elif _d(products[pid]["acquired_date"]) > supplied:
                        errs.append(f"[cross] {sid}/{bid}: 상품 {pid} 의 acquired_date {products[pid]['acquired_date']} 가 supplied_date {supplied} 이후")
        settled = b.get("settled_value_krw")
        if b["acquisition_type"] == "purchase" and settled is not None and b["declared_value_krw"] > 0 and settled > b["declared_value_krw"]:
            errs.append(f"[cross] {sid}/{bid}: 매입 배치의 settled_value_krw {settled:,} 가 신고 정상가 합 {b['declared_value_krw']:,} 초과")
        if b["description_accuracy"] == "accurate" and b["issue"] in ("quantity_short", "condition_mismatch", "expired_included"):
            errs.append(f"[cross] {sid}/{bid}: description_accuracy=accurate 인데 issue={b['issue']} (기재 불일치 이슈) → 모순")

    # status 일관성
    stats = history_stats(record)
    if status == "blocked" and not any(b["description_accuracy"] == "major_gap" or b["issue"] == "dispute" for b in hist):
        errs.append(f"[cross] {sid}: status=blocked 인데 major_gap·dispute 배치가 하나도 없음")
    if status in ("dormant", "churned") and not hist:
        errs.append(f"[cross] {sid}: status={status} 는 공급 이력이 있어야 함")
    if status == "dormant" and stats["last_supplied"] and (today - _d(stats["last_supplied"])).days < DORMANT_DAYS:
        errs.append(f"[cross] {sid}: status=dormant 인데 마지막 공급 {stats['last_supplied']} 이 {DORMANT_DAYS}일 이내")

    # 경고
    if status == "active":
        if not hist:
            warns.append(f"[warn] {sid}: 등록({registered}) 후 공급 이력 없음")
        elif (today - _d(stats["last_supplied"])).days >= DORMANT_DAYS:
            warns.append(f"[warn] {sid}: status=active 인데 마지막 공급 {stats['last_supplied']} 이 {DORMANT_DAYS}일 이상 경과 → dormant 검토")
        if stats["major_gap_ratio"] is not None and stats["major_gap_ratio"] >= MAJOR_GAP_WARN_RATIO:
            warns.append(f"[warn] {sid}: major_gap 비율 {stats['major_gap_ratio']:.0%} ({stats['accuracy'].get('major_gap', 0)}/{stats['n_batches']}) → 신뢰도 점검")
    return errs, warns


def validate_records(
    records: list[dict],
    schema: dict | None = None,
    products: dict[str, dict] | None = None,
    leaves: dict[str, Leaf] | None = None,
    today: date | None = None,
    warnings: dict[int, list[str]] | None = None,
) -> dict[int, list[str]]:
    """{레코드 인덱스: 오류 목록}. 비어 있으면 전부 통과. seller_id 중복은 파일 수준 오류로 마지막 인덱스에 붙인다."""
    schema = schema or load_schema()
    leaves = leaves or {l.code: l for l in load_categories()}
    today = today or date.today()
    v = _validator(schema)
    out: dict[int, list[str]] = {}
    seen_ids: dict[str, int] = {}
    for i, rec in enumerate(records):
        errs = schema_errors(rec, v)
        if not errs:
            errs, warns = cross_checks(rec, products, leaves, today)
            if warns and warnings is not None:
                warnings[i] = warns
            sid = rec["seller_id"]
            if sid in seen_ids:
                errs.append(f"[cross] {sid}: seller_id 가 #{seen_ids[sid]} 과 중복")
            seen_ids.setdefault(sid, i)
        if errs:
            out[i] = errs
    return out


def load_sellers(path: Path = EXAMPLES_PATH) -> dict[str, dict]:
    return {r["seller_id"]: r for r in read_jsonl(path)}


def load_products(path: Path = PRODUCT_EXAMPLES_PATH) -> dict[str, dict]:
    return {r["product_id"]: r for r in read_jsonl(path)}


def summary(records: list[dict]) -> str:
    lines = [f"레코드 {len(records)}건"]
    for fld, get in (
        ("business.type", lambda r: r["business"]["type"]),
        ("business.region", lambda r: r["business"]["region"]),
        ("status", lambda r: r.get("status", "active")),
        ("acquisition_source", lambda r: r.get("acquisition_source", "unknown")),
    ):
        c = Counter(get(r) for r in records)
        lines.append(f"{fld}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    stats = [history_stats(r) for r in records]
    nb = [s["n_batches"] for s in stats]
    lines.append(f"배치 수: 합 {sum(nb)} / 판매자당 min={min(nb)} median={sorted(nb)[len(nb)//2]} max={max(nb)} / 이력 없음 {sum(1 for x in nb if x == 0)}명")
    acc = Counter()
    issues = Counter()
    for r in records:
        for b in r["supply_history"]:
            acc[b["description_accuracy"]] += 1
            issues[b["issue"]] += 1
    lines.append("description_accuracy(배치): " + ", ".join(f"{k}={v}" for k, v in sorted(acc.items())))
    lines.append("issue(배치): " + ", ".join(f"{k}={v}" for k, v in sorted(issues.items())))
    yrs = [r["business"]["years_in_business"] for r in records]
    lines.append(f"years_in_business: min={min(yrs):g} median={sorted(yrs)[len(yrs)//2]:g} max={max(yrs):g}")
    declared = [s["declared_total_krw"] for s in stats if s["n_batches"]]
    if declared:
        lines.append(f"판매자별 신고가 합(원): min={min(declared):,} median={sorted(declared)[len(declared)//2]:,} max={max(declared):,}")
    l2 = Counter(c for r in records for c in r["business"]["main_category_codes"])
    lines.append("main_category_codes: " + ", ".join(f"{k}={v}" for k, v in sorted(l2.items())))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=Path, default=EXAMPLES_PATH)
    ap.add_argument("--products", type=Path, default=PRODUCT_EXAMPLES_PATH,
                    help="상품 레코드 JSONL. 'none' 이면 product_ids 존재 규칙 생략")
    ap.add_argument("--today", type=lambda s: date.fromisoformat(s), default=None,
                    help="기준일(YYYY-MM-DD). 기본 오늘")
    args = ap.parse_args(argv)

    schema = load_schema()
    print(f"스키마 점검 통과: {SCHEMA_PATH.name} (draft 2020-12, 최상위 필드 {len(schema['properties'])}개, 필수 {len(schema['required'])}개, "
          f"supply_history 항목 필드 {len(schema['properties']['supply_history']['items']['properties'])}개)")

    leaves = {l.code: l for l in load_categories()}
    products = None if str(args.products) == "none" else load_products(args.products)
    records = read_jsonl(args.records)
    today = args.today or date.today()
    warnings: dict[int, list[str]] = {}
    errors = validate_records(records, schema, products, leaves, today, warnings)
    rel = args.records.relative_to(REPO_ROOT) if args.records.is_relative_to(REPO_ROOT) else args.records
    print(f"레코드 검증: {rel} — {len(records)}건 중 오류 {len(errors)}건, 경고 {len(warnings)}건 "
          f"(기준일 {today}, 상품 참조 {'있음 ' + str(len(products)) + '건' if products is not None else '없음'})")
    for i, errs in errors.items():
        for e in errs:
            print(f"  #{i} {e}")
    for i, ws in warnings.items():
        for w in ws:
            print(f"  #{i} {w}")
    if not errors:
        print(summary(records))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
