import pytest

from services.crypto_candidate_cursor import (
    CandidateCursor,
    CandidateCursorError,
)
from services.crypto_candidate_generator import ParamRange, SearchSpace


def _space(**overrides):
    payload = dict(
        dataset_version="data-1",
        feature_version="features-v1",
        lookback=ParamRange(low=2, high=4, step=1),
        threshold=ParamRange(low=0.0001, high=0.0002, step=0.0001),
        signal_feature=ParamRange(choices=("bb_percent_b",)),
        model_variant=ParamRange(choices=("local-feature-momentum-v1",)),
    )
    payload.update(overrides)
    return SearchSpace(**payload)


# grid_size for _space() default = 3 (lookback) * 2 (threshold) * 1 * 1 = 6


def test_advance_requires_run_bucket():
    cursor = CandidateCursor(":memory:")
    with pytest.raises(CandidateCursorError):
        cursor.advance(search_space=_space(), seed=1, run_bucket="")


def test_sequential_advances_return_increasing_distinct_indices(tmp_path):
    cursor = CandidateCursor(tmp_path / "cursor.db")
    space = _space()
    first = cursor.advance(search_space=space, seed=1, run_bucket="bucket-1")
    second = cursor.advance(search_space=space, seed=1, run_bucket="bucket-2")
    third = cursor.advance(search_space=space, seed=1, run_bucket="bucket-3")
    assert [first.trial_index, second.trial_index, third.trial_index] == [0, 1, 2]
    assert first.replayed is False
    assert second.replayed is False
    assert third.replayed is False


def test_restart_does_not_reset_cursor(tmp_path):
    db_path = tmp_path / "cursor.db"
    space = _space()
    first_process_cursor = CandidateCursor(db_path)
    first_process_cursor.advance(search_space=space, seed=1, run_bucket="bucket-1")
    first_process_cursor.advance(search_space=space, seed=1, run_bucket="bucket-2")

    # Simulate a new process: fresh CandidateCursor instance, same db_path.
    second_process_cursor = CandidateCursor(db_path)
    third = second_process_cursor.advance(search_space=space, seed=1, run_bucket="bucket-3")
    assert third.trial_index == 2


def test_retry_in_same_bucket_is_idempotent_not_advancing(tmp_path):
    cursor = CandidateCursor(tmp_path / "cursor.db")
    space = _space()
    first = cursor.advance(search_space=space, seed=1, run_bucket="bucket-1")
    retry = cursor.advance(search_space=space, seed=1, run_bucket="bucket-1")
    assert retry.trial_index == first.trial_index
    assert retry.replayed is True

    # A genuinely new bucket still advances past the retried index.
    next_new = cursor.advance(search_space=space, seed=1, run_bucket="bucket-2")
    assert next_new.trial_index == first.trial_index + 1


def test_exhausted_search_space_is_fail_closed_not_wrap_around(tmp_path):
    cursor = CandidateCursor(tmp_path / "cursor.db")
    space = _space()  # grid_size = 6
    for index in range(6):
        result = cursor.advance(search_space=space, seed=1, run_bucket=f"bucket-{index}")
        assert result.trial_index == index
    with pytest.raises(CandidateCursorError):
        cursor.advance(search_space=space, seed=1, run_bucket="bucket-overflow")


def test_different_seeds_are_independent(tmp_path):
    cursor = CandidateCursor(tmp_path / "cursor.db")
    space = _space()
    seed1_first = cursor.advance(search_space=space, seed=1, run_bucket="bucket-1")
    seed2_first = cursor.advance(search_space=space, seed=2, run_bucket="bucket-1")
    assert seed1_first.trial_index == 0
    assert seed2_first.trial_index == 0


def test_different_search_space_versions_are_independent(tmp_path):
    cursor = CandidateCursor(tmp_path / "cursor.db")
    space_a = _space()
    space_b = _space(threshold=ParamRange(low=0.0001, high=0.0003, step=0.0001))
    assert space_a.version != space_b.version

    a_first = cursor.advance(search_space=space_a, seed=1, run_bucket="bucket-1")
    b_first = cursor.advance(search_space=space_b, seed=1, run_bucket="bucket-1")
    assert a_first.trial_index == 0
    assert b_first.trial_index == 0


def test_peek_does_not_reserve(tmp_path):
    cursor = CandidateCursor(tmp_path / "cursor.db")
    space = _space()
    assert cursor.peek(search_space_version=space.version, seed=1) == 0
    cursor.advance(search_space=space, seed=1, run_bucket="bucket-1")
    assert cursor.peek(search_space_version=space.version, seed=1) == 1
    # Peeking again does not advance further.
    assert cursor.peek(search_space_version=space.version, seed=1) == 1


def test_peek_unknown_pair_returns_zero(tmp_path):
    cursor = CandidateCursor(tmp_path / "cursor.db")
    assert cursor.peek(search_space_version="search-unknown", seed=99) == 0
