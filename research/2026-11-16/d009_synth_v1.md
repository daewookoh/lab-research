# 합성 데이터 생성기 구현 v1 — 상품 1,000건 (D009)

- 작성: 2026-11-16, AI 연구보조(Claude) 초안 / 연구책임자 승인 전
- 설계: `research/2026-11-13/synth_design.md` (D008), 파라미터: `data/synth/params_v1.yaml`
- 코드: `src/data/synth.py` (생성기 + 요약 + 평탄화 + CLI), 테스트: `tests/test_synth.py` (7건)
- 산출 데이터: `data/synth/v1/{product,transaction,seller,market_price}.jsonl`, `data/synth/v1_meta.json`(정답), `data/synth/v1_flat.csv`(모델 입력 평탄화)
- 로그: `synth_v1_summary.txt`(생성 요약), `synth_v1_check.py` → `synth_v1_check.txt`(검증·비교·변형·재현성), `validators_v1.txt`(검증기 CLI 4종 + pytest)
- **전부 합성(가상) 데이터.** 실제 거래·판매자·시세 아님. 모든 분포는 D008 가정.

## 0. 결과 요약

| 항목 | 결과 |
|---|---|
| 규모 | 판매자 120 · 상품 1,000 · 시세 1,492 · 거래 1,199 (배치 554) — 설계 목표(120/1,000/≈1,500/≈1,200) 충족 |
| 검증기 4종 (`--today 2026-11-13`) | **오류 0건** / 경고 product 0 · seller 7 · market_price 229 · transaction 16 (전부 검증기 동작 확인용 의도된 사례) |
| 상태 비율 | sold 59.9 / withdrawn 22.3 / partially_sold 10.9 / listed 6.7 / disposed 0.3 % — 설계 점검과 모두 ±5%p 안 |
| 재현성 | 같은 seed 2회 동일, 저장 파일과 동일, seed+1 은 다름 |
| 테스트 | 189 passed (기존 182 + 신규 7) |
| 미완 | `data/synth/v1.parquet` 미생성 — 실행 환경에서 pyarrow 설치 불가(패키지 저장소 접근 차단). 같은 열을 `v1_flat.csv` 로 대체 저장. 코드는 pyarrow 가 있으면 parquet 을 쓰도록 되어 있음(§5) |

## 1. 구현 구조 (`src/data/synth.py`)

```
SynthGenerator(params, seed, n_products, n_sellers, reference_date, expiry_anchor)
  gen_sellers()            판매자 120: 권역·상태·업태·운영채널·주력 중분류 1~3개
  gen_products(sellers)    상품 1,000: 공급 판매자(active·blocked 만) → 주력 중분류 안 소분류 → 속성·정상가·수량·날짜
                           판매자별 상품을 배치(1~5품목, 같은 매입일)로 묶음. 상품 잠재값 r(시세/정상가) 저장
  gen_market_prices()      상품당 0~3건, (채널·플랫폼·상태·관측일) 키 유일
  gen_transactions()       상품당 등록 1~2건: 잠재 T = lognormal(소분류) × grade × price × channel
                           경쟁 사건 min(T, W, D) vs elapsed → status / duration / quantity_sold / 수수료·배송비 / 회수율
  fill_supply_history()    (판매자, 매입일) 묶음 → 배치 레코드(product_ids·신고가·정산액·기재정확도·이슈)
                           dormant·churned 는 과거 배치만, blocked 는 major_gap/dispute 배치 보장
generate() → (tables, meta) ; summarize() ; flatten() ; write_outputs()
```

- 설계 §7 대로 `v1_meta.json` 에 product→seller 맵, 상품 잠재값(r, 매입·등록일, 경과일), 거래 잠재값(T, base, 승수 3개, p, W, D, elapsed, status)을 저장했다. `v1_flat.csv` 는 거래 1행 = 상품·판매자 피처 + 목표변수 + `latent_*` 열(정답)로, D010 EDA·D012 베이스라인·D021/D022 회귀의 입력이다.
- 난수는 `numpy.default_rng(seed)` 하나. `--seed`, `--n-products`, `--n-sellers`, `--reference-date`, `--expiry-anchor` 로 v2(D011)도 같은 코드로 만든다(브랜드·시즌 승수는 `multipliers.enabled_v2` 로 켬 — v1 는 1.0 고정, 아직 코드에 시즌 판정 없음 → D011 에서 추가).

