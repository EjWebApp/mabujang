"""비용 절감·보안 기능 테스트: 버튼 작업(무료), 비밀값 마스킹, 로그 첨부, 경로 차단, DB 마이그레이션."""
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from mabujang import db
from mabujang.app import app
from mabujang.config import settings
from mabujang.guard import check
from mabujang.prompts import request_prompt


def _origin(tmp: Path) -> Path:
    src = tmp / "src"; src.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=src, check=True, capture_output=True)
    run("init", "-b", "main"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (src / "README.md").write_text("x\n"); run("add", "."); run("commit", "-m", "i")
    subprocess.run(["git", "clone", "--bare", str(src), str(tmp / "o.git")], check=True, capture_output=True)
    return tmp / "o.git"


def _wait(fn, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.3)
    raise AssertionError("timeout")


def test_buttons_secrets_and_log_attach():
    tmp = Path(tempfile.mkdtemp())
    origin = _origin(tmp)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    py = sys.executable
    with TestClient(app) as c:
        rid = c.post("/api/repos", json={
            "name": "v2", "git_url": str(origin), "base_branch": "main",
            "test_cmd": f'{py} -c "import os,sys; sys.exit(0 if len(os.environ.get(\'DB_PASS\',\'\'))==6 else 3)"',
            "run_cmd": f"{py} -m http.server {port} --bind 127.0.0.1",
            "run_url": f"http://127.0.0.1:{port}/"}).json()["id"]
        _wait(lambda: c.get(f"/api/repos/{rid}").json()["status"] == "ready")

        # 비밀값 없이 빌드 → 실패 메시지 (AI 비용 0)
        c.post(f"/api/repos/{rid}/actions/build")
        _wait(lambda: any(m["role"] == "action_fail" for m in c.get(f"/api/repos/{rid}/messages").json()))

        # 비밀값 등록 → 화면에는 키만, 값은 노출 안 됨
        r = c.patch(f"/api/repos/{rid}", json={"env_set": {"DB_PASS": "s3cret"}}).json()
        assert r["env_keys"] == ["DB_PASS"] and "s3cret" not in str(r)
        assert "s3cret" not in str(c.get("/api/repos").json())

        c.post(f"/api/repos/{rid}/actions/build")
        _wait(lambda: any(m["role"] == "action_ok" and "빌드 성공" in m["content"]
                          for m in c.get(f"/api/repos/{rid}/messages").json()))

        # 서버 실행 → 주소 응답 확인 → 중지
        c.post(f"/api/repos/{rid}/actions/start")
        _wait(lambda: any("서버 실행됨" in m["content"] for m in c.get(f"/api/repos/{rid}/messages").json()))
        assert c.get(f"/api/repos/{rid}").json()["server"]["running"]
        c.post(f"/api/repos/{rid}/actions/stop")
        _wait(lambda: not c.get(f"/api/repos/{rid}").json()["server"]["running"])

        # 실패 로그 첨부 요청 → 작업 생성, 비용 누적 표시
        jid = c.post(f"/api/repos/{rid}/chat", json={"message": "확인만 해줘", "attach_log": True}).json()["job_id"]
        _wait(lambda: c.get(f"/api/jobs/{jid}").json()["status"] == "no_change")
        assert db.one("SELECT attach_log FROM jobs WHERE id=?", (jid,))["attach_log"] == 1
        assert c.get(f"/api/repos/{rid}").json()["total_cost_usd"] > 0

        # 승인 전 미리 보기: 작업 브랜치 코드로 서버 실행 (작업 브랜치에만 있는 파일이 보임)
        import urllib.error
        import urllib.request

        def http(path):
            try:
                return urllib.request.urlopen(f"http://127.0.0.1:{port}/{path}", timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code

        msgs = lambda: [m["content"] for m in c.get(f"/api/repos/{rid}/messages").json()]
        jid = c.post(f"/api/repos/{rid}/chat", json={"message": "공지 문구 바꿔줘"}).json()["job_id"]
        _wait(lambda: c.get(f"/api/jobs/{jid}").json()["status"] == "awaiting_approval")
        # 승인 대기로 바뀐 직후에는 작업이 잠금을 풀기 전이라 409일 수 있다
        _wait(lambda: c.post(f"/api/jobs/{jid}/preview").status_code != 409)
        _wait(lambda: any(f"#{jid} 미리 보기 실행됨" in m for m in msgs()))
        assert c.get(f"/api/repos/{rid}").json()["server"]["preview"] == jid
        assert http("MABU_CHANGES.md") == 200
        # [서버 실행]은 기준 브랜치로 다시 띄운다 → 변경 파일 없음
        n = sum("✅ 서버 실행됨" in m for m in msgs())
        c.post(f"/api/repos/{rid}/actions/start")
        _wait(lambda: sum("✅ 서버 실행됨" in m for m in msgs()) > n)
        assert c.get(f"/api/repos/{rid}").json()["server"]["preview"] is None
        assert http("MABU_CHANGES.md") == 404
        # 미리 보기 중 승인 → 미리 보기 서버를 끄고 배포
        c.post(f"/api/jobs/{jid}/preview")
        _wait(lambda: c.get(f"/api/repos/{rid}").json()["server"]["preview"] == jid)
        c.post(f"/api/jobs/{jid}/approve")
        _wait(lambda: c.get(f"/api/jobs/{jid}").json()["status"] == "done")
        assert not c.get(f"/api/repos/{rid}").json()["server"]["running"]


def test_prompt_includes_log_and_env_keys_only():
    p = request_prompt("고쳐줘", "gradlew.bat compileJava", ["DB_PASS"], "gradlew.bat bootRun",
                       "http://localhost:9080", "ERROR password authentication failed")
    assert "DB_PASS" in p and "password authentication failed" in p and "bootRun" in p


def test_guard():
    w = r"D:\mabu\data\workspaces\1-vb"
    assert check("Read", {"file_path": "src/A.java"}, w) is None
    assert check("Read", {"file_path": r"D:\dev\project\vbiomechanics\secret.yml"}, w)
    assert check("Bash", {"command": "cat ../../secret.yml"}, w)
    assert check("Bash", {"command": "ls /d/dev/project"}, w)
    assert check("Bash", {"command": "cat ~/.ssh/id_rsa"}, w)
    assert check("Bash", {"command": "curl -s http://localhost:9080/login | head"}, w) is None
    assert check("Bash", {"command": '"C:/Program Files/Java/bin/java" -version'}, w) is None
    assert check("Bash", {"command": "./gradlew.bat compileJava 2>&1 | tail -40"}, w) is None


def test_migration_from_v1_db():
    d = Path(tempfile.mkdtemp())
    old = sqlite3.connect(d / "mabujang.db")
    old.executescript("CREATE TABLE repos (id INTEGER PRIMARY KEY, name TEXT, git_url TEXT, base_branch TEXT, "
                      "workdir TEXT, test_cmd TEXT, deploy_cmd TEXT, push_on_deploy INTEGER, auto_deploy INTEGER, "
                      "status TEXT, env_notes TEXT, session_id TEXT, created_at REAL);"
                      "INSERT INTO repos VALUES (1,'old','u','main','w','','',1,0,'ready','','',0);")
    old.commit(); old.close()
    prev = settings.data_dir
    db.reset_for_tests(); settings.data_dir = str(d)
    try:
        r = db.one("SELECT * FROM repos WHERE id=1")
        assert r["name"] == "old" and r["env_vars"] == "" and "run_cmd" in r
    finally:
        db.reset_for_tests(); settings.data_dir = prev


def test_pre_run_and_port_wait():
    import asyncio
    from mabujang import actions
    tmp = Path(tempfile.mkdtemp())
    origin = _origin(tmp)
    lsock = socket.socket(); lsock.bind(("127.0.0.1", 0)); lsock.listen(); db_port = lsock.getsockname()[1]
    s = socket.socket(); s.bind(("127.0.0.1", 0)); web = s.getsockname()[1]; s.close()
    py = sys.executable
    prev = settings.port_wait
    settings.port_wait = 3
    try:
        with TestClient(app) as c:
            rid = c.post("/api/repos", json={
                "name": "pre", "git_url": str(origin), "pre_run_cmd": f'{py} -c "print(\'db up\')"',
                "wait_ports": f"127.0.0.1:{db_port}", "run_cmd": f"{py} -m http.server {web} --bind 127.0.0.1",
                "run_url": f"http://127.0.0.1:{web}/"}).json()["id"]
            _wait(lambda: c.get(f"/api/repos/{rid}").json()["status"] == "ready")
            msgs = lambda: c.get(f"/api/repos/{rid}/messages").json()

            # 준비 OK → 서버 실행
            c.post(f"/api/repos/{rid}/actions/start")
            _wait(lambda: any("서버 실행됨" in m["content"] for m in msgs()))
            assert any("실행 전 준비 완료" in m["content"] for m in msgs())
            c.post(f"/api/repos/{rid}/actions/stop")
            _wait(lambda: not c.get(f"/api/repos/{rid}").json()["server"]["running"])

            # DB 포트 닫힘 → 서버를 띄우지 않고 안내
            lsock.close()
            c.post(f"/api/repos/{rid}/actions/start")
            _wait(lambda: any("포트가" in m["content"] and m["role"] == "action_fail" for m in msgs()))
            assert not c.get(f"/api/repos/{rid}").json()["server"]["running"]

            # 실행 전 명령 실패 → 컨테이너 이름 힌트
            c.patch(f"/api/repos/{rid}", json={"pre_run_cmd": f'{py} -c "import sys; print(\'Error: No such container: pg\'); sys.exit(1)"', "wait_ports": ""})
            c.post(f"/api/repos/{rid}/actions/start")
            _wait(lambda: any("컨테이너 이름" in m["content"] for m in msgs()))
    finally:
        settings.port_wait = prev
    assert actions.parse_ports("5432, localhost:6379") == [("127.0.0.1", 5432), ("localhost", 6379)]


def test_git_output_with_korean_commit_message():
    """Windows(cp949)에서 한글 커밋 메시지 출력 때문에 stdout이 None이 되던 문제."""
    from mabujang import gitops
    tmp = Path(tempfile.mkdtemp())
    gitops.run(["git", "init", "-b", "main"], tmp)
    gitops.ensure_identity(tmp)
    (tmp / "a.txt").write_text("x\n")
    assert gitops.commit_all(tmp, "마부장님 #5: 로그인 아이디 입력칸 문구를 '아이디'로 변경")
    assert "아이디" in gitops.run(["git", "log", "-1", "--format=%s"], tmp)


def test_readonly_tools_stay_inside_workdir():
    """질문 모드 도구: 작업 폴더 밖·.git·비밀값 파일은 읽지 않는다."""
    from mabujang import gitops
    from mabujang.readonly import run_tool
    tmp = Path(tempfile.mkdtemp())
    gitops.run(["git", "init", "-b", "main"], tmp)
    (tmp / "app.yml").write_text("about-video:\n  path: storage/about\n")
    (tmp / ".env").write_text("DB_PASS=secret\n")
    (tmp / "secret.yml").write_text("pw: secret\n")
    gitops.run(["git", "add", "-A"], tmp)
    assert "2: " in run_tool(tmp, "read_file", {"path": "app.yml"})
    assert "about-video" in run_tool(tmp, "search_code", {"text": "about-video"})
    for p in ["../outside.txt", ".env", "secret.yml", ".git/config"]:
        assert "읽을 수 없는" in run_tool(tmp, "read_file", {"path": p})
    assert "secret" not in run_tool(tmp, "search_code", {"text": "secret"})
    assert ".env" not in run_tool(tmp, "list_files", {"contains": "env"})
