import pytest

from services.crypto_candidate_generator import (
    Candidate,
    CandidateGenerator,
    CandidateGeneratorError,
    ParamRange,
    SearchSpace,
    dedupe_against_registry,
)


def _space(**overrides):
    payload = dict(
        dataset_version="data-1",
        feature_version="features-v1",
        lookback=ParamRange(low=2, high=6, step=1),
        threshold=ParamRange(low=0.0001, high=0.0003, step=0.0001),
        signal_feature=ParamRange(choices=("bb_percent_b", "rsi14")),
        model_variant=ParamRange(choices=("local-feature-momentum-v1",)),
    )
    payload.update(overrides)
    return SearchSpace(**payload)


# ---------------------------------------------------------------------------
# ParamRange
# ---------------------------------------------------------------------------


def test_param_range_requires_either_bounds_or_choices():
    with pytest.raises(CandidateGeneratorError):
        ParamRange()
    with pytest.raises(CandidateGeneratorError):
        ParamRange(low=1, high=2, step=1, choices=("a",))


def test_param_range_rejects_invalid_bounds():
    with pytest.raises(CandidateGeneratorError):
        ParamRange(low=5, high=1, step=1)
    with pytest.raises(CandidateGeneratorError):
        ParamRange(low=1, high=5, step=0)


def test_param_range_rejects_empty_choices():
    with pytest.raises(CandidateGeneratorError):
        ParamRange(choices=())


def test_param_range_values_are_inclusive_grid():
    values = ParamRange(low=1, high=3, step=1).values()
    assert values == (1, 2, 3)


# ---------------------------------------------------------------------------
# SearchSpace
# ---------------------------------------------------------------------------


def test_search_space_requires_lookback_and_threshold_ranges():
    with pytest.raises(CandidateGeneratorError):
        _space(lookback=ParamRange(choices=(1, 2)))
    with pytest.raises(CandidateGeneratorError):
        _space(threshold=ParamRange(choices=(0.1,)))


def test_search_space_requires_nonneg_lookback_and_threshold_low():
    with pytest.raises(CandidateGeneratorError):
        _space(lookback=ParamRange(low=0, high=3, step=1))
    with pytest.raises(CandidateGeneratorError):
        _space(threshold=ParamRange(low=-1, high=1, step=1))


def test_search_space_requires_discrete_feature_and_model():
    with pytest.raises(CandidateGeneratorError):
        _space(signal_feature=ParamRange(low=1, high=2, step=1))
    with pytest.raises(CandidateGeneratorError):
        _space(model_variant=ParamRange(low=1, high=2, step=1))


def test_search_space_version_is_stable_content_hash():
    space_a = _space()
    space_b = _space()
    assert space_a.version == space_b.version
    space_c = _space(threshold=ParamRange(low=0.0001, high=0.0004, step=0.0001))
    assert space_c.version != space_a.version


def test_search_space_grid_size():
    space = _space()
    assert space.grid_size == 5 * 3 * 2 * 1


# ---------------------------------------------------------------------------
# CandidateGenerator
# ---------------------------------------------------------------------------


def test_generator_rejects_nonpositive_budget():
    with pytest.raises(CandidateGeneratorError):
        CandidateGenerator(search_space=_space(), seed=1, trial_budget=0)


def test_generator_rejects_budget_over_grid_size():
    space = _space()
    with pytest.raises(CandidateGeneratorError):
        CandidateGenerator(search_space=space, seed=1, trial_budget=space.grid_size + 1)


def test_generator_is_deterministic_for_same_seed():
    space = _space()
    first = CandidateGenerator(search_space=space, seed=7, trial_budget=5).generate()
    second = CandidateGenerator(search_space=space, seed=7, trial_budget=5).generate()
    assert [c.candidate_id for c in first] == [c.candidate_id for c in second]
    assert [c.strategy_params for c in first] == [c.strategy_params for c in second]


def test_generator_different_seeds_diverge():
    space = _space()
    first = CandidateGenerator(search_space=space, seed=1, trial_budget=5).generate()
    second = CandidateGenerator(search_space=space, seed=2, trial_budget=5).generate()
    assert [c.candidate_id for c in first] != [c.candidate_id for c in second]


def test_generator_respects_trial_budget_and_uniqueness():
    space = _space()
    candidates = CandidateGenerator(search_space=space, seed=3, trial_budget=6).generate()
    assert len(candidates) == 6
    assert len({c.candidate_id for c in candidates}) == 6


def test_generator_params_stay_within_declared_ranges():
    space = _space()
    candidates = CandidateGenerator(search_space=space, seed=4, trial_budget=space.grid_size).generate()
    for candidate in candidates:
        params = candidate.strategy_params
        assert space.lookback.contains(params["lookback"])
        assert space.threshold.contains(round(params["threshold"], 10))
        assert space.signal_feature.contains(params["signal_feature"])
        assert space.model_variant.contains(params["model_variant"])


def test_generator_full_budget_covers_entire_grid_without_repeats():
    space = _space()
    candidates = CandidateGenerator(search_space=space, seed=5, trial_budget=space.grid_size).generate()
    assert len(candidates) == space.grid_size
    assert len({c.candidate_id for c in candidates}) == space.grid_size


def test_generator_marks_candidates_as_research_stage_only():
    space = _space()
    candidates = CandidateGenerator(search_space=space, seed=6, trial_budget=3).generate()
    for candidate in candidates:
        assert candidate.data_stage == "RESEARCH"


def test_generator_rejects_holdout_flagged_strategy_params():
    from services.crypto_candidate_generator import _assert_no_holdout_leakage

    bad = Candidate(
        candidate_id="cand-x",
        search_space_version="search-x",
        seed=1,
        trial_index=0,
        strategy_params={"lookback": 2, "threshold": 0.1, "holdout": True},
    )
    with pytest.raises(CandidateGeneratorError):
        _assert_no_holdout_leakage([bad])


def test_generator_rejects_non_research_data_stage():
    from services.crypto_candidate_generator import _assert_no_holdout_leakage

    bad = Candidate(
        candidate_id="cand-y",
        search_space_version="search-y",
        seed=1,
        trial_index=0,
        strategy_params={"lookback": 2, "threshold": 0.1},
        data_stage="HOLDOUT",
    )
    with pytest.raises(CandidateGeneratorError):
        _assert_no_holdout_leakage([bad])


def test_candidate_to_dict_roundtrips_fields():
    space = _space()
    candidate = CandidateGenerator(search_space=space, seed=8, trial_budget=1).generate()[0]
    payload = candidate.to_dict()
    assert payload["candidate_id"] == candidate.candidate_id
    assert payload["strategy_params"] == dict(candidate.strategy_params)
    assert payload["data_stage"] == "RESEARCH"


# ---------------------------------------------------------------------------
# dedupe_against_registry
# ---------------------------------------------------------------------------


def test_dedupe_against_registry_drops_known_candidates():
    space = _space()
    candidates = CandidateGenerator(search_space=space, seed=9, trial_budget=4).generate()
    known = [candidates[0].candidate_id, candidates[2].candidate_id]
    remaining = dedupe_against_registry(candidates, known_config_hashes=known)
    assert {c.candidate_id for c in remaining} == {candidates[1].candidate_id, candidates[3].candidate_id}


def test_dedupe_against_registry_is_noop_when_nothing_known():
    space = _space()
    candidates = CandidateGenerator(search_space=space, seed=10, trial_budget=3).generate()
    remaining = dedupe_against_registry(candidates, known_config_hashes=[])
    assert remaining == candidates
