# D007 — Re-Flow Score v0 파이썬 구현 + 단위테스트

- 작성일: 2026-11-12 / 백로그 D007 / 과제1 / 로드맵 M3(규칙 기반 Re-Flow Score 재현)
- 산출물: `src/reflow/score_v0.py`(구현·CLI), `tests/test_reflow_v0.py`(80건), `research/2026-11-12/reflow_v0_run.txt`(기준일 11-12 실행 로그), `research/2026-11-12/scores_v0_examples.jsonl`(출력 레코드 15건), `research/2026-11-12/reflow_v0_time_sensitivity.txt`(기준일을 1·3·6개월 뒤로 옮긴 실행)
- 입력 데이터: `data/schema/examples/` 의 D003~D005 예시(상품 15·시세 29·거래 18·판매자 17) — **전부 합성(가상) 데이터**. 실제 거래 데이터는 아직 없다.
- 상태: 명세 `research/2026-11-11/reflow_score_v0_spec.md` 를 그대로 옮긴 것. 숫자는 여전히 **전부 가정**.

## 1. 모듈 구조 (`src/reflow/score_v0.py`)

| 구역 | 내용 |
|---|---|
| 상수 | 명세의 가중치·경계값·점수표 전부를 파일 상단에 §번호와 함께 모음(`WEIGHTS`, `BRAND_SCORE`, `VELOCITY_PRIOR_DAYS`, `CHANNEL_BASE`, `SHIP_COST_KRW`, `GRADE_BOUNDS`, `BUY_RATIO_*` 등 40여 개). D012·D024 이후 교체는 여기만 바꾼다. |
| 기본 함수 | `lin`, `lin_rev`, `clip`, `season_state`, `select_price`(§1 시세 선택 규칙) |
| 항목 함수 6개 | `score_demand`, `score_velocity`, `score_price`, `score_residual`, `score_channel`(+`channel_scores`), `score_logistics`(+`effective_size_class`). 각각 `ItemResult(score, flags, detail)` 를 돌려줘 점수·결측 플래그·중간값(d_ref, 시세 비율, 채널별 점수 등)을 분리 |
| 게이트·집계 | `gate_for`(G1 expired·G2 seller_blocked), G3 no_channel 은 채널 항목 뒤에, G4 no_price_basis 는 집계에서; `grade_of`, `buy_ratio_of`, `confidence_of` |
| 조립 | `score_product(product, leaf, prices, seller, duration_index, today)` → 명세 §5 출력 레코드(+`details`). `score_all(...)` 이 M1 테이블 4종을 받아 상품별로 돌림. `build_duration_index`(sold 거래의 duration_days 를 소분류별로 모음), `seller_for_product`(거래 레코드로 상품→판매자 연결) |
| CLI | `python -m src.reflow.score_v0 [--today] [--examples DIR] [--out scores.jsonl]` — 표 + 요약 출력, 옵션으로 JSONL 저장 |

프로토타입(`research/2026-11-11/reflow_v0_check.py`)과의 차이는 (1) 항목 함수가 전부 순수 함수이고 입력을 명시적으로 받는다(전역 상태 없음), (2) 판매속도의 이력 조회를 `duration_index` 로 분리해 거래 레코드에 카테고리를 덧붙이던 편법(`_cat`)을 없앴다, (3) 출력에 `evaluated_on`·`spec_version`·`details`(판매자·시세 레코드 ID·환산 중앙값·항목별 중간값)를 넣어 D010/D012 에서 항목별 분해가 가능하게 했다, (4) G4 에 `no_price_basis` 플래그를 추가해 "왜 manual_review 인가" 를 플래그만으로 알 수 있게 했다. 산식 자체는 바꾸지 않았다.

## 2. 테스트 (`tests/test_reflow_v0.py`, 80건)

| 구역 | 건수 | 고정한 것 |
|---|---|---|
| 기본 함수 | 15 | `lin` 양끝·중간, `clip`, 시즌 판정 9 경우(제철·2개월 전·3개월 전·연도 넘김) |
| §1 시세 선택 | 5 | match_type > 상태 일치 > 최신 > price_comparison 순서, 상태 불일치 환산 양방향(×0.55 / ÷0.55), unit_count 환산, stale 60일 경계(60 아님·61 stale), n_listings=0·타 상품 제외 |
| §3.1 수요 | 8 | search_index 결측 시 재배분(69.375), 있을 때 전체 가중, n_listings 보정 5 경계(51/50/3/2/1), brand unknown=40 |
| §3.2 속도 | 8 | sold 만 색인, 소분류→중분류→사전값 순서와 ≥5건 경계, sold_count_30d 보정 6 경계, 묶음 12 경계, 이력 있으면 prior_velocity 없음 |
| §3.3 가격 | 6 | r 0.3/0.4/0.7/1.0/1.19, suspect 1.2 경계, approx ×0.9, 결측 50 |
| §3.4 잔존 | 4 | 만점 100, expiry_class=none 고정 100, short 180일 상한 차이, 보유기간 90/405/720/1000 |
| §3.5 채널 | 4 | 점수표 S/A/B/C 전부, permit 제외 3채널, 수량(100·5 경계)·부피·판매자 회피 보정 및 클립, 동점 시 CHANNELS 순서 |
| §3.6 물류 | 7 | 가치밀도 v=1→0·v=20→100, 과중량 강등(2000g 경계), 권역 5 경우 + missing_region |
| 게이트·집계 | 20 | G1 30/31일 경계, G2 와 우선순위, 등급 7 경계, 매입가 비율 4점, confidence 는 플래그 수가 아니라 항목 수, 전체 레코드 형태·매입가 100원 반올림·JSON 직렬화, 게이트 시 매입가 없음, G3(허용 채널 0), G4(basis unknown + 시세 없음), confidence 임계, 판매자 연결 |
| 회귀 | 2 | **11-11 프로토타입 로그 표 15행과 항목·총점·등급·confidence·채널·매입가·플래그·게이트 전부 일치** (표 문자열 자체도 동일), CLI 실행·JSONL 저장 |

