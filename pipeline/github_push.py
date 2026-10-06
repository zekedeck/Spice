import subprocess
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def push_to_github(file_path: str, commit_message: str = None) -> bool:
    if commit_message is None:
        commit_message = f"Update GeoJSON output {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"

    commands = [
        ["git", "add", file_path],
        ["git", "commit", "-m", commit_message],
        ["git", "push"],
    ]

    for cmd in commands:
        logger.info("Running: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.warning("Git command failed: %s\n%s", " ".join(cmd), result.stderr)
            return False

    logger.info("Successfully pushed %s to GitHub", file_path)
    return True
