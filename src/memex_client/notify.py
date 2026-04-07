from __future__ import annotations

import shutil
import subprocess


def notify(title: str, body: str, urgency: str = "normal") -> None:
    if shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", f"--urgency={urgency}", title, body],
            check=False,
        )
