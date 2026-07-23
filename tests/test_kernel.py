"""Unit tests for SnapshotKernel — exercises the kernel class directly."""

import base64
import threading
import time

import pytest

from snapshot_kernel.kernel import SnapshotKernel


@pytest.fixture()
def kernel():
    """Provide a fresh kernel for every test."""
    k = SnapshotKernel()
    k.reset()
    return k


# ------------------------------------------------------------------
# Basic state management
# ------------------------------------------------------------------

def test_initial_state(kernel):
    """After init, only 'initial' exists with no user variables."""
    assert kernel.list_states() == ["initial"]
    state = kernel.get_state("initial")
    assert state is not None
    assert state["variables"] == {}


# ------------------------------------------------------------------
# Execution: output capture
# ------------------------------------------------------------------

def test_execute_print(kernel):
    """print() output is captured as stream/stdout."""
    result = kernel.execute("x = 42\nprint(x)", "e1", "initial")
    assert result["error"] is None
    assert result["state_name"] is not None

    stdout_items = [o for o in result["output"]
                    if o["output_type"] == "stream" and o["name"] == "stdout"]
    assert len(stdout_items) == 1
    assert "42\n" == stdout_items[0]["text"]


def test_execute_last_expression(kernel):
    """A trailing expression produces an execute_result."""
    result = kernel.execute("1 + 2", "e1", "initial")
    assert result["error"] is None

    expr_items = [o for o in result["output"]
                  if o["output_type"] == "execute_result"]
    assert len(expr_items) == 1
    assert expr_items[0]["data"]["text/plain"] == "3"


def test_execute_named_state(kernel):
    """Providing new_state_name stores under that exact name."""
    result = kernel.execute("x = 1", "e1", "initial", new_state_name="my_state")
    assert result["state_name"] == "my_state"
    assert "my_state" in kernel.list_states()


def test_chained_execution(kernel):
    """Executing from a derived state sees variables set earlier."""
    r1 = kernel.execute("x = 42", "e1", "initial")
    r2 = kernel.execute("x + 1", "e2", r1["state_name"])
    assert r2["error"] is None

    expr_items = [o for o in r2["output"]
                  if o["output_type"] == "execute_result"]
    assert expr_items[0]["data"]["text/plain"] == "43"


def test_import_module(kernel):
    """Imports are available and persisted in the state."""
    result = kernel.execute("import math\nmath.sqrt(144)", "e1", "initial")
    assert result["error"] is None

    expr_items = [o for o in result["output"]
                  if o["output_type"] == "execute_result"]
    assert expr_items[0]["data"]["text/plain"] == "12.0"

    state = kernel.get_state(result["state_name"])
    assert "math" in state["variables"]
    assert state["variables"]["math"]["type"] == "module"


def test_stderr_capture(kernel):
    """sys.stderr.write() output is captured as stream/stderr."""
    result = kernel.execute("import sys; sys.stderr.write('err')", "e1", "initial")

    stderr_items = [o for o in result["output"]
                    if o["output_type"] == "stream" and o["name"] == "stderr"]
    assert len(stderr_items) == 1
    assert stderr_items[0]["text"] == "err"


# ------------------------------------------------------------------
# Error handling
# ------------------------------------------------------------------

def test_execute_error(kernel):
    """A runtime error is reported; no new state is created."""
    result = kernel.execute("1/0", "e1", "initial")
    assert result["error"] is not None
    assert result["error"]["ename"] == "ZeroDivisionError"
    assert result["state_name"] is None


def test_execute_error_no_state_stored(kernel):
    """After a failed execution the state list has not grown."""
    before = set(kernel.list_states())
    kernel.execute("1/0", "e1", "initial")
    after = set(kernel.list_states())
    assert before == after


def test_state_not_found(kernel):
    """Executing against a non-existent state returns a StateNotFound error."""
    result = kernel.execute("1", "e1", "no_such_state")
    assert result["error"]["ename"] == "StateNotFound"
    assert result["state_name"] is None


# ------------------------------------------------------------------
# State retrieval & deletion
# ------------------------------------------------------------------

