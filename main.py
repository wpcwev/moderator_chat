import asyncio
import json
import logging
import os
import re
from pathlib import Path
from datetime import timedelta, datetime, timezone
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, ChatPermissions

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("TGTOKEN") or "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE"
assert BOT_TOKEN and BOT_TOKEN != "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE", "Укажи токен (TGTOKEN или строка в коде)."

CONFIG_PATH = Path("config.json")
SUPERADMINS={7393436735}
# SUPERADMINS="123,456"
ENV_SUPERADMINS = {int(x) for x in re.split(r"[,\s]+", os.getenv("SUPERADMINS", "").strip()) if x.isdigit()}

MUTED_PERMS = ChatPermissions(
    can_send_messages=False,
    can_send_media_messages=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_change_info=False,
    can_invite_users=True,
    can_pin_messages=False,
)

UNMUTED_PERMS = ChatPermissions(
    can_send_messages=True,
    can_send_media_messages=True,
    can_invite_users=True,
    can_pin_messages=False,
    can_change_info=False,
)

# ==================== ХРАНИЛКА ====================
def load_config():
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(data.get("banned_words", []), list):
                data["banned_words"] = []
            nm = data.get("newbie_mute_minutes", 1)
            if not isinstance(nm, int) or nm < 0:
                nm = 1
            admins = set(map(int, data.get("superadmins", [])))
            admins |= ENV_SUPERADMINS

            schedule = data.get("schedule", {}) or {}
            schedule_enabled = bool(schedule.get("enabled", True))
            open_time = schedule.get("open_time", "10:00")
            close_time = schedule.get("close_time", "19:00")
            tz_name = schedule.get("tz", "Europe/Moscow")

            managed = data.get("managed_chats", [])
            if not isinstance(managed, list):
                managed = []

            return {
                "banned_words": sorted(set(map(str.lower, data.get("banned_words", [])))),
                "newbie_mute_minutes": nm,
                "superadmins": sorted(admins),
                "schedule": {
                    "enabled": schedule_enabled,
                    "open_time": open_time,
                    "close_time": close_time,
                    "tz": tz_name,
                },
                "managed_chats": sorted(
                    set(int(x) for x in managed if isinstance(x, int) or str(x).lstrip("-").isdigit())
                ),
            }
        except Exception:
            logging.exception("config.json повреждён, пересоздаю.")
    return {
        "banned_words": [],
        "newbie_mute_minutes": 1,
        "superadmins": sorted(ENV_SUPERADMINS),
        "schedule": {"enabled": True, "open_time": "10:00", "close_time": "19:00", "tz": "Europe/Moscow"},
        "managed_chats": [],
    }

def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

CONFIG = load_config()

def build_badwords_regex():
    parts = []
    for w in CONFIG["banned_words"]:
        w = w.strip()
        if not w:
            continue
        if " " in w or "-" in w:
            parts.append(re.escape(w))
        else:
            parts.append(r"\b" + re.escape(w) + r"\b")
    if not parts:
        return None
    return re.compile(r"(?i)(" + "|".join(parts) + r")")

BADWORDS_RE = build_badwords_regex()

# ==================== ХЕЛПЕРЫ ====================
URL_RE = re.compile(r"(https?://\S+|t\.me/\S+|telegram\.me/\S+|telegram\.org/\S+)", re.IGNORECASE)
USERNAME_RE = re.compile(r"(?<!\w)@([a-zA-Z0-9_]{5,})\b")

def text_of(msg: Message) -> str:
    return (msg.text or msg.caption or "").strip()

async def delete_safely(message: Message):
    try:
        await message.delete()
    except Exception:
        logging.debug("Failed to delete a message", exc_info=True)

async def ban_safely(bot: Bot, chat_id: int, user_id: int):
    try:
        await bot.ban_chat_member(chat_id, user_id)
    except Exception:
        logging.debug("Failed to ban user", exc_info=True)

async def is_chat_admin(bot: Bot, chat_id: int, user_id: Optional[int], sender_chat_id: Optional[int] = None) -> bool:
    # анонимный админ — сообщение от имени самого чата
    if sender_chat_id and sender_chat_id == chat_id:
        return True
    if not user_id:
        return False
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

def is_private(message: Message) -> bool:
    return message.chat.type == "private"

