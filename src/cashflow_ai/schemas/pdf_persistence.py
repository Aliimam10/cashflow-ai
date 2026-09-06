"""Results returned after an approved digital-PDF statement is persisted."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from cashflow_ai.schemas.csv_imports import CsvCoverageAnalysis
from cashflow_ai.schemas.normalisation import Sha256Digest
from cashflow_ai.schemas.transactions import Identifier


class _PdfPersistenceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PdfRecordLocation(_PdfPersistenceContract):
    """Stable page and record location for one persisted PDF row."""

    page_number: PositiveInt
    page_record_number: PositiveInt


class PdfImportSummary(_PdfPersistenceContract):
    """Auditable row counts from one atomic digital-PDF import."""

    import_batch_id: Identifier
    file_hash: Sha256Digest
    rows_read: PositiveInt
    imported_transactions: int = Field(ge=0)
    exact_duplicates_skipped: int = Field(ge=0)
    probable_duplicates: int = Field(ge=0)
    rejected_rows: int = Field(ge=0)
    repeated_file: bool = False
    exact_duplicate_locations: tuple[PdfRecordLocation, ...] = ()
    probable_duplicate_locations: tuple[PdfRecordLocation, ...] = ()
    rejected_locations: tuple[PdfRecordLocation, ...] = ()
    coverage: CsvCoverageAnalysis

    @model_validator(mode="after")
    def validate_row_accounting(self) -> PdfImportSummary:
        """Require every extracted row to have one explicit persistence result."""
        accounted = (
            self.imported_transactions
            + self.exact_duplicates_skipped
            + self.probable_duplicates
            + self.rejected_rows
        )
        if accounted != self.rows_read:
            raise ValueError("PDF import result counts must account for every row")
        expected_lengths = (
            (self.exact_duplicates_skipped, self.exact_duplicate_locations),
            (self.probable_duplicates, self.probable_duplicate_locations),
            (self.rejected_rows, self.rejected_locations),
        )
        if any(count != len(locations) for count, locations in expected_lengths):
            raise ValueError("PDF import counts must match their record locations")
        return self
