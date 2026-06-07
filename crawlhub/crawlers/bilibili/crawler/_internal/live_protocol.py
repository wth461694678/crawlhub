"""Bilibili live WebSocket protocol helpers.

This module is private to the Bilibili crawler. It contains only protocol
mechanics: frame encoding/decoding, auth payloads, and a small synchronous
collector used by ``BilibiliClient``.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import time
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import websockets

logger = logging.getLogger(__name__)


# ── decode-error 兜底基础设施（与 douyin/kuaishou 等价）──
_BAD_FRAME_DIR = Path.home() / ".crawlhub" / "bad_frames" / "bilibili"
_BAD_FRAME_COUNTER: dict[str, int] = {}
_BAD_FRAME_LIMIT_PER_ROOM = 50
_MSG_LOG_BUCKETS: dict[tuple[str, str], dict[str, float]] = {}
_MSG_LOG_BURST_LIMIT = 5
_MSG_LOG_PERIOD_SECONDS = 60.0


def _log_bilibili_decode_error(stage: str, raw: bytes, exc: BaseException) -> None:
    """B 站单帧 decode 失败 → WARN 日志（限频）。"""
    err_class = type(exc).__name__
    bucket = _MSG_LOG_BUCKETS.setdefault((stage, err_class), {"count": 0, "last": 0.0})
    now = time.monotonic()
    should_log = False
    if bucket["count"] < _MSG_LOG_BURST_LIMIT:
        should_log = True
        bucket["count"] += 1
    elif now - bucket["last"] >= _MSG_LOG_PERIOD_SECONDS:
        should_log = True
    if should_log:
        bucket["last"] = now
        head_hex = raw[:32].hex() if raw else ""
        logger.warning(
            "[bilibili.live] %s decode failed: size=%d head_hex=%s err=%s: %s",
            stage, len(raw), head_hex, err_class, exc,
        )


def _save_bad_frame_bili(room_id: int | str, raw: bytes, *, exc: BaseException) -> None:
    """整帧 decode 失败 → 落盘 + 日志（同 douyin/kuaishou 模式）。"""
    rid = str(room_id) or "unknown"
    n = _BAD_FRAME_COUNTER.get(rid, 0) + 1
    _BAD_FRAME_COUNTER[rid] = n
    head_hex = raw[:48].hex() if raw else ""
    saved_path: str | None = None
    if n <= _BAD_FRAME_LIMIT_PER_ROOM:
        try:
            room_dir = _BAD_FRAME_DIR / rid
            room_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = room_dir / f"{ts}_{n:03d}_{type(exc).__name__}.bin"
            path.write_bytes(raw)
            meta = path.with_suffix(".txt")
            meta.write_text(
                f"room_id={rid}\nsize={len(raw)}\nerror={type(exc).__name__}: {exc}\n",
                encoding="utf-8",
            )
            saved_path = str(path)
        except Exception:
            saved_path = None
    logger.warning(
        "[bilibili.live] frame decode failed: room_id=%s size=%d head_hex=%s "
        "error=%s: %s saved=%s (#%d in this room)",
        rid, len(raw), head_hex, type(exc).__name__, exc, saved_path or "<none>", n,
    )

try:  # brotli is optional in the base project; decode gracefully if absent.
    import brotli  # type: ignore
except Exception:  # pragma: no cover - depends on environment
    brotli = None

HEADER_FORMAT = ">IHHII"
HEADER_SIZE = 16

OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_MESSAGE = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8

PROTO_JSON = 0
PROTO_HEARTBEAT_REPLY = 1
PROTO_ZLIB = 2
PROTO_BROTLI = 3

HEARTBEAT_INTERVAL_SECONDS = 30.0
HEARTBEAT_BODY = b"[object Object]"


def encode_frame(op: int, body: bytes, protover: int = 1) -> bytes:
    total_len = HEADER_SIZE + len(body)
    header = struct.pack(HEADER_FORMAT, total_len, HEADER_SIZE, protover, op, 1)
    return header + body


def iter_frames(buf: bytes) -> Iterator[tuple[int, int, bytes]]:
    offset = 0
    size = len(buf)
    while offset + HEADER_SIZE <= size:
        try:
            total_len, header_len, protover, op, _seq = struct.unpack(
                HEADER_FORMAT, buf[offset : offset + HEADER_SIZE]
            )
        except struct.error:
            return
        if total_len < HEADER_SIZE or header_len < HEADER_SIZE or offset + total_len > size:
            return
        body = buf[offset + header_len : offset + total_len]
        yield protover, op, body
        offset += total_len


def decode_message(raw: bytes) -> Iterator[dict[str, Any]]:
    for protover, op, body in iter_frames(raw):
        if op == OP_HEARTBEAT_REPLY:
            popularity = struct.unpack(">I", body[:4])[0] if len(body) >= 4 else 0
            yield {"event_type": "room_stats", "popularity": popularity, "raw_cmd": "HEARTBEAT_REPLY"}
            continue
        if op == OP_AUTH_REPLY:
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception:
                payload = {"raw": body.hex()}
            yield {"event_type": "auth", "raw_cmd": "AUTH_REPLY", "payload": payload}
            continue
        if op != OP_MESSAGE:
            yield {"event_type": "raw", "raw_cmd": f"OP_{op}", "payload": {"protover": protover}}
            continue
        if protover == PROTO_JSON:
            yield from _decode_json_body(body)
        elif protover == PROTO_ZLIB:
            try:
                yield from decode_message(zlib.decompress(body))
            except Exception as exc:
                yield {"event_type": "decode_error", "raw_cmd": "ZLIB", "payload": {"error": str(exc)}}
        elif protover == PROTO_BROTLI:
            if brotli is None:
                yield {"event_type": "decode_error", "raw_cmd": "BROTLI", "payload": {"error": "brotli missing"}}
                continue
            try:
                yield from decode_message(brotli.decompress(body))
            except Exception as exc:
                yield {"event_type": "decode_error", "raw_cmd": "BROTLI", "payload": {"error": str(exc)}}
        else:
            yield {"event_type": "raw", "raw_cmd": "MESSAGE", "payload": {"protover": protover}}


# ── INTERACT_WORD_V2 protobuf parser ────────────────────────────
# B站 INTERACT_WORD_V2 embeds a `pb` field (base64 protobuf) with
# richer user info than the JSON layer provides.  Cross-validated
# against CRA recording 2026-06-06.
#
# PB schema (CRA cross-validated 2026-06-06 against ENTRY_EFFECT JSON):
#   f1  = uid (varint) [CONFIRMED: 156/156 match with ENTRY_EFFECT uid]
#   f2  = nickname (bytes, UTF-8) [CONFIRMED: 156/156 match with ENTRY_EFFECT name]
#   f4  = interact_type bytes (0x01 = enter)
#   f5  = action: 1=enter, 2=? [high confidence, matches INTERACT_WORD convention]
#   f6  = real_room_id (varint) [CONFIRMED: 1016=short 115, 9922197=short 94277]
#   f7  = timestamp_seconds (varint) [confirmed: matches frame_ts within 2s]
#   f8  = timestamp_ms (varint) [confirmed: f8/1000 ≈ f7]
#   f9  = fans_medal sub-message (absent if no medal):
#     f9.f1  = medal_id or anchor_uid (constant 419220 across samples)
#     f9.f2  = medal_level (varint) [tentative: correlates but needs more data]
#     f9.f3  = medal_name (bytes, UTF-8, e.g. anchor name)
#     f9.f4  = medal_color_1 (varint)
#     f9.f5  = medal_color_2 (varint)
#     f9.f6  = medal_color_3 (varint)
#     f9.f12 = room_id (varint)
#     f9.f13 = medal_score (varint) [tentative]
#   f12 = string (empty in samples)
#   f15 = unique_event_id / trigger_time (varint)
#   f22 = user_detail sub-message:
#     f22.f1 = uid (varint) [CONFIRMED: always matches f1]
#     f22.f2 = user_base sub-message:
#       f22.f2.f1 = nickname (bytes, same as f2)
#       f22.f2.f2 = face_url (string) [CONFIRMED: 85.3% match with ENTRY_EFFECT face]
#     f22.f3 = display_style sub-message (colors, medal display info)
#     f22.f4 = wealth sub-message:
#       f22.f4.f1 = wealthy_level (varint) [CONFIRMED: 15/15 match with ENTRY_EFFECT wealthy_info.level]
#     f22.f6 = guard info (string, empty if not guard)
#   f23 = extra sub-message (icon, badge info; absent in most events)
#   f24 = string (empty in samples)
#
# NOTE: JSON layer for IWV2 only has {dmscore, pb} — NO uid/uname/copy_writing.

def _parse_iwv2_pb(pb_b64: str) -> dict[str, Any]:
    """Parse the base64-encoded protobuf from INTERACT_WORD_V2 data.pb.

    Returns a dict with uid, nickname, face_url, wealthy_level,
    medal_level, medal_name, real_room_id, action, timestamp.
    Falls back to partial data on parse errors.
    """
    if not pb_b64:
        return {}
    try:
        raw = base64.b64decode(pb_b64)
    except Exception:
        return {}

    # Use the same lightweight protobuf parser as douyin live_protocol.
    from crawlhub.crawlers.douyin.crawler._internal.live_protocol import (
        parse_fields, first_var, first_bytes, as_text,
    )

    fields = parse_fields(raw)
    uid = first_var(fields, 1) or 0
    nickname = as_text(first_bytes(fields, 2) or b"")
    action = first_var(fields, 5) or 0
    real_room_id = first_var(fields, 6) or 0
    ts_s = first_var(fields, 7) or 0
    ts_ms = first_var(fields, 8) or 0

    # f9 sub-message: fans medal info
    medal_level = 0
    medal_name = ""
    f9_raw = first_bytes(fields, 9)
    if f9_raw:
        f9 = parse_fields(f9_raw)
        medal_level = first_var(f9, 2) or 0
        medal_name = as_text(first_bytes(f9, 3) or b"")

    # f22 sub-message: user detail
    face_url = ""
    wealthy_level = 0
    f22_raw = first_bytes(fields, 22)
    if f22_raw:
        f22 = parse_fields(f22_raw)
        # f22.f2 = user_base
        f2_raw = first_bytes(f22, 2)
        if f2_raw:
            f2 = parse_fields(f2_raw)
            if not nickname:
                nickname = as_text(first_bytes(f2, 1) or b"")
            face_url = as_text(first_bytes(f2, 2) or b"")
        # f22.f4 = wealth info (NOT medal — confirmed via ENTRY_EFFECT cross-val)
        f4_raw = first_bytes(f22, 4)
        if f4_raw:
            f4 = parse_fields(f4_raw)
            wealthy_level = first_var(f4, 1) or 0

    return {
        "uid": int(uid),
        "nickname": nickname,
        "face_url": face_url,
        "wealthy_level": int(wealthy_level),
        "medal_level": int(medal_level),
        "medal_name": medal_name,
        "real_room_id": int(real_room_id),
        "action": int(action),
        "timestamp_s": int(ts_s),
        "timestamp_ms": int(ts_ms),
    }


def _decode_json_body(body: bytes) -> Iterator[dict[str, Any]]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        yield {"event_type": "decode_error", "raw_cmd": "JSON", "payload": {"error": str(exc)}}
        return
    if not isinstance(payload, dict):
        yield {"event_type": "raw", "raw_cmd": "JSON", "payload": {"value": payload}}
        return
    cmd = str(payload.get("cmd") or "")

    # ── 弹幕 ──
    # CRA cross-validated 2026-06-06 against LIKE_INFO_V3_CLICK & extra JSON:
    #   info[0] = [dm_type, 1, font_size, color, ts_ms, ?, 0, hash_str, ...]
    #     info[0][0] = dm_type (0=normal text) [confirmed via extra JSON]
    #     info[0][2] = font_size (25=default) [confirmed]
    #     info[0][3] = color (16777215=0xFFFFFF=white) [confirmed via extra JSON]
    #   info[1] = content text
    #   info[2] = [uid, nickname, 0, 0, 0, vip_type, 1, name_color_str]
    #     info[2][0] = uid [confirmed]
    #     info[2][1] = nickname [confirmed]
    #     info[2][5] = always 10000 in samples [NOT guard_level; extra JSON shows real guard_level differs]
    #     info[2][7] = name_color string (e.g. "#00D1F1")
    #   info[3] = [] or emoticon list [26, "name", "anchor", room_id, color, ...]
    #   info[4] = [medal_level, 0, medal_color, medal_rank_str, 0]
    #     info[4][0] = medal_level (for current room) [confirmed, differs from LIKE's medal which may be for other rooms]
    #     info[4][2] = medal_color (integer) [confirmed via extra JSON medal.color]
    #     info[4][3] = medal_rank (string like ">50000") [NOT medal_name]
    #   info[9] = {ct: str, ts: int}
    if cmd.startswith("DANMU_MSG"):
        info = payload.get("info") or []
        user = info[2] if len(info) > 2 and isinstance(info[2], list) else []
        medal = info[4] if len(info) > 4 and isinstance(info[4], list) else []
        meta = info[0] if len(info) > 0 and isinstance(info[0], list) else []

        uid = str(user[0] if user else "")
        nickname = str(user[1] if len(user) > 1 else "")
        content = str(info[1] if len(info) > 1 else "")

        # Extract metadata from info arrays (only confirmed fields)
        dm_type = int(meta[0]) if len(meta) > 0 and isinstance(meta[0], (int, float)) else 0
        font_size = int(meta[2]) if len(meta) > 2 and isinstance(meta[2], (int, float)) else 25
        color = int(meta[3]) if len(meta) > 3 and isinstance(meta[3], (int, float)) else 0
        # medal info
        medal_level = int(medal[0]) if len(medal) > 0 and isinstance(medal[0], (int, float)) else 0
        medal_color = int(medal[2]) if len(medal) > 2 and isinstance(medal[2], (int, float)) else 0
        medal_rank = str(medal[3]) if len(medal) > 3 and medal[3] else ""

        yield {
            "event_type": "chat",
            "raw_cmd": cmd,
            "uid": uid,
            "nickname": nickname,
            "content": content,
            "payload": payload,
            "dm_type": dm_type,
            "font_size": font_size,
            "color": color,
            "medal_level": medal_level,
            "medal_color": medal_color,
            "medal_rank": medal_rank,
        }
        return

    # ── 进房 / 入场特效 ──
    if cmd in {"INTERACT_WORD", "ENTRY_EFFECT", "INTERACT_WORD_V2"}:
        data = payload.get("data") or {}
        uid = str(data.get("uid") or "")
        nickname = str(data.get("uname") or data.get("copy_writing") or "")

        # INTERACT_WORD_V2: JSON layer only has {dmscore, pb}.
        # All user info must come from the embedded protobuf.
        pb_info: dict[str, Any] = {}
        if cmd == "INTERACT_WORD_V2":
            pb_b64 = data.get("pb") or ""
            if pb_b64:
                pb_info = _parse_iwv2_pb(pb_b64)
                if pb_info.get("uid"):
                    uid = str(pb_info["uid"])
                if pb_info.get("nickname"):
                    nickname = pb_info["nickname"]

        # ENTRY_EFFECT: extract business type and uinfo
        # CRA cross-validated 2026-06-06:
        #   business=6: normal entry effect (331/348 events)
        #   business=1: special entry effect (12 events, with privilege_type=3)
        #   privilege_type: 0=normal, 3=governor (总督)
        #   business controls visual effect type, NOT guard rank
        entry_effect: dict[str, Any] = {}
        if cmd == "ENTRY_EFFECT":
            business = data.get("business")
            privilege_type = data.get("privilege_type")
            copy_writing_v2 = str(data.get("copy_writing_v2") or "")
            uinfo = data.get("uinfo") or {}
            base_info = uinfo.get("base") or {}
            entry_effect = {
                "business": business,
                "privilege_type": privilege_type,
                "copy_writing_v2": copy_writing_v2,
            }
            # Prefer uinfo nickname
            if base_info.get("name") and not nickname:
                nickname = str(base_info["name"])

        result: dict[str, Any] = {
            "event_type": "member",
            "raw_cmd": cmd,
            "uid": uid,
            "nickname": nickname,
        }
        # Add structured extra info from PB (IWV2 only)
        if pb_info:
            result["face_url"] = pb_info.get("face_url", "")
            result["wealthy_level"] = pb_info.get("wealthy_level", 0)
            result["medal_level"] = pb_info.get("medal_level", 0)
            result["medal_name"] = pb_info.get("medal_name", "")
            result["action"] = pb_info.get("action", 0)
            result["real_room_id"] = pb_info.get("real_room_id", 0)
        if entry_effect:
            result["entry_effect"] = entry_effect
        result["payload"] = payload
        yield result
        return

    # ── 点赞 ──
    # CRA cross-validated 2026-06-06:
    #   LIKE_INFO_V3_CLICK: JSON has fans_medal, dmscore, like_text, uinfo
    #   fans_medal keys confirmed: medal_level, medal_name, guard_level, is_lighted
    #   LIKE_INFO_V3_UPDATE: has click_count (aggregate total)
    if cmd in {"LIKE_INFO_V3_CLICK", "LIKE_INFO_V3_UPDATE"}:
        data = payload.get("data") or {}
        click_raw = data.get("click_count") or data.get("like_text") or 0
        try:
            like_count = int(click_raw)
        except (TypeError, ValueError):
            like_count = 0
        # Extract fans_medal info from V3_CLICK
        fans_medal = data.get("fans_medal") or {}
        result: dict[str, Any] = {
            "event_type": "like",
            "raw_cmd": cmd,
            "uid": str(data.get("uid") or ""),
            "nickname": str(data.get("uname") or ""),
            "like_count": like_count,
            "payload": payload,
        }
        if fans_medal:
            result["medal_level"] = int(fans_medal.get("medal_level") or 0)
            result["medal_name"] = str(fans_medal.get("medal_name") or "")
            result["guard_level"] = int(fans_medal.get("guard_level") or 0)
            result["is_lighted"] = bool(fans_medal.get("is_lighted"))
        if data.get("dmscore") is not None:
            result["dmscore"] = int(data.get("dmscore") or 0)
        if data.get("like_text"):
            result["like_text"] = str(data["like_text"])
        yield result
        return

    # ── 礼物 ──
    # CRA cross-validated 2026-06-06:
    #   data.coin_type observed: "gold" (paid gifts only in this recording)
    #   B站 convention: "gold"=paid, "silver"=free (not CRA-confirmed)
    #   data.combo_send/combo_num for combo info
    if cmd in {"SEND_GIFT", "COMBO_SEND"}:
        data = payload.get("data") or {}
        coin_type = str(data.get("coin_type") or "")
        gift_count = int(data.get("num") or data.get("combo_num") or 0)
        yield {
            "event_type": "gift",
            "raw_cmd": cmd,
            "uid": str(data.get("uid") or ""),
            "nickname": str(data.get("uname") or ""),
            "gift_id": str(data.get("giftId") or data.get("gift_id") or ""),
            "gift_name": str(data.get("giftName") or data.get("gift_name") or ""),
            "gift_count": gift_count,
            "coin_type": coin_type,
            "is_free_gift": coin_type == "silver",
            "payload": payload,
        }
        return

    # ── 大航海（舰长/提督/总督） ──
    if cmd in {"GUARD_BUY", "USER_TOAST_MSG", "USER_TOAST_MSG_V2"}:
        data = payload.get("data") or {}
        yield {
            "event_type": "guard",
            "raw_cmd": cmd,
            "uid": str(data.get("uid") or data.get("target_id") or ""),
            "nickname": str(data.get("username") or data.get("uname") or ""),
            "gift_name": str(data.get("role_name") or data.get("gift_name") or ""),
            "gift_count": int(data.get("num") or 1),
            "payload": payload,
        }
        return

    # ── 上舰广播（房间外） ──
    if cmd == "GUARD_HONOR_THOUSAND":
        data = payload.get("data") or {}
        yield {
            "event_type": "guard_broadcast",
            "raw_cmd": cmd,
            "payload": payload,
        }
        return

    # ── SC（醒目留言） ──
    if cmd in {"SUPER_CHAT_MESSAGE", "SUPER_CHAT_MESSAGE_JPN"}:
        data = payload.get("data") or {}
        user_info = data.get("user_info") or {}
        yield {
            "event_type": "super_chat",
            "raw_cmd": cmd,
            "uid": str(data.get("uid") or ""),
            "nickname": str(user_info.get("uname") or ""),
            "content": str(data.get("message") or ""),
            "gift_count": int(data.get("price") or 0),  # 价格（元）
            "payload": payload,
        }
        return

    # ── SC 删除 ──
    if cmd == "SUPER_CHAT_MESSAGE_DELETE":
        yield {
            "event_type": "super_chat_delete",
            "raw_cmd": cmd,
            "payload": payload,
        }
        return

    # ── 主播实时数据更新（粉丝数/粉丝团） ──
    # CRA cross-validated 2026-06-06:
    #   data.fans = anchor's total follower count
    #   data.fans_club = fans club member count
    #   data.red_notice = red notice count (-1 = none)
    if cmd == "ROOM_REAL_TIME_MESSAGE_UPDATE":
        data = payload.get("data") or {}
        yield {
            "event_type": "room_stats",
            "raw_cmd": cmd,
            "online_count": int(data.get("fans") or 0),  # 粉丝数
            "popularity": int(data.get("fans_club") or 0),  # 粉丝团人数
            "payload": payload,
        }
        return

    # ── 在线人气 / 观看人数 ──
    # CRA cross-validated 2026-06-06:
    #   WATCHED_CHANGE.data.num = watched count (numeric)
    #   WATCHED_CHANGE.data.text_small = display text (e.g. "1.7万")
    #   WATCHED_CHANGE.data.text_large = full text (e.g. "1.7万人看过")
    if cmd in {"WATCHED_CHANGE", "ONLINE_RANK_COUNT"}:
        data = payload.get("data") or {}
        watched = data.get("watched_show") if isinstance(data.get("watched_show"), dict) else {}
        yield {
            "event_type": "room_stats",
            "raw_cmd": cmd,
            "online_count": int(data.get("count") or data.get("num") or 0),
            "popularity": int(watched.get("num") or 0),
            "payload": payload,
        }
        return

    # ── 高能用户榜 ──
    if cmd in {"ONLINE_RANK_V2", "ONLINE_RANK_V3", "ONLINE_RANK_TOP3"}:
        yield {
            "event_type": "rank",
            "raw_cmd": cmd,
            "payload": payload,
        }
        return

    # ── 人气红包 ──
    if cmd in {
        "POPULARITY_RED_POCKET_NEW",
        "POPULARITY_RED_POCKET_START",
        "POPULARITY_RED_POCKET_WINNER_LIST",
        "POPULARITY_RED_POCKET_V2_NEW",
        "POPULARITY_RED_POCKET_V2_START",
        "POPULARITY_RED_POCKET_V2_WINNER_LIST",
    }:
        yield {
            "event_type": "red_pocket",
            "raw_cmd": cmd,
            "payload": payload,
        }
        return

    # ── 抽奖 ──
    if cmd in {"ANCHOR_LOT_START", "ANCHOR_LOT_END", "ANCHOR_LOT_AWARD", "ANCHOR_LOT_CHECKSTATUS"}:
        yield {
            "event_type": "lottery",
            "raw_cmd": cmd,
            "payload": payload,
        }
        return

    # ── 关注/订阅消息 ──
    if cmd == "NOTICE_MSG":
        msg_type = payload.get("msg_type")
        yield {
            "event_type": "notice",
            "raw_cmd": cmd,
            "content": str(payload.get("msg_self") or payload.get("name") or ""),
            "payload": payload,
        }
        return

    # ── 直播状态：开播 ──
    if cmd == "LIVE":
        yield {
            "event_type": "live_start",
            "raw_cmd": cmd,
            "payload": payload,
        }
        return

    # ── 直播状态：下播（关键信号，stop_when_room_closed 依赖此 cmd） ──
    if cmd == "PREPARING":
        # PREPARING 包含 roomid，由调用方比对当前 roomid 决定是否停止
        yield {
            "event_type": "live_end",
            "raw_cmd": cmd,
            "content": str(payload.get("roomid") or ""),  # 复用 content 字段存房间号
            "payload": payload,
        }
        return

    # ── 直播间被切断/警告 ──
    if cmd in {"CUT_OFF", "WARNING", "CUT_OFF_V2", "ROOM_BLOCK_MSG", "ROOM_LOCK"}:
        yield {
            "event_type": "live_blocked",
            "raw_cmd": cmd,
            "content": str(payload.get("msg") or ""),
            "payload": payload,
        }
        return

    # ── 房间信息变更（标题/分区） ──
    if cmd in {"ROOM_CHANGE", "ROOM_SKIN_MSG"}:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        yield {
            "event_type": "room_change",
            "raw_cmd": cmd,
            "content": str(data.get("title") or ""),
            "payload": payload,
        }
        return

    # ── 互动提示（分享直播间等） ──
    # CRA cross-validated 2026-06-06:
    #   data.type: 105=share_live_room
    #   data.data: JSON string with suffix_text like "人分享了直播间"
    #   data.dmscore: interaction score
    if cmd == "DM_INTERACTION":
        data = payload.get("data") or {}
        interaction_type = int(data.get("type") or 0)
        # Parse inner data JSON string
        inner_data_str = data.get("data") or ""
        inner_data: dict[str, Any] = {}
        if inner_data_str:
            try:
                inner_data = json.loads(inner_data_str)
            except Exception:
                pass
        yield {
            "event_type": "interaction",
            "raw_cmd": cmd,
            "payload": payload,
            "interaction_type": interaction_type,
            "suffix_text": str(inner_data.get("suffix_text") or ""),
            "dmscore": int(data.get("dmscore") or 0),
        }
        return

    # ── 弹幕撤回 ──
    # CRA cross-validated 2026-06-06:
    #   data.recall_type: 2=admin_recall
    #   data.target_id: the dm id being recalled
    if cmd == "RECALL_DANMU_MSG":
        data = payload.get("data") or {}
        yield {
            "event_type": "danmu_recall",
            "raw_cmd": cmd,
            "content": str(data.get("target_id") or ""),
            "payload": payload,
        }
        return

    # ── 人气榜排名变化 ──
    # CRA cross-validated 2026-06-06:
    #   data.uid = anchor uid
    #   data.rank = current rank (0 = not on chart)
    #   data.countdown = seconds until next refresh
    #   data.rank_name_by_type = chart name (e.g. "人气榜")
    #   data.on_rank_name_by_type = sub-chart name (e.g. "单机人气")
    if cmd == "POPULAR_RANK_CHANGED":
        data = payload.get("data") or {}
        yield {
            "event_type": "rank_change",
            "raw_cmd": cmd,
            "payload": payload,
            "rank": int(data.get("rank") or 0),
            "countdown": int(data.get("countdown") or 0),
            "rank_name": str(data.get("rank_name_by_type") or ""),
        }
        return

    # ── 热门榜/分区榜排名变化 ──
    # CRA cross-validated 2026-06-06:
    #   data.uid = anchor uid, data.rank, data.rank_type
    #   data.rank_name_by_type = "热门榜" etc.
    if cmd == "RANK_CHANGED_V2":
        data = payload.get("data") or {}
        yield {
            "event_type": "rank_change",
            "raw_cmd": cmd,
            "payload": payload,
            "rank": int(data.get("rank") or 0),
            "countdown": int(data.get("countdown") or 0),
            "rank_name": str(data.get("rank_name_by_type") or ""),
            "rank_type": int(data.get("rank_type") or 0),
        }
        return

    # ── 全平台停播房间列表（广播，与当前房间无关，仅记录） ──
    if cmd == "STOP_LIVE_ROOM_LIST":
        yield {
            "event_type": "global_stop_list",
            "raw_cmd": cmd,
            "payload": payload,
        }
        return

    # ── 投喂/抽奖中奖广播 ──
    if cmd in {"GUARD_BUY_GIFT", "WIN_LOTTERY_NOTICE", "WIDGET_GIFT_STAR_PROCESS"}:
        yield {
            "event_type": "broadcast",
            "raw_cmd": cmd,
            "payload": payload,
        }
        return

    # ── 兜底：保留完整 payload，由用户自行二次解析 ──
    yield {"event_type": "raw", "raw_cmd": cmd, "payload": payload}


def build_auth_frame(room_id: int, token: str, uid: int = 0, buvid: str = "") -> bytes:
    body = json.dumps(
        {"uid": uid, "roomid": room_id, "protover": 3, "buvid": buvid, "platform": "web", "type": 2, "key": token},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return encode_frame(OP_AUTH, body, protover=1)


def build_heartbeat_frame() -> bytes:
    return encode_frame(OP_HEARTBEAT, HEARTBEAT_BODY, protover=1)


async def collect_events_async(
    *,
    room_id: int,
    token: str,
    host: str,
    port: int = 2245,
    duration_seconds: float = 60,
    on_event: Callable[[dict[str, Any]], None],
    is_cancelled: Callable[[], bool] | None = None,
    uid: int = 0,
    buvid: str = "",
    event_filter: set[str] | None = None,
) -> int:
    """Collect Bilibili live WSS events.

    Args:
        room_id: Real (long) room id used for auth.
        token: getDanmuInfo token.
        host/port: WSS endpoint.
        duration_seconds: Hard timeout cap.
        on_event: Callback for each event.
        is_cancelled: External cancellation hook.
        uid/buvid: Login state for WSS auth.
        event_filter: If provided, only emit events whose ``raw_cmd`` is in this
            set (decode_error / connection events always pass). None = all.

    Returns:
        Number of events emitted (filtered count).

    Stop conditions (auto, no user toggle):
      1. duration_seconds reached
      2. is_cancelled() returns True
      3. WSS connection closed
      4. ``PREPARING`` cmd received with matching roomid (room ended live)
    """
    deadline = (time.monotonic() + float(duration_seconds)) if duration_seconds and float(duration_seconds) > 0 else float("inf")
    event_count = 0
    uri = f"wss://{host}:{int(port or 2245)}/sub"
    target_roomid_str = str(int(room_id))
    async with websockets.connect(uri, ping_interval=None, close_timeout=3) as ws:
        await ws.send(build_auth_frame(room_id, token, uid=uid, buvid=buvid))
        await ws.send(build_heartbeat_frame())
        next_hb = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS
        while time.monotonic() < deadline:
            if is_cancelled and is_cancelled():
                break
            now = time.monotonic()
            if now >= next_hb:
                await ws.send(build_heartbeat_frame())
                next_hb = now + HEARTBEAT_INTERVAL_SECONDS
            timeout = min(1.0, max(0.05, deadline - now))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                break
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            # 整帧 decode 失败保护：保存 raw + 日志 + 继续 recv（不阻断 action）
            try:
                events_iter = list(decode_message(raw))
            except Exception as e:
                _save_bad_frame_bili(room_id, raw, exc=e)
                continue
            for event in events_iter:
                cmd = str(event.get("raw_cmd") or "")

                # ── Stop signal: PREPARING(this room) means broadcaster ended ──
                if cmd == "PREPARING":
                    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                    event_roomid = str(payload.get("roomid") or "")
                    if event_roomid and event_roomid == target_roomid_str:
                        # Always emit the live_end event so caller knows why.
                        if event_filter is None or cmd in event_filter:
                            on_event(event)
                            event_count += 1
                        return event_count

                # ── event_filter ──
                if event_filter is not None and cmd not in event_filter:
                    continue

                on_event(event)
                event_count += 1
    return event_count


def collect_events(**kwargs: Any) -> int:
    return asyncio.run(collect_events_async(**kwargs))
