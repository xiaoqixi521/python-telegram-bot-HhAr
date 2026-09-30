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


async def create_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    await update.effective_message.reply_text(
        "🛠 创建 TRC-20 代币\n\n"
        "目前机器人先提供安全引导，不在服务器保存私钥。\n"
        "准备好名称、Symbol、精度和总供应量后，可以通过你自己的钱包签名部署合约。\n\n"
        "⚠️ 不要把助记词、私钥或钱包验证码发送给机器人。"
    )


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
