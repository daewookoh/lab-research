"""D012 — Re-Flow Score v0 베이스라인: 합성 v2 에 점수를 적용해 점수 vs 판매기간 상관을 잰다 (2026-11-19). 전부 합성 데이터.

실행: python research/2026-11-19/baseline_v0.py   (저장소 루트에서, 약 1~2분)
입력: data/synth/v2/*.jsonl.gz (M1 테이블 4종), data/synth/v2_flat.csv.gz (정답 latent_* 열 — 오라클 비교에만 사용)
출력: research/2026-11-19/baseline_v0.txt, scores_v0_v2.csv.gz, fig1~4 png

평가 설계:
  (1) "등록 시점 평가"(as_of_listing, 본 베이스라인): 거래마다 today = listed_date 로 score_product 를 부른다. 판매속도 항목의
      D_ref 는 그 날짜 이전에 **이미 팔린** 거래만으로 만든다(시간 누출 방지). 시세는 그 상품의 레코드를 그대로 쓴다 —
      합성기의 observed_at 은 기준일(2026-11-18) 기준이라 등록일과 무관하므로 stale/approx 플래그는 의미가 없다(본문에 명시).
  (2) "기준일 평가"(ref_date, 누출 대조군): score_v0 CLI 기본 방식 = today 2026-11-18, D_ref 에 자기 자신·미래 거래 포함.
  상관은 D010 §7-1 대로 sold-only(Spearman)·절단 반영(KM 중앙값, C-index, 로그정규 AFT)·오라클(잠재 T, 합성에서만 가능) 셋을 나란히.
"""
from __future__ import annotations

import gzip
import json
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
from scipy import stats

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.eval.survival import concordance_index, design_matrix, kaplan_meier, lognormal_aft, ols  # noqa: E402
from src.reflow import score_v0 as S  # noqa: E402
from src.schema.categories import load as load_categories  # noqa: E402

OUT = Path(__file__).resolve().parent
LINES: list[str] = []
T0 = time.time()
C = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]  # dataviz 기본 범주 팔레트(검증됨)


def say(s: str = "") -> None:
    print(s)
    LINES.append(s)


def read_jsonl_gz(p: Path) -> list[dict]:
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip() and not l.startswith("#")]


# ---------------------------------------------------------------- 0. 적재
V2 = ROOT / "data/synth/v2"
products = read_jsonl_gz(V2 / "product.jsonl.gz")
txs = read_jsonl_gz(V2 / "transaction.jsonl.gz")
sellers = {s["seller_id"]: s for s in read_jsonl_gz(V2 / "seller.jsonl.gz")}
prices = read_jsonl_gz(V2 / "market_price.jsonl.gz")
leaves = {l.code: l for l in load_categories()}
pmap = {p["product_id"]: p for p in products}
prices_by_pid: dict[str, list[dict]] = {}
for m in prices:
    prices_by_pid.setdefault(m["product_id"], []).append(m)
flat = pd.read_csv(ROOT / "data/synth/v2_flat.csv.gz")
REF = date(2026, 11, 18)

say("# D012 Re-Flow Score v0 베이스라인 — 2026-11-19 (전부 합성 데이터 v2, seed 20261118)")
say(f"입력: 상품 {len(products)} / 거래 {len(txs)} / 판매자 {len(sellers)} / 시세 {len(prices)} (시세 없는 상품 {sum(1 for p in products if p['product_id'] not in prices_by_pid)})")

