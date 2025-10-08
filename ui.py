import math
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database import accounts_collection

async def build_account_selection_keyboard(page: int, mode: str, action: str, selection: list = None):
    """Builds a paginated keyboard for account selection."""
    if selection is None:
        selection = []

    page_size = 12  # 6 rows of 2 buttons
    accounts = list(accounts_collection.find().sort("custom_name", 1))

    if not accounts:
        return None, "You haven't added any accounts yet. Use `/add` to start."

    total_pages = math.ceil(len(accounts) / page_size)
    page = max(0, min(page, total_pages - 1))

    start_index = page * page_size
    end_index = start_index + page_size
    page_accounts = accounts[start_index:end_index]

    keyboard = []
    for i in range(0, len(page_accounts), 2):
        row = []
        # First account in row
        acc1 = page_accounts[i]
        text1 = f"✅ {acc1['custom_name']}" if acc1['custom_name'] in selection else acc1['custom_name']
        row.append(InlineKeyboardButton(text1, callback_data=f"select_{mode}_{action}_{acc1['custom_name']}"))

        # Second account in row, if it exists
        if i + 1 < len(page_accounts):
            acc2 = page_accounts[i+1]
            text2 = f"✅ {acc2['custom_name']}" if acc2['custom_name'] in selection else acc2['custom_name']
            row.append(InlineKeyboardButton(text2, callback_data=f"select_{mode}_{action}_{acc2['custom_name']}"))
        keyboard.append(row)

    # Navigation buttons
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{mode}_{action}_{page-1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{mode}_{action}_{page+1}"))
    if nav_row:
        keyboard.append(nav_row)

    # Multi-select specific buttons
    if mode == 'multi':
        keyboard.insert(0, [
            InlineKeyboardButton("Select All", callback_data=f"select_all_{action}"),
            InlineKeyboardButton("Unselect All", callback_data=f"unselect_all_{action}")
        ])
        keyboard.append([InlineKeyboardButton("Done Selecting 👌", callback_data=f"done_multi_{action}")])

    keyboard.append([InlineKeyboardButton("Cancel ❌", callback_data="cancel_selection")])

    header = f"👤 Select an account ({page+1}/{total_pages}):"
    if mode == 'multi':
        header = f"👥 Select account(s) ({len(selection)} selected):"

    return InlineKeyboardMarkup(keyboard), header
