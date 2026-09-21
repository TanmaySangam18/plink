# plink

Post-payment advertising infrastructure for India. A customer pays → webhook fires → OpenRTB 2.5 bid → Glance lock screen ad in 450ms.

**Live server:** https://plink-server-946m.onrender.com  
Built by [Tanmay Sangam](https://tanmaysangam.vercel.app)

---

## The problem

Mobile advertising in India has a targeting problem. Programmatic platforms (InMobi, DAN, Google) target on demographics and browsing history — proxies for purchase intent. The most accurate signal — a transaction that just happened — is locked inside payment processors and never reaches the ad exchange.

Post-payment is the highest-intent moment in a user's session. A customer who just paid ₹800 for dinner is primed for a coupon on dessert from the restaurant next door. That moment expires in seconds and is currently invisible to advertisers.

---

## Architecture

```
Payment processor                  Plink                         InMobi Exchange
(Cashfree / Pine Labs)             (FastAPI)                     (OpenRTB 2.5 SSP)
        │                              │                                  │
        │  POST /webhook/cashfree      │                                  │
        │  HMAC-SHA256 signed         ──>                                  │
        │                              │  verify sig                      │
        │                              │  parse & normalize phone         │
        │  {"status":"ok"}            <──                                  │
        │  (< 50ms)                    │                                  │
        │                              │  [BackgroundTask starts]         │
        │                              │                                  │
        │                              │  build OpenRTB 2.5 bid request  │
        │                              │  POST /openrtb25/bid ──────────>│
        │                              │  (450ms timeout)                 │
        │                              │                                  │  RTB auction
        │                              │  bid response <─────────────────│  (advertisers compete)
        │                              │                                  │
        │                              │                           Glance lock screen ad served
        │                              │                                  │
        │                              │  GET /win (nurl) <──────────────│  win notice
        │                              │  record confirmed revenue        │
```

**Why BackgroundTasks:** The payment processor expects a 200 within ~200ms or it retries. InMobi's RTB window is 450ms. These two constraints cannot both be satisfied in a single synchronous request. `BackgroundTasks` returns 200 to the payment processor immediately; the bid runs async in the same worker process without spawning a thread.

**Why FastAPI over Flask:** Async-native I/O means concurrent webhooks don't block each other at the bid step. Under load, a sync framework (Flask/Django) would stall waiting for InMobi's HTTP response; FastAPI handles concurrent bids without thread pool exhaustion.

---

## Platform model (zero merchant friction)

The original design required individual merchants to authorize an SDK or webhook — not viable in India where most offline merchants have no developer access.

**Solution:** B2B platform deals at the payment aggregator level.

| Platform | Coverage | Plink's deal type |
|---|---|---|
| Cashfree Marketplace | 800K+ merchants | Register as marketplace sub-merchant; Cashfree sends consolidated webhook per txn |
| Pine Labs P3P | 1.1M POS terminals | Register as P3P platform partner; Pine Labs sends webhook for every in-store UPI/card transaction |

One signed platform agreement → access to the entire merchant network. Merchants don't know Plink exists. Customers don't know Plink exists.

---

## Identity resolution

Plink resolves payment phone numbers to Glance device IDs via InMobi's user graph:

```
webhook phone number
  │
  ├─ normalize: strip country code, handle +91 / 0 / 91 prefixes, validate 10-digit
  │
  ├─ hash: SHA-256(normalized_phone) — Plink never sends raw numbers to InMobi
  │
  └─ bid request: user.buyeruid = sha256_hash
                  InMobi resolves hash → Glance install → serves on lock screen
```

Phone hashing means Plink never transmits PII to InMobi — the raw number stays server-side.

DSP mode (`server/dsp.py`): InMobi can also push bid requests to Plink. Identity resolution reverses — we look up incoming `user.ext.eids` / `user.buyeruid` against our payment store (TTL: 10 minutes from payment timestamp) and bid only if there's a recent payment match.

---

## OpenRTB 2.5 implementation

Plink sends three `imp` objects per bid — banner, video, and native — letting InMobi's auction pick the highest-value format:

```python
# models.py (abbreviated)
class Banner(BaseModel):
    w: int = 1080; h: int = 1920  # full Glance lock screen
    pos: int = 1                  # above the fold
    api: list[int] = [3, 5]       # MRAID 2 + MRAID 3

class Video(BaseModel):
    mimes: list[str] = ["video/mp4", "video/webm"]
    minduration: int = 15; maxduration: int = 30
    protocols: list[int] = [2, 3, 5, 6]  # VAST 2.0/3.0 + wrappers
    skip: int = 0                          # non-skippable
    playbackmethod: list[int] = [1]        # auto-play, sound on

class Native(BaseModel):
    # OpenRTB Native 1.2 — Glance content card layout
    # Assets: title (required), 1200×628 image (required), description, sponsor label
    request: str = '{"ver":"1.2","layout":6,"adunit":4,...}'
```

IAB category is inferred from purchase items using a keyword map (`inmobi.py: _IAB_KEYWORD_MAP`) — a list of `(keywords, IAB_code)` tuples scanned in O(n) against the transaction's item names. This lets advertisers target by category (IAB8-5 Food, IAB18 Fashion, etc.) without Plink having to maintain a product taxonomy.

---

## Revenue model

```
Cashfree (conservative — 5% of 300M txns/month):
  15M txns/month → 59% fill rate → 8.85M impressions
  ₹70 net CPM (after InMobi 40% rev share) → ₹619K/month

Pine Labs (5% of 20M txns/day):
  1M txns/day → 59% fill → 885K impressions/day
  Same CPM → ₹1.86M/month

Blended Month 2: ~₹2.5M/month (~$30K USD)
```

Assumptions: 59% fill from InMobi historical data for post-payment targeting. 40% InMobi rev share. ₹70 net CPM for banner; ₹180 net for multi-format blended.

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness probe → `{"status":"healthy"}` |
| `GET` | `/stats` | Live counters: webhooks received, bids sent, wins, confirmed revenue |
| `POST` | `/webhook/cashfree` | Cashfree Marketplace payment events (HMAC-SHA256 verified) |
| `POST` | `/webhook/pinelabs` | Pine Labs P3P in-store POS events |
| `POST` | `/webhook/razorpay` | Razorpay Technology Partner events |
| `GET` | `/win` | InMobi win notice (nurl) — records confirmed impression revenue |
| `GET` | `/test/bid` | Fire a test bid (sandbox flag set — no billing) |
| `POST` | `/openrtb25/bid` | DSP endpoint — receives bid requests from InMobi exchange |

---

## Run locally

```bash
cd server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in INMOBI_PUBLISHER_ID, INMOBI_PLACEMENT_ID
uvicorn main:app --reload
```

Test the full flow without InMobi credentials:

```bash
curl http://localhost:8000/test/bid
# fires a bid against InMobi sandbox; no billing; logs the full bid request/response
```

Simulate a payment webhook:

```bash
curl -X POST http://localhost:8000/webhook/cashfree \
  -H "Content-Type: application/json" \
  -H "x-webhook-signature: <hmac_sha256>" \
  -d '{"order_id":"ord_001","phone":"+919876543210","amount":850.00,"currency":"INR","items":["biryani","lassi"],"merchant_id":"m_001"}'
```

---

## Deploy

Railway (current):

```bash
railway up   # reads railway.toml; sets PORT, ENV=production automatically
```

Docker:

```bash
docker build -t plink ./server
docker run -p 8000:8000 --env-file server/.env plink
```

---

## Stack

Python 3.12 · FastAPI · Pydantic v2 · httpx (async HTTP) · Docker · Railway

---

## Status

Server live. Cashfree and Pine Labs platform deals in progress. InMobi publisher account under review.