# ---------------------------------------------------------------- 1. 점수 계산 (등록 시점 / 기준일)
say("\n## 1. 점수 계산")
txs_sorted = sorted(txs, key=lambda t: (t["listing"]["listed_date"], t["transaction_id"]))
sold_events = sorted(
    ((t["outcome"]["sold_date"], pmap[t["product_id"]]["category_code"], int(t["duration_days"])) for t in txs if t["outcome"]["status"] == "sold"),
    key=lambda x: x[0],
)
rows = []
idx: dict[str, list[int]] = {}
ptr = 0
t1 = time.time()
for t in txs_sorted:
    today = date.fromisoformat(t["listing"]["listed_date"])
    while ptr < len(sold_events) and sold_events[ptr][0] <= today.isoformat():   # 등록일 이전에 끝난 판매만 D_ref 에 반영
        idx.setdefault(sold_events[ptr][1], []).append(sold_events[ptr][2])
        ptr += 1
    p = pmap[t["product_id"]]
    rec = S.score_product(p, leaves[p["category_code"]], prices_by_pid.get(p["product_id"], []), sellers.get(t.get("seller_id")), idx, today)
    rows.append({"transaction_id": t["transaction_id"], "product_id": t["product_id"], "eval_mode": "as_of_listing", "evaluated_on": today.isoformat(),
                 **{f"item_{k}": v for k, v in rec["items"].items()}, "total": rec["total"], "grade_v0": rec["grade"], "gate": rec["gate"],
                 "manual_review": rec["manual_review"], "confidence": rec["confidence"], "v0_flags": "|".join(rec["flags"]),
                 "recommended_channel": rec["recommended_channel"], "suggested_buy_ratio": rec["suggested_buy_ratio"],
                 "d_ref": rec["details"]["velocity"]["d_ref"], "d_ref_source": rec["details"]["velocity"]["d_ref_source"],
                 "price_ratio_r": rec["details"]["price"]["ratio"], "normalized_median_krw": rec["details"]["normalized_median_krw"],
                 "season_v0": rec["details"]["demand"]["season"]})
sc = pd.DataFrame(rows)
say(f"  등록 시점 평가: {len(sc)} 건, {time.time() - t1:.1f}초 ; D_ref 출처 {sc.d_ref_source.value_counts().to_dict()}")

# 기준일(누출) 대조군 — score_all 과 같은 방식
t1 = time.time()
idx_full = S.build_duration_index(txs, pmap)
ref_rows = []
for t in txs:
    p = pmap[t["product_id"]]
    rec = S.score_product(p, leaves[p["category_code"]], prices_by_pid.get(p["product_id"], []), sellers.get(t.get("seller_id")), idx_full, REF)
    ref_rows.append({"transaction_id": t["transaction_id"], "total_ref": rec["total"], **{f"ref_{k}": v for k, v in rec["items"].items()}})
sc = sc.merge(pd.DataFrame(ref_rows), on="transaction_id")
say(f"  기준일(2026-11-18) 평가: {time.time() - t1:.1f}초 ; D_ref 출처 전부 l3 (자기 자신 포함, 누출 대조군)")

df = flat.merge(sc, on=["transaction_id", "product_id"], how="inner")
assert len(df) == len(flat)
ITEMS = list(S.ITEMS)
IC = [f"item_{k}" for k in ITEMS]
df["event"] = (df.status == "sold").astype(int)
df["logT"] = np.log(np.maximum(df.duration_days, 0.5))
df["log_latentT"] = np.log(df.latent_T_days)
sold = df[df.event == 1]

say("\n### 1-1. 점수 분포 (등록 시점 평가)")
for k in IC + ["total"]:
    x = df[k]
    say(f"  {k:14}: min {x.min():5.1f} p25 {x.quantile(.25):5.1f} median {x.median():5.1f} p75 {x.quantile(.75):5.1f} max {x.max():5.1f} sd {x.std():5.1f} 서로 다른 값 {x.nunique()}")
