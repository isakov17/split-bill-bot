import asyncio
import re
import os
import logging
import csv
import io
import uuid
import random
import aiohttp
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import FSInputFile
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

import database as db

# --- НАСТРОЙКА ---
logging.basicConfig(level=logging.INFO)
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
API_TOKEN = os.getenv("PROVERKA_TOKEN")

if not TOKEN:
    exit("Ошибка: Токен BOT_TOKEN не найден в файле .env!")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- ВРЕМЕННЫЕ ХРАНИЛИЩА ---
pending_expenses = {}  # Для ручных трат
active_calcs = {}      # Хранит сгенерированные переводы: {chat_id: {tx_id: data}}

class ReceiptCarousel(StatesGroup):
    chat_id = State()
    payer_id = State()
    items = State()       # Список товаров из чека
    current_idx = State() # Текущая позиция карусели

# --- ПРЕСЕТ ПОСТОЯННЫХ УЧАСТНИКОВ ---
PRESET_FRIENDS = {
    "khlbvnk": {"name": "Арсений", "phone": "+7 987 031 8998"},
    "yourmiraclle": {"name": "Ренатуля🍤", "phone": "+7 917 761 2706"},
    "grystyji": {"name": "Маша", "phone": "+7 917 384 0373"},
    "wwwrts_20": {"name": "Дима", "phone": "+7 919 604 6374"},
    "gzzxgd": {"name": "Юля", "phone": "+7 917 733 9668"},
    "tt22119": {"name": "Антон", "phone": "+7 987 477 5269"},
    "gera_is1": {"name": "German", "phone": "+7 917 443 6532"}
}

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_phone_by_name(name: str) -> str:
    for data in PRESET_FRIENDS.values():
        if data["name"] == name:
            return data["phone"]
    return None

def get_emoji_for_desc(desc: str) -> str:
    desc_lower = desc.lower()
    categories = {
        "🍔": ["бургер", "еда", "ресторан", "кафе", "пицца", "суши", "мак", "кфс", "обед", "ужин", "завтрак"],
        "🍻": ["пиво", "вино", "алко", "бар", "клуб", "коктейли", "водка", "напитки", "виски"],
        "🚕": ["такси", "бензин", "заправка", "транспорт", "автобус", "метро", "убер"],
        "🏠": ["отель", "жилье", "дом", "квартира", "аренда", "хостел"],
        "🛒": ["продукты", "магазин", "супермаркет", "пятерочка", "ашан", "лента", "вода"],
        "🎉": ["подарок", "развлечения", "квест", "музей", "кино", "билет"]
    }
    for emoji, keywords in categories.items():
        if any(word in desc_lower for word in keywords):
            return emoji
    return "🧾"

def get_expense_keyboard(expense_key: str, participants: dict, shares: dict, payer_id: int):
    builder = InlineKeyboardBuilder()
    
    for uid, name in participants.items():
        status = "✅" if shares.get(uid, False) else "❌"
        builder.button(text=f"{status} {name}", callback_data=f"tgl_{expense_key}_{uid}")
    builder.adjust(2) 
    
    if len(participants) > 2:
        builder.row(
            types.InlineKeyboardButton(text="✅ Выбрать всех", callback_data=f"all_{expense_key}"),
            types.InlineKeyboardButton(text="❌ Снять всех", callback_data=f"none_{expense_key}")
        )
    
    payer_name = participants.get(payer_id, "Неизвестно")
    builder.row(types.InlineKeyboardButton(text=f"👤 Платил(а): {payer_name} 🔄", callback_data=f"payer_{expense_key}"))
    builder.row(
        types.InlineKeyboardButton(text="💾 Сохранить", callback_data=f"save_{expense_key}"),
        types.InlineKeyboardButton(text="🗑 Отмена", callback_data=f"cancel_{expense_key}")
    )
    return builder.as_markup()

async def check_cache_or_delete(callback: types.CallbackQuery, expense_key: str):
    expense = pending_expenses.get(expense_key)
    if not expense:
        await callback.message.edit_text(
            f"<i>{callback.message.text}</i>\n\n⚠️ <b>Ошибка:</b> Данные устарели.\nПожалуйста, отправьте эту трату заново.", 
            parse_mode="HTML"
        )
        await callback.answer("Сессия истекла", show_alert=True)
        return None
    return expense


