"""마부장님 — AI 유지보수 웹서비스 (FastAPI)."""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import actions, db, gitops, notify, readonly, workflow
from .config import settings

logging.basicConfig(level=logging.INFO)
@asynccontextmanager
async def lifespan(_app):
    yield
    actions.stop_all()   # 마부장님 종료 시 버튼으로 띄운 서버도 함께 종료


app = FastAPI(title="마부장님", lifespan=lifespan)
STATIC = Path(__file__).parent / "static"
_tasks: set[asyncio.Task] = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _token() -> str:
    return hashlib.sha256(("mabu:" + settings.admin_password).encode()).hexdigest()


def auth(authorization: str = Header(default="")):
    if settings.admin_password and not hmac.compare_digest(authorization.removeprefix("Bearer "), _token()):
        raise HTTPException(401, "로그인이 필요합니다")


class Login(BaseModel):
    password: str


class RepoIn(BaseModel):
    name: str
    git_url: str
    base_branch: str = "main"
    test_cmd: str = ""
    deploy_cmd: str = ""
    push_on_deploy: bool = True
    auto_deploy: bool = False
    run_cmd: str = ""
    run_url: str = ""
    pre_run_cmd: str = ""
    wait_ports: str = ""
    env_vars: str = ""
    model: str = ""


class RepoPatch(BaseModel):
    test_cmd: str | None = None
    deploy_cmd: str | None = None
    push_on_deploy: bool | None = None
    auto_deploy: bool | None = None
    run_cmd: str | None = None
    run_url: str | None = None
    pre_run_cmd: str | None = None
    wait_ports: str | None = None
    model: str | None = None
    env_set: dict[str, str] | None = None     # 추가/변경할 비밀값
    env_remove: list[str] | None = None       # 삭제할 키


class OpenIn(BaseModel):
    path: str


class ChatIn(BaseModel):
    message: str
    attach_log: bool = False


def public_repo(r: dict) -> dict:
    """비밀값은 키 이름만 노출."""
    r = dict(r)
    r["env_keys"] = list(db.parse_env(r.pop("env_vars", "")))
    r.pop("last_action_log", None)
    r.pop("session_id", None)
    cost = db.one("SELECT COALESCE(SUM(cost_usd),0) c, COUNT(*) n FROM jobs WHERE repo_id=?", (r["id"],))
    r["total_cost_usd"], r["job_count"] = round(cost["c"], 4), cost["n"]
    r["server"] = actions.status(r["id"])
    r["data_dirs"] = data_dirs_view(r)
    return r


def data_dirs_view(repo: dict) -> list[dict]:
    """데이터 폴더 목록 + 실제 위치·있음 여부 (화면 표시용)."""
    try:
        items = json.loads(repo.get("data_dirs") or "[]")
    except json.JSONDecodeError:
        items = []
    out = []
    for d in items:
        p, inside = workflow.resolve_data_dir(repo, d.get("path", ""))
        out.append({**d, "abs": str(p) if p else "", "inside": inside, "exists": bool(p and p.is_dir()),
                    # 작업 폴더 안은 없으면 만들어서 열고, 밖은 이미 있을 때만 연다
                    "openable": bool(p and (inside or p.is_dir()))})
    return out


def _open(p: Path, may_create: bool) -> dict:
    created = not p.exists()
    if created and not may_create:
        raise HTTPException(400, f"폴더가 없습니다: {p}")
    p.mkdir(parents=True, exist_ok=True)
    try:
        actions.open_in_explorer(p)
    except Exception as e:
        return {"ok": False, "created": created, "path": str(p), "message": f"폴더를 열지 못했습니다: {e}"}
    return {"ok": True, "created": created, "path": str(p)}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def config():
    return {"auth_required": bool(settings.admin_password), "agent": settings.agent}


@app.post("/api/login")
def login(body: Login):
    if not settings.admin_password or hmac.compare_digest(body.password, settings.admin_password):
        return {"token": _token()}
    raise HTTPException(401, "비밀번호가 틀렸습니다")


@app.get("/api/repos", dependencies=[Depends(auth)])
def list_repos():
    return [public_repo(r) for r in db.q("SELECT * FROM repos ORDER BY id DESC")]


@app.post("/api/repos", dependencies=[Depends(auth)])
async def create_repo(body: RepoIn):
    rid = db.ex(
        "INSERT INTO repos(name,git_url,base_branch,workdir,test_cmd,deploy_cmd,push_on_deploy,auto_deploy,"
        "run_cmd,run_url,pre_run_cmd,wait_ports,env_vars,model,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (body.name, body.git_url, body.base_branch, "", body.test_cmd, body.deploy_cmd,
         int(body.push_on_deploy), int(body.auto_deploy), body.run_cmd, body.run_url, body.pre_run_cmd, body.wait_ports, body.env_vars,
         body.model, time.time()),
    )
    db.update("repos", rid, workdir=str(workflow.workdir_for(body.name, rid)))
    spawn(workflow.register_and_setup(rid))
    return get_repo(rid)


