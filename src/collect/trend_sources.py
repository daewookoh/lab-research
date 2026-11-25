"""검색 트렌드 데이터 소스 목록 로더·점검기 (D017). price_sources.py(D016) 와 같은 구조.

사용법:
    python -m src.collect.trend_sources            # 목록 검증 + 키워드 매핑 검증 + 커버리지 + search_index 후보 순위

하는 일:
  1) data/collect/trend_sources.yaml 필드·enum 검증 (platform 유일·snake_case, covers 는 대분류 코드, verified→evidence,
     미확인→clear 금지, prohibited→priority 0).
  2) data/collect/trend_keywords.yaml 검증 — 소분류 30개가 빠짐·중복 없이 전부 있는지, 그룹당 키워드 1~20, 앵커가 어느 그룹에도 없는지.
  3) 소분류 30개 × tos_status 별 커버리지 (자동 api·file 한정 포함).
  4) search_index 소스 후보 점수(가정) 순위.
"""
from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

import yaml

from src.collect.sources import load_categories

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCES_PATH = REPO_ROOT / "data" / "collect" / "trend_sources.yaml"
KEYWORDS_PATH = REPO_ROOT / "data" / "collect" / "trend_keywords.yaml"

SIGNAL = {"relative_index", "click_index", "absolute_count", "sales_amount"}
UNIT = {"keyword", "category", "product_group"}
GRAN = {"date", "week", "month"}
ACCESS = {"api", "file", "crawler", "manual"}
TOS = {"clear", "conditional", "prohibited", "unknown"}
PLATFORM_RE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
REQUIRED = ("platform", "name", "signal", "unit", "granularity", "earliest", "access", "covers", "tos_status",
            "legal_notes", "limits", "verified", "evidence", "priority")


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def validate_sources(doc: dict, level1_codes: set[str] | None = None) -> list[str]:
    level1_codes = level1_codes or {c["level1"] for c in load_categories()}
    errs: list[str] = []
    seen = set()
    for i, s in enumerate(doc.get("sources", [])):
        tag = f"sources[{i}] {s.get('platform', '?')}"
        for k in REQUIRED:
            if k not in s:
                errs.append(f"{tag}: 필드 없음 {k}")
        p = s.get("platform", "")
        if not PLATFORM_RE.match(p):
            errs.append(f"{tag}: platform 형식 위반")
        if p in seen:
            errs.append(f"{tag}: platform 중복")
        seen.add(p)
        if s.get("signal") not in SIGNAL:
            errs.append(f"{tag}: signal {s.get('signal')!r}")
        if s.get("unit") not in UNIT:
            errs.append(f"{tag}: unit {s.get('unit')!r}")
        if not s.get("granularity") or not set(s["granularity"]) <= GRAN:
            errs.append(f"{tag}: granularity {s.get('granularity')!r}")
        if s.get("access") not in ACCESS:
            errs.append(f"{tag}: access {s.get('access')!r}")
        if not s.get("covers") or not set(s["covers"]) <= level1_codes:
            errs.append(f"{tag}: covers 에 대분류 코드 아닌 값")
        if s.get("tos_status") not in TOS:
            errs.append(f"{tag}: tos_status {s.get('tos_status')!r}")
        if not isinstance(s.get("priority"), int) or not 0 <= s["priority"] <= 5:
            errs.append(f"{tag}: priority 는 0~5 정수")
        if s.get("verified") and not s.get("evidence"):
            errs.append(f"{tag}: verified=true 인데 evidence 없음")
        if s.get("tos_status") == "clear" and not s.get("verified"):
            errs.append(f"{tag}: 미확인 소스를 clear 로 표시할 수 없음")
        if s.get("priority", 0) > 0 and s.get("tos_status") == "prohibited":
            errs.append(f"{tag}: prohibited 소스는 priority 0 이어야 함")
    return errs


def validate_keywords(kw: dict, leaf_codes: list[str] | None = None, max_per_group: int = 20) -> list[str]:
    leaf_codes = leaf_codes or [c["code"] for c in load_categories()]
    errs: list[str] = []
    codes = [g.get("code") for g in kw.get("groups", [])]
    dup = [c for c, n in Counter(codes).items() if n > 1]
    if dup:
        errs.append(f"소분류 중복: {dup}")
    missing = sorted(set(leaf_codes) - set(codes))
    extra = sorted(set(codes) - set(leaf_codes))
    if missing:
        errs.append(f"소분류 누락: {missing}")
    if extra:
        errs.append(f"categories.yaml 에 없는 코드: {extra}")
    anchor_kws = set(kw.get("anchor", {}).get("keywords", []))
    if not anchor_kws:
        errs.append("앵커 키워드 없음")
    all_kw: Counter = Counter()
    for g in kw.get("groups", []):
        kws = g.get("keywords") or []
        if not 1 <= len(kws) <= max_per_group:
            errs.append(f"{g.get('code')}: 키워드 {len(kws)}개 (1~{max_per_group})")
        if anchor_kws & set(kws):
            errs.append(f"{g.get('code')}: 앵커 키워드가 그룹 안에 있음 {anchor_kws & set(kws)}")
        all_kw.update(kws)
    dupkw = sorted(k for k, n in all_kw.items() if n > 1)
    if dupkw:
        errs.append(f"여러 소분류에 같은 키워드: {dupkw}")
    return errs


