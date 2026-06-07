"""Kuaishou live protocol helpers (private).

══════════════════════════════════════════════════════════════════════
 Hybrid 架构 —— 浏览器扛身份证明，Python 接管 WebSocket
══════════════════════════════════════════════════════════════════════

 上层：
   * ``capture_handover(browser_session, principal_id)`` — 用 daemon
     管理的真实 Chrome（带 stealth + persistent profile + cookie 灌种）
     打开直播间，截获 ``/live_api/liveroom/websocketinfo`` 响应；返回
     ``{token, websocketUrls, liveStreamId, cookie_header, user_agent}``
     这一份 ``LiveHandover``。
   * ``collect_events`` / ``collect_events_async`` — 接收 handover，跑
     完整 WS 状态机：CS_ENTER_ROOM → SC_ENTER_ROOM_ACK → 心跳循环 +
     SC_FEED_PUSH 解析 → 解码弹幕 / 礼物 / 点赞 / 系统通知。

 下层（纯协议，无浏览器依赖）：
   * Protobuf wire-format 编解码（``ProtobufEncoder`` / ``ProtobufDecoder``）
   * AES-CBC 弹幕 payload 解密（key/iv 为 JS module 49824 逆向产物）
   * GZIP / AES 双路 ``decompress_payload``
   * 消息构建器：CS_ENTER_ROOM / CS_HEARTBEAT / CS_USER_EXIT
   * 消息解析器：FeedPush / EnterRoomAck / WatchingList / Error

══════════════════════════════════════════════════════════════════════
 设计哲学
══════════════════════════════════════════════════════════════════════

 浏览器只负责"身份证明"这件最难、最不稳定的事；一旦拿到 token + ws_url，
 Python 接管 WS。哲学上这叫"边界归一"——hybrid 抽象的边界点就是 ``LiveHandover``，
 上下两层完全解耦：协议层不知道浏览器的存在，浏览器层不知道协议是什么样。
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import struct
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse


import websockets

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from crawlhub.core.browser.host_environment import REAL_ACCEPT_LANGUAGE


# ════════════════════════════════════════════════════════════
#  Protobuf wire format —— 最小化编解码器（不依赖 protoc 生成代码）
# ════════════════════════════════════════════════════════════


class ProtobufEncoder:
    @staticmethod
    def encode_varint(value: int) -> bytes:
        result = bytearray()
        while value > 0x7F:
            result.append((value & 0x7F) | 0x80)
            value >>= 7
        result.append(value & 0x7F)
        return bytes(result)

    @staticmethod
    def encode_field(field_number: int, wire_type: int, data: bytes) -> bytes:
        tag = (field_number << 3) | wire_type
        return ProtobufEncoder.encode_varint(tag) + data

    @staticmethod
    def encode_string(field_number: int, value) -> bytes:
        if isinstance(value, str):
            value = value.encode("utf-8")
        return ProtobufEncoder.encode_field(
            field_number, 2,
            ProtobufEncoder.encode_varint(len(value)) + value,
        )

    @staticmethod
    def encode_uint64(field_number: int, value: int) -> bytes:
        return ProtobufEncoder.encode_field(
            field_number, 0, ProtobufEncoder.encode_varint(value),
        )

    @staticmethod
    def encode_uint32(field_number: int, value: int) -> bytes:
        return ProtobufEncoder.encode_uint64(field_number, value)


class ProtobufDecoder:
    def __init__(self, data) -> None:
        self.data = data if isinstance(data, (bytes, bytearray)) else bytes(data)
        self.pos = 0

    def read_varint(self) -> int:
        result, shift = 0, 0
        while self.pos < len(self.data):
            b = self.data[self.pos]
            self.pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                return result
            shift += 7
        raise ValueError("Truncated varint")

    def read_field(self):
        if self.pos >= len(self.data):
            return None
        try:
            tag = self.read_varint()
        except Exception:
            return None
        field_number, wire_type = tag >> 3, tag & 0x07
        try:
            if wire_type == 0:
                value = self.read_varint()
            elif wire_type == 1:
                if self.pos + 8 > len(self.data):
                    return None
                value = struct.unpack("<Q", self.data[self.pos:self.pos + 8])[0]
                self.pos += 8
            elif wire_type == 2:
                length = self.read_varint()
                if self.pos + length > len(self.data):
                    return None
                value = self.data[self.pos:self.pos + length]
                self.pos += length
            elif wire_type == 5:
                if self.pos + 4 > len(self.data):
                    return None
                value = struct.unpack("<I", self.data[self.pos:self.pos + 4])[0]
                self.pos += 4
            else:
                # 容错：未知 wire (3/4/6/7) 不再 raise，停止往后读。
                # 已读到的字段（caller 关心的 method/uid/text 等）保留。
                return None
        except Exception:
            return None
        return (field_number, wire_type, value)

    def read_all_fields(self) -> dict:
        fields_out: dict = {}
        while self.pos < len(self.data):
            entry = self.read_field()
            if entry is None:
                break
            fn, wt, val = entry
            fields_out.setdefault(fn, []).append((wt, val))
        return fields_out


# ════════════════════════════════════════════════════════════
#  消息类型枚举
# ════════════════════════════════════════════════════════════


class PayloadType:
    CS_HEARTBEAT = 1
    CS_ERROR = 3
    CS_PING = 4
    SC_HEARTBEAT_ACK = 101
    SC_ECHO = 102
    SC_ERROR = 103
    SC_PING_ACK = 104
    SC_INFO = 105
    CS_ENTER_ROOM = 200
    CS_USER_PAUSE = 201
    CS_USER_EXIT = 202
    SC_ENTER_ROOM_ACK = 300
    SC_FEED_PUSH = 310
    SC_RED_PACK_FEED = 330
    SC_LIVE_WATCHING_LIST = 340
    SC_GUESS_OPENED = 370
    SC_GUESS_CLOSED = 371
    SC_RIDE_CHANGED = 412
    SC_BET_CHANGED = 441
    SC_BET_CLOSED = 442
    SC_LIVE_SPECIAL_ACCOUNT_CONFIG_STATE = 645
    SC_LIVE_WARNING_MASK_STATUS_CHANGED_AUDIENCE = 758
    SC_INTERACTIVE_CHAT_CLOSED = 776
    SC_COMMENT_ZONE_RICH_TEXT = 829
    # CRA cross-validated 2026-06-06:
    #   pt=510: multi-event frame (enter-room / ranking / stream config / ad).
    #   f28 = enter-room events (57/82 frames), f40 = ranking notifications (2/82),
    #   f53 = stream config (20/82), f14 = commercial ads (9/82).
    SC_SHOW_FEED = 510


class CompressionType:
    UNKNOWN = 0
    NONE = 1
    GZIP = 2
    AES = 3


# ════════════════════════════════════════════════════════════
#  AES-CBC 弹幕 payload 解密（来源：JS module 49824 逆向）
# ════════════════════════════════════════════════════════════

_PAYLOAD_AES_KEY = b"PPbzKKL7NB15leYy"
_PAYLOAD_AES_IV = b"JRODKJiolJ9xqso0"


def aes_cbc_decrypt(data) -> bytes:
    cipher = AES.new(_PAYLOAD_AES_KEY, AES.MODE_CBC, _PAYLOAD_AES_IV)
    decrypted = cipher.decrypt(bytes(data))
    try:
        return unpad(decrypted, AES.block_size)
    except ValueError:
        return decrypted


def decompress_payload(compression_type: int, payload: bytes) -> bytes:
    if compression_type == CompressionType.AES:
        return aes_cbc_decrypt(payload)
    if compression_type == CompressionType.GZIP:
        return gzip.decompress(payload)
    return payload


# ════════════════════════════════════════════════════════════
#  SocketMessage 编解码（最外层）
# ════════════════════════════════════════════════════════════


def encode_socket_message(payload_type: int, payload_data: bytes) -> bytes:
    enc = ProtobufEncoder
    msg = enc.encode_uint32(1, payload_type)
    msg += enc.encode_string(3, payload_data)
    return msg


def decode_socket_message(data: bytes) -> dict:
    decoder = ProtobufDecoder(data)
    fields_out = decoder.read_all_fields()
    return {
        "payloadType": fields_out[1][0][1] if 1 in fields_out else 0,
        "compressionType": fields_out[2][0][1] if 2 in fields_out else 0,
        "payload": fields_out[3][0][1] if 3 in fields_out else b"",
    }


# ════════════════════════════════════════════════════════════
#  消息构建器（C → S）
# ════════════════════════════════════════════════════════════


def build_enter_room(token: str, live_stream_id: str, page_id: str = "") -> bytes:
    enc = ProtobufEncoder
    payload = enc.encode_string(1, token) + enc.encode_string(2, live_stream_id)
    payload += enc.encode_uint32(3, 0) + enc.encode_uint32(4, 0)
    if page_id:
        payload += enc.encode_string(7, page_id)
    return encode_socket_message(PayloadType.CS_ENTER_ROOM, payload)


def build_heartbeat() -> bytes:
    payload = ProtobufEncoder.encode_uint64(1, int(time.time() * 1000))
    return encode_socket_message(PayloadType.CS_HEARTBEAT, payload)


def build_user_exit() -> bytes:
    payload = ProtobufEncoder.encode_uint64(1, int(time.time() * 1000))
    return encode_socket_message(PayloadType.CS_USER_EXIT, payload)


# ════════════════════════════════════════════════════════════
#  消息解析器（S → C）
# ════════════════════════════════════════════════════════════


def _s(fields_out: dict, n: int) -> str:
    """提取 UTF-8 字符串字段。"""
    if n not in fields_out:
        return ""
    return fields_out[n][0][1].decode("utf-8", errors="replace")


def _i(fields_out: dict, n: int) -> int:
    """提取 varint 字段。"""
    return fields_out[n][0][1] if n in fields_out else 0


def parse_simple_user_info(data: bytes) -> dict:
    f = ProtobufDecoder(data).read_all_fields()
    return {
        "principalId": _s(f, 1),
        "userName": _s(f, 2),
        "headUrl": _s(f, 3),
    }


# ────────────────────────────────────────────────────────────
#  进房 / 展示特效 事件解析（pt=510 SC_SHOW_FEED）
# ────────────────────────────────────────────────────────────
# CRA cross-validated 2026-06-06 against DOM & internal consistency:
#
# pt=510 top-level is a container; each sub-field holds a different event type:
#   f28 = enter-room event (57/82 in CRA sample):
#     f28.f1  = user info sub-msg (same layout as SimpleUserInfo: f1=uid, f2=name, f3=gender, f5=avatarUrl)
#     f28.f2  = display/level info sub-msg:
#       f28.f2.f4  = level (varint) [observed: 3]
#       f28.f2.f6  = wealth grade sub-msg (f1=grade, f2=?) [observed: f1=11, f2=1]
#       f28.f2.f7  = wealth grade value (varint) [observed: 7]
#       f28.f2.f9  = badge image URL (sub-msg with f3=url)
#       f28.f2.f11 = level label image URL string
#     f28.f3  = event type varint [always 5 in CRA sample]
#     f28.f7  = display text [confirmed: "进入直播间" in all 57 samples]
#     f28.f18 = effect name string [e.g. "dengpaidakajinchang"]
#     f28.f19 = effect display duration (varint) [observed: 5000]
#     f28.f30 = rich info sub-msg:
#       f28.f30.f5  = extended user info (same as f1)
#       f28.f30.f8  = value (varint) [observed: 10000]
#       f28.f30.f15 = JSON string with reqType=2800 [confirmed]
#     f28.f34 = JSON string with fromUserId, bizType, subBizType [confirmed]
#   f40 = ranking notification:
#     f40.f1 = text [e.g. "恭喜xxx成为在线观众榜前2"]
#     f40.f2 = varint (1)
#     f40.f4 = username string
#   f53 = stream config (CDN URLs, playUrl etc.)
#   f14 = commercial/ad feed (COMMERCE_LiveAdSocialConversionFeed)

def _parse_enter_room_event(data: bytes) -> dict[str, Any]:
    """Parse a single f28 enter-room sub-message from SC_SHOW_FEED.

    Returns uid, nickname, avatar_url, wealth_grade, display_text.

    CRA cross-validated 2026-06-06:
      f1 = user info sub-msg:
        f1.f1 = userId (varint, e.g. 422972243) — numeric ID, NOT principalId string
        f1.f2 = userName (string)
        f1.f3 = gender (string, e.g. "U")
        f1.f5 = avatarUrl (string, CDN URL)
      f2 = display/level info sub-msg:
        f2.f4 = level (varint) [observed: 3]
        f2.f6 = wealth grade sub-msg (f1=grade, f2=?)
        f2.f7 = wealth grade (varint) [confirmed: matches comment/gift wealth_grade]
      f7 = display text [confirmed: "进入直播间"]
      f18 = effect name string
    """
    f = ProtobufDecoder(data).read_all_fields()
    # f1 = user info (different structure from SimpleUserInfo: f1 is varint, not string)
    uid = ""
    nickname = ""
    avatar_url = ""
    if 1 in f:
        try:
            user_f = ProtobufDecoder(f[1][0][1]).read_all_fields()
            # f1.f1 is a varint (numeric userId), convert to string
            uid = str(_i(user_f, 1))
            nickname = _s(user_f, 2)
            avatar_url = _s(user_f, 5)
        except Exception:
            pass

    # f2 = display/level info
    wealth_grade = 0
    level = 0
    if 2 in f:
        try:
            disp_f = ProtobufDecoder(f[2][0][1]).read_all_fields()
            level = _i(disp_f, 4)
            if 6 in disp_f:
                grade_f = ProtobufDecoder(disp_f[6][0][1]).read_all_fields()
                wealth_grade = _i(grade_f, 1)
            # Also try f7 as wealth grade
            if not wealth_grade:
                wealth_grade = _i(disp_f, 7)
        except Exception:
            pass

    display_text = _s(f, 7)
    effect_name = _s(f, 18)

    return {
        "uid": uid,
        "nickname": nickname,
        "avatar_url": avatar_url,
        "wealth_grade": int(wealth_grade),
        "level": int(level),
        "display_text": display_text,
        "effect_name": effect_name,
    }


def _parse_ranking_notification(data: bytes) -> dict[str, Any]:
    """Parse a single f40 ranking notification from SC_SHOW_FEED."""
    f = ProtobufDecoder(data).read_all_fields()
    return {
        "text": _s(f, 1),
        "username": _s(f, 4),
    }


def parse_show_feed(data: bytes) -> dict[str, Any]:
    """Parse a SC_SHOW_FEED (pt=510) payload.

    Returns a dict with enterRoomEvents and rankingNotifications.
    """
    f = ProtobufDecoder(data).read_all_fields()
    enter_events = [_parse_enter_room_event(v) for _, v in f.get(28, [])]
    ranking_events = [_parse_ranking_notification(v) for _, v in f.get(40, [])]
    return {
        "enterRoomEvents": enter_events,
        "rankingNotifications": ranking_events,
    }


def parse_comment_feed(data: bytes) -> dict:
    # CRA cross-validated 2026-06-06:
    #   f1 = id (string, often empty)
    #   f2 = user info sub-msg (principalId, userName, headUrl)
    #   f3 = content (string) [confirmed]
    #   f4 = token/id (base64 string) [semantic unknown]
    #   f6 = color (string) [confirmed]
    #   f7 = varint (observed: 1) [semantic unknown]
    #   f8 = display info sub-msg:
    #     f8.f4  = level (varint) [observed: 3]
    #     f8.f6  = wealth grade sub-msg (f1=grade, f2=?) [observed: f1=11]
    #     f8.f7  = wealth grade (varint) [observed: 7]
    #     f8.f11 = level label image URL string
    #   f9 = time (varint) [always 0 in CRA sample]
    f = ProtobufDecoder(data).read_all_fields()
    result: dict[str, Any] = {
        "id": _s(f, 1),
        "content": _s(f, 3),
        "color": _s(f, 6),
        "time": _i(f, 9),
    }
    if 2 in f:
        result["user"] = parse_simple_user_info(f[2][0][1])
    # f8 = display/level info
    if 8 in f:
        try:
            disp_f = ProtobufDecoder(f[8][0][1]).read_all_fields()
            result["wealth_grade"] = _i(disp_f, 7)
            result["level"] = _i(disp_f, 4)
            if 6 in disp_f:
                grade_f = ProtobufDecoder(disp_f[6][0][1]).read_all_fields()
                if not result["wealth_grade"]:
                    result["wealth_grade"] = _i(grade_f, 1)
        except Exception:
            pass
    return result


def parse_gift_feed(data: bytes) -> dict:
    # CRA cross-validated 2026-06-06:
    #   f1 = id (string, often empty)
    #   f2 = user info sub-msg
    #   f3 = time (varint) [always 0 in CRA sample]
    #   f4 = giftId (varint) [confirmed: unique per gift type]
    #   f6 = giftKey (string, format: "userId-giftId-giftType-count") [confirmed pattern]
    #   f7 = batchSize (varint) [confirmed]
    #   f8 = comboCount (varint) [confirmed]
    #   f9 = giftType (varint) [observed: 1-29 in CRA; fine-grained category, not cheap/normal/expensive]
    #   f10 = displayDuration (varint) [always 300000 in CRA; likely 5min display in ms]
    #   f16 = token (base64 string)
    #   f18 = display info sub-msg (same structure as CommentFeed f8)
    #   f19 = string (empty in samples)
    f = ProtobufDecoder(data).read_all_fields()
    result: dict[str, Any] = {
        "id": _s(f, 1),
        "time": _i(f, 3),
        "giftId": _i(f, 4),
        "batchSize": _i(f, 7),
        "comboCount": _i(f, 8),
        "giftType": _i(f, 9),
    }
    if 2 in f:
        result["user"] = parse_simple_user_info(f[2][0][1])
    # f18 = display/level info (same structure as CommentFeed f8)
    if 18 in f:
        try:
            disp_f = ProtobufDecoder(f[18][0][1]).read_all_fields()
            result["wealth_grade"] = _i(disp_f, 7)
            result["level"] = _i(disp_f, 4)
            if 6 in disp_f:
                grade_f = ProtobufDecoder(disp_f[6][0][1]).read_all_fields()
                if not result["wealth_grade"]:
                    result["wealth_grade"] = _i(grade_f, 1)
        except Exception:
            pass
    return result


def parse_like_feed(data: bytes) -> dict:
    f = ProtobufDecoder(data).read_all_fields()
    result = {"id": _s(f, 1)}
    if 2 in f:
        result["user"] = parse_simple_user_info(f[2][0][1])
    return result


def parse_system_notice_feed(data: bytes) -> dict:
    f = ProtobufDecoder(data).read_all_fields()
    result = {"id": _s(f, 1), "content": _s(f, 4), "time": _i(f, 3)}
    if 2 in f:
        result["user"] = parse_simple_user_info(f[2][0][1])
    return result


def parse_feed_push(data: bytes) -> dict:
    f = ProtobufDecoder(data).read_all_fields()
    return {
        "displayWatchingCount": _s(f, 1),
        "displayLikeCount": _s(f, 2),
        "commentFeeds": [parse_comment_feed(v) for _, v in f.get(5, [])],
        "giftFeeds": [parse_gift_feed(v) for _, v in f.get(9, [])],
        "likeFeeds": [parse_like_feed(v) for _, v in f.get(8, [])],
        "systemNoticeFeeds": [parse_system_notice_feed(v) for _, v in f.get(11, [])],
    }


def parse_enter_room_ack(data: bytes) -> dict:
    f = ProtobufDecoder(data).read_all_fields()
    return {
        "minReconnectMs": _i(f, 1),
        "maxReconnectMs": _i(f, 2),
        "heartbeatIntervalMs": _i(f, 3) or 20000,
    }


def parse_watching_list(data: bytes) -> dict:
    f = ProtobufDecoder(data).read_all_fields()
    users = []
    for _, val in f.get(1, []):
        uf = ProtobufDecoder(val).read_all_fields()
        if 1 in uf:
            users.append(parse_simple_user_info(uf[1][0][1]))
    return {"displayWatchingCount": _s(f, 2), "watchingUsers": users}


def parse_error(data: bytes) -> dict:
    f = ProtobufDecoder(data).read_all_fields()
    return {"code": _i(f, 1), "msg": _s(f, 2)}


# ════════════════════════════════════════════════════════════
#  共享常量（URL host / API path / 默认超时）
# ════════════════════════════════════════════════════════════

logger = logging.getLogger(__name__)

_LIVE_HOST = "https://live.kuaishou.com"
_WSINFO_PATH_MARK = "/live_api/liveroom/websocketinfo"
_WSINFO_TIMEOUT_S = 60.0
_WSINFO_RESULT_OK = 1


# ── decode-error 兜底基础设施（与 douyin live_protocol 等价）──
_BAD_FRAME_DIR = Path.home() / ".crawlhub" / "bad_frames" / "kuaishou"
_BAD_FRAME_COUNTER: dict[str, int] = {}
_BAD_FRAME_LIMIT_PER_ROOM = 50
_MSG_LOG_BUCKETS: dict[tuple[str, str], dict[str, float]] = {}
_MSG_LOG_BURST_LIMIT = 5
_MSG_LOG_PERIOD_SECONDS = 60.0


def _log_kuaishou_decode_error(stage: str, raw: bytes, exc: BaseException) -> None:
    """单帧/单 message decode 失败 → WARN 日志（限频）。

    stage 例：'socket_message' / 'feed_push' / 'enter_room_ack' …
    """
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
            "[kuaishou.live] %s decode failed: size=%d head_hex=%s err=%s: %s",
            stage, len(raw), head_hex, err_class, exc,
        )


def _save_bad_frame_ks(principal_id: str, raw: bytes, *, exc: BaseException) -> None:
    """整帧 decode 失败 → 落盘 + WARN（同 douyin _save_bad_frame）。"""
    if not principal_id:
        principal_id = "unknown"
    n = _BAD_FRAME_COUNTER.get(principal_id, 0) + 1
    _BAD_FRAME_COUNTER[principal_id] = n
    head_hex = raw[:48].hex() if raw else ""
    saved_path: str | None = None
    if n <= _BAD_FRAME_LIMIT_PER_ROOM:
        try:
            room_dir = _BAD_FRAME_DIR / str(principal_id)
            room_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = room_dir / f"{ts}_{n:03d}_{type(exc).__name__}.bin"
            path.write_bytes(raw)
            meta = path.with_suffix(".txt")
            meta.write_text(
                f"principal_id={principal_id}\nsize={len(raw)}\nerror={type(exc).__name__}: {exc}\n",
                encoding="utf-8",
            )
            saved_path = str(path)
        except Exception:
            saved_path = None
    logger.warning(
        "[kuaishou.live] frame decode failed: principal_id=%s size=%d head_hex=%s "
        "error=%s: %s saved=%s (#%d in this room)",
        principal_id, len(raw), head_hex, type(exc).__name__, exc, saved_path or "<none>", n,
    )


# ════════════════════════════════════════════════════════════
#  Handover —— 浏览器 → Python WS 的唯一交棒契约
# ════════════════════════════════════════════════════════════



@dataclass
class LiveHandover:
    """从浏览器抠出来的 WebSocket 入场券。

    一旦这个 dataclass 填满，浏览器使命就完成了，剩下的全是 Python 的事。

    实测发现快手不通过 HTTP /websocketinfo 发签证，而是浏览器内 JS 直接：
      1. 建 wss://livejs-ws.kuaishou.cn/groupN
      2. 发 CS_ENTER_ROOM 首帧（含完整 token + liveStreamId 的 protobuf bytes）
    所以新策略是：
      - wss_url：浏览器实际建连的 wss URL（page.on('websocket') 抓）
      - enter_room_frame：浏览器发的 CS_ENTER_ROOM 首帧 bytes
        （page.on('framesent') 抓，可直接重放给 Python 自己的 ws）
      - live_stream_id / token / ws_urls：从首帧 protobuf 解析出来的便利字段
    """

    cookie_header: str
    user_agent: str
    principal_id: str = ""
    # ── 新模式（首选）：直接抓 wss URL + 首帧 bytes ──
    wss_url: str = ""
    enter_room_frame: bytes = b""
    # ── 兼容字段：从首帧解析出的便利值，或 HTTP 模式留下来的 ──
    live_stream_id: str = ""
    token: str = ""
    ws_urls: list[str] = field(default_factory=list)
    extras: dict = field(default_factory=dict)


# ════════════════════════════════════════════════════════════
#  Live API 签名捕获（hybrid：浏览器供签名 → Python 重放）
# ════════════════════════════════════════════════════════════
#
#  快手 live API 的两条签名：
#    - kww (header)    ：所有 /live_api/* 请求都带，**会话内可复用**
#    - __NS_hxfalcon (query)：仅敏感接口（gameboard/list 等）需要，**每次刷新会变**
#
#  策略：在 daemon-managed 浏览器里走一次 /live.kuaishou.com/cate/SYXX/<gameId>
#  让 SDK 主动发出一次 /live_api/gameboard/list 请求，page.route 拦截
#  抠 `kww` header + `__NS_hxfalcon` query + 完整 cookie + UA。
#  之后 Python httpx 复刻请求，只换 `gameId` / `page` / `keyword` 等业务参数。


@dataclass
class LiveApiSignature:
    """从浏览器抠出来的 live API 入场券。

    一次浏览器握手 = 一份签名 + 一段 cookie；后续多个 /live_api/* 接口
    都用同一份重放（kww 在会话内复用；hxfalcon 仅 gameboard/list 必要，
    其它接口可以省去 query 中的 hxfalcon）。
    """

    kww: str                    # 所有 /live_api/* 接口的 header（必填）
    hxfalcon: str               # gameboard/list 的 __NS_hxfalcon query（可空）
    cookie_header: str          # 完整 cookie header
    user_agent: str
    referer: str = "https://live.kuaishou.com/"
    extras: dict = field(default_factory=dict)


# 触发用：随便挑一个稳定存在的游戏区，让浏览器走过去触发 /live_api/* 请求
_DEFAULT_TRIGGER_GAME_ID = "22790"  # 逆战：未来（哥提供的 har 里就是这个）
_GAMEBOARD_LIST_PATH = "/live_api/gameboard/list"
_CATEGORY_DATA_PATH = "/live_api/category/data"
_CATEGORY_SEARCH_PATH = "/live_api/category/search"
_LIVE_API_TIMEOUT = 20.0

# 任何 /live_api/* 请求都带 kww header（实测一致）；
# 但只有少数请求带 __NS_hxfalcon query —— 它们是我们的"高价值目标"
_LIVE_API_PATH_MARK = "/live_api/"
_HXFALCON_QUERY_KEY = "__NS_hxfalcon"


async def _capture_live_signature_async(
    page_wrapper: Any,
    trigger_game_id: str = _DEFAULT_TRIGGER_GAME_ID,
    timeout_seconds: float = 60.0,
    require_hxfalcon: bool = True,
) -> "LiveApiSignature":
    """In a daemon-managed BrowserSession, navigate to /cate/SYXX/<gameId>,
    intercept any /live_api/* request and pull out the signing bits.

    ─────────────────────────────────────────────────────────────
    捕获策略（按优先级）：
      1. 任何 /live_api/* 请求 → 拿到 kww + cookie + ua（保底，几乎 100% 命中）
      2. 任何带 __NS_hxfalcon 的请求（如 /baseuser/userLogin 或 /gameboard/list）
         → 升级抢救 hxfalcon

    实测时序（来自哥提供的 HAR）：进入 /cate/SYXX/22790 后约 8-20s 内会触发
    userLogin（带 hxfalcon），而 /gameboard/list 要等到 30s+ 才发。所以
    **拦截范围越宽，抓签名越快**。等到任意 hxfalcon 到手就立即返回，
    不再等 gameboard/list。

    Args:
      require_hxfalcon: 是否必须等到 hxfalcon 才返回。
                       True  → list_category_live_rooms 用（gameboard/list 必需 hxfalcon）
                       False → list_live_categories / search_live_categories 用
                              （只需要 kww；如果 hxfalcon 没等到也能用）
    ─────────────────────────────────────────────────────────────
    """
    # R7: page_wrapper 是 PlaywrightPageWrapper（PageHandle.raw 给的），不再嵌套 _acquire_page
    captured: dict = {}
    target_url = f"{_LIVE_HOST}/cate/SYXX/{trigger_game_id}"
    page = page_wrapper.page
    if True:

        async def on_route(route):
            req = route.request
            url = req.url
            try:
                if _LIVE_API_PATH_MARK in url:
                    headers = await req.all_headers()
                    kww = headers.get("kww") or headers.get("Kww")
                    if kww:
                        # 保底：任何带 kww 的接口先收一份
                        if "kww" not in captured:
                            captured["kww"] = str(kww)
                            captured["headers"] = headers
                            captured["url"] = url
                        # 升级：任意带 hxfalcon 的请求覆盖一次（哪个先到用哪个）
                        if _HXFALCON_QUERY_KEY in url and "hxfalcon_url" not in captured:
                            captured["hxfalcon_url"] = url
                            captured["hxfalcon_headers"] = headers
            except Exception:
                pass
            await route.continue_()

        await page.route("**/*", on_route)
        try:
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as exc:
                # SPA 页面可能 domcontentloaded 也 timeout，但 SDK 已经开始发请求
                # 不致命，继续等抓签名
                pass

            # 进 cate 页后主动 scroll 一次，加速 SDK 触发 gameboard/list
            try:
                await page.evaluate(
                    "() => window.scrollBy(0, Math.max(800, window.innerHeight))"
                )
            except Exception:
                pass

            # 主动 fetch gameboard/list ——
            # 快手前端 JS SDK 会自动给 XHR/fetch 请求注入 __NS_hxfalcon 签名。
            # 我们在浏览器内 evaluate(fetch(...)) 触发一次请求，page.route 就能拦到
            # 带签名的 URL。这是最稳的"不依赖 SPA 自然触发"方案。
            try:
                await page.evaluate(
                    """
                    () => fetch(
                        '/live_api/gameboard/list?caver=2&filterType=0&gameId="""
                    + str(trigger_game_id)
                    + """&page=1&pageSize=1',
                        {credentials: 'include'}
                    ).catch(() => {})
                    """
                )
            except Exception:
                pass

            # 等 3s 让 route handler 收到被签名后的请求
            await page.wait_for_timeout(3000)



            start = time.monotonic()
            while time.monotonic() - start < timeout_seconds:
                # require_hxfalcon=True 时必须拿到 hxfalcon 才算成功；
                # require_hxfalcon=False 时拿到任意 kww 就早退
                got_kww = "kww" in captured
                got_hx = "hxfalcon_url" in captured
                if require_hxfalcon and got_hx:
                    break
                if not require_hxfalcon and got_kww:
                    # 给 hxfalcon 一个机会（再等 3s 看能不能升级签名）
                    extra_wait_end = time.monotonic() + 3.0
                    while time.monotonic() < extra_wait_end:
                        if got_hx:
                            break
                        await page.wait_for_timeout(200)
                        got_hx = "hxfalcon_url" in captured
                    break
                await page.wait_for_timeout(250)

            if "kww" not in captured:
                raise RuntimeError(
                    f"failed to capture kuaishou live API signature within "
                    f"{timeout_seconds}s (no /live_api/* request with kww header). "
                    f"target={target_url}"
                )
            if require_hxfalcon and "hxfalcon_url" not in captured:
                raise RuntimeError(
                    f"failed to capture kuaishou __NS_hxfalcon within "
                    f"{timeout_seconds}s (got kww but no hxfalcon; "
                    f"category-only signature is incomplete for gameboard/list). "
                    f"target={target_url}"
                )

            # 提取签名（优先用带 hxfalcon 的那一份 headers，因为它通常更新）
            req_url = str(captured.get("hxfalcon_url") or captured["url"])
            qs = parse_qs(urlparse(req_url).query)
            hxfalcon = (qs.get(_HXFALCON_QUERY_KEY) or [""])[0]
            req_headers = captured.get("hxfalcon_headers") or captured.get("headers") or {}
            kww = str(req_headers.get("kww") or req_headers.get("Kww") or captured["kww"])
            user_agent = str(req_headers.get("user-agent") or req_headers.get("User-Agent") or "")

            # 抠完整 cookie jar
            ctx = page.context
            try:
                cookies = await ctx.cookies()
            except Exception:
                cookies = []
            cookie_header = "; ".join(
                f"{c.get('name')}={c.get('value')}"
                for c in cookies
                if c.get("name") and c.get("value") is not None
            )
            if not user_agent:
                try:
                    user_agent = await page.evaluate("() => navigator.userAgent")
                except Exception:
                    user_agent = ""

            return LiveApiSignature(
                kww=kww,
                hxfalcon=hxfalcon,
                cookie_header=cookie_header,
                user_agent=str(user_agent or ""),
                referer=target_url,
                extras={"trigger_url": target_url, "captured_url": req_url,
                        "has_hxfalcon": bool(hxfalcon)},
            )
        finally:
            try:
                await page.unroute("**/*", on_route)
            except Exception:
                pass


