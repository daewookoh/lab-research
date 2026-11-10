"""시세 관측 스키마 로더·검증기 (D005).

사용법:
    python -m src.schema.market_price                                       # 스키마 점검 + 예시 레코드 검증 + 요약
    python -m src.schema.market_price --records prices.jsonl --products p.jsonl --today 2026-11-10

검증 3단계 (D003·D004 와 같은 구조):
  1) JSON Schema(data/schema/market_price.json, draft 2020-12) — 타입·enum·범위·필수·format(date)
  2) 교차 규칙(이 모듈) — product_id 존재(상품 파일을 줬을 때), observed_at ≤ 기준일,
     n_listings=0 ⇔ 가격 3개 모두 null, min ≤ median ≤ max, n_listings=1 이면 세 값 동일,
     match_type=exact 면 unit_count = product.spec.unit_count, price_id 유일,
     같은 (product_id, source.channel, source.platform, condition, observed_at) 중복 금지.
  3) 경고(검증 실패 아님) — 환산 중앙값이 정상가의 1.5배 초과 또는 0.1배 미만(정상가·매칭 의심),
     condition=new 인데 환산 중앙값 > 정상가, 표본 3개 미만, used 시세를 신품 전용 채널(own_online·live_commerce)에서 관측.

정규화(요약에만 출력, 저장 안 함): 환산 중앙값 = price_median_krw × product.unit_count ÷ unit_count,
정상가 대비 비율 = 환산 중앙값 ÷ product.list_price_krw.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from statistics import median

from jsonschema import Draft202012Validator, FormatChecker

from src.schema.product import EXAMPLES_PATH as PRODUCT_EXAMPLES_PATH, read_jsonl

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "data" / "schema" / "market_price.json"
EXAMPLES_PATH = REPO_ROOT / "data" / "schema" / "examples" / "market_price_examples.jsonl"

RATIO_HIGH = 1.5   # 가정: 환산 시세가 정상가의 1.5배를 넘으면 정상가가 잘못 잡혔거나 매칭 오류
RATIO_LOW = 0.1    # 가정: 0.1배 미만이면 단위(낱개/묶음) 착오 가능성
MIN_LISTINGS = 3   # 가정: 표본 3개 미만은 대표 시세로 쓰기 어려움
NEW_ONLY_CHANNELS = {"own_online", "live_commerce"}  # 가정: 자사몰·라이브는 중고 시세가 나올 수 없음


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


def normalized_median(record: dict, product: dict) -> float | None:
    """관측 중앙값을 우리 상품 1단위(product.spec.unit_count) 기준으로 환산. 시세 없으면 None."""
    if record["price_median_krw"] is None:
        return None
    return record["price_median_krw"] * product["spec"]["unit_count"] / record["unit_count"]


def price_ratio(record: dict, product: dict) -> float | None:
    """환산 중앙값 ÷ 정상가. 가격경쟁력 항목의 기초 피처(저장하지 않음)."""
    nm = normalized_median(record, product)
    return None if nm is None else round(nm / product["list_price_krw"], 4)


def dedup_key(record: dict) -> tuple:
    return (record["product_id"], record["source"]["channel"], record["source"].get("platform"), record["condition"], record["observed_at"])


def cross_checks(
    record: dict,
    products: dict[str, dict] | None,
    today: date,
) -> tuple[list[str], list[str]]:
    """(오류, 경고). products 가 None 이면 상품 참조 규칙은 건너뛴다."""
    errs: list[str] = []
    warns: list[str] = []
    mid = record.get("price_id", "?")
    pmin, pmed, pmax, n = record["price_min_krw"], record["price_median_krw"], record["price_max_krw"], record["n_listings"]

    if _d(record["observed_at"]) > today:
        errs.append(f"[cross] {mid}: observed_at {record['observed_at']} 가 기준일 {today} 이후")

    prices = (pmin, pmed, pmax)
    if n == 0:
        if any(p is not None for p in prices):
            errs.append(f"[cross] {mid}: n_listings=0 인데 가격이 있음 (min/median/max 모두 null 이어야 함)")
    else:
        if any(p is None for p in prices):
            errs.append(f"[cross] {mid}: n_listings={n} 인데 가격이 null (세 값 모두 필요)")
        else:
            if not (pmin <= pmed <= pmax):
                errs.append(f"[cross] {mid}: min {pmin:,} ≤ median {pmed:,} ≤ max {pmax:,} 순서 위반")
            if n == 1 and not (pmin == pmed == pmax):
                errs.append(f"[cross] {mid}: n_listings=1 이면 min=median=max 여야 함")
            if n < MIN_LISTINGS:
                warns.append(f"[warn] {mid}: 표본 n_listings={n} < {MIN_LISTINGS} → 대표 시세로 부적합")

    if record["condition"] == "used" and record["source"]["channel"] in NEW_ONLY_CHANNELS:
        warns.append(f"[warn] {mid}: condition=used 를 {record['source']['channel']} 채널에서 관측 — 채널·상태 조합 확인")

    if products is not None:
        product = products.get(record["product_id"])
        if product is None:
            errs.append(f"[cross] {mid}: product_id {record['product_id']} 가 상품 레코드에 없음")
            return errs, warns
        if record["match_type"] == "exact" and record["unit_count"] != product["spec"]["unit_count"]:
            errs.append(f"[cross] {mid}: match_type=exact 인데 unit_count {record['unit_count']} ≠ 상품 spec.unit_count {product['spec']['unit_count']}")
        ratio = price_ratio(record, product)
        if ratio is not None:
            if ratio > RATIO_HIGH:
                warns.append(f"[warn] {mid}: 환산 중앙값이 정상가의 {ratio:.2f}배 (> {RATIO_HIGH}) → 정상가·매칭 확인")
            elif ratio < RATIO_LOW:
                warns.append(f"[warn] {mid}: 환산 중앙값이 정상가의 {ratio:.2f}배 (< {RATIO_LOW}) → 단위 착오 의심")
            elif record["condition"] == "new" and ratio > 1.0:
                warns.append(f"[warn] {mid}: 신품 시세가 정상가 초과 ({ratio:.2f}배) → 정상가가 낮게 잡혔을 수 있음")
    return errs, warns


def validate_records(
    records: list[dict],
    schema: dict | None = None,
    products: dict[str, dict] | None = None,
    today: date | None = None,
    warnings: dict[int, list[str]] | None = None,
) -> dict[int, list[str]]:
    """{레코드 인덱스: 오류 목록}. 비어 있으면 전부 통과. price_id·관측키 중복은 뒤에 나온 레코드의 오류로 붙인다."""
    schema = schema or load_schema()
    today = today or date.today()
    v = _validator(schema)
    out: dict[int, list[str]] = {}
    seen_ids: dict[str, int] = {}
    seen_keys: dict[tuple, int] = {}
    for i, rec in enumerate(records):
        errs = schema_errors(rec, v)
        if not errs:
            errs, warns = cross_checks(rec, products, today)
            if warns and warnings is not None:
                warnings[i] = warns
            if rec["price_id"] in seen_ids:
                errs.append(f"[cross] {rec['price_id']}: price_id 가 #{seen_ids[rec['price_id']]} 과 중복")
            seen_ids.setdefault(rec["price_id"], i)
            k = dedup_key(rec)
            if k in seen_keys:
                errs.append(f"[cross] {rec['price_id']}: 같은 상품·채널·플랫폼·상태·관측일 레코드가 #{seen_keys[k]} 에 이미 있음")
            seen_keys.setdefault(k, i)
        if errs:
            out[i] = errs
    return out


def load_products(path: Path = PRODUCT_EXAMPLES_PATH) -> dict[str, dict]:
    return {r["product_id"]: r for r in read_jsonl(path)}


def _stats(xs: list[float]) -> str:
    if not xs:
        return "n=0"
    return f"n={len(xs)} min={min(xs):g} median={median(xs):g} max={max(xs):g}"


def summary(records: list[dict], products: dict[str, dict] | None) -> str:
    lines = [f"레코드 {len(records)}건"]
    for fld, get in (
        ("source.channel", lambda r: r["source"]["channel"]),
        ("source.collected_by", lambda r: r["source"]["collected_by"]),
        ("match_type", lambda r: r["match_type"]),
        ("condition", lambda r: r["condition"]),
    ):
        c = Counter(get(r) for r in records)
        lines.append(f"{fld}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    lines.append("n_listings: " + _stats([r["n_listings"] for r in records]) + f" / 시세 없음(n=0) {sum(1 for r in records if r['n_listings'] == 0)}건")
    lines.append(f"참조 상품 수: {len({r['product_id'] for r in records})} / 관측일: {min(r['observed_at'] for r in records)} ~ {max(r['observed_at'] for r in records)}")
    if products is not None:
        ratios = [price_ratio(r, products[r["product_id"]]) for r in records if r["product_id"] in products]
        ratios = [x for x in ratios if x is not None]
        lines.append("정상가 대비 환산 중앙값 비율(전체): " + _stats(ratios))
        for cond in ("new", "used"):
            rs = [price_ratio(r, products[r["product_id"]]) for r in records if r["condition"] == cond and r["product_id"] in products]
            lines.append(f"정상가 대비 비율({cond}): " + _stats([x for x in rs if x is not None]))
        # 상품별 채널 간 시세 격차(같은 상품·같은 상태의 환산 중앙값 max/min)
        by_prod: dict[tuple, list[float]] = {}
        for r in records:
            nm = normalized_median(r, products[r["product_id"]]) if r["product_id"] in products else None
            if nm is not None:
                by_prod.setdefault((r["product_id"], r["condition"]), []).append(nm)
        spreads = [round(max(v) / min(v), 3) for v in by_prod.values() if len(v) >= 2]
        lines.append("같은 상품·상태의 채널 간 시세 격차(max/min): " + _stats(spreads))
    n_sold = sum(1 for r in records if r.get("sold_count_30d") is not None)
    n_idx = sum(1 for r in records if r.get("search_index") is not None)
    lines.append(f"null 허용 필드 채움률: sold_count_30d={n_sold}/{len(records)}, search_index={n_idx}/{len(records)}, "
                 f"source.platform={sum(1 for r in records if r['source'].get('platform'))}/{len(records)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=Path, default=EXAMPLES_PATH)
    ap.add_argument("--products", type=Path, default=PRODUCT_EXAMPLES_PATH,
                    help="상품 레코드 JSONL. 'none' 이면 상품 참조 규칙 생략")
    ap.add_argument("--today", type=lambda s: date.fromisoformat(s), default=None,
                    help="기준일(YYYY-MM-DD). 기본 오늘")
    args = ap.parse_args(argv)

    schema = load_schema()
    print(f"스키마 점검 통과: {SCHEMA_PATH.name} (draft 2020-12, 최상위 필드 {len(schema['properties'])}개, 필수 {len(schema['required'])}개)")

    products = None if str(args.products) == "none" else load_products(args.products)
    records = read_jsonl(args.records)
    today = args.today or date.today()
    warnings: dict[int, list[str]] = {}
    errors = validate_records(records, schema, products, today, warnings)
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
        print(summary(records, products))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
