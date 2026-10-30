"""Attribute routing and zero-shot scoring for the image library.

The prompt panel is worth testing without loading CLIP: parsing decides *which*
question is asked, scoring decides the ranking, and both are pure functions of
data we control here.
"""

import unittest

import numpy as np

from src.services import image_attributes as attrs


class ConstraintParsingTests(unittest.TestCase):
    """Which words map onto which visual axis."""

    @staticmethod
    def _groups(query):
        return {constraint.group: constraint.labels for constraint in attrs.constraints_for(query)}

    def test_light_colour_also_accepts_white(self):
        self.assertEqual(self._groups("浅色衣服")["color"], ("浅色", "白色"))

    def test_black_clothes(self):
        self.assertEqual(self._groups("黑色衣服")["color"], ("黑色", "深色"))

    def test_belly_is_read_as_build(self):
        self.assertEqual(self._groups("肚子胖的人")["build"], ("偏胖",))

    def test_flashy_outfit(self):
        self.assertEqual(self._groups("穿的衣服比较花哨的人")["style"], ("花哨",))

    def test_suit_is_read_as_formal(self):
        self.assertEqual(self._groups("穿西装的男人")["style"], ("正装",))

    def test_two_axes_combine(self):
        groups = self._groups("穿黑色衣服的胖子")
        self.assertEqual(groups["color"], ("黑色", "深色"))
        self.assertEqual(groups["build"], ("偏胖",))

    def test_negated_attribute_is_dropped(self):
        self.assertNotIn("build", self._groups("不瘦的人"))

    def test_negation_redirects_to_the_opposite_style(self):
        self.assertEqual(self._groups("不花哨的简约穿搭")["style"], ("素净",))

    def test_short_kow_is_a_positive_match_for_short(self):
        # 个子不高 means short; the 不 is part of the phrase, not a negation.
        self.assertEqual(self._groups("个子不高")["height"], ("偏矮",))

    def test_english_query_parses_to_nothing(self):
        self.assertEqual(attrs.constraints_for("a man in a black suit"), ())

    def test_unrelated_chinese_query_parses_to_nothing(self):
        self.assertEqual(attrs.constraints_for("红酒瓶"), ())

    def test_blank_query(self):
        self.assertEqual(attrs.constraints_for("   "), ())

    def test_at_most_one_constraint_per_group(self):
        constraints = attrs.constraints_for("浅色衣服配黑色裤子")
        self.assertEqual(len([c for c in constraints if c.group == "color"]), 1)

    def test_summary_text(self):
        constraints = attrs.constraints_for("穿黑色衣服的胖子")
        self.assertEqual(
            attrs.describe_constraints(constraints), "衣服颜色=黑色 · 体型=偏胖"
        )


class _PanelMixin:
    """Install a tiny two-label panel so scoring never loads CLIP."""

    def install_panel(self):
        self._saved = dict(attrs._GROUP_CACHE)
        attrs._GROUP_CACHE.clear()
        attrs._GROUP_CACHE["||"] = {
            "color": (
                ["黑色", "白色"],
                np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
            ),
            "build": (
                ["偏胖", "偏瘦"],
                np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
            ),
            "person": (
                ["有人", "无人"],
                np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
            ),
        }

    def tearDown(self):
        attrs._GROUP_CACHE.clear()
        attrs._GROUP_CACHE.update(getattr(self, "_saved", {}))


class ScoringTests(_PanelMixin, unittest.TestCase):
    VECTORS = np.array([[1.0, 0.0], [0.0, 1.0], [0.0, 1.0]], dtype=np.float32)

    def setUp(self):
        self.install_panel()

    def test_black_label_wins_on_the_black_vector(self):
        labels, matrix = attrs.probabilities(self.VECTORS, config=None)["color"]
        self.assertEqual(labels[0], "黑色")
        self.assertGreater(matrix[0][0], 0.99)
        self.assertLess(matrix[0][2], 0.01)

    def test_probabilities_sum_to_one_per_row(self):
        _, matrix = attrs.probabilities(self.VECTORS, config=None)["color"]
        np.testing.assert_allclose(matrix.sum(axis=0), 1.0, rtol=1e-5)

    def test_constraint_score_multiplies_groups(self):
        constraints = attrs.constraints_for("黑色衣服")
        probs = attrs.probabilities(self.VECTORS, config=None)
        score = attrs.score_constraints(probs, constraints)
        self.assertIsNotNone(score)
        self.assertGreater(score[0], score[1])

    def test_two_axes_narrow_the_ranking(self):
        constraints = attrs.constraints_for("黑色衣服的胖子")
        probs = attrs.probabilities(self.VECTORS, config=None)
        score = attrs.score_constraints(probs, constraints)
        self.assertGreater(score[0], 0.98)
        self.assertLess(score[2], 0.01)

    def test_unknown_label_yields_no_score(self):
        probs = attrs.probabilities(self.VECTORS, config=None)
        bogus = (attrs.Constraint(group="color", labels=("紫色",), needle="紫色"),)
        self.assertIsNone(attrs.score_constraints(probs, bogus))

    def test_person_gate_marks_images_without_people(self):
        probs = attrs.probabilities(self.VECTORS, config=None)
        gate = attrs.person_gate(probs, len(self.VECTORS))
        self.assertEqual(gate.tolist(), [1.0, 0.0, 0.0])

    def test_person_gate_is_all_ones_without_the_group(self):
        self.assertEqual(attrs.person_gate({}, 3).tolist(), [1.0, 1.0, 1.0])

    def test_empty_matrix_yields_no_probabilities(self):
        self.assertEqual(attrs.probabilities(np.zeros((0, 2), dtype=np.float32)), {})

    def test_height_is_flagged_low_confidence(self):
        self.assertTrue(attrs.is_low_confidence(attrs.constraints_for("个子矮的人")))
        self.assertFalse(attrs.is_low_confidence(attrs.constraints_for("黑色衣服")))

    def test_only_person_flavoured_groups_need_the_gate(self):
        self.assertTrue(attrs.needs_person_gate(attrs.constraints_for("浅色衣服")))
        self.assertFalse(attrs.needs_person_gate(()))


