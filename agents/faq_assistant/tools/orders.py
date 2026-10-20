"""Read-only mock order database for the FAQ Assistant agent.

The scoping agreement puts live order changes (cancellation, payment) out of
scope but allows *read-only mock database checks*. This module is the single
source of truth for order-status questions, so answers are deterministic and
can be graded with exact matching in the evaluation pipeline.

Order IDs (format ``######``) are NOT treated as PII by product decision,
so Model Armor intentionally leaves them unredacted.

To move to a real backend later (e.g. a read-only Cloud SQL ``orders`` table),
only ``lookup_order`` needs to change; the tool contract stays the same.
"""

from __future__ import annotations

import copy
import re
from typing import Any

# Canonical order ID format: "#" followed by exactly 5 digits.
ORDER_ID_REGEX = re.compile(r"^#\d{5}$")

# Accepts common user spellings: "#10234", "ORD-10234", "ord 10234", "ORD10234", "10234".
_ORDER_ID_INPUT_REGEX = re.compile(r"^(?:ORD[\s\-_]?|#)?(\d{5})$", re.IGNORECASE)

MAX_ORDER_ID_INPUT_LENGTH = 32

VALID_STATUSES = frozenset({
    "PROCESSING",
    "SHIPPED",
    "IN_TRANSIT",
    "OUT_FOR_DELIVERY",
    "DELIVERED",
    "DELAYED",
    "RETURN_IN_PROGRESS",
    "RETURNED",
    "CANCELLED",
})

