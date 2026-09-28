//! Property tests for `orderflow_engine::cvd::CvdTracker` through its public API.
//!
//! Covers batch-sum parity, bounded history, rejection of non-finite input, rollover and
//! reset semantics, and the cached min/max.

use orderflow_engine::cvd::CvdTracker;
use proptest::prelude::*;

/// Relative float tolerance for CVD sums (deltas bounded by 1e4, <=400 terms).
const TOL: f64 = 1e-9;

/// Finite tick deltas in a realistic VN30F1M contract-volume range.
fn finite_delta() -> impl Strategy<Value = f64> {
    -1.0e4f64..1.0e4
}

/// Deltas mixing finite values with NaN / +Inf / -Inf garbage.
fn any_delta() -> impl Strategy<Value = f64> {
    prop_oneof![
        4 => -1.0e4f64..1.0e4,
        1 => Just(f64::NAN),
        1 => Just(f64::INFINITY),
        1 => Just(f64::NEG_INFINITY),
    ]
}

/// Observable state, to check that invalid input changes nothing.
fn snapshot(cvd: &CvdTracker) -> (f64, f64, usize, f64, f64, f64) {
    (
        cvd.cumulative_delta,
        cvd.initial_value,
        cvd.len(),
        cvd.min_value,
        cvd.max_value,
        cvd.current_value(),
    )
}

