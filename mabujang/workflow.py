"""작업 흐름: 등록→환경구성 / 요구사항→작업→(승인)→배포→보고."""
import asyncio
import json
import logging
import re
import time
import traceback
from pathlib import Path

from . import actions, db, gitops, notify, notion_link, readonly
from .agent import Route, get_agent
from .config import settings
from .prompts import SETUP_PROMPT, linked_text, recent_text, request_prompt

log = logging.getLogger("mabujang.workflow")
_repo_locks: dict[int, asyncio.Lock] = {}


def _lock(repo_id: int) -> asyncio.Lock:
    return _repo_locks.setdefault(repo_id, asyncio.Lock())


def workdir_for(name: str, repo_id: int) -> Path:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-") or "repo"
    return Path(settings.workspaces_dir).resolve() / f"{repo_id}-{slug}"


def format_report(rep: dict) -> str:
    icon = {"done": "✅", "no_change": "ℹ️", "needs_developer": "🙋"}.get(rep.get("status"), "📋")
    lines = [f"{icon} **{rep.get('title', '보고')}**", "", rep.get("summary_for_owner", "")]
    if rep.get("changes"):
        lines += ["", "**변경 내용**"] + [f"- {c}" for c in rep["changes"]]
    if rep.get("tests"):
        lines += ["", f"**검증**: {rep['tests']}"]
    if rep.get("risk"):
        lines += [f"**위험도**: {dict(low='낮음', medium='보통', high='높음').get(rep['risk'], rep['risk'])}"]
    # 개발자 전달 사항은 채팅에 넣지 않는다 (메일·노션·작업 상세에서 확인)
    return "\n".join(lines)


async def _run_agent(repo: dict, job_id: int, prompt: str, model: str = ""):
    async def progress(text: str):
        db.add_message(repo["id"], "progress", text, job_id)

    env = db.parse_env(repo.get("env_vars", ""))
    return await get_agent().run(Path(repo["workdir"]), prompt, progress, resume=repo.get("session_id") or "",
                                 model=model or repo.get("model") or "", env=env)


# ---------- 요청 분류: 질문 / 간단한 수정 / 일반 작업 ----------
def _recent(repo_id: int, job_id: int) -> str:
    rows = db.q("SELECT role, content FROM messages WHERE repo_id=? AND (job_id IS NULL OR job_id<?) "
                "AND role IN ('owner','mabu') ORDER BY id DESC LIMIT 12", (repo_id, job_id))
    return recent_text(list(reversed(rows)))


async def _route(repo: dict, job: dict, recent: str, linked: str = "") -> Route:
    # 실패 로그 분석 요청은 항상 일반 작업 (코드 조사가 필요)
    if not settings.auto_route or job.get("attach_log"):
        return Route("complex")
    try:
        return await get_agent().classify(job["request"] + linked, repo.get("env_notes") or "", recent)
    except Exception:
        log.exception("route failed")
        return Route("complex")   # 분류 실패 시 기존 방식으로 안전하게 처리


async def _read_links(repo: dict, job: dict) -> str | None:
    """메시지의 노션 링크를 읽어 프롬프트에 넣을 글로 만든다. 읽지 못하면 안내하고 None (AI 비용을 쓰지 않음)."""
    links = notion_link.find_links(job["request"])
    followup = False
    if not links and not job.get("attach_log"):
        # "자세히 조사해줘" 같은 후속 요청은 직전 요청의 링크를 이어서 본다
        prev = db.one("SELECT request FROM jobs WHERE repo_id=? AND kind='request' AND id<? ORDER BY id DESC LIMIT 1",
                      (repo["id"], job["id"]))
        links, followup = notion_link.find_links(prev["request"] if prev else ""), True
    pages = []
    for url in links:
        try:
            pages.append(await asyncio.to_thread(notion_link.read_link, url))
        except notion_link.LinkError as e:
            if followup:
                continue
            title = "노션 링크를 읽지 못했습니다"
            rep = {"status": "answered", "title": title, "summary_for_owner": str(e),
                   "changes": [], "tests": "", "risk": "", "developer_note": ""}
            db.update("jobs", job["id"], status="answered", mode="question", report=json.dumps(rep, ensure_ascii=False))
            db.add_message(repo["id"], "system", f"⚠️ {title} — {e}\n\n코드는 바꾸지 않았고 AI 비용도 들지 않았습니다.", job["id"])
            return None
    for p in pages if not followup else []:
        note = " 내용이 길어 앞부분만 읽었습니다." if p.truncated else ""
        db.add_message(repo["id"], "system", f"📄 노션 페이지를 읽었습니다 — {p.title} ({len(p.text):,}자).{note}", job["id"])
    return linked_text(pages)


