"""
Plink FastAPI server — post-payment advertising for India.

Flows:
  Razorpay            →  POST /webhook/razorpay     →  BackgroundTask → InMobi bid
  Cashfree Marketplace→  POST /webhook/cashfree     →  BackgroundTask → InMobi bid
  Pine Labs Online    →  POST /webhook/pinelabs     →  BackgroundTask → InMobi bid

  InMobi win notice   →  GET  /win                 →  revenue recorded

All webhook responses return 200 {"status": "ok"} immediately.
InMobi RTB is always done in a background task to avoid blocking the payment processor.
"""

from __future__ import annotations

import hashlib
import hmac
import json as _json
import logging
import logging.config
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from config import settings
from inmobi import normalize_phone, send_bid
from models import PaymentEvent



# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    log_level = settings.LOG_LEVEL.upper()
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {
                    "()": "logging.Formatter",
                    "fmt": '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
                    "datefmt": "%Y-%m-%dT%H:%M:%S",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                    "stream": "ext://sys.stdout",
                },
            },
            "root": {
                "level": log_level,
                "handlers": ["console"],
            },
        }
    )


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-memory stats (thread-safe enough for single-process deployments)
# ---------------------------------------------------------------------------

@dataclass
class Stats:
    webhooks_received: int = 0
    inmobi_calls_made: int = 0
    inmobi_errors: int = 0
    wins_confirmed: int = 0
    revenue_usd: float = 0.0
    started_at: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict[str, Any]:
        uptime_seconds = int(time.time()) - self.started_at
        return {
            "webhooks_received": self.webhooks_received,
            "inmobi_calls_made": self.inmobi_calls_made,
            "inmobi_errors": self.inmobi_errors,
            "wins_confirmed": self.wins_confirmed,
            "revenue_usd": round(self.revenue_usd, 6),
            "uptime_seconds": uptime_seconds,
        }


_stats = Stats()


# ---------------------------------------------------------------------------
# Background task: fire InMobi bid and track revenue
# ---------------------------------------------------------------------------

async def _deliver_ad(event: PaymentEvent) -> None:
    _stats.inmobi_calls_made += 1
    try:
        result = await send_bid(event)
        if result is None:
            logger.info("InMobi no-bid or sandbox", extra={"order_id": event.order_id})
            return
        # Extract winning price and fire nurl for impression confirmation
        try:
            seatbids = result.get("seatbid", [])
            if seatbids and seatbids[0].get("bid"):
                winning_bid = seatbids[0]["bid"][0]
                price = float(winning_bid.get("price", 0))
                nurl = winning_bid.get("nurl", "")
                _stats.wins_confirmed += 1
                _stats.revenue_usd += price / 1000  # CPM → per-impression revenue
                logger.info(
                    "InMobi win — revenue recorded",
                    extra={
                        "order_id": event.order_id,
                        "price_cpm": price,
                        "revenue_usd": price / 1000,
                        "total_revenue_usd": _stats.revenue_usd,
                    },
                )
                if nurl:
                    import httpx as _hx
                    try:
                        async with _hx.AsyncClient(timeout=2.0) as c:
                            await c.get(nurl)
                    except Exception:
                        pass  # nurl fire is best-effort, never block
        except Exception as exc:
            logger.warning("Failed to parse win from InMobi response",
                           extra={"error": str(exc), "order_id": event.order_id})
    except Exception:
        _stats.inmobi_errors += 1
        logger.exception("Unhandled error in InMobi background task",
                         extra={"order_id": event.order_id})


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def _verify_razorpay_signature(body: bytes, signature_header: str | None) -> None:
    """Verify Razorpay's HMAC-SHA256 webhook signature.

    Razorpay computes: HMAC-SHA256(webhook_secret, raw_request_body)
    and sends the hex digest in the X-Razorpay-Signature header.

    Raises HTTP 401 if the signature is missing or invalid.
    Reference: https://razorpay.com/docs/webhooks/validate-test/
    """
    if not settings.PLINK_WEBHOOK_SECRET:
        logger.warning(
            "PLINK_WEBHOOK_SECRET not set — skipping Razorpay signature verification"
        )
        return

    if not signature_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Razorpay-Signature header",
        )

    expected = hmac.new(
        settings.PLINK_WEBHOOK_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Razorpay webhook signature",
        )


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    logger.info(
        "Plink server starting",
        extra={
            "env": settings.ENV,
            "sandbox_mode": settings.sandbox_mode,
            "inmobi_endpoint": settings.INMOBI_ENDPOINT,
            "log_level": settings.LOG_LEVEL,
        },
    )
    if settings.sandbox_mode:
        logger.warning(
            "Running in SANDBOX MODE — InMobi bids will NOT be sent; "
            "bid requests will be logged only"
        )
    yield
    logger.info("Plink server shutting down")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Plink API",
    description=(
        "Post-payment advertising server for India. "
        "Receives Razorpay, Cashfree, and Pine Labs webhooks, delivers lock-screen ads via InMobi Buyer Hub."
    ),
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.is_development else None,
    redoc_url="/redoc" if settings.is_development else None,
)

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/", tags=["ops"])
async def root() -> dict[str, Any]:
    """Root endpoint — company and server info."""
    return {
        "company": "Plink",
        "description": "Post-payment advertising platform — India",
        "website": "https://plink-website.vercel.app",
        "server": "healthy",
        "version": "1.0.0",
        "contact": "hello@getplink.in",
    }