def capture_live_signature(
    browser_session: Any,
    trigger_game_id: str = _DEFAULT_TRIGGER_GAME_ID,
    *,
    require_hxfalcon: bool = True,
    timeout_seconds: float = 60.0,
) -> LiveApiSignature:
    """Sync wrapper around :func:`_capture_live_signature_async`.

    Args:
      require_hxfalcon: True (default) for callers that need ``__NS_hxfalcon``
        (e.g. /gameboard/list). False for category-only callers
        (/category/data, /category/search) which only need ``kww``.
    """
    # R7: browser_session 是 PageHandle；.runner / .raw 是公开属性
    return browser_session.runner.run(
        _capture_live_signature_async(
            browser_session.raw, trigger_game_id,
            timeout_seconds=timeout_seconds,
            require_hxfalcon=require_hxfalcon,
        )
    )



# ────────────────────────────────────────────────────────────
#  Replay helpers (pure Python, no browser)
# ────────────────────────────────────────────────────────────


def _build_live_api_headers(sig: LiveApiSignature) -> dict[str, str]:
    """生产同款 headers：cookie + UA + kww + sec-ch-ua + sec-fetch-* + accept-*。

    缺一不可——少 sec-ch-ua / sec-fetch-* 时快手会回 200 + 空 list（软风控）。
    """
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "zh-CN,zh;q=0.9,fr;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
        "Host": "live.kuaishou.com",
        "Referer": sig.referer,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": sig.user_agent or "Mozilla/5.0",
        "kww": sig.kww,
        "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Cookie": sig.cookie_header,
    }


