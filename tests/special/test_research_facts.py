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
