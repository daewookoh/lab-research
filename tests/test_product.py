"""D003 상품 속성 스키마 검증 테스트. 기준일은 예시 레코드가 작성된 2026-10-30 으로 고정."""
import copy
from datetime import date

import pytest

from src.schema.categories import load as load_categories
from src.schema.product import (
    EXAMPLES_PATH,
    cross_checks,
    load_schema,
    read_jsonl,
    validate_records,
)

TODAY = date(2026, 10, 30)


@pytest.fixture(scope="module")
def schema():
    return load_schema()


@pytest.fixture(scope="module")
def leaves():
    return {l.code: l for l in load_categories()}


@pytest.fixture(scope="module")
def examples():
    return read_jsonl(EXAMPLES_PATH)


@pytest.fixture
def rec(examples):
    return copy.deepcopy(examples[0])  # 감자칩(FD-PR-SNK, expiry medium, S, sealed)


def _errs(record, schema, leaves):
    return validate_records([record], schema, leaves, TODAY).get(0, [])


def test_schema_is_valid_draft2020(schema):
    assert schema["$schema"].endswith("2020-12/schema")
    assert set(schema["required"]) >= {"category_code", "brand", "list_price_krw",
                                       "condition_grade", "packaging_state", "spec"}


def test_examples_all_pass(examples, schema, leaves):
    assert len(examples) == 15
    assert validate_records(examples, schema, leaves, TODAY) == {}


def test_examples_cover_enums(examples):
    assert {r["condition_grade"] for r in examples} == {"S", "A", "B", "C"}
    assert {r["packaging_state"] for r in examples} == {"sealed", "box_opened", "box_damaged", "no_box", "repackaged"}
    assert {r["brand"]["tier"] for r in examples} == {"major", "mid", "minor", "unbranded", "unknown"}


def test_example_ids_unique(examples):
    ids = [r["product_id"] for r in examples]
    assert len(ids) == len(set(ids))


def test_missing_required_field(rec, schema, leaves):
    del rec["brand"]
    errs = _errs(rec, schema, leaves)
    assert any("brand" in e and "required" in e for e in errs)


def test_additional_property_rejected(rec, schema, leaves):
    rec["price"] = 1000
    errs = _errs(rec, schema, leaves)
    assert any("Additional properties" in e for e in errs)


def test_bad_enum_and_range(rec, schema, leaves):
    rec["condition_grade"] = "D"
    rec["list_price_krw"] = 50
    errs = _errs(rec, schema, leaves)
    assert any("condition_grade" in e for e in errs)
    assert any("list_price_krw" in e for e in errs)


def test_bad_date_format(rec, schema, leaves):
    rec["acquired_date"] = "2026/05/12"
    errs = _errs(rec, schema, leaves)
    assert any("acquired_date" in e and "date" in e for e in errs)


def test_unknown_category_code(rec, schema, leaves):
    rec["category_code"] = "ZZ-ZZ-ZZZ"
    errs = _errs(rec, schema, leaves)
    assert any("categories.yaml 에 없음" in e for e in errs)


def test_expiry_required_for_food(rec, schema, leaves):
    rec["expiry_date"] = None
    errs = _errs(rec, schema, leaves)
    assert any("expiry_date 필수" in e for e in errs)


def test_expiry_forbidden_for_non_expiring(examples, schema, leaves):
    r = copy.deepcopy(examples[7])  # 여성의류 FA-AP-WOM, expiry none
    r["expiry_date"] = "2027-01-01"
    errs = _errs(r, schema, leaves)
    assert any("expiry_class=none" in e for e in errs)


def test_expired_rejected(rec, schema, leaves):
    rec["expiry_date"] = "2026-10-01"
    errs = _errs(rec, schema, leaves)
    assert any("재유통 불가" in e for e in errs)


def test_near_expiry_needs_grade_c(rec, schema, leaves):
    rec["expiry_date"] = "2026-12-15"  # 46일 남음
    rec["condition_grade"] = "S"
    errs = _errs(rec, schema, leaves)
    assert any("임박" in e for e in errs)
    rec["condition_grade"] = "C"
    assert _errs(rec, schema, leaves) == []


def test_sealed_grade_b_contradiction(rec, schema, leaves):
    rec["condition_grade"] = "B"  # sealed + B, 기한 임박 아님
    errs = _errs(rec, schema, leaves)
    assert any("모순" in e for e in errs)


def test_grade_s_requires_intact_packaging(rec, schema, leaves):
    rec["packaging_state"] = "no_box"
    errs = _errs(rec, schema, leaves)
    assert any("S 는 밀봉·원박스만" in e for e in errs)


def test_future_acquired_date(rec, schema, leaves):
    rec["acquired_date"] = "2026-11-15"
    errs = _errs(rec, schema, leaves)
    assert any("기준일" in e and "이후" in e for e in errs)


def test_size_class_mismatch_is_warning_not_error(examples, schema, leaves):
    r = copy.deepcopy(examples[14])  # 강아지 간식 small vs 카테고리 large
    errs, warns = cross_checks(r, leaves, TODAY)
    assert errs == []
    assert any("두 단계 차이" in w for w in warns)
    assert _errs(r, schema, leaves) == []