# Only non-sensitive fulfilment fields are stored/returned (data minimisation):
# no customer name, address, email, or payment details.
MOCK_ORDERS: dict[str, dict[str, Any]] = {
    # --- PROCESSING -------------------------------------------------------
    "#10001": {
        "status": "PROCESSING", "items": ["Wireless Earbuds Pro"],
        "order_date": "2026-10-16", "carrier": None, "tracking_number": None,
        "shipped_date": None, "estimated_delivery": "2026-10-22", "delivered_date": None,
        "status_detail": "Payment confirmed; order is being picked at the warehouse.",
        "last_update": "2026-10-17T09:12:00Z",
    },
    "#10002": {
        "status": "PROCESSING", "items": ["Standing Desk Frame", "Desk Top 60in"],
        "order_date": "2026-10-15", "carrier": None, "tracking_number": None,
        "shipped_date": None, "estimated_delivery": "2026-10-24", "delivered_date": None,
        "status_detail": "Large item; awaiting freight carrier pickup scheduling.",
        "last_update": "2026-10-17T14:40:00Z",
    },
    "#10003": {
        "status": "PROCESSING", "items": ["Gift Card $50"],
        "order_date": "2026-10-17", "carrier": None, "tracking_number": None,
        "shipped_date": None, "estimated_delivery": "2026-10-21", "delivered_date": None,
        "status_detail": "Physical gift card queued for printing.",
        "last_update": "2026-10-17T18:05:00Z",
    },
    "#10004": {
        "status": "PROCESSING", "items": ["Mechanical Gaming Keyboard", "Custom Keycap Set"],
        "order_date": "2026-10-17", "carrier": None, "tracking_number": None,
        "shipped_date": None, "estimated_delivery": "2026-10-23", "delivered_date": None,
        "status_detail": "Order received; keycap customization in progress.",
        "last_update": "2026-10-18T08:15:00Z",
    },
    "#10005": {
        "status": "PROCESSING", "items": ["Ergonomic Vertical Mouse"],
        "order_date": "2026-10-18", "carrier": None, "tracking_number": None,
        "shipped_date": None, "estimated_delivery": "2026-10-22", "delivered_date": None,
        "status_detail": "Payment processed; awaiting fulfillment queue.",
        "last_update": "2026-10-18T10:30:00Z",
    },

    # --- SHIPPED ----------------------------------------------------------
    "#10101": {
        "status": "SHIPPED", "items": ["Running Shoes Size 10"],
        "order_date": "2026-10-12", "carrier": "UPS", "tracking_number": "1Z999AA10123456784",
        "shipped_date": "2026-10-14", "estimated_delivery": "2026-10-19", "delivered_date": None,
        "status_detail": "Package handed to carrier.",
        "last_update": "2026-10-14T16:22:00Z",
    },
    "#10102": {
        "status": "SHIPPED", "items": ["Smart Watch Series 5", "Sport Band"],
        "order_date": "2026-10-13", "carrier": "FedEx", "tracking_number": "7720 1234 5678",
        "shipped_date": "2026-10-15", "estimated_delivery": "2026-10-20", "delivered_date": None,
        "status_detail": "Package handed to carrier.",
        "last_update": "2026-10-15T11:03:00Z",
    },
    "#10103": {
        "status": "SHIPPED", "items": ["Paperback: The Pragmatic Programmer"],
        "order_date": "2026-10-14", "carrier": "USPS", "tracking_number": "9400 1000 0000 0000 0001",
        "shipped_date": "2026-10-16", "estimated_delivery": "2026-10-21", "delivered_date": None,
        "status_detail": "Package handed to carrier.",
        "last_update": "2026-10-16T08:47:00Z",
    },
    "#10104": {
        "status": "SHIPPED", "items": ["USB-C Docking Station 11-in-1"],
        "order_date": "2026-10-15", "carrier": "UPS", "tracking_number": "1Z999AA10123456880",
        "shipped_date": "2026-10-17", "estimated_delivery": "2026-10-21", "delivered_date": None,
        "status_detail": "Package departed origin facility.",
        "last_update": "2026-10-17T21:10:00Z",
    },
    "#10105": {
        "status": "SHIPPED", "items": ["Stainless Steel Water Bottle 32oz"],
        "order_date": "2026-10-16", "carrier": "USPS", "tracking_number": "9400 1000 0000 0000 0010",
        "shipped_date": "2026-10-17", "estimated_delivery": "2026-10-22", "delivered_date": None,
        "status_detail": "Accepted at USPS regional origin facility.",
        "last_update": "2026-10-17T19:00:00Z",
    },

    # --- IN_TRANSIT -------------------------------------------------------
    "#10201": {
        "status": "IN_TRANSIT", "items": ["4K Monitor 27in"],
        "order_date": "2026-10-10", "carrier": "UPS", "tracking_number": "1Z999AA10123456791",
        "shipped_date": "2026-10-12", "estimated_delivery": "2026-10-19", "delivered_date": None,
        "status_detail": "Arrived at regional sorting facility in Columbus, OH.",
        "last_update": "2026-10-17T22:15:00Z",
    },
    "#10202": {
        "status": "IN_TRANSIT", "items": ["Espresso Machine"],
        "order_date": "2026-10-09", "carrier": "FedEx", "tracking_number": "7720 2234 5678",
        "shipped_date": "2026-10-11", "estimated_delivery": "2026-10-18", "delivered_date": None,
        "status_detail": "In transit to destination facility.",
        "last_update": "2026-10-17T19:30:00Z",
    },
    "#10203": {
        "status": "IN_TRANSIT", "items": ["Yoga Mat", "Resistance Bands Set"],
        "order_date": "2026-10-11", "carrier": "USPS", "tracking_number": "9400 1000 0000 0000 0002",
        "shipped_date": "2026-10-13", "estimated_delivery": "2026-10-20", "delivered_date": None,
        "status_detail": "Departed USPS facility in Memphis, TN.",
        "last_update": "2026-10-17T05:58:00Z",
    },
    "#10204": {
        "status": "IN_TRANSIT", "items": ["Bluetooth Speaker"],
        "order_date": "2026-10-08", "carrier": "DHL", "tracking_number": "JD014600006281234567",
        "shipped_date": "2026-10-10", "estimated_delivery": "2026-10-23", "delivered_date": None,
        "status_detail": "International shipment cleared customs in Toronto, Canada.",
        "last_update": "2026-10-17T13:20:00Z",
    },
    "#10205": {
        "status": "IN_TRANSIT", "items": ["Noise Isolating Earplugs", "Sleep Mask"],
        "order_date": "2026-10-11", "carrier": "UPS", "tracking_number": "1Z999AA10123456891",
        "shipped_date": "2026-10-13", "estimated_delivery": "2026-10-19", "delivered_date": None,
        "status_detail": "In transit; scan in Louisville, KY.",
        "last_update": "2026-10-18T02:40:00Z",
    },
    "#10206": {
        "status": "IN_TRANSIT", "items": ["Smart LED Light Strip 10m"],
        "order_date": "2026-10-12", "carrier": "FedEx", "tracking_number": "7720 8234 5678",
        "shipped_date": "2026-10-14", "estimated_delivery": "2026-10-20", "delivered_date": None,
        "status_detail": "Arrived at destination sorting hub.",
        "last_update": "2026-10-18T05:12:00Z",
    },

    # --- OUT_FOR_DELIVERY -------------------------------------------------
    "#10301": {
        "status": "OUT_FOR_DELIVERY", "items": ["Noise-Cancelling Headphones"],
        "order_date": "2026-10-13", "carrier": "UPS", "tracking_number": "1Z999AA10123456805",
        "shipped_date": "2026-10-15", "estimated_delivery": "2026-10-18", "delivered_date": None,
        "status_detail": "Out for delivery today; expected by 8:00 PM local time.",
        "last_update": "2026-10-18T07:45:00Z",
    },
    "#10302": {
        "status": "OUT_FOR_DELIVERY", "items": ["Phone Case", "Screen Protector"],
        "order_date": "2026-10-14", "carrier": "USPS", "tracking_number": "9400 1000 0000 0000 0003",
        "shipped_date": "2026-10-16", "estimated_delivery": "2026-10-18", "delivered_date": None,
        "status_detail": "Out for delivery today.",
        "last_update": "2026-10-18T06:10:00Z",
    },
    "#10303": {
        "status": "OUT_FOR_DELIVERY", "items": ["High-Speed HDMI 2.1 Cable 6ft"],
        "order_date": "2026-10-15", "carrier": "FedEx", "tracking_number": "7720 9234 5678",
        "shipped_date": "2026-10-16", "estimated_delivery": "2026-10-18", "delivered_date": None,
        "status_detail": "On FedEx vehicle for delivery.",
        "last_update": "2026-10-18T08:00:00Z",
    },
    "#10304": {
        "status": "OUT_FOR_DELIVERY", "items": ["Wireless Charging Pad"],
        "order_date": "2026-10-14", "carrier": "UPS", "tracking_number": "1Z999AA10123456900",
        "shipped_date": "2026-10-16", "estimated_delivery": "2026-10-18", "delivered_date": None,
        "status_detail": "Loaded on delivery truck in local area.",
        "last_update": "2026-10-18T07:15:00Z",
    },

    # --- DELIVERED --------------------------------------------------------
    "#10401": {
        "status": "DELIVERED", "items": ["Laptop 14in", "Laptop Sleeve"],
        "order_date": "2026-10-01", "carrier": "FedEx", "tracking_number": "7720 3234 5678",
        "shipped_date": "2026-10-03", "estimated_delivery": "2026-10-07", "delivered_date": "2026-10-06",
        "status_detail": "Delivered; left at front door.",
        "last_update": "2026-10-06T15:32:00Z",
    },
    "#10402": {
        "status": "DELIVERED", "items": ["Coffee Beans 2lb"],
        "order_date": "2026-10-05", "carrier": "USPS", "tracking_number": "9400 1000 0000 0000 0004",
        "shipped_date": "2026-10-06", "estimated_delivery": "2026-10-09", "delivered_date": "2026-10-09",
        "status_detail": "Delivered to mailbox.",
        "last_update": "2026-10-09T12:01:00Z",
    },
    "#10403": {
        "status": "DELIVERED", "items": ["Air Fryer 5qt"],
        "order_date": "2026-09-28", "carrier": "UPS", "tracking_number": "1Z999AA10123456812",
        "shipped_date": "2026-09-30", "estimated_delivery": "2026-10-03", "delivered_date": "2026-10-02",
        "status_detail": "Delivered; signed for by resident.",
        "last_update": "2026-10-02T17:44:00Z",
    },
    "#10404": {
        "status": "DELIVERED", "items": ["Tablet 11in"],
        "order_date": "2026-09-20", "carrier": "FedEx", "tracking_number": "7720 4234 5678",
        "shipped_date": "2026-09-22", "estimated_delivery": "2026-09-26", "delivered_date": "2026-09-25",
        "status_detail": "Delivered; handed to building front desk.",
        "last_update": "2026-09-25T10:18:00Z",
    },
    "#10405": {
        "status": "DELIVERED", "items": ["Winter Jacket Size M"],
        "order_date": "2026-10-02", "carrier": "USPS", "tracking_number": "9400 1000 0000 0000 0005",
        "shipped_date": "2026-10-03", "estimated_delivery": "2026-10-08", "delivered_date": "2026-10-07",
        "status_detail": "Delivered to parcel locker.",
        "last_update": "2026-10-07T09:55:00Z",
    },
    "#10406": {
        "status": "DELIVERED", "items": ["Wireless Trackball Mouse"],
        "order_date": "2026-10-04", "carrier": "UPS", "tracking_number": "1Z999AA10123456910",
        "shipped_date": "2026-10-05", "estimated_delivery": "2026-10-09", "delivered_date": "2026-10-08",
        "status_detail": "Delivered to porch.",
        "last_update": "2026-10-08T14:20:00Z",
    },
    "#10407": {
        "status": "DELIVERED", "items": ["Insulated Travel Mug 20oz"],
        "order_date": "2026-10-06", "carrier": "USPS", "tracking_number": "9400 1000 0000 0000 0011",
        "shipped_date": "2026-10-07", "estimated_delivery": "2026-10-11", "delivered_date": "2026-10-10",
        "status_detail": "Delivered to mailroom.",
        "last_update": "2026-10-10T11:45:00Z",
    },

    # --- DELAYED ----------------------------------------------------------
    "#10501": {
        "status": "DELAYED", "items": ["Gaming Console"],
        "order_date": "2026-10-06", "carrier": "UPS", "tracking_number": "1Z999AA10123456829",
        "shipped_date": "2026-10-08", "estimated_delivery": "2026-10-21", "delivered_date": None,
        "status_detail": "Weather delay at carrier hub; original estimate was 2026-10-13.",
        "last_update": "2026-10-16T20:02:00Z",
    },
    "#10502": {
        "status": "DELAYED", "items": ["Office Chair"],
        "order_date": "2026-10-04", "carrier": "FedEx", "tracking_number": "7720 5234 5678",
        "shipped_date": "2026-10-07", "estimated_delivery": "2026-10-22", "delivered_date": None,
        "status_detail": "Address verification required by carrier; original estimate was 2026-10-12.",
        "last_update": "2026-10-15T14:27:00Z",
    },
    "#10503": {
        "status": "DELAYED", "items": ["Backordered: Camera Lens 50mm"],
        "order_date": "2026-09-30", "carrier": None, "tracking_number": None,
        "shipped_date": None, "estimated_delivery": "2026-11-05", "delivered_date": None,
        "status_detail": "Item is backordered from the supplier; ships when restocked.",
        "last_update": "2026-10-14T09:00:00Z",
    },
    "#10504": {
        "status": "DELAYED", "items": ["Curved Gaming Monitor 34in"],
        "order_date": "2026-10-05", "carrier": "UPS", "tracking_number": "1Z999AA10123456925",
        "shipped_date": "2026-10-07", "estimated_delivery": "2026-10-23", "delivered_date": None,
        "status_detail": "Train derailment along carrier line caused transit delay.",
        "last_update": "2026-10-17T11:15:00Z",
    },
    "#10505": {
        "status": "DELAYED", "items": ["Mechanical Keyboard Switches (110x)"],
        "order_date": "2026-10-07", "carrier": "USPS", "tracking_number": "9400 1000 0000 0000 0012",
        "shipped_date": "2026-10-09", "estimated_delivery": "2026-10-22", "delivered_date": None,
        "status_detail": "Misrouted by USPS to wrong distribution center; rerouted.",
        "last_update": "2026-10-16T18:00:00Z",
    },

    # --- RETURN_IN_PROGRESS / RETURNED -------------------------------------
    "#10601": {
        "status": "RETURN_IN_PROGRESS", "items": ["Blender"],
        "order_date": "2026-09-15", "carrier": "UPS", "tracking_number": "1Z999AA10123456836",
        "shipped_date": "2026-09-17", "estimated_delivery": "2026-09-21", "delivered_date": "2026-09-20",
        "status_detail": "Return label issued 2026-10-10; awaiting drop-off at carrier.",
        "last_update": "2026-10-10T11:11:00Z",
    },
    "#10602": {
        "status": "RETURN_IN_PROGRESS", "items": ["Sneakers Size 9"],
        "order_date": "2026-09-25", "carrier": "USPS", "tracking_number": "9400 1000 0000 0000 0006",
        "shipped_date": "2026-09-26", "estimated_delivery": "2026-09-30", "delivered_date": "2026-09-29",
        "status_detail": "Return received at warehouse 2026-10-15; refund processing (3-5 business days).",
        "last_update": "2026-10-15T16:40:00Z",
    },
    "#10603": {
        "status": "RETURNED", "items": ["Electric Toothbrush"],
        "order_date": "2026-09-01", "carrier": "FedEx", "tracking_number": "7720 6234 5678",
        "shipped_date": "2026-09-03", "estimated_delivery": "2026-09-06", "delivered_date": "2026-09-05",
        "status_detail": "Return completed; refund issued to original payment method on 2026-09-20.",
        "last_update": "2026-09-20T10:00:00Z",
    },
    "#10604": {
        "status": "RETURNED", "items": ["Bluetooth Headphones (Over-Ear)"],
        "order_date": "2026-08-20", "carrier": "UPS", "tracking_number": "1Z999AA10123456930",
        "shipped_date": "2026-08-22", "estimated_delivery": "2026-08-26", "delivered_date": "2026-08-25",
        "status_detail": "Returned due to size preference; full refund posted 2026-09-05.",
        "last_update": "2026-09-05T14:10:00Z",
    },

    # --- CANCELLED --------------------------------------------------------
    "#10701": {
        "status": "CANCELLED", "items": ["Smart Home Hub"],
        "order_date": "2026-10-03", "carrier": None, "tracking_number": None,
        "shipped_date": None, "estimated_delivery": None, "delivered_date": None,
        "status_detail": "Cancelled at customer request before shipment; refund issued 2026-10-04.",
        "last_update": "2026-10-04T08:30:00Z",
    },
    "#10702": {
        "status": "CANCELLED", "items": ["Limited Edition Vinyl"],
        "order_date": "2026-10-07", "carrier": None, "tracking_number": None,
        "shipped_date": None, "estimated_delivery": None, "delivered_date": None,
        "status_detail": "Cancelled: item out of stock; refund issued 2026-10-08.",
        "last_update": "2026-10-08T12:00:00Z",
    },
    "#10703": {
        "status": "CANCELLED", "items": ["Portable Power Bank 20000mAh"],
        "order_date": "2026-10-10", "carrier": None, "tracking_number": None,
        "shipped_date": None, "estimated_delivery": None, "delivered_date": None,
        "status_detail": "Cancelled due to duplicate order submission; refund issued 2026-10-11.",
        "last_update": "2026-10-11T09:45:00Z",
    },

    # --- Multi-shipment / edge cases -------------------------------------
    "#10801": {
        "status": "SHIPPED", "items": ["Kitchen Knife Set", "Cutting Board"],
        "order_date": "2026-10-12", "carrier": "UPS", "tracking_number": "1Z999AA10123456843",
        "shipped_date": "2026-10-14", "estimated_delivery": "2026-10-19", "delivered_date": None,
        "status_detail": "Partial shipment: Kitchen Knife Set shipped; Cutting Board ships separately by 2026-10-21.",
        "last_update": "2026-10-14T13:13:00Z",
    },
    "#10802": {
        "status": "DELIVERED", "items": ["Monitor Arm"],
        "order_date": "2026-10-08", "carrier": "FedEx", "tracking_number": "7720 7234 5678",
        "shipped_date": "2026-10-09", "estimated_delivery": "2026-10-13", "delivered_date": "2026-10-13",
        "status_detail": "Carrier reports delivered; a carrier delivery investigation was opened on 2026-10-15.",
        "last_update": "2026-10-15T09:20:00Z",
    },
    "#10803": {
        "status": "IN_TRANSIT", "items": ["Replacement Charger (warranty)"],
        "order_date": "2026-10-14", "carrier": "USPS", "tracking_number": "9400 1000 0000 0000 0007",
        "shipped_date": "2026-10-15", "estimated_delivery": "2026-10-20", "delivered_date": None,
        "status_detail": "Free warranty replacement shipment.",
        "last_update": "2026-10-17T03:33:00Z",
    },
    "#10804": {
        "status": "SHIPPED", "items": ["Smart Thermostat", "Temperature Sensor 3-Pack"],
        "order_date": "2026-10-13", "carrier": "UPS", "tracking_number": "1Z999AA10123456950",
        "shipped_date": "2026-10-15", "estimated_delivery": "2026-10-20", "delivered_date": None,
        "status_detail": "Split order: Thermostat shipped via UPS; sensors shipping separately via USPS.",
        "last_update": "2026-10-15T17:00:00Z",
    },
}


def normalize_order_id(raw_order_id: Any) -> str | None:
    """Normalises user input to canonical ``######`` form, or returns None if invalid."""
    if not isinstance(raw_order_id, str):
        return None
    candidate = raw_order_id.strip()
    if not candidate or len(candidate) > MAX_ORDER_ID_INPUT_LENGTH:
        return None
    match = _ORDER_ID_INPUT_REGEX.fullmatch(candidate)
    if not match:
        return None
    normalized = f"#{match.group(1)}"
    return normalized if ORDER_ID_REGEX.fullmatch(normalized) else None


def lookup_order(order_id: str) -> dict[str, Any] | None:
    """Returns a defensive copy of the order record, or None if not found.

    ``order_id`` must already be normalised via ``normalize_order_id``.
    """
    record = MOCK_ORDERS.get(order_id)
    if record is None:
        return None
    result = copy.deepcopy(record)
    result["order_id"] = order_id
    return result
