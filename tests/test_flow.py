"""전체 흐름 테스트 (mock 에이전트): 등록→환경구성→요청→승인→배포/취소/개발자호출."""
import os
import subprocess
import tempfile
import time
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ.update(MABU_AGENT="mock", MABU_DATA_DIR=str(tmp / "data"), MABU_WORKSPACES=str(tmp / "ws"))

from fastapi.testclient import TestClient  # noqa: E402

from mabujang.app import app  # noqa: E402


def git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def make_origin() -> Path:
    src = tmp / "src"
    src.mkdir()
    git("init", "-b", "main", cwd=src)
    git("config", "user.email", "t@t", cwd=src); git("config", "user.name", "t", cwd=src)
    (src / "README.md").write_text("hello\n")
    git("add", ".", cwd=src); git("commit", "-m", "init", cwd=src)
    bare = tmp / "origin.git"
    git("clone", "--bare", str(src), str(bare), cwd=tmp)
    return bare


def wait(c, path, pred, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        d = c.get(path).json()
        if pred(d):
            return d
        time.sleep(0.2)
    raise AssertionError(f"timeout: {path} -> {d}")


def test_full_flow():
    origin = make_origin()
    with TestClient(app) as c:
        marker = tmp / "deployed.txt"
        r = c.post("/api/repos", json={"name": "demo", "git_url": str(origin), "base_branch": "demo_mabujang",
                                       "deploy_cmd": f"echo ok > {marker}", "test_cmd": "test -f README.md"}).json()
        rid = r["id"]
        repo = wait(c, f"/api/repos/{rid}", lambda d: d["status"] == "ready")
        assert repo["base_branch"] == "demo_mabujang"        # 없는 브랜치 → 생성
        assert "마부장님 환경 메모" in repo["env_notes"]

        # 요청 → 승인 대기
        jid = c.post(f"/api/repos/{rid}/chat", json={"message": "공지 문구 바꿔줘"}).json()["job_id"]
        job = wait(c, f"/api/jobs/{jid}", lambda d: d["status"] == "awaiting_approval")
        assert "MABU_CHANGES.md" in job["diff_stat"]
        assert "MABUJANG.md" not in job["diff_stat"]          # 환경 메모는 커밋 안 됨
        assert "재검증 통과" in job["report"]["tests"]
        assert job["mode"] == "simple" and job["model"] == "claude-haiku-5-5"   # 간단한 문구 수정 → 저렴한 모델
        assert "+- 공지 문구 바꿔줘" in c.get(f"/api/jobs/{jid}/diff").json()["diff"]   # 상세: 작업 브랜치 변경 코드

        # 승인 → 병합 + push + 배포 명령
        c.post(f"/api/jobs/{jid}/approve")
        wait(c, f"/api/jobs/{jid}", lambda d: d["status"] == "done")
        assert marker.read_text().strip() == "ok"
        out = subprocess.run(["git", "log", "--oneline", "demo_mabujang"], cwd=origin, capture_output=True, text=True, encoding="utf-8").stdout
        assert "마부장님" in out                               # 원격 demo_mabujang 브랜치에 반영
        main_log = subprocess.run(["git", "log", "--oneline", "main"], cwd=origin, capture_output=True, text=True, encoding="utf-8").stdout
        assert "마부장님" not in main_log                      # main은 건드리지 않음
        assert "+- 공지 문구 바꿔줘" in c.get(f"/api/jobs/{jid}/diff").json()["diff"]   # 배포 후에도 병합 커밋 기준으로 보임

        # 직전 배포 되돌리기 (AI 없음)
        assert [j["can_revert"] for j in c.get(f"/api/repos/{rid}/jobs").json() if j["id"] == jid] == [True]
        c.post(f"/api/jobs/{jid}/revert")
        wait(c, f"/api/jobs/{jid}", lambda d: d["status"] == "reverted")
        out = subprocess.run(["git", "log", "--oneline", "demo_mabujang"], cwd=origin, capture_output=True, text=True, encoding="utf-8").stdout
        assert f"#{jid} 되돌리기" in out                       # 원격에도 되돌린 커밋 반영
        shown = subprocess.run(["git", "show", "demo_mabujang:MABU_CHANGES.md"], cwd=origin, capture_output=True)
        assert shown.returncode != 0                          # 변경 파일이 사라짐
        assert c.post(f"/api/jobs/{jid}/revert").status_code == 409   # 두 번 되돌리기 불가

        # 충돌: 같은 기준에서 같은 파일을 고친 작업 두 개 → 먼저 배포한 뒤 두 번째는 '충돌', 작업 폴더는 깨끗하게 유지
        ja = c.post(f"/api/repos/{rid}/chat", json={"message": "가 문구 바꿔줘"}).json()["job_id"]
        wait(c, f"/api/jobs/{ja}", lambda d: d["status"] == "awaiting_approval")
        jb = c.post(f"/api/repos/{rid}/chat", json={"message": "나 문구 바꿔줘"}).json()["job_id"]
        wait(c, f"/api/jobs/{jb}", lambda d: d["status"] == "awaiting_approval")
        c.post(f"/api/jobs/{ja}/approve")
        wait(c, f"/api/jobs/{ja}", lambda d: d["status"] == "done")
        before = subprocess.run(["git", "rev-parse", "demo_mabujang"], cwd=origin, capture_output=True, text=True).stdout
        c.post(f"/api/jobs/{jb}/approve")
        wait(c, f"/api/jobs/{jb}", lambda d: d["status"] == "conflict")
        wd = Path(c.get(f"/api/repos/{rid}").json()["workdir"])
        assert not (wd / ".git" / "MERGE_HEAD").exists()
        assert subprocess.run(["git", "status", "--porcelain"], cwd=wd, capture_output=True, text=True).stdout == ""
        assert subprocess.run(["git", "rev-parse", "demo_mabujang"], cwd=origin, capture_output=True, text=True).stdout == before
        c.post(f"/api/jobs/{jb}/reject")
        wait(c, f"/api/jobs/{jb}", lambda d: d["status"] == "rejected")

        # 취소
        jid2 = c.post(f"/api/repos/{rid}/chat", json={"message": "버튼 색 바꿔줘"}).json()["job_id"]
        wait(c, f"/api/jobs/{jid2}", lambda d: d["status"] == "awaiting_approval")
        c.post(f"/api/jobs/{jid2}/reject")
        wait(c, f"/api/jobs/{jid2}", lambda d: d["status"] == "rejected")

        # 개발자 호출 / 변경 없음
        jid3 = c.post(f"/api/repos/{rid}/chat", json={"message": "결제 바꿔줘 개발자 필요"}).json()["job_id"]
        dev = wait(c, f"/api/jobs/{jid3}", lambda d: d["status"] == "needs_developer")
        assert dev["dev_note"] == "결제 모듈 수정 필요"                # 세부 사항은 작업 상세에
        chat = [m["content"] for m in c.get(f"/api/repos/{rid}/messages").json() if m["job_id"] == jid3]
        assert not any("결제 모듈 수정 필요" in m for m in chat)        # 채팅에는 넣지 않음
        jid4 = c.post(f"/api/repos/{rid}/chat", json={"message": "확인만 해줘"}).json()["job_id"]
        wait(c, f"/api/jobs/{jid4}", lambda d: d["status"] == "no_change")
        assert c.get(f"/api/jobs/{jid4}").json()["mode"] == "complex"

        # 질문 모드: 코드 수정·브랜치 없이 저렴한 모델로 답변
        jid5 = c.post(f"/api/repos/{rid}/chat", json={"message": "README는 어디 있어?"}).json()["job_id"]
        q = wait(c, f"/api/jobs/{jid5}", lambda d: d["status"] == "answered")
        assert q["mode"] == "question" and q["model"] == "claude-haiku-5-5" and q["cost_usd"] > 0
        assert not q["branch"] and "README.md" in q["report"]["summary_for_owner"]   # 무료 코드 검색 결과 사용
        jid6 = c.post(f"/api/repos/{rid}/chat", json={"message": "모르는 건 뭐야?"}).json()["job_id"]
        wait(c, f"/api/jobs/{jid6}", lambda d: d["status"] == "answered")

        # 답변 속 폴더 → [폴더 열기] 버튼 (작업 폴더 밖 경로는 버튼을 만들지 않음)
        jid7 = c.post(f"/api/repos/{rid}/chat", json={"message": "영상은 어디 둬?"}).json()["job_id"]
        wait(c, f"/api/jobs/{jid7}", lambda d: d["status"] == "answered")
        ans = [m["content"] for m in c.get(f"/api/repos/{rid}/messages").json() if m["job_id"] == jid7 and m["role"] == "mabu"][0]
        assert "[[open:storage/about videos]]" in ans and "outside" not in ans
        from mabujang import actions
        opened = []
        actions.open_in_explorer = opened.append        # 테스트에서는 탐색기를 실제로 열지 않음
        r = c.post(f"/api/repos/{rid}/open-folder", json={"path": "storage/about videos"}).json()
        assert r["ok"] and r["created"] and Path(r["path"]).is_dir() and opened
        for bad in ["../outside", ".git", "", ".env"]:
            assert c.post(f"/api/repos/{rid}/open-folder", json={"path": bad}).status_code == 400

        # 데이터 파일 탭: 환경 구성 뒤 자동 분석된 목록 → 작업 폴더 안은 열기, 밖(없는 폴더)은 열 수 없음
        ds = c.get(f"/api/repos/{rid}").json()["data_dirs"]
        assert [d["path"] for d in ds] == ["storage/about videos", "../outside-data"]
        assert ds[0]["inside"] and ds[0]["openable"] and not ds[1]["inside"] and not ds[1]["openable"]
        assert c.post(f"/api/repos/{rid}/data-dirs/0/open").json()["ok"]
        assert c.post(f"/api/repos/{rid}/data-dirs/1/open").status_code == 400
        assert c.post(f"/api/repos/{rid}/data-dirs/9/open").status_code == 404
        c.post(f"/api/repos/{rid}/data-dirs/analyze")
        wait(c, f"/api/repos/{rid}", lambda d: d["data_status"] == "done")

        # 직접 등록 / 삭제: 직접 등록한 것은 다시 분석해도 유지, 삭제한 분석 결과는 다시 나오지 않음
        url = f"/api/repos/{rid}/data-dirs"
        ds = c.post(url, json={"path": "uploads", "desc": "첨부파일"}).json()["data_dirs"]
        assert ds[0]["path"] == "uploads" and ds[0]["manual"] and ds[0]["openable"]
        for bad in ["uploads", "", ".git", "${X:y}"]:
            assert c.post(url, json={"path": bad}).status_code == 400
        ds = c.delete(f"{url}/2").json()["data_dirs"]                 # ../outside-data (분석 결과) 삭제
        assert [d["path"] for d in ds] == ["uploads", "storage/about videos"]
        c.post(f"{url}/analyze")
        time.sleep(0.5)
        ds = wait(c, f"/api/repos/{rid}", lambda d: d["data_status"] == "done")["data_dirs"]
        assert [d["path"] for d in ds] == ["uploads", "storage/about videos"]
        assert [d["path"] for d in c.delete(f"{url}/0").json()["data_dirs"]] == ["storage/about videos"]
        assert c.delete(f"{url}/5").status_code == 404

        msgs = c.get(f"/api/repos/{rid}/messages").json()
        roles = {m["role"] for m in msgs}
        assert {"owner", "mabu", "progress", "system"} <= roles
        assert any("배포 완료" in m["content"] for m in msgs)
