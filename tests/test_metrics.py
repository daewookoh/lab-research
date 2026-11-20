"""src/eval/metrics.py 테스트 (D013). 손계산 대조 + sklearn 과의 일치."""
import numpy as np
import pytest
from sklearn.metrics import f1_score, precision_recall_fscore_support

from src.eval import metrics as M


def test_mape_and_median_ape_hand():
    y = np.array([10, 20, 0])        # 0일은 분모 1일로 바닥
    p = np.array([12, 15, 2])
    # 오차 20%, 25%, 200% → 평균 81.67, 중앙값 25
    assert M.mape(y, p) == pytest.approx((20 + 25 + 200) / 3)
    assert M.median_ape(y, p) == pytest.approx(25.0)
    assert np.isnan(M.mape([], []))


def test_within_pct_and_min_abs():
    y = np.array([5, 50, 100])
    p = np.array([7, 60, 125])        # 오차 2(40%), 10(20%), 25(25%)
    assert M.within_pct(y, p) == pytest.approx(1 / 3)                    # 50 만 적중(경계 포함)
    assert M.within_pct(y, p, min_abs_days=7) == pytest.approx(2 / 3)    # 5일 행도 허용 폭 7일로 적중


def test_log_bias_sign():
    y = np.array([10.0, 10.0])
    assert M.log_bias(y, np.array([20.0, 20.0]))["mean_log_ratio"] == pytest.approx(np.log(2))
    assert M.log_bias(y, np.array([5.0, 5.0]))["rmsle"] == pytest.approx(np.log(2))


def test_censored_hit_rate():
    pred = np.array([30, 30, 30, 30])
    time = np.array([10, 40, 20, 50])
    event = np.array([1, 0, 0, 0])     # 절단 3행: 30>40 F, 30>20 T, 30>50 F
    assert M.censored_hit_rate(pred, time, event) == pytest.approx(1 / 3)
    assert np.isnan(M.censored_hit_rate(pred, time, np.ones(4, int)))


def test_duration_eval_bundle_consistency():
    rng = np.random.default_rng(3)
    T = rng.lognormal(np.log(30), 0.6, 2000)
    obs = rng.uniform(5, 150, 2000)
    time = np.minimum(T, obs)
    event = (T <= obs).astype(int)
    pred = np.exp(np.log(T) + rng.normal(0, 0.3, 2000))          # 정답에 노이즈
    r = M.duration_eval(pred, time, event)
    assert r["n"] == 2000 and r["n_sold"] + r["n_censored"] == 2000
    sold = event == 1
    assert r["mape"] == pytest.approx(M.mape(time[sold], pred[sold]))
    assert r["within_pct"] == pytest.approx(M.within_pct(time[sold], pred[sold]))
    assert r["censored_hit"] == pytest.approx(M.censored_hit_rate(pred, time, event))
    # 합산 적중 = (sold 적중 수 + 절단 적중 수) / n
    expect = (r["within_pct"] * r["n_sold"] + r["censored_hit"] * r["n_censored"]) / 2000
    assert r["hit_all"] == pytest.approx(expect)
    assert 0.7 < r["c_index"] < 1.0 and r["c_pairs"] > 0
    # 완전한 예측이면 sold MAPE 0, C 가 1
    perfect = M.duration_eval(T, time, event)
    assert perfect["mape"] == pytest.approx(0.0) and perfect["c_index"] == pytest.approx(1.0)
    assert perfect["censored_hit"] == pytest.approx(1.0)


def test_classification_report_matches_sklearn():
    rng = np.random.default_rng(5)
    labels = ["A", "B", "C", "D"]
    yt = rng.choice(labels, 400, p=[0.3, 0.4, 0.25, 0.05])
    yp = np.where(rng.random(400) < 0.6, yt, rng.choice(labels, 400))
    r = M.classification_report(yt, yp, labels)
    assert r["macro_f1"] == pytest.approx(f1_score(yt, yp, labels=labels, average="macro"))
    assert r["weighted_f1"] == pytest.approx(f1_score(yt, yp, labels=labels, average="weighted"))
    p, rc, f, s = precision_recall_fscore_support(yt, yp, labels=labels, zero_division=0)
    for i, lab in enumerate(labels):
        assert r["per_class"][lab]["precision"] == pytest.approx(p[i])
        assert r["per_class"][lab]["recall"] == pytest.approx(rc[i])
        assert r["per_class"][lab]["f1"] == pytest.approx(f[i])
        assert r["per_class"][lab]["support"] == s[i]
    assert r["accuracy"] == pytest.approx(np.mean(yt == yp))
    assert r["cm"].sum() == 400


def test_classification_report_zero_division():
    r = M.classification_report(["A", "A", "B"], ["A", "A", "A"], ["A", "B", "D"])
    assert r["per_class"]["B"] == {"precision": 0.0, "recall": 0.0, "f1": 0.0, "support": 1}
    assert r["per_class"]["D"]["f1"] == 0.0 and r["per_class"]["D"]["support"] == 0
    assert r["macro_f1"] == pytest.approx((0.8 + 0 + 0) / 3)    # A: p 2/3, r 1 → f1 0.8


def test_channel_hit_rates():
    yt = ["open_market", "own_online", "b2b_wholesale"]
    assert M.top1_hit_rate(yt, ["open_market", "offline_store", "b2b_wholesale"]) == pytest.approx(2 / 3)
    ranked = [["own_online", "open_market"], ["offline_store", "social_market"], ["b2b_wholesale"]]
    assert M.topk_hit_rate(yt, ranked, k=1) == pytest.approx(1 / 3)
    assert M.topk_hit_rate(yt, ranked, k=2) == pytest.approx(2 / 3)


def test_bootstrap_ci_contains_point_and_narrows():
    rng = np.random.default_rng(7)
    y = rng.lognormal(np.log(30), 0.5, 400)
    p = y * np.exp(rng.normal(0, 0.2, 400))
    pt, lo, hi = M.bootstrap_ci(M.within_pct, y, p, n_boot=200, seed=1)
    assert pt == pytest.approx(M.within_pct(y, p)) and lo <= pt <= hi
    pt2, lo2, hi2 = M.bootstrap_ci(M.within_pct, np.tile(y, 4), np.tile(p, 4), n_boot=200, seed=1)
    assert (hi2 - lo2) < (hi - lo)         # 표본 4배 → 구간이 좁아짐
