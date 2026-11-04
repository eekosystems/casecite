"""
Unit tests for the TextExtraction service.
"""

import os

os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only-32chars!"
os.environ["ENCRYPTION_SALT"] = "test-salt-16chars!"
os.environ["DEBUG"] = "true"

from unittest.mock import patch

import pytest
from app.services.text_extraction import (
    ExtractionResult,
    TextExtractionService,
    text_extraction_service,
)


@pytest.fixture
def service():
    """Create a fresh TextExtractionService."""
    return TextExtractionService()


# =============================================================================
# ExtractionResult Tests
# =============================================================================


class TestExtractionResult:
    """Tests for ExtractionResult dataclass."""

    def test_default_values(self):
        result = ExtractionResult()
        assert result.text == ""
        assert result.tables == []
        assert result.page_count == 0
        assert result.ocr_used is False
        assert result.char_count == 0
        assert result.word_count == 0

    def test_custom_values(self):
        result = ExtractionResult(
            text="Hello world",
            tables=["| A | B |"],
            page_count=5,
            ocr_used=True,
            char_count=11,
            word_count=2,
        )
        assert result.text == "Hello world"
        assert len(result.tables) == 1
        assert result.page_count == 5
        assert result.ocr_used is True


# =============================================================================
# Format Detection Tests
# =============================================================================


class TestDetectFormat:
    """Tests for _detect_format."""

    def test_detect_pdf_by_content_type(self, service):
        fmt = service._detect_format(b"", "application/pdf", "")
        assert fmt == "pdf"

    def test_detect_pdf_by_filename(self, service):
        fmt = service._detect_format(b"", "", "document.pdf")
        assert fmt == "pdf"

    def test_detect_pdf_by_magic_bytes(self, service):
        fmt = service._detect_format(b"%PDF-1.7", "", "")
        assert fmt == "pdf"

    def test_detect_docx_by_content_type(self, service):
        fmt = service._detect_format(
            b"",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "",
        )
        assert fmt == "docx"

    def test_detect_docx_by_filename(self, service):
        fmt = service._detect_format(b"", "", "contract.docx")
        assert fmt == "docx"

    def test_detect_docx_by_magic_bytes(self, service):
        fmt = service._detect_format(b"PK\x03\x04", "", "")
        assert fmt == "docx"

    def test_detect_txt_by_content_type(self, service):
        fmt = service._detect_format(b"", "text/plain", "")
        assert fmt == "txt"

    def test_detect_txt_by_filename(self, service):
        fmt = service._detect_format(b"", "", "notes.txt")
        assert fmt == "txt"

    def test_detect_rtf(self, service):
        # RTF gets its own converter now (striprtf) instead of raw-text decode.
        fmt = service._detect_format(b"", "", "document.rtf")
        assert fmt == "rtf"

    def test_detect_legacy_doc(self, service):
        # Legacy Word binaries have their own extractor; python-docx cannot read them.
        assert service._detect_format(b"", "", "old_document.doc") == "doc"
        assert service._detect_format(b"", "application/msword", "") == "doc"
        ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 8
        assert service._detect_format(ole, "", "no_extension") == "doc"

    def test_unknown_defaults_to_txt(self, service):
        fmt = service._detect_format(b"\x00\x01\x02", "", "unknown.xyz")
        assert fmt == "txt"

    def test_case_insensitive_filename(self, service):
        fmt = service._detect_format(b"", "", "Contract.PDF")
        assert fmt == "pdf"


# =============================================================================
# TXT Extraction Tests
# =============================================================================


class TestExtractTxt:
    """Tests for _extract_txt."""

    def test_utf8_text(self, service):
        content = b"Hello, World!"
        result = service._extract_txt(content)
        assert result.text == "Hello, World!"

    def test_latin1_text(self, service):
        # Use a string with multiple latin-1 chars that break utf-8 decoding
        text = "R\xe9sum\xe9 of the caf\xe9"
        content = text.encode("latin-1")
        result = service._extract_txt(content)
        assert result.text == text

    def test_cp1252_punctuation_is_decoded(self, service):
        # Curly quotes and dashes from Windows-authored files live in the
        # cp1252-only range; they must not come out as control characters.
        text = "The \u201cTenant\u201d \u2014 pays \u20ac500"
        result = service._extract_txt(text.encode("cp1252"))
        assert result.text == text

    def test_non_utf8_without_bom_is_not_read_as_utf16(self, service):
        # Even-length cp1252 bytes "decode" as UTF-16 into CJK garbage.
        content = "caf\xe9 au lait".encode("cp1252")
        assert len(content) % 2 == 0
        assert service._extract_txt(content).text == "caf\xe9 au lait"

    def test_utf8_bom_is_stripped(self, service):
        assert service._extract_txt(b"\xef\xbb\xbfHello").text == "Hello"

    def test_empty_content(self, service):
        result = service._extract_txt(b"")
        assert result.text == ""

    def test_utf16_text(self, service):
        content = "Unicode text".encode("utf-16")
        result = service._extract_txt(content)
        assert "Unicode" in result.text

    def test_binary_garbage_falls_back(self, service):
        # Random bytes that can't be decoded cleanly
        content = bytes(range(128, 256))
        result = service._extract_txt(content)
        # Should not raise, falls back to latin-1 or errors='ignore'
        assert isinstance(result.text, str)


