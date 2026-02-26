# mp3-normalizer

Tkinter ベースの GUI と CLI の両方で利用できる mp3 LUFS 正規化ツールです。指定ディレクトリ配下の mp3 を再帰的に探索し、ffmpeg の `loudnorm` フィルタ（1pass）で音量をそろえます。

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
1. 入力フォルダ（INPUT_DIR）と出力フォルダ（OUTPUT_DIR）を選択。入力フォルダを指定すると画面中央の「対象mp3プレビュー」に対象ファイル一覧が即時表示される
2. LUFS 目標値と True Peak（既定 -14 LUFS / -1 dBFS）を入力
3. 必要に応じてチェックを切り替える
   - 「処理済みでも再実行する」…履歴に存在する mp3 も再処理（既定オフ）
   - 「サブフォルダも対象にする」…INPUT_DIR 配下を再帰的に探索（既定オン）。オフにすると直下ファイルのみを扱う
4. プレビューの内容を確認し、「正規化を開始」を押すと履歴を考慮した対象 mp3 リストがモーダルダイアログで再表示される。問題なければ「実行」で続行、「キャンセル」で中断
5. 実行後はバックグラウンドスレッドで処理が走り、ログ欄に進捗・ffmpeg コマンド・成功/失敗が出力される

### CLI モード
```bash
python main.py --cli --input <INPUT_DIR> --output <OUTPUT_DIR> [--lufs -14.0] [--true-peak -1.0] [--force]
```
- `--force` を省略すると過去に処理済みのファイルは `processed_history.json` の記録にもとづきスキップされます
- 戻り値 0: すべて成功 / 1: 入力エラーまたは対象なし / 2: 一部失敗

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
- INPUT_DIR 配下のサブフォルダも含めて `.mp3` 拡張子のみ再帰的に探索
- OUTPUT_DIR 直下に INPUT_DIR と同じフォルダ構造で出力
- 既に同名ファイルが存在する場合は `_1`, `_2`, ... のサフィックスを付与
- メタデータは `-map_metadata 0` で保持

## LUFS / True Peak 仕様
- LUFS 目標値は GUI/CLI の入力値を `loudnorm=I=<値>:TP=<値>:LRA=11` に渡して実現
- True Peak は既定で -1.0 dBFS。CLI/GUI から変更可能
- loudnorm は 1pass モードを使用し、ピーク保護のため `libmp3lame -q:a 2` で再エンコード

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
  - 実行した ffmpeg コマンド全文
  - 成功/失敗およびエラー内容
- GUI のログ欄にも同一メッセージを表示

## トラブルシューティング
- `ffmpeg が見つかりません`: PATH に ffmpeg のディレクトリが含まれているか確認
- `処理対象となる mp3 ファイルが見つかりませんでした`: 入力ディレクトリ配下に `.mp3` が存在するか確認
- 変換に失敗したファイルがある場合も処理は継続し、最後に成功件数 / 失敗件数をログで通知

## 実行時に生成されるファイル
- `processed_history.json` と `mp3_normalizer.log` は実行時のカレントディレクトリに作成されます

## 変更履歴
変更履歴は README に記載します。

## ライセンス
本リポジトリのライセンスは [LICENSE](LICENSE) を参照してください。
