"""영지전 디스코드 운영 봇 (docs/설계.md §33).

- #버그제보·#건의사항: 새 글 → 스레드 + GitHub 이슈, 이슈 상태가 바뀌면 스레드에 알림
- 이슈 ↔ 스레드 중계: 스레드 답글 → 이슈 댓글, 이슈 댓글 → 스레드 (5분마다). 이슈가 닫히면 스레드 잠금·보관
- #건의사항: 하루 한 번 👍 수를 이슈 본문 첫 줄 "추천 N" 으로 갱신
- #세력순위: 고정 메시지 하나를 export/factions.json 이 바뀔 때마다 편집
- 슬래시 명령: /서버상태, /점수 [세력], /가이드 갱신·/제보 목록·/상류(관리자)
- 매주 월요일 #서버공지 웹훅에 지난 주 제보 집계 한 줄
- 상류(모드 제작자 공개 저장소)로 이슈 넘기기: /상류 또는 이슈 라벨 `상류`
- BUG_MODE=auto|list: auto 는 운영자의 Claude Code 세션이 버그를 처리(docs/운영-제보처리.md), 봇은 접수 문구만 다르다

모드 서버가 쓰는 export/*.json 파일만 읽는다(RCON 없음). 상태는 STATE_PATH 의 JSON 파일에 둔다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("mtwbot")

KST = timezone(timedelta(hours=9))


# ───────────────────────── 설정 ─────────────────────────

def _env_int(key: str) -> int:
    value = os.getenv(key, "").strip()
    return int(value) if value.isdigit() else 0


def _env_path(key: str, default: str) -> Path:
    """상대 경로는 bot.py 가 있는 폴더 기준으로 푼다 (tmux 에서 어디서 띄우든 같게)."""
    path = Path(os.getenv(key, "").strip() or default).expanduser()
    return path if path.is_absolute() else (BASE_DIR / path).resolve()


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = _env_int("GUILD_ID")
CHANNEL_BUG = _env_int("CHANNEL_BUG")
CHANNEL_SUGGEST = _env_int("CHANNEL_SUGGEST")
CHANNEL_RANKING = _env_int("CHANNEL_RANKING")
CHANNEL_GUIDE = _env_int("CHANNEL_GUIDE")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip() or "94hyuns/minetotalwar-bot"
EXPORT_DIR = _env_path("EXPORT_DIR", "../minetotalwar/export")
GUIDE_PATH = _env_path("GUIDE_PATH", "../docs/가이드.md")
STATE_PATH = _env_path("STATE_PATH", "./state.json")
BUG_MODE = "auto" if os.getenv("BUG_MODE", "").strip().lower() == "auto" else "list"
NOTICE_WEBHOOK = os.getenv("NOTICE_WEBHOOK", "").strip()
# 모드 제작자 공개 이슈 저장소 (owner/name). 비면 /상류·라벨 `상류` 가 꺼진다
UPSTREAM_REPO = os.getenv("UPSTREAM_REPO", "94hyuns/minetotalwar-bot").strip()

LABEL_BUG = "버그"
LABEL_SUGGEST = "건의"
LABEL_CHECKING = "확인중"
LABEL_HOLD = "보류"
LABEL_UPSTREAM = "상류"
# 라벨이 없으면 시작할 때 만든다 (이름 → 색)
LABEL_COLORS = {LABEL_BUG: "d73a4a", LABEL_SUGGEST: "a2eeef", LABEL_CHECKING: "fbca04", LABEL_HOLD: "c5def5", LABEL_UPSTREAM: "5319e7"}
# 라벨이 붙으면 스레드에 한 번 알리는 것 (라벨 → (상태 키, 문구)). 라벨이 빠지면 다시 붙을 때 또 알린다
LABEL_NOTICES = {
    LABEL_CHECKING: ("checking", "확인 중"),
    LABEL_HOLD: ("on_hold", "보류 (관리자 확인 필요)"),
}
# 봇이 디스코드 답글을 옮겨 단 이슈 댓글의 숨은 표식 — 이 댓글은 스레드로 되돌려 보내지 않는다
RELAY_MARKER = "<!-- from-discord -->"

VERSION_RE = re.compile(r"\bv\d+\.\d+\.\d+\b")
VOTE_LINE_RE = re.compile(r"^추천 \d+\s*$")
THUMBS_UP = "👍"

STATUS_STALE_SECONDS = 180  # status.json 이 이보다 오래되면 서버가 꺼진 것으로 본다
DISCORD_LIMIT = 2000
RELAY_LIMIT = 1900  # 이슈 댓글을 스레드로 옮길 때 한 메시지 최대 글자


# ───────────────────────── 상태 파일 ─────────────────────────

class State:
    """봇이 기억할 것: 이슈 ↔ 스레드 대응, #세력순위 메시지, #게임설명 메시지 목록."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {
            "issues": {},            # "이슈 번호" → {kind, channel_id, message_id, thread_id, closed, checking, votes}
            "ranking_message_id": 0,
            "ranking_mtime": 0.0,
            "guide_message_ids": [],
            "votes_day": "",         # 마지막으로 추천 수를 갱신한 날 (KST, YYYY-MM-DD)
            "weekly_sent": "",       # 주간 집계를 보낸 주 (그 주 월요일 날짜, KST)
        }
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except Exception:
            log.exception("상태 파일을 읽지 못했습니다: %s (빈 상태로 시작)", self.path)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            log.exception("상태 파일을 저장하지 못했습니다: %s", self.path)

    @property
    def issues(self) -> dict[str, dict[str, Any]]:
        return self.data["issues"]


