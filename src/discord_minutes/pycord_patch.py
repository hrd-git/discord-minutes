"""py-cord 2.8.x の音声受信(DAVE)まわりのバグを実行時に差し替えるパッチ。

py-cord 2.8.1 の受信処理には次の問題があり、DAVE 有効な VC では録音が止まったり途切れたりする。

1. AEAD(rtpsize)の復号で RTP 拡張ヘッダーを 8 バイト決め打ちで取り除き、
   さらに DAVE 復号の後にもう一度取り除いている(二重に削られて Opus が壊れる)
2. Opus デコードの失敗 1 回で受信スレッド全体が止まる
3. デコード後の PCM に DAVE の復号を掛けている(不要な処理)
4. ジッターバッファが番号の欠けで止まり、溜まったパケットを黙って捨てる
5. UDP 受信スレッドから loop.create_task を呼んでおり(スレッド安全でない)、パケットが溜まる・消える
6. 録音停止後に受信スレッドがもう一度 stop_recording() を呼び、無害な例外が毎回表示される

正しい順番: トランスポート復号 → 拡張ヘッダーを 1 回だけ除去 → DAVE 復号 → Opus デコード
受信スレッドの中でその場でデコードして Sink に渡し、短い欠落は Opus の FEC/PLC で補う。
py-cord 側で直ったら、このファイルと bot.py の apply() / reset_stats() 呼び出しを削除してよい。
"""
from __future__ import annotations

import logging

import discord

log = logging.getLogger(__name__)

# 受信したパケット・捨てたパケットの件数(録音停止時に bot.py が表示してリセットする)
stats: dict[str, int] = {
    "udp": 0, "ok": 0, "delivered": 0, "lost": 0, "concealed": 0, "late": 0, "no_uid": 0, "padded": 0, "empty": 0, "unencrypted": 0, "dave_fail": 0, "opus_fail": 0, "decode_err": 0,
}
_warned: set[str] = set()


def _count(key: str, msg: str = "", *args) -> None:
    stats[key] += 1
    if msg and key not in _warned:  # 種類ごとに最初の1回だけ警告を出す
        _warned.add(key)
        log.warning(msg, *args)


def reset_stats() -> dict[str, int]:
    snap = dict(stats)
    for k in stats:
        stats[k] = 0
    _warned.clear()
    return snap


def _decrypt_rtp_aead_xchacha20_poly1305_rtpsize(self, packet) -> bytes:
    import nacl.secret

    from discord.voice.receive.reader import CryptoError

    packet.adjust_rtpsize()
    nonce = packet.nonce + b"\x00" * 20
    assert isinstance(self.box, nacl.secret.Aead)
    try:
        result = self.box.decrypt(
            packet.decrypted_data or packet.data, bytes(packet.header), nonce
        )
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(exc) from exc

    offset = packet.update_extended_header(result) if packet.extended else 0
    if packet.padding and result:
        # RTP パディング(RFC 3550): 末尾 1 バイトがパディングの長さ。2.8.1 は取り除かないため、
        # DAVE の末尾の目印が見つからず「暗号化なし」扱いになり、Opus も壊れる
        pad = result[-1]
        if 0 < pad <= len(result) - offset:
            stats["padded"] += 1
            result = result[:-pad]
    return result[offset:]


def _decrypt_rtp(self, packet) -> bytes | None:
    import davey

    state = self.client._connection
    payload = self._decryptor_rtp(packet)  # 拡張ヘッダー・パディング除去済み
    if not payload:
        stats["empty"] += 1  # パディングだけのパケット(帯域の測定用など)。音声は入っていない
        return None

    dave = state.dave_session
    if dave is not None and dave.ready:
        uid = state.ssrc_user_map.get(packet.ssrc)
        if uid is None:
            _count("no_uid")  # 誰の音声か分からないうちは復号できないので捨てる
            return None
        try:
            payload = dave.decrypt(uid, davey.MediaType.audio, payload)
        except Exception as exc:  # noqa: BLE001
            if "UnencryptedWhenPassthroughDisabled" in str(exc):
                # 送信側が暗号化せずに送ったフレーム(主に発言の前後の無音)。そのまま Opus として使う
                stats["unencrypted"] += 1
                if stats["unencrypted"] <= 5:  # 原因調査用: 最初の数件だけ中身を記録する
                    log.info(
                        "unencrypted frame seq=%s ts=%s pt=%s marker=%s padding=%s ext=%s len=%s head=%s tail=%s",
                        packet.sequence, packet.timestamp, packet.payload, packet.marker,
                        packet.padding, packet.extended, len(payload),
                        bytes(payload[:8]).hex(), bytes(payload[-8:]).hex(),
                    )
            else:
                _count("dave_fail", "DAVE decrypt failed ssrc=%s: %r", packet.ssrc, exc)
                return None

    stats["ok"] += 1
    packet.decrypted_data = payload
    return payload


def _decode_packet(self, packet):
    from discord.opus import OpusError

    try:
        if packet:
            pcm = self._decoder.decode(packet.decrypted_data, fec=False)
        else:  # 欠落パケット: 次のパケットの FEC で補う
            nxt = self._buffer.peek_next()
            if nxt is not None and nxt.decrypted_data:
                pcm = self._decoder.decode(nxt.decrypted_data, fec=True)
            else:
                pcm = self._decoder.decode(None, fec=False)
    except OpusError as exc:
        _count("opus_fail", "opus decode failed ssrc=%s: %r", self.ssrc, exc)
        pcm = b""
    return packet, pcm


