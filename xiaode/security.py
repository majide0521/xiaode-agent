from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests


class URLValidationError(ValueError):
    """Raised when a user-controlled URL is not safe to request."""


_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".home", ".lan")


def _ascii_hostname(hostname: str, *, strip_www: bool = True) -> str:
    host = hostname.strip().rstrip(".").casefold()
    if strip_www and host.startswith("www."):
        host = host[4:]
    if not host:
        raise URLValidationError("官网域名不能为空")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise URLValidationError("官网域名包含无效字符") from exc
    return host


def _validate_hostname_shape(hostname: str, *, strip_www: bool = False) -> str:
    host = _ascii_hostname(hostname, strip_www=strip_www)
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        raise URLValidationError("不允许访问本机或内网域名")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise URLValidationError("官网必须使用公开域名，不能直接填写 IP")
    labels = host.split(".")
    if len(labels) < 2 or any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise URLValidationError("请输入有效的公开官网域名")
    return host


def normalize_official_domain(value: str) -> str:
    """Return only the normalized hostname, even when a full URL is supplied."""

    raw = value.strip()
    if not raw:
        raise URLValidationError("请输入竞品官网")
    candidate = raw if "://" in raw else f"//{raw}"
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise URLValidationError("官网地址格式无效") from exc
    if parsed.scheme and parsed.scheme.casefold() not in {"http", "https"}:
        raise URLValidationError("官网只支持 http 或 https")
    if parsed.username or parsed.password:
        raise URLValidationError("官网地址不能包含账号信息")
    if port not in {None, 80, 443}:
        raise URLValidationError("官网地址只允许 80 或 443 端口")
    if not parsed.hostname:
        raise URLValidationError("无法识别官网域名")
    return _validate_hostname_shape(parsed.hostname, strip_www=True)


def hostname_from_url(url: str) -> str:
    try:
        hostname = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    if not hostname:
        return ""
    try:
        return _ascii_hostname(hostname)
    except URLValidationError:
        return ""


def domain_matches(url: str, expected_domain: str) -> bool:
    host = hostname_from_url(url)
    expected = _ascii_hostname(expected_domain)
    return bool(host) and (host == expected or host.endswith(f".{expected}"))


def canonical_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    host = _ascii_hostname(parsed.hostname or "", strip_www=False)
    if not host:
        return url.strip()
    port = parsed.port
    default_port = (parsed.scheme.casefold() == "https" and port == 443) or (
        parsed.scheme.casefold() == "http" and port == 80
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.casefold(), netloc, path.rstrip("/") or "/", parsed.query, ""))


def _default_resolver(hostname: str, port: int) -> Iterable[str]:
    return {item[4][0] for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)}


def validate_public_url(
    url: str,
    *,
    resolver: Callable[[str, int], Iterable[str]] = _default_resolver,
) -> str:
    """Validate scheme, credentials, port, hostname, and every resolved address."""

    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError as exc:
        raise URLValidationError("网页地址格式无效") from exc
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise URLValidationError("网页只支持 http 或 https")
    if parsed.username or parsed.password:
        raise URLValidationError("网页地址不能包含账号信息")
    if port not in {None, 80, 443}:
        raise URLValidationError("网页只允许 80 或 443 端口")
    if not parsed.hostname:
        raise URLValidationError("网页地址缺少域名")

    host = _validate_hostname_shape(parsed.hostname, strip_www=False)
    lookup_port = port or (443 if parsed.scheme.casefold() == "https" else 80)
    try:
        addresses = list(resolver(host, lookup_port))
    except OSError as exc:
        raise URLValidationError(f"域名解析失败：{host}") from exc
    if not addresses:
        raise URLValidationError(f"域名没有可用地址：{host}")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise URLValidationError("域名解析结果不是有效 IP") from exc
        if not ip.is_global:
            raise URLValidationError("目标解析到本机、内网或保留地址，已阻止访问")
    return canonical_url(url)


def safe_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int | float = 20,
    max_bytes: int = 2_000_000,
    max_redirects: int = 5,
    session: requests.Session | None = None,
) -> requests.Response:
    """Fetch a bounded public HTTP resource and validate every redirect hop."""

    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    client = session or requests.Session()
    if session is None:
        client.trust_env = False
    current = url

    for _ in range(max_redirects + 1):
        current = validate_public_url(current)
        response = client.get(
            current,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        if response.status_code in _REDIRECT_CODES:
            location = response.headers.get("Location", "").strip()
            response.close()
            if not location:
                raise URLValidationError("重定向响应缺少目标地址")
            current = urljoin(current, location)
            continue

        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > max_bytes:
            response.close()
            raise URLValidationError("网页体积超过安全上限")
        media_type = response.headers.get("Content-Type", "").split(";", 1)[0].casefold()
        if media_type.startswith(("image/", "audio/", "video/")) or media_type in {
            "application/pdf",
            "application/zip",
            "application/x-rar-compressed",
            "application/octet-stream",
        }:
            response.close()
            raise URLValidationError(f"不支持读取该内容类型：{media_type}")

        content = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > max_bytes:
                response.close()
                raise URLValidationError("网页体积超过安全上限")
        response._content = bytes(content)
        response._content_consumed = True
        return response

    raise URLValidationError("网页重定向次数过多")
