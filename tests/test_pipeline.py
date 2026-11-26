"""src/collect/pipeline.py 테스트 (D018). 설정 검증·약관 게이트·예산·append-only 저장소·수기 폼·트렌드 변환."""
import copy
import json
from datetime import date

import pytest

from src.collect import pipeline as P
from src.collect import trend_naver as TN


@pytest.fixture(scope="module")
def cfg():
    return P.load_config()


@pytest.fixture(scope="module")
def idx():
    return P.source_index()


def test_real_config_validates(cfg, idx):
    assert P.validate_config(cfg, idx) == []
    assert len(cfg["jobs"]) >= 5


def test_prohibited_source_cannot_be_job(cfg, idx):
    bad = copy.deepcopy(cfg)
    bad["jobs"].append({"id": "JX", "table": "market_price", "platform": "coupang_web", "cadence": "daily",
                        "calls_per_run": 1, "limit": None, "approved": True, "approved_on": "2026-11-26", "raw_retain": False})
    errs = P.validate_config(bad, idx)
    assert any("prohibited" in e for e in errs)


def test_gate_conditional_requires_approval(cfg, idx):
    j = next(j for j in cfg["jobs"] if j["platform"] == "naver_apihub_shop")
    assert P.gate_state(j, idx) == "planned"
    approved = dict(j, approved=True, approved_on="2026-12-01", prerequisite=None)
    assert P.gate_state(approved, idx) == "runnable"
    # 선행 조건이 남아 있으면 승인돼도 planned
    assert P.gate_state(dict(approved, prerequisite="키 발급"), idx) == "planned"
    assert P.gate_state({"platform": "enuri", "approved": True}, idx) == "never"


def test_monthly_budget_within_limits(cfg, idx):
    rows = P.monthly_budget(cfg, idx)
    for r in rows:
        if r["share_of_limit"] is not None:
            assert r["share_of_limit"] < 1.0, r
    naver = next(r for r in rows if r["platform"] == "naver_apihub_shop")
    assert naver["calls_per_month"] == pytest.approx(20000 * 52 / 12, rel=1e-3)


def test_store_dedup_new_dup_conflict(tmp_path, cfg):
    st = P.Store(tmp_path, cfg["dedup_keys"])
    base = {"schema_version": "0.1", "price_id": "M00000001", "product_id": "P00000001", "observed_at": "2026-11-26",
            "source": {"channel": "offline_store", "platform": "price_go_kr", "collected_by": "api", "query": "x"},
            "match_type": "similar", "condition": "new", "unit_count": 1, "n_listings": 3,
            "price_min_krw": 100, "price_median_krw": 120, "price_max_krw": 150, "shipping_included": True,
            "sold_count_30d": None, "search_index": None, "notes": None, "created_at": "2026-11-26T00:00:00+00:00"}
    m1 = st.ingest("market_price", [base], "J")
    assert (m1["n_new"], m1["n_dup"], m1["n_conflict"]) == (1, 0, 0)
    # 같은 키·같은 내용(price_id·created_at 만 다름) → dup
    same = dict(base, price_id="M00000009", created_at="2026-11-27T00:00:00+00:00")
    m2 = st.ingest("market_price", [same], "J")
    assert (m2["n_new"], m2["n_dup"], m2["n_conflict"]) == (0, 1, 0)
    # 같은 키·다른 내용 → conflict → quarantine, 본문은 안 바뀜
    diff = dict(base, price_median_krw=130)
    m3 = st.ingest("market_price", [diff], "J")
    assert (m3["n_new"], m3["n_dup"], m3["n_conflict"]) == (0, 0, 1)
    part = tmp_path / "market_price" / "price_go_kr" / "2026-11.jsonl"
    rows = [json.loads(l) for l in open(part, encoding="utf-8")]
    assert len(rows) == 1 and rows[0]["price_median_krw"] == 120 and rows[0]["run_id"].startswith("J-")
    q = [json.loads(l) for l in open(tmp_path / "quarantine" / "market_price.jsonl", encoding="utf-8")]
    assert len(q) == 1 and q[0]["reason"] == "same_key_different_content"
    # 다른 관측일 → new, 다른 파티션
    later = dict(base, observed_at="2026-12-01")
    m4 = st.ingest("market_price", [later], "J")
    assert m4["n_new"] == 1 and (tmp_path / "market_price" / "price_go_kr" / "2026-12.jsonl").exists()
    assert len(st.manifest()) == 4


