# D006 — Re-Flow Score v0 계산식 명세 (6개 항목)

- 작성일: 2026-11-11 / 백로그 D006 / 과제1 / 로드맵 M3(규칙 기반 Re-Flow Score 재현)의 첫 단계
- 산출물: 이 문서, `research/2026-11-11/reflow_v0_check.py`(명세가 M1 스키마만으로 계산 가능한지 확인한 프로토타입 — `src/` 정식 구현·단위테스트는 D007), `research/2026-11-11/reflow_v0_check.txt`(합성 예시 15건에 적용한 실행 로그)
- 상태: **draft, 전부 가정.** W44 답변 3("현행 산식 자료 없음 → 계획서의 6개 항목 정의를 근거로 AI 가 가중치·산식을 가정해 v0 로 문서화하고 가정임을 명시")에 따라 작성했다. 이 문서의 숫자(가중치·경계값·점수표)는 운영 자료에서 가져온 것이 **하나도 없다.** v0 의 목적은 (1) 항목마다 "어떤 입력을 어떤 식으로 점수화하는가" 를 M1 스키마 필드 수준까지 고정해 D007 구현과 D012 베이스라인이 같은 정의를 쓰게 하는 것, (2) 실데이터·EDA(D010) 이후 어느 숫자를 어떤 근거로 바꿀지 자리를 만들어 두는 것이다.

## 0. 요약

| # | 항목 | 묻는 것 | 주 입력(M1 테이블.필드) | 가중치(가정) |
|---|---|---|---|---|
| 1 | 시장수요 | 이 상품을 찾는 사람이 있는가 | product.brand.tier, categories.seasonality × 평가월, market_price.search_index, market_price.n_listings | 0.20 |
| 2 | 판매속도 | 같은 종류가 보통 얼마 만에 팔리는가 | transaction(같은 카테고리, sold) duration_days, market_price.sold_count_30d, categories.expiry_class(사전값) | 0.20 |
| 3 | 가격경쟁력 | 시세 대비 우리 등록가에 여유가 있는가 | market_price(환산 중앙값, 상태 매칭) ÷ product.list_price_krw | 0.20 |
| 4 | 상품잔존성 | 상품이 팔릴 때까지 가치를 유지하는가 | product.expiry_date, condition_grade, packaging_state, acquired_date(보유기간), categories.expiry_class | 0.20 |
| 5 | 채널적합성 | 팔 수 있는 채널이 있고 그 채널에 잘 맞는가 | categories.regulation, product.condition_grade, quantity_on_hand, spec.size_class, seller.business.sales_channels_used | 0.10 |
| 6 | 물류효율성 | 가져오고 보내는 비용이 가격 대비 감당되는가 | product.spec.size_class/weight_g/unit_count, list_price_krw(가치밀도), seller.business.region | 0.10 |

- 항목 점수는 각각 **0~100**, 총점 = Σ(가중치 × 항목 점수) 로 0~100.
- 총점 전에 **게이트(하드 제약)** 를 먼저 본다(§2). 게이트에 걸리면 총점과 무관하게 `not_purchasable`.
- 결측은 항목별로 "중립값 50 + 결측 플래그" 가 기본이고, 결측된 항목 수를 `confidence`(= 입력이 있는 항목 수 / 6) 로 같이 출력한다. 결측을 0 으로 두면 데이터가 없는 상품이 전부 하위로 밀려 자동 산정률(목표지표 2)이 떨어지므로, "모르는 것" 과 "나쁜 것" 을 분리한다.
- 가중치를 4·4·4·4·1·1 로 둔 이유: 앞 4개는 "팔리느냐·얼마에 팔리느냐" 를 직접 재고, 채널·물류는 그 결과를 실행하는 조건이라 보조로 두었다. 근거는 없다. D012 에서 항목별로 판매기간과의 상관을 보고 재배분한다.

## 1. 공통 정의

