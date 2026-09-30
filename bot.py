"""discord-stream-bot

Joins one Discord voice channel and plays an audio stream (e.g. an Icecast/MP3
radio stream) there, 24/7. Configuration is done purely through environment
variables. The bot registers no commands and needs no privileged intents.
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import discord

log = logging.getLogger("streambot")

TICK_SECONDS = 5          # how often the supervisor checks the connection
QUICK_FAIL_SECONDS = 10   # a stream that dies sooner than this counts as a failed start
MAX_BACKOFF = 60          # cap for retry delays, seconds

DEFAULT_FFMPEG_BEFORE = (
    "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 "
    "-reconnect_delay_max 5 -rw_timeout 15000000"
)


@dataclass(frozen=True)
class Config:
    token: str
    channel_id: int
    stream_url: str
    volume: float
    log_level: str
    health_file: Path
    ffmpeg_before: str


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

    return Config(
        token=token,
        channel_id=channel_id,
        stream_url=stream_url,
        volume=volume,
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


class StreamBot(discord.Client):
    def __init__(self, cfg: Config) -> None:
        # Default intents only: no privileged intents needed for voice.
        super().__init__(intents=discord.Intents.default())
        self.cfg = cfg
        self._supervisor_task: asyncio.Task | None = None
        self._play_started = 0.0
        self._fail_streak = 0

    async def setup_hook(self) -> None:
        self._supervisor_task = asyncio.create_task(self._supervise(), name="stream-supervisor")

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)

    async def close(self) -> None:
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
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
                await asyncio.sleep(TICK_SECONDS)
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
            vc = None

        if vc is None:
            log.info("Joining #%s in %s", channel.name, channel.guild.name)
            vc = await channel.connect(timeout=30.0, reconnect=True, self_deaf=True)
        elif vc.channel.id != channel.id:
            log.info("Bot was moved out of the target channel, returning")
            await vc.move_to(channel)

        await self._ensure_stream(vc)

        if vc.is_connected() and vc.is_playing():
            self._touch_health()

    async def _resolve_channel(self) -> discord.VoiceChannel:
        channel = self.get_channel(self.cfg.channel_id) or await self.fetch_channel(self.cfg.channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            raise RuntimeError(
                f"Channel {self.cfg.channel_id} is not a regular voice channel "
                "(stage channels are not supported)"
            )
        return channel

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

        log.info("Starting stream from %s", _safe_url(self.cfg.stream_url))
        vc.play(self._make_source(), after=self._on_playback_end)
        self._play_started = time.monotonic()

    def _make_source(self) -> discord.AudioSource:
        source: discord.AudioSource = discord.FFmpegPCMAudio(
            self.cfg.stream_url,
            before_options=self.cfg.ffmpeg_before,
            options="-vn",
        )
        if abs(self.cfg.volume - 1.0) > 1e-3:
            source = discord.PCMVolumeTransformer(source, volume=self.cfg.volume)
        return source

    def _on_playback_end(self, error: Exception | None) -> None:
        # Runs in the player thread; the supervisor restarts playback on its next tick.
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

    def _touch_health(self) -> None:
        try:
            self.cfg.health_file.touch()
        except OSError as exc:
            log.debug("Could not update health file: %s", exc)


def main() -> None:
    cfg = load_config()
    level = getattr(logging, cfg.log_level, logging.INFO)

    try:
        davey_version = importlib.metadata.version("davey")
    except importlib.metadata.PackageNotFoundError:
        davey_version = None
    log.info("discord.py %s, davey %s", discord.__version__, davey_version or "MISSING")
    if davey_version is None:
        # Since 2026-03-01 Discord voice requires DAVE (E2EE); without davey voice will not work.
        print("WARNING: 'davey' is not installed - Discord voice requires DAVE support.", file=sys.stderr)

    client = StreamBot(cfg)
    client.run(cfg.token, log_level=level, root_logger=True)


if __name__ == "__main__":
    main()
