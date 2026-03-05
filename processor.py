"""音声処理ロジック"""
from __future__ import annotations

import copy
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional

try:
    from mutagen.id3 import ID3, ID3NoHeaderError
except ImportError:  # pragma: no cover
    ID3 = None
    ID3NoHeaderError = Exception

from utils import (
    DEFAULT_LUFS,
    DEFAULT_TRUE_PEAK,
    DEFAULT_WORKERS,
    FFMPEG_COMMAND,
    SUPPORTED_EXTENSIONS,
    ProcessedHistory,
    ensure_directory,
    ensure_ffmpeg_available,
    format_command,
    generate_unique_output_path,
    scan_audio_files,
)


@dataclass
class NormalizationResult:
    """処理結果を保持するデータクラス"""

    input_file: Path
    output_file: Path
    success: bool
    message: str
    command: str


@dataclass
class PlanEntry:
    """計画された処理対象"""

    source: Path
    relative: Path
    size: int
    mtime: float


@dataclass
class ProcessingPlan:
    """処理対象一覧とスキップ件数を保持"""

    entries: List[PlanEntry]
    skipped: int
    total: int

    @property
    def planned_count(self) -> int:
        return len(self.entries)


@dataclass
class ProcessSummary:
    """処理全体のサマリ"""

    total: int
    success: int
    failed: int
    skipped: int


class HistoryService:
    """処理済み履歴の管理"""

    def __init__(self, storage: ProcessedHistory | None = None) -> None:
        self.storage = storage or ProcessedHistory()

    def is_processed(self, relative_path: Path, size: int, mtime: float) -> bool:
        return self.storage.is_processed(relative_path, size, mtime)

    def mark_processed(self, relative_path: Path, size: int, mtime: float) -> None:
        self.storage.mark_processed(relative_path, size, mtime)

    def save(self) -> None:
        self.storage.save()


class LyricsTagPreserver:
    """歌詞タグ(USLT/SYLT)を入力ファイルから出力ファイルへ移植する。"""

    def __init__(
        self,
        logger: logging.Logger,
        notifier: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.logger = logger
        self.notifier = notifier
        self._is_enabled = ID3 is not None

        if not self._is_enabled:
            message = "歌詞タグ移植は無効です: mutagen が未インストールです"
            self.logger.warning(message)
            self._notify(message)

    def copy_if_present(self, source: Path, destination: Path) -> None:
        if not self._is_enabled:
            return

        try:
            source_tags = ID3(str(source))
        except ID3NoHeaderError:
            return
        except Exception as exc:  # noqa: BLE001
            self._warn(source, destination, f"入力タグ読み込み失敗: {exc}")
            return

        uslt_frames = source_tags.getall("USLT")
        sylt_frames = source_tags.getall("SYLT")
        if not uslt_frames and not sylt_frames:
            return

        try:
            try:
                dest_tags = ID3(str(destination))
            except ID3NoHeaderError:
                dest_tags = ID3()

            dest_tags.delall("USLT")
            dest_tags.delall("SYLT")
            for frame in uslt_frames:
                dest_tags.add(copy.deepcopy(frame))
            for frame in sylt_frames:
                dest_tags.add(copy.deepcopy(frame))
            dest_tags.save(str(destination))
        except Exception as exc:  # noqa: BLE001
            self._warn(source, destination, f"出力タグ保存失敗: {exc}")
            return

        info = (
            f"歌詞タグ移植: {source.name} → {destination.name} "
            f"(USLT {len(uslt_frames)} / SYLT {len(sylt_frames)})"
        )
        self.logger.info(info)
        self._notify(info)

    def _warn(self, source: Path, destination: Path, reason: str) -> None:
        message = f"歌詞タグ移植に失敗: {source.name} → {destination.name} | {reason}"
        self.logger.warning(message)
        self._notify(message)

    def _notify(self, message: str) -> None:
        if self.notifier:
            self.notifier(message)


class FfmpegExecutor:
    """ffmpeg の呼び出しをカプセル化"""

    def __init__(
        self,
        ffmpeg_cmd: str,
        logger: logging.Logger,
        notifier: Optional[Callable[[str], None]] = None,
        lyrics_preserver: LyricsTagPreserver | None = None,
    ) -> None:
        self.ffmpeg_cmd = ffmpeg_cmd or FFMPEG_COMMAND
        self.logger = logger
        self.notifier = notifier
        self.lyrics_preserver = lyrics_preserver or LyricsTagPreserver(
            logger=logger, notifier=notifier
        )

    def ensure_available(self) -> None:
        ensure_ffmpeg_available(self.ffmpeg_cmd)

    def normalize(
        self,
        input_file: Path,
        destination: Path,
        target_lufs: float,
        true_peak: float,
    ) -> NormalizationResult:
        command = [
            self.ffmpeg_cmd,
            "-hide_banner",
            "-y",
            "-i",
            str(input_file),
            "-af",
            f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            "-map_metadata",
            "0",
            str(destination),
        ]
        command_str = format_command(command)
        log_message = f"ffmpeg コマンド: {command_str}"
        self.logger.info(log_message)
        self._notify(log_message)

        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=False,
            )
        except subprocess.CalledProcessError as exc:
            stderr_text = self._decode_process_output(exc.stderr)
            error_msg = (
                f"処理失敗: {input_file.name} | LUFS目標 {target_lufs} | エラー: {stderr_text}"
            )
            self.logger.error(error_msg)
            self._notify(error_msg)
            return NormalizationResult(
                input_file=input_file,
                output_file=destination,
                success=False,
                message=stderr_text,
                command=command_str,
            )

        success_msg = f"処理成功: {input_file.name} → {destination.name} | LUFS目標 {target_lufs}"
        if result.stderr:
            self.logger.debug(self._decode_process_output(result.stderr))
        self.logger.info(success_msg)
        self._notify(success_msg)
        self.lyrics_preserver.copy_if_present(input_file, destination)
        return NormalizationResult(
            input_file=input_file,
            output_file=destination,
            success=True,
            message="",
            command=command_str,
        )

    def _notify(self, message: str) -> None:
        if self.notifier:
            self.notifier(message)

    @staticmethod
    def _decode_process_output(data: bytes | str | None) -> str:
        """ffmpeg 出力を文字化け/DecodeError を避けて文字列化する。"""
        if data is None:
            return ""
        if isinstance(data, str):
            return data.strip()
        for encoding in ("utf-8", "cp932"):
            try:
                return data.decode(encoding).strip()
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace").strip()