# ───────────────────────── GitHub ─────────────────────────

class GitHub:
    API = "https://api.github.com"

    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "minetotalwar-discord-bot",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.session = aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=30))

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        assert self.session is not None
        async with self.session.request(method, f"{self.API}{path}", **kwargs) as response:
            if response.status == 404:
                return None
            if response.status >= 400:
                text = await response.text()
                raise RuntimeError(f"GitHub {method} {path} → {response.status}: {text[:300]}")
            if response.status == 204:
                return {}
            return await response.json()

    async def ensure_labels(self) -> None:
        for name, color in LABEL_COLORS.items():
            if await self._request("GET", f"/repos/{self.repo}/labels/{name}") is None:
                await self._request("POST", f"/repos/{self.repo}/labels", json={"name": name, "color": color})
                log.info("GitHub 라벨을 만들었습니다: %s", name)

    async def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        return await self._request("POST", f"/repos/{self.repo}/issues",
                                   json={"title": title, "body": body, "labels": labels})

    async def create_issue_in(self, repo: str, title: str, body: str) -> dict[str, Any]:
        """다른 저장소(상류)에 이슈 만들기 — 라벨은 붙이지 않는다(그쪽 라벨 체계를 모름)."""
        return await self._request("POST", f"/repos/{repo}/issues", json={"title": title, "body": body})

    async def list_issues(self, state: str = "open", labels: str | None = None, since: str | None = None) -> list[dict[str, Any]]:
        """이슈 목록 (풀 리퀘스트 제외)."""
        out: list[dict[str, Any]] = []
        params: dict[str, Any] = {"state": state, "per_page": 100}
        if labels:
            params["labels"] = labels
        if since:
            params["since"] = since
        page = 1
        while True:
            params["page"] = page
            batch = await self._request("GET", f"/repos/{self.repo}/issues", params=params) or []
            out += [issue for issue in batch if "pull_request" not in issue]
            if len(batch) < 100 or page >= 10:
                return out
            page += 1

    async def get_issue(self, number: int) -> dict[str, Any] | None:
        return await self._request("GET", f"/repos/{self.repo}/issues/{number}")

    async def update_issue_body(self, number: int, body: str) -> None:
        await self._request("PATCH", f"/repos/{self.repo}/issues/{number}", json={"body": body})

    async def list_comments(self, number: int) -> list[dict[str, Any]]:
        """이슈 댓글 전부 (오래된 순)."""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = await self._request("GET", f"/repos/{self.repo}/issues/{number}/comments",
                                        params={"per_page": 100, "page": page}) or []
            out += batch
            if len(batch) < 100:
                return out
            page += 1

    async def add_comment(self, number: int, body: str) -> dict[str, Any]:
        return await self._request("POST", f"/repos/{self.repo}/issues/{number}/comments", json={"body": body})


# ───────────────────────── 표 그리기 도우미 ─────────────────────────

def text_width(text: str) -> int:
    """고정폭 글꼴에서 차지하는 칸 수 (한글·이모지 = 2칸)."""
    width = 0
    for ch in text:
        if unicodedata.combining(ch) or ch in "️‍":
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(text: str, width: int, right: bool = False) -> str:
    space = " " * max(0, width - text_width(text))
    return space + text if right else text + space


def clip(text: str, width: int) -> str:
    out = ""
    for ch in text:
        if text_width(out + ch) > width:
            while out and text_width(out + "…") > width:
                out = out[:-1]
            return out.rstrip() + "…"
        out += ch
    return out


SQUARES = [
    ("🟥", (221, 46, 68)),
    ("🟧", (244, 144, 12)),
    ("🟨", (253, 203, 88)),
    ("🟩", (120, 177, 89)),
    ("🟦", (85, 172, 238)),
    ("🟪", (170, 142, 214)),
    ("🟫", (193, 105, 79)),
    ("⬛", (49, 55, 61)),
    ("⬜", (230, 231, 232)),
]


def color_square(hex_color: str | None) -> str:
    """세력 색 "#3366FF" → 가장 가까운 색 네모 이모지."""
    try:
        value = (hex_color or "").lstrip("#")
        r, g, b = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except (ValueError, IndexError):
        return "⬜"
    return min(SQUARES, key=lambda s: (s[1][0] - r) ** 2 + (s[1][1] - g) ** 2 + (s[1][2] - b) ** 2)[0]


def fmt_num(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:,.1f}" if value != int(value) else f"{int(value):,}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value if value is not None else "-")


def kst_text(iso: str | None, fallback_ts: float | None = None) -> str:
    moment = None
    if iso:
        try:
            moment = datetime.fromisoformat(iso)
        except ValueError:
            moment = None
    if moment is None:
        moment = datetime.fromtimestamp(fallback_ts or datetime.now().timestamp(), tz=KST)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=KST)
    return moment.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def sorted_factions(data: dict[str, Any]) -> list[dict[str, Any]]:
    factions = list(data.get("factions") or [])
    return sorted(factions, key=lambda f: (-(f.get("score") or 0), -(f.get("power") or 0)))


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        log.exception("JSON 을 읽지 못했습니다: %s", path)
        return None