async def _answer(repo: dict, job: dict, route: Route, recent: str, linked: str = ""):
    """질문 모드: 코드는 건드리지 않고 무료 검색 결과 + 저렴한 모델 1회 호출로 답한다."""
    job_id, wd = job["id"], Path(repo["workdir"])
    db.update("jobs", job_id, status="running", mode="question", model=settings.fast_model)
    db.add_message(repo["id"], "system", "질문으로 판단해 코드를 바꾸지 않고 답변합니다 (저렴한 모델 사용).", job_id)
    try:
        found = await asyncio.to_thread(gitops.code_search, wd, route.search) if route.search and wd.exists() else ""
        ans = await get_agent().answer(job["request"] + linked, repo.get("env_notes") or "", recent, found,
                                       wd if wd.exists() else None)
        rep = {"status": "answered", "title": ans.title, "summary_for_owner": ans.answer,
               "changes": [], "tests": "", "risk": "", "developer_note": ""}
        db.update("jobs", job_id, status="answered", report=json.dumps(rep, ensure_ascii=False),
                  cost_usd=route.cost_usd + ans.cost_usd)
        text = f"💬 **{ans.title}**\n\n{ans.answer}"
        # 열어 볼 폴더는 화면에서 [폴더 열기] 버튼으로 바뀐다 (작업 폴더 안 경로만)
        opens = [p for p in ans.open_paths if readonly.safe_path(wd, p)]
        if opens:
            text += "\n\n" + "\n".join(f"[[open:{p.strip().strip('/')}]]" for p in opens)
        if ans.need_deeper:
            text += "\n\n더 정확히 확인하려면 **\"자세히 조사해줘\"**라고 보내 주세요. 마부장님이 코드를 직접 살펴봅니다(비용이 더 듭니다)."
        db.add_message(repo["id"], "mabu", text, job_id)
    except Exception as e:
        log.exception("answer failed")
        db.update("jobs", job_id, status="failed", cost_usd=route.cost_usd)
        db.add_message(repo["id"], "system", f"❌ 답변 중 오류가 발생했습니다.\n```\n{str(e)[-1500:]}\n```", job_id)