say(f"  v0 등급 분포: {df.grade_v0.value_counts().reindex(list('ABCD')).fillna(0).astype(int).to_dict()} ; 게이트 {df.gate.notna().sum()} ({df.gate.value_counts().to_dict()}) ; manual_review {df.manual_review.sum()} ; confidence 중앙값 {df.confidence.median():.2f}")
say(f"  플래그: {dict(Counter(f for fl in df.v0_flags for f in fl.split('|') if f))}")
say(f"  추천 채널: {df.recommended_channel.value_counts().to_dict()} ; 실제 등록 채널과 일치 {np.mean(df.recommended_channel == df.channel):.3f} (합성기는 추천 채널을 쓰지 않음 — 일치율은 우연 수준이어야 정상)")
say(f"  등록 시점 vs 기준일 총점: Spearman {stats.spearmanr(df.total, df.total_ref)[0]:.3f}, 평균 차이 {np.mean(df.total_ref - df.total):+.2f}점 ; 항목별 평균 차이 " + ", ".join(f"{k} {np.mean(df[f'ref_{k}'] - df[f'item_{k}']):+.1f}" for k in ITEMS))
say(f"  시즌 판정 일치(score_v0 등록월 vs 합성기 season_state): {np.mean(df.season_v0 == df.season_state):.3f}")
say(f"  항목 간 Spearman 상관(등록 시점):")
corr = df[IC].corr(method="spearman")
for a in IC:
    say("    " + f"{a[5:]:9}" + " ".join(f"{corr.loc[a, b]:6.2f}" for b in IC))

# ---------------------------------------------------------------- 2. 점수 vs 판매기간 — 세 가지 추정
say("\n## 2. 점수 vs 판매기간 — sold-only / 절단 반영 / 오라클(잠재 T)")
say("  (부호: 총점이 높을수록 빨리 팔려야 하므로 상관은 음수, C-index 는 0.5 초과가 기대 방향)")


def block(name: str, d: pd.DataFrame, score_cols: list[str]) -> dict:
    s = d[d.event == 1]
    out = {}
    say(f"\n### {name} (n={len(d)}, sold {len(s)}, 절단 {1 - d.event.mean():.3f})")
    say(f"  {'점수':14} {'sold-only ρ':>12} {'(p)':>8} {'오라클 ρ':>10} {'C-index':>8} {'쌍 수':>10} {'AFT β/10점':>12} {'(se)':>7} {'OLS sold β/10점':>16}")
    for col in score_cols:
        rho_s, p_s = stats.spearmanr(s[col], s.duration_days)
        rho_o, _ = stats.spearmanr(d[col], d.latent_T_days)
        cidx, npairs = concordance_index(d.duration_days.to_numpy(), d.event.to_numpy(), d[col].to_numpy())
        X, names = design_matrix({}, {"x": d[col].to_numpy() / 10.0})
        aft = lognormal_aft(X, d.duration_days.to_numpy(), d.event.to_numpy(), names)
        Xs, _ = design_matrix({}, {"x": s[col].to_numpy() / 10.0})
        o = ols(Xs, s.logT.to_numpy(), names)
        out[col] = dict(rho_sold=rho_s, rho_oracle=rho_o, cindex=cidx, aft=aft.coef("x"), aft_se=float(aft.se[1]), ols=o.coef("x"))
        say(f"  {col:14} {rho_s:12.3f} {p_s:8.1e} {rho_o:10.3f} {cidx:8.3f} {npairs:10d} {aft.coef('x'):12.3f} {aft.se[1]:7.3f} {o.coef('x'):16.3f}")
    return out

res_all = block("2-1. 전체 거래 (등록 시점 평가)", df, ["total"] + IC + ["total_ref"])
ok = df[(df.gate.isna()) & (~df.manual_review)]
res_ok = block("2-2. 게이트·manual_review 제외 (실제로 매입 판단에 쓰일 행)", ok, ["total"] + IC)
# 오라클 상한: 잠재 승수 자체로 같은 지표를 재면 얼마나 나오나 (점수가 도달 가능한 천장)
df["oracle_score"] = -np.log(df.latent_mult_total)
say("\n### 2-3. 천장: 정답 승수 −log(latent_mult_total) 를 점수로 쓰면 (합성에서만 가능)")
for name, d in [("전체", df), ("sold-only 행", df[df.event == 1])]:
    rho_s = stats.spearmanr(d[d.event == 1].oracle_score, d[d.event == 1].duration_days)[0]
    rho_o = stats.spearmanr(d.oracle_score, d.latent_T_days)[0]
    cidx, _ = concordance_index(d.duration_days.to_numpy(), d.event.to_numpy(), d.oracle_score.to_numpy())
    say(f"  {name}: sold-only ρ {rho_s:.3f} / 오라클 ρ(잠재 T) {rho_o:.3f} / C-index {cidx:.3f}")
