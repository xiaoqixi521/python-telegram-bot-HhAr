"""Telegram update handlers."""

import logging
import re

from telegram import ReplyKeyboardMarkup, Update
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import cache, db
from bot.tron import (
    get_account_info,
    get_contract_info,
    get_transaction_info,
    get_trc20_balance,
    get_transactions,
)

logger = logging.getLogger(__name__)

DB_KEY = "db"
REDIS_KEY = "redis"
_LOCAL_MESSAGE_COUNTS: dict[int, int] = {}

BOT_COMMANDS = (
    ("start", "开始使用"),
    ("help", "帮助"),
    ("wallet", "查询TRON钱包"),
    ("balance", "查询TRX余额"),
    ("token", "查询TRC-20合约"),
    ("transaction", "查询交易"),
    ("tokenbalance", "查询TRC-20余额"),
    ("transactions", "查询钱包交易"),
    ("create", "创建代币说明"),
    ("transfer", "转账说明"),
    ("about", "关于机器人"),
    ("ping", "检查机器人状态"),
)

MENU_HELP = "帮助"
MENU_WALLET = "钱包查询"
MENU_TOKEN = "代币查询"
MENU_ABOUT = "关于"
MENU_PING = "状态"

MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [[MENU_WALLET, MENU_TOKEN], [MENU_HELP, MENU_ABOUT], [MENU_PING]],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="选择功能",
)

HELP_TEXT = """TRON Forge Bot 使用帮助

/wallet 地址
查询 TRON 钱包基础信息和 TRX 余额。

/balance 地址
查询 TRX 余额。

/token 合约地址
查询 TRC-20 合约公开信息。

/transaction 交易哈希
查询 TRON 交易状态。

/create
查看创建 TRC-20 代币的安全流程。

/transfer
查看代币转账的安全流程。

只需要提供公开的 TRON 地址、合约地址或交易哈希。
不要向机器人发送助记词、私钥或验证码。"""

TRON_ADDRESS_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
TX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _arg(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args).strip() if context.args else ""


def _valid_address(value: str) -> bool:
    return bool(TRON_ADDRESS_RE.fullmatch(value))


