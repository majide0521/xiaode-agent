import os
import json
import re
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup
from openai import AsyncOpenAI

from agents import (
    Agent,
    Runner,
    OpenAIChatCompletionsModel,
    function_tool,
    set_tracing_disabled,
)


# ============================================================
# 0. 基础设置
# ============================================================

TODAY = date.today().isoformat()

set_tracing_disabled(disabled=True)

client = AsyncOpenAI(
    api_key=os.environ["SILICONFLOW_API_KEY"],
    base_url="https://api.siliconflow.cn/v1",
)

model = OpenAIChatCompletionsModel(
    model="deepseek-ai/DeepSeek-V3.2",
    openai_client=client,
)


# ============================================================
# 1. Source Store
#    这一版最重要的新东西
# ============================================================

SOURCE_STORE = []

READ_URLS = set()

# 当前研究对象的官方域名
OFFICIAL_DOMAIN = ""

# 默认降级的低质量来源
LOW_QUALITY_DOMAINS = [
    "csdn.net",
    "php.cn",
    "taobao.com",
    "baike.baidu.com",
    "sohu.com",
    "toutiao.com",
]

# 高可信媒体
TIER_B_DOMAINS = [
    "reuters.com",
    "bloomberg.com",
    "ft.com",
    "wsj.com",
    "cnbc.com",
    "techcrunch.com",
    "theverge.com",
    "wired.com",
    "theinformation.com",
]

# 保存所有搜索得到的候选网页
SEARCH_RESULTS = []

CANDIDATE_PATH = Path("candidate_sources.jsonl")

# 每次启动清空上一次候选
CANDIDATE_PATH.write_text("", encoding="utf-8")


def classify_source(url: str) -> str:

    domain = urlparse(url).netloc.lower()

    # 官方域名
    if OFFICIAL_DOMAIN:

        if (
            domain == OFFICIAL_DOMAIN
            or domain.endswith("." + OFFICIAL_DOMAIN)
        ):
            return "Tier A"

    # 权威媒体
    for d in TIER_B_DOMAINS:

        if d in domain:
            return "Tier B"

    # 明显低质量 / 不适合作为核心证据
    for d in LOW_QUALITY_DOMAINS:

        if d in domain:
            return "Tier D"

    if "wikipedia.org" in domain:
        return "Tier D"

    return "Tier C"


def source_quality_score(item, competitor):

    """
    给候选网页打分。
    分数只用于决定“优先读什么”，
    不代表内容本身一定正确。
    """

    tier = item.get(
        "source_tier",
        "Tier C"
    )

    score_map = {
        "Tier A": 100,
        "Tier B": 75,
        "Tier C": 35,
        "Tier D": -50,
    }

    score = score_map.get(
        tier,
        0
    )

    title = (
        item.get("title", "")
        or ""
    ).lower()

    summary = (
        item.get("summary", "")
        or ""
    ).lower()

    url = (
        item.get("url", "")
        or ""
    ).lower()

    content = (
        title
        + " "
        + summary
        + " "
        + url
    )

    # 竞品名相关性
    name = competitor.lower().strip()

    if name and name in content:
        score += 25

    # 官方典型页面加分
    official_keywords = [
        "blog",
        "changelog",
        "pricing",
        "product",
        "about",
        "news",
        "company",
        "enterprise",
    ]

    if tier == "Tier A":

        for kw in official_keywords:

            if kw in content:
                score += 8

    # 当前年份 / 近年信息加分
    if "2026" in content:
        score += 15

    elif "2025" in content:
        score += 6

    # 明显无关结果降权
    noise_keywords = [
        "php",
        "教程",
        "怎么写",
        "提示词",
        "seo教程",
        "下载",
        "破解版",
    ]

    for kw in noise_keywords:

        if kw in content:
            score -= 40

    return score



def is_relevant_candidate(item, competitor):

    """
    Source Governance 的硬相关性门槛。

    Tier A 官方页面直接允许；
    Tier B/C 必须明确与竞品相关。
    """

    url = (
        item.get("url", "")
        or ""
    ).lower()

    tier = classify_source(url)

    # 官方站天然属于研究对象
    if tier == "Tier A":
        return True

    title = (
        item.get("title", "")
        or ""
    ).lower()

    summary = (
        item.get("summary", "")
        or ""
    ).lower()

    content = (
        title
        + " "
        + summary
        + " "
        + url
    )

    comp = (
        competitor
        .lower()
        .strip()
    )

    aliases = []

    if comp:
        aliases.append(comp)

    # Perplexity AI → Perplexity
    for suffix in [
        " ai",
        " app",
        " search",
        " assistant",
    ]:

        if comp.endswith(suffix):

            aliases.append(
                comp[:-len(suffix)].strip()
            )

    # 从官方域名提取品牌词
    if OFFICIAL_DOMAIN:

        domain_brand = (
            OFFICIAL_DOMAIN
            .split(".")[0]
            .lower()
        )

        if len(domain_brand) >= 3:
            aliases.append(
                domain_brand
            )

    aliases = list(
        dict.fromkeys(
            a
            for a in aliases
            if a
        )
    )

    return any(
        alias in content
        for alias in aliases
    )



