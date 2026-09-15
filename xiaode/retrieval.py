from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .models import ResearchQuery, SearchPlan, SourceRecord
from .quality import DIMENSIONS, requested_dimensions
from .runtime import RunWorkspace
from .security import (
    URLValidationError,
    canonical_url,
    domain_matches,
    hostname_from_url,
    safe_get,
)


TIER_B_DOMAINS = {
    "reuters.com",
    "bloomberg.com",
    "ft.com",
    "wsj.com",
    "cnbc.com",
    "techcrunch.com",
    "theverge.com",
    "wired.com",
    "theinformation.com",
    "nikkei.com",
}

LOW_QUALITY_DOMAINS = {
    "csdn.net",
    "php.cn",
    "taobao.com",
    "baike.baidu.com",
    "sohu.com",
    "toutiao.com",
    "wikipedia.org",
}

HIGH_VALUE_PATH_TERMS = (
    "blog",
    "changelog",
    "pricing",
    "product",
    "news",
    "about",
    "company",
    "enterprise",
    "business",
    "api",
    "research",
    "update",
    "help",
)


@dataclass(frozen=True)
class ResearchBudget:
    query_limit: int
    initial_sources: int
    max_sources: int
    content_chars: int
    repair_rounds: int


def budget_for(mode: str) -> ResearchBudget:
    if mode == "quick":
        return ResearchBudget(6, 6, 8, 9_000, 1)
    return ResearchBudget(10, 8, 12, 12_000, 1)


def _is_subdomain(host: str, parent: str) -> bool:
    return host == parent or host.endswith(f".{parent}")


def classify_source(url: str, official_domain: str) -> str:
    host = hostname_from_url(url)
    if not host:
        return "Tier D"
    if _is_subdomain(host, official_domain):
        return "Tier A"
    if any(_is_subdomain(host, domain) for domain in TIER_B_DOMAINS):
        return "Tier B"
    if any(_is_subdomain(host, domain) for domain in LOW_QUALITY_DOMAINS):
        return "Tier D"
    return "Tier C"


def _extract_page_date(soup: BeautifulSoup) -> str:
    candidates = (
        ("property", "article:published_time"),
        ("name", "date"),
        ("name", "pubdate"),
        ("name", "publishdate"),
        ("name", "datePublished"),
    )
    for attribute, value in candidates:
        tag = soup.find("meta", attrs={attribute: value})
        if tag and tag.get("content"):
            return str(tag.get("content"))
    time_tag = soup.find("time")
    if time_tag:
        return str(time_tag.get("datetime") or time_tag.get_text(" ", strip=True) or "未确认")
    return "未确认"


