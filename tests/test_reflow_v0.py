"""D007 Re-Flow Score v0 단위테스트.

명세 research/2026-11-11/reflow_score_v0_spec.md 의 경계값·게이트·결측 규칙을 고정한다.
숫자는 전부 가정값이므로, 테스트는 "명세와 코드가 같은 숫자를 말하는가" 를 묻는 것이지 숫자의 타당성을 묻지 않는다.
마지막 회귀 테스트는 11-11 프로토타입 로그(reflow_v0_check.txt)의 표와 모듈 출력이 15건 전부 일치하는지 본다.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.reflow import score_v0 as rf
from src.schema.categories import Leaf, load as load_categories

TODAY = date(2026, 11, 11)
ROOT = Path(__file__).resolve().parents[1]
PROTO_LOG = ROOT / "research" / "2026-11-11" / "reflow_v0_check.txt"


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------
def make_leaf(code="FD-PR-SNK", expiry_class="medium", seasonality=(), regulation="labeling") -> Leaf:
    l1, l2, _ = code.split("-")
    return Leaf(code=code, name="x", l1_code=l1, l1_name="x", l2_code=f"{l1}-{l2}", l2_name="x",
                expiry_class=expiry_class, seasonality=list(seasonality), size_class="small", regulation=regulation)


def make_product(**over) -> dict:
    p = {
        "product_id": "P1", "category_code": "FD-PR-SNK", "brand": {"name": "x", "tier": "mid"},
        "list_price_krw": 30000, "list_price_basis": "msrp", "condition_grade": "A", "packaging_state": "sealed",
        "spec": {"unit_count": 1, "size_class": "small", "weight_g": None}, "quantity_on_hand": 20,
        "acquired_date": (TODAY - timedelta(days=30)).isoformat(), "expiry_date": (TODAY + timedelta(days=365)).isoformat(),
    }
    for k, v in over.items():
        if k in ("spec", "brand"):
            p[k].update(v)
        else:
            p[k] = v
    return p


def make_price(**over) -> dict:
    m = {"price_id": "M1", "product_id": "P1", "observed_at": (TODAY - timedelta(days=5)).isoformat(),
         "source": {"channel": "open_market", "platform": "x"}, "match_type": "exact", "condition": "new",
         "unit_count": 1, "n_listings": 10, "price_median_krw": 24000, "search_index": None, "sold_count_30d": None}
    m.update(over)
    return m


def make_seller(region="seoul", status="active", channels=()) -> dict:
    return {"seller_id": "S1", "status": status, "business": {"region": region, "sales_channels_used": list(channels)}}


# ---------------------------------------------------------------------------
# 기본 함수
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("x,expected", [(-5, 0), (0, 0), (5, 50), (10, 100), (99, 100)])
def test_lin_boundaries(x, expected):
    assert rf.lin(x, 0, 10) == expected
    assert rf.lin_rev(x, 0, 10) == 100 - expected


def test_clip_default_range():
    assert rf.clip(-3) == 0 and rf.clip(120) == 100 and rf.clip(42.5) == 42.5


@pytest.mark.parametrize("seasonality,month,expected", [
    ((), 7, "neutral"),
    (("summer",), 7, "in_season"),
    (("summer",), 4, "near_season"),     # 4월 → 6월까지 2개월
    (("summer",), 3, "off_season"),      # 3월 → 6월까지 3개월
    (("winter",), 10, "near_season"),    # 10월 → 11월
    (("holiday",), 11, "near_season"),   # 11월 → 12월 (연말)
    (("holiday",), 12, "in_season"),
    (("school",), 12, "near_season"),    # 12월 → 2월 (연도 넘김)
    (("spring_fall",), 12, "off_season"),
])
def test_season_state(seasonality, month, expected):
    assert rf.season_state(list(seasonality), month) == expected


# ---------------------------------------------------------------------------
# §1 시세 선택
# ---------------------------------------------------------------------------
def test_select_price_priority_match_type_then_condition_then_recency():
    p = make_product(condition_grade="A")  # want new
    old_exact = make_price(price_id="old", observed_at=(TODAY - timedelta(days=20)).isoformat(), price_median_krw=20000)
    new_exact = make_price(price_id="new", observed_at=(TODAY - timedelta(days=2)).isoformat(), price_median_krw=21000)
    used_exact = make_price(price_id="used", condition="used", observed_at=TODAY.isoformat(), price_median_krw=15000)
    similar = make_price(price_id="sim", match_type="similar", observed_at=TODAY.isoformat(), price_median_krw=28000)
    rec, nm, flags = rf.select_price(p, [similar, used_exact, old_exact, new_exact], TODAY)
    assert rec["price_id"] == "new" and nm == 21000 and flags == []


def test_select_price_prefers_price_comparison_on_tie():
    p = make_product()
    a = make_price(price_id="a", source={"channel": "open_market", "platform": "x"})
    b = make_price(price_id="b", source={"channel": "price_comparison", "platform": "x"})
    rec, _, _ = rf.select_price(p, [a, b], TODAY)
    assert rec["price_id"] == "b"


def test_select_price_condition_mismatch_applies_factor_both_directions():
    want_new = make_product(condition_grade="S")
    rec, nm, flags = rf.select_price(want_new, [make_price(condition="used", price_median_krw=11000)], TODAY)
    assert flags == ["approx_price"] and nm == pytest.approx(11000 / rf.USED_TO_NEW_FACTOR)
    want_used = make_product(condition_grade="B")
    rec, nm, flags = rf.select_price(want_used, [make_price(condition="new", price_median_krw=20000)], TODAY)
    assert flags == ["approx_price"] and nm == pytest.approx(20000 * rf.USED_TO_NEW_FACTOR)


def test_select_price_unit_count_normalization_and_stale():
    p = make_product(spec={"unit_count": 20})
    m = make_price(unit_count=10, price_median_krw=10000, observed_at=(TODAY - timedelta(days=61)).isoformat())
    rec, nm, flags = rf.select_price(p, [m], TODAY)
    assert nm == 20000 and flags == ["stale_price"]
    m60 = make_price(observed_at=(TODAY - timedelta(days=60)).isoformat())
    assert rf.select_price(p, [m60], TODAY)[2] == []


def test_select_price_ignores_zero_listings_and_other_products():
    p = make_product()
    zero = make_price(n_listings=0)
    other = make_price(product_id="P9")
    assert rf.select_price(p, [zero, other], TODAY) == (None, None, ["missing_price"])


# ---------------------------------------------------------------------------
# §3.1 시장수요
# ---------------------------------------------------------------------------
def test_demand_without_search_index_redistributes_weights():
    # brand mid 75 ×0.5 + 비시즌 60 ×0.3 → /0.8 = 69.375
    r = rf.score_demand(make_product(brand={"tier": "mid"}), make_leaf(), None, TODAY)
    assert r.score == pytest.approx(69.375) and r.detail["season"] == "neutral"


def test_demand_with_search_index_uses_full_weights():
    rec = make_price(search_index=80, n_listings=10)
    r = rf.score_demand(make_product(brand={"tier": "major"}), make_leaf(seasonality=("winter",)), rec, TODAY)
    assert r.score == pytest.approx(0.5 * 100 + 0.3 * 100 + 0.2 * 80)


@pytest.mark.parametrize("n,adj", [(51, -10), (50, 0), (3, 0), (2, -5), (1, -5)])
def test_demand_listings_adjustment(n, adj):
    r = rf.score_demand(make_product(), make_leaf(), make_price(n_listings=n), TODAY)
    assert r.detail["listings_adj"] == adj and r.score == pytest.approx(69.375 + adj)


def test_demand_brand_unknown_is_40_not_missing():
    r = rf.score_demand(make_product(brand={"tier": "unknown"}), make_leaf(), None, TODAY)
    assert r.flags == [] and r.score == pytest.approx((40 * 0.5 + 60 * 0.3) / 0.8)


# ---------------------------------------------------------------------------
# §3.2 판매속도
# ---------------------------------------------------------------------------
def _tx(pid, status="sold", dur=20):
    return {"product_id": pid, "outcome": {"status": status}, "duration_days": dur}


def test_duration_index_uses_only_sold():
    pmap = {"A": {"category_code": "FD-PR-SNK"}, "B": {"category_code": "FD-PR-BEV"}}
    idx = rf.build_duration_index([_tx("A", dur=10), _tx("A", "listed", 99), _tx("B", dur=5), _tx("Z", dur=1)], pmap)
    assert idx == {"FD-PR-SNK": [10], "FD-PR-BEV": [5]}


def test_reference_duration_l3_then_l2_then_prior():
    leaf = make_leaf("FD-PR-SNK", expiry_class="long")
    assert rf.reference_duration(leaf, {}) == (45.0, "prior")
    assert rf.reference_duration(leaf, {"FD-PR-SNK": [10, 20, 30, 40]}) == (45.0, "prior")  # 4건 < 5
    assert rf.reference_duration(leaf, {"FD-PR-SNK": [10, 20, 30, 40, 50]}) == (30.0, "l3")
    assert rf.reference_duration(leaf, {"FD-PR-BEV": [1, 2, 3], "FD-PR-RTE": [4, 5], "LV-CL-DET": [100] * 9}) == (3.0, "l2")


@pytest.mark.parametrize("sc,bonus", [(None, 0), (100, 15), (99, 5), (10, 5), (9, -15), (0, -15)])
def test_velocity_sold30_bonus(sc, bonus):
    r = rf.score_velocity(make_product(), make_leaf(expiry_class="medium"), make_price(sold_count_30d=sc), {})
    base = rf.lin_rev(30, 7, 120)
    assert r.detail["sold30_adj"] == bonus and r.score == pytest.approx(rf.clip(base + bonus))
    assert r.flags == ["prior_velocity"]


def test_velocity_bulk_penalty_and_no_prior_flag_with_history():
    idx = {"FD-PR-SNK": [10] * 5}
    r = rf.score_velocity(make_product(spec={"unit_count": 12}), make_leaf(), None, idx)
    assert r.flags == [] and r.score == pytest.approx(rf.lin_rev(10, 7, 120) - 10)
    r11 = rf.score_velocity(make_product(spec={"unit_count": 11}), make_leaf(), None, idx)
    assert r11.detail["bulk_adj"] == 0


# ---------------------------------------------------------------------------
# §3.3 가격경쟁력
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ratio,expected", [(0.3, 0), (0.4, 0), (0.7, 50), (1.0, 100), (1.19, 100)])
def test_price_linear(ratio, expected):
    p = make_product(list_price_krw=10000)
    r = rf.score_price(p, 10000 * ratio, [])
    assert r.score == pytest.approx(expected) and r.flags == []


def test_price_suspect_and_approx_and_missing():
    p = make_product(list_price_krw=10000)
    assert rf.score_price(p, 12001, []).flags == ["suspect_price"]
    assert rf.score_price(p, 12000, []).flags == []
    assert rf.score_price(p, 10000, ["approx_price"]).score == pytest.approx(90)
    m = rf.score_price(p, None, ["missing_price"])
    assert m.score == 50 and m.flags == ["missing_price"]


# ---------------------------------------------------------------------------
# §3.4 상품잔존성
# ---------------------------------------------------------------------------
def test_residual_all_max_is_100():
    p = make_product(condition_grade="S", packaging_state="sealed", acquired_date=(TODAY - timedelta(days=90)).isoformat(),
                     expiry_date=(TODAY + timedelta(days=365)).isoformat())
    assert rf.score_residual(p, make_leaf(expiry_class="long"), TODAY).score == pytest.approx(100)


def test_residual_expiry_none_class_fixed_100_component():
    p = make_product(expiry_date=None, condition_grade="C", packaging_state="no_box", acquired_date=(TODAY - timedelta(days=720)).isoformat())
    r = rf.score_residual(p, make_leaf("FA-AP-MEN", expiry_class="none"), TODAY)
    assert r.score == pytest.approx(0.4 * 100 + 0.3 * 30 + 0.15 * 50 + 0.15 * 0)


def test_residual_short_class_uses_180_day_ceiling():
    p = make_product(expiry_date=(TODAY + timedelta(days=105)).isoformat())
    short = rf.score_residual(p, make_leaf(expiry_class="short"), TODAY)
    medium = rf.score_residual(p, make_leaf(expiry_class="medium"), TODAY)
    # 잔여 105일: short → lin(105,30,180)=50, medium → lin(105,30,365)=22.39
    assert short.detail["remaining_days"] == 105
    assert short.score - medium.score == pytest.approx(0.4 * (50 - 100 * 75 / 335))


def test_residual_holding_boundaries():
    for days, h in [(90, 100), (405, 50), (720, 0), (1000, 0)]:
        p = make_product(acquired_date=(TODAY - timedelta(days=days)).isoformat(), expiry_date=None, condition_grade="S")
        r = rf.score_residual(p, make_leaf(expiry_class="none"), TODAY)
        assert r.score == pytest.approx(40 + 30 + 15 + 0.15 * h)


# ---------------------------------------------------------------------------
# §3.5 채널적합성
# ---------------------------------------------------------------------------
def test_channel_base_table_by_grade():
    for gi, g in enumerate("SABC"):
        scores = rf.channel_scores(make_product(condition_grade=g), make_leaf(), None)
        assert scores == {c: rf.CHANNEL_BASE[c][gi] for c in rf.CHANNELS}


def test_channel_permit_excludes_three_channels():
    r = rf.score_channel(make_product(condition_grade="B"), make_leaf("FD-HF-SUP", regulation="permit"), None)
    assert set(r.detail["scores"]) == set(rf.CHANNELS) - rf.PERMIT_EXCLUDED
    assert r.detail["recommended"] == "offline_store" and r.score == 65  # B: secondhand_app 85 제외 후 offline 65


def test_channel_quantity_and_size_and_seller_adjustments():
    p = make_product(condition_grade="S", quantity_on_hand=100, spec={"size_class": "large"})
    s = rf.channel_scores(p, make_leaf(), make_seller(channels=("open_market",)))
    assert s["b2b_wholesale"] == 100  # 85 + 15 + 5 = 105 → 클립
    assert s["secondhand_app"] == 50 - 20 - 15
    assert s["offline_store"] == 70 + 10
    assert s["live_commerce"] == 75 - 10
    assert s["open_market"] == 90 - 20
    few = rf.channel_scores(make_product(condition_grade="S", quantity_on_hand=5), make_leaf(), None)
    assert few["b2b_wholesale"] == 85 - 30
    assert rf.channel_scores(make_product(condition_grade="S", quantity_on_hand=6), make_leaf(), None)["b2b_wholesale"] == 85


def test_channel_recommended_tie_breaks_by_channel_order():
    r = rf.score_channel(make_product(condition_grade="A"), make_leaf(), None)
    # A 등급: open_market = offline_store = secondhand_app = 70 → CHANNELS 순서상 open_market
    assert r.score == 70 and r.detail["recommended"] == "open_market"


# ---------------------------------------------------------------------------
# §3.6 물류효율성
# ---------------------------------------------------------------------------
def test_logistics_value_density_boundaries():
    p_low = make_product(list_price_krw=3000, spec={"size_class": "small"})   # v=1 → density 0
    p_high = make_product(list_price_krw=60000, spec={"size_class": "small"})  # v=20 → 100
    lo = rf.score_logistics(p_low, make_seller("seoul"))
    hi = rf.score_logistics(p_high, make_seller("seoul"))
    assert lo.score == pytest.approx(0.25 * 100 + 0.30 * 100)
    assert hi.score == pytest.approx(100)


def test_logistics_overweight_downgrades_size():
    p = make_product(spec={"size_class": "small", "weight_g": 2001})
    assert rf.effective_size_class(p) == "medium"
    assert rf.effective_size_class(make_product(spec={"size_class": "small", "weight_g": 2000})) == "small"
    assert rf.effective_size_class(make_product(spec={"size_class": "large", "weight_g": 99999})) == "large"


@pytest.mark.parametrize("region,score,flag", [("seoul", 100, False), ("chungcheong", 80, False), ("jeju", 20, False), ("unknown", 70, True), (None, 70, True)])
def test_logistics_region(region, score, flag):
    p = make_product(list_price_krw=60000)
    seller = make_seller(region) if region else None
    r = rf.score_logistics(p, seller)
    assert r.score == pytest.approx(0.45 * 100 + 0.25 * 100 + 0.30 * score)
    assert ("missing_region" in r.flags) is flag


# ---------------------------------------------------------------------------
# §2·§4 게이트·집계
# ---------------------------------------------------------------------------
def test_gate_expired_boundary_30_days():
    assert rf.gate_for(make_product(expiry_date=(TODAY + timedelta(days=30)).isoformat()), None, TODAY) == "expired"
    assert rf.gate_for(make_product(expiry_date=(TODAY + timedelta(days=31)).isoformat()), None, TODAY) is None
    assert rf.gate_for(make_product(expiry_date=None), None, TODAY) is None


def test_gate_seller_blocked_and_priority():
    assert rf.gate_for(make_product(), make_seller(status="blocked"), TODAY) == "seller_blocked"
    assert rf.gate_for(make_product(), make_seller(status="dormant"), TODAY) is None
    both = make_product(expiry_date=TODAY.isoformat())
    assert rf.gate_for(both, make_seller(status="blocked"), TODAY) == "expired"


@pytest.mark.parametrize("total,grade", [(75, "A"), (74.9, "B"), (60, "B"), (59.9, "C"), (45, "C"), (44.9, "D"), (0, "D")])
def test_grade_bounds(total, grade):
    assert rf.grade_of(total) == grade


@pytest.mark.parametrize("total,ratio", [(0, 0.10), (50, 0.30), (80, 0.42), (100, 0.50)])
def test_buy_ratio(total, ratio):
    assert rf.buy_ratio_of(total) == pytest.approx(ratio)


def test_confidence_counts_items_not_flags():
    assert rf.confidence_of([]) == 1.0
    assert rf.confidence_of(["missing_price", "stale_price"]) == pytest.approx(5 / 6)   # 둘 다 price 항목
    assert rf.confidence_of(["missing_price", "prior_velocity", "missing_region"]) == pytest.approx(0.5)
    assert rf.confidence_of(["approx_price", "suspect_price"]) == 1.0                  # 결측 아님


def test_score_product_full_record_shape_and_buy_price():
    p = make_product(list_price_krw=30000)
    rec = rf.score_product(p, make_leaf(), [make_price(price_median_krw=24000)], make_seller(), {}, TODAY)
    assert rec["gate"] is None and rec["manual_review"] is False
    assert set(rec["items"]) == set(rf.ITEMS) and all(isinstance(v, int) for v in rec["items"].values())
    # 총점은 반올림 전 항목 점수로 계산하므로 정수 항목의 가중합과 1점 이내에서만 일치
    assert abs(rec["total"] - sum(rf.WEIGHTS[k] * rec["items"][k] for k in rf.ITEMS)) < 1
    assert rec["grade"] == rf.grade_of(rec["total"])
    assert rec["suggested_buy_price_krw"] == int(round(30000 * rec["suggested_buy_ratio"], -2))
    assert rec["suggested_buy_price_krw"] % 100 == 0
    assert rec["flags"] == ["prior_velocity"] and rec["confidence"] == pytest.approx(0.83)
    assert rec["evaluated_on"] == "2026-11-11" and rec["details"]["seller_id"] == "S1"
    json.dumps(rec)  # 직렬화 가능


def test_gate_suppresses_buy_price_but_keeps_scores():
    p = make_product()
    rec = rf.score_product(p, make_leaf(), [], make_seller(status="blocked"), {}, TODAY)
    assert rec["gate"] == "seller_blocked" and rec["manual_review"] is True
    assert rec["suggested_buy_price_krw"] is None and rec["total"] > 0


def test_gate_no_channel_when_all_excluded(monkeypatch):
    monkeypatch.setattr(rf, "PERMIT_EXCLUDED", frozenset(rf.CHANNELS))
    rec = rf.score_product(make_product(), make_leaf(regulation="permit"), [], None, {}, TODAY)
    assert rec["gate"] == "no_channel" and rec["items"]["channel"] == 0 and rec["recommended_channel"] is None
    assert "gate_no_channel" not in rec["flags"]


def test_g4_no_price_basis_manual_review():
    p = make_product(list_price_basis="unknown")
    rec = rf.score_product(p, make_leaf(), [], None, {}, TODAY)
    assert rec["gate"] is None and rec["manual_review"] is True and "no_price_basis" in rec["flags"]
    with_price = rf.score_product(p, make_leaf(), [make_price()], None, {}, TODAY)
    assert with_price["manual_review"] is False


def test_low_confidence_triggers_manual_review():
    # 시세 없음(price) + 사전값(velocity) + 판매자 없음(region) → confidence 0.5 → manual 아님; 거기에 stale 은 같은 항목이라 변화 없음
    rec = rf.score_product(make_product(), make_leaf(), [], None, {}, TODAY)
    assert rec["confidence"] == 0.5 and rec["manual_review"] is False
    # 하나 더 빠지면 < 0.5 — 결측 플래그 매핑에 가짜 항목을 추가해 흉내 내지 않고, 임계값 상수를 올려 동작만 확인
    rf_threshold = rf.CONFIDENCE_MANUAL_BELOW
    try:
        rf.CONFIDENCE_MANUAL_BELOW = 0.6
        assert rf.score_product(make_product(), make_leaf(), [], None, {}, TODAY)["manual_review"] is True
    finally:
        rf.CONFIDENCE_MANUAL_BELOW = rf_threshold


def test_seller_for_product_via_transactions():
    sellers = {"S1": make_seller()}
    txs = [{"product_id": "P2", "seller_id": "S1"}, {"product_id": "P1", "seller_id": "S9"}, {"product_id": "P1", "seller_id": "S1"}]
    assert rf.seller_for_product("P1", txs, sellers)["seller_id"] == "S1"
    assert rf.seller_for_product("P3", txs, sellers) is None


# ---------------------------------------------------------------------------
# 회귀: 11-11 프로토타입 로그와 15건 전부 일치
# ---------------------------------------------------------------------------
def _parse_proto_table(text: str) -> dict[str, dict]:
    rows = {}
    for line in text.splitlines():
        m = re.match(r"^(P\d{8})\s+(\S+)\s+([SABC])\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s+([ABCD])\s+([\d.]+)\s+(\S+)\s+(\d+)\s+(\S+)\s+([\d.]+)\s+(\d+)\s*(.*)$", line)
        if not m:
            continue
        g = m.groups()
        rows[g[0]] = {"items": [int(x) for x in g[3:9]], "total": float(g[9]), "grade": g[10], "confidence": float(g[11]),
                      "channel": g[14], "buy_ratio": float(g[15]), "buy": int(g[16]), "tail": g[17]}
    return rows


def test_regression_matches_prototype_log():
    proto = _parse_proto_table(PROTO_LOG.read_text(encoding="utf-8"))
    assert len(proto) == 15
    leaves = {l.code: l for l in load_categories()}
    products, prices, txs, sellers = rf.load_examples()
    rows = rf.score_all(products, leaves, prices, txs, sellers, TODAY)
    assert len(rows) == 15
    for r in rows:
        e = proto[r["product_id"]]
        assert [r["items"][k] for k in rf.ITEMS] == e["items"], r["product_id"]
        assert r["total"] == e["total"] and r["grade"] == e["grade"] and r["confidence"] == e["confidence"]
        assert (r["recommended_channel"] or "-") == e["channel"]
        assert r["suggested_buy_ratio"] == e["buy_ratio"] and (r["suggested_buy_price_krw"] or 0) == e["buy"]
        assert r["flags"] == [f for f in e["tail"].split()[0].split(",") if f] if e["tail"] else r["flags"] == []
        assert (r["gate"] is not None) == ("GATE=" in e["tail"])
    # 표 형식 자체도 프로토타입 로그와 같아야 한다 (요약 수치 비교의 근거)
    table = rf.format_table(rows, products).splitlines()[1:]
    proto_lines = [l for l in PROTO_LOG.read_text(encoding="utf-8").splitlines() if re.match(r"^P\d{8}", l)]
    assert table == proto_lines


def test_cli_runs_and_writes_jsonl(tmp_path, capsys):
    out = tmp_path / "scores.jsonl"
    assert rf.main(["--today", "2026-11-11", "--out", str(out)]) == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 15 and json.loads(lines[0])["product_id"] == "P00000001"
    assert "총점:" in capsys.readouterr().out
