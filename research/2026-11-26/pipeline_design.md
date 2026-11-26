# D018 — 시세·검색트렌드 수집 파이프라인 설계 (스케줄·저장·중복처리·약관 게이트·수기 입력 폼)

- 작성: 2026-11-26, AI 연구보조(Claude) 초안 — 연구책임자 승인 전 (draft)
- 산출물: `data/collect/pipeline.yaml`(job 7개 설정 v0.1), `data/schema/trend_index.json`(트렌드 지수 테이블 스키마 v0.1 draft), `data/collect/manual_price_form.csv`(수기 입력 폼), `src/collect/pipeline.py`(설정 검증·예산·append-only 저장소·수기 폼 변환·트렌드 변환), `tests/test_pipeline.py`(9건), `pipeline_check.py`/`pipeline_check.txt`(실행 결과)
- 데이터: **합성 v2** 시세 14,945건·상품 10,000건을 저장소 동작 점검에만 썼다. 실제 수집은 한 건도 하지 않았다(실행 가능한 자동 소스가 아직 없음). 수치는 전부 "설계에 대한 점검" 이다.
- 선행 작업 결과: **robots.txt 12개 직접 조회는 오늘도 실패**(무인 실행에서 미등장 URL 접근은 사람 승인이 필요하고 응답이 없어 거부됨 — D016 과 같은 제약). 참가격 CSV 다운로드도 같은 이유로 못 했다. 따라서 `price_sources.yaml` 의 robots 근거는 그대로 2차 자료이고, 이 두 가지는 **D018b** 로 분리해 사람(또는 네트워크 승인이 있는 환경)이 할 일로 남긴다. 설계는 그 결과에 따라 바뀌지 않도록 "소스 상태가 무엇이든 게이트가 막는다" 는 구조로 잡았다.

## 1. 목적과 범위

D016·D017 로 "어떤 소스가 있고 무엇이 허용되는가" 는 정리됐다. 결론은 둘 다 같았다 — 지금 자동으로 축적할 수 있는 clear 소스는 참가격(시세)뿐이고, 가장 중요한 소스(네이버 쇼핑 검색·데이터랩)는 약관 7.3③ 서면 확인 전엔 저장하면 안 된다. 그래서 오늘 설계의 중심은 "어떻게 많이 모으느냐" 가 아니라 **(1) 허용되지 않은 소스가 실수로도 실행·저장되지 않게 하는 것, (2) 허용이 떨어지는 순간 설정 한 줄로 켤 수 있게 해 두는 것, (3) 그 사이 담당자의 수기 조회를 같은 스키마로 받는 것** 이다.

범위: 시세(`market_price`)와 트렌드(`trend_index`) 두 테이블, 소스 7개 job, 스케줄·저장·중복·격리·매니페스트, 수기 폼. 범위 밖: 실제 소스별 수집기(D019), 품질 체크(D020), 상품 ↔ 외부 품목 매칭(M7 유사상품 검색 전까지는 `match_type=similar` 수기 매핑).

## 2. 전체 구조

```
[스케줄러] ── job(pipeline.yaml) ──▶ [게이트] tos_status / approved / prerequisite
                                        │ runnable 만 통과
                                        ▼
                      [수집기 D019]  api · file · manual(inbox CSV)
                                        │ 소스별 파서 → market_price / trend_index 레코드
                                        ▼
                      [검증]  src.schema.market_price 3단계 / validate_trend_records
                                        │ 오류 행은 적재하지 않고 run 로그에 남김
                                        ▼
                      [Store.ingest]  dedup 키 + 내용 해시 → new / dup / conflict
                                        │   new → store/<table>/<platform>/<YYYY-MM>.jsonl (append)
                                        │   dup → 버림(카운트만)
                                        │   conflict → store/quarantine/<table>.jsonl (사람 검토)
                                        ▼
                      [manifest.jsonl]  run_id · job · 시각 · n_in/new/dup/conflict
```