def _http_get_json(
    url: str,
    sig: LiveApiSignature,
    *,
    cookie_jar: Any,
    timeout: float = _LIVE_API_TIMEOUT,
) -> dict[str, Any]:
    """Replay a live API GET via curl_cffi（ja3 + IPv4 强制 + 身份头注入）.

    切勿改回 httpx —— 哪怕是 "h2 更标准" 之类的理由。
    历史血泪 2026-06-02 cfa680006e4f：httpx 用 OpenSSL 握手 + 带 SSO Cookie
    出网 = 服务端风控判定"会话被偷"，异步级联失效整个 SSO 域 token，账号
    在所有平台同时掉线。修复后必须从 http_factory 拿 session，统一 ja3 通道。

    R7 P5（2026-06-02）：cookie_jar 是必填，承载 BBA 抓到的 wire 身份头。
    metadata 缺失 → http_factory raise CookieMetadataMissing。
    """
    from .http_factory import make_session
    headers = _build_live_api_headers(sig)
    # 单次请求用临时 session（无状态调用；保持函数纯净）
    sess = make_session(cookie_jar=cookie_jar)
    resp = sess.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _slim_category_item(item: dict[str, Any]) -> dict[str, Any]:
    """从 /live_api/category/data 单个分类条目中抠核心字段。

    实测字段（HAR：逆战未来）：id / name / image / type 等；不同 type 字段可能略不同。
    """
    if not isinstance(item, dict):
        return {}
    # category/data 一项通常带 list（该分类的 top 直播间）
    rooms_raw = item.get("list") or []
    rooms_count = len(rooms_raw) if isinstance(rooms_raw, list) else 0
    return {
        "category_id": str(item.get("id") or item.get("gameId") or ""),
        "category_name": str(item.get("name") or item.get("categoryName") or ""),
        "icon_url": str(item.get("image") or item.get("icon") or item.get("poster") or ""),
        "category_type": int(item.get("type") or 0),
        "top_rooms_count": int(rooms_count),
    }


