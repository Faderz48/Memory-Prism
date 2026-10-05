import tempfile
import unittest
from pathlib import Path

from reader import AdapterError, CardInfo, PowerWaveReader, page_ecc


class FakeUSB:
    def __init__(self) -> None:
        self.writes = []

    def write(self, data, timeout=5000, pad=True):
        self.writes.append((data, timeout, pad))

    def read(self, size=1024, timeout=5000):
        return b"\x55\x5a"


class FakeWriter(PowerWaveReader):
    def __init__(self, image: bytes) -> None:
        self.image = bytearray(image)
        self.writes: list[int] = []

    def info(self) -> CardInfo:
        return CardInfo(2, 512, len(self.image) // 512, len(self.image), "1.2.0.0")

    def read_page(self, page_number: int) -> bytes:
        offset = page_number * 512
        data = bytes(self.image[offset : offset + 512])
        return data + page_ecc(data)

    def write_page(self, page_number: int, data: bytes, verify: bool = True) -> None:
        offset = page_number * 512
        self.image[offset : offset + 512] = data[:512]
        self.writes.append(page_number)


class CardWriterTests(unittest.TestCase):
    def test_page_write_uses_exact_unpadded_packet(self) -> None:
        reader = PowerWaveReader()
        reader.usb = FakeUSB()
        reader._authenticated = True

        reader.write_page(7, bytes(512), verify=False)

        packet, _timeout, pad = reader.usb.writes[0]
        self.assertEqual(len(packet), 537)
        self.assertEqual(packet[:7], b"\xaa\x57\x03\x07\0\0\0")
        self.assertEqual(packet[-2:], b"\x55\x2b")
        self.assertFalse(pad)

    def test_erased_page_ecc(self) -> None:
        expected = bytes.fromhex("77 7f 7f" * 4) + bytes(4)
        self.assertEqual(page_ecc(bytes([0xFF]) * 512), expected)

    def test_only_changed_pages_are_written(self) -> None:
        original = bytes(range(256)) * 8
        modified = bytearray(original)
        modified[512:1024] = b"A" * 512
        modified[1536:2048] = b"B" * 512

        with tempfile.TemporaryDirectory() as directory:
            before = Path(directory) / "before.ps2"
            after = Path(directory) / "after.ps2"
            before.write_bytes(original)
            after.write_bytes(modified)
            writer = FakeWriter(original)

            count = writer.write_image_changes(before, after)

        self.assertEqual(count, 2)
        self.assertEqual(writer.writes, [3, 1])
        self.assertEqual(bytes(writer.image), bytes(modified))

    def test_changed_card_is_rejected_before_writing(self) -> None:
        original = bytes(2048)
        modified = bytearray(original)
        modified[512:1024] = b"A" * 512
        inserted = bytearray(original)
        inserted[0] = 1

        with tempfile.TemporaryDirectory() as directory:
            before = Path(directory) / "before.ps2"
            after = Path(directory) / "after.ps2"
            before.write_bytes(original)
            after.write_bytes(modified)
            writer = FakeWriter(inserted)

            with self.assertRaisesRegex(AdapterError, "changed after"):
                writer.write_image_changes(before, after)

        self.assertEqual(writer.writes, [])


if __name__ == "__main__":
    unittest.main()
