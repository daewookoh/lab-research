"""D011 — 합성 데이터 v2 점검 (2026-11-18). 전부 합성 데이터.

실행: python research/2026-11-18/synth_v2_check.py  (저장소 루트에서)
입력: data/synth/v2_flat.csv.gz, data/synth/v2_meta.json.gz, data/synth/params_v2.yaml, data/synth/v1_flat.csv(비교)
출력: research/2026-11-18/synth_v2_check.txt, fig1~4 png

D010 §7-3 이 D011 에 넘긴 확인 항목:
  (A) 오라클 OLS(잠재 T) 의 승수 복원 최대 |log 차이| ≤ 0.05
  (B) 권역(seller_region) 가짜 효과 소멸 (v1: KM 중앙값 39~69일로 벌어짐 — 판매자 수가 적어 생긴 혼란)
  (C) 소분류별 n ≥ 100
추가: v2 신규 항(브랜드·시즌·판매자 랜덤효과)의 복원, sold-only OLS vs AFT 축소 비교(v1 재현), 시즌 등록월 편향, 판매자 기재정확도-u 연결.
"""
from __future__ import annotations

import gzip
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.eval.survival import design_matrix, kaplan_meier, lognormal_aft, ols  # noqa: E402

OUT = Path(__file__).resolve().parent
LINES: list[str] = []
T0 = time.time()


def say(s: str = "") -> None:
    print(s)
    LINES.append(s)


df = pd.read_csv(ROOT / "data/synth/v2_flat.csv.gz")
v1 = pd.read_csv(ROOT / "data/synth/v1_flat.csv")
with gzip.open(ROOT / "data/synth/v2_meta.json.gz", "rt", encoding="utf-8") as f:
    META = json.load(f)
P = yaml.safe_load((ROOT / "data/synth/params_v2.yaml").read_text(encoding="utf-8"))
M = P["multipliers"]
TRUE = {}
for g, v in M["grade"].items():
    TRUE[f"grade={g}"] = math.log(v)
for c, v in M["channel"].items():
    TRUE[f"ch={c}"] = math.log(v)
for b, v in M["brand"].items():
    TRUE[f"brand={b}"] = math.log(v)
# 시즌: neutral 은 소분류(seasonality 없음)로 결정되어 소분류 FE 와 완전 공선 → 시즌 품목 안의 대비만 식별된다.
#       설계행렬에서는 neutral 을 기준 수준(near_season)과 합치고, 정답은 near_season 대비 log 비로 둔다.
SEASON_REF = "near_season"
for s, v in M["season"].items():
    if s not in ("neutral", SEASON_REF):
        TRUE[f"season={s}"] = math.log(v / M["season"][SEASON_REF])
TRUE["log_p"] = float(M["price_elasticity"])
TRUE = {k: v for k, v in TRUE.items() if k not in ("grade=S", "ch=open_market", "brand=mid")}   # 기준 수준 제외


def season_col(d: pd.DataFrame) -> np.ndarray:
    return d.season_state.replace({"neutral": SEASON_REF}).to_numpy()
SIG_U = float(P["v2"]["seller_effect_sigma"])

sold = df[df.status == "sold"].copy()
ev_all = (df.status == "sold").astype(int).to_numpy()
tm_all = df.duration_days.to_numpy()

say("# D011 합성 데이터 v2 점검 — 2026-11-18 (전부 합성 데이터, seed %s, 기준일 %s)" % (META["seed"], META["reference_date"]))
say(f"승수: {META['multipliers_enabled']} / generator_stats {META['generator_stats']}")
say(f"거래 {len(df)} 행 / 상품 {df.product_id.nunique()} / 판매자 {df.seller_id.nunique()} (전체 {len(META['seller_latent'])}) / 열 {df.shape[1]}")

# ---------------------------------------------------------------- 1. 규모·상태 비율·소분류 n (확인 C)
say("\n## 1. 규모·상태 비율 (v1 대비) · 소분류 n — 확인 (C) n ≥ 100")
for name, d in [("v1", v1), ("v2", df)]:
    vc = d.status.value_counts(normalize=True).round(3).to_dict()
    say(f"  {name}: n={len(d):5d} {vc} censored={d.censored.mean():.3f} | sold duration 중앙값 {d[d.status=='sold'].duration_days.median():.0f} / 잠재 T 중앙값 {d.latent_T_days.median():.1f}")
