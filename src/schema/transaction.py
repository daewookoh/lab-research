"""거래 결과 스키마 로더·검증기 (D004).

사용법:
    python -m src.schema.transaction                                  # 스키마 점검 + 예시 레코드 검증 + 요약
    python -m src.schema.transaction --records tx.jsonl --products p.jsonl --today 2026-11-02

검증 3단계 (D003 product.py 와 같은 구조):
  1) JSON Schema(data/schema/transaction.json, draft 2020-12) — 타입·enum·범위·필수·format(date)
  2) 교차 규칙(이 모듈) — product_id 존재, 매입/위탁과 매입가, 수량 순서, 날짜 순서
     (product.acquired_date ≤ buy_date ≤ listed_date ≤ sold_date ≤ observed_at ≤ 기준일),
     status 별 필드 일관성(sold_date·quantity_sold·censored), 판매가 ≤ 등록가, 매입가 ≤ 정상가,
     유통기한 경과 후 판매 금지, duration_days·recovery_rate 재계산 일치.
  3) 경고(검증 실패 아님) — 등록가가 정상가 초과, 수수료+배송비가 매출 초과(적자), 등록 수량이 보유 수량 초과,
     regulation=permit 품목을 중고앱·SNS 채널에 등록.
  (D005 추가) 판매자 파일을 주면 seller_id 존재, buy_date ≥ seller.registered_date,
     같은 날·같은 상품을 담은 배치가 있으면 acquisition_type 일치(오류) / 같은 날 배치가 없으면 경고.
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

from src.schema.categories import Leaf, load as load_categories
from src.schema.product import EXAMPLES_PATH as PRODUCT_EXAMPLES_PATH, read_jsonl

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "data" / "schema" / "transaction.json"
EXAMPLES_PATH = REPO_ROOT / "data" / "schema" / "examples" / "transaction_examples.jsonl"
SELLER_EXAMPLES_PATH = REPO_ROOT / "data" / "schema" / "examples" / "seller_examples.jsonl"

RECOVERY_DECIMALS = 4
RECOVERY_TOL = 0.5 * 10 ** -RECOVERY_DECIMALS  # 반올림 자릿수 반 단위
CENSORED_STATUSES = {"listed", "partially_sold", "withdrawn"}
RESTRICTED_CHANNELS_FOR_PERMIT = {"secondhand_app", "social_market"}  # 가정: 허가 품목은 개인 간 채널 부적합


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


def expected_duration(record: dict) -> int:
    """판매기간 정의. sold 면 sold_date − listed_date, 그 외는 observed_at − listed_date."""
    listed = _d(record["listing"]["listed_date"])
    out = record["outcome"]
    end = _d(out["sold_date"]) if out["status"] == "sold" and out["sold_date"] else _d(out["observed_at"])
    return (end - listed).days


def expected_recovery(record: dict) -> float | None:
    """회수율 정의. (판매단가×판매수량 − 수수료 − 배송비) ÷ (매입단가×등록수량). 위탁이면 None."""
    p, l, o = record["purchase"], record["listing"], record["outcome"]
    cost = p["buy_price_krw"] * l["quantity"]
    if cost == 0:
        return None
    revenue = (o["sold_price_krw"] or 0) * o["quantity_sold"]
    return round((revenue - o["channel_fee_krw"] - o["shipping_cost_krw"]) / cost, RECOVERY_DECIMALS)


def cross_checks(
    record: dict,
    products: dict[str, dict] | None,
    leaves: dict[str, Leaf],
    today: date,
    sellers: dict[str, dict] | None = None,
) -> tuple[list[str], list[str]]:
    """(오류, 경고). products 가 None 이면 상품 참조 규칙을, sellers 가 None 이면 판매자 참조 규칙을 건너뛴다."""
    errs: list[str] = []
    warns: list[str] = []
    tid = record.get("transaction_id", "?")
    p, l, o = record["purchase"], record["listing"], record["outcome"]
    status = o["status"]

    product = None
    if products is not None:
        product = products.get(record["product_id"])
        if product is None:
            errs.append(f"[cross] {tid}: product_id {record['product_id']} 가 상품 레코드에 없음")
            return errs, warns

    # 판매자 참조 규칙 (D005)
    if sellers is not None and record.get("seller_id") is not None:
        seller = sellers.get(record["seller_id"])
        if seller is None:
            errs.append(f"[cross] {tid}: seller_id {record['seller_id']} 가 판매자 레코드에 없음")
        else:
            if _d(p["buy_date"]) < _d(seller["registered_date"]):
                errs.append(f"[cross] {tid}: buy_date {p['buy_date']} 가 판매자 registered_date {seller['registered_date']} 이전")
            same_day = [b for b in seller["supply_history"] if b["supplied_date"] == p["buy_date"]]
            if not same_day:
                warns.append(f"[warn] {tid}: 판매자 {record['seller_id']} 의 공급 이력에 buy_date {p['buy_date']} 배치가 없음")
            for b in same_day:
                if record["product_id"] in (b.get("product_ids") or []) and b["acquisition_type"] != p["acquisition_type"]:
                    errs.append(f"[cross] {tid}: 배치 {b['batch_id']} 는 {b['acquisition_type']} 인데 거래는 {p['acquisition_type']}")

    # 매입/위탁과 매입가
    if p["acquisition_type"] == "consignment" and p["buy_price_krw"] != 0:
        errs.append(f"[cross] {tid}: 위탁(consignment)인데 buy_price_krw={p['buy_price_krw']} (0 이어야 함)")
    if p["acquisition_type"] == "purchase" and p["buy_price_krw"] < 1:
        errs.append(f"[cross] {tid}: 매입(purchase)인데 buy_price_krw=0")

    # 수량
    if l["quantity"] > p["quantity"]:
        errs.append(f"[cross] {tid}: listing.quantity {l['quantity']} 가 purchase.quantity {p['quantity']} 초과")
    if o["quantity_sold"] > l["quantity"]:
        errs.append(f"[cross] {tid}: quantity_sold {o['quantity_sold']} 가 listing.quantity {l['quantity']} 초과")

    # 날짜 순서
    buy, listed, observed, sold = _d(p["buy_date"]), _d(l["listed_date"]), _d(o["observed_at"]), _d(o["sold_date"])
    if listed < buy:
        errs.append(f"[cross] {tid}: listed_date {listed} 가 buy_date {buy} 이전")
    if observed < listed:
        errs.append(f"[cross] {tid}: observed_at {observed} 가 listed_date {listed} 이전")
    if observed > today:
        errs.append(f"[cross] {tid}: observed_at {observed} 가 기준일 {today} 이후")
    if sold is not None:
        if sold < listed:
            errs.append(f"[cross] {tid}: sold_date {sold} 가 listed_date {listed} 이전")
        if sold > observed:
            errs.append(f"[cross] {tid}: sold_date {sold} 가 observed_at {observed} 이후")

    # status 별 일관성
    if status == "sold":
        if sold is None:
            errs.append(f"[cross] {tid}: status=sold 인데 sold_date 가 null")
        if o["quantity_sold"] != l["quantity"]:
            errs.append(f"[cross] {tid}: status=sold 인데 quantity_sold {o['quantity_sold']} ≠ listing.quantity {l['quantity']}")
    else:
        if sold is not None:
            errs.append(f"[cross] {tid}: status={status} 인데 sold_date 가 있음 (sold 만 허용)")
        if status == "listed" and o["quantity_sold"] != 0:
            errs.append(f"[cross] {tid}: status=listed 인데 quantity_sold={o['quantity_sold']} (partially_sold 여야 함)")
        if status == "partially_sold" and not (0 < o["quantity_sold"] < l["quantity"]):
            errs.append(f"[cross] {tid}: status=partially_sold 는 0 < quantity_sold < listing.quantity 여야 함 (현재 {o['quantity_sold']}/{l['quantity']})")
        if status in ("withdrawn", "disposed") and o["quantity_sold"] >= l["quantity"]:
            errs.append(f"[cross] {tid}: status={status} 인데 전량 판매됨 → sold 여야 함")
    expected_censored = status in CENSORED_STATUSES
    if record["censored"] != expected_censored:
        errs.append(f"[cross] {tid}: status={status} 면 censored={expected_censored} 여야 함")

    # 판매가
    if (o["quantity_sold"] > 0) != (o["sold_price_krw"] is not None):
        errs.append(f"[cross] {tid}: quantity_sold={o['quantity_sold']} 와 sold_price_krw={o['sold_price_krw']} 불일치 (판매가 있으면 단가 필수, 없으면 null)")
    if o["sold_price_krw"] is not None and o["sold_price_krw"] > l["listed_price_krw"]:
        errs.append(f"[cross] {tid}: sold_price_krw {o['sold_price_krw']} 가 listed_price_krw {l['listed_price_krw']} 초과")

    # 목표변수 재계산
    dur = expected_duration(record)
    if record["duration_days"] != dur:
        errs.append(f"[cross] {tid}: duration_days={record['duration_days']} 이나 정의대로 계산하면 {dur}")
    rec_exp = expected_recovery(record)
    rec_val = record["recovery_rate"]
    if rec_exp is None and rec_val is not None:
        errs.append(f"[cross] {tid}: 위탁(매입가 0)이면 recovery_rate 는 null 이어야 함")
    elif rec_exp is not None and (rec_val is None or abs(rec_val - rec_exp) > RECOVERY_TOL):
        errs.append(f"[cross] {tid}: recovery_rate={rec_val} 이나 정의대로 계산하면 {rec_exp}")

    # 상품 참조 규칙
    if product is not None:
        leaf = leaves.get(product["category_code"])
        if buy < _d(product["acquired_date"]):
            errs.append(f"[cross] {tid}: buy_date {buy} 가 상품 acquired_date {product['acquired_date']} 이전")
        if p["buy_price_krw"] > product["list_price_krw"]:
            errs.append(f"[cross] {tid}: buy_price_krw {p['buy_price_krw']} 가 상품 정상가 {product['list_price_krw']} 초과")
        expiry = _d(product.get("expiry_date"))
        if expiry is not None:
            end = sold if sold is not None else observed
            if o["quantity_sold"] > 0 and end > expiry:
                errs.append(f"[cross] {tid}: 유통기한 {expiry} 경과 후({end}) 판매 기록")
        if l["listed_price_krw"] > product["list_price_krw"]:
            warns.append(f"[warn] {tid}: listed_price_krw {l['listed_price_krw']} 가 상품 정상가 {product['list_price_krw']} 초과")
        if l["quantity"] > product["quantity_on_hand"]:
            warns.append(f"[warn] {tid}: listing.quantity {l['quantity']} 가 상품 quantity_on_hand {product['quantity_on_hand']} 초과 (보유 수량은 스냅샷이라 경고만)")
        if leaf is not None and leaf.regulation == "permit" and record["channel"] in RESTRICTED_CHANNELS_FOR_PERMIT:
            warns.append(f"[warn] {tid}: 허가 품목({leaf.code}, regulation=permit)을 {record['channel']} 채널에 등록 — 채널 적합성 확인 필요")

    # 적자 거래
    if o["quantity_sold"] > 0:
        gross = (o["sold_price_krw"] or 0) * o["quantity_sold"]
        if o["channel_fee_krw"] + o["shipping_cost_krw"] > gross:
            warns.append(f"[warn] {tid}: 수수료+배송비 {o['channel_fee_krw'] + o['shipping_cost_krw']:,} 가 매출 {gross:,} 초과 (적자)")
    return errs, warns


def validate_records(
    records: list[dict],
    schema: dict | None = None,
    products: dict[str, dict] | None = None,
    leaves: dict[str, Leaf] | None = None,
    today: date | None = None,
    warnings: dict[int, list[str]] | None = None,
    sellers: dict[str, dict] | None = None,
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
            errs, warns = cross_checks(rec, products, leaves, today, sellers)
            if warns and warnings is not None:
                warnings[i] = warns
        if errs:
            out[i] = errs
    return out


def load_products(path: Path = PRODUCT_EXAMPLES_PATH) -> dict[str, dict]:
    return {r["product_id"]: r for r in read_jsonl(path)}


def load_sellers(path: Path = SELLER_EXAMPLES_PATH) -> dict[str, dict]:
    return {r["seller_id"]: r for r in read_jsonl(path)}


def _stats(xs: list[float]) -> str:
    if not xs:
        return "n=0"
    return f"n={len(xs)} min={min(xs):g} median={median(xs):g} max={max(xs):g}"


def summary(records: list[dict]) -> str:
    lines = [f"레코드 {len(records)}건"]
    for fld, get in (
        ("status", lambda r: r["outcome"]["status"]),
        ("channel", lambda r: r["channel"]),
        ("acquisition_type", lambda r: r["purchase"]["acquisition_type"]),
        ("buy_price_basis", lambda r: r["purchase"].get("buy_price_basis", "unknown")),
    ):
        c = Counter(get(r) for r in records)
        lines.append(f"{fld}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    n_c = sum(1 for r in records if r["censored"])
    lines.append(f"censored: {n_c}/{len(records)}")
    sold = [r for r in records if r["outcome"]["status"] == "sold"]
    lines.append("duration_days(sold): " + _stats([r["duration_days"] for r in sold]))
    lines.append("duration_days(censored): " + _stats([r["duration_days"] for r in records if r["censored"]]))
    lines.append("recovery_rate(sold): " + _stats([r["recovery_rate"] for r in sold if r["recovery_rate"] is not None]))
    lines.append("recovery_rate(전체, null 제외): " + _stats([r["recovery_rate"] for r in records if r["recovery_rate"] is not None]))
    disc = [r["purchase"]["buy_price_krw"] / r["listing"]["listed_price_krw"] for r in records if r["purchase"]["buy_price_krw"] > 0]
    lines.append("매입가/등록가 비율: " + _stats([round(x, 3) for x in disc]))
    n_distinct = len({r["product_id"] for r in records})
    lines.append(f"참조 상품 수: {n_distinct} (복수 채널 등록 상품: {sum(1 for _, v in Counter(r['product_id'] for r in records).items() if v > 1)})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=Path, default=EXAMPLES_PATH)
    ap.add_argument("--products", type=Path, default=PRODUCT_EXAMPLES_PATH,
                    help="상품 레코드 JSONL. 'none' 이면 상품 참조 규칙 생략")
    ap.add_argument("--sellers", type=Path, default=SELLER_EXAMPLES_PATH,
                    help="판매자 레코드 JSONL (D005). 'none' 이면 판매자 참조 규칙 생략")
    ap.add_argument("--today", type=lambda s: date.fromisoformat(s), default=None,
                    help="기준일(YYYY-MM-DD). 기본 오늘")
    args = ap.parse_args(argv)

    schema = load_schema()
    n_props = len(schema["properties"])
    print(f"스키마 점검 통과: {SCHEMA_PATH.name} (draft 2020-12, 최상위 필드 {n_props}개, 필수 {len(schema['required'])}개)")

    leaves = {l.code: l for l in load_categories()}
    products = None if str(args.products) == "none" else load_products(args.products)
    sellers = None if str(args.sellers) == "none" else load_sellers(args.sellers)
    records = read_jsonl(args.records)
    today = args.today or date.today()
    warnings: dict[int, list[str]] = {}
    errors = validate_records(records, schema, products, leaves, today, warnings, sellers)
    rel = args.records.relative_to(REPO_ROOT) if args.records.is_relative_to(REPO_ROOT) else args.records
    print(f"레코드 검증: {rel} — {len(records)}건 중 오류 {len(errors)}건, 경고 {len(warnings)}건 "
          f"(기준일 {today}, 상품 참조 {'있음 ' + str(len(products)) + '건' if products is not None else '없음'}, "
          f"판매자 참조 {'있음 ' + str(len(sellers)) + '건' if sellers is not None else '없음'})")
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
