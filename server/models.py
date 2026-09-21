from __future__ import annotations

import re
import time
from typing import Any

from pydantic import BaseModel, field_validator


# ---------------------------------------------------------------------------
# Webhook / payment models
# ---------------------------------------------------------------------------

class PaymentEvent(BaseModel):
    order_id: str
    phone: str
    amount: float
    currency: str = "INR"
    items: list[str] = []
    merchant_id: str
    timestamp: int

    @field_validator("phone", mode="before")
    @classmethod
    def normalize_phone_field(cls, v: str) -> str:
        from inmobi import normalize_phone  # local import to avoid circular
        return normalize_phone(v)

    @field_validator("currency", mode="before")
    @classmethod
    def uppercase_currency(cls, v: str) -> str:
        return v.upper()


# ---------------------------------------------------------------------------
# OpenRTB 2.5 models (minimal — only fields Plink actually sends)
# ---------------------------------------------------------------------------

class Banner(BaseModel):
    w: int = 1080
    h: int = 1920
    pos: int = 1                  # above the fold
    api: list[int] = [3, 5]       # MRAID 2 + MRAID 3 — enables HTML5 rich media creatives
    format: list[dict[str, int]] = [{"w": 1080, "h": 1920}]


class Video(BaseModel):
    mimes: list[str] = ["video/mp4", "video/webm"]
    minduration: int = 15
    maxduration: int = 30
    protocols: list[int] = [2, 3, 5, 6]  # VAST 2.0, 2.0 wrapper, VAST 3.0, 3.0 wrapper
    w: int = 1080
    h: int = 1920
    linearity: int = 1            # linear (not overlay)
    skip: int = 0                 # non-skippable
    pos: int = 1                  # above the fold
    api: list[int] = [1, 2]       # VPAID 1.0, VPAID 2.0
    playbackmethod: list[int] = [1]  # auto-play, sound on
    placement: int = 2            # in-banner (Glance lock screen context)
    delivery: list[int] = [1]     # streaming


class Native(BaseModel):
    # OpenRTB Native 1.2 — Glance content card layout
    # Assets: title (required), main image (required), description, sponsor label
    request: str = (
        '{"ver":"1.2","layout":6,"adunit":4,"assets":['
        '{"id":1,"required":1,"title":{"len":90}},'
        '{"id":2,"required":1,"img":{"type":3,"w":1200,"h":628,"wmin":300,"hmin":157}},'
        '{"id":3,"required":0,"data":{"type":2,"len":140}},'
        '{"id":4,"required":1,"data":{"type":12,"len":25}}'
        ']}'
    )
    ver: str = "1.2"
    api: list[int] = [1, 2, 3]


class Imp(BaseModel):
    id: str
    banner: Banner | None = None
    video: Video | None = None
    native: Native | None = None
    tagid: str = ""
    secure: int = 1
    bidfloorcur: str = "USD"
    bidfloor: float = 0.01
    nurl: str = ""   # win notice URL — InMobi fires this when impression is confirmed served


class Device(BaseModel):
    ua: str = "Mozilla/5.0 (Linux; Android 14; Glance) AppleWebKit/537.36"
    ip: str = ""
    devicetype: int = 4   # phone
    os: str = "android"
    make: str = ""
    model: str = ""
    connectiontype: int = 2
    js: int = 1
    language: str = "en"
    carrier: str = ""
    lmt: int = 0          # limit ad tracking; 0 = tracking allowed (Glance consent via OEM T&Cs)


class User(BaseModel):
    id: str = ""
    buyeruid: str = ""    # InMobi identity graph entry point (raw phone)
    data: list[dict[str, Any]] = []
    ext: dict[str, Any] = {}  # carries eids (ID5 hashed phone for identity resolution)


class Publisher(BaseModel):
    id: str = ""
    name: str = "Plink"
    domain: str = "getplink.in"


class App(BaseModel):
    # Glance is an Android app — use app object, not site
    id: str = "com.glance.internet"
    name: str = "Glance"
    bundle: str = "com.glance.internet"   # Android package name
    storeurl: str = "https://play.google.com/store/apps/details?id=com.glance.internet"
    domain: str = "getplink.in"
    publisher: Publisher = Publisher()
    cat: list[str] = []


class Regs(BaseModel):
    coppa: int = 0
    ext: dict[str, Any] = {"gdpr": 0}


class Source(BaseModel):
    fd: int = 0            # 0 = publisher is selling directly (no intermediary)
    ext: dict[str, Any] = {}


class InMobiBidRequest(BaseModel):
    id: str
    imp: list[Imp]
    device: Device = Device()
    user: User = User()
    app: App = App()       # Glance is in-app inventory; site object is for web
    regs: Regs = Regs()
    at: int = 1            # first-price auction
    tmax: int = 450        # ms — InMobi must respond within 500ms
    cur: list[str] = ["USD"]
    test: int = 0          # 1 = test request; InMobi won't bill the impression
    source: Source = Source()
    ext: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# OpenRTB 2.5 response models
# ---------------------------------------------------------------------------

class Bid(BaseModel):
    id: str = ""
    impid: str = ""
    price: float = 0.0
    adid: str = ""
    nurl: str = ""
    adm: str = ""
    adomain: list[str] = []
    crid: str = ""
    cid: str = ""
    w: int = 0
    h: int = 0
    ext: dict[str, Any] = {}


class SeatBid(BaseModel):
    bid: list[Bid] = []
    seat: str = ""
    group: int = 0


class InMobiBidResponse(BaseModel):
    id: str = ""
    seatbid: list[SeatBid] = []
    bidid: str = ""
    cur: str = "USD"
    nbr: int | None = None   # no-bid reason code
    ext: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Internal audit / logging record
# ---------------------------------------------------------------------------

class AdDeliveryRecord(BaseModel):
    request_id: str
    order_id: str
    merchant_id: str
    phone_last4: str        # last 4 digits only for privacy
    amount: float
    currency: str
    items: list[str]
    iab_category: str
    bid_request_id: str
    inmobi_endpoint: str
    http_status: int | None   # None if request failed before HTTP
    response_nbr: int | None  # no-bid reason, if present
    bid_count: int
    winning_price: float | None
    success: bool
    error: str | None
    created_at: int = 0

    def __init__(self, **data: Any) -> None:
        if "created_at" not in data or not data["created_at"]:
            data["created_at"] = int(time.time())
        super().__init__(**data)
