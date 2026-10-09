"""Outbound connections try IPv4 addresses before IPv6.

Python connects to the addresses DNS returns one at a time, in order, and Upstox (behind Cloudflare) lists IPv6
first. On 6 Oct 2026 this network's IPv6 route was dead: every connection waited ~21 s on each of two IPv6
addresses before IPv4 answered in 0.05 s, so each Upstox call took ~42 s, the live stream's ~24-call start-up took
~17 minutes, and the paper engine had no prices all morning. Browsers and curl race both families and never
noticed. Reordering keeps IPv6 as a fallback; it only stops a broken IPv6 route from stalling every request."""
import socket

_original_getaddrinfo = socket.getaddrinfo


def _ipv4_first(*args, **kwargs):
    return sorted(_original_getaddrinfo(*args, **kwargs), key=lambda info: info[0] != socket.AF_INET)


def prefer_ipv4() -> None:
    socket.getaddrinfo = _ipv4_first
