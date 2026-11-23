"""D014 — 시간 기준 홀드아웃 분할 (2026-11-23).

판매기간은 우측 중도절단 데이터이므로 무작위 K-fold 를 쓰면 두 가지 누출이 생긴다.
  (1) 라벨 누출: 기준일(cutoff) 이후에야 알 수 있는 결과(판매 완료·판매기간)로 모델을 학습한다.
  (2) 피처 누출: 정답에서 만든 열(`latent_*`)·결과 열(`status`, `duration_days`…)·미래 집계(소분류 중앙값에 test 거래 포함)를 피처로 쓴다.
이 모듈은 "cutoff 당일에 알 수 있던 것만" 으로 train 을 만들고, test 는 cutoff 이후 등록분으로 잡는 도구를 모은다.

규약(문서 research/2026-11-23/split_strategy.md):
  - train = 등록일 < cutoff. **train 라벨은 cutoff 에서 관리적 절단**(as-of censoring): 종료일 ≥ cutoff 이면 time = cutoff − 등록일, event = 0.
  - test  = 등록일 ≥ cutoff, 그리고 최소 추적 기간 H 를 보장하려면 등록일 ≤ 관측 기준일 − H (결과와 무관하게 "충분히 오래된 등록" 만 평가).
  - 집계 피처(소분류 KM 중앙값, D_ref 등)는 as-of 절단된 train 으로만 만든다.
  - 신뢰구간은 판매자 클러스터 부트스트랩(`cluster_bootstrap_ci`).
입력은 pandas DataFrame, 날짜 열은 datetime64 또는 ISO 문자열.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------------------------- 피처 금지 목록
#: 정답(합성 잠재변수) — 실데이터엔 없지만 합성 flat 파일에 들어 있어 실수로 피처에 들어가기 쉽다.
FORBIDDEN_PATTERNS: tuple[str, ...] = (r"^latent_", r"^ref_", r"^total_ref$")
#: 등록 시점 이후에 결정되는 결과 열 — 라벨·평가 전용. 피처 금지.
OUTCOME_COLUMNS: frozenset[str] = frozenset({
    "status", "observed_at", "quantity_sold", "sold_price_krw", "channel_fee_krw", "shipping_cost_krw",
    "duration_days", "censored", "recovery_rate", "season_state",
})
#: 식별자 — 피처로 쓰면 과적합·누출(판매자 ID 는 D022 에서 타깃 인코딩할 때 train 으로만 만들어야 함).
ID_COLUMNS: frozenset[str] = frozenset({"transaction_id", "product_id"})


def forbidden_columns(columns: Iterable[str], allow_ids: bool = False) -> list[str]:
    """주어진 열 이름 중 피처로 쓰면 안 되는 것을 돌려준다(빈 리스트면 통과)."""
    bad = []
    for c in columns:
        if c in OUTCOME_COLUMNS or any(re.search(p, c) for p in FORBIDDEN_PATTERNS):
            bad.append(c)
        elif not allow_ids and c in ID_COLUMNS:
            bad.append(c)
    return bad


def assert_no_leakage(columns: Iterable[str], allow_ids: bool = False) -> None:
    bad = forbidden_columns(columns, allow_ids)
    if bad:
        raise ValueError(f"피처 금지 열이 포함됨(D014 §3): {bad}")


# ----------------------------------------------------------------------------------------------- as-of 절단
def as_of_censor(df: pd.DataFrame, cutoff, listed_col: str = "listed_date", end_col: str = "observed_at",
                 event_col: str = "status", event_value: str = "sold") -> pd.DataFrame:
    """cutoff 당일에 알 수 있던 라벨로 바꾼다. 반환 열 `time`(일), `event`(1=판매), `asof_censored`(절단이 cutoff 때문인지).

    종료일(end_col: 판매일 또는 마지막 관측일) ≥ cutoff 인 행은 time = cutoff − 등록일, event = 0 으로 덮어쓴다.
    종료일 < cutoff 인 행은 원래 라벨 그대로(원래 절단이면 절단 유지).
    """
    cutoff = pd.Timestamp(cutoff)
    listed = pd.to_datetime(df[listed_col])
    end = pd.to_datetime(df[end_col])
    orig_time = (end - listed).dt.days.astype(float)
    orig_event = (df[event_col] == event_value).astype(int)
    future = end >= cutoff
    out = df.copy()
    out["time"] = np.where(future, (cutoff - listed).dt.days.astype(float), orig_time)
    out["event"] = np.where(future, 0, orig_event)
    out["asof_censored"] = future.to_numpy()
    return out


# ----------------------------------------------------------------------------------------------- 시간 분할
@dataclass
class TimeSplit:
    cutoff: pd.Timestamp
    train: pd.DataFrame
    test: pd.DataFrame
    min_followup_days: int
    observed_max: pd.Timestamp
    n_test_dropped_short: int        # 최소 추적 기간 미달로 test 에서 뺀 행 수
    n_train_asof_censored: int       # cutoff 에서 관리적으로 절단된 train 행 수
    meta: dict = field(default_factory=dict)

    def summary(self) -> dict:
        tr, te = self.train, self.test
        return {
            "cutoff": str(self.cutoff.date()), "H": self.min_followup_days,
            "n_train": int(len(tr)), "train_censor_rate": float(1 - tr.event.mean()) if len(tr) else float("nan"),
            "n_train_asof_censored": self.n_train_asof_censored,
            "n_test": int(len(te)), "test_censor_rate": float(1 - te.event.mean()) if len(te) else float("nan"),
            "n_test_dropped_short": self.n_test_dropped_short,
            "test_listed_range": (str(te.listed_date.min().date()), str(te.listed_date.max().date())) if len(te) else None,
        }


def time_split(df: pd.DataFrame, cutoff, min_followup_days: int = 0, observed_max=None,
               listed_col: str = "listed_date", end_col: str = "observed_at",
               event_col: str = "status", event_value: str = "sold", asof_train: bool = True) -> TimeSplit:
    """등록일 기준 시간 분할.

    train: 등록일 < cutoff. asof_train=True 면 라벨을 cutoff 에서 절단(기본, 권장). False 는 '라벨 누출' 대조군.
    test : cutoff ≤ 등록일 ≤ observed_max − min_followup_days. 라벨은 observed_max 시점의 원래 라벨.
           observed_max 는 기본적으로 데이터의 관측 기준일(end_col 최대값).
    """
    d = df.copy()
    d[listed_col] = pd.to_datetime(d[listed_col])
    d[end_col] = pd.to_datetime(d[end_col])
    cutoff = pd.Timestamp(cutoff)
    obs_max = pd.Timestamp(observed_max) if observed_max is not None else d[end_col].max()

    tr = d[d[listed_col] < cutoff]
    if asof_train:
        tr = as_of_censor(tr, cutoff, listed_col, end_col, event_col, event_value)
        n_asof = int(tr.asof_censored.sum())
    else:
        tr = tr.copy()
        tr["time"] = (tr[end_col] - tr[listed_col]).dt.days.astype(float)
        tr["event"] = (tr[event_col] == event_value).astype(int)
        tr["asof_censored"] = False
        n_asof = 0

    te_all = d[d[listed_col] >= cutoff]
    last_ok = obs_max - pd.Timedelta(days=min_followup_days)
    te = te_all[te_all[listed_col] <= last_ok].copy()
    te["time"] = (te[end_col] - te[listed_col]).dt.days.astype(float)
    te["event"] = (te[event_col] == event_value).astype(int)
    return TimeSplit(cutoff, tr.reset_index(drop=True), te.reset_index(drop=True), min_followup_days, obs_max,
                     int(len(te_all) - len(te)), n_asof)


def rolling_splits(df: pd.DataFrame, cutoffs: Sequence, **kwargs) -> list[TimeSplit]:
    """여러 cutoff 로 확장 창(expanding window) 분할. 각 fold 는 독립적으로 as-of 절단된다."""
    return [time_split(df, c, **kwargs) for c in cutoffs]


def monthly_cutoffs(start, end) -> list[pd.Timestamp]:
    """start~end 사이 매월 1일 목록(양끝 포함)."""
    return list(pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="MS"))


# ----------------------------------------------------------------------------------------------- 집계 피처(train 전용)
def train_group_stat(train: pd.DataFrame, key: str, stat: Callable[[pd.DataFrame], float], default: float,
                     min_n: int = 1) -> dict:
    """train 으로만 그룹별 통계를 만든다(소분류 KM 중앙값 등). n < min_n 이거나 stat 이 None/NaN 이면 default."""
    out: dict = {}
    for k, g in train.groupby(key):
        v = stat(g) if len(g) >= min_n else None
        out[k] = default if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)
    return out


# ----------------------------------------------------------------------------------------------- 클러스터 부트스트랩
def cluster_bootstrap_ci(metric: Callable[..., float], groups, *arrays, n_boot: int = 500, alpha: float = 0.05,
                         seed: int = 0, **kwargs) -> tuple[float, float, float]:
    """클러스터(판매자) 단위 재표집 부트스트랩 백분위 구간. 반환 (점추정, 하한, 상한).

    groups: 행마다의 클러스터 라벨. 클러스터를 복원 추출하고 그 클러스터의 행을 전부 넣는다(행 수는 매번 달라짐).
    같은 판매자의 거래가 묶여 있을 때 `metrics.bootstrap_ci`(행 단위)보다 넓은, 올바른 구간을 준다.
    """
    g = np.asarray(groups)
    arrs = [np.asarray(a) for a in arrays]
    uniq, inv = np.unique(g, return_inverse=True)
    members = [np.nonzero(inv == i)[0] for i in range(len(uniq))]
    rng = np.random.default_rng(seed)
    point = float(metric(*arrs, **kwargs))
    vals = np.empty(n_boot)
    for b in range(n_boot):
        pick = rng.integers(0, len(uniq), len(uniq))
        idx = np.concatenate([members[i] for i in pick])
        vals[b] = metric(*[a[idx] for a in arrs], **kwargs)
    lo, hi = np.nanpercentile(vals, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return point, float(lo), float(hi)
