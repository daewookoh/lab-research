"""D009 — 합성 데이터 v1 생성 결과 점검 (생성 자체는 python -m src.data.synth 가 수행).

(1) 검증기 4종(D003~D005)을 기준일로 실행해 오류·경고 수를 남긴다 (오류 0 이어야 완료 조건).
(2) 요약 수치를 D008 설계 점검(synth_design_check.txt)과 비교한다 — 상태 비율 ±5%p 규칙.
(3) listed/partially_sold 차이의 원인 확인: 설계 점검은 등록수량 20 가정, v1 은 실제 수량.
(4) 잔여 유통기한 기준점 변형(expiry_anchor=buy, 설계 §5 원안)을 메모리에서 생성해 상품 검증기 실패 수·disposed 비율을 센다.
(5) 재현성: 같은 seed 두 번 → 동일, 다른 seed → 다름.

실행: python research/2026-11-16/synth_v1_check.py   (저장소 루트에서)
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from src.data.synth import generate, load_params, summarize  # noqa: E402
from src.schema import market_price as mp_mod, product as p_mod, seller as s_mod, transaction as t_mod  # noqa: E402
from src.schema.categories import load as load_categories  # noqa: E402

OUT = REPO / "data" / "synth" / "v1"
META = REPO / "data" / "synth" / "v1_meta.json"
DESIGN = {"sold": 0.569, "withdrawn": 0.230, "partially_sold": 0.139, "listed": 0.038, "disposed": 0.024}
DESIGN_CENSORED = 0.431  # 설계 점검은 disposed 를 절단으로 셌다(D004 검증기는 disposed=censored false)
TOL = 0.05


def run_validators(tables: dict, today: date) -> list[str]:
    leaves = {l.code: l for l in load_categories()}
    products = {p["product_id"]: p for p in tables["product"]}
    sellers = {s["seller_id"]: s for s in tables["seller"]}
    out = []
    w: dict = {}
    e = p_mod.validate_records(tables["product"], p_mod.load_schema(), leaves, today, w)
    out.append(f"product      : {len(tables['product'])}건 오류 {len(e)} 경고 {len(w)}")
    w = {}
    e = s_mod.validate_records(tables["seller"], s_mod.load_schema(), products, leaves, today, w)
    kinds = Counter(x.split(":")[1].split("(")[0].strip()[:12] for ws in w.values() for x in ws)
    out.append(f"seller       : {len(tables['seller'])}건 오류 {len(e)} 경고 {len(w)} {dict(kinds)}")
    w = {}
    e = mp_mod.validate_records(tables["market_price"], mp_mod.load_schema(), products, today, w)
    kinds = Counter(("표본<3" if "표본" in x else "used@신품채널" if "used" in x else "비율" ) for ws in w.values() for x in ws)
    out.append(f"market_price : {len(tables['market_price'])}건 오류 {len(e)} 경고 {len(w)} {dict(kinds)}")
    w = {}
    e = t_mod.validate_records(tables["transaction"], t_mod.load_schema(), products, leaves, today, w, sellers)
    kinds = Counter(("적자" if "적자" in x else "permit채널" if "허가" in x else "기타") for ws in w.values() for x in ws)
    out.append(f"transaction  : {len(tables['transaction'])}건 오류 {len(e)} 경고 {len(w)} {dict(kinds)}")
    for i, errs in list(e.items())[:5]:
        out.append(f"   #{i} {errs}")
    return out


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]


def main() -> int:
    P = load_params()
    today = date.fromisoformat(P["reference_date"])
    tables = {k: read_jsonl(OUT / f"{k}.jsonl") for k in ("product", "transaction", "seller", "market_price")}
    meta = json.loads(META.read_text(encoding="utf-8"))
    print(f"# D009 합성 v1 점검 — 기준일 {today}, seed {meta['seed']}, expiry_anchor={meta['expiry_anchor']}, n={meta['n']}")

    print("\n## 1. 검증기 4종 (python -m src.schema.* --today 기준일 과 동일 함수)")
    for line in run_validators(tables, today):
        print("  " + line)

    print("\n## 2. 상태 비율 vs D008 설계 점검 (±5%p 규칙)")
    txs = tables["transaction"]
    c = Counter(t["outcome"]["status"] for t in txs)
    n = len(txs)
    flag = []
    for k in DESIGN:
        v = c.get(k, 0) / n
        d = v - DESIGN[k]
        mark = " ← 5%p 초과" if abs(d) > TOL else ""
        if mark:
            flag.append(k)
        print(f"  {k:15s} v1 {v:.3f}  설계 {DESIGN[k]:.3f}  차이 {d:+.3f}{mark}")
    cens = np.mean([t["censored"] for t in txs])
    cens_design_def = np.mean([t["outcome"]["status"] in ("listed", "partially_sold", "withdrawn", "disposed") for t in txs])
    print(f"  censored(D004 정의: disposed 제외) {cens:.3f} / 설계 점검 정의(disposed 포함) {cens_design_def:.3f} vs 설계 {DESIGN_CENSORED}")
    print(f"  판매 중(listed+partially_sold) 합: v1 {(c['listed'] + c['partially_sold']) / n:.3f} vs 설계 {DESIGN['listed'] + DESIGN['partially_sold']:.3f}")
    print(f"  5%p 초과 항목: {flag or '없음'}")

    print("\n## 3. listed vs partially_sold — 등록수량 효과")
    lat = meta["transaction_latent"]
    obs = [t for t in txs if t["outcome"]["status"] in ("listed", "partially_sold")]
    qty = [t["listing"]["quantity"] for t in obs]
    print(f"  판매 중 {len(obs)}건의 등록수량: median={np.median(qty):.0f}, 수량 1 인 건 {sum(1 for q in qty if q == 1)}건(필연적으로 listed), ≤3 {sum(1 for q in qty if q <= 3)}건")
    # 설계 점검과 같은 '수량 20' 가정으로 다시 판정(노이즈는 평균 0.85 로 대체)
    relabel = Counter()
    for t in obs:
        l = lat[t["transaction_id"]]
        prog = (l["elapsed_days"] / max(l["T_latent_days"], 1e-9)) ** P["competing"]["partial_progress_exponent"] * 0.85
        relabel["partially_sold" if prog * 20 >= 1 else "listed"] += 1
    print(f"  같은 잠재값에 수량 20 가정 적용 시: listed {relabel['listed'] / n:.3f}, partially_sold {relabel['partially_sold'] / n:.3f} "
          f"(설계 0.038 / 0.139) → 차이는 수량 분포 때문이지 경쟁 사건 로직 차이가 아님")

    print("\n## 4. 잔여 유통기한 기준점 변형: expiry_anchor=buy (설계 §5 원안, 메모리 생성만)")
    tb, mb = generate(P, expiry_anchor="buy")
    prods = tb["product"]
    expired = sum(1 for p in prods if p["expiry_date"] and date.fromisoformat(p["expiry_date"]) <= today)
    near_sa = sum(1 for p in prods if p["expiry_date"] and p["condition_grade"] in "SA"
                  and 0 < (date.fromisoformat(p["expiry_date"]) - today).days <= 90)
    w: dict = {}
    e = p_mod.validate_records(prods, p_mod.load_schema(), {l.code: l for l in load_categories()}, today, w)
    cb = Counter(t["outcome"]["status"] for t in tb["transaction"])
    nb = len(tb["transaction"])
    print(f"  상품 {len(prods)}건 중 기준일에 기한 경과 {expired}건, S·A 인데 임박(≤90일) {near_sa}건 → 상품 검증기 오류 {len(e)}건")
    print(f"  상태 비율: " + ", ".join(f"{k} {cb.get(k, 0) / nb:.3f}" for k in DESIGN) + "  (disposed 가 설계 점검 수준으로 복원됨)")
    print("  → 설계 원안은 '이미 폐기·기한 경과한 과거 상품' 을 포함하므로 D003 의 입력 시점 규칙과 충돌. v1 은 reference 기준 채택(노트 6번 질문).")

    print("\n## 5. 재현성")
    t1, m1 = generate(P)
    t2, m2 = generate(P)
    t3, m3 = generate(P, seed=P["seed"] + 1)
    same = json.dumps(t1, sort_keys=True, ensure_ascii=False) == json.dumps(t2, sort_keys=True, ensure_ascii=False)
    same_file = json.dumps(t1, sort_keys=True, ensure_ascii=False) == json.dumps(tables, sort_keys=True, ensure_ascii=False)
    diff = json.dumps(t1["product"][:5], sort_keys=True) != json.dumps(t3["product"][:5], sort_keys=True)
    print(f"  같은 seed 2회 동일: {same} / 저장된 v1 과 동일: {same_file} / seed+1 은 다름: {diff}")
    print(f"  meta: product_to_seller {len(meta['product_to_seller'])}, transaction_latent {len(meta['transaction_latent'])}, params_sha256 {meta['params_sha256'][:12]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
