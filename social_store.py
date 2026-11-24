"""Small, public social-source collectors for the local research library."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from xml.etree import ElementTree
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

LEMMY_COMMUNITIES = ("world", "china", "technology", "economics")


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text(value: str | None) -> str:
    parser = _PlainText()
    parser.feed(value or "")
    return re.sub(r"\s+", " ", unescape(" ".join(parser.parts))).strip()


def _get_json(url: str) -> object:
    request = Request(url, headers={"User-Agent": "Suyuan-Course-Demo/0.1 (local research library)",
                                    "Accept": "application/json"})
    with urlopen(request, timeout=7) as response:
        return json.loads(response.read(2_000_000).decode("utf-8"))


def hackernews(query: str, limit: int = 40) -> list[dict]:
    params = urlencode({"query": query, "tags": "story", "hitsPerPage": min(limit, 50)})
    payload = _get_json(f"https://hn.algolia.com/api/v1/search_by_date?{params}")
    observed = datetime.now(UTC).isoformat()
    posts = []
    for hit in payload.get("hits", []) if isinstance(payload, dict) else []:
        identifier = str(hit.get("objectID") or "")
        title = plain_text(hit.get("title") or hit.get("story_title"))
        content = plain_text(hit.get("story_text"))
        if not identifier or len(content) < 40:
            continue
        posts.append({"id": f"hn:{identifier}", "platform": "Hacker News", "source_query": query[:255],
                      "title": title, "content": (title + "\n" + content).strip(),
                      "url": f"https://news.ycombinator.com/item?id={quote(identifier)}",
                      "author": str(hit.get("author") or ""), "created_at": hit.get("created_at") or "",
                      "observed_at": observed, "language": "en", "likes": int(hit.get("points") or 0),
                      "reposts": 0, "replies": int(hit.get("num_comments") or 0)})
    return posts


def hackernews_comments(query: str, limit: int = 40) -> list[dict]:
    params = urlencode({"query": query, "tags": "comment", "hitsPerPage": min(limit, 50)})
    payload = _get_json(f"https://hn.algolia.com/api/v1/search_by_date?{params}")
    observed = datetime.now(UTC).isoformat()
    posts = []
    for hit in payload.get("hits", []) if isinstance(payload, dict) else []:
        identifier = str(hit.get("objectID") or "")
        content = plain_text(hit.get("comment_text"))
        if not identifier or len(content) < 40:
            continue
        posts.append({"id": f"hn:{identifier}", "platform": "Hacker News", "source_query": query[:255],
                      "title": "Comment on: " + plain_text(hit.get("story_title")), "content": content[:12000],
                      "url": f"https://news.ycombinator.com/item?id={quote(identifier)}",
                      "author": str(hit.get("author") or ""), "created_at": hit.get("created_at") or "",
                      "observed_at": observed, "language": "en", "likes": 0, "reposts": 0, "replies": 0})
    return posts


def mastodon_tag(tag: str, limit: int = 40) -> list[dict]:
    tag = tag.strip().lstrip("#")
    if not re.fullmatch(r"[\w\u3400-\u9fff]{2,60}", tag, re.UNICODE):
        raise ValueError("Mastodon 话题标签需为 2–60 个字母、数字、下划线或汉字。")
    payload = _get_json(f"https://mastodon.social/api/v1/timelines/tag/{quote(tag)}?limit={min(limit, 40)}")
    observed = datetime.now(UTC).isoformat()
    posts = []
    for status in payload if isinstance(payload, list) else []:
        original = status.get("reblog") or status
        identifier = str(original.get("id") or "")
        content = plain_text(original.get("content"))
        if not identifier or not content:
            continue
        account = original.get("account") or {}
        posts.append({"id": f"mastodon:{identifier}", "platform": "Mastodon (mastodon.social)",
                      "source_query": f"#{tag}", "title": "", "content": content[:12000],
                      "url": str(original.get("url") or ""), "author": str(account.get("acct") or ""),
                      "created_at": original.get("created_at") or "", "observed_at": observed,
                      "language": original.get("language") or "", "likes": int(original.get("favourites_count") or 0),
                      "reposts": int(original.get("reblogs_count") or 0), "replies": int(original.get("replies_count") or 0)})
    return posts


def lemmy_rss(community: str, query: str, limit: int = 20) -> list[dict]:
    """One bounded RSS request per public community, filtered locally by topic words."""
    if community not in LEMMY_COMMUNITIES:
        raise ValueError("未知 Lemmy 公开社区。")
    url = f"https://lemmy.world/feeds/c/{community}.xml?sort=New"
    request = Request(url, headers={"User-Agent": "Suyuan-Course-Demo/0.2 (local research library)",
                                    "Accept": "application/rss+xml, application/xml"})
    with urlopen(request, timeout=5) as response:
        payload = response.read(500_000)
    root = ElementTree.fromstring(payload)
    observed = datetime.now(UTC).isoformat()
    words = [word.casefold() for word in re.findall(r"[\w\u3400-\u9fff]{2,}", query)
             if word.casefold() not in {"and", "the", "for", "with", "about", "news"}]
    posts = []
    for item in root.findall("./channel/item")[:limit]:
        title = plain_text(item.findtext("title"))
        link = str(item.findtext("link") or "")
        if not link.startswith("https://lemmy.world/post/"):
            continue
        description = item.findtext("description") or ""
        body = re.split(r"<br\s*/?>", description, maxsplit=2, flags=re.I)[-1]
        body = re.sub(r"<blockquote\b[^>]*>.*?</blockquote>", " ", body, flags=re.I | re.S)
        body = re.sub(r"<a\b[^>]*>https?://.*?</a>", " ", body, flags=re.I | re.S)
        body = plain_text(body)[:12000]
        content = (title + "\n" + body).strip()
        if len(content) < 25 or (words and not any(word in content.casefold() for word in words)):
            continue
        author = item.findtext("{http://purl.org/dc/elements/1.1/}creator") or ""
        published = item.findtext("pubDate") or ""
        try:
            published = parsedate_to_datetime(published).astimezone(UTC).isoformat()
        except (TypeError, ValueError):
            published = ""
        posts.append({"id": f"lemmy:{link.rsplit('/', 1)[-1]}", "platform": f"Lemmy / {community}",
                      "source_query": query[:255], "title": title, "content": content,
                      "url": link, "author": author.rsplit("/", 1)[-1], "created_at": published,
                      "observed_at": observed, "language": "", "likes": 0, "reposts": 0, "replies": 0})
    return posts


def collect(query: str, tag: str = "") -> tuple[list[dict], list[str]]:
    query = query.strip()
    if len(query) < 3 or len(query) > 200:
        raise ValueError("社媒检索词需为 3–200 个字符。")
    posts: list[dict] = []
    warnings: list[str] = []
    if tag.strip() and not re.fullmatch(r"[\w\u3400-\u9fff]{2,60}", tag.strip().lstrip("#"), re.UNICODE):
        raise ValueError("Mastodon 话题标签需为 2–60 个字母、数字、下划线或汉字。")
    with ThreadPoolExecutor(max_workers=7) as pool:
        jobs = {pool.submit(hackernews, query, 20): "Hacker News 故事",
                pool.submit(hackernews_comments, query, 40): "Hacker News 评论"}
        jobs.update({pool.submit(lemmy_rss, community, query): f"Lemmy/{community}"
                     for community in LEMMY_COMMUNITIES})
        if tag.strip():
            jobs[pool.submit(mastodon_tag, tag)] = "Mastodon"
        for future in as_completed(jobs):
            try:
                posts.extend(future.result())
            except Exception as error:
                warnings.append(f"{jobs[future]} 暂时不可用（{type(error).__name__}）。")
    return posts, warnings
