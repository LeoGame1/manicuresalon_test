"""Telegram-бот салона: меню по ролям, вопросы клиентов, чат мастеров, напоминания, статистика."""
import json, re, threading, time
from contextlib import closing
from datetime import datetime, timedelta

from core import *  # noqa: F401,F403

STATE = {}   # chat_id -> (состояние, данные): бот ждёт текстовый ввод
RECENT = {}  # (чат, пользователь) -> время, чтобы не обрабатывать вход в группу дважды
MAIN = [[B("⬅️ Главное меню", "m")]]


def screen(uid, mid, text, rows):
    """Редактирует сообщение с кнопками (или шлёт новое)."""
    if mid:
        r = api_raw("editMessageText", chat_id=uid, message_id=mid, text=text[:4000], parse_mode="HTML",
                    disable_web_page_preview=True, reply_markup={"inline_keyboard": rows or []})
        if r.get("ok") or "not modified" in r.get("description", ""):
            return
    send(uid, text, rows)


def short(s, n=40):
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------- главное меню ----------
def menu(uid, mid=None):
    rl, rows = roles_of(uid), []
    if rl["owner"]:
        title = "👑 <b>Панель владельца</b>\nВыберите раздел:"
        rows += [[B("📅 Записи", "bk"), B("👩‍🎨 Мастера", "ms")],
                 [B("🛟 Поддержка", "sp"), B("💬 Вопросы клиентов", "tks")],
                 [B("📊 Часы работы", "hr"), B("🔗 Чат салона", "ch")]]
    if rl["master"]:
        title = f"💅 <b>Привет, {esc(rl['master']['name'])}!</b>\nВаше меню мастера:"
        rows += [[B("📅 Мои записи", "my"), B("⏱ Мои часы", "myh")],
                 [B("👥 Коллектив", "team"), B("💬 Чат салона", "chm")]]
    if rl["support"] and not rl["owner"]:
        title = f"🛟 <b>{esc(rl['support']['name'])}, служба поддержки</b>"
        rows += [[B("💬 Вопросы клиентов", "tks"), B("✅ Закрытые", "tkx")], [B("📊 Моя статистика", "mys"), B("💬 Чат салона", "chm")],
                  [B("🏖 Мои выходные", "myoff"), B("👥 Коллектив", "team")]]
    if not rows:
        title = ("💅 <b>Салон маникюра</b>\n\nЗдравствуйте! 👋\n"
                 "• Напоминания о записи придут сюда 🔔\n• Есть вопрос? Просто напишите сообщение — мы ответим ✍️")
        if SITE_URL:
            rows.append([B("🌐 Записаться на сайте", url=SITE_URL)])
        rows.append([B("❓ Задать вопрос", "ask")])
    screen(uid, mid, title, rows)


# ---------- мастер ----------
def bookings_text(mid):
    with closing(db()) as c:
        rows = c.execute("SELECT date, time, name, phone, tg_username, comment FROM bookings WHERE master_id=? AND date>=? ORDER BY date, time LIMIT 30",
                         (mid, now_local().date().isoformat())).fetchall()
    if not rows:
        return "📅 <b>Ваши записи</b>\n\n🗓 Ближайших записей нет. Отдыхайте! ✨"
    out, last = ["📅 <b>Ваши записи</b>"], ""
    for r in rows:
        if r["date"] != last:
            out.append(f"\n<b>{fmt_date(r['date'])}</b>")
            last = r["date"]
        extra = (f" · ✈️ @{r['tg_username']}" if r["tg_username"] else "") + (f"\n   💬 {esc(short(r['comment'], 80))}" if r["comment"] else "")
        out.append(f"🕒 {r['time']} · 👤 {esc(r['name'])} · 📞 {esc(r['phone'])}{extra}")
    return "\n".join(out)


def team_text(with_support=False):
    masters, staff = team()
    text = "👥 <b>Коллектив салона</b>\n\n" + ("💅 <b>Мастера</b>\n" if with_support else "")
    text += "\n".join(f"💅 {esc(n)}" for n in masters) or "Пока никого нет."
    if with_support:
        text += "\n\n🛟 <b>Служба поддержки</b>\n" + ("\n".join(f"🛟 {esc(n)}" for n in staff) or "Пока никого нет.")
    return text


def chat_screen(uid, mid):
    link = get_setting("chat_link")
    if link:
        screen(uid, mid, "💬 <b>Общий чат салона</b>\n\nЗдесь собирается вся команда 👇", [[B("🚪 Перейти в чат", url=link)]] + MAIN)
    else:
        screen(uid, mid, "🔒 <b>Ссылка на чат временно недоступна</b>\n\nВладелец скоро обновит её. Попробуйте позже.", MAIN)


