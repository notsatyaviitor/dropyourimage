"""Zip ingest tests. This is the untrusted-input boundary — see docs/SECURITY.md.

Every guard here stops a specific attack, and each test constructs the actual malicious input
rather than asserting behaviour in the abstract.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.core import errors
from app.core.settings import Settings
from app.ingest import zip_reader


def make_zip(entries: dict[str, bytes], *, names_as_given: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def tiny_png() -> bytes:
    from app.imaging import export as E

    import numpy as np

    from app.models import OutputFormat

    return E.encode(np.zeros((4, 4, 3), dtype="float32"), None, fmt=OutputFormat.PNG)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        max_zip_bytes=10_000_000,
        max_zip_entries=50,
        max_uncompressed_bytes=50_000_000,
        max_image_bytes=5_000_000,
    )


class TestHappyPath:
    def test_reads_ordinary_images(self, settings):
        data = make_zip({"a.png": tiny_png(), "b.png": tiny_png()})
        entries, rejections = zip_reader.read_images(data, settings)
        assert {e.name for e in entries} == {"a.png", "b.png"}
        assert rejections == []

    def test_entries_are_sorted_deterministically(self, settings):
        """The same archive must produce the same order every run."""
        data = make_zip({"z.png": tiny_png(), "a.png": tiny_png(), "m.png": tiny_png()})
        first = [e.name for e in zip_reader.read_images(data, settings)[0]]
        second = [e.name for e in zip_reader.read_images(data, settings)[0]]
        assert first == second == ["a.png", "m.png", "z.png"]

    def test_macos_cruft_is_skipped_quietly(self, settings):
        """A macOS zip always contains these; they must not appear as rejections either."""
        data = make_zip(
            {"a.png": tiny_png(), "__MACOSX/._a.png": b"junk", ".DS_Store": b"junk"}
        )
        entries, rejections = zip_reader.read_images(data, settings)
        assert [e.name for e in entries] == ["a.png"]
        assert rejections == []


class TestPathTraversal:
    def test_parent_directory_traversal_is_rejected(self, settings):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../etc/cron.d/evil", tiny_png())
        entries, rejections = zip_reader.read_images(buf.getvalue(), settings)
        assert entries == []
        assert rejections and "not permitted" in rejections[0].reason

    def test_absolute_path_is_rejected(self, settings):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("/etc/passwd", tiny_png())
        entries, rejections = zip_reader.read_images(buf.getvalue(), settings)
        assert entries == []
        assert rejections

    def test_windows_drive_letter_path_is_rejected(self, settings):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("C:\\Windows\\System32\\evil.png", tiny_png())
        entries, rejections = zip_reader.read_images(buf.getvalue(), settings)
        assert entries == []
        assert rejections

    def test_nul_byte_in_name_is_rejected(self):
        """`_is_unsafe_path` is exercised directly.

        Python's own `zipfile.writestr` truncates a name at an embedded NUL when it *encodes* the
        entry, so round-tripping one through `ZipFile` never reaches `read_images` with the NUL
        intact — there is no way to build a realistic end-to-end fixture for this particular
        guard. The unit check is still worth having in case another zip tool is more permissive.
        """
        assert zip_reader._is_unsafe_path("a.png\x00.exe")

    def test_legitimate_nested_folder_is_fine(self, settings):
        """Traversal detection must not reject ordinary subfolders."""
        data = make_zip({"batch1/photos/a.png": tiny_png()})
        entries, _ = zip_reader.read_images(data, settings)
        assert [e.name for e in entries] == ["a.png"], "flattened to basename, not rejected"


class TestZipBombGuards:
    def test_too_many_entries_is_rejected_as_malicious(self, settings):
        data = make_zip({f"{i}.png": tiny_png() for i in range(settings.max_zip_entries + 5)})
        with pytest.raises(errors.MaliciousArchive, match="more than"):
            zip_reader.read_images(data, settings)

    def test_declared_uncompressed_size_over_the_cap_is_rejected(self, settings):
        """Caught from the zip index alone, before a single byte is decompressed."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("bomb.bin", b"\x00" * (settings.max_uncompressed_bytes + 1))
        with pytest.raises(errors.MaliciousArchive, match="uncompressed size"):
            zip_reader.read_images(buf.getvalue(), settings)

    def test_extreme_compression_ratio_on_one_entry_is_rejected(self, settings):
        """A single entry that is mostly zeroes: it can slip under a *total* cap while still
        being individually implausible."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("suspicious.bin", b"\x00" * 2_000_000)
        entries, rejections = zip_reader.read_images(buf.getvalue(), settings)
        assert entries == []
        assert rejections and "compression ratio" in rejections[0].reason

    def test_oversized_archive_itself_is_rejected(self, settings):
        oversized = b"PK" + b"\x00" * (settings.max_zip_bytes + 1)
        with pytest.raises(errors.FileTooLarge):
            zip_reader.read_images(oversized, settings)

    def test_per_image_size_cap_is_enforced(self, settings):
        from app.models import ErrorCode

        data = make_zip({"huge.png": b"\xff" * (settings.max_image_bytes + 1)})
        entries, rejections = zip_reader.read_images(data, settings)
        assert entries == []
        assert rejections and "per-image limit" in rejections[0].reason

        # The reason names both numbers. "File exceeds the size limit" on its own does not say
        # which limit or by how much, which is exactly how a 130 MB PSD became a mystery.
        assert "5 MB" in rejections[0].reason and "4 MB" in rejections[0].reason

        # And it is FILE_TOO_LARGE, not UNSUPPORTED_FILE. A supported format that is merely too
        # big must not be reported as a format problem — that sends someone converting a file
        # whose format was never the issue.
        assert rejections[0].code is ErrorCode.FILE_TOO_LARGE

    def test_a_forged_central_directory_size_is_neutralised(self, settings):
        """Defence in depth against a crafted zip whose central directory understates an entry's
        real size — the pre-check that sums `file_size` across entries would pass it clean.

        Verified experimentally: forging the size this way also breaks the entry's CRC-32 (the
        decompressed stream no longer matches what the header promised), and Python's own
        `zipfile` raises `BadZipFile` the moment the entry is read — before `read_images`'s own
        length check even runs. That exception is caught by the per-entry try/except and turned
        into a rejection. The entry is excluded and reported either way; which path catches it is
        secondary to that outcome, so this test pins the outcome rather than the mechanism.
        """
        data = bytearray(make_zip({"a.png": tiny_png() + b"\x00" * 1000}))

        # ZIP local file headers and central directory records both store a 4-byte
        # little-endian uncompressed size. Find the central directory's copy (signature
        # PK\x01\x02) and shrink it, so the archive claims to be far smaller than it truly is.
        cd_sig = b"PK\x01\x02"
        idx = data.index(cd_sig)
        size_offset = idx + 24                    # uncompressed size field within the CD record
        data[size_offset : size_offset + 4] = (1).to_bytes(4, "little")

        entries, rejections = zip_reader.read_images(bytes(data), settings)
        assert entries == [], "the tampered entry must never be treated as usable"
        assert rejections and "could not be read" in rejections[0].reason


class TestMimeSniffing:
    def test_extension_is_never_trusted(self, settings):
        """A .png that is actually plain text must be rejected, not decoded."""
        data = make_zip({"fake.png": b"this is not an image, just text pretending"})
        entries, rejections = zip_reader.read_images(data, settings)
        assert entries == []
        assert rejections and "Not an image" in rejections[0].reason

    def test_real_image_with_the_wrong_extension_is_still_accepted(self, settings):
        """Sniffing is content-based both ways: a correctly-encoded PNG named .txt still counts."""
        data = make_zip({"photo.txt": tiny_png()})
        entries, _ = zip_reader.read_images(data, settings)
        assert len(entries) == 1

    def test_svg_is_explicitly_rejected_with_a_specific_reason(self, settings):
        svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'
        data = make_zip({"vector.svg": svg})
        entries, rejections = zip_reader.read_images(data, settings)
        assert entries == []
        assert "vector" in rejections[0].reason.lower()

    def test_psd_source_is_not_an_allowed_input_format(self, settings):
        """PSDs are an OUTPUT of this pipeline, not a supported input."""
        data = make_zip({"layered.psd": b"8BPS" + b"\x00" * 20})
        entries, rejections = zip_reader.read_images(data, settings)
        assert entries == []
        assert rejections


class TestPartialFailureIsolation:
    def test_one_bad_entry_does_not_block_the_good_ones(self, settings):
        """The whole point of returning rejections rather than raising per-entry."""
        data = make_zip({"good.png": tiny_png(), "bad.txt": b"not an image"})
        entries, rejections = zip_reader.read_images(data, settings)
        assert [e.name for e in entries] == ["good.png"]
        assert len(rejections) == 1


class TestEmptyAndInvalidArchives:
    def test_empty_zip_is_rejected(self, settings):
        buf = io.BytesIO()
        zipfile.ZipFile(buf, "w").close()
        with pytest.raises(errors.MaliciousArchive, match="no files"):
            zip_reader.read_images(buf.getvalue(), settings)

    def test_not_a_zip_at_all(self, settings):
        with pytest.raises(errors.MaliciousArchive, match="not a valid zip"):
            zip_reader.read_images(b"this is not a zip file", settings)

    def test_archive_of_only_junk_files_reports_zero_entries_not_an_exception(self, settings):
        data = make_zip({".DS_Store": b"x", "__MACOSX/._x": b"x"})
        with pytest.raises(errors.MaliciousArchive, match="no files"):
            zip_reader.read_images(data, settings)
