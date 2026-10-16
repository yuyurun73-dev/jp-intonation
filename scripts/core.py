"""日本語イントネーション検査の本体。音を聞く代わりに F0（声の高さ）を測り、正しいアクセントと照合する。

判定は3系統:
  1. アクセント  pyopenjtalk の正しい高低（下がり目の位置）と、parselmouth で測った F0 を拍ごとに照合
  2. 聞こえ方    Scribe の文字起こしを読み（カナ）に直し、正しい読みと突き合わせる（読み間違い）
  3. 変な区切り  文字起こしが語の途中に「、」を入れた所（ひらがな化で崩れた時に必ず出る）
ほかに 変な間（句読点以外の無音）と 語尾上がり も見る。

校正（2026-10-16）:
  - 全ひらがな版 vs 漢字版 3組で、減点は必ずひらがな版が多い
  - 人が聞いて「変」と言った箇所（下がり目より前で落ちる型）を 強 で検出
  - 既知の誤検知: 複合語は辞書が後ろの語を別の句として扱うので出る → ok_words で外す
"""
import difflib, json, os, re, statistics, unicodedata

import numpy as np
import parselmouth
import contextlib, io
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    import pyopenjtalk  # ONNX Runtime 未導入の警告を黙らせる（使わない機能）
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.family"] = ["Hiragino Sans", "Hiragino Maru Gothic Pro", "sans-serif"]

NUCLEUS_FALL = 1.0      # 下がり目（アクセント核）の直後でこの半音以上は下がっているべき
FLAT_FALL = 3.0         # 平らな語・下がり目より前で、この半音以上ガクッと下がったら不自然
STRONG_FALL = 5.0       # ↑のうちこれ以上は「強」（はっきり違って聞こえる）
STRONG = 1.5            # 下がるべき所で逆にこれ以上上がったら「強」
PENALTY = {"変な区切り": 5, "聞こえ方": 5, "強": 3, "弱": 1, "変な間": 1, "語尾上がり": 2}
END_RISE_MAX = 2.5      # 文末が直前より何半音上がったら「語尾上がり」（疑問文は除く）
PAUSE_MIN = 0.30        # 句読点以外の位置でこの秒数以上の無音は「変な間」
PUNCT = set("、。，．！？!?「」『』（）()・…　 \n")
VOWEL_END = set("aiueoAIUEO") | {"N", "cl"}
PITCH_RANGE = {"female": (120, 600), "male": (60, 350)}


# ---------- 正しいアクセント（pyopenjtalk） ----------
def moras_from_labels(labels):
    out, cur, phrase = [], [], -1
    for lab in labels:
        p = re.search(r"-(.+?)\+", lab).group(1)
        if p in ("sil", "pau"):
            cur = []
            continue
        a = re.search(r"/A:(-?\d+|xx)\+(\d+|xx)\+(\d+|xx)/", lab)
        f = re.search(r"/F:(\d+|xx)_(\d+|xx)", lab)
        cur.append(p)
        if p in VOWEL_END:
            a2 = int(a.group(2)); n = int(f.group(2))
            if a2 == 1:
                phrase += 1
            if n == 1:
                hl = "H" if a2 == 1 else "L"
            elif n == 0:
                hl = "L" if a2 == 1 else "H"
            else:
                hl = "L" if a2 == 1 or a2 > n else "H"
            out.append({"ph": "".join(cur).lower(), "hl": hl, "phrase": phrase, "acc": n, "pos": a2 - 1})
            cur = []
    return out


def analyze_text(text):
    njd = pyopenjtalk.run_frontend(text)
    return njd, moras_from_labels(pyopenjtalk.make_label(njd))


def norm(s):
    return unicodedata.normalize("NFKC", s)


