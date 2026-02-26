"""mp3-normalizer のユーティリティ群"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

DEFAULT_LUFS = -14.0
DEFAULT_TRUE_PEAK = -1.0
SUPPORTED_EXTENSIONS = [".mp3"]
LOG_FILE = Path("mp3_normalizer.log")
FFMPEG_COMMAND = "ffmpeg"
PROCESSED_HISTORY_FILE = Path("processed_history.json")


def configure_logger() -> logging.Logger:
    """ロガーを初期化して返す"""
    logger = logging.getLogger("mp3_normalizer")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def ensure_ffmpeg_available(ffmpeg_cmd: str = FFMPEG_COMMAND) -> None:
    """ffmpeg が利用可能か検証する"""
    if shutil.which(ffmpeg_cmd) is None:
        raise FileNotFoundError(
            "ffmpeg が見つかりませんでした。インストールされているか確認してください。"
        )


def scan_audio_files(
    input_dir: Path,
    extensions: Iterable[str] | None = None,
    recursive: bool = True,
) -> List[Path]:
    """入力ディレクトリ配下の対象ファイル一覧を返す"""
    if not input_dir.exists() or not input_dir.is_dir():
        raise ValueError(f"入力ディレクトリが見つかりません: {input_dir}")

    exts = tuple(ext.lower() for ext in (extensions or SUPPORTED_EXTENSIONS))
    files: List[Path] = []
    iterator = input_dir.rglob("*") if recursive else input_dir.glob("*")
    for path in iterator:
        if path.is_file() and path.suffix.lower() in exts:
            files.append(path)
    return files


def ensure_directory(path: Path) -> None:
    """ディレクトリを作成する（既存の場合は何もしない）"""
    path.mkdir(parents=True, exist_ok=True)


def generate_unique_output_path(path: Path) -> Path:
    """同名ファイルが存在する場合は連番を付与して回避する"""
    if not path.exists():
        return path

    base = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{base}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def format_command(cmd: Iterable[str]) -> str:
    """ログ出力用にコマンドを結合する"""
    parts = []
    for token in cmd:
        if " " in token:
            parts.append(f'"{token}"')
        else:
            parts.append(token)
    return " ".join(parts)


@dataclass
class FileSignature:
    """ファイル同一性を判定するための情報"""

    size: int
    mtime: float


class ProcessedHistory:
    """処理済みファイルの履歴を JSON で保持する"""

    def __init__(self, path: Path = PROCESSED_HISTORY_FILE) -> None:
        self.path = path
        self.records: dict[str, FileSignature] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self.records = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            self.records = {}
            return
        self.records = {
            key: FileSignature(value["size"], value["mtime"])
            for key, value in data.items()
            if isinstance(value, dict)
            and "size" in value
            and "mtime" in value
        }

    def save(self) -> None:
        payload = {
            key: {"size": sig.size, "mtime": sig.mtime}
            for key, sig in self.records.items()
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def is_processed(self, relative_path: Path, size: int, mtime: float) -> bool:
        key = self._normalize_key(relative_path)
        record = self.records.get(key)
        if not record:
            return False
        return record.size == size and abs(record.mtime - mtime) < 1e-6

    def mark_processed(self, relative_path: Path, size: int, mtime: float) -> None:
        key = self._normalize_key(relative_path)
        self.records[key] = FileSignature(size=size, mtime=mtime)

    @staticmethod
    def _normalize_key(relative_path: Path) -> str:
        return relative_path.as_posix()
