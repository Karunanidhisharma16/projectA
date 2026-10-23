import json

wallets = json.load(open('scratch/verified_wallets.json', 'r', encoding='utf-8'))
prices = json.load(open('price_cache.json', 'r', encoding='utf-8'))
price_map = {m: (p[1] if isinstance(p, list) else p) for m, p in prices.items()}

meta = {
    'XsDoVfqeBukxuZHWhdvWHBhgEHjGNst4MLodqsJHzoB': 'TSLAx',
    'Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh': 'NVDAx',
    'XsbEhLAtcf6HdfpFZ5xEMdqW8nfAvcsP5bdudRLJzJp': 'AAPLx',
    'CARSsxWPkpQWvfyRBwfGMGvysJBHdHGfE46X5MNgmeta': 'CARS',
    '8RVBk8vxLiUHueLUW1f4izFVqN3nWippLhkohKg6EGkS': 'KNOTS',
    'HXQ66zSRqynwJQ6vYEYa85qGY9C2Ycz8rnYQHgApn391': 'STOKN',
    'So11111111111111111111111111111111111111112': 'SOL'
}

grand_total = 0.0
token_totals = {}

print('=== REAL ON-CHAIN LIVE AUDIT ===')
for addr, w in wallets.items():
    sol_price = price_map.get('So11111111111111111111111111111111111111112', 0)
    sol_val = w['solBalance'] * sol_price
    w_total = sol_val
    token_str = []
    if sol_val > 0:
        token_str.append(f"SOL: {w['solBalance']} (${sol_val:.2f})")
        token_totals['SOL'] = token_totals.get('SOL', 0) + w['solBalance']
    for t in w['tokens']:
        m = t['mint']
        amt = t['amount']
        sym = meta.get(m, m[:6])
        token_totals[sym] = token_totals.get(sym, 0) + amt
        p = price_map.get(m, 0)
        val = amt * p
        w_total += val
        token_str.append(f"{sym}: {amt:g} (${val:.2f})")
    grand_total += w_total
    contents = ', '.join(token_str) if token_str else 'Empty'
    print(f"{w['name']} (${w_total:.2f}): {contents}")

print(f"\n=== GRAND TOTAL USD: ${grand_total:.2f} ===")
print('=== AGGREGATE TOKEN TOTALS ===')
for sym, amt in token_totals.items():
    p = 0
    for m, s in meta.items():
        if s == sym:
            p = price_map.get(m, 0)
    print(f"  {sym:8s}: {amt:15.6f}  (Price: ${p:.6f}, Value: ${amt*p:.2f})")
