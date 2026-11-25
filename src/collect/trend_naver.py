"""네이버 데이터랩 검색어 트렌드(API HUB) 클라이언트 — 요청 빌더·응답 파서·앵커 정규화 (D017).

사용법:
    python -m src.collect.trend_naver --dry-run                 # 키 없이: 소분류 30개 호출 계획 + 문서 예시 응답으로 파서·정규화 점검
    python -m src.collect.trend_naver --start 2026-10-01 --end 2026-11-24 --time-unit date   # 키가 있으면 실제 호출
        키: 환경변수 NCP_APIGW_API_KEY_ID / NCP_APIGW_API_KEY (네이버 클라우드 콘솔 > NAVER API HUB > 인증 정보)

설계 (공식 문서 api.ncloud-docs.com/docs/naver-api-hub-search-trend.md, 2026-11-25 확인):
  - POST https://naverapihub.apigw.ntruss.com/search-trend/v1/search
  - 요청: startDate(2016-01-01 이후)·endDate·timeUnit(date|week|month)·keywordGroups(≤5쌍, 쌍당 keywords ≤20)·device·gender·ages
  - 응답: results[].title / keywords / data[].{period, ratio}. ratio = "구간별 결과에서 가장 큰 값을 100으로 설정한 상댓값".
  - 문서가 "최대 100" 이 요청 전체 기준인지 그룹별인지 명시하지 않는다. 요청 전체 기준이면(예시 응답은 이쪽) 호출마다 같은
    앵커 그룹을 넣고 앵커의 ratio 로 나눠 호출 간 비교가 가능한 "앵커 대비 배수" 로 바꿀 수 있다(normalize_by_anchor).
    그룹별 기준이면 앵커 정규화가 무의미하고 키워드 간 비교는 불가능하다 — detect_normalization 이 응답에서 판별한다.
  - 소분류 30개 = 호출당 (5 − 앵커 1) = 4 그룹 → 8 호출. 월 한도(D016 가이드 기준 50,000)에 비해 무시할 수준.

이 모듈은 네트워크 없이도 전부 테스트된다(요청 빌더·파서·정규화는 순수 함수). 실제 호출은 call() 하나이며 urllib 만 쓴다.
약관(7.3③ 저장·가공 금지) 확인 전에는 응답을 저장소에 축적하지 않는다 — call() 의 결과를 파일로 남기는 것은 호출자 책임.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
KEYWORDS_PATH = REPO_ROOT / "data" / "collect" / "trend_keywords.yaml"
ENDPOINT = "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"
EARLIEST = date(2016, 1, 1)
TIME_UNITS = ("date", "week", "month")
MAX_GROUPS = 5
MAX_KEYWORDS_PER_GROUP = 20
DEVICES = {None, "pc", "mo"}
GENDERS = {None, "m", "f"}
AGES = {str(i) for i in range(1, 12)}

# 공식 문서(.md)의 예시 응답 — 네트워크 없이 파서·정규화를 점검하는 고정 입력. 값은 문서 그대로.
DOC_EXAMPLE_RESPONSE = {
    "startDate": "2023-01-01", "endDate": "2023-06-30", "timeUnit": "month",
    "results": [
        {"title": "한글", "keywords": ["한글", "korean"],
         "data": [{"period": "2023-01-01", "ratio": 70.15342}, {"period": "2023-02-01", "ratio": 74.21207},
                  {"period": "2023-03-01", "ratio": 100}, {"period": "2023-04-01", "ratio": 84.46829}]},
        {"title": "영어", "keywords": ["영어", "english"],
         "data": [{"period": "2023-01-01", "ratio": 26.67976}, {"period": "2023-02-01", "ratio": 25.81815},
                  {"period": "2023-03-01", "ratio": 29.68029}, {"period": "2023-04-01", "ratio": 27.57161}]},
    ],
}


class TrendRequestError(ValueError):
    pass


# ----- 요청 빌더 -----------------------------------------------------------------
def build_request(start: date, end: date, time_unit: str, groups: list[dict],
                  device: str | None = None, gender: str | None = None, ages: list[str] | None = None) -> dict:
    """문서의 제약을 전부 검사한 뒤 요청 본문(dict)을 돌려준다. 위반이면 TrendRequestError."""
    if start < EARLIEST:
        raise TrendRequestError(f"startDate {start} < 2016-01-01")
    if end < start:
        raise TrendRequestError("endDate < startDate")
    if time_unit not in TIME_UNITS:
        raise TrendRequestError(f"timeUnit {time_unit!r} not in {TIME_UNITS}")
    if not 1 <= len(groups) <= MAX_GROUPS:
        raise TrendRequestError(f"keywordGroups {len(groups)}개 — 1~{MAX_GROUPS}")
    names = [g["groupName"] for g in groups]
    if len(set(names)) != len(names):
        raise TrendRequestError("groupName 중복")
    for g in groups:
        kws = g["keywords"]
        if not 1 <= len(kws) <= MAX_KEYWORDS_PER_GROUP:
            raise TrendRequestError(f"{g['groupName']}: keywords {len(kws)}개 — 1~{MAX_KEYWORDS_PER_GROUP}")
        if any(not isinstance(k, str) or not k.strip() for k in kws):
            raise TrendRequestError(f"{g['groupName']}: 빈 키워드")
    if device not in DEVICES or gender not in GENDERS:
        raise TrendRequestError("device/gender 값")
    if ages is not None and not set(ages) <= AGES:
        raise TrendRequestError(f"ages {ages} — '1'~'11'")
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "timeUnit": time_unit,
            "keywordGroups": [{"groupName": g["groupName"], "keywords": list(g["keywords"])} for g in groups]}
    if device:
        body["device"] = device
    if gender:
        body["gender"] = gender
    if ages:
        body["ages"] = list(ages)
    return body


def load_keyword_groups(path: Path = KEYWORDS_PATH) -> tuple[dict, list[dict]]:
    """trend_keywords.yaml → (anchor 그룹, 소분류 그룹 목록). groupName = 소분류 코드."""
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    anchor = {"groupName": doc["anchor"]["groupName"], "keywords": list(doc["anchor"]["keywords"])}
    groups = [{"groupName": g["code"], "keywords": list(g["keywords"])} for g in doc["groups"]]
    return anchor, groups


def plan_calls(anchor: dict, groups: list[dict], start: date, end: date, time_unit: str) -> list[dict]:
    """앵커를 모든 호출에 넣고 나머지 자리(MAX_GROUPS − 1)에 소분류 그룹을 채운 요청 본문 목록."""
    per_call = MAX_GROUPS - 1
    return [build_request(start, end, time_unit, [anchor] + groups[i:i + per_call])
            for i in range(0, len(groups), per_call)]


# ----- 응답 파서 -----------------------------------------------------------------
@dataclass(frozen=True)
class TrendRow:
    group: str
    period: str
    ratio: float


def parse_response(resp: dict) -> list[TrendRow]:
    """응답 → 긴 형식 행. 빠진 구간(문서 예시에 있음)은 행을 만들지 않는다(0 으로 채우지 않음)."""
    rows: list[TrendRow] = []
    for r in resp.get("results", []):
        for d in r.get("data", []):
            rows.append(TrendRow(r["title"], d["period"], float(d["ratio"])))
    return rows


def detect_normalization(rows: list[TrendRow], tol: float = 1e-6) -> str:
    """'max=100' 이 요청 전체 기준인지 그룹별 기준인지 응답에서 추정.
    - 모든 그룹의 최대가 100 이면 'per_group'(그룹별 정규화 — 앵커 정규화 불가)
    - 정확히 한 그룹만 100 이고 나머지는 100 미만이면 'per_request'
    - 그 외(100 이 하나도 없음 등) 'unknown'."""
    by_group: dict[str, float] = {}
    for r in rows:
        by_group[r.group] = max(by_group.get(r.group, float("-inf")), r.ratio)
    if not by_group:
        return "unknown"
    at100 = [g for g, m in by_group.items() if abs(m - 100.0) < tol]
    if len(at100) == len(by_group) and len(by_group) > 1:
        return "per_group"
    if len(at100) == 1:
        return "per_request"
    return "unknown"


def normalize_by_anchor(rows: list[TrendRow], anchor: str, mode: str = "period") -> dict[tuple[str, str], float]:
    """앵커 대비 배수로 재척도. 반환 {(group, period): value}.
    mode='period'  : 같은 구간의 앵커 ratio 로 나눔 → "그 날 앵커 검색량 대비 배수" (앵커의 시간 변동을 제거; 시즌 비교에 적합)
    mode='mean'    : 앵커의 기간 평균으로 나눔 → 앵커 평균 대비 배수 (키워드 자체의 시간 추세 보존)
    앵커가 없거나 앵커 ratio 가 0 인 구간은 건너뛴다. per_request 정규화일 때만 의미가 있다."""
    anchor_rows = {r.period: r.ratio for r in rows if r.group == anchor}
    if not anchor_rows:
        raise TrendRequestError(f"앵커 그룹 {anchor!r} 이 응답에 없음")
    out: dict[tuple[str, str], float] = {}
    if mode == "mean":
        base = sum(anchor_rows.values()) / len(anchor_rows)
        if base <= 0:
            raise TrendRequestError("앵커 평균 ratio 가 0")
        for r in rows:
            if r.group != anchor:
                out[(r.group, r.period)] = r.ratio / base
        return out
    for r in rows:
        if r.group == anchor:
            continue
        a = anchor_rows.get(r.period)
        if a:  # None 또는 0 은 건너뜀
            out[(r.group, r.period)] = r.ratio / a
    return out


def to_search_index(values: dict[tuple[str, str], float], cap_multiple: float = 2.0) -> dict[tuple[str, str], int]:
    """앵커 대비 배수 → market_price.search_index(0~100 정수).
    규칙(가정): 앵커와 같은 검색량 = 50, 앵커의 cap_multiple 배 이상 = 100, 0 = 0. 선형. D018 에서 분포를 보고 재조정."""
    out = {}
    for k, v in values.items():
        s = 100.0 * v / cap_multiple
        out[k] = int(round(max(0.0, min(100.0, s))))
    return out


# ----- 실제 호출 (키 필요) ---------------------------------------------------------
def call(body: dict, key_id: str | None = None, key: str | None = None, timeout: float = 20.0) -> dict:
    key_id = key_id or os.environ.get("NCP_APIGW_API_KEY_ID")
    key = key or os.environ.get("NCP_APIGW_API_KEY")
    if not key_id or not key:
        raise TrendRequestError("API HUB 키 없음 (NCP_APIGW_API_KEY_ID / NCP_APIGW_API_KEY)")
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
                                 headers={"X-NCP-APIGW-API-KEY-ID": key_id, "X-NCP-APIGW-API-KEY": key,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ----- CLI ------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--start", default="2026-10-01")
    ap.add_argument("--end", default="2026-11-24")
    ap.add_argument("--time-unit", default="date", choices=TIME_UNITS)
    ap.add_argument("--keywords", type=Path, default=KEYWORDS_PATH)
    a = ap.parse_args(argv)
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    anchor, groups = load_keyword_groups(a.keywords)
    calls = plan_calls(anchor, groups, start, end, a.time_unit)
    n_kw = sum(len(g["keywords"]) for g in groups)
    print(f"키워드 그룹 {len(groups)}개(키워드 {n_kw}개) + 앵커 {anchor['keywords']} → 호출 {len(calls)}건 "
          f"({a.start}~{a.end}, {a.time_unit}); 월 1회 갱신 시 월 {len(calls)}건, 주 1회 {len(calls) * 4}~{len(calls) * 5}건")
    for i, c in enumerate(calls, 1):
        print(f"  call {i}: groups={[g['groupName'] for g in c['keywordGroups']]}")
    print()
    have_key = bool(os.environ.get("NCP_APIGW_API_KEY_ID") and os.environ.get("NCP_APIGW_API_KEY"))
    if a.dry_run or not have_key:
        print("실제 호출: 건너뜀 —", "--dry-run" if a.dry_run else "API HUB 키 없음(NCP_APIGW_API_KEY_ID / NCP_APIGW_API_KEY 미설정) → blocked")
        print("문서 예시 응답으로 파서·정규화 점검:")
        rows = parse_response(DOC_EXAMPLE_RESPONSE)
        print(f"  행 {len(rows)}개, 그룹 {sorted({r.group for r in rows})}, 정규화 판별: {detect_normalization(rows)}")
        norm = normalize_by_anchor(rows, anchor="한글", mode="period")
        idx = to_search_index(norm)
        for (g, p), v in sorted(norm.items()):
            print(f"  {g} {p}: ratio→앵커('한글') 대비 {v:.4f} → search_index {idx[(g, p)]}")
        return 0
    print("실제 호출 시작 (응답은 저장하지 않고 요약만 출력 — 약관 7.3③ 확인 전)")
    for i, body in enumerate(calls, 1):
        resp = call(body)
        rows = parse_response(resp)
        print(f"  call {i}: rows={len(rows)} normalization={detect_normalization(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