cnt = df.category_code.value_counts()
cnt_sold = sold.category_code.value_counts()
say(f"  v2 소분류별 n(전체) min/median/max = {cnt.min()} / {cnt.median():.0f} / {cnt.max()} ; n<100 소분류 {[c for c in cnt.index if cnt[c] < 100]}")
say(f"  v2 소분류별 n(sold)  min/median/max = {cnt_sold.min()} / {cnt_sold.median():.0f} / {cnt_sold.max()} ; sold<100 소분류 {[c for c in cnt_sold.index if cnt_sold[c] < 100]}")
say(f"  (v1: 전체 min {v1.category_code.value_counts().min()} / sold min {v1[v1.status=='sold'].category_code.value_counts().min()})")
CHECK_C = bool(cnt.min() >= 100)
say(f"  확인 (C) 소분류 n ≥ 100 (전체 행 기준): {'PASS' if CHECK_C else 'FAIL'} ; sold 기준 ≥100: {'PASS' if cnt_sold.min() >= 100 else 'FAIL'}")
say(f"  경과일 elapsed: min {df.elapsed_days.min()} max {df.elapsed_days.max()} (설계 window 1~270; 배치 매입일 역산 규칙 때문에 v1 부터 최대 283 까지 꼬리가 있음 — v1 max {v1.elapsed_days.max()})")

# ---------------------------------------------------------------- 2. 승수 복원 (확인 A)
say("\n## 2. 승수 복원 — log T ~ 소분류 FE + 등급 + 채널 + 브랜드 + 시즌 + log(p/0.8) [+ 판매자 FE]")
say("정답(log 승수, 기준 S / open_market / mid / near_season): " + ", ".join(f"{k}={v:+.3f}" for k, v in TRUE.items()))
say("시즌 항은 neutral(비시즌 소분류)이 소분류 FE 와 공선이라 near_season 대비 in/off 만 식별 — 정답 in −0.252(=log 0.7/0.9), off +0.511(=log 1.5/0.9)")


def build(d: pd.DataFrame, seller_fe: bool = False):
    fe = d.category_code.to_numpy()
    if seller_fe:   # 소분류 FE + 판매자 FE 를 동시에 두면 공선성(절편 중복) → 판매자는 더미(기준: 첫 판매자)로 넣는다
        cats = {"grade": (d.condition_grade.to_numpy(), "S"), "ch": (d.channel.to_numpy(), "open_market"),
                "brand": (d.brand_tier.to_numpy(), "mid"), "season": (season_col(d), SEASON_REF),
                "seller": (d.seller_id.to_numpy(), sorted(d.seller_id.unique())[0])}
    else:
        cats = {"grade": (d.condition_grade.to_numpy(), "S"), "ch": (d.channel.to_numpy(), "open_market"),
                "brand": (d.brand_tier.to_numpy(), "mid"), "season": (season_col(d), SEASON_REF)}
    X, names = design_matrix(cats, {"log_p": np.log(d.latent_price_ratio_p.to_numpy() / M["price_ref_ratio"])}, fixed_effects=fe)
    return X, names


def report(fit, label, prefixes=("grade=", "ch=", "brand=", "season=", "log_p")):
    rows = [r for r in fit.table(prefixes) if r[0] in TRUE]
    say(f"[{label}] n={fit.n} sigma={fit.sigma:.3f}" + (f" loglik={fit.loglik:.1f}" if fit.loglik is not None else "") + f"  ({time.time()-T0:.0f}s)")
    maxabs, worst = 0.0, ""
    est = {}
    for n, b, s in rows:
        t = TRUE[n]
        est[n] = b
        if abs(b - t) > maxabs:
            maxabs, worst = abs(b - t), n
        say(f"   {n:22s} 추정 {b:+.3f} (se {s:.3f})  정답 {t:+.3f}  차이 {b-t:+.3f}  승수비 {math.exp(b):.2f} vs {math.exp(t):.2f}")
    say(f"   최대 |차이| = {maxabs:.3f} ({worst}) ; 가격 탄력 {est['log_p']:.3f}")
    return est, maxabs


