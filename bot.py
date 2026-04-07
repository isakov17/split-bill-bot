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

# Временное хранилище для настроек траты
pending_expenses = {}

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

# --- ГЕНЕРАТОР КЛАВИАТУРЫ ---
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
    history = await db.get_history(message.chat.id, limit=1000)
    if not history: return await message.answer("Нет данных.")
    
    filename = f"expenses_{message.chat.id}.csv"
    with open(filename, mode='w', encoding='utf-8-sig', newline='') as file:
        writer = csv.writer(file, delimiter=';')
        writer.writerow(["ID", "Сумма", "Описание", "Плательщик"])
        for row in history: writer.writerow(row)
        
    await message.answer_document(FSInputFile(filename), caption="📊 Отчет по тратам")
    os.remove(filename)

# --- ОБРАБОТКА ТРАТЫ ---
@dp.message(F.text.regexp(r'^(?:\+|/add\s+)?\d+'))
async def process_expense(message: types.Message):
    pattern = r'^(?:(?P<prefix>\+|/add)\s*)?(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<currency>[р₽$€])?\s+(?P<desc>.+)$'
    match = re.match(pattern, message.text, re.IGNORECASE)
    
    if not match or (not match.group('prefix') and not match.group('currency')):
        return # Игнорируем обычные сообщения с цифрами

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

# --- CALLBACKS ---

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
    
    creditors = sorted([[d["name"], d["balance"]] for d in balances.values() if d["balance"] > 0.01], key=lambda x: x[1], reverse=True)
    debtors = sorted([[d["name"], -d["balance"]] for d in balances.values() if d["balance"] < -0.01], key=lambda x: x[1], reverse=True)
    
    res = []
    i = j = 0
    while i < len(creditors) and j < len(debtors):
        pay = min(creditors[i][1], debtors[j][1])
        res.append(f"💸 <b>{debtors[j][0]}</b> ➔ <b>{creditors[i][0]}</b>: {pay:.2f}")
        creditors[i][1] -= pay
        debtors[j][1] -= pay
        if creditors[i][1] < 0.01: i += 1
        if debtors[j][1] < 0.01: j += 1

    report = "📊 <b>ИТОГИ:</b>\n\n" + ("\n".join(res) if res else "✅ Все в расчете!")
    await message.answer(report, parse_mode="HTML")

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