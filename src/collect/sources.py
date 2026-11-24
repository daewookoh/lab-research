"""시세 데이터 소스 목록 로더·점검기 (D016).

사용법:
    python -m src.collect.sources            # 목록 검증 + 카테고리 커버리지 + D019 후보 순위 출력
    python -m src.collect.sources --yaml data/collect/price_sources.yaml

하는 일:
  1) data/collect/price_sources.yaml 을 읽어 필드·enum 을 검증한다.
     - channel 은 data/schema/market_price.json 의 source.channel enum 에 있어야 한다.
     - platform 은 snake_case ≤50자(market_price.json source.platform 제약)·유일.
     - covers 는 categories.yaml 대분류 코드여야 한다.
     - verified=true 면 evidence 가 1개 이상 있어야 한다(manual_search 예외: 외부 페이지가 없음).
  2) 소분류 30개 × 소스 커버리지를 tos_status 별로 센다 — "지금 합법적으로 자동 수집할 수 있는 소분류가 몇 개인가" 가 핵심 숫자.
  3) D019 1차 수집기 후보를 점수로 줄 세운다(규칙은 score_source 참조, 가정임).
  4) market_price.json source.platform enum 제안 목록을 출력한다(스키마는 고치지 않음 — 승인 후 D018 에서 반영).
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCES_PATH = REPO_ROOT / "data" / "collect" / "price_sources.yaml"
MARKET_PRICE_SCHEMA = REPO_ROOT / "data" / "schema" / "market_price.json"
CATEGORIES_PATH = REPO_ROOT / "data" / "schema" / "categories.yaml"

ACCESS = {"api", "file", "crawler", "manual"}
PRICE_TYPE = {"listing", "sale", "asking"}
CONDITION = {"new", "used"}
TOS = {"clear", "conditional", "prohibited", "unknown"}
PLATFORM_RE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
NO_EVIDENCE_OK = {"manual_search"}  # 외부 페이지가 없는 소스

# access 값 → market_price.json source.collected_by 값
ACCESS_TO_COLLECTED_BY = {"api": "api", "file": "api", "crawler": "crawler", "manual": "manual"}


def load_sources(path: Path = SOURCES_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def channel_enum(schema_path: Path = MARKET_PRICE_SCHEMA) -> list[str]:
    with open(schema_path, encoding="utf-8") as f:
        schema = json.load(f)
    return list(schema["properties"]["source"]["properties"]["channel"]["enum"])


def load_categories(path: Path = CATEGORIES_PATH) -> list[dict]:
    """소분류 30개를 [{code, name, level1, expiry_class, regulation}] 로 평탄화."""
    with open(path, encoding="utf-8") as f:
        cats = yaml.safe_load(f)
    out = []
    for l1 in cats["categories"]:
        for l2 in l1["children"]:
            for l3 in l2["children"]:
                out.append({"code": l3["code"], "name": l3["name"], "level1": l1["code"],
                            "expiry_class": l3["expiry_class"], "regulation": l3["regulation"]})
    return out


def validate(doc: dict, channels: list[str] | None = None, level1_codes: set[str] | None = None) -> list[str]:
    """필드·enum 검증. 오류 문자열 목록(비면 통과)."""
    channels = channels or channel_enum()
    level1_codes = level1_codes or {c["level1"] for c in load_categories()}
    errs: list[str] = []
    seen = set()
    for i, s in enumerate(doc.get("sources", [])):
        tag = f"sources[{i}] {s.get('platform', '?')}"
        for k in ("platform", "name", "channel", "access", "price_type", "condition", "covers", "sold_count",
                  "tos_status", "legal_notes", "limits", "verified", "verified_on", "evidence", "priority"):
            if k not in s:
                errs.append(f"{tag}: 필드 없음 {k}")
        p = s.get("platform", "")
        if not PLATFORM_RE.match(p):
            errs.append(f"{tag}: platform 형식 위반(snake_case ≤50자)")
        if p in seen:
            errs.append(f"{tag}: platform 중복")
        seen.add(p)
        if s.get("channel") not in channels:
            errs.append(f"{tag}: channel {s.get('channel')!r} 이 market_price.json enum 에 없음")
        if s.get("access") not in ACCESS:
            errs.append(f"{tag}: access {s.get('access')!r}")
        if s.get("price_type") not in PRICE_TYPE:
            errs.append(f"{tag}: price_type {s.get('price_type')!r}")
        if not s.get("condition") or not set(s["condition"]) <= CONDITION:
            errs.append(f"{tag}: condition {s.get('condition')!r}")
        if not s.get("covers") or not set(s["covers"]) <= level1_codes:
            errs.append(f"{tag}: covers {s.get('covers')!r} 에 대분류 코드 아닌 값")
        if s.get("tos_status") not in TOS:
            errs.append(f"{tag}: tos_status {s.get('tos_status')!r}")
        if not isinstance(s.get("priority"), int) or not 0 <= s["priority"] <= 5:
            errs.append(f"{tag}: priority 는 0~5 정수")
        if s.get("verified") and not s.get("evidence") and p not in NO_EVIDENCE_OK:
            errs.append(f"{tag}: verified=true 인데 evidence 없음")
        if s.get("tos_status") == "clear" and not s.get("verified"):
            errs.append(f"{tag}: 미확인 소스를 clear 로 표시할 수 없음")
        if s.get("priority", 0) > 0 and s.get("tos_status") == "prohibited":
            errs.append(f"{tag}: prohibited 소스는 priority 0 이어야 함")
    return errs


def coverage(doc: dict, cats: list[dict] | None = None) -> dict:
    """소분류별로 어떤 tos_status 의 소스가 몇 개 덮는지, 신품/중고별로."""
    cats = cats or load_categories()
    per_cat: dict[str, dict] = {}
    for c in cats:
        row = {"name": c["name"], "level1": c["level1"]}
        for cond in ("new", "used"):
            cnt = Counter()
            plats = defaultdict(list)
            for s in doc["sources"]:
                if c["level1"] in s["covers"] and cond in s["condition"]:
                    cnt[s["tos_status"]] += 1
                    plats[s["tos_status"]].append(s["platform"])
            row[cond] = {"count": dict(cnt), "platforms": dict(plats)}
        per_cat[c["code"]] = row
    return per_cat


def coverage_summary(per_cat: dict, statuses=("clear", "conditional")) -> dict:
    """statuses 중 하나라도 되는 소스가 있는 소분류 수 (신품/중고/자동수집 한정)."""
    out = {}
    for cond in ("new", "used"):
        n_any = sum(1 for r in per_cat.values() if any(r[cond]["count"].get(t, 0) for t in statuses))
        out[cond] = n_any
    return out


def automated_coverage(doc: dict, per_cat: dict, statuses=("clear", "conditional")) -> dict:
    """사람 손(manual) 을 빼고 api/file 로 statuses 안에서 덮이는 소분류 수."""
    auto = {s["platform"] for s in doc["sources"] if s["access"] in ("api", "file") and s["tos_status"] in statuses}
    out = {}
    for cond in ("new", "used"):
        n = 0
        for r in per_cat.values():
            plats = {p for t in statuses for p in r[cond]["platforms"].get(t, [])}
            if plats & auto:
                n += 1
        out[cond] = n
    return out


def score_source(s: dict) -> float:
    """D019 1차 수집기 후보 점수(가정). 높을수록 먼저.
    법적 안전(clear 3 / conditional 1 / unknown 0 / prohibited −∞) + 자동화(api·file 2, crawler 0, manual 0.5)
    + 커버 대분류 수 × 0.3 + 확인됨(verified) 1 + 등록가 분포를 주는지(listing·asking 0.5, sale 0.5 — 둘 다 쓸모가 다름)."""
    if s["tos_status"] == "prohibited":
        return float("-inf")
    legal = {"clear": 3, "conditional": 1, "unknown": 0}[s["tos_status"]]
    auto = {"api": 2, "file": 2, "crawler": 0, "manual": 0.5}[s["access"]]
    return legal + auto + 0.3 * len(s["covers"]) + (1 if s["verified"] else 0) + 0.5


def rank(doc: dict) -> list[tuple[str, float]]:
    rows = [(s["platform"], score_source(s)) for s in doc["sources"]]
    return sorted(rows, key=lambda r: -r[1])


def platform_enum_proposal(doc: dict) -> list[str]:
    """market_price.json source.platform enum 제안: prohibited 가 아닌 소스 전부 + 'other'."""
    return sorted(s["platform"] for s in doc["sources"] if s["tos_status"] != "prohibited") + ["other"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yaml", type=Path, default=SOURCES_PATH)
    a = ap.parse_args(argv)
    doc = load_sources(a.yaml)
    cats = load_categories()
    errs = validate(doc)
    print(f"소스 목록 v{doc['version']} ({doc['defined_on']}, {doc['status']}): {len(doc['sources'])}개 — 검증 오류 {len(errs)}건")
    for e in errs:
        print("  ERROR", e)
    print()
    print("tos_status 별:", dict(Counter(s["tos_status"] for s in doc["sources"])))
    print("access 별    :", dict(Counter(s["access"] for s in doc["sources"])))
    print("channel 별   :", dict(Counter(s["channel"] for s in doc["sources"])))
    print("verified     :", sum(1 for s in doc["sources"] if s["verified"]), "/", len(doc["sources"]))
    missing = sorted(set(channel_enum()) - {s["channel"] for s in doc["sources"]})
    print("소스 없는 채널:", missing)
    print()
    per_cat = coverage(doc, cats)
    print("소분류 커버리지 (소분류 30개 기준, 소스 수: clear/conditional/unknown/prohibited)")
    print(f"{'코드':<11}{'소분류':<22}{'신품 c/c/u/p':<14}{'중고 c/c/u/p'}")
    for code, r in per_cat.items():
        def f(cond):
            c = r[cond]["count"]
            return f"{c.get('clear',0)}/{c.get('conditional',0)}/{c.get('unknown',0)}/{c.get('prohibited',0)}"
        print(f"{code:<11}{r['name']:<22}{f('new'):<14}{f('used')}")
    print()
    for label, st in (("clear 만", ("clear",)), ("clear+conditional", ("clear", "conditional")),
                      ("clear+conditional+unknown", ("clear", "conditional", "unknown"))):
        cs = coverage_summary(per_cat, st)
        ac = automated_coverage(doc, per_cat, st)
        print(f"{label:<28} 소분류 커버: 신품 {cs['new']:>2}/30  중고 {cs['used']:>2}/30 | 자동(api·file)만: 신품 {ac['new']:>2}/30  중고 {ac['used']:>2}/30")
    print()
    print("D019 후보 순위 (score_source, 가정):")
    for p, sc in rank(doc):
        s = next(x for x in doc["sources"] if x["platform"] == p)
        print(f"  {sc:>6.1f}  {p:<20} {s['tos_status']:<12} {s['access']:<8} {s['channel']:<17} covers={len(s['covers'])} priority(수기)={s['priority']}")
    print()
    print("source.platform enum 제안:", platform_enum_proposal(doc))
    return 1 if errs else 0


if __name__ == "__main__":
    raise SystemExit(main())
