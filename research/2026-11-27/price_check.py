"""D019 시세 수집기 v1 점검 — 참가격 형식 합성 CSV 로 파서·매칭·집계·검증·적재(append-only)·규모를 실제로 돌려 본다.

실행: python research/2026-11-27/price_check.py > research/2026-11-27/price_check.txt
데이터: research/2026-11-27/synth_price_go_kr.csv (src/collect/price_synth.py, seed 20261127) — 전부 합성. 실제 참가격 데이터 아님.
저장소는 임시 폴더에 만들고 끝나면 지운다(저장소 위치 정책 11-26 질문 1 미답).
"""
from __future__ import annotations

import csv
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
from src.collect import price as PR  # noqa: E402
from src.collect import price_synth as PSY  # noqa: E402
from src.schema import market_price as MP  # noqa: E402

HERE = Path(__file__).resolve().parent
TODAY = date(2026, 11, 27)
DATES = [date(2026, 11, 5), date(2026, 11, 19)]
SEED = 20261127
CSV = HERE / "synth_price_go_kr.csv"


def section(t):
    print(f"\n## {t}")


# ----- 0) 합성 CSV --------------------------------------------------------------------
section("0. 합성 CSV (참가격 형식, 실데이터 아님)")
rows = PSY.make_rows(SEED, DATES)
PSY.write_csv(rows, CSV, encoding="cp949")
print(f"{CSV.name}: {len(rows)}행, 상품 {len(PSY.GOODS)}개(매칭 대상 11 + 대조군 13), 업소 {len(PSY.STORES)}곳, 조사일 {[d.isoformat() for d in DATES]}, 오염 행 6, 인코딩 cp949")
print(f"크기 {CSV.stat().st_size:,}B")

items = PR.load_items()
products = MP.load_products()
cfg = P.load_config()

# ----- 1) 파싱 ------------------------------------------------------------------------
section("1. 파싱·정규화")
raw, colmap, enc = PR.read_csv(CSV)
norm, dropped = PR.normalize_rows(raw)
print(f"인코딩 {enc}, 열 매핑 {colmap}")
print(f"행 {len(raw)} → {len(norm)} (버림 {dict(dropped)})")
print("오염 행 6 중 버려야 할 4(빈 가격·0원·날짜 오류·빈 상품명) 버림, 살려야 할 2('20261105'·'2026.11.05' 날짜, '22,000원' 가격) 통과:",
      "OK" if dict(dropped) == {"bad_price": 2, "bad_date": 1, "no_name": 1} else "FAIL")
with tempfile.TemporaryDirectory() as td:
    p8 = Path(td) / "utf8.csv"
    PSY.write_csv(rows, p8, encoding="utf-8-sig")
    raw8, _, enc8 = PR.read_csv(p8)
    print(f"같은 내용 utf-8-sig 파일: 인코딩 {enc8}, 행 {len(raw8)} — 동일:", "OK" if len(raw8) == len(raw) else "FAIL")

# ----- 2) 매칭 ------------------------------------------------------------------------
section("2. 품목 매칭 (price_go_kr_items.yaml, 10 item → 9 product)")
matched, unmatched = PR.match_rows(norm, items)
per_item = Counter(it["item_id"] for _, it in matched)
per_name = Counter((r["good_name"], it["item_id"]) for r, it in matched)
print(f"매칭 {len(matched)}행 / 미매칭 {sum(unmatched.values())}행·{len(unmatched)}개 상품명")
print("item 별 행 수:", dict(sorted(per_item.items())))
print("상품명 → item:")
for (name, iid), n in sorted(per_name.items(), key=lambda kv: kv[0][1]):
    print(f"  {iid}  {name:<22} {n}")
print("미매칭 상품명(대조군 13개 전부여야 함):")
for name, n in unmatched.most_common():
    print(f"  {name:<22} {n}")
decoys = {g[0] for g in PSY.GOODS[11:]}
print("대조군 13개 전부 미매칭·매칭 대상 11개 전부 매칭:",
      "OK" if set(unmatched) == decoys and {n for n, _ in per_name} == {g[0] for g in PSY.GOODS[:11]} else "FAIL")

print("표기 변형 내성(실 파일 표기는 미확인 — 띄어쓰기·대소문자·단위 변형):")
variants = ["감자칩오리지널60G", "캔 커피 175ML", "액체 세탁세제 3000ml", "비타민씨 120정", "비타민C 1000mg", "컵라면 매운맛 65 g",
            "유아물티슈 70매", "강아지 간식 100g 치킨", "유리밀폐용기 세트", "감자칩 오리지널 (60g)", "감자칩 60g×2", "캔커피 175ml 6입"]
tol = items["content_tolerance"]
for v in variants:
    it = PR.match_item({"good_name": v, "content": PR.parse_content(v)}, items["items"], tol)
    print(f"  {v:<22} → {it['item_id'] if it else '-'}   (용량 {PR.parse_content(v)})")

