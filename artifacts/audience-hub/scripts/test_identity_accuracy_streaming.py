"""Verification-only, SQLite tests; no app DB or worker required."""
import sqlite3
import unittest
from itertools import combinations

import identity_acceptance as accuracy


class AccuracyTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        accuracy._sqlite_database(self.db)

    def tearDown(self):
        self.db.close()

    def add(self, source, external, person, profile, household=None, email=None):
        self.db.execute("INSERT INTO truth VALUES (?,?,?,?)",
                        (source, external, person, household))
        if profile is not None:
            self.db.execute("INSERT INTO actual VALUES (?,?,?,?,?,?,?)",
                            (source, external, profile, email, None, None, None))

    def test_pairwise_weights_missing_records_unknown_labels_and_households(self):
        self.add("donor_crm", "1", "a", 1, "hh")
        self.add("esp", "1", "a", 1, "hh")
        self.add("giving_platform", "1", "a", None, "hh")
        self.add("donor_crm", "2", "b", 1, "hh")
        self.add("esp", "2", "b", 2, "hh")
        self.add("donor_crm", "3", "c", 3)
        self.add("esp", "3", "c", 3)
        self.add("donor_crm", "4", "d", 3)
        self.add("donor_crm", "5", "unmatched-person", None)
        self.db.execute("INSERT INTO actual VALUES ('unknown','x',1,NULL,NULL,NULL,NULL)")
        result = accuracy._pairwise_accuracy(self.db)
        self.assertEqual(result["truth_records"], 9)
        self.assertEqual(result["unmatched_truth_records"], 2)
        self.assertEqual(result["people_without_any_matched_record"], 1)
        self.assertEqual(result["unknown_actual_records"], 1)
        self.assertEqual(result["truth_positive_pairs_all_records"], 5)
        self.assertEqual(result["predicted_positive_pairs_known_labels"], 6)
        self.assertEqual(result["true_positive_pairs"], 2)
        self.assertEqual(result["false_positive_pairs_strict"], 4)
        self.assertEqual(result["false_negative_pairs_including_unmatched"], 3)
        self.assertEqual(result["intentional_household_cross_person_pairs"], 2)
        self.assertEqual(result["other_false_positive_pairs"], 2)
        self.assertEqual(result["predicted_pairs_with_unknown_label"], 3)
        self.assertAlmostEqual(result["precision_strict_known_labels"], 2 / 6)
        self.assertAlmostEqual(result["recall_strict_all_truth"], 2 / 5)
        self.assertAlmostEqual(result["precision_household_exempt_known_labels"], 4 / 6)
        self.assertEqual(result["multisource_profiles"], 2)
        # Independent brute-force pair oracle; no SQL aggregation assumption.
        truth = list(self.db.execute("SELECT source_key,external_id,person_id FROM truth"))
        actual = {(s, e): p for s, e, p in self.db.execute(
            "SELECT source_key,external_id,profile_id FROM actual")}
        true_pairs = tp = predicted = 0
        for left, right in combinations(truth, 2):
            same_person = left[2] == right[2]
            same_profile = (left[:2] in actual and right[:2] in actual
                            and actual[left[:2]] == actual[right[:2]])
            true_pairs += same_person
            tp += same_person and same_profile
            predicted += same_profile
        self.assertEqual((true_pairs, tp, predicted), (5, 2, 6))

    def test_streamed_diagnostics_preserve_household_exemption_and_split(self):
        self.add("donor_crm", "1", "a", 1, "hh", "shared@example.org")
        self.add("esp", "1", "a", 2, "hh", "other@example.org")
        self.add("donor_crm", "2", "b", 1, "hh", "shared@example.org")
        self.add("donor_crm", "3", "c", 3, None, "mixed@example.org")
        self.add("esp", "4", "d", 3, None, "mixed@example.org")
        self.db.execute("INSERT INTO crm_csv VALUES ('1','shared@example.org',NULL)")
        split, patterns = accuracy._split_pattern_counts(self.db)
        self.assertEqual(split, 1)
        self.assertEqual(sum(patterns.values()), 1)
        cases = accuracy._false_merge_candidates(self.db)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["profile_id"], 3)
        self.assertEqual(cases[0]["persons"], ["c", "d"])
        self.assertEqual(cases[0]["shared_identifiers"],
                         {"email": {"mixed@example.org"}, "phone": set()})

    def test_empty_population_has_undefined_metrics_not_perfect_scores(self):
        result = accuracy._pairwise_accuracy(self.db)
        self.assertIsNone(result["precision_strict_known_labels"])
        self.assertIsNone(result["recall_strict_all_truth"])
        self.assertEqual(result["truth_records"], 0)


if __name__ == "__main__":
    unittest.main()
