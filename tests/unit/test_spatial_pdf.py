"""Tests for deterministic embedded-text PDF table reconstruction."""

from __future__ import annotations

from typing import Any, cast

import pymupdf
import pytest

import cashflow_ai.imports.spatial_pdf as spatial_pdf_module
from cashflow_ai.imports.spatial_pdf import (
    MappedSpatialRecord,
    SpatialColumnMapping,
    SpatialColumnRole,
    SpatialPdfCell,
    SpatialPdfColumn,
    SpatialPdfError,
    SpatialPdfErrorCode,
    SpatialPdfRecord,
    SpatialPdfState,
    SpatialPdfWord,
    _accessibility_label_bundle,
    _header_from_line,
    _mapping_value,
    _VisualLine,
    reconstruct_spatial_pdf,
)


def _document() -> Any:
    return pymupdf.open()  # type: ignore[no-untyped-call]


def _finish(document: Any, **options: Any) -> bytes:
    content = cast(bytes, document.tobytes(**options))
    document.close()
    return content


def _write_cells(
    page: Any,
    y: float,
    values: tuple[str, ...],
    positions: tuple[float, ...],
) -> None:
    for value, x in zip(values, positions, strict=True):
        if value:
            page.insert_text((x, y), value, fontsize=9)


def _signed_statement_pdf(*, move_second_header: bool = False) -> bytes:
    document = _document()
    positions = (35.0, 125.0, 345.0, 465.0)
    first = document.new_page(width=595, height=842)
    first.insert_text((35, 45), "Fictional statement", fontsize=10)
    _write_cells(first, 100, ("Date", "Description", "Amount", "Balance"), positions)
    _write_cells(
        first,
        125,
        ("01/08/2026", "SYNTHETIC SHOP", "-10.00", "490.00"),
        positions,
    )
    _write_cells(first, 140, ("", "SECOND LINE", "", ""), positions)
    _write_cells(first, 300, ("", "FICTIONAL FOOTER NOTE", "", ""), positions)
    first.insert_text((250, 820), "Page 1 of 2", fontsize=8)

    second = document.new_page(width=595, height=842)
    second_positions = (
        positions if not move_second_header else (35.0, 260.0, 390.0, 500.0)
    )
    _write_cells(
        second,
        80,
        ("Date", "Description", "Amount", "Balance"),
        second_positions,
    )
    _write_cells(
        second,
        105,
        ("02/08/2026", "SYNTHETIC REFUND", "+5.00", "495.00"),
        second_positions,
    )
    second.insert_text((35, 790), "Closing balance GBP 495.00", fontsize=9)
    return _finish(document)


def _debit_credit_pdf(*, unresolved: bool = False) -> bytes:
    document = _document()
    page = document.new_page(width=595, height=842)
    positions = (30.0, 115.0, 315.0, 390.0, 475.0)
    _write_cells(
        page,
        90,
        ("Date", "Details", "Money out", "Money in", "Balance"),
        positions,
    )
    _write_cells(
        page,
        115,
        ("03/08/2026", "SYNTHETIC RENT", "25.00", "", "975.00"),
        positions,
    )
    _write_cells(
        page,
        140,
        ("04 Aug 2026", "SYNTHETIC PAY", "", "100.00", "1075.00"),
        positions,
    )
    if unresolved:
        _write_cells(
            page,
            165,
            ("05/08/2026", "SYNTHETIC UNKNOWN", "", "", "1075.00"),
            positions,
        )
    return _finish(document)


def _headerless_pdf(
    *,
    rows: int = 2,
    cramped: bool = False,
    continuation: bool = False,
    decoy_rows: bool = False,
) -> bytes:
    document = _document()
    page = document.new_page(width=595, height=842)
    page.insert_text((35, 45), "Fictional activity", fontsize=10)
    positions = (35.0, 140.0, 350.0, 465.0)
    if cramped:
        positions = (35.0, 55.0, 70.0, 82.0)
    values = (
        ("01/09/2026", "SYNTHETIC ONE", "-4.00", "96.00"),
        ("02/09/2026", "SYNTHETIC TWO", "+9.00", "105.00"),
    )
    if decoy_rows:
        _write_cells(page, 65, ("01/01/2026", "NO MONEY", "", ""), positions)
        _write_cells(page, 82, ("03/01/2026", "", "-1.00", ""), positions)
    for index, row in enumerate(values[:rows]):
        y = 110 + index * 35 if continuation else 90 + index * 25
        _write_cells(page, y, row, positions)
        if continuation and index == 0:
            _write_cells(page, y + 15, ("", "CONTINUED TEXT", "", ""), positions)
    if continuation:
        _write_cells(page, 300, ("", "FICTIONAL FOOTER", "", ""), positions)
    return _finish(document)