def is_superadmin(user_id: Optional[int]) -> bool:
    return bool(user_id) and int(user_id) in set(CONFIG.get("superadmins", []))

async def can_manage(message: Message) -> bool:
    """
    Управление настройками:
    - В личке: только супер‑админы.
    - В группе: админы чата.
    """
    if is_private(message):
        return is_superadmin(message.from_user.id if message.from_user else None)
    return await is_chat_admin(
        message.bot, message.chat.id,
        message.from_user.id if message.from_user else None,
        message.sender_chat.id if message.sender_chat else None
    )

def parse_badword_list(raw: str) -> list[str]:
    if raw is None:
        return []
    raw = raw.strip()
    if not raw:
        return []
    if any(ch in raw for ch in ("\n", ",", ";")):
        parts = re.split(r"[,\n;]+", raw)
        return [p.strip().lower() for p in parts if p.strip()]
    return [raw.lower()]

# ---------- Планировщик ----------
SCHEDULER: AsyncIOScheduler | None = None

def _get_tz() -> ZoneInfo:
    try:
        return ZoneInfo(CONFIG["schedule"].get("tz", "Europe/Moscow"))
    except Exception:
        return ZoneInfo("Europe/Moscow")

def _parse_hhmm(s: str) -> tuple[int, int]:
    s = s.strip()
    hh, mm = s.split(":")
    h, m = int(hh), int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError
    return h, m

def _add_managed_chat(chat_id: int):
    managed = set(CONFIG.get("managed_chats", []))
    if chat_id not in managed:
        managed.add(chat_id)
        CONFIG["managed_chats"] = sorted(managed)
        save_config(CONFIG)

async def _set_chat_closed(bot: Bot, chat_id: int):
    try:
        await bot.set_chat_permissions(chat_id, MUTED_PERMS)
    except Exception:
        logging.debug(f"Failed to close chat {chat_id}", exc_info=True)

async def _set_chat_open(bot: Bot, chat_id: int):
    try:
        await bot.set_chat_permissions(chat_id, UNMUTED_PERMS)
    except Exception:
        logging.debug(f"Failed to open chat {chat_id}", exc_info=True)

def _reschedule_jobs(bot: Bot):
    global SCHEDULER
    if SCHEDULER is None:
        return
    # Удаляем предыдущие задания
    for job in list(SCHEDULER.get_jobs()):
        if job.id in ("auto_close", "auto_open"):
            SCHEDULER.remove_job(job.id)

    if not CONFIG["schedule"]["enabled"]:
        logging.info("Schedule disabled")
        return

    tz = _get_tz()
    try:
        open_h, open_m = _parse_hhmm(CONFIG["schedule"]["open_time"])
        close_h, close_m = _parse_hhmm(CONFIG["schedule"]["close_time"])
    except Exception:
        logging.error("Bad schedule times; using defaults 10:00 / 19:00")
        open_h, open_m = 10, 0
        close_h, close_m = 19, 0

    async def close_all():
        for chat_id in CONFIG.get("managed_chats", []):
            await _set_chat_closed(bot, chat_id)

    async def open_all():
        for chat_id in CONFIG.get("managed_chats", []):
            await _set_chat_open(bot, chat_id)

    SCHEDULER.add_job(
        close_all, CronTrigger(hour=close_h, minute=close_m, timezone=tz),
        id="auto_close", replace_existing=True
    )
    SCHEDULER.add_job(
        open_all, CronTrigger(hour=open_h, minute=open_m, timezone=tz),
        id="auto_open", replace_existing=True
    )
    logging.info(f"Schedule set: close {close_h:02d}:{close_m:02d}, open {open_h:02d}:{open_m:02d} ({tz})")

# ==================== РОУТЕР ====================
router = Router()

# ---------- Утилиты / суперадмины ----------
@router.message(Command("myid"))
async def cmd_myid(message: Message):
    uid = message.from_user.id if message.from_user else None
    await message.reply(f"Ваш ID: {uid}")

@router.message(Command("admins"))
async def cmd_admins_list(message: Message):
    if not is_private(message) or not is_superadmin(message.from_user.id):
        await message.reply("Команда доступна только супер‑админам в личке.")
        return
    lst = CONFIG.get("superadmins", [])
    await message.reply("Суперадмины:\n" + ("\n".join(f"• {x}" for x in lst) if lst else "— пусто —"))

