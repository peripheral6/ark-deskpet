"""Local usage accounting. No network calls, prompts, or response text."""
from datetime import datetime
import math

FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens')


def counts(value):
    if not isinstance(value, dict):
        return None
    value = dict(value)
    value.setdefault('cache_write_input_tokens', 0)
    if not all(isinstance(value.get(k), int) and not isinstance(value.get(k), bool)
               and value[k] >= 0 for k in FIELDS):
        return None
    if value['cached_input_tokens'] + value['cache_write_input_tokens'] > value['input_tokens']:
        return None
    return {k: value[k] for k in FIELDS}


def add(bucket, model, delta):
    target = bucket.setdefault(model or 'unknown', dict.fromkeys(FIELDS, 0))
    for key in FIELDS:
        target[key] += delta[key]


def merge(buckets):
    result = {}
    for bucket in buckets:
        for model, value in bucket.items():
            add(result, model, value)
    return result


def summarize(bucket, prices):
    totals = dict.fromkeys(FIELDS, 0)
    cost = 0.0
    unpriced = []
    priced = 0
    for model, value in bucket.items():
        for key in FIELDS:
            totals[key] += value[key]
        rate = prices.get(model, {})
        keys = ('input', 'cached', 'output') + (('write',) if value['cache_write_input_tokens'] else ())
        if not all(isinstance(rate.get(k), (int, float)) and not isinstance(rate.get(k), bool)
                   and math.isfinite(rate[k]) and rate[k] >= 0 for k in keys):
            unpriced.append(model)
            continue
        cost += ((value['input_tokens'] - value['cached_input_tokens'] - value['cache_write_input_tokens']) * rate['input']
                 + value['cached_input_tokens'] * rate['cached']
                 + value['cache_write_input_tokens'] * rate.get('write', 0)
                 + value['output_tokens'] * rate['output']) / 1_000_000
        priced += 1
    # Reasoning is already included in output; cached input is a subset of input.
    totals.update(total_tokens=totals['input_tokens'] + totals['output_tokens'],
                  cost_usd=cost if priced else None, unpriced=unpriced)
    return totals


class UsageLedger:
    def __init__(self):
        self.previous = None
        self.turn = {}
        self.session = {}
        self.days = {}
        self.complete = True
        self.observed = False

    def start_turn(self):
        self.turn = {}

    def consume(self, info, model, stamp):
        if not isinstance(info, dict):
            return
        total = counts(info.get('total_token_usage'))
        if total is None:
            self.complete = False
            return
        last = counts(info.get('last_token_usage'))
        if self.previous == total:
            return
        self.observed = True
        if self.previous is None:
            # A log may begin with an inherited cumulative total. Only its last
            # request is attributable to this timestamp/turn/model.
            delta = last if last and all(last[k] <= total[k] for k in FIELDS) else None
            if delta is None:
                self.complete = False
            elif delta != total:
                self.complete = False
        elif any(total[k] < self.previous[k] for k in FIELDS):
            # Reset/compaction: do not charge the whole replacement cumulative total.
            delta = last if last and all(last[k] <= total[k] for k in FIELDS) else None
            self.complete = False
        else:
            delta = {k: total[k] - self.previous[k] for k in FIELDS}
        self.previous = total
        if delta is None or delta['cached_input_tokens'] + delta['cache_write_input_tokens'] > delta['input_tokens']:
            self.complete = False
            return
        if not any(delta.values()):
            return
        add(self.turn, model, delta)
        add(self.session, model, delta)
        if stamp is not None:
            try:
                day = datetime.fromtimestamp(stamp).date().isoformat()
                add(self.days.setdefault(day, {}), model, delta)
            except (ValueError, OverflowError, OSError):
                self.complete = False
        else:
            self.complete = False

    def snapshot(self):
        day = datetime.now().astimezone().date().isoformat()
        # Independent nested dicts prevent the background reader mutating UI data.
        return dict(turn=merge([self.turn]), session=merge([self.session]),
                    today=merge([self.days.get(day, {})]), day=day,
                    complete=self.complete, observed=self.observed)

    def cache_record(self):
        """Persist counters only; never cache message or response contents."""
        return dict(previous=self.previous, turn=self.turn, session=self.session,
                    days=self.days, complete=self.complete, observed=self.observed)

    def restore_cache(self, record):
        def valid_bucket(bucket):
            return isinstance(bucket, dict) and all(
                isinstance(model, str) and counts(value) is not None
                for model, value in bucket.items())

        if not isinstance(record, dict):
            return False
        previous = record.get('previous')
        turn, session, days = (record.get(key) for key in ('turn', 'session', 'days'))
        if (previous is not None and counts(previous) is None) or not valid_bucket(turn) or not valid_bucket(session):
            return False
        if not isinstance(days, dict) or not all(
                isinstance(day, str) and valid_bucket(bucket) for day, bucket in days.items()):
            return False
        if not isinstance(record.get('complete'), bool) or not isinstance(record.get('observed'), bool):
            return False
        self.previous = previous
        self.turn, self.session, self.days = turn, session, days
        self.complete, self.observed = record['complete'], record['observed']
        return True
