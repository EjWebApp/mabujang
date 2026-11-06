"""외부 알림 — 웹훅(Slack·Discord 호환) / 개발자 호출(노션 기록 + 이메일)."""
import json
import logging
import os
import re
import smtplib
import threading
import time
import urllib.error
import urllib.request
from email.message import EmailMessage

from . import db
from .config import settings

log = logging.getLogger("mabujang.notify")

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
NOTION_TEXT_MAX = 1900       # 노션 제한은 rich text 하나당 2,000자
NOTE_MAX = 20000             # 개발자 전달 사항 최대 길이 (끝부분 유지)

_data_sources: dict[str, str] = {}   # 데이터베이스 ID -> 데이터 소스 ID
_schemas: dict[str, dict] = {}       # 데이터 소스 ID -> 열 정의


class NotionError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def send(text: str):
    if not settings.notify_webhook:
        log.info("[알림] %s", text)
        return
    body = json.dumps({"text": text, "content": text}).encode()
    req = urllib.request.Request(settings.notify_webhook, body, {"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # 알림 실패가 작업을 멈추면 안 됨
        log.warning("웹훅 전송 실패: %s", e)


# ---------- 비밀값 가리기 (메일·노션은 외부로 나가는 내용) ----------
def redact(text: str, repo: dict | None = None) -> str:
    secrets = list(db.parse_env((repo or {}).get("env_vars", "")).values())
    secrets += [os.getenv("ANTHROPIC_API_KEY", ""), settings.notion_token, settings.smtp_password, settings.admin_password]
    for s in sorted({s for s in secrets if len(s) >= 4}, key=len, reverse=True):
        text = text.replace(s, "***")
    return re.sub(r"(https?://)[^/\s@]+@", r"\1***@", text)   # Git 주소에 든 토큰


# ---------- 노션 ----------
def _notion(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {settings.notion_token}", "Notion-Version": NOTION_VERSION,
               "Content-Type": "application/json"}
    for attempt in range(3):
        req = urllib.request.Request(NOTION_API + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 529) and attempt < 2:
                after = e.headers.get("Retry-After") or ""
                time.sleep(min(int(after), 10) if after.isdigit() else 2)
                continue
            try:
                detail = json.load(e).get("message", "")
            except Exception:
                detail = ""
            raise NotionError(e.code, f"노션 응답 {e.code} {detail}".strip()) from None
    raise RuntimeError("노션 요청 실패")


def _database_id() -> str:
    """ID나 데이터베이스 링크 어느 쪽을 넣어도 되게 32자리 ID를 뽑는다. 링크의 ?v=는 보기 ID이므로 제외한다."""
    m = re.findall(r"[0-9a-fA-F]{32}", settings.notion_database.split("?")[0].replace("-", ""))
    if not m:
        raise RuntimeError("MABU_NOTION_DATABASE에서 데이터베이스 ID를 찾지 못했습니다")
    return m[-1]


def _data_source() -> tuple[str, dict]:
    db_id = _database_id()
    if db_id not in _data_sources:
        sources = _notion("GET", f"/databases/{db_id}").get("data_sources") or []
        if not sources:
            raise RuntimeError("데이터베이스에 데이터 소스가 없습니다")
        _data_sources[db_id] = sources[0]["id"]
    ds = _data_sources[db_id]
    if ds not in _schemas:
        _schemas[ds] = _notion("GET", f"/data_sources/{ds}").get("properties") or {}
    return ds, _schemas[ds]


def _rich(text: str) -> list[dict]:
    chunks = [text[i:i + NOTION_TEXT_MAX] for i in range(0, len(text), NOTION_TEXT_MAX)][:100]
    return [{"type": "text", "text": {"content": c}} for c in chunks]


def _block(kind: str, text: str, **extra) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": _rich(text), **extra}}


def _properties(schema: dict, title: str, call: dict) -> dict:
    """제목 열만 있어도 동작한다. 나머지는 같은 이름·맞는 형식의 열이 있을 때만 채운다."""
    title_col = next((k for k, v in schema.items() if v.get("type") == "title"), None)
    if not title_col:
        raise RuntimeError("데이터베이스에 제목 열이 없습니다")
    props = {title_col: {"title": _rich(title[:200])}}

    def put(name: str, value):
        col = schema.get(name)
        if not col or value in (None, ""):
            return
        kind = col.get("type")
        if kind == "rich_text":
            props[name] = {"rich_text": _rich(str(value))}
        elif kind == "select":
            props[name] = {"select": {"name": str(value)[:100]}}
        elif kind == "number" and isinstance(value, int):
            props[name] = {"number": value}
        elif kind == "status" and value in [o.get("name") for o in col.get("status", {}).get("options", [])]:
            props[name] = {"status": {"name": value}}

    put("상태", "접수")
    put("저장소", call["repo"])
    put("작업 번호", call["job_id"])
    put("브랜치", call["branch"])
    return props


def _send_notion(title: str, call: dict) -> str:
    ds, schema = _data_source()
    children = [_block("heading_2", "요약"), *[_block("paragraph", x) for x in call["facts"]],
                _block("heading_2", "요청 내용"), _block("paragraph", call["request"] or "(없음)"),
                _block("heading_2", "호출 이유"), _block("paragraph", call["reason"] or "(없음)"),
                _block("heading_2", "개발자 전달 사항"), _block("code", call["detail"] or "(없음)", language="plain text")]
    page = _notion("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": ds},
                                      "properties": _properties(schema, title, call), "children": children})
    return page.get("url", "")