def render_ranking(data: dict[str, Any], mtime: float) -> str:
    """#세력순위 고정 메시지 본문 (코드 블록 표)."""
    headers = ["순위", "세력", "시즌점수", "경제력", "전투력", "청크", "병력", "재위"]
    rows: list[list[str]] = []
    for rank, faction in enumerate(sorted_factions(data), start=1):
        name = clip(str(faction.get("name") or "?"), 12)
        reign = f"{faction.get('reign_days') or 0}일"
        if faction.get("king"):
            reign += "(왕)"
        rows.append([
            str(rank),
            f"{color_square(faction.get('color'))} {name}",
            fmt_num(faction.get("score") or 0),
            fmt_num(faction.get("economy") or 0),
            fmt_num(faction.get("power") or 0),
            fmt_num(faction.get("chunks") or 0),
            fmt_num(faction.get("troops") or 0),
            reign,
        ])
    right = {0, 2, 3, 4, 5, 6, 7}

    season = data.get("season") or {}
    title = f"**세력 순위** · 시즌 {season.get('number', '?')}"
    if season.get("end_day") is not None and season.get("day") is not None:
        title += f" (종료까지 {max(0, season['end_day'] - season['day'])}일)"
    footer = f"게임 {data.get('game_day', '?')}일 · 갱신 {kst_text(data.get('generated_at'), mtime)} (KST)"

    if not rows:
        return f"{title}\n아직 세력이 없습니다.\n{footer}"

    # 글자 수 한도 안에 들어가는 줄까지만
    while True:
        widths = [max(text_width(r[i]) for r in [headers] + rows) for i in range(len(headers))]

        def line(cells: list[str]) -> str:
            return " ".join(pad(c, widths[i], right=i in right) for i, c in enumerate(cells)).rstrip()

        table = [line(headers), "-" * text_width(line(headers))] + [line(r) for r in rows]
        extra = "" if len(rows) == len(sorted_factions(data)) else f"\n(상위 {len(rows)}개 세력만 표시)"
        content = f"{title}\n```\n" + "\n".join(table) + f"\n```{extra}\n{footer}"
        if len(content) <= DISCORD_LIMIT - 50 or len(rows) <= 1:
            return content
        rows = rows[:-1]


# ───────────────────────── 가이드 나누기 ─────────────────────────

def split_guide(text: str) -> list[str]:
    """맨 위 소개 + `## ` 절마다 하나. 2,000자가 넘는 절은 문단 단위로 다시 자른다."""
    chunks: list[str] = []
    current: list[str] = []
    for raw in text.splitlines():
        if raw.startswith("## ") and current:
            chunks.append("\n".join(current).strip())
            current = []
        current.append(raw)
    if current:
        chunks.append("\n".join(current).strip())

    out: list[str] = []
    for chunk in filter(None, chunks):
        if len(chunk) <= DISCORD_LIMIT:
            out.append(chunk)
            continue
        part = ""
        for paragraph in chunk.split("\n\n"):
            candidate = f"{part}\n\n{paragraph}" if part else paragraph
            if len(candidate) > DISCORD_LIMIT and part:
                out.append(part)
                part = paragraph
            else:
                part = candidate
        while len(part) > DISCORD_LIMIT:
            out.append(part[:DISCORD_LIMIT])
            part = part[DISCORD_LIMIT:]
        if part:
            out.append(part)
    return out


# ───────────────────────── 봇 ─────────────────────────

class MtwBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # 개발자 포털에서 Message Content Intent 를 켜야 한다
        intents.guild_messages = True
        intents.guild_reactions = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)
        self.state = State(STATE_PATH)
        self.github = GitHub(GITHUB_TOKEN, GITHUB_REPO)
        self.ranking_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        await self.github.start()
        if GITHUB_TOKEN:
            try:
                await self.github.ensure_labels()
            except Exception:
                log.exception("GitHub 라벨 확인 실패 (계속 진행)")
        else:
            log.warning("GITHUB_TOKEN 이 없어 이슈 기능이 꺼집니다.")

        register_commands(self)
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("슬래시 명령 %d개를 길드 %s 에 동기화했습니다.", len(synced), GUILD_ID)
        else:
            log.warning("GUILD_ID 가 없어 슬래시 명령을 동기화하지 않습니다.")

        self.poll_issues.start()
        self.update_votes.start()
        self.update_ranking.start()
        self.weekly_report.start()

    async def close(self) -> None:
        await self.github.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("로그인: %s (id %s)", self.user, self.user.id if self.user else "?")

    async def fetch_channel_safe(self, channel_id: int) -> Any:
        if not channel_id:
            return None
        channel = self.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden):
            return None

    # ── 제보·건의 받기 ──

    async def on_message(self, message: discord.Message) -> None:
        try:
            if message.author.bot or message.guild is None:
                return
            if message.type not in (discord.MessageType.default, discord.MessageType.reply):
                return
            if isinstance(message.channel, discord.Thread):
                await self.relay_reply(message)
            elif message.channel.id == CHANNEL_BUG and CHANNEL_BUG:
                await self.intake(message, "bug")
            elif message.channel.id == CHANNEL_SUGGEST and CHANNEL_SUGGEST:
                await self.intake(message, "suggest")
        except Exception:
            log.exception("on_message 처리 중 오류")

    def entry_for_thread(self, thread_id: int) -> tuple[int, dict[str, Any]] | None:
        for key, entry in self.state.issues.items():
            if entry.get("thread_id") == thread_id:
                return int(key), entry
        return None

    async def relay_reply(self, message: discord.Message) -> None:
        """대응된 스레드의 사람 답글 → 이슈 댓글."""
        found = self.entry_for_thread(message.channel.id)
        if found is None or not GITHUB_TOKEN:
            return
        number, _ = found
        lines = [f"**{message.author.display_name}**: {message.content.strip() or '(본문 없음)'}"]
        for attachment in message.attachments:
            if (attachment.content_type or "").startswith("image/"):
                lines.append(f"![{attachment.filename}]({attachment.url})")
            else:
                lines.append(f"- [{attachment.filename}]({attachment.url})")
        lines += ["", RELAY_MARKER]
        await self.github.add_comment(number, "\n".join(lines))
        log.info("스레드 답글 → 이슈 #%d 댓글 (%s)", number, message.author)

    async def intake(self, message: discord.Message, kind: str) -> None:
        label = LABEL_BUG if kind == "bug" else LABEL_SUGGEST
        content = message.content.strip()
        first_line = next((l.strip() for l in content.splitlines() if l.strip()), "")
        title_text = first_line or ("첨부 파일 제보" if kind == "bug" else "첨부 파일 건의")
        title = clip(f"[{label}] {title_text}", 120)

        thread = await message.create_thread(name=clip(title_text, 90), auto_archive_duration=10080)

        if not GITHUB_TOKEN:
            await thread.send("접수했지만 이슈 연결이 설정되지 않았습니다. 관리자가 확인할게요.")
            return

        lines: list[str] = []
        if kind == "suggest":
            lines += ["추천 0", ""]
        lines.append(content or "(본문 없음)")
        if message.attachments:
            lines += ["", "### 첨부"]
            for attachment in message.attachments:
                if (attachment.content_type or "").startswith("image/"):
                    lines.append(f"![{attachment.filename}]({attachment.url})")
                else:
                    lines.append(f"- [{attachment.filename}]({attachment.url})")
        lines += [
            "",
            "---",
            f"디스코드 작성자: {message.author.display_name} ({message.author.id})",
            f"원문: {message.jump_url}",
        ]

        try:
            issue = await self.github.create_issue(title, "\n".join(lines), [label])
        except Exception:
            log.exception("이슈 만들기 실패")
            await thread.send("접수 중 문제가 생겼습니다. 관리자가 확인할게요.")
            return

        number = issue["number"]
        self.state.issues[str(number)] = {
            "kind": kind,
            "channel_id": message.channel.id,
            "message_id": message.id,
            "thread_id": thread.id,
            "closed": False,
            "checking": False,
            "on_hold": False,
            "seen_comments": 0,
            "last_comment_id": 0,
            "votes": 0,
        }
        self.state.save()
        await thread.send(f"접수 #{number} — 자동 처리 중" if kind == "bug" and BUG_MODE == "auto" else f"접수 #{number}")
        try:
            await thread.edit(name=clip(f"#{number} {title_text}", 100))
        except discord.HTTPException:
            pass
        log.info("%s 접수 #%d (%s)", label, number, message.author)

    # ── 이슈 상태 → 스레드 알림 (5분마다) ──

    @tasks.loop(minutes=5)
    async def poll_issues(self) -> None:
        if not GITHUB_TOKEN:
            return
        for key, entry in list(self.state.issues.items()):
            try:
                await self.check_issue(int(key), entry)
            except Exception:
                log.exception("이슈 #%s 확인 중 오류", key)
            await asyncio.sleep(0.5)

    async def check_issue(self, number: int, entry: dict[str, Any]) -> None:
        issue = await self.github.get_issue(number)
        if issue is None:
            return
        labels = [label.get("name", "") for label in issue.get("labels", [])]
        closed = issue.get("state") == "closed"
        changed = False

        # 1) 새 이슈 댓글 → 스레드 (댓글 수가 늘었을 때만 목록을 받는다)
        comments: list[dict[str, Any]] | None = None
        total = issue.get("comments", 0) or 0
        if "last_comment_id" not in entry:
            # 이 기능 이전에 접수된 이슈: 지금까지 댓글은 건너뛰고 앞으로 달리는 것만 옮긴다
            comments = await self.github.list_comments(number) if total else []
            entry["last_comment_id"] = max((c["id"] for c in comments), default=0)
            entry["seen_comments"] = total
            changed = True
        elif total > entry.get("seen_comments", 0):
            comments = await self.github.list_comments(number)
            for comment in comments:
                if comment["id"] <= entry.get("last_comment_id", 0):
                    continue
                if RELAY_MARKER not in (comment.get("body") or ""):
                    # 이번에 막 닫힌 이슈면 잠금은 아래 닫힘 알림 때 한 번만
                    await self.thread_send(entry, relay_text(comment), closed and entry.get("closed", False))
                entry["last_comment_id"] = comment["id"]
                self.state.save()  # 한 개씩 저장해 중간에 실패해도 중복으로 올리지 않는다
            entry["seen_comments"] = total
            changed = True

        # 상류 라벨 → 상류 저장소로 넘기기 (한 번만)
        if LABEL_UPSTREAM in labels and UPSTREAM_REPO and not entry.get("upstream_url"):
            await self.send_upstream(number, entry, issue)
            changed = True

        # 2) 라벨 알림 (확인중·보류)
        for label, (key, text) in LABEL_NOTICES.items():
            if label in labels and not entry.get(key) and not closed:
                await self.thread_send(entry, text)
                entry[key] = True
                changed = True
            elif label not in labels and entry.get(key):
                entry[key] = False
                changed = True

        # 3) 닫힘 → 알림 + 스레드 잠금·보관 / 다시 열림 → 잠금 해제
        if closed and not entry.get("closed"):
            version = next((m.group(0) for name in labels if (m := VERSION_RE.search(name))), None)
            if version is None:
                if comments is None:
                    comments = await self.github.list_comments(number) if total else []
                for comment in reversed(comments):
                    if m := VERSION_RE.search(comment.get("body") or ""):
                        version = m.group(0)
                        break
            if issue.get("state_reason") == "not_planned":
                text = "처리됨 (반영하지 않기로 했습니다)"
            elif version:
                text = f"수정됨 {version}"
            else:
                text = "처리됨"
            await self.thread_send(entry, text, closed=True)
            entry["closed"] = True
            changed = True
        elif not closed and entry.get("closed"):
            # 다시 열린 이슈: 스레드를 풀고, 다음에 닫힐 때 또 알린다
            thread = await self.fetch_channel_safe(entry.get("thread_id", 0))
            if isinstance(thread, discord.Thread):
                await thread.edit(archived=False, locked=False)
            entry["closed"] = False
            changed = True

        if changed:
            self.state.save()

    async def send_upstream(self, number: int, entry: dict[str, Any], issue: dict[str, Any] | None = None) -> str:
        """서버 이슈를 상류 저장소에 같은 제목·본문으로 연다. 이미 넘겼으면 그 주소를 돌려준다.
        상류는 공개 저장소이므로 본문의 디스코드 작성자·원문 링크 꼬리와 "추천 N" 줄은 빼고 보낸다."""
        if entry.get("upstream_url"):
            return entry["upstream_url"]
        issue = issue or await self.github.get_issue(number)
        if issue is None:
            raise RuntimeError(f"이슈 #{number} 를 찾지 못했습니다.")
        body = issue.get("body") or ""
        body = body.split("\n---\n디스코드 작성자:")[0].rstrip()
        lines = body.split("\n")
        if lines and VOTE_LINE_RE.match(lines[0]):
            body = "\n".join(lines[1:]).lstrip()
        body += f"\n\n---\n원 제보: {issue.get('html_url', '')}"
        created = await self.github.create_issue_in(UPSTREAM_REPO, issue.get("title") or f"#{number}", body)
        entry["upstream_url"] = created.get("html_url", "")
        entry["upstream_number"] = created.get("number")
        self.state.save()
        await self.thread_send(entry, f"상류 저장소에 올렸습니다: {UPSTREAM_REPO}#{created.get('number')} <{entry['upstream_url']}>",
                               closed=bool(entry.get("closed")))
        # 서버 이슈에도 링크를 남긴다 (표식이 있어 스레드로 다시 오지 않음)
        await self.github.add_comment(number, f"상류 이슈: {entry['upstream_url']}\n\n{RELAY_MARKER}")
        log.info("이슈 #%d → 상류 %s", number, entry["upstream_url"])
        return entry["upstream_url"]

    # ── 주간 집계 (#서버공지 웹훅, 월요일) ──

    @tasks.loop(hours=1)
    async def weekly_report(self) -> None:
        if not NOTICE_WEBHOOK or not GITHUB_TOKEN:
            return
        now = datetime.now(KST)
        if now.weekday() != 0:
            return
        this_monday = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if self.state.data.get("weekly_sent") == this_monday.strftime("%Y-%m-%d"):
            return
        try:
            text = await self.weekly_text(this_monday)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.post(NOTICE_WEBHOOK, json={"content": text, "allowed_mentions": {"parse": []}}) as response:
                    if response.status >= 400:
                        raise RuntimeError(f"웹훅 {response.status}: {(await response.text())[:200]}")
            self.state.data["weekly_sent"] = this_monday.strftime("%Y-%m-%d")
            self.state.save()
            log.info("주간 집계를 보냈습니다: %s", text)
        except Exception:
            log.exception("주간 집계 실패 (한 시간 뒤 다시 시도)")

    async def weekly_text(self, this_monday: datetime) -> str:
        """지난 주 월~일(KST)에 열린 제보·닫힌 제보 수."""
        start = this_monday - timedelta(days=7)
        since = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        opened = handled = 0
        for issue in await self.github.list_issues(state="all", since=since):
            names = {label.get("name") for label in issue.get("labels", [])}
            if not names & {LABEL_BUG, LABEL_SUGGEST}:
                continue
            if in_range(issue.get("created_at"), start, this_monday):
                opened += 1
            if issue.get("state") == "closed" and in_range(issue.get("closed_at"), start, this_monday):
                handled += 1
        last_sunday = this_monday - timedelta(days=1)
        return f"📋 이번 주 제보 ({start.month}/{start.day}~{last_sunday.month}/{last_sunday.day}): 열린 제보 {opened}건 · 처리 {handled}건"

    # ── /제보 목록 ──

    async def report_list(self, kind: str | None) -> str:
        kinds = [kind] if kind else [LABEL_BUG, LABEL_SUGGEST]
        sections: list[list[str]] = []
        now = datetime.now(timezone.utc)
        by_number = self.state.issues
        for label in kinds:
            issues = await self.github.list_issues(state="open", labels=label)
            rows: list[tuple[int, str, str]] = []
            for issue in issues:
                votes = 0
                if label == LABEL_SUGGEST:
                    first = (issue.get("body") or "").split("\n", 1)[0]
                    if VOTE_LINE_RE.match(first):
                        votes = int(first.split()[1])
                else:
                    entry = by_number.get(str(issue["number"]))
                    if entry:
                        votes = await self.discord_votes(entry)
                created = parse_time(issue.get("created_at")) or now
                days = max(0, (now - created).days)
                extra = [n for n in (l.get("name", "") for l in issue.get("labels", [])) if n not in (LABEL_BUG, LABEL_SUGGEST)]
                line = f"#{issue['number']} {clip(issue.get('title') or '', 50)} · 👍{votes} · " + \
                       ("오늘" if days == 0 else f"{days}일 전") + (f" · {', '.join(extra)}" if extra else "")
                rows.append((votes, issue.get("created_at") or "", line))
            # 추천 많은 순, 같으면 오래된 것부터
            rows.sort(key=lambda r: (-r[0], r[1]))
            sections.append([f"**열린 {label} {len(rows)}건**"] + ([r[2] for r in rows] or ["(없음)"]))

        out: list[str] = []
        omitted = 0
        for section in sections:
            for index, line in enumerate(section):
                candidate = "\n".join(out + [line])
                if len(candidate) > RELAY_LIMIT - 20 and index > 0:
                    omitted += len(section) - index
                    break
                out.append(line)
            out.append("")
        text = "\n".join(out).strip()
        if omitted:
            text += f"\n… 외 {omitted}건 (GitHub 이슈 탭에서 확인)"
        return text

    async def discord_votes(self, entry: dict[str, Any]) -> int:
        channel = await self.fetch_channel_safe(entry.get("channel_id", 0))
        if channel is None:
            return 0
        try:
            message = await channel.fetch_message(entry["message_id"])
        except discord.HTTPException:
            return 0
        return next((r.count for r in message.reactions if str(r.emoji) == THUMBS_UP), 0)

    async def thread_send(self, entry: dict[str, Any], text: str, closed: bool = False) -> None:
        """스레드에 글을 올린다. 보관·잠긴 스레드는 잠깐 풀고, closed 면 올린 뒤 다시 잠금·보관한다.
        보내기 실패는 예외로 올라가 다음 주기에 다시 시도한다."""
        thread = await self.fetch_channel_safe(entry.get("thread_id", 0))
        if not isinstance(thread, discord.Thread):
            log.warning("스레드 %s 를 찾지 못했습니다 (알림 건너뜀).", entry.get("thread_id"))
            return
        if thread.archived or thread.locked:
            thread = await thread.edit(archived=False, locked=False)
        await thread.send(text)
        if closed:
            await thread.edit(archived=True, locked=True)

    # ── 건의 추천 수 (하루 한 번) ──

    @tasks.loop(minutes=30)
    async def update_votes(self) -> None:
        if not GITHUB_TOKEN:
            return
        today = datetime.now(KST).strftime("%Y-%m-%d")
        if self.state.data.get("votes_day") == today:
            return
        for key, entry in list(self.state.issues.items()):
            if entry.get("kind") != "suggest" or entry.get("closed"):
                continue
            try:
                await self.update_vote(int(key), entry)
            except Exception:
                log.exception("건의 #%s 추천 수 갱신 중 오류", key)
            await asyncio.sleep(0.5)
        self.state.data["votes_day"] = today
        self.state.save()

    async def update_vote(self, number: int, entry: dict[str, Any]) -> None:
        channel = await self.fetch_channel_safe(entry.get("channel_id", 0))
        if channel is None:
            return
        try:
            message = await channel.fetch_message(entry["message_id"])
        except discord.NotFound:
            return
        votes = next((r.count for r in message.reactions if str(r.emoji) == THUMBS_UP), 0)
        if votes == entry.get("votes", 0):
            return
        issue = await self.github.get_issue(number)
        if issue is None:
            return
        body = issue.get("body") or ""
        lines = body.split("\n")
        if lines and VOTE_LINE_RE.match(lines[0]):
            lines[0] = f"추천 {votes}"
        else:
            lines = [f"추천 {votes}", ""] + lines
        await self.github.update_issue_body(number, "\n".join(lines))
        entry["votes"] = votes
        log.info("건의 #%d 추천 %d", number, votes)

    # ── #세력순위 (2분마다 파일 변경 확인) ──

    @tasks.loop(minutes=2)
    async def update_ranking(self) -> None:
        try:
            await self.refresh_ranking()
        except Exception:
            log.exception("세력 순위 갱신 중 오류")

    async def refresh_ranking(self, force: bool = False) -> None:
        if not CHANNEL_RANKING:
            return
        path = EXPORT_DIR / "factions.json"
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return
        async with self.ranking_lock:
            if not force and mtime == self.state.data.get("ranking_mtime"):
                return
            data = read_json(path)
            if data is None:
                return
            content = render_ranking(data, mtime)
            channel = await self.fetch_channel_safe(CHANNEL_RANKING)
            if channel is None:
                log.warning("#세력순위 채널을 찾지 못했습니다.")
                return
            message_id = self.state.data.get("ranking_message_id") or 0
            message = None
            if message_id:
                try:
                    message = await channel.fetch_message(message_id)
                except discord.NotFound:
                    message = None
            if message is None:
                message = await channel.send(content)
                self.state.data["ranking_message_id"] = message.id
            else:
                await message.edit(content=content)
            self.state.data["ranking_mtime"] = mtime
            self.state.save()
            log.info("세력 순위를 갱신했습니다 (%d개 세력)", len(data.get("factions") or []))

    @poll_issues.before_loop
    @update_votes.before_loop
    @update_ranking.before_loop
    @weekly_report.before_loop
    async def _wait_ready(self) -> None:
        await self.wait_until_ready()

    # ── #게임설명 ──

    async def publish_guide(self) -> str:
        if not CHANNEL_GUIDE:
            return "CHANNEL_GUIDE 가 설정되지 않았습니다."
        try:
            text = GUIDE_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            return f"가이드 파일이 없습니다: {GUIDE_PATH}"
        chunks = split_guide(text)
        if UPSTREAM_REPO and chunks:
            line = f"모드 버그는 여기로: https://github.com/{UPSTREAM_REPO}/issues"
            if len(chunks[-1]) + len(line) + 2 <= DISCORD_LIMIT:
                chunks[-1] += "\n\n" + line
            else:
                chunks.append(line)
        channel = await self.fetch_channel_safe(CHANNEL_GUIDE)
        if channel is None:
            return "#게임설명 채널을 찾지 못했습니다."

        old_ids: list[int] = list(self.state.data.get("guide_message_ids") or [])
        old_messages: list[discord.Message] = []
        for message_id in old_ids:
            try:
                old_messages.append(await channel.fetch_message(message_id))
            except discord.NotFound:
                old_messages = []  # 하나라도 사라졌으면 순서가 깨지므로 전부 다시 올린다
                break

        if old_ids and len(old_messages) != len(old_ids):
            for message_id in old_ids:
                try:
                    await (await channel.fetch_message(message_id)).delete()
                except discord.HTTPException:
                    pass
            old_messages = []

        new_ids: list[int] = []
        edited = sent = deleted = 0
        for index, chunk in enumerate(chunks):
            if index < len(old_messages):
                if old_messages[index].content != chunk:
                    await old_messages[index].edit(content=chunk, suppress=True)
                    edited += 1
                new_ids.append(old_messages[index].id)
            else:
                message = await channel.send(chunk, suppress_embeds=True)
                new_ids.append(message.id)
                sent += 1
        for message in old_messages[len(chunks):]:
            try:
                await message.delete()
                deleted += 1
            except discord.HTTPException:
                pass

        self.state.data["guide_message_ids"] = new_ids
        self.state.save()
        return f"가이드 {len(chunks)}개 메시지 — 편집 {edited} · 새로 올림 {sent} · 삭제 {deleted}"


