from pathlib import Path

import pytest

from all2md.converter import convert_file


def test_missing_input_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing.docx"
    with pytest.raises(FileNotFoundError):
        convert_file(missing)
