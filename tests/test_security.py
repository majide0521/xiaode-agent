import socket
import unittest
from unittest.mock import patch

from xiaode.security import URLValidationError, normalize_official_domain, safe_get, validate_public_url


class RedirectResponse:
    status_code = 302
    headers = {"Location": "http://metadata.example/latest"}

    def close(self) -> None:
        return None


class RedirectSession:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, *_args: object, **_kwargs: object) -> RedirectResponse:
        self.calls += 1
        return RedirectResponse()


class DomainNormalizationTests(unittest.TestCase):
    def test_full_url_becomes_hostname_only(self) -> None:
        self.assertEqual(
            normalize_official_domain("https://www.doubao.com/chat/?channel=example"),
            "doubao.com",
        )

    def test_bare_domain_is_supported(self) -> None:
        self.assertEqual(normalize_official_domain("Perplexity.AI/"), "perplexity.ai")

    def test_rejects_private_targets_and_unsafe_ports(self) -> None:
        for value in ("http://127.0.0.1", "http://localhost", "ftp://example.com", "https://example.com:8080"):
            with self.subTest(value=value), self.assertRaises(URLValidationError):
                normalize_official_domain(value)

    def test_public_url_rejects_private_dns_resolution(self) -> None:
        with self.assertRaises(URLValidationError):
            validate_public_url(
                "https://example.com/research",
                resolver=lambda _host, _port: ["10.0.0.2"],
            )

    def test_public_url_accepts_global_dns_resolution(self) -> None:
        normalized = validate_public_url(
            "HTTPS://WWW.Example.com/research/#part",
            resolver=lambda _host, _port: ["93.184.216.34"],
        )
        self.assertEqual(normalized, "https://www.example.com/research")

    def test_redirect_to_private_address_is_blocked_before_second_request(self) -> None:
        def fake_dns(host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
            address = "93.184.216.34" if host == "example.com" else "169.254.169.254"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

        session = RedirectSession()
        with patch("xiaode.security.socket.getaddrinfo", side_effect=fake_dns):
            with self.assertRaises(URLValidationError):
                safe_get("https://example.com/start", session=session)  # type: ignore[arg-type]
        self.assertEqual(session.calls, 1)


if __name__ == "__main__":
    unittest.main()
