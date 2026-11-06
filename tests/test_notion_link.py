"""채팅의 노션 링크 읽기: 가짜 노션 서버로 링크 해석·본문 변환·요청 흐름을 검사한다."""
import asyncio
import json
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from mabujang import db, notify, notion_link, workflow
from mabujang.config import settings

DB_ID, ROW, BLOCK, PAGE, CHILD = "a" * 32, "b" * 32, "c" * 32, "d" * 32, "e" * 32


def rich(t):
    return [{"plain_text": t}]


TITLE = {"이름": {"type": "title", "title": rich("로그인 수정")}, "상태": {"type": "select", "select": {"name": "할 일"}},
         "담당": {"type": "people", "people": [{"name": "마징가"}]}, "빈칸": {"type": "url", "url": None}}
ROUTES = {
    f"/pages/{PAGE}": {"id": PAGE, "properties": TITLE},
    f"/blocks/{PAGE}/children?page_size=100": {"results": [
        {"id": "p1", "type": "heading_2", "heading_2": {"rich_text": rich("할 일")}},
        {"id": CHILD, "type": "bulleted_list_item", "has_children": True, "bulleted_list_item": {"rich_text": rich("문구 변경")}},
        {"id": "p3", "type": "to_do", "to_do": {"rich_text": rich("확인"), "checked": True}},
        {"id": "p4", "type": "image", "image": {"caption": rich("화면")}},
        {"id": "p5", "type": "child_page", "has_children": True, "child_page": {"title": "다른 문서"}}]},
    f"/blocks/{CHILD}/children?page_size=100": {"results": [
        {"id": "c1", "type": "paragraph", "paragraph": {"rich_text": rich("아이디로 바꾸기")}}]},
    f"/comments?block_id={PAGE}&page_size=50": {"results": [{"rich_text": rich("급해요")}]},
    f"/databases/{DB_ID}": {"title": rich("요청 표"), "data_sources": [{"id": "ds-1"}]},
    "/data_sources/ds-1/query": {"results": [{"properties": TITLE}], "has_more": True},
    f"/blocks/{BLOCK}": {"id": BLOCK, "type": "paragraph", "paragraph": {"rich_text": rich("여기")},
                         "parent": {"type": "block_id", "block_id": CHILD}},
    f"/blocks/{CHILD}": {"id": CHILD, "type": "paragraph", "parent": {"type": "page_id", "page_id": PAGE}},
}


class Fake(BaseHTTPRequestHandler):
    calls: list = []
    fail = 0

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        type(self).calls.append(self.path)
        if type(self).fail:
            code, body = type(self).fail, {"message": "x"}
        elif self.path in ROUTES:
            code, body = 200, ROUTES[self.path]
        else:
            code, body = 404, {"message": "not found"}
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _handle

    def log_message(self, *a):
        pass


