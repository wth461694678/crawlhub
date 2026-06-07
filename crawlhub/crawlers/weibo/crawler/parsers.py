"""
Weibo HTML parsers — pure functions, no self-dependency.

All parsers operate on raw SSR HTML strings and return list[dict].
Lives at the crawler/ level (not _internal/) so scraper.py can import
them directly without violating C16 (R4 conformance).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

from .utils import strip_html


def _normalize_pub_time(raw: str) -> str:
    """Normalize weibo pub_time to 'YYYY-MM-DD HH:mm'.

    Handles:
      - '06月01日 08:59' → '2026-06-01 08:59'
      - '今天 20:24'     → today's date + 20:24
      - '5分钟前'        → now - 5 min
      - '3小时前'        → now - 3 hours
    Falls back to raw string if nothing matches.
    """
    now = datetime.now()
    today = now.date()

    # MM月DD日 HH:mm
    m = re.match(r'(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})', raw)
    if m:
        month, day, hour, minute = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        try:
            dt = datetime(today.year, month, day, hour, minute)
        except ValueError:
            return raw
        # If the date is in the future, it's probably from last year
        if dt.date() > today:
            dt = datetime(today.year - 1, month, day, hour, minute)
        return dt.strftime('%Y-%m-%d %H:%M')

    # 今天 HH:mm
    m = re.match(r'今天\s*(\d{1,2}):(\d{2})', raw)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        return datetime(today.year, today.month, today.day, hour, minute).strftime('%Y-%m-%d %H:%M')

    # N分钟前
    m = re.match(r'(\d+)分钟前', raw)
    if m:
        dt = now - timedelta(minutes=int(m.group(1)))
        return dt.strftime('%Y-%m-%d %H:%M')

    # N小时前
    m = re.match(r'(\d+)小时前', raw)
    if m:
        dt = now - timedelta(hours=int(m.group(1)))
        return dt.strftime('%Y-%m-%d %H:%M')

    return raw


# ============================================================
# A. Post cards (search results)
# ============================================================

def parse_post_cards(html: str, label: str = "") -> list[dict]:
    """Parse weibo post cards from search result HTML."""
    items = []
    cards = re.split(r'<div\s+class="card-wrap"\s+action-type="feed_list_item"', html)
    for card_html in cards[1:]:
        item = _parse_single_post(card_html, label)
        if item:
            items.append(item)
    return items


def _parse_single_post(card_html: str, label: str = "") -> Optional[dict]:
    """Parse a single weibo post card (SSR HTML)."""
    mid_match = re.search(r'mid="(\d+)"', card_html)
    if not mid_match:
        return None
    mid = mid_match.group(1)

    name_match = re.search(r'nick-name="([^"]+)"', card_html)
    user_name = name_match.group(1).strip() if name_match else ""

    uid_match = re.search(r'href="//weibo\.com/(\d+)\?refer_flag', card_html)
    uid = uid_match.group(1) if uid_match else ""

    txt_match = re.search(
        r'<p\s+node-type="feed_list_content"[^>]*class="txt"[^>]*>(.*?)</p>',
        card_html, re.DOTALL
    )
    if not txt_match:
        txt_match = re.search(r'<p[^>]*class="txt"[^>]*>(.*?)</p>', card_html, re.DOTALL)
    text = strip_html(txt_match.group(1).strip() if txt_match else "")

    from_match = re.search(r'class="from"[^>]*>(.*?)</div>', card_html, re.DOTALL)
    pub_time = source = ""
    if from_match:
        from_html = from_match.group(1)
        for pat in [r'>\s*([\d]+月[\d]+日\s*[\d:]+)\s*<',
                    r'>\s*(今天\s*[\d:]+)\s*<',
                    r'>\s*(\d+分钟前)\s*<',
                    r'>\s*(\d+小时前)\s*<']:
            m = re.search(pat, from_html)
            if m:
                pub_time = _normalize_pub_time(m.group(1).strip())
                break
        # Source: weibo wraps it in <a> tags now (e.g., 来自<a href="...">iPhone客户端</a>)
        # Strip HTML first, then extract the source text after "来自"
        from_text = strip_html(from_html)
        sm = re.search(r'来自\s*(.+)', from_text)
        source = sm.group(1).strip() if sm else ""

    # Engagement data — strip HTML comments first
    reposts = comments_count = likes = 0
    if 'card-act' in card_html:
        act = card_html[card_html.index('card-act'):]
        act = re.sub(r'<!--.*?-->', '', act, flags=re.DOTALL)
        lis = re.findall(r'<li[^>]*>(.*?)</li>', act, re.DOTALL)
        for i, li in enumerate(lis[:3]):
            lm = re.search(r'woo-like-count[^>]*>(\d+)<', li)
            if lm:
                num = int(lm.group(1))
            else:
                nm = re.search(r'[\s>](\d+)\s*</a>', li)
                num = int(nm.group(1)) if nm else 0
            if i == 0:
                reposts = num
            elif i == 1:
                comments_count = num
            elif i == 2:
                likes = num

    verified = ("是" if ('woo_svg_vblue' in card_html or 'woo_svg_vyellow' in card_html or
                              'icon-vip' in card_html or 'verified' in card_html.lower())
                else "否")

    topics = re.findall(r'#([^#]+)#', text)

    return {
        "source_type": label, "mid": mid, "user_name": user_name, "user_uid": uid,
        "verified": verified, "content": text[:500], "pub_time": pub_time, "source": source,
        "repost_count": reposts, "comment_count": comments_count, "like_count": likes,
        "topic_tags": "|".join(topics) if topics else "",
        "url": f"https://weibo.com/{uid}/{mid}" if uid else "",
    }


# ============================================================
# B. User cards (user search results)
# ============================================================

def parse_user_cards(html: str) -> list[dict]:
    """Parse user cards from user search result HTML."""
    users = []
    cards = re.split(r'<div\s+class="card\s+card-user-b', html)
    for card_html in cards[1:]:
        user = _parse_single_user(card_html)
        if user:
            users.append(user)
    return users


def _parse_single_user(card_html: str) -> Optional[dict]:
    """Parse a single user card (SSR HTML)."""
    uid_match = re.search(r'//weibo\.com/u/(\d+)', card_html)
    if not uid_match:
        uid_match = re.search(r'uid="(\d+)"', card_html)
    if not uid_match:
        return None
    uid = uid_match.group(1)

    name_match = re.search(r'class="name"[^>]*>([^<]+)</a>', card_html)
    user_name = name_match.group(1).strip() if name_match else ""

    has_blue_v = 'woo_svg_vblue' in card_html or 'woo-icon--vblue' in card_html
    has_yellow_v = 'woo_svg_vyellow' in card_html or 'woo-icon--vyellow' in card_html
    verified = has_blue_v or has_yellow_v
    v_type_html = "蓝V(机构)" if has_blue_v else ("黄V(个人)" if has_yellow_v else "未认证")

    info_match = re.search(r'class="info"[^>]*>(.*?)</div>\s*<div\s+class="btn"',
                           card_html, re.DOTALL)
    desc = ""
    fans = ""
    if info_match:
        info_html = info_match.group(1)
        ps = re.findall(r'<p[^>]*>(.*?)</p>', info_html, re.DOTALL)
        for p_content in ps:
            p_text = strip_html(p_content).strip()
            if "粉丝" in p_text:
                fans = p_text.replace("粉丝：", "").replace("粉丝:", "").strip()
            elif p_text and not desc:
                desc = p_text

    return {
        "uid": uid, "user_name": user_name, "verified": "是" if verified else "否",
        "verified_type": v_type_html, "description": desc, "followers_str": fans,
        "profile_url": f"https://weibo.com/u/{uid}",
    }


# ============================================================
# C. Topic cards (topic search results)
# ============================================================

def parse_topic_cards(html: str) -> list[dict]:
    """Parse topic cards from topic search result HTML."""
    topics = []
    cards = re.split(r'<div\s+class="card\s+card-direct-a\s+card-direct-topic"', html)

    for card_html in cards[1:]:
        name_match = re.search(r'class="name"[^>]*>([^<]+)</a>', card_html)
        topic_name = name_match.group(1).strip() if name_match else ""

        link_match = re.search(r'href="([^"]*)"', card_html)
        link = link_match.group(1) if link_match else ""
        if link.startswith("/"):
            link = "https://s.weibo.com" + link

        ps = re.findall(r'<p>(.*?)</p>', card_html, re.DOTALL)
        desc = strip_html(ps[0]).strip() if len(ps) > 0 else ""
        stats = strip_html(ps[1]).strip() if len(ps) > 1 else ""

        discuss = read_count = ""
        if stats:
            dm = re.search(r'([\d.]+万?)讨论', stats)
            discuss = dm.group(1) if dm else ""
            rm = re.search(r'([\d.]+[亿万]?)阅读', stats)
            read_count = rm.group(1) if rm else ""

        if topic_name:
            topics.append({
                "topic_name": topic_name, "description": desc, "discuss_count": discuss,
                "read_count": read_count, "stats_raw": stats, "url": link,
            })

    return topics