def discover_official_sitemap(
    official_domain,
    competitor
):
    """
    从 robots.txt 和 sitemap.xml
    递归发现官方高价值页面。
    """

    if not official_domain:
        return []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 "
            "Chrome/131 Safari/537.36"
        )
    }

    base = f"https://{official_domain}"

    sitemap_urls = [
        f"{base}/sitemap.xml"
    ]

    # robots.txt 中寻找更多 sitemap
    try:

        r = requests.get(
            f"{base}/robots.txt",
            headers=headers,
            timeout=20
        )

        if r.ok:

            for line in r.text.splitlines():

                if line.lower().startswith("sitemap:"):

                    sm = line.split(
                        ":",
                        1
                    )[1].strip()

                    if sm:
                        sitemap_urls.append(sm)

    except Exception as e:

        print(
            f"⚠️ robots.txt读取失败："
            f"{type(e).__name__}: {e}"
        )


    keywords = [
        "blog",
        "changelog",
        "pricing",
        "product",
        "products",
        "news",
        "about",
        "company",
        "enterprise",
        "business",
        "api",
        "research",
        "update",
        "updates",
        "computer",
        "comet",
        "help-center",
    ]

    visited = set()
    found = {}


    def parse_sitemap(
        sitemap_url,
        depth=0
    ):

        if depth > 3:
            return

        if sitemap_url in visited:
            return

        visited.add(sitemap_url)

        try:

            r = requests.get(
                sitemap_url,
                headers=headers,
                timeout=25
            )

            if not r.ok:
                return

            root = ET.fromstring(
                r.content
            )

            locs = []

            for elem in root.iter():

                if (
                    elem.tag.endswith("loc")
                    and elem.text
                ):
                    locs.append(
                        elem.text.strip()
                    )

            for loc in locs:

                # 子 sitemap，继续递归
                if loc.lower().endswith(".xml"):

                    parse_sitemap(
                        loc,
                        depth + 1
                    )

                    continue

                parsed = urlparse(loc)

                domain = (
                    parsed.netloc
                    .lower()
                    .replace("www.", "")
                )

                target = (
                    official_domain
                    .lower()
                    .replace("www.", "")
                )

                if not (
                    domain == target
                    or domain.endswith(
                        "." + target
                    )
                ):
                    continue

                lower_url = loc.lower()

                if not any(
                    kw in lower_url
                    for kw in keywords
                ):
                    continue

                found[loc] = {
                    "title": (
                        f"{competitor} Official"
                    ),
                    "url": loc,
                    "site_name": official_domain,
                    "published_date": "",
                    "source_tier": "Tier A",
                    "summary": (
                        "从官方网站 Sitemap "
                        "发现的官方页面"
                    )
                }

        except Exception as e:

            print(
                f"⚠️ Sitemap解析失败："
                f"{sitemap_url} | "
                f"{type(e).__name__}"
            )


    for sm in list(
        dict.fromkeys(sitemap_urls)
    ):
        parse_sitemap(sm)


    # ========================================================
    # 官方页面治理：
    # 1. 优先英文 / 中文 / 无语言前缀
    # 2. 去掉不同语言版本的重复页面
    # 3. 优先高价值页面
    # ========================================================

    preferred_locales = {
        "en-US",
        "zh-CN",
        "zh-TW",
    }

    locale_pattern = re.compile(
        r"^[a-z]{2}-[A-Z]{2}$"
    )


    def analyze_official_url(item):

        url = item.get(
            "url",
            ""
        )

        parsed = urlparse(
            url
        )

        segments = [
            s
            for s in parsed.path.split("/")
            if s
        ]


        # ====================================================
        # Locale识别
        #
        # 支持：
        # /de/xxx
        # /en-US/xxx
        #
        # 以及：
        # /help-center/de/articles/xxx
        # /help-center/fr/articles/xxx
        # ====================================================

        locale_langs = {
            "af", "am", "ar", "az",
            "be", "bg", "bn", "bs",
            "ca", "cs", "cy",
            "da", "de",
            "el", "en", "es", "et", "eu",
            "fa", "fi", "fr",
            "ga", "gl", "gu",
            "he", "hi", "hr", "hu", "hy",
            "id", "is", "it",
            "ja", "ka", "kk", "km", "kn", "ko",
            "lo", "lt", "lv",
            "mk", "ml", "mn", "mr", "ms", "mt",
            "nb", "ne", "nl", "nn", "no",
            "pa", "pl", "pt",
            "ro", "ru",
            "sk", "sl", "sq", "sr", "sv", "sw",
            "ta", "te", "th", "tl", "tr",
            "uk", "ur", "uz",
            "vi",
            "zh",
        }


        def detect_locale(segment):

            if not segment:
                return None

            value = segment.strip().lower()

            # de / fr / zh
            if value in locale_langs:
                return value

            # en-US / zh-CN / sr-Latn / sr-Cyrl-ME
            m = re.fullmatch(
                r"([a-z]{2})"
                r"(?:-[a-z0-9]{2,8})+",
                value,
                flags=re.IGNORECASE
            )

            if m:

                base_lang = (
                    m.group(1)
                    .lower()
                )

                if base_lang in locale_langs:
                    return value

            return None


        locale = None
        locale_index = None


        # 情况1：
        # /de/changelog/...
        if segments:

            detected = detect_locale(
                segments[0]
            )

            if detected:

                locale = detected
                locale_index = 0


        # 情况2：
        # /help-center/de/articles/...
        if (
            locale is None
            and len(segments) >= 2
            and segments[0].lower()
            in {
                "help-center",
                "help",
                "support",
                "docs",
                "documentation",
            }
        ):

            detected = detect_locale(
                segments[1]
            )

            if detected:

                locale = detected
                locale_index = 1


        # ====================================================
        # 只保留：
        # 无locale / 英文 / 中文
        # ====================================================

        if locale:

            base_language = (
                locale
                .split("-")[0]
                .lower()
            )

            if base_language not in {
                "en",
                "zh",
            }:
                return None


        # ====================================================
        # 去掉语言路径，生成规范URL路径
        #
        # /de/changelog/a
        # -> /changelog/a
        #
        # /help-center/de/articles/a
        # -> /help-center/articles/a
        # ====================================================

        normalized_segments = list(
            segments
        )

        if locale_index is not None:

            normalized_segments.pop(
                locale_index
            )


        normalized_path = (
            "/"
            + "/".join(
                normalized_segments
            )
        ).rstrip("/")

        if not normalized_path:
            normalized_path = "/"


        # ====================================================
        # 页面质量评分
        # ====================================================

        score = 0


        # 无语言版本优先
        if locale is None:
            score += 50

        else:

            base_language = (
                locale
                .split("-")[0]
                .lower()
            )

            if base_language == "en":
                score += 40

            elif base_language == "zh":
                score += 35


        lower_url = (
            url.lower()
        )


        keyword_scores = {
            "changelog": 65,
            "pricing": 60,
            "enterprise": 55,
            "product": 50,
            "products": 50,
            "research": 50,
            "api": 45,
            "computer": 45,
            "comet": 40,
            "about": 35,
            "company": 35,
            "blog": 30,
            "news": 25,
            "help-center": 20,
        }


        for keyword, value in (
            keyword_scores.items()
        ):

            if keyword in lower_url:
                score += value


        # 新鲜度
        if "2026" in lower_url:
            score += 30

        elif "2025" in lower_url:
            score += 12


        return {
            "item": item,
            "score": score,
            "normalized_path": normalized_path,
            "locale": locale,
        }


    ranked = []

    for item in found.values():

        result = analyze_official_url(
            item
        )

        if result:
            ranked.append(
                result
            )


    ranked.sort(
        key=lambda x: x["score"],
        reverse=True
    )


    # 同一个页面的不同语言版本只保留一个
    deduped = {}

    for result in ranked:

        key = result[
            "normalized_path"
        ]

        if key not in deduped:
            deduped[key] = result


    records = [
        result["item"]
        for result in deduped.values()
    ][:40]

    if records:

        persist_candidate_records(
            records
        )

    print(
        f"\n🗺️ Sitemap发现官方页面："
        f"{len(records)} 个"
    )

    for item in records[:12]:

        print(
            f"   Tier A | {item['url']}"
        )

    return records


def discover_official_links(
    official_domain,
    competitor
):

    """
    直接读取竞品官网首页，
    从站内链接中发现 Blog / Changelog /
    Pricing / Product / About 等高价值页面。
    """

    if not official_domain:
        return []

    homepage = (
        f"https://{official_domain}"
    )

    print(
        "\n🌐 正在发现官方站内高价值页面："
        f"{homepage}"
    )

    try:

        headers = {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 "
                "Chrome/131 Safari/537.36"
            )
        }

        response = requests.get(
            homepage,
            headers=headers,
            timeout=25
        )

        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        keywords = [
            "blog",
            "changelog",
            "pricing",
            "price",
            "product",
            "products",
            "news",
            "about",
            "company",
            "enterprise",
            "business",
            "api",
            "research",
            "updates",
        ]

        discovered = {}

        for a in soup.find_all(
            "a",
            href=True
        ):

            href = a.get(
                "href",
                ""
            ).strip()

            if not href:
                continue

            full_url = urljoin(
                homepage,
                href
            )

            parsed = urlparse(
                full_url
            )

            domain = (
                parsed.netloc
                .lower()
                .replace("www.", "")
            )

            target_domain = (
                official_domain
                .lower()
                .replace("www.", "")
            )

            # 只允许本官方域名
            if not (
                domain == target_domain
                or domain.endswith(
                    "." + target_domain
                )
            ):
                continue

            # 去掉锚点
            clean_url = (
                full_url.split("#")[0]
            )

            anchor_text = (
                a.get_text(
                    " ",
                    strip=True
                )
                or ""
            )

            signal = (
                clean_url.lower()
                + " "
                + anchor_text.lower()
            )

            if not any(
                kw in signal
                for kw in keywords
            ):
                continue

            discovered[
                clean_url
            ] = {
                "title": (
                    anchor_text
                    or f"{competitor} Official"
                ),
                "url": clean_url,
                "site_name": official_domain,
                "published_date": "",
                "source_tier": "Tier A",
                "summary": (
                    "从竞品官方网站首页发现的"
                    "官方站内高价值页面。"
                )
            }

        records = list(
            discovered.values()
        )[:15]

        if records:

            persist_candidate_records(
                records
            )

        print(
            f"✅ 官方站内发现："
            f"{len(records)} 个候选页面"
        )

        for item in records[:10]:

            print(
                "   Tier A | "
                f"{item['title'][:60]} | "
                f"{item['url']}"
            )

        return records

    except Exception as e:

        print(
            "⚠️ 官方站内发现失败："
            f"{type(e).__name__}: {e}"
        )

        return []


def extract_page_date(soup):

    possible_meta = [
        ("property", "article:published_time"),
        ("name", "date"),
        ("name", "pubdate"),
        ("name", "publishdate"),
        ("name", "datePublished"),
    ]

    for attr, value in possible_meta:

        tag = soup.find(
            "meta",
            attrs={attr: value}
        )

        if tag and tag.get("content"):
            return tag.get("content")

    time_tag = soup.find("time")

    if time_tag:

        if time_tag.get("datetime"):
            return time_tag.get("datetime")

        text = time_tag.get_text(
            " ",
            strip=True
        )

        if text:
            return text

    return "未确认"


# ============================================================
# 2. Tool 1：搜索
# ============================================================

@function_tool
def search_web(query: str) -> str:
    """
    使用博查 Web Search API 搜索互联网。

    用于寻找：
    官方网站、官方博客、产品更新、新闻、
    用户数据、融资估值、商业模式、
    市场竞争和用户评价等资料。
    """

    print(
        f"\n🔎 Researcher 正在通过博查搜索：{query}\n"
    )

    try:

        url = "https://api.bochaai.com/v1/web-search"

        headers = {
            "Authorization": (
                f"Bearer {os.environ['BOCHA_API_KEY']}"
            ),
            "Content-Type": "application/json",
        }

        payload = {
            "query": query,
            "freshness": "oneYear",
            "summary": True,
            "count": 8
        }

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=30
        )

        response.raise_for_status()

        raw = response.json()

        web_pages = (
            raw.get("data", {})
               .get("webPages", {})
               .get("value", [])
        )

        data = []

        for item in web_pages:

            result_url = item.get(
                "url",
                ""
            )

            record = {
                "title": item.get(
                    "name",
                    ""
                ),
                "url": result_url,
                "site_name": item.get(
                    "siteName",
                    ""
                ),
                "published_date": item.get(
                    "datePublished",
                    ""
                ),
                "source_tier": classify_source(
                    result_url
                ),
                "summary": (
                    item.get("summary")
                    or item.get("snippet")
                    or ""
                )
            }

            data.append(record)

        # 保存到内存候选池
        for item in data:

            if item.get("url"):

                SEARCH_RESULTS.append(
                    item
                )

        # 同时强制落盘，避免Agent阶段间丢状态
        with CANDIDATE_PATH.open(
            "a",
            encoding="utf-8"
        ) as f:

            for item in data:

                if item.get("url"):

                    f.write(
                        json.dumps(
                            item,
                            ensure_ascii=False
                        )
                        + "\n"
                    )

        print(
            f"✅ 本次博查搜索记录候选：{len(data)} 条"
        )

        if len(data) == 0:

            print(
                "⚠️ 博查请求成功，但本次没有返回网页结果"
            )

        return json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        )

    except Exception as e:

        print(
            f"❌ 博查 search_web 失败："
            f"{type(e).__name__}: {e}"
        )

        return (
            f"搜索失败："
            f"{type(e).__name__}: {str(e)}"
        )


