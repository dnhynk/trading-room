"""Validation of persisted execution settings; no legacy continuation model."""
import math

DEFAULTS = dict(stop_mode='fixed', stop_tail_probability=.05,
                stop_vol_window_s=300, stop_vol_stride_s=10,
                value_exit=False, value_window_s=21600,
                value_min_paths=20, value_confidence=.95)


def validate_exit_settings(cfg):
    c = dict(DEFAULTS, **{k: cfg[k] for k in DEFAULTS if k in cfg})
    if c['stop_mode'] not in ('fixed', 'volatility'):
        raise ValueError('stop_mode must be fixed or volatility')
    if type(c['value_exit']) is not bool:
        raise ValueError('value_exit must be boolean')
    for key, lo, hi in (('stop_vol_window_s', 60, 3600), ('stop_vol_stride_s', 2, 60),
                        ('value_window_s', 3600, 86400), ('value_min_paths', 20, 1000)):
        if type(c[key]) is not int or not lo <= c[key] <= hi:
            raise ValueError('invalid '+key)
    if c['stop_vol_window_s'] % c['stop_vol_stride_s'] or c['stop_vol_window_s'] // c['stop_vol_stride_s'] < 20:
        raise ValueError('volatility needs at least 20 complete, equal sparse intervals')
    for key, lo, hi in (('stop_tail_probability', 0, .5), ('value_confidence', .5, 1)):
        x = c[key]
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or not lo < x < hi:
            raise ValueError('invalid '+key)
    hold = cfg.get('hold_s', 180)
    if type(hold) is not int or not 1 <= hold <= 300:
        raise ValueError('invalid holding horizon')
    if c['value_window_s'] < c['value_min_paths'] * (hold + 1):
        raise ValueError('value window cannot contain required nonoverlapping paths')
    return c


