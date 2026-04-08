import asyncio
import re
import os
import logging
import csv
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile

import database as db

# Настройка логирования
logging.basicConfig(level=logging.INFO)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    exit("Ошибка: Токен не найден в файле .env!")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Временные хранилища
pending_expenses = {}
active_calcs = {} # Хранит сгенерированные переводы для каждого чата

# --- АВТО-КАТЕГОРИИ (ЭМОДЗИ) ---
def get_emoji_for_desc(desc: str) -> str:
    desc_lower = desc.lower()
    categories = {
        "🍔": ["бургер", "еда", "ресторан", "кафе", "пицца", "суши", "мак", "кфс", "обед", "ужин", "завтрак"],
        "🍻": ["пиво", "вино", "алко", "бар", "клуб", "коктейли", "водка", "напитки", "виски", "шампанское"],
        "🚕": ["такси", "бензин", "заправка", "транспорт", "автобус", "билеты", "метро", "убер", "электричка"],
        "🏠": ["отель", "жилье", "дом", "квартира", "аренда", "хостел", "жильё"],
        "🛒": ["продукты", "магазин", "супермаркет", "пятерочка", "перекресток", "ашан", "лента", "вода"],
        "🎉": ["подарок", "развлечения", "квест", "музей", "кино", "экскурсия", "билет"]
    }
    for emoji, keywords in categories.items():
        if any(word in desc_lower for word in keywords):
            return emoji
    return "🧾"

# --- ГЕНЕРАТОР КЛАВИАТУРЫ ДЛЯ ТРАТ ---
def get_expense_keyboard(expense_key: str, participants: dict, shares: dict, payer_id: int):
    builder = InlineKeyboardBuilder()
    
    # Кнопки участников
    for uid, name in participants.items():
        status = "✅" if shares.get(uid, False) else "❌"
        builder.button(text=f"{status} {name}", callback_data=f"tgl_{expense_key}_{uid}")
    
    builder.adjust(2) 
    
    # Массовое управление
    if len(participants) > 2:
        builder.row(
            types.InlineKeyboardButton(text="✅ Выбрать всех", callback_data=f"all_{expense_key}"),
            types.InlineKeyboardButton(text="❌ Снять всех", callback_data=f"none_{expense_key}")
        )
    
    payer_name = participants.get(payer_id, "Неизвестно")
    
    builder.row(types.InlineKeyboardButton(
        text=f"👤 Платил(а): {payer_name} 🔄", 
        callback_data=f"payer_{expense_key}"
    ))
    builder.row(
        types.InlineKeyboardButton(text="💾 Сохранить", callback_data=f"save_{expense_key}"),
        types.InlineKeyboardButton(text="🗑 Отмена", callback_data=f"cancel_{expense_key}")
    )
    
    return builder.as_markup()

def get_calc_message(chat_id: int):
    transfers = active_calcs.get(chat_id, [])
    if not transfers:
        return "✅ Все в расчете!", None

    text_lines = ["📊 <b>КТО КОМУ ПЕРЕВОДИТ:</b>\n"]
    
    senders = {}
    for t in transfers:
        if t["from_name"] not in senders:
            senders[t["from_name"]] = []
        senders[t["from_name"]].append(t)

    for sender_name, sender_transfers in senders.items():
        total_send = sum(t["amount"] for t in sender_transfers)
        text_lines.append(f"\n👤 <b>{sender_name}</b> (всего к оплате: {total_send:.2f})")
        
        for t in sender_transfers:
            is_paid = t.get("expense_id") is not None
            status = "✅" if is_paid else "❌"
            strike_s = "<s>" if is_paid else ""
            strike_e = "</s>" if is_paid else ""
            text_lines.append(f"  {status} {strike_s}➔ {t['to_name']}: {t['amount']:.2f}{strike_e}")

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="💳 Мои долги", callback_data=f"my_debts_{chat_id}"))
    builder.row(types.InlineKeyboardButton(text="🔔 Пингануть должников", callback_data="ping_debtors"))

    return "\n".join(text_lines), builder.as_markup()

from aiogram.exceptions import TelegramForbiddenError