y_or = np.log(np.maximum(df.latent_T_days.to_numpy(), 0.5))
X, names = build(df)
est_or, max_or = report(ols(X, y_or, names), "(a) 오라클 OLS: 전체 행, y=log(잠재 T), 판매자 효과는 오차항")
Xs, names_s = build(df, seller_fe=True)
fit_or_s = ols(Xs, y_or, names_s)
est_or_s, max_or_s = report(fit_or_s, "(a') 오라클 OLS + 판매자 더미 (판매자 효과를 모형에 넣음)")
# 판매자 더미 vs 정답 u
u_true = {k: v["u"] for k, v in META["seller_latent"].items()}
ref_seller = sorted(df.seller_id.unique())[0]
est_u = {ref_seller: 0.0}
for n, b, _ in fit_or_s.table(("seller=",)):
    est_u[n.split("=", 1)[1]] = b
sellers_in = sorted(est_u)
a = np.array([est_u[s] - np.mean(list(est_u.values())) for s in sellers_in])
b = np.array([u_true[s] - np.mean([u_true[s2] for s2 in sellers_in]) for s in sellers_in])
n_per = df.seller_id.value_counts()
w = np.array([n_per[s] for s in sellers_in])
say(f"   판매자 더미(중심화) vs 정답 u(중심화): 판매자 {len(sellers_in)}명, 상관 {np.corrcoef(a, b)[0,1]:.3f}, RMSE {np.sqrt(np.mean((a-b)**2)):.3f} (정답 sd {b.std():.3f})"
    f" ; 거래 ≥20건 판매자만({(w>=20).sum()}명) 상관 {np.corrcoef(a[w>=20], b[w>=20])[0,1]:.3f}")
CHECK_A = max_or <= 0.05
say(f"  확인 (A) 오라클 최대 |차이| ≤ 0.05: {'PASS' if CHECK_A else 'FAIL'} (판매자 효과 오차항 {max_or:.3f} / 판매자 더미 {max_or_s:.3f})")
# 0.05 기준이 표본 노이즈(se ≈ 0.02~0.03) 수준이므로 다른 seed 3개로 같은 오라클 OLS 를 돌려 분포를 본다(생성 각 ~6초, 저장 안 함)
from src.data.synth import flatten, generate  # noqa: E402
seed_max = []
for k in (1, 2, 3):
    tb, mt = generate(P, seed=META["seed"] + k, n_products=10000)
    dk = flatten(tb, mt)
    Xk, nk = build(dk)
    fk = ols(Xk, np.log(np.maximum(dk.latent_T_days.to_numpy(), 0.5)), nk)
    diffs = {n: abs(fk.coef(n) - TRUE[n]) for n in TRUE}
    worst = max(diffs, key=diffs.get)
    Xk2, nk2 = build(dk, seller_fe=True)
    fk2 = ols(Xk2, np.log(np.maximum(dk.latent_T_days.to_numpy(), 0.5)), nk2)
    d2max = max(abs(fk2.coef(n) - TRUE[n]) for n in TRUE)
    seed_max.append((META["seed"] + k, diffs[worst], worst, d2max))
    say(f"   seed {META['seed']+k}: 오라클 최대 |차이| {diffs[worst]:.3f} ({worst}) / 판매자 더미 포함 {d2max:.3f}  ({time.time()-T0:.0f}s)")
say(f"   → seed 4개(본 seed 포함) 오라클 최대 |차이|: 판매자 효과 오차항 {[round(x, 3) for x in [max_or] + [m for _, m, _, _ in seed_max]]} / 판매자 더미 {[round(x, 3) for x in [max_or_s] + [m for _, _, _, m in seed_max]]}")

y_sold = np.log(np.maximum(sold.duration_days.to_numpy(), 0.5))
Xso, _ = build(sold)
fit_sold = ols(Xso, y_sold, names)
est_sold, max_sold = report(fit_sold, "(b) sold 만 OLS: y=log(duration) — 선택 편향 대조군")
fit_aft = lognormal_aft(X, tm_all, ev_all, names, init=fit_sold)
est_aft, max_aft = report(fit_aft, "(d) 로그정규 AFT: 전체 행, 중도절단 반영 (disposed 도 절단 처리)")
say(f"요약 — 최대 |차이| : 오라클 {max_or:.3f} / sold OLS {max_sold:.3f} / AFT {max_aft:.3f} ; 가격 탄력 {est_or['log_p']:.3f} / {est_sold['log_p']:.3f} / {est_aft['log_p']:.3f} (정답 1.0)"
    f" ; v1(D010) 은 0.170 / 0.365 / 0.290 ; 0.872 / 0.635 / 0.903")
