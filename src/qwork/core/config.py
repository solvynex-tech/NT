import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]


def now():
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    theta_key: str = ""

    @classmethod
    def load(cls):
        load_dotenv(ROOT / ".env", override=False)
        value = Path(os.getenv("QWORK_DATA_DIR", str(ROOT / "data")))
        if not value.is_absolute():
            value = ROOT / value
        return cls(value.resolve(), os.getenv("THETADATA_API_KEY", ""))
