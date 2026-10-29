"""D002 카테고리 체계 검증 테스트."""
import copy

import pytest

from src.schema.categories import (
    EXPECTED_LEAF_COUNT,
    CategoryError,
    flatten,
    load,
    load_raw,
    validate,
)


@pytest.fixture(scope="module")
def raw():
    return load_raw()


def test_schema_valid(raw):
    assert validate(raw) == []


def test_leaf_count(raw):
    assert len(flatten(raw)) == EXPECTED_LEAF_COUNT == 30


def test_three_levels(raw):
    for l1 in raw["categories"]:
        assert l1["children"]
        for l2 in l1["children"]:
            assert l2["children"]
            for l3 in l2["children"]:
                assert "children" not in l3


def test_codes_unique_and_prefixed(raw):
    leaves = flatten(raw)
    codes = [l.code for l in leaves]
    assert len(codes) == len(set(codes))
    for l in leaves:
        assert l.code.startswith(l.l2_code + "-")
        assert l.l2_code.startswith(l.l1_code + "-")


def test_load_returns_leaves():
    leaves = load()
    assert all(l.examples for l in leaves)
    assert all(l.expiry_class in ("none", "long", "medium", "short") for l in leaves)


def test_duplicate_code_detected(raw):
    bad = copy.deepcopy(raw)
    bad["categories"][0]["children"][0]["children"][0]["code"] = \
        bad["categories"][0]["children"][0]["children"][1]["code"]
    errs = validate(bad)
    assert any("코드 중복" in e for e in errs)


def test_bad_enum_detected(raw):
    bad = copy.deepcopy(raw)
    bad["categories"][0]["children"][0]["children"][0]["expiry_class"] = "forever"
    errs = validate(bad)
    assert any("허용값 아님" in e for e in errs)


def test_load_raises_on_invalid(tmp_path, raw):
    import yaml
    bad = copy.deepcopy(raw)
    del bad["categories"][-1]  # 소분류 개수 어긋남
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(bad, allow_unicode=True), encoding="utf-8")
    with pytest.raises(CategoryError):
        load(p)
