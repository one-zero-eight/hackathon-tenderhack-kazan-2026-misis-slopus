import pytest

from ktru_autofill import category_metrics as cm


def test_classification_micro_macro_weighted():
    gold = ["A", "A", "A", "B"]
    pred = ["A", "A", "B", "C"]
    m = cm.classification(gold, pred)
    assert m["micro_precision"] == m["micro_recall"] == m["micro_f1"] == .5
    # A: P=1, R=2/3; B: P=0 (предсказан раз, неверно), R=0; C только в ответах — не в среднем.
    assert m["categories"] == 2
    assert m["macro_precision"] == pytest.approx(.5)
    assert m["macro_recall"] == pytest.approx(1 / 3)
    assert m["weighted_recall"] == pytest.approx(.5)
    assert m["weighted_f1"] == pytest.approx(3 / 4 * .8)


def test_abstention_trades_recall_for_precision():
    gold, pred, conf = ["A", "B", "C"], ["A", "B", "X"], [.9, .6, .3]
    low, high = cm.with_abstention(gold, pred, conf, [0, .5])
    assert low["precision"] == low["recall"] == pytest.approx(2 / 3)
    assert high == {"threshold": .5, "answered": 2, "coverage": pytest.approx(2 / 3), "precision": 1.0,
                    "recall": pytest.approx(2 / 3), "f1": pytest.approx(.8)}


def test_candidate_sets():
    (one, two), = [cm.candidate_sets(["A", "B"], [["A", "C"], ["C", "B"]], (1, 2))]
    assert one["recall"] == .5 and one["precision"] == .5
    assert two["recall"] == 1 and two["precision"] == .5 and two["mean_set_size"] == 2
    with pytest.raises(ValueError):
        cm.classification([], [])
