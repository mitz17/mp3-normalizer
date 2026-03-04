"""Tkinter ベースの GUI"""
from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
import tkinter as tk

from dataclasses import dataclass

from processor import AudioProcessor
from utils import DEFAULT_LUFS, DEFAULT_TRUE_PEAK, DEFAULT_WORKERS, scan_audio_files


@dataclass
class PreviewInfo:
    """処理予定一覧のサマリー"""

    items: list[str]
    total: int
    process_count: int
    skip_count: int


class AdjusterApp(tk.Tk):
    """GUI アプリケーション本体"""

    def __init__(self, logger: logging.Logger) -> None:
        super().__init__()
        self.title("mp3-normalizer")
        self.resizable(False, False)
        self.logger = logger
        self.message_queue: queue.Queue[str] = queue.Queue()
        self.processor = AudioProcessor(
            logger=self.logger,
            notifier=self.message_queue.put,
        )
        self.worker: threading.Thread | None = None

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.lufs_var = tk.DoubleVar(value=DEFAULT_LUFS)
        self.true_peak_var = tk.DoubleVar(value=DEFAULT_TRUE_PEAK)
        self.workers_var = tk.IntVar(value=DEFAULT_WORKERS)
        self.force_var = tk.BooleanVar(value=False)
        self.include_subdirs_var = tk.BooleanVar(value=True)
        self.preview_summary_var = tk.StringVar(value="入力フォルダを選択してください")
        self._preview_update_job: str | None = None

        self._build_widgets()
        self._bind_variable_updates()
        self.after(200, self._drain_queue)
        self._refresh_preview()

    def _build_widgets(self) -> None:
        """ウィジェットを初期化する"""
        padding = {"padx": 8, "pady": 4}
        frame = ttk.Frame(self)
        frame.grid(column=0, row=0, sticky="nsew")

        ttk.Label(frame, text="入力フォルダ").grid(column=0, row=0, sticky="w", **padding)
        input_entry = ttk.Entry(frame, textvariable=self.input_var, width=40)
        input_entry.grid(column=1, row=0, **padding)
        ttk.Button(frame, text="参照", command=self._select_input).grid(column=2, row=0, **padding)

        ttk.Label(frame, text="出力フォルダ").grid(column=0, row=1, sticky="w", **padding)
        output_entry = ttk.Entry(frame, textvariable=self.output_var, width=40)
        output_entry.grid(column=1, row=1, **padding)
        ttk.Button(frame, text="参照", command=self._select_output).grid(column=2, row=1, **padding)

        ttk.Label(frame, text="LUFS 目標値").grid(column=0, row=2, sticky="w", **padding)
        ttk.Entry(frame, textvariable=self.lufs_var, width=10).grid(column=1, row=2, sticky="w", **padding)

        ttk.Label(frame, text="True Peak (dBFS)").grid(column=0, row=3, sticky="w", **padding)
        ttk.Entry(frame, textvariable=self.true_peak_var, width=10).grid(column=1, row=3, sticky="w", **padding)

        ttk.Label(frame, text="並列実行数").grid(column=0, row=4, sticky="w", **padding)
        ttk.Spinbox(frame, from_=1, to=32, textvariable=self.workers_var, width=8).grid(
            column=1, row=4, sticky="w", **padding
        )

        self.start_button = ttk.Button(frame, text="正規化を開始", command=self._start_processing)
        self.start_button.grid(column=0, row=5, columnspan=3, sticky="ew", padx=8, pady=(8, 4))

        ttk.Checkbutton(
            frame,
            text="処理済みでも再実行する",
            variable=self.force_var,
        ).grid(column=0, row=6, columnspan=3, sticky="w", padx=8, pady=(0, 4))

        ttk.Checkbutton(
            frame,
            text="サブフォルダも対象にする",
            variable=self.include_subdirs_var,
        ).grid(column=0, row=7, columnspan=3, sticky="w", padx=8, pady=(0, 8))

        preview_frame = ttk.LabelFrame(self, text="対象mp3プレビュー")
        preview_frame.grid(column=0, row=1, sticky="nsew", padx=8, pady=(0, 8))
        ttk.Label(preview_frame, textvariable=self.preview_summary_var).pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        self.preview_widget = scrolledtext.ScrolledText(
            preview_frame,
            width=70,
            height=12,
            state=tk.DISABLED,
        )
        self.preview_widget.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.log_widget = scrolledtext.ScrolledText(self, width=70, height=12, state=tk.DISABLED)
        self.log_widget.grid(column=0, row=2, padx=8, pady=(0, 8))

    def _bind_variable_updates(self) -> None:
        """プレビュー更新が必要な変数にトレースを設定"""

        def trigger(*_: object) -> None:
            self._schedule_preview_update()

        for var in (self.input_var, self.force_var, self.include_subdirs_var):
            var.trace_add("write", trigger)

    def _select_input(self) -> None:
        directory = filedialog.askdirectory(title="入力フォルダを選択")
        if directory:
            self.input_var.set(directory)
            self._schedule_preview_update()

    def _select_output(self) -> None:
        directory = filedialog.askdirectory(title="出力フォルダを選択")
        if directory:
            self.output_var.set(directory)

    def _start_processing(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("処理中", "現在処理を実行中です")
            return

        try:
            input_dir = Path(self.input_var.get()).expanduser()
            output_dir = Path(self.output_var.get()).expanduser()
            target_lufs = float(self.lufs_var.get())
            true_peak = float(self.true_peak_var.get())
            workers = int(self.workers_var.get())
        except ValueError:
            messagebox.showerror("入力エラー", "数値の入力を確認してください")
            return
        if workers < 1:
            messagebox.showerror("入力エラー", "並列実行数は 1 以上を指定してください")
            return

        if not input_dir.exists() or not input_dir.is_dir():
            messagebox.showerror("入力エラー", "入力フォルダが存在しません")
            return

        if not output_dir.exists():
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("出力エラー", f"出力フォルダを作成できません: {exc}")
                return

        include_subdirs = self.include_subdirs_var.get()
        try:
            files = scan_audio_files(input_dir, recursive=include_subdirs)
        except ValueError as exc:
            messagebox.showerror("入力エラー", str(exc))
            return

        if not files:
            messagebox.showinfo("対象なし", "指定フォルダに mp3 ファイルが見つかりません")
            return

        force = self.force_var.get()
        preview = self._build_preview_info(input_dir, files, force)
        if preview.process_count == 0 and preview.total > 0 and not force:
            messagebox.showinfo("処理対象なし", "履歴の結果、今回は全てスキップ予定です。必要に応じて再実行フラグを有効化してください。")
            return

        if not self._show_preview_dialog(preview):
            self._append_log("ユーザー操作により処理をキャンセルしました")
            return

        self.processor.force = force
        self.processor.workers = workers
        self._append_log("処理を開始します")
        self.start_button.configure(state=tk.DISABLED)
        self.worker = threading.Thread(
            target=self._run_processing,
            args=(input_dir, output_dir, target_lufs, true_peak, include_subdirs, workers),
            daemon=True,
        )
        self.worker.start()

    def _run_processing(
        self,
        input_dir: Path,
        output_dir: Path,
        target_lufs: float,
        true_peak: float,
        include_subdirs: bool,
        workers: int,
    ) -> None:
        try:
            self.processor.process_directory(
                input_dir=input_dir,
                output_dir=output_dir,
                target_lufs=target_lufs,
                true_peak=true_peak,
                recursive=include_subdirs,
                workers=workers,
            )
        except Exception as exc:  # noqa: BLE001
            message = f"予期せぬエラーが発生しました: {exc}"
            self.logger.exception(message)
            self.message_queue.put(message)
        finally:
            self.message_queue.put("__DONE__")

    def _build_preview_info(
        self,
        input_dir: Path,
        files: list[Path],
        force: bool,
    ) -> PreviewInfo:
        """処理予定ファイルの一覧情報を生成する"""
        items: list[str] = []
        process_count = 0
        skip_count = 0
        for file_path in sorted(files):
            relative = self._safe_relative(file_path, input_dir)
            label = "処理予定"
            detail = ""
            try:
                stat_result = file_path.stat()
            except OSError as exc:
                label = "確認不可"
                detail = f"情報取得失敗: {exc}"
            else:
                already_processed = False
                if not force:
                    already_processed = self.processor.history_service.is_processed(
                        relative,
                        stat_result.st_size,
                        stat_result.st_mtime,
                    )
                if already_processed and not force:
                    label = "スキップ予定"
                    skip_count += 1
                else:
                    process_count += 1
            entry = f"[{label}] {relative.as_posix()}"
            if detail:
                entry += f" ({detail})"
            items.append(entry)

        return PreviewInfo(
            items=items,
            total=len(files),
            process_count=process_count,
            skip_count=skip_count,
        )

    def _schedule_preview_update(self) -> None:
        """プレビュー更新の呼び出しをデバウンスする"""
        if self._preview_update_job is not None:
            self.after_cancel(self._preview_update_job)
        self._preview_update_job = self.after(400, self._refresh_preview)

    def _refresh_preview(self) -> None:
        """入力ディレクトリの内容を事前に表示する"""
        self._preview_update_job = None
        raw_input = self.input_var.get().strip()
        if not raw_input:
            self._render_preview("入力フォルダを選択してください", [])
            return

        input_dir = Path(raw_input).expanduser()
        if not input_dir.exists() or not input_dir.is_dir():
            self._render_preview("入力フォルダが存在しません", [])
            return

        recursive = self.include_subdirs_var.get()
        try:
            files = scan_audio_files(input_dir, recursive=recursive)
        except ValueError as exc:
            self._render_preview(str(exc), [])
            return

        if not files:
            self._render_preview("mp3 ファイルが見つかりません", [])
            return

        force = self.force_var.get()
        preview = self._build_preview_info(input_dir, files, force)
        summary = (
            f"対象 {preview.total} 件 / "
            f"処理 {preview.process_count} 件 / スキップ {preview.skip_count} 件"
        )
        self._render_preview(summary, preview.items)

    def _render_preview(self, summary: str, items: list[str]) -> None:
        """プレビュー表示をウィジェットへ反映する"""
        self.preview_summary_var.set(summary)
        self.preview_widget.configure(state=tk.NORMAL)
        self.preview_widget.delete("1.0", tk.END)
        if items:
            self.preview_widget.insert(tk.END, "\n".join(items))
        self.preview_widget.configure(state=tk.DISABLED)

    def _show_preview_dialog(self, preview: PreviewInfo) -> bool:
        """対象mp3リストを表示して実行可否を確認する"""
        dialog = tk.Toplevel(self)
        dialog.title("対象ファイル確認")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(True, True)

        summary = (
            f"対象 {preview.total} 件 / "
            f"処理 {preview.process_count} 件 / スキップ {preview.skip_count} 件"
        )
        ttk.Label(dialog, text=summary).pack(anchor="w", padx=12, pady=(12, 4))

        text_widget = scrolledtext.ScrolledText(dialog, width=70, height=18, state=tk.NORMAL)
        text_widget.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        text_widget.insert(tk.END, "\n".join(preview.items))
        text_widget.configure(state=tk.DISABLED)

        button_frame = ttk.Frame(dialog)
        button_frame.pack(fill="x", padx=12, pady=(0, 12))

        decision = {"value": False}

        def accept() -> None:
            decision["value"] = True
            dialog.destroy()

        def cancel() -> None:
            dialog.destroy()

        ttk.Button(button_frame, text="実行", command=accept).pack(side="left", expand=True, padx=(0, 6))
        ttk.Button(button_frame, text="キャンセル", command=cancel).pack(side="right", expand=True)

        dialog.wait_window()
        return decision["value"]

    @staticmethod
    def _safe_relative(path: Path, base_dir: Path) -> Path:
        try:
            return path.relative_to(base_dir)
        except ValueError:
            return Path(path.name)

    def _drain_queue(self) -> None:
        """メッセージキューをポーリングする"""
        try:
            while True:
                message = self.message_queue.get_nowait()
                if message == "__DONE__":
                    self.start_button.configure(state=tk.NORMAL)
                else:
                    self._append_log(message)
        except queue.Empty:
            pass
        finally:
            self.after(200, self._drain_queue)

    def _append_log(self, message: str) -> None:
        self.log_widget.configure(state=tk.NORMAL)
        self.log_widget.insert(tk.END, message + "\n")
        self.log_widget.see(tk.END)
        self.log_widget.configure(state=tk.DISABLED)


def launch_gui(logger: logging.Logger) -> None:
    """GUI を起動する"""
    app = AdjusterApp(logger)
    app.mainloop()
