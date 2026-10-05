"""utils.js apiFetch surfaces the server's `detail` in thrown errors."""
from __future__ import annotations

import json

import pytest

from tests.jsvm import run_js

pytestmark = pytest.mark.unit


def _call(status, body):
    return run_js(
        sources=["utils.js"],
        bootstrap="globalThis.fetch = function () { return Promise.resolve({ ok: false, status: "
                  + str(status) + ", json: function () { return " + body + "; } }); };",
        script="return apiFetch('/x').then(() => 'no error', (e) => String(e.message));",
    )


def test_detail_is_included():
    msg = _call(409, "Promise.resolve(" + json.dumps({"detail": "An options account named 'Bull' already exists"}) + ")")
    assert msg == "An options account named 'Bull' already exists (HTTP 409)"


@pytest.mark.parametrize("body", ["Promise.resolve({})", "Promise.reject(new Error('not json'))",
                                  "Promise.resolve({detail: [{msg: 'x'}]})"])
def test_falls_back_to_the_status(body):
    assert _call(500, body) == "HTTP 500"
