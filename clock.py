#!/usr/bin/env python3
"""
Clock language interpreter.

Clock is a small line-oriented, object-oriented programming language using
.clk source files.
"""

from __future__ import annotations

import ast
import operator
import subprocess
import sys
import time
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


class ClockError(Exception):
    pass


class ClockDie(Exception):
    def __init__(self, message: Any):
        self.message = message


class ClockReturn(Exception):
    def __init__(self, value: Any):
        self.value = value


@dataclass
class SpecialValue:
    kind: str
    value: Any = None


class SafeEvaluator:
    """Evaluate a restricted Python-expression AST after Clock references are expanded."""

    BIN_OPS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.BitXor: operator.xor,  # Clock's xor helper.
    }
    CMP_OPS = {
        ast.Eq: operator.eq,
        ast.NotEq: operator.ne,
        ast.Gt: operator.gt,
        ast.GtE: operator.ge,
        ast.Lt: operator.lt,
        ast.LtE: operator.le,
    }
    UNARY_OPS = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
        ast.Not: operator.not_,
    }

    def __init__(self, env: dict[str, Any]):
        self.env = env

    def evaluate(self, source: str) -> Any:
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise ClockError(f"expressão inválida: {source}") from exc
        return self.visit(tree.body)

    def visit(self, node: ast.AST) -> Any:
        method = getattr(self, f"visit_{type(node).__name__}", None)
        if method is None:
            raise ClockError(f"operação não permitida na expressão: {type(node).__name__}")
        return method(node)

    def visit_Constant(self, node: ast.Constant) -> Any:
        if node.value is None or isinstance(node.value, (str, int, float, bool)):
            return node.value
        raise ClockError("constante não suportada")

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id in self.env:
            return self.env[node.id]
        if node.id == "true":
            return True
        if node.id == "false":
            return False
        if node.id == "null":
            return None
        raise ClockError(f"nome não encontrado: {node.id}")

    def visit_List(self, node: ast.List) -> list[Any]:
        return [self.visit(x) for x in node.elts]

    def visit_Tuple(self, node: ast.Tuple) -> tuple[Any, ...]:
        return tuple(self.visit(x) for x in node.elts)

    def visit_Dict(self, node: ast.Dict) -> dict[Any, Any]:
        return {self.visit(k): self.visit(v) for k, v in zip(node.keys, node.values)}

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        left = self.visit(node.left)
        right = self.visit(node.right)
        op = self.BIN_OPS.get(type(node.op))
        if op is None:
            raise ClockError("operador não suportado")
        if isinstance(node.op, ast.BitXor):
            return bool(left) != bool(right)
        try:
            return op(left, right)
        except Exception as exc:
            raise ClockError(f"erro em operação aritmética: {exc}") from exc

    def visit_BoolOp(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            result = True
            for value in node.values:
                result = self.visit(value)
                if not result:
                    return result
            return result
        if isinstance(node.op, ast.Or):
            result = False
            for value in node.values:
                result = self.visit(value)
                if result:
                    return result
            return result
        raise ClockError("operador lógico não suportado")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        op = self.UNARY_OPS.get(type(node.op))
        if op is None:
            raise ClockError("operador unário não suportado")
        return op(self.visit(node.operand))

    def visit_Compare(self, node: ast.Compare) -> bool:
        left = self.visit(node.left)
        for op_node, comparator in zip(node.ops, node.comparators):
            op = self.CMP_OPS.get(type(op_node))
            if op is None:
                raise ClockError("comparação não suportada")
            right = self.visit(comparator)
            try:
                ok = op(left, right)
            except Exception as exc:
                raise ClockError(f"erro em comparação: {exc}") from exc
            if not ok:
                return False
            left = right
        return True

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        obj = self.visit(node.value)
        key = self.visit(node.slice)
        try:
            return obj[key]
        except Exception as exc:
            raise ClockError(f"índice inválido: {exc}") from exc

    def visit_Slice(self, node: ast.Slice) -> slice:
        lower = self.visit(node.lower) if node.lower else None
        upper = self.visit(node.upper) if node.upper else None
        step = self.visit(node.step) if node.step else None
        return slice(lower, upper, step)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        obj = self.visit(node.value)
        try:
            return getattr(obj, node.attr)
        except AttributeError as exc:
            raise ClockError(f"atributo inexistente: {node.attr}") from exc

    def visit_Call(self, node: ast.Call) -> Any:
        func = self.visit(node.func)
        if not callable(func):
            raise ClockError("tentativa de chamar algo que não é função")
        args = [self.visit(a) for a in node.args]
        if node.keywords:
            raise ClockError("argumentos nomeados ainda não são suportados")
        try:
            return func(*args)
        except ClockError:
            raise
        except ClockDie:
            raise
        except Exception as exc:
            raise ClockError(f"erro em chamada: {exc}") from exc


class FunctionProxy:
    def __init__(self, interpreter: "ClockInterpreter", name: str):
        self.interpreter = interpreter
        self.name = name

    @property
    def args(self) -> list[Any]:
        frame = self.interpreter.current_frame
        if frame and frame.callable_name == self.name:
            return frame.args
        return []

    def __call__(self, *args: Any) -> Any:
        return self.interpreter.call_function(self.name, list(args))

    def __repr__(self) -> str:
        return f"<func {self.name}>"


class BoundMethodProxy:
    def __init__(self, interpreter: "ClockInterpreter", obj: "ClockObject", name: str):
        self.interpreter = interpreter
        self.obj = obj
        self.name = name

    def __call__(self, *args: Any) -> Any:
        return self.interpreter.call_method(self.obj, self.name, list(args))

    def __repr__(self) -> str:
        return f"<method me.{self.name}>"



class CallableString(str):
    """String that can also be called like a zero-argument Clock date method."""
    def __call__(self) -> str:
        return str(self)


class ClockDate:
    """System date object exposed as $.date."""

    def __init__(self):
        now = datetime.now()
        weekday_names = [
            "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
            "sexta-feira", "sábado", "domingo",
        ]
        self.day = CallableString(f"{now.day:02d}")
        self.d = self.day
        self.year = CallableString(f"{now.year:04d}")
        self.y = self.year
        self.moth = CallableString(f"{now.month:02d}")
        self.mth = self.moth
        self.second = CallableString(f"{now.second:02d}")
        self.s = self.second
        self.minute = CallableString(f"{now.minute:02d}")
        self.min = self.minute
        self.hour = CallableString(f"{now.hour:02d}")
        self.h = self.hour
        self.milisseconds = CallableString(f"{now.microsecond // 1000:03d}")
        self.ms = self.milisseconds
        self.weekday = CallableString(weekday_names[now.weekday()])
        self.wd = self.weekday

    def __repr__(self) -> str:
        return "<data $.date>"


class ClockSystem:
    COLORS = {
        "black": 30,
        "red": 31,
        "green": 32,
        "yellow": 33,
        "blue": 34,
        "magenta": 35,
        "cyan": 36,
        "white": 37,
        "gray": 90,
        "grey": 90,
        "bright-black": 90,
        "bright-red": 91,
        "bright-green": 92,
        "bright-yellow": 93,
        "bright-blue": 94,
        "bright-magenta": 95,
        "bright-cyan": 96,
        "bright-white": 97,
        "reset": 0,
    }

    def __init__(self, interpreter: "ClockInterpreter"):
        self.interpreter = interpreter

    @property
    def line(self) -> int:
        pc = self.interpreter.current_pc
        return (pc + 1) if pc is not None else 0

    @property
    def time(self) -> float:
        return (time.perf_counter() - self.interpreter.start_time) * 1000.0

    @property
    def date(self) -> ClockDate:
        return ClockDate()

    def sleep(self, milliseconds: Any = 0) -> SpecialValue:
        try:
            delay = float(milliseconds)
        except (TypeError, ValueError) as exc:
            raise ClockError("$.sleep exige milissegundos") from exc
        if delay < 0:
            raise ClockError("$.sleep não aceita tempo negativo")
        time.sleep(delay / 1000.0)
        return SpecialValue("silent")

    def die(self, message: Any = "") -> Any:
        raise ClockDie(self.interpreter._format_value(message))

    def color(self, text: Any = "", color: Any = "reset") -> SpecialValue:
        text = self.interpreter._format_value(text)
        color_name = str(color).lower()
        if color_name not in self.COLORS:
            available = ", ".join(self.COLORS)
            raise ClockError(f"cor desconhecida: {color_name}. Disponíveis: {available}")
        code = self.COLORS[color_name]
        colored = f"\x1b[{code}m{text}\x1b[0m"
        return SpecialValue("color", colored)

    def __repr__(self) -> str:
        return "<objeto $>"


class ExecClassProxy:
    """System class exposed as CLASSES.exec."""

    def __init__(self, interpreter: "ClockInterpreter"):
        self.interpreter = interpreter

    def __call__(self, *args: Any) -> "ExecObject":
        return ExecObject(self.interpreter)

    def __repr__(self) -> str:
        return "<classe CLASSES.exec>"


class ExecObject:
    def __init__(self, interpreter: "ClockInterpreter"):
        self.interpreter = interpreter

    def execute(self, command: Any = "") -> str:
        command = str(command)
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=Path.cwd(),
            )
        except OSError as exc:
            raise ClockError(f"falha ao executar terminal: {exc}") from exc
        stdout = result.stdout
        stderr = result.stderr
        if result.returncode != 0:
            detail = stderr.strip() or stdout.strip() or f"código {result.returncode}"
            raise ClockError(f"exec.execute falhou: {detail}")
        return stdout.rstrip("\n")

    def __repr__(self) -> str:
        return "<objeto exec>"


