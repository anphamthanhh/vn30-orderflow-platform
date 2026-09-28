//! Per-level bid/ask imbalance and stacked imbalances from market-by-price depth.
//!
//! A level is flagged when `|bid_vol − ask_vol| / max(bid_vol, ask_vol, 1) >= threshold`;
//! `stacked_min_levels` consecutive flagged levels in one direction form a stack.

/// Imbalance direction of one level.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum ImbalanceDirection {
    BidHeavy,
    AskHeavy,
    Neutral,
}

/// Result for a single price level.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct LevelImbalance {
    pub price: f64,
    pub bid_vol: f64,
    pub ask_vol: f64,
    pub ratio: f64,
    pub direction: ImbalanceDirection,
}

/// A run of consecutive levels with the same imbalance direction.
#[derive(Debug, Clone, PartialEq)]
pub struct StackedImbalance {
    pub levels: Vec<LevelImbalance>,
    pub direction: ImbalanceDirection,
}

/// Detects and classifies imbalances from DOM bid/ask levels.
#[derive(Debug, Clone)]
pub struct ImbalanceDetector {
    /// Ratio threshold (0.0–1.0). Levels with ratio >= threshold are flagged.
    pub threshold: f64,
    /// Minimum consecutive levels for stacked detection.
    pub stacked_min_levels: usize,
}

impl ImbalanceDetector {
    /// Create a detector; the defaults are `threshold` 0.7 and `stacked_min_levels` 3.
    pub fn new(threshold: f64, stacked_min_levels: usize) -> Self {
        Self {
            threshold,
            stacked_min_levels,
        }
    }

    /// Return per-level results and stacked imbalances for `(price, volume)` levels.
    ///
    /// `bids` and `asks` are ordered best first and matched by index (`bids[i]` vs `asks[i]`).
    pub fn analyze(
        &self,
        bids: &[(f64, f64)],
        asks: &[(f64, f64)],
    ) -> (Vec<LevelImbalance>, Vec<StackedImbalance>) {
        let max_len = bids.len().max(asks.len());
        let mut results = Vec::with_capacity(max_len);
        let mut run_start: Option<usize> = None;
        let mut run_dir: Option<ImbalanceDirection> = None;
        let mut stacked = Vec::new();

        for i in 0..max_len {
            let bv = bids.get(i).copied().unwrap_or((0.0, 0.0)).1;
            let av = asks.get(i).copied().unwrap_or((0.0, 0.0)).1;
            let price = bids
                .get(i)
                .map(|(p, _)| *p)
                .or_else(|| asks.get(i).map(|(p, _)| *p))
                .unwrap_or(0.0);

            let max_vol = bv.max(av).max(1.0);
            let ratio = (bv - av).abs() / max_vol;
            let direction = if bv > av && ratio >= self.threshold {
                ImbalanceDirection::BidHeavy
            } else if av > bv && ratio >= self.threshold {
                ImbalanceDirection::AskHeavy
            } else {
                ImbalanceDirection::Neutral
            };

            let level = LevelImbalance {
                price,
                bid_vol: bv,
                ask_vol: av,
                ratio,
                direction,
            };
            results.push(level);

            // Track consecutive runs for stacked detection
            self.advance_run(
                direction,
                i,
                &mut run_start,
                &mut run_dir,
                &mut stacked,
                &results,
            );
        }

        // flush the final run
        if let (Some(start), Some(dir)) = (run_start, run_dir) {
            let count = max_len - start;
            if count >= self.stacked_min_levels {
                stacked.push(StackedImbalance {
                    levels: results[start..].to_vec(),
                    direction: dir,
                });
            }
        }

        (results, stacked)
    }

    fn advance_run(
        &self,
        direction: ImbalanceDirection,
        i: usize,
        run_start: &mut Option<usize>,
        run_dir: &mut Option<ImbalanceDirection>,
        stacked: &mut Vec<StackedImbalance>,
        results: &[LevelImbalance],
    ) {
        match direction {
            ImbalanceDirection::Neutral => {
                if let (Some(start), Some(dir)) = (run_start.take(), run_dir.take()) {
                    if i - start >= self.stacked_min_levels {
                        stacked.push(StackedImbalance {
                            levels: results[start..i].to_vec(),
                            direction: dir,
                        });
                    }
                }
            }
            dir => {
                match *run_start {
                    None => {
                        *run_start = Some(i);
                        *run_dir = Some(dir);
                    }
                    Some(start) => {
                        if *run_dir == Some(dir) {
                            // same direction — continue
                            *run_start = Some(start);
                        } else {
                            // direction changed — flush previous
                            if let Some(prev_dir) = run_dir.take() {
                                if i - start >= self.stacked_min_levels {
                                    stacked.push(StackedImbalance {
                                        levels: results[start..i].to_vec(),
                                        direction: prev_dir,
                                    });
                                }
                            }
                            *run_start = Some(i);
                            *run_dir = Some(dir);
                        }
                    }
                }
            }
        }
    }
}

