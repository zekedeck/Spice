"""Tests for pipeline.collector_accountlist's core logic.

Run from the repo root with:  python -m pytest tests/ -q

This module polls a fixed list of Bluesky accounts' own feeds via the AT
Protocol REST API (Client.get_author_feed) rather than the firehose, so
these tests mock the atproto Client entirely -- no real network access or
credentials are used or required, same mocking pattern as
tests/test_llm_geotagging.py's stubbed Groq client.

Three behaviors matter here:
  1. config.ACCOUNT_LIST defaults to [] and the poller must be a true
     no-op in that case -- no client constructed, no login attempted, no
     DB touched.
  2. The NYC-keyword filter applied to fetched posts must be the exact
     same _mentions_nyc() the firehose collector uses (imported, not
     duplicated).
  3. Dedup against posts already in the DB happens via insert_post()'s
     existing INSERT OR IGNORE on posts.post_uri -- polling an account
     twice (e.g. two runs that both see the same post) must not double
     insert.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pipeline import collector_accountlist
from pipeline.database import init_db, get_unprocessed_posts


def _fake_feed_item(uri, did, handle, text, created_at="2026-01-01T00:00:00Z", indexed_at=None):
    """Build a SimpleNamespace shaped like the bits of a
    models.AppBskyFeedDefs.FeedViewPost that _collect_from_account reads:
    item.post.{uri, record.{text, created_at}, author.{did, handle},
    indexed_at}."""
    record = SimpleNamespace(text=text, created_at=created_at)
    author = SimpleNamespace(did=did, handle=handle)
    post = SimpleNamespace(
        uri=uri,
        record=record,
        author=author,
        indexed_at=indexed_at or created_at,
    )
    return SimpleNamespace(post=post)


def test_run_collector_accountlist_is_a_true_noop_with_empty_account_list(tmp_path):
    """With the default empty ACCOUNT_LIST, this must not construct a
    Client, attempt login, or touch the DB -- confirmed by patching both
    Client and init_db and asserting neither was ever called."""
    with patch.object(collector_accountlist, "ACCOUNT_LIST", []), \
         patch.object(collector_accountlist, "Client") as mock_client_cls, \
         patch.object(collector_accountlist, "init_db") as mock_init_db, \
         patch.object(collector_accountlist, "_login_with_retry") as mock_login:
        collector_accountlist.run_collector_accountlist()

    mock_client_cls.assert_not_called()
    mock_init_db.assert_not_called()
    mock_login.assert_not_called()


def test_collect_from_account_filters_by_nyc_keywords_and_inserts_matches(tmp_path):
    db_path = str(tmp_path / "posts.db")
    conn = init_db(db_path)

    fake_client = MagicMock()
    fake_client.get_author_feed.return_value = SimpleNamespace(
        feed=[
            _fake_feed_item(
                uri="at://did:plc:alice/app.bsky.feed.post/1",
                did="did:plc:alice",
                handle="alice.bsky.social",
                text="grabbing coffee in Williamsburg this morning",
            ),
            _fake_feed_item(
                uri="at://did:plc:alice/app.bsky.feed.post/2",
                did="did:plc:alice",
                handle="alice.bsky.social",
                text="nothing location-related here at all",
            ),
        ]
    )

    inserted_count = collector_accountlist._collect_from_account(fake_client, conn, "alice.bsky.social")

    assert inserted_count == 1
    rows = get_unprocessed_posts(conn)
    assert len(rows) == 1
    assert rows[0]["post_uri"] == "at://did:plc:alice/app.bsky.feed.post/1"
    assert rows[0]["source"] == "bluesky_accountlist"
    assert rows[0]["did"] == "did:plc:alice"
    assert rows[0]["handle"] == "alice.bsky.social"
    conn.close()


def test_collect_from_account_dedups_against_already_seen_post_uri(tmp_path):
    db_path = str(tmp_path / "posts.db")
    conn = init_db(db_path)

    fake_client = MagicMock()
    fake_client.get_author_feed.return_value = SimpleNamespace(
        feed=[
            _fake_feed_item(
                uri="at://did:plc:bob/app.bsky.feed.post/1",
                did="did:plc:bob",
                handle="bob.bsky.social",
                text="walking through Central Park",
            ),
        ]
    )

    first_run = collector_accountlist._collect_from_account(fake_client, conn, "bob.bsky.social")
    second_run = collector_accountlist._collect_from_account(fake_client, conn, "bob.bsky.social")

    assert first_run == 1
    assert second_run == 0  # same post_uri again: INSERT OR IGNORE no-op

    rows = get_unprocessed_posts(conn)
    assert len(rows) == 1
    conn.close()


def test_collect_from_account_returns_zero_and_does_not_raise_on_fetch_error(tmp_path):
    db_path = str(tmp_path / "posts.db")
    conn = init_db(db_path)

    fake_client = MagicMock()
    fake_client.get_author_feed.side_effect = Exception("network error")

    inserted_count = collector_accountlist._collect_from_account(fake_client, conn, "broken.bsky.social")

    assert inserted_count == 0
    rows = get_unprocessed_posts(conn)
    assert len(rows) == 0
    conn.close()


def test_run_collector_accountlist_polls_every_configured_account(tmp_path):
    db_path = str(tmp_path / "posts.db")

    with patch.object(collector_accountlist, "ACCOUNT_LIST", ["alice.bsky.social", "bob.bsky.social"]), \
         patch.object(collector_accountlist, "DB_PATH", db_path), \
         patch.object(collector_accountlist, "Client") as mock_client_cls, \
         patch.object(collector_accountlist, "_login_with_retry") as mock_login:
        fake_client = MagicMock()
        mock_client_cls.return_value = fake_client
        fake_client.get_author_feed.return_value = SimpleNamespace(
            feed=[
                _fake_feed_item(
                    uri="at://did:plc:x/app.bsky.feed.post/1",
                    did="did:plc:x",
                    handle="x.bsky.social",
                    text="brunch in Astoria, Queens",
                ),
            ]
        )

        collector_accountlist.run_collector_accountlist()

    mock_login.assert_called_once()
    assert fake_client.get_author_feed.call_count == 2

    conn = init_db(db_path)
    rows = get_unprocessed_posts(conn)
    # Same post_uri returned for both configured accounts in this fake
    # setup, so the second insert is a dedup no-op -- exactly one row.
    assert len(rows) == 1
    conn.close()