def test_store_dedup_within_batch(tmp_path, cfg):
    st = P.Store(tmp_path, cfg["dedup_keys"])
    base = {"product_id": "P00000001", "observed_at": "2026-11-26", "source": {"channel": "offline_store", "platform": "p"},
            "condition": "new", "price_median_krw": 1}
    m = st.ingest("market_price", [base, dict(base), dict(base, price_median_krw=2)], "J")
    assert (m["n_new"], m["n_dup"], m["n_conflict"]) == (1, 1, 1)


def test_form_to_records_and_validation():
    rows = [{"observed_at": "2026-11-26", "product_id": "P00000001", "site": "네이버쇼핑", "query": "감자칩",
             "condition": "new", "match_type": "similar", "unit_count": "16", "n_listings": "12",
             "price_min_krw": "9,800", "price_median_krw": "12900", "price_max_krw": "15900",
             "shipping_included": "포함", "notes": ""},
            {"observed_at": "2026-11-26", "product_id": "P00000001", "site": "이상한곳", "query": "감자칩",
             "condition": "used", "match_type": "similar", "unit_count": "16", "n_listings": "0",
             "price_min_krw": "", "price_median_krw": "", "price_max_krw": "", "shipping_included": "", "notes": "품절"}]
    recs = P.form_to_records(rows, created_at="2026-11-26T00:00:00+00:00")
    assert recs[0]["source"] == {"channel": "price_comparison", "platform": "naver_shopping_web", "collected_by": "manual", "query": "감자칩"}
    assert recs[0]["price_min_krw"] == 9800 and recs[0]["shipping_included"] is True
    assert recs[1]["source"]["platform"] == "other" and recs[1]["notes"].startswith("site=이상한곳")
    assert recs[1]["price_median_krw"] is None
    errs, warns = P.validate_form_records(recs, products=None, today=date(2026, 11, 26))
    assert errs == []
    errs2, _ = P.validate_form_records(P.form_to_records([dict(rows[0], observed_at="2027-01-01")]), None, date(2026, 11, 26))
    assert errs2 and "이후" in errs2[0]


def test_form_template_parses():
    rows = P.read_form()
    assert set(rows[0]) >= {"observed_at", "product_id", "site", "condition", "n_listings", "price_median_krw", "shipping_included"}
    recs = P.form_to_records(rows, created_at="2026-11-26T00:00:00+00:00")
    assert P.validate_form_records(recs, None, date(2026, 11, 26))[0] == []


def test_trend_records_schema_and_per_group_rule():
    rows = TN.parse_response(TN.DOC_EXAMPLE_RESPONSE)
    rows = [TN.TrendRow("FD-PR-SNK" if r.group == "영어" else r.group, r.period, r.ratio) for r in rows]
    norm = TN.detect_normalization(rows)
    rel = TN.normalize_by_anchor(rows, "한글")
    sidx = TN.to_search_index(rel)
    recs = P.trend_to_records(rows, "한글", "생수", norm, rel, sidx, "naver_datalab_search", "month", "R1")
    assert len(recs) == 4 and P.validate_trend_records(recs) == []
    assert recs[0]["search_index"] == 19 and recs[0]["rel_to_anchor"] == pytest.approx(26.67976 / 70.15342, abs=1e-5)
    pg = P.trend_to_records(rows, "한글", "생수", "per_group", rel, sidx, "naver_datalab_search", "month", "R1")
    assert all(r["search_index"] is None and r["rel_to_anchor"] is None for r in pg)
    bad = copy.deepcopy(recs)
    bad[0]["rel_to_anchor"] = 9.9
    assert any("rel_to_anchor" in e for e in P.validate_trend_records(bad))
    bad2 = copy.deepcopy(recs)
    bad2[1]["period"] = bad2[0]["period"]
    assert any("dedup" in e for e in P.validate_trend_records(bad2))
