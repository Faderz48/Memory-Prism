#!/usr/bin/env python3
from __future__ import annotations

import ctypes
import functools
import operator
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from cryptography.hazmat.backends import default_backend
try:
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
except ImportError:
    from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes


VID = 0x054C
PID = 0x02EA
ENDPOINT_IN = 0x81
ENDPOINT_OUT = 0x02

CARD_KEY_1 = bytes.fromhex("CE62F68420B65A81E459FA9A2BB3598A")
CARD_IV_1 = bytes.fromhex("6C26D37F46EE9DA9")
CARD_KEY_2 = bytes.fromhex("7014A32FCC5B1237AC1FBF4ED26D1CC1")
CARD_IV_2 = bytes.fromhex("2CD160FA8C2ED362")
CHALLENGE_IV = bytes.fromhex("2C5BF48D32749127")
MECHA_NONCE = bytes.fromhex("DEADC0DEDEADC0DE")
POWERWAVE_TERMINATOR = 0x5A
AUTH_TERMINATORS = (POWERWAVE_TERMINATOR, 0xFF)


class AdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class CardInfo:
    card_type: int
    page_size: int
    page_count: int
    capacity: int
    format_version: str


class LibUSB:
    def __init__(self) -> None:
        self.lib = ctypes.CDLL("libusb-1.0.so.0")
        self.context = ctypes.c_void_p()
        self.handle = ctypes.c_void_p()

        self.lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.lib.libusb_init.restype = ctypes.c_int
        self.lib.libusb_exit.argtypes = [ctypes.c_void_p]
        self.lib.libusb_open_device_with_vid_pid.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint16,
            ctypes.c_uint16,
        ]
        self.lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
        self.lib.libusb_close.argtypes = [ctypes.c_void_p]
        self.lib.libusb_reset_device.argtypes = [ctypes.c_void_p]
        self.lib.libusb_set_configuration.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.libusb_bulk_transfer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ubyte,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_uint,
        ]

    def open(self, reset_device: bool = True) -> None:
        if self.lib.libusb_init(ctypes.byref(self.context)) != 0:
            raise AdapterError("Could not initialize USB access")
        self.handle = ctypes.c_void_p(
            self.lib.libusb_open_device_with_vid_pid(self.context, VID, PID)
        )
        if not self.handle:
            self.close()
            raise AdapterError("PowerWave adapter not found")
        if reset_device:
            self.lib.libusb_reset_device(self.handle)
        self.lib.libusb_set_configuration(self.handle, 1)
        result = self.lib.libusb_claim_interface(self.handle, 0)
        if result != 0:
            self.close()
            raise AdapterError(f"Could not claim adapter ({result})")

    def close(self) -> None:
        if self.handle:
            self.lib.libusb_release_interface(self.handle, 0)
            self.lib.libusb_close(self.handle)
            self.handle = ctypes.c_void_p()
        if self.context:
            self.lib.libusb_exit(self.context)
            self.context = ctypes.c_void_p()

    def write(self, data: bytes, timeout: int = 5000) -> None:
        padded_size = max(64, (len(data) + 63) & ~63)
        packet = data.ljust(padded_size, b"\0")
        buffer = (ctypes.c_ubyte * len(packet)).from_buffer_copy(packet)
        transferred = ctypes.c_int()
        result = self.lib.libusb_bulk_transfer(
            self.handle,
            ENDPOINT_OUT,
            buffer,
            len(packet),
            ctypes.byref(transferred),
            timeout,
        )
        if result != 0 or transferred.value != len(packet):
            raise AdapterError(f"USB write failed ({result}, {transferred.value})")

    def read(self, size: int = 1024, timeout: int = 5000) -> bytes:
        buffer = (ctypes.c_ubyte * size)()
        transferred = ctypes.c_int()
        result = self.lib.libusb_bulk_transfer(
            self.handle,
            ENDPOINT_IN,
            buffer,
            size,
            ctypes.byref(transferred),
            timeout,
        )
        if result != 0:
            raise AdapterError(f"USB read failed ({result})")
        return bytes(buffer[: transferred.value])