- **평가일** `t0`: 점수를 매기는 날(기본 오늘). 보유기간·잔여기한·시즌 판정의 기준.
- **선형 구간 점수** `lin(x, lo, hi)`: x ≤ lo → 0, x ≥ hi → 100, 사이는 선형. `lin_rev(x, lo, hi)` = 100 − lin(x, lo, hi) (작을수록 좋음).
- **시세 선택 규칙**(항목 1·2·3 공통): 상품의 `market_price` 레코드 중 (a) `match_type` 우선순위 exact > same_model_other_pack > similar, (b) 상태 매칭 — `condition_grade` S/A 는 `condition=new`, B/C 는 `used`; 매칭 상태 레코드가 없으면 반대 상태를 쓰되 new→used 환산계수 **0.55**(가정; D005 예시의 used/new 환산 중앙값 비가 0.42/0.81 ≈ 0.52 였으나 가상값) 를 곱하고 결측 플래그 대신 `approx` 플래그, (c) `n_listings ≥ 1`, (d) 같은 우선순위면 `observed_at` 최신, 그래도 여럿이면 `price_comparison` 채널 > 그 외. 선택된 레코드의 **환산 중앙값** = `price_median_krw × product.spec.unit_count ÷ record.unit_count` (D005 `normalized_median`). 관측일이 `t0` 로부터 **60일 초과**면 stale 플래그(점수에는 쓰되 confidence 에서 뺀다).
- **시즌 판정**: 평가월 → {summer: 6~8, winter: 11~2, holiday: 1·2·9·10·12(설·추석·연말), school: 2·3·8, spring_fall: 3~5·9~11}. 카테고리 `seasonality` 가 빈 배열이면 비시즌 상품(시즌 중립).

## 2. 게이트 (총점 이전, 하드 제약)

| 코드 | 조건 | 결과 |
|---|---|---|
| G1 expired | `expiry_date` 가 있고 잔여기한 ≤ **30일** (t0 기준) | not_purchasable. (D003 의 "90일 이내 → 등급 C 강제" 와 별개. 90일은 등급 강등, 30일은 매입 불가 — 둘 다 가정) |
| G2 seller_blocked | 공급 판매자 `status = blocked` | not_purchasable |
| G3 no_channel | 항목 5 에서 허용 채널이 0개 | not_purchasable |
| G4 no_price_basis | `list_price_krw` 가 없거나 `list_price_basis = unknown` **이고** 시세도 없음 | 자동 산정 보류 → `manual_review` (점수는 내되 매입가 자동 제안 안 함) |

G4 가 "매입가 산정 자동화율 80%" 의 분모·분자를 가른다: G4 에 걸린 건은 자동 산정 실패로 센다.

## 3. 항목별 산식

### 3.1 시장수요 (demand)

세 부분의 가중 합. `search_index` 가 없으면(D017 전까지 항상 없음) 그 가중치를 나머지 둘에 비례 배분한다.

| 부분 | 입력 | 점수 | 가중치 |
|---|---|---|---|
| 브랜드 인지도 | `brand.tier` | major 100 / mid 75 / minor 50 / unbranded 30 / unknown 40 | 0.5 |
| 시즌 적합 | `categories.seasonality` × 평가월 | 비시즌 상품 60 / 시즌 상품인데 제철 100 / 시즌 상품인데 제철 아님: 다음 제철까지 ≤ 2개월이면 70, 그 외 30 | 0.3 |
| 검색 트렌드 | `market_price.search_index` (0~100) | 그대로 | 0.2 (결측이면 0 → 재배분) |

보조: 선택된 시세 레코드의 `n_listings` 가 **50 초과**면 −10 (공급 과잉 신호, 가정), 0~2 면 −5(시장 자체가 얇음)·단 `n_listings=0` 은 결측 취급. 결과는 0~100 으로 클립.
결측 규칙: `brand.tier=unknown` 은 결측이 아니라 40점(알 수 없음 자체가 신호). 시즌은 카테고리만 있으면 항상 계산 가능 → 이 항목은 사실상 결측이 없다.

### 3.2 판매속도 (velocity)

"같은 카테고리 상품이 보통 며칠 만에 팔렸나" 를 기준 판매기간 `D_ref` 로 잡고 점수화한다.

1. `D_ref` 추정 순서: (a) 같은 **소분류** 의 `transaction` 중 `status=sold` 레코드 **≥ 5건** 이면 그 `duration_days` 중앙값, (b) 아니면 같은 **중분류** 로 ≥ 5건, (c) 아니면 카테고리 `expiry_class` 사전값(가정): short **14일** / medium **30일** / long **45일** / none **60일**. (a)(b) 는 sold 만 쓰므로 짧은 쪽으로 편향된다(D004 고찰). D008 에서 중도절단을 반영한 분포 가정이 나오면 사전값과 함께 교체한다.
2. 기본 점수 = `lin_rev(D_ref, 7, 120)` — 7일 이하 100, 120일 이상 0.
3. 외부 신호 보정: 선택된 시세 레코드의 `sold_count_30d` 가 있으면 `≥ 100` → +15, `10~99` → +5, `0` → −15 (가정). 없으면 보정 없음.
4. 단위 보정: `spec.unit_count ≥ 12`(박스 단위) 면 −10. 묶음은 낱개보다 수요층이 좁다는 가정.
5. 클립 0~100. `confidence` 는 (a)(b) 로 추정했을 때만 "입력 있음" 으로 세고, (c) 사전값이면 결측으로 센다(점수는 낸다).

