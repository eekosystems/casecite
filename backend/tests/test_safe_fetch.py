"""
Tests for the SSRF-safe outbound fetch helper.

These guard the picker/connector SSRF fix: server-side fetches must reject
private/loopback/link-local/metadata targets and enforce a host allowlist.
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.utils.safe_fetch import UnsafeURLError, assert_public_url, safe_download


class TestSchemeValidation:
    async def test_rejects_file_scheme(self):
        with pytest.raises(UnsafeURLError):
            await assert_public_url("file:///etc/passwd", allowed_hosts=("example.com",))

    async def test_rejects_gopher_scheme(self):
        with pytest.raises(UnsafeURLError):
            await assert_public_url("gopher://127.0.0.1:6379/_INFO", allowed_hosts=("127.0.0.1",))

    async def test_rejects_no_host(self):
        with pytest.raises(UnsafeURLError):
            await assert_public_url("https://", allowed_hosts=("example.com",))


class TestPrivateAddressBlocking:
    async def test_rejects_cloud_metadata_ip_literal(self):
        with pytest.raises(UnsafeURLError):
            await assert_public_url(
                "http://169.254.169.254/latest/meta-data/",
                allowed_hosts=("169.254.169.254",),
            )

    async def test_rejects_loopback_literal(self):
        with pytest.raises(UnsafeURLError):
            await assert_public_url("http://127.0.0.1:6379/", allowed_hosts=("127.0.0.1",))

    async def test_rejects_private_10_literal(self):
        with pytest.raises(UnsafeURLError):
            await assert_public_url("http://10.1.2.3/internal", allowed_hosts=("10.1.2.3",))

    async def test_rejects_private_192_168_literal(self):
        with pytest.raises(UnsafeURLError):
            await assert_public_url("http://192.168.0.1/", allowed_hosts=("192.168.0.1",))

    async def test_rejects_hostname_resolving_to_private(self):
        # A public-looking hostname that resolves to an internal IP.
        with (
            patch(
                "app.utils.safe_fetch._resolve_host",
                new=AsyncMock(return_value=["10.0.0.5"]),
            ),
            pytest.raises(UnsafeURLError),
        ):
            await assert_public_url("https://evil.example.com/x", allowed_hosts=("example.com",))

    async def test_allows_hostname_resolving_to_public(self):
        with patch(
            "app.utils.safe_fetch._resolve_host",
            new=AsyncMock(return_value=["93.184.216.34"]),
        ):
            # Should not raise.
            await assert_public_url("https://example.com/x", allowed_hosts=("example.com",))


class TestHostAllowlist:
    async def test_rejects_host_not_in_allowlist(self):
        with (
            patch(
                "app.utils.safe_fetch._resolve_host",
                new=AsyncMock(return_value=["93.184.216.34"]),
            ),
            pytest.raises(UnsafeURLError),
        ):
            await assert_public_url("https://attacker.com/x", allowed_hosts=("dropbox.com",))

    async def test_allows_subdomain_of_allowlisted_host(self):
        with patch(
            "app.utils.safe_fetch._resolve_host",
            new=AsyncMock(return_value=["93.184.216.34"]),
        ):
            await assert_public_url(
                "https://dl.dropboxusercontent.com/f", allowed_hosts=("dropboxusercontent.com",)
            )

    async def test_allowlist_does_not_match_suffix_trick(self):
        # notdropbox.com must NOT match an allowlist of dropbox.com
        with (
            patch(
                "app.utils.safe_fetch._resolve_host",
                new=AsyncMock(return_value=["93.184.216.34"]),
            ),
            pytest.raises(UnsafeURLError),
        ):
            await assert_public_url("https://notdropbox.com/x", allowed_hosts=("dropbox.com",))


class TestAllowlistRequired:
    async def test_allowed_hosts_parameter_is_required(self):
        # No default may exist that skips host allowlisting.
        with pytest.raises(TypeError):
            await assert_public_url("https://example.com/x")


PUBLIC = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


def _by_name(request: httpx.Request) -> str:
    """The URL as the caller named it: the connection goes to the pinned
    address, the Host header carries the hostname."""
    return f"{request.url.scheme}://{request.headers['host']}{request.url.raw_path.decode()}"


def _mock_client(handler):
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    return patch("app.utils.safe_fetch.httpx.AsyncClient", side_effect=factory)


class TestPinnedConnection:
    """The connection is made to the address that passed validation."""

    async def test_connects_to_the_validated_address_with_host_and_sni(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=b"file")

        with (
            _mock_client(handler),
            patch("app.utils.safe_fetch._resolve_host", new=AsyncMock(return_value=[PUBLIC])),
        ):
            body = await safe_download(
                "https://api.box.com/2.0/files/1/content?x=1",
                max_bytes=1024,
                allowed_hosts=("box.com",),
            )
        assert body == b"file"
        (request,) = seen
        assert request.url.host == PUBLIC  # the socket goes to the validated IP
        assert request.url.path == "/2.0/files/1/content" and request.url.query == b"x=1"
        assert request.headers["host"] == "api.box.com"  # the server still sees the name
        assert (
            request.extensions["sni_hostname"] == "api.box.com"
        )  # and the cert is checked against it

    async def test_plain_http_and_explicit_ports_keep_the_port_and_skip_sni(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=b"ok")

        with (
            _mock_client(handler),
            patch("app.utils.safe_fetch._resolve_host", new=AsyncMock(return_value=[PUBLIC])),
        ):
            await safe_download(
                "http://files.box.com:8080/a", max_bytes=16, allowed_hosts=("box.com",)
            )
        (request,) = seen
        assert request.url.host == PUBLIC and request.url.port == 8080
        assert request.headers["host"] == "files.box.com:8080"
        assert "sni_hostname" not in request.extensions

    async def test_an_ipv6_address_is_bracketed(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=b"ok")

        with (
            _mock_client(handler),
            patch("app.utils.safe_fetch._resolve_host", new=AsyncMock(return_value=[PUBLIC_V6])),
        ):
            await safe_download("https://api.box.com/a", max_bytes=16, allowed_hosts=("box.com",))
        (request,) = seen
        assert request.url.host == PUBLIC_V6
        assert request.headers["host"] == "api.box.com"

    async def test_a_record_that_flips_after_validation_changes_nothing(self):
        """The rebinding race: the name validates as public, then would resolve
        to the metadata service. Nothing resolves it a second time, so the
        connection still goes where validation looked."""
        seen = []
        lookups = []

        async def resolver(host):
            lookups.append(host)
            return [PUBLIC] if len(lookups) == 1 else ["169.254.169.254"]

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=b"ok")

        with _mock_client(handler), patch("app.utils.safe_fetch._resolve_host", new=resolver):
            await safe_download("https://api.box.com/a", max_bytes=16, allowed_hosts=("box.com",))
        assert lookups == ["api.box.com"]
        assert seen[0].url.host == PUBLIC

    async def test_a_relative_redirect_resolves_against_the_name_and_is_revalidated(self):
        seen = []
        validated = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/a":
                return httpx.Response(302, headers={"location": "/b"})
            return httpx.Response(200, content=b"ok")

        async def resolver(host):
            validated.append(host)
            return [PUBLIC]

        with _mock_client(handler), patch("app.utils.safe_fetch._resolve_host", new=resolver):
            await safe_download("https://api.box.com/a", max_bytes=16, allowed_hosts=("box.com",))
        assert [r.url.path for r in seen] == ["/a", "/b"]
        assert all(r.url.host == PUBLIC and r.headers["host"] == "api.box.com" for r in seen)
        assert validated == ["api.box.com", "api.box.com"]  # every hop validated by name


class TestRedirectCredentials:
    """A bearer token is only ever sent to the origin it was issued for."""

    async def _fetch(self, routes, url, headers):
        """Run safe_download against *routes* ({url: response}); return requests seen."""
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return routes[_by_name(request)]

        real_client = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        with (
            patch("app.utils.safe_fetch.httpx.AsyncClient", side_effect=factory),
            patch("app.utils.safe_fetch.assert_public_url", new=AsyncMock(return_value=[PUBLIC])),
        ):
            body = await safe_download(
                url, headers=headers, max_bytes=1024, allowed_hosts=("box.com", "boxcloud.com")
            )
        return body, seen

    async def test_authorization_is_dropped_on_cross_host_redirect(self):
        routes = {
            "https://api.box.com/2.0/files/1/content": httpx.Response(
                302, headers={"location": "https://dl.boxcloud.com/d/1?sig=abc"}
            ),
            "https://dl.boxcloud.com/d/1?sig=abc": httpx.Response(200, content=b"file"),
        }
        body, seen = await self._fetch(
            routes,
            "https://api.box.com/2.0/files/1/content",
            {"Authorization": "Bearer secret", "Accept": "*/*"},
        )
        assert body == b"file"
        assert seen[0].headers["authorization"] == "Bearer secret"
        assert "authorization" not in seen[1].headers
        assert seen[1].headers["accept"] == "*/*"

    async def test_authorization_is_kept_on_same_origin_redirect(self):
        routes = {
            "https://api.box.com/a": httpx.Response(302, headers={"location": "/b"}),
            "https://api.box.com/b": httpx.Response(200, content=b"file"),
        }
        _, seen = await self._fetch(
            routes, "https://api.box.com/a", {"Authorization": "Bearer secret"}
        )
        assert seen[1].headers["authorization"] == "Bearer secret"

    async def test_credentials_do_not_return_after_bouncing_back(self):
        """Once the chain has left the origin, the token is gone for good."""
        routes = {
            "https://api.box.com/a": httpx.Response(
                302, headers={"location": "https://dl.boxcloud.com/hop"}
            ),
            "https://dl.boxcloud.com/hop": httpx.Response(
                302, headers={"location": "https://api.box.com/b"}
            ),
            "https://api.box.com/b": httpx.Response(200, content=b"file"),
        }
        _, seen = await self._fetch(
            routes, "https://api.box.com/a", {"Authorization": "Bearer secret", "Cookie": "s=1"}
        )
        assert "authorization" not in seen[1].headers
        assert "authorization" not in seen[2].headers
        assert "cookie" not in seen[2].headers

    async def test_scheme_downgrade_drops_credentials(self):
        routes = {
            "https://api.box.com/a": httpx.Response(
                302, headers={"location": "http://api.box.com/a"}
            ),
            "http://api.box.com/a": httpx.Response(200, content=b"file"),
        }
        _, seen = await self._fetch(
            routes, "https://api.box.com/a", {"Authorization": "Bearer secret"}
        )
        assert "authorization" not in seen[1].headers
