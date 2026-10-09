"""The stream watchdog: a connection that looks open but delivers nothing is torn down and reported, never left silent."""
import sys
import types
import unittest
from unittest import mock

import market_stream


class FakeStreamer:
    instances: list["FakeStreamer"] = []

    def __init__(self, api_client, instruments, mode):
        self.handlers, self.calls = {}, []
        FakeStreamer.instances.append(self)

    def auto_reconnect(self, enable, *args):
        self.calls.append(("auto_reconnect", enable))

    def on(self, event, handler):
        self.handlers[event] = handler

    def connect(self):
        self.calls.append(("connect",))

    def disconnect(self):
        self.calls.append(("disconnect",))

    def subscribe(self, *args):
        pass


class Watchdog(unittest.TestCase):
    def setUp(self):
        FakeStreamer.instances.clear()
        sdk = types.SimpleNamespace(Configuration=lambda: types.SimpleNamespace(), ApiClient=lambda c: None, MarketDataStreamerV3=FakeStreamer)
        patcher = mock.patch.dict(sys.modules, {"upstox_client": sdk})
        patcher.start()
        self.addCleanup(patcher.stop)
        market_stream._stop.clear()
        self.addCleanup(market_stream._stop.clear)
        env = mock.patch.dict("os.environ", {"UPSTOX_STREAM_STALL_SECONDS": "0.05", "UPSTOX_STREAM_WATCH_SECONDS": "0.02"})
        env.start()
        self.addCleanup(env.stop)

    def test_silence_during_the_session_tears_the_connection_down_and_raises(self):
        with self.assertRaises(market_stream.StreamStalled):
            market_stream.run_market_stream("token", lambda message: None, ["NSE_INDEX|Nifty 50"], session_active=lambda: True)
        calls = FakeStreamer.instances[0].calls
        self.assertIn(("auto_reconnect", False), calls, "the SDK must not quietly reconnect the abandoned socket")
        self.assertIn(("disconnect",), calls)

    def test_silence_outside_the_session_is_normal_and_not_a_stall(self):
        import threading
        errors: list[BaseException] = []

        def run():
            try:
                market_stream.run_market_stream("token", lambda message: None, ["NSE_INDEX|Nifty 50"], session_active=lambda: False)
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=0.5)
        self.assertTrue(thread.is_alive(), "still waiting on the connection")
        self.assertEqual(errors, [])
        market_stream.request_stop()  # app shutdown: the function returns and closes the connection
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), "shutdown must never wait on the stream")
        self.assertIn(("disconnect",), FakeStreamer.instances[0].calls)


if __name__ == "__main__":
    unittest.main()
