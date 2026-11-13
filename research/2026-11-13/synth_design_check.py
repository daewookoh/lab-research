"""D008 — 합성 데이터 분포 가정 사전 점검 (생성기 아님).

data/synth/params_v1.yaml 의 판매기간·경쟁 사건·등급 가정만으로 30 소분류 × N 건을 단순 몬테카를로해서
(1) expiry_class 묶음 중앙값이 score_v0 사전값(14/30/45/60)과 맞는지,
(2) 결과 상태(sold/partially_sold/listed/withdrawn/disposed) 비율과 중도절단율이 설계 목표(sold 40~50%, 절단 50% 안팎)에 드는지,
(3) 철회 평균(withdraw_mean_days) 민감도
를 숫자로 남긴다. 테이블 5종의 실제 생성은 D009.

실행: python research/2026-11-13/synth_design_check.py [--n 2000] [--seed 20261113]
"""
from __future__ import annotations

import argparse
import statistics
from pathlib import Path

import numpy as np
import yaml

REPO = Path(__file__).resolve().parents[2]
PARAMS = REPO / "data" / "synth" / "params_v1.yaml"
CATS = REPO / "data" / "schema" / "categories.yaml"
PRIOR = {"short": 14, "medium": 30, "long": 45, "none": 60}  # score_v0 VELOCITY_PRIOR_DAYS


def leaves():
    raw = yaml.safe_load(CATS.read_text(encoding="utf-8"))
    out = {}
    for l1 in raw["categories"]:
        for l2 in l1["children"]:
            for l3 in l2["children"]:
                out[l3["code"]] = {"expiry_class": l3["expiry_class"], "l1": l1["code"], "regulation": l3["regulation"]}
    return out


def choice(rng, table: dict):
    keys = list(table)
    p = np.array([table[k] for k in keys], dtype=float)
    return keys[rng.choice(len(keys), p=p / p.sum())]


def simulate(P: dict, L: dict, n_per_leaf: int, rng, withdraw_mean=None, v2=False):
    """소분류마다 n_per_leaf 개 등록을 뽑아 상태를 결정. 반환: list of dict."""
    m = P["multipliers"]
    comp = P["competing"]
    wm = withdraw_mean or comp["withdraw_mean_days"]
    lo, hi = P["listing_window_days"]
    rows = []
    for code, spec in P["leaves"].items():
        ec = L[code]["expiry_class"]
        for _ in range(n_per_leaf):
            grade = choice(rng, P["product"]["grade_by_expiry"][ec])
            prof = P["product"]["brand_profiles"][P["product"]["brand_profile_by_l1"][L[code]["l1"]]]
            brand = choice(rng, prof)
            pop = dict(P["transaction"]["channel_popularity"])
            if L[code]["regulation"] == "permit":
                for c in P["transaction"]["permit_excluded_channels"]:
                    pop.pop(c, None)
            channel = choice(rng, pop)
            # 등록가/시세 비율 p: 등록가 = 정상가×U×등급계수, 시세 = 정상가×r
            r = float(np.clip(rng.lognormal(np.log(P["market_price"]["ratio_by_brand"][brand][0]),
                                            P["market_price"]["ratio_by_brand"][brand][1]), 0.2, 1.6))
            listed = rng.uniform(*P["transaction"]["listed_ratio"]) * P["transaction"]["listed_grade_factor"][grade]
            p = float(np.clip(listed / r, 0.3, 1.2))
            mult = m["grade"][grade] * m["channel"][channel] * (p / m["price_ref_ratio"]) ** m["price_elasticity"]
            if v2:
                mult *= m["brand"][brand] * rng.choice([m["season"][k] for k in ("in_season", "near_season", "neutral", "off_season")],
                                                       p=[0.2, 0.2, 0.4, 0.2])
            base = rng.lognormal(np.log(spec["median_days"]), spec["sigma"])
            T = base * mult
            elapsed = int(rng.integers(lo, hi + 1))
            W = rng.exponential(wm * comp["withdraw_channel_factor"][channel])
            if ec == "none":
                D = comp["max_listing_days"]
            else:
                rem_lo, rem_hi = P["product"]["remaining_expiry_by_class"][ec]
                if grade == "C":
                    rem_lo, rem_hi = P["product"]["remaining_expiry_grade_c"]
                D = rng.uniform(rem_lo, rem_hi) - comp["dispose_before_expiry_days"]
            first = min(T, W, D)
            if first > elapsed:
                prog = (elapsed / T) ** comp["partial_progress_exponent"] * rng.uniform(*comp["partial_noise"])
                status = "partially_sold" if prog * 20 >= 1 else "listed"  # 수량 20 가정
                dur, cens = elapsed, True
            elif first == T:
                status, dur, cens = "sold", T, False
            elif first == W:
                status, dur, cens = "withdrawn", W, True
            else:
                status, dur, cens = "disposed", max(D, 0), True
            rows.append({"code": code, "ec": ec, "grade": grade, "channel": channel, "T": T, "base": base,
                         "status": status, "dur": dur, "cens": cens, "elapsed": elapsed})
    return rows


