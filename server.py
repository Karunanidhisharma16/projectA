from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
import json
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parent
RPC_PROVIDERS = [
    {
        "url": "https://api.mainnet-beta.solana.com",
        "methods": {"getBalance", "getTokenAccountsByOwner", "getMultipleAccounts", "getHealth"},
        "min_interval": 0.12,
        "last_call": 0.0,
        "cooldown_until": 0.0,
    },
    {
        "url": "https://nodes.mewapi.io/rpc/sol",
        "methods": {"getBalance", "getTokenAccountsByOwner", "getMultipleAccounts", "getHealth"},
        "min_interval": 0.12,
        "last_call": 0.0,
        "cooldown_until": 0.0,
    },
    {
        "url": "https://solana-rpc.publicnode.com",
        "methods": {"getBalance", "getHealth"},
        "min_interval": 0.10,
        "last_call": 0.0,
        "cooldown_until": 0.0,
    },
    {
        "url": "https://rpc.ankr.com/solana",
        "methods": {"getBalance", "getTokenAccountsByOwner", "getMultipleAccounts", "getHealth"},
        "min_interval": 0.12,
        "last_call": 0.0,
        "cooldown_until": 0.0,
    },
]
JUPITER_PRICE_URL = "https://api.jup.ag/price/v3"
DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/"
JUPITER_TOKEN_SEARCH_URL = "https://api.jup.ag/tokens/v2/search?query="
DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
RPC_CACHE = {}
RPC_CACHE_LOCK = threading.Lock()
RPC_RATE_LIMIT_LOCK = threading.Lock()
RPC_PROVIDER_INDEX = 0
RPC_CACHE_TTL_SECONDS = 30
WALLET_CACHE_TTL_SECONDS = 60
PRICE_CACHE_TTL_SECONDS = 900
PRICE_CACHE_FILE = ROOT / "price_cache.json"
WALLET_CACHE_FILE = ROOT / "wallet_cache.json"
RPC_TIMEOUT_SECONDS = 4
WALLET_ADDRESS_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
WALLET_CACHE = {}
WALLET_SNAPSHOT_CACHE = {}
WALLET_REFRESHES = {}
WALLET_REFRESH_EXECUTOR = ThreadPoolExecutor(max_workers=2)
try:
    PRICE_CACHE = json.loads(PRICE_CACHE_FILE.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    PRICE_CACHE = {}
try:
    saved_wallets = json.loads(WALLET_CACHE_FILE.read_text(encoding="utf-8"))
    if isinstance(saved_wallets, dict):
        WALLET_SNAPSHOT_CACHE = {
            address: wallet for address, wallet in saved_wallets.items()
            if isinstance(wallet, dict)
        }
except (OSError, json.JSONDecodeError):
    pass


class TrackerHandler(BaseHTTPRequestHandler):
    def _send(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path)
        if path.path == "/api/wallet":
            qs = parse_qs(path.query)
            address = qs.get("address", [""])[0]
            force = qs.get("refresh", ["0"])[0] in ("1", "true", "True") or qs.get("force", ["0"])[0] in ("1", "true", "True")
            if not WALLET_ADDRESS_PATTERN.fullmatch(address):
                self._send(400, "application/json", b'{"error":"Invalid wallet address"}')
                return
            self._send(200, "application/json", json.dumps(self._get_wallet(address, force=force)).encode())
            return
        if path.path == "/api/refresh-all":
            self._send(200, "application/json", json.dumps(self._refresh_all_wallets()).encode())
            return
        if path.path == "/api/history":
            history_file = ROOT / "portfolio_history.json"
            try:
                data = json.loads(history_file.read_text(encoding="utf-8"))
            except Exception:
                data = {
                    "baseline12h": {
                        "timestamp": "2026-10-23T14:30:00Z",
                        "totalUsd": 384.20,
                        "note": "12-Hour Historical Baseline"
                    },
                    "snapshots": []
                }
            self._send(200, "application/json", json.dumps(data).encode())
            return
        if path.path == "/api/price":
            ids = [value for value in parse_qs(path.query).get("ids", [""])[0].split(",") if value]
            prices = {}
            for attempt in range(3):
                try:
                    request = Request(f"{JUPITER_PRICE_URL}?ids={','.join(ids)}", headers=DEFAULT_HEADERS)
                    with urlopen(request, timeout=10) as response:
                        payload = json.loads(response.read())
                    for mint, info in payload.items():
                        if isinstance(info, dict) and isinstance(info.get("usdPrice"), (int, float)):
                            prices[mint] = info["usdPrice"]
                except (HTTPError, OSError, TimeoutError, json.JSONDecodeError):
                    pass
                if len(prices) == len(ids):
                    break
                time.sleep(attempt + 1)

            for mint in ids:
                if mint in prices:
                    continue
                try:
                    request = Request(f"{JUPITER_PRICE_URL}?ids={mint}", headers=DEFAULT_HEADERS)
                    with urlopen(request, timeout=10) as response:
                        payload = json.loads(response.read())
                    info = payload.get(mint)
                    if isinstance(info, dict) and isinstance(info.get("usdPrice"), (int, float)):
                        prices[mint] = info["usdPrice"]
                except (HTTPError, OSError, TimeoutError, json.JSONDecodeError):
                    continue

            for mint in ids:
                if mint in prices:
                    continue
                try:
                    request = Request(DEXSCREENER_TOKEN_URL + mint, headers=DEFAULT_HEADERS)
                    with urlopen(request, timeout=8) as response:
                        payload = json.loads(response.read())
                    pairs = [
                        pair for pair in payload.get("pairs", [])
                        if isinstance(pair, dict) and isinstance(pair.get("priceUsd"), str)
                    ]
                    if pairs:
                        pair = max(pairs, key=lambda item: float(item.get("liquidity", {}).get("usd", 0) or 0))
                        prices[mint] = float(pair["priceUsd"])
                except (HTTPError, OSError, TimeoutError, ValueError, TypeError, json.JSONDecodeError):
                    continue

            now = time.time()
            with RPC_CACHE_LOCK:
                for mint, price in prices.items():
                    PRICE_CACHE[mint] = (now, price)
                try:
                    PRICE_CACHE_FILE.write_text(json.dumps(PRICE_CACHE), encoding="utf-8")
                except OSError:
                    pass
                for mint in ids:
                    cached = PRICE_CACHE.get(mint)
                    if mint not in prices and cached and now - cached[0] < PRICE_CACHE_TTL_SECONDS:
                        prices[mint] = cached[1]
            self._send(200, "application/json", json.dumps(prices).encode())
            return
        if path.path == "/api/tokens":
            ids = [value for value in parse_qs(path.query).get("ids", [""])[0].split(",") if value]
            known_meta = {
                "XsbEhLAtcf6HdfpFZ5xEMdqW8nfAvcsP5bdudRLJzJp": {
                    "name": "Apple xStock",
                    "symbol": "AAPLx",
                    "icon": "https://xstocks-metadata.backed.fi/logos/tokens/AAPLx.png",
                },
                "Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh": {
                    "name": "NVIDIA xStock",
                    "symbol": "NVDAx",
                    "icon": "https://xstocks-metadata.backed.fi/logos/tokens/NVDAx.png",
                },
                "XsDoVfqeBukxuZHWhdvWHBhgEHjGNst4MLodqsJHzoB": {
                    "name": "Tesla xStock",
                    "symbol": "TSLAx",
                    "icon": "https://xstocks-metadata.backed.fi/logos/tokens/TSLAx.png",
                },
                "CARSsxWPkpQWvfyRBwfGMGvysJBHdHGfE46X5MNgmeta": {
                    "name": "Rip Cars",
                    "symbol": "CARS",
                    "icon": None,
                },
                "8RVBk8vxLiUHueLUW1f4izFVqN3nWippLhkohKg6EGkS": {
                    "name": "KNOTS",
                    "symbol": "KNOTS",
                    "icon": None,
                },
                "HXQ66zSRqynwJQ6vYEYa85qGY9C2Ycz8rnYQHgApn391": {
                    "name": "STOKN",
                    "symbol": "GO",
                    "icon": "https://cdn.dexscreener.com/cms/images/BmYAuns9jUCTUwly?width=800&height=800&quality=95&format=auto",
                },
            }
            metadata = {}
            for mint in ids:
                if mint in known_meta:
                    metadata[mint] = known_meta[mint]
                    continue
                try:
                    request = Request(JUPITER_TOKEN_SEARCH_URL + mint, headers=DEFAULT_HEADERS)
                    with urlopen(request, timeout=10) as response:
                        matches = json.loads(response.read())
                    match = next((item for item in matches if item.get("id") == mint), None)
                    if match:
                        metadata[mint] = {
                            "name": match.get("name"),
                            "symbol": match.get("symbol"),
                            "icon": match.get("icon"),
                        }
                except Exception:
                    continue
            self._send(200, "application/json", json.dumps(metadata).encode())
            return

        file_path = (ROOT / path.path.lstrip("/")).resolve()
        if file_path == ROOT:
            file_path = ROOT / "index.html"
        if ROOT not in file_path.parents or not file_path.is_file():
            self._send(404, "text/plain; charset=utf-8", b"Not found")
            return
        content_type = "application/json" if file_path.suffix == ".json" else "text/html; charset=utf-8"
        self._send(200, content_type, file_path.read_bytes())

    def do_POST(self):
        if urlparse(self.path).path != "/api/solana":
            self._send(404, "text/plain; charset=utf-8", b"Not found")
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            requests = json.loads(body)
        except json.JSONDecodeError:
            self._send(400, "application/json", b'{"error":"Invalid JSON"}')
            return

        try:
            if isinstance(requests, list):
                responses = []
                for request_body in requests:
                    response_body = self._forward_rpc(json.dumps(request_body).encode())
                    if response_body is None:
                        responses.append({
                            "jsonrpc": "2.0",
                            "id": request_body.get("id"),
                            "error": {"message": "All Solana RPC providers failed"},
                        })
                    else:
                        responses.append(json.loads(response_body))
                self._send(200, "application/json", json.dumps(responses).encode())
                return

            response_body = self._forward_rpc(body)
            if response_body is None:
                response_body = json.dumps({
                    "jsonrpc": "2.0",
                    "error": {"message": "All Solana RPC providers failed"},
                }).encode()
            self._send(200, "application/json", response_body)
        except Exception as error:
            self._send(500, "application/json", json.dumps({"error": str(error)}).encode())

    def _forward_rpc(self, body):
        cache_key = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
        with RPC_CACHE_LOCK:
            cached = RPC_CACHE.get(cache_key)
        if cached and time.monotonic() - cached[0] < RPC_CACHE_TTL_SECONDS:
            return cached[1]

        # Extract method from JSON body to filter capable providers
        method = ""
        try:
            req_json = json.loads(body)
            if isinstance(req_json, dict):
                method = req_json.get("method", "")
        except Exception:
            pass

        now = time.monotonic()
        # Filter providers supporting this method
        eligible = [
            p for p in RPC_PROVIDERS
            if not method or method in p["methods"]
        ]
        if not eligible:
            eligible = RPC_PROVIDERS

        global RPC_PROVIDER_INDEX
        with RPC_CACHE_LOCK:
            RPC_PROVIDER_INDEX = (RPC_PROVIDER_INDEX + 1) % len(RPC_PROVIDERS)
            offset = RPC_PROVIDER_INDEX

        rotated = eligible[offset % len(eligible):] + eligible[:offset % len(eligible)]
        # Put providers not currently in cooldown first
        active_providers = [p for p in rotated if p["cooldown_until"] <= now]
        cooldown_providers = [p for p in rotated if p["cooldown_until"] > now]
        candidate_providers = active_providers + cooldown_providers

        for retry_round in range(2):
            for provider in candidate_providers:
                # Per-provider rate-limiting
                with RPC_RATE_LIMIT_LOCK:
                    p_now = time.monotonic()
                    elapsed = p_now - provider["last_call"]
                    if elapsed < provider["min_interval"]:
                        time.sleep(provider["min_interval"] - elapsed)
                    provider["last_call"] = time.monotonic()

                request = Request(
                    provider["url"],
                    data=body if isinstance(body, bytes) else body.encode("utf-8"),
                    headers=DEFAULT_HEADERS,
                    method="POST",
                )
                try:
                    with urlopen(request, timeout=RPC_TIMEOUT_SECONDS) as response:
                        result = response.read()
                        parsed = json.loads(result)
                        if isinstance(parsed, dict) and parsed.get("error"):
                            err_code = parsed.get("error", {}).get("code")
                            if err_code in (-32005, 429):
                                provider["cooldown_until"] = time.monotonic() + 10.0
                            continue
                        with RPC_CACHE_LOCK:
                            RPC_CACHE[cache_key] = (time.monotonic(), result)
                        return result
                except HTTPError as error:
                    if error.code in (429, 403, 502, 503, 504):
                        provider["cooldown_until"] = time.monotonic() + 15.0
                    continue
                except (OSError, TimeoutError, json.JSONDecodeError):
                    provider["cooldown_until"] = time.monotonic() + 5.0
                    continue

            if retry_round == 0:
                time.sleep(0.4)

        return None

    def _refresh_all_wallets(self):
        try:
            accounts_file = ROOT / "account.json"
            accounts = json.loads(accounts_file.read_text(encoding="utf-8"))
        except Exception:
            return {"error": "Failed to load account.json"}

        addresses = [acc["address"] for acc in accounts if isinstance(acc, dict) and "address" in acc]
        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(lambda addr: self._refresh_wallet(addr), addresses))

        with RPC_CACHE_LOCK:
            return {
                "success": True,
                "count": len(addresses),
                "wallets": WALLET_SNAPSHOT_CACHE
            }

    def _get_wallet(self, address, force=False):
        if force:
            return self._refresh_wallet(address)

        with RPC_CACHE_LOCK:
            cached = WALLET_CACHE.get(address)
            snapshot = WALLET_SNAPSHOT_CACHE.get(address)
            if snapshot is None:
                try:
                    saved = json.loads(WALLET_CACHE_FILE.read_text(encoding="utf-8"))
                    if isinstance(saved, dict) and address in saved:
                        snapshot = saved[address]
                        WALLET_SNAPSHOT_CACHE[address] = snapshot
                except Exception:
                    pass

        if cached and time.monotonic() - cached[0] < WALLET_CACHE_TTL_SECONDS:
            return cached[1]
        if snapshot:
            self._schedule_wallet_refresh(address)
            with RPC_CACHE_LOCK:
                WALLET_CACHE[address] = (time.monotonic(), snapshot)
            return snapshot
        return self._refresh_wallet(address)

    def _schedule_wallet_refresh(self, address):
        with RPC_CACHE_LOCK:
            refresh = WALLET_REFRESHES.get(address)
            if refresh is None or refresh.done():
                WALLET_REFRESHES[address] = WALLET_REFRESH_EXECUTOR.submit(
                    self._refresh_wallet, address
                )

    def _refresh_wallet(self, address):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [address]},
            {"jsonrpc": "2.0", "id": 2, "method": "getTokenAccountsByOwner", "params": [
                address, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                {"encoding": "jsonParsed"}
            ]},
            {"jsonrpc": "2.0", "id": 3, "method": "getTokenAccountsByOwner", "params": [
                address, {"programId": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"},
                {"encoding": "jsonParsed"}
            ]},
        ]
        responses = {}
        for request_body in requests:
            result = self._forward_rpc(json.dumps(request_body).encode())
            if result is not None:
                try:
                    parsed = json.loads(result)
                    if isinstance(parsed, dict) and "result" in parsed:
                        responses[request_body["id"]] = parsed
                except json.JSONDecodeError:
                    pass

        # Load previous verified data to guard against data loss
        with RPC_CACHE_LOCK:
            previous = WALLET_SNAPSHOT_CACHE.get(address)
            if previous is None:
                try:
                    disk_cache = json.loads(WALLET_CACHE_FILE.read_text(encoding="utf-8"))
                    if isinstance(disk_cache, dict) and address in disk_cache:
                        previous = disk_cache[address]
                        WALLET_SNAPSHOT_CACHE[address] = previous
                except Exception:
                    pass

        # 1. SOL balance
        balance_response = responses.get(1)
        if balance_response and isinstance(balance_response.get("result"), dict):
            sol_balance = balance_response["result"].get("value", 0) / 1_000_000_000
        elif previous and "solBalance" in previous:
            sol_balance = previous["solBalance"]
        else:
            sol_balance = 0.0

        # 2. Token balances
        got_tokenkeg = 2 in responses
        got_token2022 = 3 in responses
        token_amounts = {}

        if got_tokenkeg or got_token2022:
            for response_id in (2, 3):
                response = responses.get(response_id)
                value = response.get("result", {}).get("value", []) if response else []
                for account in value:
                    try:
                        info = account["account"]["data"]["parsed"]["info"]
                        token_amount = info["tokenAmount"]
                        raw_amount = int(token_amount["amount"])
                        if raw_amount > 0:
                            mint = info["mint"]
                            amount = float(token_amount["uiAmountString"])
                            token_amounts[mint] = token_amounts.get(mint, 0) + amount
                    except (KeyError, TypeError, ValueError):
                        continue

        # Ground truth on-chain accounting:
        # If we successfully queried token programs, store exact live on-chain balances.
        # Sold tokens (balance 0) are removed, and newly acquired tokens are added.
        if got_tokenkeg or got_token2022:
            final_tokens = [
                {"mint": mint, "amount": amount}
                for mint, amount in sorted(token_amounts.items())
            ]
        elif previous and previous.get("tokens"):
            # Only fall back to previous cached data if both token RPC queries failed due to network glitches
            final_tokens = previous.get("tokens", [])
        else:
            final_tokens = []

        is_unavailable = len(responses) == 0
        is_partial = len(responses) < 3

        wallet = {
            "solBalance": sol_balance,
            "tokens": final_tokens,
            "unavailable": is_unavailable,
            "partial": is_partial,
        }

        with RPC_CACHE_LOCK:
            # Only update refreshedAt timestamp and disk snapshot on genuine success
            if not is_unavailable and not (is_partial and len(final_tokens) == 0 and previous and len(previous.get("tokens", [])) > 0):
                wallet["refreshedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                WALLET_SNAPSHOT_CACHE[address] = wallet
                try:
                    temp_file = WALLET_CACHE_FILE.with_suffix(".tmp")
                    temp_file.write_text(json.dumps(WALLET_SNAPSHOT_CACHE, indent=2), encoding="utf-8")
                    temp_file.replace(WALLET_CACHE_FILE)
                except OSError:
                    pass
            elif previous:
                wallet["refreshedAt"] = previous.get("refreshedAt")
                wallet["solBalance"] = previous.get("solBalance", sol_balance)
                wallet["tokens"] = previous.get("tokens", final_tokens)
                WALLET_SNAPSHOT_CACHE[address] = wallet

            WALLET_CACHE[address] = (time.monotonic(), wallet)

        return wallet


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 10000))
    print(f"Solana Portfolio Tracker running on port {port}", flush=True)
    ThreadingHTTPServer(("", port), TrackerHandler).serve_forever()