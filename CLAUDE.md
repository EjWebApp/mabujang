# 마부장님 (Mabujang) — 인수인계 메모

> 이 파일은 claude.ai 세션에서 만든 이 프로젝트를 VS Code의 Claude Code가 이어받기 위한 메모입니다.
> 작성: 2026-10-31. 사용자: 마징가 (독립 개발자, 한국어로 짧고 지시적으로 소통하며 빠른 실행을 선호, 스크린샷을 자주 공유함).

## 1. 무엇을 만드는가
**AI 유지보수 웹서비스 "마부장님"**. 비개발자(웹서비스 소스 소유자)가 채팅으로 요구사항을 말하면,
마부장님이 설치된 서버 로컬에서 AI(Claude Agent SDK = Claude Code 엔진)가 저장소를 고치고, 검증하고, 승인 후 배포하고, 보고한다.

흐름: ① Git 저장소 등록 → ② 서버 로컬에 clone하고 AI가 작업환경 구성(MABUJANG.md 작성) → ③ 채팅 요청
→ ④ 작업 브랜치에서 AI 수정 → 시스템 재검증 → 관리자 [배포 승인] → 기준 브랜치에 병합, push, 배포 명령 → ⑤ 쉬운 한국어로 보고.
위험하거나 실패하면 "개발자 호출"(needs_developer, 웹훅 알림)로 처리하고 작업 브랜치는 보존한다.

## 2. 스택 / 실행
- Python 3.10+, FastAPI, SQLite(표준 sqlite3), `claude-agent-sdk`, 프론트는 단일 HTML(vanilla JS, 2초 폴링).
- AI 엔진: Claude Code CLI(`npm i -g @anthropic-ai/claude-code`)가 필요하다. SDK가 이 CLI를 실행한다.
- 실행: Windows `.\run.bat`, Linux `./run.sh` → http://localhost:8080
- 설정은 `.env`에 둔다(`.env.example` 참고). 핵심 값: `ANTHROPIC_API_KEY`, `MABU_ADMIN_PASSWORD`, `MABU_MODEL`(기본 claude-sonnet-5-5),
  `MABU_MAX_TURNS`=30, `MABU_MAX_BUDGET_USD`=1, `MABU_RESUME`=0, `MABU_RESTRICT`=1, `MABU_AGENT`(claude|mock).
- 테스트: `pip install pytest httpx && python -m pytest -q` (mock 에이전트로 전체 흐름 검증, 현재 25개 통과)
- API 키 없이 UI만 볼 때: `.env`에서 `MABU_AGENT=mock`으로 바꾼다.
- `run.bat`은 **ASCII + CRLF**로 저장해야 한다(한글이나 LF가 들어가면 cmd가 줄을 잘못 읽는다. 실제로 겪은 문제).

## 3. 파일 구조
```
mabujang/app.py        REST API, 화면 제공, lifespan 종료 시 버튼 서버 정리
mabujang/workflow.py   등록→환경구성(register_and_setup) / 요청 처리(handle_request) / deploy / reject
mabujang/agent.py      ClaudeAgent(SDK query, PreToolUse 차단 훅, 모델·env 전달) / MockAgent / 보고 블록 파싱
mabujang/actions.py    AI 없는 버튼(무료): build, start(실행 전 명령 → 포트 대기 → 서버 실행 → URL 확인), stop
                       Windows에서는 Docker Desktop 자동 실행(중복 실행 방지 락), taskkill /T로 프로세스 트리 종료
mabujang/guard.py      AI 도구 호출 시 작업 폴더 밖 경로 차단(Read/Write/Edit/Glob/Grep 경로, Bash 절대경로·../·~ 검사)
mabujang/gitops.py     clone(원격에 없는 브랜치면 생성), 작업 브랜치, commit, merge+push, sh(env 전달, 타임아웃)
mabujang/prompts.py    SYSTEM_PROMPT(마부장님 역할, 비용 절감 규칙, 보고 JSON 형식), SETUP_PROMPT, request_prompt
mabujang/db.py         스키마, 자동 마이그레이션(MIGRATIONS: 기존 DB에 컬럼 추가), parse_env
mabujang/notify.py     웹훅(Slack/Discord 호환), 개발자 호출(노션 + 메일)
mabujang/notion_link.py 채팅의 노션 링크 읽기(AI 없음)
mabujang/static/index.html  채팅 UI, 작업 현황, 설정(비밀값, 모델, 실행 전 명령, 포트), 버튼
tests/test_flow.py, tests/test_v2.py
```