say(f"  → 잠재 T 의 분산 중 승수가 설명하는 비율: {1 - np.var(np.log(df.latent_T_base_days)) / np.var(df.log_latentT):.3f} (나머지는 소분류 기본분포+로그정규 노이즈). 소분류 평균을 아는 오라클 Spearman(소분류 중앙값+승수): "
    f"{stats.spearmanr(np.log(df.groupby('category_code').latent_T_base_days.transform('median')) + np.log(df.latent_mult_total), df.latent_T_days)[0]:.3f}")

# ---------------------------------------------------------------- 3. KM 중앙값 — v0 등급 / 총점 십분위
say("\n## 3. KM 판매기간 중앙값 — v0 등급(A/B/C/D) · 총점 십분위 (전체 거래, 절단 반영)")
km_grade = {}
say(f"  {'v0 등급':8} {'n':>6} {'sold':>6} {'KM 중앙값':>10} {'S(30)':>7} {'S(60)':>7} {'sold-only 중앙값':>16} {'잠재 T 중앙값':>14}")
for g in "ABCD":
    d = df[df.grade_v0 == g]
    if len(d) == 0:
        continue
    km = kaplan_meier(d.duration_days.to_numpy(), d.event.to_numpy())
    km_grade[g] = km
    say(f"  {g:8} {len(d):6d} {d.event.sum():6d} {str(km.median):>10} {km.survival_at(30):7.3f} {km.survival_at(60):7.3f} {d[d.event == 1].duration_days.median():16.0f} {d.latent_T_days.median():14.1f}")
df["dec"] = pd.qcut(df.total, 10, labels=False, duplicates="drop")
dec_rows = []
say(f"\n  {'십분위':6} {'총점 범위':>14} {'n':>6} {'KM 중앙값':>10} {'sold-only 중앙값':>16} {'잠재 T 중앙값':>14} {'sold 비율':>9}")
for q, d in df.groupby("dec"):
    km = kaplan_meier(d.duration_days.to_numpy(), d.event.to_numpy())
    dec_rows.append((q, d.total.min(), d.total.max(), len(d), km.median, d[d.event == 1].duration_days.median(), d.latent_T_days.median(), d.event.mean()))
    say(f"  {q:6d} {d.total.min():6.1f}~{d.total.max():6.1f} {len(d):6d} {str(km.median):>10} {d[d.event == 1].duration_days.median():16.0f} {d.latent_T_days.median():14.1f} {d.event.mean():9.3f}")
dec = pd.DataFrame(dec_rows, columns=["dec", "lo", "hi", "n", "km_med", "sold_med", "latent_med", "sold_rate"])
r_km = stats.spearmanr(dec.dec, dec.km_med.astype(float))[0]
r_sold = stats.spearmanr(dec.dec, dec.sold_med)[0]
say(f"  십분위 ↔ KM 중앙값 Spearman {r_km:.2f} / ↔ sold-only 중앙값 {r_sold:.2f} / ↔ 잠재 T 중앙값 {stats.spearmanr(dec.dec, dec.latent_med)[0]:.2f}")