def coverage(doc: dict, cats: list[dict] | None = None) -> dict:
    """소분류별 tos_status 소스 수(트렌드는 신품/중고 구분 없음)."""
    cats = cats or load_categories()
    out = {}
    for c in cats:
        cnt = Counter()
        auto = Counter()
        for s in doc["sources"]:
            if c["level1"] in s["covers"]:
                cnt[s["tos_status"]] += 1
                if s["access"] in ("api", "file"):
                    auto[s["tos_status"]] += 1
        out[c["code"]] = {"name": c["name"], "count": dict(cnt), "auto": dict(auto)}
    return out


def coverage_summary(per_cat: dict, statuses=("clear", "conditional")) -> dict:
    return {"any": sum(1 for r in per_cat.values() if any(r["count"].get(t, 0) for t in statuses)),
            "auto": sum(1 for r in per_cat.values() if any(r["auto"].get(t, 0) for t in statuses))}


def score_source(s: dict) -> float:
    """search_index 소스 후보 점수(가정). 법적 안전(clear 3 / conditional 1 / unknown 0 / prohibited −∞)
    + 자동화(api·file 2, manual 0.5, crawler 0) + 단위 적합(keyword 1.5, category 1, product_group 0)
    + 일간 제공 0.5 + 확인됨 1."""
    if s["tos_status"] == "prohibited":
        return float("-inf")
    legal = {"clear": 3, "conditional": 1, "unknown": 0}[s["tos_status"]]
    auto = {"api": 2, "file": 2, "crawler": 0, "manual": 0.5}[s["access"]]
    unit = {"keyword": 1.5, "category": 1, "product_group": 0}[s["unit"]]
    return legal + auto + unit + (0.5 if "date" in s["granularity"] else 0) + (1 if s["verified"] else 0)


def rank(doc: dict) -> list[tuple[str, float]]:
    return sorted(((s["platform"], score_source(s)) for s in doc["sources"]), key=lambda r: -r[1])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yaml", type=Path, default=SOURCES_PATH)
    ap.add_argument("--keywords", type=Path, default=KEYWORDS_PATH)
    a = ap.parse_args(argv)
    doc, kw = load_yaml(a.yaml), load_yaml(a.keywords)
    cats = load_categories()
    errs = validate_sources(doc)
    kerrs = validate_keywords(kw)
    print(f"트렌드 소스 목록 v{doc['version']} ({doc['defined_on']}, {doc['status']}): {len(doc['sources'])}개 — 검증 오류 {len(errs)}건")
    for e in errs:
        print("  ERROR", e)
    n_kw = sum(len(g["keywords"]) for g in kw["groups"])
    print(f"키워드 매핑 v{kw['version']}: 그룹 {len(kw['groups'])}개·키워드 {n_kw}개·앵커 {kw['anchor']['keywords']} — 검증 오류 {len(kerrs)}건")
    for e in kerrs:
        print("  ERROR", e)
    n_cat = sum(1 for g in kw["groups"] if g.get("naver_shop_cat_id"))
    print(f"쇼핑인사이트 cat_id 채워진 소분류: {n_cat}/{len(kw['groups'])}")
    print()
    print("tos_status 별:", dict(Counter(s["tos_status"] for s in doc["sources"])))
    print("access 별    :", dict(Counter(s["access"] for s in doc["sources"])))
    print("signal 별    :", dict(Counter(s["signal"] for s in doc["sources"])))
    print("unit 별      :", dict(Counter(s["unit"] for s in doc["sources"])))
    print("verified     :", sum(1 for s in doc["sources"] if s["verified"]), "/", len(doc["sources"]))
    print()
    per_cat = coverage(doc, cats)
    for label, st in (("clear 만", ("clear",)), ("clear+conditional", ("clear", "conditional")),
                      ("clear+conditional+unknown", ("clear", "conditional", "unknown"))):
        cs = coverage_summary(per_cat, st)
        print(f"{label:<28} 소분류 커버: {cs['any']:>2}/30 | 자동(api·file)만: {cs['auto']:>2}/30")
    for st in ("clear", "conditional", "unknown"):
        plats = [s["platform"] for s in doc["sources"] if s["unit"] == "keyword" and s["access"] in ("api", "file") and s["tos_status"] == st]
        print(f"키워드 단위·자동 소스 [{st}]: {plats or '없음'}")
    print()
    print("search_index 소스 후보 순위 (score_source, 가정):")
    for p, sc in rank(doc):
        s = next(x for x in doc["sources"] if x["platform"] == p)
        print(f"  {sc:>6.1f}  {p:<30} {s['tos_status']:<12} {s['access']:<8} {s['signal']:<15} {s['unit']:<14} priority(수기)={s['priority']}")
    return 1 if errs or kerrs else 0


if __name__ == "__main__":
    raise SystemExit(main())