impl Default for ImbalanceDetector {
    fn default() -> Self {
        Self::new(0.7, 3)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn balanced_levels_are_neutral() {
        let detector = ImbalanceDetector::new(0.7, 3);
        let bids = vec![(1251.3, 100.0)];
        let asks = vec![(1251.5, 100.0)];
        let (levels, stacks) = detector.analyze(&bids, &asks);
        assert_eq!(levels.len(), 1);
        assert_eq!(levels[0].direction, ImbalanceDirection::Neutral);
        assert!(stacks.is_empty());
    }

    #[test]
    fn imbalanced_level_above_threshold() {
        let detector = ImbalanceDetector::new(0.5, 3);
        let bids = vec![(1251.3, 200.0)];
        let asks = vec![(1251.5, 50.0)];
        let (levels, _) = detector.analyze(&bids, &asks);
        assert_eq!(levels[0].direction, ImbalanceDirection::BidHeavy);
        assert!((levels[0].ratio - 0.75).abs() < 0.01); // |200-50|/200
    }

    #[test]
    fn below_threshold_is_neutral() {
        let detector = ImbalanceDetector::new(0.8, 3);
        let bids = vec![(1251.3, 100.0)];
        let asks = vec![(1251.5, 60.0)];
        let (levels, _) = detector.analyze(&bids, &asks);
        assert_eq!(levels[0].direction, ImbalanceDirection::Neutral);
        assert!((levels[0].ratio - 0.4).abs() < 0.01);
    }

    #[test]
    fn stacked_imbalance_detected() {
        let detector = ImbalanceDetector::new(0.5, 3);
        let bids = vec![
            (1251.3, 200.0),
            (1251.2, 180.0),
            (1251.1, 190.0),
            (1251.0, 150.0),
        ];
        let asks = vec![
            (1251.5, 50.0),
            (1251.6, 40.0),
            (1251.7, 30.0),
            (1251.8, 20.0),
        ];

        // by index: 200/50, 180/40, 190/30, 150/20 → ratios 0.75 to 0.87, all BidHeavy
        let (levels, stacks) = detector.analyze(&bids, &asks);
        assert_eq!(levels.len(), 4);
        assert_eq!(stacks.len(), 1);
        assert_eq!(stacks[0].direction, ImbalanceDirection::BidHeavy);
        assert_eq!(stacks[0].levels.len(), 4);
    }

    #[test]
    fn short_run_does_not_create_stack() {
        let detector = ImbalanceDetector::new(0.5, 3);
        let bids = vec![(1251.3, 200.0), (1251.2, 180.0)];
        let asks = vec![(1251.5, 50.0), (1251.6, 80.0)];
        let (_, stacks) = detector.analyze(&bids, &asks);
        assert!(stacks.is_empty()); // only 2 consecutive, need 3
    }

    #[test]
    fn mixed_directions_produce_separate_stacks() {
        let detector = ImbalanceDetector::new(0.5, 2);
        let bids = vec![
            (1251.3, 200.0),
            (1251.2, 180.0),
            (1251.1, 50.0),
            (1251.0, 40.0),
            (1250.9, 160.0),
            (1250.8, 140.0),
        ];
        let asks = vec![
            (1251.5, 50.0),
            (1251.6, 40.0),
            (1251.7, 200.0),
            (1251.8, 180.0),
            (1251.9, 50.0),
            (1252.0, 40.0),
        ];
        // Level 0-1: BidHeavy, Level 2-3: AskHeavy, Level 4-5: BidHeavy
        let (_, stacks) = detector.analyze(&bids, &asks);
        assert_eq!(stacks.len(), 3);
        assert_eq!(stacks[0].direction, ImbalanceDirection::BidHeavy);
        assert_eq!(stacks[1].direction, ImbalanceDirection::AskHeavy);
        assert_eq!(stacks[2].direction, ImbalanceDirection::BidHeavy);
    }

    #[test]
    fn empty_input_produces_empty_output() {
        let detector = ImbalanceDetector::default();
        let (levels, stacks) = detector.analyze(&[], &[]);
        assert!(levels.is_empty());
        assert!(stacks.is_empty());
    }

    #[test]
    fn default_threshold_is_0_7_and_stacked_min_3() {
        let detector = ImbalanceDetector::default();
        assert!((detector.threshold - 0.7).abs() < 0.01);
        assert_eq!(detector.stacked_min_levels, 3);
    }
}
