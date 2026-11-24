"""src/collect/sources.py 테스트 (D016). 실제 목록 파일 검증 + 작은 가짜 목록으로 커버리지·순위 규칙 확인."""
import copy

import pytest

from src.collect import sources as S


@pytest.fixture(scope="module")
def doc():
    return S.load_sources()


@pytest.fixture(scope="module")
def cats():
    return S.load_categories()


def test_real_catalogue_validates(doc):
    assert S.validate(doc) == []
    assert len(doc["sources"]) >= 10


def test_channels_are_schema_enum(doc):
    enum = set(S.channel_enum())
    assert {s["channel"] for s in doc["sources"]} <= enum
    # 시세 enum 8종 중 거래 전용 3종(자사몰·라이브·소셜)은 외부 시세 소스가 없는 것이 정상
    assert "price_comparison" in enum and "offline_store" in enum


def test_prohibited_sources_have_zero_priority(doc):
    for s in doc["sources"]:
        if s["tos_status"] == "prohibited":
            assert s["priority"] == 0
            assert S.score_source(s) == float("-inf")


def test_validate_catches_bad_rows(doc):
    bad = copy.deepcopy(doc)
    bad["sources"][0]["channel"] = "naver"          # enum 아님
    bad["sources"][1]["platform"] = "Bad-Name"      # 형식
    bad["sources"][2]["tos_status"] = "clear"; bad["sources"][2]["verified"] = False
    errs = S.validate(bad)
    assert any("enum" in e for e in errs)
    assert any("snake_case" in e for e in errs)
    assert any("clear 로 표시" in e for e in errs)


def test_coverage_counts_only_matching_condition_and_level1(cats):
    fake = {"sources": [
        {"platform": "a", "covers": ["FD"], "condition": ["new"], "tos_status": "clear", "access": "api"},
        {"platform": "b", "covers": ["FD", "FA"], "condition": ["used"], "tos_status": "unknown", "access": "crawler"},
    ]}
    per = S.coverage(fake, cats)
    assert per["FD-PR-SNK"]["new"]["count"] == {"clear": 1}
    assert per["FD-PR-SNK"]["used"]["count"] == {"unknown": 1}
    assert per["FA-AP-WOM"]["new"]["count"] == {}
    s = S.coverage_summary(per, ("clear",))
    assert s == {"new": 4, "used": 0}            # FD 소분류 4개
    a = S.automated_coverage(fake, per, ("clear", "unknown"))
    assert a == {"new": 4, "used": 0}            # b 는 crawler 라 자동 수집으로 안 셈


def test_real_catalogue_automated_coverage(doc, cats):
    """오늘 기준 핵심 숫자: 법적으로 분명(clear)하고 자동(api·file)인 소스로 덮이는 소분류."""
    per = S.coverage(doc, cats)
    auto_clear = S.automated_coverage(doc, per, ("clear",))
    assert auto_clear["used"] == 0               # 중고 시세의 clear 자동 소스는 아직 없음
    assert 0 < auto_clear["new"] < 30            # 참가격이 일부 대분류만 덮음
    auto_cc = S.automated_coverage(doc, per, ("clear", "conditional"))
    assert auto_cc == {"new": 30, "used": 30}    # 네이버 API HUB 가 허용되면 전부 덮임


def test_rank_puts_clear_api_first(doc):
    ranked = S.rank(doc)
    assert ranked[0][0] == "price_go_kr"
    assert all(sc == float("-inf") for p, sc in ranked if p in {"coupang_web", "naver_shopping_web", "enuri", "daangn"})


def test_platform_enum_proposal_excludes_prohibited(doc):
    enum = S.platform_enum_proposal(doc)
    assert "other" in enum and "enuri" not in enum and "price_go_kr" in enum
    assert enum == sorted(enum[:-1]) + ["other"]
