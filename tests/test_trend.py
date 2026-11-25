"""D017 — 트렌드 소스 목록·키워드 매핑 검증기와 네이버 데이터랩 클라이언트(요청 빌더·파서·앵커 정규화) 테스트. 네트워크 없음."""
from datetime import date

import pytest

from src.collect import trend_naver as tn
from src.collect import trend_sources as ts


# ----- 소스 목록 / 키워드 매핑 ---------------------------------------------------
def test_real_trend_sources_validate_and_key_numbers():
    doc = ts.load_yaml(ts.SOURCES_PATH)
    assert ts.validate_sources(doc) == []
    kw = ts.load_yaml(ts.KEYWORDS_PATH)
    assert ts.validate_keywords(kw) == []
    assert len(doc["sources"]) == 10
    assert len(kw["groups"]) == 30
    # 키워드 단위·자동·clear 소스는 아직 0개 — 이 숫자가 바뀌면 문서(trend_sources.md)도 바꿔야 한다
    assert [s["platform"] for s in doc["sources"] if s["unit"] == "keyword" and s["access"] == "api" and s["tos_status"] == "clear"] == []
    assert ts.rank(doc)[0][0] == "naver_datalab_search"


def test_validate_sources_rules():
    base = {"platform": "x_src", "name": "x", "signal": "relative_index", "unit": "keyword", "granularity": ["date"],
            "earliest": None, "access": "api", "covers": ["FD"], "tos_status": "unknown", "legal_notes": "", "limits": "",
            "verified": False, "evidence": [], "priority": 3}
    assert ts.validate_sources({"sources": [base]}, {"FD"}) == []
    bad_clear = dict(base, tos_status="clear")
    assert any("clear" in e for e in ts.validate_sources({"sources": [bad_clear]}, {"FD"}))
    bad_prohib = dict(base, tos_status="prohibited", priority=1)
    assert any("prohibited" in e for e in ts.validate_sources({"sources": [bad_prohib]}, {"FD"}))
    dup = [base, dict(base)]
    assert any("중복" in e for e in ts.validate_sources({"sources": dup}, {"FD"}))


def test_validate_keywords_rules():
    leaves = ["A-A-AAA", "B-B-BBB"]
    ok = {"anchor": {"keywords": ["생수"]}, "groups": [{"code": "A-A-AAA", "keywords": ["x"]}, {"code": "B-B-BBB", "keywords": ["y"]}]}
    assert ts.validate_keywords(ok, leaves) == []
    missing = {"anchor": {"keywords": ["생수"]}, "groups": [{"code": "A-A-AAA", "keywords": ["x"]}]}
    assert any("누락" in e for e in ts.validate_keywords(missing, leaves))
    anchor_in = {"anchor": {"keywords": ["x"]}, "groups": ok["groups"]}
    assert any("앵커" in e for e in ts.validate_keywords(anchor_in, leaves))
    too_many = {"anchor": {"keywords": ["생수"]}, "groups": [{"code": "A-A-AAA", "keywords": [str(i) for i in range(21)]}, ok["groups"][1]]}
    assert any("21개" in e for e in ts.validate_keywords(too_many, leaves))


# ----- 요청 빌더 ------------------------------------------------------------------
def test_build_request_limits():
    g = [{"groupName": "G", "keywords": ["a"]}]
    body = tn.build_request(date(2026, 10, 1), date(2026, 11, 24), "date", g, device="mo", ages=["3", "4"])
    assert body["startDate"] == "2026-10-01" and body["device"] == "mo" and body["ages"] == ["3", "4"]
    assert "gender" not in body
    with pytest.raises(tn.TrendRequestError):
        tn.build_request(date(2015, 12, 31), date(2016, 1, 2), "date", g)
    with pytest.raises(tn.TrendRequestError):
        tn.build_request(date(2026, 1, 1), date(2026, 1, 2), "day", g)
    with pytest.raises(tn.TrendRequestError):
        tn.build_request(date(2026, 1, 1), date(2026, 1, 2), "date", [{"groupName": str(i), "keywords": ["a"]} for i in range(6)])
    with pytest.raises(tn.TrendRequestError):
        tn.build_request(date(2026, 1, 1), date(2026, 1, 2), "date", [{"groupName": "G", "keywords": [str(i) for i in range(21)]}])
    with pytest.raises(tn.TrendRequestError):
        tn.build_request(date(2026, 1, 1), date(2026, 1, 2), "date", g, ages=["12"])


def test_plan_calls_puts_anchor_in_every_call_and_covers_all_groups():
    anchor, groups = tn.load_keyword_groups()
    calls = tn.plan_calls(anchor, groups, date(2026, 10, 1), date(2026, 11, 24), "date")
    assert len(calls) == 8
    seen = []
    for c in calls:
        names = [g["groupName"] for g in c["keywordGroups"]]
        assert names[0] == "ANCHOR" and len(names) <= tn.MAX_GROUPS
        seen += names[1:]
    assert sorted(seen) == sorted(g["groupName"] for g in groups) and len(seen) == 30


# ----- 파서 / 정규화 ------------------------------------------------------------------
def test_parse_doc_example_and_detect_per_request():
    rows = tn.parse_response(tn.DOC_EXAMPLE_RESPONSE)
    assert len(rows) == 8
    assert tn.detect_normalization(rows) == "per_request"
    norm = tn.normalize_by_anchor(rows, anchor="한글", mode="period")
    assert norm[("영어", "2023-03-01")] == pytest.approx(29.68029 / 100)
    assert ("한글", "2023-03-01") not in norm
    mean_norm = tn.normalize_by_anchor(rows, anchor="한글", mode="mean")
    assert mean_norm[("영어", "2023-03-01")] == pytest.approx(29.68029 / ((70.15342 + 74.21207 + 100 + 84.46829) / 4))


def test_detect_per_group_and_missing_period():
    resp = {"results": [
        {"title": "A", "keywords": ["a"], "data": [{"period": "2026-01-01", "ratio": 100}, {"period": "2026-01-02", "ratio": 50}]},
        {"title": "B", "keywords": ["b"], "data": [{"period": "2026-01-01", "ratio": 20}, {"period": "2026-01-03", "ratio": 100}]},
    ]}
    rows = tn.parse_response(resp)
    assert tn.detect_normalization(rows) == "per_group"
    # 빠진 구간은 행이 없고, 앵커가 없는 구간은 정규화 결과에서도 빠진다
    norm = tn.normalize_by_anchor(rows, anchor="A")
    assert set(norm) == {("B", "2026-01-01")}
    with pytest.raises(tn.TrendRequestError):
        tn.normalize_by_anchor(rows, anchor="없음")


def test_to_search_index_scale():
    vals = {("g", "d0"): 0.0, ("g", "d1"): 1.0, ("g", "d2"): 2.0, ("g", "d3"): 5.0}
    idx = tn.to_search_index(vals, cap_multiple=2.0)
    assert idx == {("g", "d0"): 0, ("g", "d1"): 50, ("g", "d2"): 100, ("g", "d3"): 100}


def test_call_without_key_is_blocked(monkeypatch):
    monkeypatch.delenv("NCP_APIGW_API_KEY_ID", raising=False)
    monkeypatch.delenv("NCP_APIGW_API_KEY", raising=False)
    with pytest.raises(tn.TrendRequestError):
        tn.call({"startDate": "2026-01-01"})
