"""Installation timing uses recorded events, never inferred file timestamps."""
import math

from .contracts import require


def first_merge_timing(state, merged_at):
    setup_start = state['started_at']
    require(math.isfinite(setup_start) and math.isfinite(merged_at) and 0 < setup_start <= merged_at,
            'timing_unavailable', 'Recorded setup and merge times are not in chronological order.')
    return {'setup_started_at': setup_start, 'merged_at': merged_at,
            'setup_to_first_merge_seconds': merged_at - setup_start}
