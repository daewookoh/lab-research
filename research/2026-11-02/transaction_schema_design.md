# D004 — 거래 결과 스키마 v0.1 설계 메모

- 작성일: 2026-11-02 / 백로그 D004 / 과제1
- 산출물: `data/schema/transaction.json`(JSON Schema draft 2020-12), `src/schema/transaction.py`(로더·3단계 검증기·CLI), `data/schema/examples/transaction_examples.jsonl`(합성 예시 18건), `tests/test_transaction.py`(26건), `research/2026-11-02/transaction_validation.txt`(실행 로그)
- 상태: **draft** — 연구책임자 승인 전. 데이터는 전부 **합성(가상)** 이다. 실제 거래 데이터는 제공 계획이 없다(W44 답변 2).

## 1. 레코드의 단위

**1레코드 = 1상품(`product_id`) × 1판매채널 × 1등록 시도.** 같은 상품을 채널을 바꿔 다시 올리면 새 레코드다(예시 T00000004→T00000005: 중고앱에서 중단 후 자사몰 재등록, T00000013→T00000014: 오픈마켓 잔량을 도매로 매도). 이렇게 둔 이유:

- 판매기간 예측(과제1)의 목표변수는 "이 채널에 올렸을 때 얼마나 걸리나" 이고, 채널 추천(M13)의 라벨도 채널 단위여야 한다. 상품 단위로 합치면 채널 효과가 사라진다.
- 매입 조건(`purchase`)은 상품 배치 단위라 여러 레코드에 같은 값이 반복된다. 정규화하면 테이블이 하나 늘지만, 1만 건 합성 단계에서는 반복 저장이 단순하다. 실데이터 수령 시 재검토.

## 2. 백로그가 요구한 5개 축과 필드

| 축 | 필드 | 정의 |
|---|---|---|
| 매입가 | `purchase.buy_price_krw` (+`acquisition_type`, `buy_price_basis`, `buy_date`, `quantity`) | 1단위당, 원. 위탁(consignment)이면 0. 산정 근거(규칙 점수/수기/협상)를 남겨 D012 베이스라인에서 "규칙 점수로 산정한 매입가" 만 골라낼 수 있게 함 |
| 판매가 | `listing.listed_price_krw`(등록가) + `outcome.sold_price_krw`(실판매 단가, 수량가중) | 등록가와 실판매가를 분리. 실판매가 ≤ 등록가(교차 검증). 가격 인하 이력은 `notes` 로만 두고 v0.1 에서는 구조화하지 않음 |
| 판매채널 | `channel` 7종 | open_market / own_online / offline_store / secondhand_app / b2b_wholesale / live_commerce / social_market. 계획서의 "판매채널 추천" 라벨 |
| 판매기간 | `duration_days` + `censored` | **등록일 → 판매완료일**(`status=sold`). 아직 안 팔렸거나 중단된 레코드는 관측일(`observed_at`)까지의 경과일을 넣고 `censored=true`. 보유기간(product.acquired_date→기준일)과는 다른 개념 |
| 회수율 | `recovery_rate` | (실판매 단가 × 판매수량 − 채널수수료 − 배송비) ÷ (매입 단가 × 등록수량). 소수 4자리. 위탁이면 null |

`outcome.status` 5종으로 결과를 구분한다: `listed`(판매 중, 0개 판매) / `sold`(전량 완료) / `partially_sold`(일부 판매, 판매 중) / `withdrawn`(중단·이전) / `disposed`(폐기·기부 종료).

## 3. 핵심 결정 두 가지

**(1) 목표변수를 명시 저장하고 검증기가 재계산한다.** D003 에서는 "파생 필드는 스키마에 넣지 않는다" 고 했으나, `duration_days`·`recovery_rate` 는 피처가 아니라 **라벨**이다. 라벨은 데이터셋에 박혀 있어야 실험 간 정의가 흔들리지 않고, 실데이터가 CSV 로 올 때도 그대로 매핑된다. 대신 정의식과 다른 값이 들어오면 검증 실패로 잡는다(`expected_duration`, `expected_recovery`). 회수율은 반올림 자릿수의 반 단위(0.00005)까지 허용.

**(2) 중도절단(censoring)을 1급으로 둔다.** 장기재고는 "아직 안 팔린" 레코드가 많을 수밖에 없다. 이를 버리면 판매기간이 짧은 쪽으로 편향되고, 관측 경과일을 그대로 판매기간으로 쓰면 반대로 과소 추정된다. `censored` 플래그를 두면 (a) 회귀 베이스라인(D021·D022)은 `sold` 만 쓰고, (b) 나중에 생존분석(Cox·AFT)으로 전체를 쓰는 비교가 가능하다. `disposed` 는 "영영 안 팔림" 이 확정된 종료이므로 절단이 아니라 결과로 본다(판매기간 = 폐기까지 경과일, 회수율은 부분 판매분만).