def _four_money_column_pdf() -> bytes:
    document = _document()
    page = document.new_page(width=595, height=842)
    positions = (30.0, 120.0, 290.0, 360.0, 430.0, 500.0)
    _write_cells(
        page,
        90,
        ("01/09/2026", "SYNTHETIC ONE", "1.00", "2.00", "3.00", "4.00"),
        positions,
    )
    _write_cells(
        page,
        120,
        ("02/09/2026", "SYNTHETIC TWO", "5.00", "6.00", "7.00", "8.00"),
        positions,
    )
    return _finish(document)


class _FakeRect:
    def __init__(self, width: float, height: float) -> None:
        self.width = width
        self.height = height


class _FakePage:
    def __init__(
        self,
        words: list[tuple[object, ...]],
        *,
        width: float = 595.0,
        height: float = 842.0,
    ) -> None:
        self.rect = _FakeRect(width, height)
        self._words = words

    def get_text(self, kind: str, *, sort: bool) -> list[tuple[object, ...]]:
        assert kind == "words"
        assert sort is True
        return self._words


class _FakeDocument:
    needs_pass = False

    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.page_count = 1

    def __enter__(self) -> _FakeDocument:
        return self

    def __exit__(self, *exc_info: object) -> None:
        del exc_info

    def load_page(self, page_index: int) -> _FakePage:
        assert page_index == 0
        return self._page


class _BrokenPage(_FakePage):
    def get_text(self, kind: str, *, sort: bool) -> list[tuple[object, ...]]:
        del kind, sort
        raise RuntimeError("synthetic page read failure")


def _accessibility_labelled_words(
    *,
    shift_second_bundle: bool = False,
    omit_second_balance_label: bool = False,
    include_unlabelled_row: bool = False,
    include_continuation: bool = False,
) -> list[tuple[object, ...]]:
    words: list[tuple[object, ...]] = []

    def add(text: str, x0: float, x1: float, top: float) -> None:
        words.append((x0, top, x1, top + 10.0, text))

    for text, x0, x1 in (
        ("Date", 30.0, 55.0),
        ("Description", 115.0, 175.0),
        ("Money", 300.0, 330.0),
        ("In", 333.0, 342.0),
        ("Money", 390.0, 420.0),
        ("Out", 423.0, 445.0),
        ("Balance", 500.0, 540.0),
    ):
        add(text, x0, x1, 10.0)

    def add_labelled_row(
        *,
        top: float,
        date_text: str,
        description: str,
        credit: str = "",
        debit: str = "",
        balance: str = "",
        shift: float = 0.0,
        include_balance_label: bool = True,
    ) -> None:
        add("Date", 30.0 + shift, 55.0 + shift, top)
        if date_text:
            add(date_text, 60.0, 90.0, top)
        add("Description", 115.0, 175.0, top)
        if description:
            add(description, 180.0, 225.0, top)
        add("Money", 300.0, 330.0, top)
        if credit:
            add(credit, 300.05, 330.05, top)
        add("In", 300.1, 330.1, top)
        add("Money", 390.0, 420.0, top)
        if debit:
            add(debit, 390.05, 420.05, top)
        add("Out", 390.1, 420.1, top)
        if include_balance_label:
            add("Balance", 500.0, 540.0, top)
        if balance:
            add(balance, 545.0, 580.0, top)

    add_labelled_row(
        top=30.0,
        date_text="01 May 26",
        description="SYNTHETIC SHOP",
        debit="10.00",
        balance="90.00",
    )
    if include_continuation:
        add_labelled_row(
            top=42.0,
            date_text="",
            description="CONTINUED TEXT",
        )
    add_labelled_row(
        top=60.0,
        date_text="02 May 26",
        description="SYNTHETIC REFUND",
        credit="5.00",
        balance="95.00",
        shift=10.0 if shift_second_bundle else 0.0,
        include_balance_label=not omit_second_balance_label,
    )
    if include_unlabelled_row:
        for text, x0, x1 in (
            ("03 May 26", 60.0, 90.0),
            ("SYNTHETIC EXTRA", 180.0, 225.0),
            ("2.00", 390.0, 420.0),
            ("93.00", 545.0, 580.0),
        ):
            add(text, x0, x1, 80.0)
    return words


