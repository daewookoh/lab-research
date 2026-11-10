"""D004 거래 결과 스키마 검증 테스트. 기준일은 예시 레코드가 작성된 2026-11-02 로 고정."""
import copy
from datetime import date

import pytest

from src.schema.categories import load as load_categories
from src.schema.transaction import (
    EXAMPLES_PATH,
    cross_checks,
    expected_duration,
    expected_recovery,
    load_products,
    load_schema,
    read_jsonl,
    validate_records,
)

TODAY = date(2026, 11, 2)


@pytest.fixture(scope="module")
def schema():
    return load_schema()


@pytest.fixture(scope="module")
def leaves():
    return {l.code: l for l in load_categories()}


@pytest.fixture(scope="module")
def products():
    return load_products()


@pytest.fixture(scope="module")
def examples():
    return read_jsonl(EXAMPLES_PATH)


@pytest.fixture
def sold(examples):
    return copy.deepcopy(examples[0])  # T00000001: 감자칩, open_market, sold, 25일


@pytest.fixture
def listed(examples):
    return copy.deepcopy(examples[4])  # T00000005: 비타민C, own_online, listed


def _errs(record, schema, products, leaves):
    return validate_records([record], schema, products, leaves, TODAY).get(0, [])


# ---------- 정상 경로 ----------

def test_schema_is_valid_draft_2020_12(schema):
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["additionalProperties"] is False
    assert {"duration_days", "censored", "recovery_rate"} <= set(schema["required"])


def test_examples_all_pass(examples, schema, products, leaves):
    assert len(examples) == 18
    errors = validate_records(examples, schema, products, leaves, TODAY)
    assert errors == {}, errors


def test_examples_cover_all_statuses_and_channels(examples, schema):
    statuses = {r["outcome"]["status"] for r in examples}
    channels = {r["channel"] for r in examples}
    assert statuses == set(schema["properties"]["outcome"]["properties"]["status"]["enum"])
    assert channels == set(schema["properties"]["channel"]["enum"])


def test_examples_without_product_reference(examples, schema, leaves):
    """상품 파일 없이도(products=None) 거래 자체 규칙만으로 통과해야 한다."""
    assert validate_records(examples, schema, None, leaves, TODAY) == {}


def test_target_definitions(sold):
    assert expected_duration(sold) == 25  # 2026-09-03 → 2026-09-28
    # (21000×12 − 25200 − 36000) / (14000×12) = 190800 / 168000
    assert expected_recovery(sold) == pytest.approx(1.1357, abs=1e-4)


def test_consignment_has_null_recovery(examples):
    con = next(r for r in examples if r["purchase"]["acquisition_type"] == "consignment")
    assert con["purchase"]["buy_price_krw"] == 0 and con["recovery_rate"] is None


# ---------- 스키마 오류 주입 ----------

def test_missing_required(sold, schema, products, leaves):
    del sold["recovery_rate"]
    assert any("recovery_rate" in e and "[schema]" in e for e in _errs(sold, schema, products, leaves))


def test_additional_property_rejected(sold, schema, products, leaves):
    sold["profit"] = 1
    assert any("[schema]" in e for e in _errs(sold, schema, products, leaves))


def test_bad_channel_enum(sold, schema, products, leaves):
    sold["channel"] = "tv_shopping"
    assert any("channel" in e and "[schema]" in e for e in _errs(sold, schema, products, leaves))


def test_bad_date_format(sold, schema, products, leaves):
    sold["listing"]["listed_date"] = "2026/09/03"
    assert any("listed_date" in e for e in _errs(sold, schema, products, leaves))


# ---------- 교차 규칙 오류 주입 ----------

def test_unknown_product(sold, schema, products, leaves):
    sold["product_id"] = "P99999999"
    assert any("상품 레코드에 없음" in e for e in _errs(sold, schema, products, leaves))


def test_consignment_with_price(sold, schema, products, leaves):
    sold["purchase"]["acquisition_type"] = "consignment"
    assert any("위탁" in e for e in _errs(sold, schema, products, leaves))


def test_listing_quantity_exceeds_purchase(sold, schema, products, leaves):
    sold["purchase"]["quantity"] = 5
    assert any("purchase.quantity" in e for e in _errs(sold, schema, products, leaves))


def test_date_order(sold, schema, products, leaves):
    sold["listing"]["listed_date"] = "2026-08-30"  # buy_date 2026-09-01 이전
    errs = _errs(sold, schema, products, leaves)
    assert any("buy_date" in e and "이전" in e for e in errs)


def test_observed_after_today(listed, schema, products, leaves):
    listed["outcome"]["observed_at"] = "2026-11-03"
    listed["duration_days"] = expected_duration(listed)
    assert any("기준일" in e for e in _errs(listed, schema, products, leaves))


def test_sold_requires_full_quantity(sold, schema, products, leaves):
    sold["outcome"]["quantity_sold"] = 11
    sold["recovery_rate"] = expected_recovery(sold)
    assert any("quantity_sold" in e and "listing.quantity" in e for e in _errs(sold, schema, products, leaves))