- 저장소는 **append-only** 다. 레코드를 고치거나 지우는 경로가 없다. 같은 키의 값이 달라지면 덮어쓰지 않고 격리해 사람이 본다. 연구노트가 Git 이력인 것과 같은 이유 — 시세 레코드는 "그날 그 소스가 그렇게 말했다" 는 관측이고, 수정 이력이 없으면 나중에 모델 결과를 재현할 수 없다.
- 상품 ↔ 외부 품목 매핑과 원문 응답(raw)은 레코드와 분리한다. raw 는 `raw_retain: true` 인 소스(공공데이터·수기)만 `store/raw/` 에 남기고, 네이버 계열은 약관 확인 전까지 **코드가 raw 를 저장하지 않는다**(D017 과 같은 원칙).

## 3. 스케줄과 호출 예산 (`pipeline.yaml`, `python -m src.collect.pipeline`)

| job | 소스 | 주기 | 호출/월 | 한도/월 | 비율 | 게이트 |
|---|---|---|---|---|---|---|
| J01 참가격 파일 | price_go_kr | 월 1회(15일) | 1 | – | – | **runnable** |
| J02 참가격 GW API | price_go_kr | 주 1회 | 43 | 미기재 | – | planned(활용신청) |
| J03 경북 밥상물가 | gyeongbuk_price | 월 1회 | 20 | 300,000 | 0.0% | planned(키) |
| J04 수기 폼 | manual_search | 매일 | 0 | – | – | **runnable** |
| J05 네이버 쇼핑 검색 | naver_apihub_shop | 주 1회, 상품 10,000×2 | 86,667 | 775,000 | 11.2% | planned(서면 확인+키) |
| J06 데이터랩 검색어 | naver_datalab_search | 월 1회, 8 호출 | 8 | 50,000(기억값) | 0.0% | planned(서면 확인+키) |
| J07 쿠팡 파트너스 | coupang_partners | 보류 | 0 | 21,600(시간 30 기준) | 0.0% | planned(가입+약관) |

- 월 호출 합계 86,739 중 **지금 실행 가능한 것은 1건**(참가격 파일)이다. 네이버가 허용되면 주 1회 전 상품 신·중고 조회가 한도의 11% 로 들어간다. 50 RPS 면 1회 실행 6.7분이지만 약관 7.3⑫(주기적 접속·과부하 금지)을 고려해 **1 RPS 로 늦추면 5.6시간** — 월요일 새벽 배치로 충분하다.
- 쿠팡 파트너스는 README 의 상충하는 한도 중 엄격한 쪽(시간 30회)으로 계산했다. 그 경우 10,000 상품에 14일이 걸려 주 1회가 불가능하므로, 허용되더라도 **샘플 500 상품(소분류당 ~17개)** 으로 축소하는 안을 적어 뒀다.
- 트렌드는 시세와 같은 파이프라인의 한 job 이다(D017 제안대로 "월 1회 8 호출 → 앵커 정규화 → search_index"). 전월 1일~말일을 `date` 단위로 받으면 소분류 30 × 30일 = 900행/월.
- 스케줄 시각은 전부 KST 새벽(03:00~05:00)으로 잡아 담당자 업무 시간과 소스 트래픽 피크를 피했다. 실제 스케줄러(cron·GitHub Actions·사내 서버)는 미정 — 아래 질문 2.

### 3.1 약관 게이트
job 마다 `approved`(약관상 저장 용도 허용 확인, 날짜·근거 필수)와 `prerequisite`(키·계정·활용신청 같은 남은 선행 조건)를 둔다. `gate_state` 규칙: prohibited 소스 → **never**(설정 자체가 검증 오류), prerequisite 가 남아 있으면 → **planned**, clear 이거나 approved=true → **runnable**, 그 외 → planned. 검증기는 (a) prohibited 소스를 job 으로 쓰는 것, (b) 소스 테이블 불일치(시세 소스를 trend_index 에), (c) clear 인데 approved=false 이고 prerequisite 도 없는 모순, (d) dedup 키가 스키마 검증기와 다른 것을 잡는다(`pipeline_check.txt` §1 에서 의도적 오설정 2건 모두 검출).

