"""溯源（Suyuan）本地演示服务。

这是一个本地课程原型：DeepSeek Search 负责发现，
DeepSeek Fetch 负责提取原站公开正文，Internet Archive 提供公开快照与可读正文回退。
"""

from __future__ import annotations

import json
import hashlib
import base64
import binascii
import ipaddress
import os
import re
import secrets
import shutil
import subprocess
import sys
import atexit
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import mysql_store
import analysis_store
import deepseek_store
import evidence as evidence_tools
import watch_store
import browser_imports
import rag_store
import social_store
import paper_store
import meltwater_source


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
MELTWATER_STUDIO_PROCESS: subprocess.Popen | None = None


def start_meltwater_studio() -> None:
    """Start the in-repository Node service with private data under data/."""
    global MELTWATER_STUDIO_PROCESS
    node = shutil.which("node")
    script = ROOT / "meltwater-studio" / "ui" / "server.mjs"
    if not node or not script.is_file():
        print("内置融文服务未启动：需要 Node.js 22+ 和 meltwater-studio/ui/server.mjs。")
        return
    data_root = DATA_DIR / "meltwater-studio"
    data_root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    studio_port = int(os.getenv("SUYUAN_MELTWATER_PORT", "8789"))
    env.update({"PORT": str(studio_port), "MW_STUDIO_DATA_ROOT": str(data_root),
                "SUYUAN_PARENT_PID": str(os.getpid())})
    log = (data_root / "studio.log").open("ab")
    try:
        MELTWATER_STUDIO_PROCESS = subprocess.Popen(
            [node, str(script)], cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        print(f"内置融文服务已启动：http://127.0.0.1:{studio_port}")
    except OSError as error:
        print(f"内置融文服务启动失败：{error}")
    finally:
        log.close()


def stop_meltwater_studio() -> None:
    if MELTWATER_STUDIO_PROCESS and MELTWATER_STUDIO_PROCESS.poll() is None:
        MELTWATER_STUDIO_PROCESS.terminate()


atexit.register(stop_meltwater_studio)
RECORDS_FILE = DATA_DIR / "records.json"
MAX_BODY_BYTES = 32_000
MAX_BROWSER_IMPORT_BYTES = 400_000
BROWSER_EXTENSION_ROUTES = {"/api/browser/import", "/api/browser/test", "/api/browser/paper"}
MIN_SAMPLE_TARGET = 30
MIN_RISK_POINTS = 4
MIN_EVIDENCE_PER_RISK = 4
ASSESSMENT_POLICY = """【Clear｜角色】你是涉华政经舆情风险研判员，只根据系统提供的原文证据分析，不把搜索摘要当事实，不推断未提供的传播范围。
【Complete｜任务】先归纳三至六项主要观点及媒体分歧，再形成四至六个相互可区分的风险点；逐点说明传播方式、支持与反向材料、跨语种语义变化、发生倾向与影响程度。每点至少关联四篇不同报道，报道可在不同风险点复用。
【Context｜输入】用户给出议题、时段；程序提供检索计划、报道来源与日期、证据等级、取文地址及 A1-P1 式原文段落。首发与主张变化无法确认时明确写未知。
【Constraints｜边界】只引用输入中真实存在且归属正确的文章与段落编号；优先独立长篇分析，排除纯通稿。公开快照与署名转载应注明实际取文来源。不得编造反证、把措辞差异当立场对立，或为凑满风险点虚构判断；材料不足就列出缺口。
【Concise｜输出】仅返回符合接口字段的 JSON：summary、risk_level、risk_points、viewpoints、actions、limitations。风险点须含支持与反向证据编号、翻译注记、两项一至五分评分及理由。"""
MAX_FETCH_CANDIDATES = 1200  # 12 languages × 2 queries × DeepSeek's maximum 50 results.
MAX_SELECTED_ARTICLES = 50
MIN_LONGFORM_CHARS = 3_000
MIN_CJK_LONGFORM_CHARS = 1_600
ACCEPTED_GENRES = {"深度报道", "专题分析", "署名评论", "社论", "长篇解读", "研究分析"}
PRIORITY_DOMAINS = (
    "reuters.com", "apnews.com", "moderndiplomacy.eu", "thediplomat.com",
    "foreignpolicy.com", "foreignaffairs.com", "ft.com", "bloomberg.com",
    "economist.com", "nytimes.com", "washingtonpost.com", "bbc.com",
    "dw.com", "lemonde.fr", "elpais.com", "spiegel.de", "nikkei.com",
)
SOURCE_NAMES = {"reuters.com": "Reuters", "apnews.com": "Associated Press",
                "moderndiplomacy.eu": "Modern Diplomacy", "thediplomat.com": "The Diplomat",
                "foreignpolicy.com": "Foreign Policy", "foreignaffairs.com": "Foreign Affairs"}
DEFAULT_LANGUAGES = ("en", "es", "de", "fr", "ru", "ar", "pt", "ko", "ja")
LANGUAGE_NAMES = {"en": "英语", "es": "西语", "de": "德语", "fr": "法语", "ko": "韩语", "ja": "日语", "zh": "中文", "ru": "俄语", "ar": "阿拉伯语", "pt": "葡语", "th": "泰语"}
LANGUAGE_ALIASES = {
    "英语": "en", "英文": "en", "english": "en", "en": "en",
    "西语": "es", "西班牙语": "es", "spanish": "es", "es": "es",
    "德语": "de", "德文": "de", "german": "de", "de": "de",
    "法语": "fr", "法文": "fr", "french": "fr", "fr": "fr",
    "韩语": "ko", "韩文": "ko", "korean": "ko", "ko": "ko",
    "日语": "ja", "日文": "ja", "japanese": "ja", "ja": "ja",
    "中文": "zh", "汉语": "zh", "chinese": "zh", "zh": "zh",
    "俄语": "ru", "俄文": "ru", "russian": "ru", "ru": "ru",
    "阿拉伯语": "ar", "阿语": "ar", "arabic": "ar", "ar": "ar",
    "葡语": "pt", "葡文": "pt", "葡萄牙语": "pt", "portuguese": "pt", "pt": "pt",
    "泰语": "th", "thai": "th", "th": "th",
}
DEPTH_TERMS = {"en": "analysis opinion editorial", "es": "análisis opinión editorial", "de": "Analyse Kommentar Leitartikel", "fr": "analyse opinion éditorial", "ko": "분석 사설 칼럼", "ja": "分析 社説 解説", "zh": "深度分析 评论 社论", "ru": "анализ мнение редакционная", "ar": "تحليل رأي افتتاحية", "pt": "análise opinião editorial", "th": "วิเคราะห์ บทบรรณาธิการ"}
NEWS_TERMS = {"en": "news report", "es": "noticias informe", "de": "Nachrichten Bericht", "fr": "actualités reportage", "ko": "뉴스 보도", "ja": "ニュース 報道", "zh": "新闻 报道", "ru": "новости репортаж", "ar": "أخبار تقرير", "pt": "notícias reportagem", "th": "ข่าว รายงาน"}
RECORDS_LOCK = threading.Lock()
JOBS_LOCK = threading.Lock()
JOBS: dict[str, dict[str, Any]] = {}
PAPER_JOBS_LOCK = threading.Lock()
PAPER_JOBS: dict[str, dict[str, Any]] = {}
MELTWATER_JOBS_LOCK = threading.Lock()
MELTWATER_JOBS: dict[str, dict[str, Any]] = {}
RAG_VECTOR_LOCK = threading.Lock()
RAG_VECTOR_JOB: dict[str, Any] = {"status": "idle", "done": 0, "total": 0, "message": ""}

PAYWALL_MARKERS = (
    "subscribe to continue",
    "subscription required",
    "sign in to continue",
    "create an account",
    "unlock this article",
    "purchase required",
    "请订阅",
    "登录后查看",
    "订阅后阅读",
)


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def load_local_env() -> dict[str, str]:
    """Read local configuration on demand so an edited API key takes effect."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            values[key] = value
    return values


def clean_text(value: Any, limit: int = 2_000) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def parse_model_object(content: str) -> dict[str, Any]:
    value = content.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I).strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        start, end = value.find("{"), value.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(value[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("模型结果须为 JSON 对象。")
    return parsed


def is_restricted_content(title: str, content: str) -> bool:
    candidate = f"{title}\n{content}".lower()
    return any(marker in candidate for marker in PAYWALL_MARKERS)


def safe_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return value


def search_languages(task: str, requested: str = "") -> list[str]:
    if requested.strip():
        tokens = re.split(r"[,，、;/\s]+", requested.strip().lower())
        languages = [LANGUAGE_ALIASES[token] for token in tokens if token in LANGUAGE_ALIASES]
        if not languages or len(languages) != len([token for token in tokens if token]):
            raise ApiError(400, "自定义语种请用中文名称或语言代码，以逗号分隔。")
    else:
        mentioned = [code for alias, code in LANGUAGE_ALIASES.items()
                     if alias in task.lower() and (len(alias) > 2 or not alias.isascii())]
        restricted = bool(re.search(r"(?:仅|只|限定|只检索|仅检索|only).{0,30}(?:语|文|english|spanish|japanese|korean|russian|arabic|portuguese)", task, re.I))
        languages = mentioned if restricted and mentioned else [*DEFAULT_LANGUAGES, *mentioned]
    unique = list(dict.fromkeys(languages))
    if len(unique) > 12:
        raise ApiError(400, "一次检索最多选择 12 种语言。")
    return unique


def parse_search_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def search_window(task: str, start_value: Any = None, end_value: Any = None) -> tuple[date, date]:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    if (start_value and not parse_search_date(start_value)) or (end_value and not parse_search_date(end_value)):
        raise ApiError(400, "日期请输入 YYYY-MM-DD 格式。")
    month = re.search(r"(20\d{2})\s*年\s*(1[0-2]|0?[1-9])\s*月", task)
    explicit_dates = re.findall(r"20\d{2}-\d{2}-\d{2}", task)
    if len(explicit_dates) >= 2 and not start_value and not end_value:
        start, end = parse_search_date(explicit_dates[0]), parse_search_date(explicit_dates[1])
    elif month and not start_value and not end_value:
        year, month_number = int(month.group(1)), int(month.group(2))
        start = date(year, month_number, 1)
        next_month = date(year + (month_number == 12), month_number % 12 + 1, 1)
        end = min(next_month - timedelta(days=1), today)
    else:
        end = parse_search_date(end_value) or today
        start = parse_search_date(start_value) or end - timedelta(days=39)
    if not start or not end or start > end or end > today or (end - start).days > 44:
        raise ApiError(400, "检索时段须为过去或当前连续不超过 45 天；请填写有效起止日期。")
    return start, end


def article_date(value: Any) -> date | None:
    match = re.search(r"20\d{2}-\d{2}-\d{2}", str(value or ""))
    return parse_search_date(match.group()) if match else None


def canonical_url(value: str) -> str:
    parsed = urlparse(value)
    query = sorted((key, part) for key, part in parse_qsl(parsed.query, keep_blank_values=True)
                   if not key.lower().startswith(("utm_", "fbclid", "gclid")))
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/", "", urlencode(query), ""))


def source_priority(url: str) -> int:
    host = (urlparse(url).hostname or "").lower()
    return 2 if any(host == domain or host.endswith(f".{domain}") for domain in PRIORITY_DOMAINS) else 1


def longform_length_met(article: dict[str, Any]) -> bool:
    content = article.get("content", "")
    compact = re.sub(r"\s+", "", content)
    language = article.get("search_language") or ""
    minimum = MIN_CJK_LONGFORM_CHARS if language in {"zh", "ja", "ko"} else MIN_LONGFORM_CHARS
    return len(compact) >= minimum


def obvious_syndicated_copy(article: dict[str, Any]) -> bool:
    """Exclude clearly attributed wire copies unless this record points to the wire original."""
    host = (urlparse(article["url"]).hostname or "").lower()
    if any(host == domain or host.endswith(f".{domain}") for domain in
           ("reuters.com", "apnews.com", "bloomberg.com", "afp.com")):
        return False
    lead = f"{article['title']} {article.get('content', '')[:1_000]}"
    return bool(re.search(
        r"(?i)\bby\s+(?:reuters|associated press|bloomberg|afp)\b|"
        r"\((?:reuters|ap|afp|bloomberg)\)|"
        r"\b(?:reuters|associated press|afp)\s*[-–]|"
        r"[（(](?:ブルームバーグ|ロイター|時事通信|共同通信)[）)]|"
        r"(?:로이터|AP/뉴시스|AFP)\s*[=/-]",
        lead,
    ))


def duplicate_article_id(article: dict[str, Any], accepted: list[dict[str, Any]]) -> str | None:
    """Catch same-story copies under different URLs, including lightly edited syndication."""
    title = re.sub(r"\W+", "", article["title"].casefold())
    body = re.sub(r"\W+", "", article["content"].casefold())[:4_000]
    for earlier in accepted:
        earlier_title = re.sub(r"\W+", "", earlier["title"].casefold())
        earlier_body = re.sub(r"\W+", "", earlier["content"].casefold())[:4_000]
        if canonical_url(article["url"]) == canonical_url(earlier["url"]):
            return earlier["id"]
        if body == earlier_body or body[:1_500] == earlier_body[:1_500]:
            return earlier["id"]
        title_similarity = SequenceMatcher(None, title, earlier_title, autojunk=False).ratio()
        if title_similarity < 0.78:
            continue
        body_similarity = SequenceMatcher(None, body, earlier_body).ratio()
        if body_similarity >= 0.82 or (title_similarity >= 0.96 and body_similarity >= 0.55):
            return earlier["id"]
    return None


class DeepSeekClient:
    """DeepSeek Search and Fetch; chat methods are reused by AnalysisClient."""

    def __init__(self) -> None:
        local_config = load_local_env()
        saved = deepseek_store.load_config() or {}
        self.api_key = (saved.get("api_key") or local_config.get("DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY", "")).strip()
        self.model = (saved.get("model") or local_config.get("DEEPSEEK_MODEL") or os.getenv("DEEPSEEK_MODEL", "deepseek-flash")).strip()
        self.reasoning_effort = saved.get("reasoning_effort", "auto")
        self.base_url = "https://api.deepseek.com"

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def post(self, path: str, payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
        if not self.configured:
            raise ApiError(409, "尚未配置 DeepSeek API Key。请打开配置中心填写。")
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Suyuan-Course-Demo/0.1",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise ApiError(error.code, f"DeepSeek 请求失败（HTTP {error.code}）：{detail}") from error
        except URLError as error:
            raise ApiError(502, f"无法连接 DeepSeek：{error.reason}") from error

    def plan_research(self, task: str, languages: list[str], start: date, end: date) -> tuple[dict[str, Any], str]:
        """Generate two native-language searches per required language."""
        language_list = ", ".join(f"{code}={LANGUAGE_NAMES.get(code, code)}" for code in languages)
        prompt = f"""你是多语种新闻检索规划器。围绕用户议题，为以下每种语言各写恰好两条该语言的检索词：{language_list}。
第一条偏长篇深度报道、专题解释与相关国家，第二条偏署名评论、社论、分析或风险叙事。优先定位原发的权威媒体和正式研究机构；不要生成纯消息通稿、聚合转载查询。不要写日期或 lang: 运算符，程序会添加。
只返回 JSON：{{"topic":"不超过80字的议题","queries_by_language":{{"en":["本地语言检索词1","本地语言检索词2"]}}}}。
queries_by_language 必须包含列出的每个语言代码，不能只用英语代替其他语言。不得建议规避付费墙。"""
        response_data: dict[str, Any] = {}
        model_ok = False
        try:
            response = self.post(
                "/chat/completions",
                {"model": self.model, "temperature": 0.2, "response_format": {"type": "json_object"},
                 "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": task}]},
            )
            response_data = parse_model_object(response["choices"][0]["message"]["content"])
            model_ok = isinstance(response_data, dict) and isinstance(response_data.get("queries_by_language"), dict)
        except (ApiError, KeyError, ValueError, TypeError, json.JSONDecodeError):
            response_data = {}
        if not isinstance(response_data, dict):
            response_data = {}
        queries_by_language = response_data.get("queries_by_language", {}) if model_ok else {}
        specs: list[dict[str, str]] = []
        fallback_languages: list[str] = []
        end_exclusive = end + timedelta(days=1)
        for language in languages:
            generated = queries_by_language.get(language, [])
            generated = generated if isinstance(generated, list) else []
            usable = [re.sub(r"\b(?:after|before|lang):\S+", "", clean_text(item, 75), flags=re.I).strip()
                      for item in generated if isinstance(item, str)]
            usable = list(dict.fromkeys(item for item in usable if item))[:2]
            if len(usable) < 2:
                fallback_languages.append(language)
                term = clean_text(task, 40)
                fallback = [f"{term} {NEWS_TERMS.get(language, 'news report')}",
                            f"{term} {DEPTH_TERMS.get(language, 'analysis opinion')}"]
                usable += fallback[len(usable):]
            for query in usable:
                specs.append({"language": language,
                              "query": clean_text(f"{query} lang:{language} after:{start.isoformat()} before:{end_exclusive.isoformat()}", 128)})
        plan = {
            "topic": clean_text(response_data.get("topic") or task, 80),
            "time_note": f"{start.isoformat()} 至 {end.isoformat()}（含首尾）",
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "profile": "standard", "target_count": MIN_SAMPLE_TARGET,
            "selection_policy": "representative_v2",
            "required_risk_points": MIN_RISK_POINTS,
            "min_evidence_per_risk": MIN_EVIDENCE_PER_RISK,
            "languages": languages, "query_specs": specs,
            "queries": [item["query"] for item in specs],
            "query_fallback_languages": fallback_languages,
            "risk_focus": [],
        }
        return plan, "多语种检索 · 模型规划" if model_ok and not fallback_languages else "多语种检索 · 部分降级"

    def search(self, query: str) -> list[dict[str, Any]]:
        for attempt in range(3):
            try:
                result = self.post("/v1/tools/search", {"query": query, "count": 50, "need_content": False}, timeout=30)
                return result.get("results", []) if isinstance(result.get("results", []), list) else []
            except ApiError as error:
                if error.status not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        return []

    def fetch(self, url: str) -> dict[str, Any]:
        for attempt in range(3):
            try:
                return self.post("/v1/tools/fetch", {"url": url}, timeout=30)
            except ApiError as error:
                if error.status not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        return {}

    def classify_articles(self, articles: list[dict[str, Any]], accepted: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Conservatively classify long-form originals and cross-language same-story copies."""
        if not articles:
            return {}
        context = [
            {"id": item["id"], "title": item["title"], "source": item["source"],
             "date": item.get("published_at"), "excerpt": item["content"][:300]}
            for item in accepted[-40:]
        ]
        candidates = [
            {"id": item["id"], "title": item["title"], "source": item["source"],
             "url": item["url"], "content_url": item.get("content_url"), "date": item.get("published_at"),
             "content": item["content"][:5_000]}
            for item in articles
        ]
        prompt = """你是新闻素材体裁与原发核验初筛员，只依据提供的网页内容判断。返回 JSON 对象：
{"items":[{"id":"A1","accepted":true,"genre":"深度报道","reason":"简短理由","duplicate_of":null}]}
accepted 只能在文章为独立撰写的长篇深度报道、专题分析、署名评论、社论、长篇解读或正式研究机构分析时为 true，genre 必须从“深度报道、专题分析、署名评论、社论、长篇解读、研究分析、通稿、短讯、转载聚合、其他”中选择。
排除通讯社纯消息稿、把别人的报道当成自己原创的转载或编译、聚合页、事件复述、新闻简报、营销文、缺少作者自身分析的材料。通讯社原创署名分析可以入选。若 url 是原始权威媒体页面、content_url 是明确署名原始媒体的公开转载或公开快照，可把它当作原发文章的一个取文版本；不得另算一篇。遇到无法判断原发或体裁时 accepted=false。
如果与“已入选材料”实为同一篇报道或译文，在 duplicate_of 填该材料 ID，且 accepted=false；不同观点讨论同一事件不算重复。不要为了凑数量放宽标准。"""
        try:
            response = self.post(
                "/chat/completions",
                {"model": self.model, "temperature": 0,
                 "response_format": {"type": "json_object"},
                 "messages": [{"role": "system", "content": prompt},
                              {"role": "user", "content": json.dumps({"accepted_context": context, "candidates": candidates}, ensure_ascii=False)}]},
                timeout=90,
            )
            parsed = parse_model_object(response["choices"][0]["message"]["content"])
            items = parsed.get("items", []) if isinstance(parsed, dict) else []
            allowed_ids = {item["id"] for item in articles}
            previous_ids = {item["id"] for item in accepted}
            candidate_order = {item["id"]: index for index, item in enumerate(articles)}
            decisions = {}
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict) or item.get("id") not in allowed_ids:
                    continue
                genre = clean_text(item.get("genre"), 20)
                target_id = item.get("duplicate_of")
                duplicate_of = target_id if (target_id in previous_ids or
                                              (target_id in candidate_order and
                                               candidate_order[target_id] < candidate_order[item["id"]])) else None
                decisions[item["id"]] = {
                    "accepted": item.get("accepted") is True and genre in ACCEPTED_GENRES,
                    "genre": genre,
                    "reason": clean_text(item.get("reason"), 120),
                    "duplicate_of": duplicate_of,
                }
            return decisions
        except (ApiError, KeyError, ValueError, TypeError, json.JSONDecodeError, TimeoutError) as error:
            print(f"Article classification failed: {type(error).__name__}")
            return {}

    def assess(self, task: str, plan: dict[str, Any], articles: list[dict[str, Any]]) -> dict[str, Any]:
        evidence = [
            {
                "id": article["id"],
                "title": article["title"],
                "url": article["url"],
                "date": article.get("published_at"),
                "source": article.get("source"),
                "source_priority": source_priority(article["url"]),
                "content_url": article.get("content_url"),
                "passages": article.get("passages") or evidence_tools.passages_for_article(article),
                "evidence_level": article["evidence_level"],
            }
            for article in articles
            if article["evidence_level"] in {"公开正文", "公开存档正文", "公开转载正文", "浏览器导入正文"}
        ]
        if len(evidence) < MIN_EVIDENCE_PER_RISK:
            return default_assessment("可用全文证据不足，系统仅保留传播线索，建议人工补充公开或授权资料。")

        prompt = ASSESSMENT_POLICY + "\n\n" + """输出严格 JSON：
{
  "summary": "100字以内，明确不确定性",
  "risk_level": "低|中|高",
  "risk_reason": "基于证据的理由",
  "risk_points": [{"heading":"最明显风险点的完整判断","analysis":"这一风险如何传播及证据依据","evidence_ids":["A1","A2","A3","A4"],"evidence_passage_ids":["A1-P1","A2-P2","A3-P1","A4-P3"],"counter_evidence_ids":[],"counter_passage_ids":[],"translation_note":"可核查的跨语种语义变化，否则留空","likelihood":3,"impact":4,"score_reason":"发生倾向与影响程度的依据"}],
  "viewpoints": [{"claim":"主要观点","difference":"媒体或语种之间的主要分歧","evidence_ids":["A1","A2"],"evidence_passage_ids":["A1-P1","A2-P2"]}],
  "actions": ["可执行建议"],
  "limitations": ["证据局限"]
}
请先归纳3到6个来自代表材料的主要观点及跨媒体分歧，再提炼至少4个、最多6个最明显且相互可区分的风险点。每个风险点至少引用4条不同报道的 evidence_ids，并从每篇报道给出的原文段落中各选至少一个真实的 evidence_passage_id；同一报道可以支撑多个风险点。确有反向材料时将编号放入 counter_evidence_ids，并在 counter_passage_ids 中给出对应原文段落，不得把措辞不同误作反证。不同语种原文若存在可指出的语义变化，写入 translation_note；无法确认则留空。likelihood 与 impact 按1至5分估计：1=很低、3=中等、5=很高；无法由材料判断时给出保守分数并在 score_reason 说明。它们是模型研判，不是测得的传播量。优先参考权威原发媒体与正式研究机构的独立分析，但不得把通讯社纯消息稿当作分析；快照或转载版本需注明实际取文来源。标题和分析必须对应所引段落，不得为凑数虚构判断。"""
        last = default_assessment("风险点证据不足，当前保留了可核验的采集材料。")
        for attempt in range(2):
            try:
                system_prompt = prompt if attempt == 0 else prompt + "\n上一次结果没有通过至少4个风险点、每点4条不同材料的校验。请重新分组，证据不足时如实说明。"
                response = self.post(
                    "/chat/completions",
                    {
                        "model": self.model, "temperature": 0.2,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": json.dumps({"task": task, "plan": plan, "evidence": evidence}, ensure_ascii=False)},
                        ],
                    },
                )
                raw = response["choices"][0]["message"]["content"]
                last = normalize_assessment(parse_model_object(raw), evidence)
                if len(last["risk_points"]) >= MIN_RISK_POINTS:
                    break
            except (ApiError, KeyError, ValueError, TypeError, json.JSONDecodeError):
                continue
        return last


