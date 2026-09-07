from datetime import datetime, timezone
import unittest

from track_special.arx_campaign.research import ResearchClassification, ResearchFact


class ResearchFactTests(unittest.TestCase):
    def test_fact_requires_aware_times_and_preserves_classification(self) -> None:
        now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        fact = ResearchFact("ARX is described as Arcium's token", "https://www.arcium.com/", None, now, now, None, "fixture", ResearchClassification.OFFICIAL_FACT)
        self.assertEqual("official_fact", fact.classification)
        with self.assertRaises(ValueError):
            ResearchFact("bad", "https://example.invalid", None, datetime(2026, 9, 7), now, None, "fixture", ResearchClassification.UNVERIFIED)

    def test_future_observation_cannot_leak_into_replay(self) -> None:
        observed = datetime(2026, 9, 7, tzinfo=timezone.utc)
        fact = ResearchFact("dated", "https://www.arcium.com/", None, observed, observed, None, "fixture", ResearchClassification.INFERENCE)
        self.assertFalse(fact.usable_at(datetime(2026, 9, 6, tzinfo=timezone.utc)))
        self.assertTrue(fact.usable_at(observed))
