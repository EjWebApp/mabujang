# 마부장님 — AI 유지보수 웹서비스

비개발자 소유자가 채팅으로 요청하면, 서버에 설치된 "마부장님"(Claude 에이전트)이
저장소를 직접 수정·검증하고, 승인을 받으면 배포한 뒤 보고합니다.

## 흐름
1. **저장소 등록**: 웹에서 Git 주소와 작업 기준 브랜치(예: `demo_mabujang`)를 입력합니다. 원격에 없는 브랜치면 새로 만듭니다.
2. **작업환경 구성**: 서버 로컬(`data/workspaces/`)에 clone한 뒤, AI가 구조 파악, 의존성 설치, 테스트를 하고 `MABUJANG.md`(환경 메모, 커밋 안 함)를 작성합니다.
3. **채팅 요청**: 요청 1건마다 작업 브랜치(`mabu/job-N`)를 따서 AI가 수정하고 테스트합니다. "검증 명령"이 설정되어 있으면 시스템이 한 번 더 직접 실행해 확인합니다.
4. **배포**: 관리자가 **[배포 승인]**을 누르면 기준 브랜치에 병합하고 push한 뒤 배포 명령을 실행합니다.
   - "위험도 낮은 작업 자동 배포" 옵션을 켜면 승인 없이 배포합니다.
5. **보고**: 쉬운 한국어 보고(무엇을/왜/검증 결과/위험도)를 채팅으로 보냅니다.
   - 위험 작업, 테스트 실패, 오류가 생기면 **개발자 호출**(웹훅 알림) 상태가 되고 작업 브랜치는 보존됩니다.

## 설치 (Linux 서버)
```bash
npm i -g @anthropic-ai/claude-code     # AI 엔진
cp .env.example .env && vi .env        # ANTHROPIC_API_KEY, MABU_ADMIN_PASSWORD
./run.sh                               # http://서버:8080
```
비공개 저장소에 접근하려면 서버에 git 인증이 필요합니다(SSH 키 또는 `https://<token>@github.com/...`).

## 비용 절감
- **AI 없이 실행하는 버튼(무료)**: [빌드 확인]은 검증 명령을, [서버 실행/중지]는 서버 실행 명령을 실행합니다. 실패하면 [마부장님께 원인 분석 요청]을 눌러, 그때만 로그를 첨부해 AI를 부릅니다.
- **실행 전 준비(무료)**: [서버 실행]을 누르면 먼저 "실행 전 명령"(예: `docker start my-postgres`)을 실행하고, "확인할 포트"(예: `5432`)가 열릴 때까지 기다린 뒤 서버를 띄웁니다. Windows에서 Docker Desktop이 꺼져 있으면 자동으로 켭니다.
- **대화 이어붙이기 끔**(`MABU_RESUME=0`): 요청마다 새로 시작하고, 맥락은 MABUJANG.md로만 전달합니다.
- **모델 선택**: 기본은 Sonnet입니다. 저장소별로 [설정]에서 Haiku(저렴), Sonnet, Opus(어려운 작업)를 고를 수 있습니다.
- **비용 표시**: 작업별 비용과 저장소 누적 비용을 화면에 보여 줍니다.

## 안전장치
- **작업 폴더 밖 접근 차단**(`MABU_RESTRICT=1`): AI는 자기 작업 폴더만 보고 고칠 수 있습니다.
- **저장소별 비밀값**: DB 비밀번호 등은 [설정 → 비밀값]에 넣습니다. 빌드, 실행, AI 작업에 환경변수로 전달되고, 화면에는 이름만 표시됩니다.
- AI는 `git commit/push`를 할 수 없습니다. 커밋, 병합, push는 시스템이 하고, 배포는 승인 후에만 진행됩니다.
- 저장소별 작업은 한 번에 하나씩 순서대로 처리합니다.
- 작업 1건당 최대 턴 수와 비용 상한을 둡니다(`MABU_MAX_TURNS`, `MABU_MAX_BUDGET_USD`).

## 테스트
```bash
pip install pytest httpx && python -m pytest -q   # mock AI로 전체 흐름 검증
MABU_AGENT=mock ./run.sh                         # API 키 없이 UI 체험
```

## 구조
```
mabujang/app.py        REST API + 화면 제공
mabujang/workflow.py   등록→환경구성→작업→승인→배포→보고
mabujang/agent.py      Claude Agent SDK 실행기 / Mock
mabujang/gitops.py     브랜치·커밋·병합·push
mabujang/prompts.py    마부장님 역할·보고 형식
mabujang/static/       채팅 UI (단일 HTML)
```