class AnalysisClient(DeepSeekClient):
    """Send planning, source screening, and assessment to one configured chat API."""

    def __init__(self, config: dict[str, str] | None = None) -> None:
        saved = config if config is not None else analysis_store.load_config()
        if saved:
            self.format = saved["format"]
            self.base_url = saved["base_url"]
            self.api_key = saved["api_key"]
            self.model = saved["model"]
            self.reasoning_effort = saved.get("reasoning_effort", "auto")
            self.source = "自定义分析接口"
        else:
            deepseek = DeepSeekClient()
            self.format = "openai"
            self.base_url = "https://api.deepseek.com/chat/completions"
            self.api_key = deepseek.api_key
            self.model = deepseek.model
            self.reasoning_effort = deepseek.reasoning_effort if isinstance(deepseek.reasoning_effort, str) else "auto"
            self.source = "DeepSeek 默认分析"

    def post(self, path: str, payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
        if not self.configured:
            raise ApiError(409, "尚未配置分析接口；请在网页填写 OpenAI/Anthropic 兼容接口，或先配置 DeepSeek Key。")
        del path
        messages = payload.get("messages", [])
        if self.format == "anthropic":
            system = "\n".join(str(item.get("content", "")) for item in messages
                               if item.get("role") in {"system", "developer"})
            user_messages = [{"role": item["role"], "content": item["content"]}
                             for item in messages if item.get("role") in {"user", "assistant"}]
            body = {"model": self.model, "max_tokens": 4096, "system": system,
                    "messages": user_messages}
            if self.reasoning_effort != "auto" and self.source == "自定义分析接口":
                body["thinking"] = {"type": "adaptive"}
                body["output_config"] = {"effort": self.reasoning_effort}
                body["max_tokens"] = 16384
            headers = {"x-api-key": self.api_key, "Authorization": f"Bearer {self.api_key}",
                       "anthropic-version": "2023-06-01"}
        else:
            body = {"model": self.model, "messages": messages}
            if self.reasoning_effort != "auto":
                body["reasoning_effort"] = self.reasoning_effort
                if self.source == "DeepSeek 默认分析":
                    body["thinking"] = {"type": "enabled"}
            if self.source == "DeepSeek 默认分析":
                body.update({key: payload[key] for key in ("temperature", "response_format") if key in payload})
            headers = {"Authorization": f"Bearer {self.api_key}"}
        headers.update({"Content-Type": "application/json", "User-Agent": "Suyuan-Course-Demo/0.1"})
        request = Request(
            analysis_store.endpoint_url(self.format, self.base_url),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST", headers=headers,
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:400].replace(self.api_key, "[已隐藏]")
            raise ApiError(error.code, f"分析接口请求失败（HTTP {error.code}）：{detail}") from error
        except (URLError, TimeoutError) as error:
            raise ApiError(502, f"无法连接分析接口：{type(error).__name__}") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiError(502, "分析接口返回的内容不是有效 JSON。") from error
        if self.format == "anthropic":
            blocks = result.get("content", []) if isinstance(result, dict) else []
            content = "".join(block.get("text", "") for block in blocks
                              if isinstance(block, dict) and block.get("type") == "text")
        else:
            try:
                content = result["choices"][0]["message"]["content"]
            except (TypeError, KeyError, IndexError) as error:
                raise ApiError(502, "分析接口缺少消息正文。") from error
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not isinstance(content, str) or not content.strip():
            raise ApiError(502, "分析接口返回空消息。")
        return {"choices": [{"message": {"content": content}}]}

    def test_connection(self) -> None:
        self.post("/chat/completions", {"messages": [
            {"role": "system", "content": "请简短回答。"},
            {"role": "user", "content": "只回复 OK"},
        ]}, timeout=30)


def list_provider_models(format_name: str, base_url: str, api_key: str) -> list[dict[str, Any]]:
    """Query the provider's model catalogue; gateways may omit this endpoint."""
    url = "https://api.deepseek.com/models" if base_url == "https://api.deepseek.com" else analysis_store.models_url(format_name, base_url)
    headers = {"User-Agent": "Suyuan-Course-Demo/0.1"}
    if format_name == "anthropic":
        headers.update({"x-api-key": api_key, "Authorization": f"Bearer {api_key}",
                        "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with urlopen(Request(url, headers=headers), timeout=20) as response:
            payload = json.loads(response.read(1_000_000).decode("utf-8"))
    except HTTPError as error:
        raise ApiError(502, f"服务未提供可用的模型列表（HTTP {error.code}）；可以手填模型标识。") from error
    except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ApiError(502, "获取模型列表失败；请检查服务链接和 Key，或手填模型标识。") from error
    items = payload.get("data", payload.get("models", [])) if isinstance(payload, dict) else []
    if not isinstance(items, list):
        raise ApiError(502, "模型列表格式无法识别；可以手填模型标识。")
    models = []
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or item.get("name") or "").strip()
        if not model_id:
            continue
        levels = []
        if isinstance(item.get("effort"), dict):
            levels = [level for level in item["effort"].get("supported_levels", [])
                      if level in analysis_store.REASONING_EFFORTS]
        capabilities = item.get("capabilities")
        if not levels and isinstance(capabilities, dict) and isinstance(capabilities.get("effort"), dict):
            levels = [level for level, detail in capabilities["effort"].items()
                      if level in analysis_store.REASONING_EFFORTS
                      and isinstance(detail, dict) and detail.get("supported")]
        models.append({"id": model_id, "name": str(item.get("name") or item.get("display_name") or model_id),
                       "effort_levels": levels, "default_effort": item.get("effort", {}).get("default_level")
                       if isinstance(item.get("effort"), dict) else None})
    return sorted(models[:500], key=lambda item: item["id"].casefold())


def browser_library(*, include_content: bool = False) -> tuple[list[dict[str, Any]], str | None]:
    """Read durable SQL clips, merged with any local clips awaiting SQL sync."""
    local_items = browser_imports.list_imports(include_content=True)
    sql_items: list[dict[str, Any]] = []
    warning = None
    try:
        config = mysql_store.load_config()
        if config:
            sql_items = mysql_store.list_clips(config, include_content=True)
        else:
            warning = "尚未配置 SQL；浏览器文章已保存在本机，配置后会自动入库。"
    except Exception as error:
        print(f"Browser clip SQL read failed: {type(error).__name__}")
        warning = "SQL 暂不可读；当前显示本地保存的浏览器文章。"
    merged = {canonical_url(item["canonical_url"] or item["url"]): item for item in sql_items}
    for item in local_items:
        key = canonical_url(item["canonical_url"] or item["url"])
        earlier = merged.get(key)
        if not earlier or str(item.get("imported_at") or "") > str(earlier.get("imported_at") or ""):
            merged[key] = {**item, "stored_in_sql": False}
    items = sorted(merged.values(), key=lambda item: item.get("imported_at") or "", reverse=True)
    if not include_content:
        items = [{key: value for key, value in item.items() if key != "content"}
                 | {"content_length": len(item.get("content") or "")} for item in items]
    return items, warning


def default_assessment(note: str) -> dict[str, Any]:
    return {
        "summary": note,
        "risk_level": "待复核",
        "risk_reason": "未形成足够的可访问正文证据。",
        "risk_rubric": "",
        "risk_points": [],
        "viewpoints": [],
        "transmission_points": [],
        "narratives": [],
        "actions": ["补充公开或已获授权的来源后重新研判。"],
        "limitations": ["系统不会用受限页面或搜索摘要替代可核验正文。"],
    }


def normalize_assessment(value: Any, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return default_assessment("模型返回格式异常，未生成风险结论。")
    allowed_ids = {item["id"] for item in evidence}

    risk_points: list[dict[str, Any]] = []
    seen_headings: set[str] = set()
    for item in value.get("risk_points", [])[:6] if isinstance(value.get("risk_points"), list) else []:
        if not isinstance(item, dict):
            continue
        heading = clean_text(item.get("heading"), 70)
        raw_ids = item.get("evidence_ids", [])
        raw_ids = raw_ids if isinstance(raw_ids, list) else []
        ids = list(dict.fromkeys(entry for entry in raw_ids
                                 if isinstance(entry, str) and entry in allowed_ids))
        if not heading or heading in seen_headings or len(ids) < MIN_EVIDENCE_PER_RISK:
            continue
        passage_ids = evidence_tools.valid_passage_ids(item.get("evidence_passage_ids"), ids, evidence)
        if any(source.get("passages") for source in evidence):
            covered = {passage_id.split("-P", 1)[0] for passage_id in passage_ids}
            if len(covered) < MIN_EVIDENCE_PER_RISK:
                continue
        strength = evidence_tools.evidence_strength(ids, passage_ids, evidence, PRIORITY_DOMAINS)
        likelihood = item.get("likelihood")
        impact = item.get("impact")
        likelihood = likelihood if isinstance(likelihood, int) and not isinstance(likelihood, bool) and 1 <= likelihood <= 5 else None
        impact = impact if isinstance(impact, int) and not isinstance(impact, bool) and 1 <= impact <= 5 else None
        seen_headings.add(heading)
        raw_counter_ids = item.get("counter_evidence_ids")
        raw_counter_ids = raw_counter_ids if isinstance(raw_counter_ids, list) else []
        counter_ids = [entry for entry in raw_counter_ids
                       if isinstance(entry, str) and entry in allowed_ids and entry not in ids][:8]
        counter_passage_ids = evidence_tools.valid_passage_ids(item.get("counter_passage_ids"), counter_ids, evidence)
        risk_points.append({
            "heading": heading,
            "analysis": clean_text(item.get("analysis"), 400),
            "evidence_ids": ids,
            "evidence_passage_ids": passage_ids,
            "counter_evidence_ids": counter_ids,
            "counter_passage_ids": counter_passage_ids,
            "translation_note": clean_text(item.get("translation_note"), 180),
            "evidence_strength": strength,
            "likelihood": likelihood,
            "impact": impact,
            "risk_score": likelihood * impact if likelihood and impact else None,
            "score_reason": clean_text(item.get("score_reason"), 180),
        })

    def evidence_items(key: str) -> list[dict[str, Any]]:
        valid: list[dict[str, Any]] = []
        raw_items = value.get(key, [])
        for item in raw_items[:3] if isinstance(raw_items, list) else []:
            if not isinstance(item, dict):
                continue
            ids = [entry for entry in item.get("evidence_ids", []) if entry in allowed_ids]
            if ids:
                valid.append({
                    "label": clean_text(item.get("label"), 70),
                    "why": clean_text(item.get("why"), 180),
                    "stance": clean_text(item.get("stance"), 20),
                    "evidence_ids": ids,
                })
        return valid

    viewpoints = []
    for item in value.get("viewpoints", [])[:6] if isinstance(value.get("viewpoints"), list) else []:
        if not isinstance(item, dict):
            continue
        ids = list(dict.fromkeys(entry for entry in item.get("evidence_ids", [])
                                 if isinstance(entry, str) and entry in allowed_ids))
        passage_ids = evidence_tools.valid_passage_ids(item.get("evidence_passage_ids"), ids, evidence)
        claim = clean_text(item.get("claim"), 100)
        if claim and ids:
            viewpoints.append({"claim": claim, "difference": clean_text(item.get("difference"), 240),
                               "evidence_ids": ids, "evidence_passage_ids": passage_ids})

    risk_level = clean_text(value.get("risk_level"), 10)
    if risk_level not in {"低", "中", "高"}:
        risk_level = "待复核"
    scores = [point["risk_score"] for point in risk_points if point["risk_score"] is not None]
    risk_rubric = ""
    if scores:
        peak = max(scores)
        risk_level = "高" if peak >= 15 else "中" if peak >= 7 else "低"
        risk_rubric = f"按最高风险点估计分级：发生倾向×影响程度={peak}/25；1–6为低，7–14为中，15–25为高。分值为模型判断，非实测传播量。"
    return {
        "summary": clean_text(value.get("summary"), 200),
        "risk_level": risk_level,
        "risk_reason": clean_text(value.get("risk_reason"), 240),
        "risk_rubric": risk_rubric,
        "risk_points": risk_points,
        "viewpoints": viewpoints,
        "transmission_points": evidence_items("transmission_points"),
        "narratives": evidence_items("narratives"),
        "actions": [clean_text(item, 130) for item in value.get("actions", [])[:4] if clean_text(item, 130)],
        "limitations": [clean_text(item, 130) for item in value.get("limitations", [])[:4] if clean_text(item, 130)],
    }


def archive_snapshot(url: str) -> dict[str, str] | None:
    """Look up a public HTML snapshot; fetching text is a separate validated step."""
    query = urlencode([
        ("url", url),
        ("output", "json"),
        ("filter", "statuscode:200"),
        ("filter", "mimetype:text/html"),
        ("fl", "timestamp,original,statuscode"),
        ("limit", "-1"),
    ])
    request = Request(
        f"https://web.archive.org/cdx/search/cdx?{query}",
        headers={"User-Agent": "Suyuan-Course-Demo/0.1"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            rows = json.loads(response.read().decode("utf-8"))
        if len(rows) < 2 or len(rows[1]) < 2:
            return None
        timestamp, original = rows[1][0], rows[1][1]
        return {
            "captured_at": timestamp,
            "snapshot_url": f"https://web.archive.org/web/{timestamp}/{original}",
        }
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None


class ArticleHTMLParser(HTMLParser):
    """Extract readable text from a public archived HTML page."""

    IGNORED = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ignored_depth = 0
        self.article_depth = 0
        self.main_depth = 0
        self.body_depth = 0
        self.article_parts: list[str] = []
        self.main_parts: list[str] = []
        self.body_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.IGNORED:
            self.ignored_depth += 1
        if tag == "article":
            self.article_depth += 1
        elif tag == "main":
            self.main_depth += 1
        elif tag == "body":
            self.body_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.IGNORED:
            self.ignored_depth = max(0, self.ignored_depth - 1)
        if tag == "article":
            self.article_depth = max(0, self.article_depth - 1)
        elif tag == "main":
            self.main_depth = max(0, self.main_depth - 1)
        elif tag == "body":
            self.body_depth = max(0, self.body_depth - 1)

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if not value or self.ignored_depth:
            return
        if self.article_depth:
            self.article_parts.append(value)
        if self.main_depth:
            self.main_parts.append(value)
        if self.body_depth:
            self.body_parts.append(value)

    def text(self) -> str:
        for parts in (self.article_parts, self.main_parts, self.body_parts):
            result = clean_text(" ".join(parts), 12_000)
            if result:
                return result
        return ""


def public_archive_text(snapshot: dict[str, str]) -> str:
    """Read a public raw HTML snapshot; never send login credentials or cookies."""
    timestamp = snapshot["captured_at"]
    url = snapshot["snapshot_url"].replace(f"/{timestamp}/", f"/{timestamp}id_/", 1)
    request = Request(url, headers={"User-Agent": "Suyuan-Course-Demo/0.1"})
    with urlopen(request, timeout=12) as response:
        if "text/html" not in response.headers.get("Content-Type", "").lower():
            return ""
        charset = response.headers.get_content_charset() or "utf-8"
        html = response.read(2_000_000).decode(charset, errors="replace")
    parser = ArticleHTMLParser()
    parser.feed(html)
    return parser.text()


def add_archive_hint(article: dict[str, Any], reason: str, client: DeepSeekClient | None = None) -> None:
    """Try a public snapshot for priority sources; always preserve the provenance link."""
    try:
        snapshot = archive_snapshot(article["url"])
    except Exception as error:
        print(f"Archive lookup failed: {type(error).__name__}")
        snapshot = None
    if snapshot:
        article["evidence_level"] = "公开存档线索"
        article["archive"] = snapshot
        article["access_note"] = f"{reason}已找到公开历史快照链接；未抓取快照正文。"
        if client and source_priority(article["url"]) > 1:
            try:
                archived_content = public_archive_text(snapshot)
                if archived_content and not is_restricted_content(article["title"], archived_content):
                    article["content"] = archived_content
                    article["excerpt"] = archived_content[:700]
                    article["content_url"] = snapshot["snapshot_url"]
                    article["evidence_level"] = "公开存档正文"
                    article["access_note"] = f"{reason}从公开历史快照提取正文；原站与快照链接均已保留。"
            except Exception as error:
                print(f"Archive fetch failed: {type(error).__name__}")
    else:
        article["access_note"] = f"{reason}未找到可用的公开历史快照。"


def find_public_version(client: DeepSeekClient, article: dict[str, Any]) -> None:
    """Look for an attributed public copy of a priority publisher's original article."""
    host = (urlparse(article["url"]).hostname or "").lower()
    brand = next((name for domain, name in SOURCE_NAMES.items()
                  if host == domain or host.endswith(f".{domain}")), "")
    if not brand or len(article["title"]) < 15 or article["content"]:
        return
    query = f'"{article["title"][:110]}" "{brand}" -site:{host}'
    try:
        results = client.search(query)
    except Exception as error:
        print(f"Public copy search failed: {type(error).__name__}")
        return
    original_title = re.sub(r"\W+", "", article["title"].casefold())
    checked = 0
    for candidate in results:
        url = safe_url(str(candidate.get("url", "")))
        title = clean_text(candidate.get("title"), 200)
        if not url or canonical_url(url) == canonical_url(article["url"]):
            continue
        if SequenceMatcher(None, original_title, re.sub(r"\W+", "", title.casefold())).ratio() < 0.86:
            continue
        checked += 1
        if checked > 2:
            break
        try:
            fetched = client.fetch(url)
            content = clean_text(fetched.get("content"), 12_000) if isinstance(fetched, dict) else ""
        except Exception:
            continue
        if not content or is_restricted_content(title, content) or brand.casefold() not in content[:3_000].casefold():
            continue
        article["content"] = content
        article["excerpt"] = content[:700]
        article["content_url"] = url
        article["evidence_level"] = "公开转载正文"
        article["access_note"] = f"原站正文不可用；从注明 {brand} 原文的公开版本取文，已保留原站和实际取文链接。"
        return


def fetch_article(client: DeepSeekClient, candidate: dict[str, Any], position: int, language: str = "") -> dict[str, Any]:
    url = safe_url(str(candidate.get("url", "")))
    title = clean_text(candidate.get("title"), 200) or url
    article: dict[str, Any] = {
        "id": f"A{position}", "title": title, "url": url,
        "source": clean_text(candidate.get("site_name"), 100) or urlparse(url).netloc,
        "published_at": clean_text(candidate.get("date"), 64) or None,
        "excerpt": clean_text(candidate.get("summary") or candidate.get("snippet"), 700),
        "content": "", "evidence_level": "仅索引线索", "access_note": "尚未提取正文。",
        "rerank_score": candidate.get("rerank_score"), "authority": candidate.get("authority"),
        "search_queries": candidate.get("search_queries", []),
        "found_languages": candidate.get("found_languages", [language] if language else []),
        "content_url": url,
        "search_language": language or None,
    }
    try:
        fetched = client.fetch(url)
        if not isinstance(fetched, dict):
            fetched = {}
        fetched_title = clean_text(fetched.get("title"), 200)
        content = clean_text(fetched.get("content"), 40_000)
        article["published_at"] = clean_text(fetched.get("date") or article["published_at"], 64) or None
        if is_restricted_content(fetched_title or title, content):
            article["evidence_level"] = "访问受限"
            add_archive_hint(article, "原站显示订阅、登录或访问控制提示；", client)
            if article["evidence_level"] == "仅索引线索":
                article["evidence_level"] = "访问受限"
        elif content:
            article["title"] = fetched_title or title
            article["content"] = content
            article["excerpt"] = content[:700]
            article["evidence_level"] = "公开正文"
            article["access_note"] = "DeepSeek Fetch 成功提取公开正文。"
        else:
            add_archive_hint(article, "未获得可提取正文；", client)
    except ApiError as error:
        # Do not use an archive to work around access controls.
        if error.status in {401, 403}:
            article["evidence_level"] = "访问受限"
            add_archive_hint(article, "原站访问受限；", client)
            if article["evidence_level"] == "仅索引线索":
                article["evidence_level"] = "访问受限"
        elif error.status in {404, 408, 502, 504}:
            add_archive_hint(article, "原页面或提取接口暂不可用；", client)
        else:
            article["access_note"] = f"提取失败：HTTP {error.status}。"
    except TimeoutError:
        print("Article fetch failed: TimeoutError")
        add_archive_hint(article, "DeepSeek Fetch 请求超时；", client)
    except Exception as error:
        print(f"Article fetch failed: {type(error).__name__}")
        add_archive_hint(article, "正文提取接口返回异常；", client)
    if not article["content"] and source_priority(url) > 1:
        find_public_version(client, article)
    return article


def browser_article(candidate: dict[str, Any], position: int, language: str = "") -> dict[str, Any]:
    """Use only text the user confirmed in their own browser tab."""
    imported = candidate["_browser_import"]
    url = imported["url"]
    content = clean_text(imported["content"], browser_imports.MAX_CONTENT_CHARS)
    return {
        "id": f"A{position}", "title": imported["title"], "url": url,
        "source": imported["source"], "published_at": imported.get("published_at"),
        "excerpt": content[:700], "content": content,
        "evidence_level": "浏览器导入正文",
        "access_note": ("用户从本机浏览器导入清理后的正文；发布日期未知时须另行核对。"
                        if imported.get("method") in {"defuddle", "clean_dom", "page_structured_data"} else
                        "用户在本机浏览器确认可见正文后手动导入；来源、日期与使用权限仍需核对。"),
        "content_url": url, "browser_import_id": imported["id"],
        "rerank_score": candidate.get("rerank_score"), "authority": candidate.get("authority"),
        "search_queries": candidate.get("search_queries", []),
        "found_languages": candidate.get("found_languages", [language] if language else []),
        "search_language": imported.get("language") or language or None,
    }


def choose_representative(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Greedy coverage across sources, languages and dates, with search relevance retained."""
    remaining = list(articles)
    selected: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    language_counts: dict[str, int] = {}
    dates: set[str] = set()
    while remaining and len(selected) < MAX_SELECTED_ARTICLES:
        def rank(article: dict[str, Any]) -> float:
            host = (urlparse(article["url"]).hostname or "").lower()
            language = article.get("search_language") or ""
            published = str(article.get("published_at") or "")[:10]
            try:
                relevance = max(0.0, min(3.0, float(article.get("rerank_score") or 0)))
            except (TypeError, ValueError):
                relevance = 0
            return (relevance * 14 + source_priority(article["url"]) * 8
                    + min(len(article.get("content") or "") / 1_000, 8)
                    + (10 if host not in source_counts else -min(source_counts[host] * 5, 20))
                    + (8 if language_counts.get(language, 0) < 3 else 0)
                    + (4 if published and published not in dates else 0))
        best = max(remaining, key=rank)
        best["representative_score"] = round(rank(best), 1)
        selected.append(best)
        remaining.remove(best)
        host = (urlparse(best["url"]).hostname or "").lower()
        source_counts[host] = source_counts.get(host, 0) + 1
        language = best.get("search_language") or ""
        language_counts[language] = language_counts.get(language, 0) + 1
        if best.get("published_at"):
            dates.add(str(best["published_at"])[:10])
    return selected


def search_by_time_segments(client: DeepSeekClient, query: str, start: date, end: date,
                            on_segment: Callable[[str], None] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Split a date range whenever the Search response reaches its 50-result ceiling."""
    base = re.sub(r"\s+(?:after|before):\S+", "", query, flags=re.I).strip()
    pending = [(start, end)]
    found: list[dict[str, Any]] = []
    calls = 0
    errors = 0
    saturated_days: list[str] = []
    while pending:
        segment_start, segment_end = pending.pop(0)
        suffix = f" after:{segment_start.isoformat()} before:{(segment_end + timedelta(days=1)).isoformat()}"
        dated_query = f"{base[:128 - len(suffix)].rstrip()}{suffix}"
        if on_segment:
            on_segment(f"{segment_start.isoformat()}—{segment_end.isoformat()}")
        calls += 1
        try:
            results = client.search(dated_query)
        except ApiError:
            errors += 1
            continue
        found.extend(results)
        if len(results) >= 50:
            if segment_start < segment_end:
                middle = segment_start + timedelta(days=(segment_end - segment_start).days // 2)
                pending.extend([(segment_start, middle), (middle + timedelta(days=1), segment_end)])
            else:
                saturated_days.append(segment_start.isoformat())
    return found, {"search_calls": calls, "errors": errors, "saturated_days": saturated_days}


def collect_research_articles(client: DeepSeekClient, plan: dict[str, Any], progress: Callable[[Any], None],
                              analysis_client: AnalysisClient | None = None,
                              skip_urls: set[str] | None = None, id_offset: int = 0,
                              manual_imports: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    languages = list(plan["languages"])
    queues: dict[str, list[dict[str, Any]]] = {language: [] for language in languages}
    stats: dict[str, dict[str, Any]] = {
        language: {"language": language, "queries": 0, "search_calls": 0, "saturated_days": [],
                   "candidates": 0, "fetched": 0, "initial_pass": 0,
                   "excluded": 0, "duplicates": 0, "errors": 0}
        for language in languages
    }
    seen_urls: set[str] = set()
    by_url: dict[str, dict[str, Any]] = {}
    specs = plan["query_specs"]
    start = parse_search_date(plan["start_date"])
    end = parse_search_date(plan["end_date"])
    if not start or not end:
        raise ApiError(400, "检索时段无效。")
    search_results: dict[int, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    progress({"stage": "search", "percent": 5,
              "message": f"正在并行检索 {len(specs)} 组多语种检索式…", "done": 0, "total": len(specs)})
    with ThreadPoolExecutor(max_workers=min(4, len(specs))) as pool:
        jobs = {pool.submit(search_by_time_segments, client, spec["query"], start, end): index
                for index, spec in enumerate(specs)}
        for future in as_completed(jobs):
            index = jobs[future]
            search_results[index] = future.result()
            done = len(search_results)
            progress({"stage": "search", "percent": 5 + round(15 * done / len(specs)),
                      "message": f"多语种检索已完成 {done}/{len(specs)} 组（最新：{LANGUAGE_NAMES.get(specs[index]['language'], specs[index]['language'])}）",
                      "done": done, "total": len(specs)})
    for index, spec in enumerate(specs):
        language = spec["language"]
        stats[language]["queries"] += 1
        results, search_stats = search_results[index]
        stats[language]["search_calls"] += search_stats["search_calls"]
        stats[language]["errors"] += search_stats["errors"]
        stats[language]["saturated_days"].extend(search_stats["saturated_days"])
        for item in results:
            url = safe_url(str(item.get("url", "")))
            if not url:
                continue
            canonical = canonical_url(url)
            if canonical in by_url:
                earlier = by_url[canonical]
                if spec["query"] not in earlier["search_queries"]:
                    earlier["search_queries"].append(spec["query"])
                if language not in earlier["found_languages"]:
                    earlier["found_languages"].append(language)
                continue
            if canonical in (skip_urls or set()):
                continue
            candidate = {**item, "search_queries": [spec["query"]], "found_languages": [language]}
            by_url[canonical] = candidate
            seen_urls.add(canonical)
            queues[language].append(candidate)
            stats[language]["candidates"] += 1
    for imported in manual_imports or []:
        canonical = canonical_url(imported["url"])
        if canonical in (skip_urls or set()):
            continue
        language = imported.get("language") if imported.get("language") in LANGUAGE_NAMES else languages[0]
        if language not in queues:
            languages.append(language)
            queues[language] = []
            stats[language] = {"language": language, "queries": 0, "search_calls": 0, "saturated_days": [],
                               "candidates": 0, "fetched": 0, "initial_pass": 0,
                               "excluded": 0, "duplicates": 0, "errors": 0}
        if canonical in by_url:
            by_url[canonical]["_browser_import"] = imported
        else:
            candidate = {"url": imported["url"], "title": imported["title"],
                         "date": imported.get("published_at"), "site_name": imported.get("source"),
                         "search_queries": [], "found_languages": [language], "_browser_import": imported}
            by_url[canonical] = candidate
            seen_urls.add(canonical)
            queues[language].append(candidate)
            stats[language]["candidates"] += 1
    plan["languages"] = languages
    for queue in queues.values():
        queue.sort(key=lambda item: source_priority(str(item.get("url", ""))), reverse=True)
    candidates: list[tuple[str, dict[str, Any]]] = []
    while any(queues.values()):
        for language in languages:
            if queues[language]:
                candidates.append((language, queues[language].pop(0)))
    articles: list[dict[str, Any]] = []
    all_articles: list[dict[str, Any]] = []
    archive_hints: list[dict[str, Any]] = []
    exclusion_counts: dict[str, int] = {}
    duplicate_count = 0
    processed = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        for offset in range(0, len(candidates), 8):
            batch = candidates[offset:offset + 8]
            futures = {}
            for position, (language, candidate) in enumerate(batch):
                article_position = id_offset + offset + position + 1
                if candidate.get("_browser_import"):
                    future = pool.submit(browser_article, candidate, article_position, language)
                else:
                    future = pool.submit(fetch_article, client, candidate, article_position, language)
                futures[future] = (language, article_position)
            batch_articles: list[tuple[int, str, dict[str, Any]]] = []
            for future in as_completed(futures):
                language, position = futures[future]
                article = future.result()
                processed += 1
                stats[language]["fetched"] += 1
                batch_articles.append((position, language, article))
                all_articles.append(article)
                progress({"stage": "fetch", "percent": 20 + round(55 * processed / max(1, len(candidates))),
                          "message": f"提取正文 {processed}/{len(candidates)}；已入选 {len(articles)}/{MIN_SAMPLE_TARGET}",
                          "done": processed, "total": len(candidates), "qualified": len(articles),
                          "duplicates": duplicate_count, "excluded": sum(exclusion_counts.values())})
            eligible: list[dict[str, Any]] = []
            for _position, language, article in sorted(batch_articles):
                published = article_date(article.get("published_at"))
                article["within_window"] = bool(published and start <= published <= end)
                if not published:
                    article["sample_status"] = "日期待核验"
                elif not article["within_window"]:
                    article["sample_status"] = "时段外"
                elif article["evidence_level"] not in {"公开正文", "公开存档正文", "公开转载正文", "浏览器导入正文"}:
                    article["sample_status"] = "正文不可用"
                elif not longform_length_met(article):
                    article["sample_status"] = "非长篇"
                elif obvious_syndicated_copy(article):
                    article["sample_status"] = "转载排除"
                else:
                    eligible.append(article)
                    continue
                stats[language]["excluded"] += 1
                exclusion_counts[article["sample_status"]] = exclusion_counts.get(article["sample_status"], 0) + 1
                if article.get("archive"):
                    archive_hints.append({key: article.get(key) for key in
                                          ("id", "title", "url", "source", "archive", "access_note")})
            progress({"stage": "screen", "percent": 20 + round(55 * processed / max(1, len(candidates))),
                      "message": f"筛选长篇原创材料：本批 {len(eligible)} 篇待判断，已入选 {len(articles)}/{MIN_SAMPLE_TARGET}",
                      "done": processed, "total": len(candidates), "qualified": len(articles),
                      "duplicates": duplicate_count, "excluded": sum(exclusion_counts.values())})
            decisions = (analysis_client or client).classify_articles(eligible, articles)
            for article in eligible:
                language = article["search_language"]
                decision = decisions.get(article["id"])
                article["genre"] = decision.get("genre") if decision else None
                model_duplicate = (decision or {}).get("duplicate_of")
                duplicate_of = model_duplicate if any(item["id"] == model_duplicate for item in articles) else None
                duplicate_of = duplicate_of or duplicate_article_id(article, articles)
                if duplicate_of:
                    article["sample_status"] = "同稿去重"
                    duplicate_count += 1
                    stats[language]["duplicates"] += 1
                elif decision and decision["accepted"]:
                    article["sample_status"] = "初筛合格"
                    article["passages"] = evidence_tools.passages_for_article(article)
                    articles.append(article)
                    stats[language]["initial_pass"] += 1
                else:
                    article["sample_status"] = "体裁或原发不合格" if decision else "体裁待核验"
                    stats[language]["excluded"] += 1
                    exclusion_counts[article["sample_status"]] = exclusion_counts.get(article["sample_status"], 0) + 1
            progress({"stage": "screen", "percent": 20 + round(55 * processed / max(1, len(candidates))),
                      "message": f"已入选 {len(articles)}/{MIN_SAMPLE_TARGET} 篇独立长篇材料；同稿 {duplicate_count} 篇",
                      "done": processed, "total": len(candidates), "qualified": len(articles),
                      "duplicates": duplicate_count, "excluded": sum(exclusion_counts.values())})
    selected = choose_representative(articles)
    selected_ids = {article["id"] for article in selected}
    for article in articles:
        if article["id"] not in selected_ids:
            article["sample_status"] = "合格未入选"
    for rank, article in enumerate(selected, start=1):
        article["selection_rank"] = rank
    for language in languages:
        stats[language]["initial_pass"] = sum(1 for article in selected if article.get("search_language") == language)
    plan["archive_hints"] = archive_hints
    plan["candidate_articles"] = all_articles
    plan["coverage"] = {
        "target": MIN_SAMPLE_TARGET, "selected_max": MAX_SELECTED_ARTICLES,
        "initial_pass": len(selected), "eligible_count": len(articles),
        "gap": max(0, MIN_SAMPLE_TARGET - len(selected)),
        "candidate_count": len(seen_urls), "fetched_count": processed,
        "duplicate_count": duplicate_count, "excluded_count": sum(exclusion_counts.values()),
        "search_calls": sum(item["search_calls"] for item in stats.values()),
        "saturated_days": sorted(set(day for item in stats.values() for day in item["saturated_days"])),
        "exclusion_counts": exclusion_counts, "archive_hint_count": len(archive_hints),
        "status": "代表材料数量达标，待提炼观点与风险点" if len(selected) >= MIN_SAMPLE_TARGET else "代表材料不足，待补",
        "languages": [stats[language] for language in languages],
    }
    return selected


def record_run(run: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with RECORDS_LOCK:
        records: list[dict[str, Any]] = []
        if RECORDS_FILE.exists():
            try:
                records = json.loads(RECORDS_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                records = []
        records.insert(0, run)
        temporary = RECORDS_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(records[:30], ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(RECORDS_FILE)


def public_run(run: dict[str, Any]) -> dict[str, Any]:
    visible = dict(run)
    for key in ("articles", "candidates"):
        visible[key] = []
        for article in run.get(key, []):
            copy = dict(article)
            copy.pop("content", None)
            visible[key].append(copy)
    return visible


def local_records(limit: int = 20) -> list[dict[str, Any]]:
    if not RECORDS_FILE.exists():
        return []
    with RECORDS_LOCK:
        try:
            records = json.loads(RECORDS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return [public_run(item) for item in records[:limit]] if isinstance(records, list) else []


def stored_run(run_id: str) -> dict[str, Any] | None:
    config = mysql_store.load_config()
    if config:
        try:
            found = mysql_store.get_run(config, run_id, include_content=True)
            if found:
                return found
        except Exception as error:
            print(f"MySQL baseline read failed: {type(error).__name__}")
    if RECORDS_FILE.exists():
        with RECORDS_LOCK:
            try:
                runs = json.loads(RECORDS_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
        return next((run for run in runs if run.get("id") == run_id), None)
    return None


def execute_research(payload: dict[str, Any], progress: Callable[[Any], None]) -> dict[str, Any]:
    watch_id = clean_text(payload.get("watch_id"), 32)
    watch = watch_store.get_watch(watch_id) if watch_id else None
    if watch_id and (not watch or not watch.get("enabled")):
        raise ApiError(404, "监测议题不存在或已暂停。")
    task = clean_text(watch["task"] if watch else payload.get("task"), 1_000)
    if len(task) < 8:
        raise ApiError(400, "请用至少 8 个字说明议题、时段或关注重点。")
    crawler = DeepSeekClient()
    if not crawler.configured:
        raise ApiError(409, "未配置 DeepSeek API Key。请在配置中心填写后重试。")
    analyst = AnalysisClient()
    if not analyst.configured:
        raise ApiError(409, "尚未配置分析接口；请在网页填写接口链接、Key 和 Model。")
    progress({"stage": "plan", "percent": 3, "message": "正在生成多语种检索式…"})
    prior = stored_run(watch["last_run_id"]) if watch and watch.get("last_run_id") else None
    if watch:
        languages = [code for code in (watch.get("languages") or search_languages(task))
                     if code in LANGUAGE_NAMES] or list(DEFAULT_LANGUAGES)
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        previous_date = parse_search_date(watch.get("last_checked_date"))
        start = max(today - timedelta(days=44), previous_date - timedelta(days=2)) if previous_date else today - timedelta(days=39)
        end = today
    else:
        languages = search_languages(task, str(payload.get("languages") or ""))
        start, end = search_window(task, payload.get("start_date"), payload.get("end_date"))
    plan, plan_mode = analyst.plan_research(task, languages, start, end)
    plan["analysis_provider"] = {"source": analyst.source, "format": analyst.format, "model": analyst.model}
    import_ids = payload.get("browser_import_ids", []) if not watch else []
    if not isinstance(import_ids, list) or len(import_ids) > 100 or any(not re.fullmatch(r"[a-f0-9]{12}", str(item)) for item in import_ids):
        raise ApiError(400, "浏览器导入材料选择无效。")
    manual_imports = [item for item in browser_library(include_content=True)[0]
                      if item["id"] in set(import_ids)] if import_ids else []
    plan["browser_import_ids"] = [item["id"] for item in manual_imports]
    previous_articles = prior.get("articles", []) if prior else []
    previous_candidates = prior.get("candidates", []) if prior else []
    id_offset = max((int(match.group(1)) for article in previous_candidates + previous_articles
                     if (match := re.fullmatch(r"A(\d+)", str(article.get("id") or "")))), default=0)
    skip_urls = {canonical_url(url) for url in watch.get("seen_urls", [])} if watch and prior else None
    fresh_articles = collect_research_articles(crawler, plan, progress, analysis_client=analyst,
                                               manual_imports=manual_imports,
                                               **({"skip_urls": skip_urls, "id_offset": id_offset} if watch else {}))
    fresh_candidates = plan.pop("candidate_articles", fresh_articles)
    if watch:
        plan["new_candidate_ids"] = [article["id"] for article in fresh_candidates]
    fresh_ids = {article["id"] for article in fresh_articles}
    combined = previous_articles + fresh_articles
    articles = choose_representative(combined) if watch else fresh_articles
    if watch:
        selected_ids = {article["id"] for article in articles}
        for article in combined:
            article["sample_status"] = "初筛合格" if article["id"] in selected_ids else "合格未入选"
        for rank, article in enumerate(articles, start=1):
            article["selection_rank"] = rank
        plan["watch_id"] = watch_id
        plan["baseline_run_id"] = prior["id"] if prior else None
        plan["new_count"] = sum(1 for article in articles if article["id"] in fresh_ids)
        plan["coverage"]["initial_pass"] = len(articles)
        plan["coverage"]["gap"] = max(0, MIN_SAMPLE_TARGET - len(articles))
        plan["coverage"]["eligible_count"] += len(previous_articles)
    candidates = list({article["id"]: article for article in previous_articles + fresh_candidates}.values()) if watch else fresh_candidates
    assessment_articles = [article for article in articles if article["sample_status"] == "初筛合格"]
    progress({"stage": "assess", "percent": 88, "message": f"正在依据 {len(assessment_articles)} 篇入选材料生成风险研判…",
              "qualified": len(assessment_articles), "target": MIN_SAMPLE_TARGET})
    assessment = analyst.assess(task, plan, assessment_articles)
    if watch:
        previous_headings = [point.get("heading", "") for point in (prior or {}).get("assessment", {}).get("risk_points", [])]
        plan["new_risk_count"] = sum(1 for point in assessment.get("risk_points", [])
            if not any(SequenceMatcher(None, point.get("heading", ""), heading).ratio() >= 0.72
                       for heading in previous_headings)) if prior else 0
    coverage = plan["coverage"]
    coverage["risk_points"] = len(assessment["risk_points"])
    coverage["required_risk_points"] = MIN_RISK_POINTS
    coverage["min_evidence_per_risk"] = MIN_EVIDENCE_PER_RISK
    coverage["structure_met"] = coverage["gap"] == 0 and len(assessment["risk_points"]) >= MIN_RISK_POINTS
    if coverage["structure_met"]:
        coverage["status"] = "数量与风险点结构达标，来源质量待核验"
    else:
        coverage["status"] = "待补：素材或风险点证据不足"
    assessment["limitations"].append("长篇体裁、原发和同稿去重由模型及规则自动初筛；作者、首发、原文语种和快照来源仍需逐篇人工核验。")
    if coverage["gap"]:
        assessment["limitations"].append(f"距 30 篇独立长篇材料还差 {coverage['gap']} 篇，本次仅作为待补研判。")
    if len(assessment["risk_points"]) < MIN_RISK_POINTS:
        assessment["limitations"].append(f"仅有 {len(assessment['risk_points'])} 个风险点通过每点至少4篇不同报道的校验，尚不足4个。")
    run = {
        "id": uuid.uuid4().hex[:12], "created_at": datetime.now(UTC).isoformat(),
        "task": task, "plan": plan, "plan_mode": plan_mode,
        "articles": articles, "candidates": candidates, "assessment": assessment,
    }
    if watch:
        _, claim_changes = watch_store.reconcile_claims(watch, run, prior)
        plan["claim_changes"] = claim_changes
        plan["new_risk_count"] = sum(change["kind"] == "new" for change in claim_changes)
    progress({"stage": "save", "percent": 96, "message": "正在保存入选报道、快照线索与研判…",
              "qualified": len(articles), "target": MIN_SAMPLE_TARGET})
    mysql_config = mysql_store.load_config()
    if mysql_config:
        try:
            mysql_store.insert_run(run, mysql_config)
            run["storage"] = "已写入 MySQL"
            try:
                rag_store.upsert_documents(mysql_store.rag_documents(mysql_config), prune=True)
                launch_news_social_vector_job()
            except Exception as index_error:
                print(f"RAG index sync failed: {type(index_error).__name__}")
        except Exception as error:
            print(f"MySQL write failed: {type(error).__name__}")
            run["storage"] = "MySQL 写入失败；本地 JSON 备份已保存"
    else:
        run["storage"] = "本地 JSON；尚未配置 MySQL"
    record_run(run)
    if watch:
        watch_store.record_result(watch_id, run, prior)
    return public_run(run)


def run_job(job_id: str, payload: dict[str, Any]) -> None:
    def progress(value: Any) -> None:
        with JOBS_LOCK:
            if isinstance(value, dict):
                JOBS[job_id]["progress"] = value.get("message", "检索进行中…")
                JOBS[job_id]["progress_detail"] = value
            else:
                JOBS[job_id]["progress"] = str(value)
    try:
        result = execute_research(payload, progress)
        with JOBS_LOCK:
            JOBS[job_id].update({"status": "done", "progress": "已完成",
                                 "progress_detail": {"stage": "done", "percent": 100, "message": "已完成"},
                                 "result": result})
    except ApiError as error:
        with JOBS_LOCK:
            JOBS[job_id].update({"status": "error", "error": error.message})
    except Exception as error:
        print(f"Research job failed: {type(error).__name__}: {error}")
        with JOBS_LOCK:
            JOBS[job_id].update({"status": "error", "error": "检索任务失败，请查看本地终端日志。"})


def launch_job(payload: dict[str, Any], *, scheduled: bool = False) -> str:
    with JOBS_LOCK:
        if any(job["status"] == "running" for job in JOBS.values()):
            raise ApiError(409, "已有检索任务正在运行，请等待其完成。")
        if scheduled and not watch_store.claim_due(str(payload.get("watch_id") or "")):
            raise ApiError(409, "监测议题尚未到运行时间。")
        job_id = uuid.uuid4().hex[:12]
        JOBS[job_id] = {"id": job_id, "status": "running", "progress": "等待开始",
                        "progress_detail": {"stage": "plan", "percent": 0, "message": "等待开始"}}
        for old_id in list(JOBS)[:-10]:
            if JOBS[old_id]["status"] != "running":
                JOBS.pop(old_id)
    threading.Thread(target=run_job, args=(job_id, payload), daemon=True).start()
    return job_id


def watch_scheduler() -> None:
    while True:
        try:
            due = watch_store.due_watch()
            if due and DeepSeekClient().configured and AnalysisClient().configured:
                launch_job({"watch_id": due["id"]}, scheduled=True)
        except ApiError:
            pass
        except Exception as error:
            print(f"Watch scheduler failed: {type(error).__name__}")
        time.sleep(30)


def launch_paper_vector_job(paper_id: str) -> str:
    with PAPER_JOBS_LOCK:
        for existing_id, job in PAPER_JOBS.items():
            if job["paper_id"] == paper_id and job["status"] == "running":
                return existing_id
        job_id = uuid.uuid4().hex[:12]
        PAPER_JOBS[job_id] = {"status": "running", "paper_id": paper_id,
                              "message": "正在加载本地多语种向量模型…", "done": 0, "total": 0}

    def work() -> None:
        def progress(done: int, total: int) -> None:
            with PAPER_JOBS_LOCK:
                PAPER_JOBS[job_id].update({"message": "正在为论文段落生成向量…", "done": done, "total": total})
        try:
            result = rag_store.vectorize_paper(paper_id, progress)
            with PAPER_JOBS_LOCK:
                PAPER_JOBS[job_id].update({"status": "done", "message": "论文向量索引已完成。", "vector_status": result})
        except Exception as error:
            print(f"Paper vectorization failed: {type(error).__name__}: {error}")
            with PAPER_JOBS_LOCK:
                PAPER_JOBS[job_id].update({"status": "error", "message": "向量化失败；正文仍已入库并可进行词面检索。请检查模型下载网络后重试。"})
    threading.Thread(target=work, daemon=True).start()
    return job_id


def launch_news_social_vector_job() -> dict[str, Any]:
    with RAG_VECTOR_LOCK:
        if RAG_VECTOR_JOB["status"] == "running":
            return dict(RAG_VECTOR_JOB)
        RAG_VECTOR_JOB.update({"status": "running", "done": 0, "total": 0,
                               "message": "正在加载本地多语种向量模型…"})

    def work() -> None:
        def progress(done: int, total: int) -> None:
            with RAG_VECTOR_LOCK:
                RAG_VECTOR_JOB.update({"done": done, "total": total,
                                       "message": f"正在建立新闻与社媒语义索引 {done}/{total} 段…"})
        try:
            result = rag_store.vectorize_news_social(progress)
            with RAG_VECTOR_LOCK:
                RAG_VECTOR_JOB.update({"status": "done", "message": "新闻与社媒语义索引已完成。",
                                       "done": result["ready"], "total": result["total"]})
        except Exception as error:
            print(f"News/social vectorization failed: {type(error).__name__}: {error}")
            with RAG_VECTOR_LOCK:
                RAG_VECTOR_JOB.update({"status": "error", "message": "语义索引失败；仍可使用词面检索。检查模型文件或网络后重试。"})
    threading.Thread(target=work, daemon=True).start()
    return dict(RAG_VECTOR_JOB)


def vision_ocr_page(image: bytes, page_number: int) -> str:
    saved = analysis_store.load_config()
    if not saved:
        raise ApiError(409, "请先配置支持图片输入的分析模型 API。")
    client = AnalysisClient(saved)
    encoded = base64.b64encode(image).decode("ascii")
    prompt = f"逐字转录这张论文第 {page_number} 页图片中的可见文字，保留段落顺序。表格用简洁 Markdown 表示。只输出转录正文；看不清的地方写[无法辨认]，不要补写或总结。"
    picture = ({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": encoded}}
               if client.format == "anthropic" else
               {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}", "detail": "high"}})
    response = client.post("/chat/completions", {"messages": [
        {"role": "user", "content": [{"type": "text", "text": prompt}, picture]},
    ]}, timeout=90)
    return str(response["choices"][0]["message"]["content"] or "")[:20_000]


def persist_paper_payload(payload: dict[str, Any], progress=None) -> dict[str, Any]:
    config = mysql_store.load_config()
    if not config:
        raise ApiError(409, "请先在配置中心连接 MySQL，论文和正文才能持久入库。")
    try:
        raw = base64.b64decode(str(payload.get("pdf_base64") or ""), validate=True)
    except (ValueError, binascii.Error) as error:
        raise ApiError(400, "PDF 文件编码无效，请重新选择文件。") from error
    source_url = str(payload.get("source_url") or "").strip()
    if source_url:
        parsed = urlparse(source_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or parsed.username or parsed.password or host == "localhost" or host.endswith((".local", ".internal")):
            raise ApiError(400, "论文来源链接必须是公开 HTTPS 论文页面。")
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ApiError(400, "论文来源链接不能指向本机或内网。")
        except ValueError:
            pass
    paper = paper_store.extract_pdf(raw, str(payload.get("filename") or ""),
                                    title=str(payload.get("title") or ""),
                                    authors=str(payload.get("authors") or ""),
                                    year=str(payload.get("year") or ""),
                                    metadata_hint=payload.get("metadata"),
                                    doi_lookup=paper_store.crossref_metadata,
                                    vision_page=vision_ocr_page if payload.get("use_vision") is True else None,
                                    progress=progress)
    paper["source_url"] = source_url[:2000]
    file_path = paper_store.PAPER_DIR / f"{paper['id']}.pdf"
    new_file = not file_path.exists()
    paper_store.save_file(raw, paper)
    try:
        duplicate = mysql_store.save_paper(paper, config)
    except Exception:
        if new_file:
            file_path.unlink(missing_ok=True)
        raise
    index_result = rag_store.upsert_documents(mysql_store.rag_documents(config), prune=True)
    job_id = launch_paper_vector_job(paper["id"])
    return {"paper": {key: paper[key] for key in
                      ("id", "filename", "title", "authors", "year", "doi", "page_count", "text_chars", "source_url", "page_methods", "ocr_warnings", "metadata")},
            "duplicate": duplicate, "index": index_result, "job_id": job_id,
            "counts": rag_store.stats()}


def launch_paper_import_job(payload: dict[str, Any]) -> str:
    job_id = uuid.uuid4().hex[:12]
    with PAPER_JOBS_LOCK:
        PAPER_JOBS[job_id] = {"status": "running", "paper_id": None, "phase": "extract",
                              "message": "正在读取 PDF…", "done": 0, "total": 0}

    def work() -> None:
        def progress(done: int, total: int, method: str) -> None:
            with PAPER_JOBS_LOCK:
                PAPER_JOBS[job_id].update({"done": done, "total": total,
                                           "message": f"正在识别第 {done}/{total} 页 · {method}"})
        try:
            result = persist_paper_payload(payload, progress)
            with PAPER_JOBS_LOCK:
                PAPER_JOBS[job_id].update({"status": "done", "phase": "stored", "paper_id": result["paper"]["id"],
                                           "message": "论文正文已入库；正在建立向量索引。", "result": result})
        except Exception as error:
            print(f"Paper import job failed: {type(error).__name__}: {error}")
            with PAPER_JOBS_LOCK:
                PAPER_JOBS[job_id].update({"status": "error", "message": error.message if isinstance(error, ApiError) else str(error) if isinstance(error, ValueError) else "论文识别或入库失败，请查看本机服务日志。"})
    threading.Thread(target=work, daemon=True).start()
    return job_id


def launch_meltwater_job(search: dict[str, str], remote_id: str, config: dict[str, Any]) -> str:
    with MELTWATER_JOBS_LOCK:
        MELTWATER_JOBS[remote_id] = {"id": remote_id, "status": "running", "stage": "search",
                                     "message": "融文正在按日期检索并下载…", "completed_months": 0,
                                     "total_months": 0, "platform_total": 0}

    def update(**values: Any) -> None:
        with MELTWATER_JOBS_LOCK:
            MELTWATER_JOBS[remote_id].update(values)

    def work() -> None:
        try:
            while True:
                remote = meltwater_source.studio_request(f"/api/jobs/{remote_id}", timeout=20)
                if remote.get("state") == "error":
                    raise meltwater_source.StudioError(str(remote.get("message") or "融文检索失败。"))
                update(message=str(remote.get("message") or "融文正在检索…"),
                       completed_months=int(remote.get("completedMonths") or 0),
                       total_months=len(remote.get("months") or []),
                       platform_total=int(remote.get("total") or 0))
                if remote.get("state") == "done":
                    break
                time.sleep(1.5)
            expected = int(remote.get("total") or 0)
            returned = int(remote.get("returned") or 0)
            if returned != expected:
                raise meltwater_source.StudioError(
                    f"融文任务校验失败：平台命中 {expected} 条，任务返回 {returned} 条。")
            update(stage="download", message=f"正在下载并校验 {expected} 条 JSONL 记录…")
            file, count = meltwater_source.download_export(remote_id, expected)
            update(stage="sql", message=f"已取全 {count} 条；正在按文档 ID 去重写入 MySQL…")
            imported = meltwater_source.import_download(config, file, remote_id, search, count)
            update(stage="index", message="SQL 入库完成；正在更新本地检索索引…")
            indexed = rag_store.upsert_documents(mysql_store.rag_documents(config), prune=True)
            vector_job = launch_news_social_vector_job()
            update(status="done", stage="done", message="融文数据已入库，可在研究库问答中检索。",
                   result={**imported, "indexed": indexed, "vector_job": vector_job,
                           "raw_file": file.name})
        except Exception as error:
            print(f"Meltwater download failed: {type(error).__name__}: {error}")
            update(status="error", stage="error", message=str(error))

    threading.Thread(target=work, daemon=True).start()
    return remote_id


class SuyuanHandler(BaseHTTPRequestHandler):
    server_version = "SuyuanDemo/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def send_json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        origin = self.headers.get("Origin", "")
        if self.path in BROWSER_EXTENSION_ROUTES and re.fullmatch(r"chrome-extension://[a-p]{32}", origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_export_file(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as file:
            while chunk := file.read(262_144):
                self.wfile.write(chunk)

    def read_json(self, max_bytes: int = MAX_BODY_BYTES) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ApiError(400, "Content-Length 无效。") from error
        if length <= 0 or length > max_bytes:
            raise ApiError(400, "请求内容为空或超过大小限制。")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiError(400, "请求必须是 UTF-8 JSON。") from error
        if not isinstance(value, dict):
            raise ApiError(400, "请求 JSON 必须是对象。")
        return value

    def do_OPTIONS(self) -> None:
        origin = self.headers.get("Origin", "")
        if self.path not in BROWSER_EXTENSION_ROUTES or not re.fullmatch(r"chrome-extension://[a-p]{32}", origin):
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Suyuan-Import-Key")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path in {"/", "/index.html"}:
            self.send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif self.path in {"/settings", "/settings.html"}:
            self.send_file(STATIC_DIR / "settings.html", "text/html; charset=utf-8")
        elif self.path in {"/library", "/library.html"}:
            self.send_file(STATIC_DIR / "library.html", "text/html; charset=utf-8")
        elif self.path == "/static/app.css":
            self.send_file(STATIC_DIR / "app.css", "text/css; charset=utf-8")
        elif self.path == "/static/app.js":
            self.send_file(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
        elif self.path == "/static/settings.css":
            self.send_file(STATIC_DIR / "settings.css", "text/css; charset=utf-8")
        elif self.path == "/static/settings.js":
            self.send_file(STATIC_DIR / "settings.js", "application/javascript; charset=utf-8")
        elif self.path == "/static/library.css":
            self.send_file(STATIC_DIR / "library.css", "text/css; charset=utf-8")
        elif self.path == "/static/library-paper.css":
            self.send_file(STATIC_DIR / "library-paper.css", "text/css; charset=utf-8")
        elif self.path == "/static/library.js":
            self.send_file(STATIC_DIR / "library.js", "application/javascript; charset=utf-8")
        elif self.path == "/api/search/languages":
            self.send_json(200, {"default": [{"code": code, "name": LANGUAGE_NAMES[code]}
                                               for code in DEFAULT_LANGUAGES],
                                 "supported": [{"code": code, "name": name}
                                               for code, name in LANGUAGE_NAMES.items()],
                                 "queries_per_language": 2, "max_languages": 12})
        elif self.path == "/api/meltwater/status":
            try:
                status = meltwater_source.studio_request("/api/status", timeout=3)
                accounts = meltwater_source.studio_request("/api/accounts", timeout=3)
                self.send_json(200, {"connected": True, "studio": status,
                                     "accounts": accounts.get("accounts", []),
                                     "sql_configured": bool(mysql_store.load_config())})
            except meltwater_source.StudioError as error:
                self.send_json(200, {"connected": False, "error": str(error), "accounts": [],
                                     "sql_configured": bool(mysql_store.load_config())})
        elif self.path == "/api/meltwater/downloads":
            config = mysql_store.load_config()
            if not config:
                self.send_json(200, {"downloads": [], "sql_configured": False})
            else:
                try:
                    self.send_json(200, {"downloads": meltwater_source.list_downloads(config),
                                         "sql_configured": True})
                except Exception as error:
                    print(f"Meltwater downloads read failed: {type(error).__name__}: {error}")
                    self.send_json(502, {"error": "无法读取融文下载记录，请检查 MySQL。"})
        elif re.fullmatch(r"/api/meltwater/jobs/[a-f0-9]{12}", self.path):
            job_id = self.path.rsplit("/", 1)[-1]
            with MELTWATER_JOBS_LOCK:
                job = dict(MELTWATER_JOBS[job_id]) if job_id in MELTWATER_JOBS else None
            self.send_json(200, job) if job else self.send_json(404, {"error": "任务不存在或服务已重启。"})
        elif re.fullmatch(r"/api/meltwater/downloads/[a-f0-9]{12}/export", self.path):
            job_id = self.path.split("/")[4]
            self.send_export_file(meltwater_source.DOWNLOAD_DIR / f"{job_id}.jsonl")
        elif self.path == "/api/rag/status":
            self.send_json(200, {"counts": rag_store.stats(),
                                 "sql_configured": bool(mysql_store.load_config()),
                                 "analysis_configured": AnalysisClient().configured,
                                 "vector_job": dict(RAG_VECTOR_JOB)})
        elif self.path == "/api/rag/vector-job":
            with RAG_VECTOR_LOCK:
                self.send_json(200, {"job": dict(RAG_VECTOR_JOB), "counts": rag_store.stats()})
        elif self.path == "/api/papers":
            try:
                config = mysql_store.load_config()
                papers = mysql_store.list_papers(config) if config else []
                for paper in papers:
                    paper["vector_status"] = rag_store.paper_vector_status(paper["id"])
                    with PAPER_JOBS_LOCK:
                        paper["active_job_id"] = next((job_id for job_id, job in PAPER_JOBS.items()
                                                       if job["paper_id"] == paper["id"] and job["status"] == "running"), None)
                self.send_json(200, {"papers": papers, "sql_configured": bool(config)})
            except Exception as error:
                print(f"Paper list failed: {type(error).__name__}")
                self.send_json(502, {"error": "无法读取论文库，请检查 MySQL 连接。"})
        elif re.fullmatch(r"/api/papers/jobs/[a-f0-9]{12}", self.path):
            job_id = self.path.rsplit("/", 1)[-1]
            with PAPER_JOBS_LOCK:
                job = dict(PAPER_JOBS[job_id]) if job_id in PAPER_JOBS else None
            self.send_json(200, job) if job else self.send_json(404, {"error": "导入任务不存在或服务已重启。"})
        elif re.fullmatch(r"/api/papers/[a-f0-9]{32}/file", self.path):
            paper_id = self.path.split("/")[3]
            self.send_file(paper_store.PAPER_DIR / f"{paper_id}.pdf", "application/pdf")
        elif self.path == "/api/config":
            client = DeepSeekClient()
            self.send_json(200, {"configured": client.configured, "model": client.model})
        elif self.path == "/api/deepseek/config":
            try:
                client = DeepSeekClient()
                self.send_json(200, {"configured": client.configured, "model": client.model,
                                     "api_key_set": client.configured,
                                     "reasoning_effort": client.reasoning_effort,
                                     "source": "网页配置" if deepseek_store.load_config() else ".env / 环境变量"})
            except (ValueError, json.JSONDecodeError):
                self.send_json(500, {"error": "本机 DeepSeek 配置文件无效。"})
        elif self.path == "/api/analysis/config":
            try:
                config = analysis_store.public_config()
                if not config["configured"]:
                    config["active_source"] = "DeepSeek 默认分析" if DeepSeekClient().configured else "未配置"
                else:
                    config["active_source"] = "自定义分析接口"
                self.send_json(200, config)
            except (ValueError, json.JSONDecodeError):
                self.send_json(500, {"error": "本机分析接口配置文件无效。"})
        elif self.path == "/api/mysql/config":
            config = mysql_store.public_config()
            if config["configured"]:
                try:
                    config["article_count"] = mysql_store.count_articles(mysql_store.load_config())
                    config["connected"] = True
                except Exception:
                    config["connected"] = False
            self.send_json(200, config)
        elif self.path == "/api/records":
            try:
                mysql_config = mysql_store.load_config()
                if mysql_config:
                    records = mysql_store.list_runs(mysql_config)
                    self.send_json(200, {"records": records, "source": "mysql"})
                    return
            except Exception as error:
                print(f"MySQL history read failed: {type(error).__name__}")
                self.send_json(200, {"records": local_records(), "source": "local_backup", "warning": "MySQL 暂不可读，当前显示本地备份。"})
                return
            self.send_json(200, {"records": local_records(), "source": "local_backup"})
        elif self.path == "/api/watches":
            watches = [{key: value for key, value in watch.items() if key != "seen_urls"}
                       for watch in watch_store.list_watches()]
            self.send_json(200, {"watches": watches})
        elif re.fullmatch(r"/api/evidence-reviews/[a-f0-9]{12}", self.path):
            config = mysql_store.load_config()
            if not config:
                self.send_json(409, {"error": "请先连接 MySQL，证据核验记录才能持久保存。"})
            else:
                try:
                    run_id = self.path.rsplit("/", 1)[-1]
                    reviews = mysql_store.list_evidence_reviews(config, run_id)
                    run = stored_run(run_id)
                    passages = {part.get("id"): part.get("text") or "" for article in (run or {}).get("articles", [])
                                for part in article.get("passages", [])}
                    for review in reviews:
                        text = passages.get(review["passage_id"])
                        review["stale"] = text is None or hashlib.sha256(text.encode("utf-8")).hexdigest() != review["excerpt_hash"]
                    self.send_json(200, {"reviews": reviews})
                except Exception as error:
                    print(f"Evidence reviews read failed: {type(error).__name__}")
                    self.send_json(502, {"error": "无法读取证据核验记录。"})
        elif self.path == "/api/browser/imports":
            origin = self.headers.get("Origin", "")
            if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
                self.send_json(403, {"error": "仅允许本机页面查看浏览器导入材料。"})
            else:
                imports, warning = browser_library()
                self.send_json(200, {"imports": imports, "pairing_token": browser_imports.pairing_token(),
                                     "warning": warning})
        elif re.fullmatch(r"/api/jobs/[a-f0-9]{12}", self.path):
            job_id = self.path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                job = dict(JOBS[job_id]) if job_id in JOBS else None
            self.send_json(200, job) if job else self.send_json(404, {"error": "任务不存在或服务已重启。"})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin", "")
        allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        extension_request = self.path in BROWSER_EXTENSION_ROUTES and bool(re.fullmatch(r"chrome-extension://[a-p]{32}", origin))
        if host not in allowed_hosts or (origin and origin not in {f"http://{item}" for item in allowed_hosts} and not extension_request):
            self.send_json(403, {"error": "仅允许从本机页面提交请求。"})
            return
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            self.send_json(415, {"error": "请求必须使用 application/json。"})
            return
        if (self.path in {"/api/meltwater/search", "/api/meltwater/accounts", "/api/meltwater/ai-query"}
                or re.fullmatch(r"/api/meltwater/accounts/(?:default|[a-f0-9]{12})/(?:connect|email)", self.path)
                or re.fullmatch(r"/api/meltwater/accounts/[a-f0-9]{12}/delete", self.path)):
            try:
                payload = self.read_json(60_000)
                if self.path == "/api/meltwater/accounts":
                    result = meltwater_source.studio_request("/api/accounts", {"label": clean_text(payload.get("label"), 60)})
                    self.send_json(202, result)
                elif self.path.endswith("/delete"):
                    account_key = self.path.split("/")[4]
                    result = meltwater_source.studio_request(f"/api/accounts/{account_key}",
                                                             timeout=12, method="DELETE")
                    self.send_json(200, result)
                elif self.path.endswith("/email"):
                    account_key = self.path.split("/")[4]
                    result = meltwater_source.studio_request(f"/api/accounts/{account_key}/email",
                                                             {"email": clean_text(payload.get("email"), 254)})
                    self.send_json(200, result)
                elif self.path.endswith("/connect"):
                    account_key = self.path.split("/")[4]
                    result = meltwater_source.studio_request(f"/api/accounts/{account_key}/connect", {}, timeout=65)
                    self.send_json(202, result)
                elif self.path == "/api/meltwater/ai-query":
                    subject = clean_text(payload.get("subject"), 300)
                    if len(subject) < 3:
                        raise ApiError(400, "请输入至少 3 个字的检索主题。")
                    client = AnalysisClient()
                    prompt = ("为融文 Meltwater 写多语种 Boolean 检索式。短语用双引号，同义词用 OR，"
                              "需要消歧时用 AND 语境组；不要把日期、内容类型或国家字段写入布尔式。"
                              "只返回 JSON 对象，包含 query、languages、rationale。"
                              f"主题：{subject}。语境：{clean_text(payload.get('context'), 500)}。"
                              f"语种：{clean_text(payload.get('languages'), 150) or '英西德法俄阿拉伯葡韩日'}。")
                    reply = client.post("/chat/completions", {"messages": [
                        {"role": "system", "content": "你是舆情检索式设计师，只返回有效 JSON。"},
                        {"role": "user", "content": prompt}]}, timeout=45)
                    draft = parse_model_object(reply["choices"][0]["message"]["content"])
                    if not isinstance(draft.get("query"), str) or not draft["query"].strip():
                        raise ApiError(502, "分析模型未返回有效检索式。")
                    self.send_json(200, {"query": draft["query"][:12000],
                                         "languages": draft.get("languages", []),
                                         "rationale": str(draft.get("rationale") or "")[:1000]})
                else:
                    config = mysql_store.load_config()
                    if not config:
                        raise ApiError(409, "请先在配置中心连接 MySQL，再开始融文全量下载。")
                    mysql_store.connect(config).close()
                    search = meltwater_source.validate_search(payload)
                    with MELTWATER_JOBS_LOCK:
                        if any(job["status"] == "running" for job in MELTWATER_JOBS.values()):
                            raise ApiError(409, "已有融文下载正在运行。")
                    remote = meltwater_source.studio_request("/api/search", search, timeout=20)
                    remote_id = str(remote.get("id") or "")
                    if not re.fullmatch(r"[a-f0-9]{12}", remote_id):
                        raise ApiError(502, "融文研究台未返回有效任务 ID。")
                    self.send_json(202, {"job_id": launch_meltwater_job(search, remote_id, config)})
            except ApiError as error:
                self.send_json(error.status, {"error": error.message})
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            except meltwater_source.StudioError as error:
                self.send_json(502, {"error": str(error)})
            except Exception as error:
                print(f"Meltwater request failed: {type(error).__name__}: {error}")
                self.send_json(502, {"error": "融文连接或入库准备失败，请检查本机服务日志。"})
            return
        if self.path in BROWSER_EXTENSION_ROUTES:
            try:
                payload = self.read_json(512 if self.path == "/api/browser/test" else
                                         21_000_000 if self.path == "/api/browser/paper" else MAX_BROWSER_IMPORT_BYTES)
                if not extension_request or not secrets.compare_digest(self.headers.get("X-Suyuan-Import-Key", ""), browser_imports.pairing_token()):
                    self.send_json(403, {"error": "请在浏览器扩展中填写网页显示的配对码。"})
                    return
                if self.path == "/api/browser/test":
                    sql_connected = False
                    try:
                        config = mysql_store.load_config()
                        if config:
                            sql_connected = mysql_store.clip_storage_ready(config)
                    except Exception:
                        pass
                    self.send_json(200, {"ok": True, "service": "溯源", "sql_connected": sql_connected,
                                         "message": "本机服务与配对码均已验证。"})
                    return
                if self.path == "/api/browser/paper":
                    if not mysql_store.load_config():
                        raise ApiError(409, "请先在配置中心连接 MySQL。")
                    self.send_json(202, {"job_id": launch_paper_import_job(payload)})
                    return
                item = browser_imports.save_import(payload, canonical_url(str(payload.get("url") or "")))
                stored_in_sql = False
                sql_warning = None
                try:
                    config = mysql_store.load_config()
                    if config:
                        full_item = browser_imports.selected_imports([item["id"]])[0]
                        mysql_store.save_clip(full_item, config)
                        stored_in_sql = True
                        try:
                            rag_store.upsert_documents(mysql_store.rag_documents(config), prune=True)
                            launch_news_social_vector_job()
                        except Exception as index_error:
                            print(f"RAG clip sync failed: {type(index_error).__name__}")
                    else:
                        sql_warning = "尚未配置 SQL，文章已保存在本机；配置数据库后会自动入库。"
                except Exception as error:
                    print(f"Browser clip SQL write failed: {type(error).__name__}")
                    sql_warning = "文章已保存在本机，但 SQL 暂不可写；数据库恢复后可在配置中心重新连接并补入。"
                self.send_json(200, {"ok": True, "article": {**item, "stored_in_sql": stored_in_sql},
                                     "warning": sql_warning})
            except ApiError as error:
                self.send_json(error.status, {"error": error.message})
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            except Exception as error:
                print(f"Browser import failed: {type(error).__name__}")
                self.send_json(500, {"error": "浏览器导入失败，请查看本机服务日志。"})
            return
        if self.path == "/api/papers/import" or re.fullmatch(r"/api/papers/[a-f0-9]{32}/vectorize", self.path):
            try:
                config = mysql_store.load_config()
                if not config:
                    raise ApiError(409, "请先在配置中心连接 MySQL，论文和正文才能持久入库。")
                if self.path == "/api/papers/import":
                    payload = self.read_json(21_000_000)
                    self.send_json(202, {"job_id": launch_paper_import_job(payload)})
                else:
                    self.read_json()
                    paper_id = self.path.split("/")[3]
                    if not any(paper["id"] == paper_id for paper in mysql_store.list_papers(config)):
                        raise ApiError(404, "论文不存在。")
                    self.send_json(202, {"job_id": launch_paper_vector_job(paper_id)})
            except ApiError as error:
                self.send_json(error.status, {"error": error.message})
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            except Exception as error:
                print(f"Paper import failed: {type(error).__name__}: {error}")
                self.send_json(502, {"error": "论文入库失败，请检查 MySQL、PDF 和本地服务日志。"})
            return
        if self.path in {"/api/social/collect", "/api/rag/ask", "/api/rag/reindex", "/api/rag/vectorize"}:
            try:
                payload = self.read_json()
                config = mysql_store.load_config()
                if not config:
                    raise ApiError(409, "请先在配置中心连接 MySQL，作为新闻和社媒研究库的持久存储。")
                if self.path == "/api/social/collect":
                    posts, warnings = social_store.collect(str(payload.get("query") or ""),
                                                           str(payload.get("mastodon_tag") or ""))
                    if not posts and warnings:
                        raise ApiError(502, "；".join(warnings))
                    saved = mysql_store.save_social_posts(posts, config)
                    indexed = rag_store.upsert_documents(mysql_store.rag_documents(config), prune=True)
                    vector_job = launch_news_social_vector_job()
                    self.send_json(200, {"saved": saved, "indexed": indexed, "warnings": warnings,
                                         "counts": rag_store.stats(), "vector_job": vector_job})
                elif self.path == "/api/rag/reindex":
                    result = rag_store.upsert_documents(mysql_store.rag_documents(config), prune=True)
                    paper_jobs = [launch_paper_vector_job(paper["id"]) for paper in mysql_store.list_papers(config)
                                  if not rag_store.paper_vector_status(paper["id"])["complete"]]
                    vector_job = launch_news_social_vector_job()
                    self.send_json(200, {"result": result, "counts": rag_store.stats(), "paper_jobs": paper_jobs,
                                         "vector_job": vector_job})
                elif self.path == "/api/rag/vectorize":
                    self.send_json(202, {"vector_job": launch_news_social_vector_job(), "counts": rag_store.stats()})
                else:
                    question = str(payload.get("question") or "").strip()
                    if len(question) < 4 or len(question) > 1000:
                        raise ApiError(400, "问题需为 4–1000 个字符。")
                    news = rag_store.search_hybrid(question, limit=6, kinds=("news", "clip"))
                    social = rag_store.search_hybrid(question, limit=6, kinds=("social",))
                    papers = rag_store.search_papers(question, limit=8)
                    sources = [{**item, "citation": f"N{number}"} for number, item in enumerate(news, 1)]
                    sources += [{**item, "citation": f"S{number}"} for number, item in enumerate(social, 1)]
                    sources += [{**item, "citation": f"K{number}"} for number, item in enumerate(papers, 1)]
                    if not sources:
                        self.send_json(200, {"answer": "研究库中暂未检索到相关材料。请先运行新闻研究或采集社媒，再换具体关键词提问。",
                                             "sources": [], "counts": rag_store.stats()})
                        return
                    client = AnalysisClient()
                    if not client.configured:
                        raise ApiError(409, "请先在配置中心配置分析模型 API。")
                    context = "\n\n".join(
                        f"[{item['citation']}] {item['kind']} | {item['title']} | {item['source']} | "
                        f"{item['published_at']} | {item['url']} | "
                        f"{'第' + str(item['page_no']) + '页' if item.get('page_no') else ''}\n{item['excerpt'][:1100]}"
                        for item in sources)
                    result = client.post("/chat/completions", {"messages": [
                        {"role": "system", "content": "你是涉华政经舆情研究库证据助手。只能根据提供的材料回答；每个可核查的事实或判断后标注 [N1]/[S1]/[K1] 等材料编号。区分新闻正文、浏览器剪藏、社媒帖子与论文；来源以‘融文 ·’开头的材料仅为平台匹配片段，不是完整正文，不能从片段推断全文观点，引用时注明‘融文匹配片段’。论文是背景研究，不等于最新政策或舆情事实，引用时注明页码。若材料中的论文结论相互冲突，必须并列写出各自主张及对应论文、页码；仅在摘录明确提供时比较研究时间、对象、方法与适用范围，并说明这些差异是否可能解释分歧。不得按论文数量、来源名气或向量相似度投票裁决真伪；证据不足以裁决时明确标为未决争议。若只检索到一方，说明未检索到反方材料，不得推断不存在反方。社媒仅是小范围观察样本，不能推断全网声量或代表性。材料不足时明确说明，不得编造来源、时间或传播链。用中文回答。"},
                        {"role": "user", "content": f"问题：{question}\n\n本地研究库检索材料：\n{context}"},
                    ]}, timeout=90)
                    self.send_json(200, {"answer": result["choices"][0]["message"]["content"],
                                         "sources": sources, "counts": rag_store.stats()})
            except ApiError as error:
                self.send_json(error.status, {"error": error.message})
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            except Exception as error:
                print(f"RAG request failed: {type(error).__name__}: {error}")
                self.send_json(502, {"error": "研究库操作失败，请检查 MySQL 连接与本机服务日志。"})
            return
        if re.fullmatch(r"/api/evidence-reviews/[a-f0-9]{12}", self.path):
            try:
                payload = self.read_json()
                run_id = self.path.rsplit("/", 1)[-1]
                passage_id = str(payload.get("passage_id") or "")
                status = str(payload.get("status") or "")
                note = str(payload.get("note") or "").strip()
                if not re.fullmatch(r"A\d+-P\d+", passage_id) or status not in {"pending", "verified", "disputed", "excluded"} or len(note) > 1000:
                    raise ApiError(400, "请选择有效的证据段落、核验状态和不超过 1000 字的备注。")
                config = mysql_store.load_config()
                if not config:
                    raise ApiError(409, "请先连接 MySQL，证据核验记录才能持久保存。")
                run = stored_run(run_id)
                if not run:
                    raise ApiError(404, "研判档案不存在。")
                passage = next((part for article in run.get("articles", []) for part in article.get("passages", [])
                                if part.get("id") == passage_id), None)
                if not passage:
                    raise ApiError(404, "该档案中没有这条证据段落。")
                digest = hashlib.sha256(str(passage.get("text") or "").encode("utf-8")).hexdigest()
                review = mysql_store.save_evidence_review(config, run_id, passage_id, digest, status, note)
                self.send_json(200, {"review": review})
            except ApiError as error:
                self.send_json(error.status, {"error": error.message})
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            except Exception as error:
                print(f"Evidence review write failed: {type(error).__name__}: {error}")
                self.send_json(502, {"error": "证据核验保存失败，请检查 MySQL。"})
            return
        if self.path == "/api/watches" or re.fullmatch(r"/api/watches/[a-f0-9]{12}/(?:run|toggle)", self.path):
            try:
                payload = self.read_json()
                if self.path == "/api/watches":
                    run_id = str(payload.get("run_id") or "")
                    if not re.fullmatch(r"[a-f0-9]{12}", run_id):
                        raise ApiError(400, "请选择已有研判档案。")
                    run = stored_run(run_id)
                    if not run:
                        raise ApiError(404, "未找到研判档案。")
                    watch = watch_store.save_watch(run, str(payload.get("daily_time") or "09:00"))
                    self.send_json(200, {"watch": {key: value for key, value in watch.items() if key != "seen_urls"}})
                else:
                    parts = self.path.strip("/").split("/")
                    watch_id, action = parts[2], parts[3]
                    watch = watch_store.get_watch(watch_id)
                    if not watch:
                        raise ApiError(404, "监测议题不存在。")
                    if action == "toggle":
                        updated = watch_store.set_enabled(watch_id, payload.get("enabled") is True)
                        self.send_json(200, {"watch": {key: value for key, value in updated.items() if key != "seen_urls"}})
                    else:
                        if not watch.get("enabled"):
                            raise ApiError(409, "请先启用该监测议题。")
                        if not DeepSeekClient().configured or not AnalysisClient().configured:
                            raise ApiError(409, "请先配置 DeepSeek 抓取与分析接口。")
                        job_id = launch_job({"watch_id": watch_id})
                        self.send_json(202, {"job_id": job_id, "status": "running"})
            except ApiError as error:
                self.send_json(error.status, {"error": error.message})
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json(400, {"error": str(error)})
            except Exception as error:
                print(f"Watch request failed: {type(error).__name__}: {error}")
                self.send_json(500, {"error": "监测议题操作失败，请查看本地终端日志。"})
            return
        if self.path in {"/api/deepseek/config", "/api/deepseek/test", "/api/deepseek/models"}:
            try:
                payload = self.read_json()
                previous = deepseek_store.load_config()
                if not previous and DeepSeekClient().configured:
                    client = DeepSeekClient()
                    previous = {"api_key": client.api_key, "model": client.model}
                config = deepseek_store.validate_config(payload, previous)
                if self.path == "/api/deepseek/models":
                    models = list_provider_models("openai", "https://api.deepseek.com", config["api_key"])
                    self.send_json(200, {"models": models})
                elif self.path == "/api/deepseek/test":
                    client = DeepSeekClient()
                    client.api_key = config["api_key"]
                    client.post("/v1/tools/search", {"query": "news", "count": 1, "need_content": False}, timeout=20)
                    self.send_json(200, {"ok": True, "model": config["model"]})
                else:
                    deepseek_store.save_config(config)
                    self.send_json(200, {"ok": True})
            except ApiError as error:
                self.send_json(error.status if 400 <= error.status < 600 else 502, {"error": error.message})
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json(400, {"error": str(error)})
            except Exception as error:
                print(f"DeepSeek configuration failed: {type(error).__name__}")
                self.send_json(502, {"error": "DeepSeek 配置失败，请检查 Key 与模型。"})
            return
        if self.path in {"/api/analysis/config", "/api/analysis/test", "/api/analysis/reset", "/api/analysis/models"}:
            try:
                payload = self.read_json()
                if self.path == "/api/analysis/reset":
                    analysis_store.clear_config()
                    self.send_json(200, {"ok": True, "active_source": "DeepSeek 默认分析"})
                    return
                if self.path == "/api/analysis/models":
                    payload["model"] = payload.get("model") or "model-list"
                config = analysis_store.validate_config(payload, analysis_store.load_config())
                if self.path == "/api/analysis/models":
                    self.send_json(200, {"models": list_provider_models(config["format"], config["base_url"], config["api_key"])})
                elif self.path == "/api/analysis/test":
                    AnalysisClient(config).test_connection()
                    self.send_json(200, {"ok": True, "format": config["format"], "model": config["model"]})
                else:
                    analysis_store.save_config(config)
                    self.send_json(200, {"ok": True, "config": analysis_store.public_config()})
            except ApiError as error:
                self.send_json(error.status if 400 <= error.status < 600 else 502, {"error": error.message})
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json(400, {"error": str(error)})
            except Exception as error:
                print(f"Analysis configuration failed: {type(error).__name__}")
                self.send_json(502, {"error": "分析接口测试或保存失败，请检查链接、Key 和 Model。"})
            return
        if self.path in {"/api/mysql/test", "/api/mysql/config"}:
            try:
                payload = self.read_json()
                if not payload.get("password"):
                    previous = mysql_store.load_config()
                    if previous:
                        payload["password"] = previous["password"]
                config = mysql_store.validate_config(payload)
                if self.path == "/api/mysql/test":
                    version = mysql_store.test_connection(config)
                    self.send_json(200, {"ok": True, "version": version})
                else:
                    imported = mysql_store.save_config(config)
                    self.send_json(200, {"ok": True, "imported_runs": imported, "article_count": mysql_store.count_articles(config)})
            except (ValueError, RuntimeError) as error:
                self.send_json(400, {"error": str(error)})
            except Exception as error:
                print(f"MySQL configuration failed: {type(error).__name__}")
                self.send_json(502, {"error": "MySQL 连接或建表失败。请核对账号、密码及建库权限。"})
            return
        if self.path not in {"/api/run", "/api/run/start"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self.read_json()
            if len(clean_text(payload.get("task"), 1_000)) < 8:
                raise ApiError(400, "请用至少 8 个字说明议题、时段或关注重点。")
            if not DeepSeekClient().configured:
                raise ApiError(409, "未配置 DeepSeek API Key。请在配置中心填写后重试。")
            if not AnalysisClient().configured:
                raise ApiError(409, "未配置分析接口；请在页面填写接口链接、Key 和 Model。")
            if self.path == "/api/run/start":
                search_languages(str(payload.get("task")), str(payload.get("languages") or ""))
                search_window(str(payload.get("task")), payload.get("start_date"), payload.get("end_date"))
                job_id = launch_job(payload)
                self.send_json(202, {"job_id": job_id, "status": "running"})
            else:
                self.send_json(200, execute_research(payload, lambda _message: None))
        except ApiError as error:
            self.send_json(error.status, {"error": error.message})
        except Exception as error:  # Keep internal details out of the browser.
            print(f"Unhandled request error: {error!r}")
            self.send_json(500, {"error": "服务处理失败，请查看本地终端日志。"})


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    host = "127.0.0.1"
    port = int(os.getenv("PORT") or load_local_env().get("PORT", "8000"))
    start_meltwater_studio()
    try:
        mysql_config = mysql_store.load_config()
        if mysql_config:
            mysql_store.initialize_tables(mysql_config)
            mysql_store.sync_local_clips(mysql_config)
            indexed = rag_store.upsert_documents(mysql_store.rag_documents(mysql_config), prune=True)
            launch_news_social_vector_job()
            print(f"RAG index: {indexed['indexed']} new/updated, {indexed['unchanged']} unchanged")
            for paper in mysql_store.list_papers(mysql_config):
                if not rag_store.paper_vector_status(paper["id"])["complete"]:
                    launch_paper_vector_job(paper["id"])
    except Exception as error:
        print(f"MySQL schema check failed: {type(error).__name__}")
    print(f"溯源 demo 已启动：http://{host}:{port}")
    print("按 Ctrl+C 停止服务。")
    threading.Thread(target=watch_scheduler, daemon=True).start()
    ThreadingHTTPServer((host, port), SuyuanHandler).serve_forever()


if __name__ == "__main__":
    main()
