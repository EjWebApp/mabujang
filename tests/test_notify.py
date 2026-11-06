"""개발자 호출: 가짜 노션 서버·가짜 메일 서버로 실제로 보내는 내용을 검사한다."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from mabujang import db, notify
from mabujang.config import settings

DB_ID = "0123456789abcdef0123456789abcdef"
SCHEMA = {"이름": {"type": "title"}, "상태": {"type": "select"}, "저장소": {"type": "rich_text"},
          "작업 번호": {"type": "number"}, "브랜치": {"type": "rich_text"}, "기타": {"type": "url"}}


class FakeNotion(BaseHTTPRequestHandler):
    calls: list = []
    fail = 0

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        type(self).calls.append((self.command, self.path, dict(self.headers), body))
        if type(self).fail:
            return self._send(type(self).fail, {"message": "unauthorized"})
        if self.path == f"/databases/{DB_ID}":
            return self._send(200, {"data_sources": [{"id": "ds-1"}]})
        if self.path == "/data_sources/ds-1":
            return self._send(200, {"properties": SCHEMA})
        if self.path == "/pages":
            return self._send(200, {"url": "https://www.notion.so/page-1"})
        return self._send(404, {"message": "not found"})

    do_GET = do_POST = _handle

    def log_message(self, *a):
        pass


class FakeSMTP:
    sent: list = []
    logins: list = []
    fail = False

    def __init__(self, host, port, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, user, pw):
        FakeSMTP.logins.append((user, pw))
        if FakeSMTP.fail:
            raise OSError("smtp down")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def env(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), FakeNotion)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FakeNotion.calls, FakeNotion.fail = [], 0
    FakeSMTP.sent, FakeSMTP.logins, FakeSMTP.fail = [], [], False
    monkeypatch.setattr(notify, "NOTION_API", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(notify, "_data_sources", {})
    monkeypatch.setattr(notify, "_schemas", {})
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    for k, v in dict(notion_token="ntn_token1234", notion_database=f"https://www.notion.so/w/{DB_ID}?v=1", smtp_host="smtp.gmail.com",
                     smtp_port=587, smtp_user="me@example.com", smtp_password="abcd efgh ijkl", mail_to="dev@example.com").items():
        monkeypatch.setattr(settings, k, v)
    now = 1.0
    rid = db.ex("INSERT INTO repos(name,git_url,workdir,env_vars,created_at) VALUES (?,?,?,?,?)",
                ("벡터", "https://user:ghp_secret@github.com/a/b.git", "x", "DB_PASS=s3cret-pw", now))
    jid = db.ex("INSERT INTO jobs(repo_id,request,status,created_at,updated_at) VALUES (?,?,?,?,?)", (rid, "문구 바꿔줘", "failed", now, now))
    yield db.one("SELECT * FROM repos WHERE id=?", (rid,)), jid
    srv.shutdown()
    srv.server_close()


def _msgs(rid):
    return [m["content"] for m in db.q("SELECT content FROM messages WHERE repo_id=?", (rid,))]


def test_notion_and_mail_get_details_with_secrets_hidden(env):
    repo, jid = env
    long_log = "$ git merge\nCONFLICT (content) password=s3cret-pw\n" + "x" * 5000
    notify.call_developer(repo, jid, "배포 실패", "서비스는 바뀌지 않았습니다", long_log, "mabu/job-8", "문구 바꿔줘", wait=True)

    # 노션: 현재 API 규격 (데이터베이스 → 데이터 소스 → 페이지)
    method, path, headers, body = FakeNotion.calls[-1]
    assert (method, path) == ("POST", "/pages")
    assert [c[1] for c in FakeNotion.calls] == [f"/databases/{DB_ID}", "/data_sources/ds-1", "/pages"]
    assert headers["Notion-Version"] == notify.NOTION_VERSION and headers["Authorization"] == "Bearer ntn_token1234"
    assert body["parent"] == {"type": "data_source_id", "data_source_id": "ds-1"}
    p = body["properties"]
    assert p["이름"]["title"][0]["text"]["content"] == f"벡터 #{jid}: 배포 실패"
    assert p["상태"] == {"select": {"name": "접수"}} and p["작업 번호"] == {"number": jid} and "기타" not in p
    code = next(b for b in body["children"] if b["type"] == "code")
    assert all(len(t["text"]["content"]) <= 2000 for t in code["code"]["rich_text"]) and len(code["code"]["rich_text"]) > 1
    sent = json.dumps(body, ensure_ascii=False)
    assert "CONFLICT" in sent and "s3cret-pw" not in sent and "ghp_secret" not in sent

    # 메일: 노션 링크 + 개발자 전달 사항, 비밀값 가림, Gmail 앱 비밀번호 공백 제거
    msg = FakeSMTP.sent[-1]
    text = msg.get_content()
    assert msg["To"] == "dev@example.com" and "개발자 호출" in msg["Subject"]
    assert "https://www.notion.so/page-1" in text and "[개발자 전달 사항]" in text and "CONFLICT" in text
    assert "s3cret-pw" not in text and "ghp_secret" not in text
    assert FakeSMTP.logins[-1] == ("me@example.com", "abcdefghijkl")

    # 채팅에는 결과만, 작업 상세에는 전달 사항과 링크
    chat = _msgs(repo["id"])
    assert any("개발자에게 알렸습니다 (노션·메일)" in m for m in chat) and not any("CONFLICT" in m for m in chat)
    j = db.one("SELECT dev_note, notion_url FROM jobs WHERE id=?", (jid,))
    assert "CONFLICT" in j["dev_note"] and "s3cret-pw" not in j["dev_note"] and j["notion_url"].endswith("page-1")


@pytest.mark.parametrize("value", [
    f"https://app.notion.com/p/{DB_ID}?v=ffffffffffffffffffffffffffffffff",   # 주소창 링크 (보기 ID 포함)
    f"https://www.notion.so/w/개발자-호출-{DB_ID}?v=ffffffffffffffffffffffffffffffff&pvs=4",
    DB_ID,
    "01234567-89ab-cdef-0123-456789abcdef",
])
def test_database_id_from_link(monkeypatch, value):
    monkeypatch.setattr(settings, "notion_database", value)
    assert notify._database_id() == DB_ID


def test_mail_still_sent_when_notion_fails(env):
    repo, jid = env
    FakeNotion.fail = 401
    notify.call_developer(repo, jid, "작업 실패", "이유", "로그", wait=True)
    assert FakeSMTP.sent and "노션:" not in FakeSMTP.sent[-1].get_content()
    chat = " ".join(_msgs(repo["id"]))
    assert "개발자에게 알렸습니다 (메일)" in chat and "노션 응답 401" in chat


def test_failures_and_missing_config_are_reported(env, monkeypatch):
    repo, jid = env
    FakeNotion.fail, FakeSMTP.fail = 401, True
    notify.call_developer(repo, jid, "작업 실패", wait=True)
    assert any(m.startswith("⚠️ 개발자에게 알리지 못했습니다") for m in _msgs(repo["id"]))

    for k in ("notion_token", "smtp_user"):
        monkeypatch.setattr(settings, k, "")
    n = len(FakeNotion.calls)
    notify.call_developer(repo, jid, "작업 실패", wait=True)
    assert len(FakeNotion.calls) == n
    assert any("설정되지 않아 화면에만" in m for m in _msgs(repo["id"]))