def _slim_category_search(item: dict[str, Any]) -> dict[str, Any]:
    """从 /live_api/category/search 单个搜索结果中抠核心字段。"""
    if not isinstance(item, dict):
        return {}
    return {
        "category_id": str(item.get("id") or item.get("gameId") or ""),
        "category_name": str(item.get("name") or item.get("categoryName") or ""),
        "icon_url": str(item.get("image") or item.get("icon") or ""),
        "category_type": int(item.get("type") or 0),
    }


def _parse_count(val) -> int:
    """快手返回的计数可能是 '1.1万' / '2.3w' / 纯数字 / None。"""
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip().lower()
    if not s:
        return 0
    try:
        return int(s)
    except ValueError:
        pass
    # 处理中文万/亿
    multiplier = 1
    if s.endswith("万") or s.endswith("w"):
        multiplier = 10000
        s = s[:-1]
    elif s.endswith("亿"):
        multiplier = 100000000
        s = s[:-1]
    try:
        return int(float(s) * multiplier)
    except (ValueError, TypeError):
        return 0


def _slim_gameboard_room(item: dict[str, Any], game_id: str) -> dict[str, Any]:

    """从 /live_api/gameboard/list 单个直播间条目中抠核心字段。"""
    if not isinstance(item, dict):
        return {}
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    game_info = item.get("gameInfo") if isinstance(item.get("gameInfo"), dict) else {}
    play_urls = item.get("playUrls") if isinstance(item.get("playUrls"), list) else []
    flv_url = ""
    if play_urls:
        first = play_urls[0]
        if isinstance(first, dict):
            adapt = first.get("adaptationSet") if isinstance(first.get("adaptationSet"), dict) else {}
            reps = adapt.get("representation") if isinstance(adapt.get("representation"), list) else []
            for rep in reps:
                if isinstance(rep, dict) and rep.get("url"):
                    flv_url = str(rep.get("url") or "")
                    break
    return {
        "live_stream_id": str(item.get("id") or ""),
        "principal_id": str(author.get("id") or ""),
        "author_name": str(author.get("name") or ""),
        "author_avatar": str(author.get("avatar") or ""),
        "title": str(item.get("caption") or ""),
        "cover_url": str(item.get("poster") or "") + ".jpg",
        "watching_count": _parse_count(item.get("watchingCount")),
        "like_count": _parse_count(item.get("likeCount")),
        "category_id": str(game_info.get("id") or game_id or ""),
        "category_name": str(game_info.get("name") or ""),
        "stream_flv": flv_url,
        "start_time": _parse_count(item.get("statrtTime") or item.get("startTime")),

    }