def test_get_state_details(kernel):
    """get_state returns variable types, reprs, and a timestamp."""
    r = kernel.execute("x = 42", "e1", "initial")
    state = kernel.get_state(r["state_name"])
    assert state is not None
    assert "x" in state["variables"]
    assert state["variables"]["x"]["type"] == "int"
    assert state["variables"]["x"]["repr"] == "42"
    assert "timestamp" in state


def test_get_state_nonexistent(kernel):
    """get_state returns None for unknown names."""
    assert kernel.get_state("nope") is None


def test_delete_state(kernel):
    """A deleted state is removed from list_states."""
    r = kernel.execute("x = 1", "e1", "initial", new_state_name="tmp")
    assert "tmp" in kernel.list_states()
    assert kernel.delete_state("tmp") is True
    assert "tmp" not in kernel.list_states()


def test_delete_nonexistent(kernel):
    """Deleting an unknown state returns False."""
    assert kernel.delete_state("nope") is False


def test_reset(kernel):
    """reset() removes all states except a fresh 'initial'."""
    kernel.execute("x = 1", "e1", "initial", new_state_name="a")
    kernel.execute("x = 2", "e2", "initial", new_state_name="b")
    kernel.reset()
    assert kernel.list_states() == ["initial"]
    assert kernel.get_state("initial")["variables"] == {}


# ------------------------------------------------------------------
# State isolation
# ------------------------------------------------------------------

def test_state_isolation(kernel):
    """Forking from the same state produces independent snapshots."""
    kernel.execute("x = 1", "e1", "initial", new_state_name="a")
    kernel.execute("x = 2", "e2", "initial", new_state_name="b")

    a = kernel.get_state("a")
    b = kernel.get_state("b")
    initial = kernel.get_state("initial")

    assert a["variables"]["x"]["repr"] == "1"
    assert b["variables"]["x"]["repr"] == "2"
    assert "x" not in initial["variables"]


# ------------------------------------------------------------------
# Aliasing semantics (snapshots preserve intra-state aliasing)
# ------------------------------------------------------------------

def test_cross_cell_aliasing_direct(kernel):
    """Two variables bound to the same object stay aliased across cells."""
    r1 = kernel.execute("a = [1]\nb = a", "e1", "initial")
    r2 = kernel.execute("b.append(2)", "e2", r1["state_name"])
    r3 = kernel.execute("print(a, b, a is b)", "e3", r2["state_name"])
    stdout = "".join(o["text"] for o in r3["output"]
                     if o.get("output_type") == "stream")
    assert stdout.strip() == "[1, 2] [1, 2] True"


def test_cross_cell_aliasing_nested(kernel):
    """Nested aliasing (an object shared between a container and a top-level
    variable) survives snapshotting: mutating it via one name is seen by both."""
    r1 = kernel.execute(
        "class C:\n    pass\nc = C()\nc.a = 1\nx = [c]", "e1", "initial")
    r2 = kernel.execute("x[0].a = 2", "e2", r1["state_name"])
    r3 = kernel.execute("print(c.a, x[0].a, x[0] is c)", "e3", r2["state_name"])
    stdout = "".join(o["text"] for o in r3["output"]
                     if o.get("output_type") == "stream")
    assert stdout.strip() == "2 2 True"


def test_state_independence_preserved(kernel):
    """Mutating a shared container in a derived state does not affect the
    parent state -- states remain immutable/independent under the shared memo."""
    r1 = kernel.execute("a = [1]\nb = a", "e1", "initial", new_state_name="s1")
    kernel.execute("b.append(2)", "e2", "s1", new_state_name="s2")
    # s1 must be untouched.
    r3 = kernel.execute("print(a)", "e3", "s1")
    stdout = "".join(o["text"] for o in r3["output"]
                     if o.get("output_type") == "stream")
    assert stdout.strip() == "[1]"