say("   항별 |차이| (sold OLS → AFT): " + ", ".join(
    f"{grp} {max(abs(est_sold[n]-TRUE[n]) for n in TRUE if n.startswith(grp)):.2f}→{max(abs(est_aft[n]-TRUE[n]) for n in TRUE if n.startswith(grp)):.2f}"
    for grp in ("grade=", "ch=", "brand=", "season=", "log_p")))

# ---------------------------------------------------------------- 3. 권역 가짜 효과 (확인 B)
say("\n## 3. 권역(seller_region) 효과 — 확인 (B) v1 가짜 효과 소멸")
regions = ["seoul", "gyeonggi_incheon", "chungcheong", "gyeongsang", "jeolla", "gangwon", "jeju"]
for name, d in [("v1", v1), ("v2", df)]:
    ev, tm = (d.status == "sold").astype(int).to_numpy(), d.duration_days.to_numpy()
    meds = {}
    for r in regions:
        m = (d.seller_region == r).to_numpy()
        meds[r] = kaplan_meier(tm[m], ev[m]).median if m.sum() else None
    say(f"  {name} KM 중앙값(일) " + ", ".join(f"{r} {v if v is None else round(v):>3}(n={int((d.seller_region==r).sum())})" for r, v in meds.items())
        + f"  범위 {min(v for v in meds.values() if v):.0f}~{max(v for v in meds.values() if v):.0f}")
# 오라클 OLS 에 권역 더미 추가(소분류 FE 통제) — 정답 0
for name, d in [("v1", v1), ("v2", df)]:
    cats = {"grade": (d.condition_grade.to_numpy(), "S"), "ch": (d.channel.to_numpy(), "open_market"),
            "region": (d.seller_region.to_numpy(), "seoul")}
    if name == "v2":
        cats.update({"brand": (d.brand_tier.to_numpy(), "mid"), "season": (season_col(d), SEASON_REF)})
    Xr, nr = design_matrix(cats, {"log_p": np.log(d.latent_price_ratio_p.to_numpy() / M["price_ref_ratio"])}, fixed_effects=d.category_code.to_numpy())
    fr = ols(Xr, np.log(np.maximum(d.latent_T_days.to_numpy(), 0.5)), nr)
    rows = fr.table(("region=",))
    say(f"  {name} 오라클 OLS 권역 계수(정답 0, 기준 seoul): " + ", ".join(f"{n.split('=')[1]} {b:+.3f}±{s:.3f}" for n, b, s in rows)
        + f"  최대 |계수| {max(abs(b) for _, b, _ in rows):.3f} ; |t|>2 개수 {sum(abs(b/s) > 2 for _, b, s in rows)}")
    if name == "v2":
        CHECK_B_coef = max(abs(b) for _, b, _ in rows)
        # 판매자 랜덤효과는 권역과 독립이므로 v2 에서 남는 권역 계수는 판매자 u 의 권역 평균 ≈ 0 ± 0.25/√(권역 판매자 수)
        d2 = df.drop_duplicates("seller_id")
        say("  v2 권역별 판매자 u 평균(정답, 랜덤효과의 권역 평균): " + ", ".join(
            f"{r} {d2[d2.seller_region==r].latent_seller_u.mean():+.3f}(판매자 {int((d2.seller_region==r).sum())})" for r in regions))
say(f"  확인 (B): v1 KM 범위 39~69일(D010) 대비 v2 범위가 좁고, 소분류 통제 후 권역 계수 최대 |{CHECK_B_coef:.3f}| — "
    f"{'PASS' if CHECK_B_coef <= 0.10 else 'CHECK'} (판매자 랜덤효과의 권역 평균 수준이면 정상)")

