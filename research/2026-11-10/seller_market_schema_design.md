# D005 — 판매자 이력 스키마 + 시세 스키마 v0.1 설계 메모

- 작성일: 2026-11-10 / 백로그 D005 / 과제1
- 산출물: `data/schema/seller.json`, `data/schema/market_price.json`(JSON Schema draft 2020-12), `src/schema/seller.py`, `src/schema/market_price.py`(로더·3단계 검증기·CLI), `data/schema/examples/seller_examples.jsonl`(합성 17건), `data/schema/examples/market_price_examples.jsonl`(합성 29건), `tests/test_seller.py`(24건), `tests/test_market_price.py`(22건), `src/schema/transaction.py` 판매자 참조 규칙 추가 + `tests/test_transaction.py` 5건 추가, `research/2026-11-10/seller_market_validation.txt`(실행 로그)
- 상태: **draft** — 연구책임자 승인 전. 데이터는 전부 **합성(가상)** 이다. 시세 레코드는 `source.collected_by=synthetic` 으로 레코드 수준에서도 합성임을 표시했다.
- 이로써 로드맵 M1 의 테이블 5개(카테고리 D002 · 상품 D003 · 거래 D004 · 판매자·시세 D005)가 모두 draft 로 정의됐다. 승인되면 M1 완료.

## 1. 판매자 이력 (`seller.json`)

**1레코드 = 1판매자(소상공인 사업체).** 프로필(`business`)과 **공급 이력(`supply_history`)** 을 한 레코드에 둔다. 공급 이력은 "재고를 한 번에 넘긴 배치" 단위 사건 목록이고, 배치 수·신고가 합·기재 정확도 비율 같은 집계값은 저장하지 않고 검증기(`history_stats`)가 계산한다(D003 의 "파생 필드는 저장하지 않는다" 원칙).

| 영역 | 필드 | 설계 의도 |
|---|---|---|
| 식별 | `seller_id` (S+6자리) | transaction.seller_id 가 참조. **사업자번호·상호·연락처는 두지 않는다**(가명화). `additionalProperties:false` 라 나중에 실수로 들어갈 수도 없다 |
| 사업체 | `business.type`(6종) / `region`(7권역+unknown) / `main_category_codes`(중분류, 1~5개) / `years_in_business` / `annual_revenue_band` / `sales_channels_used` | 판매자 단위 피처. 권역은 수거 물류(물류효율성), 운영 채널은 "판매자가 이미 쓰는 채널은 피한다" 류의 채널 적합성 규칙에 쓸 수 있다 |
| 관계 | `registered_date` / `acquisition_source` / `status`(active·dormant·churned·blocked) | 상태는 이력과 일관해야 한다(교차 검증) |
| 배치 | `batch_id`(B+8자리) / `supplied_date` / `acquisition_type` / `product_count` / `product_ids`(선택) / `declared_value_krw` / `settled_value_krw` / `description_accuracy` / `issue` | **`description_accuracy`(accurate·minor_gap·major_gap)와 `issue` 가 핵심** — "이 판매자의 기재를 얼마나 믿을 수 있나" 는 매입 심사 자동화(목표지표 2)에서 수기 개입이 필요한 건을 걸러내는 피처가 된다 |

거래(D004)와의 연결: `transaction.purchase` 는 `batch_id` 를 갖지 않는다. 대신 `seller_id` + `buy_date` = `supplied_date` 로 배치를 찾는다. 예시 18건의 거래 모두 같은 날 배치가 있어 경고 0건이다. 배치 ID 를 거래에 넣는 것이 더 명시적이지만, 실데이터가 "거래 CSV 한 장" 으로 올 가능성이 커 거래 스키마를 바꾸지 않았다. 필요해지면 v0.2 에서 `transaction.purchase.batch_id` 를 nullable 로 추가한다.

## 2. 시세 관측 (`market_price.json`)

**1레코드 = 1상품 × 1소스(채널·플랫폼) × 1상태(신품/중고) × 1관측일.** 같은 상품을 다른 날 다시 조회하면 새 레코드라 시계열이 된다(예시 M1→M3: 감자칩 1주 간격). 수집 파이프라인(D016~D019)의 출력 형식이기도 하다.

| 영역 | 필드 | 설계 의도 |
|---|---|---|
| 대상 | `product_id` + `match_type`(exact·same_model_other_pack·similar) + `unit_count` | 장기재고는 동일 상품의 등록물이 없는 경우가 많다. 유사 상품 시세도 담되 관계를 명시하고, 묶음 수가 다르면 `unit_count` 로 환산한다(환산 중앙값 = median × product.unit_count ÷ unit_count). `similar` 는 M7 유사상품 검색이 자동으로 채울 자리 |
| 소스 | `source.channel`(거래 7종 + `price_comparison`) / `platform` / `collected_by`(manual·api·crawler·**synthetic**) / `query` | 채널별로 따로 두어야 "같은 상품이 채널마다 얼마에 팔리나" 를 채널 추천(M13)에 쓸 수 있다. `platform` 은 D016 소스 조사 후 enum 으로 고정 |
| 값 | `n_listings` + `price_min/median/max_krw` + `shipping_included` | 평균 대신 **중앙값**을 대표 시세로 둔다(장기재고 시세는 끝값이 지저분하다). `n_listings=0` 이면 세 값 모두 null(시세 없음도 정보다) |
| 보조 신호 | `sold_count_30d` / `search_index`(0~100) | 각각 판매속도·시장수요 항목의 외부 신호. `search_index` 는 D017 소스 확정 전까지 자리만 |

