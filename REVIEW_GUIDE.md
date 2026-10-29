# 연구책임자 루틴 — 주 1회, 1분

이 저장소가 전자연구노트 원본이다. AI 연구보조가 매 평일 17:00~18:00(KST) 사이 임의 시각에 작업·노트를 커밋하고, 금요일에는 그 주 노트를 묶은 `review/YYYY-Www.md` 를 만든다. 연구책임자는 **주 1회 GitHub 웹에서 그 파일을 수정해 커밋**한다. GitHub 웹 커밋은 GitHub 키로 자동 서명되어 `Verified` 가 붙으므로, 그 커밋이 확인자 전자서명 + 시점인증이다. 메일은 없다.

## 주 1회 (금요일 이후 아무 때나)

1. https://github.com/daewookoh/lab-research/tree/main/review 에서 이번 주 파일을 연다. (알림을 원하면 저장소 Watch → Custom → Pushes)
2. 연필 아이콘(Edit) → `- [ ] 위 노트를 검토함` 을 `- [x]` 로 바꾼다.
3. 할 말이 있으면 `## 코멘트 / 방향 지시` 아래에 적는다. 질문 답변 칸도 채울 수 있다. (없으면 그대로)
4. **Commit changes** → **Commit directly to the main branch** → Commit. 끝.

코멘트·답변은 다음 실행에 자동 반영된다. 노트 자체는 매일 보지 않아도 되지만, 봐 두면 방향을 빨리 잡을 수 있다.

## 요건 대응

| 요건 | 구현 |
|---|---|
| 작성자·작성일시 | 모든 커밋에 작성자·시각·해시 |
| 수정 이력 보존, 위변조 방지 | Git 이력 + `main` 보호(force-push·삭제 금지) |
| 작성자 서명 | AI 연구보조 커밋 |
| 확인자 서명 | 연구책임자의 주간 Verified 커밋 |
| 시점인증 | 커밋 타임스탬프·GitHub 서버 기록 |
| 연속성 | `notes/INDEX.md` 의 일자별 해시 목록 |

## 한 번만 할 설정

Settings → Branches → Add branch protection rule → `main` → "Allow force pushes" 해제, "Allow deletions" 해제 → Create.

## 외부 제출

세액공제·특허: 해당 기간의 `notes/`, `review/`, `notes/INDEX.md` 를 PDF로 출력하고 GitHub 커밋 페이지(해시·Verified)를 캡처해 첨부. 정부 R&D 과제 참여 시엔 과제 지정 시스템에 별도 등록이 필요할 수 있다.