def mora_times(njd, moras, align):
    """文字アライメント（ElevenLabs）→ 各拍の時刻。語の中は拍数で等分する"""
    chars = align["characters"]; st = align["character_start_times_seconds"]; en = align["character_end_times_seconds"]
    ntext = "".join(norm(c) for c in chars)
    pos2idx = []
    for i, c in enumerate(chars):
        pos2idx += [i] * len(norm(c))
    ptr, mi = 0, 0
    for node in njd:
        ms = node["mora_size"]
        s = norm(node["string"])
        k = ntext.find(s, ptr) if s else -1
        if k >= 0 and k - ptr <= 3:
            i0, i1 = pos2idx[k], pos2idx[k + len(s) - 1]
            t0, t1 = st[i0], en[i1]
            ptr = k + len(s)
            for j in range(ms):
                if mi + j < len(moras):
                    moras[mi + j]["t"] = (t0 + (t1 - t0) * j / ms, t0 + (t1 - t0) * (j + 1) / ms)
                    moras[mi + j]["chars"] = (i0, i1)
        mi += ms
    return moras


# ---------- 句ごとの判定 ----------
def judge_phrase(ms):
    """耳で一番目立つ「どこで下がるか」だけを見る。平板の1拍目の低さは自然発話で消えるので見ない。
    戻り値: None=判定不能 / (True, "") / (False, 理由)"""
    L = len(ms); n = ms[0]["racc"]
    f = [m["f0"] for m in ms]
    if L < 3 or sum(x is not None for x in f) < 3:
        return None
    drops = []
    for i in range(L):
        for j in range(i + 1, min(L, i + 3)):
            if f[i] is not None and f[j] is not None:
                drops.append((f[i] - f[j], i, j)); break
    if 1 <= n < L:
        # 下がり目より前の落ち込み（例: 3拍目で下がるべき語が2拍目で落ちる）。後ろの拍が無声化で測れなくても判定する
        early = [x for x in drops if x[0] >= FLAT_FALL and x[1] >= 1 and x[2] <= n - 1]
        if early:
            top = max(early)
            return False, ("強" if top[0] >= STRONG_FALL - 0.5 else "弱") + f"・下がるのが早い（{top[1] + 1}拍目のあと{top[0]:.1f}半音）"
        hs = [f[i] for i in range(max(0, n - 2), n) if f[i] is not None]
        ls = [f[i] for i in range(n, min(L, n + 2)) if f[i] is not None]
        if not hs or not ls:
            return True, ""
        d = max(hs) - min(ls)
        if d < NUCLEUS_FALL:
            return False, ("強" if d <= -STRONG else "弱") + f"・{n}拍目のあとで下がるはずが下がっていない（{d:+.1f}半音）"
        return True, ""
    mid = [d for d in drops if d[0] >= FLAT_FALL and d[1] >= 1]
    if mid:
        top = max(mid)
        return False, ("強" if top[0] >= STRONG_FALL else "弱") + f"・平らに読むはずが途中で下がる（{top[1] + 1}拍目のあと{top[0]:.1f}半音）"
    return True, ""


# ---------- 文字起こし照合 ----------
def kana_stream(text):
    ks, bounds, puncts = [], set(), set()
    for node in pyopenjtalk.run_frontend(text):
        p = node["pron"].replace("’", "")
        if node["mora_size"] == 0:
            if p in ("、", "。", "？", "！"):
                puncts.add(len(ks))
            continue
        if node["chain_flag"] != 1:
            bounds.add(len(ks))
        ks += list(p)
    bounds.add(len(ks))
    return ks, bounds, puncts


def transcript_check(heard, ref):
    # 文字起こし側の表記ゆれで誤検知しないよう寄せる
    heard = heard.replace("行って", "行なって").replace("ゼロ", "零")
    rk, rbounds, rpunct = kana_stream(ref)
    hk, _, hpunct = kana_stream(heard)
    sm = difflib.SequenceMatcher(a=rk, b=hk, autojunk=False)
    out = []
    for tag, a0, a1, b0, b1 in sm.get_opcodes():
        if tag != "equal" and max(a1 - a0, b1 - b0) >= 2:
            out.append({"kind": "聞こえ方", "t": -1, "t1": -1, "word": "".join(rk[max(0, a0 - 4):a1 + 4]),
                        "detail": f"正しい読み「{''.join(rk[a0:a1])}」→ 聞こえた「{''.join(hk[b0:b1])}」"})
    b2a = {}
    for blk in sm.get_matching_blocks():
        for k in range(blk.size + 1):
            b2a[blk.b + k] = blk.a + k
    for hp in hpunct:
        ap = b2a.get(hp)
        if ap is not None and ap not in rbounds and ap not in rpunct:
            out.append({"kind": "変な区切り", "t": -1, "t1": -1,
                        "word": "".join(rk[max(0, ap - 6):ap]) + "｜" + "".join(rk[ap:ap + 6]),
                        "detail": "語の途中で区切って読んでいる"})
    return out


