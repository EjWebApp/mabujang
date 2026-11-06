"""SQLite 저장소 — 의존성 없이 표준 sqlite3 사용."""
import json
import sqlite3
import threading
import time
from pathlib import Path

from .config import settings

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    git_url TEXT NOT NULL,
    base_branch TEXT NOT NULL DEFAULT 'main',
    workdir TEXT NOT NULL,
    test_cmd TEXT DEFAULT '',
    deploy_cmd TEXT DEFAULT '',
    push_on_deploy INTEGER DEFAULT 1,
    auto_deploy INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'cloning',   -- cloning|setting_up|ready|error
    env_notes TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'request',     -- setup|request|data
    request TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',    -- queued|running|awaiting_approval|deploying|done|no_change|needs_developer|rejected|failed|reverting|reverted|answered|conflict
    branch TEXT DEFAULT '',
    report TEXT DEFAULT '',                    -- JSON
    diff_stat TEXT DEFAULT '',
    deploy_log TEXT DEFAULT '',
    cost_usd REAL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL,
    job_id INTEGER,
    role TEXT NOT NULL,                        -- owner|mabu|progress|system
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


# 기존 DB에 새 컬럼 추가 (버전 업그레이드 시 데이터 유지)
MIGRATIONS = {
    "repos": {
        "run_cmd": "TEXT DEFAULT ''",          # 서버 실행 명령 (AI 없이 버튼으로 실행)
        "run_url": "TEXT DEFAULT ''",          # 실행 후 확인할 주소 (예: http://localhost:9080)
        "env_vars": "TEXT DEFAULT ''",         # 저장소별 비밀값 KEY=VALUE (화면에는 키만 노출)
        "model": "TEXT DEFAULT ''",            # 저장소별 AI 모델 (비우면 기본값)
        "pre_run_cmd": "TEXT DEFAULT ''",      # 서버 실행 전 명령 (예: docker start my-db)
        "wait_ports": "TEXT DEFAULT ''",       # 실행 전 열려 있어야 할 포트 (예: 5432 또는 localhost:5432,6379)
        "last_action": "TEXT DEFAULT ''",      # 마지막 버튼 작업 이름
        "last_action_log": "TEXT DEFAULT ''",  # 마지막 버튼 작업 로그 (AI 분석 요청 시 첨부)
        "data_dirs": "TEXT DEFAULT ''",        # 소스 분석으로 찾은 데이터 파일 폴더 (JSON 목록)
        "data_status": "TEXT DEFAULT ''",      # 데이터 폴더 분석 상태: '' | analyzing | done | failed
        "data_hidden": "TEXT DEFAULT ''",      # 관리자가 삭제한 분석 결과 경로 (JSON 목록, 다시 분석해도 제외)
    },
    "jobs": {
        "attach_log": "INTEGER DEFAULT 0",     # 요청 시 마지막 버튼 작업 로그를 AI에 첨부
        "merge_sha": "TEXT DEFAULT ''",        # 배포 시 기준 브랜치에 만든 병합 커밋 ([되돌리기]에 사용)
        "dev_note": "TEXT DEFAULT ''",         # 개발자 전달 사항 (로그 등 세부 사항, 비밀값 가림)
        "notion_url": "TEXT DEFAULT ''",       # 개발자 호출 노션 페이지
        "mode": "TEXT DEFAULT ''",             # 자동 분류 결과: question | simple | complex
        "model": "TEXT DEFAULT ''",            # 실제 사용한 AI 모델
    },
}


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(Path(settings.data_dir) / "mabujang.db", check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in _conn.execute(f"PRAGMA table_info({table})")}
            for col, ddl in cols.items():
                if col not in have:
                    _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        _conn.commit()
    return _conn


def parse_env(text: str) -> dict[str, str]:
    env = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def reset_for_tests():
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = None


def q(sql: str, args=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, args=()) -> dict | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql: str, args=()) -> int:
    with _lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur.lastrowid


def update(table: str, row_id: int, **fields):
    if table == "jobs":
        fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    ex(f"UPDATE {table} SET {cols} WHERE id=?", (*fields.values(), row_id))


def add_message(repo_id: int, role: str, content: str, job_id: int | None = None) -> int:
    return ex(
        "INSERT INTO messages(repo_id, job_id, role, content, created_at) VALUES (?,?,?,?,?)",
        (repo_id, job_id, role, content, time.time()),
    )


def job_dict(job: dict) -> dict:
    job = dict(job)
    try:
        job["report"] = json.loads(job["report"]) if job["report"] else None
    except json.JSONDecodeError:
        job["report"] = None
    return job