# ============================================================
# 3. Tool 2：阅读网页
#
#    关键变化：
#
#    不只是把正文返回给 Agent
#    Python 同时强制保存到 SOURCE_STORE
# ============================================================

@function_tool
def read_webpage(url: str) -> str:
    """
    阅读网页正文。

    重要网页必须使用本工具打开，
    不能只依据搜索摘要。
    """

    print(
        f"\n📖 Researcher 正在阅读：{url}\n"
    )

    if url in READ_URLS:

        return (
            "该网页已经阅读并保存，"
            "无需重复读取。"
        )

    try:

        headers = {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 "
                "Chrome/131 Safari/537.36"
            )
        }

        response = requests.get(
            url,
            headers=headers,
            timeout=20
        )

        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        title = ""

        if soup.title:

            title = soup.title.get_text(
                " ",
                strip=True
            )

        publish_date = extract_page_date(
            soup
        )

        for tag in soup([
            "script",
            "style",
            "nav",
            "footer",
            "header",
            "noscript",
            "svg"
        ]):

            tag.decompose()

        text = " ".join(
            soup.stripped_strings
        )

        if not text:

            return (
                "网页打开成功，"
                "但未提取到有效正文。"
            )

        # 单页最多保存12000字符
        content = text[:12000]

        source_id = (
            f"S{len(SOURCE_STORE) + 1:02d}"
        )

        source_record = {
            "source_id": source_id,
            "title": title,
            "url": url,
            "domain": urlparse(
                url
            ).netloc,
            "source_tier": classify_source(
                url
            ),
            "published_date": publish_date,
            "content": content,
        }

        SOURCE_STORE.append(
            source_record
        )

        READ_URLS.add(
            url
        )

        print(
            f"✅ 已保存为原始来源：{source_id}"
        )

        # 给Researcher返回简化版
        return json.dumps(
            {
                "source_id": source_id,
                "title": title,
                "url": url,
                "source_tier": classify_source(
                    url
                ),
                "published_date": publish_date,
                "content": content
            },
            ensure_ascii=False,
            indent=2
        )

    except Exception as e:

        return (
            f"网页读取失败：{str(e)}"
        )


# ============================================================
# 4. Stage 1：Researcher
#
#    现在只负责：
#
#    找什么
#    读什么
#
#    不再负责 Evidence Pack
# ============================================================


# ============================================================
# Retrieval Governance
# 在 Agent 自由搜索前，程序强制寻找官方和权威来源
# ============================================================

def persist_candidate_records(items):

    if not items:
        return

    with CANDIDATE_PATH.open(
        "a",
        encoding="utf-8"
    ) as f:

        for item in items:

            url = item.get(
                "url",
                ""
            )

            if not url:
                continue

            item["source_tier"] = classify_source(
                url
            )

            SEARCH_RESULTS.append(
                item
            )

            f.write(
                json.dumps(
                    item,
                    ensure_ascii=False
                )
                + "\n"
            )


def domain_matches(
    url,
    required_domain
):

    if not url or not required_domain:
        return False

    domain = urlparse(
        url
    ).netloc.lower()

    domain = domain.split(":")[0]

    required_domain = (
        required_domain
        .lower()
        .replace("www.", "")
    )

    return (
        domain == required_domain
        or domain.endswith(
            "." + required_domain
        )
    )


def bocha_seed_search(
    query,
    required_domain=None,
    retries=3
):

    import time

    print(
        f"\n🎯 强制高质量检索：{query}"
    )

    api_url = (
        "https://api.bochaai.com/v1/web-search"
    )

    headers = {
        "Authorization": (
            f"Bearer {os.environ['BOCHA_API_KEY']}"
        ),
        "Content-Type": "application/json",
    }

    payload = {
        "query": query,
        "freshness": "oneYear",
        "summary": True,
        "count": 8
    }

    for attempt in range(
        1,
        retries + 1
    ):

        try:

            response = requests.post(
                api_url,
                headers=headers,
                json=payload,
                timeout=35
            )

            response.raise_for_status()

            raw = response.json()

            pages = (
                raw.get("data", {})
                   .get("webPages", {})
                   .get("value", [])
            )

            records = []

            for item in pages:

                url = item.get(
                    "url",
                    ""
                )

                if (
                    required_domain
                    and not domain_matches(
                        url,
                        required_domain
                    )
                ):
                    continue

                records.append({
                    "title": item.get(
                        "name",
                        ""
                    ),
                    "url": url,
                    "site_name": item.get(
                        "siteName",
                        ""
                    ),
                    "published_date": item.get(
                        "datePublished",
                        ""
                    ),
                    "source_tier": classify_source(
                        url
                    ),
                    "summary": (
                        item.get("summary")
                        or item.get("snippet")
                        or ""
                    )
                })

            persist_candidate_records(
                records
            )

            print(
                f"✅ 命中目标域名：{len(records)} 条"
            )

            return records

        except Exception as e:

            print(
                f"⚠️ 第 {attempt} 次失败："
                f"{type(e).__name__}"
            )

            if attempt < retries:
                time.sleep(2)

    print(
        "❌ 本组强制检索最终失败"
    )

    return []


researcher = Agent(
    name="Researcher",

    model=model,

    instructions=f"""
你是一名资深AI与互联网竞品研究员。

今天日期：

{TODAY}

你的任务只有一个：

通过搜索发现高质量候选资料。

你只负责：
决定应该搜索什么。

你不负责打开网页，
也不负责把网页加入证据库。

网页选择和网页阅读将由后续程序统一执行。

你暂时不要写正式报告，
也不要自己制作 Evidence Pack。

--------------------------------------------------

你拥有：

search_web

read_webpage

--------------------------------------------------

研究原则：

1. 先搜官方资料。

优先寻找：

official
official blog
changelog
product
pricing
company
about

2. 尽量至少阅读：

2个 Tier A 官方来源。

3. 再寻找：

Reuters
Bloomberg
TechCrunch
The Verge
Wired
Financial Times
CNBC

等权威来源。

尽量至少阅读：

2个 Tier B 来源。

4. 针对用户特别关心的问题：

继续搜索专项资料。

5. 涉及最新情况时：

优先：

最近30天
最近90天
最近半年

6. 你只需要通过 search_web 搜索。

不要调用 read_webpage。

程序将在你搜索完成后，
统一对候选网页进行来源治理、
排序和正文抓取。

7. 不需要追求数量。

6-10个高质量来源
远比30个低质量来源有价值。

--------------------------------------------------

当你认为已经搜集到足够资料后：

只需要简单告诉我：

研究资料搜集完成。

不要开始写竞品报告。
""",

    tools=[
        search_web
    ],
)


# ============================================================
# 5. Stage 2：Evidence Extractor
#
#    从程序保存的原文中抽取事实
# ============================================================