class ServiceRoutingTests(_PanelMixin, unittest.TestCase):
    """search_by_query must prefer attributes over the literal scan."""

    def setUp(self):
        self.install_panel()

    @staticmethod
    def _hit():
        return {
            "image_id": "a" * 32,
            "image_path": "/tmp/a.jpg",
            "rel_path": "a.jpg",
            "library_root": "/tmp",
            "caption": "",
            "people": [],
            "tags": [],
            "people_count": 1,
            "score": 0.9,
            "match": "attribute",
        }

    def test_attribute_hits_short_circuit_the_literal_scan(self):
        from src.services import image_index_service as service

        original_attr = service.search_by_attributes
        original_literal = service._literal_scan
        service.search_by_attributes = lambda *a, **k: [self._hit()]  # type: ignore[assignment]
        service._literal_scan = lambda *a, **k: self.fail("literal scan must not run")  # type: ignore[assignment]
        try:
            hits = service.search_by_query("浅色衣服", top_k=5)
        finally:
            service.search_by_attributes = original_attr  # type: ignore[assignment]
            service._literal_scan = original_literal  # type: ignore[assignment]
        self.assertEqual([hit["match"] for hit in hits], ["attribute"])

    def test_query_without_attribute_vocabulary_is_not_scored(self):
        from src.services import image_index_service as service

        self.assertEqual(service.search_by_attributes("红酒瓶"), [])

    def test_empty_index_returns_no_attribute_hits(self):
        from src.services import image_index_service as service
        from src.storage import image_lance_store as store

        original_iter = store.iter_image_rows
        original_key = service._matrix_cache_key
        service._VECTOR_MATRIX_CACHE.clear()
        store.iter_image_rows = lambda **kwargs: iter(())  # type: ignore[assignment]
        service._matrix_cache_key = lambda config=None: ("test", 0.0)  # type: ignore[assignment]
        try:
            self.assertEqual(service.search_by_attributes("黑色衣服"), [])
        finally:
            store.iter_image_rows = original_iter  # type: ignore[assignment]
            service._matrix_cache_key = original_key  # type: ignore[assignment]
            service._VECTOR_MATRIX_CACHE.clear()

    def test_vector_matrix_is_read_once_per_revision(self):
        from src.services import image_index_service as service
        from src.storage import image_lance_store as store

        rows = [
            {"image_id": "a" * 32, "image_path": "/tmp/a.jpg", "vector": [1.0, 0.0]},
            {"image_id": "b" * 32, "image_path": "/tmp/b.jpg", "vector": [0.0, 1.0]},
        ]
        calls = []
        original_iter = store.iter_image_rows
        original_key = service._matrix_cache_key
        service._VECTOR_MATRIX_CACHE.clear()
        store.iter_image_rows = lambda **kwargs: calls.append(1) or iter(rows)  # type: ignore[assignment]
        service._matrix_cache_key = lambda config=None: ("unit", 1.0)  # type: ignore[assignment]
        try:
            first = service._load_vector_matrix()
            second = service._load_vector_matrix()
        finally:
            store.iter_image_rows = original_iter  # type: ignore[assignment]
            service._matrix_cache_key = original_key  # type: ignore[assignment]
            service._VECTOR_MATRIX_CACHE.clear()
        self.assertEqual(len(calls), 1)
        self.assertEqual(first[1].shape, (2, 2))
        self.assertIs(first[1], second[1])


if __name__ == "__main__":
    unittest.main()