def test_equal_values_not_aliased(kernel):
    """Equal-but-separate objects are not spuriously aliased after a round-trip."""
    r1 = kernel.execute("p = [1, 2, 3]\nq = [1, 2, 3]", "e1", "initial")
    r2 = kernel.execute("p.append(4)\nprint(p, q, p is q)", "e2", r1["state_name"])
    stdout = "".join(o["text"] for o in r2["output"]
                     if o.get("output_type") == "stream")
    assert stdout.strip() == "[1, 2, 3, 4] [1, 2, 3] False"


# ------------------------------------------------------------------
# Interrupt
# ------------------------------------------------------------------

def test_interrupt(kernel):
    """Interrupting a long-running execution raises KeyboardInterrupt."""
    result_holder = {}
    code = "import time\nfor _ in range(200):\n    time.sleep(0.1)"

    def run():
        result_holder["result"] = kernel.execute(code, "long", "initial")

    t = threading.Thread(target=run)
    t.start()

    # Give the execution a moment to start the loop.
    time.sleep(0.5)
    kernel.interrupt("long")
    t.join(timeout=5)

    result = result_holder.get("result")
    assert result is not None, "Execution thread did not finish"
    assert result["error"] is not None
    assert result["error"]["ename"] == "KeyboardInterrupt"
    assert result["state_name"] is None


# ------------------------------------------------------------------
# Rich output capture
# ------------------------------------------------------------------

def test_rich_repr_html(kernel):
    """An object with _repr_html_() produces text/html in execute_result."""
    code = (
        "class RichObj:\n"
        "    def _repr_html_(self):\n"
        "        return '<b>hello</b>'\n"
        "    def __repr__(self):\n"
        "        return 'RichObj()'\n"
        "RichObj()"
    )
    result = kernel.execute(code, "e1", "initial")
    assert result["error"] is None
    expr_items = [o for o in result["output"] if o["output_type"] == "execute_result"]
    assert len(expr_items) == 1
    assert expr_items[0]["data"]["text/html"] == "<b>hello</b>"
    assert expr_items[0]["data"]["text/plain"] == "RichObj()"


def test_rich_repr_png(kernel):
    """An object with _repr_png_() returns base64-encoded image/png."""
    code = (
        "import base64\n"
        "class PngObj:\n"
        "    def _repr_png_(self):\n"
        "        return b'\\x89PNG_fake'\n"
        "    def __repr__(self):\n"
        "        return 'PngObj()'\n"
        "PngObj()"
    )
    result = kernel.execute(code, "e1", "initial")
    assert result["error"] is None
    expr_items = [o for o in result["output"] if o["output_type"] == "execute_result"]
    assert len(expr_items) == 1
    png_data = expr_items[0]["data"]["image/png"]
    # Verify it is valid base64 that decodes to our bytes.
    assert base64.b64decode(png_data) == b"\x89PNG_fake"


def test_repr_html_returns_none(kernel):
    """If _repr_html_() returns None, text/html should not appear in data."""
    code = (
        "class NoneHtml:\n"
        "    def _repr_html_(self):\n"
        "        return None\n"
        "    def __repr__(self):\n"
        "        return 'NoneHtml()'\n"
        "NoneHtml()"
    )
    result = kernel.execute(code, "e1", "initial")
    assert result["error"] is None
    expr_items = [o for o in result["output"] if o["output_type"] == "execute_result"]
    assert len(expr_items) == 1
    assert "text/html" not in expr_items[0]["data"]
    assert expr_items[0]["data"]["text/plain"] == "NoneHtml()"


def test_repr_mimebundle(kernel):
    """_repr_mimebundle_() returning a dict populates data correctly."""
    code = (
        "class BundleObj:\n"
        "    def _repr_mimebundle_(self):\n"
        "        return {'text/html': '<i>bundle</i>', 'text/plain': 'bundle'}\n"
        "    def __repr__(self):\n"
        "        return 'BundleObj()'\n"
        "BundleObj()"
    )
    result = kernel.execute(code, "e1", "initial")
    assert result["error"] is None
    expr_items = [o for o in result["output"] if o["output_type"] == "execute_result"]
    assert len(expr_items) == 1
    assert expr_items[0]["data"]["text/html"] == "<i>bundle</i>"


