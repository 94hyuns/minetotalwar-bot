"""정식 오픈 전 디스코드 정리 (docs/운영-오픈체크리스트.md 1단계). 운영 서버 봇 폴더에서 봇을 끈 상태로 실행한다.

    cd ~/minetotalwar-bot && .venv/bin/python reset_discord.py           # 지울 것만 보여 줌 (기본)
    cd ~/minetotalwar-bot && .venv/bin/python reset_discord.py --yes     # 실제로 지움

지우는 것
- #서버공지(NOTICE_WEBHOOK 의 채널)·게임 소식 채널(모드 설정 webhook.events 의 채널): 메시지 전부
- #세력순위: 메시지 전부 (봇이 다시 켜지면 순위 메시지를 새로 올림)
- #버그제보·#건의사항: 메시지 전부 + 그 채널의 스레드 전부(비공개 제보 스레드 포함)
- 봇 state.json → state.json.before-reset 으로 옮기고 새로 시작 (스레드↔이슈 대응·순위 메시지 id). #게임설명 메시지 id 만 새 파일에 남긴다
- 모드 export/notice_state.json (서버 상태 메시지 id — 재시작 때 새로 올림)
남기는 것: #게임설명(봇의 /가이드 갱신 으로 새로 씀), GitHub 이슈
봇에 해당 채널의 메시지 관리·스레드 관리 권한이 있어야 남의 글·스레드를 지울 수 있다(없으면 실패로 표시).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD = os.environ["GUILD_ID"]
API = "https://discord.com/api/v10"
MOD_DIR = Path(os.environ.get("MOD_DIR", str(Path.home() / "minetotalwar")))
DO_IT = "--yes" in sys.argv


def call(method: str, path: str, auth: bool = True):
    headers = {"User-Agent": "DiscordBot (minetotalwar reset, 1)"}
    if auth:
        headers["Authorization"] = "Bot " + TOKEN
    url = path if path.startswith("http") else API + path
    while True:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method=method, headers=headers), timeout=20) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(json.loads(e.read() or b"{}").get("retry_after", 1)) + 0.2)
                continue
            raise


def webhook_channel(url: str) -> str | None:
    url = url.strip().replace("\\:", ":")
    return call("GET", url, auth=False)["channel_id"] if url else None


def mod_property(key: str) -> str:
    file = MOD_DIR / "config" / "minetotalwar-discord.properties"
    if not file.exists():
        return ""
    for line in file.read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    return ""


def messages(channel: str) -> list[dict]:
    out, before = [], ""
    while True:
        batch = call("GET", f"/channels/{channel}/messages?limit=100{before}")
        out += batch
        if len(batch) < 100:
            return out
        before = "&before=" + batch[-1]["id"]


def threads(channel: str) -> list[dict]:
    found = {t["id"]: t for t in call("GET", f"/guilds/{GUILD}/threads/active")["threads"] if t.get("parent_id") == channel}
    for kind in ("public", "private"):
        try:
            for t in call("GET", f"/channels/{channel}/threads/archived/{kind}?limit=100")["threads"]:
                found[t["id"]] = t
        except urllib.error.HTTPError as e:
            print(f"  (보관된 {kind} 스레드 목록 실패 {e.code})")
    return list(found.values())


def remove(path: str, label: str) -> None:
    if not DO_IT:
        print("  지울 것:", label)
        return
    try:
        call("DELETE", path)
        print("  지움:", label)
    except urllib.error.HTTPError as e:
        print(f"  실패 {e.code}:", label)
    time.sleep(0.3)


channels = {
    "#서버공지": webhook_channel(os.environ.get("NOTICE_WEBHOOK", "")),
    # 모드 설정은 쉼표로 여러 주소(다중 길드)일 수 있다 — 첫 길드 것만 (추가 길드 채널은 이 스크립트가 다루지 않음)
    "게임 소식": webhook_channel(mod_property("webhook.events").split(",")[0]),
    "#세력순위": os.environ.get("CHANNEL_RANKING"),
    "#버그제보": os.environ.get("CHANNEL_BUG"),
    "#건의사항": os.environ.get("CHANNEL_SUGGEST"),
}
for name, channel in channels.items():
    if not channel:
        print(f"{name}: 설정 없음, 건너뜀")
        continue
    if name in ("#버그제보", "#건의사항"):
        found = threads(channel)
        print(f"{name}: 스레드 {len(found)}개")
        for t in found:
            remove(f"/channels/{t['id']}", f"스레드 {t['name']}")
    found = messages(channel)
    print(f"{name}: 메시지 {len(found)}개")
    for m in found:
        text = (m["embeds"][0].get("title") if m.get("embeds") else "") or m.get("content", "")
        remove(f"/channels/{channel}/messages/{m['id']}", f"{m['timestamp'][:16]} {m['author']['username']}: {text[:50]!r}")

for file, label in ((BASE / "state.json", "봇 state.json"), (MOD_DIR / "export" / "notice_state.json", "서버 상태 메시지 id")):
    if not file.exists():
        continue
    # #게임설명 은 남기므로 그 메시지 id 는 새 state.json 에 그대로 둔다 (없으면 /가이드 갱신 때 설명이 두 벌이 됨)
    keep = {}
    if file.name == "state.json":
        old = json.loads(file.read_text(encoding="utf-8"))
        guide = old.get("guide_message_ids") or []
        if guide:
            keep = {"guide_message_ids": guide}
        # 추가 길드(GUILDS_JSON)의 #게임설명 메시지 id 도 남긴다
        others = {gid: {"guide_message_ids": g["guide_message_ids"]}
                  for gid, g in (old.get("guilds") or {}).items() if isinstance(g, dict) and g.get("guide_message_ids")}
        if others:
            keep["guilds"] = others
    if DO_IT:
        file.replace(file.with_name(file.name + ".before-reset"))
        if keep:
            file.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
        print(f"{label}: {file.name}.before-reset 으로 옮김" + (" (가이드 메시지 id 는 유지)" if keep else ""))
    else:
        print(f"{label}: {file} 를 옮길 예정" + (f" (가이드 메시지 id {len(guide)}개는 유지)" if keep else ""))

print("완료" if DO_IT else "\n미리보기만 했습니다. 실제로 지우려면 --yes")
