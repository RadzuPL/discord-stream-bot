"""discord-stream-bot

Joins one Discord voice channel and plays an audio stream (e.g. an Icecast/MP3
radio stream) there, but only while somebody is actually listening. Configuration
is done purely through environment variables. The bot registers no commands and
needs no privileged intents.
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import ipaddress
import logging
import os
import queue
import shlex
import socket
import sys
import threading
import time
from array import array
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import discord

log = logging.getLogger("streambot")

TICK_SECONDS = 5          # how often the supervisor re-checks everything (events wake it earlier)
QUICK_FAIL_SECONDS = 10   # a stream that dies sooner than this counts as a failed start
MAX_BACKOFF = 60          # cap for retry delays, seconds
REJOIN_DELAY = 5          # pause before rejoining voice after leaving it (see _rejoin_cooldown)
PREFETCH_FRAMES = 250     # 20 ms frames read ahead of the player (5 s): room to buffer during the voice handshake
AUDIBLE_PEAK = 64         # 16-bit peak above which a frame counts as sound rather than silence (about -54 dBFS)
AUDIBLE_WATCH = 120       # seconds after opening the stream during which the first audible frame is looked for
SLOW_DNS = 1.0            # a stream host lookup slower than this is logged as a warning

# The last three options are about start-up time. Without them ffmpeg guesses the input format by
# reading up to 1 MB of it (`formatprobesize`), and a raw MP3 stream from Icecast has no header to
# make that guess certain early - so on a live 128 kbps mount it waited ~8 s for data that arrives
# in real time (Icecast only sends a ~0.5 s burst on connect). 8 KB is several MP3 frames at any
# bitrate and still recognises Ogg/Opus and AAC. Measured against a simulated mount: 7.8 s -> 0.16 s.
DEFAULT_FFMPEG_BEFORE = (
    "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 "
    "-reconnect_delay_max 5 -rw_timeout 15000000 "
    "-formatprobesize 8192 -probesize 32768 -analyzeduration 500000"
)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Config:
    token: str
    channel_id: int
    stream_url: str
    volume: float
    idle_timeout: int
    leave_when_empty: bool
    stream_ipv4: bool
    log_level: str
    health_file: Path
    ffmpeg_before: str


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    sys.exit(f"{name} must be true or false (also accepted: 1/0, yes/no, on/off).")


def load_config() -> Config:
    def need(name: str) -> str:
        value = os.getenv(name, "").strip()
        if not value:
            sys.exit(f"Missing required environment variable: {name}")
        return value

    token = need("DISCORD_TOKEN")
    stream_url = need("STREAM_URL")
    try:
        channel_id = int(need("VOICE_CHANNEL_ID"))
    except ValueError:
        sys.exit("VOICE_CHANNEL_ID must be a number (the voice channel's ID).")
    try:
        volume = float(os.getenv("VOLUME", "1.0"))
    except ValueError:
        sys.exit("VOLUME must be a number, e.g. 1.0 or 0.5.")
    try:
        idle_timeout = int(os.getenv("IDLE_TIMEOUT", "60"))
    except ValueError:
        sys.exit("IDLE_TIMEOUT must be a whole number of seconds (0 = always play).")

    return Config(
        token=token,
        channel_id=channel_id,
        stream_url=stream_url,
        volume=volume,
        idle_timeout=idle_timeout,
        leave_when_empty=_env_bool("LEAVE_WHEN_EMPTY", True),
        stream_ipv4=_env_bool("STREAM_IPV4", True),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        health_file=Path(os.getenv("HEALTH_FILE", "/tmp/streambot.healthy")),
        ffmpeg_before=os.getenv("FFMPEG_BEFORE_OPTIONS", DEFAULT_FFMPEG_BEFORE),
    )


def _safe_url(url: str) -> str:
    """Strip credentials and query string so the URL is safe to log."""
    parts = urlsplit(url)
    if not parts.hostname:
        return "<stream>"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}{parts.path}"


def _ipv4_target(url: str) -> tuple[str, str | None]:
    """Resolve a plain-http stream host to an IPv4 address up front.

    Returns the URL to hand ffmpeg and the Host header to send with it (None = use the URL as is).
    ffmpeg resolves the host itself with an IPv4+IPv6 lookup, and inside a container the IPv6
    half of that lookup for a Docker container name can wait for a DNS timeout (Docker's embedded
    resolver passes it upstream), which delays every stream start by seconds. Asking for IPv4 only
    is answered by Docker's resolver at once. HTTPS is left alone: rewriting it to an IP would break
    certificate checks.
    """
    parts = urlsplit(url)
    host = parts.hostname
    if parts.scheme != "http" or not host:
        return url, None
    try:
        ipaddress.ip_address(host)
        return url, None  # already an address
    except ValueError:
        pass
    infos = socket.getaddrinfo(host, parts.port or 80, socket.AF_INET, socket.SOCK_STREAM)
    ip = infos[0][4][0]
    netloc = ip + (f":{parts.port}" if parts.port else "")
    if "@" in parts.netloc:
        netloc = parts.netloc.rsplit("@", 1)[0] + "@" + netloc
    host_header = host + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)), host_header


def _is_audible(frame: bytes) -> bool:
    samples = array("h")
    samples.frombytes(frame[: len(frame) - len(frame) % 2])
    return bool(samples) and (max(samples) > AUDIBLE_PEAK or min(samples) < -AUDIBLE_PEAK)


class _PrefetchedSource(discord.AudioSource):
    """Reads the stream on its own thread from the moment ffmpeg starts.

    Two jobs: the stream connects and buffers while the voice connection is still being set up
    (instead of only after it), and the log gets real timings - when the first audio arrived from
    the stream and when it first stopped being silence (an audience-gated station sends silence
    until it is back on air).
    """

    def __init__(self, inner: discord.AudioSource, opened_at: float) -> None:
        self._inner = inner
        self._opened_at = opened_at
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=PREFETCH_FRAMES)
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._pump, name="stream-prefetch", daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        got_audio = heard = False
        try:
            while not self._closed.is_set():
                frame = self._inner.read()
                if not frame:
                    break
                elapsed = time.monotonic() - self._opened_at
                if not got_audio:
                    got_audio = True
                    log.info("Stream is delivering audio %.1fs after opening it", elapsed)
                if not heard and elapsed < AUDIBLE_WATCH and _is_audible(frame):
                    heard = True
                    log.info("Stream became audible %.1fs after opening it", elapsed)
                self._put(frame)
        except Exception as exc:  # noqa: BLE001 - reader thread must end cleanly
            log.debug("Stream reader stopped: %s", exc)
        finally:
            self._put(b"")  # end of stream for the player

    def _put(self, frame: bytes) -> None:
        while not self._closed.is_set():
            try:
                self._frames.put(frame, timeout=0.5)
                return
            except queue.Full:
                continue

    def read(self) -> bytes:
        while not self._closed.is_set():
            try:
                return self._frames.get(timeout=0.5)
            except queue.Empty:
                continue
        return b""

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self._closed.set()
        self._inner.cleanup()


class StreamBot(discord.Client):
    def __init__(self, cfg: Config) -> None:
        # Default intents only: no privileged intents needed for voice. The default set
        # includes voice_states, so the bot sees who is in the channel even when it is
        # not connected there itself (needed for LEAVE_WHEN_EMPTY).
        super().__init__(intents=discord.Intents.default())
        self.cfg = cfg
        self._supervisor_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._play_started = 0.0
        self._fail_streak = 0
        self._alone_since: float | None = None
        self._expect_stop = False
        self._had_voice = False
        self._left_voice_at: float | None = None
        self._pending: discord.AudioSource | None = None  # stream opened ahead of a (re)join

    async def setup_hook(self) -> None:
        self._supervisor_task = asyncio.create_task(self._supervise(), name="stream-supervisor")

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)

    async def on_voice_state_update(self, member, before, after) -> None:
        # Somebody joined/left/(un)deafened in the target channel: re-check right away
        # instead of waiting for the next periodic tick.
        target = self.cfg.channel_id
        if (before.channel and before.channel.id == target) or (after.channel and after.channel.id == target):
            self._wake.set()

    async def close(self) -> None:
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
        self._discard_pending()
        await self._drop_voice()
        await super().close()

    # ---- supervisor -------------------------------------------------------

    async def _supervise(self) -> None:
        await self.wait_until_ready()
        error_streak = 0
        while not self.is_closed():
            try:
                await self._tick()
                error_streak = 0
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=TICK_SECONDS)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - supervisor must never die
                error_streak += 1
                wait = min(2 ** error_streak, MAX_BACKOFF)
                log.error(
                    "Check failed (%s: %s); retrying in %ds",
                    type(exc).__name__, exc, wait,
                    exc_info=(error_streak == 1),
                )
                await self._drop_voice()
                await asyncio.sleep(wait)

    async def _tick(self) -> None:
        channel = await self._resolve_channel()
        vc = channel.guild.voice_client

        if vc is not None and not vc.is_connected():
            log.warning("Stale voice client, discarding it")
            await vc.disconnect(force=True)
            self._left_voice_at = time.monotonic()
            vc = None

        listeners = self._count_listeners(channel)

        if self.cfg.leave_when_empty:
            playing = vc is not None and vc.is_playing()
            if not self._should_stream(listeners, playing=playing):
                # Nobody to play for (and the grace period is over): leave the channel so
                # the server icon stops showing an ongoing voice call.
                self._discard_pending()
                if vc is not None:
                    self._stop_stream(vc)
                    log.info("Leaving #%s, nobody is listening", channel.name)
                    await self._drop_voice()
                self._touch_health()  # out of the channel on purpose = healthy
                return

        if vc is None:
            if self._pending is None and (listeners > 0 or self.cfg.idle_timeout <= 0):
                # Open the stream now, so it connects and buffers during the rejoin pause and the
                # voice handshake (several seconds with DAVE) instead of only after them.
                self._pending = await self._open_stream()
            await self._rejoin_cooldown()
            vc = channel.guild.voice_client  # the library may have reconnected meanwhile

        if vc is None:
            log.info("Joining #%s in %s", channel.name, channel.guild.name)
            try:
                vc = await channel.connect(timeout=30.0, reconnect=True, self_deaf=True)
            except BaseException:
                self._discard_pending()
                raise
        elif vc.channel.id != channel.id:
            log.info("Bot was moved out of the target channel, returning")
            await vc.move_to(channel)

        if vc.is_connected():
            self._had_voice = True
            self._left_voice_at = None

        listeners = self._count_listeners(channel)  # again: joining takes seconds, people come and go
        if self._should_stream(listeners, playing=vc.is_playing()):
            await self._ensure_stream(vc)
            idle = False
        else:
            self._discard_pending()
            self._stop_stream(vc)
            idle = True

        # Healthy = connected and either playing or idle on purpose (nobody to play for).
        if vc.is_connected() and (idle or vc.is_playing()):
            self._touch_health()

    async def _rejoin_cooldown(self) -> None:
        """Wait a few seconds before rejoining voice after leaving it (on purpose or not).

        When a voice session ends, Discord confirms it with a "left voice" event. If a new
        session is already starting by then (the supervisor is woken by that very event and
        would rejoin within the same second), discord.py attributes the stale event to the new
        session, treats it as a forced disconnect, waits 30 s for a reconnect, gives up and
        drops the new session too - and the cycle repeats forever. A short pause lets the old
        confirmation arrive and be discarded first.
        """
        now = time.monotonic()
        if self._left_voice_at is None:
            if not self._had_voice:
                return  # very first join: nothing to wait for
            self._left_voice_at = now  # first time we notice the drop
        remaining = REJOIN_DELAY - (now - self._left_voice_at)
        if remaining > 0:
            log.info("Rejoining voice in %.0fs", remaining)
            await asyncio.sleep(remaining)

    async def _resolve_channel(self) -> discord.VoiceChannel:
        channel = self.get_channel(self.cfg.channel_id) or await self.fetch_channel(self.cfg.channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            raise RuntimeError(
                f"Channel {self.cfg.channel_id} is not a regular voice channel "
                "(stage channels are not supported)"
            )
        return channel

    # ---- listeners --------------------------------------------------------

    def _count_listeners(self, channel: discord.VoiceChannel) -> int:
        """People in the channel who can actually hear the stream (not us, not other bots, not deafened)."""
        count = 0
        for user_id, state in channel.voice_states.items():
            if user_id == self.user.id:
                continue
            if state.deaf or state.self_deaf:
                continue
            member = channel.guild.get_member(user_id)
            if member is not None and member.bot:
                continue
            count += 1
        return count

    def _should_stream(self, listeners: int, playing: bool) -> bool:
        if self.cfg.idle_timeout <= 0 or listeners > 0:
            if self._alone_since is not None:
                log.info("Listener present again")
            self._alone_since = None
            return True

        now = time.monotonic()
        if self._alone_since is None:
            self._alone_since = now
            if playing:
                action = "leaving the channel" if self.cfg.leave_when_empty else "stopping the stream"
                log.info("Channel is empty, %s in %ds unless somebody joins", action, self.cfg.idle_timeout)
        # The grace period only keeps an already running stream alive;
        # an empty channel never starts a new one.
        return playing and (now - self._alone_since) < self.cfg.idle_timeout

    # ---- playback ---------------------------------------------------------

    async def _ensure_stream(self, vc: discord.VoiceClient) -> None:
        if vc.is_playing() or vc.is_paused():
            return

        since_start = time.monotonic() - self._play_started
        if self._play_started and since_start < QUICK_FAIL_SECONDS:
            self._fail_streak += 1
        else:
            self._fail_streak = 0

        if self._fail_streak:
            wait = min(2 ** self._fail_streak, MAX_BACKOFF)
            log.warning(
                "Stream ended after %.0fs (failure streak %d), retrying in %ds",
                since_start, self._fail_streak, wait,
            )
            await asyncio.sleep(wait)

        source = self._pending or await self._open_stream()
        self._pending = None
        log.info("Starting playback")
        vc.play(source, after=self._on_playback_end)
        self._play_started = time.monotonic()

    def _stop_stream(self, vc: discord.VoiceClient) -> None:
        if vc.is_playing() or vc.is_paused():
            log.info("Nobody has been listening for %ds, stopping the stream", self.cfg.idle_timeout)
            self._expect_stop = True
            vc.stop()
        self._play_started = 0.0
        self._fail_streak = 0

    async def _stream_target(self) -> tuple[str, str | None]:
        url = self.cfg.stream_url
        if not self.cfg.stream_ipv4:
            return url, None
        started = time.monotonic()
        try:
            target = await asyncio.get_running_loop().run_in_executor(None, _ipv4_target, url)
        except OSError as exc:
            log.warning("IPv4 lookup for the stream host failed (%s); letting ffmpeg resolve it", exc)
            return url, None
        took = time.monotonic() - started
        if took > SLOW_DNS:
            log.warning("IPv4 lookup for the stream host took %.1fs", took)
        return target

    async def _open_stream(self) -> discord.AudioSource:
        url, host_header = await self._stream_target()
        before = self.cfg.ffmpeg_before
        if host_header:
            before += " -headers " + shlex.quote(f"Host: {host_header}\r\n")
        log.info("Opening stream from %s", _safe_url(self.cfg.stream_url))
        opened_at = time.monotonic()
        ffmpeg = discord.FFmpegPCMAudio(url, before_options=before, options="-vn")
        source: discord.AudioSource = _PrefetchedSource(ffmpeg, opened_at)
        if abs(self.cfg.volume - 1.0) > 1e-3:
            source = discord.PCMVolumeTransformer(source, volume=self.cfg.volume)
        return source

    def _discard_pending(self) -> None:
        if self._pending is not None:
            self._pending.cleanup()
            self._pending = None

    def _on_playback_end(self, error: Exception | None) -> None:
        # Runs in the player thread; the supervisor restarts playback on its next tick.
        if self._expect_stop:
            self._expect_stop = False
            return
        if error:
            log.error("Playback stopped with error: %s", error)
        else:
            log.warning("Playback stopped (stream ended or ffmpeg exited)")

    # ---- helpers ----------------------------------------------------------

    async def _drop_voice(self) -> None:
        for vc in list(self.voice_clients):
            try:
                await vc.disconnect(force=True)
            except Exception as exc:  # noqa: BLE001
                log.debug("Voice disconnect failed: %s", exc)
            self._left_voice_at = time.monotonic()

    def _touch_health(self) -> None:
        try:
            self.cfg.health_file.touch()
        except OSError as exc:
            log.debug("Could not update health file: %s", exc)


def main() -> None:
    cfg = load_config()
    level = getattr(logging, cfg.log_level, logging.INFO)
    # Configure logging first, otherwise the startup lines below would be dropped.
    discord.utils.setup_logging(level=level, root=True)

    try:
        davey_version = importlib.metadata.version("davey")
    except importlib.metadata.PackageNotFoundError:
        davey_version = None
    log.info("discord.py %s, davey %s", discord.__version__, davey_version or "MISSING")
    if davey_version is None:
        # Since 2026-03-01 Discord voice requires DAVE (E2EE); without davey voice will not work.
        print("WARNING: 'davey' is not installed - Discord voice requires DAVE support.", file=sys.stderr)
    if cfg.idle_timeout > 0:
        where = "leaves the channel" if cfg.leave_when_empty else "stays in the channel, silent"
        log.info(
            "Idle mode: %ds after the last listener leaves, the stream stops and the bot %s",
            cfg.idle_timeout, where,
        )
    else:
        log.info("Idle mode disabled: the stream plays continuously and the bot never leaves")

    client = StreamBot(cfg)
    client.run(cfg.token, log_handler=None)  # logging is already configured above


if __name__ == "__main__":
    main()