def fetch_category_data_page(
    sig: LiveApiSignature,
    page: int,
    *,
    cookie_jar: Any,
) -> dict[str, Any]:
    """GET /live_api/category/data?type=1&source=2&page=N&pageSize=12 (one page).

    Other query params (type/source/pageSize) are fixed per the captured curl.
    Returns the parsed JSON envelope unchanged so callers can detect
    list / hasMore / page meta.

    cookie_jar (R7 P5)：身份头来源；缺 metadata 会在 http_factory 层 raise。
    """
    url = f"{_LIVE_HOST}{_CATEGORY_DATA_PATH}?type=1&source=2&page={int(page)}&pageSize=12"
    return _http_get_json(url, sig, cookie_jar=cookie_jar)


def list_all_live_categories(
    sig: LiveApiSignature,
    *,
    cookie_jar: Any,
    is_cancelled: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Walk /live_api/category/data page by page until exhaustion.

    Stop conditions:
      - hasMore=False or list empty
      - no new (non-duplicate) items on a page
      - is_cancelled() returns True

    cookie_jar (R7 P5)：身份头来源，向下传给 fetch_category_data_page。
    """
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    page_no = 0
    while True:
        page_no += 1
        if is_cancelled and is_cancelled():
            break
        body = fetch_category_data_page(sig, page_no, cookie_jar=cookie_jar)
        data = body.get("data") if isinstance(body, dict) else None
        items = data.get("list") if isinstance(data, dict) else []
        if not items:
            break
        added = 0
        for it in items:
            slim = _slim_category_item(it)
            cid = slim.get("category_id") or ""
            if not cid or cid in seen_ids:
                continue
            seen_ids.add(cid)
            rows.append(slim)
            added += 1
        # hasMore=False / 空 list / 本页全是重复 → 终止
        if isinstance(data, dict) and data.get("hasMore") is False:
            break
        if added == 0:
            break
        # gentle pacing — kuaishou rate-limits aggressively
        time.sleep(0.4)
    return rows


def search_live_categories(
    sig: LiveApiSignature,
    keyword: str,
    *,
    cookie_jar: Any,
) -> list[dict[str, Any]]:
    """GET /live_api/category/search?keyword=... (single shot, no pagination).

    cookie_jar (R7 P5)：身份头来源；缺 metadata 会在 http_factory 层 raise。
    """
    if not keyword:
        return []
    url = f"{_LIVE_HOST}{_CATEGORY_SEARCH_PATH}?keyword={quote(str(keyword))}"
    body = _http_get_json(url, sig, cookie_jar=cookie_jar)
    data = body.get("data") if isinstance(body, dict) else None
    items = []
    if isinstance(data, dict):
        items = data.get("list") or data.get("categories") or []
    elif isinstance(data, list):
        items = data
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for it in items or []:
        slim = _slim_category_search(it)
        cid = slim.get("category_id") or ""
        if not cid or cid in seen_ids:
            continue
        seen_ids.add(cid)
        rows.append(slim)
    return rows


def list_category_live_rooms(
    sig: LiveApiSignature,
    category_id: str,
    *,
    cookie_jar: Any,
    max_results: int = 100,
    page_size: int = 20,
    is_cancelled: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """GET /live_api/gameboard/list?gameId=...&page=N&pageSize=20 (paged).

    `__NS_hxfalcon` and `caver=2` are required; gameId/page/pageSize/filterType
    are business params we control. Fetches up to ``max_results`` rooms.

    cookie_jar (R7 P5)：身份头来源；缺 metadata 会在 http_factory 层 raise。
    """
    if not category_id:
        return []
    if not sig.hxfalcon:
        raise RuntimeError(
            "list_category_live_rooms requires LiveApiSignature.hxfalcon "
            "(set by capture_live_signature when /gameboard/list is intercepted)"
        )
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    page_no = 1
    while len(rows) < max_results:
        if is_cancelled and is_cancelled():
            break
        url = (
            f"{_LIVE_HOST}{_GAMEBOARD_LIST_PATH}"
            f"?__NS_hxfalcon={sig.hxfalcon}"
            f"&caver=2&filterType=0"
            f"&gameId={quote(str(category_id))}"
            f"&page={page_no}&pageSize={int(page_size)}"
        )

        body = _http_get_json(url, sig, cookie_jar=cookie_jar)
        data = body.get("data") if isinstance(body, dict) else None
        items = data.get("list") if isinstance(data, dict) else []
        if not items:
            break
        for it in items:
            slim = _slim_gameboard_room(it, category_id)
            sid = slim.get("live_stream_id") or ""
            if not sid or sid in seen_ids:
                continue
            seen_ids.add(sid)
            rows.append(slim)
            if len(rows) >= max_results:
                break
        if isinstance(data, dict) and data.get("hasMore") is False:
            break
        page_no += 1
        time.sleep(0.4)
    return rows[:max_results]



# ════════════════════════════════════════════════════════════
#  浏览器侧 —— 截获 websocketinfo / room info
# ════════════════════════════════════════════════════════════






def parse_principal_id(value: str) -> str:
    """从 URL 或裸 ID 中提取 principal_id（用于构造 ``/u/<pid>``）."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("principal_id is empty")
    m = re.search(r"live\.kuaishou\.com/u/([^/?#]+)", raw)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_\-]+", raw):
        return raw
    raise ValueError(f"Cannot parse principal_id from {value!r}")


def _stream_id_from_url(url: str) -> str:
    qs = parse_qs(urlparse(url).query)
    return (qs.get("liveStreamId") or [""])[0]