def test_listed_with_sales_is_inconsistent(listed, schema, products, leaves):
    listed["outcome"]["quantity_sold"] = 1
    listed["outcome"]["sold_price_krw"] = 20000
    listed["recovery_rate"] = expected_recovery(listed)
    assert any("partially_sold" in e for e in _errs(listed, schema, products, leaves))


def test_censored_flag_must_match_status(sold, schema, products, leaves):
    sold["censored"] = True
    assert any("censored" in e for e in _errs(sold, schema, products, leaves))


def test_sold_price_above_listed(sold, schema, products, leaves):
    sold["outcome"]["sold_price_krw"] = 23000
    sold["recovery_rate"] = expected_recovery(sold)
    assert any("listed_price_krw" in e and "초과" in e for e in _errs(sold, schema, products, leaves))


def test_buy_price_above_list_price(sold, schema, products, leaves):
    sold["purchase"]["buy_price_krw"] = 40000  # 감자칩 정상가 32,000
    sold["recovery_rate"] = expected_recovery(sold)
    assert any("정상가" in e and "초과" in e for e in _errs(sold, schema, products, leaves))


def test_sale_after_expiry(examples, schema, products, leaves):
    rec = copy.deepcopy(examples[1])  # T00000002 캔커피, 유통기한 2027-02-28
    products2 = copy.deepcopy(products)
    products2["P00000002"]["expiry_date"] = "2026-09-14"  # 판매일 09-15 하루 전으로 당김
    assert any("유통기한" in e and "경과" in e for e in _errs(rec, schema, products2, leaves))


def test_duration_mismatch(sold, schema, products, leaves):
    sold["duration_days"] += 1
    assert any("duration_days" in e for e in _errs(sold, schema, products, leaves))


def test_recovery_mismatch(sold, schema, products, leaves):
    sold["recovery_rate"] = round(sold["recovery_rate"] + 0.01, 4)
    assert any("recovery_rate" in e for e in _errs(sold, schema, products, leaves))


def test_recovery_tolerance_half_unit_passes(sold, schema, products, leaves):
    sold["recovery_rate"] = sold["recovery_rate"] + 0.00004
    assert _errs(sold, schema, products, leaves) == []


# ---------- 경고 (통과하되 보고) ----------

def test_warnings_are_not_errors(examples, schema, products, leaves):
    warnings = {}
    errors = validate_records(examples, schema, products, leaves, TODAY, warnings)
    assert errors == {}
    joined = "\n".join(w for ws in warnings.values() for w in ws)
    assert "regulation=permit" in joined       # T00000004
    assert "정상가 69000 초과" in joined          # T00000015
    assert "quantity_on_hand" in joined          # T00000016
    assert "적자" in joined                      # T00000018
    assert len(warnings) == 4


def test_loss_warning_direct(examples, products, leaves):
    rec = copy.deepcopy(examples[17])
    errs, warns = cross_checks(rec, products, leaves, TODAY)
    assert errs == [] and any("적자" in w for w in warns)


# ---------- 판매자 참조 (D005 추가) ----------

@pytest.fixture(scope="module")
def sellers():
    from src.schema.transaction import load_sellers
    return load_sellers()


def test_examples_pass_with_seller_reference(examples, schema, products, leaves, sellers):
    """예시 18건 모두 판매자 레코드가 있고, buy_date 에 해당 배치가 있어 판매자 경고가 없어야 한다."""
    warnings = {}
    assert validate_records(examples, schema, products, leaves, TODAY, warnings, sellers) == {}
    assert not any("공급 이력" in w for ws in warnings.values() for w in ws)


def test_unknown_seller(sold, schema, products, leaves, sellers):
    sold["seller_id"] = "S999999"
    errs = validate_records([sold], schema, products, leaves, TODAY, None, sellers).get(0, [])
    assert any("판매자 레코드에 없음" in e for e in errs)


def test_buy_before_seller_registration(sold, schema, products, leaves, sellers):
    sellers2 = copy.deepcopy(sellers)
    sellers2["S000101"]["registered_date"] = "2026-09-02"  # buy_date 2026-09-01
    errs = validate_records([sold], schema, products, leaves, TODAY, None, sellers2).get(0, [])
    assert any("registered_date" in e for e in errs)


def test_batch_acquisition_type_mismatch(sold, schema, products, leaves, sellers):
    sellers2 = copy.deepcopy(sellers)
    sellers2["S000101"]["supply_history"][1]["acquisition_type"] = "consignment"
    errs = validate_records([sold], schema, products, leaves, TODAY, None, sellers2).get(0, [])
    assert any("배치" in e and "consignment" in e for e in errs)


def test_missing_batch_is_warning(sold, schema, products, leaves, sellers):
    sellers2 = copy.deepcopy(sellers)
    sellers2["S000101"]["supply_history"] = []
    warnings = {}
    assert validate_records([sold], schema, products, leaves, TODAY, warnings, sellers2) == {}
    assert any("배치가 없음" in w for w in warnings.get(0, []))
