"""Bounded read-only price snapshot for finite research runs.

This is an in-memory view, not a persistent cache or a recurring full scan.
It preserves the store's dated universe/status joins and feature availability.
"""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date, timedelta


class ResearchDataset:
    def __init__(self, store, dataset_id, start, end, max_rows=300_000):
        self.dataset_id = dataset_id
        self.start = (date.fromisoformat(start) - timedelta(days=150)).isoformat()
        self.end = end
        before_hash = store.dataset_hash(dataset_id)
        with store.connect() as connection:
            rows = connection.execute(
                "SELECT trade_date,code,open,high,low,close,prev_close,volume,amount,adjust_factor "
                "FROM daily_bars WHERE dataset_id=? AND trade_date BETWEEN ? AND ? "
                "ORDER BY trade_date,code LIMIT ?", (dataset_id, self.start, end, max_rows + 1)
            ).fetchall()
        if len(rows) > max_rows:
            raise ValueError("RESEARCH_SNAPSHOT_ROW_LIMIT")
        self._history, self._days, self._features = defaultdict(list), {}, {}
        dates = sorted({str(row['trade_date']) for row in rows})
        for row in rows:
            self._history[str(row['code'])].append(dict(row))
        self._history_dates = {code: [row['trade_date'] for row in values]
                               for code, values in self._history.items()}
        for day in dates:
            self._days[day] = store.daily_slice(dataset_id, day)
            self._features[day] = store.features_for_date(dataset_id, day)
        self._dates = dates
        self._hash = store.dataset_hash(dataset_id)
        if self._hash != before_hash:
            raise ValueError("RESEARCH_DATASET_CHANGED_DURING_SNAPSHOT")

    def _check(self, dataset_id):
        if dataset_id != self.dataset_id:
            raise ValueError("RESEARCH_DATASET_MISMATCH")

    def daily_slice(self, dataset_id, trade_date):
        self._check(dataset_id)
        return self._days.get(trade_date, [])

    def history_until(self, dataset_id, code, trade_date, limit):
        self._check(dataset_id)
        values = self._history.get(code, [])
        end = bisect_right(self._history_dates.get(code, []), trade_date)
        return values[max(0, end - limit):end]

    def features_for_date(self, dataset_id, trade_date):
        self._check(dataset_id)
        return self._features.get(trade_date, {})

    def trade_dates(self, dataset_id, start, end):
        self._check(dataset_id)
        return self._dates[bisect_left(self._dates, start):bisect_right(self._dates, end)]

    def dataset_hash(self, dataset_id):
        self._check(dataset_id)
        return self._hash
