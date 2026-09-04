import os, re, time, threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests
import feedparser
from eth_utils import keccak
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

APP_TITLE = "Robinhood Chain Early Radar"
CHAIN = "robinhood"
CHAIN_ID = 4663
RPC_URL = os.getenv("RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
V3_FACTORY = os.getenv("RH_V3_FACTORY", "0x1f7d7550B1b028f7571E69A784071F0205FD2EfA")
WETH = os.getenv("RH_WETH", "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73").lower()
LAUNCHPAD_FEED = os.getenv("RH_LAUNCHPAD_FEED", "https://launchpad.meme/feed/new-launches.xml")
DEX = "https://api.dexscreener.com"
SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "25"))
BOOTSTRAP_BLOCKS = int(os.getenv("BOOTSTRAP_BLOCKS", "12000"))
MAX_TRACKED = int(os.getenv("MAX_TRACKED", "180"))

POOL_CREATED_TOPIC = "0x" + keccak(text="PoolCreated(address,address,uint24,int24,address)").hex()
ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")

app = FastAPI(title=APP_TITLE)
lock = threading.Lock()
state = {
    "last_block": None,
    "last_scan": None,
    "source_status": {},
    "tracked": {},       # token address -> discovery metadata
    "ranked": [],
    "history": {},       # token address -> list of snapshots
}


def rpc(method: str, params: list):
    r = requests.post(RPC_URL, json={"jsonrpc":"2.0","id":1,"method":method,"params":params}, timeout=12)
    r.raise_for_status()
    body = r.json()
    if body.get("error"):
        raise RuntimeError(body["error"])
    return body.get("result")


def current_block() -> int:
    return int(rpc("eth_blockNumber", []), 16)


def decode_topic_address(topic: str) -> str:
    return "0x" + topic[-40:]


def scan_v3_pools(from_block: int, to_block: int) -> List[str]:
    """Discover token addresses from newly created Uniswap V3 pools."""
    found = set()
    # Smaller chunks are friendlier to public RPC rate limits.
    for start in range(from_block, to_block + 1, 1800):
        end = min(start + 1799, to_block)
        logs = rpc("eth_getLogs", [{
            "fromBlock": hex(start), "toBlock": hex(end),
            "address": V3_FACTORY, "topics": [POOL_CREATED_TOPIC]
        }]) or []
        for log in logs:
            topics = log.get("topics", [])
            if len(topics) >= 3:
                a = decode_topic_address(topics[1]).lower()
                b = decode_topic_address(topics[2]).lower()
                # Prefer the non-WETH side; if neither side is WETH keep both,
                # since Robinhood Chain also supports pools against other quotes.
                if a == WETH and b != WETH: found.add(b)
                elif b == WETH and a != WETH: found.add(a)
                else:
                    if a != WETH: found.add(a)
                    if b != WETH: found.add(b)
    return list(found)


def scan_launchpad_feed() -> List[str]:
    """Secondary discovery path for coins advertised in launchpad.meme's new-launch RSS."""
    out = set()
    feed = feedparser.parse(LAUNCHPAD_FEED)
    for e in feed.entries[:120]:
        blob = " ".join(str(e.get(k, "")) for k in ("link","title","summary","description"))
        for addr in ADDRESS_RE.findall(blob):
            out.add(addr.lower())
    return list(out)


def scan_dex_profiles() -> List[str]:
    """Secondary discovery path: recently profiled/boosted Robinhood tokens on DEX Screener."""
    out = set()
    for endpoint in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
        r = requests.get(DEX + endpoint, timeout=12)
        if r.ok:
            data = r.json()
            if isinstance(data, dict): data = [data]
            for item in data or []:
                if str(item.get("chainId", "")).lower() == CHAIN:
                    addr = item.get("tokenAddress")
                    if addr and ADDRESS_RE.fullmatch(addr): out.add(addr.lower())
    return list(out)


def dex_pairs(addresses: List[str]) -> Dict[str, dict]:
    best = {}
    for i in range(0, len(addresses), 30):
        batch = addresses[i:i+30]
        try:
            r = requests.get(f"{DEX}/tokens/v1/{CHAIN}/" + ",".join(batch), timeout=15)
            if not r.ok: continue
            pairs = r.json() or []
            if isinstance(pairs, dict): pairs = pairs.get("pairs", []) or []
            for p in pairs:
                base = (p.get("baseToken") or {}).get("address", "").lower()
                quote = (p.get("quoteToken") or {}).get("address", "").lower()
                target = base if base in batch else quote if quote in batch else None
                if not target: continue
                liq = float((p.get("liquidity") or {}).get("usd") or 0)
                old = best.get(target)
                old_liq = float(((old or {}).get("liquidity") or {}).get("usd") or 0)
                if old is None or liq > old_liq:
                    best[target] = p
        except Exception:
            continue
    return best


def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


def fnum(v, default=0.0):
    try: return float(v if v is not None else default)
    except Exception: return default


