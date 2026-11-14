from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Iterable, Iterator


class ExpressionError(ValueError):
    pass


@dataclass(frozen=True)
class Column:
    dataset: str | None
    name: str


@dataclass(frozen=True)
class Literal:
    value: str | int | Decimal | bool | None


@dataclass(frozen=True)
class Star:
    pass


@dataclass(frozen=True)
class Binary:
    op: str
    left: "Expr"
    right: "Expr"


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple["Expr", ...]


Expr = Column | Literal | Star | Binary | Call


_TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<number>\d+(?:\.\d+)?)|"
    r"(?P<string>'(?:''|[^'])*')|"
    r"(?P<identifier>[A-Za-z_][A-Za-z0-9_]*)|"
    r"(?P<operator><=|>=|<>|!=|[+\-*/=<>])|"
    r"(?P<dot>\.)|(?P<comma>,)|(?P<lparen>\()|(?P<rparen>\))"
    r")"
)


def parse(text: str) -> Expr:
    tokens = tuple(_tokens(text))
    parser = _Parser(tokens)
    expression = parser.expression()
    if not parser.at_end:
        raise ExpressionError(f"unexpected token {parser.peek()[1]!r}")
    return expression


def _tokens(text: str) -> Iterator[tuple[str, str]]:
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if not match:
            raise ExpressionError(f"unsupported expression near {text[position:position + 24]!r}")
        kind = match.lastgroup
        assert kind is not None
        yield kind, match.group(kind)
        position = match.end()


class _Parser:
    def __init__(self, tokens: tuple[tuple[str, str], ...]):
        self.tokens = tokens
        self.position = 0

    @property
    def at_end(self) -> bool:
        return self.position == len(self.tokens)

    def peek(self) -> tuple[str, str]:
        if self.at_end:
            return "end", ""
        return self.tokens[self.position]

    def take(self, kind: str | None = None) -> tuple[str, str]:
        token = self.peek()
        if kind is not None and token[0] != kind:
            raise ExpressionError(f"expected {kind}, found {token[1]!r}")
        self.position += 1
        return token

    def expression(self, minimum_precedence: int = 0) -> Expr:
        left = self.primary()
        precedence = {"=": 1, "!=": 1, "<>": 1, "<": 1, "<=": 1, ">": 1, ">=": 1, "+": 2, "-": 2, "*": 3, "/": 3}
        while self.peek()[0] == "operator":
            op = self.peek()[1]
            level = precedence[op]
            if level < minimum_precedence:
                break
            self.take("operator")
            right = self.expression(level + 1)
            left = Binary(op, left, right)
        return left

    def primary(self) -> Expr:
        kind, value = self.peek()
        if kind == "number":
            self.take()
            if "." in value:
                try:
                    return Literal(Decimal(value))
                except InvalidOperation as error:
                    raise ExpressionError("invalid decimal") from error
            return Literal(int(value))
        if kind == "string":
            self.take()
            return Literal(value[1:-1].replace("''", "'"))
        if kind == "operator" and value == "*":
            self.take()
            return Star()
        if kind == "lparen":
            self.take()
            value_expr = self.expression()
            self.take("rparen")
            return value_expr
        if kind != "identifier":
            raise ExpressionError(f"unexpected token {value!r}")
        first = self.take()[1]
        keyword = first.upper()
        if keyword in {"TRUE", "FALSE", "NULL"}:
            return Literal({"TRUE": True, "FALSE": False, "NULL": None}[keyword])
        if self.peek()[0] == "lparen":
            self.take()
            args: list[Expr] = []
            if self.peek()[0] != "rparen":
                while True:
                    args.append(self.expression())
                    if self.peek()[0] != "comma":
                        break
                    self.take()
            self.take("rparen")
            return Call(keyword, tuple(args))
        if self.peek()[0] == "dot":
            self.take()
            second = self.take("identifier")[1]
            return Column(first, second)
        return Column(None, first)


def contains_call(expression: Expr, names: frozenset[str]) -> bool:
    return any(isinstance(node, Call) and node.name in names for node in walk(expression))


def walk(expression: Expr) -> Iterable[Expr]:
    yield expression
    if isinstance(expression, Binary):
        yield from walk(expression.left)
        yield from walk(expression.right)
    elif isinstance(expression, Call):
        for argument in expression.args:
            yield from walk(argument)