def share(rows, key):
    n = len(rows)
    c = {}
    for r in rows:
        c[r[key]] = c.get(r[key], 0) + 1
    return {k: round(v / n, 3) for k, v in sorted(c.items(), key=lambda kv: -kv[1])}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000, help="소분류당 표본 수")
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args(argv)
    P = yaml.safe_load(PARAMS.read_text(encoding="utf-8"))
    L = leaves()
    assert set(P["leaves"]) == set(L), "params 소분류 30개가 categories.yaml 과 다름"
    rng = np.random.default_rng(a.seed or P["seed"])

    print(f"# D008 분포 가정 점검 — params_v1.yaml, 소분류당 n={a.n}, seed={a.seed or P['seed']}")
    print("\n## 1. 기본 판매기간 중앙값(가정 표) vs score_v0 사전값 — expiry_class 묶음")
    by = {}
    for code, spec in P["leaves"].items():
        by.setdefault(L[code]["expiry_class"], []).append(spec["median_days"])
    for ec in ("short", "medium", "long", "none"):
        v = sorted(by[ec])
        print(f"  {ec:6s} n={len(v):2d} 표 중앙값={statistics.median(v):5.1f}  사전값={PRIOR[ec]:3d}  "
              f"범위={v[0]}~{v[-1]}  차이={statistics.median(v)-PRIOR[ec]:+.1f}")

    rows = simulate(P, L, a.n, rng)
    print("\n## 2. v1 승수(grade·price·channel) 적용 후 잠재 T 중앙값 — expiry_class 묶음 / 등급 / 채널")
    for ec in ("short", "medium", "long", "none"):
        t = [r["T"] for r in rows if r["ec"] == ec]
        b = [r["base"] for r in rows if r["ec"] == ec]
        print(f"  {ec:6s} base 중앙값={statistics.median(b):5.1f}  T 중앙값={statistics.median(t):5.1f}  "
              f"T p10/p90={np.percentile(t,10):.0f}/{np.percentile(t,90):.0f}")
    for g in "SABC":
        t = [r["T"] for r in rows if r["grade"] == g]
        print(f"  grade {g}: T 중앙값={statistics.median(t):5.1f} (n={len(t)})")
    for ch in P["transaction"]["channel_popularity"]:
        t = [r["T"] for r in rows if r["channel"] == ch]
        print(f"  {ch:15s}: T 중앙값={statistics.median(t):5.1f} (n={len(t)})")

    print(f"\n## 3. 관측 결과 상태 비율(기준일 window {P['listing_window_days'][0]}~{P['listing_window_days'][1]}일, 철회 평균 {P['competing']['withdraw_mean_days']}일) — 전체 / expiry_class / 등급")
    print(f"  전체   {share(rows,'status')}  중도절단율={np.mean([r['cens'] for r in rows]):.3f}")
    for ec in ("short", "medium", "long", "none"):
        sub = [r for r in rows if r["ec"] == ec]
        print(f"  {ec:6s} {share(sub,'status')}  절단율={np.mean([r['cens'] for r in sub]):.3f}")
    for g in "SABC":
        sub = [r for r in rows if r["grade"] == g]
        print(f"  grade {g} {share(sub,'status')}")
    sold = [r["dur"] for r in rows if r["status"] == "sold"]
    allc = [r["dur"] for r in rows]
    print(f"  sold 만의 duration 중앙값={statistics.median(sold):.1f}  전체 duration(절단 포함) 중앙값={statistics.median(allc):.1f}  "
          f"잠재 T 중앙값={statistics.median([r['T'] for r in rows]):.1f}")
    print("  → sold 만 쓰면 짧은 쪽으로, 경과일을 그대로 쓰면 긴 쪽으로 편향(D004 고찰 재현)")
    print(f"  등급 분포 {share(rows,'grade')}")
    print(f"  소분류별 sold 건수 최소={min(sum(1 for r in rows if r['code']==c and r['status']=='sold') for c in P['leaves'])} "
          f"(n={a.n}/소분류 기준; 1,000건 v1 은 소분류당 ≈33건 → sold ≈{33*np.mean([r['status']=='sold' for r in rows]):.0f}건, "
          f"score_v0 VELOCITY_MIN_SOLD=5 충족 여부 확인용)")

    print("\n## 4. 철회 평균(withdraw_mean_days) 민감도")
    for wm in (120, 240, 480, 1e9):
        rr = simulate(P, L, max(a.n // 4, 200), np.random.default_rng(1), withdraw_mean=wm)
        print(f"  mean={wm if wm < 1e8 else '∞':>5}: {share(rr,'status')} 절단율={np.mean([r['cens'] for r in rr]):.3f}")

    print("\n## 5. v2 승수(brand·season 추가) 적용 시 — 참고")
    rr = simulate(P, L, max(a.n // 4, 200), np.random.default_rng(2), v2=True)
    print(f"  T 중앙값={statistics.median([r['T'] for r in rr]):.1f}  {share(rr,'status')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