## 2. 설계와 달리 한 것 (전부 검증기·스키마 제약 때문)

| # | 설계(D008) | v1 구현 | 이유 |
|---|---|---|---|
| 1 | 잔여 유통기한을 **매입일 기준** U(lo,hi) 로 뽑음 | **기준일(reference_date) 기준** 으로 뽑음 (`d009.expiry_anchor: reference`) | D003 검증기가 기준일에 `expiry_date ≤ 기준일` 인 상품을 입력 거부. 매입일 기준으로 만들면 1,000건 중 66건이 기한 경과, 21건이 S·A 인데 임박 → 상품 검증기 오류 87건 (`synth_v1_check.txt` §4) |
| 2 | S·A 등급 잔여기한 하한 = 묶음 하한(short 45·medium 90) | S·A 는 하한 91일 | D003 "임박(≤90일) 이면 C" 규칙 |
| 3 | disposed = 중도절단(censored true) | disposed 는 `censored=false` | D004 검증기 정의(`CENSORED_STATUSES = listed·partially_sold·withdrawn`). 설계 문서 §4 표가 틀렸음 |
| 4 | 판매자 주력 **대분류** 1~3개 | 주력 **중분류** 1~3개 | seller.json 의 `main_category_codes` 는 중분류 코드·최대 5개 |
| 5 | 배치 분포 미정 | (판매자, 매입일) 묶음 1~5품목, 기재정확도·이슈 분포 추가 | seller 검증기의 배치 규칙(같은 날 배치·acquisition_type 일치·product_ids 존재) |
| 6 | 회수율 하한 없음 | 회수율 < −1 이면 배송비를 줄여 −1 로 맞춤(2건) | transaction.json `recovery_rate ≥ −1`. 저단가·소형 품목 1개 판매에 수수료+배송비가 붙는 경우 |

추가한 파라미터는 `params_v1.yaml` 하단 `d009:` 절에 "D009 추가·전부 가정" 으로 분리해 두었다. 1번은 생성기 옵션(`--expiry-anchor buy`)으로 원안도 만들 수 있다.

## 3. 생성 결과 vs 설계 점검 (`synth_v1_summary.txt`, `synth_v1_check.txt`)

| 지표 | 설계 점검(D008, 소분류당 2,000건) | v1 (1,000건) | 판정 |
|---|---|---|---|
| 잠재 T 중앙값 short/medium/long/none | 13.6 / 28.1 / 41.3 / 56.1 | 14.2 / 30.0 / 38.8 / 55.0 | 일치 (short 는 n=26 라 거침) |
| sold 만 duration 중앙값 / 잠재 T 중앙값 | 28.2 / 44.3 | 30.0 / 45.3 | 관측 창 선택 편향 재현 |
| sold / withdrawn / partially_sold / listed / disposed | 56.9 / 23.0 / 13.9 / 3.8 / 2.4 | 59.9 / 22.3 / 10.9 / 6.7 / 0.3 | 전부 ±5%p 안 |
| 판매 중(listed+partially_sold) 합 | 17.7 | 17.6 | 일치 |
| 등급 S/A/B/C | 43 / 30 / 17 / 10 | 39 / 32 / 20 / 9 | 일치 |
| 채널별 T 중앙값 b2b < live < … < own_online | 15.5 < 26.7 < … < 66.9 | 17.1 < 26.7 < … < 68.9 | 순서 일치 |
| 소분류별 sold 건수 최소 | (≈19 기대) | 10 (중앙값 22) | score_v0 `VELOCITY_MIN_SOLD=5` 전 소분류 충족 |

