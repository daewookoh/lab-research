"""D013 — 평가 지표를 D012 v0 점수(합성 v2)에 적용해 기준값을 등록한다 (2026-11-20). 전부 합성 데이터.

실행: python research/2026-11-20/metrics_check.py   (저장소 루트에서, 약 1분)
입력: data/synth/v2_flat.csv.gz (거래·정답 latent_* 열), research/2026-11-19/scores_v0_v2.csv.gz (등록 시점 v0 점수)
출력: research/2026-11-20/metrics_check.txt, fig1_hit_rates.png, fig2_calibration.png

설계(metrics.md §7):
  - 시간 기준 분할 미리보기: 등록일 < 2026-07-01 → 보정(train), ≥ 2026-07-01 → 평가(test). D014 의 본 분할은 아니다.
  - v0 총점은 일수 예측이 아니므로 train 에서 로그정규 AFT(log T = a + b·총점)로 보정해 중앙값 예측 T̂ 를 만든다.
  - 대조 예측기: 상수(train KM 중앙값) / 소분류 KM 중앙값 / 오라클 승수(소분류 기본 중앙값 × 정답 승수 — 노이즈만 모르는 천장).
  - 등급 F1 은 D024 전이라 **임시 라벨**(관측 결과: fast ≤30일 / mid 31~60 / slow 61~90 / stale >90, 90일 미만 절단은 제외)로 계산.
  - 채널 Top-1 은 v0 추천 vs 실제 채널(합성기는 추천을 쓰지 않으므로 우연 수준이 정상).
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
from src.eval.survival import kaplan_meier, lognormal_aft  # noqa: E402

OUT = Path(__file__).resolve().parent
LINES: list[str] = []
T0 = time.time()
C = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]  # dataviz 기본 범주 팔레트(검증됨, D010~D012 와 동일)
SPLIT = "2026-07-01"
N_BOOT = 200


def say(s: str = "") -> None:
    print(s)
    LINES.append(s)


def fmt(r: dict) -> str:
    return (f"n {r['n']:5d} (sold {r['n_sold']:4d} / 절단 {r['n_censored']:4d}, 절단율 {r['censor_rate']:.3f}) | "
            f"MAPE {r['mape']:6.1f}% MdAPE {r['median_ape']:5.1f}% ±20% {r['within_pct']:.3f} ±20%|7일 {r['within_pct_7']:.3f} "
            f"logbias {r['mean_log_ratio']:+.3f} RMSLE {r['rmsle']:.3f} | 절단적중 {r['censored_hit']:.3f} 합산 {r['hit_all']:.3f} | C {r['c_index']:.3f}")


# ---------------------------------------------------------------- 0. 적재·분할
flat = pd.read_csv(ROOT / "data/synth/v2_flat.csv.gz")
sc = pd.read_csv(ROOT / "research/2026-11-19/scores_v0_v2.csv.gz")
sc = sc[sc.eval_mode == "as_of_listing"]
df = flat.merge(sc[["transaction_id", "total", "grade_v0", "recommended_channel", "d_ref"]], on="transaction_id", how="inner")
df["event"] = (df.status == "sold").astype(int)
df["time"] = df.duration_days.astype(float)
train = df[df.listed_date < SPLIT].copy()
test = df[df.listed_date >= SPLIT].copy()

say("# D013 평가 지표 기준값 — 2026-11-20 (전부 합성 데이터 v2 + D012 등록 시점 v0 점수)")
say(f"거래 {len(df)} / 등록일 {df.listed_date.min()} ~ {df.listed_date.max()} / 관측 기준일 {df.observed_at.max()}")
say(f"시간 분할 {SPLIT}: train {len(train)} (절단율 {1 - train.event.mean():.3f}) / test {len(test)} (절단율 {1 - test.event.mean():.3f})")
say("  (test 는 등록이 늦어 관측 기간이 짧고 절단율이 높다 — 실데이터에서도 최근 구간을 평가하면 같은 구조. D014 에서 다룸)")

# ---------------------------------------------------------------- 1. 예측기
km_all = kaplan_meier(train.time, train.event)
global_med = km_all.median
sub_med: dict[str, float] = {}
for code, g in train.groupby("category_code"):
    m = kaplan_meier(g.time, g.event).median
    sub_med[code] = m if m is not None else global_med
say(f"\n## 1. 예측기 (train 에서만 보정)")
say(f"  상수: train KM 중앙값 {global_med:.0f}일 (sold-only 중앙값 {train[train.event == 1].time.median():.0f}일)")
say(f"  소분류 KM 중앙값: {len(sub_med)}개 소분류, 범위 {min(sub_med.values()):.0f}~{max(sub_med.values()):.0f}일")

X = np.column_stack([np.ones(len(train)), train.total.to_numpy() / 10.0])
aft = lognormal_aft(X, train.time.to_numpy(), train.event.to_numpy(), ["intercept", "total/10"])
a, b = aft.beta
say(f"  v0 AFT 보정(train): log T̂ = {a:.3f} {b:+.3f}·(총점/10), σ {aft.sigma:.3f} → 총점 50/70/90 → T̂ {np.exp(a + b * 5):.0f}/{np.exp(a + b * 7):.0f}/{np.exp(a + b * 9):.0f}일")
Xa = np.column_stack([np.ones(len(df)), df.total.to_numpy() / 10.0])
aft_all = lognormal_aft(Xa, df.time.to_numpy(), df.event.to_numpy(), ["intercept", "total/10"])
say(f"  v0 AFT 보정(전체, in-sample 대조): log T̂ = {aft_all.beta[0]:.3f} {aft_all.beta[1]:+.3f}·(총점/10)")

base_med = train.groupby("category_code").latent_T_base_days.median().to_dict()   # 정답 기본분포 중앙값(노이즈 포함 표본)
preds = {
    "상수(train KM 중앙값)": lambda d: np.full(len(d), global_med, float),
    "소분류 KM 중앙값": lambda d: d.category_code.map(sub_med).to_numpy(float),
    "v0 총점 AFT 보정(train)": lambda d: np.exp(a + b * d.total.to_numpy() / 10.0),
    "v0 총점 AFT 보정(전체, in-sample)": lambda d: np.exp(aft_all.beta[0] + aft_all.beta[1] * d.total.to_numpy() / 10.0),
    "오라클 승수(천장)": lambda d: d.category_code.map(base_med).to_numpy(float) * d.latent_mult_total.to_numpy(float),
}


def evaluate(d: pd.DataFrame, pred: np.ndarray) -> dict:
    r = M.duration_eval(pred, d.time.to_numpy(), d.event.to_numpy())
    sold = d.event.to_numpy() == 1
    r["within_pct_7"] = M.within_pct(d.time.to_numpy()[sold], pred[sold], min_abs_days=7)
    return r


# ---------------------------------------------------------------- 2. 판매기간 지표
say(f"\n## 2. 판매기간 지표 — test ({len(test)}건, 등록일 ≥ {SPLIT})")
results: dict[str, dict] = {}
for name, fn in preds.items():
    r = evaluate(test, fn(test))
    results[name] = r
    say(f"  {name:32s} {fmt(r)}")
say(f"\n  전체 {len(df)}건(in-sample 포함, 대조):")
results_all: dict[str, dict] = {}
for name, fn in preds.items():
    r = evaluate(df, fn(df))
    results_all[name] = r
    say(f"  {name:32s} {fmt(r)}")

say("\n  부트스트랩 95% CI (test, 행 재표집 {N_BOOT}회):".replace("{N_BOOT}", str(N_BOOT)))
cis: dict[str, dict] = {}
for name in ["소분류 KM 중앙값", "v0 총점 AFT 보정(train)", "오라클 승수(천장)"]:
    p = preds[name](test)
    t, e = test.time.to_numpy(), test.event.to_numpy()
    sold = e == 1
    w = M.bootstrap_ci(M.within_pct, t[sold], p[sold], n_boot=N_BOOT, seed=1)
    ch = M.bootstrap_ci(M.censored_hit_rate, p, t, e, n_boot=N_BOOT, seed=1)
    ci = M.bootstrap_ci(lambda pp, tt, ee: M.duration_eval(pp, tt, ee)["c_index"], p, t, e, n_boot=N_BOOT, seed=1)
    mp = M.bootstrap_ci(M.mape, t[sold], p[sold], n_boot=N_BOOT, seed=1)
    cis[name] = {"within": w, "cens": ch, "c": ci, "mape": mp}
    say(f"  {name:32s} ±20% {w[0]:.3f} [{w[1]:.3f},{w[2]:.3f}] | 절단적중 {ch[0]:.3f} [{ch[1]:.3f},{ch[2]:.3f}] | C {ci[0]:.3f} [{ci[1]:.3f},{ci[2]:.3f}] | MAPE {mp[0]:.1f} [{mp[1]:.1f},{mp[2]:.1f}]")

# 분위수 예측(중앙값 대신 p25·p75)으로 sold ±20% 와 절단 적중의 상충을 보인다
say("\n  예측 분위수를 바꾸면(v0 AFT, test): 중앙값 대신 하위/상위 분위를 점추정으로 쓸 때 — 지표 간 상충 확인")
from scipy.stats import norm  # noqa: E402

for q in (0.25, 0.5, 0.75):
    p = np.exp(a + b * test.total.to_numpy() / 10.0 + aft.sigma * norm.ppf(q))
    r = evaluate(test, p)
    say(f"    q={q:.2f}: ±20% {r['within_pct']:.3f} | 절단적중 {r['censored_hit']:.3f} | 합산 {r['hit_all']:.3f} | logbias {r['mean_log_ratio']:+.3f} | C {r['c_index']:.3f} (분위수 바꿔도 순위 동일)")

# ---------------------------------------------------------------- 3. 등급 F1 (임시 라벨)
say("\n## 3. 등급 F1 — 임시 라벨(D024 전): 관측 결과 fast ≤30 / mid 31~60 / slow 61~90 / stale >90 ; 절단 & 관측 ≤90 → 제외")


def provisional_label(t: float, e: int) -> str | None:
    if t > 90:
        return "stale"
    if e == 0:
        return None
    return "fast" if t <= 30 else ("mid" if t <= 60 else "slow")


LAB = ["fast", "mid", "slow", "stale"]


def cnt(x) -> dict:
    u, c = np.unique(x, return_counts=True)
    return {str(a): int(b) for a, b in zip(u, c)}

V0MAP = {"A": "fast", "B": "mid", "C": "slow", "D": "stale"}
for name, d in (("test", test), ("전체", df)):
    lab = np.array([provisional_label(t, e) for t, e in zip(d.time, d.event)], dtype=object)
    keep = lab != None  # noqa: E711
    yt = lab[keep]
    yp = d.grade_v0.map(V0MAP).to_numpy()[keep]
    rep = M.classification_report(yt, yp, LAB)
    say(f"  [{name}] 라벨 가능 {keep.sum()}/{len(d)} (제외 {len(d) - keep.sum()}) ; 정답 분포 {cnt(yt)} ; v0 등급 분포 {cnt(yp)}")
    say(f"    macro F1 {rep['macro_f1']:.3f} / weighted F1 {rep['weighted_f1']:.3f} / 정확도 {rep['accuracy']:.3f}")
    say("    클래스별 " + " ; ".join(f"{k}: P {v['precision']:.2f} R {v['recall']:.2f} F1 {v['f1']:.2f} (n {v['support']})" for k, v in rep["per_class"].items()))
    say("    혼동행렬(행=정답, 열=예측 " + "/".join(LAB) + "):")
    for i, l in enumerate(LAB):
        say(f"      {l:5s} " + " ".join(f"{x:5d}" for x in rep["cm"][i]))
    # 대조: 다수 클래스 예측, 소분류 KM 중앙값으로 매긴 등급
    maj = np.full(len(yt), pd.Series(yt).mode()[0])
    say(f"    대조 — 다수 클래스('{maj[0]}') 예측: macro F1 {M.classification_report(yt, maj, LAB)['macro_f1']:.3f}")
    sub_pred = d.category_code.map(sub_med).to_numpy(float)[keep]
    sub_lab = np.array([provisional_label(t, 1) for t in sub_pred], dtype=object)
    say(f"    대조 — 소분류 KM 중앙값으로 매긴 라벨: macro F1 {M.classification_report(yt, sub_lab, LAB)['macro_f1']:.3f}")
    if name == "test":
        rep_test = rep
    # 시간 분할 안 보정한 v0 AFT 예측치를 라벨로
    p_aft = np.exp(a + b * d.total.to_numpy() / 10.0)[keep]
    aft_lab = np.array([provisional_label(t, 1) for t in p_aft], dtype=object)
    say(f"    대조 — v0 AFT 보정 T̂ 를 라벨로: macro F1 {M.classification_report(yt, aft_lab, LAB)['macro_f1']:.3f}")

# ---------------------------------------------------------------- 4. 채널 Top-1
say("\n## 4. 채널 Top-1 적중률 — v0 추천 vs 실제 등록 채널 (합성기는 추천 채널을 쓰지 않음 → 우연 수준이 정상)")
for name, d in (("test", test), ("전체", df)):
    hit = M.top1_hit_rate(d.channel, d.recommended_channel)
    ci = M.bootstrap_ci(M.top1_hit_rate, d.channel.to_numpy(), d.recommended_channel.to_numpy(), n_boot=N_BOOT, seed=2)
    pa = d.channel.value_counts(normalize=True)
    pr = d.recommended_channel.value_counts(normalize=True)
    chance = float(sum(pa.get(k, 0) * pr.get(k, 0) for k in set(pa.index) | set(pr.index)))
    maj = d.channel.value_counts().index[0]
    say(f"  [{name}] Top-1 {hit:.3f} [{ci[1]:.3f},{ci[2]:.3f}] ; 우연(주변분포 곱) {chance:.3f} ; 다수 채널('{maj}') 고정 추천 {pa[maj]:.3f}")
    say(f"    v0 추천 분포 {pr.round(3).to_dict()}")
    # 추천 채널로 실제 팔린 건이 더 빨리 팔렸는지(합성에선 효과 없음이 정답)
    m = d.recommended_channel == d.channel
    km1, km0 = kaplan_meier(d.time[m], d.event[m]), kaplan_meier(d.time[~m], d.event[~m])
    say(f"    추천=실제 그룹 KM 중앙값 {km1.median:.0f}일 (n {m.sum()}) vs 불일치 {km0.median:.0f}일 (n {(~m).sum()}) — 합성에선 차이 없어야 정상")

# ---------------------------------------------------------------- 5. 차트
plt.rcParams.update({"font.family": ["Noto Sans CJK JP", "DejaVu Sans"], "axes.facecolor": "#fcfcfb", "font.size": 9})  # 한글 글꼴(환경에 KR 변형 없음, JP 변형이 한글 포함)
fig, ax = plt.subplots(figsize=(9, 4.2))
names = list(results)
x = np.arange(len(names))
w = 0.26
for i, (key, lab) in enumerate((("within_pct", "sold ±20% 적중"), ("censored_hit", "절단 T̂>관측일 적중"), ("hit_all", "합산 적중"))):
    vals = [results[n][key] for n in names]
    ax.bar(x + (i - 1) * w, vals, w * 0.92, color=C[i], label=lab, linewidth=0)
    for xi, v in zip(x + (i - 1) * w, vals):
        ax.text(xi, v + 0.01, f"{v:.2f}", ha="center", va="bottom", fontsize=7.5, color="#333")
ax.set_xticks(x)
ax.set_xticklabels([n.replace("(", "\n(") for n in names], fontsize=8)
ax.set_ylim(0, 1)
ax.set_ylabel("비율")
ax.set_title(f"판매기간 적중률 — test(등록일 ≥ {SPLIT}, n={len(test)}), 합성 v2", fontsize=10)
ax.legend(frameon=False, fontsize=8, loc="upper left")
for s in ("top", "right"):
    ax.spines[s].set_visible(False)
ax.yaxis.grid(True, color="#e5e5e5", linewidth=0.6)
ax.set_axisbelow(True)
fig.tight_layout()
fig.savefig(OUT / "fig1_hit_rates.png", dpi=150)
plt.close(fig)

fig, axes = plt.subplots(1, 2, figsize=(10, 4.2), sharey=True)
for ax, (name, col) in zip(axes, (("v0 총점 AFT 보정(train)", C[0]), ("오라클 승수(천장)", C[2]))):
    p = preds[name](test)
    sold = test.event.to_numpy() == 1
    t = test.time.to_numpy()
    dec = pd.qcut(p, 10, labels=False, duplicates="drop")
    xs, med_sold, med_km, lo, hi = [], [], [], [], []
    for q in np.unique(dec):
        m = dec == q
        xs.append(np.median(p[m]))
        km = kaplan_meier(t[m], test.event.to_numpy()[m])
        med_km.append(km.median if km.median is not None else np.nan)
        med_sold.append(np.median(t[m & sold]) if (m & sold).any() else np.nan)
    ax.plot([0, 120], [0, 120], color="#bbb", linewidth=1, linestyle="--")
    ax.fill_between([0, 120], [0, 96], [0, 144], color="#eee", linewidth=0, label="±20% 띠")
    ax.plot(xs, med_km, color=col, linewidth=2, marker="o", markersize=5, label="실제 KM 중앙값(절단 반영)")
    ax.plot(xs, med_sold, color=C[1], linewidth=2, marker="s", markersize=5, label="sold-only 중앙값")
    ax.set_title(name, fontsize=10)
    ax.set_xlabel("예측 T̂ 십분위 중앙값(일)")
    ax.set_xlim(0, 120)
    ax.set_ylim(0, 120)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(True, color="#e5e5e5", linewidth=0.6)
    ax.set_axisbelow(True)
axes[0].set_ylabel("실제 판매기간(일)")
axes[0].legend(frameon=False, fontsize=8, loc="upper left")
fig.suptitle(f"보정 곡선 — test(n={len(test)}), 합성 v2", fontsize=10)
fig.tight_layout()
fig.savefig(OUT / "fig2_calibration.png", dpi=150)
plt.close(fig)

say(f"\n차트: fig1_hit_rates.png, fig2_calibration.png ; 실행 {time.time() - T0:.0f}초")
(OUT / "metrics_check.txt").write_text("\n".join(LINES) + "\n", encoding="utf-8")