## 3. 실행 결과

- `pytest`: **182 passed** (기존 102 + 신규 80), 0.4초.
- 기준일 2026-11-11 로 실행하면 프로토타입 로그(`reflow_v0_check.txt`)와 표 15행이 **문자 단위로 동일** → 회귀 테스트로 고정.
- 기준일 2026-11-12(오늘) 실행(`reflow_v0_run.txt`): 하루 지나 보유기간·잔여기한이 1일씩 줄어 residual 이 3건에서 1점(반올림) 내려가고 총점이 최대 0.1 바뀜. 총점 min 56.6 / median 73.6 / max 87.0, 등급 A4·B10·C1·D0, 게이트 1건(P00000010 seller_blocked), confidence median 0.83, 플래그 prior_velocity 15·approx_price 1·suspect_price 1 — 어제와 같다.
- 기준일 이동(`reflow_v0_time_sensitivity.txt`, 같은 데이터 그대로 두고 날짜만 미룸):

| 기준일 | 게이트 | 등급 분포 | confidence median | 비고 |
|---|---|---|---|---|
| 2026-11-12 | 1 (seller_blocked) | A4 B10 C1 | 0.83 | |
| 2026-12-12 | 2 (+P00000003 expired) | A5 B8 C2 | 0.83 | 시즌 판정이 12월로 바뀌어 holiday 품목 수요 ↑ → A 1건 증가 |
| 2027-02-11 | 3 (+P00000002 expired) | A4 B9 C2 | 0.67 | 시세 관측 전부 60일 초과 → 15건 모두 stale_price, confidence 0.83→0.67 |
| 2027-05-11 | 4 (+P00000001 expired) | A2 B11 C2 | 0.67 | 보유기간 누적으로 총점 median 73.6→72.1 |

## 4. 관찰·한계

1. **게이트에 걸려도 총점은 높게 남는다.** P00000003 은 12-12 에 expired 게이트인데 총점 58.9(C) — 잔여기한 부분(0.4×0)만 0 이 되고 상태·포장·보유기간은 그대로라 residual 38. 게이트가 총점에 우선하므로 운영상 문제는 없지만, D012 에서 "점수 vs 판매기간" 상관을 볼 때 게이트 건은 제외하거나 별도 집계해야 한다.
2. **stale 시세는 점수에 그대로 쓰인다.** 명세대로 confidence 만 깎는다. 시세 수집이 멈추면(D019 전) 2개월 뒤 모든 상품이 confidence 0.67 이 되는데, 0.5 미만이 아니라 manual_review 는 안 뜬다. 수집 주기가 정해지면(D018) stale 기준 60일을 수집 주기의 2~3배로 다시 잡는다.
3. **판매속도는 여전히 15/15 사전값.** 합성 예시에 같은 소분류 sold 5건 이상이 없다. D009 합성 1,000건이 오면 `build_duration_index` 가 처음으로 (a)(b) 경로를 탄다 — 그때 `prior_velocity` 비율이 얼마나 떨어지는지가 사전값 의존도의 첫 수치가 된다.
4. **상품→판매자 연결이 거래 레코드 경유다.** 상품 스키마(D003)에 판매자 참조가 없어 `seller_for_product` 가 거래의 `seller_id` 를 쓴다. 매입 전(거래 레코드가 아직 없는) 상품은 판매자를 못 찾아 G2 를 검사할 수 없고 물류 권역이 결측된다. 매입 판단 시점에 쓰려면 상품 또는 입고 배치에 `seller_id` 가 있어야 한다 → 스키마 v0.2 변경 후보(노트 6번 질문).
5. 채널 동점(A 등급: open_market·offline_store·secondhand_app 모두 70)은 `CHANNELS` 순서로 깨진다. 임의 규칙이며 M13 에서 채널 추천이 모델로 바뀌면 사라진다.