# ----- 3) 집계·검증 -----------------------------------------------------------------------
section("3. 상품 × 조사일 집계 → market_price 레코드 → 3단계 검증")
recs, agg = PR.aggregate(matched, items, products, created_at="2026-11-27T09:00:00+00:00", collected_by="synthetic", source_file=CSV.name)
print(f"그룹 {agg['n_groups']} (빈 {agg['n_empty']}), 세일 제외 {agg['n_sale_excluded']}, 1+1 제외 {agg['n_plus_one_excluded']} → 레코드 {len(recs)} (상품 9 × 조사일 2)")
warns: dict[int, list[str]] = {}
errs = MP.validate_records(recs, products=products, today=TODAY, warnings=warns)
print(f"검증 오류 {sum(len(v) for v in errs.values())} / 경고 {sum(len(v) for v in warns.values())}")
print(f"{'price_id':<10} {'product':<10} {'date':<10} {'unit':>4} {'n':>3} {'min':>8} {'median':>8} {'max':>8} {'list':>8} {'ratio':>6}  warn")
for i, r in enumerate(recs):
    pr = products[r["product_id"]]
    ratio = MP.price_ratio(r, pr)
    w = "; ".join(x.split("→")[0].split(": ", 1)[1].strip() for x in warns.get(i, []))
    print(f"{r['price_id']:<10} {r['product_id']:<10} {r['observed_at']:<10} {r['unit_count']:>4} {r['n_listings']:>3} {r['price_min_krw']:>8,} {r['price_median_krw']:>8,} {r['price_max_krw']:>8,} {pr['list_price_krw']:>8,} {ratio:>6.2f}  {w}")
print("notes 예:", recs[0]["notes"])

# 세일 포함 시 중앙값 변화
cfg_incl = dict(items, exclude_sale=False, exclude_plus_one=False)
recs_incl, _ = PR.aggregate(matched, cfg_incl, products, collected_by="synthetic")
diffs = [(a["product_id"], a["observed_at"], a["price_median_krw"], b["price_median_krw"], a["price_min_krw"], b["price_min_krw"])
         for a, b in zip(recs, recs_incl)]
print("세일·1+1 제외 vs 포함: 중앙값 차이 평균 {:.1f}% , 최소값 차이 평균 {:.1f}% (포함 시 min 이 세일가로 끌려 내려감)".format(
    100 * sum((b - a) / a for _, _, a, b, _, _ in diffs) / len(diffs), 100 * sum((b - a) / a for _, _, _, _, a, b in diffs) / len(diffs)))

# ----- 4) 적재 ------------------------------------------------------------------------
section("4. append-only 적재 (임시 폴더) — 1차 적재 → 재적재 → 수정 파일 → 추가 조사일")
with tempfile.TemporaryDirectory() as td:
    store = P.Store(Path(td) / "store", cfg["dedup_keys"])
    s1 = PR.run(CSV, items, products, TODAY, store=store, collected_by="synthetic", created_at="2026-11-27T09:00:00+00:00")
    print("1차:", {k: s1["ingest"][k] for k in ("n_in", "n_new", "n_dup", "n_conflict")})
    s2 = PR.run(CSV, items, products, TODAY, store=store, collected_by="synthetic", created_at="2026-11-28T09:00:00+00:00")
    print("같은 파일 재적재(created_at 만 다름):", {k: s2["ingest"][k] for k in ("n_in", "n_new", "n_dup", "n_conflict")})
    # 수정 파일: 감자칩 한 업소 가격 변경 → 해당 상품×조사일 1건 conflict
    rows_mod = [dict(r) for r in rows]
    for r in rows_mod:
        if r["GOOD_NAME"] == "감자칩 오리지널 60g" and r["조사일"] == "2026-11-05" and r["SORE_NAME"] == "대형마트A 서울점":
            r["판매가격"] = "9999"
    pm = Path(td) / "mod.csv"
    PSY.write_csv(rows_mod, pm)
    s3 = PR.run(pm, items, products, TODAY, store=store, collected_by="synthetic", created_at="2026-11-29T09:00:00+00:00")
    print("수정 파일(파일명 다름, 감자칩 11-05 업소 1곳 가격 변경 → 집계 max 변경):", {k: s3["ingest"][k] for k in ("n_in", "n_new", "n_dup", "n_conflict")},
          "— 기대 conflict 1·dup 17:", "OK" if (s3["ingest"]["n_conflict"], s3["ingest"]["n_dup"]) == (1, 17) else "FAIL")
    print("  (1차 구현은 notes 에 file=<파일명> 을 넣어 파일명만 달라도 18건 전부 conflict 가 됐다 → 파일명은 manifest 로 옮김)")
    print("  manifest 마지막:", {k: v for k, v in store.manifest()[-1].items() if k in ("job_id", "source_file", "n_rows", "n_matched", "n_new", "n_dup", "n_conflict")})
    # 다음 달 파일: 조사일 1개 추가(11-26) + 기존 2개 → new 9
    rows_next = PSY.make_rows(SEED + 1, DATES + [date(2026, 11, 26)])
    pn = Path(td) / "next.csv"
    PSY.write_csv(rows_next, pn)
    s4 = PR.run(pn, items, products, TODAY, store=store, collected_by="synthetic", created_at="2026-11-30T09:00:00+00:00")
    print("다른 seed 의 다음 파일(조사일 11-05·11-19·11-26):", {k: s4["ingest"][k] for k in ("n_in", "n_new", "n_dup", "n_conflict")},
          "— 기존 조사일은 가격이 달라 conflict 18, 새 조사일 new 9")
    part = Path(td) / "store" / "market_price" / "price_go_kr" / "2026-11.jsonl"
    n_lines = sum(1 for _ in open(part, encoding="utf-8"))
    print(f"파티션 {part.relative_to(td)}: {n_lines}행, {part.stat().st_size:,}B ({part.stat().st_size / n_lines:.0f}B/건); quarantine {sum(1 for _ in open(Path(td) / 'store' / 'quarantine' / 'market_price.jsonl', encoding='utf-8'))}행; manifest {len(store.manifest())}건")
    print("run 요약 출력 예:")
    print("  " + PR.format_summary(s1).replace("\n", "\n  "))

