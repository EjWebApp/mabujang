"""채팅에 붙여 넣은 노션 링크 읽기 (AI 없음, 무료). 링크에서 ID만 뽑아 노션 API로 읽으므로 임의 주소에는 접속하지 않는다."""
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from .config import settings
from .notify import NotionError, _notion

LINK_RE = re.compile(r"https?://(?:[\w-]+\.)*(?:notion\.so|notion\.com|notion\.site)/[^\s<>\"']+", re.I)
ID_RE = re.compile(r"([0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I)
MAX_LINKS = 3        # 메시지 하나에서 읽을 링크 수
MAX_CHARS = 12000    # 링크 하나에서 AI에게 넘길 글자 수
MAX_BLOCKS = 300
MAX_DEPTH = 4
MAX_ROWS = 30

PREFIX = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ", "bulleted_list_item": "- ",
          "numbered_list_item": "1. ", "quote": "> "}
MEDIA = {"image": "이미지", "video": "영상", "file": "파일", "pdf": "PDF", "audio": "오디오"}


@dataclass
class LinkPage:
    title: str
    text: str
    truncated: bool = False


class LinkError(Exception):
    """관리자에게 그대로 보여 줄 수 있는 쉬운 설명."""


def find_links(text: str) -> list[str]:
    links = [m.rstrip(").,]") for m in LINK_RE.findall(text or "")]
    return list(dict.fromkeys(links))[:MAX_LINKS]


def link_ids(url: str) -> list[str]:
    """읽어 볼 ID를 구체적인 것부터: 열어 본 행(?p=), 가리킨 위치(#), 페이지 경로. ?v=는 보기 ID이므로 제외한다."""
    u = urlsplit(url)
    parts = [(parse_qs(u.query).get("p") or [""])[0], u.fragment, u.path.rstrip("/").rsplit("/", 1)[-1]]
    ids = [m.group(1).replace("-", "").lower() for m in map(ID_RE.search, parts) if m]
    return list(dict.fromkeys(ids))


def _plain(rich) -> str:
    return "".join(x.get("plain_text", "") for x in rich or [])


def _prop(p: dict) -> str:
    kind = p.get("type")
    v = p.get(kind)
    if kind in ("title", "rich_text"):
        return _plain(v)
    if kind in ("select", "status"):
        return (v or {}).get("name", "")
    if kind == "multi_select":
        return ", ".join(x.get("name", "") for x in v or [])
    if kind == "people":
        return ", ".join(x.get("name", "") for x in v or [] if x.get("name"))
    if kind == "date":
        return " ~ ".join(x for x in ((v or {}).get("start"), (v or {}).get("end")) if x)
    if kind == "checkbox":
        return "예" if v else "아니요"
    if kind == "formula":
        return _prop(v or {})
    if kind in ("number", "url", "email", "phone_number"):
        return "" if v is None else str(v)
    return ""


def _title(props: dict) -> str:
    return next((_plain(p.get("title")) for p in props.values() if p.get("type") == "title"), "") or "(제목 없음)"


def _prop_lines(props: dict) -> list[str]:
    return [f"{k}: {_prop(p)}" for k, p in props.items() if p.get("type") != "title" and _prop(p)]


def _block_line(b: dict) -> str:
    kind = b.get("type", "")
    d = b.get(kind) or {}
    text = _plain(d.get("rich_text"))
    if kind == "to_do":
        return f"[{'x' if d.get('checked') else ' '}] {text}"
    if kind == "code":
        return f"```\n{text}\n```"
    if kind in ("child_page", "child_database"):
        return f"[하위 {'페이지' if kind == 'child_page' else '표'}: {d.get('title', '')}]"
    if kind in MEDIA:
        return f"[{MEDIA[kind]}: {_plain(d.get('caption')) or d.get('name') or '설명 없음'}]"
    if kind in ("bookmark", "embed", "link_preview"):
        return f"[링크: {d.get('url', '')}]"
    if kind == "table_row":
        return " | ".join(_plain(c) for c in d.get("cells") or [])
    return PREFIX.get(kind, "") + text if text else ""