**수기 관측과 금지의 관계**: `naver_shopping_web`·`coupang_web`·`enuri` 는 크롤러에게 prohibited 이지만 담당자가 화면에서 본 값을 옮겨 적는 것은 약관의 자동화 조항과 무관하다. 그래서 platform 식별자는 금지 소스를 포함해 전부 enum 후보로 두고, **금지는 `collected_by ∈ {api, crawler}` 에만 적용**한다(§6 enum 제안).

## 4. 저장 형식과 중복 처리 (`Store`)

- 경로: `data/collect/store/<table>/<platform>/<YYYY-MM>.jsonl` (관측일 기준 월 파티션). 레코드는 스키마 그대로의 JSON 한 줄 + `run_id`. Parquet 는 이 환경에 pyarrow 가 없어 보류(D009·D011 과 같은 제약) — JSONL 은 append 가 쉽고 Git diff 가 보이므로 수집 단계엔 오히려 맞고, 학습용 변환은 D020 이후 읽을 때 한다.
- dedup 키는 스키마 교차 검증과 **같은 키**를 쓴다(`pipeline.yaml dedup_keys` 를 검증기가 대조): `market_price` = (product_id, channel, platform, condition, observed_at), `trend_index` = (category_code, period, time_unit, platform). 내용 해시는 `created_at`·ID·`run_id` 를 뺀 본문의 SHA-256 앞 16자리.
- 판정: 키 없음 → **new**(추가), 키 같고 해시 같음 → **dup**(안 씀), 키 같고 해시 다름 → **conflict**(격리). 같은 배치 안의 중복도 같은 규칙. 격리 레코드엔 run_id·이유·키·본문을 남긴다.
- 합성 v2 14,945건으로 점검(§2): 1차 적재 new 14,945 (0.63s) → 동일 배치 재적재 dup 14,945 (0.39s) → "내용 변경 50 + 새 관측일 100 + 동일 200" 배치는 정확히 conflict 50 / new 100 / dup 200. 파티션 32개(플랫폼 8 × 월 4), 본문 8.35MB, **레코드당 555B**.
- 규모 추정(네이버 허용 가정, 상품 10,000 × 신·중고 × 주 1회): 연 1,040,000행 ≈ 0.58GB 비압축, gzip 약 72MB. **Git 에 둘 크기가 아니다** — `store_root` 는 `.gitignore` 에 넣었고 보관 위치는 11-18 질문 3(저장소 용량 정책) 답변에 따른다. 현재 `existing()` 이 테이블 전체를 스캔하는 방식이라 15,045키에 0.26s → 연 100만 행이면 실행당 약 18s. 수용 가능하지만 100만 행을 넘기 전에 파티션별 키 색인(`<partition>.keys`)으로 바꿀 자리를 표시해 뒀다.
- 매니페스트(`manifest.jsonl`)는 영구 보관. 격리 90일·raw 365일 보관(가정).

## 5. 수기 입력 폼 (`manual_price_form.csv`, `form_to_records`)

현행 운영의 시세 조회 방식(11-24 질문 7)이 미답이라 **스키마 필수 필드 최소치**로 만들었다: `observed_at, product_id, site, query, condition, match_type, unit_count, n_listings, price_min/median/max_krw, shipping_included, notes`. 사람이 쓰기 쉽게 한 것 — 사이트는 한글 이름("네이버쇼핑"·"번개장터"·"당근")으로 적으면 platform·channel 로 변환하고 모르는 이름은 `other` + notes 에 원문 보존, 가격은 "9,800" 같은 쉼표 허용, 배송비는 "포함/미포함/true/false" 허용, `n_listings=0` 이면 가격 비움. 변환 결과는 `src.schema.market_price` 의 3단계 검증(스키마·교차·경고)을 그대로 통과해야 적재된다. 점검(§3): 합성 상품 20건으로 만든 가짜 폼 + 의도적 오류 3행 → 오류 4건(미래 관측일·min>median·같은 키 중복 2) 정확히 검출, 경고 2건(신품 시세 > 정상가). 인박스는 `data/collect/inbox/manual_price_*.csv` 를 매일 18:00 적재(J04).

