# Robinhood Chain Early Radar — Render-ready

A read-only, phone-friendly PWA that discovers newly active tokens on **Robinhood Chain (chain ID 4663)** and scores them for *early activity*, not predicted returns.

It does **not** connect to Robinhood, Fomo, a wallet, or trading keys.

## Fastest deployment: GitHub + Render Blueprint

### 1. Put this folder on GitHub

Create a new GitHub repository. Upload **the contents of this folder** to the root of the repository. In other words, GitHub should show `app.py`, `render.yaml`, `requirements.txt`, and the `static` folder at the top level.

### 2. Deploy on Render

1. Sign in to Render and choose **New → Blueprint**.
2. Connect the GitHub repository you just created.
3. Render will automatically read `render.yaml`.
4. Approve/create the `robinhood-chain-early-radar` web service.
5. After the deploy succeeds, open the HTTPS address Render gives you.

You do **not** need to configure SSL certificates. Render terminates HTTPS for the public service URL.

### 3. Put it on your phone

**iPhone:** open the Render HTTPS address in Safari → Share → **Add to Home Screen**.

**Android:** open it in Chrome → menu → **Add to Home screen** / **Install app**.

The included web manifest and service worker let the site launch like a lightweight app.

## What the scanner watches

- Robinhood Chain Uniswap V3 pool-creation events.
- `launchpad.meme` new-launch feed as secondary discovery.
- DEX Screener recent Robinhood Chain token profiles/boosts as secondary discovery.
- DEX Screener market data for liquidity, transactions, volume, price movement, pair age, and valuation.

## Score (0–100)

Positive inputs include freshness, usable liquidity, 5-minute transaction velocity, balanced buy pressure, volume/liquidity intensity, moderate early price momentum, and observed volume acceleration. Penalties include very thin liquidity, no recent sells despite many buys, very low liquidity versus valuation, and already-extreme short-term moves.

**Important:** the score is an activity/risk heuristic. It cannot prove that a token is safe, sellable, non-malicious, or likely to rise.

## Render configuration included

`render.yaml` already specifies:

- Python web service
- dependency installation from `requirements.txt`
- production Uvicorn start command using Render's `$PORT`
- `/health` health check
- automatic deploys after GitHub pushes
- Python 3.11.11
- scanner defaults

No environment variables are required for the first deployment.

## Optional environment variables

You can change these later in Render → your service → Environment:

- `RH_RPC_URL`: defaults to Robinhood's public mainnet RPC. A private RPC provider may be more reliable for heavier use.
- `SCAN_SECONDS`: polling interval; default `25`.
- `BOOTSTRAP_BLOCKS`: blocks scanned on first boot; default `12000`.
- `MAX_TRACKED`: maximum recent token addresses retained; default `180`.
- `RH_V3_FACTORY`, `RH_WETH`: overrides if network/DEX infrastructure changes.

After changing an environment variable, redeploy/restart the service.

## Run locally (optional)

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000`.

## Current limitations

- Discovery is strongest for **Uniswap V3**, with launchpad/profile feeds supplementing it. A token exclusive to another DEX may not appear immediately.
- It does not yet calculate holder concentration, deployer history, verified-source risk, honeypot simulation, or wallet-cluster behavior.
- Scanner state is currently stored in memory. If Render restarts the service, the scanner rebuilds its recent-token set.
- The dashboard refreshes automatically, but browser push alerts are not part of this build yet.
- A free hosting service may sleep/restart or impose resource limits. For genuinely continuous scanning/alerts, an always-on instance is preferable.

## Safety

Treat every fresh memecoin as extremely speculative. Verify the exact contract address, actual sellability, liquidity, holder/deployer behavior, and position size independently before trading.