### 3.3 가격경쟁력 (price)

`r = 환산 시세 중앙값 ÷ list_price_krw` (§1 시세 선택 규칙). r 이 1 에 가까울수록 시장이 아직 정가 근처라 우리의 할인 등록가(D004 예시 등록가/정상가 0.42~0.75, 정상가 초과 1건 제외)가 경쟁력을 갖고, r 이 낮을수록 시장이 이미 떨이라 여유가 없다.

- 점수 = `lin(r, 0.40, 1.00)`; r > 1.2 는 시세 레코드 경고(D005) 대상이므로 100 으로 두되 `suspect` 플래그.
- `approx`(상태 불일치 환산) 이면 점수에 0.9 를 곱한다(불확실성 할인, 가정).
- 시세 없음(`n_listings=0` 만 있거나 레코드 없음) → **50 + 결측 플래그**. 단 G4 조건이면 manual_review.
- v1 후보(지금 안 함): 채널 간 격차(max/min, D005 예시 median 1.16)가 크면 채널 선택으로 가격을 더 받을 수 있으니 가산; `shipping_included=false` 평균 배송비 보정(D019).

### 3.4 상품잔존성 (residual)

| 부분 | 입력 | 점수 | 가중치 |
|---|---|---|---|
| 잔여기한 | `expiry_date` − t0 | `expiry_class=none` → 100 고정. 그 외 `lin(잔여일, 30, 365)`: 30일 이하 0(G1), 365일 이상 100. 단 `expiry_class=short` 는 상한을 180일로(`lin(잔여일, 30, 180)`) | 0.40 |
| 상태등급 | `condition_grade` | S 100 / A 85 / B 60 / C 30 | 0.30 |
| 포장상태 | `packaging_state` | sealed 100 / box_opened 85 / repackaged 70 / box_damaged 60 / no_box 50 | 0.15 |
| 보유기간 | t0 − `acquired_date` | `lin_rev(보유일, 90, 720)`: 90일 이하 100, 2년 이상 0. "장기재고" 자체의 진부화(모델 변경·유행) 대리변수 | 0.15 |

`expiry_class=none` 인 카테고리(의류·공구·가전 등)는 잔여기한 부분이 항상 100 이라 변별력이 상태·포장·보유기간에서만 나온다. 의도한 것이다(기한 없는 상품의 잔존성은 상태와 진부화가 전부). `expiry_date` 가 있어야 하는 카테고리에서 null 이면 D003 검증기가 오류를 내므로 여기서는 결측 처리하지 않는다.

### 3.5 채널적합성 (channel)

채널 7종(D004 enum) 각각에 대해 적합도 0~100 을 매긴 뒤, **허용 채널 중 최댓값** 을 항목 점수로 하고 최댓값 채널을 `recommended_channel` 로 같이 출력한다(M13 채널 추천의 규칙 베이스라인·Top-1 적중률의 비교 대상).

1. **허용 여부(하드)**: `regulation=permit` 이면 secondhand_app·social_market·live_commerce 를 제외(D004 경고를 v0 에서는 제외로 격상; 법령·약관 근거는 D016 확인 사항). 허용 채널이 0개면 G3.
2. **기본 적합도(가정)** — 상태등급 × 채널:

| 채널 | S | A | B | C |
|---|---|---|---|---|
| open_market | 90 | 70 | 40 | 15 |
| own_online | 80 | 65 | 40 | 15 |
| offline_store | 70 | 70 | 65 | 50 |
| secondhand_app | 50 | 70 | 85 | 70 |
| b2b_wholesale | 85 | 60 | 40 | 25 |
| live_commerce | 75 | 60 | 35 | 10 |
| social_market | 70 | 65 | 45 | 20 |

3. **수량 보정**: `quantity_on_hand ≥ 100` → b2b_wholesale +15, secondhand_app −20 (개인 간 채널은 대량 처분 불가). `quantity_on_hand ≤ 5` → b2b_wholesale −30.
4. **부피 보정**: `size_class=large` → secondhand_app −15, live_commerce −10, offline_store +10, b2b_wholesale +5.
5. **판매자 채널 회피**: 공급 판매자의 `sales_channels_used` 에 든 채널 −20 (같은 채널에서 판매자와 경쟁·판매자 가격 교란 회피. 가정이며 운영상 실제 그런 정책이 있는지 확인 필요).
6. 각 채널 0~100 클립 → 허용 채널 최댓값.

### 3.6 물류효율성 (logistics)

