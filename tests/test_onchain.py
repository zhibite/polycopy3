import unittest

from polycopy.onchain import (
    ORDER_FILLED_V2_TOPIC,
    OrderFilledFeed,
    TokenMeta,
    TokenResolver,
    decode_order_filled,
    pad_address,
)
from polycopy.onchain_runner import _strip_mode_args
from polycopy.rules import dedupe_key

# Real V2 logs for 0xf705fa045201391d9632b7f3cde06a5e24453ca7.
SELL_LOG = {
    "address": "0xe111180000d2663c0091e4f400237545b87b996b",
    "topics": [
        ORDER_FILLED_V2_TOPIC,
        "0xfb3c60b1c7756903f2be3d371848a39ad2f20ae3e6a49dc6da0510093f474651",
        "0x000000000000000000000000f705fa045201391d9632b7f3cde06a5e24453ca7",
        "0x000000000000000000000000e111180000d2663c0091e4f400237545b87b996b",
    ],
    "data": (
        "0x0000000000000000000000000000000000000000000000000000000000000001"
        "650b6147205cd398603930b257681b34c6dfbcbafbcd3e86ee739d6e2d89bb50"
        "000000000000000000000000000000000000000000000000000000000cef2840"
        "000000000000000000000000000000000000000000000000000000000a16b3e0"
        "000000000000000000000000000000000000000000000000000000000005ae92"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000000"
    ),
    "blockNumber": "0x5d487a0",
    "transactionHash": "0x7dfc8e62358e62eac5c73e134b3e843139086725c141bef2f1ef8491b4d097dc",
    "transactionIndex": "0x19",
    "blockHash": "0xd8129ee3935208c251143119aed5d0deff7193c0a02a179c7e5c5673590382e4",
    "blockTimestamp": "0x6b040b2d",
    "logIndex": "0x109",
    "removed": False,
}

BUY_LOG = {
    "address": "0xe111180000d2663c0091e4f400237545b87b996b",
    "topics": [
        ORDER_FILLED_V2_TOPIC,
        "0x78e52a1e128c5815de49760baf4e8a73b5fb845fb6fbb866feec4d158c2ee10e",
        "0x000000000000000000000000f705fa045201391d9632b7f3cde06a5e24453ca7",
        "0x000000000000000000000000e111180000d2663c0091e4f400237545b87b996b",
    ],
    "data": (
        "0x0000000000000000000000000000000000000000000000000000000000000000"
        "ca7b1e4a4d47d24e079cab2df55c419a7f43e712cf20e3818d421540c16cb961"
        "0000000000000000000000000000000000000000000000000000000000d59f80"
        "0000000000000000000000000000000000000000000000000000000005f5e100"
        "000000000000000000000000000000000000000000000000000000000001d650"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000000"
    ),
    "blockNumber": "0x5d48757",
    "transactionHash": "0xf2043fc4ddc96c9ec553063e573303f063bf1bf1c1b40608f31fc9b73e204c41",
    "transactionIndex": "0x2b",
    "blockHash": "0xa7b3cd8f9d6a66614d78925ab889bc5f6710cb8a30f9a36f00b3405babdd8294",
    "blockTimestamp": "0x6b040ac0",
    "logIndex": "0x27b",
    "removed": False,
}