# =============================================================================
# Full Extract Method Tests
# =============================================================================


class TestExtract:
    """Tests for the main extract method."""

    def test_extract_txt_from_content(self, service):
        content = b"This is a test document with some text."
        result = service.extract(file_content=content, filename="test.txt")

        assert "test document" in result.text
        assert result.char_count > 0
        assert result.word_count > 0

    def test_extract_with_no_content_and_no_path(self, service):
        result = service.extract()
        assert result.text == ""
        assert result.char_count == 0
        assert result.word_count == 0

    def test_extract_counts_words_correctly(self, service):
        content = b"one two three four five"
        result = service.extract(file_content=content, filename="test.txt")
        assert result.word_count == 5

    def test_extract_empty_text_word_count_zero(self, service):
        content = b"   "
        result = service.extract(file_content=content, filename="test.txt")
        assert result.word_count == 0

    def test_extract_from_file_path(self, service, tmp_path):
        file = tmp_path / "test.txt"
        file.write_bytes(b"File content from disk.")

        result = service.extract(file_path=str(file))
        assert "File content from disk." in result.text

    def test_extract_pdf_without_libraries(self, service):
        with (
            patch("app.services.text_extraction.PDFPLUMBER_AVAILABLE", False),
            patch("app.services.text_extraction.PYPDF_AVAILABLE", False),
        ):
            result = service._extract_pdf(b"%PDF-1.7 fake pdf content")
            assert result.text == ""
            assert "not installed" in result.error


# =============================================================================
# Rows to Markdown Tests
# =============================================================================


class TestRowsToMarkdown:
    """Tests for _rows_to_markdown."""

    def test_basic_table(self, service):
        rows = [
            ["Name", "Age", "Role"],
            ["Alice", "30", "Attorney"],
            ["Bob", "45", "Partner"],
        ]
        md = service._rows_to_markdown(rows)
        assert "| Name | Age | Role |" in md
        assert "| --- | --- | --- |" in md
        assert "| Alice | 30 | Attorney |" in md

    def test_empty_rows(self, service):
        md = service._rows_to_markdown([])
        assert md == ""

    def test_none_cells_converted_to_empty(self, service):
        rows = [
            ["Header", None],
            [None, "Data"],
        ]
        md = service._rows_to_markdown(rows)
        assert "| Header |  |" in md

    def test_uneven_rows_padded(self, service):
        rows = [
            ["A", "B", "C"],
            ["1"],
        ]
        md = service._rows_to_markdown(rows)
        # Row should be padded to 3 columns
        lines = md.split("\n")
        assert len(lines) == 3  # header + separator + data

    def test_newlines_in_cells_removed(self, service):
        rows = [
            ["Header"],
            ["Line1\nLine2"],
        ]
        md = service._rows_to_markdown(rows)
        assert "\n" not in md.split("\n")[2]  # data row should not have embedded newline


# =============================================================================
# Singleton Tests
# =============================================================================


class TestSingleton:
    """Tests for the module-level singleton."""

    def test_singleton_exists(self):
        assert text_extraction_service is not None
        assert isinstance(text_extraction_service, TextExtractionService)

    def test_ocr_threshold_default(self):
        assert TextExtractionService.OCR_THRESHOLD == 50


# =============================================================================
# Untrusted-input handling
# =============================================================================


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


