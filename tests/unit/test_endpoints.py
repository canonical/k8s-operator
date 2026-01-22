# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Unit tests for endpoints module."""

from endpoints import build_url


def test_build_url():
    """Test build_url function."""
    test_cases = [
        # In the format of (addr, port, scheme, expected)
        # IPv4
        ("1.2.3.4", "12345", "https", "https://1.2.3.4:12345"),
        ("1.2.3.4:80", "12345", "https", "https://1.2.3.4:12345"),
        ("http://1.2.3.4", "12345", "https", "https://1.2.3.4:12345"),
        ("http://1.2.3.4:80", "12345", "https", "https://1.2.3.4:12345"),
        # IPv6
        ("::1", "12345", "https", "https://[::1]:12345"),
        ("[::1]", "12345", "https", "https://[::1]:12345"),
        ("http://[::1]:80", "12345", "https", "https://[::1]:12345"),
        ("2001:db8::1", "12345", "https", "https://[2001:db8::1]:12345"),
        ("[2001:db8::1]", "12345", "https", "https://[2001:db8::1]:12345"),
        ("[2001:db8::1]:80", "12345", "https", "https://[2001:db8::1]:12345"),
        ("http://[2001:db8::1]:80", "12345", "https", "https://[2001:db8::1]:12345"),
        # Domain
        ("example.com", "12345", "https", "https://example.com:12345"),
        ("example.com:80", "12345", "https", "https://example.com:12345"),
        ("http://example.com", "12345", "https", "https://example.com:12345"),
        ("http://example.com:80", "12345", "https", "https://example.com:12345"),
    ]

    for addr, port, scheme, expected in test_cases:
        result = build_url(addr, port, scheme)
        assert result == expected, f"Failed for {addr}: {result} != {expected}"
