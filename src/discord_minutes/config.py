"""プロジェクト共通の設定。.env を読み込み、相対パスをプロジェクトのフォルダ基準で解決する。

どのフォルダから実行しても(タスクスケジューラなどから起動しても)同じ場所を使えるようにする。
"""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

# src/discord_minutes/config.py → プロジェクトのフォルダ
PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")


def project_path(value: str | Path) -> Path:
    """相対パスならプロジェクトのフォルダ基準にする。"""
    p = Path(value)
    return p if p.is_absolute() else PROJECT_ROOT / p