# ---------- F0 ----------
def f0_track(audio, voice="female"):
    lo, hi = PITCH_RANGE.get(voice, PITCH_RANGE["female"])
    snd = parselmouth.Sound(audio)
    pitch = snd.to_pitch(time_step=0.01, pitch_floor=lo, pitch_ceiling=hi)
    t = pitch.xs(); f = pitch.selected_array["frequency"]
    st = np.where(f > 0, 12 * np.log2(np.maximum(f, 1) / 100.0), np.nan)
    inten = snd.to_intensity(time_step=0.01)
    return t, st, inten.xs(), inten.values[0], snd.duration


def mora_f0(t, st, span):
    a, b = span; m = (b - a) * 0.2
    sel = st[(t >= a + m) & (t <= b - m)]
    sel = sel[~np.isnan(sel)]
    return float(np.median(sel)) if len(sel) >= 2 else None


# ---------- 検査本体 ----------
def check(line, audio, align, transcript=None, png=None, voice="female"):
    """line = {id, text, ref?, ok_words?}  audio=mp3パス  align=文字アライメントdict  transcript=文字起こし"""
    text = line["text"]; ref = line.get("ref", text)
    njd, sp = analyze_text(text)
    sp = mora_times(njd, sp, align)
    _, rf = analyze_text(ref)
    sm = difflib.SequenceMatcher(a=[m["ph"] for m in rf], b=[m["ph"] for m in sp], autojunk=False)
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            for key in ("hl", "phrase", "acc", "pos"):
                sp[blk.b + k]["r" + key] = rf[blk.a + k][key]
            sp[blk.b + k]["exp"] = rf[blk.a + k]["hl"]
    t, st, it, iv, dur = f0_track(audio, voice)
    for m in sp:
        m["f0"] = mora_f0(t, st, m["t"]) if "t" in m else None

    chars = align["characters"]
    issues, phrases, scores = [], {}, []
    for m in sp:
        if "rphrase" in m:
            phrases.setdefault(m["rphrase"], []).append(m)
    for ms in phrases.values():
        r = judge_phrase(ms)
        if r is None:
            continue
        ok, detail = r
        scores.append(ok)
        if ok:
            continue
        seg = [m for m in ms if "chars" in m]
        if not seg:
            continue
        i0, i1 = seg[0]["chars"][0], seg[-1]["chars"][1]
        word = "".join(chars[i0:i1 + 1])
        if word in line.get("ok_words", []):
            continue
        issues.append({"kind": "アクセント", "t": seg[0]["t"][0], "t1": seg[-1]["t"][1], "word": word,
                       "detail": f"{detail}（正しい={''.join(m['exp'] for m in ms)}）"})
    if transcript:
        issues += transcript_check(transcript, ref)

    timed = [m for m in sp if "t" in m and m["f0"] is not None]
    for i, c in enumerate(chars):
        if c == "。" and i > 0 and chars[i - 1] not in "か？?":
            before = [m for m in timed if i - 12 <= m["chars"][1] < i]
            if len(before) >= 5:
                last = statistics.mean([m["f0"] for m in before[-2:]])
                prev = statistics.mean([m["f0"] for m in before[-6:-2]])
                if last - prev > END_RISE_MAX:
                    issues.append({"kind": "語尾上がり", "t": before[-2]["t"][0], "t1": before[-1]["t"][1],
                                   "word": "".join(chars[max(0, i - 8):i + 1]), "detail": f"文末が+{last - prev:.1f}半音"})

    silent = iv < np.nanmax(iv) - 25
    st_c = align["character_start_times_seconds"]
    k = 0
    while k < len(silent):
        if not silent[k]:
            k += 1; continue
        j = k
        while j < len(silent) and silent[j]:
            j += 1
        a, b = it[k], it[min(j, len(it) - 1)]
        if b - a >= PAUSE_MIN and a > 0.3 and b < dur - 0.3:
            idx = max([i for i, s in enumerate(st_c) if s <= a] or [0])
            if not any(ch in PUNCT for ch in chars[max(0, idx - 2):idx + 4]):
                issues.append({"kind": "変な間", "t": a, "t1": b, "word": "".join(chars[max(0, idx - 6):idx + 5]),
                               "detail": f"{b - a:.2f}秒の無音"})
        k = j

    issues.sort(key=lambda x: (x["t"] >= 0, x["t"]))
    penalty = sum(PENALTY["強" if i["detail"].startswith("強") else "弱"] if i["kind"] == "アクセント"
                  else PENALTY.get(i["kind"], 1) for i in issues)
    if png:
        plot(png, line["id"], t, st, sp, chars, align, issues, dur)
    return {"id": line["id"], "phrases": len(scores), "ok": sum(scores), "penalty": penalty,
            "rate": sum(scores) / len(scores) if scores else 0.0, "issues": issues, "duration": dur}