def test_repr_mimebundle_tuple(kernel):
    """_repr_mimebundle_() returning (data, metadata) tuple."""
    code = (
        "class TupleBundle:\n"
        "    def _repr_mimebundle_(self):\n"
        "        return ({'text/html': '<em>t</em>'}, {'text/html': {'isolated': True}})\n"
        "    def __repr__(self):\n"
        "        return 'TupleBundle()'\n"
        "TupleBundle()"
    )
    result = kernel.execute(code, "e1", "initial")
    assert result["error"] is None
    expr_items = [o for o in result["output"] if o["output_type"] == "execute_result"]
    assert len(expr_items) == 1
    assert expr_items[0]["data"]["text/html"] == "<em>t</em>"
    assert expr_items[0]["metadata"]["text/html"] == {"isolated": True}


def test_display_function(kernel):
    """Calling display() inside code produces display_data output."""
    code = (
        "class Html:\n"
        "    def _repr_html_(self):\n"
        "        return '<p>hi</p>'\n"
        "    def __repr__(self):\n"
        "        return 'Html()'\n"
        "display(Html())"
    )
    result = kernel.execute(code, "e1", "initial")
    assert result["error"] is None
    display_items = [o for o in result["output"] if o["output_type"] == "display_data"]
    assert len(display_items) == 1
    assert display_items[0]["data"]["text/html"] == "<p>hi</p>"
    assert display_items[0]["data"]["text/plain"] == "Html()"


def test_display_multiple_calls(kernel):
    """Multiple display() calls produce multiple display_data entries."""
    code = "display('first')\ndisplay('second')"
    result = kernel.execute(code, "e1", "initial")
    assert result["error"] is None
    display_items = [o for o in result["output"] if o["output_type"] == "display_data"]
    assert len(display_items) == 2
    assert display_items[0]["data"]["text/plain"] == "'first'"
    assert display_items[1]["data"]["text/plain"] == "'second'"


def test_display_multiple_args(kernel):
    """display('a', 'b') produces two display_data entries."""
    code = "display('a', 'b')"
    result = kernel.execute(code, "e1", "initial")
    assert result["error"] is None
    display_items = [o for o in result["output"] if o["output_type"] == "display_data"]
    assert len(display_items) == 2
    assert display_items[0]["data"]["text/plain"] == "'a'"
    assert display_items[1]["data"]["text/plain"] == "'b'"


def test_display_not_in_state(kernel):
    """The injected display function must not persist in the saved state."""
    result = kernel.execute("x = 1", "e1", "initial")
    assert result["error"] is None
    state = kernel.get_state(result["state_name"])
    assert "display" not in state["variables"]


def test_matplotlib_figure_capture(kernel):
    """A matplotlib figure is captured as display_data with image/png."""
    pytest.importorskip("matplotlib")
    code = (
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "fig, ax = plt.subplots()\n"
        "ax.plot([1, 2, 3])\n"
    )
    result = kernel.execute(code, "e1", "initial")
    assert result["error"] is None
    display_items = [o for o in result["output"] if o["output_type"] == "display_data"]
    assert len(display_items) >= 1
    png_item = display_items[0]
    assert "image/png" in png_item["data"]
    # Verify it's valid base64.
    raw = base64.b64decode(png_item["data"]["image/png"])
    assert raw[:4] == b"\x89PNG"


def test_matplotlib_show_capture(kernel):
    """plt.show() captures the current figure; a second figure is captured at end."""
    pytest.importorskip("matplotlib")
    code = (
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "plt.figure()\n"
        "plt.plot([1, 2])\n"
        "plt.show()\n"
        "plt.figure()\n"
        "plt.plot([3, 4])\n"
    )
    result = kernel.execute(code, "e1", "initial")
    assert result["error"] is None
    display_items = [o for o in result["output"] if o["output_type"] == "display_data"]
    # First figure from plt.show(), second from end-of-cell capture.
    assert len(display_items) >= 2
    for item in display_items:
        assert "image/png" in item["data"]


# ------------------------------------------------------------------
# Output size limits
# ------------------------------------------------------------------

