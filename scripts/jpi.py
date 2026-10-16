#!/usr/bin/env python3
"""jp-intonation CLI（直接呼ばず scripts/jpi 経由で。venv を自動で用意する）

  jpi narrate  <lines.json> [id ...]          生成（最大takes回）→ 文字起こし → 検査 → 減点最小を audio/<id>.mp3 に採用
  jpi check    <lines.json> [id ...]          audio/<id>.mp3 を検査だけ（タイミングが無ければ forced-alignment で後付け）
  jpi check-file <音声> --text "..." [--ref "..."] [--voice-type male]   単発の音声を検査（VSL・広告ナレ等）
  jpi variants <lines.json> <id> "<書き方1>" "<書き方2>" ...   崩れる語の書き方を比べる（ref は元の行のまま）

lines.json:
  {"voice": "<voice_id>", "model": "eleven_v3", "takes": 3, "voice_type": "female",
   "lines": [{"id": "s01", "text": "読ませる文", "ref": "正しいアクセント用の漢字表記(省略可)", "ok_words": ["確認済み誤検知"]}]}
  ※ 旧形式（[{"file":..., "text":...}] の配列）も読める。その場合 voice/model は --voice/--model で渡す
出力: lines.json と同じフォルダの audio/ と qc/（qc/report.md・qc/<id>.png）
"""
import argparse, base64, json, os, shutil, sys, urllib.error, urllib.request, uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

API = "https://api.elevenlabs.io/v1"


def api_key():
    k = os.environ.get("ELEVENLABS_API_KEY")
    if k:
        return k.strip()
    return open(os.path.expanduser("~/.config/elevenlabs/key")).read().strip()


def _post(url, data, ctype):
    req = urllib.request.Request(url, data=data, headers={"xi-api-key": api_key(), "Content-Type": ctype})
    try:
        return json.load(urllib.request.urlopen(req, timeout=300))
    except urllib.error.HTTPError as e:
        sys.exit(f"ElevenLabs エラー {e.code}: {e.read().decode()[:300]}")


def _multipart(fields, audio_path):
    b = uuid.uuid4().hex
    body = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items())
    body += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="a.mp3"\r\n'
             f'Content-Type: audio/mpeg\r\n\r\n').encode() + open(audio_path, "rb").read() + f"\r\n--{b}--\r\n".encode()
    return body, f"multipart/form-data; boundary={b}"


def tts(text, voice, model, mp3):
    d = _post(f"{API}/text-to-speech/{voice}/with-timestamps?output_format=mp3_44100_128",
              json.dumps({"text": text, "model_id": model, "language_code": "ja"}).encode(), "application/json")
    open(mp3, "wb").write(base64.b64decode(d["audio_base64"]))
    json.dump(d["alignment"], open(mp3[:-4] + ".align.json", "w"), ensure_ascii=False)
    return d["alignment"]


def transcribe(audio):
    body, ct = _multipart({"model_id": "scribe_v1", "language_code": "jpn"}, audio)
    return _post(f"{API}/speech-to-text", body, ct)["text"]


def force_align(audio, text):
    body, ct = _multipart({"text": text}, audio)
    ch = _post(f"{API}/forced-alignment", body, ct)["characters"]
    return {"characters": [c["text"] for c in ch],
            "character_start_times_seconds": [c["start"] for c in ch],
            "character_end_times_seconds": [c["end"] for c in ch]}


# ---------- プロジェクト ----------
class Project:
    def __init__(self, path, voice=None, model=None):
        self.path = os.path.abspath(path)
        self.dir = os.path.dirname(self.path)
        d = json.load(open(self.path))
        if isinstance(d, list):
            d = {"lines": d}
        self.lines = [{**l, "id": l.get("id") or l["file"]} for l in d["lines"]]
        self.voice = voice or d.get("voice")
        self.model = model or d.get("model", "eleven_v3")
        self.takes = int(d.get("takes", 3))
        self.voice_type = d.get("voice_type", "female")
        self.audio = os.path.join(self.dir, d.get("audio_dir", "audio"))
        self.qc = os.path.join(self.dir, "qc")
        os.makedirs(os.path.join(self.audio, "takes"), exist_ok=True)
        os.makedirs(self.qc, exist_ok=True)

    def pick(self, ids):
        return [l for l in self.lines if not ids or l["id"] in ids]

    def run_check(self, line, mp3, name):
        align_p = mp3[:-4] + ".align.json"
        if not os.path.exists(align_p):
            json.dump(force_align(mp3, line["text"]), open(align_p, "w"), ensure_ascii=False)
        txt_p = os.path.join(self.qc, name + ".txt")
        if not os.path.exists(txt_p) or os.path.getmtime(txt_p) < os.path.getmtime(mp3):
            open(txt_p, "w").write(transcribe(mp3))
        return core.check({**line, "id": name}, mp3, json.load(open(align_p)), open(txt_p).read(),
                          png=os.path.join(self.qc, name + ".png"), voice=self.voice_type)


def summarize(r):
    bad = [i for i in r["issues"] if i["kind"] != "アクセント" or i["detail"].startswith("強")]
    return f"減点{r['penalty']:>3}  " + " / ".join(f"{i['kind']}「{i['word']}」" for i in bad)