def _missing_repeated_header_pdf(*, transaction_signal: bool) -> bytes:
    document = _document()
    positions = (35.0, 125.0, 345.0, 465.0)
    first = document.new_page(width=595, height=842)
    _write_cells(first, 80, ("Date", "Description", "Amount", "Balance"), positions)
    _write_cells(
        first,
        105,
        ("01/08/2026", "SYNTHETIC SHOP", "-10.00", "90.00"),
        positions,
    )
    second = document.new_page(width=595, height=842)
    second.insert_text((35, 45), "Fictional continuation page text", fontsize=9)
    _write_cells(second, 70, ("02/08/2026", "NO AMOUNT HERE", "", ""), positions)
    if transaction_signal:
        _write_cells(
            second,
            95,
            ("02/08/2026", "SYNTHETIC REFUND", "+5.00", "95.00"),
            positions,
        )
    return _finish(document)


def test_signed_amount_reconstruction_is_multipage_and_preserves_lineage() -> None:
    result = reconstruct_spatial_pdf(_signed_statement_pdf())

    assert result.state is SpatialPdfState.READY
    assert result.reason_code == "automatic_header_mapping"
    assert result.page_count == 2
    assert result.table is not None
    assert len(result.table.columns) == 4
    assert len(result.records) == 2
    assert result.records[0].transaction_date_text == "01/08/2026"
    assert result.records[0].description_text == "SYNTHETIC SHOP\nSECOND LINE"
    assert result.records[0].signed_amount_text == "-10.00"
    assert result.records[0].running_balance_text == "490.00"
    assert result.records[0].source.page_number == 1
    assert result.records[0].source.page_record_number == 1
    assert len(result.records[0].source.source_line_numbers) == 2
    assert result.records[1].source.page_number == 2
    assert result.records[1].source.page_record_number == 1
    assert result.table.structure_digest.isalnum()


def test_debit_credit_reconstruction_supports_uk_dates_and_optional_balance() -> None:
    result = reconstruct_spatial_pdf(_debit_credit_pdf())

    assert result.state is SpatialPdfState.READY
    assert result.mapping is not None
    assert result.mapping.signed_amount is None
    assert result.mapping.debit_amount is not None
    assert result.mapping.credit_amount is not None
    assert result.records[0].debit_amount_text == "25.00"
    assert result.records[0].credit_amount_text == ""
    assert result.records[1].transaction_date_text == "04 Aug 2026"
    assert result.records[1].debit_amount_text == ""
    assert result.records[1].credit_amount_text == "100.00"
    assert result.records[1].running_balance_text == "1075.00"


def test_accessibility_labels_are_removed_only_from_the_mapped_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage(_accessibility_labelled_words())
    monkeypatch.setattr(pymupdf, "open", lambda **kwargs: _FakeDocument(page))

    result = reconstruct_spatial_pdf(b"synthetic", min_embedded_characters=1)

    assert result.state is SpatialPdfState.READY
    assert len(result.records) == 2
    assert result.records[0].transaction_date_text == "01 May 26"
    assert result.records[0].description_text == "SYNTHETIC SHOP"
    assert result.records[0].debit_amount_text == "10.00"
    assert result.records[1].credit_amount_text == "5.00"
    assert result.table is not None
    assert "Date" in result.table.records[0].value("column_1")
    assert "Money" in result.table.records[0].value("column_4")
    assert result.table.records[0].mapped_cells is not None


def test_accessibility_labelled_continuations_keep_raw_and_mapped_text_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage(_accessibility_labelled_words(include_continuation=True))
    monkeypatch.setattr(pymupdf, "open", lambda **kwargs: _FakeDocument(page))

    result = reconstruct_spatial_pdf(b"synthetic", min_embedded_characters=1)

    assert result.state is SpatialPdfState.READY
    assert result.records[0].description_text == "SYNTHETIC SHOP\nCONTINUED TEXT"
    assert result.table is not None
    raw_description = result.table.records[0].value("column_2")
    assert raw_description.count("Description") == 2
    assert "Description" not in result.records[0].description_text


@pytest.mark.parametrize(
    "words",
    [
        _accessibility_labelled_words(shift_second_bundle=True),
        _accessibility_labelled_words(omit_second_balance_label=True),
        _accessibility_labelled_words(include_unlabelled_row=True),
    ],
)
def test_inconsistent_accessibility_labelled_rows_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    words: list[tuple[object, ...]],
) -> None:
    monkeypatch.setattr(
        pymupdf,
        "open",
        lambda **kwargs: _FakeDocument(_FakePage(words)),
    )

    result = reconstruct_spatial_pdf(b"synthetic", min_embedded_characters=1)

    assert result.state is SpatialPdfState.MAPPING_REQUIRED
    assert result.reason_code == "unresolved_spatial_rows"