@router.message(Command("add_admin"))
async def cmd_add_admin(message: Message, command: CommandObject):
    if not is_private(message) or not is_superadmin(message.from_user.id):
        await message.reply("Команда доступна только супер‑админам в личке.")
        return
    if not command.args or not command.args.strip().isdigit():
        await message.reply("Использование: /add_admin <user_id>\n(узнать ID: /myid)")
        return
    uid = int(command.args.strip())
    admins = set(CONFIG.get("superadmins", []))
    admins.add(uid)
    CONFIG["superadmins"] = sorted(admins)
    save_config(CONFIG)
    await message.reply(f"Добавлен супер‑админ: {uid}")

@router.message(Command("remove_admin"))
async def cmd_remove_admin(message: Message, command: CommandObject):
    if not is_private(message) or not is_superadmin(message.from_user.id):
        await message.reply("Команда доступна только супер‑админам в личке.")
        return
    if not command.args or not command.args.strip().isdigit():
        await message.reply("Использование: /remove_admin <user_id>")
        return
    uid = int(command.args.strip())
    admins = set(CONFIG.get("superadmins", []))
    if uid in admins:
        admins.remove(uid)
        CONFIG["superadmins"] = sorted(admins)
        save_config(CONFIG)
        await message.reply(f"Супер‑админ удалён: {uid}")
    else:
        await message.reply("Такого ID нет в списке.")

# ---------- Команды ----------
@router.message(Command("start", "help"))
async def cmd_help(message: Message):
    header = (
        "Я — модератор чата.\n\n"
        "Глобальные настройки общие для всех чатов.\n"
        "Управление:\n"
        "• В группах — админы чата.\n"
        "• В личке со мной — супер‑админы (по ID).\n\n"
    )
    cmds = (
        "Команды:\n"
        "• /mute1m — запретить писать всем на 1 минуту (в группе у админов)\n"
        "• /badwords — показать список запрещённых слов\n"
        "• /add_badword <слово или список> — столбик/запятые/reply\n"
        "• /remove_badword <слово или список>\n"
        "• /newbie_mute — показать авто‑мут новичков (мин)\n"
        "• /set_newbie_mute <минуты> — 0 = выключить\n"
        "• /schedule_show — показать расписание (MSK)\n"
        "• /schedule_set <HH:MM_открыть> <HH:MM_закрыть>\n"
        "• /schedule_enable / /schedule_disable\n"
        "• /schedule_tz <IANA_Timezone> (по умолчанию Europe/Moscow)\n"
        "\nСупер‑админские в личке:\n"
        "• /myid — показать ваш ID\n"
        "• /admins — список супер‑админов\n"
        "• /add_admin <id> /remove_admin <id>\n"
        "\nПравила модерации: удаляю системные сообщения, ссылки, @username, аудио/видео/войс/видеосообщения, односимвольные; за запрещённые слова — удаление и пермабан; добавленных ботов — удаляю, пригласившего — бан. Администраторы чата и супер‑админы не подпадают под фильтры."
    )
    await message.reply(header + cmds, parse_mode=None)

@router.message(Command("badwords"))
async def cmd_badwords(message: Message):
    if not await can_manage(message):
        await message.reply("Недостаточно прав.")
        return
    words = CONFIG["banned_words"]
    await message.reply("Список запрещённых слов пуст." if not words else "Запрещённые слова/фразы:\n• " + "\n• ".join(words))

@router.message(Command("add_badword"))
async def cmd_add_badword(message: Message, command: CommandObject):
    if not await can_manage(message):
        await message.reply("Недостаточно прав.")
        return
    source_text = command.args
    if (not source_text) and message.reply_to_message:
        source_text = text_of(message.reply_to_message)
    words = parse_badword_list(source_text or "")
    if not words:
        await message.reply(
            "Укажи слово/фразу или пришли список в столбик/через запятые; можно ответом на сообщение со списком.",
            parse_mode=None,
        )
        return
    before = set(CONFIG["banned_words"])
    after = before.union(words)
    CONFIG["banned_words"] = sorted(after)
    save_config(CONFIG)
    global BADWORDS_RE
    BADWORDS_RE = build_badwords_regex()
    added = sorted(set(words) - before)
    await message.reply(("Добавлено: " + ", ".join(added[:20]) + ("…" if len(added) > 20 else "")) if added else "Ничего нового.")

