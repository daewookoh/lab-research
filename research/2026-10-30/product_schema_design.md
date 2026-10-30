# D003 — 상품 속성 스키마 v0.1 설계 메모

- 작성일: 2026-10-30 / 백로그 D003 / 과제1
- 산출물: `data/schema/product.json`(JSON Schema draft 2020-12), `src/schema/product.py`(로더·2단계 검증기·CLI), `data/schema/examples/product_examples.jsonl`(합성 예시 15건), `tests/test_product.py`(17건), `research/2026-10-30/product_validation.txt`(실행 로그)
- 상태: **draft** — 연구책임자 승인 전. 데이터는 전부 **합성(가상)** 이다.

## 1. 설계 원칙

1. **1레코드 = 소상공인이 보유한 1개 SKU 묶음.** 거래(D004)·시세(D005)와 분리해 상품 자체의 불변 속성만 담는다. 거래 결과·시세는 `product_id` 로 참조한다.
2. **백로그가 요구한 6개 축을 전부 1급 필드로 둔다.** 카테고리(`category_code`) · 브랜드(`brand.name`, `brand.tier`) · 정상가(`list_price_krw`, 근거 `list_price_basis`) · 상태등급(`condition_grade`) · 포장상태(`packaging_state`) · 규격(`spec.*`). 여기에 판매기간 예측과 과제2에 바로 필요한 보조 필드를 넣었다: `quantity_on_hand`, `acquired_date`(보유기간의 시작점), `manufacture_date`/`expiry_date`(상품잔존성), `attributes`(카테고리별 자유 속성), `source_text`(과제2 입력 원문), `images`.
3. **JSON Schema 로 표현 가능한 것은 스키마에, 안 되는 것은 코드에.** 타입·enum·범위·필수·날짜 형식은 `product.json` 이 맡고, 다른 파일(`categories.yaml`)을 봐야 하는 규칙과 필드 간 논리 규칙은 `src/schema/product.py` 의 `cross_checks` 가 맡는다. 실제 데이터로 교체될 때도 스키마는 그대로 두고 교차 규칙만 조정하면 된다(W44 답변 2 반영).
4. **`additionalProperties: false`.** 임의 필드가 섞여 들어오는 것을 막고, 카테고리별 가변 속성은 `attributes` 안으로만 넣게 했다(값은 문자열·숫자·불리언, 최대 30개).

## 2. 주요 필드 정의와 근거

| 필드 | 값 | 근거 / 연결 |
|---|---|---|
| `product_id` | `P` + 8자리 | 외래키. 합성 1만 건·실데이터 모두 수용 |
| `category_code` | `XX-XX-XXX` | D002 소분류 30개. 존재 여부는 교차 검증 |
| `brand.tier` | major / mid / minor / unbranded / unknown | 가격경쟁력·시장수요 피처. 인지도 등급은 **가정**이며 실데이터에서 검색량 기준으로 재정의 예정 |
| `list_price_krw` | 100 ~ 1억 원, 정수 | 1단위(`spec.unit_count` 묶음) 기준. `list_price_basis` 로 출처(msrp / 판매자 마지막 판매가 / 시세추정 / 불명) 구분 |
| `condition_grade` | S / A / B / C | S=미개봉 신품, A=개봉·전시 흔적, B=경미한 흠, C=명확한 외관 하자 **또는 유통기한 90일 이내**. 파손·기한 경과는 등급 외(입력 거부) |
| `packaging_state` | sealed / box_opened / box_damaged / no_box / repackaged | 상태등급과 분리. 과제2 사진 판정(M10)의 라벨 후보 |
| `spec.unit_count` | ≥1 | "24입" 같은 묶음 단위. 가격·판매속도는 묶음 기준 |
| `spec.net_content` + `net_unit` | 수치 + g/kg/ml/L/ea/m | 낱개 내용량. 해당 없으면 null |
| `spec.size_class` | small / medium / large | 카테고리 기본값을 품목별로 덮어쓸 수 있음. 물류효율성 피처 |
| `spec.weight_g`, `dimensions_cm` | null 허용 | 배송비 추정용. 소상공인 입력에서 자주 비어 있을 것으로 예상 |
| `acquired_date` | date | 보유기간 = 기준일 − acquired_date. D004 의 판매기간과 구분(보유기간은 입고→기준일, 판매기간은 등록→판매) |
| `expiry_date` | date / null | `expiry_class ≠ none` 이면 필수, `none` 이면 금지(교차 검증) |