@dp.callback_query(F.data.startswith("my_debts_"))
async def show_personal_cabinet(callback: types.CallbackQuery):
    chat_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    
    if chat_id not in active_calcs:
        return await callback.answer("Данные устарели. Обновите через /calc", show_alert=True)

    # Ищем долги конкретно этого пользователя
    user_debts = [
        (idx, t) for idx, t in enumerate(active_calcs[chat_id]) 
        if t["from_uid"] == user_id and t.get("expense_id") is None
    ]

    if not user_debts:
        return await callback.answer("У вас нет неоплаченных долгов! 🎉", show_alert=True)

    text_lines = ["💳 <b>Ваши неоплаченные долги:</b>\n"]
    builder = InlineKeyboardBuilder()

    for idx, t in user_debts:
        text_lines.append(f"➔ <b>{t['to_name']}</b>: {t['amount']:.2f}")
        # Передаем индекс перевода и chat_id для оплаты
        builder.button(text=f"✅ Оплатил {t['to_name']} ({t['amount']:.2f})", callback_data=f"pay_{chat_id}_{idx}")

    builder.adjust(1)

    try:
        # Пытаемся отправить сообщение в личку
        await bot.send_message(user_id, "\n".join(text_lines), reply_markup=builder.as_markup(), parse_mode="HTML")
        await callback.answer("Отправил список в личные сообщения! 📩")
    except TelegramForbiddenError:
        # Если бот заблокирован или не запущен пользователем
        await callback.answer(
            "❌ Ошибка!\nСначала напишите мне в личные сообщения (нажмите Start), чтобы я мог прислать ваши долги.", 
            show_alert=True
        )

@dp.callback_query(F.data.startswith("pay_"))
async def process_personal_payment(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    chat_id = int(parts[1])
    idx = int(parts[2])

    if chat_id not in active_calcs or idx >= len(active_calcs[chat_id]):
        return await callback.message.edit_text("Данные устарели. Запросите новый список долгов в группе.")

    t = active_calcs[chat_id][idx]

    if t.get("expense_id") is None:
        # Записываем в базу
        exp_id = await db.save_full_expense(
            chat_id, 
            payer_id=t["from_uid"], 
            amount=t["amount"], 
            description="💸 Возврат долга", 
            shares=[t["to_uid"]]
        )
        t["expense_id"] = exp_id
        
        # Обновляем сообщение в личке, убирая кнопку
        await callback.message.edit_text(
            f"✅ <b>Отлично!</b>\nПеревод {t['to_name']} на сумму {t['amount']:.2f} зафиксирован.",
            parse_mode="HTML"
        )
        await callback.answer()
        
        # ОПЦИОНАЛЬНО: Можно заставить бота тут же обновить сообщение в группе, 
        # но для этого нужно где-то хранить message_id сообщения с /calc. 
        # Чаще всего людям проще просто написать /calc еще раз, чтобы увидеть свежий итог.


@dp.message(Command("mock"))
async def cmd_mock(message: types.Message):
    fake_friends = ["Арсений", "Родион"]
    for i, name in enumerate(fake_friends):
        await db.add_participant(message.chat.id, 1000 + i, name)
    await message.answer("🤖 <b>Режим теста:</b> Добавлено 2 фейковых друзей!")

# --- ЗАЩИТА ОТ ПЕРЕЗАГРУЗКИ ---
async def check_cache_or_delete(callback: types.CallbackQuery, expense_key: str):
    expense = pending_expenses.get(expense_key)
    if not expense:
        await callback.message.edit_text(
            f"<i>{callback.message.text}</i>\n\n"
            "⚠️ <b>Ошибка:</b> Данные устарели (бот был перезагружен).\n"
            "Пожалуйста, отправьте эту трату заново.", 
            parse_mode="HTML"
        )
        await callback.answer("Сессия истекла", show_alert=True)
        return None
    return expense

# --- КОМАНДЫ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "✈️ <b>Бот для разделения трат в поездках!</b>\n\n"
        "1️⃣ Все участники жмут <b>/join</b>\n"
        "2️⃣ Пишите трату в любом формате:\n"
        "   • <code>+1500 Вино</code>\n"
        "   • <code>500р Кофе</code>\n"
        "   • <code>/add 2000 Такси</code>\n"
        "3️⃣ Настройте, кто платил и на кого делить.\n\n"
        "📊 <b>Команды:</b>\n"
        "<b>/calc</b> — расчет долгов\n"
        "<b>/me</b> — мой баланс\n"
        "<b>/history</b> — последние траты\n"
        "<b>/export</b> — выгрузка в Excel\n"
        "<b>/reset</b> — начать новую поездку",
        parse_mode="HTML"
    )

@dp.message(Command("join"))
async def cmd_join(message: types.Message):
    await db.add_participant(message.chat.id, message.from_user.id, message.from_user.first_name)
    await message.answer(f"✅ <b>{message.from_user.first_name}</b> в игре!", parse_mode="HTML")

