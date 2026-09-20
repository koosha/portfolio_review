"""Market-adapter configuration: adapter selection and the provider budget bounds."""

import unittest

from portfolio_lab.config import DEFAULTS, validate_config
from portfolio_research.market_data import ADAPTER_VERSION


class ConfigTests(unittest.TestCase):
    def test_defaults_name_the_adapter_and_its_provider_budget(self):
        data = validate_config({})["data"]
        self.assertEqual(data["market_adapter"], ADAPTER_VERSION)
        self.assertEqual(data["provider_timeout_seconds"], 30)
        self.assertEqual(data["provider_min_interval_seconds"], 0.25)
        self.assertEqual(data["provider_refresh_hours"], 20)
        self.assertEqual(data["assumed_publication_lag_days"], 90)
        self.assertIsNone(data["model_provider"])
        self.assertEqual(data["news_limit"], 20)
        self.assertEqual(DEFAULTS["data"]["market_adapter"], ADAPTER_VERSION)

    def test_the_legacy_adapter_stays_selectable(self):
        self.assertEqual(
            validate_config({"data": {"market_adapter": "legacy"}})["data"]["market_adapter"],
            "legacy",
        )

    def test_out_of_range_provider_settings_are_refused(self):
        for patch_values in [
            {"market_adapter": "gpt"},
            {"provider_timeout_seconds": 0},
            {"provider_timeout_seconds": 3000},
            {"provider_min_interval_seconds": -1},
            {"provider_refresh_hours": -1},
            {"assumed_publication_lag_days": 1.5},
            {"assumed_publication_lag_days": 400},
            {"news_limit": 0},
            {"news_limit": 5.5},
            {"model_provider": ""},
            {"model_provider": 5},
        ]:
            with self.assertRaises(ValueError, msg=patch_values):
                validate_config({"data": patch_values})

    def test_a_model_provider_class_path_is_accepted(self):
        config = validate_config({"data": {"model_provider": "acme.adapters:Summarizer"}})
        self.assertEqual(config["data"]["model_provider"], "acme.adapters:Summarizer")


if __name__ == "__main__":
    unittest.main()
