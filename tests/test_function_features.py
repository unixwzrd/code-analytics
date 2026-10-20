from __future__ import annotations

import hashlib

import pytest

from code_analysis.analysis.clones import CloneSource
from code_analysis.analysis.commonality import generate_commonality_candidates
from code_analysis.analysis.function_features import discover_function_features


def _features(text: str):
    source = CloneSource("sample", "sample.py", hashlib.sha256(text.encode()).hexdigest(), text)
    return {item.logical_name: item for item in discover_function_features((source,))}


def test_builtin_types_are_descriptive_not_global_ownership() -> None:
    facts = _features(
        "raise AssertionError('must not execute target')\n"
        "def inspect(value: dict) -> tuple:\n"
        "    result: list = value\n"
        "    return isinstance(result, (dict, tuple, str, int, bytes))\n"
    )["sample:inspect"]
    assert facts.attributes_globals == ()
    assert "parameter:named:dict" in facts.signature_shapes
    assert "return:named:tuple" in facts.signature_shapes
    assert facts.unresolved_call_count == 1


@pytest.mark.parametrize(
    "binding",
    [
        "try:\n        invoke()\n    except ValueError as exc:\n        return exc",
        "import plugin as exc\n    return exc",
        "from plugin import value as exc\n    return exc",
        "def exc():\n        return 1\n    return exc",
        "class exc:\n        pass\n    return exc",
        "match value:\n        case {'item': exc}:\n            return exc",
        "match value:\n        case {'item': _, **exc}:\n            return exc",
        "match value:\n        case [*exc]:\n            return exc",
    ],
)
def test_non_name_store_local_bindings_do_not_become_globals(binding: str) -> None:
    facts = _features("def inspect(value):\n    " + binding + "\n")["sample:inspect"]
    assert "exc" not in facts.attributes_globals


@pytest.mark.parametrize(
    "source, name",
    [
        ("dict = object()\ndef f():\n    return dict\n", "sample:f"),
        ("from plugin import mapping as dict\ndef f():\n    return dict\n", "sample:f"),
        ("def outer(dict):\n    def inner():\n        return dict\n", "sample:outer.inner"),
        ("from plugin import *\ndef f():\n    return dict\n", "sample:f"),
    ],
)
def test_shadowed_or_captured_builtin_names_remain_evidence(source: str, name: str) -> None:
    assert "dict" in _features(source)[name].attributes_globals


def test_global_and_nonlocal_mutations_are_not_local_bindings() -> None:
    facts = _features(
        "counter = 0\ndef f():\n    global counter\n    counter += 1\n    return counter\n"
        "def outer():\n    count = 0\n    def inner():\n        nonlocal count\n"
        "        count += 1\n        return count\n"
    )
    assert facts["sample:f"].attributes_globals == ("counter",)
    assert facts["sample:outer.inner"].attributes_globals == ("count",)


def test_one_callee_plus_builtin_type_cannot_admit_commonality_pair() -> None:
    features = _features(
        "def helper(value):\n    return value\n"
        "def left(value):\n    return helper(isinstance(value, dict))\n"
        "def right(data):\n    return helper(isinstance(data, dict))\n"
    )
    selected = (features["sample:left"], features["sample:right"])
    options = {
        "minimum_ownership_dimensions": 2,
        "minimum_bilateral_coverage": 0.25,
        "maximum_feature_subject_fraction": 0.1,
        "maximum_pairs": 50_000,
    }
    result = generate_commonality_candidates(selected, (), **options)
    assert result.unique_candidates == result.dimension_rejections == 0
    assert result.index_dimension_rejections == 1
    assert not result.pairs
    clones = (("sample:left", "sample:right"),)
    assert generate_commonality_candidates(selected, clones, **options).pairs == clones