# --- БАЗОВЫЕ КОМАНДЫ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "✈️ <b>Бот для разделения трат в поездках!</b>\n\n"
        "1️⃣ Все участники жмут <b>/join</b>\n"
        "2️⃣ Пишите трату в любом формате:\n"
        "   • <code>+1500 Вино</code>\n"
        "   • <code>/add 2000 Такси</code>\n"
        "   • Или отправьте фото чека с подписью <code>чек</code>\n\n"
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

@dp.message(Command("mock"))
async def cmd_mock(message: types.Message):
    fake_friends = ["Арсений", "Родион", "Кирилл", "Маша", "Дима", "Лена", "Юля"]
    for i, name in enumerate(fake_friends):
        await db.add_participant(message.chat.id, 1000 + i, name)
    await message.answer(f"🤖 <b>Режим теста:</b> Добавлено {len(fake_friends)} фейковых друзей!", parse_mode="HTML")

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
        await message.answer(f"🧾 <b>{amount:.2f}</b> — {desc}\nПлатил(а): {payer or '???'}", reply_markup=builder.as_markup(), parse_mode="HTML")

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


# --- ЕДИНЫЙ ОБРАБОТЧИК ДОБАВЛЕНИЯ (/add, +) ---

@dp.message(F.text.regexp(r'^(?:\+|/add\s+|/add$)'))
async def smart_add_handler(message: types.Message):
    text = message.text.strip()
    
    # 1. Принудительное добавление пользователя (Свайп + /add)
    if message.reply_to_message and text in ["/add", "+"]:
        target = message.reply_to_message.from_user
        if target.is_bot:
            return await message.answer("🤖 Ботов добавлять нельзя!")
        await db.add_participant(message.chat.id, target.id, target.first_name)
        return await message.answer(f"✅ <b>{target.first_name}</b> принудительно добавлен в расчеты!", parse_mode="HTML")

    # 2. Ручная трата (например: +500 пиво или /add 500 пиво)
    pattern = r'^(?:(?P<prefix>\+|/add)\s*)?(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<currency>[р₽$€])?\s+(?P<desc>.+)$'
    match = re.match(pattern, message.text, re.IGNORECASE)
    
    if match:
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
        return await message.reply(f"💰 <b>{amount:.2f}</b> — <i>{desc}</i>\nНа кого делим?", reply_markup=kb, parse_mode="HTML")
    
    # Если написали просто /add без параметров и без свайпа
    if text == "/add":
        await message.answer(
            "ℹ️ <b>Как пользоваться:</b>\n"
            "• <code>/add 500 кофе</code> — добавить трату\n"
            "• <code>/add</code> (в ответ на сообщение) — добавить друга", parse_mode="HTML"
        )


# --- CALLBACKS ДЛЯ РУЧНЫХ ТРАТ ---

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


# --- ОБРАБОТКА ЧЕКОВ (OCR) ---

@dp.message(F.photo, F.caption.regexp(r'(?i)(чек|/scan|скан)'))
async def handle_receipt_photo(message: types.Message, state: FSMContext):
    await process_receipt_image(message, message.photo[-1], state)

@dp.message(Command("scan"))
async def handle_receipt_reply(message: types.Message, state: FSMContext):
    if not message.reply_to_message or not message.reply_to_message.photo:
        return await message.answer("⚠️ Отправьте фото с подписью <b>«чек»</b>, или ответьте на фото командой <b>/scan</b>.", parse_mode="HTML")
    await process_receipt_image(message, message.reply_to_message.photo[-1], state)

