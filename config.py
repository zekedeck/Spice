import os
from dotenv import load_dotenv

load_dotenv()

BSKY_HANDLE = os.getenv("BSKY_HANDLE")
BSKY_APP_PASSWORD = os.getenv("BSKY_APP_PASSWORD")

# Bluesky account-list poller (pipeline/collector_accountlist.py) -- a
# fixed list of account handles (or DIDs) to poll via the AT Protocol REST
# API (getAuthorFeed) instead of the firehose. Left empty by default: the
# owner hasn't finalized the real account list yet. With an empty list the
# poller module is fully built and wired into the GitHub Actions workflow,
# but stays functionally inert -- it logs that the list is empty and exits
# cleanly without even logging in, rather than erroring. To activate it,
# populate this with real handles, e.g.:
#   ACCOUNT_LIST = ["someaccount.bsky.social", "another.bsky.social"]
ACCOUNT_LIST = []

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
# missing. Default is "groq" -- the provider in place and working since
# before Claude support was added as an (unrequested) option; keeps any
# invocation path that doesn't set this env var explicitly (local runs,
# etc.) on the known-working provider rather than silently landing on an
# unconfigured one.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").lower()
