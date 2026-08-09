"""Getting text out of an uploaded file.

Real files on disk rather than mocks: the thing being tested is the behaviour
of pypdf, python-docx and BeautifulSoup on the shapes a regulator's website
actually serves, and a mocked extractor would assert only that this module
calls the function it calls.
"""
from __future__ import annotations

import pytest

from app.services import extraction


@pytest.fixture
def files(tmp_path):
    return tmp_path


def _write_pdf(path, pages: list[str]):
    """A PDF with a real text layer.

    Hand-built rather than produced by a rendering library: nothing in the
    dependency set can author a content stream, and the point of these tests is
    that the extractor finds text in a genuine PDF text layer rather than that
    a mock returned a string. A page whose text is empty is a valid PDF with no
    text layer, which is what a scan looks like.
    """
    path.write_bytes(_minimal_pdf(pages))
    return path


def _minimal_pdf(pages: list[str]) -> bytes:
    """A structurally valid PDF, xref table included.

    The xref is not optional decoration — pypdf refuses a file without one, so
    the byte offset of every object has to be tracked as the body is built.
    """
    header = b"%PDF-1.4\n"
    # Object 1 is the catalogue and 2 the page tree; each page then takes two
    # objects, the page itself and its content stream.
    page_ids = [3 + 2 * i for i in range(len(pages))]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)

    bodies: dict[int, bytes] = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: f"<</Type/Pages/Kids[{kids}]/Count {len(pages)}>>".encode("latin-1"),
    }

    for page_id, text in zip(page_ids, pages, strict=True):
        content_id = page_id + 1
        # An empty string draws nothing, leaving a page with no text layer.
        stream = (
            f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1") if text else b""
        )
        bodies[page_id] = (
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
            f"/Resources<</Font<</F1<</Type/Font/Subtype/Type1"
            f"/BaseFont/Helvetica>>>>>>/Contents {content_id} 0 R>>"
        ).encode("latin-1")
        bodies[content_id] = (
            f"<</Length {len(stream)}>>stream\n".encode("latin-1")
            + stream
            + b"\nendstream"
        )

    out = bytearray(header)
    offsets: dict[int, int] = {}
    for obj_id in sorted(bodies):
        offsets[obj_id] = len(out)
        out += f"{obj_id} 0 obj".encode("latin-1") + bodies[obj_id] + b"endobj\n"

    xref_at = len(out)
    count = max(bodies) + 1
    out += f"xref\n0 {count}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for obj_id in range(1, count):
        out += f"{offsets[obj_id]:010d} 00000 n \n".encode("latin-1")

    out += f"trailer<</Size {count}/Root 1 0 R>>\nstartxref\n{xref_at}\n%%EOF".encode(
        "latin-1"
    )
    return bytes(out)


class TestNormalise:
    def test_runs_of_spaces_collapse(self):
        assert extraction.normalise("a     b") == "a b"

    def test_trailing_whitespace_per_line_goes(self):
        assert extraction.normalise("a   \n   b") == "a\nb"

    def test_three_or_more_blank_lines_become_one_gap(self):
        assert extraction.normalise("a\n\n\n\n\nb") == "a\n\nb"

    def test_a_single_blank_line_is_preserved(self):
        """Paragraph structure is meaning in a statutory text."""
        assert extraction.normalise("a\n\nb") == "a\n\nb"

    def test_windows_line_endings_are_normalised(self):
        assert extraction.normalise("a\r\nb") == "a\nb"

    def test_non_breaking_spaces_collapse_too(self):
        """PDF extraction produces these constantly."""
        assert extraction.normalise("a\xa0\xa0b") == "a b"


class TestClipping:
    def test_short_text_is_untouched(self):
        assert extraction.clip_for_model("short") == "short"

    def test_long_text_is_cut_and_says_so(self):
        """Without the marker a model answers as confidently about half a
        circular as about the whole."""
        clipped = extraction.clip_for_model("x" * 500, limit=100)

        assert clipped.startswith("x" * 100)
        assert "TRUNCATED" in clipped
        assert "400 more characters" in clipped