async def process_receipt_image(message: types.Message, photo: types.PhotoSize, state: FSMContext):
    if not API_TOKEN: return await message.answer("API токен для чеков не настроен.")
    
    msg = await message.answer("🔄 Анализирую QR-код чека...")
    file_info = await bot.get_file(photo.file_id)
    downloaded_file = await bot.download_file(file_info.file_path)
    
    url = 'https://proverkacheka.com/api/v1/check/get'
    data = aiohttp.FormData()
    data.add_field('token', API_TOKEN)
    data.add_field('qrfile', downloaded_file.read(), filename='receipt.jpg', content_type='image/jpeg')

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data) as resp:
                api_resp = await resp.json()
    except Exception as e:
        logging.error(f"API Error: {e}")
        return await msg.edit_text("❌ Ошибка соединения с сервером проверки чеков.")

    if api_resp.get("code") != 1:
        return await msg.edit_text("❌ Не удалось распознать чек. Убедитесь, что QR-код четкий.")

    raw_items = api_resp.get("data", {}).get("json", {}).get("items", [])
    if not raw_items:
        return await msg.edit_text("❌ Чек распознан, но товаров нет.")

    # Инициализация долей (shares) сразу при парсинге
    participants = await db.get_participants(message.chat.id)
    items = []
    for item in raw_items:
        items.append({
            "name": item['name'],
            "price": round(item['sum'] / 100, 2),
            "shares": {uid: False for uid in participants.keys()}
        })

    await state.set_state(ReceiptCarousel.items)
    await state.update_data(
        chat_id=message.chat.id,
        payer_id=message.from_user.id, 
        items=items,
        current_idx=0
    )
    await msg.delete()
    await send_carousel_item(message, state)

async def send_carousel_item(message: types.Message, state: FSMContext, edit_msg=None):
    data = await state.get_data()
    idx, items = data['current_idx'], data['items']
    chat_id, payer_id = data['chat_id'], data['payer_id']
    
    if idx >= len(items):
        await state.clear()
        final_text = "🎉 <b>Чек полностью распределен!</b>\nВсе позиции записаны в общий расчет."
        if edit_msg: return await edit_msg.edit_text(final_text, parse_mode="HTML")
        else: return await message.answer(final_text, parse_mode="HTML")

    item = items[idx]
    participants = await db.get_participants(chat_id)
    payer_name = participants.get(payer_id, "Неизвестно")
    
    builder = InlineKeyboardBuilder()
    for uid, name in participants.items():
        status = "✅" if item['shares'].get(uid) else "❌"
        builder.button(text=f"{status} {name}", callback_data=f"carshare_{uid}")
        
    builder.adjust(2)
    builder.row(types.InlineKeyboardButton(text=f"👤 Оплатил чек: {payer_name} 🔄", callback_data="car_change_payer"))
    builder.row(
        types.InlineKeyboardButton(text="👥 На всех", callback_data="car_all"),
        types.InlineKeyboardButton(text="➡️ Далее", callback_data="car_next")
    )
    
    text = (f"🧾 <b>Позиция {idx + 1} из {len(items)}</b>\n"
            f"🍔 <b>{item['name']}</b>\n"
            f"💰 Сумма: {item['price']:.2f} ₽\n\n"
            f"<i>Отметьте, кто это заказывал:</i>")

    if edit_msg: await edit_msg.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    else: await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("carshare_"))
async def carousel_toggle(callback: types.CallbackQuery, state: FSMContext):
    target_uid = int(callback.data.split("_")[1])
    data = await state.get_data()
    if not data: return await callback.answer("Сессия истекла")
    
    idx = data['current_idx']
    data['items'][idx]['shares'][target_uid] = not data['items'][idx]['shares'][target_uid]
    await state.update_data(items=data['items'])
    await send_carousel_item(callback.message, state, edit_msg=callback.message)
    await callback.answer()

