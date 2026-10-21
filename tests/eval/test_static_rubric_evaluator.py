"""Unit tests for static_rubric_evaluator.py and the golden-set keyword matcher.

Run with:  python3 -m unittest tests/eval/test_static_rubric_evaluator.py -v
Expected values are worked out by hand in the comments.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, REPO_ROOT)


import static_rubric_evaluator as sre  # noqa: E402
from scripts.eval.golden_set import extract_key_facts, match_urgent_keywords  # noqa: E402


def _case(segment="S", reference="", key_facts=None):
    return {"segment": segment, "key_facts": key_facts or [],
            "reference": {"response": {"parts": [{"text": reference}]}}}


class TestNormalization(unittest.TestCase):
    def test_strips_handles_urls_signoffs_entities(self):
        self.assertEqual(sre.tokenize("@AppleSupport Try this &amp; that https://t.co/x ^AB"),
                         ["try", "this", "that"])


class TestRougeL(unittest.TestCase):
    def test_identical(self):
        self.assertAlmostEqual(sre.rouge_l("reset your phone", "reset your phone"), 1.0)

    def test_hand_computed(self):
        # cand=[the,cat,sat] ref=[the,cat,on,the,mat,sat]; LCS=3, P=1, R=0.5, F1=2/3
        self.assertAlmostEqual(sre.rouge_l("the cat sat", "the cat on the mat sat"), 2 / 3, places=4)

    def test_no_overlap(self):
        self.assertEqual(sre.rouge_l("alpha beta", "gamma delta"), 0.0)


class TestBleu(unittest.TestCase):
    def test_identical_corpus(self):
        self.assertAlmostEqual(sre.corpus_bleu(["a b c d e"], ["a b c d e"]), 1.0)

    def test_hand_computed_corpus(self):
        # p1=4/5 p2=3/4 p3=2/3 p4=1/2 -> (0.2)^(1/4)=0.66874; equal lengths -> BP=1
        self.assertAlmostEqual(sre.corpus_bleu(["a b c d e"], ["a b c d f"]), 0.2 ** 0.25, places=4)

    def test_brevity_penalty(self):
        # cand 5 tokens all matching, ref 10 tokens -> BP=exp(1-10/5)=e^-1
        ref = "a b c d e f g h i j"
        self.assertAlmostEqual(sre.corpus_bleu(["a b c d e"], [ref]), 2.718281828 ** -1, places=4)

    def test_corpus_pools_counts(self):
        # A 1-token case has zero 4-grams on its own but must not zero the pooled score.
        score = sre.corpus_bleu(["a b c d e", "x"], ["a b c d e", "x"])
        self.assertAlmostEqual(score, 1.0)


class TestPolicy(unittest.TestCase):
    def test_violations_detected(self):
        v = sre.policy_violations("Based on our records (twcs_123), DM us. Email me at a@b.com")
        self.assertIn("doc_id_in_answer", v)
        self.assertIn("retrieval_disclosure", v)
        self.assertIn("pii_email", v)

    def test_lazy_dm(self):
        self.assertIn("lazy_dm_redirect", sre.policy_violations("Please DM us."))
        self.assertNotIn("lazy_dm_redirect", sre.policy_violations(
            "Hold the setup button for 15 seconds. If that fails, DM us your serial number."))

    def test_clean_answer(self):
        self.assertEqual(sre.policy_violations("Hold the setup button for 15 seconds."), [])


class TestEvaluate(unittest.TestCase):
    def test_scope(self):
        for seg in ("U", "V"):
            self.assertFalse(sre.evaluate_static_rubric(_case(seg), "x", [], False, False)["applicable"])

    def test_judge_scores_take_precedence(self):
        r = sre.evaluate_static_rubric(_case("S", "reset it", ["reset it"]), "reset it", ["unrelated"],
                                       True, True, False,
                                       judge_scores={"faithfulness": 0.95, "grounding_recall": 0.9})
        self.assertEqual(r["faithfulness"]["source"], "llm_judge")
        self.assertTrue(r["faithfulness"]["pass"])

    def test_policy_violation_fails_faithfulness(self):
        r = sre.evaluate_static_rubric(_case("S", "x"), "See twcs_99 for details.", ["see details"],
                                       True, True, False, judge_scores={"faithfulness": 1.0})
        self.assertEqual(r["faithfulness"]["score"], 0.0)

    def test_out_of_corpus(self):
        ok = sre.evaluate_static_rubric(_case("O"), "I don't have specific information on that. "
                                        "I can connect you with a human agent.", [], True, False, True)
        bad = sre.evaluate_static_rubric(_case("O"), "Yes, helicopters deliver in 2 hours.", [], True, True, False)
        self.assertTrue(ok["faithfulness"]["pass"])
        self.assertFalse(bad["faithfulness"]["pass"])
        self.assertIsNone(ok["lexical"]["rouge_l"])  # O references are synthetic

    def test_aggregate_flags_proxy(self):
        r = sre.evaluate_static_rubric(_case("S", "reset the phone", ["reset the phone"]),
                                       "reset the phone", ["reset the phone"], True, True, False)
        agg = sre.aggregate_results([r, sre.evaluate_static_rubric(_case("U"), "", [], False, False)])
        self.assertEqual(agg["cases_applicable"], 1)
        self.assertIsNotNone(agg["warning"])


class TestRubricScope(unittest.TestCase):
    def test_label_based_scope(self):
        urgent_labelled = dict(_case("S"), expected_sentiment="URGENT_FRUSTRATED", search_ready=True)
        not_ready = dict(_case("S"), expected_sentiment="NEUTRAL_FACTUAL", search_ready=False)
        ok = dict(_case("S"), expected_sentiment="NEUTRAL_FACTUAL", search_ready=True)
        self.assertFalse(sre.evaluate_static_rubric(urgent_labelled, "x", [], True, True)["applicable"])
        self.assertFalse(sre.evaluate_static_rubric(not_ready, "x", [], True, True)["applicable"])
        self.assertTrue(sre.evaluate_static_rubric(ok, "x", [], True, True)["applicable"])

    def test_rubric_dataset_only_neutral_ready(self):
        import json
        path = os.path.join(HERE, "datasets", "faq_rubric_dataset.json")
        if not os.path.exists(path):
            self.skipTest("run scripts/build_eval_datasets.py first")
        cases = json.load(open(path))["eval_cases"]
        self.assertTrue(cases)
        for c in cases:
            self.assertEqual(c["expected_sentiment"], "NEUTRAL_FACTUAL", c["eval_case_id"])
            self.assertTrue(c["search_ready"], c["eval_case_id"])
            self.assertNotIn(c["segment"], ("U", "V"))


class TestKeyFacts(unittest.TestCase):
    def test_drops_greetings_and_apologies(self):
        facts = extract_key_facts("Hi Karine, sorry for the delay. Please accept our apologies. "
                                  "Hold the side button for 10 seconds to restart the phone. ^AB")
        self.assertEqual(facts, ["Hold the side button for 10 seconds to restart the phone"])

    def test_greeting_only_reply_has_no_facts(self):
        self.assertEqual(extract_key_facts("Hi there! Please DM us. Thanks!"), [])


class TestKeywordMatcher(unittest.TestCase):
    def test_no_substring_false_positive(self):
        self.assertEqual(match_urgent_keywords("this is not an issue"), [])

    def test_whole_word_and_phrase(self):
        self.assertEqual(match_urgent_keywords("i will sue you"), ["sue"])
        self.assertIn("late shipping", match_urgent_keywords("very late   shipping again"))


if __name__ == "__main__":
    unittest.main()
