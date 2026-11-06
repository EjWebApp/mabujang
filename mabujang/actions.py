"""AI 없이 실행하는 버튼 작업 (비용 0원): 빌드 확인 / 서버 실행 / 서버 중지."""
import asyncio
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

from . import db, gitops
from .config import settings

_procs: dict[int, dict] = {}   # repo_id -> {"proc", "log": deque, "started"}


def _env(repo: dict) -> dict:
    return {**os.environ, **db.parse_env(repo.get("env_vars", ""))}


def _save(repo_id: int, action: str, log: str):
    db.update("repos", repo_id, last_action=action, last_action_log=log[-20000:])


def status(repo_id: int) -> dict:
    p = _procs.get(repo_id)
    running = bool(p and p["proc"].poll() is None)
    return {"running": running, "pid": p["proc"].pid if running else None,
            "since": p["started"] if running else None,
            "preview": p.get("preview") if running else None}   # 미리 보기 중인 작업 번호 (기준 브랜치면 None)


async def build(repo: dict) -> tuple[bool, str]:
    if not repo["test_cmd"]:
        return False, "검증 명령이 설정되지 않았습니다. [설정]에서 입력해 주세요."
    db.add_message(repo["id"], "system", f"🔨 빌드 확인 중… `{repo['test_cmd']}`")
    t = time.time()
    code, out = await asyncio.to_thread(gitops.sh, repo["test_cmd"], repo["workdir"], settings.deploy_timeout,
                                        db.parse_env(repo.get("env_vars", "")))
    _save(repo["id"], "빌드 확인", out)
    ok = code == 0
    msg = (f"✅ 빌드 성공 ({time.time()-t:.0f}초)" if ok else f"❌ 빌드 실패 (exit {code})") + \
          f"\n```\n{_tail(out, 25)}\n```"
    db.add_message(repo["id"], "action_ok" if ok else "action_fail", msg)
    return ok, msg


def _tail(text: str, n: int) -> str:
    return "\n".join(text.strip().splitlines()[-n:]) or "(출력 없음)"


# ---------- 실행 전 준비: 실행 전 명령 + 포트 확인 ----------
DOCKER_DOWN = ("error during connect", "cannot connect to the docker daemon", "docker desktop is not running",
               "the system cannot find the file specified", "is the docker daemon running")


def parse_ports(text: str) -> list[tuple[str, int]]:
    out = []
    for part in (text or "").replace(" ", "").split(","):
        if not part:
            continue
        host, _, port = part.rpartition(":")
        if port.isdigit():
            out.append((host or "127.0.0.1", int(port)))
    return out


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _docker_up() -> bool:
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def _desktop_running() -> bool:
    """Docker Desktop.exe 가 이미 떠 있는지 (켜지는 중이면 또 실행하지 않기 위해)."""
    if sys.platform != "win32":
        return False
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Docker Desktop.exe", "/NH"],
                         capture_output=True, text=True, errors="replace").stdout
    return "docker desktop.exe" in out.lower()


_docker_lock = asyncio.Lock()
_starting: set[int] = set()


