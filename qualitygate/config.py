from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    max_bytes: int = 4 * 1024 * 1024
    max_rows: int = 20000
    max_columns: int = 80
    max_issues: int = 10000

    @property
    def database(self) -> Path:
        return self.data_dir / "qualitygate.sqlite3"