# ───────────────────────── 슬래시 명령 ─────────────────────────

def relay_text(comment: dict[str, Any]) -> str:
    """GitHub 댓글 → 스레드 메시지. 길면 자르고 댓글 링크를 붙인다."""
    author = (comment.get("user") or {}).get("login", "?")
    body = (comment.get("body") or "").strip()
    text = f"GitHub 댓글 ({author}): {body}"
    if len(text) > RELAY_LIMIT:
        link = f"\n… (전체: <{comment.get('html_url', '')}>)"
        text = text[:RELAY_LIMIT - len(link)].rstrip() + link
    return text


def parse_time(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def in_range(iso: str | None, start: datetime, end: datetime) -> bool:
    moment = parse_time(iso)
    return moment is not None and start <= moment < end


def status_text() -> str:
    path = EXPORT_DIR / "status.json"
    try:
        age = datetime.now().timestamp() - path.stat().st_mtime
    except FileNotFoundError:
        return "서버가 꺼져 있거나 응답 없음"
    data = read_json(path)
    if data is None or age > STATUS_STALE_SECONDS:
        return "서버가 꺼져 있거나 응답 없음"

    online = list(data.get("online") or [])
    lines = [f"**접속 {len(online)}/{data.get('max_players', '?')}명**" + (f": {', '.join(online)}" if online else "")]
    mspt = data.get("mspt")
    if isinstance(mspt, (int, float)):
        tps = min(20.0, 1000.0 / mspt) if mspt > 0 else 20.0
        lines.append(f"틱 속도: {mspt:.1f} ms (TPS {tps:.1f})")
    lines.append(f"게임 {data.get('game_day', '?')}일 {data.get('game_time') or ''}".rstrip())
    season = data.get("season") or {}
    if season:
        left = (season.get("end_day") or 0) - (season.get("day") or 0)
        lines.append(f"시즌 {season.get('number', '?')} · D-{max(0, left)}")
    invasion = data.get("invasion")
    if invasion:
        lines.append(f"침공: {invasion.get('theme', '?')} 침공군 {invasion.get('size', '?')}명 → {invasion.get('target', '?')}")
    else:
        lines.append("침공: 없음")
    lines.append(f"요새: {data.get('fortresses', 0)}곳")
    lines.append(f"-# 갱신 {kst_text(data.get('generated_at'), path.stat().st_mtime)} (KST)")
    return "\n".join(lines)


def find_faction(factions: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    query = query.strip().lower()
    for faction in factions:
        if str(faction.get("name", "")).lower() == query:
            return faction
    for faction in factions:
        if query in str(faction.get("name", "")).lower():
            return faction
    return None


def score_text(query: str | None) -> str:
    path = EXPORT_DIR / "factions.json"
    data = read_json(path)
    if data is None:
        return "점수 정보가 아직 없습니다."
    factions = sorted_factions(data)
    if not factions:
        return "아직 세력이 없습니다."

    season = data.get("season") or {}
    lines = [f"**시즌 {season.get('number', '?')} 점수판** (게임 {data.get('game_day', '?')}일)"]
    for rank, faction in enumerate(factions[:5], start=1):
        king = " 👑" if faction.get("king") else ""
        lines.append(f"{rank}. {color_square(faction.get('color'))} **{faction.get('name')}**{king} — {fmt_num(faction.get('score') or 0)}점")

    if query:
        faction = find_faction(factions, query)
        if faction is None:
            lines += ["", f"'{query}' 세력을 찾지 못했습니다."]
        else:
            rank = factions.index(faction) + 1
            parts = faction.get("score_parts") or {}
            lines += [
                "",
                f"**{faction.get('name')}** — {rank}위 · {fmt_num(faction.get('score') or 0)}점",
                f"보유 {fmt_num(parts.get('holding', 0))} · 토벌 {fmt_num(parts.get('hunt', 0))} · "
                f"전쟁 {fmt_num(parts.get('war', 0))} · 재위 {fmt_num(parts.get('reign', 0))}",
                f"세력장 {faction.get('leader', '?')} · 세력원 {len(faction.get('members') or [])}명",
            ]
    lines.append(f"-# 갱신 {kst_text(data.get('generated_at'), path.stat().st_mtime)} (KST)")
    return "\n".join(lines)


def register_commands(bot: MtwBot) -> None:
    tree = bot.tree

    @tree.command(name="서버상태", description="접속 인원, 틱 속도, 게임 날짜, 침공·요새 현황")
    async def server_status(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(status_text())

    async def faction_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        data = read_json(EXPORT_DIR / "factions.json") or {}
        names = [str(f.get("name", "")) for f in sorted_factions(data)]
        current = current.lower()
        return [app_commands.Choice(name=n, value=n) for n in names if current in n.lower()][:25]

    @tree.command(name="점수", description="시즌 점수판 상위 5 세력 (세력 이름을 주면 점수 구성까지)")
    @app_commands.describe(세력="점수 구성을 볼 세력 이름")
    @app_commands.autocomplete(세력=faction_autocomplete)
    async def score(interaction: discord.Interaction, 세력: str | None = None) -> None:
        await interaction.response.send_message(score_text(세력))

    guide = app_commands.Group(name="가이드", description="#게임설명 채널 관리",
                               default_permissions=discord.Permissions(administrator=True), guild_only=True)

    @guide.command(name="갱신", description="docs/가이드.md 로 #게임설명 채널 메시지를 다시 올리거나 편집 (관리자)")
    async def guide_refresh(interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.guild_permissions.administrator:
            await interaction.response.send_message("관리자만 쓸 수 있습니다.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await bot.publish_guide()
        except Exception as error:
            log.exception("가이드 갱신 중 오류")
            result = f"가이드 갱신 실패: {error}"
        await interaction.followup.send(result, ephemeral=True)

    tree.add_command(guide)

    async def require_admin(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if isinstance(member, discord.Member) and member.guild_permissions.administrator:
            return True
        await interaction.response.send_message("관리자만 쓸 수 있습니다.", ephemeral=True)
        return False

    report = app_commands.Group(name="제보", description="버그·건의 이슈 정리 (관리자)",
                                default_permissions=discord.Permissions(administrator=True), guild_only=True)

    @report.command(name="목록", description="열린 버그·건의 이슈를 추천 수·날짜순으로 요약")
    @app_commands.describe(종류="버그 또는 건의 (비우면 둘 다)")
    @app_commands.choices(종류=[app_commands.Choice(name=LABEL_BUG, value=LABEL_BUG),
                              app_commands.Choice(name=LABEL_SUGGEST, value=LABEL_SUGGEST)])
    async def report_list(interaction: discord.Interaction, 종류: app_commands.Choice[str] | None = None) -> None:
        if not await require_admin(interaction):
            return
        if not GITHUB_TOKEN:
            await interaction.response.send_message("GITHUB_TOKEN 이 설정되지 않았습니다.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = await bot.report_list(종류.value if 종류 else None)
        except Exception as error:
            log.exception("제보 목록 오류")
            text = f"목록을 만들지 못했습니다: {error}"
        await interaction.followup.send(text, ephemeral=True)

    tree.add_command(report)

    @tree.command(name="상류", description="이 스레드의 이슈를 모드 제작자 공개 저장소에도 올림 (관리자)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def upstream(interaction: discord.Interaction) -> None:
        if not await require_admin(interaction):
            return
        if not UPSTREAM_REPO or not GITHUB_TOKEN:
            await interaction.response.send_message("UPSTREAM_REPO 또는 GITHUB_TOKEN 이 설정되지 않았습니다.", ephemeral=True)
            return
        found = bot.entry_for_thread(interaction.channel_id or 0)
        if found is None:
            await interaction.response.send_message("제보 스레드 안에서만 쓸 수 있습니다.", ephemeral=True)
            return
        number, entry = found
        if entry.get("upstream_url"):
            await interaction.response.send_message(f"이미 올렸습니다: <{entry['upstream_url']}>", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            url = await bot.send_upstream(number, entry)
            text = f"상류에 올렸습니다: <{url}>"
        except Exception as error:
            log.exception("상류 올리기 오류")
            text = f"상류에 올리지 못했습니다: {error}"
        await interaction.followup.send(text, ephemeral=True)

    @tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.exception("슬래시 명령 오류", exc_info=error)
        text = "명령을 처리하지 못했습니다."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


def main() -> None:
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN 이 없습니다. .env 를 확인하세요.")
        sys.exit(1)
    log.info("export 폴더: %s · 가이드: %s · 상태 파일: %s", EXPORT_DIR, GUIDE_PATH, STATE_PATH)
    MtwBot().run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