class PowerWaveReader:
    def __init__(self, reset_usb: bool = True) -> None:
        self.usb = LibUSB()
        self.reset_usb = reset_usb
        self._authenticated = False

    def __enter__(self) -> "PowerWaveReader":
        self.usb.open(reset_device=self.reset_usb)
        return self

    def __exit__(self, *_args: object) -> None:
        self.usb.close()

    def _command(self, payload: bytes) -> None:
        self.usb.write(b"\xaa" + payload)

    def _long_command(self, payload: bytes) -> None:
        self._command(b"\x42" + struct.pack("<H", len(payload)) + payload)

    def _response(self) -> bytes:
        response = self.usb.read()
        if not response or response[0] != 0x55:
            raise AdapterError(f"Invalid adapter response: {response.hex(' ')}")
        return response[1:]

    def _long_response(self) -> tuple[int, bytes]:
        response = self._response()
        status = response[0]
        if status != 0x5A:
            return status, b""
        if len(response) < 3:
            raise AdapterError("Truncated adapter response")
        expected = struct.unpack("<H", response[1:3])[0]
        data = bytearray(response[3:])
        while len(data) < expected:
            data.extend(self.usb.read())
        return status, bytes(data[:expected])

    def card_type(self) -> int:
        self._command(b"\x40")
        response = self._response()
        if len(response) != 1:
            raise AdapterError(f"Unexpected card-type response: {response.hex(' ')}")
        return response[0]

    @staticmethod
    def _strip_response(data: bytes, padding: int = 2) -> bytes:
        if len(data) < padding or any(byte != 0xFF for byte in data[:-padding]):
            raise AdapterError(f"Malformed command response: {data.hex(' ')}")
        return data[-padding:]

    def _f0(self, sequence: int) -> bool:
        self._long_command(bytes((0x81, 0xF0, sequence, 0, 0)))
        status, data = self._long_response()
        response = self._strip_response(data)
        return status == 0x5A and response[0] == 0x2B and response[1] in AUTH_TERMINATORS

    def _receive_f0(self, sequence: int, length: int = 9) -> bytes:
        padding = length + 2
        self._long_command(bytes((0x81, 0xF0, sequence)) + bytes(padding))
        status, data = self._long_response()
        if status != 0x5A:
            raise AdapterError(f"Authentication receive {sequence:02x} failed ({status:02x})")
        response = self._strip_response(data, padding)
        if response[0] != 0x2B or response[-1] not in AUTH_TERMINATORS:
            raise AdapterError("Malformed authentication data")
        return response[1:-1]

    def _send_f0(self, sequence: int, data: bytes) -> None:
        if len(data) != 9:
            raise ValueError("Authentication payload must be 9 bytes")
        self._long_command(bytes((0x81, 0xF0, sequence)) + data + b"\0\0")
        status, response = self._long_response()
        response = self._strip_response(response)
        if (
            status != 0x5A
            or response[0] != 0x2B
            or response[1] not in AUTH_TERMINATORS
        ):
            raise AdapterError(f"Authentication send {sequence:02x} failed")

    def _simple_long(
        self,
        payload: bytes,
        expected: bytes = bytes((0x2B, POWERWAVE_TERMINATOR)),
    ) -> None:
        self._long_command(payload)
        status, data = self._long_response()
        response = self._strip_response(data, len(expected))
        matches = len(response) == len(expected) and all(
            actual == wanted
            or (wanted == POWERWAVE_TERMINATOR and actual in AUTH_TERMINATORS)
            for actual, wanted in zip(response, expected)
        )
        if status != 0x5A or not matches:
            raise AdapterError(
                f"Adapter rejected command {payload.hex(' ')} "
                f"(status {status:02x}, data {data.hex(' ')})"
            )

    def is_authenticated(self) -> bool:
        self._long_command(b"\x81\x11\0\0")
        status, data = self._long_response()
        if status == 0xAF:
            return False
        return status == 0x5A and self._strip_response(data) == b"\x2b\x55"

    @staticmethod
    def _decrypt_wire(value: bytes) -> bytes:
        if len(value) != 9:
            raise AdapterError("Invalid authentication value length")
        if functools.reduce(operator.xor, value[:8], 0) != value[8]:
            raise AdapterError("Authentication checksum mismatch")
        return value[:8][::-1]

    @staticmethod
    def _encrypt_wire(value: bytes) -> bytes:
        return value[::-1] + bytes((functools.reduce(operator.xor, value, 0),))

    @staticmethod
    def _triple_des(data: bytes, key: bytes, iv: bytes) -> bytes:
        if len(key) == 16:
            key += key[:8]
        encryptor = Cipher(
            TripleDES(key),
            modes.CBC(iv),
            backend=default_backend(),
        ).encryptor()
        return encryptor.update(data) + encryptor.finalize()

    def authenticate(self) -> None:
        if self._authenticated:
            return
        if self.is_authenticated():
            self._authenticated = True
            return

        self._simple_long(b"\x81\xf3\0\0\0")
        self._simple_long(b"\x81\xf7\x01\0\0")
        if not self._f0(0):
            raise AdapterError("Could not start card authentication")

        card_iv = self._decrypt_wire(self._receive_f0(1))
        card_material = self._decrypt_wire(self._receive_f0(2))
        if not self._f0(3):
            raise AdapterError("Card authentication stopped early")
        card_nonce = self._decrypt_wire(self._receive_f0(4))

        mixed = bytes(a ^ b for a, b in zip(card_iv, card_material))
        unique_key = (
            self._triple_des(mixed, CARD_KEY_1, CARD_IV_1)
            + self._triple_des(mixed, CARD_KEY_2, CARD_IV_2)
        )
        challenge_1 = self._triple_des(MECHA_NONCE, unique_key, CHALLENGE_IV)
        challenge_2 = self._triple_des(card_nonce, unique_key, challenge_1)
        challenge_3 = self._triple_des(card_iv, unique_key, challenge_2)

        if not self._f0(5):
            raise AdapterError("Card authentication timed out")
        self._send_f0(6, self._encrypt_wire(challenge_3))
        self._send_f0(7, self._encrypt_wire(challenge_2))
        for sequence in (8, 9, 10):
            if not self._f0(sequence):
                raise AdapterError(f"Card authentication step {sequence:02x} failed")
        self._send_f0(11, self._encrypt_wire(challenge_1))
        for sequence in (12, 13, 14):
            if not self._f0(sequence):
                raise AdapterError(f"Card authentication step {sequence:02x} failed")
        self._receive_f0(15)
        self._f0(16)
        self._receive_f0(17)
        self._f0(18)
        self._receive_f0(19)
        self._f0(20)

        self._simple_long(
            b"\x81\x28\0\0\0",
            bytes((0x2B, POWERWAVE_TERMINATOR, POWERWAVE_TERMINATOR)),
        )
        self._simple_long(b"\x81\x27\x55\0\0", b"\x2b\x55")
        self._long_command(b"\x81\x26" + bytes(11))
        status, data = self._long_response()
        if status != 0x5A:
            raise AdapterError("Could not read card specifications")
        specs = self._strip_response(data, 11)
        if specs[0] != 0x2B or specs[-1] not in (0x55, *AUTH_TERMINATORS):
            raise AdapterError("Invalid card specifications")

        if not self.is_authenticated():
            raise AdapterError("Card rejected the authentication response")
        self._authenticated = True

    def read_page(self, page_number: int) -> bytes:
        self.authenticate()
        self._command(b"\x52\x03" + struct.pack("<I", page_number) + b"\x55\x2b")
        status, data = self._long_response()
        if status != 0x5A or len(data) != 0x210:
            raise AdapterError(
                f"Page {page_number} read failed ({status:02x}, {len(data)} bytes)"
            )
        return data

    def info(self) -> CardInfo:
        card_type = self.card_type()
        if card_type != 2:
            raise AdapterError("No PS2 memory card is inserted")
        page = self.read_page(0)
        raw = page[:512]
        if raw[:28] != b"Sony PS2 Memory Card Format ":
            raise AdapterError("The inserted card is not PS2-formatted")
        version = raw[28:40].rstrip(b"\0").decode("ascii", "replace")
        page_size = struct.unpack_from("<H", raw, 40)[0]
        clusters = struct.unpack_from("<I", raw, 48)[0]
        return CardInfo(card_type, page_size, clusters * 2, clusters * 1024, version)

    def backup(
        self,
        output: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        info = self.info()
        temporary = output.with_suffix(output.suffix + ".partial")
        with temporary.open("wb") as target:
            for page_number in range(info.page_count):
                target.write(self.read_page(page_number)[: info.page_size])
                if progress and (page_number % 128 == 0 or page_number + 1 == info.page_count):
                    progress(page_number + 1, info.page_count)
        temporary.replace(output)


if __name__ == "__main__":
    with PowerWaveReader() as reader:
        print(f"Card type: {reader.card_type()}")
        print(reader.info())
