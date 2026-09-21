#!/usr/bin/env python3
"""Verify a written zone never carries firmware-owned keys.

Why this exists
---------------
v1.3.0-v1.3.5 created zones that every device agreed on - correct membership,
correct dev_count - but that only ever sounded on the host. The member stayed
silent over AirPlay and over Spotify Connect alike, so it was not a streaming
problem: the zone never carried audio to the member at all.

The cause was one key. ``set_groups`` writes built ``dev_info`` entries with
``"host": true`` on the chosen device. ``host`` is the firmware's output, not
the writer's input - the device elects a host after the write and echoes the
flag back. Asserting it up front suppressed whatever the firmware does on
election, and the member never joined. Capturing the Play app's own
``set_groups`` write off a device's MQTT broker settled it: the app does not
send the key at all. ``"host": false`` is not the fix either.

The same capture showed the per-group ``timestamp`` we sent is never echoed
back, so it is write-only noise.

Three further differences from that capture turned out to matter just as much,
and are checked here too:

* the ``set_groups`` **body** carries epoch seconds (``0`` when the last zone
  is cleared). Without it the speakers store the zone as ``timestamp: -1``.
* a zone is created with ``group_index: 1``, never ``0``.
* ``wb_enable``/``wb_device``/``wb_input`` are sent only while broadcasting.

Measured on five UPL-PORTs with the speakers cleared between runs: shipped
v1.4.0 wrote ``timestamp: -1`` and ``group_index: 0`` and played on the host
alone; the same zone written with all of the above played in every room;
reverting brought the silence back.

None of these failures are loud. The zone forms, the entities look right, and
the only symptom is silence in one room - which is exactly the class of bug the
other guards in this directory exist for. So: assert statically that the
payload builder for a *written* zone gets all of it right.

Not covered here
----------------
``gs_to_dict`` deliberately DOES echo ``host``. It re-serialises zones the
devices reported, for the sibling entries the write path resends untouched
alongside the zone being edited; stripping their elected host would force a
re-election on an unrelated - and possibly playing - zone every time the user
edits a different one.
"""

from __future__ import annotations

import ast
import pathlib
import sys

FORBIDDEN = {"host", "timestamp"}
WIDEBAND = {"wb_enable", "wb_device", "wb_input"}
TARGET = "group_payload"
PKG = (
    pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "unifi_play"
)


def _dict_keys(node: ast.AST) -> set[str]:
    """Literal string keys of every dict literal under node."""
    keys: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Dict):
            keys |= {
                k.value
                for k in sub.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
        elif isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant):
            if isinstance(sub.slice.value, str):
                keys.add(sub.slice.value)
    return keys


def main() -> int:
    helpers = PKG / "helpers.py"
    try:
        tree = ast.parse(helpers.read_text(), filename=str(helpers))
    except SyntaxError as err:
        print(f"needs a newer Python to parse the package ({err})")
        return 0

    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == TARGET
        ),
        None,
    )
    if fn is None:
        print(f"error: {TARGET}() not found in {helpers.name}")
        return 1

    offending = sorted(_dict_keys(fn) & FORBIDDEN)
    if offending:
        print(f"error: {TARGET}() emits firmware-owned key(s): {offending}")
        print("       'host' is elected by the device; writing it silences members.")
        print("       a per-group 'timestamp' is never echoed back.")
        print(f"       see {helpers.name}:dev_info_entry and docs/api.md")
        return 1

    failures = _check_wideband_is_conditional(fn)
    failures += _check_writer()
    if failures:
        for line in failures:
            print(f"error: {line}")
        print("       these match the Play app's own write; without them a zone")
        print("       forms correctly and then plays on the host alone.")
        print("       see docs/api.md, 'Do not simplify the written payload'")
        return 1

    print(f"ok: {TARGET}() matches the app's set_groups payload")
    return 0


def _check_wideband_is_conditional(fn: ast.FunctionDef) -> list[str]:
    """wb_* may only be written from inside a branch, never unconditionally.

    The app omits all three while streaming, and an absent wb_enable reads as
    off. Sending them always is what this guards against; exactly which
    condition guards them is left to the implementation.
    """
    guarded = {
        key
        for branch in ast.walk(fn)
        if isinstance(branch, ast.If)
        for key in _dict_keys(branch)
    }
    loose = sorted(k for k in WIDEBAND if k in _dict_keys(fn) and k not in guarded)
    if loose:
        return [f"{TARGET}() always emits {loose}; send them only while broadcasting"]
    return []


def _check_writer() -> list[str]:
    """zone_writer must stamp the body and create zones with group_index 1."""
    writer = PKG / "zone_writer.py"
    try:
        tree = ast.parse(writer.read_text(), filename=str(writer))
    except SyntaxError:
        return []

    fns = {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    failures: list[str] = []

    publish = fns.get("_publish")
    if publish is None:
        failures.append("zone_writer._publish() not found")
    elif "timestamp" not in _dict_keys(publish):
        failures.append(
            "zone_writer._publish() sends no body 'timestamp'; "
            "speakers then store the zone as timestamp -1"
        )

    create = fns.get("create")
    if create is None:
        failures.append("zone_writer.create() not found")
    elif not any(
        kw.arg == "group_index"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value == 1
        for call in ast.walk(create)
        if isinstance(call, ast.Call)
        for kw in call.keywords
    ):
        failures.append(
            "zone_writer.create() does not pass group_index=1; the app never uses 0"
        )

    return failures


if __name__ == "__main__":
    sys.exit(main())