@pytest.mark.parametrize(
    "source, name",
    [
        ("from api import send\ndef f(send):\n    return send()\n", "sample:f"),
        ("import api\ndef f(api):\n    return api.send()\n", "sample:f"),
        ("from api import send\ndef f():\n    send = replacement\n    return send()\n", "sample:f"),
        ("import api\ndef f():\n    import other as api\n    return api.send()\n", "sample:f"),
        ("def send():\n    pass\ndef f(send):\n    return send()\n", "sample:f"),
        (
            "class Other:\n    def send(self):\n        pass\ndef f():\n    return send()\n",
            "sample:f",
        ),
        ("def outer():\n    def send():\n        pass\ndef f():\n    return send()\n", "sample:f"),
        ("def send():\n    pass\ndef send():\n    pass\ndef f():\n    return send()\n", "sample:f"),
    ],
)
def test_shadowed_or_inaccessible_calls_remain_unresolved(source: str, name: str) -> None:
    facts = _features(source)[name]
    assert facts.callees == ()
    assert facts.unresolved_call_count == 1


def test_unshadowed_calls_keep_module_and_import_identity() -> None:
    facts = _features(
        "import api\nfrom remote import send\ndef helper():\n    pass\n"
        "class Other:\n    def helper(self):\n        pass\n"
        "def f():\n    helper()\n    send()\n    api.send()\n"
    )["sample:f"]
    assert facts.callees == ("api.send", "remote.send", "sample:helper")
    assert facts.unresolved_call_count == 0


def test_lexical_helpers_and_recursion_do_not_resolve_across_sibling_scopes() -> None:
    facts = _features(
        "def first():\n    def visit():\n        return visit()\n    return visit()\n"
        "def second():\n    def visit():\n        return 2\n    return visit()\n"
        "def outer():\n    def helper():\n        pass\n"
        "    def inner():\n        return helper()\n"
        "    class Nested:\n        def method(self):\n            return helper()\n"
    )
    assert facts["sample:first"].callees == ("sample:first.visit",)
    assert facts["sample:first.visit"].callees == ("sample:first.visit",)
    assert facts["sample:second"].callees == ("sample:second.visit",)
    assert facts["sample:outer.inner"].callees == ("sample:outer.helper",)
    assert facts["sample:outer.Nested.method"].callees == ("sample:outer.helper",)


@pytest.mark.parametrize("binding", ["send = replacement", "def send():\n        pass"])
def test_enclosing_bindings_shadow_module_imports(binding: str) -> None:
    facts = _features(
        "from api import send\ndef outer():\n    "
        + binding
        + "\n    def inner():\n        return send()\n"
    )["sample:outer.inner"]
    assert "api.send" not in facts.callees
    assert facts.callees == (("sample:outer.send",) if binding.startswith("def ") else ())


def test_rebound_module_import_is_not_resolved() -> None:
    facts = _features("import api\napi = replacement\ndef f():\n    return api.send()\n")[
        "sample:f"
    ]
    assert facts.callees == () and facts.unresolved_call_count == 1


def test_dotted_namespace_imports_keep_the_root_binding() -> None:
    facts = _features(
        "import importlib.metadata\nimport importlib.util\n"
        "import remote.api as api\ndef f():\n"
        '    importlib.metadata.version("x")\n    importlib.util.find_spec("x")\n'
        "    api.send()\n"
    )["sample:f"]
    assert facts.callees == (
        "importlib.metadata.version",
        "importlib.util.find_spec",
        "remote.api.send",
    )
    assert facts.unresolved_call_count == 0


def test_shadowed_import_cannot_supply_commonality_ownership() -> None:
    facts = _features(
        "from api import send\n"
        "def left(send):\n    send()\n    raise ValueError('left')\n"
        "def right(send):\n    send()\n    raise ValueError('right')\n"
    )
    pair = (facts["sample:left"], facts["sample:right"])
    result = generate_commonality_candidates(
        pair,
        (),
        minimum_ownership_dimensions=2,
        minimum_bilateral_coverage=0.25,
        maximum_feature_subject_fraction=0.1,
        maximum_pairs=50_000,
    )
    assert not result.pairs
    assert all(item.exceptions == ("ValueError",) for item in pair)


