"""D014 — 시간 기준 홀드아웃 분할 전략을 합성 v2 + D012 v0 점수로 점검한다 (2026-11-23). 전부 합성 데이터.

실행: python research/2026-11-23/split_check.py   (저장소 루트에서, 약 1~2분)
입력: data/synth/v2_flat.csv.gz, research/2026-11-19/scores_v0_v2.csv.gz (등록 시점 v0 점수)
출력: research/2026-11-23/split_check.txt, fig1_rolling_folds.png, fig2_min_followup.png

점검 항목(split_strategy.md 의 번호와 같음):
  A. 롤링 cutoff(05-01~09-01) 확장 창: train 라벨을 cutoff 에서 as-of 절단한 경우 vs 미래 결과를 그대로 쓴 경우(라벨 누출 대조군)
     — AFT 보정 계수·σ 의 이동과 test 지표(±20%·절단적중·C) 차이.
  B. 최소 추적 기간 H(0/30/60/90일): test 크기·절단율·지표가 어떻게 달라지는지 (cutoff 07-01).
  C. 행 부트스트랩 vs 판매자 클러스터 부트스트랩 CI 폭 (cutoff 07-01).
  D. v2_flat 열에 대한 피처 금지 목록 적용 결과.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.eval import metrics as M  # noqa: E402
from src.eval import split as S  # noqa: E402
from src.eval.survival import kaplan_meier, lognormal_aft  # noqa: E402

OUT = Path(__file__).resolve().parent
LINES: list[str] = []
T0 = time.time()
C = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]  # dataviz 기본 범주 팔레트(D010~D013 과 동일)
N_BOOT = 200


def say(s: str = "") -> None:
    print(s)
    LINES.append(s)


def fmt(r: dict) -> str:
    return (f"n {r['n']:5d} (절단율 {r['censor_rate']:.3f}) | MdAPE {r['median_ape']:5.1f}% ±20% {r['within_pct']:.3f} "
            f"logbias {r['mean_log_ratio']:+.3f} | 절단적중 {r['censored_hit']:.3f} 합산 {r['hit_all']:.3f} | C {r['c_index']:.3f}")


# ---------------------------------------------------------------- 0. 적재
flat = pd.read_csv(ROOT / "data/synth/v2_flat.csv.gz")
sc = pd.read_csv(ROOT / "research/2026-11-19/scores_v0_v2.csv.gz")
sc = sc[sc.eval_mode == "as_of_listing"]
df = flat.merge(sc[["transaction_id", "total"]], on="transaction_id", how="inner")
df["listed_date"] = pd.to_datetime(df.listed_date)
df["observed_at"] = pd.to_datetime(df.observed_at)
OBS_MAX = df.observed_at.max()

say("# D014 시간 기준 분할 점검 — 2026-11-23 (전부 합성 데이터 v2 seed 20261118 + D012 등록 시점 v0 점수)")
say(f"거래 {len(df)} / 등록일 {df.listed_date.min().date()} ~ {df.listed_date.max().date()} / 관측 기준일 {OBS_MAX.date()} / 판매자 {df.seller_id.nunique()}")

# ---------------------------------------------------------------- D. 피처 금지 목록
say("\n## D. 피처 금지 목록 적용 (v2_flat 54열 + total)")
bad = S.forbidden_columns(df.columns)
say(f"  금지 {len(bad)}열: {bad}")
ok = [c for c in df.columns if c not in bad]
say(f"  허용 {len(ok)}열: {ok}")
say("  주의: 허용 열 중 `buy_price_krw`·`buy_date`·`acquisition_type` 은 매입 '후' 정보라 매입가 산정(과제 2) 모델에선 타깃이고, "
    "판매기간 모델(과제 1)에선 등록 시점에 알 수 있으므로 허용. `channel`·`listed_price_krw` 는 운영자 결정이라 추천 모델에선 타깃.")


# ---------------------------------------------------------------- 예측기 (train 전용 보정)
def fit_predictors(train: pd.DataFrame) -> dict:
    """train(time/event 열)으로 상수·소분류 KM 중앙값·v0 AFT 보정을 만든다."""
    km = kaplan_meier(train.time, train.event)
    gmed = km.median if km.median is not None else float(train.time.max())
    sub = S.train_group_stat(train, "category_code", lambda g: kaplan_meier(g.time, g.event).median, default=gmed, min_n=20)
    X = np.column_stack([np.ones(len(train)), train.total.to_numpy() / 10.0])
    aft = lognormal_aft(X, train.time.to_numpy(), train.event.to_numpy(), ["intercept", "total/10"])
    return {"const": gmed, "sub": sub, "aft": aft}


def predict(p: dict, test: pd.DataFrame) -> dict[str, np.ndarray]:
    a, b = p["aft"].beta
    return {
        "상수(train KM)": np.full(len(test), p["const"]),
        "소분류 KM 중앙값": test.category_code.map(p["sub"]).fillna(p["const"]).to_numpy(),
        "v0 AFT 보정": np.exp(a + b * test.total.to_numpy() / 10.0),
    }


# ---------------------------------------------------------------- A. 롤링 cutoff, as-of vs 누출
say("\n## A. 롤링 cutoff 확장 창 — train 라벨 as-of 절단 vs 미래 결과 사용(누출 대조군), H=0")
cutoffs = S.monthly_cutoffs("2026-05-01", "2026-09-01")
rowsA = []
for cut in cutoffs:
    for mode, asof in (("as-of", True), ("누출", False)):
        sp = S.time_split(df, cut, asof_train=asof)
        p = fit_predictors(sp.train)
        a, b = p["aft"].beta
        preds = predict(p, sp.test)
        r_aft = M.duration_eval(preds["v0 AFT 보정"], sp.test.time, sp.test.event)
        r_sub = M.duration_eval(preds["소분류 KM 중앙값"], sp.test.time, sp.test.event)
        rowsA.append({"cutoff": cut.strftime("%m-%d"), "mode": mode, "n_train": len(sp.train), "train_cens": 1 - sp.train.event.mean(),
                      "asof_n": sp.n_train_asof_censored, "n_test": len(sp.test), "test_cens": 1 - sp.test.event.mean(),
                      "a": a, "b": b, "sigma": p["aft"].sigma, "T70": float(np.exp(a + b * 7)), "const": p["const"],
                      "aft_w20": r_aft["within_pct"], "aft_ch": r_aft["censored_hit"], "aft_c": r_aft["c_index"], "aft_lb": r_aft["mean_log_ratio"],
                      "sub_w20": r_sub["within_pct"], "sub_ch": r_sub["censored_hit"], "sub_c": r_sub["c_index"], "sub_lb": r_sub["mean_log_ratio"]})
A = pd.DataFrame(rowsA)
say("  cutoff mode   n_train 절단율 asof절단 | n_test 절단율 | AFT a      b     σ    T̂(70) KM중앙 | v0: ±20%  절단적중  C    logbias | 소분류KM: ±20% 절단적중  C")
for _, r in A.iterrows():
    say(f"  {r.cutoff}  {r['mode']:5s} {r.n_train:6d}  {r.train_cens:.3f}  {r.asof_n:5d}   | {r.n_test:5d}  {r.test_cens:.3f} | "
        f"{r.a:6.3f} {r.b:+.3f} {r.sigma:.3f} {r.T70:5.0f}일 {r.const:4.0f}일 | "
        f"{r.aft_w20:.3f}  {r.aft_ch:.3f}  {r.aft_c:.3f} {r.aft_lb:+.2f} | {r.sub_w20:.3f}  {r.sub_ch:.3f}  {r.sub_c:.3f}")
asof = A[A["mode"] == "as-of"].set_index("cutoff")
leak = A[A["mode"] == "누출"].set_index("cutoff")
say(f"  as-of 절단으로 train 절단율은 평균 {asof.train_cens.mean():.3f} (누출 대조군 {leak.train_cens.mean():.3f}); "
    f"AFT 기울기 b 범위 as-of {asof.b.min():+.3f}~{asof.b.max():+.3f} / 누출 {leak.b.min():+.3f}~{leak.b.max():+.3f}")
say(f"  T̂(총점 70) as-of {asof.T70.min():.0f}~{asof.T70.max():.0f}일 / 누출 {leak.T70.min():.0f}~{leak.T70.max():.0f}일 — 분할마다 수준이 움직임(시즌 구성)")
say(f"  test C: as-of 평균 {asof.aft_c.mean():.3f} / 누출 {leak.aft_c.mean():.3f} ; ±20%: {asof.aft_w20.mean():.3f} / {leak.aft_w20.mean():.3f} ; "
    f"절단적중 {asof.aft_ch.mean():.3f} / {leak.aft_ch.mean():.3f}")
say("  (해석: 합성 v2 는 시간에 따라 정답 구조가 바뀌지 않으므로 누출 대조군이 크게 이득을 보지 않는 것이 정상. "
    "as-of 절단은 정보가 줄어도 AFT 가 불편추정이라 계수가 비슷해야 하고, 이것이 확인 포인트)")

# ---------------------------------------------------------------- B. 최소 추적 기간 H
say("\n## B. 최소 추적 기간 H — cutoff 2026-07-01, test 는 등록일 ≤ 관측기준일 − H")
rowsB = []
sp_base = None
for H in (0, 30, 60, 90, 120):
    sp = S.time_split(df, "2026-07-01", min_followup_days=H)
    if H == 0:
        sp_base = sp
    p = fit_predictors(sp.train)
    preds = predict(p, sp.test)
    cz = sp.test[sp.test.event == 0]
    comp = ", ".join(f"{k} {v}" for k, v in cz.status.value_counts().items())
    say(f"  H={H:3d}: test n {len(sp.test):5d} (제외 {sp.n_test_dropped_short:4d}) 절단율 {1 - sp.test.event.mean():.3f} "
        f"등록 {sp.test.listed_date.min().date()}~{sp.test.listed_date.max().date()} | 절단 구성: {comp} ; "
        f"절단 행 관측일 중앙값 {cz.time.median():.0f}일 (sold 행 판매기간 중앙값 {sp.test[sp.test.event == 1].time.median():.0f}일)")
    for name, pr in preds.items():
        r = M.duration_eval(pr, sp.test.time, sp.test.event)
        rowsB.append({"H": H, "pred": name, **r})
        say(f"         {name:12s} {fmt(r)}")
B = pd.DataFrame(rowsB)
b_aft = B[B.pred == "v0 AFT 보정"].set_index("H")
say(f"  v0 AFT: H 0→90 에서 절단율 {b_aft.loc[0, 'censor_rate']:.3f}→{b_aft.loc[90, 'censor_rate']:.3f}, "
    f"±20% {b_aft.loc[0, 'within_pct']:.3f}→{b_aft.loc[90, 'within_pct']:.3f}, 절단적중 {b_aft.loc[0, 'censored_hit']:.3f}→{b_aft.loc[90, 'censored_hit']:.3f}, "
    f"C {b_aft.loc[0, 'c_index']:.3f}→{b_aft.loc[90, 'c_index']:.3f}, logbias {b_aft.loc[0, 'mean_log_ratio']:+.2f}→{b_aft.loc[90, 'mean_log_ratio']:+.2f}")
say("  (해석: H 를 늘리면 짧게 관측된 최근 등록분이 빠져 절단율이 내려가고 sold 행의 선택 편향(짧은 것만 팔림)이 줄어 log편향이 0 쪽으로 움직여야 함. "
    "대신 test 가 작아지고 평가가 '오래된' 등록분으로 치우침)")

# ---------------------------------------------------------------- C. 행 vs 클러스터 부트스트랩
say(f"\n## C. 신뢰구간 — 행 부트스트랩 vs 판매자 클러스터 부트스트랩 (cutoff 07-01, H=0, {N_BOOT}회)")
sp = sp_base
p = fit_predictors(sp.train)
preds = predict(p, sp.test)
te = sp.test
say(f"  test 판매자 {te.seller_id.nunique()}명, 판매자당 거래 중앙값 {te.groupby('seller_id').size().median():.0f} 최대 {te.groupby('seller_id').size().max()}")
sold = te.event.to_numpy() == 1


def w20(t, pr):
    return M.within_pct(t, pr, 0.20)


def cidx(t, e, pr):
    return M.concordance_index(t, e, -pr)[0]


def chit(pr, t, e):
    return M.censored_hit_rate(pr, t, e)


rowsC = []
for name in ("소분류 KM 중앙값", "v0 AFT 보정"):
    pr = preds[name]
    for metric_name, fn, arrs, groups in (
        ("±20%(sold)", w20, (te.time.to_numpy()[sold], pr[sold]), te.seller_id.to_numpy()[sold]),
        ("절단적중", chit, (pr, te.time.to_numpy(), te.event.to_numpy()), te.seller_id.to_numpy()),
        ("C-index", cidx, (te.time.to_numpy(), te.event.to_numpy(), pr), te.seller_id.to_numpy()),
    ):
        pt, lo_r, hi_r = M.bootstrap_ci(fn, *arrs, n_boot=N_BOOT, seed=0)
        _, lo_c, hi_c = S.cluster_bootstrap_ci(fn, groups, *arrs, n_boot=N_BOOT, seed=0)
        ratio = (hi_c - lo_c) / (hi_r - lo_r) if hi_r > lo_r else float("nan")
        rowsC.append({"pred": name, "metric": metric_name, "point": pt, "row": (lo_r, hi_r), "cluster": (lo_c, hi_c), "ratio": ratio})
        say(f"  {name:12s} {metric_name:10s} {pt:.3f}  행 [{lo_r:.3f},{hi_r:.3f}] 폭 {hi_r - lo_r:.3f}  "
            f"클러스터 [{lo_c:.3f},{hi_c:.3f}] 폭 {hi_c - lo_c:.3f}  비 {ratio:.2f}")
ratios = [r["ratio"] for r in rowsC]
say(f"  클러스터/행 CI 폭 비: 최소 {min(ratios):.2f} 최대 {max(ratios):.2f} 평균 {np.mean(ratios):.2f}")
say("  (해석: 합성 v2 의 판매자 랜덤효과(σ_u 작음)만큼 넓어진다. 실데이터에서 판매자 묶임이 크면 비가 더 커질 것. 규약은 클러스터 CI 보고)")

# ---------------------------------------------------------------- E. 규약 후보 기준값 (H=60, 롤링 4 fold)
say("\n## E. 규약 후보: train as-of 절단 + H=60, cutoff 05-01/06-01/07-01/08-01 — D021 이후 모델이 넘어야 할 기준값")
rowsE = []
for cut in S.monthly_cutoffs("2026-05-01", "2026-08-01"):
    sp = S.time_split(df, cut, min_followup_days=60)
    p = fit_predictors(sp.train)
    preds = predict(p, sp.test)
    for name, pr in preds.items():
        r = M.duration_eval(pr, sp.test.time, sp.test.event)
        rowsE.append({"cutoff": cut.strftime("%m-%d"), "pred": name, "n_train": len(sp.train), **r})
E = pd.DataFrame(rowsE)
for cut, g in E.groupby("cutoff", sort=True):
    g = g.set_index("pred")
    r0 = g.iloc[0]
    say(f"  cutoff {cut}: train {int(r0.n_train):5d} / test {int(r0.n):5d} (절단율 {r0.censor_rate:.3f}) | "
        + " | ".join(f"{name}: ±20% {g.loc[name, 'within_pct']:.3f} 절단적중 {g.loc[name, 'censored_hit']:.3f} C {g.loc[name, 'c_index']:.3f}"
                     for name in ("소분류 KM 중앙값", "v0 AFT 보정")))
agg = E.groupby("pred")[["within_pct", "censored_hit", "c_index", "mean_log_ratio"]].agg(["mean", "min", "max"])
for name in ("상수(train KM)", "소분류 KM 중앙값", "v0 AFT 보정"):
    a_ = agg.loc[name]
    say(f"  4 fold 평균[범위] {name:12s}: ±20% {a_[('within_pct', 'mean')]:.3f} [{a_[('within_pct', 'min')]:.3f},{a_[('within_pct', 'max')]:.3f}] "
        f"절단적중 {a_[('censored_hit', 'mean')]:.3f} [{a_[('censored_hit', 'min')]:.3f},{a_[('censored_hit', 'max')]:.3f}] "
        f"C {a_[('c_index', 'mean')]:.3f} [{a_[('c_index', 'min')]:.3f},{a_[('c_index', 'max')]:.3f}] logbias {a_[('mean_log_ratio', 'mean')]:+.2f}")

# ---------------------------------------------------------------- 그림
fig, axes = plt.subplots(1, 3, figsize=(13, 4), dpi=130)
x = np.arange(len(cutoffs))
labels = [c.strftime("%m-%d") for c in cutoffs]
for ax, (col, title) in zip(axes, (("aft_c", "C-index (test)"), ("aft_w20", "sold ±20% 적중률 (test)"), ("T70", "AFT 보정: 총점 70 → T̂ (일)"))):
    ax.plot(x, asof[col].to_numpy(), "o-", color=C[0], label="train as-of 절단 (규약)")
    ax.plot(x, leak[col].to_numpy(), "s--", color=C[1], label="train 미래 결과 사용 (누출)")
    if col != "T70":
        ax.plot(x, asof["sub_" + col.split("_")[1]].to_numpy(), "^:", color=C[2], label="소분류 KM 중앙값 (as-of)")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_title(title, fontsize=10)
    ax.grid(alpha=0.3)
axes[0].set_xlabel("cutoff (2026)")
axes[0].legend(fontsize=8, loc="lower left")
fig.suptitle("D014 Fig 1 — rolling cutoff, expanding window (synthetic v2, v0 score calibrated on train)", fontsize=10)
fig.tight_layout()
fig.savefig(OUT / "fig1_rolling_folds.png")
plt.close(fig)

fig, axes = plt.subplots(1, 3, figsize=(13, 4), dpi=130)
Hs = sorted(B.H.unique())
for ax, (col, title) in zip(axes, (("within_pct", "sold ±20% 적중률"), ("censored_hit", "절단 행 T̂>관측일 적중"), ("mean_log_ratio", "log 편향 (sold, +=과대)"))):
    for i, name in enumerate(("상수(train KM)", "소분류 KM 중앙값", "v0 AFT 보정")):
        sub = B[B.pred == name].set_index("H").loc[Hs]
        ax.plot(Hs, sub[col].to_numpy(), "o-", color=C[i], label=name)
    ax.set_xlabel("min follow-up H (days)")
    ax.set_title(title, fontsize=10)
    ax.grid(alpha=0.3)
ax2 = axes[0].twinx()
ns = [B[(B.H == h) & (B.pred == "v0 AFT 보정")].n.iloc[0] for h in Hs]
ax2.bar(Hs, ns, width=12, alpha=0.15, color=C[3])
ax2.set_ylabel("test n", fontsize=8)
axes[0].legend(fontsize=8)
fig.suptitle("D014 Fig 2 — minimum follow-up rule, cutoff 2026-07-01 (synthetic v2)", fontsize=10)
fig.tight_layout()
fig.savefig(OUT / "fig2_min_followup.png")
plt.close(fig)

say(f"\n소요 {time.time() - T0:.0f}초. 그림: fig1_rolling_folds.png, fig2_min_followup.png")
(OUT / "split_check.txt").write_text("\n".join(LINES) + "\n", encoding="utf-8")