# ----- 5) 규모 -----------------------------------------------------------------------
section("5. 규모 — D016 스냅샷 크기(235,632행)만큼 부풀려 파싱·매칭 시간")
big = []
k = 0
while len(big) < 235_632:
    for r in rows:
        if r["GOOD_NAME"] and r["판매가격"] not in ("", "0"):
            big.append(dict(r, SORE_NAME=f"{r['SORE_NAME']}#{k}"))
            if len(big) >= 235_632:
                break
    k += 1
with tempfile.TemporaryDirectory() as td:
    pb = Path(td) / "big.csv"
    PSY.write_csv(big, pb)
    t0 = time.perf_counter()
    rb, _, _ = PR.read_csv(pb)
    t1 = time.perf_counter()
    nb, db = PR.normalize_rows(rb)
    t2 = time.perf_counter()
    mb, ub = PR.match_rows(nb, items)
    t3 = time.perf_counter()
    recb, aggb = PR.aggregate(mb, items, products, collected_by="synthetic")
    t4 = time.perf_counter()
    print(f"{len(rb):,}행 {pb.stat().st_size / 1e6:.1f}MB: 읽기 {t1 - t0:.2f}s, 정규화 {t2 - t1:.2f}s, 매칭 {t3 - t2:.2f}s, 집계 {t4 - t3:.2f}s → 레코드 {len(recb)} (업소 수만 늘어 상품×조사일 수는 같음), 매칭 {len(mb):,}행")

# ----- 6) 그림 ------------------------------------------------------------------------
section("6. 그림 fig1_price_vs_list.png — 상품별 환산 시세(중앙값·min~max) vs 정상가, 조사일 2개 (합성)")
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    pids = sorted({r["product_id"] for r in recs})
    labels = [f"{p}\n{products[p]['name'][:12]}" for p in pids]
    colors = {"2026-11-05": "#257de0", "2026-11-19": "#f47230"}
    fig, ax = plt.subplots(figsize=(11, 5), dpi=130)
    for j, d in enumerate(DATES):
        ds = d.isoformat()
        xs = [i + (-0.18 if j == 0 else 0.18) for i in range(len(pids))]
        rr = {r["product_id"]: r for r in recs if r["observed_at"] == ds}
        med = [rr[p]["price_median_krw"] / products[p]["list_price_krw"] for p in pids]
        lo = [med[i] - rr[p]["price_min_krw"] / products[p]["list_price_krw"] for i, p in enumerate(pids)]
        hi = [rr[p]["price_max_krw"] / products[p]["list_price_krw"] - med[i] for i, p in enumerate(pids)]
        ax.errorbar(xs, med, yerr=[lo, hi], fmt="o", ms=6, color=colors[ds], ecolor=colors[ds], elinewidth=2, capsize=3, label=f"조사일 {ds}")
    ax.axhline(1.0, color="#666666", lw=1, ls="--")
    ax.text(len(pids) - 0.5, 1.005, "정상가 = 1.0", ha="right", va="bottom", fontsize=8, color="#666666")
    ax.set_xticks(range(len(pids)))
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel("환산 시세 ÷ 정상가 (중앙값, 선은 min~max)")
    ax.set_title("참가격 형식 합성 CSV → market_price 집계 (D019 v1, 합성 데이터)", fontsize=10)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#eeeeee")
    ax.legend(frameon=False, fontsize=8)
    for font in ("NanumGothic", "Noto Sans CJK KR", "AppleGothic", "Malgun Gothic"):
        if any(font in f.name for f in matplotlib.font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = font
            break
    fig.tight_layout()
    fig.savefig(HERE / "fig1_price_vs_list.png")
    print("저장:", (HERE / "fig1_price_vs_list.png").name)
except Exception as e:  # noqa: BLE001
    print("그림 생략:", e)