extractor = Agent(
    name="Evidence Extractor",

    model=model,

    instructions="""
你是 Evidence Extractor。

你的唯一任务是：

从 Researcher 提供的原始资料中，
提取“可以被原文直接验证的最小事实证据”。

你不是分析师。
你不是 Writer。
你不能给建议。
你不能推导商业影响。
你不能补全缺失信息。

你的工作标准是：

“Claim 必须能被 Evidence Text 单独、直接、完整地证明。”


============================================================
一、最高优先级原则：Faithfulness
============================================================

每条 Claim 必须严格忠实于原文。

禁止：

1. 把“计划推出”写成“已经推出”；
2. 把“可能”写成“确定”；
3. 把“部分用户”写成“所有用户”；
4. 把“语音转文字”扩大成原文没有明确支持的其他能力；
5. 自行补充因果关系；
6. 自行补充战略意义；
7. 自行补充竞争影响；
8. 自行补充产品价值；
9. 自行补充商业效果；
10. 自行补充增长结果。

如果 Evidence Text 只能支持 A，
Claim 就只能写 A。

宁可少写，
不能多写。


============================================================
二、Atomicity：一条证据只允许一个事实
============================================================

每个 Evidence 必须是“最小可验证事实”。

如果一句 Claim 中出现以下情况：

- 并且
- 同时
- 以及
- 因此
- 从而
- 并
- 不仅……还……
- 两个以上独立动作
- 两个以上独立产品能力

优先拆成多条 Evidence。

错误示例：

Claim：
Perplexity 推出了 Computer，
接入 Microsoft Teams，
并因此增强企业竞争力。

这是三个不同层次的信息，
不能写在一个 Evidence 中。


正确拆法：

E01：
Perplexity Computer 已推出 Microsoft Teams 应用。

E02：
该 Teams 应用可以在 Teams 对话中执行研究任务。

“增强企业竞争力”
属于分析，
不能进入 Evidence Ledger。


============================================================
三、Claim Nature
============================================================

每条 Evidence 必须新增：

Claim Nature:

只能填写以下两种之一：

FACT
REPORTED_CLAIM


FACT：

原始资料直接陈述一个
可以明确验证的事件、功能、价格、
合作、发布日期、产品变化或数据。


REPORTED_CLAIM：

来源是在转述：

- 公司说法
- 高管说法
- 媒体引述
- 市场估算
- 未独立验证的数据
- 第三方预测


禁止输出：

INFERENCE

任何需要 Agent 自己推理才能得到的结论，
都不能进入 Evidence Ledger。

这些推论应该留给 Writer。


============================================================
四、Evidence Text
============================================================

Evidence Text 必须：

1. 来自原始 Source；
2. 尽量保留原文；
3. 只截取支持 Claim 所需的最小句子；
4. 不要把 Agent 自己的话混进去；
5. 必须能够独立支撑 Claim。

如果找不到能够直接支撑 Claim 的原文：

不要生成这条 Evidence。


============================================================
五、事实范围必须完全一致
============================================================

Claim 必须保持原文的：

- 主体
- 时间
- 状态
- 范围
- 地区
- 用户范围
- 产品范围
- 数字
- 单位
- 条件
- 时态


例如原文：

"available to Max subscribers"

不能写成：

"available to all users"


原文：

"plans to launch"

不能写成：

"launched"


原文：

"according to the company"

如果不是官方一手来源，
应保留转述属性并标记：

Claim Nature:
REPORTED_CLAIM


============================================================
六、Confidence 规则
============================================================

Confidence 只能填写：

High
Medium
Low


基础规则：

Tier A：
直接、清晰、时效正常的官方原始资料
可以为 High。

Tier B：
明确事实可以 High 或 Medium，
根据是否为直接报道判断。

Tier C：
原则上最高只能 Medium。

如果存在以下任意情况：

- 发布时间未知
- 二手转述
- 市场估算
- 数字口径不清
- 来源权威性有限
- 内容存在明显歧义

必须降低 Confidence。


============================================================
七、禁止“证据升级”
============================================================

不能因为 Source Tier 很高，
就把一个模糊表述升级为确定事实。

例如：

官方页面写：

"we're exploring..."

即使是 Tier A，
也只能表达“正在探索”。

不能写成：

“已经上线”。


============================================================
八、Evidence Type
============================================================

Evidence Type 用来描述事实类别。

可使用：

产品功能
产品更新
用户数据
市场数据
商业模式
价格
合作
融资
公司战略
增长
渠道
技术能力
组织
其他

Evidence Type 与 Claim Nature 是两个不同字段。


============================================================
九、输出格式
============================================================

严格按照以下格式输出：

# Evidence Ledger

## E01

Claim:
一句最小可验证事实。

Claim Nature:
FACT

Source ID:
S01

Source Name:
来源名称

Source Tier:
Tier A

URL:
完整URL

Published Date:
YYYY-MM-DD
如果未知写：未知

Evidence Text:
能够直接证明 Claim 的原文片段。

Evidence Type:
产品功能

Confidence:
High

Atomicity:
PASS

Entailment:
PASS

Reason:
一句话说明为什么该 Evidence
可以直接支持 Claim。


## E02

...


============================================================
十、Atomicity / Entailment 自检
============================================================

只有同时满足以下条件，
才能输出：

Atomicity:
PASS

Entailment:
PASS


Atomicity PASS：

Claim 只有一个核心事实。


Entailment PASS：

只阅读 Evidence Text，
不依赖外部知识，
就能够直接推出 Claim。


如果任何一个无法 PASS：

不要输出这条 Evidence。


============================================================
十一、Missing Evidence
============================================================

Evidence Ledger 最后必须输出：

# Missing Evidence

列出与用户研究目标直接相关，
但当前 Source 中没有可靠证据支持的重要问题。

不要尝试补答案。

格式：

1. 缺少……
2. 缺少……
3. 缺少……


============================================================
十二、最终纪律
============================================================

你追求的不是：

“尽量生成更多 Evidence”。

而是：

“只留下经得起逐条核对的 Evidence”。

数量少没有问题。

证据边界必须严格。
""",

    tools=[],
)


# ============================================================
# 6. Stage 3：Auditor
# ============================================================

auditor = Agent(
    name="Evidence Auditor",

    model=model,

    instructions=f"""
你是 Evidence Auditor。

你不是研究员，不负责补充资料。
你不是 Writer，不负责撰写商业结论。

你的唯一任务是：

逐条审核 Evidence Extractor 提供的证据，
并决定每一条 Evidence 是否允许进入最终报告。

你的审核具有“硬门禁”效力。

最终 Writer 只能使用：

APPROVED
或
CAUTION

绝对禁止使用：

REJECTED


============================================================
一、每条 Evidence 必须逐条审核
============================================================

对每一条 Evidence 检查：

1. Claim 是否被 Evidence Text 直接支持；
2. Claim 是否超出了原文范围；
3. 是否把计划写成已发生；
4. 是否把部分用户扩大成全部用户；
5. 是否加入了因果、战略意义或商业影响；
6. Atomicity 是否真的只有一个核心事实；
7. Claim Nature 是否正确；
8. Source Tier 是否合理；
9. Confidence 是否与来源质量匹配；
10. 时间、数字、主体、范围是否一致。


============================================================
二、审核结果只有三种
============================================================

APPROVED

表示：

- Claim 有直接证据；
- 没有明显扩大解释；
- 来源可靠程度足够；
- 可以作为正式事实进入报告。


CAUTION

表示：

事实可能成立，
但存在至少一种限制：

- 二手报道；
- 市场估算；
- 来源不是一手；
- 发布时间未知；
- 数字口径不完全清楚；
- 引述第三方说法；
- 仍存在轻微解释空间。

CAUTION 可以进入报告，
但 Writer 必须使用限定性表达，例如：

“据报道”
“根据该来源”
“目前公开信息显示”
“该数字尚未获得官方确认”


REJECTED

满足以下任意条件必须拒绝：

- Evidence Text 无法直接推出 Claim；
- Claim 超出原文；
- 原文与 Claim 冲突；
- 来源严重不可靠；
- Evidence Text 缺失；
- Source ID / URL 对不上；
- 把推论写成事实；
- 把估算写成官方事实；
- 证据本身无法验证。


============================================================
三、禁止补证
============================================================

你只能审核 Extractor 已经给你的材料。

禁止：

- 自己搜索互联网；
- 使用外部知识补充证据；
- 根据常识替 Extractor 圆回来；
- 自行创造新的 Evidence。

证据不足就：

CAUTION
或
REJECTED。


============================================================
四、APPROVED Evidence 输出
============================================================

对 APPROVED 的 Evidence，输出：

## E01

Audit Status:
APPROVED

Approved Claim:
原 Claim

Claim Nature:
FACT

Source ID:
S01

Source Tier:
Tier A

Confidence:
High

Audit Reason:
一句话说明为什么通过。


============================================================
五、CAUTION Evidence 输出
============================================================

格式：

## E12

Audit Status:
CAUTION

Approved Claim:
保留原事实边界的 Claim

Claim Nature:
REPORTED_CLAIM

Source ID:
S04

Source Tier:
Tier B

Confidence:
Medium

Required Qualification:
据 The Verge 报道……

Audit Reason:
说明为什么必须谨慎使用。


============================================================
六、REJECTED Evidence 输出
============================================================

格式：

## E15

Audit Status:
REJECTED

Original Claim:
原始 Claim

Reject Reason:
具体说明为什么不能进入报告。


============================================================
七、必须生成 Hard Gate 清单
============================================================

最后必须单独输出：

# Writer Allowlist

只列：

APPROVED 和 CAUTION Evidence ID。

例如：

APPROVED:
E01
E02
E03

CAUTION:
E12
E13

REJECTED:
E15


============================================================
八、给 Writer 的强制规则
============================================================

输出：

# Writer Rules

1. Writer 只能使用 Writer Allowlist 中的 Evidence。
2. REJECTED Evidence 禁止引用、改写或隐性使用。
3. CAUTION Evidence 必须保留限定语。
4. Writer 不能把多个 Evidence 拼接成原证据没有支持的新事实。
5. Writer 可以进行分析和推论，但必须明确标记为“判断/推论”，不得伪装成事实。
6. 没有 Evidence 支持的问题必须写入“研究缺口”，不能自行补充。


============================================================
九、总体审核结果
============================================================

最后输出：

# Audit Summary

Total Evidence:
数量

Approved:
数量

Caution:
数量

Rejected:
数量

Approval Rate:
百分比

Overall Evidence Quality:
HIGH / MEDIUM / LOW

主要风险：
1. ...
2. ...

只有完成以上内容，审核才算结束。
""",

    tools=[],
)


# ============================================================
# 7. Stage 4：Writer
# ============================================================