저장하지 않는 파생값: 환산 중앙값, 정상가 대비 비율(`price_ratio`). 가격경쟁력 항목의 기초 피처이지만 정상가가 바뀌면 같이 바뀌어야 하므로 계산으로 얻는다.

## 3. 교차 검증 규칙

판매자(오류): 중분류 코드 존재 / `registered_date ≤ 기준일` / 배치 `batch_id` 유일·시간순·`registered_date ≤ supplied_date ≤ 기준일` / `product_ids` 길이 = `product_count`, 각 상품 존재, 상품 `acquired_date ≤ supplied_date` / 매입 배치의 정산액 ≤ 신고가 / `accurate` 인데 기재 불일치 이슈 → 모순 / `blocked` 는 major_gap·dispute 근거 필요 / `dormant`·`churned` 는 이력 필요, `dormant` 는 마지막 공급 365일 이상 / `seller_id` 파일 내 중복.
판매자(경고): active 인데 공급 없음 / active 인데 365일 이상 공급 없음 / major_gap 비율 50% 이상.

시세(오류): 상품 존재 / `observed_at ≤ 기준일` / `n_listings=0 ⇔ 가격 null` / `min ≤ median ≤ max` / `n_listings=1` 이면 세 값 동일 / `exact` 면 `unit_count` = 상품 `unit_count` / `price_id` 유일 / 같은 (상품·채널·플랫폼·상태·관측일) 중복 금지.
시세(경고): 환산 중앙값이 정상가의 1.5배 초과(정상가·매칭 의심) / 0.1배 미만(단위 착오) / 신품인데 정상가 초과 / 표본 3개 미만 / 자사몰·라이브에서 중고 관측. 경계값(1.5·0.1·3)은 **가정**이며 D010 EDA 후 조정.

거래(D004 에 추가, 판매자 파일을 줬을 때만): `seller_id` 존재(오류) / `buy_date ≥ registered_date`(오류) / 같은 날 배치가 상품을 담고 있으면 매입·위탁 구분 일치(오류) / 같은 날 배치 없음(경고). 기존 호출 방식(`sellers` 생략)은 그대로 동작해 D004 테스트 26건을 바꾸지 않았다.

## 4. 실행 결과 (`seller_market_validation.txt`)

```
seller.json: 최상위 10필드·필수 4, 배치 항목 10필드 / 17건 오류 0, 경고 2 (S000109 major_gap 50%, S000116 공급 없음 — 의도)
  status active=14 dormant=1 churned=1 blocked=1 / 배치 24건 / accuracy accurate=15 minor_gap=7 major_gap=2 / issue 없음 16, 수량부족 3, 상태불일치 2 …
market_price.json: 최상위 17필드·필수 12 / 29건 오류 0, 경고 7 (표본<3 4건, 정상가 초과 2건, 1.59배 1건, used@own_online 1건 — 의도)
  채널 8종 모두 ≥1 / match exact=18 other_pack=3 similar=8 / new=24 used=5 / 시세 없음 1건
  정상가 대비 환산 중앙값: new median 0.81 (0.63~1.59), used median 0.42 (0.33~0.54) / 채널 간 격차(max/min) median 1.16, max 1.44
transaction.json 재실행(판매자 참조 17건): 18건 오류 0, 경고 4 (D004 와 동일 — 판매자 경고 0)
pytest: 102 passed (D002 8 + D003 17 + D004 26+5 + D005 seller 24 + market 22)
```

수치는 전부 **스키마 동작 확인용 가상값**이다. 다만 두 가지는 D008 분포 가정에 그대로 쓸 만한 구조적 관찰이다: (a) 중고 시세는 정상가의 절반 아래에 몰리고 신품 시세는 0.6~1.0 에 몰린다는 가정을 넣어 두었고, (b) 같은 상품도 채널에 따라 1.0~1.4배 격차가 나도록 만들었다. 실데이터가 없으므로 이 폭 자체는 근거가 없다.

## 5. 한계·열린 질문

- **판매자 신뢰도의 라벨링 주체.** `description_accuracy` 는 검수 담당자가 배치마다 판단해야 생기는 값이다. 현행 운영에서 검수 결과를 기록하는지, 기록한다면 어떤 단위(배치/품목)인지에 따라 이 필드는 바뀐다. → 연구책임자 확인.
- 권역 7개 구분과 업태 6종은 가정이다. 수거 물류를 거리로 계산하려면 시·군·구 단위가 필요하지만 가명화 수준과 상충한다.
- 시세는 "등록가" 관측이라 실제 체결가와 다르다. 체결가는 `sold_count_30d` 같은 간접 신호로만 본다. 중고앱은 체결 여부가 노출되므로 D016 에서 소스별로 어떤 값이 보이는지 정리해야 한다.
- 배송비 포함 여부(`shipping_included`)를 레코드별로 두었으나 보정 금액(평균 배송비)은 아직 없다. D019 수집기에서 소스별 상수로 둘 예정.
- `search_index` 를 시세 레코드에 붙였는데, 검색 트렌드는 상품이 아니라 키워드·카테고리 단위다. D017 에서 별도 테이블(키워드 × 일자)로 분리할 가능성이 크다.
- 시세 스키마의 `price_id` 는 수집기가 발급해야 한다. 수집기가 같은 날 같은 소스를 두 번 돌면 중복 키 오류로 잡히도록 했으나, 하루 두 번 관측이 의도된 경우(가격 급변 모니터링)는 v0.1 에서 표현할 수 없다.
