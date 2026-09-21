"""
DSP bid handler — receives OpenRTB 2.5 bid requests from InMobi.

InMobi is the sell-side (SSP); Plink is the buy-side (DSP).
InMobi sends us a bid request when a Glance user hits the lock screen.
We look up whether that user just made a payment and, if so, bid to show them
a contextual post-payment ad.

Identity resolution order:
  1. user.ext.eids — look for 64-char hex strings (sha256 phone hashes)
  2. user.buyeruid — raw phone or hash from prior cookie sync
  3. user.id       — same treatment as buyeruid
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

from store import PaymentContext, payment_store, phone_hash

logger = logging.getLogger(__name__)

# Base CPM in USD; we always outbid the floor by $0.01
_BASE_CPM_USD = 5.00

# Context-aware headlines keyed by IAB category
_HEADLINES: dict[str, str] = {
    "IAB8-5":  "Hungry again? Here's what's near you.",
    "IAB18":   "Complete your look — trending styles await.",
    "IAB19":   "Upgrade your tech — exclusive deals inside.",
    "IAB7":    "Feel your best — health picks curated for you.",
    "IAB10":   "Make your space shine — home essentials on sale.",
    "IAB17":   "Keep the momentum — gear up for your next session.",
    "IAB5":    "Keep learning — handpicked reads & courses.",
    "IAB2":    "Drive smarter — accessories and upgrades for your ride.",
    "IAB20":   "Your next trip starts here — great deals ahead.",
    "IAB13":   "Grow your money — offers tailored after your purchase.",
    "IAB1":    "Entertainment your way — deals on games, movies & more.",
    "IAB6":    "Little ones deserve the best — top picks for kids.",
}
_DEFAULT_HEADLINE = "Great finds — just for you, right after your purchase."


def _make_html_creative(ctx: PaymentContext) -> str:
    """Return a minimal full-screen HTML5 banner (1080x1920)."""
    headline = _HEADLINES.get(ctx.iab_category, _DEFAULT_HEADLINE)
    return (
        "<!DOCTYPE html><html><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<style>"
        "body{margin:0;padding:0;background:#1a1a2e;display:flex;flex-direction:column;"
        "align-items:center;justify-content:center;height:100vh;font-family:'Segoe UI',sans-serif;}"
        ".card{background:linear-gradient(135deg,#16213e,#0f3460);"
        "border-radius:24px;padding:48px 32px;text-align:center;max-width:900px;"
        "box-shadow:0 8px 32px rgba(0,0,0,0.4);}"
        ".brand{color:#e94560;font-size:52px;font-weight:800;letter-spacing:2px;margin-bottom:16px;}"
        ".headline{color:#ffffff;font-size:56px;font-weight:600;line-height:1.3;margin-bottom:32px;}"
        ".sub{color:#a8b2d8;font-size:36px;margin-bottom:40px;}"
        ".cta{background:#e94560;color:#fff;border:none;border-radius:50px;"
        "padding:24px 64px;font-size:40px;font-weight:700;cursor:pointer;}"
        "</style>"
        "</head><body><div class='card'>"
        f"<div class='brand'>Plink</div>"
        f"<div class='headline'>{headline}</div>"
        f"<div class='sub'>Exclusive offer — just for you</div>"
        "<button class='cta'>Explore Now</button>"
        "</div></body></html>"
    )


def _make_native_adm(ctx: PaymentContext) -> str:
    """Return a native ad markup JSON string (OpenRTB Native 1.2)."""
    headline = _HEADLINES.get(ctx.iab_category, _DEFAULT_HEADLINE)
    native_obj = {
        "ver": "1.2",
        "assets": [
            {"id": 1, "required": 1, "title": {"text": headline}},
            {
                "id": 2,
                "required": 1,
                "img": {
                    "type": 3,
                    "url": "https://getplink.in/static/native-main.jpg",
                    "w": 1200,
                    "h": 628,
                },
            },
            {"id": 3, "required": 0, "data": {"type": 2, "value": "Exclusive offer after your purchase"}},
            {"id": 4, "required": 1, "data": {"type": 12, "value": "Plink"}},
        ],
        "link": {"url": "https://getplink.in"},
        "eventtrackers": [],
    }
    return json.dumps({"native": native_obj})


def _resolve_identity(bid_req: dict[str, Any]) -> PaymentContext | None:
    """Walk identity signals in the bid request and return matching PaymentContext."""
    user: dict[str, Any] = bid_req.get("user") or {}

    # 1. user.ext.eids — look for 64-char hex strings (sha256 phone hashes)
    eids: list[dict[str, Any]] = (user.get("ext") or {}).get("eids") or []
    for eid in eids:
        for uid_obj in eid.get("uids") or []:
            uid_val: str = uid_obj.get("id") or ""
            if len(uid_val) == 64 and all(c in "0123456789abcdef" for c in uid_val.lower()):
                ctx = payment_store.get(uid_val.lower())
                if ctx is not None:
                    logger.debug("Identity matched via eids", extra={"hash_prefix": uid_val[:8]})
                    return ctx

    # 2. user.buyeruid — may be raw phone or a hash
    for field_val in (user.get("buyeruid") or "", user.get("id") or ""):
        if not field_val:
            continue
        # Try as a direct phone hash (64-char hex)
        if len(field_val) == 64 and all(c in "0123456789abcdef" for c in field_val.lower()):
            ctx = payment_store.get(field_val.lower())
            if ctx is not None:
                return ctx
        # Try as a raw phone number — normalize and hash
        try:
            from inmobi import normalize_phone
            normalized = normalize_phone(field_val)
            key = phone_hash(normalized)
            ctx = payment_store.get(key)
            if ctx is not None:
                logger.debug("Identity matched via raw phone in buyeruid/id")
                return ctx
        except (ValueError, ImportError):
            pass

    return None


def _pick_imp(bid_req: dict[str, Any]) -> dict[str, Any] | None:
    """Return the preferred imp object: banner > native > video > first."""
    imps: list[dict[str, Any]] = bid_req.get("imp") or []
    if not imps:
        return None
    for imp in imps:
        if imp.get("banner"):
            return imp
    for imp in imps:
        if imp.get("native"):
            return imp
    for imp in imps:
        if imp.get("video"):
            return imp
    return imps[0]


def handle_bid_request(bid_req: dict[str, Any]) -> dict[str, Any] | None:
    """
    Core DSP logic.

    Returns an OpenRTB 2.5 BidResponse dict if we decide to bid,
    or None if we pass (caller sends HTTP 204).
    """
    request_id: str = bid_req.get("id") or str(uuid.uuid4())

    ctx = _resolve_identity(bid_req)
    if ctx is None:
        logger.info(
            "DSP: no matching payment context — passing on impression",
            extra={"request_id": request_id},
        )
        return None

    imp = _pick_imp(bid_req)
    if imp is None:
        logger.warning(
            "DSP: bid request has no imp objects — passing",
            extra={"request_id": request_id},
        )
        return None

    imp_id: str = imp.get("id") or "1"
    bid_floor: float = float(imp.get("bidfloor") or 0.0)

    # Always beat the floor by $0.01; start at $5.00 CPM
    price: float = max(_BASE_CPM_USD, bid_floor + 0.01)

    # Choose creative format based on imp type
    if imp.get("native"):
        adm = _make_native_adm(ctx)
        w, h = 0, 0  # native has no fixed dimensions
    else:
        adm = _make_html_creative(ctx)
        w, h = 1080, 1920

    crid = f"plink-{ctx.iab_category}-{ctx.order_id[:8]}"

    bid_response: dict[str, Any] = {
        "id": request_id,
        "seatbid": [
            {
                "bid": [
                    {
                        "id": str(uuid.uuid4()),
                        "impid": imp_id,
                        "price": price,
                        "adid": "plink-post-payment",
                        "adm": adm,
                        "adomain": ["getplink.in"],
                        "crid": crid,
                        "w": w,
                        "h": h,
                    }
                ],
                "seat": "plink",
            }
        ],
        "cur": "USD",
    }

    # Drop w/h for native (0,0 is misleading)
    if imp.get("native"):
        del bid_response["seatbid"][0]["bid"][0]["w"]
        del bid_response["seatbid"][0]["bid"][0]["h"]

    logger.info(
        "DSP: bidding on impression",
        extra={
            "request_id": request_id,
            "imp_id": imp_id,
            "price_usd": price,
            "iab_category": ctx.iab_category,
            "order_id": ctx.order_id,
            "phone_last4": ctx.phone_last4,
            "crid": crid,
        },
    )

    return bid_response
