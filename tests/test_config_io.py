import tempfile
import unittest
from pathlib import Path

from polycopy.config import (
    AccountConfig, AppConfig, LiveConfig, PaperConfig, RunConfig, TargetConfig,
    load_config,
)
from polycopy.web import config_io


class ConfigIOTest(unittest.TestCase):
    def test_roundtrip(self):
        cfg = AppConfig(
            account=AccountConfig(starting_cash=12345.0),
            paper=PaperConfig(poll_seconds=7, db_path="data/x.db", log_path="data/x.log"),
            targets={
                "t": TargetConfig(
                    address="0xabc",
                    buy_price_min=0.2,
                    buy_price_max=0.8,
                    scale_factor=0.03,
                    fee_rate=0.05,
                    exclude_event_substrings=["-updown-"],
                )
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            config_io.save_config(cfg, path)
            loaded = load_config(path)
            self.assertEqual(loaded.paper.poll_seconds, 7)
            self.assertEqual(loaded.targets["t"].buy_price_max, 0.8)
            self.assertEqual(loaded.targets["t"].scale_factor, 0.03)
            self.assertEqual(loaded.targets["t"].fee_rate, 0.05)
            self.assertEqual(loaded.targets["t"].exclude_event_substrings, ["-updown-"])

            # Second save creates a backup of the previous file.
            cfg.paper.poll_seconds = 9
            config_io.save_config(cfg, path)
            self.assertTrue(path.with_name("config.toml.bak").exists())
            self.assertEqual(load_config(path).paper.poll_seconds, 9)

    def test_unknown_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[account]\nstarting_cash = 1000.0\n\n"
                "[paper]\npoll_seconds = 5\nnew_option = 1\n\n"
                "[targets.t]\naddress = \"0xabc\"\nfuture_flag = true\n",
                encoding="utf-8",
            )
            cfg = load_config(path)
            self.assertEqual(cfg.paper.poll_seconds, 5)
            self.assertEqual(cfg.targets["t"].address, "0xabc")

    def test_none_fee_rate_is_omitted(self):
        cfg = AppConfig(
            account=AccountConfig(),
            paper=PaperConfig(),
            targets={"t": TargetConfig(address="0xabc", fee_rate=None)},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            config_io.save_config(cfg, path)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("fee_rate = None", text)
            self.assertIsNone(load_config(path).targets["t"].fee_rate)

    def test_run_and_live_roundtrip(self):
        cfg = AppConfig(
            account=AccountConfig(),
            paper=PaperConfig(),
            run=RunConfig(mode="live"),
            live=LiveConfig(
                dry_run=False,
                funder="0xfunder",
                private_key_env="TEST_POLY_KEY",
                signature_type=2,
                max_order_usd=25.0,
                max_daily_loss_usd=10.0,
                kill_switch_path="data/TEST_HALT",
            ),
            targets={"t": TargetConfig(address="0xabc")},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            config_io.save_config(cfg, path)
            loaded = load_config(path)
            self.assertEqual(loaded.run.mode, "live")
            self.assertFalse(loaded.live.dry_run)
            self.assertEqual(loaded.live.funder, "0xfunder")
            self.assertEqual(loaded.live.private_key_env, "TEST_POLY_KEY")
            self.assertEqual(loaded.live.max_order_usd, 25.0)
            self.assertEqual(loaded.live.kill_switch_path, "data/TEST_HALT")

    def test_live_signature_type_3_is_valid(self):
        live = LiveConfig(signature_type=3)
        live.validate()
        self.assertEqual(live.signature_type, 3)
        with self.assertRaises(ValueError):
            LiveConfig(signature_type=4).validate()

    def test_missing_run_section_defaults_to_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[account]\nstarting_cash = 1000.0\n\n"
                "[targets.t]\naddress = \"0xabc\"\n",
                encoding="utf-8",
            )
            cfg = load_config(path)
            self.assertEqual(cfg.run.mode, "paper")
            self.assertTrue(cfg.live.dry_run)

    def test_multi_target_and_maker_fields_roundtrip(self):
        cfg = AppConfig(
            account=AccountConfig(),
            paper=PaperConfig(
                risk_check_seconds=45, orphan_grace_seconds=90.0),
            targets={
                "a": TargetConfig(
                    address="0xAAA", sizing_mode="fixed",
                    fixed_notional_usd=10.0, min_target_usd=1000.0,
                    min_hours_to_end=2.0, maker_entry_enabled=True,
                    maker_timeout_seconds=300, fak_max_premium_pct=0.02,
                    take_profit_pct=0.10, stop_loss_pct=0.20,
                ),
                "b": TargetConfig(
                    address="0xbbb", sizing_mode="fixed",
                    fixed_notional_usd=20.0, min_hours_to_end=0.0,
                    maker_entry_enabled=False,
                ),
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            config_io.save_config(cfg, path)
            loaded = load_config(path)
            self.assertEqual(sorted(loaded.targets), ["a", "b"])
            self.assertEqual(loaded.targets["a"].address, "0xaaa")
            self.assertEqual(loaded.targets["b"].address, "0xbbb")
            self.assertAlmostEqual(
                loaded.targets["a"].fixed_notional_usd, 10.0)
            self.assertAlmostEqual(loaded.targets["b"].fixed_notional_usd, 20.0)
            self.assertAlmostEqual(
                loaded.targets["a"].min_hours_to_end, 2.0)
            self.assertTrue(loaded.targets["a"].maker_entry_enabled)
            self.assertFalse(loaded.targets["b"].maker_entry_enabled)
            self.assertAlmostEqual(
                loaded.targets["a"].take_profit_pct, 0.10)
            self.assertAlmostEqual(
                loaded.targets["a"].stop_loss_pct, 0.20)
            self.assertEqual(loaded.paper.risk_check_seconds, 45)
            self.assertAlmostEqual(loaded.paper.orphan_grace_seconds, 90.0)

    def test_duplicate_target_address_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[account]\nstarting_cash = 1000.0\n\n"
                "[targets.a]\naddress = \"0xAbC\"\n\n"
                "[targets.b]\naddress = \"0xabc\"\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_config(path)

    def test_unsafe_target_name_is_rejected_on_save(self):
        cfg = AppConfig(
            account=AccountConfig(), paper=PaperConfig(),
            targets={"bad name": TargetConfig(address="0xabc")},
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                config_io.save_config(cfg, Path(tmp) / "config.toml")


if __name__ == "__main__":
    unittest.main()
