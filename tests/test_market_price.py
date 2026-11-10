"""D005 시세 관측 스키마 검증 테스트. 기준일은 예시 레코드가 작성된 2026-11-10 로 고정."""
import copy
from datetime import date

import pytest

from src.schema.market_price import (
    EXAMPLES_PATH,
    cross_checks,
    load_products,
    load_schema,
    normalized_median,
    price_ratio,
    read_jsonl,
    validate_records,
)

TODAY = date(2026, 11, 10)


@pytest.fixture(scope="module")
def schema():
    return load_schema()


@pytest.fixture(scope="module")
def products():
    return load_products()


@pytest.fixture(scope="module")
def examples():
    return read_jsonl(EXAMPLES_PATH)


@pytest.fixture
def rec(examples):
    return copy.deepcopy(examples[5])  # M00000006: 컵라면, open_market, exact, new, n=21


@pytest.fixture
def pack(examples):
    return copy.deepcopy(examples[3])  # M00000004: 캔커피 24입 관측 (상품은 30입)


def _errs(record, schema, products):
    return validate_records([record], schema, products, TODAY).get(0, [])


# ---------- 정상 경로 ----------

def test_schema_is_valid_draft_2020_12(schema):
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["additionalProperties"] is False
    assert {"price_min_krw", "price_median_krw", "price_max_krw", "n_listings"} <= set(schema["required"])


def test_examples_all_pass(examples, schema, products):
    assert len(examples) == 29
    assert validate_records(examples, schema, products, TODAY) == {}


def test_examples_cover_channels_matches_conditions(examples, schema):
    assert {r["source"]["channel"] for r in examples} == set(schema["properties"]["source"]["properties"]["channel"]["enum"])
    assert {r["match_type"] for r in examples} == set(schema["properties"]["match_type"]["enum"])
    assert {r["condition"] for r in examples} == {"new", "used"}
    assert all(r["source"]["collected_by"] == "synthetic" for r in examples)  # 합성임을 레코드 수준에서 표시


def test_examples_reference_all_products(examples, products):
    assert {r["product_id"] for r in examples} == set(products)


def test_examples_without_product_reference(examples, schema):
    assert validate_records(examples, schema, None, TODAY) == {}


def test_normalization(pack, products):
    # 캔커피: 상품 30입, 관측 24입 중앙값 15,900 → 30입 환산 19,875 / 정상가 21,000 = 0.9464
    assert normalized_median(pack, products["P00000002"]) == pytest.approx(19875)
    assert price_ratio(pack, products["P00000002"]) == pytest.approx(0.9464, abs=1e-4)


def test_no_price_record_has_none_ratio(examples, products):
    none_rec = next(r for r in examples if r["n_listings"] == 0)
    assert price_ratio(none_rec, products[none_rec["product_id"]]) is None


# ---------- 스키마 오류 주입 ----------

def test_missing_required(rec, schema, products):
    del rec["shipping_included"]
    assert any("shipping_included" in e and "[schema]" in e for e in _errs(rec, schema, products))


def test_additional_property_rejected(rec, schema, products):
    rec["price_ratio"] = 0.9
    assert any("[schema]" in e for e in _errs(rec, schema, products))


def test_bad_channel_enum(rec, schema, products):
    rec["source"]["channel"] = "tv_shopping"
    assert any("channel" in e for e in _errs(rec, schema, products))


def test_bad_collected_by(rec, schema, products):
    rec["source"]["collected_by"] = "guess"
    assert any("collected_by" in e for e in _errs(rec, schema, products))


# ---------- 교차 규칙 오류 주입 ----------

def test_unknown_product(rec, schema, products):
    rec["product_id"] = "P99999999"
    assert any("상품 레코드에 없음" in e for e in _errs(rec, schema, products))


def test_observed_after_today(rec, schema, products):
    rec["observed_at"] = "2026-11-11"
    assert any("기준일" in e for e in _errs(rec, schema, products))


def test_price_order(rec, schema, products):
    rec["price_min_krw"], rec["price_max_krw"] = rec["price_max_krw"], rec["price_min_krw"]
    assert any("순서 위반" in e for e in _errs(rec, schema, products))


def test_zero_listings_with_price(rec, schema, products):
    rec["n_listings"] = 0
    assert any("n_listings=0" in e for e in _errs(rec, schema, products))


def test_listings_without_price(rec, schema, products):
    rec["price_median_krw"] = None
    assert any("null" in e and "세 값" in e for e in _errs(rec, schema, products))


def test_single_listing_prices_must_match(rec, schema, products):
    rec["n_listings"] = 1
    assert any("n_listings=1" in e for e in _errs(rec, schema, products))


def test_exact_requires_same_unit_count(rec, schema, products):
    rec["unit_count"] = 12  # 컵라면 상품은 24입
    assert any("match_type=exact" in e for e in _errs(rec, schema, products))


def test_duplicate_price_id_and_key(examples, schema, products):
    a = copy.deepcopy(examples[5])
    b = copy.deepcopy(examples[5])
    errors = validate_records([a, b], schema, products, TODAY)
    assert 1 in errors
    assert any("price_id" in e and "중복" in e for e in errors[1])
    assert any("이미 있음" in e for e in errors[1])


def test_same_key_different_id_is_duplicate(examples, schema, products):
    a = copy.deepcopy(examples[5])
    b = copy.deepcopy(examples[5])
    b["price_id"] = "M00000099"
    errors = validate_records([a, b], schema, products, TODAY)
    assert 1 in errors and any("이미 있음" in e for e in errors[1])


# ---------- 경고 ----------

def test_warnings_are_not_errors(examples, schema, products):
    warnings = {}
    assert validate_records(examples, schema, products, TODAY, warnings) == {}
    joined = "\n".join(w for ws in warnings.values() for w in ws)
    assert "1.59배" in joined                      # M00000007 비타민C 정상가 초과
    assert "신품 시세가 정상가 초과" in joined       # M00000001 감자칩
    assert "condition=used 를 own_online" in joined  # M00000017
    assert joined.count("대표 시세로 부적합") == 4   # n<3: M8, M10, M15, M29
    assert len(warnings) == 7


def test_unit_mistake_warns_low_ratio(rec, products):
    rec["match_type"] = "similar"
    rec["unit_count"] = 240  # 단위 착오 → 환산 중앙값이 정상가의 0.09배
    errs, warns = cross_checks(rec, products, TODAY)
    assert errs == [] and any("단위 착오" in w for w in warns)