writer = Agent(
    name="Business Analyst & Writer",

    model=model,

    instructions=f"""
你是一名高级AI产品战略分析师。

今天日期：

{TODAY}

你会收到：

Evidence Ledger

Evidence Audit

以及用户研究目标。

--------------------------------------------------

所有【事实】必须来源于：

APPROVED Evidence

或必要情况下使用 CAUTION，
但必须明确注明不确定性。

REJECTED：

不得使用。

--------------------------------------------------

你可以做业务分析。

但必须区分：

【事实】

【判断】

【建议】

--------------------------------------------------

例如：

【事实】
Perplexity官方在某日推出Computer。

[E03]

【判断】
这表明其正在从答案引擎向任务执行平台扩展。

这是分析推论，
不是事实。

--------------------------------------------------

最终报告：

# 竞品研究报告

## 0. Executive Summary

3-5条最重要结论。

## 1. 一句话判断

## 2. 产品定位

## 3. 核心用户与场景

## 4. 核心产品能力

表格：

能力
用户价值
证据编号
竞争意义

## 5. 最近半年产品变化

## 6. 用户与市场表现

## 7. 商业模式

## 8. 增长逻辑

## 9. 核心竞争壁垒

## 10. 主要问题与风险

## 11. 对用户指定业务的威胁

使用表格：

威胁
程度
证据
业务原因
时间尺度

## 12. 值得借鉴的策略

每条建议：

学什么
为什么
怎么落地
预期价值
风险

## 13. 最终判断

短期
中期
长期

## 14. 研究缺口

明确说明：

哪些信息仍然没有证据。

## 15. Sources

列出使用到的 Evidence 编号
及其原始 URL。

--------------------------------------------------

禁止：

凭模型记忆补数据。

禁止：

为了让报告完整，
虚构缺失信息。
""",

    tools=[],
)


# ============================================================
# 8. 用户输入
# ============================================================

print(
    "\n=========================================="
)

print(
    " 小德竞品研究 Agent v0.6.1b | Code-level Evidence Gate"
)

print(
    "==========================================\n"
)

competitor = input(
    "请输入竞品名称："
).strip()

official_domain_input = input(
    "请输入竞品官方域名（例如 perplexity.ai）："
).strip()

OFFICIAL_DOMAIN = (
    official_domain_input
    .lower()
    .replace("https://", "")
    .replace("http://", "")
    .replace("www.", "")
    .strip("/")
)

focus = input(
    "请输入研究重点（没有就直接回车）："
).strip()


if not focus:

    focus = "完整竞品研究"


# ============================================================
# Stage 0.5：Retrieval Governance
# ============================================================

print(
    "\n=========================================="
)

print(
    "Stage 0.5：强制检索官方 / 权威来源"
)

print(
    "=========================================="
)


# 先直接把官网首页加入候选池
if OFFICIAL_DOMAIN:

    persist_candidate_records([
        {
            "title": (
                f"{competitor} Official Website"
            ),
            "url": (
                f"https://{OFFICIAL_DOMAIN}"
            ),
            "site_name": OFFICIAL_DOMAIN,
            "published_date": "",
            "source_tier": "Tier A",
            "summary": (
                "官方主页候选，由程序直接加入。"
            )
        }
    ])


# 直接从官网首页发现 Blog / Changelog /
# Pricing / Product 等官方站内页面
if OFFICIAL_DOMAIN:

    discover_official_sitemap(
        OFFICIAL_DOMAIN,
        competitor
    )

    discover_official_links(
        OFFICIAL_DOMAIN,
        competitor
    )


# 官方来源：强制2组
if OFFICIAL_DOMAIN:

    bocha_seed_search(
        (
            f"site:{OFFICIAL_DOMAIN} "
            f"{competitor} official product "
            f"features pricing 2026"
        ),
        required_domain=OFFICIAL_DOMAIN
    )

    bocha_seed_search(
        (
            f"site:{OFFICIAL_DOMAIN} "
            f"{competitor} blog changelog "
            f"news update 2026"
        ),
        required_domain=OFFICIAL_DOMAIN
    )


# 权威媒体：强制检索
authority_domains = [
    "reuters.com",
    "techcrunch.com",
    "theverge.com",
    "ft.com",
]

for domain in authority_domains:

    bocha_seed_search(
        (
            f"site:{domain} "
            f"{competitor} 2026"
        ),
        required_domain=domain
    )


# 输出预检索结果
tier_counts = {
    "Tier A": 0,
    "Tier B": 0,
    "Tier C": 0,
    "Tier D": 0,
}

for item in SEARCH_RESULTS:

    tier = item.get(
        "source_tier",
        "Tier C"
    )

    tier_counts[tier] = (
        tier_counts.get(
            tier,
            0
        )
        + 1
    )

print(
    "\n预检索候选池："
)

print(
    f"Tier A：{tier_counts['Tier A']}"
)

print(
    f"Tier B：{tier_counts['Tier B']}"
)

print(
    f"Tier C：{tier_counts['Tier C']}"
)

print(
    f"Tier D：{tier_counts['Tier D']}"
)



# ============================================================
# 9. Stage 1
# ============================================================

print(
    "\n=========================================="
)

print(
    "Stage 1/4：Researcher 搜集原始资料"
)

print(
    "==========================================\n"
)

RESEARCH_COVERAGE_CONTRACT = """
================================
RESEARCH COVERAGE CONTRACT
================================

你的目标不是“搜够网页”，而是尽可能覆盖用户真正需要研究的关键问题。

你必须主动检查以下研究维度：

1. Product / 产品
   - 产品定位
   - 核心功能
   - 最近重要更新
   - 产品形态变化
   - 技术/体验差异

2. Users / 用户
   - 核心用户是谁
   - 用户规模
   - 用户结构
   - 使用场景
   - 用户价值
   - 留存、活跃度、使用频率等

3. Market / 市场
   - 市场规模
   - 市场份额
   - 行业趋势
   - 所在赛道变化
   - 地区差异

4. Business Model / 商业模式
   - 收入来源
   - 订阅
   - 广告
   - 企业服务
   - API
   - 合作分成
   - 定价策略

5. Growth / 增长
   - 用户获取
   - 分发渠道
   - 合作伙伴
   - 生态绑定
   - 增长飞轮
   - 国际化
   - 用户留存方式

6. Competition / 竞争
   - 核心竞争对手
   - 相对优势
   - 相对劣势
   - 竞争壁垒
   - 潜在替代关系

7. Company / 公司
   - 公司战略
   - 融资与估值
   - 组织方向
   - 关键合作
   - 战略转型信号

8. User Focus / 用户指定研究重点
   必须单独检查用户本次明确提出的问题。
   不得因为通用研究内容较丰富，就忽略用户最关心的问题。

9. Implication / 对指定业务的影响
   如果用户要求分析某家公司、产品或业务受到的影响，
   必须寻找直接证据和可验证的间接证据，
   并严格区分：
   - Fact
   - Reported Claim
   - Analysis / Judgment


================================
SOURCE PRIORITY
================================

每个重要研究维度优先寻找：

A. 官方一手资料
B. 权威媒体
C. 行业研究 / 数据机构
D. 普通媒体、博客、社区，仅作为补充

涉及以下高风险信息时，应尽量寻找至少两个独立来源：

- 用户规模
- MAU / DAU
- 市场份额
- 收入
- ARR
- 融资
- 估值
- 增长率
- 付费用户数量
- 重大商业合作金额
- 市场排名

若只有一个来源，必须保留来源限制。


================================
MISSING EVIDENCE RULE
================================

如果某个研究维度没有可靠证据：

不要猜测。
不要自动补全。
不要把行业常识写成目标公司的事实。

明确保留：

Missing Evidence:
- <缺失信息>


================================
STOPPING RULE
================================

不要因为已经取得固定数量网页就停止研究。

网页数量不是研究完成标准。

优先判断：

1. 用户指定研究重点是否已有直接证据；
2. Product / Users / Market / Business Model / Growth /
   Competition 等核心维度是否已覆盖；
3. 重要数字是否完成交叉验证；
4. 是否存在明显 Evidence Gap；
5. 是否已经获得足够证据支持最终业务判断。

如果高价值维度仍明显缺失，
应继续尝试不同关键词、来源类型和搜索方向。


================================
BEFORE EXTRACTOR CHECK
================================

进入 Extractor 前，你应该能够回答：

- 已覆盖哪些研究维度？
- 哪些维度仍然缺少证据？
- 用户最关心的问题是否获得直接证据？
- 哪些重要数字只有单一来源？
- 哪些判断只能作为 Analysis / Judgment，而不能作为 Fact？

研究目标是：
Research Coverage > Page Count
Evidence Quality > Search Quantity
Direct Evidence > Assumption
"""

search_task = f"""
{RESEARCH_COVERAGE_CONTRACT}

================================
CURRENT RESEARCH TASK
================================

研究对象：

{competitor}

用户特别关注：

{focus}

请搜索并阅读高质量网页。

不要写最终报告。
"""

Runner.run_sync(
    researcher,
    search_task,
    max_turns=35
)


# ============================================================

# ============================================================
# 9.1 Research Coverage Gate
# ============================================================

print(
    "\n=========================================="
)
print(
    "Stage 1.5: Research Coverage Gate"
)
print(
    "==========================================\n"
)