class ClockObject:
    def __init__(self, interpreter: "ClockInterpreter", class_def: "ClockClass", args: list[Any]):
        self.interpreter = interpreter
        self.class_def = class_def
        self.args = list(args)
        self.fields: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        if name in self.fields:
            return self.fields[name]
        if name in self.class_def.methods:
            return BoundMethodProxy(self.interpreter, self, name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"interpreter", "class_def", "args", "fields"}:
            object.__setattr__(self, name, value)
        else:
            self.fields[name] = value

    def __repr__(self) -> str:
        return f"<objeto {self.class_def.name}>"


class ClassNamespace:
    def __init__(self, interpreter: "ClockInterpreter"):
        self.interpreter = interpreter

    def __getattr__(self, name: str) -> Any:
        if name == "std":
            return StdClassProxy(self.interpreter)
        if name == "exec":
            return ExecClassProxy(self.interpreter)
        if name in self.interpreter.classes:
            return ClassProxy(self.interpreter, self.interpreter.classes[name])
        raise AttributeError(name)

    def __repr__(self) -> str:
        return "<CLASSES>"


class ClassProxy:
    def __init__(self, interpreter: "ClockInterpreter", class_def: "ClockClass"):
        self.interpreter = interpreter
        self.class_def = class_def

    def __call__(self, *args: Any) -> ClockObject:
        return ClockObject(self.interpreter, self.class_def, list(args))

    def __repr__(self) -> str:
        return f"<classe {self.class_def.name}>"