def score_pair(addr: str, p: dict, discovered_at: float, history: list) -> dict:
    now_ms = time.time() * 1000
    created = fnum(p.get("pairCreatedAt"), now_ms)
    age_min = max(0.0, (now_ms - created) / 60000)
    liq = fnum((p.get("liquidity") or {}).get("usd"))
    mc = fnum(p.get("marketCap") or p.get("fdv"))
    vol = p.get("volume") or {}
    tx = p.get("txns") or {}
    pc = p.get("priceChange") or {}
    v5, v1h, v6h = fnum(vol.get("m5")), fnum(vol.get("h1")), fnum(vol.get("h6"))
    t5 = tx.get("m5") or {}; t1 = tx.get("h1") or {}; t6 = tx.get("h6") or {}
    buys5, sells5 = fnum(t5.get("buys")), fnum(t5.get("sells"))
    buys1, sells1 = fnum(t1.get("buys")), fnum(t1.get("sells"))
    trades5 = buys5 + sells5
    trades1 = buys1 + sells1
    buy_ratio = (buys5 + 1) / (sells5 + 1)
    ch5, ch1, ch6 = fnum(pc.get("m5")), fnum(pc.get("h1")), fnum(pc.get("h6"))

    # 0-100 heuristic. It is intentionally conservative around thin liquidity
    # and giant immediate pumps, which are common failure modes in fresh tokens.
    s = 0.0
    # Freshness (max 18)
    if age_min <= 10: s += 18
    elif age_min <= 30: s += 15
    elif age_min <= 120: s += 11
    elif age_min <= 360: s += 6
    elif age_min <= 1440: s += 2
    # Liquidity quality (max 20)
    if liq >= 100000: s += 20
    elif liq >= 50000: s += 17
    elif liq >= 20000: s += 13
    elif liq >= 8000: s += 8
    elif liq >= 3000: s += 3
    else: s -= 15
    # Transaction velocity (max 20)
    s += min(20, trades5 * 0.7 + trades1 * 0.08)
    # Buy pressure (max 14; balanced positive flow beats zero-sell anomalies)
    if sells5 == 0 and buys5 >= 8: s += 2
    elif 1.15 <= buy_ratio < 1.6: s += 7
    elif 1.6 <= buy_ratio < 2.5: s += 11
    elif 2.5 <= buy_ratio <= 5: s += 14
    elif buy_ratio > 5: s += 8
    # Volume intensity (max 14)
    if liq > 0:
        intensity = v5 / liq
        s += min(14, intensity * 45)
    # Price structure (reward early movement, penalize blow-off)
    if 0.5 <= ch5 <= 8: s += 8
    elif 8 < ch5 <= 18: s += 4
    elif ch5 > 25: s -= 12
    if ch1 > 80: s -= 16
    elif ch1 > 40: s -= 8
    elif 2 <= ch1 <= 25: s += 6
    # Liquidity to valuation sanity check
    if mc > 0:
        lmr = liq / mc
        if lmr >= .15: s += 6
        elif lmr < .02: s -= 8
    # Require evidence of exits being possible; no sells + many buys is a warning.
    no_sell_warning = (sells1 == 0 and buys1 >= 12)
    if no_sell_warning: s -= 12

    # Snapshot acceleration: compare to previous scanner observation.
    acceleration = 0.0
    if history:
        prev = history[-1]
        dt = max(1, time.time() - prev["ts"])
        prev_v5 = fnum(prev.get("v5"))
        acceleration = (v5 - prev_v5) / dt * 60
        if acceleration > 1000: s += 7
        elif acceleration > 250: s += 4
        elif acceleration < -1000: s -= 3

    score = int(round(clamp(s)))
    risk = "HIGH"
    if liq >= 50000 and not no_sell_warning and (mc == 0 or liq/mc >= .05): risk = "MED-HIGH"
    if liq < 5000 or no_sell_warning: risk = "VERY HIGH"
    status = "EARLY" if score >= 78 else "WATCH" if score >= 60 else "WARM" if score >= 42 else "SKIP"
    if ch5 > 25 or ch1 > 80: status = "OVEREXTENDED"

    base = p.get("baseToken") or {}; quote = p.get("quoteToken") or {}
    token_meta = base if base.get("address", "").lower() == addr else quote
    reasons = []
    if age_min <= 30: reasons.append("fresh pair")
    if liq >= 20000: reasons.append("meaningful liquidity")
    if buy_ratio >= 1.6 and sells5 > 0: reasons.append("buy pressure")
    if trades5 >= 15: reasons.append("fast transaction activity")
    if 0.5 <= ch5 <= 18: reasons.append("early price momentum")
    if acceleration > 250: reasons.append("volume accelerating")
    warnings = []
    if liq < 8000: warnings.append("thin liquidity")
    if no_sell_warning: warnings.append("no recent sells; exitability unproven")
    if ch5 > 25 or ch1 > 80: warnings.append("already heavily extended")
    if mc > 0 and liq/mc < .02: warnings.append("very low liquidity vs valuation")

    return {
        "address": addr, "name": token_meta.get("name") or "Unknown", "symbol": token_meta.get("symbol") or "?",
        "score": score, "status": status, "risk": risk, "ageMinutes": round(age_min,1),
        "priceUsd": p.get("priceUsd"), "liquidityUsd": round(liq,2), "marketCap": round(mc,2),
        "volume5m": round(v5,2), "volume1h": round(v1h,2), "volume6h": round(v6h,2),
        "buys5m": int(buys5), "sells5m": int(sells5), "buys1h": int(buys1), "sells1h": int(sells1),
        "price5m": ch5, "price1h": ch1, "price6h": ch6, "buyRatio5m": round(buy_ratio,2),
        "volumeAccelPerMin": round(acceleration,2), "dex": p.get("dexId"), "pairAddress": p.get("pairAddress"),
        "dexUrl": p.get("url"), "imageUrl": (p.get("info") or {}).get("imageUrl"),
        "reasons": reasons, "warnings": warnings,
        "discoveredAt": datetime.fromtimestamp(discovered_at, timezone.utc).isoformat(),
    }