# ---------------------------------------------------------------- 4. 6개 항목 동시 모형 → 암묵적 가중치
say("\n## 4. 6개 항목 동시 모형 (표준화 항목, 전체 거래) — AFT vs sold-only OLS vs 오라클 OLS(잠재 T)")
Z = (df[IC] - df[IC].mean()) / df[IC].std()
X, names = design_matrix({}, {k[5:]: Z[k].to_numpy() for k in IC})
aft6 = lognormal_aft(X, df.duration_days.to_numpy(), df.event.to_numpy(), names)
Xs, _ = design_matrix({}, {k[5:]: Z.loc[sold.index, k].to_numpy() for k in IC})
ols6 = ols(Xs, sold.logT.to_numpy(), names)
orc6 = ols(X, df.log_latentT.to_numpy(), names)
say(f"  {'항목':10} {'v0 가중치':>9} {'AFT β(sd)':>11} {'(se)':>6} {'sold OLS β':>11} {'오라클 β':>9} {'→ 암묵 가중치(AFT)':>18} {'(오라클)':>8}")
w_aft = {k: max(0.0, -aft6.coef(k)) for k in ITEMS}
w_orc = {k: max(0.0, -orc6.coef(k)) for k in ITEMS}
sa, so = sum(w_aft.values()), sum(w_orc.values())
for k in ITEMS:
    say(f"  {k:10} {S.WEIGHTS[k]:9.2f} {aft6.coef(k):11.3f} {aft6.se[names.index(k)]:6.3f} {ols6.coef(k):11.3f} {orc6.coef(k):9.3f} {w_aft[k] / sa:18.2f} {w_orc[k] / so:8.2f}")
say(f"  AFT σ {aft6.sigma:.3f} (잠재 log T 의 sd {df.log_latentT.std():.3f}, 소분류 통제 없음) ; sold OLS 잔차 sd {ols6.sigma:.3f} ; 오라클 R² {1 - orc6.sigma**2 / df.log_latentT.var():.3f}")
# 소분류 고정효과를 넣으면 (소분류 기본분포 분리 후 항목의 순수 기여)
Xf, namesf = design_matrix({}, {k[5:]: Z[k].to_numpy() for k in IC}, fixed_effects=df.category_code.to_numpy())
aft6f = lognormal_aft(Xf, df.duration_days.to_numpy(), df.event.to_numpy(), namesf)
orc6f = ols(Xf, df.log_latentT.to_numpy(), namesf)
say(f"  소분류 FE 추가 시 AFT β: " + ", ".join(f"{k} {aft6f.coef(k):+.3f}" for k in ITEMS) + f" (σ {aft6f.sigma:.3f})")
say(f"  소분류 FE 추가 시 오라클 β: " + ", ".join(f"{k} {orc6f.coef(k):+.3f}" for k in ITEMS))

# ---------------------------------------------------------------- 5. 등급(상품잔존성) 항목 — 가격 통제 조건부
say("\n## 5. 등급 효과는 가격 통제 조건부 (D010 §4 재확인, v0 항목 단위)")
df["log_p_obs"] = np.log(df.listed_price_krw / df.normalized_median_krw.where(df.normalized_median_krw > 0))
df["log_p_true"] = np.log(df.latent_price_ratio_p)
d5 = df[df.log_p_obs.notna()]
say(f"  관측 가격비 proxy = 등록가 ÷ 환산 시세(v0 §1 선택 레코드), 사용 가능 {len(d5)} 행 ; proxy vs 정답 log p 상관 {stats.pearsonr(d5.log_p_obs, d5.log_p_true)[0]:.3f}")
for label, extra in [("통제 없음", {}), ("+ log p(관측 proxy)", {"log_p": d5.log_p_obs.to_numpy()}), ("+ log p(정답)", {"log_p": d5.log_p_true.to_numpy()})]:
    X, names = design_matrix({"grade": (d5.condition_grade.to_numpy(), "S")}, {"residual_item": d5.item_residual.to_numpy() / 10.0, **extra}, fixed_effects=d5.category_code.to_numpy())
    a = lognormal_aft(X, d5.duration_days.to_numpy(), d5.event.to_numpy(), names)
    say(f"  {label:20}: AFT β residual/10점 {a.coef('residual_item'):+.3f}±{a.se[names.index('residual_item')]:.3f} ; 등급 더미 A {a.coef('grade=A'):+.3f} B {a.coef('grade=B'):+.3f} C {a.coef('grade=C'):+.3f}"
        + (f" ; log p {a.coef('log_p'):+.3f}" if "log_p" in names else "") + "   (정답 log 승수 A +0.095 B +0.336 C +0.693, 가격 탄력 +1.0)")