def test_headerless_layout_requires_mapping_then_becomes_ready() -> None:
    content = _headerless_pdf()
    review = reconstruct_spatial_pdf(content)

    assert review.state is SpatialPdfState.MAPPING_REQUIRED
    assert review.reason_code == "column_mapping_required"
    assert review.table is not None
    assert review.mapping is None
    assert len(review.table.columns) == 4
    assert len(review.table.records) == 2

    mapping = SpatialColumnMapping(
        transaction_date="column_1",
        description="column_2",
        signed_amount="column_3",
        running_balance="column_4",
    )
    ready = reconstruct_spatial_pdf(content, mapping=mapping)

    assert ready.state is SpatialPdfState.READY
    assert ready.reason_code == "explicit_column_mapping"
    assert ready.mapping == mapping
    assert ready.records[1].description_text == "SYNTHETIC TWO"
    assert ready.records[1].signed_amount_text == "+9.00"


def test_headerless_inference_joins_descriptions_and_ignores_non_rows() -> None:
    result = reconstruct_spatial_pdf(
        _headerless_pdf(continuation=True, decoy_rows=True),
        mapping=SpatialColumnMapping(
            transaction_date="column_1",
            description="column_2",
            signed_amount="column_3",
            running_balance="column_4",
        ),
    )

    assert result.state is SpatialPdfState.READY
    assert result.records[0].description_text == "SYNTHETIC ONE\nCONTINUED TEXT"


def test_unresolved_rows_fail_closed_for_user_review() -> None:
    result = reconstruct_spatial_pdf(_debit_credit_pdf(unresolved=True))

    assert result.state is SpatialPdfState.MAPPING_REQUIRED
    assert result.reason_code == "unresolved_spatial_rows"
    assert result.table is not None
    assert result.table.records[-1].unresolved is True
    assert result.records[-1].transaction_date_text == "05/08/2026"


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (_headerless_pdf(rows=1), "no_stable_spatial_table"),
        (_headerless_pdf(cramped=True), "no_stable_spatial_table"),
        (_signed_statement_pdf(move_second_header=True), "inconsistent_page_layout"),
    ],
)
def test_unknown_or_inconsistent_layouts_are_not_guessed(
    content: bytes, reason: str
) -> None:
    result = reconstruct_spatial_pdf(content)

    assert result.state is SpatialPdfState.UNSUPPORTED_LAYOUT
    assert result.reason_code == reason
    assert result.table is None
    assert not result.records


def test_unsupported_spatial_structures_have_specific_safe_reasons() -> None:
    assert (
        reconstruct_spatial_pdf(_four_money_column_pdf()).reason_code
        == "no_stable_spatial_table"
    )

    document = _document()
    page = document.new_page(width=595, height=842)
    _write_cells(
        page,
        90,
        ("Date", "Description", "Amount", "Balance"),
        (35.0, 125.0, 345.0, 465.0),
    )
    assert (
        reconstruct_spatial_pdf(_finish(document)).reason_code
        == "no_spatial_transaction_rows"
    )


def test_header_roles_in_an_unsafe_order_are_not_accepted() -> None:
    words = (
        SpatialPdfWord(1, 20.0, 10.0, 60.0, 20.0, "Amount"),
        SpatialPdfWord(1, 100.0, 10.0, 130.0, 20.0, "Date"),
        SpatialPdfWord(1, 180.0, 10.0, 240.0, 20.0, "Description"),
    )
    line = _VisualLine(page_number=1, line_number=1, words=words)

    assert _header_from_line(line, line_index=0, page_width=595.0) is None


@pytest.mark.parametrize("header_text", ["", "Money Money"])
def test_ambiguous_accessibility_header_tokens_are_not_removed(
    header_text: str,
) -> None:
    line = _VisualLine(
        page_number=1,
        line_number=1,
        words=(SpatialPdfWord(1, 10.0, 10.0, 20.0, 20.0, "Money"),),
    )
    columns = (SpatialPdfColumn("column_1", 0.0, 1.0, header_text),)

    assert _accessibility_label_bundle(line, page_width=100.0, columns=columns) is None