def my_hours(uid, mid, master_id):
    lines = ["⏱ <b>Мои часы</b>\n"]
    for title, p in (("Эта неделя", "week"), ("Прошлая неделя", "prev"), ("Этот месяц", "month")):
        h, n = hours_for(master_id, *period_range(p))
        lines.append(f"🗓 {title}: <b>{h} ч</b> ({n} {plural(n, ('приём', 'приёма', 'приёмов'))})")
    screen(uid, mid, "\n".join(lines), MAIN)


def my_support_stats(uid, mid, sid):
    lines = ["📊 <b>Моя статистика</b>\n"]
    for title, p in (("Эта неделя", "week"), ("Прошлая неделя", "prev"), ("Этот месяц", "month")):
        t, r = support_counts(sid, *period_range(p))
        lines.append(f"🗓 {title}: <b>{t} {plural(t, ('вопрос', 'вопроса', 'вопросов'))}</b> ({r} {plural(r, ('ответ', 'ответа', 'ответов'))})")
    lines.append("\n<i>Вопрос считается обработанным, если вы на него ответили.</i>")
    screen(uid, mid, "\n".join(lines), MAIN)


KIND = {"m": "master", "s": "support"}


def parse_days(text):
    """«25.11» или «25.11-27.11» -> (с, по)."""
    found = re.findall(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", text)
    if not 1 <= len(found) <= 2:
        return None
    today, out = now_local().date(), []
    for d, mo, y in found:
        try:
            yy = (int(y) + (2000 if int(y) < 100 else 0)) if y else today.year
            day = date(yy, int(mo), int(d))
            if not y and day < today:
                day = date(yy + 1, int(mo), int(d))
        except ValueError:
            return None
        out.append(day.isoformat())
    return out[0], (out[1] if len(out) == 2 else None)


def days_off_screen(uid, mid, kind, pid):
    with closing(db()) as c:
        p = c.execute(f"SELECT name FROM {DO[kind][2]} WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        return screen(uid, mid, "🤷 Не найден.", MAIN)
    k, back = ("m", f"ms:{pid}") if kind == "master" else ("s", f"sp:{pid}")
    rows = days_off_rows(kind, pid)
    text = f"🏖 <b>Выходные: {esc(p['name'])}</b>\n\n" + ("\n".join(f"📅 {fmt_date(r['date'])}" for r in rows) if rows else "Ближайших выходных нет.")
    btns = [B(f"✖ {r['date'][8:]}.{r['date'][5:7]}", f"dod:{k}:{r['id']}:{pid}") for r in rows[:30]]
    screen(uid, mid, text + ("\n\n<i>Нажмите на дату, чтобы убрать выходной.</i>" if rows else ""),
           [btns[i:i + 3] for i in range(0, len(btns), 3)] + [[B("➕ Добавить выходной", f"doa:{k}:{pid}")], [B("⬅️ Назад", back)]])


def my_off(uid, mid, sid):
    rows = days_off_rows("support", sid)
    text = "🏖 <b>Мои выходные</b>\n\n" + ("\n".join(f"📅 {fmt_date(r['date'])}" for r in rows) if rows else "Ближайших выходных нет.")
    screen(uid, mid, text + "\n\n<i>Выходные назначает владелец. В выходной вам не приходят уведомления о новых вопросах.</i>", MAIN)


# ---------- записи (владелец) ----------
def booking_row(bid):
    with closing(db()) as c:
        return c.execute("""SELECT b.*, m.name AS master FROM bookings b JOIN masters m ON m.id=b.master_id WHERE b.id=?""", (bid,)).fetchone()


def bookings_list(uid, mid):
    with closing(db()) as c:
        rows = c.execute("""SELECT b.id, b.date, b.time, b.name, m.name AS master FROM bookings b JOIN masters m ON m.id=b.master_id
                            WHERE b.date>=? ORDER BY b.date, b.time LIMIT 15""", (now_local().date().isoformat(),)).fetchall()
    kb = [[B(short(f"{r['date'][8:]}.{r['date'][5:7]} {r['time']} · {r['name']} · {r['master']}", 58), f"bk:{r['id']}")] for r in rows]
    text = "📅 <b>Ближайшие записи</b>\nНажмите на запись, чтобы изменить или отменить:" if rows else "📅 <b>Записей пока нет</b> 🗓"
    screen(uid, mid, text, kb + MAIN)


def booking_card(uid, mid, bid):
    r = booking_row(bid)
    if not r:
        return screen(uid, mid, "🤷 Запись не найдена.", [[B("⬅️ К записям", "bk")]])
    text = (f"📋 <b>Запись #{r['id']}</b>\n\n📅 {fmt_date(r['date'])}\n🕒 {r['time']}\n👩‍🎨 Мастер: {esc(r['master'])}\n"
            f"👤 Клиент: {esc(r['name'])}\n📞 {esc(r['phone'])}\n"
            + (f"✈️ Telegram: @{r['tg_username']}\n" if r["tg_username"] else "")
            + (f"💬 {esc(r['comment'])}\n" if r["comment"] else "")
            + f"🔔 Бот у клиента: {'подключён ✅' if r['client_chat_id'] else 'не подключён'}")
    screen(uid, mid, text, [[B("👩‍🎨 Сменить мастера", f"bm:{bid}"), B("🕒 Перенести", f"bt:{bid}")],
                            [B("🗑 Отменить запись", f"bd:{bid}")], [B("⬅️ К записям", "bk")]])


def parse_when(text):
    m = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s+(\d{1,2})[:.](\d{2})\s*", text)
    if not m:
        return None
    d, mo, y, h, mi = m.groups()
    today = now_local().date()
    y = int(y) + (2000 if y and int(y) < 100 else 0) if y else today.year
    try:
        day = date(y, int(mo), int(d))
    except ValueError:
        return None
    if not m.group(3) and day < today:
        day = date(y + 1, int(mo), int(d))
    return day.isoformat(), f"{int(h):02d}:{mi}"


# ---------- мастера и поддержка (владелец) ----------
def people_list(uid, mid, table, cmd, add_cmd, title, icon):
    with closing(db()) as c:
        rows = c.execute(f"SELECT id, name FROM {table} WHERE active=1 ORDER BY name").fetchall()
    kb = [[B(f"{icon} {r['name']}", f"{cmd}:{r['id']}")] for r in rows]
    kb.append([B("➕ Добавить", add_cmd)])
    screen(uid, mid, f"{title}\n\n" + ("Выберите, чтобы открыть карточку:" if rows else "Пока никого нет."), kb + MAIN)


def person_card(uid, mid, table, cmd, pid, icon, pw_cmd, del_cmd, back):
    with closing(db()) as c:
        r = c.execute(f"SELECT id, name, telegram_id FROM {table} WHERE id=? AND active=1", (pid,)).fetchone()
    if not r:
        return screen(uid, mid, "🤷 Не найден.", [[B("⬅️ Назад", back)]])
    tgid = f"<code>{r['telegram_id']}</code>" if r["telegram_id"] else "не указан"
    screen(uid, mid, f"{icon} <b>{esc(r['name'])}</b>\n🆔 Telegram ID: {tgid}",
           [[B("🏖 Выходные", f"do:{'m' if table == 'masters' else 's'}:{pid}")], [B("🔑 Новый пароль для сайта", f"{pw_cmd}:{pid}")], [B("🗑 Удалить", f"{del_cmd}:{pid}")], [B("⬅️ Назад", back)]])


def add_person_done(uid, kind, name, tgid):
    table, fn, site, icon = (("masters", add_master, "/master", "💅") if kind == "m" else ("support_staff", add_support, "/support", "🛟"))
    _, pw = fn(name, tgid)
    if tgid:
        tg(tgid, f"{icon} <b>Вас добавили в систему салона!</b>\nНажмите /start, чтобы открыть меню.")
    send(uid, f"✅ <b>{esc(name)}</b> добавлен(а)!\n\n🔑 Пароль для входа на сайт ({esc(SITE_URL + site if SITE_URL else site)}):\n<code>{pw}</code>\n\n"
              "Сохраните его — повторно он не показывается (можно выпустить новый).", MAIN)


def ticket_list(uid, mid, status="open"):
    with closing(db()) as c:
        rows = c.execute("""SELECT t.id, t.client_name, t.channel,
                              (SELECT sender FROM ticket_messages WHERE ticket_id=t.id ORDER BY id DESC LIMIT 1) AS last
                            FROM tickets t WHERE t.status=? ORDER BY t.updated_at DESC LIMIT 12""", (status,)).fetchall()
    closed = status == "closed"
    kb = [[B(f"{'✅' if closed else '🔴' if r['last'] == 'client' else '🟡'} #{r['id']} · {short(r['client_name'] or 'Клиент', 18)} · {'🌐' if r['channel'] == 'site' else '✈️'}", f"tk:{r['id']}")] for r in rows]
    kb.append([B("🔴 Открытые", "tks"), B("🔄 Обновить", "tkx")] if closed else [B("✅ Закрытые", "tkx"), B("🔄 Обновить", "tks")])
    if closed:
        text = "✅ <b>Закрытые вопросы</b>\nПоследние 12:" if rows else "✅ <b>Закрытых вопросов нет</b>"
    else:
        text = "💬 <b>Открытые вопросы</b>\n🔴 ждёт ответа · 🟡 вы ответили" if rows else "💬 <b>Открытых вопросов нет</b> ✨"
    screen(uid, mid, text, kb + MAIN)


def ticket_view(uid, mid, tid):
    with closing(db()) as c:
        t = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not t:
        return screen(uid, mid, "🤷 Вопрос не найден.", [[B("⬅️ К вопросам", "tks")]])
    msgs = ticket_messages(tid, 8)
    lines = [f"💬 <b>Вопрос #{tid}</b> · {'🌐 сайт' if t['channel'] == 'site' else '✈️ Telegram'}"
             + ("" if t["status"] == "open" else " · ✅ закрыт"),
             f"👤 {esc(t['client_name'] or 'Клиент')}" + (f" · 📞 {esc(t['contact'])}" if t["contact"] else ""), ""]
    for m in msgs:
        who = "👤" if m["sender"] == "client" else "🛟"
        lines.append(f"{who} <b>{esc(m['author'] or '')}</b> <i>{m['created_at'][5:]}</i>\n{esc(short(m['text'], 500))}\n")
    kb = [[B("✍️ Ответить", f"tkr:{tid}")] + ([B("✅ Закрыть", f"tkc:{tid}")] if t["status"] == "open" else []), [B("📋 Открытые", "tks"), B("✅ Закрытые", "tkx")], [B("🏠 Меню", "m")]]
    screen(uid, mid, "\n".join(lines), kb)


def author_name(rl):
    return "Владелец" if rl["owner"] else (rl["support"]["name"] if rl["support"] else "Поддержка")


# ---------- сообщения ----------
def client_question(uid, user, text):
    with closing(db()) as c:
        t = c.execute("SELECT id FROM tickets WHERE tg_chat_id=? AND status='open' ORDER BY id DESC LIMIT 1", (str(uid),)).fetchone()
    if t:
        ticket_client_message(t["id"], text[:1000])
        send(uid, "📨 Дополнение отправлено. Мы ответим здесь же.")
    else:
        name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or "Клиент"
        contact = f"@{user['username']}" if user.get("username") else ""
        ticket_create("telegram", name, contact, str(uid), text[:1000])
        send(uid, "✅ <b>Вопрос отправлен!</b>\nПоддержка ответит прямо в этом чате. 💅")


def check_member(gid, user):
    """Вход в общий чат: пускаем владельца, действующих мастеров и сотрудников поддержки."""
    if user.get("is_bot") or str(gid) != str(group_id()):
        return
    key, now = (str(gid), user["id"]), time.time()
    if now - RECENT.get(key, 0) < 30:
        return
    RECENT[key] = now
    rl = roles_of(user["id"])
    if rl["owner"] or rl["master"] or rl["support"]:
        send(gid, f"👋 Добро пожаловать, <b>{esc(user.get('first_name', ''))}</b>! 💅")
        return
    kick_from_group(user["id"])
    send(gid, f"🚫 <b>{esc(user.get('first_name', 'Пользователь'))}</b> не найден(а) среди сотрудников салона и удалён(а) из чата.")


def on_message(msg):
    chat, user, text = msg["chat"], msg.get("from") or {}, (msg.get("text") or "").strip()
    if chat["type"] in ("group", "supergroup"):
        for u in msg.get("new_chat_members", []):
            check_member(chat["id"], u)
        if text.startswith("/setgroup") and str(user.get("id")) == OWNER:
            set_setting("group_id", str(chat["id"]))
            send(chat["id"], "✅ Этот чат подключён как общий чат салона. 💅\nСюда будет приходить недельная статистика, "
                             "а лишние участники будут удаляться. Бот должен быть администратором с правом блокировать участников.")
        return
    if chat["type"] != "private" or not user.get("id"):
        return
    uid = user["id"]
    if text.startswith("/id"):
        return send(uid, f"🆔 Ваш Telegram ID: <code>{uid}</code>")
    if text.startswith("/start"):
        STATE.pop(uid, None)
        arg = text.split(maxsplit=1)[1] if " " in text else ""
        if re.fullmatch(r"[a-f0-9]{32}", arg):
            with closing(db()) as c:
                n = c.execute("UPDATE bookings SET client_chat_id=? WHERE token=?", (str(uid), arg)).rowcount
            return send(uid, "🔔 <b>Готово!</b> Мы напомним о записи и сообщим, если её придётся изменить или отменить. 💅"
                        if n else "🤷 Эта запись не найдена.", [[B("⬅️ Меню", "m")]])
        return menu(uid)
    if text in ("/menu", "/cancel"):
        STATE.pop(uid, None)
        return menu(uid)
    if uid in STATE:
        return on_state(uid, text)
    rl = roles_of(uid)
    if rl["owner"] or rl["master"] or rl["support"]:
        return menu(uid)
    if text:
        client_question(uid, user, text)


def on_state(uid, text):
    st, data = STATE.pop(uid)
    rl = roles_of(uid)
    if st == "resched":
        when = parse_when(text)
        if not when:
            STATE[uid] = (st, data)
            return send(uid, "🤔 Не понял формат. Введите дату и время так: <code>25.11 15:00</code>\n(/cancel — отмена)")
        ok, msg = move_booking(data, day=when[0], t=when[1])
        STATE[uid] = (st, data) if not ok else None
        if ok:
            STATE.pop(uid, None)
        return send(uid, "✅ Запись перенесена, клиент и мастер уведомлены. 🔄" if ok else f"⚠️ {esc(msg)}\nПопробуйте другое время или /cancel.",
                    [[B("📋 К записи", f"bk:{data}")]] if ok else None)
    if st in ("am_name", "as_name"):
        STATE[uid] = ("am_tg" if st == "am_name" else "as_tg", text[:80])
        return send(uid, "🆔 Теперь пришлите <b>Telegram ID</b> человека (число).\nОн узнаёт его у этого бота командой /id.\nЕсли ID пока нет, отправьте «-».")
    if st in ("am_tg", "as_tg"):
        tgid = "" if text == "-" else text
        if tgid and not re.fullmatch(r"\d{5,15}", tgid):
            STATE[uid] = (st, data)
            return send(uid, "⚠️ ID — это число, например <code>123456789</code>. Или «-», чтобы пропустить.")
        return add_person_done(uid, "m" if st == "am_tg" else "s", data, tgid)
    if st == "doff":
        kind, pid = data
        rng = parse_days(text)
        if not rng:
            STATE[uid] = (st, data)
            return send(uid, "🤔 Не понял. Напишите дату так: <code>25.11</code> или период <code>25.11-27.11</code>\n(/cancel — отмена)")
        ok, msg, n = add_days_off(kind, pid, *rng)
        if not ok:
            STATE[uid] = (st, data)
            return send(uid, f"⚠️ {esc(msg)}")
        warn = f"\n\n⚠️ У мастера на эти даты уже записей: <b>{n}</b>. Передайте их другому мастеру в разделе «📅 Записи»." if n else ""
        return send(uid, "✅ Выходные добавлены. 🏖" + warn, [[B("🏖 К выходным", f"do:{'m' if kind == 'master' else 's'}:{pid}")]])
    if st == "set_link":
        if not re.match(r"https?://\S+$", text):
            STATE[uid] = (st, data)
            return send(uid, "⚠️ Пришлите ссылку целиком, начиная с https://")
        set_setting("chat_link", text)
        return send(uid, "✅ Ссылка на общий чат сохранена. 🔗", MAIN)
    if st == "reply":
        if not text:
            return
        ticket_staff_reply(data, author_name(rl), text[:1000], rl["support"]["id"] if rl["support"] and not rl["owner"] else None)
        return send(uid, "✅ Ответ отправлен. 📨", [[B("💬 К вопросу", f"tk:{data}"), B("📋 Все вопросы", "tks")]])


# ---------- кнопки ----------
OWNER_ONLY = {"bk", "bd", "bdy", "bt", "bm", "bms", "ms", "msa", "msd", "msdy", "msp", "sp", "spa", "spd", "spdy", "spp", "hr", "ch", "cha", "chd", "do", "doa", "dod"}
SUPPORT = {"tks", "tkx", "tk", "tkr", "tkc"}


def on_callback(cb):
    uid, mid, data = cb["from"]["id"], (cb.get("message") or {}).get("message_id"), cb.get("data", "")
    api("answerCallbackQuery", callback_query_id=cb["id"])
    cmd, *a = data.split(":")
    rl = roles_of(uid)
    staff = rl["owner"] or rl["support"]
    if (cmd in OWNER_ONLY and not rl["owner"]) or (cmd in SUPPORT and not staff) or (cmd in ("my", "myh") and not rl["master"]) \
            or (cmd == "team" and not (rl["master"] or rl["owner"] or rl["support"])) \
            or (cmd == "chm" and not (rl["master"] or rl["owner"] or rl["support"])) or (cmd in ("mys", "myoff") and not rl["support"]):
        return send(uid, "🔒 Нет доступа.")
    STATE.pop(uid, None)
    i = int(a[0]) if a and a[0].isdigit() else None
    j = int(a[1]) if len(a) > 1 and a[1].isdigit() else None

    if cmd == "m": menu(uid, mid)
    elif cmd == "ask": send(uid, "✍️ Напишите ваш вопрос одним сообщением, и поддержка ответит здесь.")
    # мастер
    elif cmd == "my": screen(uid, mid, bookings_text(rl["master"]["id"]), MAIN)
    elif cmd == "myh": my_hours(uid, mid, rl["master"]["id"])
    elif cmd == "team": screen(uid, mid, team_text(True), MAIN)
    elif cmd == "chm": chat_screen(uid, mid)
    # записи
    elif cmd == "bk": booking_card(uid, mid, i) if i else bookings_list(uid, mid)
    elif cmd == "bd":
        screen(uid, mid, "🗑 <b>Отменить запись?</b>\nМастер и клиент получат уведомление.", [[B("✅ Да, отменить", f"bdy:{i}"), B("↩️ Нет", f"bk:{i}")]])
    elif cmd == "bdy":
        ok = cancel_booking(i, skip_chat=uid)
        screen(uid, mid, "✅ Запись отменена, мастер и клиент уведомлены. 🗑" if ok else "🤷 Запись уже удалена.", [[B("⬅️ К записям", "bk")]])
    elif cmd == "bt":
        STATE[uid] = ("resched", i)
        send(uid, "🕒 <b>Перенос записи</b>\n\nВведите новую дату и время, например: <code>25.11 15:00</code>\n"
                  f"Время приёма: {', '.join(SLOTS)}\n(/cancel — отмена)")
    elif cmd == "bm":
        with closing(db()) as c:
            ms = c.execute("SELECT id, name FROM masters WHERE active=1 ORDER BY name").fetchall()
        screen(uid, mid, "👩‍🎨 <b>Выберите нового мастера:</b>", [[B(f"💅 {m['name']}", f"bms:{i}:{m['id']}")] for m in ms] + [[B("⬅️ Назад", f"bk:{i}")]])
    elif cmd == "bms":
        ok, msg = move_booking(i, master_id=j)
        screen(uid, mid, "✅ Мастер изменён, все уведомлены. 🔄" if ok else f"⚠️ {esc(msg)}", [[B("📋 К записи", f"bk:{i}")]])
    # мастера
    elif cmd == "ms":
        people_list(uid, mid, "masters", "ms", "msa", "👩‍🎨 <b>Мастера салона</b>", "💅") if not i else person_card(uid, mid, "masters", "ms", i, "💅", "msp", "msd", "ms")
    elif cmd == "msa": STATE[uid] = ("am_name", None); send(uid, "👩‍🎨 <b>Новый мастер</b>\n\nКак зовут мастера? Напишите имя.\n(/cancel — отмена)")
    elif cmd == "msd": screen(uid, mid, "🗑 <b>Удалить мастера?</b>\nОн будет удалён и из общего чата. Будущие записи останутся — их можно передать другому мастеру.", [[B("✅ Да, удалить", f"msdy:{i}"), B("↩️ Нет", f"ms:{i}")]])
    elif cmd == "msdy":
        r = deactivate_master(i)
        screen(uid, mid, "🤷 Мастер уже удалён." if not r else f"✅ <b>{esc(r[0])}</b> удалён(а) из системы и общего чата."
               + (f"\n\n⚠️ Будущих записей: <b>{r[1]}</b>. Передайте их другому мастеру в разделе «📅 Записи»." if r[1] else ""), [[B("⬅️ К мастерам", "ms")]])
    elif cmd == "msp": pw = new_password("masters", i); screen(uid, mid, f"🔑 Новый пароль:\n<code>{pw}</code>" if pw else "🤷 Не найден.", [[B("⬅️ Назад", f"ms:{i}")]])
    # поддержка
    elif cmd == "sp":
        people_list(uid, mid, "support_staff", "sp", "spa", "🛟 <b>Служба поддержки</b>", "🛟") if not i else person_card(uid, mid, "support_staff", "sp", i, "🛟", "spp", "spd", "sp")
    elif cmd == "spa": STATE[uid] = ("as_name", None); send(uid, "🛟 <b>Новый сотрудник поддержки</b>\n\nНапишите имя.\n(/cancel — отмена)")
    elif cmd == "spd": screen(uid, mid, "🗑 <b>Удалить сотрудника поддержки?</b>", [[B("✅ Да, удалить", f"spdy:{i}"), B("↩️ Нет", f"sp:{i}")]])
    elif cmd == "spdy": deactivate_support(i); screen(uid, mid, "✅ Сотрудник удалён. 🗑", [[B("⬅️ К поддержке", "sp")]])
    elif cmd == "spp": pw = new_password("support_staff", i); screen(uid, mid, f"🔑 Новый пароль:\n<code>{pw}</code>" if pw else "🤷 Не найден.", [[B("⬅️ Назад", f"sp:{i}")]])
    # вопросы клиентов
    elif cmd == "tks": ticket_list(uid, mid)
    elif cmd == "tkx": ticket_list(uid, mid, "closed")
    elif cmd == "mys": my_support_stats(uid, mid, rl["support"]["id"])
    elif cmd == "myoff": my_off(uid, mid, rl["support"]["id"])
    elif cmd == "do" and a[0] in KIND: days_off_screen(uid, mid, KIND[a[0]], int(a[1]))
    elif cmd == "doa" and a[0] in KIND:
        STATE[uid] = ("doff", (KIND[a[0]], int(a[1])))
        send(uid, "🏖 <b>Выходной</b>\n\nНапишите дату: <code>25.11</code>\nили период: <code>25.11-27.11</code>", [[B("↩️ Отмена", f"do:{a[0]}:{a[1]}")]])
    elif cmd == "dod" and a[0] in KIND:
        day_off_remove(KIND[a[0]], int(a[1]))
        days_off_screen(uid, mid, KIND[a[0]], int(a[2]))
    elif cmd == "tk": ticket_view(uid, mid, i)
    elif cmd == "tkr": STATE[uid] = ("reply", i); send(uid, f"✍️ Напишите ответ на вопрос <b>#{i}</b> одним сообщением.", [[B("↩️ Отмена", f"tk:{i}"), B("📋 Все вопросы", "tks")]])
    elif cmd == "tkc": ticket_close(i); screen(uid, mid, f"✅ Вопрос #{i} закрыт.", [[B("📋 Все вопросы", "tks")]])
    # часы и чат
    elif cmd == "hr":
        if a:
            title = {"week": "Эта неделя", "prev": "Прошлая неделя", "month": "Этот месяц"}.get(a[0], "Неделя")
            screen(uid, mid, stats_text(f"Часы работы · {title}", *period_range(a[0])) + support_tail(period_range(a[0])), [[B("⬅️ К периодам", "hr")]] + MAIN)
        else:
            screen(uid, mid, "📊 <b>Часы работы мастеров</b>\nЗа какой период?",
                   [[B("🗓 Эта неделя", "hr:week"), B("⏮ Прошлая", "hr:prev")], [B("📆 Этот месяц", "hr:month")]] + MAIN)
    elif cmd == "ch":
        link, gid = get_setting("chat_link"), group_id()
        text = (f"🔗 <b>Общий чат салона</b>\n\nСсылка: {esc(link) if link else '🔒 не задана (мастера видят «временно недоступна»)'}\n"
                f"Группа для проверки участников: {'✅ подключена' if gid else '⚠️ не подключена'}\n\n"
                "<i>Чтобы подключить группу, добавьте бота в неё администратором и отправьте там /setgroup</i>")
        screen(uid, mid, text, [[B("✏️ Изменить ссылку" if link else "➕ Добавить ссылку", "cha")]] + ([[B("🙈 Временно убрать ссылку", "chd")]] if link else []) + MAIN)
    elif cmd == "cha": STATE[uid] = ("set_link", None); send(uid, "🔗 Пришлите ссылку-приглашение в общий чат (https://t.me/...).\n(/cancel — отмена)")
    elif cmd == "chd":
        set_setting("chat_link", "")
        screen(uid, mid, "🙈 Ссылка убрана. Мастера увидят, что она временно недоступна.", [[B("➕ Добавить ссылку", "cha")]] + MAIN)


def handle_update(u):
    if "callback_query" in u:
        on_callback(u["callback_query"])
    elif "message" in u:
        on_message(u["message"])
    elif "chat_member" in u:
        cm = u["chat_member"]
        if cm["new_chat_member"]["status"] == "member" and cm["old_chat_member"]["status"] in ("left", "kicked"):
            check_member(cm["chat"]["id"], cm["new_chat_member"]["user"])


# ---------- фоновые задачи ----------
def tick():
    now = now_local()
    today = now.date()
    with closing(db()) as c:
        due = c.execute("""SELECT b.id, b.date, b.time, b.r24, b.r2, b.client_chat_id, m.name AS master FROM bookings b
                           JOIN masters m ON m.id=b.master_id
                           WHERE b.client_chat_id IS NOT NULL AND b.date BETWEEN ? AND ? AND (b.r24=0 OR b.r2=0)""",
                        (today.isoformat(), (today + timedelta(days=1)).isoformat())).fetchall()
    for r in due:  # напоминания клиентам: за сутки и за 2 часа
        delta = datetime.fromisoformat(f"{r['date']}T{r['time']}") - now
        if delta <= timedelta(0):
            continue
        if delta <= timedelta(hours=2) and not r["r2"]:
            when, flags = "⏰ <b>Через 2 часа</b> ваша запись!", "r2=1, r24=1"
        elif delta <= timedelta(hours=24) and not r["r24"] and delta > timedelta(hours=2):
            when, flags = ("⏰ <b>Напоминание:</b> сегодня у вас запись!" if r["date"] == today.isoformat() else "⏰ <b>Напоминание:</b> завтра у вас запись!"), "r24=1"
        else:
            continue
        send(r["client_chat_id"], f"{when}\n\n📅 {fmt_date(r['date'])}\n🕒 {r['time']}\n👩‍🎨 Мастер: {esc(r['master'])}\n\nЖдём вас! 💅")
        with closing(db()) as c:
            c.execute(f"UPDATE bookings SET {flags} WHERE id=?", (r["id"],))
    if now.hour >= 8 and get_setting("digest_date") != today.isoformat():  # утренняя сводка мастерам
        set_setting("digest_date", today.isoformat())
        with closing(db()) as c:
            ms = c.execute("SELECT id, name, telegram_id FROM masters WHERE active=1 AND telegram_id IS NOT NULL").fetchall()
            for m in ms:
                rows = c.execute("SELECT time, name, phone FROM bookings WHERE master_id=? AND date=? ORDER BY time", (m["id"], today.isoformat())).fetchall()
                if rows:
                    lines = "\n".join(f"🕒 {r['time']} · 👤 {esc(r['name'])} · 📞 {esc(r['phone'])}" for r in rows)
                    send(m["telegram_id"], f"🌅 <b>Доброе утро, {esc(m['name'])}!</b>\n\nСегодня у вас {len(rows)} {plural(len(rows), ('запись', 'записи', 'записей'))}:\n\n{lines}")
    if today.weekday() == 6 and now.hour >= 21:  # недельная статистика в общую группу
        key = (today - timedelta(days=6)).isoformat()
        if get_setting("stats_week") != key:
            set_setting("stats_week", key)
            text = stats_text("Итоги недели: кто сколько проработал", *period_range("week")) + support_tail(period_range("week"))
            gid = group_id()
            send(gid, text) if gid else send(OWNER, text + "\n\n⚠️ Группа не подключена — отправьте /setgroup в общем чате.")


def poll():
    me = api("getMe")
    if not me:
        print("БОТ НЕ ЗАПУЩЕН: Telegram не принял BOT_TOKEN (проверьте токен в переменных)", flush=True)
        return
    print(f"Бот @{me.get('username')} запущен, жду сообщения", flush=True)
    api("deleteWebhook")
    api("setMyCommands", commands=[{"command": "start", "description": "Главное меню"}, {"command": "id", "description": "Узнать свой Telegram ID"}])
    offset = None
    while True:
        try:
            p = {"timeout": 30, "allowed_updates": json.dumps(["message", "callback_query", "chat_member"])}
            if offset is not None:
                p["offset"] = offset
            r = api_raw("getUpdates", **p)
            if not r.get("ok"):  # например, «Conflict»: тот же бот запущен ещё где-то
                print("getUpdates:", r.get("description", "нет ответа от Telegram"), flush=True)
                time.sleep(5)
                continue
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                try:
                    handle_update(u)
                except Exception as e:  # ошибка в одном сообщении не должна останавливать бота
                    print("bot error:", repr(e))
        except Exception:
            time.sleep(5)


def scheduler():
    while True:
        try:
            tick()
        except Exception as e:
            print("scheduler error:", repr(e))
        time.sleep(60)


def start_bot():
    if not BOT_TOKEN:
        print("БОТ НЕ ЗАПУЩЕН: переменная BOT_TOKEN не задана", flush=True)
        return
    threading.Thread(target=poll, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