class TestUntrustedInput:
    """A bad upload yields a failed document with a reason, never an exception."""

    def test_corrupt_docx_reports_a_reason(self, service):
        result = service.extract(file_content=b"PK\x03\x04 not really a zip", filename="a.docx")
        assert result.text == ""
        assert "not a valid Office document" in result.error

    def test_zip_valid_but_not_docx_reports_a_reason(self, service):
        # python-docx raises its own exception types here, not ValueError/OSError.
        content = _zip_bytes({"hello.txt": b"hi"})
        result = service.extract(file_content=content, filename="a.docx")
        assert result.text == ""
        assert "could not be read as DOCX" in result.error

    def test_corrupt_pdf_reports_a_reason(self, service):
        result = service.extract(file_content=b"%PDF-1.4 truncated", filename="a.pdf")
        assert result.text == ""
        assert "could not be read" in result.error

    def test_corrupt_xlsx_and_pptx_report_a_reason(self, service):
        for name in ("a.xlsx", "a.pptx"):
            result = service.extract(file_content=_zip_bytes({"x": b"y"}), filename=name)
            assert result.text == ""
            assert result.error

    def test_decompression_bomb_is_rejected_before_parsing(self, service):
        # 2 MB of zeros compresses to ~2 KB; with a 1 MB limit it must be refused.
        content = _zip_bytes({"word/document.xml": b"\x00" * (2 * 1024 * 1024)})
        assert len(content) < 16 * 1024
        with (
            patch("app.services.text_extraction._MAX_UNCOMPRESSED_BYTES", 1024 * 1024),
            patch("app.services.text_extraction.DocxDocument") as parser,
        ):
            result = service.extract(file_content=content, filename="bomb.docx")
        parser.assert_not_called()
        assert "when decompressed" in result.error

    def test_too_many_zip_members_is_rejected(self, service):
        content = _zip_bytes({f"part{i}.xml": b"x" for i in range(20)})
        with patch("app.services.text_extraction._MAX_ZIP_MEMBERS", 10):
            result = service.extract(file_content=content, filename="many.odt")
        assert "too many internal parts" in result.error

    def test_binary_file_of_unknown_type_is_not_indexed_as_text(self, service):
        result = service.extract(file_content=b"\x00\x01\x02\x03" * 64, filename="blob.bin")
        assert result.text == ""
        assert "not supported" in result.error

    def test_empty_text_file_has_a_reason(self, service):
        result = service.extract(file_content=b"   ", filename="empty.txt")
        assert result.error == "The file contains no extractable text."

    def test_scanned_pdf_without_ocr_says_so(self, service):
        with (
            patch("app.services.text_extraction.OCR_AVAILABLE", False),
            patch.object(service, "_extract_pdf", return_value=ExtractionResult(page_count=3)),
        ):
            result = service.extract(file_content=b"%PDF-1.7", filename="scan.pdf")
        assert "scanned" in result.error
        assert "OCR is not enabled" in result.error

    def test_pdf_page_cap_is_reported_not_silent(self, service):
        pypdf = pytest.importorskip("pypdf")
        import io

        writer = pypdf.PdfWriter()
        for _ in range(4):
            writer.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        writer.write(buf)
        with patch("app.services.text_extraction._MAX_PDF_PAGES", 2):
            result = service._extract_pdf_pypdf(buf.getvalue())
        assert result.page_count == 4
        assert result.truncated is True
        assert "first 2 of 4 pages" in result.warnings[0]

    @pytest.mark.asyncio
    async def test_extract_async_runs_off_the_event_loop(self, service):
        import threading

        seen: list[int] = []
        real = service.extract

        def spy(**kwargs):
            seen.append(threading.get_ident())
            return real(**kwargs)

        with patch.object(service, "extract", side_effect=spy):
            result = await service.extract_async(file_content=b"hello", filename="a.txt")
        assert result.text == "hello"
        assert seen and seen[0] != threading.get_ident()


class TestLegacyDoc:
    """Word 97-2003 binary: the piece table is read directly."""

    @staticmethod
    def _clx(pieces: list[tuple[int, int, bool]], cps: list[int]) -> bytes:
        """Build a Clx with one leading Prc and a Pcdt of (offset, _, compressed) pieces."""
        plc = b"".join(cp.to_bytes(4, "little") for cp in cps)
        for offset, _unused, compressed in pieces:
            fc = (offset * 2) | 0x40000000 if compressed else offset
            plc += b"\x00\x00" + fc.to_bytes(4, "little") + b"\x00\x00"
        prc = b"\x01" + (2).to_bytes(2, "little") + b"\xaa\xbb"
        return prc + b"\x02" + len(plc).to_bytes(4, "little") + plc

    def test_piece_table_mixes_cp1252_and_utf16_runs(self):
        from app.services.text_extraction import _read_doc_piece_table

        ansi = "Rent is due \u2014 ".encode("cp1252")
        wide = "\u5951\u7d04 applies.\r".encode("utf-16-le")
        word = b"\x00" * 16 + ansi + wide
        n_ansi, n_wide = len(ansi), len(wide) // 2
        clx = self._clx(
            [(16, 0, True), (16 + len(ansi), 0, False)],
            [0, n_ansi, n_ansi + n_wide],
        )
        text = _read_doc_piece_table(word, clx, n_ansi + n_wide)
        assert text == "Rent is due \u2014 \u5951\u7d04 applies.\r"

    def test_piece_table_stops_at_main_document_text(self):
        from app.services.text_extraction import _read_doc_piece_table

        body = b"Main text.FOOTNOTE"
        clx = self._clx([(0, 0, True)], [0, len(body)])
        assert _read_doc_piece_table(body, clx, 10) == "Main text."

    def test_malformed_piece_table_is_a_clean_error(self):
        from app.services.text_extraction import ExtractionError, _read_doc_piece_table

        with pytest.raises(ExtractionError):
            _read_doc_piece_table(b"", b"\x05garbage", 0)

    def test_control_characters_become_plain_text(self):
        from app.services.text_extraction import _clean_doc_text

        raw = (
            'Title\rSee \x13 HYPERLINK "https://example.com" \x14the terms\x15 here.\r'
            "Party\x07Role\x07\x07Acme\x07Landlord\x07\x07"
        )
        cleaned = _clean_doc_text(raw)
        assert cleaned.splitlines()[:2] == ["Title", "See the terms here."]
        assert "HYPERLINK" not in cleaned
        assert "Party\tRole" in cleaned and "Acme\tLandlord" in cleaned

    def test_nested_field_instructions_are_dropped(self):
        from app.services.text_extraction import _clean_doc_text

        raw = "A \x13 IF \x13 PAGE \x142\x15 = 2 \x14shown\x15 Z"
        assert _clean_doc_text(raw) == "A shown Z"

    def test_non_word_file_named_doc_reports_a_reason(self, service):
        result = service.extract(file_content=b"just some bytes", filename="letter.doc")
        assert result.text == ""
        assert result.error

    def test_docx_saved_with_doc_extension_is_still_read(self, service):
        docx = pytest.importorskip("docx")
        import io

        document = docx.Document()
        document.add_paragraph("Saved with the wrong extension.")
        buf = io.BytesIO()
        document.save(buf)
        result = service.extract(file_content=buf.getvalue(), filename="letter.doc")
        assert "wrong extension" in result.text