# ---------- 이메일 ----------
def _send_mail(title: str, call: dict, notion_url: str):
    msg = EmailMessage()
    msg["Subject"] = " ".join(f"[마부장님] 개발자 호출 - {title}".split())[:200]
    msg["From"] = settings.mail_from or settings.smtp_user
    msg["To"] = settings.mail_to
    msg.set_content("\n".join([
        "마부장님이 개발자를 호출했습니다.", "", *call["facts"],
        *([f"노션: {notion_url}"] if notion_url else []),
        "", "[요청 내용]", call["request"] or "(없음)",
        "", "[호출 이유]", call["reason"] or "(없음)",
        "", "[개발자 전달 사항]", call["detail"] or "(없음)", ""]))
    host, port = settings.smtp_host, settings.smtp_port
    password = settings.smtp_password.replace(" ", "") if "gmail" in host else settings.smtp_password   # Gmail 앱 비밀번호는 공백 없이
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20) as s:
            s.login(settings.smtp_user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            s.login(settings.smtp_user, password)
            s.send_message(msg)


# ---------- 개발자 호출 ----------
def call_developer(repo: dict, job_id: int | None, title: str, reason: str = "", detail: str = "",
                   branch: str = "", request: str = "", wait: bool = False):
    """세부 사항을 노션·메일로 보내고 결과를 채팅에 알린다. 외부 호출이라 작업을 막지 않도록 별도 스레드에서 처리한다."""
    detail = redact(detail, repo)[-NOTE_MAX:]
    call = {
        "repo": repo["name"], "job_id": job_id, "branch": branch, "reason": redact(reason, repo)[-3000:],
        "request": redact(request, repo)[-3000:], "detail": detail,
        "facts": [f"저장소: {repo['name']} ({redact(repo.get('git_url', ''))})", f"기준 브랜치: {repo.get('base_branch', '')}",
                  *([f"작업 번호: #{job_id}"] if job_id else []), *([f"작업 브랜치: {branch} (보존됨)"] if branch else [])],
    }
    full = f"{repo['name']} #{job_id}: {title}" if job_id else f"{repo['name']}: {title}"
    if job_id:
        db.update("jobs", job_id, dev_note=detail)
    t = threading.Thread(target=_deliver, args=(repo["id"], job_id, full, call), daemon=True)
    t.start()
    if wait:
        t.join()


def _deliver(repo_id: int, job_id: int | None, title: str, call: dict):
    done, errors, url = [], [], ""
    if settings.notion_token and settings.notion_database:
        try:
            url = _send_notion(title, call)
            done.append("노션")
            if job_id and url:
                db.update("jobs", job_id, notion_url=url)
        except Exception as e:   # 노션이 실패해도 메일은 보낸다
            log.warning("노션 기록 실패: %s", e)
            errors.append(f"노션: {redact(str(e))}")
    if settings.smtp_user and settings.smtp_password and settings.mail_to:
        try:
            _send_mail(title, call, url)
            done.append("메일")
        except Exception as e:
            log.warning("메일 전송 실패: %s", e)
            errors.append(f"메일: {redact(str(e))}")
    send(f"🙋 [마부장님] 개발자 호출 — {title}\n{call['reason'][:500]}" + (f"\n{url}" if url else ""))

    if done:
        text = f"📨 개발자에게 알렸습니다 ({'·'.join(done)})." + (f"\n{url}" if url else "")
        if errors:
            text += "\n일부 실패 — " + " / ".join(errors)
    elif errors:
        text = "⚠️ 개발자에게 알리지 못했습니다. [설정]의 알림 값을 확인해 주세요.\n" + "\n".join(errors)
    else:
        text = "⚠️ 개발자 알림(노션·메일)이 설정되지 않아 화면에만 표시했습니다."
    db.add_message(repo_id, "system", text, job_id)
