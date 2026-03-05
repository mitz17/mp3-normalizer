"""エントリーポイント"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gui import launch_gui
from processor import AudioProcessor
from utils import DEFAULT_LUFS, DEFAULT_TRUE_PEAK, DEFAULT_WORKERS, configure_logger


def build_parser() -> argparse.ArgumentParser:
    """コマンドライン引数を定義する"""
    parser = argparse.ArgumentParser(description="mp3-normalizer: mp3 の LUFS 正規化ツール")
    parser.add_argument("--input", "-i", type=Path, help="入力フォルダのパス")
    parser.add_argument("--output", "-o", type=Path, help="出力フォルダのパス")
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

    processor = AudioProcessor(logger=logger, force=args.force, workers=max(1, args.workers))
    try:
        summary = processor.process_directory(
            input_dir=input_dir,
            output_dir=output_dir,
            target_lufs=args.lufs,
            true_peak=args.true_peak,
            workers=max(1, args.workers),
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
