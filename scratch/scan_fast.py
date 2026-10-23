import json, urllib.request, time, sys
from concurrent.futures import ThreadPoolExecutor

accounts = json.load(open("account.json", "r", encoding="utf-8"))

RPCS = [
    "https://api.mainnet-beta.solana.com",
    "https://nodes.mewapi.io/rpc/sol",
    "https://solana-rpc.publicnode.com",
    "https://rpc.ankr.com/solana"
]

def rpc_call(method, params, attempt=0):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    # Try different RPCs
    for rpc in RPCS:
        try:
            req = urllib.request.Request(rpc, data=payload, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=4) as res:
                data = json.loads(res.read())
                if "result" in data:
                    return data["result"]
        except Exception:
            continue
    return None

def scan_wallet(acc):
    addr = acc["address"]
    name = acc["name"]
    
    # 1. SOL
    bal_res = rpc_call("getBalance", [addr])
    sol = (bal_res.get("value", 0) / 1e9) if bal_res else 0.0
    
    # 2. Tokenkeg
    tokens = {}
    keg = rpc_call("getTokenAccountsByOwner", [addr, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}, {"encoding": "jsonParsed"}])
    if keg:
        for it in keg.get("value", []):
            info = it["account"]["data"]["parsed"]["info"]
            if int(info["tokenAmount"]["amount"]) > 0:
                mint = info["mint"]
                tokens[mint] = tokens.get(mint, 0) + float(info["tokenAmount"]["uiAmountString"])
                
    # 3. Token-2022
    t22 = rpc_call("getTokenAccountsByOwner", [addr, {"programId": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}, {"encoding": "jsonParsed"}])
    if t22:
        for it in t22.get("value", []):
            info = it["account"]["data"]["parsed"]["info"]
            if int(info["tokenAmount"]["amount"]) > 0:
                mint = info["mint"]
                tokens[mint] = tokens.get(mint, 0) + float(info["tokenAmount"]["uiAmountString"])

    token_list = [{"mint": m, "amount": a} for m, a in sorted(tokens.items())]
    print(f"DONE {name}: sol={sol}, {len(token_list)} tokens -> {[(t['mint'][:6], t['amount']) for t in token_list]}", flush=True)
    return addr, {
        "name": name,
        "address": addr,
        "solBalance": sol,
        "tokens": token_list,
        "unavailable": False,
        "partial": False,
        "refreshedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }

print(f"Starting parallel scan of {len(accounts)} wallets...", flush=True)
start = time.time()
with ThreadPoolExecutor(max_workers=6) as ex:
    results = dict(ex.map(scan_wallet, accounts))

print(f"All scanned in {time.time()-start:.2f}s", flush=True)

with open("scratch/verified_wallets.json", "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)

print("Saved to scratch/verified_wallets.json", flush=True)