## 4. 주요 설계 결정 (바꾸기 전에 사용자와 상의할 것)
- **AI는 git commit/push/배포를 하지 않는다.** 시스템이 커밋하고, 관리자 승인 후에만 병합·배포한다(auto_deploy 옵션은 위험도 low일 때만).
- **보고 형식**: AI 응답 마지막의 ```mabujang-report {json}``` 블록(status: done|no_change|needs_developer).
- **비용 절감**(사용자가 요청 1건 약 $0.5를 보고 줄여 달라고 함):
  실행·빌드는 AI 없는 버튼으로 처리하고 실패 시에만 [마부장님께 원인 분석 요청](attach_log=1이면 마지막 로그를 첨부)한다.
  세션 이어붙이기는 기본으로 끈다. 기본 모델은 Sonnet이고 저장소별로 Haiku/Opus를 선택할 수 있다. 작업별·누적 비용을 화면에 표시한다.
- **보안**: 작업 폴더 밖 접근을 차단한다(실제로 AI가 원본 폴더의 secret.yml을 들여다본 일이 있어서 추가함).
  저장소별 비밀값(env_vars)은 화면에 키 이름만 노출하고, 빌드·실행·AI에 환경변수로 전달한다. WebSearch/WebFetch는 비활성.
- **DB 방식 무관**: "실행 전 명령"은 임의 명령이다(docker start / net start / systemctl / 비움). "확인할 포트"는 항상 권장한다.
- MABUJANG.md(AI 환경 메모)는 `.git/info/exclude`에 넣어 커밋하지 않는다.

## 5. 테스트 대상: vbiomechanics (벡터바이오)
- 저장소: https://github.com/vectorbio/vbiomechanics.git (비공개), 기준 브랜치 **`demo_mabujang`**(demo_dev와 같은 커밋 b3b8e58에서 시작). main/master는 건드리지 않는다.
- 스택: Spring Boot 3.5.5, Java 17, Gradle 8.14, Thymeleaf, PostgreSQL. 정적 이미지가 많아 저장소 크기가 약 281MB다.
- 로컬 실행: `gradlew.bat bootRun`, 포트 **9080**, 확인 주소 `http://localhost:9080/ui/login`. 검증 명령은 `gradlew.bat compileJava`(BUILD SUCCESSFUL 확인됨).
- DB: localhost:5432/demo, user `demo`, 비밀번호는 환경변수 `DB_PASS`(application-local.yml의 `${DB_PASS:기본값}` 기본값은 현재 DB와 맞지 않음).
  사용자 PC에서 PostgreSQL을 **도커로** 운영한다. DB가 꺼져 있으면 앱이 E-COM-500 JSON을 반환한다.