say("  가격경쟁력 항목(r = 시세÷정상가)은 '시장이 얼마나 쳐주나' 이고 판매기간을 결정하는 것은 등록가÷시세(p) — 다른 양이다. v0 에는 p 가 없다(등록가는 매입 판단 시점에 아직 없음).")

# ---------------------------------------------------------------- 6. 회수율
say("\n## 6. 총점 vs 회수율 (sold·purchase 만, 진행 중 행 제외)")
d6 = sold[(sold.acquisition_type == "purchase") & sold.recovery_rate.notna()]
say(f"  n={len(d6)} ; Spearman(총점, 회수율) {stats.spearmanr(d6.total, d6.recovery_rate)[0]:+.3f} ; Spearman(제안 매입가 비율, 회수율) {stats.spearmanr(d6.suggested_buy_ratio, d6.recovery_rate)[0]:+.3f}")
say(f"  v0 등급별 회수율 중앙값·적자율: " + " / ".join(f"{g} {d6[d6.grade_v0 == g].recovery_rate.median():.2f}·{(d6[d6.grade_v0 == g].recovery_rate < 1).mean():.2f}(n={len(d6[d6.grade_v0 == g])})" for g in "ABCD" if (d6.grade_v0 == g).any()))
say("  (합성기 매입가는 등록가의 35~60% 균등 — score_v0 와 무관하게 생성되므로 회수율 상관은 설계상 0 근처가 정상. 실데이터 해석 기준 아님.)")

# ---------------------------------------------------------------- 7. 부호가 반대인 항목·약한 항목의 원인 진단 (합성 정답 열 사용)
say("\n## 7. 진단 — 왜 velocity 는 약하고 residual·logistics 는 부호가 반대인가 (합성 정답 열로 분해)")
T_, E_ = df.duration_days.to_numpy(), df.event.to_numpy()
cidx = lambda x: concordance_index(T_, E_, np.asarray(x, dtype=float))[0]  # noqa: E731
leaf_med = df.groupby("category_code").latent_T_base_days.transform("median")
say(f"  velocity: D_ref(등록 시점, 과거 sold 중앙값) 의 C-index {cidx(-df.d_ref):.3f} vs 소분류 정답 기본분포 중앙값의 C-index {cidx(-np.log(leaf_med)):.3f} ; Spearman(D_ref, 정답 소분류 중앙값) {stats.spearmanr(df.d_ref, leaf_med)[0]:.3f}")
early = df[pd.to_datetime(df.listed_date) < pd.Timestamp("2026-06-01")]
late = df[pd.to_datetime(df.listed_date) >= pd.Timestamp("2026-09-01")]
say(f"    등록 시기별 Spearman(D_ref, 정답 소분류 중앙값): 6월 이전 등록 {stats.spearmanr(early.d_ref, early.groupby('category_code').latent_T_base_days.transform('median'))[0]:.3f} (n={len(early)}) / 9월 이후 {stats.spearmanr(late.d_ref, late.groupby('category_code').latent_T_base_days.transform('median'))[0]:.3f} (n={len(late)})")
say(f"    D_ref 는 '그때까지 팔린 것의 중앙값' 이라 초기에는 표본이 적고 짧은 T 만 관측된 편향(D010 duration~elapsed)을 그대로 갖는다. 전체 D_ref 중앙값 {df.d_ref.median():.0f}일 vs sold-only 전체 중앙값 {sold.duration_days.median():.0f}일 vs KM 중앙값 {kaplan_meier(T_, E_).median:.0f}일")
say("  residual 하위 요소별 잠재 T 중앙값(합성 정답):")
for col, order in [("expiry_class", ["short", "medium", "long", "none"]), ("condition_grade", list("SABC")), ("packaging_state", ["sealed", "box_opened", "repackaged", "box_damaged", "no_box"])]:
    say(f"    {col:16}: " + " / ".join(f"{k} {df[df[col] == k].latent_T_days.median():.1f}" for k in order if (df[col] == k).any()))
