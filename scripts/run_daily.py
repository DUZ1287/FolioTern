"""Task Scheduler entry: logs stay local and no console window is required."""
import contextlib
from datetime import datetime
from pathlib import Path
import sys

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from agentradar.pipeline import run, AlreadyRunning

if __name__ == "__main__":
    (root / "logs").mkdir(exist_ok=True)
    with (root / "logs" / (datetime.now().strftime("%Y-%m-%d") + ".log")).open("a", encoding="utf-8") as log:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            print(datetime.now().isoformat(), "Daily collection started", flush=True)
            try:
                report, code = run(root)
                print("Finished", report["date"], "collected", report["collected"], "mode", report["mode"], "exit", code, flush=True)
            except AlreadyRunning:
                print("Skipped: another run is active", flush=True)
                code = 0
            except Exception as exc:
                print("Failed:", type(exc).__name__, flush=True)
                code = 2
    raise SystemExit(code)