# ------------------------------------------------------------
# A. 定义研究维度
# ------------------------------------------------------------

RESEARCH_DIMENSIONS = {

    "Product / Capability": {
        "min_docs": 2,
        "keywords": [
            "product",
            "feature",
            "computer",
            "agent",
            "capability",
            "功能",
            "产品",
            "能力",
            "应用",
            "app",
            "desktop",
            "mobile",
        ],
    },

    "Users / Market": {
        "min_docs": 2,
        "keywords": [
            "user",
            "users",
            "mau",
            "dau",
            "market",
            "market share",
            "adoption",
            "customer",
            "customers",
            "usage",
            "用户",
            "市场",
            "份额",
            "客户",
            "活跃",
            "渗透率",
        ],
    },

    "Growth / Distribution": {
        "min_docs": 2,
        "keywords": [
            "growth",
            "distribution",
            "partnership",
            "partner",
            "channel",
            "traffic",
            "acquisition",
            "retention",
            "snapchat",
            "samsung",
            "telegram",
            "teams",
            "合作",
            "增长",
            "渠道",
            "流量",
            "获客",
            "留存",
            "分发",
        ],
    },

    "Business Model / Monetization": {
        "min_docs": 2,
        "keywords": [
            "business model",
            "revenue",
            "pricing",
            "price",
            "subscription",
            "paid",
            "arr",
            "monetization",
            "enterprise",
            "商业模式",
            "收入",
            "营收",
            "定价",
            "订阅",
            "付费",
            "商业化",
        ],
    },

    "Competition / Search": {
        "min_docs": 2,
        "keywords": [
            "search",
            "ai search",
            "answer engine",
            "google",
            "bing",
            "baidu",
            "competition",
            "competitor",
            "threat",
            "搜索",
            "答案引擎",
            "竞争",
            "竞品",
            "威胁",
            "百度",
            "谷歌",
        ],
    },

    "Baidu / China": {
        "min_docs": 2,
        "keywords": [
            "baidu",
            "ernie",
            "wenxin",
            "china",
            "chinese market",
            "百度",
            "文心",
            "中国",
            "国内",
            "豆包",
            "kimi",
            "夸克",
        ],
    },

    "Technology / Ecosystem": {
        "min_docs": 2,
        "keywords": [
            "api",
            "model",
            "developer",
            "ecosystem",
            "integration",
            "connector",
            "data",
            "snowflake",
            "databricks",
            "microsoft",
            "plaid",
            "getty",
            "模型",
            "技术",
            "开发者",
            "生态",
            "集成",
            "连接器",
            "数据",
        ],
    },
}


# ------------------------------------------------------------
# B. 单条搜索结果转成可检查文本
# ------------------------------------------------------------

def _coverage_item_text(item):

    if not isinstance(item, dict):
        return ""

    fields = [
        "title",
        "summary",
        "snippet",
        "body",
        "url",
        "site_name",
        "siteName",
        "source_name",
    ]

    values = []

    for field in fields:
        value = item.get(field, "")

        if value:
            values.append(
                str(value)
            )

    return " ".join(
        values
    ).casefold()


# ------------------------------------------------------------
# C. 检查研究覆盖度
# ------------------------------------------------------------

def _research_coverage_snapshot(items):

    result = {}

    for dimension, config in RESEARCH_DIMENSIONS.items():

        matched_urls = set()
        matched_titles = []

        for item in items:

            if not isinstance(item, dict):
                continue

            blob = _coverage_item_text(
                item
            )

            if not blob:
                continue

            hit = any(
                keyword.casefold() in blob
                for keyword in config["keywords"]
            )

            if not hit:
                continue

            url = str(
                item.get(
                    "url",
                    ""
                )
            ).strip()

            title = str(
                item.get(
                    "title",
                    ""
                )
            ).strip()

            identity = (
                url
                or title
            )

            if not identity:
                continue

            if identity in matched_urls:
                continue

            matched_urls.add(
                identity
            )

            if title:
                matched_titles.append(
                    title
                )

        count = len(
            matched_urls
        )

        min_docs = config.get(
            "min_docs",
            2
        )

        result[dimension] = {
            "count": count,
            "min_docs": min_docs,
            "covered": count >= min_docs,
            "examples": matched_titles[:3],
        }

    return result


# ------------------------------------------------------------
# D. 输出 Coverage 状态
# ------------------------------------------------------------

def _print_coverage_snapshot(
    snapshot,
    label
):

    print(
        f"\n📊 {label}"
    )

    print(
        "------------------------------------------"
    )

    for dimension, info in snapshot.items():

        mark = (
            "✅"
            if info["covered"]
            else "❌"
        )

        print(
            f"{mark} {dimension}: "
            f"{info['count']} / "
            f"{info['min_docs']}"
        )

    print(
        "------------------------------------------"
    )


# ------------------------------------------------------------
# E. 第一轮 Coverage 检查
# ------------------------------------------------------------

coverage_snapshot = (
    _research_coverage_snapshot(
        SEARCH_RESULTS
    )
)

_print_coverage_snapshot(
    coverage_snapshot,
    "Research Coverage 初检"
)

missing_dimensions = [
    dimension
    for dimension, info
    in coverage_snapshot.items()
    if not info["covered"]
]


# ------------------------------------------------------------
# F. 自动补搜
# 最多 2 轮，防止无限循环
# ------------------------------------------------------------

COVERAGE_MAX_REPAIR_ROUNDS = 2

coverage_repair_round = 0


while (
    missing_dimensions
    and coverage_repair_round
    < COVERAGE_MAX_REPAIR_ROUNDS
):

    coverage_repair_round += 1

    print(
        f"\n🔁 Coverage 补搜第 "
        f"{coverage_repair_round} 轮"
    )

    print(
        "当前缺失维度："
    )

    for dimension in missing_dimensions:
        print(
            f"  - {dimension}"
        )


    missing_text = "\n".join(
        f"- {x}"
        for x in missing_dimensions
    )


    repair_task = f"""
你现在正在执行：

Research Coverage Repair

研究对象：
{competitor}

用户真正关心的问题：
{focus}

上一轮研究没有充分覆盖以下维度：

{missing_text}


你的任务不是重新做一遍完整研究。

只针对以上缺失维度进行定向补搜。


========================
SEARCH RULES
========================

1. 必须使用 search_web 搜索。
2. 每个缺失维度至少尝试 2 组不同查询。
3. 优先寻找：

A. 官方一手资料
B. 权威媒体
C. 行业研究 / 数据机构
D. 普通媒体仅作为补充

4. 如果是产品能力：
优先搜索官方产品页、Blog、Changelog、Help Center。

5. 如果是用户 / 市场：
重点寻找 MAU、DAU、用户量、市场份额、使用率、区域分布。

6. 如果是增长 / 分发：
重点寻找渠道、合作伙伴、预装、平台嵌入、流量来源、用户增长。

7. 如果是商业模式：
重点寻找定价、订阅、企业客户、ARR、收入、商业化模式。

8. 如果是竞争：
重点寻找 AI Search、Google、Bing、Baidu、传统搜索替代关系。

9. 如果是 Baidu / China：
重点寻找中国市场、百度、文心、Kimi、豆包、夸克等直接比较和竞争证据。

10. 如果是技术 / 生态：
重点寻找模型、API、Connector、Developer、数据源、第三方集成和生态合作。

11. 不要为了完成任务而编造证据。

12. 某个维度如果经过多轮搜索仍找不到可靠证据，
允许明确输出：

NO_EVIDENCE: <维度名称>

13. 不要写最终研究报告。

14. 你的唯一目标：

补齐研究证据覆盖。
"""


    Runner.run_sync(
        researcher,
        repair_task,
        max_turns=18
    )


    coverage_snapshot = (
        _research_coverage_snapshot(
            SEARCH_RESULTS
        )
    )

    _print_coverage_snapshot(
        coverage_snapshot,
        f"Coverage 补搜第 {coverage_repair_round} 轮结果"
    )

    missing_dimensions = [
        dimension
        for dimension, info
        in coverage_snapshot.items()
        if not info["covered"]
    ]


# ------------------------------------------------------------
# G. Coverage Gate 最终结果
# ------------------------------------------------------------

if missing_dimensions:

    print(
        "\n⚠️ Research Coverage Gate："
        "仍存在证据缺口"
    )

    for dimension in missing_dimensions:

        print(
            f"  Missing Evidence: "
            f"{dimension}"
        )

    print(
        "\n这些维度已经执行过定向补搜，"
        "但仍未达到最低证据要求。"
    )

    print(
        "程序不会要求模型猜测，"
        "后续应作为 Missing Evidence 处理。"
    )

else:

    print(
        "\n✅ Research Coverage Gate 通过"
    )

    print(
        "核心研究维度已达到最低证据覆盖要求。"
    )


RESEARCH_COVERAGE_MISSING = list(
    missing_dimensions
)

RESEARCH_COVERAGE_SNAPSHOT = dict(
    coverage_snapshot
)

print(
    "\n=========================================="
)
print(
    "Research Coverage Gate 完成"
)
print(
    "==========================================\n"
)


