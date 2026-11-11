"""D006 프로토타입 — reflow_score_v0_spec.md 의 산식을 M1 스키마 예시(합성 데이터)에 적용해
"명세가 M1 필드만으로 계산 가능한가" 와 "항목 점수가 예시 안에서 변별되는가" 를 확인한다.

정식 구현(모듈화·단위테스트·CLI)은 D007 `src/reflow/score_v0.py` 에서 한다. 이 파일은 명세 검증용 일회성 스크립트다.
실행: python research/2026-11-11/reflow_v0_check.py [--today 2026-11-11]
"""
from __future__ import annotations

import argparse
import math
import statistics
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.schema.categories import load as load_categories  # noqa: E402
from src.schema.market_price import normalized_median  # noqa: E402
from src.schema.product import read_jsonl  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
EX = ROOT / "data" / "schema" / "examples"

WEIGHTS = {"demand": 0.2, "velocity": 0.2, "price": 0.2, "residual": 0.2, "channel": 0.1, "logistics": 0.1}
CHANNELS = ["open_market", "own_online", "offline_store", "secondhand_app", "b2b_wholesale", "live_commerce", "social_market"]
BRAND = {"major": 100, "mid": 75, "minor": 50, "unbranded": 30, "unknown": 40}
SEASON_MONTHS = {"summer": {6, 7, 8}, "winter": {11, 12, 1, 2}, "holiday": {1, 2, 9, 10, 12}, "school": {2, 3, 8}, "spring_fall": {3, 4, 5, 9, 10, 11}}
VELOCITY_PRIOR = {"short": 14, "medium": 30, "long": 45, "none": 60}
GRADE = {"S": 100, "A": 85, "B": 60, "C": 30}
PACK = {"sealed": 100, "box_opened": 85, "repackaged": 70, "box_damaged": 60, "no_box": 50}
CHANNEL_BASE = {  # 채널 × 상태등급 S/A/B/C
    "open_market": (90, 70, 40, 15), "own_online": (80, 65, 40, 15), "offline_store": (70, 70, 65, 50),
    "secondhand_app": (50, 70, 85, 70), "b2b_wholesale": (85, 60, 40, 25), "live_commerce": (75, 60, 35, 10),
    "social_market": (70, 65, 45, 20),
}
PERMIT_EXCLUDED = {"secondhand_app", "social_market", "live_commerce"}
SHIP_COST = {"small": 3000, "medium": 4500, "large": 7000}
SIZE_SCORE = {"small": 100, "medium": 70, "large": 40}
SIZE_WEIGHT_G = {"small": 1000, "medium": 5000, "large": 15000}
REGION = {"seoul": 100, "gyeonggi_incheon": 100, "chungcheong": 80, "gyeongsang": 55, "jeolla": 55, "gangwon": 55, "jeju": 20}
USED_FACTOR = 0.55


def lin(x: float, lo: float, hi: float) -> float:
    if x <= lo:
        return 0.0
    if x >= hi:
        return 100.0
    return 100.0 * (x - lo) / (hi - lo)


def lin_rev(x, lo, hi):
    return 100.0 - lin(x, lo, hi)