# ---------- 1·2. 저장소 등록 + 작업환경 구성 ----------
async def register_and_setup(repo_id: int):
    repo = db.one("SELECT * FROM repos WHERE id=?", (repo_id,))
    wd = Path(repo["workdir"])
    try:
        db.add_message(repo_id, "system", f"저장소를 내려받는 중입니다: {repo['git_url']}")
        await asyncio.to_thread(gitops.clone, repo["git_url"], wd, repo["base_branch"])
        actual = await asyncio.to_thread(gitops.current_branch, wd)
        db.update("repos", repo_id, base_branch=actual, status="setting_up")
        repo = db.one("SELECT * FROM repos WHERE id=?", (repo_id,))
    except Exception as e:
        db.update("repos", repo_id, status="error")
        db.add_message(repo_id, "system", f"❌ 저장소를 내려받지 못했습니다.\n```\n{e}\n```")
        return

    now = time.time()
    job_id = db.ex(
        "INSERT INTO jobs(repo_id, kind, request, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (repo_id, "setup", "작업 환경 구성", "running", now, now),
    )
    db.add_message(repo_id, "system", "마부장님이 작업 환경을 구성하고 있습니다…", job_id)
    async with _lock(repo_id):
        try:
            res = await _run_agent(repo, job_id, SETUP_PROMPT)
            notes = (wd / "MABUJANG.md").read_text(encoding="utf-8") if (wd / "MABUJANG.md").exists() else ""
            # 환경 메모는 저장소에 커밋하지 않고 로컬에만 둔다
            _exclude(wd, "MABUJANG.md")
            db.update("jobs", job_id, status="done", report=json.dumps(res.report, ensure_ascii=False), cost_usd=res.cost_usd)
            db.update("repos", repo_id, status="ready", env_notes=notes, session_id=res.session_id)
            db.add_message(repo_id, "mabu", format_report(res.report) + "\n\n이제 요청사항을 말씀해 주세요.", job_id)
        except Exception as e:
            log.exception("setup failed")
            db.update("jobs", job_id, status="failed")
            db.update("repos", repo_id, status="ready")
            db.add_message(repo_id, "system", f"⚠️ 환경 구성 중 문제가 생겼지만 요청은 받을 수 있습니다.\n```\n{e}\n```", job_id)
            return
    await analyze_data_dirs(repo_id)   # 환경 구성 뒤 데이터 파일 폴더도 미리 찾아 둔다 (저렴한 모델)


# ---------- 데이터 파일 폴더 분석 (설정 → 데이터 파일 탭) ----------
def resolve_data_dir(repo: dict, path: str) -> tuple[Path | None, bool]:
    """설정에 적힌 경로를 실제 폴더로 바꾼다. 상대 경로는 서버 실행 위치(작업 폴더) 기준. (경로, 작업 폴더 안 여부)"""
    if not path or "${" in path:
        return None, False
    root = Path(repo["workdir"]).resolve()
    p = (root / path.strip()).resolve()     # 절대 경로면 그대로, 상대 경로면 작업 폴더 기준
    inside = p.is_relative_to(root)
    if inside and not readonly.safe_path(root, p.relative_to(root).as_posix()):
        return None, True                   # .git·비밀값 파일·작업 폴더 자체는 열지 않음
    return p, inside


def _jlist(text: str) -> list:
    try:
        v = json.loads(text or "[]")
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


def add_data_dir(repo_id: int, path: str, desc: str) -> str | None:
    """관리자가 직접 등록. 문제가 있으면 이유 문자열."""
    path = path.strip().strip('"')
    if not path or "${" in path:
        return "폴더 경로를 입력해 주세요"
    repo = db.one("SELECT * FROM repos WHERE id=?", (repo_id,))
    p, inside = resolve_data_dir(repo, path)
    if not p:
        return "열 수 없는 경로입니다 (.git·비밀값 파일·작업 폴더 자체는 등록할 수 없음)"
    dirs = _jlist(repo["data_dirs"])
    if any(d.get("path") == path for d in dirs):
        return "이미 등록된 폴더입니다"
    dirs.insert(0, {"path": path, "desc": desc.strip(), "source": "직접 등록", "profile": "", "manual": True})
    hidden = [h for h in _jlist(repo["data_hidden"]) if h != path]
    db.update("repos", repo_id, data_dirs=json.dumps(dirs, ensure_ascii=False), data_hidden=json.dumps(hidden, ensure_ascii=False))
    return None