def test_image_only_or_mixed_pdf_is_outside_digital_reconstruction() -> None:
    document = _document()
    document.new_page(width=595, height=842)
    result = reconstruct_spatial_pdf(_finish(document))

    assert result.state is SpatialPdfState.UNSUPPORTED_LAYOUT
    assert result.reason_code == "image_only_or_mixed_pdf"
    assert result.page_count == 1


@pytest.mark.parametrize(
    "mapping",
    [
        lambda: SpatialColumnMapping(
            transaction_date="", description="column_2", signed_amount="column_3"
        ),
        lambda: SpatialColumnMapping(
            transaction_date="column_1",
            description="column_2",
            debit_amount="column_3",
        ),
        lambda: SpatialColumnMapping(
            transaction_date="column_1", description="column_2"
        ),
        lambda: SpatialColumnMapping(
            transaction_date="column_1",
            description="column_2",
            signed_amount="column_3",
            debit_amount="column_4",
            credit_amount="column_5",
        ),
        lambda: SpatialColumnMapping(
            transaction_date="column_1",
            description="column_1",
            signed_amount="column_3",
        ),
    ],
)
def test_invalid_mapping_shapes_are_rejected(
    mapping: Any,
) -> None:
    with pytest.raises(SpatialPdfError) as error:
        mapping()
    assert error.value.code is SpatialPdfErrorCode.INVALID_MAPPING


def test_mapping_cannot_reference_an_unknown_reconstructed_column() -> None:
    mapping = SpatialColumnMapping(
        transaction_date="column_1",
        description="column_2",
        signed_amount="column_99",
    )
    with pytest.raises(SpatialPdfError) as error:
        reconstruct_spatial_pdf(_headerless_pdf(), mapping=mapping)
    assert error.value.code is SpatialPdfErrorCode.INVALID_MAPPING


def test_a_manual_mapping_that_does_not_describe_rows_remains_untrusted() -> None:
    mapping = SpatialColumnMapping(
        transaction_date="column_2",
        description="column_1",
        signed_amount="column_3",
        running_balance="column_4",
    )
    result = reconstruct_spatial_pdf(_headerless_pdf(), mapping=mapping)

    assert result.state is SpatialPdfState.MAPPING_REQUIRED
    assert result.reason_code == "invalid_mapped_rows"


@pytest.mark.parametrize(
    "limits",
    [
        {"max_pages": 0},
        {"max_words": 0},
        {"min_embedded_characters": 0},
    ],
)
def test_limits_must_be_positive(limits: dict[str, int]) -> None:
    with pytest.raises(SpatialPdfError) as error:
        reconstruct_spatial_pdf(_headerless_pdf(), **cast(Any, limits))
    assert error.value.code is SpatialPdfErrorCode.INVALID_LIMIT


def test_malformed_encrypted_page_and_word_limits_fail_safely() -> None:
    with pytest.raises(SpatialPdfError) as malformed:
        reconstruct_spatial_pdf(b"not a PDF")
    assert malformed.value.code is SpatialPdfErrorCode.MALFORMED_PDF

    document = _document()
    page = document.new_page(width=595, height=842)
    page.insert_text((35, 45), "Fictional encrypted statement", fontsize=9)
    encrypted = _finish(
        document,
        encryption=cast(Any, pymupdf).PDF_ENCRYPT_AES_256,
        owner_pw="synthetic-owner",
        user_pw="synthetic-user",
    )
    with pytest.raises(SpatialPdfError) as protected:
        reconstruct_spatial_pdf(encrypted)
    assert protected.value.code is SpatialPdfErrorCode.ENCRYPTED_PDF

    document = _document()
    document.new_page()
    document.new_page()
    with pytest.raises(SpatialPdfError) as pages:
        reconstruct_spatial_pdf(_finish(document), max_pages=1)
    assert pages.value.code is SpatialPdfErrorCode.TOO_MANY_PAGES

    with pytest.raises(SpatialPdfError) as words:
        reconstruct_spatial_pdf(_headerless_pdf(), max_words=1)
    assert words.value.code is SpatialPdfErrorCode.PAGE_TOO_COMPLEX


def test_page_text_read_failure_is_reported_without_source_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken_page = _BrokenPage([])
    monkeypatch.setattr(pymupdf, "open", lambda **kwargs: _FakeDocument(broken_page))

    with pytest.raises(SpatialPdfError) as captured:
        reconstruct_spatial_pdf(b"synthetic", min_embedded_characters=1)

    assert captured.value.code is SpatialPdfErrorCode.MALFORMED_PDF
    assert "synthetic page read failure" not in str(captured.value)


