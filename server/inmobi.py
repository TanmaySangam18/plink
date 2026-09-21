"""
InMobi Buyer Hub API client (OpenRTB 2.5).

Key facts:
- InMobi resolves Indian phone numbers → Glance device IDs via user.buyeruid.
- Ad format: 1080x1920 full-screen banner (Glance lock screen).
- RTB timeout: 450ms (InMobi budget is 500ms; we leave 50ms headroom).
- Auth: Bearer token in Authorization header.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from typing import Any

import httpx

from config import settings
from models import (
    AdDeliveryRecord,
    App,
    Banner,
    Device,
    Imp,
    InMobiBidRequest,
    InMobiBidResponse,
    Native,
    PaymentEvent,
    Publisher,
    Regs,
    SeatBid,
    Source,
    User,
    Video,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# IAB category keyword map (lower-cased keywords → IAB tier-2 code)
# ---------------------------------------------------------------------------

_IAB_KEYWORD_MAP: list[tuple[list[str], str]] = [
    # Food & Drink
    (["pizza", "burger", "food", "restaurant", "meal", "dinner", "lunch", "breakfast",
      "coffee", "tea", "drink", "juice", "snack", "biryani", "dosa", "thali",
      "curry", "noodle", "sushi", "cake", "dessert", "sweet", "chocolate"], "IAB8-5"),
    # Fashion / Apparel
    (["shirt", "t-shirt", "dress", "jeans", "trouser", "pant", "skirt", "kurta",
      "saree", "lehenga", "suit", "jacket", "coat", "hoodie", "sweatshirt",
      "shoe", "sandal", "chappal", "slipper", "sneaker", "boot", "heel",
      "bag", "purse", "handbag", "wallet", "belt", "scarf", "cap", "hat"], "IAB18"),
    # Electronics / Tech
    (["phone", "mobile", "laptop", "tablet", "charger", "cable", "earphone",
      "headphone", "speaker", "keyboard", "mouse", "monitor", "printer",
      "camera", "tv", "television", "smartwatch", "watch", "gadget",
      "power bank", "router", "modem"], "IAB19"),
    # Health & Beauty
    (["medicine", "vitamin", "supplement", "cream", "lotion", "shampoo",
      "conditioner", "soap", "toothpaste", "perfume", "deodorant", "makeup",
      "lipstick", "foundation", "serum", "moisturizer", "sunscreen",
      "health", "wellness", "gym", "protein"], "IAB7"),
    # Home & Garden
    (["furniture", "chair", "table", "bed", "sofa", "couch", "shelf",
      "lamp", "curtain", "pillow", "mattress", "kitchen", "cookware",
      "utensil", "pan", "pot", "appliance", "blender", "mixer"], "IAB10"),
    # Sports & Outdoors
    (["cricket", "football", "soccer", "badminton", "tennis", "cycle",
      "bicycle", "treadmill", "dumbbell", "yoga", "sport", "outdoor",
      "camping", "hiking", "trek", "ball", "bat", "racket"], "IAB17"),
    # Books & Education
    (["book", "novel", "textbook", "notebook", "stationery", "pen", "pencil",
      "course", "tutorial", "class", "study", "education", "school"], "IAB5"),
    # Automotive
    (["car", "bike", "motorcycle", "scooter", "auto", "vehicle",
      "tyre", "tire", "oil", "spare", "helmet"], "IAB2"),
    # Travel
    (["ticket", "flight", "hotel", "bus", "train", "travel", "trip",
      "vacation", "tour", "holiday"], "IAB20"),
    # Finance
    (["insurance", "policy", "loan", "emi", "investment", "mutual fund",
      "stock", "gold", "silver", "banking", "credit"], "IAB13"),
    # Entertainment
    (["movie", "film", "game", "gaming", "toy", "puzzle", "board game",
      "music", "concert", "ticket", "streaming"], "IAB1"),
    # Baby & Kids
    (["baby", "infant", "diaper", "nappy", "kids", "child", "toy",
      "school bag", "uniform", "children"], "IAB6"),
]

_IAB_DEFAULT = "IAB24"  # Uncategorized


def normalize_phone(phone: str) -> str:
    """Normalize any Indian mobile number representation to +91XXXXXXXXXX.

    Handles:
      +919876543210   → +919876543210
      919876543210    → +919876543210
      09876543210     → +919876543210
      9876543210      → +919876543210
    """
    if not phone:
        raise ValueError("Phone number is empty")

    # Strip all non-digit characters except leading +
    digits_only = re.sub(r"[^\d]", "", phone)

    if len(digits_only) == 12 and digits_only.startswith("91"):
        # 919876543210
        local = digits_only[2:]
    elif len(digits_only) == 11 and digits_only.startswith("0"):
        # 09876543210
        local = digits_only[1:]
    elif len(digits_only) == 10:
        # 9876543210
        local = digits_only
    elif len(digits_only) == 13 and digits_only.startswith("091"):
        # edge case: 0919876543210
        local = digits_only[3:]
    else:
        raise ValueError(f"Cannot normalize phone number: {phone!r}")

    if not re.match(r"^[6-9]\d{9}$", local):
        raise ValueError(f"Not a valid Indian mobile number: {phone!r}")

    return f"+91{local}"


def infer_category(items: list[str]) -> str:
    """Map a list of product names to the best-fitting IAB category code."""
    if not items:
        return _IAB_DEFAULT

    combined = " ".join(items).lower()

    # Count keyword hits per category; pick highest score
    best_code = _IAB_DEFAULT
    best_score = 0

    for keywords, code in _IAB_KEYWORD_MAP:
        score = sum(1 for kw in keywords if kw in combined)
        if score > best_score:
            best_score = score
            best_code = code

    return best_code


def _make_request_id() -> str:
    return str(uuid.uuid4())


def _phone_sha256(phone: str) -> str:
    """SHA-256 hash of normalized phone number for ID5 identity resolution."""
    return hashlib.sha256(phone.encode("utf-8")).hexdigest()


def build_bid_request(event: PaymentEvent, test: bool = False) -> dict[str, Any]:
    """Build an OpenRTB 2.5 BidRequest dict for the given PaymentEvent.

    Sends three imp objects — banner, video, native — in a single request.
    InMobi returns the highest bid across all three formats.
    Set test=True to mark the request as a test impression (not billed).
    """
    request_id = _make_request_id()
    iab_cat = infer_category(event.items)
    pub_id = settings.INMOBI_PUBLISHER_ID
    placement_id = settings.INMOBI_PLACEMENT_ID or f"plink-lockscreen-{pub_id}"
    phone_hash = _phone_sha256(event.phone)

    win_base = (
        "https://plink-server-946m.onrender.com/win"
        f"?price=${{AUCTION_PRICE}}&rid={request_id}"
    )

    bid_request = InMobiBidRequest(
        id=request_id,
        imp=[
            Imp(
                id=f"{request_id}-b",
                banner=Banner(),
                tagid=str(placement_id),
                secure=1,
                bidfloor=0.005,
                bidfloorcur="USD",
                nurl=f"{win_base}&iid={request_id}-b",
            ),
            Imp(
                id=f"{request_id}-v",
                video=Video(),
                tagid=str(placement_id),
                secure=1,
                bidfloor=0.05,
                bidfloorcur="USD",
                nurl=f"{win_base}&iid={request_id}-v",
            ),
            Imp(
                id=f"{request_id}-n",
                native=Native(),
                tagid=str(placement_id),
                secure=1,
                bidfloor=0.02,
                bidfloorcur="USD",
                nurl=f"{win_base}&iid={request_id}-n",
            ),
        ],
        device=Device(
            ua="Mozilla/5.0 (Linux; Android 14; Glance/1.0) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
            devicetype=4,
            os="android",
            connectiontype=2,
            language="en",
            js=1,
            lmt=0,
        ),
        user=User(
            id=event.phone,
            buyeruid=event.phone,
            ext={
                # ID5 identity resolution: SHA-256 hashed phone number
                # InMobi resolves this against their identity graph to find the Glance device
                "eids": [{
                    "source": "id5-sync.com",
                    "uids": [{"id": phone_hash, "atype": 3}],
                }],
            },
        ),
        app=App(
            id=event.merchant_id,
            name="Glance",
            bundle="com.glance.internet",
            storeurl="https://play.google.com/store/apps/details?id=com.glance.internet",
            domain="plink-website.vercel.app",
            publisher=Publisher(
                id=pub_id,
                name="Plink",
                domain="plink-website.vercel.app",
            ),
            cat=[iab_cat],
        ),
        regs=Regs(coppa=0, ext={"gdpr": 0}),
        at=1,
        tmax=450,
        cur=["USD"],
        test=1 if test else 0,
        source=Source(
            fd=0,
            ext={
                "schain": {
                    "ver": "1.0",
                    "complete": 1,
                    "nodes": [{
                        "asi": "plink-website.vercel.app",
                        "sid": pub_id,
                        "hp": 1,
                    }],
                }
            },
        ),
        ext={
            "plink": {
                "order_id": event.order_id,
                "merchant_id": event.merchant_id,
                "amount_inr": event.amount,
                "iab_category": iab_cat,
            }
        },
    )

    return bid_request.model_dump(exclude_none=True)


async def send_bid(event: PaymentEvent) -> dict[str, Any] | None:
    """Send an OpenRTB 2.5 bid request to InMobi.

    Returns the parsed response dict, or None if the call failed or was skipped.
    Logs everything; never raises — errors are swallowed so the caller's
    background task never crashes the server.
    """
    phone_last4 = event.phone[-4:] if len(event.phone) >= 4 else event.phone
    bid_req = build_bid_request(event)
    request_id = bid_req["id"]

    record_base: dict[str, Any] = {
        "request_id": request_id,
        "order_id": event.order_id,
        "merchant_id": event.merchant_id,
        "phone_last4": phone_last4,
        "amount": event.amount,
        "currency": event.currency,
        "items": event.items,
        "iab_category": bid_req["app"]["cat"][0] if bid_req.get("app", {}).get("cat") else _IAB_DEFAULT,
        "bid_request_id": request_id,
        "inmobi_endpoint": settings.INMOBI_ENDPOINT,
        "http_status": None,
        "response_nbr": None,
        "bid_count": 0,
        "winning_price": None,
        "success": False,
        "error": None,
    }

    # ------------------------------------------------------------------
    # Sandbox mode: log instead of calling InMobi
    # ------------------------------------------------------------------
    if settings.sandbox_mode:
        logger.info(
            "SANDBOX MODE — InMobi bid request logged (not sent)",
            extra={
                "request_id": request_id,
                "order_id": event.order_id,
                "phone_last4": phone_last4,
                "bid_request": bid_req,
            },
        )
        record = AdDeliveryRecord(
            **{**record_base, "http_status": 0, "success": True, "error": "sandbox_mode"},
        )
        logger.debug("AdDeliveryRecord: %s", record.model_dump())
        return None

    # ------------------------------------------------------------------
    # Live call to InMobi Buyer Hub
    # ------------------------------------------------------------------
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-inmobi-publisher-id": settings.INMOBI_PUBLISHER_ID,
    }
    if settings.INMOBI_API_KEY:
        headers["Authorization"] = f"Bearer {settings.INMOBI_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=0.5) as client:  # 500ms hard limit
            logger.info(
                "Sending bid request to InMobi",
                extra={
                    "request_id": request_id,
                    "order_id": event.order_id,
                    "phone_last4": phone_last4,
                    "endpoint": settings.INMOBI_ENDPOINT,
                },
            )

            response = await client.post(
                settings.INMOBI_ENDPOINT,
                json=bid_req,
                headers=headers,
            )

            record_base["http_status"] = response.status_code

            # HTTP 204 = no bid
            if response.status_code == 204:
                logger.info(
                    "InMobi returned no-bid (204)",
                    extra={
                        "request_id": request_id,
                        "order_id": event.order_id,
                        "phone_last4": phone_last4,
                    },
                )
                record = AdDeliveryRecord(**{**record_base, "success": True})
                logger.debug("AdDeliveryRecord: %s", record.model_dump())
                return None

            if response.status_code != 200:
                err = f"InMobi returned HTTP {response.status_code}: {response.text[:256]}"
                logger.warning(
                    "InMobi non-200 response",
                    extra={
                        "request_id": request_id,
                        "order_id": event.order_id,
                        "http_status": response.status_code,
                        "body_preview": response.text[:256],
                    },
                )
                record = AdDeliveryRecord(**{**record_base, "error": err})
                logger.debug("AdDeliveryRecord: %s", record.model_dump())
                return None

            # Parse OpenRTB response
            raw = response.json()
            bid_resp = InMobiBidResponse.model_validate(raw)

            bid_count = sum(len(sb.bid) for sb in bid_resp.seatbid)
            winning_price: float | None = None
            if bid_resp.seatbid and bid_resp.seatbid[0].bid:
                winning_price = bid_resp.seatbid[0].bid[0].price

            record_base.update(
                {
                    "response_nbr": bid_resp.nbr,
                    "bid_count": bid_count,
                    "winning_price": winning_price,
                    "success": True,
                }
            )

            logger.info(
                "InMobi bid response received",
                extra={
                    "request_id": request_id,
                    "order_id": event.order_id,
                    "phone_last4": phone_last4,
                    "bid_count": bid_count,
                    "winning_price": winning_price,
                    "response_id": bid_resp.id,
                },
            )

            record = AdDeliveryRecord(**record_base)
            logger.debug("AdDeliveryRecord: %s", record.model_dump())
            return raw

    except httpx.TimeoutException as exc:
        err = f"InMobi request timed out: {exc}"
        logger.warning(
            "InMobi timeout",
            extra={
                "request_id": request_id,
                "order_id": event.order_id,
                "phone_last4": phone_last4,
                "error": str(exc),
            },
        )
        record = AdDeliveryRecord(**{**record_base, "error": err})
        logger.debug("AdDeliveryRecord: %s", record.model_dump())
        return None

    except httpx.RequestError as exc:
        err = f"InMobi request error: {exc}"
        logger.error(
            "InMobi request error",
            extra={
                "request_id": request_id,
                "order_id": event.order_id,
                "phone_last4": phone_last4,
                "error": str(exc),
            },
        )
        record = AdDeliveryRecord(**{**record_base, "error": err})
        logger.debug("AdDeliveryRecord: %s", record.model_dump())
        return None

    except Exception as exc:
        err = f"Unexpected error during InMobi call: {exc}"
        logger.exception(
            "Unexpected InMobi error",
            extra={
                "request_id": request_id,
                "order_id": event.order_id,
                "phone_last4": phone_last4,
            },
        )
        record = AdDeliveryRecord(**{**record_base, "error": err})
        logger.debug("AdDeliveryRecord: %s", record.model_dump())
        return None