## 4. 교차 검증 규칙 (`cross_checks`)

오류(검증 실패):
- `product_id` 가 상품 레코드에 없음 (상품 파일을 줬을 때만)
- 위탁인데 매입가 ≠ 0 / 매입인데 매입가 = 0
- 수량: `listing.quantity ≤ purchase.quantity`, `quantity_sold ≤ listing.quantity`
- 날짜: `product.acquired_date ≤ buy_date ≤ listed_date ≤ sold_date ≤ observed_at ≤ 기준일`
- status 일관성: sold ⇔ `sold_date` 있음 & 전량 판매 / listed 는 0개 / partially_sold 는 0<n<전량 / withdrawn·disposed 는 전량 미만 / `censored` 는 status 로 결정
- 판매수량>0 ⇔ `sold_price_krw` 있음, 실판매가 ≤ 등록가, 매입가 ≤ 상품 정상가
- 유통기한 경과 후 판매 기록 (상품의 `expiry_date` 참조)
- `duration_days`·`recovery_rate` 가 정의식과 불일치

경고(통과하되 보고):
- 등록가가 상품 정상가 초과 (T00000015 이불: 시즌 프리미엄일 수도 있어 오류로 두지 않음)
- 등록 수량이 상품 `quantity_on_hand` 초과 (보유 수량은 입력 시점 스냅샷)
- `regulation=permit` 품목(건강기능식품·의료소모품)을 중고앱·SNS 채널에 등록 — D002 의 규제 속성이 처음으로 채널 규칙에 쓰임. **가정**이며 법령·플랫폼 약관 확인은 D016 에서
- 수수료+배송비 > 매출 (적자 거래). 라이브 진행비처럼 고정비가 매출을 넘는 경우가 실제로 있을 수 있어 경고만

## 5. 실행 결과 (`transaction_validation.txt`)

```
스키마 점검 통과: transaction.json (최상위 필드 13개, 필수 9개)
18건 중 오류 0건, 경고 4건 (기준일 2026-11-02, 상품 참조 15건) — 경고 4건은 모두 의도적으로 심은 것
status: sold=9, partially_sold=4, listed=2, withdrawn=2, disposed=1 / channel 7종 모두 1건 이상 / censored 8/18
duration_days(sold): min 0, median 8, max 56 / (censored): min 8, median 29, max 152
recovery_rate(sold): 0.70 ~ 1.42, median 1.23 / 전체: −0.015 ~ 1.42
매입가/등록가 비율: 0.35 ~ 0.69, median 0.57
상품 파일 없이(--products none) 실행: 오류 0, 경고 1 (상품 참조 규칙만 빠짐)
pytest: 51 passed (D002 8 + D003 17 + D004 26)
```

테스트 26건 = 정상 6 + 스키마 오류 주입 4 + 교차 오류 주입 13 + 허용 오차 1 + 경고 2. 예시 수치(판매기간 중앙값 8일, 회수율 1.2 등)는 **스키마 동작 확인용 가상값**이며 어떤 분포도 대표하지 않는다. 분포 가정은 D008 에서 따로 문서화한다.

## 6. 한계·열린 질문

- 회수율 분모를 "등록수량 기준 매입원가" 로 두었다. 상품 배치 전체 매입원가(`purchase.quantity`)로 두면 채널을 나눠 판 경우 각 레코드의 회수율이 과소 집계된다. 배치 단위 회수율은 집계 단계에서 `product_id`+`buy_date` 로 묶어 계산한다.
- 위탁(consignment)의 회수율을 null 로 둔 것은 분모가 0 이기 때문이다. 정산율을 넣으면 "소상공인 수취액 / 정상가" 같은 대체 지표가 가능하지만 v0.1 범위 밖.
- `sold_price_krw` 가 수량가중 평균이라 등록 후 가격 인하 과정은 보이지 않는다. 가격조정 자동화(2028년 과제)에는 가격 변경 이력 테이블이 따로 필요하다.
- 채널 7종은 가정이다. 실제 운영 채널 목록(특히 B2B 땡처리 비중)이 있으면 교체한다. → 연구책임자 확인 사항.
- `seller_id` 는 D005 에서 정의할 외래키라 지금은 패턴(`S`+6자리)만 둔 채 존재 검증을 하지 않는다. D005 완료 후 교차 규칙에 추가.
- D003 과 같이, 실데이터로 교체될 때 스키마는 그대로 두고 교차 규칙(특히 허가 품목 채널 제한, 정상가 초과 경고)만 조정하면 되도록 분리했다.