class StdClassProxy:
    def __init__(self, interpreter: "ClockInterpreter"):
        self.interpreter = interpreter

    def __call__(self, *args: Any) -> "StdObject":
        return StdObject()


class StdObject:
    def print(self, value: Any = "") -> SpecialValue:
        return SpecialValue("print", value)

    def erro(self, value: Any = "") -> SpecialValue:
        return SpecialValue("error", value)

    def debug(self, value: Any = "") -> SpecialValue:
        return SpecialValue("debug", value)

    def __repr__(self) -> str:
        return "<objeto std>"


@dataclass
class FunctionDef:
    name: str
    start: int
    end: int


@dataclass
class MethodDef:
    name: str
    start: int
    end: int


@dataclass
class ClockClass:
    name: str
    start: int
    end: int
    methods: dict[str, MethodDef]


@dataclass
class Frame:
    callable_name: str
    args: list[Any]
    locals: dict[str, Any]
    me: Optional[ClockObject] = None


class ClockInterpreter:
    VERSION = "0.2.0"

    def __init__(self, source: str, filename: str = "<memory>", *, debug: bool = False):
        self.filename = filename
        self.debug = debug
        self.lines = source.splitlines()
        self.functions: dict[str, FunctionDef] = {}
        self.classes: dict[str, ClockClass] = {}
        self.globals: dict[str, Any] = {}
        self.frames: list[Frame] = []
        self.pc = 0
        self.current_pc: Optional[int] = None
        self.start_time = time.perf_counter()
        self.if_pairs: dict[int, dict[str, Any]] = {}
        self.block_skip_end: dict[int, int] = {}
        self._scan_program()
        self.globals["CLASSES"] = ClassNamespace(self)
        self.globals["clock"] = ClockSystem(self)

    @property
    def current_frame(self) -> Optional[Frame]:
        return self.frames[-1] if self.frames else None

    def _clean_line(self, line: str) -> str:
        out = []
        in_string = False
        quote = ""
        ref_depth = 0
        escaped = False
        for i, ch in enumerate(line):
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                # Clock uses backslashes as ref delimiters. We keep them while
                # scanning comments; they do not alter quote state.
                continue
            if ch in {"'", '"'}:
                if in_string:
                    if ch == quote:
                        in_string = False
                else:
                    in_string = True
                    quote = ch
                out.append(ch)
                continue
            if ch == "#" and not in_string:
                break
            out.append(ch)
        return "".join(out).strip()

    def _scan_program(self) -> None:
        cleaned = [self._clean_line(x) for x in self.lines]
        self.cleaned = cleaned

        # First pass: conditional markers, function blocks and class blocks.
        stack: list[tuple[str, int, Any]] = []
        function_markers: list[tuple[int, str, bool]] = []
        class_markers: list[tuple[int, str]] = []

        for i, line in enumerate(cleaned):
            if not line:
                continue
            low = line.lower()
            if low.startswith("class "):
                class_name = line.split(None, 1)[1].strip()
                class_markers.append((i, class_name))
                stack.append(("class", i, class_name))
                continue
            if low.startswith("end "):
                if stack and stack[-1][0] == "class":
                    _, start, name = stack.pop()
                    class_markers.append((i, name))
                continue

            if low.startswith("func "):
                name = line[5:].strip()
                function_markers.append((i, name, True))
                stack.append(("func", i, name))
                continue

            # Method form inside a class: `me.foo` then `endfunc me.foo`.
            if low.startswith("me.") and "(" not in line and not low.startswith("me.args"):
                # Only accept as a method marker if the next structural close is endfunc.
                name = line[3:].strip()
                function_markers.append((i, name, False))
                stack.append(("method", i, name))
                continue

            if low.startswith("endfunc"):
                if stack and stack[-1][0] in {"func", "method"}:
                    _, start, name = stack.pop()
                    function_markers.append((i, name, True))
                continue

        # Rebuild exact function maps from matching `func` / `endfunc` pairs.
        fstack: list[tuple[int, str, str]] = []
        class_ranges: list[tuple[int, int, str]] = []
        cstack: list[tuple[int, str]] = []
        for i, line in enumerate(cleaned):
            low = line.lower()
            if low.startswith("class "):
                cstack.append((i, line.split(None, 1)[1].strip()))
            elif low.startswith("end ") and cstack:
                start, name = cstack.pop()
                class_ranges.append((start, i, name))

            if low.startswith("func "):
                fstack.append((i, line[5:].strip(), "func"))
            elif low.startswith("endfunc") and fstack:
                start, name, kind = fstack.pop()
                self.functions[name] = FunctionDef(name, start + 1, i)

        # Method blocks without the `func` keyword.
        mstack: list[tuple[int, str]] = []
        for start, end, cname in class_ranges:
            mstack.clear()
            for i in range(start + 1, end):
                line = cleaned[i]
                low = line.lower()
                if low.startswith("func me."):
                    mstack.append((i, line[8:].strip()))
                elif low.startswith("me.") and "(" not in line and not low.startswith("me.args"):
                    mstack.append((i, line[3:].strip()))
                elif low.startswith("endfunc me.") and mstack:
                    s, name = mstack.pop()
                    # Remove accidental duplicate prefix.
                    if name.startswith("me."):
                        name = name[3:]
                    if cname not in self.classes:
                        pass
                    # temporary method data attached below
            methods: dict[str, MethodDef] = {}
            mstack = []
            for i in range(start + 1, end):
                line = cleaned[i]
                low = line.lower()
                if low.startswith("func me."):
                    mstack.append((i, line[8:].strip()))
                elif low.startswith("me.") and "(" not in line and not low.startswith("me.args"):
                    mstack.append((i, line[3:].strip()))
                elif low.startswith("endfunc me."):
                    if not mstack:
                        raise ClockError(f"endfunc sem método correspondente na linha {i+1}")
                    s, name = mstack.pop()
                    name = name[3:] if name.startswith("me.") else name
                    methods[name] = MethodDef(name, s + 1, i)
            self.classes[cname] = ClockClass(cname, start, end, methods)

        # Match if / elseif / else / endif using nesting.
        istack: list[int] = []
        for i, line in enumerate(cleaned):
            low = line.lower()
            if (low.startswith("if ") or low.startswith("ifnt ")) and " do " not in low:
                istack.append(i)
            elif low.startswith("elseif ") or low == "else":
                if not istack:
                    raise ClockError(f"{line} sem if na linha {i+1}")
                # nothing else needed here; branch chains are resolved dynamically.
            elif low == "endif":
                if not istack:
                    raise ClockError(f"endif sem if na linha {i+1}")
                start = istack.pop()
                self.if_pairs[start] = {"end": i}
                # All branch markers are mapped later.
        if istack:
            raise ClockError("existe um if sem endif")

        # Build branch chain data by walking each if block.
        for start, info in list(self.if_pairs.items()):
            end = info["end"]
            branches = [start]
            else_line: Optional[int] = None
            depth = 0
            i = start + 1
            while i < end:
                low = cleaned[i].lower()
                if (low.startswith("if ") or low.startswith("ifnt ")) and " do " not in low:
                    depth += 1
                elif low == "endif":
                    depth -= 1
                elif depth == 0 and low.startswith("elseif "):
                    branches.append(i)
                elif depth == 0 and low == "else":
                    else_line = i
                i += 1
            info["branches"] = branches
            info["else"] = else_line
            for b_idx, b in enumerate(branches):
                next_branch = branches[b_idx + 1] if b_idx + 1 < len(branches) else (else_line if else_line is not None else end)
                info.setdefault("next", {})[b] = next_branch
            if else_line is not None:
                info.setdefault("next", {})[else_line] = end

    def _lookup_name(self, name: str) -> Any:
        # `me` exists only inside a method call.
        frame = self.current_frame
        if name == "me":
            if frame and frame.me is not None:
                return frame.me
            raise ClockError("`me` só existe dentro de um método")
        if frame and name in frame.locals:
            return frame.locals[name]
        if name in self.globals:
            return self.globals[name]
        if name in self.functions:
            return FunctionProxy(self, name)
        if name in self.classes or name == "std":
            if name == "std":
                return StdClassProxy(self)
        raise ClockError(f"nome não encontrado: {name}")

    def _env(self) -> dict[str, Any]:
        env = dict(self.globals)
        frame = self.current_frame
        if frame:
            env.update(frame.locals)
            if frame.me is not None:
                env["me"] = frame.me
        for name in self.functions:
            env[name] = FunctionProxy(self, name)
        # CLASSES is already in globals.
        return env

    def _expand_refs(self, expr: str) -> str:
        r"""Turn Clock's `\expr\` references into normal expression text.

        Nested references are recursively expanded with a heuristic that
        treats a backslash after an opening/operator token as an opener and a
        backslash after a value token as a closer.
        """
        def find_close(s: str, start: int) -> int:
            prev_sig = ""
            depth = 1
            i = start + 1
            while i < len(s):
                ch = s[i]
                if ch == "\\":
                    # We can distinguish nested openers from closers reasonably
                    # well from the previous significant token.
                    j = i - 1
                    while j >= start + 1 and s[j].isspace():
                        j -= 1
                    prev = s[j] if j >= start + 1 else ""
                    if depth > 0 and (prev in "([,{=:+-*/%!<>^&|" or prev == ""):
                        depth += 1
                    else:
                        depth -= 1
                        if depth == 0:
                            return i
                i += 1
            raise ClockError("referência `\\...\\` sem fechamento")

        out: list[str] = []
        i = 0
        while i < len(expr):
            if expr[i] != "\\":
                out.append(expr[i])
                i += 1
                continue
            j = find_close(expr, i)
            inner = expr[i + 1 : j]
            out.append(self._expand_refs(inner))
            i = j + 1
        return "".join(out)

    def _expand_system_alias(self, expr: str) -> str:
        """Replace Clock's `$` system namespace with the internal identifier `clock`."""
        out: list[str] = []
        in_string = False
        quote = ""
        i = 0
        while i < len(expr):
            ch = expr[i]
            if ch in {"\"", "'"}:
                if in_string and ch == quote:
                    in_string = False
                elif not in_string:
                    in_string = True
                    quote = ch
                out.append(ch)
                i += 1
                continue
            if not in_string and ch == "$":
                out.append("clock")
                i += 1
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    def eval_expr(self, expr: str) -> Any:
        expanded = self._expand_refs(expr)
        expanded = self._expand_system_alias(expanded)
        expanded = expanded.replace(" xor ", " ^ ")
        expanded = expanded.replace(" XOR ", " ^ ")
        # Permit a standalone bare `xor` at beginning/end.
        expanded = expanded.replace(" xor", " ^").replace("xor ", "^ ")
        return SafeEvaluator(self._env()).evaluate(expanded.strip())

    def _split_inline_if(self, line: str) -> tuple[str, Optional[str], bool]:
        # Find ` if ` at top level, ignoring quoted strings and backslash refs.
        in_string = False
        quote = ""
        ref_depth = 0
        i = 0
        while i < len(line):
            ch = line[i]
            if ch in {'"', "'"}:
                if in_string and ch == quote:
                    in_string = False
                elif not in_string:
                    in_string = True
                    quote = ch
                i += 1
                continue
            if not in_string and ch == "\\":
                ref_depth = 1 - ref_depth
                i += 1
                continue
            if not in_string and ref_depth == 0 and line[i : i + 4].lower() == " if ":
                return line[:i].strip(), line[i + 4 :].strip(), False
            if not in_string and ref_depth == 0 and line[i : i + 6].lower() == " ifnt ":
                return line[:i].strip(), line[i + 6 :].strip(), True
            i += 1
        return line, None, False

    def _split_do_if(self, line: str) -> tuple[Optional[str], Optional[str], bool]:
        """Parse `if CONDITION do COMMAND` / `ifnt CONDITION do COMMAND`."""
        import re
        m = re.match(r"^(ifnt|if)\s+(.+?)\s+do\s+(.+)$", line, flags=re.IGNORECASE)
        if not m:
            return None, None, False
        keyword, condition, command = m.groups()
        return command.strip(), condition.strip(), keyword.lower() == "ifnt"

    def _match_branch_end(self, start: int) -> int:
        return self.if_pairs[start]["end"]

    def call_function(self, name: str, args: list[Any]) -> Any:
        if name not in self.functions:
            raise ClockError(f"função não encontrada: {name}")
        fn = self.functions[name]
        frame = Frame(callable_name=name, args=args, locals={}, me=None)
        self.frames.append(frame)
        try:
            result = self._execute_range(fn.start, fn.end, is_function=True)
            return result
        finally:
            self.frames.pop()

    def call_method(self, obj: ClockObject, name: str, args: list[Any]) -> Any:
        method = obj.class_def.methods.get(name)
        if method is None:
            raise ClockError(f"método não encontrado: {obj.class_def.name}.{name}")
        frame = Frame(callable_name=f"me.{name}", args=args, locals={}, me=obj)
        self.frames.append(frame)
        try:
            return self._execute_range(method.start, method.end, is_function=True)
        finally:
            self.frames.pop()

    def _set_var(self, target: str, value: Any, force_global: bool = False) -> None:
        target = target.strip()
        if target.startswith("\\") and target.endswith("\\"):
            target = target[1:-1].strip()
        if target.startswith("me."):
            frame = self.current_frame
            if not frame or frame.me is None:
                raise ClockError("`me` só existe dentro de um método")
            setattr(frame.me, target[3:].strip(), value)
            return
        if "." in target or "[" in target:
            # Assignment to an existing object/array slot, e.g. obj.x or arr[0].
            self._assign_complex_target(target, value)
            return
        frame = self.current_frame
        if force_global or frame is None:
            self.globals[target] = value
        else:
            frame.locals[target] = value

    def _assign_complex_target(self, target: str, value: Any) -> None:
        # Supports obj.field and arr[index], plus obj.field[index].
        import re
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)((?:\.[A-Za-z_][A-Za-z0-9_]*)|(?:\[[^\]]+\]))+$", target)
        if not m:
            raise ClockError(f"alvo de atribuição inválido: {target}")
        root = self._lookup_name(m.group(1))
        rest = target[len(m.group(1)):]
        # Split .attr and [expr]
        token_re = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[([^\]]+)\]")
        tokens = token_re.findall(rest)
        obj = root
        for idx, (attr, sub) in enumerate(tokens):
            last = idx == len(tokens) - 1
            if attr:
                if last:
                    setattr(obj, attr, value)
                    return
                obj = getattr(obj, attr)
            else:
                key = self.eval_expr(sub)
                if last:
                    obj[key] = value
                    return
                obj = obj[key]
        raise ClockError(f"não foi possível atribuir: {target}")

    def _execute_range(self, start: int, end: int, *, is_function: bool = False) -> Any:
        pc = start
        last_value = None
        # Local condition state inside this call.
        if_stack: list[dict[str, Any]] = []
        while pc < end:
            raw = self.cleaned[pc]
            if not raw:
                pc += 1
                continue
            self.current_pc = pc
            try:
                next_pc, value, returned = self._execute_line(pc, raw, if_stack, end)
            except ClockReturn:
                raise
            if value is not None:
                last_value = value
            if returned:
                return value
            pc = next_pc
        return last_value

    def _execute_line(self, pc: int, line: str, if_stack: list[dict[str, Any]], end_limit: int) -> tuple[int, Any, bool]:
        # Structured definitions are no-ops while executing the surrounding program.
        low = line.lower()
        if low.startswith("func "):
            name = line[5:].strip()
            fn = self.functions.get(name)
            return ((fn.end + 1 if fn else pc + 1), None, False)
        if low.startswith("class "):
            name = line.split(None, 1)[1].strip()
            cls = self.classes.get(name)
            return ((cls.end + 1 if cls else pc + 1), None, False)
        if low.startswith("endfunc") or low.startswith("end "):
            return pc + 1, None, False
        if low.startswith("me.") and "(" not in line and not low.startswith("me.args"):
            # Method declaration outside class execution path.
            # If it somehow appears during direct execution, skip to matching endfunc.
            j = pc + 1
            while j < end_limit and not self.cleaned[j].lower().startswith("endfunc"):
                j += 1
            return (j + 1 if j < end_limit else end_limit), None, False

        # `if CONDITION do COMMAND` / `ifnt CONDITION do COMMAND` single-command form.
        do_command, do_cond, do_invert = self._split_do_if(line)
        if do_command is not None and do_cond is not None:
            cond = bool(self.eval_expr(do_cond))
            if do_invert:
                cond = not cond
            if cond:
                return self._execute_single_command(pc, do_command, if_stack, end_limit)
            return pc + 1, None, False

        # `... if condition` and `... ifnt condition` single-line form.
        command_part, cond_expr, invert = self._split_inline_if(line)
        if cond_expr is not None:
            cond = bool(self.eval_expr(cond_expr))
            if invert:
                cond = not cond
            return (self._execute_single_command(pc, command_part, if_stack, end_limit) if cond else (pc + 1, None, False))

        if low.startswith("ifnt ") or low.startswith("if "):
            invert = low.startswith("ifnt ")
            expr = line[5:].strip() if invert else line[3:].strip()
            cond = bool(self.eval_expr(expr))
            if invert:
                cond = not cond
            # Find matching branch/end for this if.
            info = self.if_pairs.get(pc)
            if info is None:
                raise ClockError(f"if sem endif na linha {pc+1}")
            if_stack.append({"end": info["end"], "taken": cond, "if": pc})
            if cond:
                return pc + 1, None, False
            target = info["next"][pc]
            if target == info["end"]:
                if_stack.pop()
                return info["end"] + 1, None, False
            return target, None, False

        if low.startswith("elseif "):
            if not if_stack:
                return pc + 1, None, False
            state = if_stack[-1]
            if state["taken"]:
                # Previous branch already ran: skip to endif.
                end = state["end"]
                if_stack.pop()
                return end + 1, None, False
            cond = bool(self.eval_expr(line[7:].strip()))
            state["taken"] = cond
            if cond:
                return pc + 1, None, False
            info = self.if_pairs[state["if"]]
            target = info["next"][pc]
            if target == state["end"]:
                if_stack.pop()
                return state["end"] + 1, None, False
            return target, None, False

        if low == "else":
            if not if_stack:
                return pc + 1, None, False
            state = if_stack[-1]
            if state["taken"]:
                if_stack.pop()
                return state["end"] + 1, None, False
            state["taken"] = True
            return pc + 1, None, False

        if low == "endif":
            if if_stack:
                if_stack.pop()
            return pc + 1, None, False

        return self._execute_single_command(pc, line, if_stack, end_limit)

    def _execute_single_command(self, pc: int, line: str, if_stack: list[dict[str, Any]], end_limit: int) -> tuple[int, Any, bool]:
        line = line.strip()
        low = line.lower()
        if not line:
            return pc + 1, None, False

        if low.startswith("set "):
            body = line[4:].strip()
            # split only the first ` as `
            pos = body.lower().find(" as ")
            if pos < 0:
                raise ClockError(f"set sem `as` na linha {pc+1}")
            target = body[:pos].strip()
            expr = body[pos + 4 :].strip()
            value = self.eval_expr(expr)
            self._set_var(target, value)
            return pc + 1, None, False

        if low.startswith("global "):
            body = line[7:].strip()
            pos = body.lower().find(" as ")
            if pos < 0:
                raise ClockError(f"global sem `as` na linha {pc+1}")
            target = body[:pos].strip()
            expr = body[pos + 4 :].strip()
            value = self.eval_expr(expr)
            self._set_var(target, value, force_global=True)
            return pc + 1, None, False

        if low.startswith("return"):
            expr = line[6:].strip()
            value = self.eval_expr(expr) if expr else None
            return pc + 1, value, True

        if low.startswith("get "):
            expr = line[4:].strip()
            value = self.eval_expr(expr)
            self._consume_get(value)
            return pc + 1, value, False

        if low.startswith("toline "):
            parts = line[7:].strip()
            try:
                dest = int(parts)
            except ValueError:
                raise ClockError(f"toline exige um número na linha {pc+1}")
            if dest < 1 or dest > len(self.cleaned):
                raise ClockError(f"toline fora do arquivo: {dest}")
            if_stack.clear()
            return dest - 1, None, False

        if low.startswith("retline "):
            parts = line[8:].strip()
            try:
                amount = int(parts)
            except ValueError:
                raise ClockError(f"retline exige um número na linha {pc+1}")
            if_stack.clear()
            return max(0, pc - amount), None, False

        if low.startswith("jumpline "):
            parts = line[9:].strip()
            try:
                amount = int(parts)
            except ValueError:
                raise ClockError(f"jumpline exige um número na linha {pc+1}")
            if_stack.clear()
            return min(end_limit, pc + amount + 1), None, False

        if low.startswith("execline "):
            parts = line[9:].strip()
            try:
                dest = int(parts)
            except ValueError:
                raise ClockError(f"execline exige um número na linha {pc+1}")
            if dest < 1 or dest > len(self.cleaned):
                raise ClockError(f"execline fora do arquivo: {dest}")
            # One-shot execution of simple commands. Ignore any jump returned by it.
            saved_stack = list(if_stack)
            if_stack.clear()
            try:
                _, value, returned = self._execute_line(dest - 1, self.cleaned[dest - 1], if_stack, end_limit)
            finally:
                if_stack[:] = saved_stack
            if returned and self.current_frame:
                raise ClockReturn(value)
            return pc + 1, value, False

        # Bare expression / function call is allowed as a convenience.
        try:
            value = self.eval_expr(line)
            if isinstance(value, SpecialValue):
                self._consume_get(value)
            return pc + 1, value, False
        except ClockError as exc:
            raise ClockError(f"linha {pc+1}: comando desconhecido ou inválido: {line} ({exc})") from exc

    def _consume_get(self, value: Any) -> None:
        if isinstance(value, SpecialValue):
            if value.kind == "silent":
                return
            if value.kind == "print":
                print(self._format_value(value.value))
            elif value.kind == "color":
                print(value.value)
            elif value.kind == "error":
                print(self._format_value(value.value), file=sys.stderr)
            elif value.kind == "debug":
                print(f"[DEBUG] {self._format_value(value.value)}")
            else:
                print(self._format_value(value.value))
        else:
            print(self._format_value(value))

    def _format_value(self, value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return "null"
        if isinstance(value, list):
            return "[" + ", ".join(self._format_value(v) for v in value) + "]"
        return str(value)

    def run(self) -> Any:
        try:
            return self._execute_range(0, len(self.cleaned), is_function=False)
        except ClockReturn as exc:
            return exc.value
        except ClockDie as exc:
            raise ClockError(f"programa encerrado: {exc.message}") from exc
        except ClockError as exc:
            raise ClockError(f"{self.filename}: {exc}") from exc


def usage() -> None:
    print(f"Clock {ClockInterpreter.VERSION}")
    print("Uso: Clock arquivo.clk")
    print("     Clock --version")
    print("     Clock --help")


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] in {"--help", "-h"}:
        usage()
        return 0 if len(argv) == 2 else 1
    if argv[1] in {"--version", "-v"}:
        print(ClockInterpreter.VERSION)
        return 0

    path = Path(argv[1])
    if not path.exists():
        print(f"Clock: arquivo não encontrado: {path}", file=sys.stderr)
        return 1
    if path.suffix.lower() != ".clk":
        print(f"Clock: aviso: o arquivo {path.name!r} não termina em .clk", file=sys.stderr)

    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Clock: não foi possível ler o arquivo: {exc}", file=sys.stderr)
        return 1

    try:
        ClockInterpreter(source, str(path)).run()
    except ClockError as exc:
        print(f"Clock: erro: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
