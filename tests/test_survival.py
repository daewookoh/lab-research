"""src/eval/survival.py 테스트 (D010). KM 손계산 대조, OLS·AFT 가 시뮬레이션 정답을 복원하는지."""
import numpy as np
import pytest

from src.eval.survival import design_matrix, kaplan_meier, lognormal_aft, ols


def test_km_hand_computed():
    # 시각 1(사건), 2(절단), 3(사건), 3(사건), 5(절단) → S(1)=4/5, S(3)=4/5*(1-2/3)=4/15
    t = np.array([1, 2, 3, 3, 5])
    e = np.array([1, 0, 1, 1, 0])
    km = kaplan_meier(t, e)
    assert list(km.times) == [1, 3]
    assert km.survival == pytest.approx([0.8, 0.8 * (1 - 2 / 3)])
    assert km.median == 3
    assert km.survival_at(0) == 1.0 and km.survival_at(2.5) == pytest.approx(0.8)


def test_km_no_censoring_matches_empirical_median():
    rng = np.random.default_rng(0)
    t = rng.lognormal(np.log(30), 0.7, 500)
    km = kaplan_meier(t, np.ones(500, int))
    assert km.median == pytest.approx(np.sort(t)[249], rel=1e-9) or km.median == pytest.approx(np.sort(t)[250], rel=1e-9)


def test_km_median_none_when_mostly_censored():
    km = kaplan_meier(np.array([1, 2, 3, 4]), np.array([1, 0, 0, 0]))
    assert km.median is None


def test_design_matrix_shapes_and_names():
    g = np.array(["S", "A", "B", "S"])
    X, names = design_matrix({"grade": (g, "S")}, {"x": np.array([1.0, 2.0, 3.0, 4.0])})
    assert names == ["intercept", "grade=A", "grade=B", "x"]
    assert X.shape == (4, 4)
    X2, names2 = design_matrix({"grade": (g, "S")}, fixed_effects=np.array(["L1", "L2", "L1", "L2"]))
    assert names2[:2] == ["fe:L1", "fe:L2"] and X2[:, 0].tolist() == [1, 0, 1, 0]


def _simulate(n=3000, seed=1):
    rng = np.random.default_rng(seed)
    grade = rng.choice(["S", "A", "B", "C"], n, p=[0.4, 0.3, 0.2, 0.1])
    beta = {"S": 0.0, "A": 0.1, "B": 0.35, "C": 0.7}
    x = rng.normal(0, 0.3, n)
    logT = np.log(30) + np.array([beta[g] for g in grade]) + 1.0 * x + rng.normal(0, 0.7, n)
    T = np.exp(logT)
    C = rng.uniform(1, 150, n)           # 독립 중도절단(관측창)
    time = np.minimum(T, C)
    event = (T <= C).astype(int)
    X, names = design_matrix({"grade": (grade, "S")}, {"x": x})
    return X, names, T, time, event


def test_ols_recovers_on_uncensored():
    X, names, T, _, _ = _simulate()
    fit = ols(X, np.log(T), names)
    assert fit.coef("grade=C") == pytest.approx(0.7, abs=0.08)
    assert fit.coef("x") == pytest.approx(1.0, abs=0.08)
    assert fit.sigma == pytest.approx(0.7, abs=0.05)


def test_aft_recovers_under_censoring_better_than_event_only_ols():
    X, names, T, time, event = _simulate()
    assert 0.3 < event.mean() < 0.9
    naive = ols(X[event == 1], np.log(time[event == 1]), names)
    aft = lognormal_aft(X, time, event, names)
    for term, truth in [("grade=C", 0.7), ("x", 1.0)]:
        assert abs(aft.coef(term) - truth) < abs(naive.coef(term) - truth)
        assert aft.coef(term) == pytest.approx(truth, abs=0.1)
    assert aft.sigma == pytest.approx(0.7, abs=0.06)
    assert aft.loglik is not None and np.all(np.isfinite(aft.se))