class DecodeOrderFilledTest(unittest.TestCase):
    def test_sell_matches_data_api(self):
        fill = decode_order_filled(SELL_LOG)
        self.assertIsNotNone(fill)
        self.assertEqual(fill.maker,
                         "0xf705fa045201391d9632b7f3cde06a5e24453ca7")
        self.assertEqual(fill.side, "SELL")
        self.assertEqual(fill.token_id,
                         "45703704411582499464332374069133096759975115041207788028520907227627254299472")
        self.assertAlmostEqual(fill.shares, 217.0)
        self.assertAlmostEqual(fill.gross_usd, 169.26)
        self.assertAlmostEqual(fill.fee_usd, 0.372370)
        self.assertAlmostEqual(fill.price, 0.78)
        self.assertAlmostEqual(fill.net_usd, 168.88763)
        self.assertEqual(fill.block_ts, 1795427117)
        self.assertEqual(fill.log_index, 265)

        meta = TokenMeta(
            condition_id="0x766996108017c3b6452c23db79ab1c714d8d9d9da846a4c316f5e047754a229d",
            token_id=fill.token_id,
            outcome="No",
            outcome_index=1,
            title="Will Ethereum dip to $1,250 by December 31, 2026?",
            slug="will-ethereum-dip-to-1250-by-december-31-2026-from-june-8",
        )
        row = fill.to_row(meta)
        self.assertEqual(row["side"], "SELL")
        self.assertEqual(row["size"], 217.0)
        self.assertEqual(row["usdc_size"], 168.88763)
        self.assertEqual(row["price"], 0.78)
        self.assertEqual(row["condition_id"], meta.condition_id)
        self.assertEqual(row["log_index"], 265)
        self.assertEqual(row["source"], "onchain")
        # Cross-source dedupe: the on-chain row must produce the same key as
        # the data-api row for the same fill (log_index is not part of it).
        data_api_row = {key: value for key, value in row.items()
                        if key not in ("log_index", "source")}
        self.assertEqual(dedupe_key(row), dedupe_key(data_api_row))

    def test_buy_matches_data_api(self):
        fill = decode_order_filled(BUY_LOG)
        self.assertIsNotNone(fill)
        self.assertEqual(fill.side, "BUY")
        self.assertAlmostEqual(fill.shares, 100.0)
        self.assertAlmostEqual(fill.gross_usd, 14.0)
        self.assertAlmostEqual(fill.fee_usd, 0.1204)
        self.assertAlmostEqual(fill.price, 0.14)
        self.assertAlmostEqual(fill.net_usd, 14.1204)
        self.assertEqual(fill.block_ts, 1795427008)

    def test_ignores_other_contracts_and_topics(self):
        other = dict(SELL_LOG, address="0x0000000000000000000000000000000000000000")
        self.assertIsNone(decode_order_filled(other))
        bad_topic = dict(SELL_LOG, topics=["0xdeadbeef"] + SELL_LOG["topics"][1:])
        self.assertIsNone(decode_order_filled(bad_topic))
        short = dict(SELL_LOG, data="0x00")
        self.assertIsNone(decode_order_filled(short))

    def test_pad_address(self):
        self.assertEqual(
            pad_address("0xf705fa045201391d9632b7f3cde06a5e24453ca7"),
            "0x000000000000000000000000f705fa045201391d9632b7f3cde06a5e24453ca7",
        )


class FakeRpc:
    def __init__(self, logs, head, timestamps):
        self._logs = logs
        self.head = head
        self.timestamps = timestamps
        self.calls = []

    def block_number(self):
        return self.head

    def logs(self, addresses, wallets, from_block, to_block):
        self.calls.append((from_block, to_block))
        out = []
        for log in self._logs:
            block = int(log["blockNumber"], 16)
            if from_block <= block <= to_block:
                out.append(log)
        return out

    def block_timestamp(self, block_number):
        return self.timestamps.get(block_number)


class FakeClob:
    def __init__(self, tokens):
        self.tokens = tokens
        self.calls = 0

    def market_by_token(self, token_id):
        self.calls += 1
        return {"condition_id": "c1"}

    def market(self, condition_id):
        return {
            "condition_id": condition_id,
            "question": "Test market",
            "market_slug": "test-market",
            "tokens": self.tokens,
        }


