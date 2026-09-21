# plink

Post-payment advertising infrastructure. A payment completes → webhook fires → programmatic bid → ad serves on the user's device within 450ms.

Built by [Tanmay Sangam](https://tanmaysangam.vercel.app)

---

## Architecture

```
Payment processor              Plink (FastAPI)               Ad Exchange (OpenRTB 2.5)
       │                             │                                  │
       │  POST /webhook/{provider}   │                                  │
       │  HMAC-signed ─────────────> │                                  │
       │                             │  verify signature                │
       │                             │  parse + normalize phone         │
       │  {"status":"ok"}           <─                                  │
       │  (< 50ms)                   │                                  │
       │                             │  [BackgroundTask]                │
       │                             │  build OpenRTB 2.5 bid request  │
       │                             │  POST /openrtb25/bid ──────────>│
       │                             │  (450ms timeout)                 │
       │                             │                          auction + ad served
       │                             │  GET /win (nurl) <─────────────│
       │                             │  record confirmed revenue        │
```

**Why BackgroundTasks:** Payment processors expect a 200 within ~200ms or they retry. The RTB window is 450ms. These constraints can't be satisfied synchronously — `BackgroundTasks` returns 200 immediately; the bid runs async in the same worker process without spawning threads.

**Why FastAPI:** Async-native I/O. Under concurrent webhooks, a sync framework stalls at the bid step waiting for the exchange's HTTP response. FastAPI handles concurrent bids without thread pool exhaustion.

---

## Identity resolution

```
webhook phone number
  │
  ├─ normalize: strip country code, handle prefix variants, validate format
  │
  ├─ hash: SHA-256(normalized_phone) — raw number never leaves the server
  │
  └─ bid request: user.buyeruid = sha256_hash
                  exchange resolves hash → device → ad served
```

Phone numbers are SHA-256 hashed before being sent to the exchange. PII stays server-side.

---

## OpenRTB 2.5 implementation

Three `imp` objects per bid request — banner, video, and native — letting the exchange's auction pick the highest-value format:

```python
class Banner(BaseModel):
    w: int = 1080; h: int = 1920   # full-screen
    pos: int = 1                    # above the fold
    api: list[int] = [3, 5]         # MRAID 2 + MRAID 3

class Video(BaseModel):
    mimes: list[str] = ["video/mp4", "video/webm"]
    minduration: int = 15; maxduration: int = 30
    protocols: list[int] = [2, 3, 5, 6]   # VAST 2.0/3.0 + wrappers
    skip: int = 0                           # non-skippable

class Native(BaseModel):
    # OpenRTB Native 1.2
    # Assets: title (required), main image (required), description, sponsor label
    request: str = '{"ver":"1.2","layout":6,"adunit":4,...}'
```

IAB category is inferred from transaction item names using a keyword map — scanned in O(n) against the item list, no external taxonomy dependency.

---

## DSP mode

`server/dsp.py` handles incoming bid requests from the exchange (buy-side). Identity resolution reverses — incoming `user.ext.eids` / `user.buyeruid` are matched against a local payment store (TTL: 10 minutes from payment timestamp). Bid only fires if there's a recent payment match for that device.

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness probe |
| `GET` | `/stats` | Live counters: webhooks, bids, wins, revenue |
| `POST` | `/webhook/{provider}` | Payment event webhook (HMAC-SHA256 verified) |
| `GET` | `/win` | Win notice (nurl) — records confirmed impression |
| `GET` | `/test/bid` | Test bid with sandbox flag — no billing |
| `POST` | `/openrtb25/bid` | DSP endpoint for incoming exchange bid requests |

---

## Run locally

```bash
cd server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload
```

```bash
# Test the bid pipeline (sandbox — no billing)
curl http://localhost:8000/test/bid
```

---

## Stack

Python 3.12 · FastAPI · Pydantic v2 · httpx (async) · Docker · OpenRTB 2.5
