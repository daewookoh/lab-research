"""src/collect/price.py 테스트 (D019). 참가격 CSV 파서·품목 매칭·집계·검증·적재. 입력은 전부 합성(price_synth)."""
from datetime import date
from pathlib import Path

import pytest

from src.collect import pipeline as P
from src.collect import price as PR
from src.collect import price_synth as PSY
from src.schema import market_price as MP

TODAY = date(2026, 11, 27)
DATES = [date(2026, 11, 5), date(2026, 11, 19)]


@pytest.fixture(scope="module")
def items():
    return PR.load_items()


@pytest.fixture(scope="module")
def products():
    return MP.load_products()


@pytest.fixture(scope="module")
def synth_csv(tmp_path_factory):
    p = tmp_path_factory.mktemp("inbox") / "price_go_kr_synth.csv"
    PSY.write_csv(PSY.make_rows(1, DATES), p, encoding="cp949")
    return p


def test_items_reference_known_products(items, products):
    ids = {it["item_id"] for it in items["items"]}
    assert len(ids) == len(items["items"]) == 10
    assert all(it["product_id"] in products for it in items["items"])


def test_column_aliases_and_missing_column():
    m = PR.resolve_columns(["GOOD_NAME", "조사일", "판매가격", "SORE_NAME", "제조사"])
    assert m["good_name"] == "GOOD_NAME" and m["store_name"] == "SORE_NAME"
    m2 = PR.resolve_columns(["상품명", "조사일자", "판매가격(원)", "업체명"])  # API 식 표기 변형
    assert m2["survey_date"] == "조사일자" and m2["price"] == "판매가격(원)"
    with pytest.raises(PR.ParseError):
        PR.resolve_columns(["상품명", "판매가격"])  # 조사일·업소 없음


def test_read_csv_detects_cp949_and_utf8(tmp_path):
    rows = PSY.make_rows(2, DATES[:1], n_bad=0)[:5]
    for enc in ("cp949", "utf-8-sig"):
        p = tmp_path / f"f_{enc}.csv"
        PSY.write_csv(rows, p, encoding=enc)
        got, colmap, used = PR.read_csv(p)
        assert len(got) == 5 and got[0]["good_name"] == rows[0]["GOOD_NAME"]
        assert used == enc


def test_parsers():
    assert PR.parse_date("2026-11-05") == PR.parse_date("20261105") == PR.parse_date("2026.11.05") == date(2026, 11, 5)
    assert PR.parse_date("2026-13-40") is None
    assert PR.parse_price("1,500") == 1500 and PR.parse_price("22,000원") == 22000 and PR.parse_price("1500.0") == 1500
    assert PR.parse_price("") is None and PR.parse_price("0") is None and PR.parse_price("abc") is None
    assert PR.parse_content("액체세탁세제 리필 3L") == (3000.0, "ml")
    assert PR.parse_content("감자칩 오리지널 60g") == (60.0, "g")
    assert PR.parse_content("유리 밀폐용기 사각") is None
    assert PR.parse_yn("Y") and PR.parse_yn("y") and not PR.parse_yn("N") and not PR.parse_yn("")


def test_match_item_rules(items):
    tol = items["content_tolerance"]
    def row(name):
        return {"good_name": name, "content": PR.parse_content(name)}
    assert PR.match_item(row("감자칩 오리지널 60g"), items["items"], tol)["item_id"] == "PG01"
    assert PR.match_item(row("감자칩 저염 110g"), items["items"], tol) is None            # exclude + 용량 밖
    assert PR.match_item(row("감자칩 바베큐 70g"), items["items"], tol)["item_id"] == "PG01"  # ±25% 안
    assert PR.match_item(row("스틱커피 믹스 100입"), items["items"], tol) is None           # exclude
    assert PR.match_item(row("유아 물티슈 70매"), items["items"], tol)["item_id"] == "PG08"
    assert PR.match_item(row("아기 물티슈 100매"), items["items"], tol)["item_id"] == "PG09"
    assert PR.match_item(row("물티슈 휴대용 20매"), items["items"], tol) is None
    assert PR.match_item(row("유리 밀폐용기 사각 1.0L"), items["items"], tol)["item_id"] == "PG06"  # content 없음 → 용량 무시
    assert PR.match_item(row("감자칩"), items["items"], tol) is None                      # require_content 인데 용량 없음


