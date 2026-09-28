//! Incremental Wilder ADX (average directional index) with +DI and −DI.
//!
//! ADX measures trend strength in either direction; +DI and −DI give the direction. Fed
//! completed bars, it returns `None` until warm (about `2 * period` bars).
//!
//! ```text
//! +DM = (H-Hprev) if it exceeds (Lprev-L) and > 0 else 0
//! -DM = (Lprev-L) if it exceeds (H-Hprev) and > 0 else 0
//! TR  = max(H-L, |H-Cprev|, |L-Cprev|)
//! smX = Wilder-smoothed sum over `period`
//! +DI = 100·sm(+DM)/sm(TR);  -DI = 100·sm(-DM)/sm(TR)
//! DX  = 100·|+DI − -DI| / (+DI + -DI)
//! ADX = Wilder-smoothed DX
//! ```

/// One warm ADX reading.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct AdxValue {
    pub plus_di: f64,
    pub minus_di: f64,
    pub adx: f64,
}

/// O(1)-per-bar Wilder ADX.
#[derive(Debug, Clone)]
pub struct Adx {
    period: usize,
    prev_high: Option<f64>,
    prev_low: Option<f64>,
    prev_close: Option<f64>,
    // Wilder-smoothed sums (seeded by summing the first `period`).
    sm_tr: Option<f64>,
    sm_plus_dm: Option<f64>,
    sm_minus_dm: Option<f64>,
    seed_tr: f64,
    seed_plus: f64,
    seed_minus: f64,
    seed_count: usize,
    // ADX smoothing of DX.
    adx: Option<f64>,
    dx_seed_sum: f64,
    dx_seed_count: usize,
    last: Option<AdxValue>,
}

impl Adx {
    /// `period` == 0 clamps to 1.
    pub fn new(period: usize) -> Self {
        Self {
            period: period.max(1),
            prev_high: None,
            prev_low: None,
            prev_close: None,
            sm_tr: None,
            sm_plus_dm: None,
            sm_minus_dm: None,
            seed_tr: 0.0,
            seed_plus: 0.0,
            seed_minus: 0.0,
            seed_count: 0,
            adx: None,
            dx_seed_sum: 0.0,
            dx_seed_count: 0,
            last: None,
        }
    }

    /// Feed one completed bar; returns the reading once warm. Invalid bars are ignored.
    pub fn on_bar(&mut self, high: f64, low: f64, close: f64) -> Option<AdxValue> {
        if !high.is_finite() || !low.is_finite() || !close.is_finite() || high < low {
            return self.last;
        }
        let (ph, pl, pc) = match (self.prev_high, self.prev_low, self.prev_close) {
            (Some(h), Some(l), Some(c)) => (h, l, c),
            _ => {
                self.prev_high = Some(high);
                self.prev_low = Some(low);
                self.prev_close = Some(close);
                return None; // first bar: no directional movement yet
            }
        };
        let up = high - ph;
        let down = pl - low;
        let plus_dm = if up > down && up > 0.0 { up } else { 0.0 };
        let minus_dm = if down > up && down > 0.0 { down } else { 0.0 };
        let tr = (high - low).max((high - pc).abs()).max((low - pc).abs());
        self.prev_high = Some(high);
        self.prev_low = Some(low);
        self.prev_close = Some(close);

        let n = self.period as f64;
        // Seed or Wilder-smooth the directional sums.
        match (self.sm_tr, self.sm_plus_dm, self.sm_minus_dm) {
            (Some(str_), Some(spdm), Some(smdm)) => {
                self.sm_tr = Some(str_ - str_ / n + tr);
                self.sm_plus_dm = Some(spdm - spdm / n + plus_dm);
                self.sm_minus_dm = Some(smdm - smdm / n + minus_dm);
            }
            _ => {
                self.seed_tr += tr;
                self.seed_plus += plus_dm;
                self.seed_minus += minus_dm;
                self.seed_count += 1;
                if self.seed_count >= self.period {
                    self.sm_tr = Some(self.seed_tr);
                    self.sm_plus_dm = Some(self.seed_plus);
                    self.sm_minus_dm = Some(self.seed_minus);
                } else {
                    return None; // still seeding directional sums
                }
            }
        }

        let sm_tr = self.sm_tr.unwrap();
        let plus_di = if sm_tr > 0.0 {
            100.0 * self.sm_plus_dm.unwrap() / sm_tr
        } else {
            0.0
        };
        let minus_di = if sm_tr > 0.0 {
            100.0 * self.sm_minus_dm.unwrap() / sm_tr
        } else {
            0.0
        };
        let di_sum = plus_di + minus_di;
        let dx = if di_sum > 0.0 {
            100.0 * (plus_di - minus_di).abs() / di_sum
        } else {
            0.0
        };

        // Seed then Wilder-smooth ADX from DX.
        let adx = match self.adx {
            Some(a) => {
                let a = (a * (n - 1.0) + dx) / n;
                self.adx = Some(a);
                Some(a)
            }
            None => {
                self.dx_seed_sum += dx;
                self.dx_seed_count += 1;
                if self.dx_seed_count >= self.period {
                    let a = self.dx_seed_sum / n;
                    self.adx = Some(a);
                    Some(a)
                } else {
                    None
                }
            }
        };

        match adx {
            Some(a) => {
                let v = AdxValue {
                    plus_di,
                    minus_di,
                    adx: a,
                };
                self.last = Some(v);
                Some(v)
            }
            None => None,
        }
    }

