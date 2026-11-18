"""D011 합성 데이터 생성기 v2 테스트 — 작은 규모(상품 300·판매자 60)로 v2 승수(브랜드·시즌·판매자)·검증기·v1 불변을 확인."""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import date

import numpy as np
import pytest

from src.data.synth import DEFAULT_PARAMS, flatten, generate, load_params
from src.reflow.score_v0 import season_state
from src.schema import market_price as mp_mod, product as p_mod, seller as s_mod, transaction as t_mod
from src.schema.categories import load as load_categories

PARAMS_V2 = DEFAULT_PARAMS.parent / "params_v2.yaml"
N_PRODUCTS, N_SELLERS = 300, 60


@pytest.fixture(scope="module")
def params():
    return load_params(PARAMS_V2)


@pytest.fixture(scope="module")
def gen(params):
    tables, meta = generate(params, n_products=N_PRODUCTS, n_sellers=N_SELLERS)
    return tables, meta, date.fromisoformat(params["reference_date"])


@pytest.fixture(scope="module")
def leaves():
    return {l.code: l for l in load_categories()}


def test_v2_params_and_meta(params, gen):
    _, meta, _ = gen
    assert params["version"] == "2" and params["multipliers"]["active"] == "enabled_v2"
    assert meta["version"] == "2" and meta["generated_for_backlog"] == "D011"
    assert meta["multipliers_enabled"] == ["grade", "price", "channel", "season", "brand", "seller"]
    assert len(meta["seller_latent"]) == N_SELLERS
    assert meta["n"]["seller"] == N_SELLERS and meta["n"]["product"] == N_PRODUCTS


def test_validators_pass(gen, leaves):
    tables, _, today = gen
    products = {p["product_id"]: p for p in tables["product"]}
    sellers = {s["seller_id"]: s for s in tables["seller"]}
    assert p_mod.validate_records(tables["product"], p_mod.load_schema(), leaves, today) == {}
    assert s_mod.validate_records(tables["seller"], s_mod.load_schema(), products, leaves, today) == {}
    assert mp_mod.validate_records(tables["market_price"], mp_mod.load_schema(), products, today) == {}
    assert t_mod.validate_records(tables["transaction"], t_mod.load_schema(), products, leaves, today, None, sellers) == {}


def test_multiplier_decomposition(gen, params, leaves):
    """total = grade × price × channel × brand × season × seller, 각 항이 params·판정 규칙과 일치."""
    tables, meta, _ = gen
    M = params["multipliers"]
    products = {p["product_id"]: p for p in tables["product"]}
    lat, slat = meta["transaction_latent"], meta["seller_latent"]
    seen_season, seen_brand = set(), set()
    for t in tables["transaction"]:
        l, m = lat[t["transaction_id"]], lat[t["transaction_id"]]["multipliers"]
        p = products[t["product_id"]]
        leaf = leaves[p["category_code"]]
        assert l["multiplier_total"] == pytest.approx(
            m["grade"] * m["price"] * m["channel"] * m["brand"] * m["season"] * m["seller"], rel=1e-3)
        assert m["brand"] == M["brand"][p["brand"]["tier"]]
        listed = date.fromisoformat(t["listing"]["listed_date"])
        assert l["season_state"] == season_state(leaf.seasonality, listed.month)
        assert m["season"] == M["season"][l["season_state"]]
        assert m["seller"] == pytest.approx(math.exp(slat[t["seller_id"]]["u"]), rel=1e-3)
        seen_season.add(l["season_state"]); seen_brand.add(p["brand"]["tier"])
    assert {"in_season", "near_season", "off_season", "neutral"} <= seen_season
    assert {"major", "mid", "minor", "unbranded"} <= seen_brand


def test_seller_effect_and_accuracy_link(gen, params):
    """u ~ N(0, σ) 이고, slow 판매자의 공급 이력이 fast 보다 accurate 비율이 낮다(프로필 조건부 분포)."""
    tables, meta, _ = gen
    us = np.array([v["u"] for v in meta["seller_latent"].values()])
    sigma = params["v2"]["seller_effect_sigma"]
    assert abs(us.mean()) < 0.15 and 0.5 * sigma < us.std() < 1.6 * sigma
    acc = {"fast": Counter(), "slow": Counter()}
    for s in tables["seller"]:
        prof = meta["seller_latent"][s["seller_id"]]["profile"]
        if prof in acc:
            for b in s["supply_history"]:
                acc[prof][b["description_accuracy"]] += 1
    share = {k: v["accurate"] / max(1, sum(v.values())) for k, v in acc.items()}
    assert sum(acc["fast"].values()) > 20 and sum(acc["slow"].values()) > 20
    assert share["fast"] > share["slow"]


def test_pre_season_listing_bias(gen, leaves, params):
    """시즌 품목은 near_season 등록 비율이 비시즌 품목의 균등 기대치보다 높다."""
    tables, meta, _ = gen
    products = {p["product_id"]: p for p in tables["product"]}
    seasonal = [meta["product_latent"][p["product_id"]] for p in tables["product"] if leaves[p["category_code"]].seasonality]
    near = np.mean([l["season_state"] == "near_season" for l in seasonal])
    assert meta["generator_stats"]["pre_season_relisted"] > 0
    assert near >= 0.25   # 균등이면 소분류별 near 달 비율이 대개 1/6~1/3, 여기에 35% 가 near 로 강제됨
    # 매입→등록 간격·관측창 규칙은 그대로 지켜진다
    lo_w, hi_w = params["listing_window_days"]
    gap_lo, gap_hi = params["transaction"]["listed_after_buy_days"]
    for t in tables["transaction"]:
        bd, ld = date.fromisoformat(t["purchase"]["buy_date"]), date.fromisoformat(t["listing"]["listed_date"])
        assert gap_lo <= (ld - bd).days <= gap_hi
        # v1 부터의 규칙: 배치 매입일 = ref − (U(window) + U(gap)) 뒤에 gap 을 다시 뽑으므로 경과일 상한은 window + gap_hi − 1 (v1 과 동일한 꼬리)
        assert lo_w <= (date.fromisoformat(params["reference_date"]) - ld).days <= hi_w + gap_hi - 1


def test_flatten_v2_columns(gen):
    tables, meta, _ = gen
    df = flatten(tables, meta)
    for c in ("listed_month", "season_state", "latent_mult_brand", "latent_mult_season", "latent_mult_seller", "latent_seller_u"):
        assert c in df.columns
    assert np.allclose(df.latent_mult_seller, np.exp(df.latent_seller_u), atol=2e-3)
    assert set(df.listed_month.unique()) <= set(range(1, 13))


def test_v1_params_unchanged_by_v2_code():
    """v1 params 로는 brand·season·seller 승수가 전부 1.0 (v1 데이터 재현성 유지)."""
    tables, meta = generate(load_params(DEFAULT_PARAMS), n_products=60, n_sellers=10)
    assert meta["multipliers_enabled"] == ["grade", "price", "channel"] and meta["seller_latent"] == {}
    for l in meta["transaction_latent"].values():
        assert l["multipliers"]["brand"] == 1.0 and l["multipliers"]["season"] == 1.0 and l["multipliers"]["seller"] == 1.0
    assert json.dumps(tables["product"][0], sort_keys=True)   # 직렬화 가능