def _feed_rtp(self, packet) -> None:
    """ジッターバッファを通さず、届いたパケットをその場でデコードして Sink に渡す。

    2.8.1 のジッターバッファは、番号が 1 つ欠けると取り出しが止まり、
    溜まったパケットを黙って捨てるため、音声が大きく欠ける。
    """
    out = []
    with self._lock:
        decoder = self.get_decoder(packet.ssrc)
        if decoder is None:
            return
        last = getattr(decoder, "_pl_last_seq", None)
        if last is not None:
            d = (packet.sequence - last) & 0xFFFF
            if d == 0 or d >= 0x8000:
                stats["late"] += 1  # 重複・追い越して届いたパケットは捨てる
                return
            if 1 < d <= MAX_CONCEAL + 1:
                out.extend(_conceal(decoder, packet, d - 1))
            elif d > MAX_CONCEAL + 1:
                stats["lost"] += d - 1
        decoder._pl_last_seq = packet.sequence
        try:
            data = decoder._process_packet(packet)
        except Exception as exc:  # noqa: BLE001
            _count("decode_err", "packet processing failed ssrc=%s: %r", packet.ssrc, exc)
            data = None
        if data is not None:
            out.append(data)
    for data in out:
        stats["delivered"] += 1
        self.sink.write(data, data.source)


MAX_CONCEAL = 5  # これ以下の連続欠落は Opus の FEC/PLC で補う(それ以上は無音)


def _conceal(decoder, packet, lost: int) -> list:
    """欠落したパケットの分の音声を作る。直前の 1 つは次パケットの FEC、それ以外は PLC。"""
    import types

    from discord.opus import OpusError
    from discord.voice import VoiceData

    stats["lost"] += lost
    member = decoder._get_cached_member()
    if member is None and decoder._cached_id:
        from discord.object import Object

        member = Object(id=decoder._cached_id)
    res = []
    for i in range(lost):
        fec = i == lost - 1 and bool(packet.decrypted_data)
        try:
            pcm = decoder._decoder.decode(packet.decrypted_data if fec else None, fec=fec)
        except OpusError:
            continue
        ts = (packet.timestamp - 960 * (lost - i)) & 0xFFFFFFFF
        fake = types.SimpleNamespace(timestamp=ts, sequence=None, decrypted_data=None)
        res.append(VoiceData(fake, member, pcm=pcm))
        stats["concealed"] += 1
    return res


def _socket_reader_do_run(self) -> None:
    """UDP 受信ループ。2.8.1 は受信のたびに別スレッドから loop.create_task を呼んでおり
    (スレッド安全でない)、パケットが溜まったり取りこぼされたりして音声が途切れる。
    録音用コールバック(AudioReader.callback)はこのスレッドで直接呼び、
    それ以外は call_soon_threadsafe でイベントループに渡す。
    """
    import select
    import socket as _socket

    from discord import utils
    from discord.voice.receive.reader import AudioReader

    sock_with_buf = None
    while not self._end.is_set():
        if not self._running.is_set():
            self._running.wait()
            continue

        sock = self.state.socket
        if sock is not sock_with_buf:
            try:  # Windows の既定受信バッファは小さいので広げておく
                sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_RCVBUF, 1 << 20)
            except Exception:  # noqa: BLE001
                pass
            sock_with_buf = sock

        try:
            readable, _, _ = select.select([sock], [], [], 30)
        except (ValueError, TypeError, OSError):
            continue
        if not readable:
            continue
        try:
            data = sock.recv(self.buffer_size)
        except OSError:
            continue

        stats["udp"] += 1
        for cb in list(self._callbacks):
            try:
                if isinstance(getattr(cb, "__self__", None), AudioReader):
                    cb(data)
                else:
                    loop = self.state.loop
                    loop.call_soon_threadsafe(
                        lambda cb=cb, data=data: self.state._dispatch_task_set.add(
                            loop.create_task(utils.maybe_coroutine(cb, data))
                        )
                    )
            except Exception:  # noqa: BLE001
                log.exception("socket callback %r failed", cb)


def _router_run(self) -> None:
    """受信スレッドの本体。2.8.1 は終了時に必ず stop_recording() を呼ぶため、
    /rec_stop で先に止めていると「You are not recording」の例外が毎回表示される。
    すでに止まっている場合はその例外を無視する。
    """
    from discord.sinks.errors import RecordingException

    try:
        self._do_run()
    except Exception as exc:  # noqa: BLE001
        log.exception("Error in %s loop", self)
        self.reader.error = exc
    finally:
        try:
            self.reader.client.stop_recording()
        except RecordingException:
            pass
        self.waiter.clear()


_applied = False


def apply() -> None:
    global _applied
    if _applied:
        return
    if not discord.__version__.startswith("2.8."):
        log.warning("py-cord %s: パッチは 2.8.x 用なので適用しません", discord.__version__)
        return

    from discord.opus import PacketDecoder
    from discord.voice.receive.reader import PacketDecryptor
    from discord.voice.receive.router import PacketRouter

    from discord.voice.state import SocketReader

    PacketRouter.feed_rtp = _feed_rtp
    PacketRouter.run = _router_run
    SocketReader._do_run = _socket_reader_do_run
    PacketDecryptor._decrypt_rtp_aead_xchacha20_poly1305_rtpsize = (
        _decrypt_rtp_aead_xchacha20_poly1305_rtpsize
    )
    PacketDecryptor.decrypt_rtp = _decrypt_rtp
    PacketDecoder._decode_packet = _decode_packet
    _applied = True
    log.info("applied voice receive patch for py-cord %s", discord.__version__)