class TestTextFiles:
    def test_a_plain_text_file_reads(self, files):
        path = files / "notice.txt"
        path.write_text("Notice under Section 73 of the CGST Act")

        text, method = extraction.extract_text(path)

        assert "Section 73" in text
        assert method == "text"

    def test_html_tags_are_stripped(self, files):
        path = files / "circular.html"
        path.write_text("<html><body><h1>Circular 210</h1><p>Applies to all.</p></body></html>")

        text, method = extraction.extract_text(path)

        assert "Circular 210" in text
        assert "Applies to all." in text
        assert "<h1>" not in text
        assert method == "html"

    def test_script_and_style_content_is_discarded(self, files):
        """A regulator's page carries navigation and analytics; neither is the
        circular, and both would be handed to the model as if they were."""
        path = files / "page.html"
        path.write_text(
            "<html><body><script>var x=1;</script>"
            "<style>.a{color:red}</style>"
            "<nav>Home | About</nav>"
            "<p>The operative text.</p></body></html>"
        )

        text, _ = extraction.extract_text(path)

        assert "The operative text." in text
        assert "var x" not in text
        assert "color:red" not in text
        assert "About" not in text


class TestDocx:
    def test_paragraphs_are_read(self, files):
        import docx

        path = files / "filing.docx"
        document = docx.Document()
        document.add_paragraph("Form MGT-7 for FY2025-26")
        document.save(str(path))

        text, method = extraction.extract_text(path)

        assert "MGT-7" in text
        assert method == "docx"

    def test_table_contents_are_read_too(self, files):
        """python-docx omits tables from ``paragraphs``, and an annexure's
        figures are exactly what the extraction exists to find."""
        import docx

        path = files / "annexure.docx"
        document = docx.Document()
        document.add_paragraph("Annexure A")
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "Tax payable"
        table.cell(0, 1).text = "125000"
        document.save(str(path))

        text, _ = extraction.extract_text(path)

        assert "Tax payable" in text
        assert "125000" in text


class TestPdf:
    def test_a_pdf_with_a_text_layer_reads(self, files):
        path = _write_pdf(
            files / "notice.pdf",
            ["Notice under Section 73 of the CGST Act 2017 issued to the taxpayer"],
        )

        text, method = extraction.extract_text(path)

        assert "Section 73" in text
        assert method == "pdf_text"

    def test_every_page_is_read(self, files):
        path = _write_pdf(
            files / "multi.pdf",
            [
                "First page carries the demand and the assessment reference",
                "Second page carries the annexure and the computation",
            ],
        )

        text, _ = extraction.extract_text(path)

        assert "First page" in text
        assert "Second page" in text

    def test_a_page_of_furniture_alone_counts_as_a_scan(self, files):
        """A header and a page number are not a text layer.

        A scan run through a tool that stamped page numbers on it would
        otherwise be treated as a readable document and handed to the model as
        a two-word notice.
        """
        path = _write_pdf(files / "stamped.pdf", ["Page 1"])

        with pytest.raises(extraction.ScannedDocument):
            extraction.extract_text(path)

    def test_a_pdf_with_no_text_layer_is_reported_as_a_scan(self, files):
        """Not returned as an empty string, which reads as an empty notice."""
        path = _write_pdf(files / "scan.pdf", [""])

        with pytest.raises(extraction.ScannedDocument):
            extraction.extract_text(path)


class TestDispatch:
    def test_a_missing_file_is_an_extraction_error(self, files):
        with pytest.raises(extraction.ExtractionError):
            extraction.extract_text(files / "nope.txt")

    def test_an_unknown_extension_is_refused(self, files):
        path = files / "archive.zip"
        path.write_bytes(b"PK\x03\x04")

        with pytest.raises(extraction.ExtractionError):
            extraction.extract_text(path)

    def test_the_mime_type_is_a_fallback_when_there_is_no_extension(self, files):
        path = files / "download"
        path.write_text("Notice text")

        text, method = extraction.extract_text(path, mime_type="text/plain")

        assert "Notice text" in text
        assert method == "text"

    def test_a_charset_suffix_on_the_mime_type_is_tolerated(self, files):
        path = files / "download2"
        path.write_text("Notice text")

        text, _ = extraction.extract_text(path, mime_type="text/plain; charset=utf-8")

        assert "Notice text" in text

    def test_the_extension_wins_over_a_wrong_mime_type(self, files):
        """The extension describes the bytes stored; the MIME type is whatever
        the client claimed."""
        path = files / "real.txt"
        path.write_text("Plain text after all")

        text, method = extraction.extract_text(path, mime_type="application/pdf")

        assert method == "text"
        assert "Plain text" in text

    def test_a_corrupt_file_raises_extraction_error_not_a_crash(self, files):
        path = files / "broken.pdf"
        path.write_bytes(b"not a pdf at all")

        with pytest.raises(extraction.ExtractionError):
            extraction.extract_text(path)