class ProcessingPlanner:
    """処理計画の作成"""

    def __init__(
        self,
        history: HistoryService,
        logger: logging.Logger,
        notifier: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.history = history
        self.logger = logger
        self.notifier = notifier

    def create_plan(
        self,
        files: Iterable[Path],
        base_dir: Path,
        force: bool,
    ) -> ProcessingPlan:
        entries: List[PlanEntry] = []
        skipped = 0
        total = 0
        for file_path in files:
            total += 1
            relative = self._relativize(file_path, base_dir)
            stat_result = file_path.stat()
            if not force and self.history.is_processed(
                relative, stat_result.st_size, stat_result.st_mtime
            ):
                skipped += 1
                skip_msg = f"スキップ: {relative} は処理済みのため実行しません"
                self.logger.info(skip_msg)
                self._notify(skip_msg)
                continue
            entries.append(
                PlanEntry(
                    source=file_path,
                    relative=relative,
                    size=stat_result.st_size,
                    mtime=stat_result.st_mtime,
                )
            )

        plan = ProcessingPlan(entries=entries, skipped=skipped, total=total)
        summary_plan = (
            f"実行前情報: ログ履歴参照の結果、今回処理 {plan.planned_count} 件 / "
            f"対象 {plan.total} 件 / スキップ予定 {plan.skipped} 件"
        )
        self.logger.info(summary_plan)
        self._notify(summary_plan)
        return plan

    @staticmethod
    def _relativize(path: Path, base_dir: Path) -> Path:
        try:
            return path.relative_to(base_dir)
        except ValueError:
            return Path(path.name)

    def _notify(self, message: str) -> None:
        if self.notifier:
            self.notifier(message)


class ResultAggregator:
    """集計と最終メッセージ出力"""

    def __init__(
        self,
        logger: logging.Logger,
        notifier: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.logger = logger
        self.notifier = notifier

    def summarize(
        self,
        plan: ProcessingPlan,
        results: List[NormalizationResult],
    ) -> ProcessSummary:
        success_count = sum(1 for result in results if result.success)
        failure_count = len(results) - success_count
        if plan.planned_count == 0:
            message = "処理対象の新規ファイルが存在しません"
            if plan.skipped:
                message += f" (スキップ {plan.skipped} 件)"
            self.logger.info(message)
            self._notify(message)
            return ProcessSummary(
                total=plan.total,
                success=0,
                failed=0,
                skipped=plan.skipped,
            )

        message = (
            f"処理完了: 成功 {success_count} 件 / "
            f"失敗 {failure_count} 件 / スキップ {plan.skipped} 件"
        )
        self.logger.info(message)
        self._notify(message)
        return ProcessSummary(
            total=plan.total,
            success=success_count,
            failed=failure_count,
            skipped=plan.skipped,
        )

    def _notify(self, message: str) -> None:
        if self.notifier:
            self.notifier(message)


class AudioProcessor:
    """ffmpeg を利用した LUFS 正規化を行う"""

    def __init__(
        self,
        logger: logging.Logger,
        ffmpeg_cmd: str = FFMPEG_COMMAND,
        notifier: Optional[Callable[[str], None]] = None,
        history_service: HistoryService | None = None,
        executor: FfmpegExecutor | None = None,
        planner: ProcessingPlanner | None = None,
        aggregator: ResultAggregator | None = None,
        force: bool = False,
        workers: int = DEFAULT_WORKERS,
    ) -> None:
        if logger is None:
            raise ValueError("logger is required")
        self.logger = logger
        self.notifier = notifier
        self.force = force
        self.workers = max(1, workers)
        self.history_service = history_service or HistoryService()
        self.executor = executor or FfmpegExecutor(
            ffmpeg_cmd=ffmpeg_cmd, logger=self.logger, notifier=notifier
        )
        self.planner = planner or ProcessingPlanner(
            history=self.history_service, logger=self.logger, notifier=notifier
        )
        self.aggregator = aggregator or ResultAggregator(
            logger=self.logger, notifier=notifier
        )

    def _notify(self, message: str) -> None:
        if self.notifier:
            self.notifier(message)

    def process_directory(
        self,
        input_dir: Path,
        output_dir: Path,
        target_lufs: float = DEFAULT_LUFS,
        true_peak: float = DEFAULT_TRUE_PEAK,
        recursive: bool = True,
        workers: int | None = None,
    ) -> ProcessSummary:
        """ディレクトリ配下のファイルを順次処理する"""
        self.executor.ensure_available()
        ensure_directory(output_dir)
        files = scan_audio_files(
            input_dir,
            SUPPORTED_EXTENSIONS,
            recursive=recursive,
        )
        if not files:
            message = "処理対象となる mp3 ファイルが見つかりませんでした。"
            self.logger.warning(message)
            self._notify(message)
            return ProcessSummary(total=0, success=0, failed=0, skipped=0)

        plan = self.planner.create_plan(files, input_dir, self.force)
        results: List[NormalizationResult] = []
        planned_count = plan.planned_count
        worker_count = min(max(1, workers or self.workers), planned_count) if planned_count else 1
        self.logger.info("並列実行数: %s", worker_count)
        self._notify(f"並列実行数: {worker_count}")

        if worker_count == 1:
            for index, entry in enumerate(plan.entries, start=1):
                result = self._process_single_entry(
                    index=index,
                    total=planned_count,
                    entry=entry,
                    output_dir=output_dir,
                    target_lufs=target_lufs,
                    true_peak=true_peak,
                )
                results.append(result)
                if result.success:
                    self.history_service.mark_processed(entry.relative, entry.size, entry.mtime)
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = {
                    pool.submit(
                        self._process_single_entry,
                        index,
                        planned_count,
                        entry,
                        output_dir,
                        target_lufs,
                        true_peak,
                    ): entry
                    for index, entry in enumerate(plan.entries, start=1)
                }
                for future in as_completed(futures):
                    entry = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001
                        message = f"処理失敗: {entry.source.name} | 予期せぬエラー: {exc}"
                        self.logger.exception(message)
                        self._notify(message)
                        result = NormalizationResult(
                            input_file=entry.source,
                            output_file=output_dir / entry.relative,
                            success=False,
                            message=str(exc),
                            command="",
                        )
                    results.append(result)
                    if result.success:
                        self.history_service.mark_processed(entry.relative, entry.size, entry.mtime)

        self.history_service.save()
        return self.aggregator.summarize(plan, results)

    def _process_single_entry(
        self,
        index: int,
        total: int,
        entry: PlanEntry,
        output_dir: Path,
        target_lufs: float,
        true_peak: float,
    ) -> NormalizationResult:
        destination = output_dir / entry.relative
        ensure_directory(destination.parent)
        destination = destination.with_suffix(".mp3")
        destination = generate_unique_output_path(destination)
        self.logger.info("[%s/%s] %s を処理します", index, total, entry.source.name)
        return self.executor.normalize(
            input_file=entry.source,
            destination=destination,
            target_lufs=target_lufs,
            true_peak=true_peak,
        )