async def _capture_handover_async(
    page_wrapper: Any,
    principal_id: str,
    timeout_seconds: float = _WSINFO_TIMEOUT_S,
) -> LiveHandover:
    """In a daemon-managed BrowserSession, open the live page and capture the
    real WebSocket URL + first CS_ENTER_ROOM frame from the browser's JS SDK.

    ─────────────────────────────────────────────────────────────────
    实测真相（2026-05-29）：
      快手并不走 HTTP ``/websocketinfo`` 接口签发 token。前端 JS 直接：
        1. 建 ``wss://livejs-ws.kuaishou.cn/groupN``（无 query）
        2. 立刻发 CS_ENTER_ROOM protobuf（含 token + liveStreamId）
      所以我们要拦的不是 HTTP，而是：
        - ``page.on('websocket')`` → 拿真实 wss URL
        - ``page.on('framesent')`` → 拿原汁原味的首帧 bytes
      然后 Python 直接重建 wss + 重放该首帧，跳过签名计算环节。

    历史坑（2026-05-29 撤销）：
      早期在隔离 minimal-args 测试中，sw.js 会让 fetch 全部 fail → 视频不播 →
      JS SDK 不建 WSS。当时给全局 launch args 加了 ``--disable-features=ServiceWorker``
      作为兜底。但该全局开关把抖音搜索页打废（任务 172e89dfa0bc 实证），
      已撤销。BBA persistent context 在叠加 stealth + cookie 注入 + IsolateOrigins
      关闭等条件后，实测 sw.js 能正常工作，不再需要禁用 SW。
      若快手未来再出现"建不了 WSS"，先复现 sw.js 错误再决定补救（推荐用
      page.route abort sw.js 而非全局禁用 SW）。
    ─────────────────────────────────────────────────────────────────
    """
    # R7: page_wrapper 是 PlaywrightPageWrapper（PageHandle.raw 给的），不再嵌套 _acquire_page
    target_url = f"{_LIVE_HOST}/u/{principal_id}"
    captured: dict[str, Any] = {}
    page = page_wrapper.page
    if True:

        def on_ws(ws):
            url = str(getattr(ws, "url", "") or "")
            # 只关心 livejs-ws 弹幕通道（quality / cdn / metric 等其他 wss 忽略）
            if "livejs-ws" not in url and "livejs.kuaishou.com" not in url:
                return
            if "wss_url" in captured:
                return
            captured["wss_url"] = url

            def on_frame_sent(payload):
                # 第一个 sent 帧就是 CS_ENTER_ROOM；后续是心跳，不要覆盖
                if "enter_room_frame" in captured:
                    return
                # payload 可能是 bytes 或 str（弹幕都是 binary，应该是 bytes）
                if isinstance(payload, str):
                    payload = payload.encode("utf-8")
                if not isinstance(payload, (bytes, bytearray)) or len(payload) < 30:
                    return
                captured["enter_room_frame"] = bytes(payload)

            ws.on("framesent", on_frame_sent)

        page.on("websocket", on_ws)
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
            start = time.monotonic()
            while time.monotonic() - start < timeout_seconds:
                if "wss_url" in captured and "enter_room_frame" in captured:
                    break
                await page.wait_for_timeout(250)

            if "wss_url" not in captured:
                raise RuntimeError(
                    f"failed to capture kuaishou wss URL within {timeout_seconds}s "
                    f"(target={target_url}). 排查：(1) cookie 是否过期/被风控；"
                    f"(2) 浏览器 console 是否有 sw.js fetch 失败 → 若有，"
                    f"在本函数 page.goto 前注入 page.route 阻断 sw.js（不要全局禁用 SW）"
                )
            if "enter_room_frame" not in captured:
                raise RuntimeError(
                    f"captured wss URL {captured['wss_url']} but no CS_ENTER_ROOM "
                    f"frame within {timeout_seconds}s (browser opened ws but didn't send enter)"
                )

            # 解析首帧 protobuf 抠出 token + liveStreamId（向后兼容字段）
            wss_url = str(captured["wss_url"])
            frame_bytes = captured["enter_room_frame"]
            token, live_stream_id = _parse_enter_room_frame(frame_bytes)

            # 抠完整 cookie jar：拼成 HTTP Cookie header
            ctx = page.context
            try:
                cookies = await ctx.cookies()
            except Exception:
                cookies = []
            cookie_header = "; ".join(
                f"{c.get('name')}={c.get('value')}"
                for c in cookies
                if c.get("name") and c.get("value") is not None
            )
            try:
                user_agent = await page.evaluate("() => navigator.userAgent")
            except Exception:
                user_agent = ""

            return LiveHandover(
                cookie_header=cookie_header,
                user_agent=str(user_agent or ""),
                principal_id=principal_id,
                wss_url=wss_url,
                enter_room_frame=frame_bytes,
                live_stream_id=live_stream_id,
                token=token,
                ws_urls=[wss_url],  # 单条 URL 以列表形式给老代码
                extras={
                    "target_url": target_url,
                    "frame_size": len(frame_bytes),
                    "wss_url_full": wss_url,
                },
            )
        finally:
            try:
                page.remove_listener("websocket", on_ws)
            except Exception:
                pass


def _parse_enter_room_frame(frame_bytes: bytes) -> tuple[str, str]:
    """从浏览器抓到的 CS_ENTER_ROOM 首帧 bytes 中抠 token + liveStreamId。

    Returns (token, live_stream_id)。任一字段解析失败时返回空字符串，
    不抛异常 —— 因为我们有 enter_room_frame 原始 bytes 兜底，不依赖这两个字段。
    """
    try:
        msg = decode_socket_message(frame_bytes)
        payload = decompress_payload(msg.get("compressionType", 0), msg.get("payload", b""))
        # CS_ENTER_ROOM payload 内部:
        #   field 1 (string): token
        #   field 2 (string): liveStreamId
        fields = ProtobufDecoder(payload).read_all_fields()
        token_bytes = (fields.get(1) or [(2, b"")])[0][1]
        stream_bytes = (fields.get(2) or [(2, b"")])[0][1]
        if isinstance(token_bytes, (bytes, bytearray)):
            token = token_bytes.decode("utf-8", errors="replace")
        else:
            token = ""
        if isinstance(stream_bytes, (bytes, bytearray)):
            stream_id = stream_bytes.decode("utf-8", errors="replace")
        else:
            stream_id = ""
        return token, stream_id
    except Exception:
        return "", ""


def capture_handover(browser_session: Any, principal_id: str) -> LiveHandover:
    """Sync wrapper around :func:`_capture_handover_async`.

    ``browser_session`` is a :class:`BrowserSessionHandle` exposing
    ``raw`` (the underlying ``BrowserSession``) and ``_runner``.
    """
    parsed = parse_principal_id(principal_id)
    # R7: browser_session 是 PageHandle；.runner / .raw 是公开属性
    return browser_session.runner.run(
        _capture_handover_async(browser_session.raw, parsed)
    )


# ════════════════════════════════════════════════════════════
#  Live room info —— 从浏览器侧 evaluate 拿首屏数据
# ════════════════════════════════════════════════════════════


async def _capture_room_info_async(
    page_wrapper: Any,
    principal_id: str,
    timeout_seconds: float = 30.0,
) -> dict:
    """Open the live page, then read ``__NUXT__.state.liveroom`` (first-screen
    SSR data injected by Kuaishou's frontend)."""
    # R7: page_wrapper 是 PlaywrightPageWrapper（PageHandle.raw 给的），不再嵌套 _acquire_page
    target_url = f"{_LIVE_HOST}/u/{principal_id}"
    page = page_wrapper.page
    if True:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(2000)
        # 不依赖 NUXT 全局结构（变化频繁），也尝试 evaluate 多个候选路径
        snapshot = await page.evaluate(
            """
            () => {
              const out = {url: location.href};
              try {
                const nux = window.__NUXT__ || window.__INITIAL_STATE__ || {};
                const rl = (nux.state && (nux.state.liveroom || nux.state.LiveRoom)) || nux.liveroom || {};
                out.nuxt = rl;
              } catch (e) { out.nuxt_error = String(e); }
              try {
                const titleEl = document.querySelector('h1') || document.querySelector('[class*="title"]');
                if (titleEl) out.title_dom = titleEl.innerText;
              } catch (e) {}
              try {
                out.title = document.title;
              } catch (e) {}
              return out;
            }
            """
        )
    return snapshot


def get_live_room_info(browser_session: Any, principal_id: str) -> dict:
    """Best-effort first-screen room info. The exact structure depends on
    Kuaishou's frontend SSR shape, so we return a slim dict with whatever
    we can extract."""
    parsed = parse_principal_id(principal_id)
    # R7: browser_session 是 PageHandle；.runner / .raw 是公开属性
    raw = browser_session.runner.run(
        _capture_room_info_async(browser_session.raw, parsed)
    )
    nuxt = raw.get("nuxt") or {}
    live_stream = nuxt.get("liveStream") if isinstance(nuxt, dict) else {}
    live_stream = live_stream if isinstance(live_stream, dict) else {}
    author = nuxt.get("author") if isinstance(nuxt, dict) else {}
    author = author if isinstance(author, dict) else {}
    return {
        "principal_id": parsed,
        "live_stream_id": str(live_stream.get("id") or ""),
        "title": str(live_stream.get("caption") or raw.get("title_dom") or raw.get("title") or ""),
        "author_id": str(author.get("id") or live_stream.get("user_id") or parsed),
        "author_name": str(author.get("name") or live_stream.get("user_name") or ""),
        "is_live": bool(live_stream.get("living") or live_stream.get("status") in (1, "LIVE", "live") or False),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_url": str(raw.get("url") or ""),
    }


