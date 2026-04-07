import aiosqlite

DB_NAME = "split_bill.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS participants (
                chat_id INTEGER,
                user_id INTEGER,
                name TEXT,
                PRIMARY KEY (chat_id, user_id)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                payer_id INTEGER,
                amount REAL,
                description TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS expense_shares (
                expense_id INTEGER,
                user_id INTEGER,
                PRIMARY KEY (expense_id, user_id)
            )
        ''')
        await db.commit()

async def add_participant(chat_id: int, user_id: int, name: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO participants (chat_id, user_id, name) VALUES (?, ?, ?)",
            (chat_id, user_id, name)
        )
        await db.commit()

async def get_participants(chat_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, name FROM participants WHERE chat_id = ?", (chat_id,)) as cursor:
            return {row[0]: row[1] for row in await cursor.fetchall()}

async def save_full_expense(chat_id: int, payer_id: int, amount: float, description: str, shares: list) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT INTO expenses (chat_id, payer_id, amount, description) VALUES (?, ?, ?, ?)",
            (chat_id, payer_id, amount, description)
        )
        expense_id = cursor.lastrowid
        
        for user_id in shares:
            await db.execute(
                "INSERT INTO expense_shares (expense_id, user_id) VALUES (?, ?)",
                (expense_id, user_id)
            )
        await db.commit()
        return expense_id

async def get_balances(chat_id: int):
    participants = await get_participants(chat_id)
    if not participants:
        return {}

    balances = {uid: {"name": name, "balance": 0.0} for uid, name in participants.items()}

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT payer_id, SUM(amount) FROM expenses WHERE chat_id = ? GROUP BY payer_id", (chat_id,)) as cursor:
            async for payer_id, total_paid in cursor:
                if payer_id in balances:
                    balances[payer_id]["balance"] += total_paid

        async with db.execute("SELECT id, amount FROM expenses WHERE chat_id = ?", (chat_id,)) as cursor:
            expenses = await cursor.fetchall()
            
        for exp_id, amount in expenses:
            async with db.execute("SELECT user_id FROM expense_shares WHERE expense_id = ?", (exp_id,)) as cur:
                shares = [row[0] for row in await cur.fetchall()]
            
            if shares:
                split_amount = amount / len(shares)
                for uid in shares:
                    if uid in balances:
                        balances[uid]["balance"] -= split_amount

    return balances
    
async def get_history_with_shares(chat_id: int, limit: int = 1000):
    async with aiosqlite.connect(DB_NAME) as db:
        # Сложный запрос: собираем имена участников через запятую (GROUP_CONCAT)
        query = """
            SELECT 
                e.id, 
                e.amount, 
                e.description, 
                p_payer.name AS payer_name,
                GROUP_CONCAT(p_share.name, ', ') AS shared_with
            FROM expenses e
            LEFT JOIN participants p_payer ON e.payer_id = p_payer.user_id AND e.chat_id = p_payer.chat_id
            LEFT JOIN expense_shares es ON e.id = es.expense_id
            LEFT JOIN participants p_share ON es.user_id = p_share.user_id AND e.chat_id = p_share.chat_id
            WHERE e.chat_id = ?
            GROUP BY e.id
            ORDER BY e.id DESC
            LIMIT ?
        """
        async with db.execute(query, (chat_id, limit)) as cursor:
            return await cursor.fetchall()

async def get_history(chat_id: int, limit: int = 10):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT e.id, e.amount, e.description, p.name 
            FROM expenses e
            LEFT JOIN participants p ON e.payer_id = p.user_id AND e.chat_id = p.chat_id
            WHERE e.chat_id = ? 
            ORDER BY e.id DESC LIMIT ?
        """, (chat_id, limit)) as cursor:
            return await cursor.fetchall()

async def delete_expense(expense_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM expense_shares WHERE expense_id = ?", (expense_id,))
        await db.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        await db.commit()

async def clear_chat_data(chat_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM participants WHERE chat_id = ?", (chat_id,))
        await db.execute("DELETE FROM expenses WHERE chat_id = ?", (chat_id,))
        await db.execute("DELETE FROM expense_shares WHERE expense_id IN (SELECT id FROM expenses WHERE chat_id = ?)", (chat_id,))
        await db.commit()