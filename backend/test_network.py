import socket
import unittest
from unittest import mock

import network


class PreferIpv4(unittest.TestCase):
    def test_ipv4_addresses_come_first_and_ipv6_stays_as_a_fallback(self):
        v6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4407::6812:284d", 443, 0, 0))
        v4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.18.40.77", 443))
        with mock.patch.object(network, "_original_getaddrinfo", return_value=[v6, v4]):
            self.assertEqual(network._ipv4_first("api.upstox.com", 443), [v4, v6])


if __name__ == "__main__":
    unittest.main()