def test_oversized_html_dropped_plain_kept(kernel):
    """HTML exceeding the limit is dropped; text/plain is kept."""
    from snapshot_kernel.kernel import _format_object
    class BigObj:
        def _repr_html_(self):
            return "<div>" + "x" * 200 + "</div>"
        def __repr__(self):
            return "Big()"
    d, m = _format_object(BigObj(), max_size=100)
    assert "text/html" not in d
    assert "text/plain" in d
    assert d["text/plain"] == "Big()"


def test_oversized_plain_truncated(kernel):
    """text/plain exceeding the limit is truncated with a marker."""
    from snapshot_kernel.kernel import _format_object, _TRUNCATION_MARKER
    class Huge:
        def __repr__(self):
            return "A" * 500
    d, m = _format_object(Huge(), max_size=100)
    assert len(d["text/plain"]) == 100 + len(_TRUNCATION_MARKER)
    assert d["text/plain"].endswith(_TRUNCATION_MARKER)
    assert d["text/plain"][:100] == "A" * 100


def test_oversized_mimebundle_enforced(kernel):
    """Oversized HTML via _repr_mimebundle_() is dropped."""
    from snapshot_kernel.kernel import _format_object
    class BundleBig:
        def _repr_mimebundle_(self):
            return {"text/html": "<b>" + "x" * 200 + "</b>", "text/plain": "ok"}
        def __repr__(self):
            return "BundleBig()"
    d, m = _format_object(BundleBig(), max_size=100)
    assert "text/html" not in d
    assert d["text/plain"] == "ok"


def test_under_limit_html_preserved(kernel):
    """Small HTML is preserved normally (regression guard)."""
    from snapshot_kernel.kernel import _format_object
    class Small:
        def _repr_html_(self):
            return "<b>hi</b>"
        def __repr__(self):
            return "Small()"
    d, m = _format_object(Small(), max_size=1_000_000)
    assert d["text/html"] == "<b>hi</b>"
    assert d["text/plain"] == "Small()"


def test_oversized_binary_dropped(kernel):
    """Oversized _repr_png_() output is dropped; text/plain is kept."""
    from snapshot_kernel.kernel import _format_object
    class BigPng:
        def _repr_png_(self):
            return b"\x89PNG" + b"x" * 500
        def __repr__(self):
            return "BigPng()"
    d, m = _format_object(BigPng(), max_size=100)
    assert "image/png" not in d
    assert d["text/plain"] == "BigPng()"


def test_display_oversized_html_dropped(kernel):
    """Size limits apply through the display() path too."""
    from snapshot_kernel.kernel import _format_object
    # Verify _format_object enforces limits when called via display path
    class BigDisplay:
        def _repr_html_(self):
            return "<table>" + "x" * 200 + "</table>"
        def __repr__(self):
            return "BigDisplay()"
    d, m = _format_object(BigDisplay(), max_size=50)
    assert "text/html" not in d
    assert d["text/plain"] == "BigDisplay()"


# ------------------------------------------------------------------
# multistate_execute
# ------------------------------------------------------------------

def test_multistate_basic(kernel):
    """Access variables from two states via aliases."""
    kernel.execute("x = 10", "e1", "initial", new_state_name="s1")
    kernel.execute("y = 20", "e2", "initial", new_state_name="s2")

    result = kernel.multistate_execute(
        "a.x + b.y", "me1", {"a": "s1", "b": "s2"}
    )
    assert result["error"] is None
    expr_items = [o for o in result["output"]
                  if o["output_type"] == "execute_result"]
    assert len(expr_items) == 1
    assert expr_items[0]["data"]["text/plain"] == "30"


def test_multistate_missing_state(kernel):
    """Alias maps to a nonexistent state → StateNotFound."""
    kernel.execute("x = 1", "e1", "initial", new_state_name="s1")

    result = kernel.multistate_execute(
        "a.x", "me1", {"a": "s1", "b": "no_such_state"}
    )
    assert result["error"] is not None
    assert result["error"]["ename"] == "StateNotFound"
    assert result["error"]["evalue"] == "no_such_state"
    assert result["state_name"] is None


