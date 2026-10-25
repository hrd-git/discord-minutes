# Discord 輪読会 文字起こしBot(検証用)

VCの音声を**ユーザー別**に録音し、faster-whisperで文字起こしして、Meetのような時系列の発言録にします。

```
/rec_start → ユーザー別wav(録音開始からの連続した時間軸) → minutes-transcribe → transcript.txt
```

## フォルダ構成

```
discord-minutes/
├─ src/discord_minutes/
│   ├─ bot.py           スラッシュコマンド(/rec_start, /rec_stop)と起動処理
│   ├─ recorder.py      ユーザー別に wav を書き出す録音処理
│   ├─ pycord_patch.py  py-cord 2.8.x の音声受信バグの修正
│   ├─ transcribe.py    Whisper で文字起こし
│   └─ config.py        .env の読み込みとパスの基準
├─ models/        Whisper のモデル(自動ダウンロード、git 管理外)
├─ recordings/    録音データ(git 管理外)
├─ pyproject.toml / poetry.lock
└─ .env           トークンなどの設定(git 管理外)
```

`.env` の `OUT_DIR` などの相対パスは、どのフォルダから実行してもこのプロジェクトのフォルダ基準になります。

## 1. セットアップ(Windows 11 / Python 3.13 / Poetry)

```powershell
pyenv local 3.13.17                          # このフォルダの .python-version に記録される
poetry config virtualenvs.in-project true
poetry env use 3.13
poetry install                               # Bot 用の依存だけ(軽い)
copy .env.example .env                       # DISCORD_TOKEN を記入
```

- Whisper 関連(faster-whisper、CUDA ライブラリ。約1GB超)は `asr` グループに分けてあり、`poetry install --with asr` で入れます
- Python 3.14 は onnxruntime / av / PyNaCl / davey の対応が未確認なので使いません

## 2. Discord側の準備

### 最初の1回だけ: アプリを作る

[Developer Portal](https://discord.com/developers/applications) でアプリを作成 → Bot タブでトークンを発行し、`.env` の `DISCORD_TOKEN` に記入します(トークンは他人に見せない)。

### サーバーに Bot を追加する(サーバーを増やすときも同じ手順)

作業する人は、そのサーバーで「サーバー管理」権限が必要です。

1. **招待 URL を作る**: Developer Portal でアプリを開き、OAuth2 → OAuth2 URL Generator で次を選んで、一番下の Generated URL をコピー
   - SCOPES: `bot`、`applications.commands`
   - BOT PERMISSIONS: `View Channels`、`Send Messages`、`Attach Files`(文字起こしの投稿に使う)、`Connect`
   - Integration Type: `Guild Install`
2. **招待する**: URL をブラウザで開き、追加先のサーバーを選んで認証。メンバー一覧に Bot が出れば成功
3. **サーバー ID を調べる**: Discord のユーザー設定 → 詳細設定 →「開発者モード」をオンにし、サーバーのアイコンを右クリック →「サーバー ID をコピー」
4. **`.env` に追加する**: `GUILD_ID` にカンマ区切りで追記(例: `GUILD_ID=111,222`)。空にすると招待したすべてのサーバーで使えますが、コマンドの初回の表示に時間がかかることがあります
5. **Bot を再起動する**: Bot のウィンドウを閉じて `start_bot.bat` を実行し直す
6. **動作確認**: そのサーバーのボイスチャンネルに入り、`/rec_start` → 少し話す → `/rec_stop`。文字起こしが投稿されれば完了

招待 URL に `Attach Files` を入れ忘れた場合は、サーバー設定 → ロール →「文字起こし」ロール → 権限で「ファイルを添付」をオンにしてください。チャンネル単位で添付が禁止されている場合は、そのチャンネルの権限設定も確認してください。

新しいサーバーの参加者にも、「5. 運用上の注意」の Discord の音声設定をしてもらってください。

## 3. 録音

```powershell
poetry run minutes-bot
```

エクスプローラーから `start_bot.bat` をダブルクリックしても起動できます(同じ処理です)。

`logged in as ...` と表示されたら準備完了です。Bot は起動している間だけ `/rec_start` を受け付けます(Discord からは起動できません)。終了は Ctrl+C です。同じ Bot を2つ同時に起動しないでください。

1. VCに入り、`/rec_start` を実行
2. 話し終わったら `/rec_stop` を実行
3. `recordings\<日時>\` に `<ユーザーID>.wav` と `meta.json` ができます
4. 続けて Bot が自動で文字起こしを実行し、終わったら `/rec_stop` を実行したチャンネルに `transcript.txt` を添付して投稿します(録音1時間あたり十数分が目安。その間 Bot を止めないでください)。失敗したときの詳細は `recordings\<日時>\transcribe.log` にあります

自動で文字起こしをしない場合は `.env` に `AUTO_TRANSCRIBE=0` を書きます。

py-cord 2.8.1 は `/rec_start` と `/rec_stop` のたびに「Voice reception is currently broken due to Discord's DAVE ...」という RuntimeWarning を出します。受信側の DAVE 復号は `pycord_patch.py` で対応済みなので無視して構いません。

`/rec_stop` で「音声を1パケットも取得できませんでした」と出た場合は、DAVE(音声E2EE)の復号に失敗しています。コンソールの `[sink] first packet` の行とエラーログを確認してください。

## 4. 手動で文字起こしする

```powershell
poetry run minutes-transcribe recordings\20261023_130000
```

- 初回はWhisperのモデル(large-v3、約3GB)が `models\` にダウンロードされます。ダウンロードが止まる場合は `$env:HF_HUB_DISABLE_XET=1` を設定してから実行してください
- GPUのメモリ不足になる場合は `--model medium` を指定してください
- GPUで動かない場合は自動でCPU(smallモデル)に切り替わります。CPUでも使いたいモデルがあれば `--cpu-model medium`
- 本のタイトルや専門用語は `--prompt "題名や用語をここに書く"` で認識精度が上がります

## 5. 運用上の注意

- **参加者の Discord 設定(重要):** ユーザー設定 →「音声・ビデオ」で、「入力感度」の自動調整を**オフ**にしてスライダーを**一番左**(常に送信)にし、「ノイズ抑制」を**なし**にしてもらってください。自動調整のままだと、Discord が声の一部を「話していない」と判定して送信しないため、録音が途切れます(Bot 側では補えません)。無音の間も送信されますが、文字起こしは無音を自動で飛ばすので問題ありません
- py-cord 2.8.1 の受信処理にはバグが多いため、`pycord_patch.py` で実行時に差し替えています。`/rec_stop` のときに `[rec] receive stats {...}` が表示され、`dave_fail` や `lost` が多い場合は受信に問題があります
- 録音前に参加者へ告知し、同意を得てください。`/rec_start` はチャンネルに録音中のメッセージを投稿します
- スピーカーで聞いている参加者がいると、他の人の声がその人のマイクにも入って二重に文字起こしされます。ヘッドホンの利用を推奨します
- wavは1時間・1人あたり約115MBです
