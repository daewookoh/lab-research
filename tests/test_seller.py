"""D005 판매자 이력 스키마 검증 테스트. 기준일은 예시 레코드가 작성된 2026-11-10 로 고정."""
import copy
from datetime import date

import pytest

from src.schema.categories import load as load_categories
from src.schema.seller import (
    EXAMPLES_PATH,
    cross_checks,
    history_stats,
    load_products,
    load_schema,
    read_jsonl,
    validate_records,
)

TODAY = date(2026, 11, 10)


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
def seller(examples):
    return copy.deepcopy(examples[0])  # S000101: 배치 2건, active


@pytest.fixture
def blocked(examples):
    return copy.deepcopy(examples[9])  # S000110: blocked, major_gap + dispute


def _errs(record, schema, products, leaves):
    return validate_records([record], schema, products, leaves, TODAY).get(0, [])


# ---------- 정상 경로 ----------

def test_schema_is_valid_draft_2020_12(schema):
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["additionalProperties"] is False
    assert schema["properties"]["supply_history"]["items"]["additionalProperties"] is False


def test_examples_all_pass(examples, schema, products, leaves):
    assert len(examples) == 17
    assert validate_records(examples, schema, products, leaves, TODAY) == {}


def test_examples_cover_statuses_types_accuracy(examples, schema):
    assert {r["status"] for r in examples} == set(schema["properties"]["status"]["enum"])
    assert {r["business"]["type"] for r in examples} == set(schema["properties"]["business"]["properties"]["type"]["enum"])
    accs = {b["description_accuracy"] for r in examples for b in r["supply_history"]}
    assert accs == {"accurate", "minor_gap", "major_gap"}  # unknown 은 예시에 없음


def test_examples_cover_transaction_sellers(examples):
    from src.schema.transaction import EXAMPLES_PATH as TX_PATH
    tx_sellers = {r["seller_id"] for r in read_jsonl(TX_PATH) if r.get("seller_id")}
    assert tx_sellers <= {r["seller_id"] for r in examples}


def test_examples_without_product_reference(examples, schema, leaves):
    assert validate_records(examples, schema, None, leaves, TODAY) == {}


def test_history_stats(seller):
    s = history_stats(seller)
    assert s["n_batches"] == 2 and s["n_products"] == 4
    assert s["declared_total_krw"] == 910000 + 384000
    assert s["major_gap_ratio"] == 0 and s["last_supplied"] == "2026-09-01"


# ---------- 스키마 오류 주입 ----------

def test_missing_required(seller, schema, products, leaves):
    del seller["supply_history"]
    assert any("supply_history" in e and "[schema]" in e for e in _errs(seller, schema, products, leaves))


def test_additional_property_rejected(seller, schema, products, leaves):
    seller["business"]["phone"] = "010-0000-0000"
    assert any("[schema]" in e for e in _errs(seller, schema, products, leaves))


def test_bad_region_enum(seller, schema, products, leaves):
    seller["business"]["region"] = "busan"
    assert any("region" in e for e in _errs(seller, schema, products, leaves))


def test_bad_batch_id_pattern(seller, schema, products, leaves):
    seller["supply_history"][0]["batch_id"] = "BATCH-1"
    assert any("batch_id" in e for e in _errs(seller, schema, products, leaves))


# ---------- 교차 규칙 오류 주입 ----------

def test_unknown_level2_code(seller, schema, products, leaves):
    seller["business"]["main_category_codes"] = ["FD-PR-SNK"[:5], "ZZ-ZZ"]
    assert any("ZZ-ZZ" in e and "중분류" in e for e in _errs(seller, schema, products, leaves))


def test_batch_before_registration(seller, schema, products, leaves):
    seller["supply_history"][0]["supplied_date"] = "2026-05-01"  # registered 2026-05-20
    assert any("registered_date" in e and "이전" in e for e in _errs(seller, schema, products, leaves))


def test_batch_after_today(seller, schema, products, leaves):
    seller["supply_history"][1]["supplied_date"] = "2026-11-11"
    assert any("기준일" in e for e in _errs(seller, schema, products, leaves))


def test_batches_must_be_chronological(seller, schema, products, leaves):
    seller["supply_history"].reverse()
    assert any("시간순" in e for e in _errs(seller, schema, products, leaves))


def test_duplicate_batch_id(seller, schema, products, leaves):
    seller["supply_history"][1]["batch_id"] = seller["supply_history"][0]["batch_id"]
    assert any("중복" in e for e in _errs(seller, schema, products, leaves))


def test_product_ids_length_and_existence(seller, schema, products, leaves):
    seller["supply_history"][1]["product_ids"] = ["P00000001", "P99999999"]
    errs = _errs(seller, schema, products, leaves)
    assert any("product_count" in e for e in errs)
    assert any("P99999999" in e and "없음" in e for e in errs)


def test_product_acquired_after_supply(seller, schema, products, leaves):
    products2 = copy.deepcopy(products)
    products2["P00000001"]["acquired_date"] = "2026-09-02"  # 배치 2026-09-01
    assert any("acquired_date" in e for e in _errs(seller, schema, products2, leaves))


def test_settled_exceeds_declared(seller, schema, products, leaves):
    seller["supply_history"][1]["settled_value_krw"] = 400000  # 신고 384,000
    assert any("settled_value_krw" in e and "초과" in e for e in _errs(seller, schema, products, leaves))


def test_accurate_with_mismatch_issue_is_contradiction(seller, schema, products, leaves):
    seller["supply_history"][1]["issue"] = "quantity_short"
    assert any("모순" in e for e in _errs(seller, schema, products, leaves))


def test_blocked_requires_evidence(blocked, schema, products, leaves):
    blocked["supply_history"][0]["description_accuracy"] = "accurate"
    blocked["supply_history"][0]["issue"] = "none"
    assert any("blocked" in e for e in _errs(blocked, schema, products, leaves))


def test_dormant_requires_old_history(seller, schema, products, leaves):
    seller["status"] = "dormant"  # 마지막 공급 2026-09-01, 70일 전
    assert any("dormant" in e and "이내" in e for e in _errs(seller, schema, products, leaves))


def test_duplicate_seller_id(examples, schema, products, leaves):
    recs = [copy.deepcopy(examples[0]), copy.deepcopy(examples[0])]
    errors = validate_records(recs, schema, products, leaves, TODAY)
    assert 1 in errors and any("seller_id" in e and "중복" in e for e in errors[1])


# ---------- 경고 ----------

def test_warnings_are_not_errors(examples, schema, products, leaves):
    warnings = {}
    assert validate_records(examples, schema, products, leaves, TODAY, warnings) == {}
    joined = "\n".join(w for ws in warnings.values() for w in ws)
    assert "S000109" in joined and "major_gap" in joined
    assert "S000116" in joined and "공급 이력 없음" in joined
    assert len(warnings) == 2


def test_active_but_stale_warns(seller, products, leaves):
    seller["registered_date"] = "2025-05-01"
    seller["supply_history"] = [seller["supply_history"][0]]
    seller["supply_history"][0]["supplied_date"] = "2025-06-15"  # 기준일 기준 513일 전
    errs, warns = cross_checks(seller, products, leaves, TODAY)
    assert errs == [] and any("dormant 검토" in w for w in warns)