def test_aggregate_excludes_sale_and_scales_units(items, products):
    it = next(i for i in items["items"] if i["item_id"] == "PG08")  # 물티슈 1팩 → 우리 상품 10팩
    d = date(2026, 11, 5)
    base = {"survey_date": d, "store_name": "s", "sale": False, "plus_one": False}
    matched = [
        ({**base, "price": 1000, "store_name": "a"}, it),
        ({**base, "price": 1200, "store_name": "b"}, it),
        ({**base, "price": 1400, "store_name": "c"}, it),
        ({**base, "price": 500, "store_name": "d", "sale": True}, it),
        ({**base, "price": 2000, "store_name": "e", "plus_one": True}, it),
    ]
    recs, st = PR.aggregate(matched, items, products, created_at="2026-11-27T00:00:00+00:00")
    assert len(recs) == 1 and st["n_sale_excluded"] == 1 and st["n_plus_one_excluded"] == 1
    r = recs[0]
    assert r["n_listings"] == 3 and r["unit_count"] == 10
    assert (r["price_min_krw"], r["price_median_krw"], r["price_max_krw"]) == (10000, 12000, 14000)
    assert r["source"]["channel"] == "offline_store" and r["condition"] == "new" and r["shipping_included"] is True
    assert "세일 1" in r["notes"]
    # products 없이 돌리면 환산 없음
    recs2, _ = PR.aggregate(matched, items, None)
    assert recs2[0]["unit_count"] == 1 and recs2[0]["price_median_krw"] == 1200


def test_aggregate_merges_items_sharing_product(items, products):
    pg08 = next(i for i in items["items"] if i["item_id"] == "PG08")
    pg09 = next(i for i in items["items"] if i["item_id"] == "PG09")
    d = date(2026, 11, 5)
    base = {"survey_date": d, "sale": False, "plus_one": False}
    matched = [({**base, "price": 1000, "store_name": "a"}, pg08), ({**base, "price": 2000, "store_name": "b"}, pg09)]
    recs, _ = PR.aggregate(matched, items, products)
    assert len(recs) == 1 and recs[0]["n_listings"] == 2 and "PG08,PG09" in recs[0]["notes"]


def test_run_end_to_end_validates_and_counts(synth_csv, items, products):
    s = PR.run(synth_csv, items, products, TODAY, collected_by="synthetic")
    assert s["encoding"] == "cp949"
    assert s["dropped"] == {"bad_price": 2, "bad_date": 1, "no_name": 1}
    assert s["n_records"] == 9 * 2 and s["n_errors"] == 0            # 상품 9 × 조사일 2
    assert set(s["per_item"]) == {f"PG{i:02d}" for i in range(1, 11)}
    assert all(r["source"]["collected_by"] == "synthetic" and r["notes"].startswith("합성") for r in s["records"])
    assert s["n_unmatched_names"] == 13                              # 대조군 13개 상품명은 전부 미매칭


def test_run_ingests_then_dedups(synth_csv, items, products, tmp_path):
    store = P.Store(tmp_path / "store", P.load_config()["dedup_keys"])
    s1 = PR.run(synth_csv, items, products, TODAY, store=store, collected_by="synthetic", created_at="2026-11-27T00:00:00+00:00")
    assert s1["ingest"]["n_new"] == 18 and s1["ingest"]["n_conflict"] == 0
    s2 = PR.run(synth_csv, items, products, TODAY, store=store, collected_by="synthetic", created_at="2026-11-28T00:00:00+00:00")
    assert s2["ingest"]["n_dup"] == 18 and s2["ingest"]["n_new"] == 0  # created_at 만 다르면 dup
    assert (tmp_path / "store" / "market_price" / "price_go_kr" / "2026-11.jsonl").exists()


def test_run_refuses_to_ingest_on_validation_error(synth_csv, items, products, tmp_path):
    store = P.Store(tmp_path / "store", P.load_config()["dedup_keys"])
    s = PR.run(synth_csv, items, products, date(2026, 11, 10), store=store, collected_by="synthetic")  # 기준일이 조사일보다 앞
    assert s["n_errors"] > 0 and s["ingest"] is None
    assert not (tmp_path / "store" / "market_price").exists()


def test_synth_generator_is_deterministic():
    a = PSY.make_rows(7, DATES)
    b = PSY.make_rows(7, DATES)
    assert a == b and len(a) > 1000
