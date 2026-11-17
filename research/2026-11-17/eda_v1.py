"""D010 — 합성 데이터 v1 EDA (2026-11-17). 전부 합성 데이터.

실행: python research/2026-11-17/eda_v1.py  (저장소 루트에서)
출력: research/2026-11-17/eda_v1.txt, fig_*.png
입력: data/synth/v1_flat.csv (거래 1행 = 상품·판매자 피처 + 목표변수 + latent_* 정답), data/synth/params_v1.yaml (정답 승수)

절:
 1 분포(판매기간·잠재 T·가격·회수율)    2 이상치     3 상관(Spearman)
 4 승수 복원: (a) 잠재 T 오라클 OLS (b) sold 만 OLS (c) KM 중앙값 비 (d) 로그정규 AFT(중도절단 반영)
 5 등급 효과 — 가격 통제 전후            6 회수율·적자 조건 + 매입가 비율 반사실
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.eval.survival import design_matrix, kaplan_meier, lognormal_aft, ols  # noqa: E402

OUT = Path(__file__).resolve().parent
LINES: list[str] = []


def say(s: str = "") -> None:
    print(s)
    LINES.append(s)


def q(x, ps=(0.05, 0.25, 0.5, 0.75, 0.95)):
    x = np.asarray(x, dtype=float)
    return " ".join(f"p{int(p*100)}={np.quantile(x, p):.2f}" for p in ps)


df = pd.read_csv(ROOT / "data/synth/v1_flat.csv")
P = yaml.safe_load((ROOT / "data/synth/params_v1.yaml").read_text(encoding="utf-8"))
M = P["multipliers"]
TRUE_GRADE = {k: math.log(v) for k, v in M["grade"].items()}
TRUE_CH = {k: math.log(v) for k, v in M["channel"].items()}
TRUE_PRICE = float(M["price_elasticity"])
LEAF_SIGMA = {k: v["sigma"] for k, v in P["leaves"].items()}

sold = df[df.status == "sold"].copy()
purch = df[df.acquisition_type == "purchase"]
sold_p = sold[sold.acquisition_type == "purchase"]

say("# D010 합성 데이터 v1 EDA — 2026-11-17 (전부 합성 데이터, seed 20261113)")
say(f"거래 {len(df)} 행 / 상품 {df.product_id.nunique()} / 판매자 {df.seller_id.nunique()} / 열 {df.shape[1]}")
say(f"상태: {df.status.value_counts().to_dict()}  censored={df.censored.mean():.3f}")
say(f"결측: expiry_date {df.expiry_date.isna().mean():.3f}, sold_price(미판매) {df.sold_price_krw.isna().mean():.3f}, "
    f"recovery_rate(위탁) {df.recovery_rate.isna().mean():.3f}")

# ---------------------------------------------------------------- 1. 분포
say("\n## 1. 분포")
say(f"duration_days(sold, n={len(sold)}): {q(sold.duration_days)}  mean={sold.duration_days.mean():.1f}")
say(f"latent_T_days(전체):              {q(df.latent_T_days)}")
say(f"duration_days(censored):          {q(df[df.censored].duration_days)}")
say(f"log(duration) sold 표준편차 {np.log(np.maximum(sold.duration_days,0.5)).std():.3f} / log(latent T) 전체 {np.log(np.maximum(df.latent_T_days,0.5)).std():.3f}")
say("expiry_class 별 중앙값 — sold duration / 잠재 T / n(sold) / n(전체):")
for ec in ["short", "medium", "long", "none"]:
    a, b = sold[sold.expiry_class == ec], df[df.expiry_class == ec]
    say(f"  {ec:7s} {a.duration_days.median():6.1f} / {b.latent_T_days.median():6.1f} / {len(a):4d} / {len(b):4d}")
say("소분류별 n(전체) 최소/중앙/최대: "
    f"{df.category_code.value_counts().min()} / {df.category_code.value_counts().median():.0f} / {df.category_code.value_counts().max()}")
say(f"list_price_krw: {q(df.list_price_krw, (0.05,0.5,0.95))}  log sd={np.log(df.list_price_krw).std():.3f} (설계 0.5 공통)")
say(f"listed/list 비율: {q(df.listed_price_krw/df.list_price_krw, (0.05,0.5,0.95))}   buy/listed 비율(purchase): {q(purch.buy_price_krw/purch.listed_price_krw, (0.05,0.5,0.95))}")
say(f"latent_price_ratio_p(등록가/시세): {q(df.latent_price_ratio_p, (0.05,0.5,0.95))}  클립 하한(0.3) {np.mean(df.latent_price_ratio_p<=0.3):.3f} 상한(1.2) {np.mean(df.latent_price_ratio_p>=1.2):.3f}")
say(f"recovery_rate(sold·purchase, n={len(sold_p)}): {q(sold_p.recovery_rate)}  적자(<1) {np.mean(sold_p.recovery_rate<1):.3f}")
say(f"recovery_rate(partially_sold·purchase): {q(purch[purch.status=='partially_sold'].recovery_rate)}")
say("등급 비율: " + str((df.condition_grade.value_counts(normalize=True).round(3)).to_dict()))
say("채널 비율: " + str((df.channel.value_counts(normalize=True).round(3)).to_dict()))

# ---------------------------------------------------------------- 2. 이상치
say("\n## 2. 이상치·특이 행")
num_cols = ["duration_days", "latent_T_days", "list_price_krw", "listed_price_krw", "buy_price_krw", "quantity_on_hand",
            "listing_quantity", "seller_years", "recovery_rate", "elapsed_days", "unit_count"]
say("IQR 규칙(Q3+3·IQR 초과) 건수 — 로그정규 꼬리 확인용:")
for c in num_cols:
    x = df[c].dropna()
    q1, q3 = x.quantile([0.25, 0.75])
    hi = q3 + 3 * (q3 - q1)
    say(f"  {c:18s} 초과 {int((x>hi).sum()):4d} 건 (경계 {hi:,.1f}, 최대 {x.max():,.1f})")
say(f"duration_days==0 (등록 당일 판매): {int((sold.duration_days==0).sum())} 건 → log 변환 시 0.5 로 바닥")
say(f"sold 인데 duration > 270(관측창 상한): {int((sold.duration_days>270).sum())} 건 (정상: 0 이어야 함)")
say(f"latent_T ≥ 365: {int((df.latent_T_days>=365).sum())} 건, 최대 {df.latent_T_days.max():.0f} 일 — lognormal 꼬리, 모두 미판매·철회로 관측됨")
say(f"recovery_rate < 0: {int((df.recovery_rate<0).sum())} 건 (판매 매출 < 수수료+배송비), == -1 클립: {int((df.recovery_rate<=-1).sum())} 건")
say(f"수수료+배송비 > 매출(sold): {int(((sold.channel_fee_krw+sold.shipping_cost_krw)>(sold.sold_price_krw*sold.quantity_sold)).sum())} 건")
say(f"listed_price > list_price(정상가 초과 등록): {int((df.listed_price_krw>df.list_price_krw).sum())} 건")
say(f"buy_price > listed_price: {int((purch.buy_price_krw>purch.listed_price_krw).sum())} 건")
big = df[df.listing_quantity >= 200]
say(f"listing_quantity ≥ 200: {len(big)} 건 (size_class {big.size_class.value_counts().to_dict()}) — 소형 소모품 다량 재고")
say("판매자별 거래 수 상위 3: " + str(df.seller_id.value_counts().head(3).to_dict()))

# ---------------------------------------------------------------- 3. 상관
say("\n## 3. Spearman 상관 (sold 만, n=%d)" % len(sold))
corr_df = pd.DataFrame({
    "duration": sold.duration_days,
    "latent_T": sold.latent_T_days,
    "log_list_price": np.log(sold.list_price_krw),
    "p_ratio": sold.latent_price_ratio_p,
    "r_market": sold.latent_market_ratio_r,
    "listing_qty": sold.listing_quantity,
    "seller_years": sold.seller_years,
    "unit_count": sold.unit_count,
    "elapsed": sold.elapsed_days,
    "recovery": sold.recovery_rate,
})
C = corr_df.corr(method="spearman")
say(C.round(2).to_string())
say("주목: duration~latent_T %.2f (sold 는 T 그대로이므로 1에 가까워야 함), duration~elapsed %.2f (선택 편향: 경과일이 짧으면 짧은 T 만 sold 로 관측), "
    "duration~p_ratio %.2f (가격 탄력), recovery~duration %.2f" % (C.loc["duration","latent_T"], C.loc["duration","elapsed"], C.loc["duration","p_ratio"], C.loc["duration","recovery"]))

# 범주형 vs duration (sold 중앙값, KM 중앙값)
say("\n범주별 sold 중앙값 / KM 중앙값(전체, 중도절단 반영) / 잠재 T 중앙값 / n_sold / n_all:")
ev_all = (df.status == "sold").astype(int).to_numpy()
tm_all = df.duration_days.to_numpy()
def cat_table(col, order=None):
    levels = order or sorted(df[col].unique())
    for lv in levels:
        m = (df[col] == lv).to_numpy()
        km = kaplan_meier(tm_all[m], ev_all[m]).median
        s = sold[sold[col] == lv]
        say(f"  {col}={lv:16s} {s.duration_days.median():6.1f} / {km if km is None else round(km,1)!s:>6} / {df[m].latent_T_days.median():6.1f} / {len(s):4d} / {int(m.sum()):4d}")
cat_table("condition_grade", ["S", "A", "B", "C"])
cat_table("channel", ["open_market", "own_online", "offline_store", "secondhand_app", "b2b_wholesale", "live_commerce", "social_market"])
cat_table("brand_tier")
cat_table("seller_region")
cat_table("size_class", ["small", "medium", "large"])

# ---------------------------------------------------------------- 4. 승수 복원
say("\n## 4. 승수 복원 — log T ~ 소분류 FE + 등급 + 채널 + log(p/0.8)")
say("정답(log 승수): 등급 " + ", ".join(f"{k}={v:+.3f}" for k, v in TRUE_GRADE.items()) +
    " / 채널 " + ", ".join(f"{k}={v:+.3f}" for k, v in TRUE_CH.items()) + f" / 가격 탄력 {TRUE_PRICE:+.3f}")
say("오차 항은 소분류별 lognormal sigma(0.60~0.90)이므로 잔차 sd ≈ 0.7~0.8 이 정상.")

def build(d: pd.DataFrame):
    X, names = design_matrix(
        {"grade": (d.condition_grade.to_numpy(), "S"), "ch": (d.channel.to_numpy(), "open_market")},
        {"log_p": np.log(d.latent_price_ratio_p.to_numpy() / M["price_ref_ratio"])},
        fixed_effects=d.category_code.to_numpy(),
    )
    return X, names

def report(fit, label):
    rows = fit.table(("grade=", "ch=", "log_p"))
    say(f"[{label}] n={fit.n} sigma={fit.sigma:.3f}" + (f" loglik={fit.loglik:.1f}" if fit.loglik is not None else ""))
    maxabs = 0.0
    for n, b, s in rows:
        if n.startswith("grade="):
            t = TRUE_GRADE[n.split("=")[1]]
        elif n.startswith("ch="):
            t = TRUE_CH[n.split("=")[1]]
        else:
            t = TRUE_PRICE
        maxabs = max(maxabs, abs(b - t))
        say(f"   {n:20s} 추정 {b:+.3f} (se {s:.3f})  정답 {t:+.3f}  차이 {b-t:+.3f}  승수비 {math.exp(b):.2f} vs {math.exp(t):.2f}")
    say(f"   최대 |차이| = {maxabs:.3f}")
    return {n: b for n, b, _ in rows}

X_all, names = build(df)
fit_oracle = ols(X_all, np.log(np.maximum(df.latent_T_days.to_numpy(), 0.5)), names)
est_oracle = report(fit_oracle, "(a) 오라클: 전체 행, y=log(잠재 T)")

X_sold, _ = build(sold)
fit_sold = ols(X_sold, np.log(np.maximum(sold.duration_days.to_numpy(), 0.5)), names)
est_sold = report(fit_sold, "(b) sold 만 OLS: y=log(duration) — 선택 편향 기대")

say("(c) KM 중앙값 비(기준 S / open_market), 중도절단 반영, 공변량 통제 없음:")
km_ratio = {}
for col, ref, true in [("condition_grade", "S", M["grade"]), ("channel", "open_market", M["channel"])]:
    base = kaplan_meier(tm_all[(df[col] == ref).to_numpy()], ev_all[(df[col] == ref).to_numpy()]).median
    for lv in true:
        if lv == ref:
            continue
        m = (df[col] == lv).to_numpy()
        med = kaplan_meier(tm_all[m], ev_all[m]).median
        ratio = None if (med is None or base is None) else med / base
        km_ratio[f"{col}={lv}"] = ratio
        say(f"   {col}={lv:16s} KM중앙값 {med!s:>6} / 기준 {base!s:>5} = {('n/a' if ratio is None else f'{ratio:.2f}'):>5}  정답 승수 {true[lv]:.2f}")

ev_aft = ((df.status == "sold")).astype(int).to_numpy()   # disposed(3건) 도 T 미관측이므로 중도절단으로 취급
fit_aft = lognormal_aft(X_all, tm_all, ev_aft, names, init=fit_sold)
est_aft = report(fit_aft, "(d) 로그정규 AFT: 전체 행, 중도절단 반영 (disposed 3건도 절단 처리)")

say("요약 — 최대 |차이| : 오라클 %.3f / sold OLS %.3f / AFT %.3f  ; 가격 탄력 추정: %.3f / %.3f / %.3f (정답 1.0)" % (
    max(abs(est_oracle[n] - (TRUE_GRADE.get(n.split('=')[1], TRUE_CH.get(n.split('=')[1], TRUE_PRICE)) if '=' in n else TRUE_PRICE)) for n in est_oracle),
    max(abs(est_sold[n] - (TRUE_GRADE.get(n.split('=')[1], TRUE_CH.get(n.split('=')[1], TRUE_PRICE)) if '=' in n else TRUE_PRICE)) for n in est_sold),
    max(abs(est_aft[n] - (TRUE_GRADE.get(n.split('=')[1], TRUE_CH.get(n.split('=')[1], TRUE_PRICE)) if '=' in n else TRUE_PRICE)) for n in est_aft),
    est_oracle["log_p"], est_sold["log_p"], est_aft["log_p"]))

# ---------------------------------------------------------------- 5. 등급 효과 — 가격 통제 전후
say("\n## 5. 등급 효과 — 가격 통제 전후 (y=log duration, sold 만 / AFT 전체)")
say("설계상 C 등급은 등록가 계수 0.60 → p 가 낮아 가격 승수가 등급 승수(2.0)를 일부 상쇄. 통제 없이 보면 등급 효과가 작게 보여야 함.")
say("등급별 평균 p(등록가/시세): " + ", ".join(f"{g}={df[df.condition_grade==g].latent_price_ratio_p.mean():.3f}" for g in "SABC"))
specs = {
    "등급만": ({"grade": (None, "S")}, {}, False),
    "등급+소분류FE": ({"grade": (None, "S")}, {}, True),
    "등급+소분류FE+log p": ({"grade": (None, "S")}, {"log_p": None}, True),
    "등급+소분류FE+log p+채널": ({"grade": (None, "S"), "ch": (None, "open_market")}, {"log_p": None}, True),
}
say(f"{'모형':28s} {'방법':6s} " + " ".join(f"{g:>8s}" for g in "ABC") + "   (정답 A +0.095 B +0.336 C +0.693)")
for label, (cats, nums, fe) in specs.items():
    for method, d in [("sold", sold), ("AFT", df)]:
        cc = {}
        for k, (_, ref) in cats.items():
            cc[k] = (d.condition_grade.to_numpy() if k == "grade" else d.channel.to_numpy(), ref)
        nn = {"log_p": np.log(d.latent_price_ratio_p.to_numpy() / M["price_ref_ratio"])} if "log_p" in nums else None
        X, nm = design_matrix(cc, nn, fixed_effects=d.category_code.to_numpy() if fe else None)
        if method == "sold":
            f = ols(X, np.log(np.maximum(d.duration_days.to_numpy(), 0.5)), nm)
        else:
            f = lognormal_aft(X, d.duration_days.to_numpy(), (d.status == "sold").astype(int).to_numpy(), nm)
        say(f"{label:28s} {method:6s} " + " ".join(f"{f.coef(f'grade={g}'):+8.3f}" for g in "ABC"))

# ---------------------------------------------------------------- 6. 회수율
say("\n## 6. 회수율·적자 조건 (sold·purchase, n=%d)" % len(sold_p))
sp = sold_p.copy()
sp["rev"] = sp.sold_price_krw * sp.quantity_sold
sp["cost"] = sp.buy_price_krw * sp.listing_quantity
sp["fee_share"] = sp.channel_fee_krw / sp.rev
sp["ship_share"] = sp.shipping_cost_krw / sp.rev
sp["unit_price"] = sp.sold_price_krw
sp["loss"] = (sp.recovery_rate < 1).astype(int)
chk = ((sp.rev - sp.channel_fee_krw - sp.shipping_cost_krw) / sp.cost).round(4)
say(f"정의 재계산 일치: {np.mean(np.abs(chk - sp.recovery_rate) < 1e-3):.3f} (하한 −1 클립 {int((sp.recovery_rate<=-1).sum())} 건 제외하면 전부)")
say(f"매출 대비 수수료 비중 중앙값 {sp.fee_share.median():.3f}, 배송비 비중 중앙값 {sp.ship_share.median():.3f}, p90 {sp.ship_share.quantile(0.9):.3f}")
say("적자율·회수율 중앙값 — 조건별:")
def loss_table(col, order=None):
    levels = order or sorted(sp[col].unique())
    for lv in levels:
        s = sp[sp[col] == lv]
        say(f"  {col}={lv!s:16s} n={len(s):4d} 적자 {s.loss.mean():.3f} 회수율 중앙값 {s.recovery_rate.median():.2f} 배송비비중 {s.ship_share.median():.3f}")
loss_table("channel", ["open_market", "own_online", "offline_store", "secondhand_app", "b2b_wholesale", "live_commerce", "social_market"])
loss_table("size_class", ["small", "medium", "large"])
sp["unit_price_band"] = pd.cut(sp.unit_price, [0, 5000, 10000, 20000, 50000, 1e9], labels=["<5천", "5천~1만", "1만~2만", "2만~5만", ">5만"])
loss_table("unit_price_band", ["<5천", "5천~1만", "1만~2만", "2만~5만", ">5만"])
sp["qty_band"] = pd.cut(sp.quantity_sold, [0, 1, 3, 10, 1e9], labels=["1", "2~3", "4~10", ">10"])
loss_table("qty_band", ["1", "2~3", "4~10", ">10"])
# 배송비 1회당 단가 대비
sp["ship_per_unit_over_price"] = sp.shipping_cost_krw / sp.quantity_sold / sp.unit_price
say(f"적자 행의 (배송비/판매수량)/판매단가 중앙값 {sp[sp.loss==1].ship_per_unit_over_price.median():.2f} vs 흑자 {sp[sp.loss==0].ship_per_unit_over_price.median():.2f}")
say("적자 조건 요약: 배송비가 붙는 채널에서 판매단가 1만원 미만·소량(1~3개)·small/medium 일 때 집중. 수수료율(≤0.20)만으로는 적자가 나지 않음(매입가가 등록가의 0.35~0.60 이라 마진 ≥ 0.4).")
# 반사실: 매입가 비율 k 배
say("매입가 비율 반사실(매입가 × k → 회수율 ÷ k):")
for k in [1.0, 1.2, 1.4, 1.6, 1.8]:
    r = sp.recovery_rate / k
    say(f"  k={k:.1f} (buy/listed 중앙값 {(purch.buy_price_krw/purch.listed_price_krw).median()*k:.2f}) → 회수율 중앙값 {r.median():.2f}, p10 {r.quantile(0.1):.2f}, 적자 {np.mean(r<1):.3f}")
k_120 = sp.recovery_rate.median() / 1.2
k_100 = sp.recovery_rate.median() / 1.0
bl_med = (purch.buy_price_krw / purch.listed_price_krw).median()
say(f"회수율 중앙값 1.2 가 되는 k ≈ {k_120:.2f} (buy/listed 중앙값 ≈ {bl_med*k_120:.2f}), 1.0 이 되는 k ≈ {k_100:.2f} (buy/listed ≈ {bl_med*k_100:.2f})")

# ---------------------------------------------------------------- 차트
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

PAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
plt.rcParams.update({"font.family": "DejaVu Sans", "axes.spines.top": False, "axes.spines.right": False,
                     "axes.grid": True, "grid.color": "#e6e6e3", "grid.linewidth": 0.6, "axes.edgecolor": "#52514e",
                     "axes.labelcolor": "#0b0b0b", "xtick.color": "#52514e", "ytick.color": "#52514e", "figure.facecolor": "#fcfcfb",
                     "axes.facecolor": "#fcfcfb", "font.size": 9})

# fig1: 분포 — log duration(sold) vs log latent T(all)
fig, ax = plt.subplots(figsize=(7, 3.6), dpi=150)
bins = np.linspace(0, 7, 36)
ax.hist(np.log(np.maximum(df.latent_T_days, 0.5)), bins=bins, color=PAL[0], alpha=0.85, label=f"latent T, all rows (n={len(df)})")
ax.hist(np.log(np.maximum(sold.duration_days, 0.5)), bins=bins, color=PAL[1], alpha=0.75, label=f"observed duration, sold only (n={len(sold)})")
ax.axvline(np.log(270), color="#52514e", lw=1, ls="--")
ax.text(np.log(270), ax.get_ylim()[1]*0.95, "listing window 270d", ha="right", va="top", fontsize=8, color="#52514e")
ax.set_xlabel("log(days)"); ax.set_ylabel("rows"); ax.set_title("Fig 1. Sale duration: latent vs observed (synthetic v1)", loc="left")
ax.legend(frameon=False)
fig.tight_layout(); fig.savefig(OUT / "fig1_duration_dist.png"); plt.close(fig)

# fig2: KM by grade
fig, ax = plt.subplots(figsize=(7, 3.8), dpi=150)
for i, g in enumerate("SABC"):
    m = (df.condition_grade == g).to_numpy()
    km = kaplan_meier(tm_all[m], ev_all[m])
    t = np.concatenate([[0], km.times]); s = np.concatenate([[1], km.survival])
    ax.step(t, s, where="post", color=PAL[i], lw=2, label=f"grade {g} (n={int(m.sum())}, KM median {km.median:.0f}d)")
ax.axhline(0.5, color="#52514e", lw=0.8, ls=":")
ax.set_xlim(0, 270); ax.set_ylim(0, 1); ax.set_xlabel("days since listing"); ax.set_ylabel("P(still unsold)")
ax.set_title("Fig 2. Kaplan-Meier unsold curves by condition grade (censoring-aware)", loc="left"); ax.legend(frameon=False, fontsize=8)
fig.tight_layout(); fig.savefig(OUT / "fig2_km_grade.png"); plt.close(fig)

# fig3: coefficient recovery dot plot
terms = [n for n in names if n.startswith(("grade=", "ch=", "log_p"))]
truth = [TRUE_GRADE[n.split("=")[1]] if n.startswith("grade=") else TRUE_CH[n.split("=")[1]] if n.startswith("ch=") else TRUE_PRICE for n in terms]
fig, ax = plt.subplots(figsize=(7.5, 4.2), dpi=150)
y = np.arange(len(terms))
ax.scatter(truth, y, marker="|", s=260, color="#0b0b0b", label="true log-multiplier", zorder=3)
for off, (lab, est, col) in enumerate([("oracle OLS on latent T", est_oracle, PAL[2]), ("sold-only OLS", est_sold, PAL[1]), ("lognormal AFT (censored)", est_aft, PAL[0])]):
    ax.scatter([est[n] for n in terms], y + (off - 1) * 0.22, s=28, color=col, label=lab, zorder=4, edgecolor="#fcfcfb", linewidth=0.8)
ax.set_yticks(y); ax.set_yticklabels([t.replace("ch=", "channel=").replace("log_p", "price elasticity (log p/0.8)") for t in terms])
ax.axvline(0, color="#52514e", lw=0.8); ax.set_xlabel("coefficient on log(duration)")
ax.set_title("Fig 3. Multiplier recovery: true vs three estimators", loc="left"); ax.legend(frameon=False, fontsize=8, loc="upper left")
fig.tight_layout(); fig.savefig(OUT / "fig3_coef_recovery.png"); plt.close(fig)

# fig4: recovery distribution + loss share by channel
fig, (a1, a2) = plt.subplots(1, 2, figsize=(9, 3.6), dpi=150, gridspec_kw={"width_ratios": [1.1, 1]})
a1.hist(sp.recovery_rate, bins=np.linspace(-1, 3, 41), color=PAL[0])
a1.axvline(1.0, color=PAL[7], lw=1.5); a1.text(1.02, a1.get_ylim()[1]*0.95, "break-even", color=PAL[7], va="top", fontsize=8)
a1.set_xlabel("recovery rate (sold, purchase)"); a1.set_ylabel("rows"); a1.set_title("Fig 4a. Recovery rate", loc="left")
chs = ["open_market", "own_online", "offline_store", "secondhand_app", "b2b_wholesale", "live_commerce", "social_market"]
ls_ = [sp[sp.channel == c].loss.mean() for c in chs]
ns_ = [int((sp.channel == c).sum()) for c in chs]
a2.barh(np.arange(len(chs)), ls_, color=PAL[0], height=0.6)
for i, (v, n) in enumerate(zip(ls_, ns_)):
    a2.text(v + 0.005, i, f"{v:.0%} (n={n})", va="center", fontsize=8, color="#52514e")
a2.set_yticks(np.arange(len(chs))); a2.set_yticklabels(chs); a2.invert_yaxis(); a2.set_xlim(0, max(ls_) * 1.6 + 0.02)
a2.set_xlabel("share of sales with recovery < 1"); a2.set_title("Fig 4b. Loss share by channel", loc="left")
fig.tight_layout(); fig.savefig(OUT / "fig4_recovery.png"); plt.close(fig)

# fig5: spearman heatmap (diverging blue-gray-orange)
from matplotlib.colors import LinearSegmentedColormap
cmap = LinearSegmentedColormap.from_list("div", ["#2a78d6", "#f0efec", "#eb6834"])
fig, ax = plt.subplots(figsize=(6.4, 5.4), dpi=150)
im = ax.imshow(C.values, cmap=cmap, vmin=-1, vmax=1)
ax.set_xticks(range(len(C))); ax.set_xticklabels(C.columns, rotation=45, ha="right"); ax.set_yticks(range(len(C))); ax.set_yticklabels(C.index)
for i in range(len(C)):
    for j in range(len(C)):
        ax.text(j, i, f"{C.values[i,j]:.2f}", ha="center", va="center", fontsize=7, color="#0b0b0b")
ax.grid(False); fig.colorbar(im, ax=ax, shrink=0.8, label="Spearman rho")
ax.set_title("Fig 5. Spearman correlation (sold rows)", loc="left")
fig.tight_layout(); fig.savefig(OUT / "fig5_spearman.png"); plt.close(fig)

say("\n차트: fig1_duration_dist.png, fig2_km_grade.png, fig3_coef_recovery.png, fig4_recovery.png, fig5_spearman.png")
(OUT / "eda_v1.txt").write_text("\n".join(LINES) + "\n", encoding="utf-8")
