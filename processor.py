"""音声処理ロジック"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from utils import (
    DEFAULT_LUFS,
    DEFAULT_TRUE_PEAK,
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


class FfmpegExecutor:
    """ffmpeg の呼び出しをカプセル化"""

    def __init__(
        self,
        ffmpeg_cmd: str,
        logger: logging.Logger,
        notifier: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.ffmpeg_cmd = ffmpeg_cmd or FFMPEG_COMMAND
        self.logger = logger
        self.notifier = notifier

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
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            error_msg = (
                f"処理失敗: {input_file.name} | LUFS目標 {target_lufs} | エラー: {exc.stderr.strip()}"
            )
            self.logger.error(error_msg)
            self._notify(error_msg)
            return NormalizationResult(
                input_file=input_file,
                output_file=destination,
                success=False,
                message=exc.stderr.strip(),
                command=command_str,
            )

        success_msg = f"処理成功: {input_file.name} → {destination.name} | LUFS目標 {target_lufs}"
        if result.stderr:
            self.logger.debug(result.stderr)
        self.logger.info(success_msg)
        self._notify(success_msg)
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
    ) -> None:
        if logger is None:
            raise ValueError("logger is required")
        self.logger = logger
        self.notifier = notifier
        self.force = force
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
        for index, entry in enumerate(plan.entries, start=1):
            destination = output_dir / entry.relative
            ensure_directory(destination.parent)
            destination = destination.with_suffix(".mp3")
            destination = generate_unique_output_path(destination)
            self.logger.info(
                "[%s/%s] %s を処理します",
                index,
                plan.planned_count,
                entry.source.name,
            )
            result = self.executor.normalize(
                input_file=entry.source,
                destination=destination,
                target_lufs=target_lufs,
                true_peak=true_peak,
            )
            results.append(result)
            if result.success:
                self.history_service.mark_processed(entry.relative, entry.size, entry.mtime)

        self.history_service.save()
        return self.aggregator.summarize(plan, results)
