import os
from dotenv import load_dotenv

load_dotenv()

BSKY_HANDLE = os.getenv("BSKY_HANDLE")
BSKY_APP_PASSWORD = os.getenv("BSKY_APP_PASSWORD")

# Rolling window — how many days of posts to keep in the database
ROLLING_WINDOW_DAYS = 30

# Pipeline run settings
DEFAULT_RADIUS_MILES = 25.0

# MVP data retention — rolling live window + daily archive files
LIVE_WINDOW_HOURS = 48
ARCHIVE_RETENTION_DAYS = 30
LIVE_OUTPUT_PATH = "data/live.geojson"
ARCHIVE_DIR = "data/archive"

# SQLite database — path overridden via env var; GitHub Actions points this
# at a path restored from Actions cache between runs (no persistent server)
DB_PATH = os.getenv("DB_PATH", "/data/raw/posts.db")

# LLM geotagging kill switch — set to "false" to disable the LLM layer
# (whichever provider is selected below) and fall back entirely to the
# rule-based pipeline
LLM_ENABLED = os.getenv("LLM_ENABLED", "true").lower() == "true"

# Which LLM provider backs the geotagging layer: "claude" (Anthropic) or
# "groq". Both implementations live in pipeline/llm_geotagging.py behind the
# same resolve_entity_with_llm() interface, so this is a one-line toggle,
# not a code change, to switch back if needed. Requires the matching API key
# env var (ANTHROPIC_API_KEY or GROQ_API_KEY) to actually be set — see
# init_llm_client() in pipeline/llm_geotagging.py, which degrades to the
# rule-based path (not a crash) if the key for the selected provider is
# missing.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "claude").lower()
