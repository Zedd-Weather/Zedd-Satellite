"""SX1262 LoRa HAT driver for Zedd-Satellite.

The Waveshare / RAK SX1262 HAT plugs onto the Pi's 40-pin header and
exposes the Semtech SX1262 transceiver over SPI. We use it as an
out-of-band telemetry channel that is independent of the Wi-Fi / wired
network stack:

* The ground station periodically transmits a compact heartbeat frame
  (timestamp, CPU temperature, free disk %, last decoded image).
* If a paired payload (CubeSat / HAB) is in range the same radio is
  switched to RX between heartbeats and any received payload telemetry
  is logged to ``logs/lora-rx.log``.

This driver talks to **real hardware only** -- there is no simulation
mode. If the SPI device or GPIO library is missing it raises
:class:`LoRaUnavailable` and the daemon continues without the radio.

The implementation deliberately uses the chip's documented register
interface via ``spidev`` rather than a third-party black box library so
the behaviour is auditable and the dependency surface is minimal.
References: Semtech SX1261/2 datasheet rev 2.1, sections 11 & 13.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

LOGGER = logging.getLogger(__name__)

# --- Selected SX1262 opcodes (datasheet section 13) -----------------
_OP_SET_STANDBY = 0x80
_OP_SET_PACKET_TYPE = 0x8A
_OP_SET_RF_FREQUENCY = 0x86
_OP_SET_PA_CONFIG = 0x95
_OP_SET_TX_PARAMS = 0x8E
_OP_SET_BUFFER_BASE_ADDR = 0x8F
_OP_SET_MODULATION_PARAMS = 0x8B
_OP_SET_PACKET_PARAMS = 0x8C
_OP_WRITE_BUFFER = 0x0E
_OP_READ_BUFFER = 0x1E
_OP_SET_TX = 0x83
_OP_SET_RX = 0x82
_OP_GET_STATUS = 0xC0
_OP_GET_IRQ_STATUS = 0x12
_OP_CLEAR_IRQ_STATUS = 0x02
_OP_GET_RX_BUFFER_STATUS = 0x13

# Packet types
_PKT_TYPE_LORA = 0x01

# IRQ bits
_IRQ_TX_DONE = 1 << 0
_IRQ_RX_DONE = 1 << 1
_IRQ_TIMEOUT = 1 << 9


class LoRaUnavailable(RuntimeError):
    """Raised when the LoRa hardware cannot be opened."""


class LoRaError(RuntimeError):
    """Raised when an operation against the radio fails."""


@dataclass(frozen=True)
class LoRaConfig:
    """Tunable LoRa link parameters."""

    frequency_hz: int
    spreading_factor: int  # 7..12
    bandwidth_hz: int  # 7800..500000
    coding_rate: int  # 5..8 (4/5 .. 4/8)
    tx_power_dbm: int  # -9..22
    preamble_length: int = 8
    sync_word: int = 0x1424  # private network


# Bandwidth opcode mapping per datasheet
_BW_LOOKUP = {
    7800: 0x00, 10400: 0x08, 15600: 0x01, 20800: 0x09,
    31250: 0x02, 41700: 0x0A, 62500: 0x03,
    125000: 0x04, 250000: 0x05, 500000: 0x06,
}


class LoRaSX1262:
    """Minimal blocking driver for the SX1262 LoRa HAT.

    Args:
        settings: Parsed ``config/settings.json``. Reads ``lora.*``.
    """

    def __init__(self, settings: Dict) -> None:
        lora_cfg = settings.get("lora", {}) or {}
        self._enabled: bool = bool(lora_cfg.get("enabled", False))
        self._spi_bus: int = int(lora_cfg.get("spi_bus", 0))
        self._spi_device: int = int(lora_cfg.get("spi_device", 0))
        self._spi_speed_hz: int = int(lora_cfg.get("spi_speed_hz", 2_000_000))
        self._reset_pin: int = int(lora_cfg.get("reset_pin", 22))
        self._busy_pin: int = int(lora_cfg.get("busy_pin", 23))
        self._dio1_pin: int = int(lora_cfg.get("dio1_pin", 26))
        self._heartbeat_period_s: int = int(
            lora_cfg.get("heartbeat_period_s", 300)
        )

        self._config = LoRaConfig(
            frequency_hz=int(lora_cfg.get("frequency_hz", 868_000_000)),
            spreading_factor=int(lora_cfg.get("spreading_factor", 9)),
            bandwidth_hz=int(lora_cfg.get("bandwidth_hz", 125_000)),
            coding_rate=int(lora_cfg.get("coding_rate", 5)),
            tx_power_dbm=int(lora_cfg.get("tx_power_dbm", 14)),
        )

        # The spi/GPIO handles are opened lazily so the module can be
        # imported on dev machines that lack the libraries.
        self._spi = None
        self._gpio = None
        self._lock = threading.Lock()
        self._opened = False

    # ------------------------------------------------------------------ API
    @property
    def enabled(self) -> bool:
        """Whether the LoRa subsystem is enabled in config."""
        return self._enabled

    @property
    def heartbeat_period_s(self) -> int:
        """Configured heartbeat cadence in seconds."""
        return self._heartbeat_period_s

    def open(self) -> None:
        """Open the SPI bus + GPIO and bring the chip into a known state.

        Raises:
            LoRaUnavailable: If the kernel SPI device or the
                ``spidev`` / ``RPi.GPIO`` packages are missing.
        """
        if self._opened:
            return
        try:
            import spidev  # type: ignore
            import RPi.GPIO as GPIO  # type: ignore
        except ImportError as exc:
            raise LoRaUnavailable(
                f"LoRa libraries not installed: {exc}. "
                "Install spidev + RPi.GPIO on the Raspberry Pi."
            ) from exc

        try:
            spi = spidev.SpiDev()
            spi.open(self._spi_bus, self._spi_device)
            spi.max_speed_hz = self._spi_speed_hz
            spi.mode = 0
        except (FileNotFoundError, OSError) as exc:
            raise LoRaUnavailable(
                f"Cannot open /dev/spidev{self._spi_bus}.{self._spi_device}: "
                f"{exc}. Enable SPI with `sudo raspi-config`."
            ) from exc

        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self._reset_pin, GPIO.OUT, initial=GPIO.HIGH)
        GPIO.setup(self._busy_pin, GPIO.IN)
        GPIO.setup(self._dio1_pin, GPIO.IN)

        self._spi = spi
        self._gpio = GPIO
        self._opened = True

        try:
            self._reset_chip()
            self._wait_busy()
            self._configure_radio()
            LOGGER.info(
                "SX1262 LoRa HAT initialized: %.3f MHz SF%d BW%d CR4/%d %ddBm",
                self._config.frequency_hz / 1e6,
                self._config.spreading_factor,
                self._config.bandwidth_hz,
                self._config.coding_rate,
                self._config.tx_power_dbm,
            )
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Release the SPI handle and GPIO claims."""
        if not self._opened:
            return
        try:
            if self._spi is not None:
                self._spi.close()
        except Exception:  # pragma: no cover - defensive
            LOGGER.debug("Error closing SPI", exc_info=True)
        try:
            if self._gpio is not None:
                self._gpio.cleanup(
                    [self._reset_pin, self._busy_pin, self._dio1_pin]
                )
        except Exception:  # pragma: no cover - defensive
            LOGGER.debug("Error cleaning up GPIO", exc_info=True)
        self._spi = None
        self._gpio = None
        self._opened = False

    def transmit(self, payload: bytes, timeout_s: float = 10.0) -> None:
        """Transmit ``payload`` (max 255 bytes) blocking until TX done.

        Raises:
            LoRaError: If the operation fails or times out.
        """
        if not self._opened:
            raise LoRaError("LoRa radio is not open; call open() first")
        if not 1 <= len(payload) <= 255:
            raise LoRaError(
                f"Payload length must be 1..255, got {len(payload)}"
            )

        with self._lock:
            self._set_standby()
            self._command(_OP_CLEAR_IRQ_STATUS, [0xFF, 0xFF])
            # Set packet length on the fly
            self._command(
                _OP_SET_PACKET_PARAMS,
                [
                    (self._config.preamble_length >> 8) & 0xFF,
                    self._config.preamble_length & 0xFF,
                    0x00,           # variable length packet
                    len(payload),   # payload length
                    0x01,           # CRC on
                    0x00,           # standard IQ
                ],
            )
            self._command(_OP_SET_BUFFER_BASE_ADDR, [0x00, 0x00])
            self._command(_OP_WRITE_BUFFER, [0x00] + list(payload))
            # Timeout = 0 -> single TX, no internal timer
            self._command(_OP_SET_TX, [0x00, 0x00, 0x00])

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                irq = self._read_irq_status()
                if irq & _IRQ_TX_DONE:
                    self._command(_OP_CLEAR_IRQ_STATUS, [0xFF, 0xFF])
                    LOGGER.debug("LoRa TX complete (%d bytes)", len(payload))
                    return
                if irq & _IRQ_TIMEOUT:
                    self._command(_OP_CLEAR_IRQ_STATUS, [0xFF, 0xFF])
                    raise LoRaError("LoRa TX reported chip timeout IRQ")
                time.sleep(0.01)
            raise LoRaError(f"LoRa TX did not complete within {timeout_s}s")

    def receive(self, timeout_s: float = 5.0) -> Optional[bytes]:
        """Listen for a single packet up to ``timeout_s``.

        Returns the payload bytes, or ``None`` on timeout.
        """
        if not self._opened:
            raise LoRaError("LoRa radio is not open; call open() first")

        with self._lock:
            self._set_standby()
            self._command(_OP_CLEAR_IRQ_STATUS, [0xFF, 0xFF])
            # 24-bit timeout in 15.625 us steps; we poll instead, so use 0.
            self._command(_OP_SET_RX, [0x00, 0x00, 0x00])

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                irq = self._read_irq_status()
                if irq & _IRQ_RX_DONE:
                    self._command(_OP_CLEAR_IRQ_STATUS, [0xFF, 0xFF])
                    status = self._command(
                        _OP_GET_RX_BUFFER_STATUS, [0x00, 0x00, 0x00]
                    )
                    payload_len = status[2]
                    start_addr = status[3]
                    rx = self._command(
                        _OP_READ_BUFFER,
                        [start_addr] + [0x00] * (payload_len + 1),
                    )
                    return bytes(rx[2:])
                if irq & _IRQ_TIMEOUT:
                    self._command(_OP_CLEAR_IRQ_STATUS, [0xFF, 0xFF])
                    return None
                time.sleep(0.01)
            self._set_standby()
            return None

    # --------------------------------------------------------- internals
    def _reset_chip(self) -> None:
        """Pulse the RESET line to bring the SX1262 to a known state."""
        self._gpio.output(self._reset_pin, self._gpio.LOW)
        time.sleep(0.005)
        self._gpio.output(self._reset_pin, self._gpio.HIGH)
        time.sleep(0.010)

    def _wait_busy(self, timeout_s: float = 1.0) -> None:
        """Block until the chip's BUSY line is low."""
        deadline = time.monotonic() + timeout_s
        while self._gpio.input(self._busy_pin):
            if time.monotonic() > deadline:
                raise LoRaError("SX1262 BUSY line stuck high")
            time.sleep(0.001)

    def _command(self, opcode: int, payload: list) -> list:
        """Send an opcode + payload over SPI, returning the response bytes."""
        self._wait_busy()
        return self._spi.xfer2([opcode] + list(payload))

    def _read_irq_status(self) -> int:
        """Return the 16-bit IRQ status register."""
        resp = self._command(_OP_GET_IRQ_STATUS, [0x00, 0x00, 0x00])
        # First two bytes are NOP/status; payload is bytes 2..3
        return (resp[2] << 8) | resp[3]

    def _set_standby(self) -> None:
        """Place the radio in STDBY_RC."""
        self._command(_OP_SET_STANDBY, [0x00])

    def _configure_radio(self) -> None:
        """Apply the configured LoRa parameters to the chip."""
        self._set_standby()
        self._command(_OP_SET_PACKET_TYPE, [_PKT_TYPE_LORA])

        # Frequency: freq_reg = freq_hz * 2^25 / 32_000_000
        freq_reg = int(self._config.frequency_hz * (1 << 25) // 32_000_000)
        self._command(
            _OP_SET_RF_FREQUENCY,
            [
                (freq_reg >> 24) & 0xFF,
                (freq_reg >> 16) & 0xFF,
                (freq_reg >> 8) & 0xFF,
                freq_reg & 0xFF,
            ],
        )

        # PA config for SX1262 (high power) -- datasheet table 13-21.
        self._command(_OP_SET_PA_CONFIG, [0x04, 0x07, 0x00, 0x01])
        # TX params: power dBm + ramp time 200us (0x04)
        power = max(-9, min(22, self._config.tx_power_dbm))
        self._command(_OP_SET_TX_PARAMS, [power & 0xFF, 0x04])

        # Modulation: SF, BW, CR, low-data-rate-optimize off
        bw_op = _BW_LOOKUP.get(self._config.bandwidth_hz)
        if bw_op is None:
            raise LoRaError(
                f"Unsupported LoRa bandwidth {self._config.bandwidth_hz} Hz"
            )
        self._command(
            _OP_SET_MODULATION_PARAMS,
            [
                self._config.spreading_factor & 0xFF,
                bw_op,
                (self._config.coding_rate - 4) & 0xFF,
                0x00,
            ],
        )

        # Packet params: variable length, CRC on
        self._command(
            _OP_SET_PACKET_PARAMS,
            [
                (self._config.preamble_length >> 8) & 0xFF,
                self._config.preamble_length & 0xFF,
                0x00, 0xFF, 0x01, 0x00,
            ],
        )
        self._command(_OP_SET_BUFFER_BASE_ADDR, [0x00, 0x00])