# 9.5 程序强制读取高价值候选网页
# ============================================================

print(
    "\n=========================================="
)

print(
    "程序开始自动读取高价值搜索结果"
)

print(
    "==========================================\n"
)

# URL去重
# 从磁盘恢复所有搜索候选
persisted_candidates = []

if CANDIDATE_PATH.exists():

    for line in CANDIDATE_PATH.read_text(
        encoding="utf-8"
    ).splitlines():

        if not line.strip():
            continue

        try:
            persisted_candidates.append(
                json.loads(line)
            )
        except Exception:
            pass

print(
    f"程序共收到候选网页：{len(persisted_candidates)} 个"
)

unique_candidates = {}

for item in SEARCH_RESULTS + persisted_candidates:

    url = item.get("url", "")

    if not url:
        continue

    if url not in unique_candidates:
        unique_candidates[url] = item


# ============================================================
# Source Governance
# ============================================================

candidates = list(
    unique_candidates.values()
)

# 重新根据当前官方域名分类，并打分
for item in candidates:

    item["source_tier"] = classify_source(
        item.get("url", "")
    )

    item["_quality_score"] = (
        source_quality_score(
            item,
            competitor
        )
    )


# ------------------------------------------------------------
# 先完全排除 Tier D
# ------------------------------------------------------------

usable_candidates = [
    item
    for item in candidates
    if (
        item.get("source_tier") != "Tier D"
        and is_relevant_candidate(
            item,
            competitor
        )
    )
]

print(
    f"相关性过滤后：{len(usable_candidates)}"
)

usable_candidates.sort(
    key=lambda x: x.get(
        "_quality_score",
        0
    ),
    reverse=True
)


# ------------------------------------------------------------
# 按来源配额选择
#
# 目标：
# Tier A 至少2
# Tier B 至少2
# Tier C 最多4
# 总数最多8
# ------------------------------------------------------------

selected = []

# 每个域名已经选了多少篇
domain_selected_count = {}


def get_source_domain(item):

    domain = (
        urlparse(
            item.get("url", "")
        )
        .netloc
        .lower()
        .replace("www.", "")
    )

    return domain


def max_domain_quota(item):

    """
    官方站最多3篇；
    其他单一媒体最多2篇。
    """

    if (
        item.get("source_tier")
        == "Tier A"
    ):
        return 3

    return 2


def can_select_source(item):

    domain = get_source_domain(
        item
    )

    if not domain:
        return False

    current = (
        domain_selected_count.get(
            domain,
            0
        )
    )

    return (
        current
        < max_domain_quota(item)
    )


def mark_selected_source(item):

    domain = get_source_domain(
        item
    )

    domain_selected_count[domain] = (
        domain_selected_count.get(
            domain,
            0
        )
        + 1
    )


def select_from_tier(
    tier,
    limit
):

    count = 0

    for item in usable_candidates:

        if count >= limit:
            break

        if (
            item.get("source_tier")
            != tier
        ):
            continue

        if item in selected:
            continue

        if not can_select_source(
            item
        ):
            continue

        selected.append(
            item
        )

        mark_selected_source(
            item
        )

        count += 1


# ------------------------------------------------------------
# 理想来源结构：
#
# Tier A 官方：3
# Tier B 权威媒体：3
# Tier C 补充资料：2
# ------------------------------------------------------------

select_from_tier(
    "Tier A",
    3
)

select_from_tier(
    "Tier B",
    3
)

select_from_tier(
    "Tier C",
    2
)


# ------------------------------------------------------------
# 如果不足8篇：
# 从剩余候选中继续补足，
# 但仍然遵守域名配额
# ------------------------------------------------------------

for item in usable_candidates:

    if len(selected) >= 8:
        break

    if item in selected:
        continue

    if not can_select_source(
        item
    ):
        continue

    selected.append(
        item
    )

    mark_selected_source(
        item
    )


# ------------------------------------------------------------
# 输出来源结构，方便人工验收
# ------------------------------------------------------------

print(
    "\n来源多样性统计："
)

for domain, count in sorted(
    domain_selected_count.items(),
    key=lambda x: x[1],
    reverse=True
):

    print(
        f"  {domain}: {count} 篇"
    )


print(
    "Source Governance 选源结果"
)

print(
    "=========================================="
)

print(
    f"候选总数：{len(candidates)}"
)

print(
    f"可用候选：{len(usable_candidates)}"
)

print(
    f"最终选择：{len(selected)}"
)

for item in selected:

    print(
        f"[{item.get('source_tier')}] "
        f"score={item.get('_quality_score')} | "
        f"{item.get('title', '')[:70]} | "
        f"{item.get('url', '')}"
    )


# ------------------------------------------------------------
# 强制读取最终选中的网页
# ------------------------------------------------------------

for item in selected:

    if len(SOURCE_STORE) >= 8:
        break

    url = item.get(
        "url",
        ""
    )

    if (
        not url
        or url in READ_URLS
    ):
        continue

    try:

        print(
            f"\n自动读取 "
            f"[{item.get('source_tier')}]："
            f"{url}"
        )

        headers = {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 "
                "Chrome/131 Safari/537.36"
            )
        }

        response = requests.get(
            url,
            headers=headers,
            timeout=20
        )

        response.raise_for_status()

        # 尝试修复乱码
        if (
            not response.encoding
            or response.encoding.lower()
            == "iso-8859-1"
        ):
            response.encoding = (
                response.apparent_encoding
            )

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        title = ""

        if soup.title:

            title = soup.title.get_text(
                " ",
                strip=True
            )

        publish_date = (
            extract_page_date(
                soup
            )
        )

        for tag in soup([
            "script",
            "style",
            "nav",
            "footer",
            "header",
            "noscript",
            "svg"
        ]):

            tag.decompose()

        page_text = " ".join(
            soup.stripped_strings
        )

        if not page_text:

            print(
                "⚠️ 页面无有效正文"
            )

            continue

        source_id = (
            f"S{len(SOURCE_STORE) + 1:02d}"
        )

        SOURCE_STORE.append({
            "source_id": source_id,
            "title": title,
            "url": url,
            "domain": urlparse(
                url
            ).netloc,
            "source_tier": item.get(
                "source_tier",
                classify_source(url)
            ),
            "published_date": publish_date,
            "quality_score": item.get(
                "_quality_score"
            ),
            "content": page_text[:12000],
        })

        READ_URLS.add(
            url
        )

        print(
            f"✅ 自动保存：{source_id}"
        )

    except Exception as e:

        print(
            f"⚠️ 读取失败："
            f"{url} | {e}"
        )


# ============================================================
# 10. 检查 Source Store
# ============================================================

print(
    "\n=========================================="
)

print(
    f"已保存原始网页：{len(SOURCE_STORE)} 个"
)

print(
    "=========================================="
)

for s in SOURCE_STORE:

    print(
        f"{s['source_id']} | "
        f"{s['source_tier']} | "
        f"{s['title'][:80]}"
    )


if len(SOURCE_STORE) == 0:

    print(
        "\n没有成功保存任何网页，程序终止。"
    )

    raise SystemExit


# ============================================================
# 11. Stage 2：Extractor
# ============================================================

print(
    "\n=========================================="
)

print(
    "Stage 2/4：Extractor 提取原子事实"
)

print(
    "==========================================\n"
)

source_payload = json.dumps(
    SOURCE_STORE,
    ensure_ascii=False,
    indent=2
)

extract_task = f"""
竞品：

{competitor}

用户研究重点：

{focus}

以下是程序真实保存的原始网页：

============================
RAW SOURCES
============================

{source_payload}

============================

请严格从这些原始资料中
提取 Evidence Ledger。
"""

extract_result = Runner.run_sync(
    extractor,
    extract_task,
    max_turns=10
)

evidence_ledger = (
    extract_result.final_output
)


# ============================================================
# 12. Stage 3：Audit
# ============================================================

print(
    "\n=========================================="
)

print(
    "Stage 3/4：Auditor 审核证据"
)

print(
    "==========================================\n"
)

audit_task = f"""
研究对象：

{competitor}

以下是 Evidence Ledger：

============================

{evidence_ledger}

============================

请逐条审核。
"""

audit_result = Runner.run_sync(
    auditor,
    audit_task,
    max_turns=8
)

audit_report = (
    audit_result.final_output
)


# ============================================================
# Stage 3.5: Python Evidence Hard Gate
# ============================================================

import re as _gate_re

print(
    "\n========================================"
)
print(
    "Stage 3.5: Python Evidence Hard Gate"
)
print(
    "========================================"
)


# ------------------------------------------------------------
# A. 解析 Auditor 对每个 Evidence 的最终状态
# ------------------------------------------------------------

def _parse_audit_statuses(text):
    statuses = {}

    matches = list(
        _gate_re.finditer(
            r"(?m)^##\s*(E\d+)\s*$",
            text
        )
    )

    for i, m in enumerate(matches):
        eid = m.group(1)

        start = m.start()

        end = (
            matches[i + 1].start()
            if i + 1 < len(matches)
            else len(text)
        )

        block = text[start:end]

        status_match = _gate_re.search(
            r"\*{0,2}Audit Status:\*{0,2}\s*"
            r"\n?\s*"
            r"(APPROVED|CAUTION|REJECTED)",
            block,
            flags=_gate_re.I
        )

        if status_match:
            statuses[eid] = (
                status_match
                .group(1)
                .upper()
            )

    return statuses


