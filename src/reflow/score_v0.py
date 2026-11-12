"""Re-Flow Score v0 — 규칙 기반 재유통 가능성 점수 (D007).

명세: research/2026-11-11/reflow_score_v0_spec.md (D006). 이 모듈은 그 명세를 그대로 코드로 옮긴 것이며,
모든 경계값·가중치·점수표는 **운영 근거 없는 가정**이다. 숫자는 전부 이 파일 상단 상수에 모아 두었고,
D012(베이스라인 상관)·D024(등급 라벨) 이후 교체할 때 여기만 바꾸고 명세 문서에 버전·근거를 남긴다.

입력은 M1 테이블 4종(product / market_price / transaction / seller) + 카테고리(D002)뿐이다.

사용법:
    python -m src.reflow.score_v0 [--today 2026-11-12] [--out scores.jsonl]
        → data/schema/examples/ 의 합성 예시에 적용해 표·요약을 출력(옵션으로 JSONL 저장)

라이브러리:
    from src.reflow.score_v0 import score_product, score_all
    rec = score_product(product, leaf, prices, seller, duration_index, today)
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from src.schema.categories import Leaf, load as load_categories
from src.schema.market_price import normalized_median
from src.schema.product import read_jsonl

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "data" / "schema" / "examples"

SPEC_VERSION = "v0"  # research/2026-11-11/reflow_score_v0_spec.md

# ---------------------------------------------------------------------------
# 상수 (전부 가정 — 명세 §절 번호를 함께 적음)
# ---------------------------------------------------------------------------
ITEMS = ("demand", "velocity", "price", "residual", "channel", "logistics")
WEIGHTS = {"demand": 0.20, "velocity": 0.20, "price": 0.20, "residual": 0.20, "channel": 0.10, "logistics": 0.10}  # §0
CHANNELS = ("open_market", "own_online", "offline_store", "secondhand_app", "b2b_wholesale", "live_commerce", "social_market")

# §1 공통
MATCH_PRIORITY = {"exact": 0, "same_model_other_pack": 1, "similar": 2}
USED_TO_NEW_FACTOR = 0.55     # new→used 환산계수(상태 불일치 시)
STALE_DAYS = 60               # 관측일이 이보다 오래되면 stale_price
SEASON_MONTHS = {
    "summer": {6, 7, 8},
    "winter": {11, 12, 1, 2},
    "holiday": {1, 2, 9, 10, 12},
    "school": {2, 3, 8},
    "spring_fall": {3, 4, 5, 9, 10, 11},
}

# §2 게이트
GATE_EXPIRY_DAYS = 30         # 잔여기한 ≤ 30일 → expired
GATE_SELLER_STATUS = "blocked"

# §3.1 시장수요
BRAND_SCORE = {"major": 100, "mid": 75, "minor": 50, "unbranded": 30, "unknown": 40}
SEASON_SCORE = {"neutral": 60, "in_season": 100, "near_season": 70, "off_season": 30}
SEASON_NEAR_MONTHS = 2
DEMAND_WEIGHTS = {"brand": 0.5, "season": 0.3, "search": 0.2}
LISTINGS_OVERSUPPLY = 50      # n_listings > 50 → −10
LISTINGS_THIN = 2             # n_listings ≤ 2 → −5
OVERSUPPLY_PENALTY = 10
THIN_PENALTY = 5

# §3.2 판매속도
VELOCITY_MIN_SOLD = 5         # 소분류/중분류 sold 이력이 이보다 적으면 사전값
VELOCITY_PRIOR_DAYS = {"short": 14, "medium": 30, "long": 45, "none": 60}
VELOCITY_LO, VELOCITY_HI = 7, 120
SOLD30_HIGH, SOLD30_MID = 100, 10
SOLD30_BONUS = {"high": 15, "mid": 5, "zero": -15}
BULK_UNIT_COUNT = 12
BULK_PENALTY = 10

# §3.3 가격경쟁력
PRICE_LO, PRICE_HI = 0.40, 1.00
PRICE_SUSPECT_RATIO = 1.2
APPROX_PRICE_FACTOR = 0.9
NEUTRAL_SCORE = 50.0

# §3.4 상품잔존성
RESIDUAL_WEIGHTS = {"expiry": 0.40, "grade": 0.30, "packaging": 0.15, "holding": 0.15}
EXPIRY_LO, EXPIRY_HI = 30, 365
EXPIRY_HI_SHORT = 180
GRADE_SCORE = {"S": 100, "A": 85, "B": 60, "C": 30}
PACKAGING_SCORE = {"sealed": 100, "box_opened": 85, "repackaged": 70, "box_damaged": 60, "no_box": 50}
HOLDING_LO, HOLDING_HI = 90, 720

# §3.5 채널적합성 — 채널 × 상태등급(S, A, B, C)
CHANNEL_BASE = {
    "open_market": (90, 70, 40, 15),
    "own_online": (80, 65, 40, 15),
    "offline_store": (70, 70, 65, 50),
    "secondhand_app": (50, 70, 85, 70),
    "b2b_wholesale": (85, 60, 40, 25),
    "live_commerce": (75, 60, 35, 10),
    "social_market": (70, 65, 45, 20),
}
PERMIT_EXCLUDED = frozenset({"secondhand_app", "social_market", "live_commerce"})
QTY_BULK = 100
QTY_BULK_ADJ = {"b2b_wholesale": 15, "secondhand_app": -20}
QTY_FEW = 5
QTY_FEW_ADJ = {"b2b_wholesale": -30}
LARGE_ADJ = {"secondhand_app": -15, "live_commerce": -10, "offline_store": 10, "b2b_wholesale": 5}
SELLER_CHANNEL_PENALTY = 20

# §3.6 물류효율성
LOGISTICS_WEIGHTS = {"density": 0.45, "size": 0.25, "region": 0.30}
SHIP_COST_KRW = {"small": 3000, "medium": 4500, "large": 7000}
SIZE_SCORE = {"small": 100, "medium": 70, "large": 40}
SIZE_WEIGHT_G = {"small": 1000, "medium": 5000, "large": 15000}
OVERWEIGHT_FACTOR = 2
SIZE_DOWNGRADE = {"small": "medium", "medium": "large", "large": "large"}
DENSITY_LOG_LO, DENSITY_LOG_HI = 0.0, 1.3   # v=1 → 0점, v=20 → 100점
REGION_SCORE = {"seoul": 100, "gyeonggi_incheon": 100, "chungcheong": 80, "gyeongsang": 55, "jeolla": 55, "gangwon": 55, "jeju": 20}
REGION_UNKNOWN_SCORE = 70

# §4 총점·등급·매입가
GRADE_BOUNDS = (("A", 75), ("B", 60), ("C", 45))   # 그 미만은 D
BUY_RATIO_BASE, BUY_RATIO_SLOPE = 0.10, 0.40
BUY_RATIO_MIN, BUY_RATIO_MAX = 0.10, 0.50
CONFIDENCE_MANUAL_BELOW = 0.5

# 결측 플래그 → confidence 에서 빼는 항목
MISSING_FLAG_ITEM = {"missing_price": "price", "stale_price": "price", "prior_velocity": "velocity", "missing_region": "logistics"}


# ---------------------------------------------------------------------------
# 기본 함수
# ---------------------------------------------------------------------------
def lin(x: float, lo: float, hi: float) -> float:
    """선형 구간 점수: x ≤ lo → 0, x ≥ hi → 100, 사이는 선형."""
    if x <= lo:
        return 0.0
    if x >= hi:
        return 100.0
    return 100.0 * (x - lo) / (hi - lo)


def lin_rev(x: float, lo: float, hi: float) -> float:
    """작을수록 좋은 입력용: 100 − lin."""
    return 100.0 - lin(x, lo, hi)


def clip(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


@dataclass
class ItemResult:
    score: float
    flags: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# §1 시세 선택
# ---------------------------------------------------------------------------
def wanted_condition(product: dict) -> str:
    return "new" if product["condition_grade"] in ("S", "A") else "used"


def select_price(product: dict, prices: list[dict], today: date) -> tuple[dict | None, float | None, list[str]]:
    """§1 시세 선택 규칙. (선택 레코드, 환산 중앙값, flags) 를 돌려준다. 시세 없음 → (None, None, ['missing_price'])."""
    want = wanted_condition(product)
    cands = [m for m in prices if m["product_id"] == product["product_id"] and (m.get("n_listings") or 0) >= 1 and m.get("price_median_krw") is not None]
    if not cands:
        return None, None, ["missing_price"]
    cands.sort(
        key=lambda m: (
            MATCH_PRIORITY[m["match_type"]],
            m["condition"] != want,
            -_d(m["observed_at"]).toordinal(),
            m["source"]["channel"] != "price_comparison",
        )
    )
    best = cands[0]
    nm = normalized_median(best, product)
    flags: list[str] = []
    if best["condition"] != want:
        nm = nm * USED_TO_NEW_FACTOR if want == "used" else nm / USED_TO_NEW_FACTOR
        flags.append("approx_price")
    if (today - _d(best["observed_at"])).days > STALE_DAYS:
        flags.append("stale_price")
    return best, nm, flags


# ---------------------------------------------------------------------------
# §3 항목별 산식
# ---------------------------------------------------------------------------
def season_state(seasonality: list[str], month: int) -> str:
    """neutral / in_season / near_season(다음 제철까지 ≤ 2개월) / off_season."""
    if not seasonality:
        return "neutral"
    months = set().union(*(SEASON_MONTHS[s] for s in seasonality))
    if month in months:
        return "in_season"
    for k in range(1, SEASON_NEAR_MONTHS + 1):
        if ((month - 1 + k) % 12) + 1 in months:
            return "near_season"
    return "off_season"


def score_demand(product: dict, leaf: Leaf, price_rec: dict | None, today: date) -> ItemResult:
    """§3.1 시장수요 = 브랜드(0.5) + 시즌(0.3) + 검색지수(0.2, 결측이면 재배분) − 공급 과잉/희박 보정."""
    state = season_state(leaf.seasonality, today.month)
    parts = {"brand": (BRAND_SCORE[product["brand"]["tier"]], DEMAND_WEIGHTS["brand"]), "season": (SEASON_SCORE[state], DEMAND_WEIGHTS["season"])}
    si = price_rec.get("search_index") if price_rec else None
    if si is not None:
        parts["search"] = (float(si), DEMAND_WEIGHTS["search"])
    wsum = sum(w for _, w in parts.values())
    score = sum(v * w for v, w in parts.values()) / wsum
    adj = 0
    if price_rec:
        n = price_rec["n_listings"]
        if n > LISTINGS_OVERSUPPLY:
            adj = -OVERSUPPLY_PENALTY
        elif n <= LISTINGS_THIN:
            adj = -THIN_PENALTY
    return ItemResult(clip(score + adj), [], {"season": state, "listings_adj": adj, "search_index": si})


def build_duration_index(transactions: list[dict], products_by_id: dict[str, dict]) -> dict[str, list[int]]:
    """status=sold 거래의 duration_days 를 소분류 코드별로 모은다 (§3.2 (a)(b) 의 입력)."""
    idx: dict[str, list[int]] = {}
    for t in transactions:
        if t["outcome"]["status"] != "sold" or t.get("duration_days") is None:
            continue
        p = products_by_id.get(t["product_id"])
        if p is None:
            continue
        idx.setdefault(p["category_code"], []).append(int(t["duration_days"]))
    return idx


def reference_duration(leaf: Leaf, duration_index: dict[str, list[int]]) -> tuple[float, str]:
    """D_ref 와 출처('l3' / 'l2' / 'prior')."""
    same3 = duration_index.get(leaf.code, [])
    if len(same3) >= VELOCITY_MIN_SOLD:
        return float(statistics.median(same3)), "l3"
    same2 = [x for code, xs in duration_index.items() if code.startswith(leaf.l2_code + "-") for x in xs]
    if len(same2) >= VELOCITY_MIN_SOLD:
        return float(statistics.median(same2)), "l2"
    return float(VELOCITY_PRIOR_DAYS[leaf.expiry_class]), "prior"


def score_velocity(product: dict, leaf: Leaf, price_rec: dict | None, duration_index: dict[str, list[int]]) -> ItemResult:
    """§3.2 판매속도 = lin_rev(D_ref, 7, 120) + sold_count_30d 보정 − 묶음 보정."""
    d_ref, source = reference_duration(leaf, duration_index)
    flags = ["prior_velocity"] if source == "prior" else []
    score = lin_rev(d_ref, VELOCITY_LO, VELOCITY_HI)
    sc = price_rec.get("sold_count_30d") if price_rec else None
    bonus = 0
    if sc is not None:
        bonus = SOLD30_BONUS["high"] if sc >= SOLD30_HIGH else SOLD30_BONUS["mid"] if sc >= SOLD30_MID else SOLD30_BONUS["zero"]
    bulk = -BULK_PENALTY if product["spec"]["unit_count"] >= BULK_UNIT_COUNT else 0
    return ItemResult(clip(score + bonus + bulk), flags, {"d_ref": d_ref, "d_ref_source": source, "sold30_adj": bonus, "bulk_adj": bulk})


def score_price(product: dict, normalized: float | None, price_flags: list[str]) -> ItemResult:
    """§3.3 가격경쟁력 = lin(r, 0.4, 1.0), r = 환산 시세 ÷ 정상가. 시세 없으면 50 + missing_price."""
    if normalized is None:
        return ItemResult(NEUTRAL_SCORE, ["missing_price"], {"ratio": None})
    r = normalized / product["list_price_krw"]
    score = lin(r, PRICE_LO, PRICE_HI)
    flags = ["suspect_price"] if r > PRICE_SUSPECT_RATIO else []
    if "approx_price" in price_flags:
        score *= APPROX_PRICE_FACTOR
    return ItemResult(clip(score), flags, {"ratio": round(r, 4)})


def score_residual(product: dict, leaf: Leaf, today: date) -> ItemResult:
    """§3.4 상품잔존성 = 잔여기한(0.4) + 상태등급(0.3) + 포장(0.15) + 보유기간(0.15)."""
    exp = _d(product.get("expiry_date"))
    if leaf.expiry_class == "none" or exp is None:
        e, remain = 100.0, None
    else:
        remain = (exp - today).days
        hi = EXPIRY_HI_SHORT if leaf.expiry_class == "short" else EXPIRY_HI
        e = lin(remain, EXPIRY_LO, hi)
    hold = (today - _d(product["acquired_date"])).days
    h = lin_rev(hold, HOLDING_LO, HOLDING_HI)
    w = RESIDUAL_WEIGHTS
    score = w["expiry"] * e + w["grade"] * GRADE_SCORE[product["condition_grade"]] + w["packaging"] * PACKAGING_SCORE[product["packaging_state"]] + w["holding"] * h
    return ItemResult(clip(score), [], {"remaining_days": remain, "holding_days": hold})


def channel_scores(product: dict, leaf: Leaf, seller: dict | None) -> dict[str, float]:
    """§3.5 허용 채널별 적합도 0~100. 허용 채널이 없으면 빈 dict."""
    gi = "SABC".index(product["condition_grade"])
    allowed = [c for c in CHANNELS if not (leaf.regulation == "permit" and c in PERMIT_EXCLUDED)]
    q = product["quantity_on_hand"]
    size = product["spec"]["size_class"]
    used = set(seller["business"].get("sales_channels_used") or []) if seller else set()
    out: dict[str, float] = {}
    for c in allowed:
        s = CHANNEL_BASE[c][gi]
        if q >= QTY_BULK:
            s += QTY_BULK_ADJ.get(c, 0)
        if q <= QTY_FEW:
            s += QTY_FEW_ADJ.get(c, 0)
        if size == "large":
            s += LARGE_ADJ.get(c, 0)
        if c in used:
            s -= SELLER_CHANNEL_PENALTY
        out[c] = clip(s)
    return out


def score_channel(product: dict, leaf: Leaf, seller: dict | None) -> ItemResult:
    """§3.5 채널적합성 = 허용 채널 중 최댓값, 최댓값 채널을 recommended_channel 로."""
    scores = channel_scores(product, leaf, seller)
    if not scores:
        return ItemResult(0.0, ["gate_no_channel"], {"recommended": None, "scores": {}})
    best = max(CHANNELS, key=lambda c: scores.get(c, -1))  # 동점이면 CHANNELS 순서 우선
    return ItemResult(scores[best], [], {"recommended": best, "scores": scores})


def effective_size_class(product: dict) -> str:
    size = product["spec"]["size_class"]
    w = product["spec"].get("weight_g")
    if w is not None and w > OVERWEIGHT_FACTOR * SIZE_WEIGHT_G[size]:
        return SIZE_DOWNGRADE[size]
    return size


def score_logistics(product: dict, seller: dict | None) -> ItemResult:
    """§3.6 물류효율성 = 가치밀도(0.45) + 부피 등급(0.25) + 수거 거리(0.30, 수도권 거점 가정)."""
    size = effective_size_class(product)
    v = product["list_price_krw"] / SHIP_COST_KRW[size]
    density = lin(math.log10(v), DENSITY_LOG_LO, DENSITY_LOG_HI) if v > 0 else 0.0
    region = seller["business"].get("region") if seller else None
    flags: list[str] = []
    if region in REGION_SCORE:
        reg = REGION_SCORE[region]
    else:
        reg, region = REGION_UNKNOWN_SCORE, region or "none"
        flags.append("missing_region")
    w = LOGISTICS_WEIGHTS
    score = w["density"] * density + w["size"] * SIZE_SCORE[size] + w["region"] * reg
    return ItemResult(clip(score), flags, {"size_class_eff": size, "value_density": round(v, 2), "region": region})


# ---------------------------------------------------------------------------
# §2·§4 게이트·집계
# ---------------------------------------------------------------------------
def gate_for(product: dict, seller: dict | None, today: date) -> str | None:
    """G1 expired, G2 seller_blocked (G3 no_channel 은 채널 항목 계산 뒤 붙인다)."""
    exp = _d(product.get("expiry_date"))
    if exp is not None and (exp - today).days <= GATE_EXPIRY_DAYS:
        return "expired"
    if seller is not None and seller.get("status") == GATE_SELLER_STATUS:
        return "seller_blocked"
    return None


def grade_of(total: float) -> str:
    for g, lo in GRADE_BOUNDS:
        if total >= lo:
            return g
    return "D"


def buy_ratio_of(total: float) -> float:
    return clip(BUY_RATIO_BASE + BUY_RATIO_SLOPE * total / 100.0, BUY_RATIO_MIN, BUY_RATIO_MAX)


def confidence_of(flags: list[str]) -> float:
    missing_items = {MISSING_FLAG_ITEM[f] for f in flags if f in MISSING_FLAG_ITEM}
    return (len(ITEMS) - len(missing_items)) / len(ITEMS)


def seller_for_product(product_id: str, transactions: list[dict], sellers_by_id: dict[str, dict]) -> dict | None:
    """상품의 공급 판매자: 그 상품의 거래 레코드 중 seller_id 가 있는 첫 건. (상품 스키마에 판매자 참조가 없어 거래로 잇는다 — D003/D005 한계)"""
    for t in transactions:
        if t["product_id"] == product_id and t.get("seller_id") in sellers_by_id:
            return sellers_by_id[t["seller_id"]]
    return None


def score_product(
    product: dict,
    leaf: Leaf,
    prices: list[dict],
    seller: dict | None,
    duration_index: dict[str, list[int]],
    today: date,
) -> dict:
    """명세 §5 출력 레코드 하나를 만든다. `details` 는 디버그·EDA 용 부가 정보."""
    gate = gate_for(product, seller, today)
    rec, nm, pflags = select_price(product, prices, today)
    flags: list[str] = list(pflags)

    res = {
        "demand": score_demand(product, leaf, rec, today),
        "velocity": score_velocity(product, leaf, rec, duration_index),
        "price": score_price(product, nm, pflags),
        "residual": score_residual(product, leaf, today),
        "channel": score_channel(product, leaf, seller),
        "logistics": score_logistics(product, seller),
    }
    for r in res.values():
        flags += [f for f in r.flags if not f.startswith("gate_")]
    recommended = res["channel"].detail["recommended"]
    if recommended is None:
        gate = gate or "no_channel"

    total = sum(WEIGHTS[k] * res[k].score for k in ITEMS)
    confidence = confidence_of(flags)
    no_price_basis = (product.get("list_price_krw") is None or product.get("list_price_basis", "unknown") == "unknown") and nm is None
    if no_price_basis:
        flags.append("no_price_basis")
    manual = gate is not None or confidence < CONFIDENCE_MANUAL_BELOW or no_price_basis
    ratio = buy_ratio_of(total)
    buy = None if manual else int(round(product["list_price_krw"] * ratio, -2))

    return {
        "product_id": product["product_id"],
        "evaluated_on": today.isoformat(),
        "spec_version": SPEC_VERSION,
        "gate": gate,
        "manual_review": manual,
        "items": {k: int(round(res[k].score)) for k in ITEMS},
        "flags": sorted(set(flags)),
        "confidence": round(confidence, 2),
        "total": round(total, 1),
        "grade": grade_of(total),
        "recommended_channel": recommended,
        "suggested_buy_ratio": round(ratio, 3),
        "suggested_buy_price_krw": buy,
        "details": {
            "seller_id": seller["seller_id"] if seller else None,
            "price_id": rec["price_id"] if rec else None,
            "normalized_median_krw": round(nm, 1) if nm is not None else None,
            **{f"{k}": res[k].detail for k in ITEMS},
        },
    }


def score_all(
    products: list[dict],
    leaves: dict[str, Leaf],
    prices: list[dict],
    transactions: list[dict],
    sellers_by_id: dict[str, dict],
    today: date,
) -> list[dict]:
    pmap = {p["product_id"]: p for p in products}
    idx = build_duration_index(transactions, pmap)
    out = []
    for p in products:
        seller = seller_for_product(p["product_id"], transactions, sellers_by_id)
        out.append(score_product(p, leaves[p["category_code"]], prices, seller, idx, today))
    return out


# ---------------------------------------------------------------------------
# 출력·CLI
# ---------------------------------------------------------------------------
def format_table(rows: list[dict], products: list[dict]) -> str:
    pmap = {p["product_id"]: p for p in products}
    lines = [f"{'product':10} {'cat':10} G {'dem':>4} {'vel':>4} {'pri':>4} {'res':>4} {'chn':>4} {'log':>4} {'total':>6} gr conf {'r':>6} {'D_ref':>5} {'channel':15} {'buy%':>5} {'buy':>7} flags"]
    for r in rows:
        p = pmap[r["product_id"]]
        it = r["items"]
        d = r["details"]
        ratio = d["price"]["ratio"]
        lines.append(
            f"{r['product_id']:10} {p['category_code']:10} {p['condition_grade']} {it['demand']:4d} {it['velocity']:4d} {it['price']:4d} {it['residual']:4d} {it['channel']:4d} {it['logistics']:4d} "
            f"{r['total']:6.1f} {r['grade']:2} {r['confidence']:.2f} {(ratio if ratio is not None else float('nan')):6.3f} {d['velocity']['d_ref']:5.0f} "
            f"{r['recommended_channel'] or '-':15} {r['suggested_buy_ratio']:5.3f} {(r['suggested_buy_price_krw'] or 0):7d} {','.join(r['flags'])}"
            f"{' GATE=' + r['gate'] if r['gate'] else ''}{' manual' if r['manual_review'] else ''}"
        )
    return "\n".join(lines)


def summary(rows: list[dict]) -> str:
    lines = []
    for k in ITEMS:
        xs = [r["items"][k] for r in rows]
        lines.append(f"항목 {k:9}: min {min(xs):3d} median {statistics.median(xs):5.1f} max {max(xs):3d} (서로 다른 값 {len(set(xs))}개)")
    tot = [r["total"] for r in rows]
    lines.append(f"총점: min {min(tot)} median {statistics.median(tot)} max {max(tot)} / 등급 분포 " + ", ".join(f"{g}={sum(1 for r in rows if r['grade'] == g)}" for g in "ABCD"))
    lines.append(f"게이트: {sum(1 for r in rows if r['gate'])}건, manual_review: {sum(1 for r in rows if r['manual_review'])}건, confidence median {statistics.median(r['confidence'] for r in rows):.2f}")
    lines.append("플래그: " + json.dumps(dict(Counter(f for r in rows for f in r["flags"])), ensure_ascii=False))
    lines.append("추천 채널: " + json.dumps(dict(Counter(r["recommended_channel"] for r in rows)), ensure_ascii=False))
    return "\n".join(lines)


def load_examples(examples_dir: Path = EXAMPLES_DIR) -> tuple[list[dict], list[dict], list[dict], dict[str, dict]]:
    products = read_jsonl(examples_dir / "product_examples.jsonl")
    prices = read_jsonl(examples_dir / "market_price_examples.jsonl")
    txs = read_jsonl(examples_dir / "transaction_examples.jsonl")
    sellers = {s["seller_id"]: s for s in read_jsonl(examples_dir / "seller_examples.jsonl")}
    return products, prices, txs, sellers


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Re-Flow Score v0 — M1 예시(합성)에 적용")
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--examples", type=Path, default=EXAMPLES_DIR)
    ap.add_argument("--out", type=Path, default=None, help="점수 레코드를 JSONL 로 저장")
    a = ap.parse_args(argv)
    today = date.fromisoformat(a.today)
    leaves = {l.code: l for l in load_categories()}
    products, prices, txs, sellers = load_examples(a.examples)
    rows = score_all(products, leaves, prices, txs, sellers, today)
    print(f"Re-Flow Score {SPEC_VERSION} — 기준일 {today} / 입력: 상품 {len(products)} 시세 {len(prices)} 거래 {len(txs)} 판매자 {len(sellers)} ({a.examples.relative_to(REPO_ROOT) if a.examples.is_relative_to(REPO_ROOT) else a.examples}; 합성 데이터)")
    print(format_table(rows, products))
    print()
    print(summary(rows))
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\n저장: {a.out} ({len(rows)}건)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