def _repo(rid: int) -> dict:
    r = db.one("SELECT * FROM repos WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404)
    return r


@app.get("/api/repos/{rid}", dependencies=[Depends(auth)])
def get_repo(rid: int):
    return public_repo(_repo(rid))


@app.patch("/api/repos/{rid}", dependencies=[Depends(auth)])
def patch_repo(rid: int, body: RepoPatch):
    data = body.model_dump()
    env_set, env_remove = data.pop("env_set"), data.pop("env_remove")
    fields = {k: (int(v) if isinstance(v, bool) else v) for k, v in data.items() if v is not None}
    if env_set or env_remove:
        env = db.parse_env(_repo(rid)["env_vars"])
        for k in env_remove or []:
            env.pop(k, None)
        for k, v in (env_set or {}).items():
            if k.strip():
                env[k.strip()] = v
        fields["env_vars"] = "\n".join(f"{k}={v}" for k, v in env.items())
    if fields:
        db.update("repos", rid, **fields)
    return get_repo(rid)


# ---------- AI 없이 실행하는 버튼 작업 (비용 0원) ----------
@app.post("/api/repos/{rid}/actions/{name}", dependencies=[Depends(auth)])
async def run_action(rid: int, name: str):
    repo = _repo(rid)
    if repo["status"] != "ready":
        raise HTTPException(409, "아직 작업 환경을 준비 중입니다.")
    if name == "stop":
        ok, msg = await actions.stop(repo)
        return {"ok": ok, "message": msg}
    fn = {"build": actions.build, "start": actions.start}.get(name)
    if not fn:
        raise HTTPException(404)
    if workflow.busy(rid):
        raise HTTPException(409, "마부장님이 작업 중입니다. 끝난 뒤 다시 눌러 주세요.")
    spawn(fn(repo))
    return {"ok": True}


@app.post("/api/repos/{rid}/open-folder", dependencies=[Depends(auth)])
def open_folder(rid: int, body: OpenIn):
    """마부장님이 설치된 PC의 파일 탐색기로 작업 폴더 안의 폴더를 연다 (없으면 만든다)."""
    p = readonly.safe_path(Path(_repo(rid)["workdir"]), body.path)
    if not p:
        raise HTTPException(400, "작업 폴더 안의 폴더만 열 수 있습니다")
    return _open(p.parent if p.is_file() else p, may_create=True)


@app.post("/api/repos/{rid}/data-dirs/analyze", dependencies=[Depends(auth)])
async def analyze_data(rid: int):
    if _repo(rid)["data_status"] == "analyzing":
        raise HTTPException(409, "이미 분석 중입니다")
    spawn(workflow.analyze_data_dirs(rid))
    return {"ok": True}


class DataDirIn(BaseModel):
    path: str
    desc: str = ""


@app.post("/api/repos/{rid}/data-dirs", dependencies=[Depends(auth)])
def add_data(rid: int, body: DataDirIn):
    _repo(rid)
    err = workflow.add_data_dir(rid, body.path, body.desc)
    if err:
        raise HTTPException(400, err)
    return get_repo(rid)


@app.delete("/api/repos/{rid}/data-dirs/{idx}", dependencies=[Depends(auth)])
def delete_data(rid: int, idx: int):
    _repo(rid)
    if not workflow.remove_data_dir(rid, idx):
        raise HTTPException(404, "없는 항목입니다")
    return get_repo(rid)


@app.post("/api/repos/{rid}/data-dirs/{idx}/open", dependencies=[Depends(auth)])
def open_data_dir(rid: int, idx: int):
    """분석 결과에 있는 폴더만 연다 (임의 경로는 받지 않음)."""
    dirs = data_dirs_view(_repo(rid))
    if not 0 <= idx < len(dirs) or not dirs[idx]["abs"]:
        raise HTTPException(404, "열 수 없는 폴더입니다")
    d = dirs[idx]
    return _open(Path(d["abs"]), may_create=d["inside"])


@app.post("/api/repos/{rid}/notify-test", dependencies=[Depends(auth)])
def notify_test(rid: int):
    """노션·메일 설정 확인용 시험 호출. 결과는 채팅에 표시된다."""
    notify.call_developer(_repo(rid), None, "알림 시험", "마부장님 알림 설정을 확인하는 시험 메시지입니다. 조치할 것은 없습니다.",
                          "개발자 전달 사항은 이 칸에 들어갑니다.", request="(시험)")
    return {"ok": True}


@app.get("/api/repos/{rid}/server-log", dependencies=[Depends(auth)])
def server_log(rid: int):
    return {"status": actions.status(rid), "log": actions.recent_log(rid)}


@app.get("/api/repos/{rid}/messages", dependencies=[Depends(auth)])
def messages(rid: int, after: int = 0):
    return db.q("SELECT * FROM messages WHERE repo_id=? AND id>? ORDER BY id", (rid, after))


@app.get("/api/repos/{rid}/jobs", dependencies=[Depends(auth)])
def jobs(rid: int):
    last = workflow.last_deployed(rid)
    return [{**db.job_dict(j), "can_revert": bool(last and last["id"] == j["id"])}
            for j in db.q("SELECT * FROM jobs WHERE repo_id=? ORDER BY id DESC", (rid,))]


@app.post("/api/repos/{rid}/chat", dependencies=[Depends(auth)])
async def chat(rid: int, body: ChatIn):
    repo = get_repo(rid)
    if repo["status"] not in ("ready",):
        raise HTTPException(409, "아직 작업 환경을 준비 중입니다. 잠시 후 다시 요청해 주세요.")
    text = body.message.strip()
    if not text:
        raise HTTPException(400, "내용을 입력해 주세요")
    now = time.time()
    jid = db.ex("INSERT INTO jobs(repo_id,kind,request,status,attach_log,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (rid, "request", text, "queued", int(body.attach_log), now, now))
    db.add_message(rid, "owner", text, jid)
    spawn(workflow.handle_request(jid))
    return {"job_id": jid}


def _job(jid: int, expect: str | tuple) -> dict:
    j = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    if not j:
        raise HTTPException(404)
    if j["status"] not in ((expect,) if isinstance(expect, str) else expect):
        raise HTTPException(409, f"현재 상태({j['status']})에서는 할 수 없습니다")
    return j


@app.get("/api/jobs/{jid}", dependencies=[Depends(auth)])
def get_job(jid: int):
    j = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    if not j:
        raise HTTPException(404)
    return db.job_dict(j)


@app.post("/api/jobs/{jid}/approve", dependencies=[Depends(auth)])
async def approve(jid: int):
    j = _job(jid, "awaiting_approval")
    db.add_message(j["repo_id"], "owner", f"#{jid} 배포 승인", jid)
    spawn(workflow.deploy(jid))
    return {"ok": True}


@app.get("/api/jobs/{jid}/diff", dependencies=[Depends(auth)])
def job_diff(jid: int):
    """작업 상세: 실제 변경 코드. 배포 전이면 작업 브랜치, 배포 후면 병합 커밋 기준."""
    j = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    if not j:
        raise HTTPException(404)
    repo = _repo(j["repo_id"])
    wd, base = repo["workdir"], repo["base_branch"]
    if j["status"] in ("done", "reverted", "reverting") and (j.get("merge_sha") or j["branch"]):
        sha = j.get("merge_sha") or gitops.find_merge(wd, base, j["branch"])
        rng = [f"{sha}^1", sha] if sha else None
    elif j["branch"] and gitops.run(["git", "rev-parse", "--verify", "-q", j["branch"]], wd, check=False):
        rng = [f"{base}...{j['branch']}"]
    else:
        rng = None
    if not rng:
        return {"diff": "", "note": "변경된 코드가 없거나 작업 브랜치가 정리되었습니다."}
    out = gitops.run(["git", "diff", "--no-color", *rng], wd, check=False)
    note = "" if len(out) <= 150_000 else "변경이 많아 앞부분만 표시합니다."
    return {"diff": out[:150_000], "note": note}


@app.post("/api/jobs/{jid}/preview", dependencies=[Depends(auth)])
async def preview(jid: int):
    """승인 전 미리 보기: 작업 브랜치 코드로 서버를 띄운다 (AI 비용 없음)."""
    j = _job(jid, "awaiting_approval")
    repo = _repo(j["repo_id"])
    if workflow.busy(repo["id"]):
        raise HTTPException(409, "마부장님이 작업 중입니다. 끝난 뒤 다시 눌러 주세요.")
    spawn(actions.start(repo, j))
    return {"ok": True}


@app.post("/api/jobs/{jid}/revert", dependencies=[Depends(auth)])
async def revert(jid: int):
    j = _job(jid, "done")
    last = workflow.last_deployed(j["repo_id"])
    if not last or last["id"] != jid:
        raise HTTPException(409, "가장 최근에 배포된 작업만 되돌릴 수 있습니다")
    db.add_message(j["repo_id"], "owner", f"#{jid} 되돌리기", jid)
    spawn(workflow.revert(jid))
    return {"ok": True}


@app.post("/api/jobs/{jid}/reject", dependencies=[Depends(auth)])
async def reject(jid: int):
    j = _job(jid, ("awaiting_approval", "conflict"))   # 충돌로 배포 못 한 작업도 취소 가능
    db.add_message(j["repo_id"], "owner", f"#{jid} 변경 취소", jid)
    spawn(workflow.reject(jid))
    return {"ok": True}