proptest! {
    /// Incremental `update()` equals the batch sum of the same deltas.
    #[test]
    fn incremental_cvd_equals_batch_sum(deltas in prop::collection::vec(finite_delta(), 0..200)) {
        let mut cvd = CvdTracker::new(10_000);
        for (i, d) in deltas.iter().enumerate() {
            cvd.update(*d, i as i64);
        }
        let batch: f64 = deltas.iter().sum();
        prop_assert!(
            (cvd.current_value() - batch).abs() <= TOL * (1.0 + batch.abs()),
            "incremental {} != batch {}",
            cvd.current_value(),
            batch
        );
    }

    /// `update()` returns `current_value()`, including for rejected non-finite input.
    #[test]
    fn update_return_matches_current_value(deltas in prop::collection::vec(any_delta(), 0..100)) {
        let mut cvd = CvdTracker::new(50);
        for (i, d) in deltas.iter().enumerate() {
            let returned = cvd.update(*d, i as i64);
            let current = cvd.current_value();
            prop_assert!(
                (returned - current).abs() <= TOL * (1.0 + current.abs()),
                "update returned {returned} but current_value is {current}"
            );
        }
    }

    /// The history never exceeds `max_points`.
    #[test]
    fn history_never_exceeds_max_points(
        max_points in 0usize..64,
        deltas in prop::collection::vec(finite_delta(), 0..200),
    ) {
        let mut cvd = CvdTracker::new(max_points);
        for (i, d) in deltas.iter().enumerate() {
            cvd.update(*d, i as i64);
            prop_assert!(
                cvd.len() <= max_points,
                "history len {} exceeded max_points {}",
                cvd.len(),
                max_points
            );
        }
    }

    /// Cached `min_value` / `max_value` equal a fresh scan of the history, across evictions.
    #[test]
    fn cached_bounds_match_history_scan(
        max_points in 1usize..16,
        deltas in prop::collection::vec(finite_delta(), 1..150),
    ) {
        let mut cvd = CvdTracker::new(max_points);
        for (i, d) in deltas.iter().enumerate() {
            cvd.update(*d, i as i64);
            let true_min = cvd.history.iter().map(|p| p.value).fold(f64::INFINITY, f64::min);
            let true_max = cvd
                .history
                .iter()
                .map(|p| p.value)
                .fold(f64::NEG_INFINITY, f64::max);
            prop_assert!(
                (cvd.min_value - true_min).abs() <= TOL * (1.0 + true_min.abs()),
                "cached min {} != scanned min {}",
                cvd.min_value,
                true_min
            );
            prop_assert!(
                (cvd.max_value - true_max).abs() <= TOL * (1.0 + true_max.abs()),
                "cached max {} != scanned max {}",
                cvd.max_value,
                true_max
            );
        }
    }

    /// NaN and infinite deltas leave every observable field bit-for-bit unchanged.
    #[test]
    fn non_finite_deltas_leave_state_unchanged(
        prefix in prop::collection::vec(finite_delta(), 0..50),
        garbage in prop::collection::vec(
            prop_oneof![
                Just(f64::NAN),
                Just(f64::INFINITY),
                Just(f64::NEG_INFINITY),
            ],
            1..10,
        ),
    ) {
        let mut cvd = CvdTracker::new(100);
        for (i, d) in prefix.iter().enumerate() {
            cvd.update(*d, i as i64);
        }
        let before = snapshot(&cvd);
        for (i, g) in garbage.iter().enumerate() {
            cvd.update(*g, 1_000 + i as i64);
            prop_assert_eq!(snapshot(&cvd), before, "garbage delta {} mutated state", g);
        }
        // Sane input still works after the garbage burst.
        let expected = before.5 + 1.0;
        let after = cvd.update(1.0, 2_000);
        prop_assert!((after - expected).abs() <= TOL * (1.0 + expected.abs()));
    }

    /// `rollover()` keeps `current_value()` as the seed and clears history and bounds; later
    /// deltas add to the seed.
    #[test]
    fn rollover_preserves_value_and_clears_history(
        session1 in prop::collection::vec(finite_delta(), 1..80),
        session2 in prop::collection::vec(finite_delta(), 1..80),
    ) {
        let mut cvd = CvdTracker::new(1_000);
        for (i, d) in session1.iter().enumerate() {
            cvd.update(*d, i as i64);
        }
        let carried = cvd.current_value();

        cvd.rollover();
        prop_assert!((cvd.current_value() - carried).abs() <= TOL * (1.0 + carried.abs()));
        prop_assert!(cvd.is_empty());
        prop_assert_eq!(cvd.cumulative_delta, 0.0);
        prop_assert_eq!(cvd.min_value, 0.0);
        prop_assert_eq!(cvd.max_value, 0.0);

        for (i, d) in session2.iter().enumerate() {
            cvd.update(*d, 10_000 + i as i64);
        }
        let expected = carried + session2.iter().sum::<f64>();
        prop_assert!(
            (cvd.current_value() - expected).abs() <= TOL * (1.0 + expected.abs()),
            "post-rollover value {} != seed+sum {}",
            cvd.current_value(),
            expected
        );
    }
}

/// With zero capacity the history stays empty while the value still accumulates.
#[test]
fn max_points_zero_history_always_empty_but_value_accumulates() {
    let mut cvd = CvdTracker::new(0);
    let v1 = cvd.update(10.0, 1);
    let v2 = cvd.update(-4.0, 2);
    assert!(cvd.is_empty(), "zero-capacity history must stay empty");
    assert_eq!(cvd.len(), 0);
    assert!((v1 - 10.0).abs() <= TOL);
    assert!((v2 - 6.0).abs() <= TOL);
    assert!((cvd.current_value() - 6.0).abs() <= TOL);
    // bounds over an empty buffer fall back to 0.0
    assert_eq!(cvd.min_value, 0.0);
    assert_eq!(cvd.max_value, 0.0);
}

/// `reset()` also clears the seed installed by `rollover()`.
#[test]
fn reset_clears_rollover_seed() {
    let mut cvd = CvdTracker::new(100);
    cvd.update(25.0, 1);
    cvd.rollover();
    assert!((cvd.current_value() - 25.0).abs() <= TOL);
    cvd.reset();
    assert_eq!(cvd.current_value(), 0.0);
    assert_eq!(cvd.initial_value, 0.0);
    assert_eq!(cvd.cumulative_delta, 0.0);
    assert!(cvd.is_empty());
}
