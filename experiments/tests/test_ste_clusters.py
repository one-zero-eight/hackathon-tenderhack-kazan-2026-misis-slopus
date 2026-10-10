import numpy as np
import pytest

from ktru_autofill.ste_clusters import Cluster, cluster_name, cluster_ste, normalize_name, pair_metrics, truncate


def test_normalize_name_drops_digits_and_punctuation():
    assert normalize_name("Тетради, 48 листов!") == normalize_name("тетрадь 96 лист")


def test_cluster_name_common_words_then_majority():
    assert cluster_name(["Ручка шариковая синяя 0,7", "Ручка шариковая красная", "ручка ШАРИКОВАЯ"]) == "Ручка шариковая"
    assert cluster_name(["Степлер №24", "Степлер металл", "Скобы"]) == "Степлер"
    assert cluster_name([]) == ""


def test_cluster_ste_respects_categories_and_threshold():
    v = truncate(np.array([[1., 0.], [.99, .1], [0., 1.], [1., 0.]]), None)
    clusters = cluster_ste([1, 2, 3, 4], ["A", "A", "A", "B"], ["Диван", "Диван угловой", "Стол", "Диван"], v, .5)
    assert sorted(sorted(c.ste_ids) for c in clusters) == [[1, 2], [3], [4]]
    assert {c.category for c in clusters if 4 in c.ste_ids} == {"B"}
    big = next(c for c in clusters if len(c.ste_ids) == 2)
    assert big.name == "Диван" and big.medoid in {1, 2}
    merged = cluster_ste([1, 2, 3], ["A"] * 3, ["a", "b", "c"], v[:3], 10.)
    assert len(merged) == 1
    with pytest.raises(ValueError):
        cluster_ste([1], ["A", "B"], ["a"], v[:1], .5)


def test_truncate_renormalizes():
    out = truncate(np.array([[3., 4., 12.]]), 2)
    np.testing.assert_allclose(out, [[.6, .8]])


def test_pair_metrics():
    clusters = [Cluster(0, "A", "x", [1, 2, 3]), Cluster(1, "A", "y", [4])]
    m = pair_metrics(clusters, {1: "g", 2: "g", 3: "h", 4: "h"})
    # верные пары: (1,2) найдена, (3,4) нет; ложные склейки: (1,3), (2,3)
    assert (m["precision"], m["recall"]) == (pytest.approx(1 / 3), .5)


def test_average_linkage_default_and_ward_option():
    v = truncate(np.array([[1., 0.], [.999, .04], [.9, .44], [0., 1.]]), None)
    avg = cluster_ste([1, 2, 3, 4], ["A"] * 4, ["a", "b", "c", "d"], v, .01)
    assert sorted(sorted(c.ste_ids) for c in avg) == [[1, 2], [3], [4]]
    ward = cluster_ste([1, 2, 3, 4], ["A"] * 4, ["a", "b", "c", "d"], v, .5, linkage="ward")
    assert sorted(sorted(c.ste_ids) for c in ward) == [[1, 2, 3], [4]]
