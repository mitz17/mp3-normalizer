# mp3-normalizer

Tkinter ベースの GUI と CLI の両方で利用できる音声ラウドネス正規化ツールです。指定ディレクトリ配下の音声ファイルを再帰的に探索し、ffmpeg の `loudnorm` フィルタ（2pass）で音量をそろえます。

## 開発ログ
https://mitz17.com/blog/mp3-normalizer-devlog/

## 必要環境
- Python 3.11 以上
- ffmpeg 6.x 以上（`ffmpeg` コマンドへ PATH でアクセスできること）
  - ffmpeg は別途インストールが必要です。ライセンスは ffmpeg 側の条件に従ってください。

### ffmpeg のインストール例
- Windows: https://www.gyan.dev/ffmpeg/builds/ から zip を取得し、展開した `bin` ディレクトリを PATH に追加
- macOS: `brew install ffmpeg`
- Linux: `sudo apt install ffmpeg` など各ディストリビューションのパッケージマネージャを利用

## セットアップ
```bash
python -m venv .venv
. .venv/bin/activate   # Windows PowerShell の場合は .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
依存関係は標準ライブラリのみですが、開発時の明示性のため `requirements.txt` を配置しています。

## 使い方
### GUI モード（デフォルト）
```bash
python main.py
```
1. 入力フォルダ（INPUT_DIR）と出力フォルダ（OUTPUT_DIR）を選択。入力フォルダを指定すると画面中央の「対象音声プレビュー」に対象ファイル一覧が即時表示される
2. LUFS / True Peak / LRA、出力形式を設定（メタデータは all 固定）
   - 並列実行数（既定: `min(4, CPU論理コア数)`）を必要に応じて調整
3. 必要に応じてチェックを切り替える
   - 「処理済みでも再実行する」…履歴に存在するファイルも再処理（既定オフ）
   - 「サブフォルダも対象にする」…INPUT_DIR 配下を再帰的に探索（既定オン）。オフにすると直下ファイルのみを扱う
4. プレビューの内容を確認し、「正規化を開始」を押すと履歴を考慮した対象リストがモーダルダイアログで再表示される。問題なければ「実行」で続行、「キャンセル」で中断
5. 実行後はバックグラウンドスレッドで処理が走り、ログ欄に進捗・ffmpeg コマンド・成功/失敗が出力される

### CLI モード
```bash
python main.py --cli --input <INPUT_DIR> --output <OUTPUT_DIR> [--lufs -18.0] [--true-peak -1.5] [--lra 16] [--output-format mp3] [--force] [--workers 4]
```
- `--force` を省略すると過去に処理済みのファイルは `processed_history.json` の記録にもとづきスキップされます
- `--workers` は並列実行数です（既定: `min(4, CPU論理コア数)`）
- `--workers` は `1` 以上を指定してください。上限の目安は `CPU論理コア数` です
- `--output-format` は `mp3 / aac / flac / wav / ogg` を指定可能
- 戻り値 0: すべて成功 / 1: 入力エラーまたは対象なし / 2: 一部失敗

### 並列実行数の目安
- 上限目安: `CPU論理コア数`
- 既定値: `min(4, CPU論理コア数)`（多くの環境で安定しやすい）
- 推奨レンジ:
  - SSD: `4〜8`
  - 他作業しながら利用: `2〜4`
  - HDD: `2〜4`（上げすぎると逆に遅くなる場合あり）

## ディレクトリ構成
```
mp3-normalizer/
├─ main.py          # エントリーポイント
├─ gui.py           # Tkinter GUI
├─ processor.py     # ffmpeg を呼び出す処理層
├─ utils.py         # 定数・ユーティリティ
├─ requirements.txt
├─ README.md
```

## 入出力仕様
- INPUT_DIR 配下のサブフォルダも含めて `.mp3 / .m4a / .aac / .flac / .wav / .ogg` を再帰的に探索
- OUTPUT_DIR 直下に INPUT_DIR と同じフォルダ構造で出力
- 既に同名ファイルが存在する場合は `_1`, `_2`, ... のサフィックスを付与
- メタデータは `-map_metadata 0` で全コピー（all 固定）

## LUFS / True Peak 仕様
- loudnorm は 2pass を使用し、1passで測定値を取得して2passで本処理
- 2pass では `linear=true`（既定）を適用
- LRA は既定 `16`（GUI/CLI で変更可能）
- True Peak は既定 `-1.5 dBFS`。CLI/GUI から変更可能
- 出力 codec/品質は出力形式に応じて自動選択（CLI で `--audio-codec` / `--audio-quality` / `--audio-bitrate` で上書き可）

## サブフォルダ処理仕様
- `Path.rglob('*')` で対象を列挙し、各ファイルの相対パスを維持して OUTPUT_DIR に複製
- 空フォルダは作成されません
- GUI では「サブフォルダも対象にする」チェックを外すと INPUT_DIR 直下のみを対象とし、CLI/Processor 層は従来通り再帰探索を既定とします

## 同名衝突時の挙動
- `generate_unique_output_path` で存在確認を行い、`sample.mp3` と衝突した場合は `sample_1.mp3` のように連番を付与

## 処理済みファイルの扱い
- `processed_history.json` に入力ファイルの相対パスと (サイズ, 更新時刻) を記録し、同じファイルを二重に処理しない
- 履歴は GUI/CLI どちらからの実行でも共有し、ログにも「スキップ」メッセージを残す
- 実行開始時にログ（`mp3_normalizer.log`）の履歴を参照し、「今回処理する件数 / 対象件数 / スキップ予定件数」を必ず表示する
- 再度音量調整したい場合は GUI のチェックボックス、または CLI の `--force` オプションを利用するか、履歴ファイルを削除して再生成する

## ログ仕様
- `mp3_normalizer.log` にタイムスタンプ付きで以下を記録
  - ファイル名
  - LUFS 目標値
  - 実行した ffmpeg 1pass/2pass コマンド全文
  - ffmpeg stderr（デバッグレベル）
  - 成功/失敗およびエラー内容
- GUI のログ欄にも同一メッセージを表示

## トラブルシューティング
- `ffmpeg が見つかりません`: PATH に ffmpeg のディレクトリが含まれているか確認
- `処理対象となる音声ファイルが見つかりませんでした`: 入力ディレクトリ配下に対応拡張子ファイルが存在するか確認
- 変換に失敗したファイルがある場合も処理は継続し、最後に成功件数 / 失敗件数をログで通知

## 実行時に生成されるファイル
- `processed_history.json` と `mp3_normalizer.log` は実行時のカレントディレクトリに作成されます

## ライセンス
本リポジトリのライセンスは [LICENSE](LICENSE) を参照してください。

## 変更履歴
### 2026-03-04
- 不具合修正:
  - `ImportError: cannot import name 'AudioProcessorx'` を修正（`main.py` の誤インポートを `AudioProcessor` に訂正）
  - `UnicodeDecodeError: 'cp932' codec can't decode byte 0x83 ...` を修正（`ffmpeg` 出力を安全デコード）
- 機能追加:
  - 並列処理を導入（`ThreadPoolExecutor`）
  - GUI に「並列実行数」入力を追加
  - CLI に `--workers` オプションを追加
- 実測結果（145件, `.mp3`）:
  - Before（直列）: `670秒`（11分10秒, 終了コード `0`）
  - After（並列4）: `207.3秒`（3分27.3秒, 終了コード `0`）
  - 短縮: `462.7秒`（`69.1%`短縮, 約`3.23倍`高速）


## 修正内容（2026-03-07）
- mp3以外の拡張子にも対応（`.mp3 / .m4a / .aac / .flac / .wav / .ogg`）
- 出力形式を可変化（`mp3 / aac / flac / wav / ogg`）
- loudnorm を 1pass から 2pass へ変更（1pass測定 + 2pass本処理）
- `linear=true` を既定化し、LRA を可変化（既定 `16`）
- ffmpeg 実行のエラー処理を強化（終了コード判定・stderr記録）
- 既定のチェックは `.mp3 / .aac / .flac / .wav` をオン
- 入力ビットレートを検出し、MP3/AAC/OGG 出力時は可能な限り同等ビットレートを維持するよう変更