def _walk(block_id: str, depth: int, lines: list[str], budget: list[int]):
    cursor = ""
    while budget[0] > 0:
        d = _notion("GET", f"/blocks/{block_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else ""))
        for b in d.get("results") or []:
            if budget[0] <= 0:
                return
            budget[0] -= 1
            line = _block_line(b)
            if line:
                lines.append("  " * depth + line)
            # 하위 페이지·표는 다른 문서이므로 따라가지 않는다
            if b.get("has_children") and depth + 1 < MAX_DEPTH and b.get("type") not in ("child_page", "child_database"):
                _walk(b["id"], depth + 1, lines, budget)
        if not d.get("has_more"):
            return
        cursor = d.get("next_cursor") or ""
        if not cursor:
            return


def _comments(page_id: str) -> list[str]:
    try:   # 통합에 '댓글 읽기' 권한이 없어도 본문은 읽는다
        d = _notion("GET", f"/comments?block_id={page_id}&page_size=50")
    except NotionError:
        return []
    return [t for t in (_plain(c.get("rich_text")) for c in d.get("results") or []) if t]


def _finish(title: str, lines: list[str], cut: bool = False) -> LinkPage:
    text = "\n".join(lines)
    return LinkPage(title, text[:MAX_CHARS], cut or len(text) > MAX_CHARS)


def _read_page(page: dict, focus: str = "") -> LinkPage:
    props = page.get("properties") or {}
    lines, body, budget = _prop_lines(props), [], [MAX_BLOCKS]
    _walk(page["id"], 0, body, budget)
    if focus:
        lines += ["", f"(링크가 가리키는 부분: {focus})"]
    lines += ["", *body]
    comments = _comments(page["id"])
    if comments:
        lines += ["", "페이지 댓글:", *[f"- {c}" for c in comments]]
    return _finish(_title(props), lines, budget[0] <= 0)


def _read_database(database: dict) -> LinkPage:
    sources = database.get("data_sources") or []
    if not sources:
        raise LinkError("노션 표에 읽을 내용이 없습니다")
    d = _notion("POST", f"/data_sources/{sources[0]['id']}/query", {"page_size": MAX_ROWS})
    lines = []
    for row in d.get("results") or []:
        props = row.get("properties") or {}
        lines.append(" | ".join([_title(props), *_prop_lines(props)]))
    return _finish(_plain(database.get("title")) or "(제목 없는 표)", lines, bool(d.get("has_more")))


def _read_block(block: dict) -> LinkPage | None:
    """페이지 안의 특정 부분을 가리키는 링크: 그 부분이 속한 페이지 전체를 읽는다."""
    focus, cur = _block_line(block), block
    for _ in range(MAX_DEPTH + 2):
        parent = cur.get("parent") or {}
        if parent.get("type") == "page_id":
            return _read_page(_notion("GET", f"/pages/{parent['page_id']}"), focus)
        if parent.get("type") != "block_id":
            return None
        cur = _notion("GET", f"/blocks/{parent['block_id']}")
    return None


def _try(path: str) -> dict | None:
    try:
        return _notion("GET", path)
    except NotionError as e:
        if e.code in (400, 404):   # 이 종류의 ID가 아니거나 연결되지 않음
            return None
        raise


def read_link(url: str) -> LinkPage:
    """노션 링크를 읽는다. 실패하면 LinkError(쉬운 설명)."""
    if not settings.notion_token:
        raise LinkError("노션 토큰이 없습니다. .env의 MABU_NOTION_TOKEN을 넣고 서버를 다시 시작해 주세요.")
    ids = link_ids(url)
    if not ids:
        raise LinkError("링크에서 페이지 번호를 찾지 못했습니다. 노션의 [링크 복사]로 다시 복사해 주세요.")
    try:
        for i in ids:
            # 표 ID도 블록으로 조회되므로 페이지 → 표 → 블록 순서로 확인한다
            page = _try(f"/pages/{i}")
            if page:
                return _read_page(page)
            database = _try(f"/databases/{i}")
            if database:
                return _read_database(database)
            block = _try(f"/blocks/{i}")
            found = _read_block(block) if block else None
            if found:
                return found
    except NotionError as e:
        if e.code in (401, 403):
            raise LinkError("노션 토큰이 맞지 않거나 읽기 권한이 없습니다. 통합 설정의 토큰과 '콘텐츠 읽기' 권한을 확인해 주세요.") from None
        raise LinkError(f"노션에서 페이지를 읽지 못했습니다. 잠시 후 다시 시도해 주세요. ({e})") from None
    raise LinkError("이 페이지가 마부장님과 연결되어 있지 않습니다. 노션에서 페이지(또는 표) 오른쪽 위 ••• → 연결에 통합을 추가한 뒤 다시 보내 주세요.")
