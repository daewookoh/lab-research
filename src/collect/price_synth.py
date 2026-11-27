"""참가격 형식 합성 CSV 생성기 — D019 개발·점검용. 실제 참가격 데이터가 아니다.

D018b(실 CSV 수령) 전까지 파서를 개발하기 위해 D016 에서 확인한 열 이름(GOOD_NAME·조사일·판매가격·SORE_NAME·제조사·세일여부·원플러스원)과
"업소 × 상품 × 조사일 1행" 구조를 흉내 낸다. 가격 수준은 우리 상품 정상가(product_examples)를 낱개로 환산한 값 근처로 잡은 가정이다.

    python -m src.collect.price_synth --out research/2026-11-27/synth_price_go_kr.csv --seed 20261127
"""
from __future__ import annotations

import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

# (상품명, 제조사, 낱개 기준가) — 앞 11개는 매칭표(price_go_kr_items.yaml)에 걸리도록, 뒤는 미매칭 대조군(실 파일의 수백 품목을 흉내).
GOODS: list[tuple[str, str, int]] = [
    ("감자칩 오리지널 60g", "스낵원", 1500),
    ("감자칩 사워크림 60g", "크런치푸드", 1600),
    ("캔커피 마일드 175ml", "데일리빈", 850),
    ("컵라면 매운맛 65g", "면사랑", 950),
    ("비타민C 1000mg 120정", "헬스랩", 24000),
    ("액체세탁세제 리필 3L", "클린홈", 11900),
    ("유리 밀폐용기 사각 1.0L", "글라스원", 5200),
    ("수분크림 50ml", "리프스킨", 22000),
    ("유아 물티슈 70매", "베베순", 1400),
    ("아기 물티슈 100매", "순수맘", 1900),
    ("강아지 간식 치킨 트릿 100g", "멍멍푸드", 3900),
    # 미매칭 대조군 (exclude·용량·토큰으로 걸러져야 함)
    ("감자칩 저염 110g", "스낵원", 2600),
    ("스틱커피 믹스 100입", "데일리빈", 12500),
    ("왕컵라면 110g", "면사랑", 1400),
    ("어린이 비타민C 츄어블 60정", "헬스랩", 9800),
    ("주방세제 1L", "클린홈", 4500),
    ("핸드크림 50ml", "리프스킨", 6500),
    ("물티슈 휴대용 20매", "베베순", 700),
    ("고양이 간식 참치 100g", "냥냥푸드", 3500),
    ("생수 2L", "맑은물", 980),
    ("흰우유 1L", "목장", 2900),
    ("두루마리 휴지 30롤", "소프트", 15900),
    ("즉석밥 210g", "밥사랑", 1300),
    ("샴푸 1L", "헤어랩", 9900),
]
STORES: list[tuple[str, float]] = (
    [(f"대형마트{chr(65 + i)} {b}점", 1.00) for i, b in enumerate(["서울", "부산", "대구", "인천", "광주", "대전", "울산", "수원", "창원", "고양"])]
    + [(f"SSM{chr(65 + i)} {b}점", 1.04) for i, b in enumerate(["성남", "용인", "청주", "천안", "전주", "포항", "김해", "제주", "안양", "세종"])]
    + [(f"전통시장 {b}상회", 0.93) for b in ["중앙", "남부", "북부", "동부", "서부", "신시장", "큰장", "역전", "모란", "부평"]]
)


def make_rows(seed: int, survey_dates: list[date], p_missing: float = 0.15, p_sale: float = 0.08, p_plus_one: float = 0.03,
              n_bad: int = 6) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    for d in survey_dates:
        for name, maker, base in GOODS:
            for store, k in STORES:
                if rng.random() < p_missing:
                    continue
                price = base * k * rng.lognormvariate(0, 0.06)
                sale = rng.random() < p_sale
                plus_one = (not sale) and rng.random() < p_plus_one
                if sale:
                    price *= rng.uniform(0.7, 0.9)
                price = int(round(price / 10.0) * 10)
                rows.append({"GOOD_NAME": name, "조사일": d.isoformat(), "판매가격": f"{price:,}" if rng.random() < 0.3 else str(price),
                             "SORE_NAME": store, "제조사": maker, "세일여부": "Y" if sale else "N", "원플러스원": "Y" if plus_one else "N"})
    # 오염 행: 빈 가격·날짜 형식 오류·0원 — 파서가 버려야 한다
    bad = [
        {"GOOD_NAME": "감자칩 오리지널 60g", "조사일": survey_dates[0].isoformat(), "판매가격": "", "SORE_NAME": "대형마트A 서울점", "제조사": "스낵원", "세일여부": "N", "원플러스원": "N"},
        {"GOOD_NAME": "캔커피 마일드 175ml", "조사일": "2026-13-40", "판매가격": "850", "SORE_NAME": "SSMA 성남점", "제조사": "데일리빈", "세일여부": "N", "원플러스원": "N"},
        {"GOOD_NAME": "컵라면 매운맛 65g", "조사일": survey_dates[0].isoformat(), "판매가격": "0", "SORE_NAME": "전통시장 중앙상회", "제조사": "면사랑", "세일여부": "N", "원플러스원": "N"},
        {"GOOD_NAME": "", "조사일": survey_dates[0].isoformat(), "판매가격": "1000", "SORE_NAME": "대형마트B 부산점", "제조사": "", "세일여부": "N", "원플러스원": "N"},
        {"GOOD_NAME": "수분크림 50ml", "조사일": survey_dates[0].strftime("%Y%m%d"), "판매가격": "22,000원", "SORE_NAME": "대형마트C 대구점", "제조사": "리프스킨", "세일여부": "N", "원플러스원": "N"},
        {"GOOD_NAME": "유아 물티슈 70매", "조사일": survey_dates[0].strftime("%Y.%m.%d"), "판매가격": "1400", "SORE_NAME": "대형마트D 인천점", "제조사": "베베순", "세일여부": "n", "원플러스원": "N"},
    ][:n_bad]
    rows.extend(bad)
    rng.shuffle(rows)
    return rows


def write_csv(rows: list[dict], out: Path, encoding: str = "cp949") -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding=encoding, newline="") as f:
        w = csv.DictWriter(f, fieldnames=["GOOD_NAME", "조사일", "판매가격", "SORE_NAME", "제조사", "세일여부", "원플러스원"])
        w.writeheader()
        w.writerows(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=20261127)
    ap.add_argument("--dates", type=str, default="2026-11-05,2026-11-19")
    ap.add_argument("--encoding", type=str, default="cp949", help="참가격 파일은 cp949 로 추정(미확인) — 파서는 둘 다 읽는다")
    a = ap.parse_args(argv)
    dates = [date.fromisoformat(s) for s in a.dates.split(",")]
    rows = make_rows(a.seed, dates)
    write_csv(rows, a.out, a.encoding)
    print(f"{len(rows)} rows → {a.out} ({a.encoding}); goods {len(GOODS)}, stores {len(STORES)}, dates {[d.isoformat() for d in dates]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