# ---------------------------------------------------------------- 4. 시즌
say("\n## 4. 시즌 — 등록월 편향·시즌 상태별 KM 중앙값")
seasonal_leaf = df.groupby("category_code").season_state.transform(lambda s: (s != "neutral").any())
ds, dn = df[seasonal_leaf], df[~seasonal_leaf]
say(f"  시즌 품목 거래 {len(ds)} / 비시즌 {len(dn)} ; 시즌 상태 비율(시즌 품목): {ds.season_state.value_counts(normalize=True).round(3).to_dict()}")
say("  등록월 분포(%): 시즌 품목 " + " ".join(f"{m}:{100*np.mean(ds.listed_month==m):.0f}" for m in range(1, 13))
    + " / 비시즌 " + " ".join(f"{m}:{100*np.mean(dn.listed_month==m):.0f}" for m in range(1, 13)))
say(f"  균등 대조: v1(편향 없음) 시즌성 소분류 등록월 집중도(최대 월 비율) {pd.to_datetime(v1.listed_date).dt.month.value_counts(normalize=True).max():.3f} vs v2 시즌 품목 {ds.listed_month.value_counts(normalize=True).max():.3f}")
for st in ["in_season", "near_season", "neutral", "off_season"]:
    m = (df.season_state == st).to_numpy()
    km = kaplan_meier(tm_all[m], ev_all[m]).median
    say(f"  season={st:12s} n={int(m.sum()):5d} KM 중앙값 {km if km is None else round(km):>4} / 잠재 T 중앙값 {df[m].latent_T_days.median():5.1f} / "
        f"sold 중앙값 {sold[sold.season_state==st].duration_days.median():4.0f} / 정답 승수 {M['season'][st]:.2f}")
say("  → 조건 없는 KM 은 소분류 혼합(시즌 품목 = 패션·가전 등 느린 소분류)으로 승수 방향이 안 보일 수 있음; §2 의 소분류 FE 모형에서는 복원됨.")

# ---------------------------------------------------------------- 5. 판매자 랜덤효과 ↔ 기재 정확도
say("\n## 5. 판매자 랜덤효과 u ↔ 공급 이력 기재 정확도 (실데이터에서 쓸 수 있는 대리 피처)")
with gzip.open(ROOT / "data/synth/v2/seller.jsonl.gz", "rt", encoding="utf-8") as f:
    sellers = [json.loads(l) for l in f if not l.startswith("#")]
acc_share = {}
for s in sellers:
    hist = s["supply_history"]
    if hist:
        acc_share[s["seller_id"]] = np.mean([b["description_accuracy"] == "accurate" for b in hist])
d2 = df.drop_duplicates("seller_id").set_index("seller_id")
d2 = d2[d2.index.isin(acc_share)]
d2["acc_share"] = [acc_share[s] for s in d2.index]
d2["profile"] = [META["seller_latent"][s]["profile"] for s in d2.index]
say("  프로필별 판매자 수 / u 평균 / accurate 비율: " + ", ".join(
    f"{p} {int((d2.profile==p).sum())} / {d2[d2.profile==p].latent_seller_u.mean():+.3f} / {d2[d2.profile==p].acc_share.mean():.2f}" for p in ("fast", "mid", "slow")))
say(f"  판매자 단위 corr(u, accurate 비율) = {np.corrcoef(d2.latent_seller_u, d2.acc_share)[0,1]:+.3f} (n={len(d2)})")
# 거래 단위: accurate 비율을 피처로 넣은 AFT (판매자 u 를 모르는 실데이터 상황)
df["acc_share"] = df.seller_id.map(acc_share).fillna(df.seller_id.map(acc_share).mean())
Xa, na = design_matrix({"grade": (df.condition_grade.to_numpy(), "S"), "ch": (df.channel.to_numpy(), "open_market"),
                        "brand": (df.brand_tier.to_numpy(), "mid"), "season": (season_col(df), SEASON_REF)},
                       {"log_p": np.log(df.latent_price_ratio_p.to_numpy() / M["price_ref_ratio"]), "acc_share": df.acc_share.to_numpy()},
                       fixed_effects=df.category_code.to_numpy())