def clip(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def d(s):
    return date.fromisoformat(s) if s else None


def select_price(product: dict, prices: list[dict], today: date):
    """§1 시세 선택 규칙. (레코드, 환산중앙값, flags) 반환."""
    want = "new" if product["condition_grade"] in ("S", "A") else "used"
    cands = [m for m in prices if m["product_id"] == product["product_id"] and m["n_listings"] >= 1]
    if not cands:
        return None, None, ["missing_price"]
    order = {"exact": 0, "same_model_other_pack": 1, "similar": 2}
    cands.sort(key=lambda m: (order[m["match_type"]], m["condition"] != want, -d(m["observed_at"]).toordinal(), m["source"]["channel"] != "price_comparison"))
    best = cands[0]
    nm = normalized_median(best, product)
    flags = []
    if best["condition"] != want:
        nm = nm * USED_FACTOR if want == "used" else nm / USED_FACTOR
        flags.append("approx_price")
    if (today - d(best["observed_at"])).days > 60:
        flags.append("stale_price")
    return best, nm, flags


def demand(product, leaf, price_rec, today):
    parts = [(BRAND[product["brand"]["tier"]], 0.5)]
    m = today.month
    if not leaf.seasonality:
        season = 60
    elif any(m in SEASON_MONTHS[s] for s in leaf.seasonality):
        season = 100
    else:
        near = any(((m % 12) + 1) in SEASON_MONTHS[s] or ((m + 1) % 12 + 1) in SEASON_MONTHS[s] for s in leaf.seasonality)
        season = 70 if near else 30
    parts.append((season, 0.3))
    si = price_rec.get("search_index") if price_rec else None
    if si is not None:
        parts.append((si, 0.2))
    wsum = sum(w for _, w in parts)
    score = sum(v * w for v, w in parts) / wsum
    if price_rec:
        n = price_rec["n_listings"]
        if n > 50:
            score -= 10
        elif n <= 2:
            score -= 5
    return clip(score), []


def velocity(product, leaf, price_rec, txs):
    flags = []
    sold = [t for t in txs if t["outcome"]["status"] == "sold"]
    same3 = [t["duration_days"] for t in sold if t["_cat"] == leaf.code]
    same2 = [t["duration_days"] for t in sold if t["_cat"].startswith(leaf.l2_code + "-")]
    if len(same3) >= 5:
        d_ref = statistics.median(same3)
    elif len(same2) >= 5:
        d_ref = statistics.median(same2)
    else:
        d_ref = VELOCITY_PRIOR[leaf.expiry_class]
        flags.append("prior_velocity")
    score = lin_rev(d_ref, 7, 120)
    sc = price_rec.get("sold_count_30d") if price_rec else None
    if sc is not None:
        score += 15 if sc >= 100 else (5 if sc >= 10 else -15)
    if product["spec"]["unit_count"] >= 12:
        score -= 10
    return clip(score), flags, d_ref


def price(product, nm, flags):
    if nm is None:
        return 50.0, ["missing_price"], None
    r = nm / product["list_price_krw"]
    score = lin(r, 0.40, 1.00)
    out = []
    if r > 1.2:
        out.append("suspect_price")
    if "approx_price" in flags:
        score *= 0.9
    return clip(score), out, round(r, 3)


def residual(product, leaf, today):
    exp = d(product.get("expiry_date"))
    if leaf.expiry_class == "none" or exp is None:
        e = 100.0
    else:
        remain = (exp - today).days
        e = lin(remain, 30, 180) if leaf.expiry_class == "short" else lin(remain, 30, 365)
    hold = (today - d(product["acquired_date"])).days
    h = lin_rev(hold, 90, 720)
    score = 0.4 * e + 0.3 * GRADE[product["condition_grade"]] + 0.15 * PACK[product["packaging_state"]] + 0.15 * h
    return clip(score), []


def channel(product, leaf, seller):
    gi = "SABC".index(product["condition_grade"])
    allowed = [c for c in CHANNELS if not (leaf.regulation == "permit" and c in PERMIT_EXCLUDED)]
    if not allowed:
        return 0.0, ["gate_no_channel"], None
    q = product["quantity_on_hand"]
    size = product["spec"]["size_class"]
    used = set(seller["business"]["sales_channels_used"]) if seller else set()
    scores = {}
    for c in allowed:
        s = CHANNEL_BASE[c][gi]
        if q >= 100:
            s += {"b2b_wholesale": 15, "secondhand_app": -20}.get(c, 0)
        if q <= 5 and c == "b2b_wholesale":
            s -= 30
        if size == "large":
            s += {"secondhand_app": -15, "live_commerce": -10, "offline_store": 10, "b2b_wholesale": 5}.get(c, 0)
        if c in used:
            s -= 20
        scores[c] = clip(s)
    best = max(scores, key=scores.get)
    return scores[best], [], best


def logistics(product, seller):
    flags = []
    size = product["spec"]["size_class"]
    w = product["spec"].get("weight_g")
    if w is not None and w > 2 * SIZE_WEIGHT_G[size]:
        size = {"small": "medium", "medium": "large", "large": "large"}[size]
    v = product["list_price_krw"] / SHIP_COST[size]
    density = lin(math.log10(v), 0, 1.3) if v > 0 else 0.0
    region = seller["business"]["region"] if seller else "unknown"
    if region in REGION:
        reg = REGION[region]
    else:
        reg = 70
        flags.append("missing_region")
    score = 0.45 * density + 0.25 * SIZE_SCORE[size] + 0.30 * reg
    return clip(score), flags


def score_product(product, leaves, prices, txs, sellers, today):
    leaf = leaf_of(leaf_map := leaves, product["category_code"])
    seller = None
    for t in txs:
        if t["product_id"] == product["product_id"] and t.get("seller_id") in sellers:
            seller = sellers[t["seller_id"]]
            break
    flags: list[str] = []
    gate = None
    exp = d(product.get("expiry_date"))
    if exp is not None and (exp - today).days <= 30:
        gate = "expired"
    if seller and seller["status"] == "blocked":
        gate = gate or "seller_blocked"
    rec, nm, pflags = select_price(product, prices, today)
    flags += pflags
    items = {}
    items["demand"], f = demand(product, leaf, rec, today); flags += f
    items["velocity"], f, d_ref = velocity(product, leaf, rec, txs); flags += f
    items["price"], f, r = price(product, nm, pflags); flags += f
    items["residual"], f = residual(product, leaf, today); flags += f
    items["channel"], f, best_ch = channel(product, leaf, seller); flags += f
    if best_ch is None:
        gate = gate or "no_channel"
    items["logistics"], f = logistics(product, seller); flags += f
    missing = {"missing_price", "prior_velocity", "missing_region", "stale_price"}
    n_ok = 6 - len({"price" if x in ("missing_price", "stale_price") else "velocity" if x == "prior_velocity" else "logistics" for x in flags if x in missing})
    confidence = n_ok / 6
    total = sum(WEIGHTS[k] * v for k, v in items.items())
    grade = "A" if total >= 75 else "B" if total >= 60 else "C" if total >= 45 else "D"
    manual = gate is not None or confidence < 0.5 or (product["list_price_basis"] == "unknown" and nm is None)
    ratio = clip(0.10 + 0.40 * total / 100, 0.10, 0.50)
    buy = None if manual else int(round(product["list_price_krw"] * ratio, -2))
    return dict(product_id=product["product_id"], gate=gate, manual_review=manual, items={k: round(v) for k, v in items.items()},
                flags=sorted(set(flags)), confidence=round(confidence, 2), total=round(total, 1), grade=grade,
                recommended_channel=best_ch, d_ref=d_ref, price_ratio=r, buy_ratio=round(ratio, 3), buy_price=buy, seller=seller["seller_id"] if seller else None)


def leaf_of(leaves, code):
    return leaves[code]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", default="2026-11-11")
    a = ap.parse_args(argv)
    today = date.fromisoformat(a.today)
    leaves = {l.code: l for l in load_categories()}
    products = read_jsonl(EX / "product_examples.jsonl")
    prices = read_jsonl(EX / "market_price_examples.jsonl")
    txs = read_jsonl(EX / "transaction_examples.jsonl")
    sellers = {s["seller_id"]: s for s in read_jsonl(EX / "seller_examples.jsonl")}
    pmap = {p["product_id"]: p for p in products}
    for t in txs:
        t["_cat"] = pmap[t["product_id"]]["category_code"]

    print(f"Re-Flow Score v0 프로토타입 — 기준일 {today} / 합성 예시: 상품 {len(products)} 시세 {len(prices)} 거래 {len(txs)} 판매자 {len(sellers)} (전부 가상 데이터)")
    rows = [score_product(p, leaves, prices, txs, sellers, today) for p in products]
    hdr = f"{'product':10} {'cat':10} {'G':1} {'dem':>4} {'vel':>4} {'pri':>4} {'res':>4} {'chn':>4} {'log':>4} {'total':>6} gr conf {'r':>6} {'D_ref':>5} {'channel':15} {'buy%':>5} {'buy':>7} flags"
    print(hdr)
    for p, r in zip(products, rows):
        it = r["items"]
        print(f"{r['product_id']:10} {p['category_code']:10} {p['condition_grade']} {it['demand']:4d} {it['velocity']:4d} {it['price']:4d} {it['residual']:4d} {it['channel']:4d} {it['logistics']:4d} {r['total']:6.1f} {r['grade']:2} {r['confidence']:.2f} "
              f"{(r['price_ratio'] if r['price_ratio'] is not None else float('nan')):6.3f} {r['d_ref']:5.0f} {r['recommended_channel'] or '-':15} {r['buy_ratio']:5.3f} {(r['buy_price'] if r['buy_price'] is not None else 0):7d} {','.join(r['flags'])}{' GATE=' + r['gate'] if r['gate'] else ''}{' manual' if r['manual_review'] else ''}")

    print()
    for k in WEIGHTS:
        xs = [r["items"][k] for r in rows]
        print(f"항목 {k:9}: min {min(xs):3d} median {statistics.median(xs):5.1f} max {max(xs):3d} (서로 다른 값 {len(set(xs))}개)")
    tot = [r["total"] for r in rows]
    print(f"총점: min {min(tot)} median {statistics.median(tot)} max {max(tot)} / 등급 분포 " + ", ".join(f"{g}={sum(1 for r in rows if r['grade']==g)}" for g in "ABCD"))
    print(f"게이트: {sum(1 for r in rows if r['gate'])}건, manual_review: {sum(1 for r in rows if r['manual_review'])}건, confidence median {statistics.median(r['confidence'] for r in rows):.2f}")
    from collections import Counter
    print("플래그:", dict(Counter(f for r in rows for f in r["flags"])))
    print("추천 채널:", dict(Counter(r["recommended_channel"] for r in rows)))

    # 예시 거래와의 대조 (n 이 작고 전부 가상값이라 상관은 참고용 수치일 뿐이다)
    print()
    rmap = {r["product_id"]: r for r in rows}
    hit = sum(1 for t in txs if t["channel"] == rmap[t["product_id"]]["recommended_channel"])
    print(f"추천 채널 == 예시 거래 채널: {hit}/{len(txs)} (Top-1, 참고용)")
    sold = [(rmap[t["product_id"]]["total"], t["duration_days"]) for t in txs if t["outcome"]["status"] == "sold"]
    if len(sold) >= 3:
        rank = lambda xs: [sorted(xs).index(x) + 1 for x in xs]
        a_, b_ = rank([s for s, _ in sold]), rank([dd for _, dd in sold])
        n = len(sold)
        rho = 1 - 6 * sum((x - y) ** 2 for x, y in zip(a_, b_)) / (n * (n * n - 1))
        print(f"총점 vs 판매기간(sold {n}건) Spearman ρ = {rho:+.3f} (음수여야 방향이 맞음; 가상 데이터라 의미 없음)")
    buy_pairs = [(rmap[t["product_id"]]["buy_price"], t["purchase"]["buy_price_krw"]) for t in txs if t["purchase"]["acquisition_type"] == "purchase" and rmap[t["product_id"]]["buy_price"]]
    diffs = [abs(a - b) / b for a, b in buy_pairs]
    print(f"제안 매입가 vs 예시 매입가: n={len(diffs)} 절대 상대오차 median {statistics.median(diffs):.2f}, ±20% 이내 {sum(1 for x in diffs if x <= 0.2)}/{len(diffs)} (참고용)")
    bl = [t["purchase"]["buy_price_krw"] / pmap[t["product_id"]]["list_price_krw"] for t in txs if t["purchase"]["acquisition_type"] == "purchase"]
    ll = [t["listing"]["listed_price_krw"] / pmap[t["product_id"]]["list_price_krw"] for t in txs]
    print(f"예시 매입가/정상가: min {min(bl):.2f} max {max(bl):.2f} / 등록가/정상가: min {min(ll):.2f} max {max(ll):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