# ════════════════════════════════════════════════════════════
#  Python WS 接管 —— collect_events
# ════════════════════════════════════════════════════════════

_HEARTBEAT_INTERVAL_S = 20.0

# SC_ERROR.code 直播结束（kuaishou 协议公开常量；收到此码 = 主播下播）
LIVE_END_ERROR_CODE = 60200

# 合成 cmd：实际协议层没有独立 LiveEnd 类型，是 SC_ERROR 60200 的语义化别名
LIVE_END_RAW_CMD = "SC_LIVE_END"


# ────────────────────────────────────────────────────────────
#  cmd → event 解析（细粒度 raw_cmd）
# ────────────────────────────────────────────────────────────


def _emit_feed_events(
    feed: dict,
    *,
    principal_id: str,
    live_stream_id: str,
    started: float,
    event_filter: set[str] | None,
    on_event: Callable[[dict[str, Any]], None],
) -> int:
    """从 SC_FEED_PUSH 解析结果中拆 commentFeeds / giftFeeds / likeFeeds /
    systemNoticeFeeds，每条都带细粒度 raw_cmd（SC_FEED_PUSH_COMMENT 等）。

    event_filter 在这里就生效，未勾选的子事件直接 drop。
    """
    n = 0
    online_str = feed.get("displayWatchingCount", "")
    like_str = feed.get("displayLikeCount", "")
    common = {
        "principal_id": principal_id,
        "live_stream_id": live_stream_id,
        "online_count_str": online_str,
        "like_count_str": like_str,
    }

    def _maybe_emit(raw_cmd: str, payload_dict: dict) -> None:
        nonlocal n
        if event_filter is not None and raw_cmd not in event_filter:
            return
        on_event({**payload_dict, "raw_cmd": raw_cmd})
        n += 1

    for c in feed.get("commentFeeds", []):
        u = c.get("user", {}) or {}
        _maybe_emit("SC_FEED_PUSH_COMMENT", {
            **common,
            "event_type": "chat",
            "uid": str(u.get("principalId") or ""),
            "nickname": str(u.get("userName") or ""),
            "content": str(c.get("content") or ""),
            "wealth_grade": int(c.get("wealth_grade") or 0),
            "level": int(c.get("level") or 0),
            "ts": round(time.monotonic() - started, 3),
            "payload": c,
        })

    for g in feed.get("giftFeeds", []):
        u = g.get("user", {}) or {}
        _maybe_emit("SC_FEED_PUSH_GIFT", {
            **common,
            "event_type": "gift",
            "uid": str(u.get("principalId") or ""),
            "nickname": str(u.get("userName") or ""),
            "gift_id": str(g.get("giftId") or ""),
            "gift_type": int(g.get("giftType") or 0),
            "gift_count": int(g.get("comboCount") or 0),
            "wealth_grade": int(g.get("wealth_grade") or 0),
            "level": int(g.get("level") or 0),
            "ts": round(time.monotonic() - started, 3),
            "payload": g,
        })

    for lk in feed.get("likeFeeds", []):
        u = lk.get("user", {}) or {}
        _maybe_emit("SC_FEED_PUSH_LIKE", {
            **common,
            "event_type": "like",
            "uid": str(u.get("principalId") or ""),
            "nickname": str(u.get("userName") or ""),
            "ts": round(time.monotonic() - started, 3),
            "payload": lk,
        })

    for note in feed.get("systemNoticeFeeds", []):
        _maybe_emit("SC_FEED_PUSH_SYSTEM_NOTICE", {
            **common,
            "event_type": "system_notice",
            "content": str(note.get("content") or ""),
            "ts": round(time.monotonic() - started, 3),
            "payload": note,
        })

    return n


def _payload_type_to_raw_cmd(pt: int) -> str:
    """把 PayloadType int 反查为 'SC_xxx' 名（用于 raw_cmd 标准化）."""
    for name in (
        "SC_LIVE_WATCHING_LIST", "SC_RED_PACK_FEED",
        "SC_GUESS_OPENED", "SC_GUESS_CLOSED",
        "SC_BET_CHANGED", "SC_BET_CLOSED",
        "SC_RIDE_CHANGED",
        "SC_LIVE_SPECIAL_ACCOUNT_CONFIG_STATE",
        "SC_LIVE_WARNING_MASK_STATUS_CHANGED_AUDIENCE",
        "SC_INTERACTIVE_CHAT_CLOSED",
        "SC_COMMENT_ZONE_RICH_TEXT",
        "SC_SHOW_FEED",
        "SC_ERROR", "SC_HEARTBEAT_ACK", "SC_ENTER_ROOM_ACK",
    ):
        if getattr(PayloadType, name, None) == pt:
            return name
    return f"SC_PAYLOAD_{pt}"


# 顶层 SC_* cmd 中"直接整段透传 payload"的简单类型（不需要细解码字段）。
_SC_PASSTHROUGH_CMDS: dict[int, tuple[str, str]] = {
    # payload_type: (raw_cmd, event_type)
    PayloadType.SC_RED_PACK_FEED: ("SC_RED_PACK_FEED", "red_pack"),
    PayloadType.SC_GUESS_OPENED: ("SC_GUESS_OPENED", "guess"),
    PayloadType.SC_GUESS_CLOSED: ("SC_GUESS_CLOSED", "guess"),
    PayloadType.SC_BET_CHANGED: ("SC_BET_CHANGED", "bet"),
    PayloadType.SC_BET_CLOSED: ("SC_BET_CLOSED", "bet"),
    PayloadType.SC_RIDE_CHANGED: ("SC_RIDE_CHANGED", "ride"),
    PayloadType.SC_COMMENT_ZONE_RICH_TEXT: ("SC_COMMENT_ZONE_RICH_TEXT", "rich_text"),
    PayloadType.SC_INTERACTIVE_CHAT_CLOSED: ("SC_INTERACTIVE_CHAT_CLOSED", "interactive_chat"),
    PayloadType.SC_LIVE_WARNING_MASK_STATUS_CHANGED_AUDIENCE: (
        "SC_LIVE_WARNING_MASK_STATUS_CHANGED", "live_warning",
    ),
}


# ────────────────────────────────────────────────────────────
#  collect_events_async / collect_events
# ────────────────────────────────────────────────────────────