fa_or = ols(Xa, y_or, na)
fa_aft = lognormal_aft(Xa, tm_all, ev_all, na, init=fa_or)
say(f"  accurate 비율 계수(log T, 음수면 '정확한 판매자가 빨리 팖'): 오라클 OLS {fa_or.coef('acc_share'):+.3f}±{fa_or.se[na.index('acc_share')]:.3f} / AFT {fa_aft.coef('acc_share'):+.3f}±{fa_aft.se[na.index('acc_share')]:.3f}"
    f"  (판매자 u 의 sd {SIG_U} 중 설명되는 몫: 오라클 잔차 sd {fa_or.sigma:.3f} vs 판매자 더미 모형 {fit_or_s.sigma:.3f} vs 판매자 없음 {ols(X, y_or, names).sigma:.3f})")

# ---------------------------------------------------------------- 6. 브랜드
say("\n## 6. 브랜드 — 조건 없는 KM 중앙값 vs 정답 승수")
for b_ in ["major", "mid", "minor", "unbranded", "unknown"]:
    m = (df.brand_tier == b_).to_numpy()
    km = kaplan_meier(tm_all[m], ev_all[m]).median
    say(f"  brand={b_:10s} n={int(m.sum()):5d} KM 중앙값 {km if km is None else round(km):>4} / 잠재 T 중앙값 {df[m].latent_T_days.median():5.1f} / 정답 승수 {M['brand'][b_]:.2f} / 평균 p {df[m].latent_price_ratio_p.mean():.2f}")
say("  (브랜드는 시세 비율 r 과도 연결되어 있어(major r 0.85 … unbranded 0.55) p 를 통해 가격 승수와 얽힘 — §2 모형은 p 를 통제하므로 복원됨)")

# ---------------------------------------------------------------- 7. 결론
say("\n## 7. 확인 항목 결과")
say(f"  (A) 오라클 복원 최대 |차이| ≤ 0.05 : {'PASS' if CHECK_A else 'FAIL'} ({max_or:.3f}; 판매자 더미 포함 {max_or_s:.3f})")
say(f"  (B) 권역 가짜효과 소멸            : {'PASS' if CHECK_B_coef <= 0.10 else 'CHECK'} (소분류 통제 권역 계수 최대 |{CHECK_B_coef:.3f}|)")
say(f"  (C) 소분류 n ≥ 100                : {'PASS' if CHECK_C else 'FAIL'} (min {cnt.min()}, sold min {cnt_sold.min()})")
say(f"  AFT vs sold-only 최대 |차이|      : {max_aft:.3f} vs {max_sold:.3f} (v1 0.290 vs 0.365)")
say(f"  소요 {time.time()-T0:.0f}s")

(OUT / "synth_v2_check.txt").write_text("\n".join(LINES) + "\n", encoding="utf-8")

# ---------------------------------------------------------------- 차트 (dataviz 기본 팔레트, D010 과 동일)
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

PAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
plt.rcParams.update({"font.family": "DejaVu Sans", "axes.spines.top": False, "axes.spines.right": False,
                     "axes.grid": True, "grid.color": "#e6e6e3", "grid.linewidth": 0.6, "axes.edgecolor": "#52514e",
                     "axes.labelcolor": "#0b0b0b", "xtick.color": "#52514e", "ytick.color": "#52514e", "figure.facecolor": "#fcfcfb",
                     "axes.facecolor": "#fcfcfb", "font.size": 9})

# fig1: coefficient recovery (v2 all terms)
terms = [n for n in names if n in TRUE]
fig, ax = plt.subplots(figsize=(7.5, 6.2), dpi=150)
yy = np.arange(len(terms))
ax.scatter([TRUE[n] for n in terms], yy, marker="|", s=260, color="#0b0b0b", label="true log-multiplier", zorder=3)
for off, (lab, est, col) in enumerate([("oracle OLS on latent T", est_or, PAL[2]), ("sold-only OLS", est_sold, PAL[1]), ("lognormal AFT (censored)", est_aft, PAL[0])]):
    ax.scatter([est[n] for n in terms], yy + (off - 1) * 0.22, s=26, color=col, label=lab, zorder=4, edgecolor="#fcfcfb", linewidth=0.8)
