import os
from dataclasses import dataclass, field


def _bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # 데이터/작업공간 위치 (마부장님이 설치된 서버의 로컬)
    data_dir: str = field(default_factory=lambda: os.getenv("MABU_DATA_DIR", "./data"))
    workspaces_dir: str = field(default_factory=lambda: os.getenv("MABU_WORKSPACES", "./data/workspaces"))
    # 관리자 로그인 비밀번호 (비어 있으면 인증 없음 — 로컬 테스트용)
    admin_password: str = field(default_factory=lambda: os.getenv("MABU_ADMIN_PASSWORD", ""))
    # claude = 실제 Claude 에이전트, mock = 테스트용 가짜 에이전트
    agent: str = field(default_factory=lambda: os.getenv("MABU_AGENT", "claude"))
    # 비용 절감 기본값: 중급 모델, 짧은 턴, 낮은 상한, 이전 대화 이어붙이지 않음
    model: str = field(default_factory=lambda: os.getenv("MABU_MODEL", "claude-sonnet-5-5"))
    max_turns: int = field(default_factory=lambda: int(os.getenv("MABU_MAX_TURNS", "30")))
    max_budget_usd: float = field(default_factory=lambda: float(os.getenv("MABU_MAX_BUDGET_USD", "1")))
    resume_session: bool = field(default_factory=lambda: _bool("MABU_RESUME", "0"))
    # 요청 자동 분류: 질문은 질문 모드(코드 수정 없이 1회 답변), 간단한 수정은 저렴한 모델로 처리
    auto_route: bool = field(default_factory=lambda: _bool("MABU_AUTO_ROUTE", "1"))
    fast_model: str = field(default_factory=lambda: os.getenv("MABU_FAST_MODEL", "claude-haiku-5-5"))
    # 작업 폴더 밖 접근 차단
    restrict_to_workdir: bool = field(default_factory=lambda: _bool("MABU_RESTRICT", "1"))
    # 개발자 호출/보고 알림 (Slack/Discord 호환 웹훅, 선택)
    notify_webhook: str = field(default_factory=lambda: os.getenv("MABU_NOTIFY_WEBHOOK", ""))
    # 개발자 호출: 노션 기록 + 이메일 (선택)
    notion_token: str = field(default_factory=lambda: os.getenv("MABU_NOTION_TOKEN", ""))
    notion_database: str = field(default_factory=lambda: os.getenv("MABU_NOTION_DATABASE", ""))   # 데이터베이스 ID 또는 링크
    smtp_host: str = field(default_factory=lambda: os.getenv("MABU_SMTP_HOST", "smtp.gmail.com"))
    smtp_port: int = field(default_factory=lambda: int(os.getenv("MABU_SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.getenv("MABU_SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("MABU_SMTP_PASSWORD", ""))       # Gmail은 앱 비밀번호
    mail_from: str = field(default_factory=lambda: os.getenv("MABU_MAIL_FROM", ""))               # 비우면 smtp_user
    mail_to: str = field(default_factory=lambda: os.getenv("MABU_MAIL_TO", ""))                   # 받을 개발자 (쉼표로 여러 명)
    deploy_timeout: int = field(default_factory=lambda: int(os.getenv("MABU_DEPLOY_TIMEOUT", "900")))
    run_wait: int = field(default_factory=lambda: int(os.getenv("MABU_RUN_WAIT", "120")))
    port_wait: int = field(default_factory=lambda: int(os.getenv("MABU_PORT_WAIT", "60")))
    docker_desktop: str = field(default_factory=lambda: os.getenv(
        "MABU_DOCKER_DESKTOP", r"C:\Program Files\Docker\Docker\Docker Desktop.exe"))


settings = Settings()
