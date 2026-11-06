"""작업 폴더 밖 접근 차단 (AI 도구 호출 직전에 검사)."""
import os
import re
import tempfile
from pathlib import PureWindowsPath

FILE_TOOLS = {"Read": "file_path", "Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
              "NotebookEdit": "notebook_path", "Glob": "path", "Grep": "path"}

# 시스템 도구 경로는 허용 (java, git 실행 등). 사용자 데이터 폴더는 차단.
SYSTEM_PREFIXES = ["/dev/", "/tmp/", "/usr/", "/bin/", "/sbin/", "/opt/", "/etc/alternatives/", "/proc/self/",
                   "c:/program files/", "c:/program files (x86)/", "c:/windows/"]

_ABS_RE = re.compile(r"""(?:(?<=[\s'"=(:])|^)((?:[A-Za-z]:[\\/]|/|~[\\/]?|\$HOME|%USERPROFILE%)[^\s'";|&<>)]*)""")


def norm(p: str) -> str:
    p = p.strip().strip("'\"").replace("\\", "/")
    m = re.match(r"^/([a-zA-Z])(/|$)", p)           # git-bash 형식 /d/dev → d:/dev
    if m and not p.startswith(("/dev/", "/dev", "/tmp", "/usr", "/bin", "/opt", "/etc", "/proc")):
        p = f"{m.group(1)}:/{p[3:]}"
    p = p.lower()
    # .. 정리
    drive = ""
    if re.match(r"^[a-z]:/", p):
        drive, p = p[:2], p[2:]
    parts = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return drive + "/" + "/".join(parts) + ("/" if parts else "")


def _inside(path: str, workdir: str) -> bool:
    n, w = norm(path), norm(workdir)
    if n.startswith(w):
        return True
    tmp = norm(tempfile.gettempdir())
    return n.startswith(tmp) or any(n.startswith(s) for s in SYSTEM_PREFIXES) or n in ("/dev/null/",)


def check(tool: str, inp: dict, workdir: str) -> str | None:
    """허용이면 None, 차단이면 이유 문자열."""
    if tool in FILE_TOOLS:
        p = inp.get(FILE_TOOLS[tool])
        if not p:
            return None
        full = p if (os.path.isabs(p) or PureWindowsPath(p).is_absolute() or p.startswith("~")) else os.path.join(workdir, p)
        if p.startswith("~") or not _inside(full, workdir):
            return f"작업 폴더 밖 경로는 접근할 수 없습니다: {p}"
        return None
    if tool == "Bash":
        cmd = inp.get("command", "")
        # 따옴표로 감싼 경로("C:/Program Files/...")는 통째로 검사하고 원문에서 제거
        for q in re.findall(r"\"([^\"]*)\"|'([^']*)'", cmd):
            s = q[0] or q[1]
            if re.match(r"^(?:[A-Za-z]:[\\/]|/(?!/)|~|\$HOME|%USERPROFILE%)", s) and not _inside(s, workdir):
                return f"작업 폴더 밖 경로는 접근할 수 없습니다: {s}"
        cmd = re.sub(r"\"[^\"]*\"|'[^']*'", " ", cmd)
        for tok in _ABS_RE.findall(cmd):
            if tok.startswith("//"):   # URL (http://...)
                continue
            if tok.startswith(("~", "$HOME", "%USERPROFILE%")):
                return f"홈 폴더 접근은 허용되지 않습니다: {tok}"
            if not _inside(tok, workdir):
                return f"작업 폴더 밖 경로는 접근할 수 없습니다: {tok}"
        # 상대경로로 상위 폴더 탈출 (cd ../.., cat ../../x)
        for tok in re.findall(r"(?:^|[\s'\"=])((?:\.\./)+[^\s'\";|&]*|(?:\.\.\\)+[^\s'\";|&]*|\.\.)(?=$|[\s'\";|&])", cmd):
            if not _inside(os.path.join(workdir, tok), workdir):
                return f"작업 폴더 밖 경로는 접근할 수 없습니다: {tok}"
    return None
