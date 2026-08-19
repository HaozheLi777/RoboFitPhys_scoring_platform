"""RoboFit 打分平台跨平台启动器(Linux / Windows / macOS)。

用法:
    python run.py

启动前检查依赖与 FFmpeg,随后启动服务并自动打开浏览器。
可用环境变量与 run.sh 一致:SCORING_HOST(默认 127.0.0.1)、SCORING_PORT(默认 8010)、
SCORING_DATA_ROOT / SCORING_CACHE_ROOT / SCORING_ANNOTATION_ROOT / SCORING_PART_SETS_PATH。
设置 SCORING_NO_BROWSER=1 可禁止自动打开浏览器。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import threading
import webbrowser
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent


def _ensure_console_utf8() -> None:
    # Windows 控制台默认 GBK,中文提示会乱码;能切换则切到 UTF-8
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except OSError:
                pass


def main() -> int:
    _ensure_console_utf8()

    required = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "numpy": "numpy",
        "cv2": "opencv-python",
    }
    missing = [package for module, package in required.items() if importlib.util.find_spec(module) is None]
    if missing:
        print("缺少运行依赖:", ", ".join(missing))
        print(f"请先安装 Python 3.10+ 并执行:  pip install -r {PLATFORM_ROOT / 'requirements.txt'}")
        input("按回车退出...")
        return 1

    ffmpeg = shutil.which("ffmpeg") or (Path(sys.executable).resolve().parent / "ffmpeg")
    if not ffmpeg or not Path(ffmpeg).is_file():
        print("提示:未找到 FFmpeg。标注与逐帧预览不受影响,但无法生成流畅预览 MP4。")
        print("安装 FFmpeg 后重启本程序即可(或把 ffmpeg 可执行文件放到 Python 同目录)。")
        if sys.platform == "win32":
            print("Windows 可执行: winget install ffmpeg  或从 https://ffmpeg.org 下载。")

    host = os.getenv("SCORING_HOST", "127.0.0.1")
    port = int(os.getenv("SCORING_PORT", "8010"))
    url = f"http://{host if host not in ('0.0.0.0', '::') else '127.0.0.1'}:{port}"

    if not os.getenv("SCORING_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    import uvicorn

    import backend  # 触发配置解析(参数 > 环境变量 > config.local.json > 默认值)
    print(f"RoboFit 打分平台启动: {url}  (Ctrl+C 退出)")
    print(f"数据目录: {backend.app.state.data_root}")
    print(f"标注目录: {backend.app.state.annotations_root}")
    uvicorn.run("backend:app", host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