@app.get("/health", tags=["ops"])
async def health() -> dict[str, str]:
    """Liveness probe — returns immediately."""
    return {"status": "healthy", "version": "1.0.0"}


@app.get("/stats", tags=["ops"])
async def stats() -> dict[str, Any]:
    """In-memory counters for monitoring."""
    return _stats.to_dict()


@app.get("/win", tags=["ops"])
async def win_notice(
    price: float = 0.0,
    rid: str = "",
    iid: str = "",
) -> Response:
    """
    Win notice endpoint — InMobi calls this URL to confirm a Glance impression was served.

    InMobi fires: GET /win?price=<clearing_cpm>&rid=<request_id>&iid=<imp_id>
    We record the confirmed impression and revenue.
    Always returns 200 immediately (InMobi ignores the body).
    """
    _stats.wins_confirmed += 1
    _stats.revenue_usd += price / 1000  # CPM → per-impression revenue
    logger.info(
        "Win notice received",
        extra={
            "price_cpm": price,
            "revenue_usd": price / 1000,
            "total_revenue_usd": _stats.revenue_usd,
            "request_id": rid,
            "imp_id": iid,
        },
    )
    return Response(status_code=200)


@app.get("/test/bid", tags=["ops"])
async def test_bid_outbound() -> dict[str, Any]:
    """Fire a real outbound bid to InMobi (SSP mode legacy test).

    Use this to verify InMobi credentials and endpoint connectivity.
    Safe to call — a bid that doesn't win costs nothing.
    """
    import httpx as _httpx
    from inmobi import build_bid_request as _build
    from models import PaymentEvent as _PE

    dummy_event = _PE(
        order_id="plink-test-001",
        phone="+919876543210",
        amount=299.0,
        currency="INR",
        items=["coffee"],
        merchant_id="test_merchant",
        timestamp=int(time.time()),
    )

    bid_req = _build(dummy_event, test=True)

    if not settings.INMOBI_PUBLISHER_ID:
        return {
            "status": "sandbox_mode",
            "note": "INMOBI_PUBLISHER_ID not set — add it to Render env vars",
            "bid_request_preview": bid_req,
        }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-inmobi-publisher-id": settings.INMOBI_PUBLISHER_ID,
    }
    if settings.INMOBI_API_KEY:
        headers["Authorization"] = f"Bearer {settings.INMOBI_API_KEY}"

    try:
        async with _httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                settings.INMOBI_ENDPOINT,
                json=bid_req,
                headers=headers,
            )
        return {
            "status": "called",
            "http_status": response.status_code,
            "response_body": response.text[:2000],
            "endpoint": settings.INMOBI_ENDPOINT,
            "publisher_id": settings.INMOBI_PUBLISHER_ID,
            "bid_request_id": bid_req["id"],
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
            "endpoint": settings.INMOBI_ENDPOINT,
        }


