"""평가 지표 (D013, 2026-11-20). 정의 문서: research/2026-11-20/metrics.md

네 과제 지표(로드맵 "목표 지표")와 중도절단 행 평가 방식을 한 곳에 둔다. 입력은 numpy 배열(또는 list), 반환은 float/dict.
외부 의존 없음(numpy 만). sklearn 과의 일치는 tests/test_metrics.py 에서 확인한다.

  판매기간(회귀)        mape · median_ape · within_pct · log_bias  — sold(사건) 행에서만 정의
  절단 행               censored_hit_rate("T̂ > 관측일" 적중) · concordance_index(순위, src/eval/survival 재사용)
  묶음                  duration_eval() — 위 전부 + sold·절단 합산 적중률 한 번에
  등급 분류             confusion_matrix · classification_report(정밀도·재현율·F1, macro/weighted, 정확도)
  채널 추천             top1_hit_rate · topk_hit_rate
  신뢰구간              bootstrap_ci — 모든 지표에 공통으로 쓰는 비모수 부트스트랩 백분위 구간

관례: 판매기간 예측치 `pred` 는 **일(day) 단위 점추정**(중앙값 예측 권장). 순위 지표(C-index)에는 "클수록 빨리 팔림" 인 점수를
넣어야 하므로 일수 예측이면 risk = −pred 로 바꿔서 넘긴다(duration_eval 이 처리).
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np

from src.eval.survival import concordance_index

__all__ = [
    "mape", "median_ape", "within_pct", "log_bias", "censored_hit_rate", "duration_eval",
    "confusion_matrix", "classification_report", "top1_hit_rate", "topk_hit_rate", "bootstrap_ci",
]


def _arr(x, dtype=float) -> np.ndarray:
    return np.asarray(x, dtype=dtype)


# ----------------------------------------------------------------------------------------------- 판매기간(회귀, sold 행)
def mape(y_true, y_pred, floor_days: float = 1.0) -> float:
    """평균 절대 백분율 오차(%). |ŷ−y| / max(y, floor_days) 의 평균 × 100.

    판매기간 0일(등록 당일 판매)이 있어 분모를 floor_days(기본 1일)로 바닥 처리한다 — 문서 §2-1. 빈 입력은 nan.
    """
    y, p = _arr(y_true), _arr(y_pred)
    if len(y) == 0:
        return float("nan")
    return float(np.mean(np.abs(p - y) / np.maximum(y, floor_days)) * 100.0)


def median_ape(y_true, y_pred, floor_days: float = 1.0) -> float:
    """절대 백분율 오차의 중앙값(%). MAPE 가 짧은 판매기간의 큰 비율오차에 끌려가는 것을 보완하는 보조 지표."""
    y, p = _arr(y_true), _arr(y_pred)
    if len(y) == 0:
        return float("nan")
    return float(np.median(np.abs(p - y) / np.maximum(y, floor_days)) * 100.0)


def within_pct(y_true, y_pred, pct: float = 0.20, min_abs_days: float = 0.0) -> float:
    """±pct 적중률(0~1). |ŷ−y| ≤ max(pct·y, min_abs_days) 인 비율.

    로드맵 목표 "품목별 판매기간 예측 오차 ±20% 이내" 의 품목 단위 판정. min_abs_days 를 주면 짧은 판매기간(예: 5일의 20% = 1일)
    에서 허용 폭이 하루 미만으로 떨어지는 것을 막는다 — 기본값 0 은 로드맵 문구 그대로, 문서 §2-2 에서 7일 대안을 제시.
    """
    y, p = _arr(y_true), _arr(y_pred)
    if len(y) == 0:
        return float("nan")
    tol = np.maximum(pct * y, min_abs_days)
    return float(np.mean(np.abs(p - y) <= tol))


def log_bias(y_true, y_pred, floor_days: float = 0.5) -> dict[str, float]:
    """로그 비율 오차 요약: mean(log ŷ − log y) 는 과대(+)/과소(−) 예측 편향, rmsle 는 그 제곱평균제곱근.

    AFT(log T 회귀) 계열 모형의 자연스러운 척도. 0일은 floor_days 로 바닥 처리.
    """
    y, p = _arr(y_true), _arr(y_pred)
    if len(y) == 0:
        return {"mean_log_ratio": float("nan"), "rmsle": float("nan")}
    d = np.log(np.maximum(p, floor_days)) - np.log(np.maximum(y, floor_days))
    return {"mean_log_ratio": float(np.mean(d)), "rmsle": float(np.sqrt(np.mean(d * d)))}


# ----------------------------------------------------------------------------------------------- 절단 행
def censored_hit_rate(pred, time, event) -> float:
    """절단(event=0) 행에서 ŷ > 관측 기간 인 비율. "아직 안 팔렸다" 를 맞춘 비율(문서 §3-1).

    절단 행의 정답은 T > time 뿐이므로 검증 가능한 명제는 이것 하나다. 혼자 보면 "크게 예측" 으로 올라가므로
    반드시 sold 행 ±pct 적중률·log_bias 와 같이 본다. 절단 행이 없으면 nan.
    """
    p, t, e = _arr(pred), _arr(time), _arr(event, int)
    m = e == 0
    if not m.any():
        return float("nan")
    return float(np.mean(p[m] > t[m]))


def duration_eval(pred, time, event, pct: float = 0.20, min_abs_days: float = 0.0, floor_days: float = 1.0) -> dict[str, float]:
    """판매기간 예측 묶음 평가(문서 §3-3).

    pred: 일 단위 점추정, time: 관측 기간(sold 면 판매기간, 아니면 등록~관측일), event: 1=판매 0=절단.
    반환 키: n, n_sold, n_censored, censor_rate, mape, median_ape, within_pct(sold 행), mean_log_ratio, rmsle,
            censored_hit(절단 행 T̂>관측일), hit_all(sold ±pct 적중 + 절단 T̂>관측일 을 전체 n 으로 나눈 합산 적중률),
            c_index(risk=−pred, 비교 가능 쌍 기준), c_pairs.
    """
    p, t, e = _arr(pred), _arr(time), _arr(event, int)
    sold = e == 1
    out: dict[str, float] = {
        "n": int(len(p)), "n_sold": int(sold.sum()), "n_censored": int((~sold).sum()),
        "censor_rate": float(np.mean(~sold)) if len(p) else float("nan"),
        "mape": mape(t[sold], p[sold], floor_days),
        "median_ape": median_ape(t[sold], p[sold], floor_days),
        "within_pct": within_pct(t[sold], p[sold], pct, min_abs_days),
    }
    out.update(log_bias(t[sold], p[sold]))
    out["censored_hit"] = censored_hit_rate(p, t, e)
    tol = np.maximum(pct * t, min_abs_days)
    hit = np.where(sold, np.abs(p - t) <= tol, p > t)
    out["hit_all"] = float(np.mean(hit)) if len(p) else float("nan")
    c, pairs = concordance_index(t, e, -p) if len(p) else (float("nan"), 0)
    out["c_index"], out["c_pairs"] = float(c), int(pairs)
    return out


# ----------------------------------------------------------------------------------------------- 등급 분류
def confusion_matrix(y_true, y_pred, labels: Sequence) -> np.ndarray:
    """행 = 정답, 열 = 예측, labels 순서. labels 에 없는 값은 무시(둘 중 하나라도 없으면 그 행 제외)."""
    yt, yp = np.asarray(y_true), np.asarray(y_pred)
    idx = {lab: i for i, lab in enumerate(labels)}
    cm = np.zeros((len(labels), len(labels)), dtype=int)
    for a, b in zip(yt, yp):
        if a in idx and b in idx:
            cm[idx[a], idx[b]] += 1
    return cm


def classification_report(y_true, y_pred, labels: Sequence) -> dict:
    """클래스별 precision·recall·f1·support + macro_f1(단순 평균), weighted_f1(support 가중), accuracy.

    분모 0 인 클래스(예측도 정답도 없음)는 precision·recall·f1 = 0 (sklearn zero_division=0 과 동일). 등급 F1 의 주 지표는
    **macro_f1** — 소수 등급(D)이 묻히지 않도록(문서 §4). cm 도 함께 돌려준다.
    """
    cm = confusion_matrix(y_true, y_pred, labels)
    tp = np.diag(cm).astype(float)
    pred_n = cm.sum(axis=0).astype(float)
    true_n = cm.sum(axis=1).astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        prec = np.where(pred_n > 0, tp / pred_n, 0.0)
        rec = np.where(true_n > 0, tp / true_n, 0.0)
        f1 = np.where(prec + rec > 0, 2 * prec * rec / (prec + rec), 0.0)
    n = cm.sum()
    per_class = {lab: {"precision": float(prec[i]), "recall": float(rec[i]), "f1": float(f1[i]), "support": int(true_n[i])}
                 for i, lab in enumerate(labels)}
    return {
        "labels": list(labels), "cm": cm, "per_class": per_class, "n": int(n),
        "accuracy": float(tp.sum() / n) if n else float("nan"),
        "macro_f1": float(np.mean(f1)) if n else float("nan"),
        "weighted_f1": float(np.sum(f1 * true_n) / n) if n else float("nan"),
    }


# ----------------------------------------------------------------------------------------------- 채널 추천
def top1_hit_rate(y_true, y_pred) -> float:
    """추천 채널 1개가 실제 판매 채널과 같은 비율(0~1). 로드맵 목표 "판매채널 추천 적중률 70%" 의 정의(문서 §5)."""
    yt, yp = np.asarray(y_true), np.asarray(y_pred)
    return float(np.mean(yt == yp)) if len(yt) else float("nan")


def topk_hit_rate(y_true, ranked_preds: Sequence[Sequence], k: int = 3) -> float:
    """상위 k 추천 안에 실제 채널이 있는 비율. ranked_preds[i] 는 i 번째 행의 추천 채널을 순위대로 나열한 시퀀스."""
    yt = np.asarray(y_true)
    if len(yt) == 0:
        return float("nan")
    return float(np.mean([t in list(r)[:k] for t, r in zip(yt, ranked_preds)]))


# ----------------------------------------------------------------------------------------------- 신뢰구간
def bootstrap_ci(metric: Callable[..., float], *arrays, n_boot: int = 500, alpha: float = 0.05, seed: int = 0,
                 **kwargs) -> tuple[float, float, float]:
    """행 단위 비모수 부트스트랩 백분위 구간. metric(*arrays[idx], **kwargs) 를 n_boot 번. 반환 (점추정, 하한, 상한).

    모든 arrays 는 같은 길이. 거래 행이 독립이라는 가정(같은 판매자·같은 상품의 여러 거래가 있으면 과소 추정 — 문서 §6).
    """
    arrs = [np.asarray(a) for a in arrays]
    n = len(arrs[0])
    rng = np.random.default_rng(seed)
    point = float(metric(*arrs, **kwargs))
    vals = np.empty(n_boot)
    for b in range(n_boot):
        idx = rng.integers(0, n, n)
        vals[b] = metric(*[a[idx] for a in arrs], **kwargs)
    lo, hi = np.nanpercentile(vals, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return point, float(lo), float(hi)
