"""src/eval/split.py 테스트 (D014). 손으로 만든 작은 프레임으로 as-of 절단·최소 추적 기간·금지 열·클러스터 부트스트랩을 확인."""
import numpy as np
import pandas as pd
import pytest

from src.eval import split as S


def _frame():
    # 등록일, 종료일(판매일 또는 마지막 관측일), 상태
    rows = [
        ("t1", "s1", "2026-03-01", "2026-03-21", "sold"),        # cutoff 전 종료, 판매
        ("t2", "s1", "2026-03-10", "2026-04-20", "sold"),        # cutoff(04-01) 이후 판매 → as-of 절단 22일
        ("t3", "s2", "2026-03-15", "2026-03-30", "withdrawn"),   # cutoff 전 철회(원래 절단 유지)
        ("t4", "s2", "2026-03-20", "2026-06-30", "listed"),      # 계속 등록 중 → as-of 절단 12일
        ("t5", "s3", "2026-04-01", "2026-04-15", "sold"),        # test (cutoff 당일 등록)
        ("t6", "s3", "2026-05-20", "2026-06-30", "listed"),      # test, 추적 41일
        ("t7", "s4", "2026-06-25", "2026-06-30", "sold"),        # test, 추적 5일 → H=30 이면 제외
    ]
    return pd.DataFrame(rows, columns=["transaction_id", "seller_id", "listed_date", "observed_at", "status"])


def test_as_of_censor_overrides_future_outcomes():
    out = S.as_of_censor(_frame(), "2026-04-01")
    by = out.set_index("transaction_id")
    assert by.loc["t1", "time"] == 20 and by.loc["t1", "event"] == 1 and not by.loc["t1", "asof_censored"]
    assert by.loc["t2", "time"] == 22 and by.loc["t2", "event"] == 0 and by.loc["t2", "asof_censored"]
    assert by.loc["t3", "time"] == 15 and by.loc["t3", "event"] == 0 and not by.loc["t3", "asof_censored"]
    assert by.loc["t4", "time"] == 12 and by.loc["t4", "event"] == 0
    # cutoff 이후 등록(t5~t7)은 time 이 음수가 되므로 호출자가 먼저 걸러야 한다 — 여기선 표시만 확인
    assert by.loc["t5", "asof_censored"]


def test_time_split_train_test_membership_and_asof():
    sp = S.time_split(_frame(), "2026-04-01")
    assert sorted(sp.train.transaction_id) == ["t1", "t2", "t3", "t4"]
    assert sorted(sp.test.transaction_id) == ["t5", "t6", "t7"]
    assert sp.n_train_asof_censored == 2
    assert sp.train.set_index("transaction_id").loc["t2", "event"] == 0
    # test 라벨은 관측 기준일(06-30) 기준 원래 라벨
    te = sp.test.set_index("transaction_id")
    assert te.loc["t5", "time"] == 14 and te.loc["t5", "event"] == 1
    assert te.loc["t6", "time"] == 41 and te.loc["t6", "event"] == 0
    assert sp.observed_max == pd.Timestamp("2026-06-30")
    s = sp.summary()
    assert s["n_train"] == 4 and s["n_test"] == 3 and s["n_test_dropped_short"] == 0


def test_time_split_leaky_control_keeps_future_labels():
    sp = S.time_split(_frame(), "2026-04-01", asof_train=False)
    tr = sp.train.set_index("transaction_id")
    assert tr.loc["t2", "time"] == 41 and tr.loc["t2", "event"] == 1   # 미래 결과 그대로(누출 대조군)
    assert sp.n_train_asof_censored == 0


def test_min_followup_drops_recent_listings_regardless_of_outcome():
    sp = S.time_split(_frame(), "2026-04-01", min_followup_days=30)
    # observed_max 06-30 − 30일 = 05-31 → t7(06-25 등록, 팔렸음)은 제외, t6(05-20)은 유지
    assert sorted(sp.test.transaction_id) == ["t5", "t6"]
    assert sp.n_test_dropped_short == 1
    sp90 = S.time_split(_frame(), "2026-04-01", min_followup_days=90)
    assert sorted(sp90.test.transaction_id) == ["t5"]


def test_rolling_and_monthly_cutoffs():
    cuts = S.monthly_cutoffs("2026-03-01", "2026-05-01")
    assert [c.strftime("%m-%d") for c in cuts] == ["03-01", "04-01", "05-01"]
    folds = S.rolling_splits(_frame(), cuts)
    n_train = [len(f.train) for f in folds]
    assert n_train == sorted(n_train)                      # 확장 창: train 은 단조 증가
    for f in folds:                                        # train·test 겹침 없음, test 등록일 ≥ cutoff
        assert not set(f.train.transaction_id) & set(f.test.transaction_id)
        assert (f.test.listed_date >= f.cutoff).all() and (f.train.listed_date < f.cutoff).all()


def test_forbidden_columns():
    cols = ["category_code", "latent_T_days", "status", "duration_days", "ref_demand", "total_ref",
            "transaction_id", "list_price_krw", "season_state", "elapsed_days", "total"]
    bad = S.forbidden_columns(cols)
    assert set(bad) == {"latent_T_days", "status", "duration_days", "ref_demand", "total_ref", "transaction_id", "season_state"}
    assert S.forbidden_columns(cols, allow_ids=True) == [c for c in bad if c != "transaction_id"]
    with pytest.raises(ValueError):
        S.assert_no_leakage(["list_price_krw", "latent_mult_total"])
    S.assert_no_leakage(["list_price_krw", "category_code", "elapsed_days", "total"])   # 통과


def test_train_group_stat_default_and_min_n():
    tr = pd.DataFrame({"k": ["a", "a", "b"], "v": [1.0, 3.0, 10.0]})
    st = S.train_group_stat(tr, "k", lambda g: g.v.mean(), default=-1.0, min_n=2)
    assert st == {"a": 2.0, "b": -1.0}
    st2 = S.train_group_stat(tr, "k", lambda g: None, default=5.0)
    assert st2 == {"a": 5.0, "b": 5.0}


def test_cluster_bootstrap_wider_than_row_bootstrap_when_clustered():
    rng = np.random.default_rng(1)
    n_cluster, m = 40, 25
    groups = np.repeat(np.arange(n_cluster), m)
    cluster_eff = rng.normal(0, 1.0, n_cluster)
    x = cluster_eff[groups] + rng.normal(0, 0.3, n_cluster * m)   # 클러스터 내 상관이 큰 자료
    from src.eval.metrics import bootstrap_ci
    p_row, lo_r, hi_r = bootstrap_ci(np.mean, x, n_boot=300, seed=0)
    p_cl, lo_c, hi_c = S.cluster_bootstrap_ci(np.mean, groups, x, n_boot=300, seed=0)
    assert p_row == pytest.approx(p_cl)
    assert (hi_c - lo_c) > 2.0 * (hi_r - lo_r)                 # 클러스터 구간이 훨씬 넓어야 함
    assert lo_c <= p_cl <= hi_c
    # 재현성
    assert S.cluster_bootstrap_ci(np.mean, groups, x, n_boot=50, seed=3) == S.cluster_bootstrap_ci(np.mean, groups, x, n_boot=50, seed=3)