@dp.callback_query(F.data == "car_all")
async def carousel_all(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    idx = data['current_idx']
    for uid in data['items'][idx]['shares'].keys():
        data['items'][idx]['shares'][uid] = True
    await state.update_data(items=data['items'])
    await send_carousel_item(callback.message, state, edit_msg=callback.message)
    await callback.answer()

@dp.callback_query(F.data == "car_change_payer")
async def carousel_change_payer(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    uids = list((await db.get_participants(data['chat_id'])).keys())
    curr_payer = data.get('payer_id')
    curr_idx = uids.index(curr_payer) if curr_payer in uids else -1
    next_payer = uids[(curr_idx + 1) % len(uids)]
    
    await state.update_data(payer_id=next_payer)
    await send_carousel_item(callback.message, state, edit_msg=callback.message)
    await callback.answer()

@dp.callback_query(F.data == "car_next")
async def carousel_next(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    idx = data['current_idx']
    item = data['items'][idx]
    
    included = [uid for uid, val in item['shares'].items() if val]
    if not included:
        return await callback.answer("Выберите хотя бы одного человека!", show_alert=True)
    
    await db.save_full_expense(data['chat_id'], data['payer_id'], item['price'], f"🧾 {item['name'][:20]}...", shares=included)
    await state.update_data(current_idx=idx + 1)
    await send_carousel_item(callback.message, state, edit_msg=callback.message)
    await callback.answer()


# --- РАСЧЕТ И ОПЛАТА ДОЛГОВ (/calc) ---

@dp.message(Command("calc"))
async def cmd_calc(message: types.Message):
    balances = await db.get_balances(message.chat.id)
    if not balances: return await message.answer("Данных нет.")
    
    creditors = sorted([[uid, d["name"], d["balance"]] for uid, d in balances.items() if d["balance"] > 0.01], key=lambda x: x[2], reverse=True)
    debtors = sorted([[uid, d["name"], -d["balance"]] for uid, d in balances.items() if d["balance"] < -0.01], key=lambda x: x[2], reverse=True)
    
    transfers = {}
    i = j = 0
    while i < len(creditors) and j < len(debtors):
        c_uid, c_name, c_amt = creditors[i]
        d_uid, d_name, d_amt = debtors[j]
        pay = round(min(c_amt, d_amt), 2)
        
        tx_id = uuid.uuid4().hex[:8]
        transfers[tx_id] = {
            "from_uid": d_uid, "from_name": d_name,
            "to_uid": c_uid, "to_name": c_name,
            "amount": pay, "paid": False
        }
        
        creditors[i][2] -= pay
        debtors[j][2] -= pay
        if creditors[i][2] < 0.01: i += 1
        if debtors[j][2] < 0.01: j += 1

    if not transfers: return await message.answer("✅ Все в расчете!")
    active_calcs[message.chat.id] = transfers

    text_lines = ["📊 <b>КТО КОМУ ПЕРЕВОДИТ:</b>\n"]
    senders = {}
    for t in transfers.values():
        if t["from_name"] not in senders: senders[t["from_name"]] = []
        senders[t["from_name"]].append(t)

    for sender_name, sender_transfers in senders.items():
        total_send = sum(t["amount"] for t in sender_transfers)
        text_lines.append(f"\n👤 <b>{sender_name}</b> (всего к оплате: {total_send:.2f})")
        for t in sender_transfers:
            status = "✅" if t["paid"] else "❌"
            strike_s = "<s>" if t["paid"] else ""
            strike_e = "</s>" if t["paid"] else ""
            text_lines.append(f"  {status} {strike_s}➔ {t['to_name']}: {t['amount']:.2f}{strike_e}")

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="💳 Мои долги", callback_data=f"my_debts_{message.chat.id}"))
    builder.row(types.InlineKeyboardButton(text="🔔 Пингануть должников", callback_data="ping_debtors"))

    await message.answer("\n".join(text_lines), reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "ping_debtors")
async def ping_debtors_call(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    if chat_id not in active_calcs: return await callback.answer("Данные устарели. Нажмите /calc снова.", show_alert=True)

    debtors = set()
    for t in active_calcs[chat_id].values():
        if not t["paid"]:
            debtors.add(f'<a href="tg://user?id={t["from_uid"]}">{t["from_name"]}</a>')

    if not debtors: return await callback.answer("Все всё перевели, должников нет! 🎉", show_alert=True)
    await callback.message.answer("💰 <b>Напоминание!</b>\nПожалуйста, не забудьте перевести деньги:\n" + ", ".join(debtors), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("my_debts_"))
async def show_personal_cabinet(callback: types.CallbackQuery):
    chat_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    
    if chat_id not in active_calcs:
        return await callback.answer("Данные устарели. Обновите через /calc", show_alert=True)

    user_debts = {tid: t for tid, t in active_calcs[chat_id].items() if t["from_uid"] == user_id and not t["paid"]}
    if not user_debts: return await callback.answer("У вас нет неоплаченных долгов! 🎉", show_alert=True)

    text_lines = ["💳 <b>Ваши неоплаченные долги:</b>\n"]
    builder = InlineKeyboardBuilder()

    for tid, t in user_debts.items():
        phone = get_phone_by_name(t['to_name'])
        if phone: text_lines.append(f"➔ <b>{t['to_name']}</b>: {t['amount']:.2f} (СБП: <code>{phone}</code>)")
        else: text_lines.append(f"➔ <b>{t['to_name']}</b>: {t['amount']:.2f}")
        builder.button(text=f"✅ Оплатил {t['to_name']} ({t['amount']:.2f})", callback_data=f"pay_{chat_id}_{tid}")

    builder.adjust(1)
    try:
        await bot.send_message(user_id, "\n".join(text_lines), reply_markup=builder.as_markup(), parse_mode="HTML")
        await callback.answer("Отправил список в личные сообщения! 📩")
    except TelegramForbiddenError:
        await callback.answer("❌ Сначала напишите мне в личные сообщения (нажмите Start), чтобы я мог прислать долги.", show_alert=True)

@dp.callback_query(F.data.startswith("pay_"))
async def process_personal_payment(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    chat_id, tid = int(parts[1]), parts[2]
    t = active_calcs.get(chat_id, {}).get(tid)

    if not t: return await callback.message.edit_text("⚠️ Данные устарели. Запросите расчет заново в группе.")

    if not t["paid"]:
        try:
            builder = InlineKeyboardBuilder()
            builder.button(text="✅ Получил", callback_data=f"confpay_{chat_id}_{tid}")
            builder.button(text="❌ Не пришли", callback_data=f"rejectpay_{chat_id}_{tid}")

            await bot.send_message(
                t["to_uid"],
                f"💰 <b>Подтверждение платежа</b>\n\n"
                f"Пользователь <b>{t['from_name']}</b> утверждает, что перевел тебе <b>{t['amount']:.2f}</b>.\n"
                f"Подтверждаешь получение?",
                reply_markup=builder.as_markup(), parse_mode="HTML"
            )
            await callback.message.edit_text(f"⏳ <b>Запрос отправлен!</b>\nЖдем подтверждения от <b>{t['to_name']}</b>.", parse_mode="HTML")
        except TelegramForbiddenError:
            await callback.answer(f"❌ {t['to_name']} еще не запустил меня в личке. Попроси его нажать Start.", show_alert=True)

@dp.callback_query(F.data.startswith("confpay_"))
async def confirm_payment(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    chat_id, tid = int(parts[1]), parts[2]
    t = active_calcs.get(chat_id, {}).get(tid)
    
    if not t: return await callback.message.edit_text("Ошибка данных.")
    if t["paid"]: return await callback.message.edit_text("✅ Оплата уже подтверждена.")

    t["paid"] = True
    await db.save_full_expense(chat_id, t["from_uid"], t["amount"], "💸 Возврат долга", [t["to_uid"]])
    
    await callback.message.edit_text(f"✅ Получение {t['amount']:.2f} от {t['from_name']} подтверждено!")
    try: await bot.send_message(t["from_uid"], f"🎉 <b>{t['to_name']}</b> подтвердил получение перевода на {t['amount']:.2f}!", parse_mode="HTML")
    except: pass
    await bot.send_message(chat_id, f"💳 <b>Оплата подтверждена:</b> {t['from_name']} ➔ {t['to_name']} ({t['amount']:.2f})", parse_mode="HTML")

@dp.callback_query(F.data.startswith("rejectpay_"))
async def reject_payment(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    chat_id, tid = int(parts[1]), parts[2]
    t = active_calcs.get(chat_id, {}).get(tid)
    
    if not t: return
    await callback.message.edit_text(f"❌ Ты отклонил подтверждение перевода от {t['from_name']}.")
    try: await bot.send_message(t["from_uid"], f"⚠️ <b>{t['to_name']}</b> не подтвердил твой перевод на {t['amount']:.2f}. Проверь реквизиты.", parse_mode="HTML")
    except: pass


# --- СБРОС И ЗАПУСК ---

@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔥 СТЕРЕТЬ ВСЁ", callback_data="conf_reset")
    builder.button(text="ОТМЕНА", callback_data="cancel_reset")
    await message.answer("Удалить все данные поездки?", reply_markup=builder.as_markup())

@dp.callback_query(F.data == "conf_reset")
async def conf_reset(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    await db.clear_chat_data(chat_id)
    if chat_id in active_calcs: del active_calcs[chat_id]
    await callback.message.edit_text("🧹 База очищена!")

@dp.callback_query(F.data == "cancel_reset")
async def cancel_reset(callback: types.CallbackQuery):
    await callback.message.edit_text("Действие отменено.")

async def main():
    await db.init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())