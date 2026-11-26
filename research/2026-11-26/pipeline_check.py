"""D018 파이프라인 설계 점검 — 설정 검증·월간 예산·append-only 저장·중복 처리·수기 폼·트렌드 변환을 실제로 돌려 본다.

실행: python research/2026-11-26/pipeline_check.py > research/2026-11-26/pipeline_check.txt
데이터: 합성 v2 market_price 14,945건(collected_by=synthetic)과 합성 상품 10,000건. 실제 시세 아님.
저장소는 임시 폴더(tempfile)에 만들고 끝나면 지운다 — 저장소 용량 정책(11-18 질문 3) 미답이라 Git 에는 아무것도 남기지 않는다.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import random
import sys
import tempfile
import time
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.collect import pipeline as P  # noqa: E402
from src.collect import trend_naver as TN  # noqa: E402
from src.schema import market_price as MP  # noqa: E402

TODAY = date(2026, 11, 26)
random.seed(20261126)


def read_gz_jsonl(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip() and not l.startswith("#")]


def section(t):
    print(f"\n## {t}")


# 1. 설정 검증 + 예산 ---------------------------------------------------------------
section("1. pipeline.yaml 검증·월간 호출 예산")
cfg = P.load_config()
idx = P.source_index()
errs = P.validate_config(cfg, idx)
print(f"job {len(cfg['jobs'])}개, 검증 오류 {len(errs)}건", errs)
budget = P.monthly_budget(cfg, idx)
print(P.format_budget(budget))
gates = Counter(r["gate"] for r in budget)
print("게이트:", dict(gates))
total_calls = sum(r["calls_per_month"] for r in budget)
runnable_calls = sum(r["calls_per_month"] for r in budget if r["gate"] == "runnable")
print(f"월 호출 합계 {total_calls:,.0f} (지금 실행 가능한 job 만 {runnable_calls:,.0f})")

# 네이버 쇼핑 실행 시간: 50 RPS 한도 vs 7.3⑫ 과부하 금지 → 1 RPS 가정
naver = next(r for r in budget if r["id"] == "J05_naver_apihub_shop")
j05 = next(j for j in cfg["jobs"] if j["id"] == "J05_naver_apihub_shop")
print(f"J05 1회 실행 {j05['calls_per_run']:,} 호출: 50 RPS 면 {j05['calls_per_run']/50/60:.1f}분, 1 RPS 면 {j05['calls_per_run']/3600:.1f}시간")

# 잘못된 설정은 잡히는지
bad = json.loads(json.dumps(cfg))
bad["jobs"].append({"id": "JX", "table": "market_price", "platform": "coupang_web", "cadence": "daily",
                    "calls_per_run": 1, "limit": None, "approved": True, "approved_on": "2026-11-26", "raw_retain": False})
bad["jobs"].append({"id": "JY", "table": "trend_index", "platform": "price_go_kr", "cadence": "monthly",
                    "calls_per_run": 1, "limit": None, "approved": True, "approved_on": "2026-11-26", "raw_retain": False})
berrs = P.validate_config(bad, idx)
print(f"의도적 오설정 2건(prohibited 소스 job, 테이블 불일치) → 오류 {len(berrs)}건:")
for e in berrs:
    print("  -", e)

# 2. 저장소: 합성 v2 시세 14,945건 적재 → 재적재 → 변경분 ---------------------------------
section("2. append-only 저장소·중복 처리 (합성 v2 market_price 14,945건)")
recs = read_gz_jsonl(ROOT / "data/synth/v2/market_price.jsonl.gz")
products = {r["product_id"]: r for r in read_gz_jsonl(ROOT / "data/synth/v2/product.jsonl.gz")}
print(f"레코드 {len(recs):,} / 상품 {len(products):,} / 플랫폼 {sorted(Counter(r['source']['platform'] for r in recs))}")
with tempfile.TemporaryDirectory() as td:
    st = P.Store(Path(td), cfg["dedup_keys"])
    t0 = time.perf_counter()
    m1 = st.ingest("market_price", recs, job_id="SYNTH_V2")
    t1 = time.perf_counter()
    m2 = st.ingest("market_price", recs, job_id="SYNTH_V2")       # 같은 배치 재적재
    t2 = time.perf_counter()
    changed = []
    for r in random.sample(recs, 50):                                # 50건을 내용만 바꿔 재적재 → conflict
        r2 = json.loads(json.dumps(r)); r2["price_median_krw"] = r2["price_median_krw"] + 1 if r2["price_median_krw"] else None
        changed.append(r2)
    fresh = []
    for r in random.sample(recs, 100):                               # 100건을 관측일만 바꿔 → new (시계열 추가)
        r2 = json.loads(json.dumps(r)); r2["observed_at"] = "2026-11-26"; r2["price_id"] = "M9" + r2["price_id"][2:]
        fresh.append(r2)
    m3 = st.ingest("market_price", changed + fresh + recs[:200], job_id="SYNTH_V2_DELTA")
    t3 = time.perf_counter()
    print(f"1차 적재: {m1['n_in']:,}건 → new {m1['n_new']:,} dup {m1['n_dup']} conflict {m1['n_conflict']}  ({t1-t0:.2f}s)")
    print(f"2차 재적재(동일): new {m2['n_new']} dup {m2['n_dup']:,} conflict {m2['n_conflict']}  ({t2-t1:.2f}s)")
    print(f"3차 변경분(내용변경 50 + 새 관측일 100 + 동일 200): new {m3['n_new']} dup {m3['n_dup']} conflict {m3['n_conflict']}  ({t3-t2:.2f}s)")
    parts = sorted(Path(td).glob("market_price/*/*.jsonl"))
    total_bytes = sum(p.stat().st_size for p in parts)
    q = Path(td) / "quarantine" / "market_price.jsonl"
    qn = sum(1 for _ in open(q, encoding="utf-8")) if q.exists() else 0
    print(f"파티션 {len(parts)}개 (platform × 월), 본문 {total_bytes/1e6:.2f}MB, 레코드당 {total_bytes/(m1['n_new']+m3['n_new']):.0f}B, quarantine {qn}건, manifest {len(st.manifest())}행")
    # 파티션 분포
    by_plat = Counter(p.parent.name for p in parts)
    print("파티션(플랫폼별 월 수):", dict(by_plat))
    # 실제 운영 규모 추정: 상품 10,000 × 신·중고 2 × 주 1회 (네이버 허용 시) + 참가격 월 1회 16 소분류
    per_rec = total_bytes / (m1["n_new"] + m3["n_new"])
    weekly_rows = 10000 * 2
    yearly_rows = weekly_rows * 52
    print(f"운영 추정(네이버 허용 가정): 주 {weekly_rows:,}행 → 연 {yearly_rows:,}행 ≈ {yearly_rows*per_rec/1e9:.2f}GB(비압축) / gzip 약 1/8 가정 {yearly_rows*per_rec/8/1e6:.0f}MB — Git 에 둘 크기가 아님(11-18 질문 3)")
    # existing() 색인 메모리·시간: 전체 스캔 방식의 한계 측정
    t4 = time.perf_counter(); have = st.existing("market_price"); t5 = time.perf_counter()
    print(f"existing() 전체 스캔: {len(have):,}키 {t5-t4:.2f}s → 연 {yearly_rows:,}행이면 약 {(t5-t4)*yearly_rows/len(have):.0f}s/실행 (파티션 색인 필요 시점: 설계 §4)")

# 3. 수기 입력 폼 → market_price 레코드 → 3단계 검증 ---------------------------------------
section("3. 수기 입력 폼(manual_price_form.csv) → market_price 변환·검증")
tmpl = P.read_form()
print("폼 열:", list(tmpl[0].keys()))
# 합성 상품 20개로 가짜 폼 작성(값은 합성 시세 레코드에서 가져와 '담당자가 본 값' 처럼 적음)
rows = []
sites = ["네이버쇼핑", "쿠팡", "번개장터", "중고나라", "다나와", "알 수 없는 사이트"]
for r in random.sample(recs, 20):
    site = random.choice(sites)
    cond = "used" if site in ("번개장터", "중고나라") else "new"
    rows.append({"observed_at": "2026-11-26", "product_id": r["product_id"], "site": site,
                 "query": products[r["product_id"]]["name"], "condition": cond, "match_type": "similar",
                 "unit_count": str(r["unit_count"]), "n_listings": str(r["n_listings"]),
                 "price_min_krw": f"{r['price_min_krw']:,}" if r["price_min_krw"] else "",
                 "price_median_krw": str(r["price_median_krw"] or ""), "price_max_krw": str(r["price_max_krw"] or ""),
                 "shipping_included": random.choice(["포함", "미포함", "true", "false"]), "notes": ""})
# 일부러 틀린 행 3개: 관측일 미래, min>median, n=0 인데 가격
rows.append(dict(rows[0], observed_at="2026-12-01"))
rows.append(dict(rows[1], price_min_krw="99999999"))
rows.append(dict(rows[2], n_listings="0"))
form_recs = P.form_to_records(rows, created_at="2026-11-26T09:00:00+00:00")
ferrs, fwarns = P.validate_form_records(form_recs, products, TODAY)
print(f"행 {len(rows)} (정상 20 + 의도적 오류 3) → 오류 {len(ferrs)}건, 경고 {len(fwarns)}건")
for e in ferrs:
    print("  -", e)
for w in fwarns[:5]:
    print("  ·", w)
print("site → platform/channel:", sorted(Counter((x["source"]["platform"], x["source"]["channel"]) for x in form_recs).items()))
print("예:", json.dumps(form_recs[0], ensure_ascii=False)[:300], "...")

# 4. 트렌드 변환: 문서 예시 응답 → trend_index 레코드 → 스키마 검증 ----------------------------
section("4. 트렌드 → trend_index 레코드 (문서 예시 응답, 숫자 의미 없음)")
rows_t = TN.parse_response(TN.DOC_EXAMPLE_RESPONSE)
norm = TN.detect_normalization(rows_t)
rel = TN.normalize_by_anchor(rows_t, "한글", mode="period")
sidx = TN.to_search_index(rel)
# 예시의 그룹명 '영어' 는 소분류 코드가 아니므로 FD-PR-SNK 로 바꿔 스키마 형식을 맞춘다(형식 점검용)
rows_t2 = [TN.TrendRow("FD-PR-SNK" if r.group == "영어" else r.group, r.period, r.ratio) for r in rows_t]
rel2 = {("FD-PR-SNK", p): v for (g, p), v in rel.items()}
sidx2 = {("FD-PR-SNK", p): v for (g, p), v in sidx.items()}
trecs = P.trend_to_records(rows_t2, anchor_group="한글", anchor_keyword="생수", normalization=norm, rel=rel2, idx=sidx2,
                           platform="naver_datalab_search", time_unit="month", run_id="J06-DRYRUN",
                           keywords={"FD-PR-SNK": ["감자칩", "쿠키"]}, keywords_version="0.1")
terrs = P.validate_trend_records(trecs)
print(f"정규화 판별 {norm}, 레코드 {len(trecs)}건, 스키마·교차 오류 {len(terrs)}건", terrs)
print("예:", json.dumps(trecs[0], ensure_ascii=False))
# per_group 이면 search_index 를 저장하지 않는지
trecs_pg = P.trend_to_records(rows_t2, anchor_group="한글", anchor_keyword="생수", normalization="per_group", rel=rel2, idx=sidx2,
                              platform="naver_datalab_search", time_unit="month", run_id="J06-DRYRUN")
print("per_group 가정 시 search_index:", [t["search_index"] for t in trecs_pg], "오류", P.validate_trend_records(trecs_pg))
with tempfile.TemporaryDirectory() as td:
    st = P.Store(Path(td), cfg["dedup_keys"])
    a = st.ingest("trend_index", trecs, "J06"); b = st.ingest("trend_index", trecs, "J06")
    print(f"trend_index 적재: new {a['n_new']} → 재적재 dup {b['n_dup']}, 파티션 {[str(p.relative_to(td)) for p in Path(td).glob('trend_index/*/*.jsonl')]}")

# 5. platform enum 제안(스키마는 고치지 않음) -----------------------------------------------
section("5. market_price.source.platform enum 제안 (승인 전 — 스키마 미수정)")
from src.collect import sources as PS  # noqa: E402
doc = PS.load_sources()
all_platforms = sorted(s["platform"] for s in doc["sources"]) + ["other"]
print(f"제안 enum {len(all_platforms)}개(prohibited 포함 — 수기 관측 사이트 식별용; 금지는 collected_by∈{{api,crawler}} 에만 적용):")
print(" ", all_platforms)
synth_plats = sorted(Counter(r["source"]["platform"] for r in recs))
print(f"합성 v2 가 쓰는 platform {synth_plats} 중 enum 에 없는 것: {[p for p in synth_plats if p not in all_platforms]} → enum 반영 시 합성 생성기(D034)도 갱신 필요")
print("\n끝.")