class TestOcrByDefault:
    """Scanned PDFs are OCR'd with the bundled rasterizer; when they cannot
    be, the document says so in a field, not only in prose."""

    @staticmethod
    def _image_only_pdf() -> bytes:
        import io

        from PIL import Image, ImageDraw

        page = Image.new("RGB", (850, 1100), "white")
        ImageDraw.Draw(page).text(
            (80, 80), "INDEMNIFICATION. Vendor shall indemnify.", fill="black"
        )
        buf = io.BytesIO()
        page.save(buf, format="PDF")
        return buf.getvalue()

    def test_status_names_what_is_available(self):
        from app.services.text_extraction import OCR_STATUS

        assert set(OCR_STATUS) == {"available", "rasterizer", "tesseract", "reason"}
        if OCR_STATUS["available"]:
            assert OCR_STATUS["rasterizer"] in ("pypdfium2", "pymupdf")
            assert OCR_STATUS["tesseract"]
        else:
            assert OCR_STATUS["reason"]

    def test_pypdfium2_renders_a_scanned_page_for_tesseract(self):
        pytest.importorskip("pypdfium2")
        from app.services import text_extraction as te

        seen = {}

        def fake_tesseract(img):
            seen["size"] = img.size
            seen["mode"] = img.mode
            return "INDEMNIFICATION. Vendor shall indemnify."

        with (
            patch.object(te, "OCR_AVAILABLE", True),
            patch.object(te, "fitz", None),
            patch.object(te.pytesseract, "image_to_string", side_effect=fake_tesseract),
        ):
            session = te._OcrSession(self._image_only_pdf())
            text = session.improve(0, "")
            session.close()
        assert "indemnify" in text
        assert session.used is True
        # Rendered at 300 DPI from a Letter-sized page, as RGB.
        assert seen["mode"] == "RGB"
        assert seen["size"][0] > 2000 and seen["size"][1] > seen["size"][0]

    def test_a_page_past_the_end_yields_nothing(self):
        pytest.importorskip("pypdfium2")
        from app.services import text_extraction as te

        with patch.object(te, "OCR_AVAILABLE", True), patch.object(te, "fitz", None):
            session = te._OcrSession(self._image_only_pdf())
            assert session.improve(5, "kept") == "kept"
            session.close()

    def test_image_only_pdf_without_ocr_is_marked_needs_ocr(self, service):
        with (
            patch("app.services.text_extraction.OCR_AVAILABLE", False),
            patch.object(service, "_extract_pdf", return_value=ExtractionResult(page_count=2)),
        ):
            result = service.extract(file_content=b"%PDF-1.7", filename="scan.pdf")
        assert result.needs_ocr is True
        assert "OCR is not enabled on this server (" in result.error

    def test_a_text_pdf_is_never_marked_needs_ocr(self, service):
        with (
            patch("app.services.text_extraction.OCR_AVAILABLE", False),
            patch.object(
                service, "_extract_pdf", return_value=ExtractionResult(text="Hello", page_count=1)
            ),
        ):
            result = service.extract(file_content=b"%PDF-1.7", filename="text.pdf")
        assert result.needs_ocr is False and result.error == ""