async def ensure_docker(rid: int) -> tuple[bool, str]:
    """Docker 엔진이 꺼져 있으면 (Windows) Docker Desktop을 켜고 준비될 때까지 대기."""
    if not shutil.which("docker"):
        return False, "이 PC에 docker 명령이 없습니다. Docker Desktop 설치가 필요합니다(개발자 확인)."
    if await asyncio.to_thread(_docker_up):
        return True, ""
    if sys.platform != "win32" or not Path(settings.docker_desktop).exists():
        return False, "Docker가 꺼져 있습니다. Docker Desktop을 실행해 주세요."
    async with _docker_lock:   # 여러 번 눌러도 Docker Desktop은 한 번만 실행
        if await asyncio.to_thread(_docker_up):
            return True, ""
        if await asyncio.to_thread(_desktop_running):
            db.add_message(rid, "system", "🐳 Docker Desktop이 켜지는 중입니다. 준비될 때까지 기다립니다… (최대 2분)")
        else:
            db.add_message(rid, "system", "🐳 Docker가 꺼져 있어 Docker Desktop을 켜는 중입니다… (최대 2분)")
            subprocess.Popen([settings.docker_desktop], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        for _ in range(60):
            await asyncio.sleep(2)
            if await asyncio.to_thread(_docker_up):
                return True, "Docker Desktop 준비 완료"
    return False, ("Docker Desktop이 2분 안에 준비되지 않았습니다. Docker 창에 경고가 떠 있으면 확인해 주세요 "
                   "('Lingering processes' 경고는 [Stop processes]를 누르면 됩니다).")


async def prepare(repo: dict) -> tuple[bool, str]:
    rid, logs = repo["id"], []
    pre = (repo.get("pre_run_cmd") or "").strip()
    if pre:
        if pre.lower().startswith("docker"):
            ok, note = await ensure_docker(rid)
            if not ok:
                return False, note
            if note:
                logs.append(note)
        db.add_message(rid, "system", f"🔧 실행 전 준비… `{pre}`")
        code, out = await asyncio.to_thread(gitops.sh, pre, repo["workdir"], 300,
                                            db.parse_env(repo.get("env_vars", "")))
        logs.append(f"$ {pre}\n{out}")
        if code != 0:
            hint = ""
            if any(k in out.lower() for k in DOCKER_DOWN):
                hint = "\n→ Docker가 꺼져 있는 것 같습니다. Docker Desktop을 실행해 주세요."
            elif "no such container" in out.lower():
                hint = "\n→ 컨테이너 이름이 맞지 않습니다. `docker ps -a`로 이름을 확인해 [설정]을 고쳐 주세요."
            return False, f"실행 전 명령 실패 (exit {code}){hint}\n```\n{_tail(out, 15)}\n```"
    for host, port in parse_ports(repo.get("wait_ports", "")):
        deadline = time.time() + settings.port_wait
        while not await asyncio.to_thread(port_open, host, port):
            if time.time() > deadline:
                return False, (f"{host}:{port} 포트가 {settings.port_wait}초 안에 열리지 않았습니다. "
                               f"DB 등 필요한 서비스가 켜져 있는지 확인해 주세요.")
            await asyncio.sleep(2)
        logs.append(f"포트 확인: {host}:{port} 열림")
    return True, "\n".join(logs)


async def start(repo: dict, job: dict | None = None) -> tuple[bool, str]:
    """서버 실행. job을 주면 승인 전 미리 보기 — 그 작업 브랜치의 코드로 띄운다. 아니면 기준 브랜치."""
    rid = repo["id"]
    if not repo["run_cmd"]:
        return False, "서버 실행 명령이 설정되지 않았습니다. [설정]에서 입력해 주세요."
    st = status(rid)
    want = job["id"] if job else None
    if st["running"] and st["preview"] == want:
        return True, "이미 실행 중입니다."
    if rid in _starting:
        return True, "이미 실행을 준비 중입니다."
    _starting.add(rid)
    try:
        if st["running"]:
            await stop(repo)                     # 다른 코드로 떠 있으면 끄고 다시 띄운다
        branch = job["branch"] if job else repo["base_branch"]
        try:
            await asyncio.to_thread(gitops.cleanup_merge, repo["workdir"])   # 합치다 만 흔적이 있으면 정리
            await asyncio.to_thread(gitops.run, ["git", "checkout", branch], repo["workdir"], 60)
        except gitops.GitError as e:
            what = f"#{job['id']} 미리 보기" if job else "서버 실행"
            msg = f"❌ {what}를 하지 못했습니다 — 실행할 코드({branch})로 바꾸는 중 문제가 생겼습니다.\n```\n{_tail(str(e), 10)}\n```"
            db.add_message(rid, "action_fail", msg)
            return False, msg
        if job:
            db.add_message(rid, "system", f"👀 #{job['id']} 변경을 반영한 코드로 미리 보기를 실행합니다 (아직 배포 전).", job["id"])
        return await _start(repo, want)
    finally:
        _starting.discard(rid)


async def _start(repo: dict, preview: int | None = None) -> tuple[bool, str]:
    rid = repo["id"]
    ok, prep_log = await prepare(repo)
    if not ok:
        _save(rid, "서버 실행 전 준비", prep_log)
        msg = f"❌ 서버 실행 전 준비 실패 — {prep_log}"
        db.add_message(rid, "action_fail", msg)
        return False, msg
    if repo.get("pre_run_cmd") or repo.get("wait_ports"):
        db.add_message(rid, "system", "✔️ 실행 전 준비 완료" + (f" ({prep_log.splitlines()[-1]})" if prep_log else ""))
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
    proc = subprocess.Popen(repo["run_cmd"], cwd=repo["workdir"], shell=True, env=_env(repo),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", **kw)
    log: deque = deque(maxlen=2000)
    _procs[rid] = {"proc": proc, "log": log, "started": time.time(), "preview": preview}
    asyncio.get_running_loop().run_in_executor(None, _pump, proc, log)
    db.add_message(rid, "system", f"▶️ 서버 실행 중… `{repo['run_cmd']}`")

    url, deadline = repo.get("run_url"), time.time() + settings.run_wait
    ok, detail = False, ""
    while time.time() < deadline:
        await asyncio.sleep(2)
        if proc.poll() is not None:
            detail = f"서버가 종료되었습니다 (exit {proc.returncode})"
            break
        if not url:
            if time.time() - _procs[rid]["started"] > 15:
                ok, detail = True, "15초간 정상 동작 중 (확인 주소 미설정)"
                break
            continue
        code = await asyncio.to_thread(_http_status, url)
        if code and code < 500:
            ok, detail = True, f"{url} 응답 {code}"
            break
        if code:
            detail = f"{url} 응답 {code} (서버 오류)"
    else:
        detail = detail or f"{settings.run_wait}초 안에 응답이 없습니다"

    text = "\n".join(log)
    _save(rid, "서버 실행", text)
    if ok:
        msg = (f"✅ #{preview} 미리 보기 실행됨" if preview else "✅ 서버 실행됨") + f" — {detail}" + (f"\n접속: {url}" if url else "")
        if preview:
            msg += "\n확인한 뒤 [배포 승인] 또는 [취소]를 눌러 주세요."
        db.add_message(rid, "action_ok", msg)
    else:
        msg = f"❌ 서버 실행 실패 — {detail}\n```\n{_tail(text, 25)}\n```"
        db.add_message(rid, "action_fail", msg)
    return ok, msg


def _pump(proc, log: deque):
    for line in proc.stdout:
        log.append(line.rstrip())


def _http_status(url: str) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def _kill(proc):
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        # gradle bootRun은 자식 java.exe가 남으므로 트리 전체 종료
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(10)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass


async def stop(repo: dict) -> tuple[bool, str]:
    p = _procs.get(repo["id"])
    if not p or p["proc"].poll() is not None:
        return True, "실행 중인 서버가 없습니다."
    await asyncio.to_thread(_kill, p["proc"])
    db.add_message(repo["id"], "system", "⏹ 서버를 종료했습니다.")
    return True, "종료했습니다."


def recent_log(repo_id: int, n: int = 200) -> str:
    p = _procs.get(repo_id)
    return "\n".join(list(p["log"])[-n:]) if p else ""


def stop_all():
    for p in _procs.values():
        _kill(p["proc"])


def open_in_explorer(path: Path):
    """마부장님이 설치된 PC에서 폴더를 연다 (Windows 탐색기 / macOS Finder / Linux 기본 파일 관리자)."""
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 — 검증된 경로만 전달됨 (readonly.safe_path / 분석 목록)
        _bring_explorer_front(path.name)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _bring_explorer_front(folder: str, wait: float = 8.0) -> bool:
    """서버(백그라운드 프로세스)가 연 창은 Windows가 작업표시줄에만 두므로, 탐색기 창을 찾아 복원·맨 앞으로 가져온다.
    Windows 11은 새 탭으로 열고 그 탭을 선택하므로 창 제목이 '폴더명 - 파일 탐색기' 또는 '폴더명 및 N개 탭 - …'이 된다."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    leaf, found = folder.lower(), []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(h, _):
        cls = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(h, cls, 64)
        if cls.value == "CabinetWClass":   # 파일 탐색기 창
            t = ctypes.create_unicode_buffer(512)
            u.GetWindowTextW(h, t, 512)
            title = t.value.lower()
            if title == leaf or title.startswith(leaf + " "):
                found.append(h)
                return False
        return True

    end = time.time() + wait
    while time.time() < end:
        found.clear()
        u.EnumWindows(each, 0)
        if found:
            h = found[0]
            if u.IsIconic(h):
                u.ShowWindow(h, 9)                  # 최소화 해제
            u.keybd_event(0x12, 0, 0, 0)            # Alt 입력: 백그라운드 프로세스의 포커스 제한 우회
            u.SetForegroundWindow(h)
            u.keybd_event(0x12, 0, 2, 0)
            return True
        time.sleep(0.1)
    return False