@app.post("/webhook/razorpay", tags=["webhook"])
async def razorpay_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """
    Receive a signed payment.captured event from Razorpay.

    Razorpay signs the request body with HMAC-SHA256 using the webhook secret
    configured during app registration (stored in PLINK_WEBHOOK_SECRET).
    The signature is sent in the X-Razorpay-Signature header.

    Payload structure:
      {
        "entity": "event",
        "event": "payment.captured",
        "payload": {
          "payment": {
            "entity": {
              "id": "pay_xxx",
              "amount": 45000,           # paise (÷100 for rupees)
              "currency": "INR",
              "status": "captured",
              "contact": "+919876543210",
              "email": "customer@example.com",
              "description": "...",
              "merchant_id": "merchant_xxx"
            }
          }
        }
      }
    """
    body: bytes = await request.body()

    # 1. Razorpay HMAC-SHA256 signature verification
    _verify_razorpay_signature(body, request.headers.get("X-Razorpay-Signature"))

    # 2. Parse payload
    try:
        raw_data = _json.loads(body)
    except _json.JSONDecodeError as exc:
        logger.warning(
            "Failed to parse Razorpay webhook body",
            extra={"error": str(exc), "body_preview": body[:256].decode("utf-8", errors="replace")},
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid JSON payload: {exc}",
        )

    # 3. Only process payment.captured events — ack everything else silently
    event_type: str = raw_data.get("event", "")
    if event_type != "payment.captured":
        logger.info(
            "Razorpay event ignored (not payment.captured)",
            extra={"event": event_type},
        )
        return {"status": "ok", "note": f"event {event_type!r} not handled"}

    # 4. Extract payment entity
    try:
        payment_entity: dict[str, Any] = raw_data["payload"]["payment"]["entity"]
    except (KeyError, TypeError) as exc:
        logger.warning(
            "Razorpay webhook missing payment entity",
            extra={"error": str(exc), "body_preview": body[:256].decode("utf-8", errors="replace")},
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Missing payload.payment.entity: {exc}",
        )

    # 5. Extract and normalize phone number
    raw_contact: str = payment_entity.get("contact", "")
    try:
        phone = normalize_phone(raw_contact)
    except ValueError as exc:
        logger.warning(
            "Razorpay webhook: invalid phone number — skipping bid",
            extra={"contact": raw_contact, "payment_id": payment_entity.get("id", "")},
        )
        # Return 200 so Razorpay does not retry — the customer simply won't get an ad
        return {"status": "ok", "note": "invalid phone number, ad skipped"}

    # Amount is in paise; convert to rupees for consistency with WooCommerce events
    amount_paise: int = payment_entity.get("amount", 0)
    amount_inr: float = amount_paise / 100.0

    merchant_id: str = payment_entity.get("merchant_id", "razorpay_unknown")
    payment_id: str = payment_entity.get("id", "")
    description: str = payment_entity.get("description", "")
    currency: str = payment_entity.get("currency", "INR")

    # 6. Build a PaymentEvent compatible with the existing InMobi pipeline
    try:
        event = PaymentEvent(
            order_id=payment_id,
            phone=phone,
            amount=amount_inr,
            currency=currency,
            items=[description] if description else [],
            merchant_id=merchant_id,
            timestamp=int(time.time()),
        )
    except Exception as exc:
        logger.warning(
            "Failed to construct PaymentEvent from Razorpay payload",
            extra={"error": str(exc), "payment_id": payment_id},
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid payment data: {exc}",
        )

    _stats.webhooks_received += 1

    logger.info(
        "Razorpay webhook received",
        extra={
            "payment_id": payment_id,
            "merchant_id": merchant_id,
            "phone_last4": phone[-4:],
            "amount_inr": amount_inr,
            "currency": currency,
        },
    )

    # 7. Store payment context in background — must not block the 200 response
    background_tasks.add_task(_deliver_ad, event)

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Cashfree Marketplace webhook
# ---------------------------------------------------------------------------
# Cashfree sends a webhook for every payment across all sub-merchants on your
# platform account. No per-merchant setup — merchants are onboarded via API.
# Docs: https://www.cashfree.com/docs/payments/online/webhooks/overview
#
# Cashfree signature: HMAC-SHA256(timestamp + raw_body, client_secret)
# Header: x-webhook-signature  (format: "timestamp.signature")
# ---------------------------------------------------------------------------