def _valid_txid(value: str) -> bool:
    return bool(TX_RE.fullmatch(value))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    pool = context.bot_data.get(DB_KEY)
    is_new = True
    if pool is not None:
        is_new = await db.upsert_user(pool, user.id, user.username, user.first_name)

    name = user.first_name or "朋友"
    greeting = "欢迎" if is_new else "欢迎回来"
    await message.reply_text(
        f"{greeting}，{name}！\n\n"
        "TRON Forge Bot 已上线。\n"
        "支持 TRON 钱包、TRX 余额、TRC-20 合约和交易查询。\n\n"
        "输入 /help 查看全部功能。",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.effective_message:
        await update.effective_message.reply_text(HELP_TEXT)


async def wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    address = _arg(context)
    if not address:
        context.user_data["pending"] = "wallet"
        await update.effective_message.reply_text("请输入 TRON 钱包地址（T 开头）：")
        return
    if not _valid_address(address):
        await update.effective_message.reply_text("TRON 地址格式不正确，请重新发送 T 开头的地址。")
        return

    try:
        info = await get_account_info(address)
        status = "已激活" if info["exists"] else "未发现账户数据"
        await update.effective_message.reply_text(
            f"🪙 TRON 钱包\n\n"
            f"地址：{address}\n"
            f"状态：{status}\n"
            f"TRX余额：{info['balance']:.6f} TRX"
        )
    except Exception as exc:
        logger.warning("wallet query failed: %s", exc)
        await update.effective_message.reply_text("查询失败，请稍后重试。")


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await wallet(update, context)


async def token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    address = _arg(context)
    if not address:
        context.user_data["pending"] = "token"
        await update.effective_message.reply_text("请输入 TRC-20 合约地址（T 开头）：")
        return
    if not _valid_address(address):
        await update.effective_message.reply_text(
            "用法：/token TRC-20合约地址"
        )
        return

    try:
        info = await get_contract_info(address)
        if not info.get("exists"):
            await update.effective_message.reply_text(
                f"未找到公开合约信息。\n合约：{address}"
            )
            return

        await update.effective_message.reply_text(
            "🪙 TRC-20 合约\n\n"
            f"合约地址：{address}\n"
            f"合约存在：是\n"
            f"所有者：{info.get('owner_address') or '未知'}\n"
            f"类型：{info.get('type') or '智能合约'}"
        )
    except Exception as exc:
        logger.warning("token query failed: %s", exc)
        await update.effective_message.reply_text("合约查询失败，请稍后重试。")


async def transaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txid = _arg(context)
    if not txid:
        context.user_data["pending"] = "transaction"
        await update.effective_message.reply_text("请输入 64 位 TRON 交易哈希：")
        return
    if not _valid_txid(txid):
        await update.effective_message.reply_text(
            "用法：/transaction 64位交易哈希"
        )
        return

    try:
        info = await get_transaction_info(txid)
        if not info.get("exists"):
            await update.effective_message.reply_text(
                f"未找到交易。\n哈希：{txid}"
            )
            return

        await update.effective_message.reply_text(
            "🔎 TRON 交易\n\n"
            f"哈希：{txid}\n"
            f"状态：{info.get('status', '未知')}\n"
            f"区块：{info.get('block_number', '未确认')}"
        )
    except Exception as exc:
        logger.warning("transaction query failed: %s", exc)
        await update.effective_message.reply_text("交易查询失败，请稍后重试。")


async def tokenbalance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        context.user_data["tokenbalance_step"] = "wallet"
        await update.effective_message.reply_text("请输入要查询余额的 TRON 钱包地址（T 开头）：")
        return
    if len(context.args) == 1:
        wallet_address = context.args[0]
        if not _valid_address(wallet_address):
            await update.effective_message.reply_text("钱包地址格式不正确，请重新输入。")
            return
        context.user_data["tokenbalance_wallet"] = wallet_address
        context.user_data["tokenbalance_step"] = "contract"
        await update.effective_message.reply_text("请输入 TRC-20 合约地址（T 开头）：")
        return
    await _tokenbalance_result(update, context.args[0], context.args[1])


async def _tokenbalance_result(update: Update, wallet_address: str, contract: str) -> None:
    if not _valid_address(wallet_address) or not _valid_address(contract):
        await update.effective_message.reply_text("钱包地址或合约地址格式不正确。")
        return
    try:
        info = await get_trc20_balance(wallet_address, contract)
        if not info.get("exists"):
            await update.effective_message.reply_text("未找到该钱包持有的这个 TRC-20 代币余额。")
            return
        decimals = info["decimals"]
        raw = int(info["balance"])
        amount = raw / (10 ** decimals) if decimals else raw
        await update.effective_message.reply_text(
            "💰 TRC-20 余额\n\n"
            f"钱包：{wallet_address}\n代币：{info['symbol']}\n"
            f"余额：{amount:,.{min(decimals, 8)}f}\n合约：{contract}"
        )
    except Exception as exc:
        logger.warning("token balance query failed: %s", exc)
        await update.effective_message.reply_text("代币余额查询失败，请稍后重试。")


async def transactions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    address = _arg(context)
    if not address:
        context.user_data["transactions_step"] = "wallet"
        await update.effective_message.reply_text("请输入要查询交易记录的 TRON 钱包地址（T 开头）：")
        return
    await _transactions_result(update, address)


async def _transactions_result(update: Update, address: str) -> None:
    if not _valid_address(address):
        await update.effective_message.reply_text("TRON 地址格式不正确，请重新发送 T 开头的地址。")
        return
    try:
        items = await get_transactions(address, 10)
        if not items:
            await update.effective_message.reply_text("这个地址暂时没有可查询的已确认交易记录。")
            return

        lines = [f"📋 最近 {len(items)} 笔交易\n", f"钱包：{address}\n"]
        for i, item in enumerate(items, 1):
            lines.append(
                f"{i}. {item['type']}\n"
                f"状态：{item['status']}  区块：{item['block']}\n"
                f"哈希：{item['txid']}\n"
            )
        text = "\n".join(lines)
        await update.effective_message.reply_text(text[:4000])
    except Exception as exc:
        logger.warning("transactions query failed: %s", exc)
        await update.effective_message.reply_text("交易记录查询失败，请稍后重试。")


async def create_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["create_step"] = "name"
    context.user_data.pop("token_draft", None)
    await update.effective_message.reply_text(
        "🛠 创建 TRC-20 代币\n\n"
        "第 1 步 / 4\n请输入代币名称，例如：My Token"
    )


async def handle_create_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.effective_message
    if message is None or not message.text:
        return False

    step = context.user_data.get("create_step")
    if not step:
        return False

    value = message.text.strip()
    draft = context.user_data.setdefault("token_draft", {})

    if step == "name":
        if not 1 <= len(value) <= 50:
            await message.reply_text("名称长度应为 1-50 个字符，请重新输入。")
            return True
        draft["name"] = value
        context.user_data["create_step"] = "symbol"
        await message.reply_text("第 2 步 / 4\n请输入代币 Symbol，例如：TFO")
        return True

    if step == "symbol":
        symbol = value.upper()
        if not re.fullmatch(r"[A-Z0-9]{1,12}", symbol):
            await message.reply_text("Symbol 只能使用 1-12 位英文字母或数字，请重新输入。")
            return True
        draft["symbol"] = symbol
        context.user_data["create_step"] = "decimals"
        await message.reply_text("第 3 步 / 4\n请输入精度，例如：6")
        return True

    if step == "decimals":
        if not value.isdigit() or not 0 <= int(value) <= 18:
            await message.reply_text("精度请输入 0-18 的整数，例如：6")
            return True
        draft["decimals"] = int(value)
        context.user_data["create_step"] = "supply"
        await message.reply_text("第 4 步 / 4\n请输入总供应量，例如：10000000")
        return True

    if step == "supply":
        if not re.fullmatch(r"\d+(?:\.\d+)?", value):
            await message.reply_text("总供应量请输入数字，例如：10000000")
            return True
        if float(value) <= 0:
            await message.reply_text("总供应量必须大于 0，请重新输入。")
            return True
        draft["supply"] = value
        context.user_data.pop("create_step", None)
        await message.reply_text(
            "✅ 参数已收集\n\n"
            f"名称：{draft['name']}\n"
            f"Symbol：{draft['symbol']}\n"
            f"精度：{draft['decimals']}\n"
            f"总供应量：{draft['supply']}\n\n"
            "下一步可以生成 TRC-20 合约部署材料。\n"
            "部署交易需要由你的钱包签名。机器人不会索取或保存私钥。"
        )
        return True

    return False


async def transfer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    await update.effective_message.reply_text(
        "💸 TRC-20 转账\n\n"
        "当前版本只提供安全引导，不接收或保存私钥。\n"
        "实际转账应由你的钱包完成签名。\n\n"
        "不要向机器人发送助记词或私钥。"
    )


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    await update.effective_message.reply_text(
        "TRON Forge Bot\nTRON / TRC-20 链上工具\n"
        "支持钱包、余额、合约和交易公开信息查询。"
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    client = context.bot_data.get(REDIS_KEY)
    if client is None:
        await message.reply_text("pong")
        return

    cached = await cache.get_or_set_ping(client)
    source = "缓存" if cached else "实时"
    await message.reply_text(f"pong（{source}）")


async def menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    text = message.text.strip()
    if text == MENU_HELP:
        await help_command(update, context)
    elif text == MENU_WALLET:
        await message.reply_text("请输入：/wallet TRON地址")
    elif text == MENU_TOKEN:
        await message.reply_text("请输入：/token TRC-20合约地址")
    elif text == MENU_ABOUT:
        await about(update, context)
    elif text == MENU_PING:
        await ping(update, context)


async def echo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = context.user_data.get("transactions_step")
    if pending == "wallet":
        value = update.effective_message.text.strip() if update.effective_message and update.effective_message.text else ""
        context.user_data.pop("transactions_step", None)
        await _transactions_result(update, value)
        return

    pending = context.user_data.get("tokenbalance_step")
    if pending:
        value = update.effective_message.text.strip() if update.effective_message and update.effective_message.text else ""
        if pending == "wallet":
            if not _valid_address(value):
                await update.effective_message.reply_text("钱包地址格式不正确，请重新发送 T 开头的地址。")
                return
            context.user_data["tokenbalance_wallet"] = value
            context.user_data["tokenbalance_step"] = "contract"
            await update.effective_message.reply_text("请输入 TRC-20 合约地址（T 开头）：")
            return
        wallet_address = context.user_data.pop("tokenbalance_wallet", "")
        context.user_data.pop("tokenbalance_step", None)
        await _tokenbalance_result(update, wallet_address, value)
        return

    if await handle_create_input(update, context):
        return
    message = update.effective_message
    user = update.effective_user
    if message is None or not message.text or user is None:
        return

    pending = context.user_data.pop("pending", None)
    if pending == "wallet":
        address = message.text.strip()
        if not _valid_address(address):
            await message.reply_text("TRON 地址格式不正确，请重新发送 T 开头的地址。")
            return
        try:
            info = await get_account_info(address)
            status = "已激活" if info["exists"] else "未发现账户数据"
            await message.reply_text(
                f"🪙 TRON 钱包\n\n地址：{address}\n状态：{status}\n"
                f"TRX余额：{info['balance']:.6f} TRX"
            )
        except Exception as exc:
            logger.warning("wallet query failed: %s", exc)
            await message.reply_text("查询失败，请稍后重试。")
        return

    if pending == "token":
        address = message.text.strip()
        if not _valid_address(address):
            await message.reply_text("TRC-20 合约地址格式不正确，请重新发送 T 开头的地址。")
            return
        try:
            info = await get_contract_info(address)
            if not info.get("exists"):
                await message.reply_text(f"未找到公开合约信息。\n合约：{address}")
            else:
                await message.reply_text(
                    "🪙 TRC-20 合约\n\n"
                    f"合约地址：{address}\n合约存在：是\n"
                    f"所有者：{info.get('owner_address') or '未知'}\n"
                    f"类型：{info.get('type') or '智能合约'}"
                )
        except Exception as exc:
            logger.warning("token query failed: %s", exc)
            await message.reply_text("合约查询失败，请稍后重试。")
        return

    if pending == "transaction":
        txid = message.text.strip()
        if not _valid_txid(txid):
            await message.reply_text("交易哈希格式不正确，请发送 64 位十六进制交易哈希。")
            return
        try:
            info = await get_transaction_info(txid)
            if not info.get("exists"):
                await message.reply_text(f"未找到交易。\n哈希：{txid}")
            else:
                await message.reply_text(
                    "🔎 TRON 交易\n\n"
                    f"哈希：{txid}\n状态：{info.get('status', '未知')}\n"
                    f"区块：{info.get('block_number', '未确认')}"
                )
        except Exception as exc:
            logger.warning("transaction query failed: %s", exc)
            await message.reply_text("交易查询失败，请稍后重试。")
        return

    client = context.bot_data.get(REDIS_KEY)
    if client is not None:
        count = await cache.increment_message_count(client, user.id)
    else:
        count = _LOCAL_MESSAGE_COUNTS[user.id] = _LOCAL_MESSAGE_COUNTS.get(user.id, 0) + 1
    await message.reply_text(f"收到第 {count} 条消息：\n{message.text}")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.effective_message:
        await update.effective_message.reply_text("未知命令，请输入 /help 查看帮助。")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, (Conflict, NetworkError, TimedOut)):
        logger.warning("Transient Telegram error: %s", error)
        return

    logger.exception("Error while processing update: %s", update, exc_info=error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("处理消息时发生错误，请稍后重试。")


async def set_bot_commands(application: Application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)


def register_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("wallet", wallet))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("token", token))
    application.add_handler(CommandHandler("transaction", transaction))
    application.add_handler(CommandHandler("tokenbalance", tokenbalance))
    application.add_handler(CommandHandler("transactions", transactions))
    application.add_handler(CommandHandler("create", create_token))
    application.add_handler(CommandHandler("transfer", transfer))
    application.add_handler(CommandHandler("about", about))
    application.add_handler(CommandHandler("ping", ping))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_handler(
        MessageHandler(
            filters.Regex(f"^({MENU_HELP}|{MENU_WALLET}|{MENU_TOKEN}|{MENU_ABOUT}|{MENU_PING})$"),
            menu_button,
        )
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_message))
