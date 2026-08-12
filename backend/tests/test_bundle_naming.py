"""Download filename for the bundle.

Client feedback: every order arrived as `outputs.zip`, which is indistinguishable in a downloads
folder as soon as a second order lands.

One image is named after itself; a batch is not. Naming a four-product order after its first file
produced `AI_Avenafyt 100ml 2.zip` — a name that is confidently wrong, which is worse than a
generic one. A batch is identified by its size and the time the order was placed.

The prefix also carries meaning — it marks the folder as processed output rather than a copy of
the source material, which matters because raw orders now ship the untouched original alongside.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.jobs import _content_disposition, bundle_filename
from app.models import ImageResult, ImageState


def image(name: str) -> ImageResult:
    return ImageResult(source_name=name, state=ImageState.DONE)


class TestBundleFilename:
    def test_one_image_is_still_named_after_its_source(self):
        """Unchanged, and deliberately so: for a single file the source name is exactly right."""
        assert bundle_filename([image("product-42.png")]) == "AI_product-42.zip"

    def test_the_source_extension_is_replaced_not_appended(self):
        """`AI_shot.nef.zip` would suggest the zip contains a nef, which it may not."""
        assert bundle_filename([image("shot.nef")]) == "AI_shot.zip"

    def test_a_batch_is_named_by_size_and_time_not_by_one_of_its_files(self):
        """The bug this replaced: a four-product order downloaded as `AI_Avenafyt 100ml 2.zip`,
        naming one product and silently hiding the other three. A generic name is better than a
        confidently wrong one."""
        names = [image("first.png"), image("second.png"), image("third.png")]
        when = datetime(2026, 8, 12, 14, 43, tzinfo=timezone.utc)

        assert bundle_filename(names, when) == "AI_3-files_2026-08-12_1443.zip"

    def test_no_source_filename_leaks_into_a_batch_name(self):
        names = [image("Avenafyt 100ml 2.psd"), image("GSM 120 Capsules 2.psd")]
        assert "Avenafyt" not in bundle_filename(names, datetime(2026, 8, 12, tzinfo=timezone.utc))

    def test_the_stamp_comes_from_the_job_not_the_packaging_clock(self):
        """Re-downloading an order must give the same filename it gave the first time."""
        names = [image("a.png"), image("b.png")]
        when = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
        assert bundle_filename(names, when) == bundle_filename(names, when)
        assert "2026-01-02_0304" in bundle_filename(names, when)

    def test_spaces_and_brackets_survive(self):
        """Real client files look like this; mangling them makes the output hard to match up."""
        assert bundle_filename([image("NEF_9038 (1) 1.nef")]) == "AI_NEF_9038 (1) 1.zip"

    def test_an_empty_job_still_produces_a_usable_name(self):
        name = bundle_filename([], datetime(2026, 8, 12, 14, 43, tzinfo=timezone.utc))
        assert name.startswith("AI_") and name.endswith(".zip")

    def test_a_dotfile_name_does_not_produce_an_empty_stem(self):
        assert bundle_filename([image(".hidden")]) == "AI_.hidden.zip"

    @pytest.mark.parametrize("hostile", ['a"b.png', "a/b.png", "a\\b.png", "a\r\nb.png", "a:b.png"])
    def test_characters_that_would_break_the_header_are_stripped(self, hostile):
        """This value lands in a Content-Disposition header. A quote or a newline there is header
        injection, not a cosmetic problem, and a separator implies a directory that is not there."""
        name = bundle_filename([image(hostile)])
        for bad in ('"', "/", "\\", "\r", "\n", ":"):
            assert bad not in name
        assert name.startswith("AI_") and name.endswith(".zip")


class TestContentDisposition:
    def test_it_marks_the_response_as_an_attachment(self):
        assert _content_disposition("AI_x.zip").startswith("attachment;")

    def test_it_carries_both_the_plain_and_utf8_forms(self):
        """A bare `filename=` is ASCII-only, so an accented or CJK order name downloads mangled
        without the starred form."""
        header = _content_disposition("AI_café.zip")
        assert 'filename="AI_caf?.zip"' in header
        assert "filename*=UTF-8''AI_caf%C3%A9.zip" in header

    def test_spaces_are_quoted_rather_than_breaking_the_header(self):
        header = _content_disposition("AI_NEF_9038 (1) 1.zip")
        assert 'filename="AI_NEF_9038 (1) 1.zip"' in header
