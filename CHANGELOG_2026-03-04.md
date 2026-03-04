# 変更記録 (2026-03-04)

## 概要
- 対象: `mp3-normalizer`
- 日付: 2026-03-04
- 目的: 処理停止に見える事象の切り分け、文字コードエラー修正、処理速度改善

## 1. 調査結果
- 事象: `145件で途中終了したように見える`
- 結論: 途中終了ではなく正常完了
- 根拠ログ:
  - `2026-03-04 21:00:17 [INFO] 実行前情報: ... 今回処理 145 件 / 対象 145 件 / スキップ予定 0 件`
  - `2026-03-04 21:11:27 [INFO] 処理完了: 成功 145 件 / 失敗 0 件 / スキップ 0 件`
- 補足: 入力 `C:/Users/cagal/Desktop/Music` の実ファイル内訳
  - 全ファイル `1157`
  - `.mp3` `145`
  - `.m4a` `945`
  - 現実装は `.mp3` のみ対象

## 2. エラー/失敗点と対応

### 2-1. 文字コードエラー
- 発生エラー:
  - `UnicodeDecodeError: 'cp932' codec can't decode byte 0x83 in position 220: illegal multibyte sequence`
- 原因:
  - `subprocess.run(..., text=True)` が Windows 既定 (`cp932`) で `ffmpeg` 出力をデコード
  - UTF-8 系バイトが混在するとデコード失敗
- 対応:
  - `processor.py` の `FfmpegExecutor.normalize` を `text=False` に変更
  - 受け取った bytes を `utf-8 -> cp932 -> utf-8(replace)` の順で安全デコード
- 影響:
  - 同種のデコード例外で処理が落ちるリスクを低減

### 2-2. 速度ボトルネック
- 失敗点:
  - 全件直列処理（1ファイルずつ）で時間が長い
- 対応:
  - `ThreadPoolExecutor` による並列処理を導入
  - GUIに「並列実行数」入力追加
  - CLIに `--workers` 追加
  - 既定並列数を `min(4, CPUコア数)` に設定

## 3. 実測ベンチマーク

### 条件
- 入力: `C:/Users/cagal/Desktop/Music`
- 対象: `.mp3` 145件
- 設定: `LUFS=-14.0`, `TP=-1.0`, `--force`

### Before（直列）
- ソース: 既存ログ
- 開始: `2026-03-04 21:00:17`
- 完了: `2026-03-04 21:11:27`
- 所要時間: `670秒`（11分10秒）
- 終了コード: `0`

### After（並列4）
- 実行コマンド:
  - `python main.py --cli -i "C:/Users/cagal/Desktop/Music" -o "D:/programs/mp3-normalizer/bench_out_w4" --force --workers 4`
- 計測結果:
  - `elapsed_seconds=207.3`
  - ログ開始: `2026-03-04 21:28:34`
  - ログ完了: `2026-03-04 21:32:02`
- 所要時間: `207.3秒`（3分27.3秒）
- 終了コード: `0`

### 短縮効果
- 短縮時間: `462.7 秒`
- 短縮率: `69.1 %`
- 速度倍率: `約 3.23 倍`

## 4. 変更ファイル
- `processor.py`
  - ffmpeg出力の安全デコード対応
  - 並列処理実装 (`ThreadPoolExecutor`)
- `gui.py`
  - 並列実行数入力（Spinbox）追加
- `main.py`
  - CLI引数 `--workers` 追加
- `utils.py`
  - `DEFAULT_WORKERS` 追加

## 5. 既知の制約
- 現在の対象拡張子は `.mp3` のみ
- `.m4a` を含める場合は `SUPPORTED_EXTENSIONS` の拡張と変換ポリシー調整が必要
