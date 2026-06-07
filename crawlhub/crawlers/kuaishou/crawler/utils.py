"""
工具函数：URL 解析、文件加载等
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs


def _extract_photo_id_from_url(url: str) -> str:
    """Extract photo_id from a fully expanded URL (path or query param)."""

    # Path patterns: /short-video/{id}, /video/{id}, /long-video/{id}, /fw/photo/{id}
    m = re.search(r"/(?:short-video|video|long-video|fw/photo)/([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1)

    # Query param: photoId=xxx
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "photoId" in qs:
        return qs["photoId"][0]

    return ""


def parse_video_id(raw: str) -> str:
    """从 URL 或纯 ID 中提取 photo_id。

    支持格式：
      - 纯 ID：3xp334ga5a9z7wi
      - 完整链接：https://www.kuaishou.com/short-video/3xp334ga5a9z7wi
      - 带参数：https://www.kuaishou.com/short-video/3xp334ga5a9z7wi?fid=...
    """

    raw = raw.strip()
    if not raw:
        return ""

    # Standard URL
    pid = _extract_photo_id_from_url(raw)

    if pid:
        return pid

    # 纯 ID（字母数字，长度 8-30，无点号）
    if re.match(r"^[A-Za-z0-9_-]{8,30}$", raw) and "." not in raw:
        return raw

    # 其他 URL：取最后一段路径
    path = raw.rstrip("/").split("?")[0].split("/")[-1]
    if re.match(r"^[A-Za-z0-9_-]{8,30}$", path):
        return path

    return raw  # 原样返回，让 API 报错


def load_ids_from_file(filepath: str) -> list[str]:
    """从 txt 文件加载视频 ID / URL 列表（每行一个，# 开头为注释）。"""
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"输入文件不存在: {filepath}")

    ids = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ids.append(line)
    return ids