- 사용자 PC 경로: 마부장님 `D:\dev\project\9_VectorBio\src\mabujang\mabujang`, 기존 저장소 사본 `...\mabujang\vbiomechanics_demo_mabujang`
  (마부장님은 이 사본을 쓰지 않고 `data\workspaces\`에 따로 clone한다).
  사본 폴더의 `.git\mabujang\demo_mabujang_src.tar.gz`는 테스트용으로 남긴 파일이라 지워도 된다.
- 실제 AI 실행 결과(사용자 PC): 환경 구성 성공, "실행해 줘" → DB 비밀번호 인증 실패로 needs_developer(코드 변경 없음).

## 6. 현재 상태
- 완료: 전체 흐름, 버튼 작업, 비밀값, 모델 선택, 비용 표시, 경로 차단, 실행 전 명령·포트 대기, Docker Desktop 자동 실행과 중복 방지, DB 자동 마이그레이션.
- 검증 완료: mock 테스트 6개, Playwright 화면 확인(클라우드), 사용자 PC에서 실제 Claude 실행(환경 구성 + 요청 1건).
- 검증 완료(2026-10-31, 사용자 PC): 실행 전 명령 `docker start vbio-postgres` + 포트 5432 + 비밀값 DB_PASS로 [서버 실행] → 약 9초 만에 9080 응답, DB 연결(Hikari) 성공, [중지] 후 9080 해제.
- 수정(2026-10-31): 첫 실제 요청(#5, 로그인 아이디 칸 'ID'→'아이디')이 AI 수정·커밋까지 성공한 뒤 `gitops.run()`이 git 출력의 한글을
  cp949로 읽다 stdout=None → `None + str`로 실패했다. subprocess에 `encoding="utf-8", errors="replace"`를 지정했다(테스트 7개 통과).
  Windows에서 subprocess로 텍스트를 읽을 때는 항상 encoding을 명시할 것. #5의 수정은 브랜치 `mabu/job-5`에 남아 있다.
- 검증 완료(2026-10-31): 실제 요청 #6 → 승인 → `demo_mabujang` push(0eca43e)까지 전체 흐름.
- 추가(2026-10-31): 작업 목록의 [↩ 되돌리기] 버튼(AI 없음). 가장 최근에 배포된 작업 1건만 대상이고, `git revert -m 1` → push → 배포 명령 순서로 처리한다.
  병합 커밋은 `jobs.merge_sha`에 저장한다(없으면 "마부장님: <branch> 반영" 메시지로 찾음). 충돌하면 중단하고 개발자를 호출한다. 배포 전 취소는 기존 [취소](reject) 버튼을 쓴다.
  배경: 사용자가 채팅으로 "이번 명령 취소해줘"라고 했더니 AI가 $0.03만 쓰고 아무것도 하지 못했다.
- 반응형 UI(2026-10-31): 1280px 이하는 좌우 패널을 좁히고, 980px 이하(휴대폰)는 채팅만 보인다. 저장소 목록(☰)과 작업 현황은 서랍으로 연다.
  승인·취소·되돌리기는 입력창 위 버튼 막대(.quick)에도 나온다. Edge + Playwright로 1280/1100/390px 화면을 확인했다.
- 자동 분류(2026-10-31, `MABU_AUTO_ROUTE`=1): 요청마다 Haiku(`MABU_FAST_MODEL`)로 question/simple/complex를 먼저 나눈다(약 $0.0003, 1초).
  question은 Claude Code 없이 `anthropic` SDK로 처리한다. Haiku가 읽기 전용 도구(`readonly.py`: list_files·search_code·read_file,
  작업 폴더 안만, .git·.env·secret 차단)로 최대 6번 조사한다. 실측 약 $0.002 (Claude Code는 $0.106). 상태는 `answered`이고 코드·브랜치 변경은 없다.
  simple은 저장소 모델이 기본값일 때만 Haiku로 Claude Code를 실행하고, complex와 attach_log는 기존 방식이다. 분류에 실패하면 complex로 처리한다.
  jobs.mode와 jobs.model을 기록하고, 요청 프롬프트에 최근 대화 6개를 넣는다("자세히 조사해줘" 같은 후속 요청용).
- 폴더 열기(2026-10-31): 질문 모드 답변의 `open_paths`를 메시지에 `[[open:경로]]`로 넣으면 화면이 [📂 폴더 열기] 버튼으로 바꾼다.
  `POST /api/repos/{id}/open-folder`는 작업 폴더 안 경로만 받고(readonly.safe_path), 폴더가 없으면 만든 뒤 서버 PC 탐색기로 연다(휴대폰에서 누르면 PC에서 열림).
  vbiomechanics의 `storage/`는 .gitignore 대상이라 `git clean -fd`로 지워지지 않는다.
- 데이터 파일 탭(2026-10-31): [설정] 창을 [기본 설정]/[데이터 파일] 탭으로 나눴다. Haiku가 읽기 전용 도구로 설정·코드를 읽어 데이터 폴더 목록을 만든다
  (repos.data_dirs JSON, data_status, job kind=data). 환경 구성 직후 자동으로 돌고, 탭의 [다시 분석하기]로도 실행한다. vbiomechanics 실측 $0.0036, 15초, 4곳.
  상대 경로는 작업 폴더 기준으로 해석한다(workflow.resolve_data_dir). 열기는 목록 번호로만 한다(`/data-dirs/{idx}/open`).
  작업 폴더 안 폴더는 없으면 만들어 열고, 밖(D:/opt/vector-storage, ../AI-solution 등)은 이미 있을 때만 연다(사용자 결정: 밖은 만들지 않음).
  [직접 등록하기](manual=true, 다시 분석해도 유지)와 [삭제]를 지원한다. 삭제한 분석 결과는 repos.data_hidden에 넣어 다시 분석해도 나오지 않게 한다.
  삭제는 목록에서만 지우고 폴더·파일은 그대로 둔다.
- 폴더 열기(Windows): os.startfile 뒤에 제목이 폴더 이름인 CabinetWClass 창을 찾아, Alt 입력과 SetForegroundWindow로 맨 앞에 띄운다
  (서버가 연 창이 작업표시줄에만 뜨는 문제를 해결함. Win11은 새 탭으로 열림).
- 미리 보기(2026-10-31): 승인 대기 작업의 [👀 미리 보기]는 작업 브랜치를 checkout하고 서버를 실행한다(`POST /api/jobs/{id}/preview`, status.preview=작업 번호).
  [서버 실행]은 항상 기준 브랜치를 checkout한 뒤 실행한다. 승인이나 취소 전에는 미리 보기 서버를 끈다(_stop_preview).
  AI 작업이나 배포 중(workflow.busy=저장소 lock)에는 실행과 미리 보기를 409로 막는다(브랜치 전환 충돌 방지).
  배경: #12 자동 재생 수정이 승인 대기라 작업 브랜치에만 있었는데, [서버 실행]은 기준 브랜치로 떠서 사용자가 "안 된다"고 느꼈다.
- 자동으로 브라우저 열기: [서버 실행]·[미리 보기]를 누르는 순간 빈 탭을 열어 두고(나중에 열면 팝업 차단에 막힘),
  '서버 실행됨/미리 보기 실행됨' 메시지가 오면 그 탭을 run_url로 옮긴다. 실패하면 탭에 실패 안내를 띄운다(index.html withTab/settleTab).
  메시지 속 http(s) 주소는 링크로 바꿔 보여 준다.
- 작업 상세(⋮): 작업 현황 카드의 ⋮ 버튼으로 상세 창(jobDlg)을 연다. 요청, 처리 방식·모델·비용, 시각, 보고 전체, 변경 파일, 변경 코드(diff 색 표시), 배포 로그,
  상태별 버튼, [채팅에서 보기]가 있다. `GET /api/jobs/{id}/diff`는 배포 전이면 base...branch, 배포 후면 merge_sha^1..merge_sha를 보여 주고 150KB에서 자른다.
- 병합 충돌(2026-11-02): #12와 #8이 같은 main.html을 고쳐 #8 배포 중 충돌이 났고, 작업 폴더가 합치다 만 상태(MERGE_HEAD)로 남아 [서버 실행]이 계속 실패했다.
  이제 merge_and_push는 실패하면 merge --abort와 reset으로 원상복구한다. push가 실패해도 로컬 병합을 취소한다.
  충돌하면 jobs.status=conflict(화면 표시 '충돌')로 두고 [취소](reject)를 허용한다. 시작·실행 전에 cleanup_merge로 남은 흔적을 정리한다.
- 시간 표시: 메시지마다 '#작업번호 · 시각'을 붙이고, 날짜가 바뀌면 구분선을 넣는다. 작업 카드와 진행 단계에도 시각을 표시한다.
- 디자인 교체(2026-11-02): 시안 `마부장님 콘솔.html` 적용(index.html만 변경, API·서버 메시지 문구는 그대로). 어두운 사이드바, 상태 점, 작업 카드, 입력창 위 칩 형태의 빠른 버튼.
  채팅 메시지는 화면에서 다시 나눈다(msgHtml/cardParts): 첫 줄 이모지를 떼어 제목으로 쓰고, ` — ` 뒤는 본문, ``` 블록은 어두운 로그로 표시한다. **actions.py/workflow.py의 메시지 형식을 바꾸면 이 함수도 확인할 것.**
  작업 카드를 누르면 상세 창이 열리고, [변경 파일]·[배포 로그]는 상세 창의 해당 위치로 간다(카드 안에서 펼치던 방식 대체).
  글꼴은 CDN(jsDelivr Pretendard, Google Fonts JetBrains Mono)이며 인터넷이 없으면 시스템 글꼴로 대체된다.
- 개발자 호출 알림(2026-11-02): notify.call_developer가 노션 페이지를 만들고, 그 링크를 담은 메일을 보낸 뒤, 결과를 채팅에 알린다(별도 스레드라 작업을 막지 않음).
  대상은 needs_developer, 작업 실패, 배포 실패, 되돌리기 실패다. 충돌은 관리자가 [취소]로 처리하므로 호출하지 않고, 완료 알림은 웹훅만 쓴다.
  git 로그 같은 세부 사항은 채팅에 넣지 않고 메일·노션의 "개발자 전달 사항"과 작업 상세(jobs.dev_note, notion_url)에 둔다. 비밀값은 notify.redact로 가린다.
  노션 API는 버전 2026-03-11 기준이다: GET /databases/{id} → data_sources[0].id → GET /data_sources/{id} → POST /pages(parent=data_source_id). 제목 열만 있어도 동작하고, 상태·저장소·작업 번호·브랜치 열은 있으면 채운다.
  설정: MABU_NOTION_TOKEN, MABU_NOTION_DATABASE, MABU_SMTP_*, MABU_MAIL_TO(.env.example 참고). [설정]의 [개발자 알림 시험 보내기]로 확인한다.
  **실제 노션·Gmail로는 아직 미검증**(가짜 서버 테스트만 통과). 토큰을 받으면 시험 발송으로 확인할 것.
- 노션 링크 읽기(2026-11-02): 채팅 메시지의 노션 링크(notion.so·notion.com·notion.site)를 `MABU_NOTION_TOKEN`으로 읽어 프롬프트에 붙인다(notion_link.py, workflow._read_links). 링크 주소로 직접 접속하지 않고 ID만 뽑아 노션 API를 호출한다.
  페이지(속성, 본문, 페이지 댓글), 표(앞 30행), 페이지 안 특정 위치(#)를 읽는다. 하위 페이지와 이미지·파일 내용은 읽지 않는다. 링크당 12,000자까지. 읽지 못하면 AI를 부르지 않고 이유를 채팅에 알린다(status=answered).
  내용은 prompts.linked_text의 <linked> 안에 참고 자료로 넣고, 비밀값 출력·외부 전송 등은 따르지 말라고 지시한다. 링크 없는 다음 요청은 직전 요청의 링크를 이어서 읽는다("자세히 조사해줘"용).
  **실제 노션으로는 아직 미검증**(가짜 서버 테스트만 통과, 대상 페이지가 통합에 연결되지 않아 404). 노션에 댓글 달기는 아직 없다.
- **미검증**: Windows에서 Docker Desktop이 꺼져 있을 때 자동 실행과 중복 방지, 간단한 수정(Haiku)의 실제 품질.
- 직전 이슈: Docker Desktop이 3개 동시에 실행돼 "Lingering processes detected" 경고가 떴다. [Stop processes]로 해결하라고 안내했고, 코드도 수정했다(_desktop_running, _docker_lock, _starting).

## 7. 다음 할 일 (우선순위 순)
1. ~~사용자 PC에서 [설정] → 실행 전 명령, 포트, 비밀값 DB_PASS를 넣고 [서버 실행]으로 확인~~ (완료)
2. ~~실제 요청 → 승인 → push~~ (완료, #6). [되돌리기]로 #6을 되돌려 사용자 PC에서 확인한다.
3. AI가 직접 bootRun을 띄우면 Windows에서 java.exe가 남아 포트를 잡는다. 작업 종료 후 9080 등 포트를 점유한 자식 프로세스를 정리하는 후처리를 검토한다.
4. guard의 Bash 검사는 휴리스틱이다(변수 확장, 스크립트 내부 경로는 못 막음). 운영 서버라면 OS 사용자 분리나 컨테이너 격리를 검토한다.
5. 작업 취소 버튼(실행 중인 AI 작업 중단), 작업 큐 표시.
6. 운영 배포: HTTPS, 인증 강화(현재는 단일 비밀번호와 sha256 토큰), Linux 서버용 서비스 등록(systemd).
7. ~~작업 종류별 모델 자동 선택~~ (완료: 질문 모드 + 간단한 수정 Haiku). simple 수정의 실제 품질·비용을 실측한다.
8. 비밀값 암호화: repos.env_vars가 data/mabujang.db에 평문으로 저장된다. `.env`의 비밀 키(예: MABU_SECRET_KEY)로 암호화해 저장하고,
   기존 평문 값은 서버 시작 시 자동 변환한다(DB 파일 유출 대비, 운영 서버 배포 전 필수). 키를 잃으면 비밀값을 다시 입력해야 한다는 점을 안내한다.
9. 원격 접속(2026-10-31 논의, 아직 하지 않음): 마부장님은 사용자 노트북에서 서비스한다. 서버는 이미 0.0.0.0:8080으로 뜬다.
   - 접속 방법: 같은 와이파이는 노트북 IP:8080(방화벽 허용 필요). 외부는 Tailscale을 추천한다.
     공유기 포트 포워딩으로 인터넷에 공개하는 것은 HTTPS와 인증 강화(6번) 전에는 하지 않는다.
   - 실행 주소 자동 변환: run_url의 localhost를 브라우저가 접속한 호스트(location.hostname)로 바꿔 보여 준다(원격에서 localhost:9080 링크가 안 열림).
   - 데이터 파일 탭에 [파일 올리기]·[파일 목록]: [폴더 열기]는 서버 PC 화면에만 열리므로 휴대폰에서 영상을 올릴 수 없다. 작업 폴더 안 데이터 폴더로만 제한한다.
   - 노트북은 켜 두고 절전 모드에 들어가지 않게 안내한다.

## 8. 작업 원칙
- UI와 보고 문구는 비개발자 눈높이의 한국어로 쓴다.
- 기능을 바꾸면 `tests/`에 테스트를 추가하고 `python -m pytest -q`를 통과시킨다.
- 테스트는 `tests/conftest.py`가 모의 에이전트와 임시 DB를 강제한다. 테스트 함수를 스크립트에서 직접 부를 때는 반드시 `import tests.conftest`를 먼저 한다.
  (2026-11-02에 이걸 빠뜨려 실제 Claude와 사용자 DB로 테스트가 돌았고, 약 $0.76를 썼으며 테스트 데이터를 지워야 했다.)
- DB 스키마를 바꿀 때는 `db.MIGRATIONS`에 컬럼을 추가한다(사용자 PC의 기존 data/mabujang.db를 유지해야 함).
- 사용자에게 업데이트를 안내할 때는 "서버 Ctrl+C → 덮어쓰기(.env, data 유지) → .\run.bat → 브라우저 Ctrl+F5" 순서로 알려 준다.
