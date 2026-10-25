"""ユーザー別の録音処理。

各 wav は録音開始時刻(t0)から連続した時間軸になるよう、無音区間を埋めて書き出す。
Discord は発言していない間パケットを送らないため、素の WaveSink だと無音が詰められて
時系列の突き合わせができなくなる。それを避けるための自前 Sink。
"""
from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import numpy as np
from discord.sinks import Sink

# py-cord が渡してくる PCM: 48kHz / 16bit / ステレオ
# 保存形式: 16kHz / 16bit / モノラル(Whisper 向け。容量も 1/6)
OUT_RATE = 16000
OUT_BYTES_PER_SEC = OUT_RATE * 2
# 前のパケットから RTP タイムスタンプでこれ以上空いたら、届いた時刻で位置を合わせ直す
RTP_RESYNC_SAMPLES = 48000 * 1


def to_16k_mono(pcm: bytes) -> bytes:
    """48kHz ステレオ s16le → 16kHz モノラル s16le(平均化による簡易ダウンサンプル)"""
    pcm = pcm[: len(pcm) // 2 * 2]
    a = np.frombuffer(pcm, dtype=np.int16)
    n = len(a) - (len(a) % 6)  # 2ch × 3(48k→16k)
    if n <= 0:
        return b""
    # 2ch × 3 サンプルの平均。切り捨てだと小さい音が 0 に潰れるので四捨五入する
    down = a[:n].reshape(-1, 6).astype(np.float32).mean(axis=1)
    return np.clip(np.rint(down), -32768, 32767).astype(np.int16).tobytes()


class TimelineSink(Sink):
    """ユーザー別に wav を書き出す Sink。録音開始からの経過時間に合わせて無音を埋める。"""

    def __init__(self, out_dir: Path):
        super().__init__()
        self.out_dir = out_dir
        self.t0 = time.perf_counter()
        self.t0_epoch = time.time()
        self._lock = threading.Lock()
        self._files: dict[int, wave.Wave_write] = {}
        self._written: dict[int, int] = {}
        self._rtp_end: dict[int, int] = {}  # ユーザー別: 最後に書いたパケットの直後の RTP タイムスタンプ
        self._seen_users: list[int] = []
        self.packets = 0
        self._logged_first = False

    # py-cord 2.8.x の受信処理は新しい Sink API を前提にしているが、同梱の Sink は旧版のまま。
    # 受信側が参照する属性をここで補う。
    __sink_listeners__: tuple = ()

    def walk_children(self, *, with_self: bool = False):
        return iter((self,)) if with_self else iter(())

    def is_opus(self) -> bool:
        return False  # PCM にデコードしてから write に渡してもらう

    @property
    def user_ids(self) -> list[int]:
        return list(self._seen_users)

    @staticmethod
    def _parse_args(args, kwargs):
        """write(data, user) / write(user, data) のどちらの形でも受けられるようにする。"""
        user = kwargs.get("user")
        data = kwargs.get("data")
        for a in args:
            if isinstance(a, (bytes, bytearray)):
                data = a
            elif hasattr(a, "pcm") or hasattr(a, "decoded_data"):
                data = a
            else:
                user = a
        if data is not None and not isinstance(data, (bytes, bytearray)):
            data = getattr(data, "pcm", None) or getattr(data, "decoded_data", None)
        return user, (bytes(data) if data else None)

    @staticmethod
    def _rtp_timestamp(args) -> int | None:
        for a in args:
            ts = getattr(getattr(a, "packet", None), "timestamp", None)
            if isinstance(ts, int):
                return ts
        return None

    def write(self, *args, **kwargs):
        user, pcm = self._parse_args(args, kwargs)
        ts = self._rtp_timestamp(args)
        if not self._logged_first:
            self._logged_first = True
            print(
                "[sink] first packet:",
                [type(a).__name__ for a in args],
                "pcm_len=", len(pcm) if pcm else None,
            )
        if user is None or not pcm:
            return
        uid = int(getattr(user, "id", user))
        out = to_16k_mono(pcm)
        if not out:
            return
        now = time.perf_counter()
        with self._lock:
            w = self._files.get(uid)
            if w is None:
                w = wave.open(str(self.out_dir / f"{uid}.wav"), "wb")
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(OUT_RATE)
                self._files[uid] = w
                self._written[uid] = 0
                self._seen_users.append(uid)

            gap = None
            last_end = self._rtp_end.get(uid)
            if ts is not None and last_end is not None:
                # 発言中は送信側の RTP タイムスタンプ(48kHz)で位置を決める。
                # 届いた時刻を使うと、ネットワークの揺れで発言の途中に無音が入ってしまう。
                d = (ts - last_end) & 0xFFFFFFFF
                if d >= 1 << 31:
                    return  # 追い越して届いた古いパケット(すでに書き込んだ位置より前)
                if d <= RTP_RESYNC_SAMPLES:
                    gap = d // 3 * 2  # 48kHz サンプル → 16kHz モノラルのバイト数
            if gap is None:
                # 最初のパケット、または長い無音の後: 届いた時刻に合わせる
                expected_start = int((now - self.t0) * OUT_BYTES_PER_SEC) - len(out)
                gap = max(0, expected_start - self._written[uid])
            gap -= gap % 2
            if gap:
                w.writeframes(b"\x00" * gap)
                self._written[uid] += gap
            w.writeframes(out)
            self._written[uid] += len(out)
            if ts is not None:
                self._rtp_end[uid] = (ts + len(pcm) // 4) & 0xFFFFFFFF  # 48kHz ステレオ s16
            self.packets += 1

    def cleanup(self):
        with self._lock:
            for w in self._files.values():
                try:
                    w.close()
                except Exception:
                    pass
            self._files.clear()
        self.finished = True
