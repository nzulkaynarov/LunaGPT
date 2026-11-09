"""Manufacturer hints from the IEEE registry bundled with mac-vendor-lookup."""

import logging

from mac_vendor_lookup import AsyncMacLookup, InvalidMacError, VendorNotFoundError

logger = logging.getLogger("lunagpt.assistant.vendors")
_lookup = AsyncMacLookup()


async def vendor_of(mac: str) -> str | None:
    """A hint only: private addresses are not registered, and a failure must not break the menu."""
    try:
        return str(await _lookup.lookup(mac))
    except (VendorNotFoundError, InvalidMacError):
        return None
    except Exception:
        logger.warning("MAC vendor list is unavailable", exc_info=True)
        return None
