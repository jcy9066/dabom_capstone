"""DABOM manual button alert sequencer (integration-ready, no hardware access).

Callbacks are injected by the Pi client, so this module neither opens UART,
plays audio, sends Telegram nor touches GPIO on import. The caller MUST use an
actual TTS playback callback that completes only after the audio has finished.
A report must be accepted by Telegram before its caller starts a report alert.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Literal

AlertKind = Literal['warning', 'report']
AsyncLed = Callable[[bool], Awaitable[None]]
AsyncBeep = Callable[[int], Awaitable[None]]
AsyncTts = Callable[[AlertKind], Awaitable[None]]

log = logging.getLogger(__name__)


class AlertBusyError(RuntimeError):
    """A manual alert is already playing; do not overlap physical outputs."""


class ManualAlertEngine:
    def __init__(
        self,
        *,
        beep: AsyncBeep,
        led: AsyncLed,
        tts: AsyncTts,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        half_period_sec: float = 0.5,
    ) -> None:
        if not 0 < half_period_sec <= 2:
            raise ValueError('half_period_sec must be positive and <= 2')
        self.beep = beep
        self.led = led
        self.tts = tts
        self.sleep = sleep
        self.half_period_sec = half_period_sec
        self._task: asyncio.Task[None] | None = None
        self._reserved = False

    @property
    def busy(self) -> bool:
        return self._reserved or (self._task is not None and not self._task.done())

    def start(self, kind: AlertKind) -> asyncio.Task[None]:
        """Schedule once and return immediately; caller may ACK 'accepted'.

        A 10s-old automatic warning MUST NOT call this path.
        """
        if kind not in ('warning', 'report'):
            raise ValueError('invalid manual alert kind')
        if self.busy:
            raise AlertBusyError('manual alert already active')
        self._reserved = True
        task = asyncio.create_task(self._run(kind), name=f'dabom-manual-{kind}')
        self._task = task
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[None]) -> None:
        if self._task is task:
            self._task = None
            self._reserved = False
        try:
            error = task.exception()
            if error is not None:
                log.error('manual alert failed: %s', error)
        except asyncio.CancelledError:
            log.warning('manual alert cancelled')

    async def _blink_warning_led(self, stop: asyncio.Event) -> None:
        """LED toggles at 0.5s cadence until the TTS playback completes."""
        while not stop.is_set():
            await self.led(True)
            await self.sleep(self.half_period_sec)
            if stop.is_set():
                break
            await self.led(False)
            await self.sleep(self.half_period_sec)

    async def _run(self, kind: AlertKind) -> None:
        repeats = 3 if kind == 'warning' else 2
        stop_led = asyncio.Event()
        led_task: asyncio.Task | None = None
        if kind == 'warning':
            led_task = asyncio.create_task(self._blink_warning_led(stop_led), name='dabom-warning-led')
            await asyncio.sleep(0)  # first LED ON before the first Beep command
        try:
            for _ in range(repeats):
                # Pico generates the ON pulse; schedule the next pulse at 1s
                # from this command's start, rather than adding ACK latency.
                start = asyncio.get_running_loop().time()
                await self.beep(round(self.half_period_sec * 1000))
                elapsed = asyncio.get_running_loop().time() - start
                await self.sleep(max(0, 2 * self.half_period_sec - elapsed))
            await self.tts(kind)  # must await REAL audio completion
        finally:
            if led_task is not None:
                stop_led.set()
                led_task.cancel()
                try:
                    await led_task
                except asyncio.CancelledError:
                    pass
                await self.led(False)

    async def close(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._reserved = False
