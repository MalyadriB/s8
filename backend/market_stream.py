import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("uvicorn.error")


_stop = threading.Event()


def request_stop() -> None:
    """Called when the app shuts down (or reloads): lets run_market_stream return, so the process can actually exit
    instead of waiting forever on this thread."""
    _stop.set()


class StreamStalled(RuntimeError):
    """The connection is open as far as the SDK knows but no message has arrived for too long."""


def stream_instruments() -> list[str]:
    return [
        value.strip()
        for value in os.getenv("UPSTOX_INSTRUMENT_TOKENS", "").split(",")
        if value.strip()
    ]


def run_market_stream(
    access_token: str,
    on_message: Callable[[Any], None],
    instruments: list[str] | None = None,
    refresh_instruments: Callable[[], list[str]] | None = None,
    extra_instruments: Callable[[], list[str]] | None = None,
    session_active: Callable[[], bool] | None = None,
) -> None:
    """Run the blocking Upstox V3 stream until the process is stopped.

    `extra_instruments` names instruments to ADD to the subscription (the paper-trading engine's strikes). It is
    polled every few seconds so a request made at 09:25 is subscribed within seconds, and the periodic refresh
    below never unsubscribes them."""
    instruments = instruments or stream_instruments()
    if not instruments:
        logger.warning("Market stream not started: UPSTOX_INSTRUMENT_TOKENS is empty")
        return

    try:
        import upstox_client
    except ImportError as error:
        logger.error("Market stream requires upstox-python-sdk")
        raise RuntimeError("Install upstox-python-sdk to enable market streaming") from error

    configuration = upstox_client.Configuration()
    configuration.access_token = access_token
    streamer = upstox_client.MarketDataStreamerV3(
        upstox_client.ApiClient(configuration),
        instruments,
        os.getenv("UPSTOX_STREAM_MODE", "full"),
    )
    streamer.auto_reconnect(True, 5, 3)
    streamer.on("open", lambda *args: logger.info("Upstox market stream connected"))
    streamer.on("close", lambda *args: logger.warning("Upstox market stream closed"))
    streamer.on("reconnecting", lambda *args: logger.warning("Upstox market stream reconnecting"))
    streamer.on("error", lambda *args: logger.error("Upstox market stream error: %s", args))
    last_message = [time.monotonic()]

    def on_any_message(message: Any) -> None:
        last_message[0] = time.monotonic()
        on_message(message)

    streamer.on("message", on_any_message)
    logger.info("Starting Upstox market stream for %d instruments", len(instruments))

    stop_refresh = threading.Event()
    subscribed = set(instruments)
    subscribed_lock = threading.Lock()

    def wanted_extras() -> set[str]:
        return set(extra_instruments()) if extra_instruments else set()

    def refresh_loop() -> None:
        interval = int(os.getenv("UPSTOX_RESUBSCRIBE_SECONDS", "300"))
        while not stop_refresh.wait(interval):
            try:
                with subscribed_lock:
                    updated = (set(refresh_instruments()) if refresh_instruments else set(instruments)) | wanted_extras()
                    added = sorted(updated - subscribed)
                    removed = sorted(subscribed - updated)
                    if removed:
                        streamer.unsubscribe(removed)
                    if added:
                        streamer.subscribe(added, os.getenv("UPSTOX_STREAM_MODE", "full"))
                    if added or removed:
                        logger.info("Updated stream subscriptions: added=%d removed=%d", len(added), len(removed))
                    subscribed.clear()
                    subscribed.update(updated)
            except Exception:
                logger.exception("Market stream subscription refresh failed")

    def extras_loop() -> None:
        while not stop_refresh.wait(5):
            try:
                with subscribed_lock:
                    added = sorted(wanted_extras() - subscribed)
                    if added:
                        streamer.subscribe(added, os.getenv("UPSTOX_STREAM_MODE", "full"))
                        subscribed.update(added)
                        logger.info("Subscribed %d extra instruments on request", len(added))
            except Exception:
                logger.exception("Market stream extra subscription failed")

    refresh_thread = threading.Thread(target=refresh_loop, daemon=True)
    if refresh_instruments or extra_instruments:
        refresh_thread.start()
    if extra_instruments:
        threading.Thread(target=extras_loop, daemon=True).start()
    try:
        # connect() only starts the websocket's own thread and returns; this loop is what keeps the function (and so
        # the subscription refresh threads) alive, and it is the watchdog.
        streamer.connect()
        connected_at = time.monotonic()
        stall_seconds = float(os.getenv("UPSTOX_STREAM_STALL_SECONDS", "20"))
        watch_seconds = float(os.getenv("UPSTOX_STREAM_WATCH_SECONDS", "2"))
        while not _stop.wait(watch_seconds):
            quiet = time.monotonic() - max(last_message[0], connected_at)
            if quiet > stall_seconds and (session_active is None or session_active()):
                logger.warning("Upstox market stream delivered nothing for %.0fs - rebuilding the connection", quiet)
                _teardown(streamer)
                raise StreamStalled(f"no stream message for {quiet:.0f}s")
        _teardown(streamer)  # shutting down
    finally:
        stop_refresh.set()
        if refresh_thread.is_alive():
            refresh_thread.join(timeout=2)


def _teardown(streamer: Any) -> None:
    """Close the old connection without letting the SDK reconnect it, and never hang on a half-open socket."""
    def close() -> None:
        try:
            streamer.auto_reconnect(False)
            streamer.disconnect()
        except Exception:  # noqa: BLE001 - the connection is being abandoned anyway
            logger.debug("Closing the stalled market stream raised", exc_info=True)

    closer = threading.Thread(target=close, daemon=True)
    closer.start()
    closer.join(timeout=5)