@router.message(Command("remove_badword"))
async def cmd_remove_badword(message: Message, command: CommandObject):
    if not await can_manage(message):
        await message.reply("Недостаточно прав.")
        return
    source_text = command.args
    if (not source_text) and message.reply_to_message:
        source_text = text_of(message.reply_to_message)
    words = parse_badword_list(source_text or "")
    if not words:
        await message.reply("Укажи слово/фразу для удаления или список (столбик/запятые/reply).")
        return
    before = set(CONFIG["banned_words"])
    removed = sorted(before.intersection(words))
    if not removed:
        await message.reply("Из указанного ничего нет в списке.")
        return
    CONFIG["banned_words"] = sorted(before.difference(words))
    save_config(CONFIG)
    global BADWORDS_RE
    BADWORDS_RE = build_badwords_regex()
    await message.reply("Удалено: " + ", ".join(removed[:20]) + ("…" if len(removed) > 20 else ""))

@router.message(Command("newbie_mute"))
async def cmd_newbie_mute_show(message: Message):
    if not await can_manage(message):
        await message.reply("Недостаточно прав.")
        return
    m = CONFIG.get("newbie_mute_minutes", 1)
    await message.reply("Авто‑мут новичков: выключен (0 минут)." if m <= 0 else f"Авто‑мут новичков: {m} мин.")

@router.message(Command("set_newbie_mute"))
async def cmd_newbie_mute_set(message: Message, command: CommandObject):
    if not await can_manage(message):
        await message.reply("Недостаточно прав.")
        return
    if not command.args:
        await message.reply("Использование: /set_newbie_mute <минуты> (0 = выключить)")
        return
    try:
        minutes = int(command.args.strip())
    except ValueError:
        await message.reply("Минуты должны быть целым числом.")
        return
    minutes = max(0, min(1440, minutes))
    CONFIG["newbie_mute_minutes"] = minutes
    save_config(CONFIG)
    await message.reply(f"Готово. Авто‑мут новичков: {minutes} мин.")

@router.message(Command("mute1m"))
async def cmd_mute_all(message: Message):
    # Только в группах и только у админов группы
    if is_private(message) or not await can_manage(message):
        await message.reply("Команда работает только в группе у админов.")
        return
    chat_id = message.chat.id
    try:
        await message.bot.set_chat_permissions(chat_id, MUTED_PERMS)
        await message.reply("Чат замьючен на 1 минуту.")
    except Exception:
        await message.reply("Не удалось изменить разрешения. Дайте боту права на управление чатом.")
        return
    async def unmute_later():
        await asyncio.sleep(60)
        try:
            await message.bot.set_chat_permissions(chat_id, UNMUTED_PERMS)
        except Exception:
            logging.debug("Failed to unmute chat back", exc_info=True)
    asyncio.create_task(unmute_later())

# ---------- Расписание: команды ----------
@router.message(Command("schedule_show"))
async def cmd_schedule_show(message: Message):
    if not await can_manage(message):
        await message.reply("Недостаточно прав.")
        return
    s = CONFIG["schedule"]
    await message.reply(
        f"Расписание: {'включено' if s['enabled'] else 'выключено'}\n"
        f"Открытие: {s['open_time']}  Закрытие: {s['close_time']}\n"
        f"Таймзона: {s.get('tz', 'Europe/Moscow')}",
        parse_mode=None
    )

@router.message(Command("schedule_set"))
async def cmd_schedule_set(message: Message, command: CommandObject):
    if not await can_manage(message):
        await message.reply("Недостаточно прав.")
        return
    if not command.args:
        await message.reply("Использование: /schedule_set <HH:MM_открыть> <HH:MM_закрыть>\nНапример: /schedule_set 10:00 19:00")
        return
    parts = command.args.split()
    if len(parts) != 2:
        await message.reply("Нужно два времени: открыть и закрыть. Пример: /schedule_set 10:00 19:00")
        return
    try:
        _parse_hhmm(parts[0]); _parse_hhmm(parts[1])
    except Exception:
        await message.reply("Неверный формат времени. Используй HH:MM, например 09:30")
        return
    CONFIG["schedule"]["open_time"] = parts[0]
    CONFIG["schedule"]["close_time"] = parts[1]
    save_config(CONFIG)
    _reschedule_jobs(message.bot)
    await message.reply(f"Готово. Теперь: открыть {parts[0]}, закрыть {parts[1]}")