def best_take(p, line, takes, tag=None):
    name0 = tag or line["id"]
    results = []
    for k in range(1, takes + 1):
        name = f"{name0}_t{k}"
        mp3 = os.path.join(p.audio, "takes", name + ".mp3")
        tts(line["text"], p.voice, p.model, mp3)
        r = p.run_check(line, mp3, name)
        print(f"  {name}: {summarize(r)}", flush=True)
        results.append((r["penalty"], k, name, r))
        if r["penalty"] == 0:
            break
    return min(results, key=lambda x: (x[0], x[1]))


def cmd_narrate(a):
    p = Project(a.lines, a.voice, a.model)
    if not p.voice:
        sys.exit("voice が未指定（lines.json の voice か --voice）")
    takes = a.takes or p.takes
    results = []
    for line in p.pick(a.ids):
        print(f"=== {line['id']}", flush=True)
        _, k, name, r = best_take(p, line, takes)
        for ext in (".mp3", ".align.json"):
            shutil.copy(os.path.join(p.audio, "takes", name + ext), os.path.join(p.audio, line["id"] + ext))
        for ext in (".txt", ".png"):
            shutil.copy(os.path.join(p.qc, name + ext), os.path.join(p.qc, line["id"] + ext))
        print(f"  → 採用 {k}回目", flush=True)
        results.append({**r, "id": line["id"]})
    write_report(p, results)


def cmd_check(a):
    p = Project(a.lines)
    results = []
    for line in p.pick(a.ids):
        mp3 = os.path.join(p.audio, line["id"] + ".mp3")
        if not os.path.exists(mp3):
            print("音声なし:", mp3); continue
        results.append(p.run_check(line, mp3, line["id"]))
    write_report(p, results)


def cmd_check_file(a):
    text = a.text or open(a.text_file).read().strip()
    base = os.path.splitext(os.path.abspath(a.audio))[0]
    align_p = base + ".align.json"
    if not os.path.exists(align_p):
        json.dump(force_align(a.audio, text), open(align_p, "w"), ensure_ascii=False)
    line = {"id": os.path.basename(base), "text": text}
    if a.ref:
        line["ref"] = a.ref
    if a.ok_words:
        line["ok_words"] = a.ok_words.split(",")
    r = core.check(line, a.audio, json.load(open(align_p)), transcribe(a.audio),
                   png=base + ".intonation.png", voice=a.voice_type)
    rep = core.report_md([r])
    open(base + ".intonation.md", "w").write(rep)
    print(rep)
    print("図:", base + ".intonation.png")


def cmd_variants(a):
    p = Project(a.lines, a.voice, a.model)
    base = next(l for l in p.lines if l["id"] == a.id)
    ref = base.get("ref", base["text"])
    takes = a.takes or p.takes
    rows = []
    for i, text in enumerate([base["text"]] + a.texts):
        line = {**base, "text": text, "ref": ref}
        print(f"=== 案{i}: {text[:40]}", flush=True)
        pens = []
        for k in range(1, takes + 1):
            name = f"var_{a.id}_{i}_t{k}"
            mp3 = os.path.join(p.audio, "takes", name + ".mp3")
            tts(text, p.voice, p.model, mp3)
            r = p.run_check(line, mp3, name)
            print(f"  {name}: {summarize(r)}", flush=True)
            pens.append(r["penalty"])
        rows.append((i, text, pens))
    print("\n| 案 | 書き方 | 各回の減点 | 平均 |\n|---|---|---|---|")
    for i, text, pens in rows:
        print(f"| {i} | {text[:30]} | {pens} | {sum(pens) / len(pens):.1f} |")


def write_report(p, results):
    rep = core.report_md(results)
    open(os.path.join(p.qc, "report.md"), "w").write(rep)
    print(rep)
    print("図:", p.qc)


def main():
    ap = argparse.ArgumentParser(description="日本語TTSのイントネーション検査")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("narrate", cmd_narrate), ("check", cmd_check)):
        s = sub.add_parser(name); s.add_argument("lines"); s.add_argument("ids", nargs="*")
        s.add_argument("--voice"); s.add_argument("--model"); s.add_argument("--takes", type=int)
        s.set_defaults(fn=fn)
    s = sub.add_parser("check-file"); s.add_argument("audio")
    s.add_argument("--text"); s.add_argument("--text-file"); s.add_argument("--ref"); s.add_argument("--ok-words")
    s.add_argument("--voice-type", default="female", choices=["female", "male"]); s.set_defaults(fn=cmd_check_file)
    s = sub.add_parser("variants"); s.add_argument("lines"); s.add_argument("id"); s.add_argument("texts", nargs="+")
    s.add_argument("--voice"); s.add_argument("--model"); s.add_argument("--takes", type=int); s.set_defaults(fn=cmd_variants)
    a = ap.parse_args()
    if a.cmd == "check-file" and not (a.text or a.text_file):
        sys.exit("--text か --text-file が必要")
    a.ids = set(getattr(a, "ids", []) or [])
    a.fn(a)


if __name__ == "__main__":
    main()