class TokenResolverTest(unittest.TestCase):
    def test_resolves_outcome_and_caches(self):
        clob = FakeClob([{"token_id": "t1", "outcome": "Yes"}])
        resolver = TokenResolver(clob)
        meta = resolver.resolve("t1")
        self.assertEqual(meta.condition_id, "c1")
        self.assertEqual(meta.outcome, "Yes")
        self.assertEqual(meta.outcome_index, 0)
        self.assertEqual(meta.title, "Test market")
        self.assertEqual(resolver.resolve("t1"), meta)
        self.assertEqual(clob.calls, 1)

    def test_rejects_token_missing_from_market(self):
        resolver = TokenResolver(FakeClob([{"token_id": "other"}]))
        self.assertIsNone(resolver.resolve("t1"))

    def test_resolves_market_end_timestamp(self):
        class ExpiryClob(FakeClob):
            def market(self, condition_id):
                body = super().market(condition_id)
                body["end_date_iso"] = "2026-01-01T00:00:00Z"
                return body

        resolver = TokenResolver(ExpiryClob([{"token_id": "t1", "outcome": "Yes"}]))
        meta = resolver.resolve("t1")
        self.assertEqual(meta.end_ts, 1_767_225_600)
        fill = decode_order_filled(BUY_LOG)
        row = fill.to_row(meta)
        self.assertEqual(row["end_ts"], 1_767_225_600)

    def test_old_cached_metadata_without_end_ts_loads(self):
        meta = TokenMeta(**{
            "condition_id": "c1", "token_id": "t1", "outcome": "Yes",
            "outcome_index": 0, "title": "T", "slug": "s", "event_slug": "e",
        })
        self.assertIsNone(meta.end_ts)


class FeedTest(unittest.TestCase):
    def test_catch_up_enqueues_and_dedupes(self):
        wallet = "0xf705fa045201391d9632b7f3cde06a5e24453ca7"
        head = 97_816_800
        logs = [
            dict(SELL_LOG, blockNumber=hex(head - 1),
                 logIndex="0x109", blockTimestamp=hex(1795427117)),
        ]
        rpc = FakeRpc(logs, head, {head - 1: 1795427117})
        feed = OrderFilledFeed(rpc, [wallet], ws_urls=[], start_block=head - 2)
        feed._catch_up()
        fill = feed.get(timeout=0)
        self.assertIsNotNone(fill)
        self.assertEqual(fill.tx_hash, SELL_LOG["transactionHash"])
        self.assertEqual(fill.block_ts, 1795427117)
        self.assertEqual(feed.last_block, head)

        feed._catch_up()
        self.assertIsNone(feed.get(timeout=0))
        self.assertEqual(len(feed._seen), 1)

    def test_live_log_does_not_advance_catchup_frontier(self):
        wallet = "0xf705fa045201391d9632b7f3cde06a5e24453ca7"
        start = 97_816_000
        feed = OrderFilledFeed(FakeRpc([], start, {}), [wallet],
                               ws_urls=[], start_block=start)
        feed._handle_log(SELL_LOG, catchup=False)
        self.assertIsNotNone(feed.get(timeout=0))
        self.assertEqual(feed.last_block, start)

    def test_feed_ignores_other_makers(self):
        head = 97_816_800
        other = dict(SELL_LOG, blockNumber=hex(head), logIndex="0x10a",
                     topics=[SELL_LOG["topics"][0],
                             SELL_LOG["topics"][1],
                             "0x" + "0" * 24 + "1234567890abcdef1234567890abcdef12345678",
                             SELL_LOG["topics"][3]])
        rpc = FakeRpc([other], head, {head: 1795427117})
        feed = OrderFilledFeed(rpc, ["0xf705fa045201391d9632b7f3cde06a5e24453ca7"],
                               ws_urls=[], start_block=head - 1)
        feed._catch_up()
        self.assertIsNone(feed.get(timeout=0))


class RestartArgsTest(unittest.TestCase):
    def test_strips_mode_override(self):
        self.assertEqual(
            _strip_mode_args(["onchain", "run", "--mode", "live",
                              "--config", "live.toml"]),
            ["onchain", "run", "--config", "live.toml"],
        )
        self.assertEqual(
            _strip_mode_args(["onchain", "run", "--mode=paper",
                              "--config", "live.toml"]),
            ["onchain", "run", "--config", "live.toml"],
        )
        self.assertEqual(
            _strip_mode_args(["onchain", "run", "--config", "live.toml"]),
            ["onchain", "run", "--config", "live.toml"],
        )


if __name__ == "__main__":
    unittest.main()