## 3. 교차 검증 규칙 (`cross_checks`)

오류(검증 실패):
- 카테고리 코드가 `categories.yaml` 에 없음
- `expiry_class=none` 인데 `expiry_date` 있음 / `≠none` 인데 없음
- 날짜 순서: `manufacture_date ≤ acquired_date ≤ 기준일 < expiry_date`
- 기한 경과·당일 → 재유통 불가로 입력 거부
- 기한 90일 이내인데 등급 S/A → 등급 정의 위반(C 로 조정 요구)
- `sealed` 인데 등급 B/C (기한 임박이 아닐 때) → 모순
- `box_damaged / no_box / repackaged` 인데 등급 S → S 는 밀봉·원박스만 허용

경고(통과하되 보고):
- `spec.size_class` 가 카테고리 기본값과 두 단계 차이(small↔large). 처음에는 오류로 두었으나 예시 "강아지 간식 100g(반려동물용품 기본 large)" 처럼 정당한 경우가 바로 나와 경고로 내렸다.

## 4. 실행 결과 (`product_validation.txt`)

```
스키마 점검 통과: product.json (draft 2020-12, 최상위 필드 18개, 필수 10개)
레코드 검증: 15건 중 오류 0건, 경고 1건 (기준일 2026-10-30)
condition_grade: A=5, B=1, C=1, S=8 / packaging_state: sealed=9, box_opened=2, box_damaged=2, no_box=1, repackaged=1
brand.tier: major=3, mid=5, minor=5, unbranded=1, unknown=1 / 대분류 7개 모두 1건 이상
null 허용 필드 채움률: manufacture_date 8/15, expiry_date 8/15, dimensions_cm 11/15, net_content 9/15, variant 10/15, brand.name 13/15
pytest: 25 passed (D002 8건 + D003 17건)
```

테스트 17건 중 12건은 의도적 오류 주입(필수 누락, 추가 필드, enum·범위, 날짜 형식, 미존재 카테고리, 유통기한 필수/금지/경과/임박, 포장·등급 모순 2종, 미래 입고일)이고, 1건은 size_class 불일치가 경고로만 남는지 확인한다.

## 5. 한계·열린 질문

- 등급 S/A/B/C 의 경계와 "기한 임박 90일" 은 **가정**이다. 실제 매입 심사 기준(또는 현행 Re-Flow 운영 기준)이 있으면 그 값으로 바꾼다. → 연구책임자 확인 사항.
- `brand.tier` 는 사람이 판단해야 하는 필드다. 합성 데이터(D009)에서는 카테고리별 비율로 샘플링하고, 과제2 단계에서 브랜드명→tier 자동 매핑(검색량·공식몰 유무)을 시도한다.
- `list_price_krw` 가 묶음 기준이라 단가 비교에는 `list_price_krw / unit_count` 가 필요하다. 파생 필드는 스키마에 넣지 않고 피처 단계에서 계산한다.
- 유통기한 없는 품목(의류·가전)의 "노후화" 는 `acquired_date` 와 `attributes.season`(예: FW25) 으로만 표현된다. 시즌성 피처(D027)에서 다룬다.
- 예시 15건은 스키마 동작 확인용이며 분포를 대표하지 않는다. 분포 가정은 D008 에서 문서화한다.
