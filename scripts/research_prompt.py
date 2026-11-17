"""Pass a prompted question as data, never interpolate it into a shell command."""
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from radar import main

if __name__ == "__main__":
    question = os.environ.get("RADAR_QUESTION", "").strip()
    if not question:
        raise SystemExit("研究问题不能为空")
    sys.argv = [str(root / "radar.py"), "research", question]
    main()
