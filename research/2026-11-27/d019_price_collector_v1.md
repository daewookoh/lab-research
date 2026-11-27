# D019 — 시세 수집기 v1: 참가격 월간 CSV 파서 (J01)

- 일자: 2026-11-27 (금) / 작성: AI 연구보조(Claude) / 상태: draft — 연구책임자 검토 전
- 범위: 1개 소스(한국소비자원 참가격 공공데이터, `price_go_kr`) × 10개 품목(매칭 item) → `market_price` 레코드 → 검증 → append-only 적재
- **데이터: 전부 합성.** 실제 참가격 CSV 는 D018b(사람 작업) 전까지 받지 못했다. 열 이름은 D016 에서 포털 페이지로 확인한 7개(상품명 GOOD_NAME·조사일·판매가격·판매업소 SORE_NAME·제조사·세일여부·원플러스원)를 그대로 쓰고, 행 구조("업소 × 상품 × 조사일 1행")와 가격 수준은 가정이다. 합성 CSV 는 `src/collect/price_synth.py` 가 만들고 `research/2026-11-27/synth_price_go_kr.csv` 에 둔다(1,241행, cp949).
- 산출물: `src/collect/price.py`, `src/collect/price_synth.py`, `data/collect/price_go_kr_items.yaml`, `tests/test_price.py`(11건, 전체 258 passed), `research/2026-11-27/price_check.py` → `price_check.txt`, `fig1_price_vs_list.png`, `synth_price_go_kr.csv`; `src/collect/pipeline.py` 의 `Store.ingest` 에 `extra`(manifest 출처 정보) 추가.

## 1. 구조

```
CSV ──read_csv──▶ 행(내부 열 이름) ──normalize_rows──▶ 날짜·가격·세일 플래그 ──match_rows──▶ (행, item)
     인코딩 utf-8-sig→cp949→euc-kr    버림: 빈 상품명·날짜 오류·가격 0/빈값     price_go_kr_items.yaml, 첫 매칭 1개
     열 별칭(필수 4열 없으면 ParseError)

(행, item) ──aggregate──▶ 상품 × 조사일 1레코드 ──MP.validate_records──▶ 오류 0 일 때만 ──Store.ingest──▶ store/market_price/price_go_kr/YYYY-MM.jsonl
             세일·1+1 제외, 우리 상품 unit_count 로 환산     스키마+교차 3단계                       new/dup/conflict + manifest(source_file)
```

실행: `python -m src.collect.price --csv <파일> --today YYYY-MM-DD [--dry-run | --store data/collect/store] [--synthetic-input] [--dump out.jsonl]`. 검증 오류가 1건이라도 있으면 적재하지 않고 종료 코드 1.

## 2. 설계 결정

| 항목 | 결정 | 근거·대안 |
|---|---|---|
| 열 이름 | 내부 이름 9개(필수 4: 상품명·조사일·가격·업소)에 별칭 목록. 못 찾으면 멈춤 | 실 파일 표기 미확인. 추측 적재보다 중단이 안전. 별칭은 포털 파일 표기(GOOD_NAME·SORE_NAME)와 GW API 식 camelCase(goodName·goodDwDate·entpName) 둘 다 |
| 인코딩 | utf-8-sig → cp949 → euc-kr → utf-8 순서로 시도 | 공공데이터 CSV 는 cp949 가 흔하지만 미확인. 둘 다 점검 통과 |
| 매칭 | item 별 `include_all`/`include_any`/`exclude_any` 토큰 + 상품명에서 읽은 용량(g·ml·L·정·매…)이 기대값 ±25% 안. 한 행은 목록 순서로 첫 item 1개만 | 브랜드명은 쓰지 않는다(참가격 품목 브랜드 ≠ 우리 합성 브랜드). 용량으로 "저염 110g"·"왕컵 110g"·"휴대용 20매" 같은 이웃 품목을 걸러낸다 |
| item → 상품 | 10 item → 9 product(P00000014 물티슈는 70매·100매 item 2개가 한 상품으로 합산) | 참가격 품목 여러 개가 우리 상품 하나의 "유사 상품" 이 되는 것이 자연스럽고, 실 파일에서는 이쪽이 더 흔할 것 |
| 집계 단위 | 상품 × 조사일. n = 통계에 쓴 업소 관측 수, min/median/max | `market_price` 1레코드 = 1상품 × 1소스 × 1상태 × 1관측일(D005). 업소 유형(대형마트/SSM/시장) 구분은 파일에 없어 notes 에 업소 수만 |
| 가격 환산 | 참가격 낱개 가격 × (우리 상품 unit_count ÷ item.unit_count), `unit_count` 에 우리 상품 값 | `price_ratio` 가 바로 "정상가 대비" 가 되게. products 없이 돌리면 환산 없이 낱개 그대로 |
| 세일·1+1 | 기본 제외(건수만 notes 에) | 용도가 "정상 소매가 기준선". 포함 시 중앙값 −1.4%, 최소값 −11.1%(합성) — min 이 세일가에 끌려 내려간다. `exclude_sale: false` 로 바꿀 수 있음 |
| match_type | v1 전부 `similar` | exact 판정 근거(브랜드·규격 일치) 없음. M7 유사상품 검색이 채울 자리 |
| collected_by | 실 파일 `api`, 합성 입력 `synthetic` (`--synthetic-input`) | D018 설계와 동일. 합성 레코드는 notes 첫머리에도 "합성 CSV(실데이터 아님)" |
| 출처 파일명 | notes 가 아니라 적재 manifest(`source_file`·n_rows·n_matched) | 1차 구현은 notes 에 넣었다가 **파일명만 달라도 18건 전부 conflict** 가 되는 것을 점검에서 확인(§3.4). 내용 해시는 관측값만 봐야 한다 |

