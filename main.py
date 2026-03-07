"""エントリーポイント"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gui import launch_gui
from processor import AudioProcessor
from utils import (
    DEFAULT_AAC_BITRATE,
    DEFAULT_AUDIO_CODEC,
    DEFAULT_LINEAR,
    DEFAULT_LRA,
    DEFAULT_LUFS,
    DEFAULT_MP3_QUALITY,
    DEFAULT_OUTPUT_FORMAT,
    DEFAULT_TRUE_PEAK,
    DEFAULT_WORKERS,
    SUPPORTED_EXTENSIONS,
    SUPPORTED_OUTPUT_FORMATS,
    configure_logger,
)


def build_parser() -> argparse.ArgumentParser:
    """コマンドライン引数を定義する"""
    parser = argparse.ArgumentParser(description="mp3-normalizer: mp3 の LUFS 正規化ツール")
    parser.add_argument("--input", "-i", type=Path, help="入力フォルダのパス")
    parser.add_argument("--output", "-o", type=Path, help="出力フォルダのパス")
    parser.add_argument(
        "--input-ext",
        action="append",
        choices=[ext.lstrip(".") for ext in SUPPORTED_EXTENSIONS],
        help="処理対象拡張子（複数指定可。例: --input-ext mp3 --input-ext flac）",
    )
    parser.add_argument(
        "--lufs",
        type=float,
        default=DEFAULT_LUFS,
        help=f"LUFS 目標値 (既定: {DEFAULT_LUFS})",
    )
    parser.add_argument(
        "--true-peak",
        type=float,
        default=DEFAULT_TRUE_PEAK,
        help=f"True Peak 目標値 (既定: {DEFAULT_TRUE_PEAK})",
    )
    parser.add_argument(
        "--lra",
        type=float,
        default=DEFAULT_LRA,
        help=f"LRA 目標値 (既定: {DEFAULT_LRA})",
    )
    parser.add_argument(
        "--linear",
        dest="linear",
        action="store_true",
        default=DEFAULT_LINEAR,
        help="loudnorm 2pass で linear=true を使用する",
    )
    parser.add_argument(
        "--no-linear",
        dest="linear",
        action="store_false",
        help="linear=true を無効化する",
    )
    parser.add_argument(
        "--output-format",
        choices=SUPPORTED_OUTPUT_FORMATS,
        default=DEFAULT_OUTPUT_FORMAT,
        help=f"出力形式 (既定: {DEFAULT_OUTPUT_FORMAT})",
    )
    parser.add_argument(
        "--audio-codec",
        default=DEFAULT_AUDIO_CODEC,
        help=f"音声コーデック (auto で形式別既定。既定: {DEFAULT_AUDIO_CODEC})",
    )
    parser.add_argument(
        "--audio-quality",
        default=DEFAULT_MP3_QUALITY,
        help=f"VBR 品質値(q:a) (既定: {DEFAULT_MP3_QUALITY})",
    )
    parser.add_argument(
        "--audio-bitrate",
        default=DEFAULT_AAC_BITRATE,
        help=f"CBR/ABR ビットレート(b:a) (既定: {DEFAULT_AAC_BITRATE})",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="GUI を起動せずに CLI モードで実行する",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="処理済みファイルも再度処理する",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"並列実行数 (既定: {DEFAULT_WORKERS})",
    )
    return parser


def run_cli(args: argparse.Namespace, logger) -> int:
    """CLI 実行をハンドルする"""
    if not args.input or not args.output:
        logger.error("CLI モードでは --input と --output が必須です")
        return 1

    input_dir = args.input.expanduser()
    output_dir = args.output.expanduser()

    target_lufs = args.lufs
    true_peak = args.true_peak
    input_extensions = (
        [f".{ext.lower().lstrip('.')}" for ext in args.input_ext]
        if args.input_ext
        else None
    )

    processor = AudioProcessor(logger=logger, force=args.force, workers=max(1, args.workers))
    try:
        summary = processor.process_directory(
            input_dir=input_dir,
            output_dir=output_dir,
            target_lufs=target_lufs,
            lra=args.lra,
            linear=args.linear,
            output_format=args.output_format,
            audio_codec=args.audio_codec,
            audio_quality=args.audio_quality,
            audio_bitrate=args.audio_bitrate,
            true_peak=true_peak,
            input_extensions=input_extensions,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("CLI 処理中に致命的エラー: %s", exc)
        return 1

    if summary.total == 0:
        return 1
    if summary.failed > 0:
        return 2
    # 全件スキップ（success=failed=0, skipped>0）は成功扱い
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logger = configure_logger()
    if args.cli:
        return run_cli(args, logger)

    launch_gui(logger)
    return 0


if __name__ == "__main__":
    sys.exit(main())