폼에 **없는 것**: 상품 ↔ 외부 품목 매칭 근거(URL)와 조회 결과 상위 n 건의 개별 가격. 담당자가 min/median/max 를 직접 계산하게 하는 건 부담이므로, 현행 방식 답변에 따라 "상위 5개 가격을 그대로 적는 폼" 으로 바꿀 수 있다(그러면 통계는 코드가 계산).

## 6. `trend_index.json` 스키마와 enum 제안

- `trend_index` v0.1: 1레코드 = 소분류 × 구간 × 소스. 필드 `ratio`(원값), `anchor`·`anchor_ratio`, `normalization`(per_request/per_group/unknown), `rel_to_anchor`, `search_index`(0~100), `source.keywords`·`keywords_version`(재현용), `run_id`. **per_group 이면 rel_to_anchor·search_index 는 null** — 비교 불가능한 숫자를 저장하지 않는다(검증기가 강제). 문서 예시 응답으로 변환·검증·적재·재적재(dup 4) 까지 확인(§4). `market_price.search_index` 는 그대로 두고, 학습 시 (category_code, observed_at 의 월) 로 조인해 채운다.
- `market_price.source.platform` enum 제안(16개, **스키마는 고치지 않음** — 승인 후 반영): `bunjang, coupang_partners, coupang_web, daangn, danawa, domeggook, enuri, gmarket_auction, gyeongbuk_price, joongna, manual_search, naver_apihub_shop, naver_shopping_web, price_go_kr, st11_openapi, other`. D016 제안(11개)과 다른 점: prohibited 소스를 포함했다(수기 관측 사이트 식별용, §3.1). 합성 v2 는 `coupang`·`gmarket`·`naver_shopping`·`joonggonara`·`brand_mall` 을 쓰고 있어 enum 반영 시 생성기(D034)도 같이 고쳐야 한다.

## 7. 결론·권고

1. 지금 켤 수 있는 것은 **참가격 월간 파일(J01)과 수기 폼(J04)** 뿐이다. D019 수집기 v1 은 J01 파서(CSV → 품목 매칭 → 상품×조사일 집계)로 시작하고, 매칭 어휘는 FD·LV·BT·HB examples 10개부터.
2. 네이버(J05·J06)는 서면 확인과 키가 오면 `approved: true, prerequisite: null` 두 줄로 켠다. 그 전에는 코드가 실행도 저장도 하지 않는다. J06 은 켜기 전 D017b(8 호출)로 정규화 기준을 먼저 본다.
3. 저장소는 Git 밖 append-only JSONL. 보관 위치·백업은 11-18 질문 3 답변 필요.
4. D018b(사람 작업): robots.txt 12개 직접 조회 → `price_sources.yaml` 갱신, 참가격 CSV 1회 다운로드 → 품목 수·판매처 유형·조사 주기 집계(J01 파서 설계 입력). 이 환경에서는 반복해도 같은 이유로 실패한다.
5. 운영 중 실제 거래 데이터(Q1)에 시세 조회 기록이 있으면 그것을 `manual_search` 레코드로 역적재해 수기 폼의 첫 데이터로 쓴다.

## 8. 가정·미확인 목록

- 참가격 파일 갱신일(매월 15일 전후)·GW API 트래픽 한도: 포털 페이지 미기재. 경북 API 일 10,000 은 D016 1차 확인값.
- 데이터랩 월 50,000: D016 기억값, 재확인 실패. 쿠팡 한도 상충(분 50 vs 시간 30) 미확정.
- `to_search_index` 의 "앵커=50, 2배=100" 척도, 격리 90일·raw 365일 보관, 1 RPS 자율 제한 — 전부 가정.
- 파티션 색인 전환 시점(100만 행), 스케줄러 종류, 저장소 위치 — 미정.