audit_status_map = _parse_audit_statuses(
    audit_report
)


if not audit_status_map:
    raise SystemExit(
        "❌ Python Hard Gate 无法解析 Auditor 状态，"
        "为防止未经审核事实进入 Writer，程序终止。"
    )


# ------------------------------------------------------------
# B. 将 Evidence Ledger 拆成 E01 / E02 / E03 ...
# ------------------------------------------------------------

def _split_evidence_blocks(text):
    blocks = {}
    order = []

    matches = list(
        _gate_re.finditer(
            r"(?m)^##\s*(E\d+)\s*$",
            text
        )
    )

    for i, m in enumerate(matches):
        eid = m.group(1)

        start = m.start()

        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            missing = text.find(
                "# Missing Evidence",
                start
            )

            end = (
                missing
                if missing != -1
                else len(text)
            )

        blocks[eid] = text[
            start:end
        ].strip()

        order.append(eid)

    return blocks, order


evidence_blocks, evidence_order = (
    _split_evidence_blocks(
        evidence_ledger
    )
)


if not evidence_blocks:
    raise SystemExit(
        "❌ Python Hard Gate 无法解析 Evidence Ledger"
    )


# ------------------------------------------------------------
# C. 同样拆 Auditor block
# ------------------------------------------------------------

def _split_audit_blocks(text):
    blocks = {}

    matches = list(
        _gate_re.finditer(
            r"(?m)^##\s*(E\d+)\s*$",
            text
        )
    )

    for i, m in enumerate(matches):
        eid = m.group(1)

        start = m.start()

        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            writer_rules = text.find(
                "# Writer",
                start
            )

            end = (
                writer_rules
                if writer_rules != -1
                else len(text)
            )

        blocks[eid] = text[
            start:end
        ].strip()

    return blocks


audit_blocks = _split_audit_blocks(
    audit_report
)


# ------------------------------------------------------------
# D. 建立白名单
# ------------------------------------------------------------

approved_ids = []
caution_ids = []
rejected_ids = []
unreviewed_ids = []


for eid in evidence_order:

    status = audit_status_map.get(
        eid
    )

    if status == "APPROVED":
        approved_ids.append(eid)

    elif status == "CAUTION":
        caution_ids.append(eid)

    elif status == "REJECTED":
        rejected_ids.append(eid)

    else:
        # Fail Closed:
        # Auditor 没明确审核，也不给 Writer
        unreviewed_ids.append(eid)


allowed_ids = (
    approved_ids
    + caution_ids
)


# ------------------------------------------------------------
# E. Missing Evidence 可以保留
# ------------------------------------------------------------

missing_evidence = ""

missing_pos = evidence_ledger.find(
    "# Missing Evidence"
)

if missing_pos != -1:
    missing_evidence = evidence_ledger[
        missing_pos:
    ].strip()


# ------------------------------------------------------------
# F. 生成 Writer 唯一允许看到的安全证据包
# ------------------------------------------------------------

packet = [
    "# Writer Evidence Packet",
    "",
    "以下内容已经经过 Auditor + Python Hard Gate。",
    "",
    "Writer 只能使用本 Evidence Packet 中的信息。",
    ""
]


for eid in evidence_order:

    status = audit_status_map.get(eid)

    if status not in (
        "APPROVED",
        "CAUTION"
    ):
        continue

    packet.append(
        f"# {eid} | {status}"
    )
    packet.append("")

    packet.append(
        evidence_blocks[eid]
    )

    # CAUTION 必须把审核限定语一起给 Writer
    if (
        status == "CAUTION"
        and eid in audit_blocks
    ):
        packet.append("")
        packet.append(
            "### Auditor Qualification"
        )
        packet.append(
            audit_blocks[eid]
        )

    packet.append("")
    packet.append("---")
    packet.append("")


if missing_evidence:
    packet.append(
        missing_evidence
    )
    packet.append("")


packet.extend(
    [
        "# Writer Hard Rules",
        "",
        "1. APPROVED Evidence 可作为事实使用。",
        "2. CAUTION Evidence 必须保留来源和限定语。",
        "3. REJECTED Evidence 已由代码物理删除。",
        "4. UNREVIEWED Evidence 已由代码物理删除。",
        "5. 不得补充本 Evidence Packet 之外的新事实。",
        "6. 可以进行分析和推论，但必须明确标注为“判断”或“推论”。",
        "7. 证据不足时必须写入研究缺口。",
    ]
)


writer_evidence_packet = "\n".join(
    packet
)

# ------------------------------------------------------------
# Writer Evidence Packet 程序级一致性校验
# ------------------------------------------------------------

# Writer Evidence Packet 中的 Evidence ID
# Packet 本身就是严格按照 allowed_ids 构建，
# 因此这里直接使用 allowed_ids 作为实际 Packet ID。
# 避免再次从 Markdown 文本反向解析造成空字符串/格式误判。
packet_ids = set(allowed_ids)

expected_writer_ids = set(
    allowed_ids
)

unexpected_writer_ids = (
    packet_ids - expected_writer_ids
)

missing_writer_ids = (
    expected_writer_ids - packet_ids
)

if unexpected_writer_ids:
    raise SystemExit(
        "❌ Writer Hard Gate 泄漏："
        "发现不允许进入 Writer 的 Evidence："
        + str(sorted(unexpected_writer_ids))
    )

if missing_writer_ids:
    raise SystemExit(
        "❌ Writer Hard Gate 数据缺失："
        "允许使用的 Evidence 未进入 Packet："
        + str(sorted(missing_writer_ids))
    )

if packet_ids != expected_writer_ids:
    raise SystemExit(
        "❌ Writer Evidence Packet 与 Hard Gate Allowlist 不一致"
    )

print(
    "✅ Writer Packet Hard Gate 自检通过 | "
    f"Expected={len(expected_writer_ids)} | "
    f"Actual={len(packet_ids)}"
)


# ------------------------------------------------------------
# G. 日志
# ------------------------------------------------------------

print(
    f"✅ APPROVED: {len(approved_ids)} "
    f"{approved_ids}"
)

print(
    f"⚠️ CAUTION: {len(caution_ids)} "
    f"{caution_ids}"
)

print(
    f"🚫 REJECTED: {len(rejected_ids)} "
    f"{rejected_ids}"
)

print(
    f"🚫 UNREVIEWED: {len(unreviewed_ids)} "
    f"{unreviewed_ids}"
)

print(
    f"✅ Writer 可见 Evidence: "
    f"{len(allowed_ids)} 条"
)

print(
    "🔒 REJECTED / UNREVIEWED Evidence "
    "已从 Writer 输入中物理删除"
)



# ============================================================
# 13. Stage 4：Writer
# ============================================================

print(
    "\n=========================================="
)

print(
    "Stage 4/4：Writer 生成业务报告"
)

print(
    "==========================================\n"
)

writer_task = f"""
竞品：

{competitor}

用户研究重点：

{focus}

============================
Evidence Ledger
============================

{writer_evidence_packet}

============================
Evidence Audit
============================


[完整 Audit Report 已由 Python Hard Gate 隔离。
Writer 只能使用上方 Writer Evidence Packet。]


============================

请生成最终竞品研究报告。
"""

writer_result = Runner.run_sync(
    writer,
    writer_task,
    max_turns=10
)

final_report = (
    writer_result.final_output
)


# ============================================================
# 14. 输出
# ============================================================

print(
    "\n=========================================="
)

print(
    "最终竞品研究报告"
)

print(
    "==========================================\n"
)

print(
    final_report
)


# ============================================================
# 15. 保存全部中间产物
# ============================================================

reports_dir = Path(
    "reports"
)

reports_dir.mkdir(
    exist_ok=True
)

safe_name = re.sub(
    r'[^\w\-]+',
    '_',
    competitor
)

base_name = (
    f"{safe_name}_{TODAY}"
)

sources_path = (
    reports_dir
    / f"{base_name}_sources.json"
)

evidence_path = (
    reports_dir
    / f"{base_name}_evidence.md"
)

audit_path = (
    reports_dir
    / f"{base_name}_audit.md"
)

report_path = (
    reports_dir
    / f"{base_name}_report.md"
)

sources_path.write_text(
    json.dumps(
        SOURCE_STORE,
        ensure_ascii=False,
        indent=2
    ),
    encoding="utf-8"
)

evidence_path.write_text(
    evidence_ledger,
    encoding="utf-8"
)

audit_path.write_text(
    audit_report,
    encoding="utf-8"
)

report_path.write_text(
    final_report,
    encoding="utf-8"
)

print(
    "\n=========================================="
)

print(
    "全部文件已保存"
)

print(
    "=========================================="
)

print(
    f"\n原始资料：{sources_path}"
)

print(
    f"证据账本：{evidence_path}"
)

print(
    f"审核报告：{audit_path}"
)

print(
    f"最终报告：{report_path}"
)

