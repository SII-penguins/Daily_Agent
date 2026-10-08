"""Keep partial public-source results without hiding failed subrequests."""
from __future__ import annotations

import httpx


class PartialSourceError(RuntimeError):
    def __init__(self, source, items, failures):
        self.partial_items = items
        self.partial_success = True
        self.failures = failures
        super().__init__(f'{source} partial collection: ' + '; '.join(
            f"{row['target']}: {row['message']}" for row in failures))


def failure_record(target, error):
    return {'target': target, 'type': type(error).__name__,
            'status_code': error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None,
            'message': str(error)}


def finish_collection(source, items, failures, errors, successful_requests):
    if not errors:
        return items
    if successful_requests:
        raise PartialSourceError(source, items, failures)
    # Keep the native HTTP/network exception identity on total failure.
    error = errors[-1]
    error.partial_items = items
    error.failures = failures
    error.args = (f'{source} collection failed: ' + '; '.join(
        f"{row['target']}: {row['message']}" for row in failures),)
    raise error