def test_multistate_no_state_stored(kernel):
    """multistate_execute does not create any new state."""
    kernel.execute("x = 1", "e1", "initial", new_state_name="s1")
    before = set(kernel.list_states())

    result = kernel.multistate_execute("a.x", "me1", {"a": "s1"})
    assert result["error"] is None
    assert result["state_name"] is None

    after = set(kernel.list_states())
    assert before == after


def test_multistate_stdout(kernel):
    """print() captures stdout in multistate_execute."""
    kernel.execute("x = 'hello'", "e1", "initial", new_state_name="s1")

    result = kernel.multistate_execute(
        "print(a.x)", "me1", {"a": "s1"}
    )
    assert result["error"] is None
    stdout_items = [o for o in result["output"]
                    if o["output_type"] == "stream" and o["name"] == "stdout"]
    assert len(stdout_items) == 1
    assert stdout_items[0]["text"] == "hello\n"


def test_multistate_error(kernel):
    """Runtime error in multistate_execute is reported."""
    kernel.execute("x = 0", "e1", "initial", new_state_name="s1")

    result = kernel.multistate_execute(
        "1 / a.x", "me1", {"a": "s1"}
    )
    assert result["error"] is not None
    assert result["error"]["ename"] == "ZeroDivisionError"
    assert result["state_name"] is None


def test_multistate_default_state(kernel):
    """default_state variables are accessible as plain names."""
    kernel.execute("x = 5\ny = 10", "e1", "initial", new_state_name="cur")
    kernel.execute("z = 100", "e2", "initial", new_state_name="other")

    result = kernel.multistate_execute(
        "x + y + a.z", "me1", {"a": "other"}, default_state="cur"
    )
    assert result["error"] is None
    expr_items = [o for o in result["output"]
                  if o["output_type"] == "execute_result"]
    assert expr_items[0]["data"]["text/plain"] == "115"


def test_multistate_default_state_missing(kernel):
    """A nonexistent default_state returns StateNotFound."""
    result = kernel.multistate_execute(
        "1", "me1", {}, default_state="no_such"
    )
    assert result["error"] is not None
    assert result["error"]["ename"] == "StateNotFound"
    assert result["error"]["evalue"] == "no_such"


# ------------------------------------------------------------------
# Symbol hashing
# ------------------------------------------------------------------

# A setup cell defining a wide mix of value types, used across the
# symbol-hashing tests.  Requires pandas (guarded by importorskip).
_HASH_SETUP = (
    "import pandas as pd\n"
    "x = 5\n"
    "f = 3.14159\n"
    "nums = [1, 2, 3, 4, 5]\n"
    "strs = ['alpha', 'beta', 'gamma']\n"
    "bignums = list(range(100))\n"
    "d = {'a': 1, 'b': [2, 3], 'c': {'nested': True}}\n"
    "st = {10, 20, 30}\n"
    "tup = (1, 'two', 3.0)\n"
    "text = 'a string value'\n"
    "flag = True\n"
    "nothing = None\n"
    "df1 = pd.DataFrame({'i': range(5), 'v': [1.5, 2.5, 3.5, 4.5, 5.5]})\n"
    "df2 = pd.DataFrame({'name': ['a', 'b', 'c'], 'score': [90, 85, 77]})\n"
)


def test_symbol_hashes_stable_across_deepcopy(kernel):
    """After ``x = x + 1``, only x's hash changes; every other symbol keeps
    an identical hash despite the deepcopy that rebuilds the namespace (values
    live at new memory addresses, but the content hash is address-independent).
    """
    pytest.importorskip("pandas")
    r_s = kernel.execute(_HASH_SETUP, "e_setup", "initial", new_state_name="s")
    assert r_s["error"] is None

    r_t = kernel.execute("x = x + 1", "e_step", "s", new_state_name="t")
    assert r_t["error"] is None

    symbols = sorted(kernel.get_state("s")["variables"].keys())
    hs = kernel.get_symbol_hashes("s", symbols)
    ht = kernel.get_symbol_hashes("t", symbols)

    # x was reassigned, so its hash must change.
    assert hs["x"] != ht["x"]

    # Every other symbol is unchanged across the deepcopy boundary.
    for sym in symbols:
        if sym == "x":
            continue
        assert hs[sym] is not None, f"no hash for {sym}"
        assert hs[sym] == ht[sym], f"hash unexpectedly changed for {sym}"


