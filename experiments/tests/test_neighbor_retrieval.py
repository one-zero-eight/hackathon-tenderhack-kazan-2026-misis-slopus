import importlib.util
from pathlib import Path
import sys

import numpy as np

EXPERIMENTS = Path(__file__).resolve().parents[1] / "experiments"
sys.path.insert(0, str(EXPERIMENTS))
spec = importlib.util.spec_from_file_location("neighbor_retrieval_eval", EXPERIMENTS / "neighbor_retrieval_eval.py")
nre = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nre)


def test_colbert_maxsim_matches_bruteforce():
    rng = np.random.default_rng(0)
    q_vecs = [rng.normal(size=(3, 4)), rng.normal(size=(2, 4))]
    d_vecs = [rng.normal(size=(5, 4)), rng.normal(size=(1, 4)), rng.normal(size=(4, 4))]
    pack = lambda vs: {"colbert": np.concatenate(vs).astype(np.float16),
                       "colbert_offsets": np.concatenate([[0], np.cumsum([len(v) for v in vs])])}
    got = nre.colbert_scores(pack(q_vecs), pack(d_vecs), device="cpu")
    want = [[(q.astype(np.float16).astype(np.float32) @ d.astype(np.float16).astype(np.float32).T).max(1).mean()
             for d in d_vecs] for q in q_vecs]
    np.testing.assert_allclose(got, want, rtol=1e-2, atol=1e-2)


def test_rrf_sums_reciprocal_ranks():
    a = np.array([[.9, .8, .1]])   # места 1, 2, 3
    b = np.array([[.1, .95, .9]])  # места 3, 1, 2
    fused = nre.rrf(a, b)
    np.testing.assert_allclose(fused[0], [1/61 + 1/63, 1/62 + 1/61, 1/63 + 1/62])
    assert fused[0].argmax() == 1