def run_scan():
    statuses = {}
    discovered = {}
    # 1. On-chain V3 pool discovery — most important path.
    try:
        latest = current_block()
        with lock: last = state["last_block"]
        start = max(0, latest - BOOTSTRAP_BLOCKS) if last is None else last + 1
        if start <= latest:
            for addr in scan_v3_pools(start, latest): discovered[addr] = "uniswap-v3-onchain"
        with lock: state["last_block"] = latest
        statuses["onchain"] = "ok"
    except Exception as e:
        statuses["onchain"] = f"error: {str(e)[:100]}"

    # 2/3. Secondary discovery paths.
    try:
        for addr in scan_launchpad_feed(): discovered.setdefault(addr, "launchpad-feed")
        statuses["launchpad"] = "ok"
    except Exception as e: statuses["launchpad"] = f"error: {str(e)[:100]}"
    try:
        for addr in scan_dex_profiles(): discovered.setdefault(addr, "dexscreener-latest")
        statuses["dexscreener"] = "ok"
    except Exception as e: statuses["dexscreener"] = f"error: {str(e)[:100]}"

    now = time.time()
    with lock:
        for addr, source in discovered.items():
            state["tracked"].setdefault(addr, {"first_seen": now, "source": source})
        # Keep most recently found tokens, bounded for API/rate-limit safety.
        items = sorted(state["tracked"].items(), key=lambda kv: kv[1]["first_seen"], reverse=True)[:MAX_TRACKED]
        state["tracked"] = dict(items)
        addresses = list(state["tracked"].keys())

    pairs = dex_pairs(addresses) if addresses else {}
    ranked = []
    with lock:
        for addr, p in pairs.items():
            hist = state["history"].setdefault(addr, [])
            row = score_pair(addr, p, state["tracked"][addr]["first_seen"], hist)
            row["source"] = state["tracked"][addr]["source"]
            ranked.append(row)
            hist.append({"ts": now, "v5": row["volume5m"], "score": row["score"], "price": row["priceUsd"]})
            if len(hist) > 120: del hist[:-120]
        ranked.sort(key=lambda x: (x["score"], x["volume5m"]), reverse=True)
        state["ranked"] = ranked
        state["last_scan"] = datetime.now(timezone.utc).isoformat()
        state["source_status"] = statuses


def scanner_loop():
    while True:
        try: run_scan()
        except Exception as e:
            with lock: state["source_status"]["scanner"] = f"error: {e}"
        time.sleep(SCAN_SECONDS)

@app.on_event("startup")
def startup():
    threading.Thread(target=scanner_loop, daemon=True).start()

@app.get("/health")
def health():
    return {"ok": True, "service": "robinhood-chain-early-radar"}

@app.get("/api/radar")
def radar(limit: int = 60):
    with lock:
        return {
            "chain": "Robinhood Chain", "chainId": CHAIN_ID,
            "lastScan": state["last_scan"], "lastBlock": state["last_block"],
            "sourceStatus": state["source_status"], "tracked": len(state["tracked"]),
            "tokens": state["ranked"][:max(1,min(limit,100))]
        }

@app.post("/api/track/{address}")
def track(address: str):
    a = address.lower()
    if not ADDRESS_RE.fullmatch(a): raise HTTPException(400, "Invalid EVM token address")
    with lock: state["tracked"].setdefault(a, {"first_seen": time.time(), "source": "manual"})
    return {"ok": True, "address": a}

@app.post("/api/rescan")
def rescan():
    threading.Thread(target=run_scan, daemon=True).start()
    return {"ok": True}

@app.get("/")
def home(): return FileResponse("static/index.html")
@app.get("/manifest.webmanifest")
def manifest(): return FileResponse("static/manifest.webmanifest", media_type="application/manifest+json")
@app.get("/sw.js")
def sw(): return FileResponse("static/sw.js", media_type="application/javascript")

app.mount("/static", StaticFiles(directory="static"), name="static")
