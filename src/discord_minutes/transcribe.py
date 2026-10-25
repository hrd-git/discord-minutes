"""録音ディレクトリの wav を文字起こしし、Meet のような時系列の発言録にする。

使い方:
    poetry run minutes-transcribe recordings/20261023_130000
出力:
    <dir>/transcript.txt   [13:00:05] 田中: ...
    <dir>/transcript.json  発言ごとの開始/終了秒つきデータ
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from .config import PROJECT_ROOT

# Whisper のモデル(large-v3 で約3GB)はプロジェクト内に保存する
MODELS_DIR = PROJECT_ROOT / "models"

# Whisper が無音・ノイズ区間で出しがちな定型文(完全一致で除去)
HALLUCINATIONS = {
    "ご視聴ありがとうございました",
    "ご視聴ありがとうございます",
    "最後までご視聴いただきありがとうございました",
    "チャンネル登録お願いします",
    "チャンネル登録よろしくお願いします",
}


def _norm(text: str) -> str:
    return re.sub(r"[\s、。！？!?,.]+", "", text)


def is_hallucination(text: str) -> bool:
    return _norm(text) in {_norm(h) for h in HALLUCINATIONS}


def merge_utterances(utts: list[dict], gap: float = 3.0) -> list[dict]:
    """開始時刻でソートし、同じ人の連続発言(間隔 gap 秒未満)を1つにまとめる。"""
    merged: list[dict] = []
    for u in sorted(utts, key=lambda x: x["start"]):
        last = merged[-1] if merged else None
        if last and last["user"] == u["user"] and u["start"] - last["end"] < gap:
            last["text"] += " " + u["text"]
            last["end"] = max(last["end"], u["end"])
        else:
            merged.append(dict(u))
    return merged


def _add_nvidia_dll_dirs() -> None:
    """pip で入れた nvidia-cublas/cudnn の DLL を Windows の検索パスに追加する。"""
    if os.name != "nt":
        return
    try:
        import nvidia  # type: ignore
    except ImportError:
        return
    for base in getattr(nvidia, "__path__", []):
        for p in Path(base).glob("*/bin"):
            os.add_dll_directory(str(p))
            os.environ["PATH"] = str(p) + os.pathsep + os.environ["PATH"]


def load_normalized(wav: Path, target_peak: float = 0.9, max_gain_db: float = 30.0):
    """16kHz モノラル wav を float32 で読み、ピークが target_peak になるよう音量を上げる。

    Discord の音声は小さめに届くことがあり、そのままだと VAD が声を無音と判定して捨ててしまう。
    無音ばかりのファイルでノイズを増幅しすぎないよう、上げ幅は max_gain_db までにする。
    """
    import wave

    import numpy as np

    with wave.open(str(wav), "rb") as w:
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0
    # 一瞬の大きな音に引っ張られないよう、ピークは上位 0.1% の値で見る
    peak = float(np.quantile(np.abs(a), 0.999)) if len(a) else 0.0
    if peak > 0:
        gain = min(target_peak / peak, 10 ** (max_gain_db / 20))
        if gain > 1:
            print(f"[transcribe]   音量を {20 * np.log10(gain):+.1f} dB 補正")
            a = np.clip(a * gain, -1.0, 1.0)
    return a


def transcribe_dir(rec_dir: Path, meta: dict, model_name: str, device: str,
                   compute_type: str, language: str, prompt: str | None) -> list[dict]:
    from faster_whisper import WhisperModel

    model = WhisperModel(
        model_name, device=device, compute_type=compute_type, download_root=str(MODELS_DIR)
    )
    utts: list[dict] = []
    for uid, name in meta["users"].items():
        wav = rec_dir / f"{uid}.wav"
        if not wav.exists():
            continue
        print(f"[transcribe] {name} ({wav.name})")
        segments, _info = model.transcribe(
            load_normalized(wav),
            language=language,
            vad_filter=True,
            # 小さめの声が「無音」と判定されて捨てられないよう、判定を緩めて前後に余白を取る
            # (旧設定 min_silence 500ms だけだと、文の途中や末尾が丸ごと抜けることがあった)
            vad_parameters={"threshold": 0.2, "speech_pad_ms": 800, "min_silence_duration_ms": 2000},
            condition_on_previous_text=False,  # 繰り返しループ(幻覚)の抑制
            initial_prompt=prompt,
            beam_size=5,
        )
        for s in segments:
            text = s.text.strip()
            if not text or is_hallucination(text) or s.no_speech_prob > 0.8:
                continue
            utts.append({"user": name, "start": s.start, "end": s.end, "text": text})
    return utts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("rec_dir", type=Path)
    ap.add_argument("--model", default="large-v3", help="GPU用モデル(VRAM不足なら medium)")
    ap.add_argument("--cpu-model", default="small", help="CPUにフォールバックした時のモデル")
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--language", default="ja")
    ap.add_argument("--prompt", default=None, help="固有名詞などのヒント(例: 本の題名や用語)")
    args = ap.parse_args()

    meta = json.loads((args.rec_dir / "meta.json").read_text(encoding="utf-8"))
    _add_nvidia_dll_dirs()

    utts = None
    if args.device in ("auto", "cuda"):
        try:
            utts = transcribe_dir(args.rec_dir, meta, args.model, "cuda",
                                  "float16", args.language, args.prompt)
        except Exception as e:  # noqa: BLE001
            if args.device == "cuda":
                raise
            print(f"[warn] GPUでの実行に失敗したのでCPUに切り替えます: {e!r}")
    if utts is None:
        utts = transcribe_dir(args.rec_dir, meta, args.cpu_model if args.device == "auto" else args.model,
                              "cpu", "int8", args.language, args.prompt)

    merged = merge_utterances(utts)
    t0 = meta["t0_epoch"]
    start_dt = datetime.fromtimestamp(t0)
    end_dt = start_dt + timedelta(seconds=meta["duration_sec"])
    speakers = sorted({u["user"] for u in merged})

    lines = [
        "# 文字起こし",
        f"日時: {start_dt:%Y-%m-%d %H:%M} 〜 {end_dt:%H:%M}",
        f"チャンネル: {meta.get('guild', '')} / {meta.get('channel', '')}",
        f"発言者: {', '.join(speakers)}",
        "",
    ]
    for u in merged:
        ts = datetime.fromtimestamp(t0 + u["start"]).strftime("%H:%M:%S")
        lines.append(f"[{ts}] {u['user']}: {u['text']}")

    (args.rec_dir / "transcript.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (args.rec_dir / "transcript.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"done: {args.rec_dir / 'transcript.txt'} ({len(merged)} 発言)")


if __name__ == "__main__":
    main()