@app.post("/webhook/cashfree", tags=["webhook"])
async def cashfree_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """
    Receive payment events from Cashfree Marketplace platform account.

    Cashfree fires this for every successful payment across all merchants
    you have onboarded as sub-accounts — no individual merchant authorization
    needed beyond initial KYC onboarding via the Cashfree Marketplace API.

    Expected payload (Cashfree webhook v2025-01-01):
      {
        "data": {
          "order": { "order_id": "...", "order_amount": 450.00, "order_currency": "INR" },
          "payment": {
            "payment_id": "...",
            "payment_status": "SUCCESS",
            "payment_amount": 450.00,
            "payment_currency": "INR",
            "payment_phone": "+919876543210",
            "cf_payment_id": "..."
          },
          "customer_details": { "customer_phone": "+919876543210" }
        },
        "event_time": "2026-09-01T12:00:00+05:30",
        "type": "PAYMENT_SUCCESS_WEBHOOK",
        "merchant_data": { "merchant_id": "merchant_xyz" }
      }
    """
    body: bytes = await request.body()

    # Cashfree signature: "timestamp.base64signature" in x-webhook-signature header
    sig_header = request.headers.get("x-webhook-signature", "")
    ts_header = request.headers.get("x-webhook-timestamp", "")
    if settings.PLINK_WEBHOOK_SECRET and sig_header:
        import base64
        expected = hmac.new(
            settings.PLINK_WEBHOOK_SECRET.encode(),
            (ts_header + body.decode("utf-8")).encode(),
            hashlib.sha256,
        ).digest()
        provided_sig = sig_header.split(".")[-1] if "." in sig_header else sig_header
        try:
            provided_bytes = base64.b64decode(provided_sig)
        except Exception:
            provided_bytes = b""
        if not hmac.compare_digest(expected, provided_bytes):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Invalid Cashfree webhook signature")

    try:
        raw = _json.loads(body)
    except _json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"Invalid JSON: {exc}")

    # Only process successful payments
    event_type: str = raw.get("type", "")
    if event_type != "PAYMENT_SUCCESS_WEBHOOK":
        return {"status": "ok", "note": f"event {event_type!r} not handled"}

    try:
        data = raw["data"]
        payment = data["payment"]
        customer = data.get("customer_details", {})
        raw_phone: str = customer.get("customer_phone") or payment.get("payment_phone", "")
        amount: float = float(payment.get("payment_amount", 0))
        currency: str = payment.get("payment_currency", "INR")
        payment_id: str = payment.get("cf_payment_id") or payment.get("payment_id", "")
        merchant_id: str = raw.get("merchant_data", {}).get("merchant_id", "cashfree_unknown")
    except (KeyError, TypeError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"Missing fields: {exc}")

    try:
        phone = normalize_phone(raw_phone)
    except ValueError:
        logger.warning("Cashfree webhook: invalid phone — skipping bid",
                       extra={"phone": raw_phone, "payment_id": payment_id})
        return {"status": "ok", "note": "invalid phone, ad skipped"}

    event = PaymentEvent(
        order_id=payment_id,
        phone=phone,
        amount=amount,
        currency=currency,
        items=[],
        merchant_id=merchant_id,
        timestamp=int(time.time()),
    )

    _stats.webhooks_received += 1
    logger.info("Cashfree webhook received",
                extra={"payment_id": payment_id, "merchant_id": merchant_id,
                       "phone_last4": phone[-4:], "amount": amount})

    background_tasks.add_task(_deliver_ad, event)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Pine Labs P3P (in-store POS) webhook