@pytest.mark.parametrize(
    "source",
    [
        "def f(print):\n    print('message')\n",
        "print = replacement\ndef f():\n    print('message')\n",
        "from plugin import *\ndef f():\n    print('message')\n",
        "import logging\ndef f(logging):\n    logging.info('message')\n",
        "import logging\nlogging = replacement\ndef f():\n    logging.info('message')\n",
        "import other as logging\ndef f():\n    logging.info('message')\n",
        "def f():\n    logging.info('message')\n",
    ],
)
def test_output_effects_require_unshadowed_provenance(source: str) -> None:
    assert _features(source)["sample:f"].side_effects == ()


@pytest.mark.parametrize(
    "source, expected",
    [
        ("def f():\n    print('message')\n", "output:print"),
        ("import logging\ndef f():\n    logging.info('message')\n", "output:logging.info"),
        ("import logging as log\ndef f():\n    log.warning('message')\n", "output:logging.warning"),
        (
            "from logging import error as emit\ndef f():\n    emit('message')\n",
            "output:logging.error",
        ),
    ],
)
def test_output_effects_preserve_resolved_alias_identity(source: str, expected: str) -> None:
    assert _features(source)["sample:f"].side_effects == (expected,)


def test_shadowed_output_cannot_originate_commonality() -> None:
    facts = _features(
        "def left(print):\n    print('left')\n    raise ValueError('left')\n"
        "def right(print):\n    print('right')\n    raise ValueError('right')\n"
    )
    result = generate_commonality_candidates(
        (facts["sample:left"], facts["sample:right"]),
        (),
        minimum_ownership_dimensions=2,
        minimum_bilateral_coverage=0.25,
        maximum_feature_subject_fraction=0.1,
        maximum_pairs=50_000,
    )
    assert not result.pairs


@pytest.mark.parametrize(
    "source, name",
    [
        ("def f(self):\n    return self.send()\n", "sample:f"),
        (
            "def outer():\n    def send():\n        pass\n"
            "    def f(self):\n        return self.send()\n",
            "sample:outer.f",
        ),
        ("class C:\n    def f(self):\n        return self.service.send()\n", "sample:C.f"),
        ("class C:\n    def f(self):\n        return self.missing()\n", "sample:C.f"),
        (
            "class C:\n    def send(self):\n        pass\n"
            "    def f(self):\n        self = replacement\n        return self.send()\n",
            "sample:C.f",
        ),
        (
            "class C:\n    def send(self):\n        pass\n"
            "    @staticmethod\n    def f(self):\n        return self.send()\n",
            "sample:C.f",
        ),
        (
            "class C:\n    @property\n    def send(self):\n        pass\n"
            "    def f(self):\n        return self.send()\n",
            "sample:C.f",
        ),
        (
            "class C:\n    def send(self):\n        pass\n    send = replacement\n"
            "    def f(self):\n        return self.send()\n",
            "sample:C.f",
        ),
        (
            "class C:\n    def send(self):\n        pass\n"
            "    def f(self):\n        self.send = replacement\n        return self.send()\n",
            "sample:C.f",
        ),
    ],
)
def test_receiver_spelling_is_not_callee_provenance(source: str, name: str) -> None:
    feature = _features(source)[name]
    assert feature.callees == ()
    assert feature.unresolved_call_count == 1


def test_direct_same_class_methods_keep_lexical_identity() -> None:
    features = _features(
        "class C:\n    def send(receiver):\n        pass\n"
        "    def f(receiver):\n        return receiver.send()\n"
        "    async def repeat(self):\n        return await self.repeat()\n"
    )
    assert features["sample:C.f"].callees == ("sample:C.send",)
    assert features["sample:C.repeat"].callees == ("sample:C.repeat",)
    assert features["sample:C.f"].unresolved_call_count == 0
