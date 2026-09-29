"""Shared result type for accountable database writes."""


class WriteResult(int):
    """Write counts while remaining compatible with legacy integer callers."""

    inserted: int
    duplicates: int
    failed: int
    ignored_count: int

    def __new__(
        cls,
        inserted: int = 0,
        duplicates: int = 0,
        failed: int = 0,
        ignored_count: int | None = None,
    ) -> "WriteResult":
        obj = super().__new__(cls, inserted)
        obj.inserted = inserted
        obj.duplicates = duplicates
        obj.failed = failed
        obj.ignored_count = duplicates if ignored_count is None else ignored_count
        return obj

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "failed": self.failed,
        }
