"""Discord VC 録音Bot

/rec_start : 実行者がいるボイスチャンネルに参加して録音を開始
/rec_stop  : 録音を停止し、ユーザー別の wav(16kHz/モノラル)と meta.json を保存。
             続けて文字起こしを実行し、transcript.txt をチャンネルに投稿する

起動: poetry run minutes-bot
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import discord

from . import pycord_patch
from .config import project_path
from .recorder import TimelineSink

OUT_ROOT = project_path(os.getenv("OUT_DIR", "recordings"))
# スラッシュコマンドを表示するサーバーの ID(カンマ区切りで複数可)。
# 空なら招待したすべてのサーバーに表示する(初回の反映に時間がかかることがある)
GUILD_IDS = [int(x) for x in os.getenv("GUILD_ID", "").replace(" ", "").split(",") if x]
# 0 にすると /rec_stop 後の自動文字起こしをしない
AUTO_TRANSCRIBE = os.getenv("AUTO_TRANSCRIBE", "1") not in ("0", "false", "False", "")

# 文字起こしは GPU を使うので 1 件ずつ順番に処理する
_transcribe_lock = asyncio.Lock()
# 実行中のバックグラウンド処理(参照を持っておかないと途中で回収されることがある)
_background: set[asyncio.Task] = set()

PREVIEW_CHARS = 1500  # Discord のメッセージは 2000 文字まで


def _is_new_voice_api() -> bool:
    """py-cord 2.7 以降は start_recording の callback が任意になった。"""
    try:
        major, minor = (int(x) for x in discord.__version__.split(".")[:2])
    except ValueError:
        return True
    return (major, minor) >= (2, 7)


async def _noop_callback(*_args):
    return None


def _new_out_dir() -> Path:
    """録音ごとの保存フォルダを作る。複数のサーバーで同じ秒に始めても別のフォルダにする。"""
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    base = datetime.now().strftime("%Y%m%d_%H%M%S")
    for i in range(100):
        out_dir = OUT_ROOT / (base if i == 0 else f"{base}_{i + 1}")
        try:
            out_dir.mkdir()
            return out_dir
        except FileExistsError:
            continue
    raise RuntimeError(f"保存フォルダを作れませんでした: {OUT_ROOT / base}")


intents = discord.Intents.default()  # voice_states を含む
bot = discord.Bot(intents=intents, debug_guilds=GUILD_IDS or None)
sessions: dict[int, dict] = {}


@bot.event
async def on_ready():
    print(f"logged in as {bot.user} (py-cord {discord.__version__})")


@bot.slash_command(name="rec_start", description="いるVCに参加して録音を開始します")
async def rec_start(ctx: discord.ApplicationContext):
    if ctx.guild is None:
        await ctx.respond("サーバー内で実行してください。", ephemeral=True)
        return
    if ctx.guild.id in sessions:
        await ctx.respond("すでに録音中です。", ephemeral=True)
        return
    state = ctx.author.voice
    if state is None or state.channel is None:
        await ctx.respond("先にボイスチャンネルに参加してください。", ephemeral=True)
        return

    await ctx.defer()
    try:
        vc = await state.channel.connect()
    except Exception as e:  # noqa: BLE001
        await ctx.followup.send(f"VCへの接続に失敗しました: {e!r}")
        return

    out_dir = _new_out_dir()
    sink = TimelineSink(out_dir)
    pycord_patch.reset_stats()
    sink.vc = vc  # 受信スレッドが sink.client(= vc)を参照するため、開始前に設定する
    try:
        if _is_new_voice_api():
            vc.start_recording(sink)
        else:
            vc.start_recording(sink, _noop_callback)
    except Exception as e:  # noqa: BLE001
        await vc.disconnect()
        await ctx.followup.send(f"録音の開始に失敗しました: {e!r}")
        return

    sessions[ctx.guild.id] = {"vc": vc, "sink": sink, "dir": out_dir, "channel": state.channel.name}
    dave = getattr(vc, "is_dave_connection", lambda: None)()
    print(f"[rec] started dir={out_dir} dave={dave}")
    await ctx.followup.send(
        "🔴 **録音と文字起こしを開始しました。** 議事録作成のため、このVCの音声を録音します。"
        "終了時は `/rec_stop` を実行してください。"
    )


@bot.slash_command(name="rec_stop", description="録音を停止して保存します")
async def rec_stop(ctx: discord.ApplicationContext):
    s = sessions.pop(ctx.guild.id, None) if ctx.guild else None
    if s is None:
        await ctx.respond("録音していません。", ephemeral=True)
        return
    await ctx.defer()
    vc, sink, out_dir = s["vc"], s["sink"], s["dir"]
    duration = time.perf_counter() - sink.t0
    try:
        vc.stop_recording()
    except Exception as e:  # noqa: BLE001
        print(f"[rec] stop_recording error: {e!r}")
    await asyncio.sleep(1.0)  # 受信スレッドの書き込み完了待ち
    sink.cleanup()
    try:
        await vc.disconnect()
    except Exception as e:  # noqa: BLE001
        print(f"[rec] disconnect error: {e!r}")

    users: dict[str, str] = {}
    for uid in sink.user_ids:
        name = str(uid)
        try:
            m = ctx.guild.get_member(uid) or await ctx.guild.fetch_member(uid)
            name = m.display_name
        except Exception:  # noqa: BLE001
            pass
        users[str(uid)] = name

    meta = {
        "t0_epoch": sink.t0_epoch,
        "duration_sec": duration,
        "guild": ctx.guild.name,
        "channel": s["channel"],
        "users": users,
        "packets": sink.packets,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[rec] stopped packets={sink.packets} users={users}")
    print(f"[rec] receive stats {pycord_patch.reset_stats()}")

    if sink.packets == 0:
        await ctx.followup.send(
            "⚠️ 録音は終了しましたが、音声を1パケットも取得できませんでした。"
            "DAVE(音声E2EE)の復号に失敗している可能性があります。コンソールのログを確認してください。"
        )
        return

    msg = f"⏹ 録音を終了しました({duration/60:.1f}分 / {len(users)}人分)。保存先: `{out_dir}`"
    if not AUTO_TRANSCRIBE:
        await ctx.followup.send(msg)
        return
    waiting = " 前の文字起こしが終わり次第始めます。" if _transcribe_lock.locked() else ""
    await ctx.followup.send(f"{msg}\n📝 文字起こし中です。終わったらこのチャンネルに投稿します。{waiting}")
    task = asyncio.create_task(_transcribe_and_post(ctx.channel_id, out_dir))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _transcribe_and_post(channel_id: int, out_dir: Path) -> None:
    """別プロセスで文字起こしを実行し、結果をチャンネルに投稿する。

    別プロセスにするのは、処理中も Bot が応答できるようにするためと、
    終わったら GPU メモリを確実に解放するため。
    """
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    log_path = out_dir / "transcribe.log"
    async with _transcribe_lock:
        t = time.perf_counter()
        print(f"[transcribe] start {out_dir}")
        try:
            with log_path.open("wb") as log_file:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "discord_minutes.transcribe", str(out_dir),
                    stdout=log_file, stderr=asyncio.subprocess.STDOUT,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                )
                code = await proc.wait()
        except Exception as e:  # noqa: BLE001
            code = None
            log_path.write_text(repr(e), encoding="utf-8")
        elapsed = time.perf_counter() - t
        print(f"[transcribe] end {out_dir} code={code} {elapsed:.0f}s")

    transcript = out_dir / "transcript.txt"
    if code != 0 or not transcript.exists():
        tail = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-5:]
        hint = ""
        if any("No module named 'faster_whisper'" in line for line in tail):
            hint = "\n`poetry install --with asr` で文字起こし用のライブラリを入れてください。"
        await channel.send(
            f"⚠️ `{out_dir.name}` の文字起こしに失敗しました。{hint}\n"
            f"```\n{chr(10).join(tail)[-1500:]}\n```"
        )
        return

    utts = json.loads((out_dir / "transcript.json").read_text(encoding="utf-8"))
    if not utts:
        await channel.send(f"📝 `{out_dir.name}` の文字起こしが終わりましたが、発言を検出できませんでした。")
        return

    # 本文には冒頭の発言だけを載せ、全文はファイルで添付する
    lines = [ln for ln in transcript.read_text(encoding="utf-8").splitlines() if ln.startswith("[")]
    preview, size = [], 0
    for ln in lines:
        if size + len(ln) > PREVIEW_CHARS:
            break
        preview.append(ln)
        size += len(ln) + 1
    if not preview:  # 1行目から長すぎる場合は途中で切る
        preview = [lines[0][:PREVIEW_CHARS] + "…"]
    more = f"\n…ほか {len(lines) - len(preview)} 件は添付ファイルを見てください。" if len(preview) < len(lines) else ""
    await channel.send(
        f"📝 文字起こしが完了しました({len(utts)} 発言 / 処理 {elapsed/60:.1f}分)\n"
        f"```\n{chr(10).join(preview)}\n```{more}",
        file=discord.File(transcript, filename=f"transcript_{out_dir.name}.txt"),
    )


def main() -> None:
    # py-cord は受信スレッド内のエラーを logging に出すだけなので、表示されるようにしておく
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    pycord_patch.apply()
    bot.run(os.environ["DISCORD_TOKEN"])


if __name__ == "__main__":
    main()