# ---------------------------------------------------------------------------
# Pine Labs Payment Protocol launched June 2026. Covers 980K+ physical
# merchants (POS terminals). Partner API gives centralized payment events.
# Docs: developer.pinelabs.com (P3P section)
# ---------------------------------------------------------------------------

@app.post("/webhook/pinelabs", tags=["webhook"])
async def pinelabs_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """
    Receive ORDER_PROCESSED events from Pine Labs Online P3P platform.

    Pine Labs fires this for every completed order across all merchants on
    the P3P platform account. No per-merchant setup required.

    Payload structure (Pine Labs Online webhook v2):
      {
        "event_type": "ORDER_PROCESSED",
        "data": {
          "order_id": "v1-240909084141-aa-O2oJwd",
          "merchant_id": "109500",
          "order_amount": { "value": 200, "currency": "INR" },
          "purchase_details": {
            "customer": {
              "mobile_number": "9876543210",
              "country_code": "91",
              "email_id": "..."
            }
          },
          "payments": [...]
        }
      }
    """
    body: bytes = await request.body()

    # Pine Labs webhook-signature verification (from official docs):
    # Header: "webhook-signature" — value is "v1,<base64_hmac>"
    # Signs: "{webhook_id}.{webhook_timestamp}.{raw_body}"
    # Secret: Base64-decode the secret from Pine Labs dashboard before use
    sig_header = request.headers.get("webhook-signature")
    webhook_id = request.headers.get("webhook-id", "")
    webhook_ts = request.headers.get("webhook-timestamp", "")
    if settings.PLINK_WEBHOOK_SECRET and sig_header:
        import base64 as _b64
        try:
            secret_bytes = _b64.b64decode(settings.PLINK_WEBHOOK_SECRET)
        except Exception:
            secret_bytes = settings.PLINK_WEBHOOK_SECRET.encode()
        signed_content = f"{webhook_id}.{webhook_ts}.{body.decode('utf-8')}".encode()
        expected = _b64.b64encode(
            hmac.new(secret_bytes, signed_content, hashlib.sha256).digest()
        ).decode()
        provided = sig_header.removeprefix("v1,")
        if not hmac.compare_digest(expected, provided):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Invalid Pine Labs signature")

    try:
        raw = _json.loads(body)
    except _json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"Invalid JSON: {exc}")

    # Only process completed orders
    if raw.get("event_type") != "ORDER_PROCESSED":
        return {"status": "ok", "note": f"event {raw.get('event_type')!r} not handled"}

    try:
        data = raw["data"]
        customer = data["purchase_details"]["customer"]
        country_code: str = customer.get("country_code", "91")
        mobile_number: str = customer.get("mobile_number", "")
        # Build full number; normalize_phone handles +91/91/0 prefixes
        raw_phone = f"+{country_code}{mobile_number}" if mobile_number else ""
        amount: float = float(data["order_amount"]["value"])  # already in INR
        currency: str = data["order_amount"].get("currency", "INR")
        merchant_id: str = str(data.get("merchant_id", "pinelabs_unknown"))
        order_id: str = data.get("order_id", "")
    except (KeyError, TypeError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"Missing fields: {exc}")

    if not mobile_number:
        # Card payments may not carry mobile number — skip gracefully
        return {"status": "ok", "note": "no customer mobile, ad skipped"}

    try:
        phone = normalize_phone(raw_phone)
    except ValueError:
        return {"status": "ok", "note": "invalid phone, ad skipped"}

    event = PaymentEvent(
        order_id=order_id,
        phone=phone,
        amount=amount,
        currency=currency,
        items=[],
        merchant_id=merchant_id,
        timestamp=int(time.time()),
    )

    _stats.webhooks_received += 1
    logger.info("Pine Labs webhook received",
                extra={"order_id": order_id, "merchant_id": merchant_id,
                       "phone_last4": phone[-4:], "amount": amount})

    background_tasks.add_task(_deliver_ad, event)
    return {"status": "ok"}


