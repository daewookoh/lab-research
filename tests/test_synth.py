"""D009 합성 데이터 생성기 v1 테스트 — 작은 규모(상품 150·판매자 25)로 검증기 4종 통과·재현성·정답 일관성을 확인."""
from __future__ import annotations

import json
from datetime import date

import pytest

from src.data.synth import CENSORED_STATUSES, flatten, generate, load_params, summarize
from src.schema import market_price as mp_mod, product as p_mod, seller as s_mod, transaction as t_mod
from src.schema.categories import load as load_categories

N_PRODUCTS, N_SELLERS = 150, 25


@pytest.fixture(scope="module")
def params():
    return load_params()


@pytest.fixture(scope="module")
def gen(params):
    tables, meta = generate(params, n_products=N_PRODUCTS, n_sellers=N_SELLERS)
    return tables, meta, date.fromisoformat(params["reference_date"])


@pytest.fixture(scope="module")
def leaves():
    return {l.code: l for l in load_categories()}


def test_sizes(gen):
    tables, meta, _ = gen
    assert len(tables["product"]) == N_PRODUCTS
    assert len(tables["seller"]) == N_SELLERS
    assert len(tables["transaction"]) >= N_PRODUCTS           # 상품당 등록 1~2건
    assert len(tables["transaction"]) <= 2 * N_PRODUCTS
    assert meta["n"] == {k: len(v) for k, v in tables.items()}
    assert meta["synthetic"] is True


def test_validators_pass(gen, leaves):
    tables, _, today = gen
    products = {p["product_id"]: p for p in tables["product"]}
    sellers = {s["seller_id"]: s for s in tables["seller"]}
    assert p_mod.validate_records(tables["product"], p_mod.load_schema(), leaves, today) == {}
    assert s_mod.validate_records(tables["seller"], s_mod.load_schema(), products, leaves, today) == {}
    assert mp_mod.validate_records(tables["market_price"], mp_mod.load_schema(), products, today) == {}
    assert t_mod.validate_records(tables["transaction"], t_mod.load_schema(), products, leaves, today, None, sellers) == {}


def test_reproducible(params):
    a, _ = generate(params, n_products=40, n_sellers=8)
    b, _ = generate(params, n_products=40, n_sellers=8)
    c, _ = generate(params, n_products=40, n_sellers=8, seed=params["seed"] + 7)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert json.dumps(a["product"], sort_keys=True) != json.dumps(c["product"], sort_keys=True)


def test_latent_consistent_with_outcome(gen):
    tables, meta, today = gen
    lat = meta["transaction_latent"]
    for t in tables["transaction"]:
        l = lat[t["transaction_id"]]
        st = t["outcome"]["status"]
        assert l["status"] == st
        assert t["censored"] == (st in CENSORED_STATUSES)
        T_days = max(0, int(round(l["T_latent_days"])))
        if st == "sold":
            assert t["duration_days"] == T_days
        else:
            assert t["duration_days"] <= l["elapsed_days"]
            if st in ("listed", "partially_sold"):
                assert t["outcome"]["observed_at"] == today.isoformat()
                assert T_days > l["elapsed_days"]        # 아직 안 팔린 이유: 잠재 T 가 경과일보다 큼
        # 승수 분해: total = grade × price × channel (v1 는 brand·season 1.0)
        m = l["multipliers"]
        assert l["multiplier_total"] == pytest.approx(m["grade"] * m["price"] * m["channel"], rel=1e-3)
        assert m["brand"] == 1.0 and m["season"] == 1.0


def test_product_seller_map_matches_transactions(gen):
    tables, meta, _ = gen
    p2s = meta["product_to_seller"]
    assert set(p2s) == {p["product_id"] for p in tables["product"]}
    for t in tables["transaction"]:
        assert t["seller_id"] == p2s[t["product_id"]]
    # 상품을 공급한 판매자의 배치에 그 상품이 들어 있음
    in_batches = {pid for s in tables["seller"] for b in s["supply_history"] for pid in (b.get("product_ids") or [])}
    assert in_batches == set(p2s)


def test_permit_channels_excluded(gen, leaves, params):
    tables, _, _ = gen
    products = {p["product_id"]: p for p in tables["product"]}
    excluded = set(params["transaction"]["permit_excluded_channels"])
    for t in tables["transaction"]:
        if leaves[products[t["product_id"]]["category_code"]].regulation == "permit":
            assert t["channel"] not in excluded


def test_flatten_and_summary(gen, params):
    tables, meta, _ = gen
    df = flatten(tables, meta)
    assert len(df) == len(tables["transaction"])
    assert {"duration_days", "censored", "latent_T_days", "latent_mult_grade", "channel"} <= set(df.columns)
    text = summarize(tables, meta, params)
    assert "관측 상태 비율" in text and "등급 분포" in text