class RetrievalEngine:
    def __init__(
        self,
        *,
        competitor: str,
        official_domain: str,
        focus: str,
        mode: str,
        bocha_api_key: str,
        workspace: RunWorkspace,
    ) -> None:
        self.competitor = competitor.strip()
        self.official_domain = official_domain
        self.focus = focus.strip()
        self.mode = mode
        self.bocha_api_key = bocha_api_key
        self.workspace = workspace
        self.budget = budget_for(mode)
        self.candidates: dict[str, dict[str, Any]] = {}
        self.sources: list[SourceRecord] = []
        self.read_urls: set[str] = set()
        self.executed_queries: set[str] = set()
        self._prefetched: dict[str, requests.Response] = {}
        self._headers = {
            "User-Agent": "Mozilla/5.0 (compatible; XiaodeResearchBot/0.7; +research)",
            "Accept": "text/html,application/xhtml+xml,application/xml,text/plain;q=0.9,*/*;q=0.1",
        }

    def _add_candidate(
        self,
        *,
        title: str,
        url: str,
        site_name: str = "",
        published_date: str = "",
        summary: str = "",
        dimension: str = "",
    ) -> None:
        try:
            parsed = urlsplit(url.strip())
            if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
                return
            normalized = canonical_url(url)
        except (TypeError, ValueError):
            return
        record = {
            "title": title.strip(),
            "url": normalized,
            "site_name": site_name.strip(),
            "published_date": published_date.strip(),
            "summary": summary.strip(),
            "dimension": dimension.strip(),
            "source_tier": classify_source(normalized, self.official_domain),
        }
        existing = self.candidates.get(normalized)
        if existing:
            for field in ("title", "site_name", "published_date", "summary", "dimension"):
                if not existing.get(field) and record.get(field):
                    existing[field] = record[field]
            return
        self.candidates[normalized] = record
        self.workspace.append_jsonl("candidates.jsonl", record)

    def discover_official(self) -> None:
        homepage = f"https://{self.official_domain}/"
        self._add_candidate(
            title=f"{self.competitor} Official Website",
            url=homepage,
            site_name=self.official_domain,
            summary="程序验证并加入的官方网站首页。",
            dimension="product",
        )
        sitemap_urls = deque([f"https://{self.official_domain}/sitemap.xml"])
        try:
            robots = safe_get(
                f"https://{self.official_domain}/robots.txt",
                headers=self._headers,
                timeout=15,
                max_bytes=500_000,
            )
            if robots.ok:
                for line in robots.text.splitlines():
                    if line.casefold().startswith("sitemap:"):
                        sitemap_urls.append(line.split(":", 1)[1].strip())
        except (requests.RequestException, URLValidationError) as exc:
            self.workspace.log_event("robots_failed", error_type=type(exc).__name__)

        visited: set[str] = set()
        page_urls: list[str] = []
        while sitemap_urls and len(visited) < 6 and len(page_urls) < 80:
            sitemap_url = sitemap_urls.popleft()
            if sitemap_url in visited:
                continue
            visited.add(sitemap_url)
            try:
                response = safe_get(
                    sitemap_url,
                    headers=self._headers,
                    timeout=20,
                    max_bytes=2_000_000,
                )
                response.raise_for_status()
                upper_prefix = response.content[:20_000].upper()
                if b"<!DOCTYPE" in upper_prefix or b"<!ENTITY" in upper_prefix:
                    raise ET.ParseError("sitemap DTD/entity declarations are not allowed")
                root = ET.fromstring(response.content)
            except (requests.RequestException, URLValidationError, ET.ParseError) as exc:
                self.workspace.log_event(
                    "sitemap_failed",
                    url=sitemap_url,
                    error_type=type(exc).__name__,
                )
                continue
            for element in root.iter():
                if not element.tag.endswith("loc") or not element.text:
                    continue
                location = element.text.strip()
                if not domain_matches(location, self.official_domain):
                    continue
                if location.casefold().split("?", 1)[0].endswith(".xml"):
                    sitemap_urls.append(location)
                    continue
                if any(term in location.casefold() for term in HIGH_VALUE_PATH_TERMS):
                    page_urls.append(location)
                if len(page_urls) >= 80:
                    break
        for url in page_urls:
            self._add_candidate(
                title=f"{self.competitor} Official",
                url=url,
                site_name=self.official_domain,
                summary="从官方网站 Sitemap 发现的高价值页面。",
                dimension="product",
            )

        try:
            response = safe_get(homepage, headers=self._headers, timeout=20)
            response.raise_for_status()
            self._prefetched[canonical_url(homepage)] = response
            soup = BeautifulSoup(response.text, "html.parser")
            for anchor in soup.find_all("a", href=True):
                full_url = urljoin(homepage, str(anchor.get("href", "")).strip())
                if not domain_matches(full_url, self.official_domain):
                    continue
                signal = f"{full_url} {anchor.get_text(' ', strip=True)}".casefold()
                if not any(term in signal for term in HIGH_VALUE_PATH_TERMS):
                    continue
                self._add_candidate(
                    title=anchor.get_text(" ", strip=True) or f"{self.competitor} Official",
                    url=full_url.split("#", 1)[0],
                    site_name=self.official_domain,
                    summary="从官方网站首页发现的站内页面。",
                    dimension="product",
                )
        except (requests.RequestException, URLValidationError) as exc:
            self.workspace.log_event("homepage_discovery_failed", error_type=type(exc).__name__)

        self.workspace.log_event(
            "official_discovery_complete",
            candidates=len(self.candidates),
            sitemaps=len(visited),
        )

    def search(self, query: ResearchQuery) -> int:
        query_text = re.sub(r"\s+", " ", query.query).strip()
        key = query_text.casefold()
        if not query_text or key in self.executed_queries:
            return 0
        if len(self.executed_queries) >= self.budget.query_limit:
            return 0
        self.executed_queries.add(key)
        print(f"\n🔎 搜索 [{query.dimension}]：{query_text}", flush=True)
        payload = {
            "query": query_text,
            "freshness": "oneYear",
            "summary": True,
            "count": 8,
        }
        headers = {
            "Authorization": f"Bearer {self.bocha_api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = requests.post(
                    "https://api.bochaai.com/v1/web-search",
                    headers=headers,
                    json=payload,
                    timeout=30,
                )
                response.raise_for_status()
                pages = (
                    response.json().get("data", {}).get("webPages", {}).get("value", [])
                )
                added_before = len(self.candidates)
                for item in pages:
                    self._add_candidate(
                        title=str(item.get("name", "")),
                        url=str(item.get("url", "")),
                        site_name=str(item.get("siteName", "")),
                        published_date=str(item.get("datePublished", "")),
                        summary=str(item.get("summary") or item.get("snippet") or ""),
                        dimension=query.dimension,
                    )
                added = len(self.candidates) - added_before
                self.workspace.log_event(
                    "search_complete",
                    dimension=query.dimension,
                    results=len(pages),
                    new_candidates=added,
                )
                return added
            except (requests.RequestException, ValueError, TypeError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1)
        self.workspace.log_event(
            "search_failed",
            dimension=query.dimension,
            error_type=type(last_error).__name__ if last_error else "Unknown",
        )
        print(f"⚠️ 搜索失败：{type(last_error).__name__ if last_error else 'Unknown'}", flush=True)
        return 0

    def execute_plan(self, plan: SearchPlan) -> None:
        allowed_dimensions = requested_dimensions(self.focus)
        authority_dimension = next(
            (dimension for dimension in allowed_dimensions if dimension != "product"),
            "product",
        )
        seeds = [
            ResearchQuery(
                query=f"site:{self.official_domain} {self.competitor} product pricing official",
                dimension="product",
                preferred_source="official",
            ),
            ResearchQuery(
                query=f"site:{self.official_domain} {self.competitor} blog changelog update {date.today().year}",
                dimension="product",
                preferred_source="official",
            ),
            ResearchQuery(
                query=f"{self.competitor} Reuters TechCrunch The Verge {date.today().year}",
                dimension=authority_dimension,
                preferred_source="authority",
            ),
        ]
        repair_reserve = 1 if self.mode == "quick" else 2
        initial_limit = max(3, self.budget.query_limit - repair_reserve)
        planned = [query for query in plan.queries if query.dimension in allowed_dimensions]
        for query in [*seeds, *planned]:
            if len(self.executed_queries) >= initial_limit:
                break
            self.search(query)

    def _candidate_score(self, item: dict[str, Any]) -> int:
        tier_score = {"Tier A": 100, "Tier B": 75, "Tier C": 35, "Tier D": -100}
        score = tier_score.get(str(item.get("source_tier")), 0)
        blob = f"{item.get('title', '')} {item.get('summary', '')} {item.get('url', '')}".casefold()
        aliases = {self.competitor.casefold(), self.official_domain.split(".", 1)[0].casefold()}
        if any(alias and alias in blob for alias in aliases):
            score += 25
        if item.get("dimension") in requested_dimensions(self.focus):
            score += 12
        if any(term in blob for term in HIGH_VALUE_PATH_TERMS):
            score += 8
        if str(date.today().year) in blob:
            score += 10
        return score

    def _is_relevant(self, item: dict[str, Any]) -> bool:
        if item.get("source_tier") == "Tier A":
            return True
        blob = f"{item.get('title', '')} {item.get('summary', '')} {item.get('url', '')}".casefold()
        aliases = {
            self.competitor.casefold(),
            re.sub(r"\s+(?:ai|app|search|assistant)$", "", self.competitor.casefold()),
            self.official_domain.split(".", 1)[0].casefold(),
        }
        return any(alias and alias in blob for alias in aliases)

    def _ranked_candidates(self) -> list[dict[str, Any]]:
        usable = [
            item
            for item in self.candidates.values()
            if item.get("source_tier") != "Tier D" and self._is_relevant(item)
        ]
        for item in usable:
            item["quality_score"] = self._candidate_score(item)
        return sorted(usable, key=lambda item: int(item["quality_score"]), reverse=True)

    def _selected_candidates(self, target: int) -> list[dict[str, Any]]:
        ranked = self._ranked_candidates()
        selected: list[dict[str, Any]] = []
        domain_counts: dict[str, int] = {}

        def add_from_tier(tier: str, limit: int) -> None:
            count = 0
            for item in ranked:
                if count >= limit or len(selected) >= target:
                    return
                if item.get("source_tier") != tier or item in selected:
                    continue
                host = hostname_from_url(str(item.get("url", "")))
                quota = 3 if tier == "Tier A" else 2
                if not host or domain_counts.get(host, 0) >= quota:
                    continue
                selected.append(item)
                domain_counts[host] = domain_counts.get(host, 0) + 1
                count += 1

        add_from_tier("Tier A", min(3, target))
        add_from_tier("Tier B", min(3, target))
        add_from_tier("Tier C", min(2, target))
        for item in ranked:
            if len(selected) >= target:
                break
            if item in selected:
                continue
            host = hostname_from_url(str(item.get("url", "")))
            quota = 3 if item.get("source_tier") == "Tier A" else 2
            if not host or domain_counts.get(host, 0) >= quota:
                continue
            selected.append(item)
            domain_counts[host] = domain_counts.get(host, 0) + 1
        return selected

    def _fetch_candidate(self, item: dict[str, Any]) -> SourceRecord | None:
        url = str(item["url"])
        if url in self.read_urls:
            return None
        print(f"📖 读取 [{item['source_tier']}]：{url}", flush=True)
        try:
            response = self._prefetched.pop(url, None) or safe_get(
                url,
                headers=self._headers,
                timeout=20,
                max_bytes=2_000_000,
            )
            response.raise_for_status()
            if not response.encoding or response.encoding.casefold() == "iso-8859-1":
                response.encoding = response.apparent_encoding
            soup = BeautifulSoup(response.text, "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else str(item.get("title", ""))
            published_date = _extract_page_date(soup)
            for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg", "form"]):
                tag.decompose()
            content = " ".join(soup.stripped_strings)
            if len(content) < 200:
                raise ValueError("正文不足 200 字符")
            record = SourceRecord(
                source_id=f"S{len(self.sources) + 1:02d}",
                title=title,
                url=url,
                domain=hostname_from_url(url),
                source_tier=str(item["source_tier"]),
                published_date=published_date,
                quality_score=int(item.get("quality_score", 0)),
                content=content[: self.budget.content_chars],
            )
            self.read_urls.add(url)
            self.sources.append(record)
            self.workspace.log_event(
                "source_saved",
                source_id=record.source_id,
                tier=record.source_tier,
                domain=record.domain,
                chars=len(record.content),
            )
            return record
        except (requests.RequestException, URLValidationError, ValueError) as exc:
            self.read_urls.add(url)
            self.workspace.log_event(
                "source_failed",
                url=url,
                error_type=type(exc).__name__,
            )
            print(f"⚠️ 读取失败：{type(exc).__name__}: {exc}", flush=True)
            return None

    def fetch_sources(self, target: int | None = None) -> list[SourceRecord]:
        target_count = min(target or self.budget.initial_sources, self.budget.max_sources)
        selection_pool = max(self.budget.max_sources * 3, target_count)
        for item in self._selected_candidates(selection_pool):
            if len(self.sources) >= target_count:
                break
            self._fetch_candidate(item)
        return list(self.sources)

    def source_coverage(self) -> dict[str, dict[str, Any]]:
        snapshot: dict[str, dict[str, Any]] = {}
        for key in requested_dimensions(self.focus):
            config = DIMENSIONS[key]
            matches: list[str] = []
            for source in self.sources:
                blob = f"{source.title} {source.content}".casefold()
                if any(str(term).casefold() in blob for term in config["terms"]):
                    matches.append(source.source_id)
            snapshot[key] = {
                "label": config["label"],
                "source_ids": list(dict.fromkeys(matches)),
                "covered": bool(matches),
            }
        return snapshot

    def repair_coverage(self) -> dict[str, dict[str, Any]]:
        for _ in range(self.budget.repair_rounds):
            snapshot = self.source_coverage()
            missing = [key for key, info in snapshot.items() if not info["covered"]]
            if not missing or len(self.executed_queries) >= self.budget.query_limit:
                return snapshot
            for key in missing:
                terms = " ".join(DIMENSIONS[key]["terms"][:4])
                self.search(
                    ResearchQuery(
                        query=f"{self.competitor} {terms} {date.today().year}",
                        dimension=key,
                        preferred_source="authority",
                    )
                )
                if len(self.executed_queries) >= self.budget.query_limit:
                    break
            self.fetch_sources(self.budget.max_sources)
        return self.source_coverage()