def remove_data_dir(repo_id: int, idx: int) -> bool:
    repo = db.one("SELECT data_dirs, data_hidden FROM repos WHERE id=?", (repo_id,))
    dirs = _jlist(repo["data_dirs"])
    if not 0 <= idx < len(dirs):
        return False
    d = dirs.pop(idx)
    hidden = _jlist(repo["data_hidden"])
    if not d.get("manual") and d.get("path") not in hidden:
        hidden.append(d.get("path"))       # 다시 분석해도 나오지 않게
    db.update("repos", repo_id, data_dirs=json.dumps(dirs, ensure_ascii=False), data_hidden=json.dumps(hidden, ensure_ascii=False))
    return True


async def analyze_data_dirs(repo_id: int):
    repo = db.one("SELECT * FROM repos WHERE id=?", (repo_id,))
    if repo["data_status"] == "analyzing":
        return
    now = time.time()
    job_id = db.ex("INSERT INTO jobs(repo_id, kind, request, status, mode, model, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                   (repo_id, "data", "데이터 파일 위치 분석", "running", "question", settings.fast_model, now, now))
    db.update("repos", repo_id, data_status="analyzing")
    try:
        found, cost = await get_agent().data_dirs(repo.get("env_notes") or "", Path(repo["workdir"]))
        # 분석 중에 바뀌었을 수 있으니 최신 목록 기준으로: 직접 등록한 것은 유지, 삭제한 것은 다시 넣지 않음
        latest = db.one("SELECT data_dirs, data_hidden FROM repos WHERE id=?", (repo_id,))
        manual = [d for d in _jlist(latest["data_dirs"]) if d.get("manual")]
        skip = set(_jlist(latest["data_hidden"])) | {d["path"] for d in manual}
        dirs = manual + [d for d in found if d["path"] not in skip]
        db.update("repos", repo_id, data_dirs=json.dumps(dirs, ensure_ascii=False), data_status="done")
        rep = {"status": "answered", "title": f"데이터 파일 폴더 {len(dirs)}곳을 찾았습니다", "summary_for_owner": "",
               "changes": [], "tests": "", "risk": "", "developer_note": ""}
        db.update("jobs", job_id, status="answered", report=json.dumps(rep, ensure_ascii=False), cost_usd=cost)
        db.add_message(repo_id, "system", f"📁 데이터 파일 폴더 {len(dirs)}곳을 찾았습니다. [설정] → [데이터 파일]에서 열어 볼 수 있습니다.", job_id)
    except Exception as e:
        log.exception("data dirs failed")
        db.update("repos", repo_id, data_status="failed")
        db.update("jobs", job_id, status="failed")
        db.add_message(repo_id, "system", f"⚠️ 데이터 파일 위치 분석에 실패했습니다.\n```\n{str(e)[-800:]}\n```", job_id)


def _exclude(wd: Path, name: str):
    ex = wd / ".git" / "info" / "exclude"
    ex.parent.mkdir(parents=True, exist_ok=True)
    cur = ex.read_text() if ex.exists() else ""
    if name not in cur.split():
        ex.write_text(cur.rstrip("\n") + f"\n{name}\n")


# ---------- 3·4. 요구사항 처리 ----------
async def handle_request(job_id: int):
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    repo = db.one("SELECT * FROM repos WHERE id=?", (job["repo_id"],))
    wd, base = Path(repo["workdir"]), repo["base_branch"]
    branch = f"mabu/job-{job_id}"
    recent = _recent(repo["id"], job_id)

    db.update("jobs", job_id, status="running")
    linked = await _read_links(repo, job)
    if linked is None:
        return
    route = await _route(repo, job, recent, linked)
    if route.kind == "question":
        return await _answer(repo, job, route, recent, linked)
    # 저장소에 모델을 직접 골랐으면 그대로, 기본값이면 간단한 수정만 저렴한 모델
    model = repo.get("model") or (settings.fast_model if route.kind == "simple" else settings.model)
    db.update("jobs", job_id, mode=route.kind, model=model)

    async with _lock(repo["id"]):
        db.update("jobs", job_id, status="running", branch=branch)
        db.add_message(repo["id"], "system", "마부장님이 작업을 시작했습니다."
                       + (" (간단한 수정으로 판단해 저렴한 모델 사용)" if model == settings.fast_model else ""), job_id)
        try:
            if actions.status(repo["id"])["running"]:
                await actions.stop(repo)   # 버튼으로 띄운 서버가 파일/포트를 잡지 않도록
            await asyncio.to_thread(gitops.start_job_branch, wd, base, branch)
            env = db.parse_env(repo.get("env_vars", ""))
            prompt = request_prompt(job["request"], repo["test_cmd"], list(env), repo.get("run_cmd", ""),
                                    repo.get("run_url", ""), repo.get("last_action_log", "") if job.get("attach_log") else "",
                                    repo.get("pre_run_cmd", ""), recent, linked)
            res = await _run_agent(repo, job_id, prompt, model)
            res.cost_usd += route.cost_usd
            rep = res.report
            if res.session_id:
                db.update("repos", repo["id"], session_id=res.session_id)

            # 사용자 지정 검증 명령은 시스템이 한 번 더 직접 실행 (AI 보고만 믿지 않음)
            if repo["test_cmd"] and rep.get("status") == "done":
                code, out = await asyncio.to_thread(gitops.sh, repo["test_cmd"], wd, settings.deploy_timeout, env)
                if code != 0:
                    rep["status"] = "needs_developer"
                    rep["developer_note"] = (rep.get("developer_note") or "") + f"\n검증 명령 실패(exit {code}):\n{out[-1500:]}"
                    rep["tests"] = f"시스템 재검증 실패: `{repo['test_cmd']}`"
                else:
                    rep["tests"] = (rep.get("tests") or "") + f" / 시스템 재검증 통과: `{repo['test_cmd']}`"

            committed = await asyncio.to_thread(gitops.commit_all, wd, f"마부장님 #{job_id}: {rep.get('title', job['request'][:50])}")
            ahead = await asyncio.to_thread(gitops.commits_ahead, wd, base, branch)
            stat = await asyncio.to_thread(gitops.diff_stat, wd, base, branch)
            db.update("jobs", job_id, report=json.dumps(rep, ensure_ascii=False), diff_stat=stat, cost_usd=res.cost_usd)

            if rep.get("status") == "needs_developer":
                db.update("jobs", job_id, status="needs_developer")
                db.add_message(repo["id"], "mabu", format_report(rep) + "\n\n개발자를 호출했습니다. 작업 브랜치는 보존되어 있습니다.", job_id)
                notify.call_developer(repo, job_id, rep.get("title") or "개발자 확인 필요", rep.get("summary_for_owner", ""),
                                      rep.get("developer_note", ""), branch, job["request"])
                await asyncio.to_thread(gitops.run, ["git", "checkout", base], wd, 60, False)
                return
            if not ahead:
                db.update("jobs", job_id, status="no_change")
                db.add_message(repo["id"], "mabu", format_report(rep), job_id)
                await asyncio.to_thread(gitops.discard_branch, wd, base, branch)
                return

            db.update("jobs", job_id, status="awaiting_approval")
            await asyncio.to_thread(gitops.run, ["git", "checkout", base], wd, 60, False)
            if repo["auto_deploy"] and rep.get("risk") == "low":
                db.add_message(repo["id"], "mabu", format_report(rep) + "\n\n위험도가 낮아 자동 배포합니다.", job_id)
            else:
                db.add_message(repo["id"], "mabu", format_report(rep) + "\n\n👉 확인 후 **[배포 승인]** 을 눌러 주세요.", job_id)
                return
        except Exception as e:
            log.exception("job failed")
            db.update("jobs", job_id, status="failed")
            db.add_message(repo["id"], "system", f"❌ #{job_id} 작업 중 오류가 발생해 개발자를 호출했습니다. 서비스는 바뀌지 않았습니다.", job_id)
            notify.call_developer(repo, job_id, "작업 실패", f"작업 중 오류: {str(e)[-500:]}", traceback.format_exc(), branch, job["request"])
            await asyncio.to_thread(gitops.run, ["git", "checkout", "-f", base], wd, 60, False)
            return
    await deploy(job_id)  # auto_deploy 경로


# ---------- 4·5. 배포 + 보고 ----------
async def _stop_preview(repo: dict, job_id: int):
    """이 작업을 미리 보기로 띄운 서버가 있으면 끈다 (브랜치를 병합·삭제하기 전에)."""
    st = actions.status(repo["id"])
    if st["running"] and st["preview"] == job_id:
        await actions.stop(repo)


def busy(repo_id: int) -> bool:
    """AI 작업·배포가 작업 폴더를 쓰는 중인지 (이때 브랜치를 바꾸면 안 됨)."""
    return _lock(repo_id).locked()


async def deploy(job_id: int):
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    repo = db.one("SELECT * FROM repos WHERE id=?", (job["repo_id"],))
    wd, base = Path(repo["workdir"]), repo["base_branch"]
    async with _lock(repo["id"]):
        await _stop_preview(repo, job_id)
        db.update("jobs", job_id, status="deploying")
        db.add_message(repo["id"], "system", "배포를 진행합니다…", job_id)
        logs: list[str] = []
        try:
            logs.append(await asyncio.to_thread(gitops.merge_and_push, wd, base, job["branch"], bool(repo["push_on_deploy"])))
            if repo["deploy_cmd"]:
                code, out = await asyncio.to_thread(gitops.sh, repo["deploy_cmd"], wd, settings.deploy_timeout,
                                                    db.parse_env(repo.get("env_vars", "")))
                logs.append(f"$ {repo['deploy_cmd']}\n{out}")
                if code != 0:
                    raise RuntimeError(f"배포 명령 실패 (exit {code})\n{out[-1500:]}")
            await asyncio.to_thread(gitops.run, ["git", "branch", "-d", job["branch"]], wd, 60, False)
            sha = await asyncio.to_thread(gitops.run, ["git", "rev-parse", base], wd, 60, False)
            db.update("jobs", job_id, status="done", deploy_log="\n\n".join(logs)[-20000:], merge_sha=sha)
            rep = db.job_dict(job)["report"] or {}
            msg = f"🚀 배포 완료 — {rep.get('title', job['request'][:40])}"
            db.add_message(repo["id"], "mabu", msg + "\n\n서비스에 반영되었습니다. 이상이 있으면 말씀해 주세요.", job_id)
            notify.send(f"🚀 [마부장님] {repo['name']} #{job_id} 배포 완료: {rep.get('title','')}")
        except gitops.MergeConflict as e:
            # 그 사이 배포된 다른 작업이 같은 곳을 고침 → 자동으로 합칠 수 없음. 작업 폴더는 원래대로 되돌려 둠
            db.update("jobs", job_id, status="conflict", deploy_log=str(e))
            db.add_message(repo["id"], "system",
                           f"⚠️ #{job_id}은(는) 배포하지 않았습니다. 그 사이에 배포된 다른 작업이 같은 부분을 먼저 고쳐서 "
                           f"자동으로 합칠 수 없습니다. 서비스와 GitHub는 바뀌지 않았습니다.\n({e})\n\n"
                           f"이미 원하는 결과가 반영됐다면 #{job_id}을(를) **[취소]**하고, 아니면 다시 요청해 주세요.", job_id)
        except Exception as e:
            db.update("jobs", job_id, status="failed", deploy_log=str(e)[-20000:])
            state = ("코드는 이미 합쳐졌지만 배포 명령이 실패해 서비스에 반영되지 않았을 수 있습니다" if logs
                     else "서비스와 GitHub는 바뀌지 않았습니다")
            db.add_message(repo["id"], "system", f"❌ #{job_id} 배포에 실패해 개발자를 호출했습니다. {state}.", job_id)
            notify.call_developer(repo, job_id, "배포 실패", state, str(e), job["branch"], job["request"])


def last_deployed(repo_id: int) -> dict | None:
    """[되돌리기] 대상: 가장 최근에 배포된 요청 작업 (그 뒤에 배포·되돌리기된 작업이 없어야 함)."""
    j = db.one("SELECT * FROM jobs WHERE repo_id=? AND kind='request' AND status IN ('done','reverted','reverting') "
               "ORDER BY updated_at DESC LIMIT 1", (repo_id,))
    return j if j and j["status"] == "done" else None


# ---------- 직전 배포 되돌리기 (AI 없음, 무료) ----------
async def revert(job_id: int):
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    repo = db.one("SELECT * FROM repos WHERE id=?", (job["repo_id"],))
    wd, base = Path(repo["workdir"]), repo["base_branch"]
    async with _lock(repo["id"]):
        db.update("jobs", job_id, status="reverting")
        db.add_message(repo["id"], "system", f"#{job_id} 변경을 되돌리는 중입니다…", job_id)
        logs: list[str] = []
        try:
            sha = job.get("merge_sha") or await asyncio.to_thread(gitops.find_merge, wd, base, job["branch"])
            if not sha:
                raise RuntimeError("배포 기록(병합 커밋)을 찾지 못했습니다.")
            title = (db.job_dict(job)["report"] or {}).get("title") or job["request"][:40]
            logs.append(await asyncio.to_thread(gitops.revert_merge, wd, base, sha,
                                                f"마부장님: #{job_id} 되돌리기 ({title})", bool(repo["push_on_deploy"])))
            if repo["deploy_cmd"]:
                code, out = await asyncio.to_thread(gitops.sh, repo["deploy_cmd"], wd, settings.deploy_timeout,
                                                    db.parse_env(repo.get("env_vars", "")))
                logs.append(f"$ {repo['deploy_cmd']}\n{out}")
                if code != 0:
                    raise RuntimeError(f"되돌린 뒤 배포 명령 실패 (exit {code})\n{out[-1500:]}")
            db.update("jobs", job_id, status="reverted", deploy_log="\n\n".join(logs)[-20000:])
            db.add_message(repo["id"], "mabu", f"↩️ #{job_id} 변경을 되돌렸습니다 — {title}\n\n서비스가 변경 전 상태로 돌아갔습니다.", job_id)
            notify.send(f"↩️ [마부장님] {repo['name']} #{job_id} 되돌리기 완료: {title}")
        except Exception as e:
            # 되돌린 코드가 이미 합쳐졌으면(배포 명령만 실패) 다시 되돌리기를 누르지 못하게 상태도 맞춘다
            db.update("jobs", job_id, status="reverted" if logs else "done", deploy_log=f"[되돌리기 실패]\n{str(e)[-20000:]}")
            state = ("코드는 이미 되돌렸지만 배포 명령이 실패해 서비스에 반영되지 않았을 수 있습니다" if logs
                     else "서비스와 GitHub는 바뀌지 않았습니다")
            db.add_message(repo["id"], "system", f"❌ #{job_id} 되돌리기에 실패해 개발자를 호출했습니다. {state}.", job_id)
            notify.call_developer(repo, job_id, "되돌리기 실패", state, str(e), "", job["request"])


async def reject(job_id: int):
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    repo = db.one("SELECT * FROM repos WHERE id=?", (job["repo_id"],))
    async with _lock(repo["id"]):
        await _stop_preview(repo, job_id)
        await asyncio.to_thread(gitops.discard_branch, Path(repo["workdir"]), repo["base_branch"], job["branch"])
        db.update("jobs", job_id, status="rejected")
        db.add_message(repo["id"], "system", "변경을 취소했습니다. 서비스에는 반영되지 않았습니다.", job_id)
