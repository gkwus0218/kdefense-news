# K-Defense Monitor — GitHub Actions 무료 자동 실행 버전

**PC를 꺼도** GitHub Actions에서 15분 간격(예약 기준)으로 해외 매체 공개 뉴스 메타데이터를 조회하고, 관련 기사 제목/출처/링크를 **휴대폰 텔레그램**으로 보냅니다. 기존 SQLite 중복 기록은 GitHub 저장소의 `data/articles.db`에 자동 커밋하여 다음 실행에도 이어집니다.

> 기사 전체 번역 및 AI 요약은 제공하지 않습니다. 기사 검색 결과의 시차, API 장애, GitHub 예약 실행 지연/누락으로 즉시성이 보장되지 않습니다.

## 준비물
- GitHub 계정 (https://github.com)
- 텔레그램 앱 + @BotFather에서 만든 봇의 토큰
- 텔레그램 봇과 대화에서 `/start`를 누른 후 확인한 Chat ID

## 가장 쉬운 설치 순서

1. GitHub에서 **New repository** → 예: `kdefense-news` 생성. 무료로 꾸준히 쓰려면 **Public** 선택 가능. 코드와 `data/articles.db`는 누구나 볼 수 있으므로 기록 공개가 싫다면 **Private**를 고르고 무료 사용량을 확인하세요. 봇 토큰은 절대 코드에 넣지 않습니다.
2. 이 압축파일을 풀고 **숨김 폴더 `.github/workflows/monitor.yml`을 포함**한 모든 파일을 저장소 루트에 업로드합니다. GitHub 웹 UI는 파일별 업로드가 가능하지만 숨김 폴더 경로를 유지하기 어려울 수 있어 `git` 또는 GitHub Desktop 사용을 추천합니다.
3. 저장소 **Settings → Secrets and variables → Actions → New repository secret**에서 아래 두 값을 등록합니다.
   - `TELEGRAM_BOT_TOKEN`: BotFather 토큰
   - `TELEGRAM_CHAT_ID`: 내 Chat ID
4. **Settings → Actions → General → Workflow permissions**에서 `Read and write permissions`를 설정합니다. 조직 정책상 막혀 있으면 관리자 설정이 필요합니다. 워크플로에 `permissions: contents: write`도 들어 있습니다.
5. 저장소 **Actions → K-Defense News Monitor → Run workflow**로 처음 수동 실행합니다. 첫 실행은 기존 기사에 대한 알림 폭주를 막고 **읽은 기사만 등록합니다**. Actions 실행 로그에서 `First run: seeded`를 확인하세요.
6. 다음부터 약 15분 간격의 예약 실행으로 새로운 기사가 발견되면 텔레그램 알림이 옵니다. `data/articles.db`가 추가되고 각 실행 후 업데이트되는지 확인하세요.

## 텔레그램 Chat ID 알아내기
1. 텔레그램에서 @BotFather로 봇을 생성하고 토큰을 받습니다.
2. 생성된 봇의 채팅방에서 `/start`를 보냅니다.
3. 웹브라우저 주소창에 아래 주소를 입력해 `chat.id`를 찾습니다. 토큰이 URL/브라우저 기록에 남으므로 공유하거나 화면 캡처를 게시하지 마세요.
   `https://api.telegram.org/bot<내_토큰>/getUpdates`
4. 위 ID를 `TELEGRAM_CHAT_ID`라는 GitHub Secret에 넣습니다.

## 비용
- **Public** 저장소의 GitHub 제공 표준 실행 환경은 무료. 코드 및 기사 기록이 공개됩니다.
- **Private** 저장소는 GitHub Free 요금제에서 월 **2,000분** 기본 제공(2026-10 기준). 15분마다 실행 = 하루 96회, 월 약 2,880회이므로 실행당 1분으로 계산되는 경우 한도를 넘을 수 있습니다. 자동 과금을 원치 않으면 사용량/예산을 확인하거나 스케줄을 30분으로 바꾸세요.
- 더 적은 검색 횟수: `.github/workflows/monitor.yml`의 cron을 `7,37 * * * *` (30분마다)로 변경하세요.

## 작동 원리와 유의사항
- RSS와 GDELT에서 해외 매체 기사 제목을 찾고, 한국 방산 관련 키워드 기반으로 필터링합니다. **AI 의미 분석이 아닙니다.**
- 기사별 제목 기반 SHA-256을 SQLite에 저장하고 GitHub Bot이 변경된 DB를 `git commit`/`git push`하므로 재실행에도 중복 알림을 줄입니다.
- 동시 실행은 GitHub Actions `concurrency` 설정으로 직렬화합니다. 텔레그램 전송에 성공하고 DB가 GitHub에 커밋되기 전에 실행이 중단된다면 드물게 중복 알림이 생길 수 있습니다.
- GitHub 저장소의 기본 브랜치에서만 스케줄이 동작합니다. GitHub 정책에 따라 장기간 비활성 공개 저장소의 스케줄이 중단될 수 있습니다.
- 일부 출처는 GDELT 또는 Google News에서 누락됩니다. 기사 전체 본문을 무단으로 우회 수집하지 않습니다.
- `data/articles.db`는 장기간 사용 시 크기와 git 이력이 커질 수 있으므로 장기 운영 시 외부 DB 또는 저장소 이력 관리가 필요합니다.

## 설정 수정
- 검색 대상: `monitor.py`의 `SOURCES`, `QUERIES`, `RSS_QUERIES`
- 검색 정확도: 환경변수 `MIN_SCORE` (기본 3)
- 이미 발생한 기사도 처음부터 알림: GitHub Actions의 첫 실행에서 기본적으로 하지 않음. 기존 기록을 삭제하거나 `SEED_ON_FIRST_RUN`을 `false`로 수정하는 것은 예상치 못한 알림 폭주를 일으킬 수 있습니다.
- 반복 오류 발생 시 Actions → 해당 실행 → 로그 확인

## PC에서 점검(선택)
```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

`monitor.py --loop` 모드는 로컬 상시 실행용으로 남겨 두었지만 GitHub에서는 `python monitor.py`로 **한 번만** 실행합니다.