hold = (pd.to_datetime(df.listed_date) - pd.to_datetime(df.acquired_date)).dt.days
say(f"    보유기간(등록일−매입일) vs 잠재 T Spearman {stats.spearmanr(hold, df.latent_T_days)[0]:+.3f} (합성기에 보유기간 효과 없음 — 설계대로 0)")
say("    → expiry 하위점수(0.40 가중)는 '유통기한 없음 = 100점' 인데 v2 에서 none 류는 기본분포가 가장 느리고 short 류가 가장 빠르다(설계 사전값 14/30/45/60). 잔여기한 점수가 소분류 기본속도와 역방향으로 얽혀 residual 전체 부호를 뒤집는다.")
say("  logistics 하위 요소: " + " / ".join(f"{k} {df[df.size_class == k].latent_T_days.median():.1f}" for k in ["small", "medium", "large"]) + " (size_class별 잠재 T 중앙값) ; "
    + "권역별 " + " / ".join(f"{k} {df[df.seller_region == k].latent_T_days.median():.0f}" for k in sorted(df.seller_region.dropna().unique()))
    + f" ; Spearman(정상가, 잠재 T) {stats.spearmanr(df.list_price_krw, df.latent_T_days)[0]:+.3f}")
say("    → 합성기에 부피·권역·가치밀도 효과가 없고(설계), 정상가가 높은 대분류(DE 49,800원)가 느린 편이라 가치밀도 하위점수가 약한 역방향 신호가 된다. 두 항목의 역부호는 v0 가 틀렸다는 증거가 아니라 v2 가정의 반영이다.")
say(f"  demand 하위 요소: 브랜드 등급별 잠재 T 중앙값 " + " / ".join(f"{k} {df[df.brand_tier == k].latent_T_days.median():.1f}" for k in ["major", "mid", "minor", "unbranded", "unknown"])
    + f" ; 시즌 판정별 " + " / ".join(f"{k} {df[df.season_state == k].latent_T_days.median():.1f}" for k in ["in_season", "near_season", "off_season", "neutral"]))

# ---------------------------------------------------------------- 7. 그림 (영문 라벨 — 실행 환경 matplotlib 기본 폰트에 한글 없음, D010·D011 과 같은 관례)
plt.rcParams.update({"font.family": "DejaVu Sans", "axes.spines.top": False, "axes.spines.right": False, "axes.facecolor": "#fcfcfb", "font.size": 9})
fig, ax = plt.subplots(figsize=(7, 4.2))
for g, c in zip("ABCD", C):
    if g in km_grade:
        km = km_grade[g]
        ax.step(np.concatenate([[0], km.times]), np.concatenate([[1], km.survival]), where="post", color=c, lw=2, label=f"v0 grade {g} (n={int((df.grade_v0 == g).sum())}, KM median {km.median:.0f}d)")
ax.set_xlabel("days since listing"); ax.set_ylabel("still unsold S(t)"); ax.set_xlim(0, 240); ax.set_ylim(0, 1)
ax.set_title("Fig 1. Kaplan-Meier unsold curves by Re-Flow v0 grade (synthetic v2, scored as of listing)", loc="left"); ax.legend(frameon=False, fontsize=8); ax.grid(alpha=.25)
fig.tight_layout(); fig.savefig(OUT / "fig1_km_by_v0_grade.png", dpi=130); plt.close(fig)

