import unittest

import watch_store


class ClaimTrackingTests(unittest.TestCase):
    def run_record(self, run_id, heading, score, source_ids):
        return {"id": run_id, "created_at": f"2026-11-{20 if run_id == 'baseline' else 21}T08:00:00+08:00",
                "articles": [{"id": key, "url": f"https://example.org/{key}", "source": f"Source {key}",
                              "published_at": "2026-11-20", "search_language": "en",
                              "passages": [{"id": f"{key}-P1", "text": "Original English passage."}]}
                             for key in source_ids],
                "assessment": {"risk_points": [{"heading": heading, "risk_score": score,
                                                 "evidence_ids": source_ids,
                                                 "counter_evidence_ids": [],
                                                 "translation_note": ""}]}}

    def test_same_claim_gets_stable_id_and_delta(self):
        baseline = self.run_record("baseline", "China trade restrictions may raise costs", 9, ["A1", "A2"])
        claims, changes = watch_store.reconcile_claims({"id": "watch-1"}, baseline)
        self.assertEqual(changes[0]["kind"], "new")
        updated = self.run_record("update", "China trade restrictions may raise costs", 12, ["A1", "A3"])
        claims, changes = watch_store.reconcile_claims({"id": "watch-1", "claims": claims}, updated)
        self.assertEqual(len(claims), 1)
        self.assertEqual(changes[0]["kind"], "updated")
        self.assertEqual(changes[0]["new_sources"], 1)
        self.assertEqual(changes[0]["risk_delta"], 3)
        self.assertEqual(updated["assessment"]["risk_points"][0]["claim_id"], claims[0]["id"])

    def test_distinct_claim_remains_separate(self):
        baseline = self.run_record("baseline", "Trade restrictions raise supply chain costs", 9, ["A1"])
        claims, _ = watch_store.reconcile_claims({"id": "watch-1"}, baseline)
        updated = self.run_record("update", "Overseas investment falls as banks tighten lending", 15, ["A9"])
        claims, changes = watch_store.reconcile_claims({"id": "watch-1", "claims": claims}, updated)
        self.assertEqual(len(claims), 2)
        self.assertEqual(changes[0]["kind"], "new")

    def test_missing_claim_is_marked_not_repeated(self):
        baseline = self.run_record("baseline", "Trade restrictions raise supply chain costs", 9, ["A1"])
        claims, _ = watch_store.reconcile_claims({"id": "watch-1"}, baseline)
        empty = self.run_record("update", "", None, [])
        empty["assessment"]["risk_points"] = []
        claims, changes = watch_store.reconcile_claims({"id": "watch-1", "claims": claims}, empty)
        self.assertEqual(changes[0]["kind"], "not_repeated")
        self.assertEqual(claims[0]["history"][-1]["status"], "not_repeated")


if __name__ == "__main__":
    unittest.main()
