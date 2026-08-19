import sys
from pathlib import Path

# 使 `from backend import create_app` 可用(backend.py 位于 scoring_platform/ 根目录)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
