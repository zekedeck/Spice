import logging
import datetime
import time

from atproto import CAR, Client, FirehoseSubscribeReposClient, parse_subscribe_repos_message

from pipeline.database import init_db, insert_post, prune_old_posts, delete_post
from config import DB_PATH, BSKY_HANDLE, BSKY_APP_PASSWORD, ROLLING_WINDOW_DAYS

logger = logging.getLogger(__name__)

_PROFILE_CACHE = {}
_PROFILE_CACHE_MAX = 10000


def build_post_url(handle: str, uri: str) -> str:
    rkey = uri.split("/")[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def run_collector() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = init_db(DB_PATH)

    client = Client()
    client.login(BSKY_HANDLE, BSKY_APP_PASSWORD)

    post_count = 0

    def on_message_handler(message) -> None:
        nonlocal post_count

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
            logger.warning("Firehose disconnected: %s. Reconnecting in 5 seconds...", e)
            time.sleep(5)


if __name__ == "__main__":
    run_collector()
