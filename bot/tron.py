"""Read-only TRON / TRC-20 public API helpers."""

import asyncio
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TRONGRID_API = "https://api.trongrid.io"
TRONGRID_API_KEY = os.getenv("TRONGRID_API_KEY", "").strip()
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _get_json_sync(url: str) -> dict:
    headers = {"Accept": "application/json"}
    if TRONGRID_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"TRON API request failed: {exc}") from exc


async def _get_json(url: str) -> dict:
    return await asyncio.to_thread(_get_json_sync)


def _post_json_sync(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if TRONGRID_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
    request = Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"TRON API request failed: {exc}") from exc


async def _post_json(url: str, payload: dict) -> dict:
    return await asyncio.to_thread(_post_json_sync, url, payload)

async def get_account_info(address: str) -> dict:
    # Use the official FullNode account endpoint for the core wallet query.
    # This avoids making the basic wallet lookup depend on TronGrid V1 indexing/API-key access.
    data = await _post_json(
        f"{TRONGRID_API}/wallet/getaccount",
        {"address": address, "visible": True},
    )
    if data.get("Error"):
        raise RuntimeError(str(data.get("Error")))
    if not data.get("address"):
        return {"address": address, "balance": 0.0, "exists": False}

    return {
        "address": address,
        "balance": int(data.get("balance", 0)) / 1_000_000,
        "exists": True,
    }


async def get_contract_info(address: str) -> dict:
    data = await _post_json(
        f"{TRONGRID_API}/wallet/getcontract",
        {"value": address, "visible": True},
    )
    if data.get("Error") or not data.get("contract_address"):
        return {"exists": False}

    return {
        "exists": True,
        "owner_address": data.get("origin_address"),
        "type": "SmartContract",
    }

async def get_transaction_info(txid: str) -> dict:
    data = await _get_json(f"{TRONGRID_API}/v1/transactions/{txid}")
    transactions = data.get("data", [])
    if not transactions:
        return {"exists": False}

    tx = transactions[0]
    ret = tx.get("ret") or []
    status = ret[0].get("contractRet") if ret else None
    return {
        "exists": True,
        "status": status or "已提交",
        "block_number": tx.get("blockNumber") or "未确认",
    }


async def get_trc20_balance(address: str, contract: str) -> dict:
    data = await _get_json(f"{TRONGRID_API}/v1/accounts/{address}/tokens?only_confirmed=true")
    for item in data.get("data", []):
        if item.get("address") == contract:
            return {
                "exists": True,
                "symbol": item.get("symbol") or "UNKNOWN",
                "decimals": int(item.get("decimals") or 0),
                "balance": item.get("balance") or "0",
            }
    return {"exists": False}


async def get_transactions(address: str, limit: int = 10) -> list[dict]:
    """Return recent TRX transactions for a public TRON address."""
    data = await _get_json(
        f"{TRONGRID_API}/v1/accounts/{address}/transactions"
        f"?only_confirmed=true&limit={min(max(limit, 1), 20)}"
    )
    result = []
    for tx in data.get("data", []):
        txid = tx.get("txID", "")
        block = tx.get("blockNumber", "未确认")
        ret = tx.get("ret") or []
        status = ret[0].get("contractRet") if ret else "UNKNOWN"
        contracts = (tx.get("raw_data") or {}).get("contract") or []
        contract_type = contracts[0].get("type", "未知") if contracts else "未知"
        result.append({
            "txid": txid,
            "block": block,
            "status": status,
            "type": contract_type,
        })
    return result


USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def _base58_decode(value: str) -> bytes:
    number = 0
    for char in value:
        number = number * 58 + BASE58_ALPHABET.index(char)
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading = len(value) - len(value.lstrip("1"))
    return b"\\x00" * leading + raw


def _tron_address_to_abi_hex(address: str) -> str:
    raw = _base58_decode(address)
    if len(raw) < 25:
        raise ValueError("invalid TRON address")
    payload = raw[:-4]
    if len(payload) != 21 or payload[0] != 0x41:
        raise ValueError("invalid TRON address")
    return payload[1:].hex().rjust(64, "0")


async def get_usdt_balance(address: str) -> float:
    """Read USDT balance directly from the TRC-20 contract."""
    result = await _post_json(
        f"{TRONGRID_API}/wallet/triggerconstantcontract",
        {
            "owner_address": address,
            "contract_address": USDT_CONTRACT,
            "function_selector": "balanceOf(address)",
            "parameter": _tron_address_to_abi_hex(address),
            "visible": True,
        },
    )
    values = result.get("constant_result") or []
    if not values:
        raise RuntimeError("TRC-20 balance query returned no result")
    return int(values[0], 16) / 1_000_000


async def get_recent_activity(address: str, limit: int = 20) -> list[dict]:
    """Return recent confirmed TRX/TRC-10 and TRC-20 transfers combined."""
    limit = min(max(limit, 1), 20)
    trx_data, trc20_data = await asyncio.gather(
        _get_json(
            f"{TRONGRID_API}/v1/accounts/{address}/transactions"
            f"?only_confirmed=true&limit={limit}"
        ),
        _get_json(
            f"{TRONGRID_API}/v1/accounts/{address}/transactions/trc20"
            f"?only_confirmed=true&limit={limit}"
        ),
    )
    result = []
    for tx in trx_data.get("data", []):
        contracts = ((tx.get("raw_data") or {}).get("contract") or [])
        contract_type = contracts[0].get("type", "未知") if contracts else "未知"
        ret = tx.get("ret") or []
        result.append({
            "txid": tx.get("txID", ""),
            "timestamp": tx.get("block_timestamp") or 0,
            "type": contract_type,
            "status": (ret[0].get("contractRet") if ret else "UNKNOWN"),
            "asset": "TRX/链上交易",
        })
    for tx in trc20_data.get("data", []):
        token = tx.get("token_info") or {}
        result.append({
            "txid": tx.get("transaction_id", ""),
            "timestamp": tx.get("block_timestamp") or 0,
            "type": tx.get("type", "Transfer"),
            "status": "SUCCESS" if tx.get("success", True) else "FAILED",
            "asset": token.get("symbol") or token.get("name") or "TRC-20",
        })
    result.sort(key=lambda item: item.get("timestamp", 0), reverse=True)
    return result[:limit]
