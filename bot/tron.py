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
