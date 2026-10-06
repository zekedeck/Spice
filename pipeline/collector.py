import logging
import datetime
import os
import time

from atproto import CAR, Client, FirehoseSubscribeReposClient, parse_subscribe_repos_message

from pipeline.database import init_db, insert_post, prune_old_posts, delete_post
from config import DB_PATH, BSKY_HANDLE, BSKY_APP_PASSWORD, ROLLING_WINDOW_DAYS

logger = logging.getLogger(__name__)

_PROFILE_CACHE = {}
_PROFILE_CACHE_MAX = 10000

_NYC_KEYWORDS = frozenset({
    "nyc",
    "new york city",
    "new york",
    "manhattan",
    "brooklyn",
    "queens",
    "bronx",
    "staten island",
    "williamsburg",
    "dumbo",
    "astoria",
    "chelsea",
    "midtown",
    "soho",
    "tribeca",
    "harlem",
    "bed-stuy",
    "bushwick",
    "greenwich village",
    "east village",
    "lower east side",
    "financial district",
    "prospect park",
    "central park",
    "times square",
    "flushing",
    "coney island",
    "park slope",
    "long island city",
    "flatiron",
    "hell's kitchen",
    "upper east side",
    "upper west side",
    "washington heights",
})


def _mentions_nyc(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in _NYC_KEYWORDS)


def build_post_url(handle: str, uri: str) -> str:
    rkey = uri.split("/")[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def run_collector(duration_seconds: int = 180) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    duration_seconds = int(os.getenv("COLLECTOR_DURATION_SECONDS", duration_seconds))
    conn = init_db(DB_PATH)

    client = Client()
    client.login(BSKY_HANDLE, BSKY_APP_PASSWORD)

    post_count = 0
    deadline = time.monotonic() + duration_seconds

    def on_message_handler(message) -> None:
        nonlocal post_count

        if time.monotonic() >= deadline:
            firehose.stop()
            return

        commit = parse_subscribe_repos_message(message)
        if not hasattr(commit, "ops"):
            return

        for op in commit.ops:
            if op.action == "delete" and op.path.startswith("app.bsky.feed.post/"):
                uri = f"at://{commit.repo}/{op.path}"
                delete_post(conn, uri)
                continue

            if op.action == "create" and op.path.startswith("app.bsky.feed.post/"):
                if not commit.blocks:
                    continue

                car = CAR.from_bytes(commit.blocks)
                record = car.blocks.get(op.cid)
                if record is None:
                    continue

                did = commit.repo
                text = record.get("text", "")
                if not text:
                    continue

                if not _mentions_nyc(text):
                    continue

                text = text[:4096]

                created_at = record.get("createdAt", datetime.datetime.utcnow().isoformat())
                uri = f"at://{did}/{op.path}"

                if did in _PROFILE_CACHE:
                    handle = _PROFILE_CACHE[did]
                else:
                    try:
                        profile = client.get_profile(did)
                        handle = profile.handle
                        _PROFILE_CACHE[did] = handle
                        if len(_PROFILE_CACHE) > _PROFILE_CACHE_MAX:
                            for key in list(_PROFILE_CACHE.keys())[:_PROFILE_CACHE_MAX // 2]:
                                del _PROFILE_CACHE[key]
                    except Exception:
                        handle = did

                post_url = build_post_url(handle, uri)

                inserted = insert_post(
                    conn,
                    did=did,
                    handle=handle,
                    post_uri=uri,
                    post_url=post_url,
                    text=text,
                    created_at=created_at,
                )
                if inserted:
                    logger.info("Inserted: %s", post_url)
                    post_count += 1

                if post_count > 0 and post_count % 1000 == 0:
                    prune_old_posts(conn, ROLLING_WINDOW_DAYS)

    while True:
        try:
            firehose = FirehoseSubscribeReposClient()
            firehose.start(on_message_handler)
        except Exception as e:
            if time.monotonic() >= deadline:
                logger.info("Collector duration elapsed; stopping.")
                break
            logger.warning("Firehose disconnected: %s. Reconnecting in 5 seconds...", e)
            time.sleep(5)
            continue

        if time.monotonic() >= deadline:
            break


if __name__ == "__main__":
    run_collector()
