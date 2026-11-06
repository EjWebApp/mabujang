"""질문 모드용 읽기 전용 도구 — 작업 폴더 안의 파일만 찾고 읽는다 (수정·실행 불가)."""
import re
from pathlib import Path

from . import gitops

# 비밀값이 들어 있을 수 있는 파일은 읽지 않는다
SECRET_RE = re.compile(r"(^|/)(\.env[^/]*|secret[^/]*|[^/]*\.(pem|key|p12|jks|keystore))$", re.I)

TOOLS = [
    {"name": "list_files", "description": "저장소 파일 경로 중 글자(대소문자 무시)를 포함하는 것을 최대 50개 찾는다.",
     "input_schema": {"type": "object", "properties": {"contains": {"type": "string"}}, "required": ["contains"],
                      "additionalProperties": False}},
    {"name": "search_code", "description": "파일 내용에서 글자(대소문자 무시)를 찾아 '경로:줄번호:내용'을 최대 40줄 돌려준다.",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
                      "additionalProperties": False}},
    {"name": "read_file", "description": "파일의 일부를 줄 번호와 함께 읽는다 (한 번에 최대 150줄).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"},
                                                       "end_line": {"type": "integer"}},
                      "required": ["path"], "additionalProperties": False}},
]


def _blocked(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    return rel.startswith(".git/") or rel == ".git" or bool(SECRET_RE.search(rel))


def safe_path(cwd: Path, rel: str) -> Path | None:
    """작업 폴더 안의 경로만 돌려준다 (.git·비밀값 파일·폴더 밖이면 None)."""
    root = Path(cwd).resolve()
    p = (root / str(rel).strip().strip("/\\")).resolve()
    if p == root or not p.is_relative_to(root) or _blocked(p.relative_to(root).as_posix()):
        return None
    return p


def run_tool(cwd: Path, name: str, inp: dict) -> str:
    try:
        if name == "list_files":
            q = str(inp.get("contains", "")).lower()
            files = [f for f in gitops.run(["git", "ls-files"], cwd, check=False).splitlines()
                     if q in f.lower() and not _blocked(f)]
            return "\n".join(files[:50]) or "(없음)"
        if name == "search_code":
            out = gitops.run(["git", "grep", "-n", "-I", "-i", "-F", "--max-count=5", "-e", str(inp.get("text", "")), "--",
                              ".", ":!*.min.js", ":!*.min.css", ":!*.map"], cwd, check=False).splitlines()
            out = [o[:240] for o in out if not _blocked(o.split(":", 1)[0])]
            return "\n".join(out[:40]) or "(없음)"
        if name == "read_file":
            root = Path(cwd).resolve()
            p = (root / str(inp.get("path", ""))).resolve()
            if not p.is_relative_to(root) or _blocked(p.relative_to(root).as_posix()):
                return "읽을 수 없는 경로입니다."
            if not p.is_file():
                return "파일이 없습니다."
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            s = max(1, int(inp.get("start_line") or 1))
            e = min(len(lines), int(inp.get("end_line") or s + 149), s + 149)
            return "\n".join(f"{i}: {lines[i - 1][:300]}" for i in range(s, e + 1)) or "(빈 파일)"
        return "알 수 없는 도구입니다."
    except Exception as ex:   # 도구 오류는 모델에게 알려 주고 계속 진행
        return f"오류: {ex}"
