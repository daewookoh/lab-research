"""시세 수집기 v1 — 참가격(한국소비자원 생필품 가격정보, 공공데이터포털) 월간 CSV 파서 (D019, J01).

흐름:  CSV 읽기(인코딩·열 이름 별칭) → 행 정규화(날짜·가격·세일 플래그) → 품목 매칭(data/collect/price_go_kr_items.yaml)
       → 상품 × 조사일 집계(min/median/max, n) → market_price 레코드 → 3단계 검증(src.schema.market_price) → Store.ingest

사용법:
    python -m src.collect.price --csv data/collect/inbox/price_go_kr_2026-11.csv --today 2026-11-27 --dry-run
    python -m src.collect.price --csv ... --store data/collect/store            # 적재
    python -m src.collect.price --csv ... --synthetic-input                     # 입력이 합성 CSV 임을 레코드에 표시(collected_by=synthetic)

주의
  - 실제 참가격 CSV 는 D018b(사람 작업) 전까지 받지 못했다. 열 이름은 D016 에서 포털 페이지로 확인한 7개
    (상품명 GOOD_NAME·조사일·판매가격·판매업소 SORE_NAME·제조사·세일여부·원플러스원) 기준이고, 표기 변형에 대비해 별칭을 둔다.
    실 파일에서 열을 못 찾으면 ParseError 로 멈춘다(추측해서 적재하지 않는다).
  - 참가격은 오프라인 매장의 낱개 실판매가. 레코드는 channel=offline_store, condition=new, shipping_included=true,
    match_type=similar(v1 전부), collected_by=api(파일 다운로드도 공식 제공 경로라 api 로 둔다 — D018 설계와 동일).
  - 세일·1+1 행은 기본 제외(정상 소매가 기준선 용도). 제외 건수는 notes 에 남긴다.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from src.collect import pipeline as P
from src.schema import market_price as MP

REPO_ROOT = Path(__file__).resolve().parents[2]
ITEMS_PATH = REPO_ROOT / "data" / "collect" / "price_go_kr_items.yaml"
JOB_ID = "J01_price_go_kr_file"

# 열 이름 별칭 — 왼쪽 키가 내부 이름. 비교는 공백 제거·소문자 기준.
COLUMN_ALIASES: dict[str, list[str]] = {
    "good_name": ["GOOD_NAME", "상품명", "goodName", "품목명", "제품명"],
    "survey_date": ["조사일", "조사일자", "goodDwDate", "RESEARCH_DATE", "survey_date", "조사날짜", "date"],
    "price": ["판매가격", "판매가격(원)", "goodPrice", "PRICE", "가격", "판매가"],
    "store_name": ["SORE_NAME", "STORE_NAME", "판매업소", "업체명", "entpName", "판매업소명", "업소명"],
    "maker": ["제조사", "goodMaker", "MAKER", "제조업체"],
    "sale_yn": ["세일여부", "goodSaleYn", "SALE_YN", "세일"],
    "plus_one_yn": ["원플러스원", "plusoneYn", "PLUS_ONE_YN", "1+1", "원플러스원여부"],
    "good_id": ["GOOD_ID", "goodId", "상품코드", "상품ID"],
    "store_id": ["ENTP_ID", "entpId", "업체ID", "업체코드"],
}
REQUIRED_COLUMNS = ("good_name", "survey_date", "price", "store_name")
ENCODINGS = ("utf-8-sig", "cp949", "euc-kr", "utf-8")

_CONTENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|g|ml|l|매|정|개|입|봉|p|포|캡슐)", re.IGNORECASE)
_UNIT_NORMALIZE = {"kg": ("g", 1000.0), "l": ("ml", 1000.0), "p": ("매", 1.0), "포": ("개", 1.0), "입": ("개", 1.0)}


class ParseError(ValueError):
    pass


# ----- 1) CSV 읽기 --------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).lower()


def resolve_columns(header: list[str]) -> dict[str, str]:
    """내부 이름 → 실제 열 이름. 필수 열이 없으면 ParseError."""
    normed = {_norm(h): h for h in header}
    out: dict[str, str] = {}
    for key, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            if _norm(a) in normed:
                out[key] = normed[_norm(a)]
                break
    missing = [k for k in REQUIRED_COLUMNS if k not in out]
    if missing:
        raise ParseError(f"필수 열을 찾지 못함: {missing} (헤더: {header})")
    return out


def read_csv(path: Path) -> tuple[list[dict], dict[str, str], str]:
    """(행 목록[내부 열 이름], 열 매핑, 사용한 인코딩). 인코딩은 utf-8-sig → cp949 → euc-kr → utf-8 순으로 시도."""
    last_err: Exception | None = None
    for enc in ENCODINGS:
        try:
            with open(path, encoding=enc, newline="") as f:
                reader = csv.DictReader(f)
                header = reader.fieldnames or []
                colmap = resolve_columns(header)
                rows = []
                for r in reader:
                    rows.append({k: (r.get(v) or "").strip() for k, v in colmap.items()})
                return rows, colmap, enc
        except UnicodeDecodeError as e:  # 다음 인코딩
            last_err = e
            continue
    raise ParseError(f"인코딩 판별 실패: {path} ({last_err})")


# ----- 2) 행 정규화 -------------------------------------------------------------------
_DATE_FORMATS = ("%Y-%m-%d", "%Y%m%d", "%Y.%m.%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S")


def parse_date(s: str) -> date | None:
    s = (s or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_price(s: str) -> int | None:
    s = re.sub(r"[,\s원]", "", s or "")
    if not re.fullmatch(r"\d+(\.0+)?", s):
        return None
    v = int(float(s))
    return v if v > 0 else None


def parse_yn(s: str) -> bool:
    return _norm(s) in {"y", "yes", "1", "true", "예", "세일", "o"}


def parse_content(name: str) -> tuple[float, str] | None:
    """상품명에서 첫 번째 용량·수량 표기를 (값, 단위) 로. kg→g, L→ml 로 통일. 없으면 None."""
    m = _CONTENT_RE.search(name or "")
    if not m:
        return None
    v, u = float(m.group(1)), m.group(2).lower()
    if u in _UNIT_NORMALIZE:
        u2, k = _UNIT_NORMALIZE[u]
        return v * k, u2
    return v, u


def normalize_rows(rows: list[dict]) -> tuple[list[dict], Counter]:
    """날짜·가격 파싱. 실패한 행은 버리고 사유를 센다."""
    out, drop = [], Counter()
    for r in rows:
        d = parse_date(r.get("survey_date", ""))
        p = parse_price(r.get("price", ""))
        name = r.get("good_name", "")
        if not name:
            drop["no_name"] += 1
            continue
        if d is None:
            drop["bad_date"] += 1
            continue
        if p is None:
            drop["bad_price"] += 1
            continue
        out.append({
            "good_name": name, "survey_date": d, "price": p,
            "store_name": r.get("store_name", ""), "maker": r.get("maker", ""),
            "sale": parse_yn(r.get("sale_yn", "")), "plus_one": parse_yn(r.get("plus_one_yn", "")),
            "good_id": r.get("good_id", ""), "store_id": r.get("store_id", ""),
            "content": parse_content(name),
        })
    return out, drop


# ----- 3) 품목 매칭 -------------------------------------------------------------------
def load_items(path: Path = ITEMS_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for it in cfg["items"]:
        it.setdefault("include_all", [])
        it.setdefault("include_any", [])
        it.setdefault("exclude_any", [])
        it.setdefault("require_content", False)
        it.setdefault("unit_count", 1)
        it.setdefault("match_type", "similar")
    return cfg


def _content_ok(content: tuple[float, str] | None, item: dict, tol: float) -> bool:
    want = item.get("content")
    if not want:
        return True
    if content is None:
        return not item["require_content"]
    v, u = content
    wv, wu = float(want["value"]), str(want["unit"]).lower()
    if wu in _UNIT_NORMALIZE:
        wu2, k = _UNIT_NORMALIZE[wu]
        wv, wu = wv * k, wu2
    if u != wu:
        return False
    return abs(v - wv) <= tol * wv


def match_item(row: dict, items: list[dict], tol: float) -> dict | None:
    """행 하나에 대응하는 item. 목록 순서대로 첫 매칭을 돌려준다(한 행은 최대 1개 item)."""
    name = _norm(row["good_name"])
    for it in items:
        if any(_norm(t) not in name for t in it["include_all"]):
            continue
        if it["include_any"] and not any(_norm(t) in name for t in it["include_any"]):
            continue
        if any(_norm(t) in name for t in it["exclude_any"]):
            continue
        if not _content_ok(row.get("content"), it, tol):
            continue
        return it
    return None


def match_rows(rows: list[dict], cfg: dict) -> tuple[list[tuple[dict, dict]], Counter]:
    """(매칭된 (행, item) 목록, 미매칭 상품명 Counter)."""
    tol = float(cfg.get("content_tolerance", 0.25))
    matched, unmatched = [], Counter()
    for r in rows:
        it = match_item(r, cfg["items"], tol)
        if it is None:
            unmatched[r["good_name"]] += 1
        else:
            matched.append((r, it))
    return matched, unmatched


# ----- 4) 집계 → market_price 레코드 ----------------------------------------------------
def aggregate(matched: list[tuple[dict, dict]], cfg: dict, products: dict[str, dict] | None,
              start_id: int = 1, created_at: str | None = None, collected_by: str = "api",
              source_file: str | None = None) -> tuple[list[dict], dict]:
    """상품 × 조사일 → 레코드 1건. 가격은 '우리 상품 1단위(product.spec.unit_count)' 기준으로 환산해 저장하고
    unit_count 에 그 값을 적는다(products 가 없으면 item.unit_count 그대로, 환산 없음).
    세일·1+1 행은 설정에 따라 제외하고 건수만 notes 에 남긴다.
    source_file 은 notes 에 넣지 않는다(내용 해시에 들어가 파일명만 달라도 conflict 가 됨 — 점검 §4) → 적재 manifest 에 기록."""
    created_at = created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    excl_sale, excl_p1 = bool(cfg.get("exclude_sale", True)), bool(cfg.get("exclude_plus_one", True))
    groups: dict[tuple[str, date], dict] = defaultdict(lambda: {"prices": [], "n_sale": 0, "n_plus_one": 0, "items": set(), "stores": set(), "unit_count": None})
    for r, it in matched:
        pid = it["product_id"]
        g = groups[(pid, r["survey_date"])]
        g["items"].add(it["item_id"])
        g["stores"].add(r["store_name"])
        if r["sale"] and excl_sale:
            g["n_sale"] += 1
            continue
        if r["plus_one"] and excl_p1:
            g["n_plus_one"] += 1
            continue
        if products is not None and pid in products:
            target = products[pid]["spec"]["unit_count"]
            price = r["price"] * target / it["unit_count"]
        else:
            target = it["unit_count"]
            price = r["price"]
        if g["unit_count"] is None:
            g["unit_count"] = target
        g["prices"].append(price)

    records, stats = [], {"n_groups": 0, "n_empty": 0, "n_sale_excluded": 0, "n_plus_one_excluded": 0}
    for i, ((pid, d), g) in enumerate(sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0]))):
        stats["n_groups"] += 1
        stats["n_sale_excluded"] += g["n_sale"]
        stats["n_plus_one_excluded"] += g["n_plus_one"]
        prices = sorted(g["prices"])
        n = len(prices)
        if n == 0:
            stats["n_empty"] += 1
        note_bits = [f"참가격 오프라인 실판매가, 업소 {len(g['stores'])}곳, item {','.join(sorted(g['items']))}"]
        if g["n_sale"] or g["n_plus_one"]:
            note_bits.append(f"제외 세일 {g['n_sale']}·1+1 {g['n_plus_one']}")
        if collected_by == "synthetic":
            note_bits.insert(0, "합성 CSV(실데이터 아님)")
        records.append({
            "schema_version": "0.1",
            "price_id": f"M{start_id + i:08d}",
            "product_id": pid,
            "observed_at": d.isoformat(),
            "source": {"channel": cfg.get("channel", "offline_store"), "platform": cfg.get("platform", "price_go_kr"),
                       "collected_by": collected_by, "query": None},
            "match_type": "similar",
            "condition": cfg.get("condition", "new"),
            "unit_count": int(g["unit_count"] or 1),
            "n_listings": n,
            "price_min_krw": int(round(prices[0])) if n else None,
            "price_median_krw": int(round(statistics.median(prices))) if n else None,
            "price_max_krw": int(round(prices[-1])) if n else None,
            "shipping_included": bool(cfg.get("shipping_included", True)),
            "sold_count_30d": None,
            "search_index": None,
            "notes": "; ".join(note_bits)[:300],
            "created_at": created_at,
        })
    return records, stats


# ----- 5) 전체 실행 -------------------------------------------------------------------
def run(csv_path: Path, items_cfg: dict, products: dict[str, dict] | None, today: date,
        store: P.Store | None = None, collected_by: str = "api", start_id: int = 1,
        created_at: str | None = None) -> dict:
    """파일 1개 → 레코드 → 검증 → (store 가 있으면) 적재. 요약 dict 를 돌려준다. 검증 오류가 있으면 적재하지 않는다."""
    raw_rows, colmap, enc = read_csv(csv_path)
    rows, dropped = normalize_rows(raw_rows)
    matched, unmatched = match_rows(rows, items_cfg)
    per_item = Counter(it["item_id"] for _, it in matched)
    records, agg = aggregate(matched, items_cfg, products, start_id=start_id, created_at=created_at,
                             collected_by=collected_by, source_file=csv_path.name)
    warns: dict[int, list[str]] = {}
    errs = MP.validate_records(records, products=products, today=today, warnings=warns)
    out = {
        "file": str(csv_path), "encoding": enc, "columns": colmap,
        "n_raw": len(raw_rows), "n_rows": len(rows), "dropped": dict(dropped),
        "n_matched": len(matched), "n_unmatched_rows": sum(unmatched.values()), "n_unmatched_names": len(unmatched),
        "unmatched_top": unmatched.most_common(10), "per_item": dict(sorted(per_item.items())),
        "survey_dates": sorted({r["survey_date"].isoformat() for r in rows}),
        **agg,
        "n_records": len(records), "n_errors": sum(len(v) for v in errs.values()),
        "errors": [e for es in errs.values() for e in es][:20],
        "n_warnings": sum(len(v) for v in warns.values()), "warnings": [w for ws in warns.values() for w in ws][:20],
        "records": records, "ingest": None,
    }
    if store is not None and not errs:
        out["ingest"] = store.ingest("market_price", records, JOB_ID, extra={"source_file": csv_path.name, "n_rows": len(rows), "n_matched": len(matched)})
    return out


def format_summary(s: dict) -> str:
    lines = [
        f"파일 {s['file']} (인코딩 {s['encoding']}) 열 매핑 {s['columns']}",
        f"행 {s['n_raw']} → 정규화 {s['n_rows']} (버림 {s['dropped']})",
        f"매칭 {s['n_matched']}행 / 미매칭 {s['n_unmatched_rows']}행·{s['n_unmatched_names']}개 상품명 ; item 별 {s['per_item']}",
        f"조사일 {s['survey_dates']}",
        f"집계 그룹 {s['n_groups']} (빈 그룹 {s['n_empty']}, 세일 제외 {s['n_sale_excluded']}, 1+1 제외 {s['n_plus_one_excluded']}) → 레코드 {s['n_records']}",
        f"검증 오류 {s['n_errors']} / 경고 {s['n_warnings']}",
    ]
    for e in s["errors"]:
        lines.append("  " + e)
    for w in s["warnings"]:
        lines.append("  " + w)
    if s["ingest"]:
        lines.append(f"적재 {s['ingest']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="참가격 CSV → market_price 레코드 (D019 v1)")
    ap.add_argument("--csv", required=True, type=Path)
    ap.add_argument("--items", type=Path, default=ITEMS_PATH)
    ap.add_argument("--products", type=Path, default=None, help="product jsonl(.gz 가능). 없으면 예시 15건 사용")
    ap.add_argument("--today", type=str, default=None)
    ap.add_argument("--store", type=Path, default=None, help="적재할 store root (없으면 dry-run)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--synthetic-input", action="store_true", help="입력이 합성 CSV 이면 collected_by=synthetic")
    ap.add_argument("--start-id", type=int, default=1)
    ap.add_argument("--dump", type=Path, default=None, help="레코드를 JSONL 로 저장")
    a = ap.parse_args(argv)

    today = date.fromisoformat(a.today) if a.today else date.today()
    products = MP.load_products(a.products) if a.products else MP.load_products()
    cfg = load_items(a.items)
    store = None
    if a.store and not a.dry_run:
        pcfg = P.load_config()
        store = P.Store(a.store, pcfg["dedup_keys"])
    s = run(a.csv, cfg, products, today, store=store, collected_by="synthetic" if a.synthetic_input else "api", start_id=a.start_id)
    print(format_summary(s))
    if a.dump:
        with open(a.dump, "w", encoding="utf-8") as f:
            for r in s["records"]:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return 1 if s["n_errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