    pub fn value(&self) -> Option<AdxValue> {
        self.last
    }

    pub fn is_warm(&self) -> bool {
        self.last.is_some()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A relentless uptrend: +DI ends well above −DI and ADX climbs high.
    #[test]
    fn strong_uptrend_high_adx_plus_di_dominant() {
        let mut adx = Adx::new(3);
        let mut c = 100.0;
        let mut last = None;
        for _ in 0..30 {
            c += 2.0;
            last = adx.on_bar(c + 1.0, c - 0.5, c);
        }
        let v = last.expect("warm");
        assert!(
            v.plus_di > v.minus_di,
            "+DI {} should dominate -DI {}",
            v.plus_di,
            v.minus_di
        );
        assert!(
            v.adx > 40.0,
            "strong trend ADX should be high, got {}",
            v.adx
        );
        assert!((0.0..=100.0).contains(&v.adx));
    }

    /// A steady downtrend: −DI dominates.
    #[test]
    fn strong_downtrend_minus_di_dominant() {
        let mut adx = Adx::new(3);
        let mut c = 100.0;
        let mut last = None;
        for _ in 0..30 {
            c -= 2.0;
            last = adx.on_bar(c + 0.5, c - 1.0, c);
        }
        let v = last.expect("warm");
        assert!(
            v.minus_di > v.plus_di,
            "-DI {} should dominate +DI {}",
            v.minus_di,
            v.plus_di
        );
        assert!(v.adx > 40.0);
    }

    /// A choppy range keeps ADX low (weak trend).
    #[test]
    fn choppy_range_low_adx() {
        let mut adx = Adx::new(4);
        let seq = [
            100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 100.0, 101.0,
            100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 100.0, 101.0,
        ];
        let mut last = None;
        for c in seq {
            last = adx.on_bar(c + 0.5, c - 0.5, c);
        }
        let v = last.expect("warm");
        assert!(v.adx < 40.0, "range ADX should be modest, got {}", v.adx);
    }

    #[test]
    fn warmup_none_then_warm() {
        let mut adx = Adx::new(3);
        // Needs ~2*period bars; assert None early, Some later.
        assert_eq!(adx.on_bar(101.0, 99.0, 100.0), None);
        for _ in 0..3 {
            adx.on_bar(101.0, 99.0, 100.0);
        }
        // After enough bars it warms.
        let mut warm = false;
        for i in 0..10 {
            let c = 100.0 + i as f64;
            if adx.on_bar(c + 1.0, c - 1.0, c).is_some() {
                warm = true;
            }
        }
        assert!(warm);
    }

    #[test]
    fn invalid_bar_ignored() {
        let mut adx = Adx::new(2);
        adx.on_bar(101.0, 99.0, 100.0);
        let before = adx.value();
        assert_eq!(adx.on_bar(f64::NAN, 99.0, 100.0), before);
        assert_eq!(adx.on_bar(99.0, 101.0, 100.0), before); // high < low
    }
}