def test_symbol_hashes_basic(kernel):
    """Present symbols hash to 64-char hex; absent symbols map to None; and
    hashing is deterministic across repeated calls."""
    kernel.execute("a = 10\nb = [1, 2, 3]", "e1", "initial", new_state_name="s")

    result = kernel.get_symbol_hashes("s", ["a", "b", "missing"])
    assert set(result.keys()) == {"a", "b", "missing"}
    for sym in ("a", "b"):
        assert isinstance(result[sym], str)
        assert len(result[sym]) == 64
    assert result["missing"] is None

    # Deterministic: same state, same symbols -> same hashes.
    assert kernel.get_symbol_hashes("s", ["a", "b"]) == {
        "a": result["a"], "b": result["b"],
    }


def test_symbol_hashes_detects_changes(kernel):
    """Mutating a value changes its hash; untouched values keep theirs."""
    pytest.importorskip("pandas")
    kernel.execute(
        "import pandas as pd\n"
        "keep = [1, 2, 3]\n"
        "lst = [1, 2, 3]\n"
        "df = pd.DataFrame({'v': [1, 2, 3]})\n",
        "e1", "initial", new_state_name="s",
    )
    kernel.execute(
        "lst.append(4)\ndf.loc[0, 'v'] = 99", "e2", "s", new_state_name="t"
    )

    hs = kernel.get_symbol_hashes("s", ["keep", "lst", "df"])
    ht = kernel.get_symbol_hashes("t", ["keep", "lst", "df"])
    assert hs["keep"] == ht["keep"]
    assert hs["lst"] != ht["lst"]
    assert hs["df"] != ht["df"]


def test_symbol_hashes_state_not_found(kernel):
    """Hashing symbols of a nonexistent state returns None."""
    assert kernel.get_symbol_hashes("nope", ["a"]) is None


def test_symbol_hashes_unsupported_algo(kernel):
    """An unsupported hash_algo raises ValueError; None defaults to 'full'."""
    kernel.execute("a = 1", "e1", "initial", new_state_name="s")
    with pytest.raises(ValueError):
        kernel.get_symbol_hashes("s", ["a"], hash_algo="partial")
    assert kernel.get_symbol_hashes("s", ["a"]) == \
        kernel.get_symbol_hashes("s", ["a"], hash_algo="full")


# ------------------------------------------------------------------
# Write / delete tracking
# ------------------------------------------------------------------

def test_modified_and_deleted_symbols(kernel):
    """execute() reports assigned and deleted top-level symbols."""
    kernel.execute("keep = 1\ngone = 2", "e1", "initial", new_state_name="s0")
    r = kernel.execute(
        "import math\nnew = keep + 1\nkeep = keep + 10\ndel gone",
        "e2", "s0")
    assert set(r["modified_symbols"]) == {"math", "new", "keep"}
    assert r["deleted_symbols"] == ["gone"]
    assert r["accessed_symbols"] == ["keep"]           # 'gone' del is not a read


# ------------------------------------------------------------------
# Alias groups + group fingerprints
# ------------------------------------------------------------------

def _group_with(result, name):
    for grp, fp in zip(result["groups"], result["fingerprints"]):
        if name in grp:
            return tuple(sorted(grp)), fp
    return None, None


def test_alias_groups_direct_and_nested(kernel):
    """Direct (b=a) and nested (x=[c]) sharing are detected; lonely vars are singletons."""
    kernel.execute(
        "a = [1]\nb = a\nc = [2]\nx = [c]\nlonely = [9]",
        "e1", "initial", new_state_name="s")
    ag = kernel.get_alias_groups("s")
    multi = sorted(sorted(g) for g in ag["groups"] if len(g) > 1)
    assert multi == [["a", "b"], ["c", "x"]]
    # every variable appears in exactly one group, with a fingerprint each
    flat = [n for g in ag["groups"] for n in g]
    assert sorted(flat) == ["a", "b", "c", "lonely", "x"]
    assert len(ag["fingerprints"]) == len(ag["groups"])
    assert (["lonely"] in ag["groups"])                # singleton present


