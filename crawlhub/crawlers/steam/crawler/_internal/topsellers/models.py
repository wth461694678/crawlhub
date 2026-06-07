"""
Steam Top Sellers - Data Models

Based on reverse engineering of:
  - IStoreTopSellersService/GetWeeklyTopSellers/v1
  - IStoreBrowseService/GetItems/v1
"""

from __future__ import annotations

import datetime
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ══════════════════════════════════════════════════════
#  Protobuf Wire Format Helpers
# ══════════════════════════════════════════════════════

def encode_varint(value: int) -> bytes:
    """Encode an unsigned integer as Protobuf varint."""
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a varint from data at position pos. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("Truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if (byte & 0x80) == 0:
            break
        shift += 7
    return result, pos


def encode_string(field_num: int, value: str) -> bytes:
    """Encode a string field as Protobuf."""
    tag = encode_varint((field_num << 3) | 2)
    encoded = value.encode("utf-8")
    length = encode_varint(len(encoded))
    return tag + length + encoded


def encode_uint32(field_num: int, value: int) -> bytes:
    """Encode a uint32 field as Protobuf varint."""
    tag = encode_varint((field_num << 3) | 0)
    return tag + encode_varint(value)


def encode_message(field_num: int, data: bytes) -> bytes:
    """Encode a nested message field."""
    tag = encode_varint((field_num << 3) | 2)
    length = encode_varint(len(data))
    return tag + length + data


def decode_protobuf(data: bytes) -> dict:
    """Decode Protobuf bytes into {field_num: (wire_type, value)} dict."""
    result = {}
    pos = 0
    while pos < len(data):
        tag, pos = decode_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:  # Varint
            value, pos = decode_varint(data, pos)
            result[field_num] = (wire_type, value)
        elif wire_type == 2:  # Length-delimited
            length, pos = decode_varint(data, pos)
            if pos + length > len(data):
                raise ValueError("Truncated length-delimited field")
            value = data[pos : pos + length]
            pos += length
            result[field_num] = (wire_type, value)
        elif wire_type == 5:  # 32-bit
            value = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            result[field_num] = (wire_type, value)
        elif wire_type == 1:  # 64-bit
            value = struct.unpack_from("<Q", data, pos)[0]
            pos += 8
            result[field_num] = (wire_type, value)
        else:
            raise ValueError(f"Unsupported wire type {wire_type}")
    return result


def decode_protobuf_fields(data: bytes) -> List[Tuple[int, int, bytes]]:
    """Decode Protobuf bytes into a list of (field_number, wire_type, value) tuples.

    Unlike decode_protobuf() (which is a dict and only keeps the last value per
    field), this function preserves ALL values — essential for repeated fields.

    For wire_type == 0 (varint):   value is int
    For wire_type == 2 (length-delimited): value is bytes
    For wire_type == 5 (fixed32):  value is int ( little-endian uint32)
    For wire_type == 1 (fixed64):  value is int (little-endian uint64)
    """
    fields: List[Tuple[int, int, bytes]] = []
    pos = 0
    while pos < len(data):
        tag, pos = decode_varint(data, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:  # varint
            value, pos = decode_varint(data, pos)
            fields.append((field_number, wire_type, value))
        elif wire_type == 2:  # length-delimited
            length, pos = decode_varint(data, pos)
            if pos + length > len(data):
                raise ValueError("Truncated length-delimited field")
            value = data[pos : pos + length]
            pos += length
            fields.append((field_number, wire_type, value))
        elif wire_type == 5:  # fixed32
            if pos + 4 > len(data):
                break  # Truncated data, stop parsing
            value = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            fields.append((field_number, wire_type, value))
        elif wire_type == 1:  # fixed64
            if pos + 8 > len(data):
                break  # Truncated data, stop parsing
            value = struct.unpack_from("<Q", data, pos)[0]
            pos += 8
            fields.append((field_number, wire_type, value))
        else:
            # Unknown wire type — stop parsing defensively
            break
    return fields


def get_field_value(fields: list, field_number: int, default=None):
    """Get the FIRST value for a given field number from decode_protobuf_fields output."""
    for fn, wt, val in fields:
        if fn == field_number:
            return val
    return default


def get_all_field_values(fields: list, field_number: int):
    """Get ALL values for a repeated field from decode_protobuf_fields output."""
    return [val for fn, wt, val in fields if fn == field_number]


# ══════════════════════════════════════════════════════
#  Request Builders
# ══════════════════════════════════════════════════════

def build_get_weekly_top_sellers_request(
    country_code: str = "US",
    language: str = "schinese",
    start_date: Optional[int] = None,
    count: int = 20,
) -> bytes:
    """Build Protobuf request for GetWeeklyTopSellers.

    Field order matters — must match what Steam server expects.
    Based on HAR capture, the correct order is:
      field 1: country_code (string)
      field 2: StoreContext (message)
      field 3: DataFilter (message)
      field 4: start_date (varint, optional)
      field 6: count (varint)

    Args:
        country_code: Country code (e.g. "US", "CN")
        language: Language code (e.g. "schinese", "english")
        start_date: Week start timestamp (Tuesday 00:00:00 UTC),
                    None = current week (field omitted)
        count: Number of results to return

    Returns:
        Protobuf-encoded request bytes
    """
    # field 1: string country_code
    buf = encode_string(1, country_code)

    # field 2: message StoreContext { field 1: string language, field 3: string country }
    ctx_inner = encode_string(1, language) + encode_string(3, country_code)
    buf += encode_message(2, ctx_inner)

    # field 3: message DataFilter
    filter_inner = (
        encode_uint32(1, 1)    # include_basic_info
        + encode_uint32(2, 1)  # include_tag_count
        + encode_uint32(3, 1)  # include_reviews
        + encode_uint32(5, 1)  # include_assets
        + encode_uint32(6, 1)  # include_platforms
        + encode_uint32(8, count)  # page_size  (use `count` not hard-coded 20)
        + encode_uint32(9, 1)   # include_trailers
        + encode_uint32(10, 1)  # include_screenshots
    )
    buf += encode_message(3, filter_inner)

    # field 4: uint32 start_date (optional — omit for current week)
    if start_date is not None:
        buf += encode_uint32(4, start_date)

    # field 6: uint32 count
    buf += encode_uint32(6, count)

    return buf


def build_get_items_request(
    app_ids: List[int],
    language: str = "schinese",
    country: str = "US",
) -> bytes:
    """Build Protobuf request for GetItems (batch game details).

    Args:
        app_ids: List of Steam app IDs
        language: Language code
        country: Country code

    Returns:
        Protobuf-encoded request bytes
    """
    buf = b""

    # field 1: repeated message AppIdWrapper { field 1: uint32 appid }
    for app_id in app_ids:
        inner = encode_uint32(1, app_id)
        buf += encode_message(1, inner)

    # field 2: message StoreContext { field 1: string language, field 3: string country }
    ctx_inner = encode_string(1, language) + encode_string(3, country)
    buf += encode_message(2, ctx_inner)

    # field 3: message ContextFlags { field 2: uint32 flag }
    flags_inner = encode_uint32(2, 1)
    buf += encode_message(3, flags_inner)

    return buf


# ══════════════════════════════════════════════════════
#  Response Parsers
# ══════════════════════════════════════════════════════

@dataclass
class TopSellerItem:
    """A single game in the top sellers list."""
    rank: int = 0
    app_id: int = 0
    name: str = ""

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "app_id": str(self.app_id),
            "game_name": self.name,
        }


def _parse_topseller_item(data: bytes) -> Optional[TopSellerItem]:
    """Parse a single TopSellerItem message from the protobuf response."""
    fields = decode_protobuf_fields(data)
    item = TopSellerItem()

    # field 1: rank/position (varint)
    pos_val = get_field_value(fields, 1)
    if pos_val is not None:
        item.rank = pos_val

    # field 2: app_id (varint)
    app_id = get_field_value(fields, 2)
    if app_id is not None:
        item.app_id = app_id

    # field 3: nested message with game details (populated by DataFilter)
    item_info_bytes = get_field_value(fields, 3)
    if item_info_bytes and isinstance(item_info_bytes, bytes):
        _enrich_item_from_protobuf(item, item_info_bytes)

    return item if item.app_id else None


def _enrich_item_from_protobuf(item: TopSellerItem, data: bytes) -> None:
    """Extract game name from the nested field 3 message inside a TopSellerItem.

    Based on actual HAR response capture, field 6 contains the game name.
    """
    fields = decode_protobuf_fields(data)

    # field 6: game name (string)
    name_bytes = get_field_value(fields, 6)
    if name_bytes and isinstance(name_bytes, bytes):
        try:
            item.name = name_bytes.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            pass


def parse_weekly_top_sellers_response(data: bytes) -> List[TopSellerItem]:
    """Parse Protobuf response from GetWeeklyTopSellers.

    Response message schema:
      message GetWeeklyTopSellersResponse {
        uint32 start_date = 1;
        repeated TopSellerItem items = 2;
      }
      message TopSellerItem {
        uint32 rank = 1;
        uint32 app_id = 2;
        StoreItem item_info = 3;  // populated by DataFilter
      }

    Args:
        data: Raw Protobuf response bytes (application/octet-stream)

    Returns:
        List of TopSellerItem, sorted by rank
    """
    fields = decode_protobuf_fields(data)
    results: List[TopSellerItem] = []

    # field 2: repeated TopSellerItem (length-delimited messages)
    items_data = get_all_field_values(fields, 2)
    for item_bytes in items_data:
        if isinstance(item_bytes, bytes):
            item = _parse_topseller_item(item_bytes)
            if item_data := item:
                results.append(item_data)

    return results


def parse_get_items_response(data: bytes) -> dict[int, dict]:
    """Parse Protobuf response from GetItems (batch game details).

    Actual response message schema (verified via HAR):
      message GetItemsResponse {
        repeated StoreItem items = 1;
      }
      message StoreItem {
        uint32 field1 = 1;     // unknown flag
        uint32 appid = 2;
        uint32 field3 = 3;     // unknown flag
        uint32 field4 = 4;     // unknown flag
        string name = 6;        // game name (plain string, NOT nested message)
        string capsule = 7;     // capsule image URL
        uint32 field9 = 9;      // unknown
        uint32 field10 = 10;    // unknown
        uint32 field13 = 13;    // unknown
        repeated uint32 field20 = 20;  // unknown repeated
        bytes  field22 = 22;    // unknown blob
        bytes  field31 = 31;    // unknown blob (likely pricing/assets)
      }

    NOTE: Pricing/assets may be in field 31 or other undocumented fields.
    For full details, use GetWeeklyTopSellers which has DataFilter.

    Args:
        data: Raw Protobuf response bytes

    Returns:
        Dict mapping app_id (int) -> game detail dict
    """
    fields = decode_protobuf_fields(data)
    results: dict[int, dict] = {}

    # field 1: repeated StoreItem
    items_data = get_all_field_values(fields, 1)
    for item_bytes in items_data:
        if not isinstance(item_bytes, bytes):
            continue
        item_fields = decode_protobuf_fields(item_bytes)

        # field 2: appid (varint)
        appid = get_field_value(item_fields, 2)
        if appid is None:
            continue

        game_info: dict = {"appid": appid}

        # field 6: name (plain string)
        name_bytes = get_field_value(item_fields, 6)
        if name_bytes and isinstance(name_bytes, bytes):
            try:
                name = name_bytes.decode("utf-8")
                if len(name) > 1:
                    game_info["name"] = name
            except UnicodeDecodeError:
                pass

        results[int(appid)] = game_info

    return results


# ══════════════════════════════════════════════════════
#  Date Helpers
# ══════════════════════════════════════════════════════

def date_to_tuesday_timestamp(date_str: str) -> int:
    """Convert a date string to Tuesday 00:00:00 UTC timestamp.

    Steam weeks start on Tuesday. If the given date is not a Tuesday,
    it will be rounded back to the previous Tuesday.

    Args:
        date_str: Date in YYYY-MM-DD format (any day of the week)

    Returns:
        Unix timestamp (seconds since epoch)
    """
    dt = datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # Make it timezone-aware UTC
    dt = dt - datetime.timedelta(seconds=datetime.timezone.utc.utcoffset(dt) or 0)

    # Adjust to previous Tuesday (weekday 1)
    days_since_tuesday = (dt.weekday() - 1) % 7
    tuesday = dt - datetime.timedelta(days=days_since_tuesday)

    return int(tuesday.timestamp())


def timestamp_to_date(ts: int) -> str:
    """Convert Unix timestamp to YYYY-MM-DD string."""
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d")
