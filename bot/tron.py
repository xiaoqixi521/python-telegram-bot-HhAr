"""Read-only TRON / TRC-20 public API helpers."""

import asyncio
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TRONGRID_API = "https://api.trongrid.io"


def _get_json_sync(url: str) -> dict:
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"TRON API request failed: {exc}") from exc


async def _get_json(url: str) -> dict:
    return await asyncio.to_thread(_get_json_sync, url)


async def get_account_info(address: str) -> dict:
    data = await _get_json(f"{TRONGRID_API}/v1/accounts/{address}")
    accounts = data.get("data", [])
    if not accounts:
        return {"address": address, "balance": 0.0, "exists": False}

    account = accounts[0]
    return {
        "address": address,
        "balance": int(account.get("balance", 0)) / 1_000_000,
        "exists": True,
    }


async def get_contract_info(address: str) -> dict:
    data = await _get_json(f"{TRONGRID_API}/v1/contracts/{address}")
    contracts = data.get("data", [])
    if not contracts:
        return {"exists": False}

    contract = contracts[0]
    return {
        "exists": True,
        "owner_address": contract.get("owner_address"),
        "type": contract.get("type") or "SmartContract",
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


async def get_usdt_balance(address: str) -> float:
    """Return confirmed USDT balance for a public TRON address."""
    # Use the account aggregate endpoint first; it includes TRC-20 holdings.
    data = await _get_json(
        f"{TRONGRID_API}/v1/accounts/{address}?only_confirmed=true"
    )
    accounts = data.get("data", [])
    if accounts:
        account = accounts[0]
        holdings = account.get("trc20") or []
        for item in holdings:
            if not isinstance(item, dict):
                continue
            if USDT_CONTRACT in item:
                try:
                    return int(item[USDT_CONTRACT]) / 1_000_000
                except (TypeError, ValueError):
                    pass

    # Fallback to the dedicated TRC-20 balance endpoint.
    data = await _get_json(
        f"{TRONGRID_API}/v1/accounts/{address}/trc20/balance"
        f"?only_confirmed=true&contract_address={USDT_CONTRACT}"
    )
    total = 0
    for item in data.get("data", []):
        if isinstance(item, dict) and USDT_CONTRACT in item:
            try:
                total += int(item[USDT_CONTRACT])
            except (TypeError, ValueError):
                pass
    return total / 1_000_000


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
