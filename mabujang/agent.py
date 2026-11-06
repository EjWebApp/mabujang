"""AI 에이전트 실행기.

ClaudeAgent : Claude Agent SDK(Claude Code 엔진)로 저장소 안에서 실제 작업
MockAgent   : API 키 없이 흐름을 시험하는 가짜 에이전트
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .config import settings
from .prompts import DATA_PROMPT, QA_PROMPT, ROUTER_PROMPT, SYSTEM_PROMPT

Progress = Callable[[str], Awaitable[None]]

REPORT_RE = re.compile(r"```mabujang-report\s*(\{.*?\})\s*```", re.S)
JSON_RE = re.compile(r"\{.*\}", re.S)

# 100만 토큰당 (입력, 출력) 달러 — 질문 모드처럼 SDK가 비용을 알려주지 않는 직접 호출에 사용
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0)}


def _cost(model: str, usage) -> float:
    pin, pout = PRICES.get(model, PRICES["claude-sonnet-5-5"])
    tin = ((usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * 1.25
           + (getattr(usage, "cache_read_input_tokens", 0) or 0) * 0.1)
    return (tin * pin + (usage.output_tokens or 0) * pout) / 1_000_000


def _json(text: str) -> dict:
    m = JSON_RE.search(text or "")
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


def _clean_dirs(items) -> list[dict]:
    """모델이 돌려준 데이터 폴더 목록을 정리 (경로 중복 제거, 최대 15개)."""
    out, seen = [], set()
    for d in items if isinstance(items, list) else []:
        if not isinstance(d, dict) or not str(d.get("path") or "").strip():
            continue
        path = str(d["path"]).strip()
        if path in seen:
            continue
        seen.add(path)
        out.append({"path": path, "desc": str(d.get("desc") or ""), "source": str(d.get("source") or ""),
                    "profile": str(d.get("profile") or "")})
    return out[:15]


@dataclass
class Route:
    kind: str                 # question | simple | complex
    search: list[str] = field(default_factory=list)
    cost_usd: float = 0.0


@dataclass
class Answer:
    title: str
    answer: str
    need_deeper: bool = False
    cost_usd: float = 0.0
    open_paths: list[str] = field(default_factory=list)   # 관리자가 열어 볼 폴더 (저장소 기준)


@dataclass
class AgentResult:
    report: dict
    session_id: str = ""
    cost_usd: float = 0.0
    raw_text: str = ""
    errors: list[str] = field(default_factory=list)


def parse_report(text: str) -> dict:
    m = REPORT_RE.findall(text or "")
    if m:
        try:
            return json.loads(m[-1])
        except json.JSONDecodeError:
            pass
    return {
        "status": "done",
        "title": "작업 완료 (보고 형식 누락)",
        "summary_for_owner": (text or "").strip()[-1500:],
        "changes": [], "tests": "", "risk": "medium", "developer_note": "",
    }


def _tool_label(name: str, inp: dict) -> str:
    if name == "Bash":
        return f"명령 실행: `{str(inp.get('command', ''))[:120]}`"
    if name in ("Read", "Write", "Edit", "MultiEdit"):
        verb = {"Read": "파일 읽기", "Write": "파일 작성", "Edit": "파일 수정", "MultiEdit": "파일 수정"}[name]
        return f"{verb}: {inp.get('file_path', '')}"
    if name in ("Grep", "Glob"):
        return f"코드 검색: {inp.get('pattern', '')}"
    return f"{name} 사용"


class ClaudeAgent:
    async def _ask(self, system: str, user: str, max_tokens: int) -> tuple[str, float]:
        """Claude Code 없이 저렴한 모델에 한 번만 묻는다 (도구·반복 없음)."""
        import anthropic
        model = settings.fast_model
        async with anthropic.AsyncAnthropic() as client:
            resp = await client.messages.create(
                model=model, max_tokens=max_tokens, system=system,
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": user}],
            )
        text = "".join(b.text for b in resp.content if b.type == "text")
        return text, _cost(model, resp.usage)

    async def _explore(self, system: str, user: str, cwd: Path | None, max_rounds: int) -> tuple[str, float]:
        """저렴한 모델 + 읽기 전용 도구 루프. 마지막 차례에는 도구를 끄고 답하게 한다."""
        import anthropic

        from .readonly import TOOLS, run_tool
        model = settings.fast_model
        messages = [{"role": "user", "content": user}]
        cost, text = 0.0, ""
        async with anthropic.AsyncAnthropic() as client:
            for i in range(max_rounds):
                last = i == max_rounds - 1 or cwd is None
                resp = await client.messages.create(
                    model=model, max_tokens=4000, system=[{"type": "text", "text": system}], messages=messages,
                    tools=TOOLS, tool_choice={"type": "none"} if last else {"type": "auto"},
                    output_config={"effort": "low"}, cache_control={"type": "ephemeral"},
                )
                cost += _cost(model, resp.usage)
                text = "".join(b.text for b in resp.content if b.type == "text")
                calls = [b for b in resp.content if b.type == "tool_use"]
                if not calls:
                    break
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": b.id, "content": run_tool(cwd, b.name, b.input)} for b in calls]})
        return text, cost

    async def data_dirs(self, notes: str, cwd: Path) -> tuple[list[dict], float]:
        """소스와 설정을 읽어 서비스가 쓰는 데이터 파일 폴더 목록을 만든다."""
        text, cost = await self._explore(f"{DATA_PROMPT}\n\n환경 메모:\n{notes[:8000]}",
                                         "이 저장소의 데이터 파일 폴더를 찾아 주세요.", cwd, 10)
        return _clean_dirs(_json(text).get("dirs")), cost

    async def classify(self, request: str, notes: str, recent: str) -> Route:
        text, cost = await self._ask(
            ROUTER_PROMPT,
            f"환경 메모:\n{notes[:6000]}\n\n최근 대화:\n{recent}\n\n새 메시지:\n{request}", 2000)
        d = _json(text)
        kind = d.get("kind") if d.get("kind") in ("question", "simple", "complex") else "complex"
        return Route(kind, [str(x) for x in d.get("search") or []][:6], cost)

    async def answer(self, question: str, notes: str, recent: str, found: str, cwd: Path | None = None,
                     max_rounds: int = 6) -> Answer:
        """질문 모드: 저렴한 모델이 읽기 전용 도구(파일 찾기·검색·읽기)로 몇 번만 직접 찾아보고 답한다."""
        text, cost = await self._explore(
            f"{QA_PROMPT}\n\n환경 메모:\n{notes[:8000]}",
            f"처음 검색 결과:\n{found or '(없음)'}\n\n최근 대화:\n{recent}\n\n질문:\n{question}", cwd, max_rounds)
        d = _json(text)
        return Answer(str(d.get("title") or "답변"), str(d.get("answer") or text.strip()), bool(d.get("need_deeper")), cost,
                      [str(x) for x in d.get("open_paths") or [] if isinstance(x, str)][:3])

    async def run(self, cwd: Path, prompt: str, progress: Progress, resume: str = "",
                  model: str = "", env: dict | None = None) -> AgentResult:
        from claude_agent_sdk import (
            AssistantMessage, ClaudeAgentOptions, HookMatcher, ResultMessage, TextBlock, ToolUseBlock, query,
        )
        from .guard import check

        async def guard_hook(inp, tool_use_id, ctx):
            reason = check(inp.get("tool_name", ""), inp.get("tool_input") or {}, str(cwd))
            if reason:
                await progress("⛔ " + reason)
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                               "permissionDecision": "deny", "permissionDecisionReason": reason}}
            return {}

        model = model or settings.model
        opts = ClaudeAgentOptions(
            cwd=str(cwd),
            system_prompt={"type": "preset", "preset": "claude_code", "append": SYSTEM_PROMPT},
            permission_mode="bypassPermissions",
            disallowed_tools=["Bash(git push:*)", "Bash(git commit:*)", "Bash(git reset --hard:*)",
                              "WebSearch", "WebFetch"],
            max_turns=settings.max_turns,
            max_budget_usd=settings.max_budget_usd,
            setting_sources=["project"],  # 저장소의 CLAUDE.md 등 반영
            resume=(resume or None) if settings.resume_session else None,
            env=env or {},
            hooks={"PreToolUse": [HookMatcher(hooks=[guard_hook])]} if settings.restrict_to_workdir else None,
            **({"model": model} if model else {}),
        )
        texts: list[str] = []
        result = AgentResult(report={})
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock) and b.text.strip():
                        texts.append(b.text)
                        if "mabujang-report" not in b.text:
                            await progress(b.text.strip()[:600])
                    elif isinstance(b, ToolUseBlock):
                        await progress("🔧 " + _tool_label(b.name, b.input or {}))
            elif isinstance(msg, ResultMessage):
                result.session_id = msg.session_id or ""
                result.cost_usd = msg.total_cost_usd or 0.0
                if msg.result:
                    texts.append(msg.result)
                if msg.is_error:
                    result.errors.append(str(msg.errors or msg.subtype))
        result.raw_text = "\n".join(texts)
        result.report = parse_report(result.raw_text)
        return result


class MockAgent:
    """요청 문구에 '개발자'가 있으면 개발자 호출, '확인만'이면 변경 없음, 그 외엔 파일 하나 수정.
    분류: '?'로 끝나면 질문, '문구'가 있으면 간단한 수정, 나머지는 일반 작업."""

    async def data_dirs(self, notes: str, cwd: Path) -> tuple[list[dict], float]:
        return _clean_dirs([{"path": "storage/about videos", "desc": "(mock) 소개 영상", "source": "app.yml"},
                            {"path": "../outside-data", "desc": "(mock) 작업 폴더 밖", "source": "app.yml"}]), 0.004

    async def classify(self, request: str, notes: str, recent: str) -> Route:
        if request.strip().endswith("?"):
            return Route("question", ["README"], 0.001)
        return Route("simple" if "문구" in request else "complex", [], 0.001)

    async def answer(self, question: str, notes: str, recent: str, found: str, cwd: Path | None = None) -> Answer:
        return Answer("질문 답변", f"(mock) 검색 결과:\n{found[:200]}", "모르" in question, 0.002,
                      ["storage/about videos", "../outside"] if "영상" in question else [])

    async def run(self, cwd: Path, prompt: str, progress: Progress, resume: str = "",
                  model: str = "", env: dict | None = None) -> AgentResult:
        await progress("🔧 파일 읽기: README.md")
        req = prompt.split('"""')[1].strip() if '"""' in prompt else prompt   # 최근 대화를 빼고 요구사항만 본다
        if "처음 투입" in prompt:
            (cwd / "MABUJANG.md").write_text("# 마부장님 환경 메모\n- (mock) 테스트 환경\n", encoding="utf-8")
            rep = {"status": "done", "title": "작업 환경 구성 완료",
                   "summary_for_owner": "(mock) 저장소를 살펴보고 환경 메모를 만들었습니다.",
                   "changes": ["MABUJANG.md 작성"], "tests": "N/A", "risk": "low", "developer_note": ""}
        elif "개발자" in req:
            rep = {"status": "needs_developer", "title": "개발자 확인 필요",
                   "summary_for_owner": "(mock) 위험한 변경이라 개발자 확인이 필요합니다.",
                   "changes": [], "tests": "", "risk": "high", "developer_note": "결제 모듈 수정 필요"}
        elif "확인만" in req:
            rep = {"status": "no_change", "title": "확인 완료", "summary_for_owner": "(mock) 변경할 것이 없습니다.",
                   "changes": [], "tests": "", "risk": "low", "developer_note": ""}
        else:
            with open(cwd / "MABU_CHANGES.md", "a", encoding="utf-8") as f:
                f.write(f"- {req}\n")
            await progress("🔧 파일 수정: MABU_CHANGES.md")
            rep = {"status": "done", "title": f"요청 반영: {req[:30]}",
                   "summary_for_owner": "(mock) 요청을 반영했습니다.", "changes": ["MABU_CHANGES.md"],
                   "tests": "mock 테스트 통과", "risk": "low", "developer_note": ""}
        return AgentResult(report=rep, session_id="mock-session", cost_usd=0.01,
                           raw_text=json.dumps(rep, ensure_ascii=False))


def get_agent():
    return MockAgent() if settings.agent == "mock" else ClaudeAgent()
