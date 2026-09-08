#!/usr/bin/env python3
"""Выгрузка переписки из Telegram в sources/ проекта.

Ключи — в .env в корне репозитория, сессия — там же (telegram_session.session).

    python automation/tg_export.py --list                                  # чаты, к которым есть доступ
    python automation/tg_export.py "часть названия" --project o3           # 14 дней → projects/o3/sources/
    python automation/tg_export.py "часть названия" --days 60 --project o3  # глубже
    python automation/tg_export.py "Сделано" --topics                      # темы форумного чата
    python automation/tg_export.py "Сделано" --topic "O3" --project o3

Ограничение только по дням — тогда понятно, какой период выгружен целиком.
Повторный запуск дописывает новые сообщения в тот же файл, старые не трогает.

Работает с личными перепиской, группами, супергруппами, каналами и темами форумов.

Первый запуск: в терминале появится QR — Telegram на телефоне → Настройки →
Устройства → Подключить устройство → навести камеру. Если стоит облачный
пароль (2FA) — скрипт его спросит. Сессия сохраняется в telegram_session.session
в корне репозитория; отозвать — Telegram → Настройки → Устройства.

Нужны: pip install telethon python-dotenv qrcode
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from getpass import getpass
from pathlib import Path

import qrcode
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import ApiIdInvalidError, PasswordHashInvalidError, SessionPasswordNeededError
from telethon.tl.functions.messages import GetForumTopicsRequest
from telethon.tl.types import Channel, Chat, User

ROOT = Path(__file__).resolve().parent.parent  # корень репозитория
sys.path.insert(0, str(Path(__file__).resolve().parent))
from redact import redact_text  # noqa: E402  — пароли и ключи скрываются при выгрузке
load_dotenv(ROOT / ".env")
API_ID = int(os.getenv("TELEGRAM_API_ID", "0") or 0)
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
SESSION = str(ROOT / "telegram_session")


# ---------- вход ----------

def render_qr(url: str) -> None:
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True, tty=sys.stdout.isatty())
    print("\nСканируй: Telegram → Настройки → Устройства → Подключить устройство\n", flush=True)


async def enter_password(client: TelegramClient) -> bool:
    print("\nВключён облачный пароль Telegram (Настройки → Конфиденциальность → Облачный пароль).")
    for attempt in range(1, 4):
        pw = getpass(f"Облачный пароль (попытка {attempt}/3): ")
        try:
            await client.sign_in(password=pw)
            return True
        except PasswordHashInvalidError:
            print("Неверный пароль, попробуй ещё раз.\n")
    return False


async def ensure_login(client: TelegramClient) -> None:
    if await client.is_user_authorized():
        return
    authed = False
    try:
        try:
            qr_login = await client.qr_login()
        except ApiIdInvalidError:
            print("Ключи не подошли: проверь TELEGRAM_API_ID и TELEGRAM_API_HASH в .env "
                  "(my.telegram.org → API development tools). Пробелы и кавычки убрать.", file=sys.stderr)
            sys.exit(1)
        while not authed:
            render_qr(qr_login.url)
            try:
                await qr_login.wait(timeout=30)
                authed = True
            except asyncio.TimeoutError:
                print("QR обновился, наведи камеру ещё раз.\n", flush=True)
                await qr_login.recreate()
            except SessionPasswordNeededError:
                authed = await enter_password(client)
    except SessionPasswordNeededError:
        authed = await enter_password(client)
    if not authed:
        print("Войти не удалось.", file=sys.stderr)
        sys.exit(1)
    me = await client.get_me()
    print(f"Вошли как {me.first_name} (@{me.username}). Сессия сохранена в корне репозитория.\n")


# ---------- чаты ----------

def sender_name(sender) -> str:
    if sender is None:
        return "Unknown"
    if isinstance(sender, User):
        name = " ".join(p for p in [sender.first_name or "", sender.last_name or ""] if p).strip()
        return name or sender.username or f"User_{sender.id}"
    if isinstance(sender, (Channel, Chat)):
        return sender.title or f"Chat_{sender.id}"
    return str(getattr(sender, "id", "?"))


async def list_dialogs(client: TelegramClient, limit: int = 60) -> None:
    print("Чаты (последние по активности):")
    async for d in client.iter_dialogs(limit=limit):
        print(f"  {d.name}  (id={d.id})")


async def find_chat(client: TelegramClient, query: str):
    q = query.lower()
    async for d in client.iter_dialogs():
        if q in (d.name or "").lower():
            print(f"Нашёл чат: «{d.name}» (id={d.id})")
            return d.entity
    print(f"Чат по запросу «{query}» не найден. Посмотри список: python tg_export.py --list")
    sys.exit(1)


async def forum_topics(client: TelegramClient, chat) -> list:
    if not (isinstance(chat, Channel) and getattr(chat, "forum", False)):
        print("Это не форумный чат — тем нет, выгружай целиком.")
        return []
    res = await client(GetForumTopicsRequest(peer=chat, offset_date=datetime.now(), offset_id=0,
                                             offset_topic=0, limit=100, q=""))
    topics = list(res.topics)
    if len(topics) >= 100:
        print("Показаны первые 100 тем.")
    return topics


async def find_topic(client: TelegramClient, chat, query: str) -> int:
    topics = await forum_topics(client, chat)
    q = query.lower()
    for t in topics:
        if q in (t.title or "").lower():
            print(f"Нашёл тему: «{t.title}» (id={t.id})")
            return t.id
    print(f"Тема «{query}» не найдена. Список: python tg_export.py \"<чат>\" --topics")
    sys.exit(1)


async def export(client: TelegramClient, chat, limit: int | None, days: int | None,
                 topic_id: int | None = None) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    out: list[dict] = []
    hidden = 0
    kwargs = {"limit": limit}
    if topic_id:
        kwargs["reply_to"] = topic_id
    async for msg in client.iter_messages(chat, **kwargs):
        if cutoff and msg.date < cutoff:
            break
        clean = redact_text(msg.message or "")
        hidden += clean.count
        entry = {
            "id": msg.id,
            "date": msg.date.astimezone().isoformat(timespec="minutes"),
            "sender": sender_name(msg.sender) if msg.sender else None,
            "text": clean.text,
            "reply_to": getattr(msg.reply_to, "reply_to_msg_id", None) if msg.reply_to else None,
        }
        if msg.media:
            entry["media"] = type(msg.media).__name__
            doc = getattr(msg.media, "document", None)
            if doc:
                for a in doc.attributes:
                    if hasattr(a, "file_name"):
                        entry["file"] = a.file_name
                        break
        out.append(entry)
        if len(out) % 100 == 0:
            print(f"  {len(out)} сообщений…", end="\r")
    out.reverse()  # в хронологический порядок
    if hidden:
        print(f"\nСкрыто паролей/ключей: {hidden} (оригинал — в Telegram по номеру сообщения)")
    return out


def save(messages: list[dict], chat_title: str, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w-]+", "-", chat_title, flags=re.U).strip("-").lower()[:40] or "chat"
    json_path = out_dir / f"telegram-{slug}.json"
    md_path = out_dir / f"telegram-{slug}.md"
    if json_path.exists():  # повторная выгрузка дописывает, не затирает: сырьё только пополняется
        old = {m["id"]: m for m in json.loads(json_path.read_text(encoding="utf-8"))}
        new_ids = [m["id"] for m in messages if m["id"] not in old]
        old.update({m["id"]: m for m in messages})
        messages = sorted(old.values(), key=lambda m: m["id"])
        print(f"Файл уже был: добавлено новых сообщений {len(new_ids)}, всего {len(messages)}")
    json_path.write_text(json.dumps(messages, ensure_ascii=False, indent=1), encoding="utf-8")

    dates = [m["date"][:10] for m in messages]
    lines = [
        f"# Telegram: {chat_title}",
        "",
        f"Выгружено: {datetime.now().strftime('%Y-%m-%d %H:%M')} · сообщений: {len(messages)}"
        + (f" · период: {min(dates)} — {max(dates)}" if dates else ""),
        "Время — местное на этом компьютере. Ссылка на сообщение = номер после #.",
        "",
    ]
    day = None
    for m in messages:
        if m["date"][:10] != day:
            day = m["date"][:10]
            lines += [f"## {day}", ""]
        head = f"**{m['date'][11:16]} {m.get('sender') or 'Unknown'}** #{m['id']}"
        if m.get("reply_to"):
            head += f" (ответ на #{m['reply_to']})"
        if m.get("media"):
            head += f" [{m['media']}" + (f": {m['file']}" if m.get("file") else "") + "]"
        lines.append(head)
        if m["text"]:
            lines.append("> " + m["text"].replace("\n", "\n> "))
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nГотово: {md_path}\n        {json_path}")
    print(f"Сообщений: {len(messages)}" + (f", период {min(dates)} — {max(dates)}" if dates else ""))


# ---------- main ----------

async def main() -> None:
    p = argparse.ArgumentParser(description="Выгрузка чата Telegram в sources/ за последние N дней (по умолчанию 14)")
    p.add_argument("chat", nargs="?", help="часть названия чата")
    p.add_argument("--days", type=int, default=14, help="за последние N дней (по умолчанию 14)")
    p.add_argument("--project", help="имя проекта → projects/<имя>/sources/")
    p.add_argument("--out", type=Path, help="куда сохранить (если не --project)")
    p.add_argument("--list", action="store_true", help="показать доступные чаты")
    p.add_argument("--topics", action="store_true", help="показать темы форумного чата")
    p.add_argument("--topic", help="часть названия темы форума (выгрузить только её)")
    a = p.parse_args()

    if not API_ID or not API_HASH:
        print("Нет ключей. Создай файл .env в корне репозитория (см. .env.example):\n"
              "TELEGRAM_API_ID=...\nTELEGRAM_API_HASH=...\n"
              "Ключи выдаёт https://my.telegram.org → API development tools", file=sys.stderr)
        sys.exit(1)
    if not a.list and not a.chat:
        p.error("укажи часть названия чата или --list")
    out_dir = a.out or (ROOT / "projects" / a.project / "sources" if a.project else Path.cwd() / "sources")

    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    try:
        await ensure_login(client)
        if a.list:
            await list_dialogs(client)
            return
        chat = await find_chat(client, a.chat)
        title = getattr(chat, "title", None) or sender_name(chat)
        if a.topics:
            for t in await forum_topics(client, chat):
                print(f"  {t.title}  (id={t.id})")
            return
        topic_id = await find_topic(client, chat, a.topic) if a.topic else None
        if topic_id:
            title = f"{title} — {a.topic}"
        messages = await export(client, chat, limit=None, days=a.days, topic_id=topic_id)
        if not messages:
            print(f"Сообщений за {a.days} дн. нет. Увеличь период: --days 60")
            return
        save(messages, title, out_dir)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