def plot(png, title, t, st, sp, chars, align, issues, dur):
    row = 8.0
    rows = max(1, int(np.ceil(dur / row)))
    fig, axes = plt.subplots(rows, 1, figsize=(18, 2.6 * rows), squeeze=False)
    valid = st[~np.isnan(st)]
    lo, hi = (np.percentile(valid, 2) - 2, np.percentile(valid, 98) + 3) if len(valid) else (0, 20)
    for r in range(rows):
        ax = axes[r][0]; a, b = r * row, (r + 1) * row
        for iss in issues:
            if iss["t"] >= 0 and iss["t1"] >= a and iss["t"] <= b:
                ax.axvspan(iss["t"], iss["t1"], color="#ffe14d", alpha=0.55, zorder=0)
        for m in sp:
            if "t" not in m or m["t"][1] < a or m["t"][0] > b:
                continue
            if "exp" in m:
                y = hi - 0.8 if m["exp"] == "H" else hi - 1.6
                ax.plot(m["t"], [y, y], color="#d62728" if m["exp"] == "H" else "#1f77b4", lw=4, solid_capstyle="butt")
            if m.get("f0") is not None:
                ax.plot(m["t"], [m["f0"], m["f0"]], color="black", lw=2)
        sel = (t >= a) & (t <= b)
        ax.plot(t[sel], st[sel], ".", ms=2, color="#888")
        for i, c in enumerate(chars):
            s = align["character_start_times_seconds"][i]
            if a <= s < b and c.strip():
                ax.text(s, lo + 0.3, c, fontsize=10)
        ax.set_xlim(a, b); ax.set_ylim(lo, hi); ax.set_yticks([])
    fig.suptitle(f"{title}  灰=声の高さ / 黒=音ごとの高さ / 上の帯 赤=高くあるべき 青=低くあるべき / 黄=指摘", fontsize=12)
    fig.tight_layout()
    fig.savefig(png, dpi=70)
    plt.close(fig)


def report_md(results):
    out = ["# イントネーション検査", "", "| 行 | 合格句/検査句 | 合格率 | 指摘数 | 減点 |", "|---|---|---|---|---|"]
    det = []
    for r in results:
        out.append(f"| {r['id']} | {r['ok']}/{r['phrases']} | {r['rate']:.0%} | {len(r['issues'])} | {r['penalty']} |")
        det.append(f"\n## {r['id']}")
        for i in r["issues"]:
            when = f"{i['t']:.1f}秒" if i["t"] >= 0 else "全体"
            det.append(f"- {when} [{i['kind']}] 「{i['word']}」 {i['detail']}")
    return "\n".join(out + det) + "\n"