ax.set_yticks(yy); ax.set_yticklabels([t.replace("ch=", "channel=").replace("log_p", "price elasticity (log p/0.8)") for t in terms])
ax.invert_yaxis()
ax.axvline(0, color="#52514e", lw=0.8); ax.set_xlabel("coefficient on log(duration)")
ax.set_title("Fig 1. v2 multiplier recovery (n=%d): true vs three estimators" % len(df), loc="left")
ax.legend(frameon=False, fontsize=8, loc="upper right")
fig.tight_layout(); fig.savefig(OUT / "fig1_coef_recovery_v2.png"); plt.close(fig)

# fig2: KM by season state within one leaf (건강기능식품 FD-HF-SUP, seasonality=holiday → 창 안에 in/near/off 세 상태가 다 있음; 의류는 off 가 없음)
fig, ax = plt.subplots(figsize=(7, 3.8), dpi=150)
sub = df[df.category_code == "FD-HF-SUP"]
for i, st in enumerate(["in_season", "near_season", "off_season"]):
    m = (sub.season_state == st).to_numpy()
    if m.sum() < 20:
        continue
    km = kaplan_meier(sub.duration_days.to_numpy()[m], (sub.status == "sold").astype(int).to_numpy()[m])
    t = np.concatenate([[0], km.times]); s = np.concatenate([[1], km.survival])
    ax.step(t, s, where="post", color=PAL[i], lw=2, label=f"{st} (n={int(m.sum())}, KM median {km.median:.0f}d, true ×{M['season'][st]:.1f})")
ax.axhline(0.5, color="#52514e", lw=0.8, ls=":")
ax.set_xlim(0, 270); ax.set_ylim(0, 1); ax.set_xlabel("days since listing"); ax.set_ylabel("P(still unsold)")
ax.set_title("Fig 2. KM survival by season state — one leaf (FD-HF-SUP, holiday-seasonal)", loc="left"); ax.legend(frameon=False, fontsize=8)
fig.tight_layout(); fig.savefig(OUT / "fig2_km_season_leaf.png"); plt.close(fig)

# fig3: seller effect recovery scatter
fig, ax = plt.subplots(figsize=(5.4, 5), dpi=150)
big = w >= 20
ax.scatter(b[~big], a[~big], s=14, color=PAL[1], alpha=0.6, label=f"sellers with <20 rows (n={int((~big).sum())})", edgecolor="#fcfcfb", linewidth=0.4)
ax.scatter(b[big], a[big], s=18, color=PAL[0], alpha=0.85, label=f"sellers with ≥20 rows (n={int(big.sum())})", edgecolor="#fcfcfb", linewidth=0.4)
lim = (-1.1, 1.1)
ax.plot(lim, lim, color="#52514e", lw=0.8, ls="--"); ax.set_xlim(lim); ax.set_ylim(lim)
ax.set_xlabel("true seller effect u (centered)"); ax.set_ylabel("estimated seller dummy, oracle OLS (centered)")
ax.set_title("Fig 3. Seller random effect recovery (r=%.2f)" % np.corrcoef(a, b)[0, 1], loc="left"); ax.legend(frameon=False, fontsize=8, loc="upper left")
fig.tight_layout(); fig.savefig(OUT / "fig3_seller_effect.png"); plt.close(fig)

# fig4: listing month — seasonal vs non-seasonal leaves
fig, ax = plt.subplots(figsize=(7, 3.4), dpi=150)
months = np.arange(1, 13)
hs = np.array([np.mean(ds.listed_month == m) for m in months]); hn = np.array([np.mean(dn.listed_month == m) for m in months])
ax.bar(months - 0.2, hs, width=0.38, color=PAL[0], label=f"seasonal leaves (n={len(ds)})")
ax.bar(months + 0.2, hn, width=0.38, color=PAL[1], label=f"non-seasonal leaves (n={len(dn)})")
ax.set_xticks(months); ax.set_xlabel("listing month (window = %s-02-21 … %s-11-17)" % (META["reference_date"][:4], META["reference_date"][:4])); ax.set_ylabel("share of rows")
ax.set_title("Fig 4. Listing month: pre-season bias on seasonal leaves", loc="left"); ax.legend(frameon=False, fontsize=8)
fig.tight_layout(); fig.savefig(OUT / "fig4_listing_month.png"); plt.close(fig)
print("charts saved", time.time() - T0)