## 3. 점검 결과 (`price_check.txt`, 합성)

1. **파싱**: 1,241행 → 1,237행. 오염 행 6개 중 버려야 할 4개(빈 가격·0원·`2026-13-40`·빈 상품명) 버림, 살려야 할 2개(`20261105`·`2026.11.05` 날짜, `22,000원` 가격) 통과. cp949·utf-8-sig 둘 다 같은 결과.
2. **매칭**: 매칭 대상 11개 상품명 전부 매칭(554행), 대조군 13개(저염 감자칩·스틱커피·왕컵라면·어린이 비타민·주방세제·핸드크림·휴대용 물티슈·고양이 간식·생수·우유·휴지·즉석밥·샴푸) 전부 미매칭(683행). 표기 변형 12개(띄어쓰기 없음·대문자 단위·3000ml·"비타민씨"·괄호·×2·6입) 전부 의도한 item 으로. 단 `비타민C 1000mg` 은 용량 단위 mg 를 모르고(None) `require_content: false` 라 PG04 로 통과 — 실 파일에서 mg 표기가 있으면 단위 표에 넣어야 한다.
3. **집계·검증**: 18 레코드(9 상품 × 2 조사일), n 17~50, 스키마·교차 오류 0, 경고 8건 — 전부 "신품 시세가 정상가 초과(1.01~1.26배)". 캔커피·컵라면·밀폐용기·물티슈가 1 을 넘는데 이는 합성 가격 수준(낱개 소매가 합 > 묶음 정상가)의 결과이지만, **실 데이터에서도 같은 방향**일 것이다 — 우리 "정상가" 는 묶음 온라인가, 참가격은 낱개 오프라인가. `price_ratio > 1.0` 경고 규칙(D005)은 이 소스에 상시 발동하므로 소스별 기준이 필요하다(§5).
4. **적재(append-only, 임시 폴더)**: 1차 new 18 → 같은 파일 재적재 dup 18 → 파일명 다르고 업소 1곳 가격만 바꾼 파일 → conflict 1·dup 17(정확) → 다른 seed 의 다음 달 파일(조사일 3개) → 기존 2 조사일 conflict 18·새 조사일 new 9. 파티션 `market_price/price_go_kr/2026-11.jsonl` 27행·676B/건, quarantine 19, manifest 4건(source_file 포함).
5. **규모**: D016 스냅샷 크기 235,632행(15.6MB)으로 부풀려 읽기 0.6s + 정규화 1.9s + 매칭 2.2s + 집계 0.1s ≈ **4.8s**. 월 1회 배치라 최적화 불필요. 레코드 수는 상품 × 조사일로 결정되므로 10 item·월 2~4 조사일이면 **월 18~36건**.
6. 그림 `fig1_price_vs_list.png`: 상품별 환산 중앙값(점)·min~max(선) ÷ 정상가, 조사일 2개.

## 4. 한계

- 열 이름·행 구조·조사 주기·업소 수·세일 표기 값("Y"/"N" 인지)은 전부 가정. 실 파일 수령(D018b) 뒤 첫 실행은 `--dry-run` 으로 돌려 `unmatched_top` 과 `per_item` 을 보고 매칭표를 고친다.
- 품목 매칭은 토큰·용량 규칙이라 "오리지널 60g" 와 "사워크림 60g" 를 같은 item 으로 본다(의도). 반대로 참가격이 "포테이토칩" 으로 적으면 놓친다 — unmatched 목록을 보고 토큰을 추가하는 운영이 전제.
- 10 item 이 덮는 우리 상품은 예시 15건 중 9건. 합성 v2 10,000 상품에 쓰려면 소분류 단위 매칭(item → category_code → 그 소분류의 모든 상품)이 필요한데 이는 `market_price` 가 product_id 단위라 D020 또는 D034 에서 정한다.
- 업소 유형·지역은 파일에 없어 "전국 오프라인 평균" 으로만 쓴다. 참가격 GW API(J02)는 활용신청(11-24 질문 6) 후.

## 5. 다음 작업으로 넘기는 것

- D020(검증·품질 체크): (a) 소스별 `price_ratio` 경고 기준 — 참가격(낱개 오프라인)은 1.0 초과가 정상이라 상한을 1.5 가 아닌 소스별 값으로, (b) 조사일 간 중앙값 급변(예: ±30%) 경고, (c) item 별 매칭 행 수가 전월 대비 급감하면 표기 변경 의심 경고, (d) 월 1회 실행의 "파일이 전월 것인지" 판정(조사일 최대값으로).
- D018b 수령 후: 실 파일 1회 dry-run → 매칭표 토큰·단위(mg 등) 보강 → `price_sources.yaml` 의 품목 수·조사 주기 채움.
- 저장 위치(11-26 질문 1) 확정 전까지 `--store` 는 임시 폴더나 `data/collect/store/`(.gitignore) 에만.
