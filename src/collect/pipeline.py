"""시세·트렌드 수집 파이프라인 — 설정 검증·호출 예산·저장(append-only)·중복 처리·수기 폼 적재 (D018).

사용법:
    python -m src.collect.pipeline                    # 설정 검증 + 월간 호출 예산표 + 게이트 상태
    python -m src.collect.pipeline --month 2026-12
    python -m src.collect.pipeline --ingest-form data/collect/inbox/manual_price_2026-11-26.csv --products data/synth/v2/products.jsonl.gz

구성 요소
  1) 설정(data/collect/pipeline.yaml) 검증 — job 의 platform 이 소스 목록에 있고, prohibited 소스는 job 불가,
     conditional/unknown 소스는 approved=true 전엔 "계획만"(약관 게이트). dedup 키는 스키마 검증기와 같아야 한다.
  2) 월간 호출 예산 — cadence × calls_per_run 으로 한 달 호출 수를 세고 소스 한도 대비 비율을 낸다.
  3) 저장소(Store) — <root>/<table>/<platform>/<YYYY-MM>.jsonl 에 추가만 한다. 레코드마다 dedup 키와 내용 해시를 비교해
       new(처음) / dup(키·내용 같음 → 안 씀) / conflict(키 같고 내용 다름 → quarantine, 사람이 본다) 로 나눈다. 덮어쓰기는 없다.
     실행마다 manifest.jsonl 에 run_id·job·시각·건수를 남긴다.
  4) 수기 입력 폼(data/collect/manual_price_form.csv) → market_price 레코드 변환 + 스키마·교차 검증(src.schema.market_price).
  5) 트렌드 변환 — trend_naver 의 (group, period) 결과를 trend_index 레코드로 만들고 data/schema/trend_index.json 으로 검증.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from src.collect import sources as PS
from src.collect import trend_sources as TS
from src.schema import market_price as MP

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "data" / "collect" / "pipeline.yaml"
TREND_SCHEMA_PATH = REPO_ROOT / "data" / "schema" / "trend_index.json"
FORM_PATH = REPO_ROOT / "data" / "collect" / "manual_price_form.csv"

CADENCE_RUNS_PER_MONTH = {"daily": 30.0, "weekly": 52.0 / 12.0, "monthly": 1.0, "on_demand": 0.0}
PERIOD_TO_MONTH = {"month": 1.0, "day": 30.0, "hour": 30.0 * 24, "minute": 30.0 * 24 * 60}
TABLES = {"market_price", "trend_index"}
GATE_OPEN = {"clear"}  # 이 상태는 approved 없이도 실행 가능

# 수기 폼의 site(사람이 본 사이트) → platform 식별자. 모르는 값은 'other' 로 두고 notes 에 원문을 남긴다.
SITE_ALIASES = {
    "네이버쇼핑": "naver_shopping_web", "네이버": "naver_shopping_web", "naver": "naver_shopping_web",
    "쿠팡": "coupang_web", "coupang": "coupang_web",
    "번개장터": "bunjang", "번장": "bunjang", "bunjang": "bunjang",
    "중고나라": "joongna", "joongna": "joongna",
    "당근": "daangn", "당근마켓": "daangn", "daangn": "daangn",
    "다나와": "danawa", "danawa": "danawa",
    "에누리": "enuri", "enuri": "enuri",
    "11번가": "st11_openapi", "지마켓": "gmarket_auction", "옥션": "gmarket_auction", "g마켓": "gmarket_auction",
    "도매꾹": "domeggook", "참가격": "price_go_kr",
}
PLATFORM_TO_CHANNEL = {
    "naver_shopping_web": "price_comparison", "coupang_web": "open_market", "bunjang": "secondhand_app",
    "joongna": "secondhand_app", "daangn": "secondhand_app", "danawa": "price_comparison", "enuri": "price_comparison",
    "st11_openapi": "open_market", "gmarket_auction": "open_market", "domeggook": "b2b_wholesale",
    "price_go_kr": "offline_store", "other": "price_comparison",
}


# ----- 설정 ----------------------------------------------------------------------
def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def source_index() -> dict[str, dict]:
    """platform → {tos_status, access, table}. 시세 소스와 트렌드 소스를 합친다."""
    idx: dict[str, dict] = {}
    for s in PS.load_sources()["sources"]:
        idx[s["platform"]] = {"tos_status": s["tos_status"], "access": s["access"], "table": "market_price"}
    for s in TS.load_yaml(TS.SOURCES_PATH)["sources"]:
        idx[s["platform"]] = {"tos_status": s["tos_status"], "access": s["access"], "table": "trend_index"}
    return idx


def validate_config(cfg: dict, idx: dict[str, dict] | None = None) -> list[str]:
    idx = idx or source_index()
    errs: list[str] = []
    seen = set()
    for j in cfg.get("jobs", []):
        tag = f"job {j.get('id', '?')}"
        for k in ("id", "table", "platform", "cadence", "calls_per_run", "limit", "approved", "raw_retain"):
            if k not in j:
                errs.append(f"{tag}: 필드 없음 {k}")
        if j.get("id") in seen:
            errs.append(f"{tag}: id 중복")
        seen.add(j.get("id"))
        if j.get("table") not in TABLES:
            errs.append(f"{tag}: table {j.get('table')!r} 은 {sorted(TABLES)} 중 하나여야 함")
        if j.get("cadence") not in CADENCE_RUNS_PER_MONTH:
            errs.append(f"{tag}: cadence {j.get('cadence')!r} 미지원")
        src = idx.get(j.get("platform"))
        if src is None:
            errs.append(f"{tag}: platform {j.get('platform')!r} 이 price_sources/trend_sources 에 없음")
            continue
        if src["table"] != j.get("table"):
            errs.append(f"{tag}: platform {j['platform']} 은 {src['table']} 소스인데 table={j.get('table')}")
        if src["tos_status"] == "prohibited":
            errs.append(f"{tag}: tos_status=prohibited 소스는 job 으로 쓸 수 없음")
        if src["tos_status"] in GATE_OPEN and not j.get("approved") and not j.get("prerequisite"):
            errs.append(f"{tag}: clear 소스인데 approved=false 이고 prerequisite 도 없음 (게이트 기본값과 불일치)")
        if j.get("approved") and not j.get("approved_on"):
            errs.append(f"{tag}: approved=true 면 approved_on 필요")
        lim = j.get("limit")
        if lim is not None and (lim.get("period") not in PERIOD_TO_MONTH or not isinstance(lim.get("calls"), int)):
            errs.append(f"{tag}: limit 은 {{period: month|day|hour|minute, calls: int}}")
        if not isinstance(j.get("calls_per_run"), int) or j["calls_per_run"] < 0:
            errs.append(f"{tag}: calls_per_run 은 0 이상 정수")
    dk = cfg.get("dedup_keys", {})
    if dk.get("market_price") != ["product_id", "source.channel", "source.platform", "condition", "observed_at"]:
        errs.append("dedup_keys.market_price 가 src.schema.market_price.dedup_key 와 다름")
    if dk.get("trend_index") != ["category_code", "period", "time_unit", "source.platform"]:
        errs.append("dedup_keys.trend_index 가 설계와 다름")
    return errs


def gate_state(job: dict, idx: dict[str, dict]) -> str:
    """runnable(실행 가능) / planned(약관 게이트 닫힘) / never(prohibited)."""
    st = idx[job["platform"]]["tos_status"]
    if st == "prohibited":
        return "never"
    if job.get("prerequisite"):          # 키·계정·활용신청 같은 선행 조건이 남아 있음
        return "planned"
    if st in GATE_OPEN or job.get("approved"):
        return "runnable"
    return "planned"


# ----- 호출 예산 -----------------------------------------------------------------
def monthly_budget(cfg: dict, idx: dict[str, dict] | None = None) -> list[dict]:
    idx = idx or source_index()
    rows = []
    for j in cfg["jobs"]:
        runs = CADENCE_RUNS_PER_MONTH[j["cadence"]]
        calls = runs * j["calls_per_run"]
        lim = j.get("limit")
        limit_month = lim["calls"] * PERIOD_TO_MONTH[lim["period"]] if lim else None
        rows.append({
            "id": j["id"], "platform": j["platform"], "table": j["table"], "cadence": j["cadence"],
            "runs_per_month": round(runs, 2), "calls_per_month": round(calls, 1),
            "limit_per_month": limit_month,
            "share_of_limit": (calls / limit_month) if limit_month else None,
            "gate": gate_state(j, idx),
        })
    return rows


# ----- 저장소 --------------------------------------------------------------------
def _get(rec: dict, dotted: str):
    cur = rec
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def record_key(rec: dict, key_fields: list[str]) -> tuple:
    return tuple(_get(rec, f) for f in key_fields)


def content_hash(rec: dict, ignore=("created_at", "price_id", "trend_id", "run_id")) -> str:
    body = {k: v for k, v in rec.items() if k not in ignore}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


class Store:
    """append-only JSONL 저장소. 파티션 <root>/<table>/<platform>/<YYYY-MM>.jsonl, 격리 <root>/quarantine/<table>.jsonl, 매니페스트 <root>/manifest.jsonl."""

    def __init__(self, root: Path, dedup_keys: dict[str, list[str]]):
        self.root = Path(root)
        self.dedup_keys = dedup_keys
        self.root.mkdir(parents=True, exist_ok=True)

    def _partition(self, table: str, platform: str, period_field_value: str) -> Path:
        ym = period_field_value[:7]
        p = self.root / table / (platform or "unknown") / f"{ym}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _period_value(self, table: str, rec: dict) -> str:
        return rec["observed_at"] if table == "market_price" else rec["period"]

    def existing(self, table: str) -> dict[tuple, str]:
        """테이블 전체의 {dedup 키: 내용 해시}. 규모가 커지면 파티션별 색인으로 바꾼다(설계 §4)."""
        out: dict[tuple, str] = {}
        tdir = self.root / table
        if not tdir.exists():
            return out
        kf = self.dedup_keys[table]
        for p in sorted(tdir.glob("*/*.jsonl")):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        r = json.loads(line)
                        out[record_key(r, kf)] = content_hash(r)
        return out

    def ingest(self, table: str, records: list[dict], job_id: str, run_id: str | None = None) -> dict:
        run_id = run_id or f"{job_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        kf = self.dedup_keys[table]
        have = self.existing(table)
        counts = {"n_in": len(records), "n_new": 0, "n_dup": 0, "n_conflict": 0}
        seen_in_batch: dict[tuple, str] = {}
        for rec in records:
            k = record_key(rec, kf)
            h = content_hash(rec)
            prev = have.get(k, seen_in_batch.get(k))
            if prev is None:
                rec = dict(rec, run_id=rec.get("run_id", run_id))
                with open(self._partition(table, _get(rec, "source.platform"), self._period_value(table, rec)), "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                seen_in_batch[k] = h
                counts["n_new"] += 1
            elif prev == h:
                counts["n_dup"] += 1
            else:
                qdir = self.root / "quarantine"
                qdir.mkdir(exist_ok=True)
                with open(qdir / f"{table}.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps({"run_id": run_id, "reason": "same_key_different_content", "key": list(k), "record": rec}, ensure_ascii=False) + "\n")
                counts["n_conflict"] += 1
        manifest = {"run_id": run_id, "job_id": job_id, "table": table, "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **counts}
        with open(self.root / "manifest.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(manifest, ensure_ascii=False) + "\n")
        return manifest

    def manifest(self) -> list[dict]:
        p = self.root / "manifest.jsonl"
        if not p.exists():
            return []
        with open(p, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


# ----- 수기 입력 폼 ----------------------------------------------------------------
def _to_int(v: str) -> int | None:
    v = (v or "").strip().replace(",", "")
    return int(v) if v else None


def _to_bool(v: str) -> bool:
    return (v or "").strip().lower() in {"true", "1", "y", "yes", "예", "포함"}


def read_form(path: Path = FORM_PATH) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def form_to_records(rows: list[dict], start_id: int = 1, created_at: str | None = None) -> list[dict]:
    """폼 행 → market_price 레코드. 검증은 하지 않는다(validate_form_records 참조)."""
    created_at = created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = []
    for i, r in enumerate(rows):
        site_raw = (r.get("site") or "").strip()
        platform = SITE_ALIASES.get(site_raw.lower(), SITE_ALIASES.get(site_raw, "other"))
        notes = (r.get("notes") or "").strip() or None
        if platform == "other" and site_raw:
            notes = f"site={site_raw}" + (f"; {notes}" if notes else "")
        n = _to_int(r.get("n_listings")) or 0
        out.append({
            "schema_version": "0.1",
            "price_id": f"M{start_id + i:08d}",
            "product_id": (r.get("product_id") or "").strip(),
            "observed_at": (r.get("observed_at") or "").strip(),
            "source": {"channel": PLATFORM_TO_CHANNEL[platform], "platform": platform, "collected_by": "manual",
                       "query": (r.get("query") or "").strip() or None},
            "match_type": (r.get("match_type") or "similar").strip(),
            "condition": (r.get("condition") or "new").strip(),
            "unit_count": _to_int(r.get("unit_count")) or 1,
            "n_listings": n,
            "price_min_krw": _to_int(r.get("price_min_krw")) if n else None,
            "price_median_krw": _to_int(r.get("price_median_krw")) if n else None,
            "price_max_krw": _to_int(r.get("price_max_krw")) if n else None,
            "shipping_included": _to_bool(r.get("shipping_included")),
            "sold_count_30d": None,
            "search_index": None,
            "notes": notes[:300] if notes else None,
            "created_at": created_at,
        })
    return out


def validate_form_records(records: list[dict], products: dict[str, dict] | None, today: date) -> tuple[list[str], list[str]]:
    """(오류 목록, 경고 목록) — src.schema.market_price 의 3단계 검증을 그대로 쓴다."""
    warns: dict[int, list[str]] = {}
    errs = MP.validate_records(records, products=products, today=today, warnings=warns)
    return [e for es in errs.values() for e in es], [w for ws in warns.values() for w in ws]


# ----- 트렌드 변환 -----------------------------------------------------------------
def load_trend_schema(path: Path = TREND_SCHEMA_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        schema = json.load(f)
    Draft202012Validator.check_schema(schema)
    return schema


def trend_to_records(rows, anchor_group: str, anchor_keyword: str, normalization: str, rel: dict, idx: dict,
                     platform: str, time_unit: str, run_id: str, keywords: dict[str, list[str]] | None = None,
                     keywords_version: str | None = None, start_id: int = 1, collected_by: str = "api") -> list[dict]:
    """trend_naver 결과 → trend_index 레코드. rows: TrendRow 목록(앵커 포함), rel/idx: normalize_by_anchor/to_search_index 출력.
    per_request 가 아니면(그룹별 정규화·판별 불가) rel_to_anchor·search_index 는 null 로 둔다 — 비교 불가능한 숫자를 저장하지 않는다."""
    anchor_ratio = {r.period: r.ratio for r in rows if r.group == anchor_group}
    per_request = normalization == "per_request"
    out, n = [], start_id
    for r in rows:
        if r.group == anchor_group:
            continue
        key = (r.group, r.period)
        out.append({
            "schema_version": "0.1", "trend_id": f"T{n:08d}", "category_code": r.group, "period": r.period,
            "time_unit": time_unit,
            "source": {"platform": platform, "collected_by": collected_by,
                       "keywords": (keywords or {}).get(r.group), "keywords_version": keywords_version},
            "ratio": r.ratio, "anchor": anchor_keyword, "anchor_ratio": anchor_ratio.get(r.period),
            "normalization": normalization,
            "rel_to_anchor": round(rel[key], 6) if per_request and key in rel else None,
            "search_index": idx.get(key) if per_request else None,
            "run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        n += 1
    return out


def validate_trend_records(records: list[dict], schema: dict | None = None) -> list[str]:
    schema = schema or load_trend_schema()
    v = Draft202012Validator(schema, format_checker=FormatChecker())
    errs = []
    seen = set()
    for rec in records:
        for e in sorted(v.iter_errors(rec), key=lambda e: list(e.path)):
            errs.append(f"[schema] {rec.get('trend_id', '?')}: {'/'.join(str(p) for p in e.path) or '(root)'} — {e.message}")
        k = (rec.get("category_code"), rec.get("period"), rec.get("time_unit"), (rec.get("source") or {}).get("platform"))
        if k in seen:
            errs.append(f"[cross] {rec.get('trend_id')}: dedup 키 중복 {k}")
        seen.add(k)
        if rec.get("normalization") == "per_group" and (rec.get("rel_to_anchor") is not None or rec.get("search_index") is not None):
            errs.append(f"[cross] {rec.get('trend_id')}: per_group 인데 rel_to_anchor/search_index 가 있음")
        if rec.get("normalization") == "per_request" and rec.get("anchor_ratio") and rec.get("rel_to_anchor") is not None:
            if abs(rec["ratio"] / rec["anchor_ratio"] - rec["rel_to_anchor"]) > 1e-4:
                errs.append(f"[cross] {rec.get('trend_id')}: rel_to_anchor ≠ ratio/anchor_ratio")
    return errs


# ----- CLI ------------------------------------------------------------------------
def format_budget(rows: list[dict]) -> str:
    lines = [f"{'job':<26}{'platform':<28}{'cadence':<10}{'runs/월':>8}{'호출/월':>10}{'한도/월':>12}{'비율':>8}  gate"]
    for r in rows:
        lim = f"{int(r['limit_per_month']):,}" if r["limit_per_month"] else "-"
        share = f"{r['share_of_limit']*100:.1f}%" if r["share_of_limit"] is not None else "-"
        lines.append(f"{r['id']:<26}{r['platform']:<28}{r['cadence']:<10}{r['runs_per_month']:>8}{r['calls_per_month']:>10,.0f}{lim:>12}{share:>8}  {r['gate']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=CONFIG_PATH)
    ap.add_argument("--ingest-form", type=Path, default=None)
    ap.add_argument("--products", type=Path, default=None)
    ap.add_argument("--store", type=Path, default=None)
    ap.add_argument("--today", type=date.fromisoformat, default=date.today())
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    idx = source_index()
    errs = validate_config(cfg, idx)
    print(f"pipeline.yaml v{cfg['version']} — job {len(cfg['jobs'])}개, 검증 오류 {len(errs)}건")
    for e in errs:
        print("  -", e)
    rows = monthly_budget(cfg, idx)
    print(format_budget(rows))
    gates = defaultdict(list)
    for r in rows:
        gates[r["gate"]].append(r["id"])
    print("게이트: " + " / ".join(f"{g} {len(v)} ({', '.join(v)})" for g, v in gates.items()))

    if a.ingest_form:
        products = None
        if a.products:
            products = MP.load_products(a.products)
        recs = form_to_records(read_form(a.ingest_form))
        ferrs, fwarns = validate_form_records(recs, products, a.today)
        print(f"폼 {a.ingest_form}: 행 {len(recs)}, 오류 {len(ferrs)}, 경고 {len(fwarns)}")
        for e in ferrs[:20]:
            print("  -", e)
        if not ferrs and a.store:
            st = Store(a.store, cfg["dedup_keys"])
            print("적재:", st.ingest("market_price", recs, job_id="J04_manual_form"))
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
