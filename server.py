from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
import json
import re
import time
import threading

ROOT = Path(__file__).resolve().parent
SOLANA_RPC_URLS = (
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet.solana.com",
    "https://api.mainnet-beta.solana.com",
)
JUPITER_PRICE_URL = "https://api.jup.ag/price/v3"
DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/"
JUPITER_TOKEN_SEARCH_URL = "https://api.jup.ag/tokens/v2/search?query="
RPC_CACHE = {}
RPC_CACHE_LOCK = threading.Lock()
RPC_PROVIDER_INDEX = 0
RPC_CACHE_TTL_SECONDS = 30
WALLET_CACHE_TTL_SECONDS = 60
PRICE_CACHE_TTL_SECONDS = 900
PRICE_CACHE_FILE = ROOT / "price_cache.json"
RPC_TIMEOUT_SECONDS = 12
WALLET_ADDRESS_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
RPC_REQUEST_LOCK = threading.Lock()
WALLET_CACHE = {}
try:
    PRICE_CACHE = json.loads(PRICE_CACHE_FILE.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    PRICE_CACHE = {}


class TrackerHandler(BaseHTTPRequestHandler):
    def _send(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path)
        if path.path == "/api/wallet":
            address = parse_qs(path.query).get("address", [""])[0]
            if not WALLET_ADDRESS_PATTERN.fullmatch(address):
                self._send(400, "application/json", b'{"error":"Invalid wallet address"}')
                return
            self._send(200, "application/json", json.dumps(self._get_wallet(address)).encode())
            return
        if path.path == "/api/price":
            ids = [value for value in parse_qs(path.query).get("ids", [""])[0].split(",") if value]
            prices = {}
            for attempt in range(3):
                try:
                    request = Request(f"{JUPITER_PRICE_URL}?ids={','.join(ids)}")
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
                    request = Request(f"{JUPITER_PRICE_URL}?ids={mint}")
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
                    request = Request(DEXSCREENER_TOKEN_URL + mint)
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
            metadata = {}
            for mint in ids:
                if mint == "XsbEhLAtcf6HdfpFZ5xEMdqW8nfAvcsP5bdudRLJzJp":
                    metadata[mint] = {
                        "name": "Apple xStock",
                        "symbol": "AAPLx",
                        "icon": "https://xstocks-metadata.backed.fi/logos/tokens/AAPLx.png",
                    }
                    continue
                try:
                    request = Request(JUPITER_TOKEN_SEARCH_URL + mint)
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
        cache_key = body.decode("utf-8")
        with RPC_CACHE_LOCK:
            cached = RPC_CACHE.get(cache_key)
        if cached and time.monotonic() - cached[0] < RPC_CACHE_TTL_SECONDS:
            return cached[1]

        global RPC_PROVIDER_INDEX
        with RPC_CACHE_LOCK:
            start_index = RPC_PROVIDER_INDEX
            RPC_PROVIDER_INDEX = (RPC_PROVIDER_INDEX + 1) % len(SOLANA_RPC_URLS)
        ordered_urls = SOLANA_RPC_URLS[start_index:] + SOLANA_RPC_URLS[:start_index]
        for retry_round in range(2):
            for rpc_url in ordered_urls:
                request = Request(
                    rpc_url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urlopen(request, timeout=RPC_TIMEOUT_SECONDS) as response:
                        result = response.read()
                        parsed = json.loads(result)
                        if isinstance(parsed, dict) and parsed.get("error"):
                            continue
                        with RPC_CACHE_LOCK:
                            RPC_CACHE[cache_key] = (time.monotonic(), result)
                        return result
                except (HTTPError, OSError, TimeoutError, json.JSONDecodeError):
                    continue
            time.sleep(retry_round + 1)
        return None

    def _get_wallet(self, address):
        with RPC_REQUEST_LOCK:
            with RPC_CACHE_LOCK:
                cached = WALLET_CACHE.get(address)
            if cached and time.monotonic() - cached[0] < WALLET_CACHE_TTL_SECONDS:
                return cached[1]

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
                responses[request_body["id"]] = json.loads(result)

        sol_balance = 0
        balance_response = responses.get(1)
        if balance_response and isinstance(balance_response.get("result"), dict):
            sol_balance = balance_response["result"].get("value", 0) / 1_000_000_000

        token_amounts = {}
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

        wallet = {
            "solBalance": sol_balance,
            "tokens": [
                {"mint": mint, "amount": amount}
                for mint, amount in token_amounts.items()
            ],
            "unavailable": not responses,
            "partial": len(responses) < 3,
        }
        if not wallet["unavailable"] and not wallet["partial"]:
            with RPC_CACHE_LOCK:
                WALLET_CACHE[address] = (time.monotonic(), wallet)
        return wallet


# if __name__ == "__main__":
#     print("Solana Portfolio Tracker: http://127.0.0.1:8000/index.html")
#     ThreadingHTTPServer(("127.0.0.1", 8000), TrackerHandler).serve_forever()
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 10000))
    print(f"Solana Portfolio Tracker running on port {port}", flush=True)
    ThreadingHTTPServer(("", port), TrackerHandler).serve_forever()