| 부분 | 입력 | 점수 | 가중치 |
|---|---|---|---|
| 가치밀도 | `list_price_krw ÷ 예상 편도 배송비` | 예상 배송비(가정): small 3,000 / medium 4,500 / large 7,000원. 비 v = 가격/배송비. 점수 = `lin(log10(v), 0, 1.3)` — v ≤ 1 (가격이 배송비 이하) 0, v ≥ 20 100 | 0.45 |
| 부피 등급 | `spec.size_class` | small 100 / medium 70 / large 40 | 0.25 |
| 수거 거리 | `seller.business.region` | seoul·gyeonggi_incheon 100 / chungcheong 80 / gyeongsang·jeolla·gangwon 55 / jeju 20 / unknown·판매자 없음 70 + 결측 플래그 | 0.30 |

전제: 재유통 사업자의 물류 거점이 **수도권** 이라는 가정(확인 사항). `weight_g` 가 있고 size_class 기준 가정 중량(small 1kg / medium 5kg / large 15kg)을 2배 넘기면 부피 등급을 한 단계 내린다. 보유 수량·합포장은 v0 에서 반영하지 않는다(대량이면 단위당 수거비는 내려가지만 보관비가 오르는 양면성 — D012 이후).

## 4. 총점·등급·매입가 제안 (v0, 전부 가정)

- 총점 `S = 0.2·demand + 0.2·velocity + 0.2·price + 0.2·residual + 0.1·channel + 0.1·logistics`.
- 등급(가정, D024 라벨링 기준에서 확정): A ≥ 75 / B 60~74 / C 45~59 / D < 45.
- 매입가 제안(가정, M12 매입가 역산 로직이 대체할 자리): `buy_ratio = clip(0.10 + 0.40 × S/100, 0.10, 0.50)`, `suggested_buy_price = round(list_price_krw × buy_ratio, −2)`. S=50 → 정상가의 30%, S=80 → 42%. 처음에는 `0.20 + 0.50 × S/100` 으로 두었으나 프로토타입에서 예시 매입가(D004, 가상) 대비 절대 상대오차 중앙값 0.56·±20% 적중 0/16 이 나와, D004 예시의 매입가/정상가 비 범위(0.25~0.44)에 겹치도록 낮췄다(재실행: 오차 중앙값·적중률은 `reflow_v0_check.txt`). 예시가 가상값이므로 "맞췄다" 가 아니라 "스케일을 맞춰 두었다" 는 뜻이다. G4 면 제안하지 않는다.
- `confidence = 입력이 있는 항목 수 ÷ 6` (사전값·중립값으로 메운 항목은 제외). confidence < 0.5 면 `manual_review` 권고.

## 5. 출력 레코드(D007 구현 인터페이스)

```
{ product_id, evaluated_on, gate: null | "expired"|"seller_blocked"|"no_channel", manual_review: bool,
  items: {demand, velocity, price, residual, channel, logistics},   # 각 0~100 (정수 반올림)
  flags: [...],                       # missing_price, approx_price, stale_price, suspect_price, prior_velocity, missing_region, ...
  confidence, total, grade, recommended_channel, suggested_buy_ratio, suggested_buy_price_krw }
```

입력은 M1 테이블 4종 + 카테고리뿐이다. 즉 **v0 는 M1 스키마만으로 계산 가능**해야 하며, 프로토타입(`reflow_v0_check.py`)으로 합성 예시 15건에서 이를 확인했다(로그 `reflow_v0_check.txt`).

## 6. 알려진 한계·v1 에서 바꿀 것

1. 숫자가 전부 가정이다. D010(EDA)·D012(베이스라인 상관) 결과로 경계값·가중치를 갱신하고, 갱신할 때마다 이 문서의 버전과 근거를 남긴다.
2. 항목 간 중복: 브랜드(수요)와 시세 비율(가격), 기한(잔존)과 판매속도 사전값(기한 등급 기반)이 서로 상관될 것이다. 규칙 점수에서는 감수하고, ML(D022)에서 피처 중요도로 확인한다.
3. 판매속도의 이력 추정은 sold 만 쓰므로 낙관 편향. 생존분석 기반 추정은 Q2 후보.
4. 채널적합성 점수표는 "최댓값" 을 쓰므로 채널 하나만 좋아도 높다. 복수 채널 병행 등록(D004 예시 3건)은 반영 못 한다.
5. `search_index`·`sold_count_30d` 는 D017·D019 전까지 거의 항상 결측이라 수요·속도 항목이 사실상 브랜드·시즌·카테고리 사전값으로만 결정된다 — v0 의 변별력 한계.