- **disposed −2.1%p** 는 §2-1 의 직접 결과다. 기준일 기준으로 잔여기한을 뽑으면 "기한 −14일 폐기" 가 관측 창 안에 거의 들어오지 않는다(기한 없는 품목의 240일 폐기만 남아 0.3%). `--expiry-anchor buy` 변형에서는 disposed 3.0% 로 설계 수준이 복원되지만 상품 검증기가 87건 실패한다.
- **listed +2.9%p / partially_sold −3.0%p** 는 등록수량 효과다. 설계 점검은 수량 20 고정이었고 v1 은 실제 수량(판매 중 211건의 중앙값 12, 수량 1 인 건 10건은 필연적으로 listed). 같은 잠재값에 수량 20 가정을 다시 적용하면 listed 4.3 / partially_sold 13.3% 로 설계(3.8 / 13.9)와 맞는다 → 경쟁 사건 로직은 설계와 동일.
- 생성기 1차 실행에서는 listed 가 8.9%(+5.1%p, 규칙 위반)였다. 원인은 배치 매입일을 U(2,284) 로 뽑고 등록 간격을 클리핑해 경과일 1~15일 구간 밀도가 높아진 것. 매입일 = 기준일 − (경과일 U(1,270) + 간격 U(1,14)) 로 바꿔 경과일 분포를 설계와 같게 한 뒤(평균 135.0, ≤15일 6.6%) 규칙 안에 들어왔다. "±5%p 초과면 버그 의심" 규칙이 실제로 버그를 잡았다.
- 회수율(sold) 중앙값 1.59, p10 0.91, 적자(<1) 13.1% — 설계 §5 의 "1.0~1.4 중심" 보다 높다. 매입가 = 등록가×U(0.35,0.60) 이라 매출/원가가 ≈2 에서 시작하기 때문이며 설계값 그대로의 결과다. D010 에서 매입가 비율을 올릴지 판단.
- 시세: 상품 899건에 1,492건, 관측일 stale(>60일) 31.5%, used 28.3%. 비율 경고 123건 = 신품 시세>정상가 116 + 환산 중앙값>1.5배 7 (score_v0 suspect_price 사례) — 설계 의도대로 경고 경로가 생겼다. 시세 없는 상품 101건(10%).
- 판매자: 상품 공급 94명(active 89 + blocked 5, blocked 판매자 상품 5~16건), 배치 554건(판매자당 중앙값 3) → score_v0 G2 게이트 사례 존재. 잔여기한 ≤30일 상품 11건 → G1 사례 존재.

## 4. 검증기 경고의 성격 (오류 아님)

- seller 7건: 등록 후 공급 없음 3명(active 인데 상품 미배정), major_gap 비율 ≥50% 4명(배치 1~2건인 판매자에서 우연히).
- market_price 229건: 표본<3 86건(n_listings lognormal 하단), 신품 시세>정상가 116건(r 분포 상단·묶음 환산 노이즈), 환산 중앙값>1.5배 7건, used@own_online 20건.
- transaction 16건: 수수료+배송비 > 매출(적자) — 소형 저단가 품목 소량 판매. 설계 §5 가 예상한 사례.

## 5. 남은 일·다음 단계

- parquet: `pip install pyarrow` 가 되는 환경에서 `python -m src.data.synth` 를 다시 실행하면 같은 seed 로 동일한 JSONL 과 `v1.parquet` 이 나온다(재현성 §5 확인). 그 전까지 `v1_flat.csv` 가 모델 입력.
- D010 EDA 는 `v1_flat.csv` 의 `latent_*` 열로 (a) log(duration) ~ 승수 복원 (b) 등급 효과를 가격 통제 후 보기 (c) 회수율 분포·적자 조건을 본다.
- D011 v2: `multipliers.enabled_v2` 로 brand·season 승수를 켜려면 등록월 → 시즌 판정(score_v0 `SEASON_MONTHS` 재사용)과 판매자 랜덤효과를 생성기에 추가해야 한다(현재 `mult["season"]=1.0` 고정).
- 설계 문서 §4 표의 disposed censored 표기와 §5 잔여기한 기준점은 D008 문서를 고치지 않고 이 문서 §2 에 차이로 남긴다(D008 커밋은 그대로 둠).
