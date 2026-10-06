from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any

import pandas as pd


class FilterSyntaxError(ValueError):
    def __init__(self, message: str, position: int):
        super().__init__(message)
        self.message = message
        self.position = position


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    position: int


TOKEN_RE = re.compile(
    r"(?P<WS>\s+)"
    r"|(?P<NUMBER>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"|(?P<STRING>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"
    r"|(?P<OP>==|!=|>=|<=|>|<|\&|\||!)"
    r"|(?P<LPAREN>\()|(?P<RPAREN>\))|(?P<COMMA>,)"
    r"|(?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)"
)


def tokenize(expression: str) -> list[Token]:
    tokens: list[Token] = []
    cursor = 0
    while cursor < len(expression):
        match = TOKEN_RE.match(expression, cursor)
        if not match:
            raise FilterSyntaxError(f"Unexpected character {expression[cursor]!r}", cursor)
        kind = match.lastgroup or ""
        if kind != "WS":
            tokens.append(Token(kind, match.group(), cursor))
        cursor = match.end()
    tokens.append(Token("EOF", "", len(expression)))
    return tokens


class Parser:
    def __init__(self, expression: str, frame: pd.DataFrame):
        self.expression = expression
        self.frame = frame
        self.tokens = tokenize(expression)
        self.index = 0

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def advance(self) -> Token:
        token = self.current
        self.index += 1
        return token

    def expect(self, kind: str, value: str | None = None) -> Token:
        token = self.current
        if token.kind != kind or (value is not None and token.value != value):
            wanted = value or kind.lower()
            raise FilterSyntaxError(f"Expected {wanted}", token.position)
        return self.advance()

    def parse(self) -> pd.Series:
        if self.current.kind == "EOF":
            return pd.Series(True, index=self.frame.index, dtype=bool)
        result = self.parse_or()
        if self.current.kind != "EOF":
            raise FilterSyntaxError("Unexpected trailing input", self.current.position)
        return self.to_mask(result, self.current.position)

    def parse_or(self) -> Any:
        left = self.parse_and()
        while self.current.kind == "OP" and self.current.value == "|":
            position = self.advance().position
            right = self.parse_and()
            left = self.to_mask(left, position) | self.to_mask(right, position)
        return left

    def parse_and(self) -> Any:
        left = self.parse_unary()
        while self.current.kind == "OP" and self.current.value == "&":
            position = self.advance().position
            right = self.parse_unary()
            left = self.to_mask(left, position) & self.to_mask(right, position)
        return left

    def parse_unary(self) -> Any:
        if self.current.kind == "OP" and self.current.value == "!":
            position = self.advance().position
            return ~self.to_mask(self.parse_unary(), position)
        if self.current.kind == "LPAREN":
            self.advance()
            value = self.parse_or()
            self.expect("RPAREN")
            return value
        return self.parse_comparison()

    def parse_comparison(self) -> Any:
        left = self.parse_atom()
        if self.current.kind == "OP" and self.current.value in {"==", "!=", ">", ">=", "<", "<="}:
            operator = self.advance()
            right = self.parse_atom()
            try:
                return {
                    "==": lambda: left == right,
                    "!=": lambda: left != right,
                    ">": lambda: left > right,
                    ">=": lambda: left >= right,
                    "<": lambda: left < right,
                    "<=": lambda: left <= right,
                }[operator.value]()
            except (TypeError, ValueError) as exc:
                raise FilterSyntaxError(str(exc), operator.position) from exc
        return left

    def parse_atom(self) -> Any:
        token = self.current
        if token.kind == "IDENT":
            name = self.advance().value
            if self.current.kind == "LPAREN":
                return self.parse_function(name, token.position)
            if name not in self.frame.columns:
                raise FilterSyntaxError(f"Unknown variable: {name}", token.position)
            return self.frame[name]
        if token.kind == "NUMBER":
            self.advance()
            return (
                float(token.value)
                if any(c in token.value.lower() for c in ".e")
                else int(token.value)
            )
        if token.kind == "STRING":
            self.advance()
            return ast.literal_eval(token.value)
        raise FilterSyntaxError("Expected a variable, literal, or function", token.position)

    def parse_function(self, name: str, position: int) -> pd.Series:
        self.expect("LPAREN")
        if name == "missing":
            variable = self.expect("IDENT")
            if variable.value not in self.frame.columns:
                raise FilterSyntaxError(f"Unknown variable: {variable.value}", variable.position)
            self.expect("RPAREN")
            return self.frame[variable.value].isna()
        if name == "inlist":
            variable = self.expect("IDENT")
            if variable.value not in self.frame.columns:
                raise FilterSyntaxError(f"Unknown variable: {variable.value}", variable.position)
            values: list[Any] = []
            while self.current.kind == "COMMA":
                self.advance()
                if self.current.kind not in {"NUMBER", "STRING"}:
                    raise FilterSyntaxError("inlist values must be literals", self.current.position)
                values.append(self.parse_atom())
            self.expect("RPAREN")
            if not values:
                raise FilterSyntaxError("inlist requires at least one value", position)
            return self.frame[variable.value].isin(values)
        raise FilterSyntaxError(f"Function not allowed: {name}", position)

    def to_mask(self, value: Any, position: int) -> pd.Series:
        if isinstance(value, pd.Series):
            if not pd.api.types.is_bool_dtype(value.dtype):
                raise FilterSyntaxError(
                    "This expression does not produce true/false values", position
                )
            return value.fillna(False).astype(bool)
        if isinstance(value, bool):
            return pd.Series(value, index=self.frame.index, dtype=bool)
        raise FilterSyntaxError("This expression does not produce true/false values", position)


def evaluate_filter(expression: str, frame: pd.DataFrame) -> pd.Series:
    """Safely evaluate the supported Stata-like filter subset."""
    return Parser(expression.strip(), frame).parse()
