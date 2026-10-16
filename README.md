# jp-intonation

日本語TTS（ElevenLabs）のナレーションについて、イントネーション・読み間違い・変な区切りを自動で判定する Claude Code スキルです。

人が全部を聞いて確かめる代わりに、次の3つで判定します。
- 声の高さ（F0）を測り、日本語のアクセント辞書（pyopenjtalk）と照らし合わせる
- 生成した音声を ElevenLabs の文字起こし（Scribe）にかけ、読みを突き合わせる
- 1行につき最大3回生成し、一番自然な回を自動で採用する

## 入れ方

```bash
git clone https://github.com/yuyurun73-dev/jp-intonation ~/.claude/skills/jp-intonation
mkdir -p ~/.config/elevenlabs && printf '%s' '<ElevenLabsのAPIキー>' > ~/.config/elevenlabs/key   # または環境変数 ELEVENLABS_API_KEY
```

必要なもの：macOS、`uv`、`ffmpeg`。Python の依存（parselmouth / pyopenjtalk-plus / numpy / matplotlib）は、初回実行時に `~/.local/share/jp-intonation/venv` へ自動で入ります。

Claude Code で「ナレーション作って」「イントネーションがおかしい」などと言うと、このスキルが使われます。

## 使い方（コマンド）

```bash
S=~/.claude/skills/jp-intonation/scripts/jpi
$S narrate  案件/lines.json              # 生成 → 検査 → 一番自然な回を audio/<id>.mp3 に採用
$S check    案件/lines.json              # 生成済みの音声を検査だけする（作り直さない）
$S check-file 音声.mp3 --text-file 台本.txt   # 他の工程で作った音声を検査する
$S variants 案件/lines.json <id> "書き方A" "書き方B"   # 崩れる語の書き方を比べる
```

lines.json の例：

```json
{"voice": "<voice_id>", "model": "eleven_v3", "takes": 3, "voice_type": "female",
 "lines": [{"id": "s01", "text": "店は、はなてん駅の近くにあります。", "ref": "店は、放出駅の近くにあります。"}]}
```

- `text`：実際に読ませる文
- `ref`：正しいアクセントを出すための漢字表記（`text` を読み対策で変えた時だけ入れる）
- `ok_words`：確認済みの誤検知として、指摘から外す語

出力は lines.json と同じフォルダに作られます。
- `qc/report.md`：指摘の一覧
- `qc/<id>.png`：声の高さの図（赤＝高くあるべき音、青＝低くあるべき音、黄＝指摘箇所）

判定基準と運用ルールの詳細は [SKILL.md](SKILL.md) にあります。
