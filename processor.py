"""音声処理ロジック"""
from __future__ import annotations

import copy
import base64
import json
import logging
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError
except ImportError:  # pragma: no cover
    MutagenFile = None
    APIC = None
    ID3 = None
    ID3NoHeaderError = Exception

from utils import (
    DEFAULT_AAC_BITRATE,
    DEFAULT_AUDIO_CODEC,
    DEFAULT_LINEAR,
    DEFAULT_LUFS,
    DEFAULT_LRA,
    DEFAULT_METADATA_MODE,
    DEFAULT_MP3_QUALITY,
    DEFAULT_OUTPUT_FORMAT,
    DEFAULT_TRUE_PEAK,
    DEFAULT_WORKERS,
    FFMPEG_COMMAND,
    SUPPORTED_EXTENSIONS,
    SUPPORTED_OUTPUT_FORMATS,
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


@dataclass
class NormalizationOptions:
    """正規化/エンコード挙動をまとめる設定"""

    target_lufs: float = DEFAULT_LUFS
    true_peak: float = DEFAULT_TRUE_PEAK
    lra: float = DEFAULT_LRA
    linear: bool = DEFAULT_LINEAR
    output_format: str = DEFAULT_OUTPUT_FORMAT
    audio_codec: str = DEFAULT_AUDIO_CODEC
    audio_quality: str = DEFAULT_MP3_QUALITY
    audio_bitrate: str = DEFAULT_AAC_BITRATE

    def normalized_output_format(self) -> str:
        fmt = self.output_format.lower().lstrip(".")
        if fmt not in SUPPORTED_OUTPUT_FORMATS:
            raise ValueError(f"未対応の出力形式です: {self.output_format}")
        return fmt

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


class MetadataPreserver:
    """ID3 メタデータを安全モードで移植する。"""

    LYRICS_ID3_PREFIXES = {
        "USLT",  # unsynced lyrics
        "SYLT",  # synced lyrics
    }

    SAFE_ID3_PREFIXES = {
        "TIT2",  # title
        "TPE1",  # artist
        "TPE2",  # album artist
        "TALB",  # album
        "TRCK",  # track
        "TPOS",  # disc
        "TDRC",  # date
        "TYER",  # year
        "TCON",  # genre
        "COMM",  # comment
        "USLT",  # unsynced lyrics
        "SYLT",  # synced lyrics
        "APIC",  # artwork
    }

    def __init__(
        self,
        logger: logging.Logger,
        notifier: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.logger = logger
        self.notifier = notifier
        self._is_enabled = ID3 is not None

        if not self._is_enabled:
            message = "ID3 メタデータ移植は無効です: mutagen が未インストールです"
            self.logger.warning(message)
            self._notify(message)

    def copy_if_present(self, source: Path, destination: Path, mode: str) -> None:
        if destination.suffix.lower() != ".mp3":
            return
        if not self._is_enabled:
            return

        if mode == "safe":
            self._copy_safe_frames(source, destination)
            return
        if mode == "all":
            self._copy_selected_frames(
                source,
                destination,
                prefixes=self.LYRICS_ID3_PREFIXES,
                log_label="歌詞メタデータ移植",
            )
            self._copy_artwork_only(source, destination)
            return
        return

    def _copy_safe_frames(self, source: Path, destination: Path) -> None:
        try:
            source_tags = ID3(str(source))
        except ID3NoHeaderError:
            return
        except Exception as exc:  # noqa: BLE001
            self._warn(source, destination, f"入力タグ読み込み失敗: {exc}")
            return

        safe_frames = [
            copy.deepcopy(frame)
            for key, frame in source_tags.items()
            if key.split(":", maxsplit=1)[0] in self.SAFE_ID3_PREFIXES
        ]
        if not safe_frames:
            return

        try:
            try:
                dest_tags = ID3(str(destination))
            except ID3NoHeaderError:
                dest_tags = ID3()

            for key in list(dest_tags.keys()):
                if key.split(":", maxsplit=1)[0] in self.SAFE_ID3_PREFIXES:
                    del dest_tags[key]
            for frame in safe_frames:
                dest_tags.add(frame)
            dest_tags.save(str(destination))
        except Exception as exc:  # noqa: BLE001
            self._warn(source, destination, f"出力タグ保存失敗: {exc}")
            return

        info = (
            f"安全メタデータ移植: {source.name} → {destination.name} "
            f"(frames {len(safe_frames)})"
        )
        self.logger.info(info)
        self._notify(info)

    def _copy_selected_frames(
        self,
        source: Path,
        destination: Path,
        prefixes: set[str],
        log_label: str,
    ) -> None:
        try:
            source_tags = ID3(str(source))
        except ID3NoHeaderError:
            return
        except Exception as exc:  # noqa: BLE001
            self._warn(source, destination, f"入力タグ読み込み失敗: {exc}")
            return

        selected_frames = [
            copy.deepcopy(frame)
            for key, frame in source_tags.items()
            if key.split(":", maxsplit=1)[0] in prefixes
        ]
        if not selected_frames:
            return

        try:
            try:
                dest_tags = ID3(str(destination))
            except ID3NoHeaderError:
                dest_tags = ID3()

            for key in list(dest_tags.keys()):
                if key.split(":", maxsplit=1)[0] in prefixes:
                    del dest_tags[key]
            for frame in selected_frames:
                dest_tags.add(frame)
            dest_tags.save(str(destination))
        except Exception as exc:  # noqa: BLE001
            self._warn(source, destination, f"出力タグ保存失敗: {exc}")
            return

        info = f"{log_label}: {source.name} → {destination.name} (frames {len(selected_frames)})"
        self.logger.info(info)
        self._notify(info)

    def _copy_artwork_only(self, source: Path, destination: Path) -> None:
        apic_frames = self._extract_apic_frames(source)
        if not apic_frames:
            return

        try:
            try:
                dest_tags = ID3(str(destination))
            except ID3NoHeaderError:
                dest_tags = ID3()
            dest_tags.delall("APIC")
            for frame in apic_frames:
                dest_tags.add(frame)
            dest_tags.save(str(destination))
        except Exception as exc:  # noqa: BLE001
            self._warn(source, destination, f"出力タグ保存失敗: {exc}")
            return

        info = f"アートワーク移植: {source.name} → {destination.name} (APIC {len(apic_frames)})"
        self.logger.info(info)
        self._notify(info)

    def _extract_apic_frames(self, source: Path) -> list:
        frames: list = []
        if APIC is None:
            return frames

        # 1) MP3/ID3 APIC を優先
        try:
            id3_tags = ID3(str(source))
            frames = [copy.deepcopy(frame) for frame in id3_tags.getall("APIC")]
            if frames:
                return frames
        except ID3NoHeaderError:
            pass
        except Exception:
            pass

        if MutagenFile is None:
            return frames

        # 2) mutagen 汎用タグから artwork を抽出して APIC 化
        try:
            meta = MutagenFile(str(source))
        except Exception:  # noqa: BLE001
            return frames
        if not meta:
            return frames

        tags = getattr(meta, "tags", None)
        if tags:
            covr = tags.get("covr")
            if covr:
                for item in covr:
                    mime = "image/jpeg"
                    imageformat = getattr(item, "imageformat", None)
                    if imageformat == 14:
                        mime = "image/png"
                    frames.append(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=bytes(item)))
                if frames:
                    return frames

            mbp_items = tags.get("metadata_block_picture")
            if mbp_items:
                for encoded in mbp_items:
                    try:
                        decoded = base64.b64decode(encoded)
                    except Exception:  # noqa: BLE001
                        continue
                    picture = self._parse_flac_picture(decoded)
                    if picture is None:
                        continue
                    frames.append(
                        APIC(
                            encoding=3,
                            mime=picture["mime"],
                            type=picture["type"],
                            desc=picture["desc"],
                            data=picture["data"],
                        )
                    )
                if frames:
                    return frames

        pictures = getattr(meta, "pictures", None)
        if pictures:
            for pic in pictures:
                frames.append(
                    APIC(
                        encoding=3,
                        mime=getattr(pic, "mime", "image/jpeg") or "image/jpeg",
                        type=getattr(pic, "type", 3) or 3,
                        desc=getattr(pic, "desc", "") or "",
                        data=getattr(pic, "data", b""),
                    )
                )
        return [frame for frame in frames if getattr(frame, "data", b"")]

    @staticmethod
    def _parse_flac_picture(data: bytes) -> dict[str, object] | None:
        # FLAC picture block parser for VorbisComment metadata_block_picture fallback.
        if len(data) < 32:
            return None
        try:
            offset = 0
            pic_type = int.from_bytes(data[offset:offset + 4], "big")
            offset += 4
            mime_len = int.from_bytes(data[offset:offset + 4], "big")
            offset += 4
            mime = data[offset:offset + mime_len].decode("utf-8", errors="replace")
            offset += mime_len
            desc_len = int.from_bytes(data[offset:offset + 4], "big")
            offset += 4
            desc = data[offset:offset + desc_len].decode("utf-8", errors="replace")
            offset += desc_len
            offset += 16  # width, height, depth, colors
            pic_data_len = int.from_bytes(data[offset:offset + 4], "big")
            offset += 4
            pic_data = data[offset:offset + pic_data_len]
            if not pic_data:
                return None
            return {
                "mime": mime or "image/jpeg",
                "type": pic_type or 3,
                "desc": desc or "",
                "data": pic_data,
            }
        except Exception:  # noqa: BLE001
            return None

    def _warn(self, source: Path, destination: Path, reason: str) -> None:
        message = f"メタデータ移植に失敗: {source.name} → {destination.name} | {reason}"
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
        metadata_preserver: MetadataPreserver | None = None,
    ) -> None:
        self.ffmpeg_cmd = ffmpeg_cmd or FFMPEG_COMMAND
        self.logger = logger
        self.notifier = notifier
        self.metadata_preserver = metadata_preserver or MetadataPreserver(
            logger=logger, notifier=notifier
        )

    def ensure_available(self) -> None:
        ensure_ffmpeg_available(self.ffmpeg_cmd)

    def normalize(
        self,
        input_file: Path,
        destination: Path,
        options: NormalizationOptions,
    ) -> NormalizationResult:
        output_format = options.normalized_output_format()
        metadata_mode = DEFAULT_METADATA_MODE
        destination = destination.with_suffix(f".{output_format}")
        source_bitrate = self._detect_input_bitrate(input_file)
        if source_bitrate:
            self.logger.info("入力ビットレート検出: %s (%s)", input_file.name, source_bitrate)
            self._notify(f"入力ビットレート検出: {input_file.name} ({source_bitrate})")

        analysis_filter = self._build_analysis_filter(options)
        first_pass_command = [
            self.ffmpeg_cmd,
            "-hide_banner",
            "-y",
            "-i",
            str(input_file),
            "-af",
            analysis_filter,
            "-f",
            "null",
            "NUL" if sys.platform == "win32" else "/dev/null",
        ]
        first_pass_str = format_command(first_pass_command)
        log_message = f"ffmpeg 1pass(測定): {first_pass_str}"
        self.logger.info(log_message)
        self._notify(log_message)

        measured, first_pass_error = self._run_and_parse_first_pass(first_pass_command)
        if measured is None:
            stderr_text = first_pass_error or "loudnorm 1pass 測定値の取得に失敗しました"
            error_msg = (
                f"処理失敗: {input_file.name} | LUFS目標 {options.target_lufs} | エラー: {stderr_text}"
            )
            self.logger.error(error_msg)
            self._notify(error_msg)
            return NormalizationResult(
                input_file=input_file,
                output_file=destination,
                success=False,
                message=stderr_text,
                command=first_pass_str,
            )

        final_filter = self._build_final_filter(options, measured)
        second_pass_command = [
            self.ffmpeg_cmd,
            "-hide_banner",
            "-y",
            "-i",
            str(input_file),
            "-af",
            final_filter,
            *self._build_codec_args(options, source_bitrate),
            *self._build_metadata_args(metadata_mode, output_format),
            str(destination),
        ]
        command_str = format_command(second_pass_command)
        self.logger.info("ffmpeg 2pass(本処理): %s", command_str)
        self._notify(f"ffmpeg 2pass(本処理): {command_str}")

        result = subprocess.run(
            second_pass_command,
            check=False,
            capture_output=True,
            text=False,
        )
        stderr_text = self._decode_process_output(result.stderr)
        if result.returncode != 0:
            error_msg = (
                f"処理失敗: {input_file.name} | LUFS目標 {options.target_lufs} | "
                f"終了コード {result.returncode} | エラー: {stderr_text}"
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

        if stderr_text:
            self.logger.debug(stderr_text)
        self.metadata_preserver.copy_if_present(input_file, destination, metadata_mode)
        success_msg = (
            f"処理成功: {input_file.name} → {destination.name} | "
            f"LUFS目標 {options.target_lufs} | LRA {options.lra}"
        )
        self.logger.info(success_msg)
        self._notify(success_msg)
        return NormalizationResult(
            input_file=input_file,
            output_file=destination,
            success=True,
            message="",
            command=command_str,
        )

    def _build_analysis_filter(self, options: NormalizationOptions) -> str:
        loudnorm = (
            f"loudnorm=I={options.target_lufs}:TP={options.true_peak}:"
            f"LRA={options.lra}:print_format=json"
        )
        return loudnorm

    def _build_final_filter(
        self,
        options: NormalizationOptions,
        measured: dict[str, float],
    ) -> str:
        linear_flag = "true" if options.linear else "false"
        loudnorm = (
            f"loudnorm=I={options.target_lufs}:TP={options.true_peak}:LRA={options.lra}:"
            f"linear={linear_flag}:measured_I={measured['input_i']}:"
            f"measured_TP={measured['input_tp']}:measured_LRA={measured['input_lra']}:"
            f"measured_thresh={measured['input_thresh']}:offset={measured['target_offset']}"
        )
        return loudnorm

    @staticmethod
    def _build_codec_args(options: NormalizationOptions, source_bitrate: str | None) -> list[str]:
        output_format = options.normalized_output_format()
        codec = options.audio_codec.strip().lower()
        args: list[str] = []

        if codec and codec != "auto":
            args.extend(["-c:a", codec])
            if source_bitrate:
                args.extend(["-b:a", source_bitrate])
            elif options.audio_quality:
                args.extend(["-q:a", str(options.audio_quality)])
            elif options.audio_bitrate:
                args.extend(["-b:a", str(options.audio_bitrate)])
            return args

        if output_format == "mp3":
            if source_bitrate:
                return ["-c:a", "libmp3lame", "-b:a", source_bitrate]
            return ["-c:a", "libmp3lame", "-q:a", str(options.audio_quality or DEFAULT_MP3_QUALITY)]
        if output_format == "aac":
            if source_bitrate:
                return ["-c:a", "aac", "-b:a", source_bitrate]
            return ["-c:a", "aac", "-b:a", str(options.audio_bitrate or DEFAULT_AAC_BITRATE)]
        if output_format == "flac":
            return ["-c:a", "flac"]
        if output_format == "wav":
            return ["-c:a", "pcm_s16le"]
        if output_format == "ogg":
            if source_bitrate:
                return ["-c:a", "libvorbis", "-b:a", source_bitrate]
            return ["-c:a", "libvorbis", "-q:a", str(options.audio_quality or "5")]
        raise ValueError(f"未対応の出力形式です: {output_format}")

    @staticmethod
    def _detect_input_bitrate(input_file: Path) -> str | None:
        if MutagenFile is None:
            return None
        try:
            meta = MutagenFile(str(input_file))
        except Exception:  # noqa: BLE001
            return None
        if not meta or not getattr(meta, "info", None):
            return None
        raw_bitrate = getattr(meta.info, "bitrate", None)
        if not isinstance(raw_bitrate, (int, float)) or raw_bitrate <= 0:
            return None
        kbps = max(32, int(round(raw_bitrate / 1000.0)))
        return f"{kbps}k"

    @staticmethod
    def _build_metadata_args(metadata_mode: str, output_format: str) -> list[str]:
        if metadata_mode == "all":
            # Keep metadata and map optional attached picture stream for MP3 output.
            args = ["-map_metadata", "0", "-map", "0:a:0"]
            if output_format == "mp3":
                args.extend(["-map", "0:v?", "-c:v", "copy", "-disposition:v:0", "attached_pic"])
            return args
        if metadata_mode in {"safe", "none"}:
            return ["-map_metadata", "-1"]
        raise ValueError(f"未対応のメタデータモードです: {metadata_mode}")

    def _run_and_parse_first_pass(self, command: list[str]) -> tuple[dict[str, float] | None, str]:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=False,
        )
        stdout_text = self._decode_process_output(result.stdout)
        stderr_text = self._decode_process_output(result.stderr)
        combined_text = "\n".join(part for part in (stdout_text, stderr_text) if part)
        if stderr_text:
            self.logger.debug(stderr_text)
        if stdout_text:
            self.logger.debug(stdout_text)

        if result.returncode != 0:
            detail = combined_text or "(stdout/stderrなし)"
            message = f"1pass測定失敗: 終了コード {result.returncode} | {detail}"
            self.logger.error(message)
            return None, message

        measured = self._parse_loudnorm_stats(combined_text)
        if measured is None:
            detail = combined_text or "(stdout/stderrなし)"
            message = f"1pass測定値のJSON解析失敗 | {detail}"
            self.logger.error(message)
            return None, message
        return measured, ""

    @staticmethod
    def _parse_loudnorm_stats(stderr_text: str) -> dict[str, float] | None:
        matches = re.findall(r"\{[\s\S]*?\}", stderr_text)
        if not matches:
            return None
        for raw_json in reversed(matches):
            try:
                payload = json.loads(raw_json)
            except json.JSONDecodeError:
                continue
            required = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
            if not all(key in payload for key in required):
                continue
            try:
                return {key: float(payload[key]) for key in required}
            except (TypeError, ValueError):
                continue
        return None

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
        failed_results = [result for result in results if not result.success]
        if failed_results:
            self.logger.error("失敗ファイル一覧 (%s件):", len(failed_results))
            self._notify(f"失敗ファイル一覧 ({len(failed_results)}件):")
            for result in failed_results:
                detail = result.message or "詳細不明"
                line = f"- {result.input_file.name} | {detail}"
                self.logger.error(line)
                self._notify(line)
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
        lra: float = DEFAULT_LRA,
        linear: bool = DEFAULT_LINEAR,
        output_format: str = DEFAULT_OUTPUT_FORMAT,
            audio_codec: str = DEFAULT_AUDIO_CODEC,
            audio_quality: str = DEFAULT_MP3_QUALITY,
            audio_bitrate: str = DEFAULT_AAC_BITRATE,
            input_extensions: Iterable[str] | None = None,
            recursive: bool = True,
            workers: int | None = None,
    ) -> ProcessSummary:
        """ディレクトリ配下のファイルを順次処理する"""
        self.executor.ensure_available()
        ensure_directory(output_dir)
        options = NormalizationOptions(
            target_lufs=target_lufs,
            true_peak=true_peak,
            lra=lra,
            linear=linear,
            output_format=output_format,
            audio_codec=audio_codec,
            audio_quality=audio_quality,
            audio_bitrate=audio_bitrate,
        )
        selected_extensions = self._normalize_input_extensions(input_extensions)
        if not selected_extensions:
            message = "処理対象の拡張子が選択されていません。"
            self.logger.warning(message)
            self._notify(message)
            return ProcessSummary(total=0, success=0, failed=0, skipped=0)

        files = scan_audio_files(input_dir, selected_extensions, recursive=recursive)
        if not files:
            message = "処理対象となる音声ファイルが見つかりませんでした。"
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
                    options=options,
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
                        options,
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
        options: NormalizationOptions,
    ) -> NormalizationResult:
        destination = output_dir / entry.relative
        ensure_directory(destination.parent)
        destination = destination.with_suffix(f".{options.normalized_output_format()}")
        destination = generate_unique_output_path(destination)
        self.logger.info("[%s/%s] %s を処理します", index, total, entry.source.name)
        return self.executor.normalize(
            input_file=entry.source,
            destination=destination,
            options=options,
        )

    @staticmethod
    def _normalize_input_extensions(input_extensions: Iterable[str] | None) -> list[str]:
        if input_extensions is None:
            return [ext.lower() for ext in SUPPORTED_EXTENSIONS]
        normalized = []
        supported = {ext.lower() for ext in SUPPORTED_EXTENSIONS}
        for ext in input_extensions:
            token = ext.strip().lower()
            if not token:
                continue
            if not token.startswith("."):
                token = f".{token}"
            if token in supported and token not in normalized:
                normalized.append(token)
        return normalized