@router.message(Command("schedule_enable"))
async def cmd_schedule_enable(message: Message):
    if not await can_manage(message):
        await message.reply("Недостаточно прав.")
        return
    CONFIG["schedule"]["enabled"] = True
    save_config(CONFIG)
    _reschedule_jobs(message.bot)
    await message.reply("Расписание включено.")

@router.message(Command("schedule_disable"))
async def cmd_schedule_disable(message: Message):
    if not await can_manage(message):
        await message.reply("Недостаточно прав.")
        return
    CONFIG["schedule"]["enabled"] = False
    save_config(CONFIG)
    _reschedule_jobs(message.bot)
    await message.reply("Расписание выключено.")

@router.message(Command("schedule_tz"))
async def cmd_schedule_tz(message: Message, command: CommandObject):
    if not await can_manage(message):
        await message.reply("Недостаточно прав.")
        return
    tz = (command.args or "").strip() or "Europe/Moscow"
    try:
        ZoneInfo(tz)
    except Exception:
        await message.reply("Неизвестная таймзона. Пример: Europe/Moscow")
        return
    CONFIG["schedule"]["tz"] = tz
    save_config(CONFIG)
    _reschedule_jobs(message.bot)
    await message.reply(f"Таймзона установлена: {tz}")

# ---------- Сервисные события ----------
@router.message(F.new_chat_members)
async def on_new_members(message: Message):
    _add_managed_chat(message.chat.id)
    inviter_id = message.from_user.id if message.from_user else None
    chat_id = message.chat.id
    await delete_safely(message)  # чистим системку
    newbie_minutes = CONFIG.get("newbie_mute_minutes", 1)

    for member in message.new_chat_members:
        if member.is_bot:
            await ban_safely(message.bot, chat_id, member.id)
            if inviter_id:
                await ban_safely(message.bot, chat_id, inviter_id)
            continue
        # Не трогаем админов
        if newbie_minutes > 0:
            try:
                m_admin = await message.bot.get_chat_member(chat_id, member.id)
                if m_admin.status in ("administrator", "creator"):
                    continue
            except Exception:
                pass
            try:
                until = datetime.now(timezone.utc) + timedelta(minutes=newbie_minutes)
                await message.bot.restrict_chat_member(chat_id=chat_id, user_id=member.id, permissions=MUTED_PERMS, until_date=until)
            except Exception:
                logging.debug("Failed to restrict newbie", exc_info=True)

@router.message(F.left_chat_member)
async def on_left_member(message: Message):
    await delete_safely(message)

# ---------- Главный фильтр ----------
@router.message()
async def moderation_gate(message: Message):
    if message.chat.type in ("group", "supergroup"):
        _add_managed_chat(message.chat.id)

    # Админы чата (и анонимные) — игнорируем фильтры
    if await is_chat_admin(
        message.bot, message.chat.id,
        message.from_user.id if message.from_user else None,
        message.sender_chat.id if message.sender_chat else None
    ):
        return

    txt = text_of(message)

    # 1) медиа: аудио/видео/voice/video_note — удаляем без бана
    if message.audio or message.video or message.voice or message.video_note:
        await delete_safely(message)
        return

    # 2) односимвольные
    if txt and len(txt) == 1:
        await delete_safely(message)
        return

    # 3) ссылки
    entities = (message.entities or []) + (message.caption_entities or [])
    if any(e.type in ("url", "text_link") for e in entities) or (txt and URL_RE.search(txt)):
        await delete_safely(message)
        return

    # 4) @username
    if any(e.type == "mention" for e in entities) or (txt and USERNAME_RE.search(txt)):
        await delete_safely(message)
        return

    # 5) запрещённые слова — удаляем + пермабан
    if BADWORDS_RE and txt and BADWORDS_RE.search(txt):
        await delete_safely(message)
        await ban_safely(message.bot, message.chat.id, message.from_user.id)
        return

# ==================== ЗАПУСК ====================
async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)

    global SCHEDULER
    SCHEDULER = AsyncIOScheduler()
    SCHEDULER.start()
    _reschedule_jobs(bot)  # подтянуть расписание из config.json

    await dp.start_polling(bot, allowed_updates=["message", "chat_member"])

if __name__ == "__main__":
    asyncio.run(main())
