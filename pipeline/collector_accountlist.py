"""Second ingestion module: a Bluesky account-list poller.

Unlike pipeline/collector.py (which holds a long-lived firehose
connection open for minutes), this module makes a handful of quick AT
Protocol REST calls -- one getAuthorFeed call per configured account --
and returns in seconds. It's designed to run as its own short step,
separate from the firehose collector, in the GitHub Actions workflow.

For each handle in config.ACCOUNT_LIST: fetch that account's recent own
posts via Client.get_author_feed(), apply the same NYC-keyword filter the
firehose collector uses (_mentions_nyc, imported from pipeline.collector
rather than duplicated here), and insert any matching, not-already-seen
post through the same IngestedPost ingestion contract (models.py) that
pipeline.collector uses, with source="bluesky_accountlist". Dedup against
posts already in the DB is handled by insert_post()'s existing
INSERT OR IGNORE on posts.post_uri -- no separate lookup needed.

config.ACCOUNT_LIST defaults to an empty list (the real list hasn't been
finalized). With an empty list this module does nothing: it logs that
the list is empty and returns without even constructing a Bluesky client
or logging in, so running it needs no credentials and has no side
effects. See config.py for where to put a real list.
"""

import logging

from atproto import Client

from pipeline.collector import _mentions_nyc, build_post_url, _login_with_retry
from pipeline.database import init_db, insert_post
from models import IngestedPost
from config import DB_PATH, BSKY_HANDLE, BSKY_APP_PASSWORD, ACCOUNT_LIST

logger = logging.getLogger(__name__)

SOURCE_NAME = "bluesky_accountlist"

# Recent-posts page size per account per run. Generous relative to how
# often this step is expected to run -- dedup via insert_post's
# INSERT OR IGNORE makes re-fetching already-seen posts harmless, just
# wasted work, so this errs on the side of not missing posts between runs.
_FEED_LIMIT = 50


def _extract_record_fields(record) -> tuple:
    """Pull (text, created_at) off a feed post's record.

    record is normally an atproto AppBskyFeedPost.Record (has .text and
    .created_at), but defensively tolerate anything record-shaped via
    getattr rather than assuming the exact type, since this comes back
    through a third-party SDK response object.
    """
    text = getattr(record, "text", "") or ""
    created_at = getattr(record, "created_at", None)
    return text, created_at


def _collect_from_account(client: Client, conn, actor: str) -> int:
    """Fetch actor's recent posts, filter to NYC-mentioning ones, insert
    any new matches. Returns the count of newly inserted posts. Never
    raises -- a failure fetching one account's feed is logged and
    skipped so it doesn't take down the rest of the account list."""
    try:
        response = client.get_author_feed(actor=actor, limit=_FEED_LIMIT)
    except Exception:
        logger.warning("Failed to fetch author feed for %s", actor, exc_info=True)
        return 0

    inserted_count = 0
    for item in getattr(response, "feed", []):
        post = item.post
        text, created_at = _extract_record_fields(post.record)
        if not text:
            continue

        if not _mentions_nyc(text):
            continue

        text = text[:4096]
        if not created_at:
            created_at = post.indexed_at

        author_handle = post.author.handle
        author_did = post.author.did
        post_url = build_post_url(author_handle, post.uri)

        ingested = IngestedPost(
            source=SOURCE_NAME,
            author_id=author_did,
            handle=author_handle,
            post_uri=post.uri,
            post_url=post_url,
            text=text,
            created_at=created_at,
        )

        if insert_post(conn, ingested):
            inserted_count += 1
            logger.info("Inserted: %s", post_url)

    return inserted_count


def run_collector_accountlist() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not ACCOUNT_LIST:
        logger.info(
            "ACCOUNT_LIST is empty; account-list poller has nothing to do, exiting cleanly."
        )
        return

    conn = init_db(DB_PATH)

    client = Client()
    _login_with_retry(client, BSKY_HANDLE, BSKY_APP_PASSWORD)

    total_inserted = 0
    for actor in ACCOUNT_LIST:
        total_inserted += _collect_from_account(client, conn, actor)

    logger.info(
        "Account-list poll complete: %d new post(s) inserted across %d account(s)",
        total_inserted, len(ACCOUNT_LIST),
    )


if __name__ == "__main__":
    run_collector_accountlist()