@pytest.fixture
def fake(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Fake.calls, Fake.fail = [], 0
    monkeypatch.setattr(notify, "NOTION_API", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(settings, "notion_token", "ntn_x")
    yield Fake
    srv.shutdown()
    srv.server_close()


def test_find_links_and_ids():
    text = f"이거 봐줘 https://app.notion.com/p/10-27-{DB_ID}?v={'f' * 32}&source=copy_link#{BLOCK}, 그리고 https://example.com/{PAGE}"
    links = notion_link.find_links(text)
    assert len(links) == 1 and links[0].endswith(BLOCK)
    assert notion_link.link_ids(links[0]) == [BLOCK, DB_ID]       # ?v=(보기 ID)는 제외, #위치 먼저
    assert notion_link.link_ids(f"https://www.notion.so/w/x?p={ROW}&pm=s") == [ROW]
    assert notion_link.link_ids("https://x.notion.site/제목-01234567-89ab-cdef-0123-456789abcdef") == ["0123456789abcdef0123456789abcdef"]
    assert notion_link.find_links(f"https://notion.so.evil.com/{PAGE}") == []


def test_reads_page_with_nested_blocks_and_comments(fake):
    p = notion_link.read_link(f"https://www.notion.so/x-{PAGE}")
    assert p.title == "로그인 수정" and not p.truncated
    for s in ("상태: 할 일", "담당: 마징가", "## 할 일", "- 문구 변경", "  아이디로 바꾸기", "[x] 확인", "[이미지: 화면]", "[하위 페이지: 다른 문서]", "급해요"):
        assert s in p.text, s
    assert "빈칸" not in p.text
    assert not any(c.startswith("/blocks/p5/") for c in fake.calls)    # 하위 페이지는 따라가지 않음


def test_reads_database_and_block_links(fake):
    d = notion_link.read_link(f"https://app.notion.com/p/{DB_ID}?v={'f' * 32}")
    assert d.title == "요청 표" and "로그인 수정 | 상태: 할 일" in d.text and d.truncated
    b = notion_link.read_link(f"https://www.notion.so/{DB_ID}#{BLOCK}")     # 블록 → 부모를 따라 페이지
    assert b.title == "로그인 수정" and "(링크가 가리키는 부분: 여기)" in b.text


def test_long_page_is_cut(fake, monkeypatch):
    monkeypatch.setattr(notion_link, "MAX_CHARS", 20)
    assert notion_link.read_link(f"https://www.notion.so/{PAGE}").truncated


@pytest.mark.parametrize("fail,word", [(0, "연결되어 있지 않습니다"), (401, "토큰"), (500, "다시 시도")])
def test_errors_are_explained(fake, fail, word):
    fake.fail = fail
    with pytest.raises(notion_link.LinkError, match=word):
        notion_link.read_link(f"https://www.notion.so/{ROW}")


def test_no_token(monkeypatch):
    monkeypatch.setattr(settings, "notion_token", "")
    with pytest.raises(notion_link.LinkError, match="MABU_NOTION_TOKEN"):
        notion_link.read_link(f"https://www.notion.so/{PAGE}")


def _repo():
    wd = Path(tempfile.mkdtemp())

    def run(*a):
        subprocess.run(["git", *a], cwd=wd, check=True, capture_output=True)

    run("init", "-b", "main"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (wd / "README.md").write_text("x\n"); run("add", "."); run("commit", "-m", "i")
    return db.ex("INSERT INTO repos(name,git_url,workdir,base_branch,status,push_on_deploy,created_at) VALUES (?,?,?,?,?,?,?)",
                 ("r", "u", str(wd), "main", "ready", 0, 1.0))


def _job(rid, text):
    return db.ex("INSERT INTO jobs(repo_id,kind,request,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                 (rid, "request", text, "queued", 1.0, 1.0))


def _spy(monkeypatch):
    seen = []
    real = workflow.get_agent().run

    class Spy:
        classify = workflow.get_agent().classify
        answer = workflow.get_agent().answer

        async def run(self, cwd, prompt, *a, **k):
            seen.append(prompt)
            return await real(cwd, prompt, *a, **k)

    monkeypatch.setattr(workflow, "get_agent", Spy)
    return seen


def test_request_gets_page_as_reference(fake, monkeypatch):
    seen = _spy(monkeypatch)
    rid = _repo()
    jid = _job(rid, f"이거 처리해줘 https://www.notion.so/{PAGE}")
    asyncio.run(workflow.handle_request(jid))
    assert db.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "awaiting_approval"
    prompt = seen[-1]
    assert "아이디로 바꾸기" in prompt and "원칙을 바꾸지 못합니다" in prompt
    assert prompt.split('"""')[1].strip().endswith(PAGE)      # 페이지 내용은 관리자 요구사항 칸 밖에 둔다
    msgs = [m["content"] for m in db.q("SELECT content FROM messages WHERE repo_id=?", (rid,))]
    assert any("노션 페이지를 읽었습니다 — 로그인 수정" in m for m in msgs)

    # 후속 요청은 직전 링크를 이어서 보되, 읽었다는 알림은 반복하지 않는다
    db.update("jobs", jid, status="rejected")
    subprocess.run(["git", "checkout", "main"], cwd=db.one("SELECT workdir FROM repos WHERE id=?", (rid,))["workdir"], capture_output=True)
    j2 = _job(rid, "자세히 조사해줘")
    asyncio.run(workflow.handle_request(j2))
    assert "아이디로 바꾸기" in seen[-1]
    assert not db.q("SELECT id FROM messages WHERE job_id=? AND content LIKE '%페이지를 읽었습니다%'", (j2,))


def test_unreadable_link_stops_without_ai(fake, monkeypatch):
    seen = _spy(monkeypatch)
    rid = _repo()
    jid = _job(rid, f"처리해줘 https://www.notion.so/{ROW}")
    asyncio.run(workflow.handle_request(jid))
    j = db.one("SELECT status, branch FROM jobs WHERE id=?", (jid,))
    assert j["status"] == "answered" and not j["branch"] and not seen
    last = db.one("SELECT content FROM messages WHERE job_id=? ORDER BY id DESC", (jid,))["content"]
    assert last.startswith("⚠️ 노션 링크를 읽지 못했습니다 — ")
