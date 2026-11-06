"""git 작업 — 작업 브랜치 생성, 커밋, 병합, 푸시."""
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def run(cmd: list[str], cwd: str | Path, timeout: int = 600, check: bool = True) -> str:
    # git 출력은 UTF-8. Windows 기본(cp949)으로 읽으면 한글 커밋 메시지에서 디코딩이 실패해 stdout이 None이 된다
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if check and p.returncode != 0:
        raise GitError(f"$ {' '.join(cmd)}\n{out}")
    return out


def sh(command: str, cwd: str | Path, timeout: int = 900, env: dict | None = None) -> tuple[int, str]:
    import os
    try:
        p = subprocess.run(command, cwd=cwd, shell=True, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, **(env or {})}, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, f"{out}\n[시간 초과: {timeout}초]"
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def ensure_identity(cwd):
    run(["git", "config", "user.name", "마부장님"], cwd)
    run(["git", "config", "user.email", "mabujang@localhost"], cwd)


def clone(url: str, dest: Path, branch: str) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = run(["git", "clone", url, str(dest)], dest.parent, timeout=1800)
    try:
        run(["git", "checkout", branch], dest)
    except GitError:
        # 원격에 없는 브랜치 → 기본 브랜치에서 새로 만듦 (예: demo_mabujang). 첫 배포 때 원격에 생성됨
        run(["git", "checkout", "-b", branch], dest, check=False)
    ensure_identity(dest)
    return out


def current_branch(cwd) -> str:
    return run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)


def has_remote(cwd) -> bool:
    return bool(run(["git", "remote"], cwd, check=False))


def start_job_branch(cwd, base: str, branch: str):
    cleanup_merge(cwd)
    run(["git", "checkout", base], cwd)
    if has_remote(cwd):
        run(["git", "pull", "--ff-only", "origin", base], cwd, check=False)
    run(["git", "checkout", "-B", branch], cwd)


def commit_all(cwd, message: str) -> bool:
    run(["git", "add", "-A"], cwd)
    if not run(["git", "status", "--porcelain"], cwd):
        return False
    run(["git", "commit", "-m", message], cwd)
    return True


def diff_stat(cwd, base: str, branch: str) -> str:
    return run(["git", "diff", "--stat", f"{base}...{branch}"], cwd, check=False)


def commits_ahead(cwd, base: str, branch: str) -> int:
    out = run(["git", "rev-list", "--count", f"{base}..{branch}"], cwd, check=False)
    return int(out) if out.isdigit() else 0


class MergeConflict(GitError):
    """다른 작업이 같은 부분을 먼저 고쳐서 자동으로 합칠 수 없음."""


def cleanup_merge(cwd):
    """합치다 실패한 흔적(MERGE_HEAD)이 남아 있으면 원래대로 되돌린다."""
    if (Path(cwd) / ".git" / "MERGE_HEAD").exists():
        run(["git", "merge", "--abort"], cwd, check=False)


def merge_and_push(cwd, base: str, branch: str, push: bool) -> str:
    """작업 브랜치를 기준 브랜치에 합치고 push. 실패하면 합치기 전 상태로 되돌린다(작업 폴더를 깨진 채로 두지 않음)."""
    cleanup_merge(cwd)
    log = [run(["git", "checkout", base], cwd)]
    before = run(["git", "rev-parse", "HEAD"], cwd)
    try:
        log.append(run(["git", "merge", "--no-ff", branch, "-m", f"마부장님: {branch} 반영"], cwd))
    except GitError as e:
        cleanup_merge(cwd)
        run(["git", "reset", "--hard", before], cwd, check=False)
        if "CONFLICT" in str(e):
            files = [ln.split(" in ", 1)[-1].strip() for ln in str(e).splitlines() if "CONFLICT" in ln]
            raise MergeConflict("겹치는 파일: " + ", ".join(files)) from e
        raise
    if push and has_remote(cwd):
        try:
            log.append(run(["git", "push", "origin", base], cwd, timeout=600))
        except GitError:
            run(["git", "reset", "--hard", before], cwd, check=False)   # 원격에 못 올렸으면 로컬 병합도 취소
            raise
    return "\n".join(x for x in log if x)


def find_merge(cwd, base: str, branch: str) -> str:
    """merge_sha가 저장되기 전에 배포된 작업용: 병합 커밋 메시지로 찾는다."""
    return run(["git", "log", base, "--merges", "-1", "--format=%H", "--fixed-strings",
                f"--grep=마부장님: {branch} 반영"], cwd, check=False)


def revert_merge(cwd, base: str, sha: str, message: str, push: bool) -> str:
    """배포된 병합 커밋을 되돌리는 새 커밋을 만든다(이력은 지우지 않음). 충돌하면 원상복구 후 GitError."""
    log = [run(["git", "checkout", base], cwd)]
    if push and has_remote(cwd):
        log.append(run(["git", "pull", "--ff-only", "origin", base], cwd, check=False))
    try:
        log.append(run(["git", "revert", "-m", "1", "--no-commit", sha], cwd))
        log.append(run(["git", "commit", "-m", message], cwd))
    except GitError:
        run(["git", "revert", "--abort"], cwd, check=False)
        run(["git", "reset", "--hard"], cwd, check=False)
        raise
    if push and has_remote(cwd):
        try:
            log.append(run(["git", "push", "origin", base], cwd, timeout=600))
        except GitError:
            run(["git", "reset", "--hard", "HEAD~1"], cwd, check=False)   # 원격에 못 올렸으면 되돌린 커밋도 취소
            raise
    return "\n".join(x for x in log if x)


def discard_branch(cwd, base: str, branch: str):
    run(["git", "reset", "--hard"], cwd, check=False)
    run(["git", "clean", "-fd"], cwd, check=False)
    run(["git", "checkout", base], cwd, check=False)
    run(["git", "branch", "-D", branch], cwd, check=False)


def code_search(cwd, terms: list[str], per_term: int = 25, max_chars: int = 12000) -> str:
    """질문 모드용 무료 검색: 파일 이름과 내용에서 검색어를 찾아 짧게 모은다."""
    files = run(["git", "ls-files"], cwd, check=False).splitlines()
    out, used = [], 0
    for t in [t.strip() for t in terms if t and t.strip()][:6]:
        names = [f for f in files if t.lower() in f.lower()][:10]
        hits = run(["git", "grep", "-n", "-I", "-i", "-F", "--max-count=3", "-e", t, "--",
                    ".", ":!*.min.js", ":!*.min.css", ":!*.map", ":!*.lock"], cwd, check=False).splitlines()
        hits = [h[:240] for h in hits[:per_term]]
        block = f"## 검색어: {t}\n" + (("파일: " + ", ".join(names) + "\n") if names else "") + ("\n".join(hits) or "(내용 검색 결과 없음)")
        if used + len(block) > max_chars:
            block = block[:max(0, max_chars - used)]
        out.append(block); used += len(block)
        if used >= max_chars:
            break
    return "\n\n".join(out)
