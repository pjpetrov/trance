"""Per-language tree-sitter grammars and the queries that drive extraction.

Adding a language means adding one `LanguageSpec` here — no changes to
parse.py. Everything is expressed as tree-sitter queries rather than
hand-rolled AST walking, so grammar upgrades come for free.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Parser, Query


@dataclass(frozen=True)
class LanguageSpec:
    name: str
    extensions: tuple[str, ...]
    #: Captures: @def.<kind> on the definition node, @name on its identifier.
    definitions: str
    #: Captures: @call on the call node, @callee on the name being called.
    calls: str
    #: Captures: @module on the imported module path.
    imports: str
    _language: object = None

    @property
    def language(self) -> Language:
        return _load_language(self.name)


PYTHON = LanguageSpec(
    name="python",
    extensions=(".py",),
    definitions="""
        (function_definition name: (identifier) @name) @def.function
        (class_definition    name: (identifier) @name) @def.class
        ; Module-level constants (PAGE_SIZE, DEFAULT_TIMEOUT, ...). Restricted to
        ; module scope on purpose — capturing every `self.x = ...` would bury the
        ; graph in noise.
        (module (expression_statement (assignment left: (identifier) @name) @def.variable))
    """,
    calls="""
        (call function: (identifier) @callee) @call
        (call function: (attribute attribute: (identifier) @callee)) @call
    """,
    imports="""
        (import_statement name: (dotted_name) @module)
        (import_from_statement module_name: (dotted_name) @module)
    """,
)

# TS and TSX share a grammar family; the queries are identical, only the
# parser differs (tsx allows JSX syntax).
_TS_DEFINITIONS = """
    (function_declaration name: (identifier) @name) @def.function
    (generator_function_declaration name: (identifier) @name) @def.function
    (method_definition name: (property_identifier) @name) @def.method
    (class_declaration name: (type_identifier) @name) @def.class
    (variable_declarator name: (identifier) @name value: (arrow_function)) @def.function
    (variable_declarator name: (identifier) @name value: (function_expression)) @def.function
    ; Module-level constants. This also re-matches `const f = () => {}`, which the
    ; two patterns above already claimed as functions; parse.py dedupes by range
    ; and keeps the more specific kind.
    (program (lexical_declaration (variable_declarator name: (identifier) @name) @def.variable))
    (program (variable_declaration (variable_declarator name: (identifier) @name) @def.variable))
    ; The .d.ts surface: what a library *declares* rather than defines. These
    ; are the shapes an agent asks the graph about — `declare function`,
    ; interfaces, type aliases, enums, and the signatures inside ambient
    ; classes and interfaces, none of which have bodies to match above.
    (function_signature name: (identifier) @name) @def.function
    (method_signature name: (property_identifier) @name) @def.method
    (interface_declaration name: (type_identifier) @name) @def.class
    (type_alias_declaration name: (type_identifier) @name) @def.class
    (enum_declaration name: (identifier) @name) @def.class
    (abstract_class_declaration name: (type_identifier) @name) @def.class
"""
_TS_CALLS = """
    (call_expression function: (identifier) @callee) @call
    (call_expression function: (member_expression property: (property_identifier) @callee)) @call
"""
_TS_IMPORTS = """
    (import_statement source: (string (string_fragment) @module))
"""

TYPESCRIPT = LanguageSpec(
    name="typescript",
    extensions=(".ts", ".mts", ".cts"),
    definitions=_TS_DEFINITIONS,
    calls=_TS_CALLS,
    imports=_TS_IMPORTS,
)

TSX = LanguageSpec(
    name="tsx",
    extensions=(".tsx", ".jsx", ".js", ".mjs", ".cjs"),
    definitions=_TS_DEFINITIONS,
    calls=_TS_CALLS,
    imports=_TS_IMPORTS,
)

SPECS: tuple[LanguageSpec, ...] = (PYTHON, TYPESCRIPT, TSX)
BY_EXTENSION: dict[str, LanguageSpec] = {ext: spec for spec in SPECS for ext in spec.extensions}


@lru_cache(maxsize=None)
def _load_language(name: str) -> Language:
    if name == "python":
        return Language(tree_sitter_python.language())
    if name == "typescript":
        return Language(tree_sitter_typescript.language_typescript())
    if name == "tsx":
        return Language(tree_sitter_typescript.language_tsx())
    raise ValueError(f"unknown language: {name}")


@lru_cache(maxsize=None)
def get_parser(name: str) -> Parser:
    return Parser(_load_language(name))


@lru_cache(maxsize=None)
def get_query(lang_name: str, source: str) -> Query:
    return Query(_load_language(lang_name), source)


def spec_for_path(path: str) -> LanguageSpec | None:
    for ext, spec in BY_EXTENSION.items():
        if path.endswith(ext):
            return spec
    return None