def test_missing_repeated_header_is_rejected_only_when_rows_are_present() -> None:
    unsafe = reconstruct_spatial_pdf(
        _missing_repeated_header_pdf(transaction_signal=True)
    )
    harmless = reconstruct_spatial_pdf(
        _missing_repeated_header_pdf(transaction_signal=False)
    )

    assert unsafe.state is SpatialPdfState.UNSUPPORTED_LAYOUT
    assert unsafe.reason_code == "inconsistent_page_layout"
    assert harmless.state is SpatialPdfState.READY
    assert len(harmless.records) == 1


def test_low_level_word_filtering_fails_closed_without_exposing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filtered_page = _FakePage(
        [
            (0.0, 0.0, 1.0, 1.0),
            (0.0, 0.0, 1.0, 1.0, " "),
            (0.0, 0.0, 0.0, 1.0, "INVALID"),
            (0.0, 0.0, 10.0, 10.0, "SAFE"),
        ]
    )
    monkeypatch.setattr(pymupdf, "open", lambda **kwargs: _FakeDocument(filtered_page))
    filtered = reconstruct_spatial_pdf(b"synthetic", min_embedded_characters=1)
    assert filtered.reason_code == "no_stable_spatial_table"

    invalid_geometry = _FakePage([], width=float("nan"))
    monkeypatch.setattr(
        pymupdf, "open", lambda **kwargs: _FakeDocument(invalid_geometry)
    )
    with pytest.raises(SpatialPdfError) as geometry:
        reconstruct_spatial_pdf(b"synthetic", min_embedded_characters=1)
    assert geometry.value.code is SpatialPdfErrorCode.MALFORMED_PDF

    long_word = _FakePage([(0.0, 0.0, 10.0, 10.0, "TOO-LONG")])
    monkeypatch.setattr(spatial_pdf_module, "_MAX_WORD_CHARACTERS", 3)
    monkeypatch.setattr(pymupdf, "open", lambda **kwargs: _FakeDocument(long_word))
    with pytest.raises(SpatialPdfError) as complex_page:
        reconstruct_spatial_pdf(b"synthetic", min_embedded_characters=1)
    assert complex_page.value.code is SpatialPdfErrorCode.PAGE_TOO_COMPLEX

    monkeypatch.setattr(spatial_pdf_module, "_MAX_WORD_CHARACTERS", 4_096)
    non_finite = _FakePage([(float("nan"), 0.0, 10.0, 10.0, "WORD")])
    monkeypatch.setattr(pymupdf, "open", lambda **kwargs: _FakeDocument(non_finite))
    with pytest.raises(SpatialPdfError) as invalid_word:
        reconstruct_spatial_pdf(b"synthetic", min_embedded_characters=1)
    assert invalid_word.value.code is SpatialPdfErrorCode.PAGE_TOO_COMPLEX


def test_private_source_values_are_excluded_from_representations() -> None:
    word = SpatialPdfWord(1, 1.0, 2.0, 3.0, 4.0, "PRIVATE WORD")
    cell = SpatialPdfCell("column_1", "PRIVATE CELL", 1.0, 2.0, 3.0, 4.0)
    source = SpatialPdfRecord(1, 1, (1,), (cell,))
    mapped = MappedSpatialRecord(
        source=source,
        transaction_date_text="01/01/2026",
        description_text="PRIVATE DESCRIPTION",
        signed_amount_text="-123.45",
    )

    assert "PRIVATE WORD" not in repr(word)
    assert "PRIVATE CELL" not in repr(cell)
    assert "PRIVATE DESCRIPTION" not in repr(mapped)
    assert "-123.45" not in repr(mapped)
    assert source.value("column_1") == "PRIVATE CELL"
    assert source.value("missing") == ""
    assert _mapping_value((cell,), None) == ""


def test_explicit_header_mapping_can_omit_the_optional_balance() -> None:
    mapping = SpatialColumnMapping(
        transaction_date="column_1",
        description="column_2",
        signed_amount="column_3",
    )
    result = reconstruct_spatial_pdf(_signed_statement_pdf(), mapping=mapping)

    assert result.state is SpatialPdfState.READY
    assert result.reason_code == "explicit_column_mapping"
    assert result.records[0].running_balance_text is None
    assert result.table is not None
    assert result.table.columns[0].role_hint is SpatialColumnRole.TRANSACTION_DATE