async def collect_events_async(
    *,
    handover: LiveHandover,
    duration_seconds: float,
    on_event: Callable[[dict[str, Any]], None],
    is_cancelled: Callable[[], bool] | None = None,
    event_filter: set[str] | None = None,
) -> int:
    """Run the Kuaishou WS state machine to completion.

    Stop conditions (auto, no user toggle):
      1. duration_seconds reached
      2. is_cancelled() returns True
      3. WSS connection closed
      4. SC_ERROR with code=60200 (broadcaster ended live -> emit
         a synthetic SC_LIVE_END event then return)

    Args:
        handover: Bootstrap data from browser-side capture.
        duration_seconds: Hard timeout cap.
        on_event: Callback for each emitted event (already raw_cmd-tagged).
        is_cancelled: External cancellation hook.
        event_filter: If provided, only emit events whose ``raw_cmd`` is in
            this set. ``SC_LIVE_END`` is always evaluated for stop-detection
            regardless of filter (and emitted iff the filter accepts it).
    """
    # 优先用 ws_urls 列表（旧 API 兼容），没有时降级到单条 wss_url
    ws_urls_to_try = list(handover.ws_urls) if handover.ws_urls else (
        [handover.wss_url] if handover.wss_url else []
    )
    if not ws_urls_to_try:
        raise RuntimeError("handover has neither ws_urls nor wss_url")
    deadline = (time.monotonic() + float(duration_seconds)) if duration_seconds and float(duration_seconds) > 0 else float("inf")
    started = time.monotonic()
    count = 0
    headers = {
        "User-Agent": handover.user_agent or "Mozilla/5.0",
        "Origin": _LIVE_HOST,
        # ─────────────────────────────────────────────────────────────
        #  指纹对齐（2026-06-02）：删除 Cache-Control / Pragma: no-cache
        #  ──────────────────────────────────────────────────────────
        #  这两个 header 跟 wss 概念无关——真用户浏览器握手时不会带，
        #  反而被反爬当作"自动化客户端尝试绕缓存"的指纹。
        #  Accept-Language 用全局常量保持与 BrowserContext 一致。
        # ─────────────────────────────────────────────────────────────
        "Accept-Language": REAL_ACCEPT_LANGUAGE,
    }
    if handover.cookie_header:
        headers["Cookie"] = handover.cookie_header

    principal_id = handover.principal_id
    live_stream_id = handover.live_stream_id

    def _emit_top_level(raw_cmd: str, event: dict[str, Any]) -> int:
        """对顶层 cmd 应用 event_filter，返回 emit 计数（0 或 1）。"""
        event["raw_cmd"] = raw_cmd
        event.setdefault("principal_id", principal_id)
        event.setdefault("live_stream_id", live_stream_id)
        if event_filter is not None and raw_cmd not in event_filter:
            return 0
        on_event(event)
        return 1

    last_exc: Exception | None = None
    for ws_url in ws_urls_to_try:
        try:
            try:
                ws_cm = websockets.connect(
                    ws_url, additional_headers=headers,
                    ping_interval=None, close_timeout=3,
                )
            except TypeError:
                ws_cm = websockets.connect(
                    ws_url, extra_headers=headers,
                    ping_interval=None, close_timeout=3,
                )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
        async with ws_cm as ws:
            # 进房首帧：优先重放浏览器抓到的原始 bytes（最忠实，包含浏览器算的
            # token / liveStreamId / pageId），缺失时 fallback 到 Python 自建。
            if handover.enter_room_frame:
                await ws.send(handover.enter_room_frame)
            else:
                page_id = f"page_{int(time.time() * 1000) % 100000000:08x}"
                await ws.send(build_enter_room(
                    handover.token, handover.live_stream_id, page_id=page_id,
                ))

            heartbeat_interval = _HEARTBEAT_INTERVAL_S
            next_hb = time.monotonic() + heartbeat_interval

            while time.monotonic() < deadline:
                if is_cancelled and is_cancelled():
                    return count
                now = time.monotonic()
                if now >= next_hb:
                    try:
                        await ws.send(build_heartbeat())
                    except Exception:
                        break
                    next_hb = now + heartbeat_interval
                timeout = min(1.0, max(0.05, deadline - now))
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    break
                except Exception:
                    break

                if isinstance(raw, str):
                    raw = raw.encode("utf-8")

                try:
                    msg = decode_socket_message(raw)
                    payload_type = msg["payloadType"]
                    payload = decompress_payload(msg["compressionType"], msg["payload"])
                except Exception as e:
                    # 整帧 decode 失败：落盘 + 日志 + 继续 recv（不阻断 action）
                    _save_bad_frame_ks(principal_id, raw, exc=e)
                    continue

                # SC_ENTER_ROOM_ACK：调整心跳节奏
                if payload_type == PayloadType.SC_ENTER_ROOM_ACK:
                    ack = parse_enter_room_ack(payload)
                    interval_ms = int(ack.get("heartbeatIntervalMs") or 20000)
                    heartbeat_interval = max(5.0, interval_ms / 1000.0)
                    next_hb = time.monotonic() + heartbeat_interval
                    continue

                # SC_FEED_PUSH：拆 4 子类型，event_filter 在 _emit_feed_events 内生效
                if payload_type == PayloadType.SC_FEED_PUSH:
                    try:
                        feed = parse_feed_push(payload)
                    except Exception as e:
                        _log_kuaishou_decode_error("feed_push", payload, e)
                        continue
                    count += _emit_feed_events(
                        feed,
                        principal_id=principal_id,
                        live_stream_id=live_stream_id,
                        started=started,
                        event_filter=event_filter,
                        on_event=on_event,
                    )
                    continue

                # SC_LIVE_WATCHING_LIST：在线观众列表
                if payload_type == PayloadType.SC_LIVE_WATCHING_LIST:
                    w = parse_watching_list(payload)
                    count += _emit_top_level("SC_LIVE_WATCHING_LIST", {
                        "event_type": "room_stats",
                        "online_count": len(w.get("watchingUsers") or []),
                        "online_count_str": w.get("displayWatchingCount", ""),
                        "ts": round(time.monotonic() - started, 3),
                        "payload": w,
                    })
                    continue

                # SC_ERROR：60200 是下播信号；其它码作为常规错误
                if payload_type == PayloadType.SC_ERROR:
                    err = parse_error(payload)
                    code = int(err.get("code") or 0)
                    msg_text = str(err.get("msg") or "")
                    ts_now = round(time.monotonic() - started, 3)
                    if code == LIVE_END_ERROR_CODE:
                        # 协议定义的下播事件：合成 SC_LIVE_END，总是评估
                        live_end_event = {
                            "event_type": "live_end",
                            "principal_id": principal_id,
                            "live_stream_id": live_stream_id,
                            "raw_cmd": LIVE_END_RAW_CMD,
                            "error_code": code,
                            "content": msg_text or "live ended",
                            "ts": ts_now,
                            "payload": err,
                        }
                        if event_filter is None or LIVE_END_RAW_CMD in event_filter:
                            on_event(live_end_event)
                            count += 1
                        return count
                    # 非下播错误：走 SC_ERROR
                    count += _emit_top_level("SC_ERROR", {
                        "event_type": "error",
                        "error_code": code,
                        "content": msg_text,
                        "ts": ts_now,
                        "payload": err,
                    })
                    continue

                # SC_HEARTBEAT_ACK：静默吞
                if payload_type == PayloadType.SC_HEARTBEAT_ACK:
                    continue

                # SC_SHOW_FEED (pt=510)：进房 / 排行榜通知 / 流配置 / 广告
                # CRA cross-validated 2026-06-06:
                #   f28 = enter-room events (member join), f40 = ranking notifications
                if payload_type == PayloadType.SC_SHOW_FEED:
                    try:
                        show = parse_show_feed(payload)
                    except Exception as e:
                        _log_kuaishou_decode_error("show_feed", payload, e)
                        continue
                    ts_now = round(time.monotonic() - started, 3)
                    for enter in show.get("enterRoomEvents", []):
                        if event_filter is not None and "SC_SHOW_FEED_ENTER_ROOM" not in event_filter:
                            continue
                        on_event({
                            "raw_cmd": "SC_SHOW_FEED_ENTER_ROOM",
                            "event_type": "member",
                            "principal_id": principal_id,
                            "live_stream_id": live_stream_id,
                            # Note: uid is numeric userId (e.g. 422972243),
                            # different from principalId used in comment/gift feeds
                            "uid": enter.get("uid", ""),
                            "nickname": enter.get("nickname", ""),
                            "avatar_url": enter.get("avatar_url", ""),
                            "wealth_grade": enter.get("wealth_grade", 0),
                            "level": enter.get("level", 0),
                            "display_text": enter.get("display_text", ""),
                            "ts": ts_now,
                            "payload": enter,
                        })
                        count += 1
                    for rank in show.get("rankingNotifications", []):
                        if event_filter is not None and "SC_SHOW_FEED_RANKING" not in event_filter:
                            continue
                        on_event({
                            "raw_cmd": "SC_SHOW_FEED_RANKING",
                            "event_type": "ranking",
                            "principal_id": principal_id,
                            "live_stream_id": live_stream_id,
                            "content": rank.get("text", ""),
                            "username": rank.get("username", ""),
                            "ts": ts_now,
                            "payload": rank,
                        })
                        count += 1
                    continue

                # 顶层 SC_* 透传 cmd（红包/竞猜/投注/座驾/富文本/警告/聊天关闭等）
                if payload_type in _SC_PASSTHROUGH_CMDS:
                    raw_cmd, event_type = _SC_PASSTHROUGH_CMDS[payload_type]
                    count += _emit_top_level(raw_cmd, {
                        "event_type": event_type,
                        "ts": round(time.monotonic() - started, 3),
                        "payload": {"payload_type": payload_type, "size": len(payload)},
                    })
                    continue

                # UNKNOWN：仍带 raw_cmd（payload_type 反推），event_filter 默认 drop
                raw_cmd = _payload_type_to_raw_cmd(payload_type)
                count += _emit_top_level(raw_cmd, {
                    "event_type": "raw",
                    "ts": round(time.monotonic() - started, 3),
                    "payload": {"payload_type": payload_type, "size": len(payload)},
                })
            return count

    if last_exc is not None:
        raise last_exc
    return count


def collect_events(**kwargs: Any) -> int:
    return asyncio.run(collect_events_async(**kwargs))


__all__ = [
    "LiveHandover",
    "LiveApiSignature",
    "PayloadType",
    "CompressionType",
    "ProtobufEncoder",
    "ProtobufDecoder",
    "aes_cbc_decrypt",
    "decompress_payload",
    "encode_socket_message",
    "decode_socket_message",
    "build_enter_room",
    "build_heartbeat",
    "build_user_exit",
    "parse_feed_push",
    "parse_show_feed",
    "parse_enter_room_ack",
    "parse_watching_list",
    "parse_error",
    "parse_principal_id",
    "capture_handover",
    "capture_live_signature",
    "fetch_category_data_page",
    "list_all_live_categories",
    "search_live_categories",
    "list_category_live_rooms",
    "get_live_room_info",
    "collect_events",
    "collect_events_async",
    "LIVE_END_ERROR_CODE",
    "LIVE_END_RAW_CMD",
]