fig, ax = plt.subplots(figsize=(7, 4.2))
ax.plot(dec.dec, dec.km_med.astype(float), "o-", color=C[0], lw=2, ms=8, label="KM median (censoring-aware)")
ax.plot(dec.dec, dec.sold_med, "s--", color=C[1], lw=2, ms=7, label="sold-only median")
ax.plot(dec.dec, dec.latent_med, "^:", color=C[2], lw=2, ms=7, label="latent T median (ground truth)")
ax.set_xlabel("v0 total score decile (0 = lowest)"); ax.set_ylabel("median days to sell"); ax.set_xticks(range(10)); ax.grid(alpha=.25)
ax.set_title("Fig 2. Days to sell by total-score decile: three estimators", loc="left"); ax.legend(frameon=False, fontsize=8)
fig.tight_layout(); fig.savefig(OUT / "fig2_decile_duration.png", dpi=130); plt.close(fig)

fig, ax = plt.subplots(figsize=(7.5, 4.2))
x = np.arange(len(ITEMS)); w = 0.26
ax.bar(x - w, [ols6.coef(k) for k in ITEMS], w, color=C[1], label="sold-only OLS")
ax.bar(x, [aft6.coef(k) for k in ITEMS], w, color=C[0], label="lognormal AFT (censoring-aware)")
ax.bar(x + w, [orc6.coef(k) for k in ITEMS], w, color=C[2], label="oracle OLS on latent T")
ax.axhline(0, color="#777", lw=1); ax.set_xticks(x); ax.set_xticklabels(ITEMS); ax.set_ylabel("log T coefficient per 1 sd of item")
ax.set_title("Fig 3. Joint model of the 6 items (negative = higher score sells faster)", loc="left"); ax.legend(frameon=False, fontsize=8); ax.grid(axis="y", alpha=.25)
fig.tight_layout(); fig.savefig(OUT / "fig3_item_coefficients.png", dpi=130); plt.close(fig)

fig, axes = plt.subplots(1, 2, figsize=(10, 4.2), sharey=True)
axes[0].hexbin(sold.total, np.maximum(sold.duration_days, 0.5), gridsize=35, cmap="Blues", mincnt=1, yscale="log")
axes[0].set_title(f"sold-only: total vs observed days (rho={res_all['total']['rho_sold']:+.3f})", loc="left"); axes[0].set_xlabel("v0 total score"); axes[0].set_ylabel("days (log)")
axes[1].hexbin(df.total, df.latent_T_days, gridsize=35, cmap="Blues", mincnt=1, yscale="log")
axes[1].set_title(f"oracle: total vs latent T, all rows (rho={res_all['total']['rho_oracle']:+.3f})", loc="left"); axes[1].set_xlabel("v0 total score")
fig.suptitle("Fig 4. Total score vs days to sell", x=0.01, ha="left", fontsize=10)
fig.tight_layout(); fig.savefig(OUT / "fig4_total_vs_duration.png", dpi=130); plt.close(fig)

# ---------------------------------------------------------------- 저장
keep = ["transaction_id", "product_id", "eval_mode", "evaluated_on"] + IC + ["total", "grade_v0", "gate", "manual_review", "confidence", "v0_flags",
        "recommended_channel", "suggested_buy_ratio", "d_ref", "d_ref_source", "price_ratio_r", "season_v0", "total_ref"] + [f"ref_{k}" for k in ITEMS]
sc_out = df[keep]
sc_out.to_csv(OUT / "scores_v0_v2.csv.gz", index=False, compression="gzip")
say(f"\n저장: research/2026-11-19/scores_v0_v2.csv.gz ({len(sc_out)} 행) ; 총 {time.time() - T0:.0f}초")
(OUT / "baseline_v0.txt").write_text("\n".join(LINES) + "\n", encoding="utf-8")