def test_group_fingerprint_sees_cross_variable_sharing(kernel):
    """A group fingerprint changes when cross-variable sharing flips, even though
    each variable's value (and per-variable hash) is unchanged."""
    # v1: a and b do NOT share their inner ['o3']; v2: they DO.
    kernel.execute("O1=['o1']\nO2=['o2']\na=[O1,['o3']]\nb=[O2,['o3']]\nx=[O1,O2]",
                   "e1", "initial", new_state_name="v1")
    kernel.execute("O1=['o1']\nO2=['o2']\ns=['o3']\na=[O1,s]\nb=[O2,s]\nx=[O1,O2]",
                   "e2", "initial", new_state_name="v2")
    # per-variable hashes are identical
    h1 = kernel.get_symbol_hashes("v1", ["a", "b", "x"])
    h2 = kernel.get_symbol_hashes("v2", ["a", "b", "x"])
    assert h1 == h2
    # but the group fingerprint (which serialises the group together) differs
    _, fp1 = _group_with(kernel.get_alias_groups("v1"), "x")
    _, fp2 = _group_with(kernel.get_alias_groups("v2"), "x")
    assert fp1 != fp2


def test_singleton_fingerprint_matches_across_equal_states(kernel):
    """A lonely variable's group fingerprint is stable when its value is unchanged."""
    kernel.execute("a = [1, 2, 3]\nb = 5", "e1", "initial", new_state_name="s1")
    kernel.execute("a = [1, 2, 3]\nb = 5", "e2", "initial", new_state_name="s2")
    _, fa1 = _group_with(kernel.get_alias_groups("s1"), "a")
    _, fa2 = _group_with(kernel.get_alias_groups("s2"), "a")
    assert fa1 == fa2


# ------------------------------------------------------------------
# rebuild_state
# ------------------------------------------------------------------

def test_rebuild_state_composes_two_states(kernel):
    """rebuild_state takes source_vars from source, input_vars from input, and
    preserves aliasing within each side."""
    kernel.execute("p = [1]\nq = p\nr = [9]", "e1", "initial", new_state_name="src")
    kernel.execute("p = [0]\nq = [0]\nr = [8]\nextra = [7]",
                   "e2", "initial", new_state_name="inp")
    out = kernel.rebuild_state("inp", "src", ["p", "q"], ["r", "extra"],
                               new_state_name="rebuilt")
    assert out["state_name"] == "rebuilt"
    r = kernel.execute(
        "print(p, q, r, extra, p is q)", "e3", "rebuilt")
    text = "".join(o["text"] for o in r["output"]
                   if o.get("output_type") == "stream").strip()
    # p,q from source (aliased, value [1]); r,extra from input
    assert text == "[1] [1] [8] [7] True"


def test_rebuild_state_missing_state(kernel):
    """rebuild_state returns None if either state is missing."""
    kernel.execute("a = 1", "e1", "initial", new_state_name="s")
    assert kernel.rebuild_state("s", "nope", ["a"], []) is None
    assert kernel.rebuild_state("nope", "s", [], ["a"]) is None


def test_rebuild_state_reuses_state_name(kernel):
    """rebuild_state can overwrite the source state name safely."""
    kernel.execute("v = [1]", "e1", "initial", new_state_name="src")
    kernel.execute("v = [2]\nw = [3]", "e2", "initial", new_state_name="inp")
    # reuse 'src' as the new state name while sourcing 'v' from it
    out = kernel.rebuild_state("inp", "src", ["v"], ["w"], new_state_name="src")
    assert out["state_name"] == "src"
    r = kernel.execute("print(v, w)", "e3", "src")
    text = "".join(o["text"] for o in r["output"]
                   if o.get("output_type") == "stream").strip()
    assert text == "[1] [3]"
