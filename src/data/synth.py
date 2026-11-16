"""합성 데이터 생성기 v1 (D009). 설계: research/2026-11-13/synth_design.md, 파라미터: data/synth/params_v1.yaml.

**전부 합성(가상) 데이터** — 실제 거래·판매자·시세 아님. 모든 분포는 가정.

생성 순서(설계 §1): categories.yaml → seller → product → market_price · transaction.
목표 변수는 "잠재 판매기간 T(소분류 lognormal × 승수)" 와 철회 W·폐기 D·관측일 elapsed 의 경쟁 사건으로 결정한다.
정답(T·승수 분해·product→seller 맵)은 v1_meta.json 에 따로 저장한다.

사용법:
    python -m src.data.synth --params data/synth/params_v1.yaml --out data/synth/v1 \
        [--n-products 1000] [--n-sellers 120] [--seed 20261113] [--reference-date 2026-11-13] [--expiry-anchor reference|buy]
    → <out>/{product,transaction,seller,market_price}.jsonl, <out>.parquet(거래×상품×판매자 평탄화), <out>_meta.json
    이후 검증: python -m src.schema.product --records <out>/product.jsonl --today <reference-date> (4종 모두)

라이브러리로:
    tables, meta = generate(params, seed=..., n_products=..., n_sellers=..., reference_date=date(...))
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import median

import numpy as np
import yaml

from src.schema.categories import Leaf, load as load_categories

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PARAMS = REPO_ROOT / "data" / "synth" / "params_v1.yaml"
DEFAULT_OUT = REPO_ROOT / "data" / "synth" / "v1"

CHANNELS = ["open_market", "own_online", "offline_store", "secondhand_app", "b2b_wholesale", "live_commerce", "social_market"]
CENSORED_STATUSES = {"listed", "partially_sold", "withdrawn"}   # transaction.py 와 동일(disposed 는 censored=false)
RECOVERY_MIN = -1.0                                             # transaction.json recovery_rate minimum
RECOVERY_DECIMALS = 4
SIZE_ORDER = ["small", "medium", "large"]


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------
def load_params(path: Path = DEFAULT_PARAMS) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _choice(rng: np.random.Generator, table: dict):
    """{값: 가중치} 에서 하나. 키 None(yaml null) 허용."""
    keys = list(table)
    p = np.array([table[k] for k in keys], dtype=float)
    return keys[int(rng.choice(len(keys), p=p / p.sum()))]


def _lognormal(rng: np.random.Generator, med: float, sigma: float) -> float:
    return float(rng.lognormal(math.log(med), sigma))


def _u(rng: np.random.Generator, lo_hi) -> float:
    return float(rng.uniform(lo_hi[0], lo_hi[1]))


def _round100(x: float) -> int:
    return max(100, int(round(x / 100.0)) * 100)


def _iso(d: date) -> str:
    return d.isoformat()


class _Ids:
    """P/T/S/M/B + 자릿수 순번."""

    def __init__(self) -> None:
        self.n: Counter = Counter()

    def next(self, prefix: str, width: int) -> str:
        self.n[prefix] += 1
        return f"{prefix}{self.n[prefix]:0{width}d}"


# ---------------------------------------------------------------------------
# 생성기
# ---------------------------------------------------------------------------
class SynthGenerator:
    def __init__(self, params: dict, seed: int, n_products: int, n_sellers: int,
                 reference_date: date, expiry_anchor: str | None = None):
        self.P = params
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        self.n_products = n_products
        self.n_sellers = n_sellers
        self.ref = reference_date
        self.anchor = expiry_anchor or params.get("d009", {}).get("expiry_anchor", "reference")
        assert self.anchor in ("reference", "buy")
        self.leaves: dict[str, Leaf] = {l.code: l for l in load_categories()}
        assert set(params["leaves"]) == set(self.leaves), "params 소분류가 categories.yaml 과 다름"
        self.l2_to_leaves: dict[str, list[str]] = defaultdict(list)
        for code, leaf in self.leaves.items():
            self.l2_to_leaves[leaf.l2_code].append(code)
        self.ids = _Ids()
        self.created_at = f"{reference_date.isoformat()}T17:00:00+09:00"
        self.multipliers_on = list(params["multipliers"]["enabled_v1"])
        self.stats: Counter = Counter()

    # ----- 1. 판매자 ---------------------------------------------------------
    def gen_sellers(self) -> list[dict]:
        P, rng = self.P["seller"], self.rng
        D = self.P["d009"]
        l2_codes = sorted(self.l2_to_leaves)
        sellers = []
        for _ in range(self.n_sellers):
            lo, hi = P["main_categories_per_seller"]
            k = int(rng.integers(lo, hi + 1))
            mains = sorted(rng.choice(l2_codes, size=k, replace=False).tolist())
            n_ch = int(_choice(rng, P["n_channels_used"]))
            channels = sorted(rng.choice(CHANNELS, size=n_ch, replace=False).tolist()) if n_ch else []
            sellers.append({
                "schema_version": "0.1",
                "seller_id": self.ids.next("S", 6),
                "business": {
                    "type": _choice(rng, P["business_type"]),
                    "region": _choice(rng, P["region"]),
                    "main_category_codes": mains,
                    "years_in_business": round(min(60.0, _lognormal(rng, *P["years_in_business"])), 1),
                    "annual_revenue_band": _choice(rng, D["annual_revenue_band"]),
                    "sales_channels_used": channels,
                },
                "registered_date": None,          # 배치 배정 후 채움
                "acquisition_source": _choice(rng, D["acquisition_source"]),
                "status": _choice(rng, P["status"]),
                "supply_history": [],
                "notes": None,
                "created_at": self.created_at,
                "updated_at": None,
            })
        return sellers

    # ----- 2. 상품 -----------------------------------------------------------
    def _brand(self, l1: str) -> dict:
        P, rng = self.P["product"], self.rng
        tier = _choice(rng, P["brand_profiles"][P["brand_profile_by_l1"][l1]])
        if tier in ("unbranded", "unknown"):
            return {"name": None, "tier": tier}
        letter = chr(ord("A") + int(rng.integers(0, 26)))
        return {"name": f"브랜드{letter}{int(rng.integers(1, 40))}", "tier": tier}

    def _name_and_text(self, leaf: Leaf, brand: dict, unit_count: int, variant: str | None, qty: int,
                       expiry: date | None) -> tuple[str, str | None]:
        rng = self.rng
        base = str(rng.choice(leaf.examples))
        parts = [base]
        if variant:
            parts.append(variant)
        if unit_count > 1:
            parts.append(f"{unit_count}입")
        name = " ".join(parts)
        if rng.random() >= self.P["product"]["source_text_prob"]:
            return name, None
        b = (brand["name"] + " ") if brand["name"] else ""
        tail = f" {qty}개 남음"
        if expiry is not None:
            tail += f" 유통기한 {expiry.year % 100}년{expiry.month}월"
        style = int(rng.integers(0, 3))
        if style == 0:
            text = f"{b}{name}{tail}"
        elif style == 1:
            text = f"{b}{base} {unit_count}개입 재고 {qty}개 정리합니다" + (f", 기한 {expiry.isoformat()}" if expiry else "")
        else:
            text = f"{name} / {b.strip() or '브랜드 없음'} / 수량 {qty}" + (f" / 유통기한 {expiry.strftime('%Y.%m')}" if expiry else "")
        return name, text[:2000]

    def gen_products(self, sellers: list[dict]) -> tuple[list[dict], dict[str, str], dict[str, dict]]:
        """상품 레코드, product→seller 맵, 상품별 잠재 변수(r 등)."""
        P, rng, D = self.P["product"], self.rng, self.P["d009"]
        # 상품을 공급할 판매자: active·blocked 만(dormant·churned 는 과거 배치만 가짐)
        suppliers = [s for s in sellers if s["status"] in ("active", "blocked")]
        w = rng.lognormal(0, 0.8, size=len(suppliers))
        owner_idx = rng.choice(len(suppliers), size=self.n_products, p=w / w.sum())
        # 판매자별 상품을 배치(같은 날 인수한 묶음, 1~5품목)로 묶고 배치마다 매입일을 하나 뽑는다.
        buy_date_of: dict[int, date] = {}
        lo_w, hi_w = self.P["listing_window_days"]
        gap_hi = self.P["transaction"]["listed_after_buy_days"][1]
        for s_i in range(len(suppliers)):
            members = [i for i in range(self.n_products) if int(owner_idx[i]) == s_i]
            pos = 0
            while pos < len(members):
                k = int(_choice(rng, D["batch_products"]))
                # 배치 매입일 = 기준일 − (등록 경과일 U(window) + 매입→등록 간격 U(1, gap_hi)) → 경과일 분포가 설계(U(1,270))와 같아진다
                bd = self.ref - timedelta(days=int(rng.integers(lo_w, hi_w + 1)) + int(rng.integers(1, gap_hi + 1)))
                for i in members[pos:pos + k]:
                    buy_date_of[i] = bd
                pos += k
        products, p2s, latent = [], {}, {}
        for i in range(self.n_products):
            seller = suppliers[int(owner_idx[i])]
            l2 = str(rng.choice(seller["business"]["main_category_codes"]))
            code = str(rng.choice(self.l2_to_leaves[l2]))
            leaf = self.leaves[code]
            spec = self.P["leaves"][code]
            ec = leaf.expiry_class
            grade = _choice(rng, P["grade_by_expiry"][ec])
            packaging = _choice(rng, P["packaging_by_grade"][grade])
            brand = self._brand(leaf.l1_code)
            # 규격
            choices = spec["unit_count_choices"]
            if len(choices) == 1 or rng.random() < self.P["unit_count_weights"][0]:
                unit_count = int(choices[0])          # 단품(첫 후보)
            else:
                unit_count = int(rng.choice(choices[1:]))   # 나머지 후보 균등
            size_class = leaf.size_class
            if rng.random() < 0.10:   # 품목별로 한 단계 덮어씀
                k = SIZE_ORDER.index(size_class) + int(rng.choice([-1, 1]))
                size_class = SIZE_ORDER[min(max(k, 0), 2)]
            pool = D["variant_pool"].get(leaf.l1_code)
            variant = str(rng.choice(pool)) if pool and rng.random() < 0.6 else None
            list_price = _round100(_lognormal(rng, spec["list_price_median"], self.P["list_price_sigma"]))
            qty = max(1, int(round(_lognormal(rng, *P["quantity_by_size"][size_class]))))
            # 날짜: 거래에서 역산(listed = ref − elapsed, buy = listed − gap, acquired = buy − holding).
            # 상품마다 "매입일" 하나를 먼저 정해 두고 거래가 이를 쓴다.
            buy_date = buy_date_of[i]
            gap = int(rng.integers(self.P["transaction"]["listed_after_buy_days"][0], gap_hi + 1))
            gap = min(gap, (self.ref - buy_date).days - lo_w)      # listed_date ≤ ref − window 하한
            listed_date = buy_date + timedelta(days=gap)
            elapsed = (self.ref - listed_date).days
            holding = max(P["holding_min_days"], int(round(_lognormal(rng, *P["holding_days"]))))
            acquired = buy_date - timedelta(days=holding)
            manufacture = None
            if rng.random() < P["manufacture_date_known_prob"]:
                manufacture = acquired - timedelta(days=int(rng.integers(*D["manufacture_before_acquired_days"])))
            expiry = None
            if ec != "none":
                rem_lo, rem_hi = P["remaining_expiry_by_class"][ec]
                if grade == "C":
                    rem_lo, rem_hi = P["remaining_expiry_grade_c"]
                elif self.anchor == "reference":
                    rem_lo = max(rem_lo, D["near_expiry_days"])   # S·A 는 기준일에 임박(≤90) 이면 안 됨
                rem = int(rng.integers(rem_lo, rem_hi + 1))
                base_day = self.ref if self.anchor == "reference" else buy_date
                expiry = base_day + timedelta(days=rem)
                if manufacture is not None and expiry <= manufacture:
                    manufacture = None
            name, text = self._name_and_text(leaf, brand, unit_count, variant, qty, expiry)
            attrs: dict = {}
            if leaf.l1_code == "FA" and variant:
                col, _, sz = variant.partition("/")
                attrs = {"color": col, "size": sz}
            elif leaf.l1_code == "FD" and variant:
                attrs = {"flavor": variant}
            # 시세/정상가 비율 r (상품 잠재값) — 가격 승수와 시세 레코드가 공유
            rb = self.P["market_price"]["ratio_by_brand"][brand["tier"]]
            r = float(np.clip(_lognormal(rng, rb[0], rb[1]), 0.2, 1.6))
            pid = self.ids.next("P", 8)
            products.append({
                "schema_version": "0.1",
                "product_id": pid,
                "name": name,
                "category_code": code,
                "brand": brand,
                "list_price_krw": list_price,
                "list_price_basis": _choice(rng, P["list_price_basis"]),
                "condition_grade": grade,
                "packaging_state": packaging,
                "spec": {"unit_count": unit_count, "net_content": None, "net_unit": None, "size_class": size_class,
                         "weight_g": None, "dimensions_cm": None, "variant": variant},
                "quantity_on_hand": qty,
                "acquired_date": _iso(acquired),
                "manufacture_date": _iso(manufacture) if manufacture else None,
                "expiry_date": _iso(expiry) if expiry else None,
                "attributes": attrs,
                "source_text": text,
                "images": [],
                "created_at": self.created_at,
            })
            p2s[pid] = seller["seller_id"]
            latent[pid] = {"r_market_ratio": round(r, 4), "buy_date": _iso(buy_date), "listed_date": _iso(listed_date),
                           "elapsed_days": elapsed, "expiry_class": ec, "l1": leaf.l1_code}
        return products, p2s, latent

    # ----- 3. 시세 -----------------------------------------------------------
    def gen_market_prices(self, products: list[dict], latent: dict[str, dict]) -> list[dict]:
        P, rng, D = self.P["market_price"], self.rng, self.P["d009"]
        out = []
        for prod in products:
            n_obs = int(_choice(rng, P["n_obs_probs"]))
            seen: set[tuple] = set()
            r = latent[prod["product_id"]]["r_market_ratio"]
            for _ in range(n_obs):
                for _try in range(10):
                    channel = _choice(rng, P["source_channel"])
                    platform = str(rng.choice(D["platform_by_channel"][channel]))
                    used = rng.random() < P["condition_used_prob_by_grade"][prod["condition_grade"]]
                    if channel == "secondhand_app":
                        used = used or rng.random() < 0.7
                    cond = "used" if used else "new"
                    observed = self.ref - timedelta(days=int(rng.integers(P["observed_days_before_ref"][0],
                                                                           P["observed_days_before_ref"][1] + 1)))
                    key = (channel, platform, cond, observed)
                    if key not in seen:
                        seen.add(key)
                        break
                else:
                    continue
                match = _choice(rng, P["match_type"])
                puc = prod["spec"]["unit_count"]
                if match == "exact":
                    unit_count = puc
                elif match == "same_model_other_pack":
                    cands = [c for c in self.P["leaves"][prod["category_code"]]["unit_count_choices"] if c != puc] or [puc * 2]
                    unit_count = int(rng.choice(cands))
                else:
                    unit_count = puc if rng.random() < 0.7 else max(1, int(round(puc * rng.choice([0.5, 2]))))
                # 관측 중앙값 = 정상가 × r × (관측 묶음/우리 묶음) × 관측 노이즈 × (used 환산)
                med = prod["list_price_krw"] * r * (unit_count / puc) * _lognormal(rng, 1.0, 0.08)
                if cond == "used":
                    med *= P["used_to_new_factor"]
                n = min(P["n_listings"]["max"], max(1, int(round(_lognormal(rng, P["n_listings"]["median"], P["n_listings"]["sigma"])))))
                med_i = max(1, _round100(med))
                if n == 1:
                    pmin = pmax = med_i
                else:
                    pmin = max(1, _round100(med * _u(rng, D["price_spread"]["min"])))
                    pmax = max(med_i, _round100(med * _u(rng, D["price_spread"]["max"])))
                    pmin = min(pmin, med_i)
                sold30 = int(round(_lognormal(rng, *D["sold_count_30d_lognormal"]))) if rng.random() < P["sold_count_30d_known_prob"] else None
                sidx = round(float(rng.uniform(0, 100)), 1) if rng.random() < P["search_index_known_prob"] else None
                out.append({
                    "schema_version": "0.1",
                    "price_id": self.ids.next("M", 8),
                    "product_id": prod["product_id"],
                    "observed_at": _iso(observed),
                    "source": {"channel": channel, "platform": platform, "collected_by": "synthetic",
                               "query": prod["name"] if rng.random() < 0.7 else None},
                    "match_type": match,
                    "condition": cond,
                    "unit_count": unit_count,
                    "n_listings": n,
                    "price_min_krw": pmin,
                    "price_median_krw": med_i,
                    "price_max_krw": pmax,
                    "shipping_included": bool(rng.random() < D["shipping_included_prob"]),
                    "sold_count_30d": sold30,
                    "search_index": sidx,
                    "notes": None,
                    "created_at": self.created_at,
                })
        return out

    # ----- 4. 거래 (경쟁 사건) --------------------------------------------------
    def _shipping(self, size_class: str, channel: str, qty_sold: int) -> int:
        T = self.P["transaction"]
        if qty_sold <= 0 or channel in T["no_shipping_channels"]:
            return 0
        per = T["shipping_cost_per_unit_by_size"][size_class]
        if channel == "b2b_wholesale" and T["b2b_single_shipment"]:
            return per
        return per * math.ceil(qty_sold / T["units_per_shipment_by_size"][size_class])

    def gen_transactions(self, products: list[dict], p2s: dict[str, str], latent: dict[str, dict]
                         ) -> tuple[list[dict], dict[str, dict], dict[str, str]]:
        """거래 레코드, 거래별 잠재값(정답), product→acquisition_type."""
        T, M, C, rng = self.P["transaction"], self.P["multipliers"], self.P["competing"], self.rng
        txs, tx_latent, acq_by_product = [], {}, {}
        batch_acq: dict[tuple[str, str], str] = {}   # (seller, buy_date) → 배치 단위 매입/위탁
        for prod in products:
            pid = prod["product_id"]
            lat = latent[pid]
            leaf = self.leaves[prod["category_code"]]
            spec = self.P["leaves"][prod["category_code"]]
            grade = prod["condition_grade"]
            key = (p2s[pid], lat["buy_date"])
            if key not in batch_acq:
                batch_acq[key] = "consignment" if rng.random() < T["consignment_prob"] else "purchase"
            acq = batch_acq[key]
            acq_by_product[pid] = acq
            buy_date = date.fromisoformat(lat["buy_date"])
            listed_date = date.fromisoformat(lat["listed_date"])
            elapsed = lat["elapsed_days"]
            purchase_qty = max(1, int(round(prod["quantity_on_hand"] * _u(rng, T["purchase_qty_ratio"]))))
            listed_price = _round100(prod["list_price_krw"] * _u(rng, T["listed_ratio"]) * T["listed_grade_factor"][grade])
            listed_price = min(listed_price, prod["list_price_krw"])
            buy_price = 0 if acq == "consignment" else max(1, min(prod["list_price_krw"], int(round(listed_price * _u(rng, T["buy_ratio_of_listed"]) / 100.0)) * 100))
            n_list = int(_choice(rng, T["n_listings_per_purchase"]))
            if purchase_qty < 2:
                n_list = 1
            pop = dict(T["channel_popularity"])
            if leaf.regulation == "permit":
                for ch in T["permit_excluded_channels"]:
                    pop.pop(ch, None)
            channels = []
            for _ in range(n_list):
                ch = _choice(rng, {k: v for k, v in pop.items() if k not in channels} or pop)
                channels.append(ch)
            qtys = [purchase_qty] if n_list == 1 else [None, None]
            if n_list == 2:
                q1 = int(rng.integers(1, purchase_qty))
                qtys = [q1, purchase_qty - q1]
            expiry = date.fromisoformat(prod["expiry_date"]) if prod["expiry_date"] else None
            for ch, lq in zip(channels, qtys):
                # 잠재 판매기간 T = base × 승수
                base = _lognormal(rng, spec["median_days"], spec["sigma"])
                p_ratio = float(np.clip(listed_price / (prod["list_price_krw"] * lat["r_market_ratio"]), 0.3, 1.2))
                mult = {
                    "grade": M["grade"][grade],
                    "price": (p_ratio / M["price_ref_ratio"]) ** M["price_elasticity"],
                    "channel": M["channel"][ch],
                    "brand": 1.0, "season": 1.0,
                }
                total = 1.0
                for k in self.multipliers_on:
                    total *= mult[k]
                T_lat = base * total
                T_days = max(0, int(round(T_lat)))
                W = float(rng.exponential(C["withdraw_mean_days"] * C["withdraw_channel_factor"][ch]))
                W_days = max(1, int(math.ceil(W)))
                if expiry is None:
                    D_days = int(C["max_listing_days"])
                else:
                    D_days = (expiry - listed_date).days - int(C["dispose_before_expiry_days"])
                    D_days = max(D_days, 1)
                # 경쟁: 가장 이른 사건(동률이면 sold > withdrawn > disposed)
                first = min(T_days, W_days, D_days)
                if first > elapsed:
                    end_days, observed = elapsed, self.ref
                    prog = (elapsed / max(T_lat, 1e-9)) ** C["partial_progress_exponent"] * _u(rng, C["partial_noise"])
                    q_sold = min(lq - 1, int(math.floor(prog * lq)))
                    status = "partially_sold" if q_sold >= 1 else "listed"
                    sold_date = None
                elif first == T_days:
                    status, end_days, q_sold = "sold", T_days, lq
                    sold_date = observed = listed_date + timedelta(days=T_days)
                else:
                    status = "withdrawn" if first == W_days else "disposed"
                    end_days = first
                    observed = listed_date + timedelta(days=end_days)
                    prog = (end_days / max(T_lat, 1e-9)) ** C["partial_progress_exponent"] * _u(rng, C["partial_noise"])
                    q_sold = max(0, min(lq - 1, int(math.floor(prog * lq))))
                    sold_date = None
                sold_price = None
                if q_sold > 0:
                    sold_price = min(listed_price, max(1, _round100(listed_price * _u(rng, T["sold_ratio_of_listed"]))))
                fee = int(round((sold_price or 0) * q_sold * T["channel_fee_rate"][ch]))
                ship = self._shipping(prod["spec"]["size_class"], ch, q_sold)
                cost = buy_price * lq
                recovery = None
                if cost > 0:
                    revenue = (sold_price or 0) * q_sold
                    recovery = (revenue - fee - ship) / cost
                    if recovery < RECOVERY_MIN:     # 스키마 하한(−1): 배송비를 구매자 부담으로 돌린 것으로 간주해 줄임
                        self.stats["recovery_clipped"] += 1
                        ship = max(0, int(revenue - fee + cost))   # (rev − fee − ship)/cost = −1
                        recovery = (revenue - fee - ship) / cost
                    recovery = round(recovery, RECOVERY_DECIMALS)
                tid = self.ids.next("T", 8)
                txs.append({
                    "schema_version": "0.1",
                    "transaction_id": tid,
                    "product_id": pid,
                    "seller_id": p2s[pid],
                    "channel": ch,
                    "purchase": {"acquisition_type": acq, "buy_price_krw": buy_price,
                                 "buy_price_basis": _choice(rng, T["buy_price_basis"]),
                                 "buy_date": _iso(buy_date), "quantity": purchase_qty},
                    "listing": {"listed_date": _iso(listed_date), "listed_price_krw": listed_price, "quantity": lq},
                    "outcome": {"status": status, "observed_at": _iso(observed), "quantity_sold": q_sold,
                                "sold_date": _iso(sold_date) if sold_date else None, "sold_price_krw": sold_price,
                                "channel_fee_krw": fee, "shipping_cost_krw": ship},
                    "duration_days": end_days,
                    "censored": status in CENSORED_STATUSES,
                    "recovery_rate": recovery,
                    "notes": None,
                    "created_at": self.created_at,
                })
                tx_latent[tid] = {
                    "product_id": pid, "channel": ch, "T_latent_days": round(T_lat, 3), "T_base_days": round(base, 3),
                    "multiplier_total": round(total, 4),
                    "multipliers": {k: round(v, 4) for k, v in mult.items()},
                    "price_ratio_p": round(p_ratio, 4),
                    "withdraw_days_W": W_days, "dispose_days_D": D_days, "elapsed_days": elapsed,
                    "status": status, "expiry_class": leaf.expiry_class, "grade": grade,
                }
        return txs, tx_latent, acq_by_product

    # ----- 5. 판매자 공급 이력 채우기 ---------------------------------------------
    def fill_supply_history(self, sellers: list[dict], products: list[dict], p2s: dict[str, str],
                            latent: dict[str, dict], acq_by_product: dict[str, str], txs: list[dict]) -> None:
        rng, D, P = self.rng, self.P["d009"], self.P["seller"]
        prod_by_id = {p["product_id"]: p for p in products}
        settled: Counter = Counter()
        for t in txs:
            if t["purchase"]["acquisition_type"] == "purchase":
                settled[(t["seller_id"], t["purchase"]["buy_date"])] += t["purchase"]["buy_price_krw"] * t["purchase"]["quantity"]
        # (seller, buy_date) 별 상품 묶음 → 배치. 같은 날 상품이 많으면 batch_products 분포로 쪼갠다.
        groups: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for pid, sid in p2s.items():
            groups[sid][latent[pid]["buy_date"]].append(pid)
        for s in sellers:
            sid = s["seller_id"]
            batches = []
            for bdate in sorted(groups.get(sid, {})):
                pids = groups[sid][bdate]
                acq = acq_by_product[pids[0]]
                declared = sum(prod_by_id[p]["list_price_krw"] * prod_by_id[p]["quantity_on_hand"] for p in pids)
                # 매입 배치 정산액 = 해당 날 거래들의 매입가 × 매입수량 합(품목별 거래 1건 기준이므로 복수 등록은 1회만 센다)
                per_pid: dict[str, int] = {}
                for t in txs:
                    if t["seller_id"] == sid and t["purchase"]["buy_date"] == bdate:
                        per_pid.setdefault(t["product_id"], t["purchase"]["buy_price_krw"] * t["purchase"]["quantity"])
                settled_v = sum(per_pid.values()) if acq == "purchase" else None
                acc = _choice(rng, D["description_accuracy"])
                issue = _choice(rng, D["issue_by_accuracy"][acc])
                batches.append({
                    "batch_id": self.ids.next("B", 8), "supplied_date": bdate, "acquisition_type": acq,
                    "product_count": len(pids), "product_ids": pids, "declared_value_krw": declared,
                    "settled_value_krw": settled_v, "description_accuracy": acc, "issue": issue, "notes": None,
                })
            if s["status"] in ("dormant", "churned"):
                lo, hi = D["inactive_batch_age_days"][s["status"]]
                n_b = int(rng.integers(D["inactive_batches"][0], D["inactive_batches"][1] + 1))
                ages = sorted((int(rng.integers(lo, hi + 1)) for _ in range(n_b)), reverse=True)
                for age in ages:
                    acc = _choice(rng, D["description_accuracy"])
                    acq = "consignment" if rng.random() < self.P["transaction"]["consignment_prob"] else "purchase"
                    declared = int(_lognormal(rng, 2_000_000, 0.8) // 1000 * 1000)
                    batches.append({
                        "batch_id": self.ids.next("B", 8), "supplied_date": _iso(self.ref - timedelta(days=age)),
                        "acquisition_type": acq, "product_count": int(rng.integers(1, 6)),
                        "declared_value_krw": declared,
                        "settled_value_krw": int(declared * _u(rng, (0.2, 0.5))) if acq == "purchase" else None,
                        "description_accuracy": acc, "issue": _choice(rng, D["issue_by_accuracy"][acc]), "notes": None,
                    })
            batches.sort(key=lambda b: b["supplied_date"])
            if s["status"] == "blocked":
                if not batches:   # 상품이 배정되지 않은 blocked 판매자 → 과거 분쟁 배치 1건
                    batches.append({
                        "batch_id": self.ids.next("B", 8), "supplied_date": _iso(self.ref - timedelta(days=int(rng.integers(30, 300)))),
                        "acquisition_type": "purchase", "product_count": 1, "declared_value_krw": 500_000,
                        "settled_value_krw": 150_000, "description_accuracy": "major_gap", "issue": "dispute", "notes": None,
                    })
                if not any(b["description_accuracy"] == "major_gap" or b["issue"] == "dispute" for b in batches):
                    b = batches[-1]
                    b["description_accuracy"], b["issue"] = "major_gap", "dispute"
                s["notes"] = "허위 기재·분쟁으로 거래 중단(합성)"
            s["supply_history"] = batches
            first = date.fromisoformat(batches[0]["supplied_date"]) if batches else self.ref - timedelta(days=int(rng.integers(0, 120)))
            s["registered_date"] = _iso(first - timedelta(days=int(rng.integers(*D["registered_before_first_batch_days"]))))
            if batches:
                s["updated_at"] = self.created_at

    # ----- 전체 -----------------------------------------------------------------
    def generate(self) -> tuple[dict[str, list[dict]], dict]:
        sellers = self.gen_sellers()
        products, p2s, latent = self.gen_products(sellers)
        prices = self.gen_market_prices(products, latent)
        txs, tx_latent, acq = self.gen_transactions(products, p2s, latent)
        self.fill_supply_history(sellers, products, p2s, latent, acq, txs)
        tables = {"seller": sellers, "product": products, "market_price": prices, "transaction": txs}
        meta = {
            "version": self.P.get("version"), "generated_for_backlog": "D009", "synthetic": True,
            "seed": self.seed, "reference_date": _iso(self.ref), "expiry_anchor": self.anchor,
            "n": {k: len(v) for k, v in tables.items()},
            "multipliers_enabled": self.multipliers_on,
            "generator_stats": dict(self.stats),
            "product_to_seller": p2s,
            "product_latent": latent,
            "transaction_latent": tx_latent,
        }
        return tables, meta


def generate(params: dict | Path | str = DEFAULT_PARAMS, seed: int | None = None, n_products: int = 1000,
             n_sellers: int | None = None, reference_date: date | None = None, expiry_anchor: str | None = None
             ) -> tuple[dict[str, list[dict]], dict]:
    if not isinstance(params, dict):
        params = load_params(Path(params))
    seed = params["seed"] if seed is None else seed
    n_sellers = params["seller"]["count_v1"] if n_sellers is None else n_sellers
    ref = date.fromisoformat(params["reference_date"]) if reference_date is None else reference_date
    g = SynthGenerator(params, seed, n_products, n_sellers, ref, expiry_anchor)
    return g.generate()


# ---------------------------------------------------------------------------
# 요약(설계 점검 synth_design_check.py 와 같은 항목) · 평탄화 · 저장
# ---------------------------------------------------------------------------
def summarize(tables: dict[str, list[dict]], meta: dict, params: dict) -> str:
    leaves = {l.code: l for l in load_categories()}
    txs, prods = tables["transaction"], {p["product_id"]: p for p in tables["product"]}
    lat = meta["transaction_latent"]
    lines = [f"# 합성 v1 요약 — n: {meta['n']}, seed={meta['seed']}, 기준일={meta['reference_date']}, expiry_anchor={meta['expiry_anchor']}"]
    lines.append("## 1. 묶음별 잠재 T 중앙값(승수 적용) / sold 만 duration 중앙값 / 설계 사전값")
    prior = {"short": 14, "medium": 30, "long": 45, "none": 60}
    for ec in ("short", "medium", "long", "none"):
        ts = [lat[t["transaction_id"]]["T_latent_days"] for t in txs if leaves[prods[t["product_id"]]["category_code"]].expiry_class == ec]
        bs = [lat[t["transaction_id"]]["T_base_days"] for t in txs if leaves[prods[t["product_id"]]["category_code"]].expiry_class == ec]
        sd = [t["duration_days"] for t in txs if t["outcome"]["status"] == "sold" and leaves[prods[t["product_id"]]["category_code"]].expiry_class == ec]
        lines.append(f"  {ec:6s} n={len(ts):4d} base 중앙값={median(bs):5.1f} T 중앙값={median(ts):5.1f} sold duration 중앙값={median(sd) if sd else float('nan'):5.1f} 사전값={prior[ec]}")

    def share(rows, key):
        c = Counter(key(r) for r in rows)
        n = len(rows)
        return {k: round(v / n, 3) for k, v in sorted(c.items(), key=lambda kv: -kv[1])}

    st = lambda t: t["outcome"]["status"]
    lines.append("## 2. 관측 상태 비율 / 중도절단율(censored=true: listed·partially_sold·withdrawn)")
    lines.append(f"  전체   {share(txs, st)}  censored={np.mean([t['censored'] for t in txs]):.3f}  "
                 f"(설계 점검: sold 0.569 / withdrawn 0.230 / partially_sold 0.139 / listed 0.038 / disposed 0.024)")
    for ec in ("short", "medium", "long", "none"):
        sub = [t for t in txs if leaves[prods[t["product_id"]]["category_code"]].expiry_class == ec]
        lines.append(f"  {ec:6s} {share(sub, st)}")
    for g in "SABC":
        sub = [t for t in txs if prods[t["product_id"]]["condition_grade"] == g]
        lines.append(f"  grade {g} {share(sub, st)}  T 중앙값={median([lat[t['transaction_id']]['T_latent_days'] for t in sub]):.1f}")
    for ch in CHANNELS:
        sub = [t for t in txs if t["channel"] == ch]
        if sub:
            lines.append(f"  {ch:15s} n={len(sub):4d} {share(sub, st)}  T 중앙값={median([lat[t['transaction_id']]['T_latent_days'] for t in sub]):.1f}")
    sold = [t["duration_days"] for t in txs if st(t) == "sold"]
    lines.append(f"  sold 만의 duration 중앙값={median(sold):.1f} / 전체 duration 중앙값={median([t['duration_days'] for t in txs]):.1f} / "
                 f"잠재 T 중앙값={median([v['T_latent_days'] for v in lat.values()]):.1f}  (설계 점검 28.2 / 28.0 / 44.3)")
    lines.append("## 3. 상품 속성")
    lines.append(f"  등급 분포 {share(tables['product'], lambda p: p['condition_grade'])}  (설계 점검 S 0.43 A 0.30 B 0.17 C 0.10)")
    lines.append(f"  브랜드 {share(tables['product'], lambda p: p['brand']['tier'])}")
    lines.append(f"  포장 {share(tables['product'], lambda p: p['packaging_state'])}")
    ref = date.fromisoformat(meta["reference_date"])
    rem = [(date.fromisoformat(p["expiry_date"]) - ref).days for p in tables["product"] if p["expiry_date"]]
    lines.append(f"  유통기한 있음 {len(rem)}/{len(tables['product'])}, 잔여 ≤30일(v0 게이트 G1) {sum(1 for x in rem if x <= 30)}건, ≤90일 {sum(1 for x in rem if x <= 90)}건")
    per_leaf = Counter(prods[t["product_id"]]["category_code"] for t in txs if st(t) == "sold")
    lines.append(f"  소분류별 sold 건수 min={min(per_leaf.get(c, 0) for c in leaves)} median={median([per_leaf.get(c, 0) for c in leaves]):.0f} "
                 f"max={max(per_leaf.values())} / sold<5 소분류={[c for c in leaves if per_leaf.get(c, 0) < 5]}")
    lines.append("## 4. 거래·회수율")
    rr = [t["recovery_rate"] for t in txs if t["recovery_rate"] is not None]
    rs = [t["recovery_rate"] for t in txs if t["recovery_rate"] is not None and st(t) == "sold"]
    lines.append(f"  acquisition {share(txs, lambda t: t['purchase']['acquisition_type'])}  복수 등록 상품 "
                 f"{sum(1 for _, v in Counter(t['product_id'] for t in txs).items() if v > 1)}")
    lines.append(f"  recovery_rate(sold) n={len(rs)} p10/median/p90={np.percentile(rs, 10):.3f}/{median(rs):.3f}/{np.percentile(rs, 90):.3f}  "
                 f"<1(적자) {np.mean([x < 1 for x in rs]):.3f}; 전체(null 제외) median={median(rr):.3f}; 하한 클립 {meta['generator_stats'].get('recovery_clipped', 0)}건")
    lines.append("## 5. 시세·판매자")
    mp = tables["market_price"]
    lines.append(f"  시세 {len(mp)}건, 상품당 {share(list(Counter(m['product_id'] for m in mp).values()), lambda x: x)}  "
                 f"stale(>60일) {np.mean([(ref - date.fromisoformat(m['observed_at'])).days > 60 for m in mp]):.3f}  "
                 f"used {np.mean([m['condition'] == 'used' for m in mp]):.3f}  시세 없는 상품 {len(prods) - len({m['product_id'] for m in mp})}")
    se = tables["seller"]
    lines.append(f"  판매자 {len(se)}명 status {share(se, lambda s: s['status'])}  상품 공급 판매자 {len(set(meta['product_to_seller'].values()))}명  "
                 f"배치 {sum(len(s['supply_history']) for s in se)}건, 판매자당 상품 max={max(Counter(meta['product_to_seller'].values()).values())}")
    return "\n".join(lines)


def flatten(tables: dict[str, list[dict]], meta: dict):
    """거래 1행 = 모델 입력 1행: 상품·판매자 피처 + 목표변수 + 잠재값(정답)."""
    import pandas as pd

    leaves = {l.code: l for l in load_categories()}
    prods = {p["product_id"]: p for p in tables["product"]}
    sellers = {s["seller_id"]: s for s in tables["seller"]}
    lat = meta["transaction_latent"]
    rows = []
    for t in tables["transaction"]:
        p, s, l = prods[t["product_id"]], sellers[t["seller_id"]], lat[t["transaction_id"]]
        leaf = leaves[p["category_code"]]
        rows.append({
            "transaction_id": t["transaction_id"], "product_id": p["product_id"], "seller_id": s["seller_id"],
            "category_code": p["category_code"], "l1": leaf.l1_code, "l2": leaf.l2_code, "expiry_class": leaf.expiry_class,
            "regulation": leaf.regulation, "brand_tier": p["brand"]["tier"], "condition_grade": p["condition_grade"],
            "packaging_state": p["packaging_state"], "size_class": p["spec"]["size_class"], "unit_count": p["spec"]["unit_count"],
            "list_price_krw": p["list_price_krw"], "quantity_on_hand": p["quantity_on_hand"],
            "acquired_date": p["acquired_date"], "expiry_date": p["expiry_date"],
            "seller_region": s["business"]["region"], "seller_type": s["business"]["type"], "seller_status": s["status"],
            "seller_years": s["business"]["years_in_business"],
            "channel": t["channel"], "acquisition_type": t["purchase"]["acquisition_type"],
            "buy_price_krw": t["purchase"]["buy_price_krw"], "buy_date": t["purchase"]["buy_date"],
            "listed_date": t["listing"]["listed_date"], "listed_price_krw": t["listing"]["listed_price_krw"],
            "listing_quantity": t["listing"]["quantity"],
            "status": t["outcome"]["status"], "observed_at": t["outcome"]["observed_at"], "quantity_sold": t["outcome"]["quantity_sold"],
            "sold_price_krw": t["outcome"]["sold_price_krw"], "channel_fee_krw": t["outcome"]["channel_fee_krw"],
            "shipping_cost_krw": t["outcome"]["shipping_cost_krw"],
            "duration_days": t["duration_days"], "censored": t["censored"], "recovery_rate": t["recovery_rate"],
            "latent_T_days": l["T_latent_days"], "latent_T_base_days": l["T_base_days"], "latent_mult_total": l["multiplier_total"],
            "latent_mult_grade": l["multipliers"]["grade"], "latent_mult_price": l["multipliers"]["price"],
            "latent_mult_channel": l["multipliers"]["channel"], "latent_price_ratio_p": l["price_ratio_p"],
            "latent_market_ratio_r": meta["product_latent"][p["product_id"]]["r_market_ratio"],
            "latent_W_days": l["withdraw_days_W"], "latent_D_days": l["dispose_days_D"], "elapsed_days": l["elapsed_days"],
        })
    return pd.DataFrame(rows)


def write_outputs(tables: dict[str, list[dict]], meta: dict, out_dir: Path, params_path: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    header = {
        "product": "D009 합성 상품 레코드 — 전부 합성(가상) 데이터. 실제 상품·거래 아님. 브랜드명은 가명.",
        "transaction": "D009 합성 거래 결과 레코드 — 전부 합성(가상) 데이터. product_id·seller_id 는 같은 폴더의 product.jsonl·seller.jsonl 참조.",
        "seller": "D009 합성 판매자 이력 레코드 — 전부 합성(가상) 데이터. 실제 판매자·사업체 정보 아님.",
        "market_price": "D009 합성 시세 관측 레코드 — 전부 합성(가상) 데이터(source.collected_by=synthetic). 플랫폼 식별자는 자리표시용.",
    }
    paths = {}
    for name, recs in tables.items():
        p = out_dir / f"{name}.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"# {header[name]} seed={meta['seed']} 기준일={meta['reference_date']} (python -m src.data.synth)\n")
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        paths[name] = p
    meta = dict(meta)
    meta["params_path"] = str(params_path.relative_to(REPO_ROOT)) if params_path.is_relative_to(REPO_ROOT) else str(params_path)
    meta["params_sha256"] = hashlib.sha256(params_path.read_bytes()).hexdigest()
    meta_path = out_dir.parent / f"{out_dir.name}_meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    paths["meta"] = meta_path
    df = flatten(tables, meta)
    try:
        pq = out_dir.parent / f"{out_dir.name}.parquet"
        df.to_parquet(pq, index=False)
        paths["parquet"] = pq
    except ImportError as e:   # pyarrow/fastparquet 없으면 CSV 로 대체 저장(같은 열)
        csv = out_dir.parent / f"{out_dir.name}_flat.csv"
        df.to_csv(csv, index=False)
        paths["flat_csv"] = csv
        print(f"[warn] parquet 저장 생략({e}) → {csv.name} 로 대체", file=sys.stderr)
    return paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--params", type=Path, default=DEFAULT_PARAMS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="JSONL 출력 폴더. parquet·meta 는 <out>.parquet, <out>_meta.json")
    ap.add_argument("--n-products", type=int, default=1000)
    ap.add_argument("--n-sellers", type=int, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--reference-date", type=lambda s: date.fromisoformat(s), default=None)
    ap.add_argument("--expiry-anchor", choices=["reference", "buy"], default=None)
    ap.add_argument("--summary", type=Path, default=None, help="요약 텍스트 저장 경로(생략하면 stdout 만)")
    a = ap.parse_args(argv)
    params = load_params(a.params)
    tables, meta = generate(params, a.seed, a.n_products, a.n_sellers, a.reference_date, a.expiry_anchor)
    paths = write_outputs(tables, meta, a.out, a.params)
    text = summarize(tables, meta, params)
    print(text)
    if a.summary:
        a.summary.parent.mkdir(parents=True, exist_ok=True)
        a.summary.write_text(text + "\n", encoding="utf-8")
    print("\n저장:", ", ".join(f"{k}={p.relative_to(REPO_ROOT) if p.is_relative_to(REPO_ROOT) else p}" for k, p in paths.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
