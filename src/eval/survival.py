"""우측 중도절단 판매기간을 다루는 최소 도구 (D010, 2026-11-17).

실행 환경에 lifelines·statsmodels 를 설치할 수 없어(패키지 저장소 차단) numpy/scipy 만으로 구현했다.
- kaplan_meier(): 생존함수 추정 + 중앙값
- ols(): 더미 설계행렬 최소제곱 (log 판매기간 회귀)
- lognormal_aft(): 로그정규 AFT 회귀 MLE — log T = Xβ + σ·ε, ε ~ N(0,1), 중도절단 행은 생존확률 1−Φ(z) 로 기여
  (합성 v1 의 잠재 T 가 lognormal × 승수 이므로 모형이 정확히 맞는 경우. 실데이터에선 Weibull·Cox 와 비교해야 함)

모두 작은 데이터(수천 행) 전제. 입력은 numpy 배열.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import optimize, stats


@dataclass
class KMResult:
    times: np.ndarray      # 사건 발생 시각(오름차순)
    survival: np.ndarray   # 각 시각 직후의 S(t)
    n_at_risk: np.ndarray
    n_events: np.ndarray
    n: int
    n_event: int

    @property
    def median(self) -> float | None:
        """S(t) ≤ 0.5 가 되는 첫 시각. 끝까지 0.5 위면 None(추정 불가)."""
        idx = np.nonzero(self.survival <= 0.5)[0]
        return float(self.times[idx[0]]) if len(idx) else None

    def survival_at(self, t: float) -> float:
        idx = np.searchsorted(self.times, t, side="right") - 1
        return 1.0 if idx < 0 else float(self.survival[idx])


def kaplan_meier(time: np.ndarray, event: np.ndarray) -> KMResult:
    """time: 관측 기간, event: 1=사건(판매), 0=중도절단."""
    time = np.asarray(time, dtype=float)
    event = np.asarray(event, dtype=int)
    order = np.argsort(time, kind="stable")
    time, event = time[order], event[order]
    uniq = np.unique(time[event == 1])
    surv, risk, ev = [], [], []
    s = 1.0
    for t in uniq:
        n_risk = int(np.sum(time >= t))
        d = int(np.sum((time == t) & (event == 1)))
        s *= 1.0 - d / n_risk
        surv.append(s)
        risk.append(n_risk)
        ev.append(d)
    return KMResult(uniq, np.array(surv), np.array(risk), np.array(ev), int(len(time)), int(event.sum()))


def design_matrix(cat_cols: dict[str, tuple[np.ndarray, str]], num_cols: dict[str, np.ndarray] | None = None,
                  fixed_effects: np.ndarray | None = None) -> tuple[np.ndarray, list[str]]:
    """더미 설계행렬. cat_cols: {이름: (값 배열, 기준 수준)}. fixed_effects 는 고정효과 그룹 라벨(절편 대신 그룹별 절편)."""
    cols, names = [], []
    if fixed_effects is None:
        cols.append(np.ones(len(next(iter(cat_cols.values()))[0]) if cat_cols else len(next(iter(num_cols.values())))))
        names.append("intercept")
    else:
        fe = np.asarray(fixed_effects)
        for g in sorted(np.unique(fe)):
            cols.append((fe == g).astype(float))
            names.append(f"fe:{g}")
    for name, (vals, ref) in cat_cols.items():
        vals = np.asarray(vals)
        for lv in sorted(np.unique(vals)):
            if lv == ref:
                continue
            cols.append((vals == lv).astype(float))
            names.append(f"{name}={lv}")
    for name, vals in (num_cols or {}).items():
        cols.append(np.asarray(vals, dtype=float))
        names.append(name)
    return np.column_stack(cols), names


@dataclass
class FitResult:
    names: list[str]
    beta: np.ndarray
    se: np.ndarray
    sigma: float
    n: int
    loglik: float | None = None

    def coef(self, name: str) -> float:
        return float(self.beta[self.names.index(name)])

    def table(self, only_prefix: tuple[str, ...] = ()) -> list[tuple[str, float, float]]:
        out = []
        for n, b, s in zip(self.names, self.beta, self.se):
            if only_prefix and not n.startswith(only_prefix):
                continue
            out.append((n, float(b), float(s)))
        return out


def ols(X: np.ndarray, y: np.ndarray, names: list[str]) -> FitResult:
    y = np.asarray(y, dtype=float)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    dof = max(1, X.shape[0] - X.shape[1])
    s2 = float(resid @ resid / dof)
    cov = s2 * np.linalg.pinv(X.T @ X)
    return FitResult(names, beta, np.sqrt(np.diag(cov)), float(np.sqrt(s2)), int(len(y)))


def lognormal_aft(X: np.ndarray, time: np.ndarray, event: np.ndarray, names: list[str],
                  init: FitResult | None = None) -> FitResult:
    """log T = Xβ + σ ε, ε~N(0,1). event=1 행은 밀도, 0 행은 생존확률. BFGS 로 MLE, SE 는 수치 헤시안 역행렬."""
    y = np.log(np.maximum(np.asarray(time, dtype=float), 0.5))
    ev = np.asarray(event, dtype=float)
    k = X.shape[1]

    def nll(theta):
        beta, log_s = theta[:k], theta[k]
        s = np.exp(log_s)
        z = (y - X @ beta) / s
        ll = ev * (stats.norm.logpdf(z) - log_s) + (1 - ev) * stats.norm.logsf(z)
        return -float(np.sum(ll))

    if init is None:
        init = ols(X, y, names)
    theta0 = np.concatenate([init.beta, [np.log(max(init.sigma, 1e-3))]])
    res = optimize.minimize(nll, theta0, method="BFGS", options={"gtol": 1e-6, "maxiter": 2000})
    theta = res.x
    # 수치 헤시안
    h = 1e-4
    H = np.zeros((k + 1, k + 1))
    f0 = nll(theta)
    for i in range(k + 1):
        for j in range(i, k + 1):
            ei, ej = np.zeros(k + 1), np.zeros(k + 1)
            ei[i], ej[j] = h, h
            fij = nll(theta + ei + ej)
            fi = nll(theta + ei)
            fj = nll(theta + ej)
            H[i, j] = H[j, i] = (fij - fi - fj + f0) / (h * h)
    try:
        cov = np.linalg.inv(H)
        se = np.sqrt(np.maximum(np.diag(cov), 0.0))
    except np.linalg.LinAlgError:
        se = np.full(k + 1, np.nan)
    return FitResult(names, theta[:k], se[:k], float(np.exp(theta[k])), int(len(y)), loglik=-float(res.fun))