@dp.message(Command("me"))
async def cmd_me(message: types.Message):
    balances = await db.get_balances(message.chat.id)
    user_data = balances.get(message.from_user.id)
    if not user_data:
        return await message.answer("Сначала нажмите /join")
    
    bal = user_data["balance"]
    if bal > 0.01: text = f"💰 Ваш баланс: <b>+{bal:.2f}</b> (вам должны)"
    elif bal < -0.01: text = f"💸 Ваш баланс: <b>{bal:.2f}</b> (вы должны)"
    else: text = "⚖️ Ваш баланс: <b>0.00</b> (вы в расчете)"
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("history"))
async def cmd_history(message: types.Message):
    history = await db.get_history(message.chat.id, limit=10)
    if not history: return await message.answer("Трат пока нет.")
    
    await message.answer("<b>Последние 10 трат:</b>", parse_mode="HTML")
    for exp_id, amount, desc, payer in history:
        builder = InlineKeyboardBuilder()
        builder.button(text="🗑 Удалить", callback_data=f"delexp_{exp_id}")
        await message.answer(f"🧾 <b>{amount:.2f}</b> — {desc}\nПлатил(а): {payer or '???'}", 
                             reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.message(Command("export"))
async def cmd_export(message: types.Message):
    history = await db.get_history_with_shares(message.chat.id, limit=1000)
    
    if not history:
        return await message.answer("Нет данных для выгрузки.")

    filename = f"expenses_{message.chat.id}.csv"
    
    with open(filename, mode='w', encoding='utf-8-sig', newline='') as file:
        writer = csv.writer(file, delimiter=';')
        writer.writerow(["ID", "Сумма", "Описание", "Кто платил", "На кого делили"]) 
        for row in history:
            writer.writerow(row)

    document = FSInputFile(filename)
    await message.answer_document(document, caption="📊 Детальный отчет по всем тратам")
    os.remove(filename)

# --- ОБРАБОТКА ТРАТЫ ---
@dp.message(F.text.regexp(r'^(?:\+|/add\s+)?\d+'))
async def process_expense(message: types.Message):
    pattern = r'^(?:(?P<prefix>\+|/add)\s*)?(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<currency>[р₽$€])?\s+(?P<desc>.+)$'
    match = re.match(pattern, message.text, re.IGNORECASE)
    
    if not match or (not match.group('prefix') and not match.group('currency')):
        return 

    amount = float(match.group('amount').replace(',', '.'))
    raw_desc = match.group('desc').strip()
    desc = f"{get_emoji_for_desc(raw_desc)} {raw_desc}"
    
    await db.add_participant(message.chat.id, message.from_user.id, message.from_user.first_name)
    participants = await db.get_participants(message.chat.id)
    
    expense_key = f"{message.chat.id}_{message.message_id}"
    pending_expenses[expense_key] = {
        "creator_id": message.from_user.id,
        "payer_id": message.from_user.id,
        "amount": amount,
        "desc": desc,
        "shares": {uid: True for uid in participants.keys()}
    }
    
    kb = get_expense_keyboard(expense_key, participants, pending_expenses[expense_key]["shares"], message.from_user.id)
    await message.reply(f"💰 <b>{amount:.2f}</b> — <i>{desc}</i>\nНа кого делим?", 
                        reply_markup=kb, parse_mode="HTML")

# --- CALLBACKS ДЛЯ ТРАТ ---

@dp.callback_query(F.data.startswith("tgl_"))
async def toggle_share(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    expense_key = f"{parts[1]}_{parts[2]}"
    target_uid = int(parts[3])
    
    expense = await check_cache_or_delete(callback, expense_key)
    if not expense or callback.from_user.id != expense["creator_id"]: return
    
    expense["shares"][target_uid] = not expense["shares"][target_uid]
    participants = await db.get_participants(callback.message.chat.id)
    kb = get_expense_keyboard(expense_key, participants, expense["shares"], expense["payer_id"])
    try: await callback.message.edit_reply_markup(reply_markup=kb)
    except TelegramBadRequest: pass
    await callback.answer()

@dp.callback_query(F.data.startswith(("all_", "none_")))
async def bulk_toggle(callback: types.CallbackQuery):
    is_all = callback.data.startswith("all_")
    expense_key = callback.data.replace("all_", "").replace("none_", "")
    
    expense = await check_cache_or_delete(callback, expense_key)
    if not expense or callback.from_user.id != expense["creator_id"]: return
    
    expense["shares"] = {uid: is_all for uid in expense["shares"].keys()}
    participants = await db.get_participants(callback.message.chat.id)
    kb = get_expense_keyboard(expense_key, participants, expense["shares"], expense["payer_id"])
    try: await callback.message.edit_reply_markup(reply_markup=kb)
    except TelegramBadRequest: pass
    await callback.answer()

@dp.callback_query(F.data.startswith("payer_"))
async def change_payer(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    expense_key = f"{parts[1]}_{parts[2]}"
    
    expense = await check_cache_or_delete(callback, expense_key)
    if not expense or callback.from_user.id != expense["creator_id"]: return

    participants = await db.get_participants(callback.message.chat.id)
    uids = list(participants.keys())
    if len(uids) <= 1: return await callback.answer("Нужно больше участников!", show_alert=True)
    
    current_idx = uids.index(expense["payer_id"]) if expense["payer_id"] in uids else -1
    expense["payer_id"] = uids[(current_idx + 1) % len(uids)]
    
    kb = get_expense_keyboard(expense_key, participants, expense["shares"], expense["payer_id"])
    try: await callback.message.edit_reply_markup(reply_markup=kb)
    except TelegramBadRequest: pass
    await callback.answer()

@dp.callback_query(F.data.startswith("save_"))
async def save_expense(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    expense_key = f"{parts[1]}_{parts[2]}"
    expense = await check_cache_or_delete(callback, expense_key)
    if not expense or callback.from_user.id != expense["creator_id"]: return

    included = [uid for uid, val in expense["shares"].items() if val]
    if not included: return await callback.answer("Выберите участников!", show_alert=True)

    await db.save_full_expense(callback.message.chat.id, expense["payer_id"], expense["amount"], expense["desc"], included)
    del pending_expenses[expense_key]
    
    participants = await db.get_participants(callback.message.chat.id)
    payer_name = participants.get(expense['payer_id'], '???')
    await callback.message.edit_text(
        f"✅ <b>Сохранено!</b>\nСумма: {expense['amount']:.2f} ({expense['desc']})\n"
        f"Платил(а): {payer_name}\nНа {len(included)} чел.", parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("cancel_"))
async def cancel_expense(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    expense_key = f"{parts[1]}_{parts[2]}"
    if expense_key in pending_expenses: del pending_expenses[expense_key]
    await callback.message.edit_text("🗑 Трата отменена.")

@dp.callback_query(F.data.startswith("delexp_"))
async def delete_history_exp(callback: types.CallbackQuery):
    exp_id = int(callback.data.split("_")[1])
    await db.delete_expense(exp_id)
    await callback.message.edit_text("🗑 Трата удалена.")

# --- РАСЧЕТ И СБРОС ---

@dp.message(Command("calc"))
async def cmd_calc(message: types.Message):
    balances = await db.get_balances(message.chat.id)
    if not balances: return await message.answer("Данных нет.")
    
    # Собираем кредиторов (кому должны) и должников (кто должен)
    creditors = sorted([[uid, d["name"], d["balance"]] for uid, d in balances.items() if d["balance"] > 0.01], key=lambda x: x[2], reverse=True)
    debtors = sorted([[uid, d["name"], -d["balance"]] for uid, d in balances.items() if d["balance"] < -0.01], key=lambda x: x[2], reverse=True)
    
    transfers = []
    i = j = 0
    while i < len(creditors) and j < len(debtors):
        c_uid, c_name, c_amt = creditors[i]
        d_uid, d_name, d_amt = debtors[j]
        
        pay = min(c_amt, d_amt)
        
        transfers.append({
            "from_uid": d_uid,
            "from_name": d_name,
            "to_uid": c_uid,
            "to_name": c_name,
            "amount": pay,
            "expense_id": None # Пока не оплачено, ID = None
        })
        
        creditors[i][2] -= pay
        debtors[j][2] -= pay
        if creditors[i][2] < 0.01: i += 1
        if debtors[j][2] < 0.01: j += 1

    if not transfers:
        return await message.answer("✅ Все в расчете!")

    # Сохраняем переводы в память для инлайн-кнопок
    active_calcs[message.chat.id] = transfers
    
    text, kb = get_calc_message(message.chat.id)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "ping_debtors")
async def ping_debtors_call(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    if chat_id not in active_calcs:
        return await callback.answer("Данные устарели. Нажмите /calc снова.", show_alert=True)

    debtors = set()
    for t in active_calcs[chat_id]:
        if t["expense_id"] is None:
            # Тегаем пользователя через встроенную ссылку Telegram
            debtors.add(f'<a href="tg://user?id={t["from_uid"]}">{t["from_name"]}</a>')

    if not debtors:
        return await callback.answer("Все всё перевели, должников нет! 🎉", show_alert=True)

    ping_text = "💰 <b>Напоминание!</b>\nПожалуйста, не забудьте перевести деньги:\n" + ", ".join(debtors)
    await callback.message.answer(ping_text, parse_mode="HTML")
    await callback.answer()


@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔥 СТЕРЕТЬ ВСЁ", callback_data="conf_reset")
    builder.button(text="ОТМЕНА", callback_data="cancel_reset")
    await message.answer("Удалить все данные поездки?", reply_markup=builder.as_markup())

@dp.callback_query(F.data == "conf_reset")
async def conf_reset(callback: types.CallbackQuery):
    await db.clear_chat_data(callback.message.chat.id)
    await callback.message.edit_text("🧹 База очищена!")

@dp.callback_query(F.data == "cancel_reset")
async def cancel_reset(callback: types.CallbackQuery):
    await callback.message.edit_text("Действие отменено.")

async def main():
    await db.init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())